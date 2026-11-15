# -*- coding: utf-8 -*-
"""TCP stream byte accounting.

Traffic reports usually need two different numbers for the same TCP
connection:

* the bytes that actually travelled over the wire (every captured payload
  byte, including bytes carried by retransmissions), and
* the bytes the application receives for the first time, i.e. the union of
* distinct* sequence-number ranges, so overlapping retransmissions and
  re-ordered segments are only counted once.

This module keeps both ledgers per direction of a connection.  Sequence
numbers are 32-bit values that wrap around, so all bookkeeping is done on a
linearized sequence space and coverage is remembered for one full sequence
space behind the leading edge, which is enough to de-duplicate any segment a
TCP receive window (always smaller than 2**31 bytes) can legitimately cover.

Accounting rules:

* Only TCP payload counts as application bytes; Ethernet/IP/TCP headers do
  not.  SYN and FIN consume a sequence number but carry no payload, so they
  are recorded as flags and never touch either ledger.
* ``wire_bytes`` sums the payload length of every captured segment, so a
  retransmitted range is billed every time it appears on the wire.
* ``bytes`` sums sequence-number space covered for the first time.  The
  overlapping part of a retransmission adds nothing; only genuinely new
  ranges extend the union.
* Sequence-number wrap is transparent: ranges crossing the 2**32 boundary
  stay continuous.
* When the capture itself is truncated (snaplen shorter than the frame),
  only the payload bytes actually present in the capture are witnessed, so
  both ledgers describe captured evidence rather than the original wire
  sizes claimed by the IP header.
"""
from __future__ import absolute_import
from __future__ import division

import bisect

from . import dpkt
from . import ethernet
from . import ip
from . import ip6
from . import loopback
from . import pcap
from . import pcapng
from . import sll
from . import sll2
from . import tcp
from .compat import compat_ord
from .tcp import TH_FIN
from .tcp import TH_RST
from .tcp import TH_SYN

SEQ_SPACE = 1 << 32      # size of the TCP sequence-number space
SEQ_MASK = SEQ_SPACE - 1
SEQ_HALF = 1 << 31       # distances at/above this are "behind" the reference

_LINK_DECODERS = {
    pcap.DLT_EN10MB: ethernet.Ethernet,
    pcap.DLT_NULL: loopback.Loopback,
    pcap.DLT_LOOP: loopback.Loopback,
    pcap.DLT_LINUX_SLL: sll.SLL,
    pcap.DLT_LINUX_SLL2: sll2.SLL2,
}


def _raw_ip(buf):
    """Parse a bare IPv4/IPv6 datagram (DLT_RAW style captures)."""
    if not buf:
        return None
    version = compat_ord(buf[0]) >> 4
    if version == 4:
        return ip.IP(buf)
    if version == 6:
        return ip6.IP6(buf)
    return None


class TCPDirection(object):
    """One direction of a TCP flow.

    Attributes:
        isn: raw sequence number of the first observed segment, or None
        segments: number of TCP segments observed in this direction
        syns: number of segments carrying SYN
        fins: number of segments carrying FIN
        rsts: number of segments carrying RST
        wire_bytes: payload bytes observed on the wire, retransmissions
            counted every time they are captured
        bytes: distinct payload bytes, i.e. the size of the union of all
            captured sequence-number ranges
    """

    def __init__(self):
        self.isn = None
        self.segments = 0
        self.syns = 0
        self.fins = 0
        self.rsts = 0
        self.wire_bytes = 0
        self.bytes = 0
        # (raw sequence number, linear sequence number) anchor used to
        # translate wrapping 32-bit sequence numbers onto a linear axis
        self._ref_raw = None
        self._ref_lin = 0
        # merged, sorted [start, end) intervals on the linear axis
        self._ranges = []

    @property
    def retrans_bytes(self):
        """Captured payload bytes that duplicated an already-seen range."""
        return self.wire_bytes - self.bytes

    def covered_ranges(self):
        """Return a copy of the covered linear intervals as (start, end) tuples."""
        return [(start, end) for start, end in self._ranges]

    def _linearize(self, raw_seq, length):
        """Map a raw segment [seq, seq+length) onto the linear axis."""
        if self._ref_raw is None:
            self._ref_raw = raw_seq
            self._ref_lin = 0
        delta = (raw_seq - self._ref_raw) & SEQ_MASK
        if delta >= SEQ_HALF:
            delta -= SEQ_SPACE
        lo = self._ref_lin + delta
        hi = lo + length
        # advance the anchor to the leading edge so the next segment keeps
        # mapping correctly across wraps
        if hi > self._ref_lin:
            self._ref_raw = (raw_seq + length) & SEQ_MASK
            self._ref_lin = hi
        return lo, hi

    def _add_range(self, lo, hi):
        """Merge [lo, hi) into the covered intervals; return newly covered bytes."""
        if lo >= hi:
            return 0
        # forget intervals more than a full sequence space behind, their raw
        # sequence numbers can no longer collide with anything current
        floor = lo - SEQ_SPACE
        if self._ranges and self._ranges[0][1] <= floor:
            self._ranges = [(start, end) for start, end in self._ranges if end > floor]

        added = hi - lo
        index = bisect.bisect_left(self._ranges, (lo, -1))

        # merge with intervals extending into [lo, hi) from the left
        while index > 0 and self._ranges[index - 1][1] >= lo:
            start, end = self._ranges.pop(index - 1)
            index -= 1
            overlap = min(end, hi) - max(start, lo)
            if overlap > 0:
                added -= overlap
            lo = min(lo, start)
            hi = max(hi, end)

        # merge with intervals starting at or before hi on the right
        while index < len(self._ranges) and self._ranges[index][0] <= hi:
            start, end = self._ranges.pop(index)
            overlap = min(end, hi) - start
            if overlap > 0:
                added -= overlap
            hi = max(hi, end)

        self._ranges.insert(index, (lo, hi))
        return added

    def update(self, segment):
        """Account for one :class:`dpkt.tcp.TCP` segment; return new bytes added."""
        if self.isn is None:
            self.isn = segment.seq
        self.segments += 1
        if segment.flags & TH_SYN:
            self.syns += 1
        if segment.flags & TH_FIN:
            self.fins += 1
        if segment.flags & TH_RST:
            self.rsts += 1

        length = len(segment.data)
        self.wire_bytes += length
        if not length:
            return 0

        lo, hi = self._linearize(segment.seq, length)
        added = self._add_range(lo, hi)
        self.bytes += added
        return added


class TCPFlow(object):
    """A single TCP connection with both directions of traffic.

    The key is the 4-tuple ``(src_ip, src_port, dst_ip, dst_port)`` of the
    first observed segment; that side is the client-to-server (``c2s``)
    direction and the reverse side is ``s2c``.
    """

    def __init__(self, key):
        self.key = key
        self.c2s = TCPDirection()
        self.s2c = TCPDirection()
        self.packets = 0
        self.start_time = None
        self.last_time = None

    @property
    def src(self):
        return self.key[0]

    @property
    def sport(self):
        return self.key[1]

    @property
    def dst(self):
        return self.key[2]

    @property
    def dport(self):
        return self.key[3]

    @property
    def wire_bytes(self):
        return self.c2s.wire_bytes + self.s2c.wire_bytes

    @property
    def bytes(self):
        return self.c2s.bytes + self.s2c.bytes

    @property
    def retrans_bytes(self):
        return self.c2s.retrans_bytes + self.s2c.retrans_bytes

    def _direction_for(self, layer3, segment):
        if (layer3.src, segment.sport) == (self.key[0], self.key[1]):
            return self.c2s
        return self.s2c

    def update(self, layer3, segment=None, ts=None):
        """Account for one IP/IP6 datagram carrying a TCP segment."""
        if segment is None:
            segment = layer3.data
        if not isinstance(segment, tcp.TCP):
            return 0
        direction = self._direction_for(layer3, segment)
        added = direction.update(segment)
        self.packets += 1
        if ts is not None:
            if self.start_time is None or ts < self.start_time:
                self.start_time = ts
            if self.last_time is None or ts > self.last_time:
                self.last_time = ts
        return added

    def report(self):
        """Return a plain-dict snapshot suitable for a traffic report."""
        def snapshot(direction):
            return {
                'segments': direction.segments,
                'syns': direction.syns,
                'fins': direction.fins,
                'rsts': direction.rsts,
                'wire_bytes': direction.wire_bytes,
                'bytes': direction.bytes,
                'retrans_bytes': direction.retrans_bytes,
            }
        return {
            'src': self.src,
            'sport': self.sport,
            'dst': self.dst,
            'dport': self.dport,
            'packets': self.packets,
            'start_time': self.start_time,
            'last_time': self.last_time,
            'c2s': snapshot(self.c2s),
            's2c': snapshot(self.s2c),
            'wire_bytes': self.wire_bytes,
            'bytes': self.bytes,
            'retrans_bytes': self.retrans_bytes,
        }


class TCPStreamTracker(object):
    """Track TCP flows and their two byte ledgers across a capture."""

    def __init__(self):
        self.flows = {}

    @staticmethod
    def _flow_key(layer3, segment):
        return layer3.src, segment.sport, layer3.dst, segment.dport

    def update(self, layer3, segment=None, ts=None):
        """Account for one parsed IP/IP6 datagram; return its flow (or None)."""
        if segment is None:
            segment = getattr(layer3, 'data', None)
        if not isinstance(segment, tcp.TCP):
            return None

        key = self._flow_key(layer3, segment)
        flow = self.flows.get(key)
        if flow is None:
            reverse_key = (layer3.dst, segment.dport, layer3.src, segment.sport)
            flow = self.flows.get(reverse_key)
            if flow is None:
                flow = TCPFlow(key)
                self.flows[key] = flow
        flow.update(layer3, segment, ts)
        return flow

    def update_frame(self, buf, linktype=None):
        """Account for one link-layer frame buffer; return its flow (or None)."""
        layer3 = self._decode_layer3(buf, linktype)
        if layer3 is None:
            return None
        return self.update(layer3)

    def update_pcap(self, source):
        """Consume a pcap/pcapng reader or an open capture file object.

        A reader is anything iterable yielding ``(timestamp, buf)`` tuples
        that also exposes ``datalink()`` (e.g. :class:`dpkt.pcap.Reader` or
        :class:`dpkt.pcapng.Reader`).  A plain file object is opened as pcap
        first and, failing that, as pcapng.
        """
        if hasattr(source, 'datalink'):
            reader = source
        else:
            source.seek(0)
            try:
                reader = pcap.Reader(source)
            except (ValueError, dpkt.NeedData, dpkt.UnpackError):
                source.seek(0)
                reader = pcapng.Reader(source)
        linktype = reader.datalink()
        for ts, buf in reader:
            self.update_frame(buf, linktype)
        return self

    @staticmethod
    def _decode_layer3(buf, linktype=None):
        if linktype is None:
            try:
                layer3 = ethernet.Ethernet(buf).data
            except (dpkt.UnpackError, dpkt.NeedData):
                layer3 = None
            if isinstance(layer3, (ip.IP, ip6.IP6)):
                return layer3
            return _raw_ip(buf)

        if linktype == pcap.DLT_RAW:
            return _raw_ip(buf)
        decoder = _LINK_DECODERS.get(linktype)
        if decoder is None:
            return None
        try:
            layer3 = decoder(buf).data
        except (dpkt.UnpackError, dpkt.NeedData):
            return None
        # Loopback falls back to Ethernet for unfamiliar address families
        while isinstance(layer3, ethernet.Ethernet):
            layer3 = layer3.data
        if isinstance(layer3, (ip.IP, ip6.IP6)):
            return layer3
        return None


def track_pcap(source):
    """Convenience wrapper: build a tracker and consume one capture."""
    return TCPStreamTracker().update_pcap(source)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def _ip_tcp(src, dst, sport, dport, seq, payload=b'', flags=tcp.TH_ACK):
    segment = tcp.TCP(sport=sport, dport=dport, seq=seq, flags=flags, data=payload)
    return ip.IP(src=src, dst=dst, p=6, data=segment)


SRC = b'\x0a\x00\x00\x01'
DST = b'\x0a\x00\x00\x02'


def test_wire_vs_unique_bytes():
    tracker = TCPStreamTracker()
    first = _ip_tcp(SRC, DST, 1234, 80, 1000, b'0123456789')
    flow = tracker.update(first)
    # the same ten bytes retransmitted, plus ten new ones
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 1000, b'0123456789'))
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 1010, b'abcdefghij'))
    assert flow.c2s.wire_bytes == 30
    assert flow.c2s.bytes == 20
    assert flow.c2s.retrans_bytes == 10
    assert flow.packets == 3


def test_overlapping_retransmission_only_counts_new_range():
    tracker = TCPStreamTracker()
    flow = tracker.update(_ip_tcp(SRC, DST, 1234, 80, 0, b'\x00' * 10))
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 5, b'\x01' * 10))
    # [0,10) plus [5,15): 20 on the wire, 15 distinct
    assert flow.c2s.wire_bytes == 20
    assert flow.c2s.bytes == 15
    assert flow.c2s.covered_ranges() == [(0, 15)]


def test_out_of_order_segments_merge():
    tracker = TCPStreamTracker()
    flow = tracker.update(_ip_tcp(SRC, DST, 1234, 80, 10, b'\x02' * 10))
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 0, b'\x01' * 10))
    assert flow.c2s.wire_bytes == 20
    assert flow.c2s.bytes == 20
    # linear origin is the first observed seq (10), so raw seq 0 maps to -10
    assert flow.c2s.covered_ranges() == [(-10, 10)]


def test_gaps_are_not_invented():
    tracker = TCPStreamTracker()
    flow = tracker.update(_ip_tcp(SRC, DST, 1234, 80, 0, b'\x01' * 4))
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 10, b'\x02' * 4))
    assert flow.c2s.bytes == 8
    assert flow.c2s.covered_ranges() == [(0, 4), (10, 14)]
    # filling the hole extends the union without touching the wire ledger
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 4, b'\x03' * 6))
    assert flow.c2s.wire_bytes == 14
    assert flow.c2s.bytes == 14
    assert flow.c2s.covered_ranges() == [(0, 14)]


def test_syn_and_fin_consume_seq_but_no_payload():
    tracker = TCPStreamTracker()
    flow = tracker.update(_ip_tcp(SRC, DST, 1234, 80, 1000, flags=tcp.TH_SYN))
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 1001, b'hello'))
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 1006, flags=tcp.TH_FIN))
    direction = flow.c2s
    assert direction.syns == 1
    assert direction.fins == 1
    assert direction.isn == 1000
    assert direction.wire_bytes == 5
    assert direction.bytes == 5
    assert direction.covered_ranges() == [(0, 5)]


def test_sequence_wrap_stays_continuous():
    tracker = TCPStreamTracker()
    # 32 bytes starting six below the wrap: this segment itself crosses 0
    flow = tracker.update(_ip_tcp(SRC, DST, 1234, 80, SEQ_MASK - 5, b'\x01' * 32))
    # next new byte is raw seq 26 (2**32 - 6 + 32 == 26 modulo 2**32)
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 26, b'\x02' * 16))
    assert flow.c2s.bytes == 48
    assert flow.c2s.covered_ranges() == [(0, 48)]
    # retransmissions on both sides of the wrap add nothing new
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, SEQ_MASK - 3, b'\x03' * 8))
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 10, b'\x04' * 8))
    assert flow.c2s.bytes == 48
    assert flow.c2s.wire_bytes == 32 + 16 + 8 + 8
    assert flow.c2s.retrans_bytes == 16


def test_reverse_directions_are_separate():
    tracker = TCPStreamTracker()
    flow = tracker.update(_ip_tcp(SRC, DST, 1234, 80, 0, b'\x01' * 7))
    same_flow = tracker.update(_ip_tcp(DST, SRC, 80, 1234, 500, b'\x02' * 3))
    assert same_flow is flow
    assert len(tracker.flows) == 1
    assert flow.c2s.bytes == 7
    assert flow.s2c.bytes == 3
    assert flow.bytes == 10
    assert flow.wire_bytes == 10


def test_multiple_flows_are_keyed_independently():
    tracker = TCPStreamTracker()
    tracker.update(_ip_tcp(SRC, DST, 1234, 80, 0, b'\x01' * 4))
    tracker.update(_ip_tcp(SRC, DST, 2222, 80, 0, b'\x02' * 6))
    assert len(tracker.flows) == 2
    assert sorted(flow.bytes for flow in tracker.flows.values()) == [4, 6]


def test_non_tcp_is_ignored():
    from . import udp
    datagram = ip.IP(src=SRC, dst=DST, p=17, data=udp.UDP(sport=1, dport=2, data=b'x' * 4))
    tracker = TCPStreamTracker()
    assert tracker.update(datagram) is None
    assert tracker.flows == {}


def test_truncated_frame_only_bills_captured_bytes():
    payload = b'x' * 20
    datagram = _ip_tcp(SRC, DST, 1234, 80, 0, payload)
    frame = bytes(ethernet.Ethernet(
        src=b'\x00' * 6, dst=b'\x00' * 6, type=ethernet.ETH_TYPE_IP, data=datagram))

    full = TCPStreamTracker()
    full_flow = full.update_frame(frame, pcap.DLT_EN10MB)
    assert full_flow.c2s.wire_bytes == 20
    assert full_flow.c2s.bytes == 20

    # snaplen cut five payload bytes off the captured frame
    cut = TCPStreamTracker()
    cut_flow = cut.update_frame(frame[:-5], pcap.DLT_EN10MB)
    assert cut_flow.c2s.wire_bytes == 15
    assert cut_flow.c2s.bytes == 15


def test_pcap_round_trip():
    import io
    frame = bytes(ethernet.Ethernet(
        src=b'\x00' * 6, dst=b'\x00' * 6, type=ethernet.ETH_TYPE_IP,
        data=_ip_tcp(SRC, DST, 1234, 80, 0, b'\x01' * 10)))
    buffer = io.BytesIO()
    writer = pcap.Writer(buffer, linktype=pcap.DLT_EN10MB)
    writer.writepkt(frame)
    writer.writepkt(frame)  # retransmission
    buffer.seek(0)

    tracker = track_pcap(buffer)
    assert len(tracker.flows) == 1
    flow = next(iter(tracker.flows.values()))
    assert flow.c2s.wire_bytes == 20
    assert flow.c2s.bytes == 10
    assert flow.c2s.retrans_bytes == 10
    assert flow.report()['c2s']['wire_bytes'] == 20


def test_raw_ip_frame():
    datagram = bytes(_ip_tcp(SRC, DST, 1234, 80, 0, b'abc'))
    tracker = TCPStreamTracker()
    flow = tracker.update_frame(datagram, pcap.DLT_RAW)
    assert flow.c2s.bytes == 3
    assert tracker.update_frame(b'') is None
