# -*- coding: utf-8 -*-
"""Per-source UDP payload accounting for packet captures.

The report deliberately keeps three independent byte counts:

``declared_payload_bytes``
    Payload bytes declared by the UDP length field (``ulen - 8``).  When the
    UDP length field is zero the value is interpreted according to the IP
    version; the derivation and the original field values are retained as
    evidence on the packet record.

``captured_payload_bytes``
    Payload bytes that are actually present in the capture and belong to the
    declared UDP datagram.  This is never inflated to the declared size, and
    bytes outside the declared datagram (link padding or an overstated IP
    length) are not counted.

``decodable_payload_bytes``
    Payload bytes from datagrams whose declared payload was captured
    completely and which are not IP fragments.  A datagram left short by the
    capture contributes zero here; its missing bytes are reported as
    ``truncated_payload_bytes`` instead of being treated as packet loss.  A
    datagram left short only because the length fields contradict each other
    also contributes zero here, but is reported as a length conflict rather
    than capture truncation.

When the IP total length, the UDP length and the capture extent disagree, the
raw values are all preserved and a conflict code is attached to the packet.
The report never substitutes ``max(ip_length, udp_length, captured_length)``
for a missing length.

Zero-length fields are version specific:

* IPv4 total length ``0`` means "not supplied" (normally TCP segmentation
  offload), so the capture extent is used only for availability, not as a
  declared value.  IPv4 has no jumbogram, therefore UDP length ``0`` is
  treated as an omitted length and derived from the IPv4 payload length.
* IPv6 payload length ``0`` is a jumbogram marker when a Hop-by-Hop Jumbo
  Payload option is present.  Without that option it is treated as an
  omitted/offload length.  IPv6 UDP length ``0`` is the jumbogram encoding;
  without a jumbo option it is malformed but the IPv6 payload length is still
  retained as evidence.
"""

from __future__ import absolute_import
from __future__ import print_function

import csv
import os
from decimal import Decimal
import socket
import struct
import sys
from collections import Counter, OrderedDict

try:
    from io import StringIO
except ImportError:  # pragma: no cover - Python 2
    from StringIO import StringIO

from . import pcap
from . import pcapng
from .compat import compat_ord


# Path-like values accepted by ``analyze``.  ``os.PathLike`` does not exist
# on Python 2, which dpkt still supports.
_PATH_TYPES = (str, bytes) + ((os.PathLike,) if hasattr(os, 'PathLike') else ())


# Conflict codes.  They are stable strings so callers can persist them without
# depending on the Python class layout.
IP_HEADER_TRUNCATED = 'ip_header_truncated'
IP_HEADER_LEN_INVALID = 'ip_header_len_invalid'
IP_TOTAL_LEN_ZERO = 'ip_total_len_zero'
IP_TOTAL_LEN_BELOW_HEADER = 'ip_total_len_below_header'
IP_TRUNCATED_BY_CAPTURE = 'ip_truncated_by_capture'
CAPTURE_EXCEEDS_IP_TOTAL_LEN = 'capture_exceeds_ip_total_len'
IPV6_PAYLOAD_LEN_ZERO = 'ipv6_payload_len_zero'
IPV6_EXTENSION_TRUNCATED = 'ipv6_extension_truncated'
IPV6_JUMBO_LENGTH_INVALID = 'ipv6_jumbo_length_invalid'
UDP_HEADER_TRUNCATED = 'udp_header_truncated'
UDP_LEN_BELOW_HEADER = 'udp_len_below_header'
UDP_LEN_ZERO_IPV4 = 'udp_len_zero_ipv4'
UDP_LEN_ZERO_IPV6_NON_JUMBO = 'udp_len_zero_ipv6_non_jumbo'
UDP_LEN_EXCEEDS_IP_PAYLOAD = 'udp_len_exceeds_ip_payload'
IP_PAYLOAD_EXCEEDS_UDP_LEN = 'ip_payload_exceeds_udp_len'
CAPTURE_EXCEEDS_UDP_LEN = 'capture_exceeds_udp_len'
UDP_PAYLOAD_TRUNCATED_BY_CAPTURE = 'udp_payload_truncated_by_capture'
FRAGMENTED_DATAGRAM = 'fragmented_datagram'
NON_INITIAL_FRAGMENT = 'non_initial_fragment'
FRAGMENT_PAYLOAD_INCOMPLETE = 'fragment_payload_incomplete'
UNSUPPORTED_LINKTYPE = 'unsupported_linktype'
NON_IP_PACKET = 'non_ip_packet'
NON_UDP_PACKET = 'non_udp_packet'
TRUNCATED_LINK_HEADER = 'truncated_link_header'
CAPTURE_TRUNCATED_BY_SNAPLEN = 'capture_truncated_by_snaplen'
WIRE_LEN_BELOW_CAPTURED_LEN = 'wire_len_below_captured_len'


_STATUS_COMPLETE = 'complete'
_STATUS_TRUNCATED = 'truncated'
_STATUS_FRAGMENTED = 'fragmented'
_STATUS_MALFORMED = 'malformed'


def _u16(buf, off):
    return struct.unpack('>H', buf[off:off + 2])[0]


def _u32(buf, off):
    return struct.unpack('>I', buf[off:off + 4])[0]


def _inet_ntop(addr):
    """Convert packed IPv4/IPv6 address bytes to text without importing DNS."""
    family = socket.AF_INET if len(addr) == 4 else socket.AF_INET6
    return socket.inet_ntop(family, addr)


def _as_bytes(buf):
    if isinstance(buf, bytearray):
        return bytes(buf)
    return buf


class PacketRecord(object):
    """One UDP packet (or one IP fragment of a UDP datagram).

    All length attributes are byte counts and may be ``None`` when the
    corresponding field was not present in the capture.  The raw values are
    intentionally not normalised: for example ``udp_declared_total_len`` can
    be greater than ``ip_declared_payload_len``.  Use :attr:`conflicts` and
    :attr:`conflict_evidence` to inspect such disagreements.
    """

    def __init__(self):
        self.index = None
        self.timestamp = None
        self.linktype = None
        self.frame_len = 0
        self.wire_len = None
        self.ip_offset = None

        self.ip_version = None
        self.is_udp = False
        self.src = None
        self.dst = None
        self.src_port = None
        self.dst_port = None

        self.ip_header_len = None
        self.ip_declared_total_len = None
        self.ip_captured_total_len = None
        self.ip_declared_payload_len = None
        self.ip_captured_payload_len = None
        self.ip_payload_len_source = None

        self.ipv6_payload_len = None
        self.ipv6_jumbo_payload_len = None
        self.ipv6_extension_len = None
        self.ipv6_extension_captured_len = None

        self.udp_header_captured = False
        self.udp_declared_total_len = None
        self.udp_declared_payload_len = None
        self.declared_payload_len = None
        self.declared_payload_source = None

        self.captured_payload_len = 0
        self.capture_excess_len = 0
        self.decodable_payload_len = 0
        self.truncated_payload_len = 0
        self.fragment_payload_len = 0

        self.fragmented = False
        self.non_initial_fragment = False
        self.complete = False
        self.status = _STATUS_MALFORMED
        self.conflicts = []
        self.conflict_evidence = {}

    # Short aliases used by report consumers.
    @property
    def declared_bytes(self):
        return self.declared_payload_len

    @property
    def captured_bytes(self):
        return self.captured_payload_len

    @property
    def decodable_bytes(self):
        return self.decodable_payload_len

    @property
    def truncated_bytes(self):
        return self.truncated_payload_len

    def add_conflict(self, code, **evidence):
        if code not in self.conflicts:
            self.conflicts.append(code)
        if evidence:
            self.conflict_evidence.setdefault(code, {}).update(evidence)

    def as_dict(self):
        return {
            'index': self.index,
            'timestamp': self.timestamp,
            'linktype': self.linktype,
            'frame_len': self.frame_len,
            'wire_len': self.wire_len,
            'ip_offset': self.ip_offset,
            'ip_version': self.ip_version,
            'is_udp': self.is_udp,
            'src': self.src,
            'dst': self.dst,
            'src_port': self.src_port,
            'dst_port': self.dst_port,
            'ip_header_len': self.ip_header_len,
            'ip_declared_total_len': self.ip_declared_total_len,
            'ip_captured_total_len': self.ip_captured_total_len,
            'ip_declared_payload_len': self.ip_declared_payload_len,
            'ip_captured_payload_len': self.ip_captured_payload_len,
            'ip_payload_len_source': self.ip_payload_len_source,
            'ipv6_payload_len': self.ipv6_payload_len,
            'ipv6_jumbo_payload_len': self.ipv6_jumbo_payload_len,
            'ipv6_extension_len': self.ipv6_extension_len,
            'ipv6_extension_captured_len': self.ipv6_extension_captured_len,
            'udp_header_captured': self.udp_header_captured,
            'udp_declared_total_len': self.udp_declared_total_len,
            'udp_declared_payload_len': self.udp_declared_payload_len,
            'declared_payload_len': self.declared_payload_len,
            'declared_payload_source': self.declared_payload_source,
            'captured_payload_len': self.captured_payload_len,
            'capture_excess_len': self.capture_excess_len,
            'decodable_payload_len': self.decodable_payload_len,
            'truncated_payload_len': self.truncated_payload_len,
            'fragment_payload_len': self.fragment_payload_len,
            'fragmented': self.fragmented,
            'non_initial_fragment': self.non_initial_fragment,
            'complete': self.complete,
            'status': self.status,
            'conflicts': list(self.conflicts),
            'conflict_evidence': dict(self.conflict_evidence),
        }

    def __repr__(self):
        return ('PacketRecord(index=%r, src=%r, sport=%r, declared=%r, '
                'captured=%r, decodable=%r, status=%r, conflicts=%r)' %
                (self.index, self.src, self.src_port, self.declared_payload_len,
                 self.captured_payload_len, self.decodable_payload_len,
                 self.status, self.conflicts))


class SourceStats(object):
    """Aggregated UDP payload accounting for one source address."""

    def __init__(self, source, ip_version=None):
        self.source = source
        self.ip_version = ip_version

        self.packets = 0
        self.datagrams = 0
        self.complete_packets = 0
        self.truncated_packets = 0
        self.fragmented_packets = 0
        self.non_initial_fragments = 0
        self.malformed_packets = 0
        self.unknown_declared_packets = 0

        self.declared_payload_bytes = 0
        self.captured_payload_bytes = 0
        self.decodable_payload_bytes = 0
        self.truncated_payload_bytes = 0
        self.capture_excess_bytes = 0
        self.fragment_payload_bytes = 0

        self.conflicts = Counter()

    @property
    def declared_bytes(self):
        return self.declared_payload_bytes

    @property
    def captured_bytes(self):
        return self.captured_payload_bytes

    @property
    def decodable_bytes(self):
        return self.decodable_payload_bytes

    @property
    def truncated_bytes(self):
        return self.truncated_payload_bytes

    def add(self, record):
        if record.src is None or not record.is_udp:
            return
        self.packets += 1
        if self.ip_version is None:
            self.ip_version = record.ip_version

        if record.non_initial_fragment:
            self.non_initial_fragments += 1
            self.fragment_payload_bytes += record.fragment_payload_len
        elif record.udp_header_captured:
            self.datagrams += 1
            if record.declared_payload_len is None:
                self.unknown_declared_packets += 1
            else:
                self.declared_payload_bytes += record.declared_payload_len
            self.captured_payload_bytes += record.captured_payload_len
            self.decodable_payload_bytes += record.decodable_payload_len
            self.truncated_payload_bytes += record.truncated_payload_len
            self.capture_excess_bytes += record.capture_excess_len
            if record.fragment_payload_len:
                self.fragment_payload_bytes += record.fragment_payload_len

        if record.complete:
            self.complete_packets += 1
        elif record.fragmented:
            self.fragmented_packets += 1
        elif record.status == _STATUS_TRUNCATED:
            self.truncated_packets += 1
        elif record.status == _STATUS_MALFORMED:
            self.malformed_packets += 1

        for code in record.conflicts:
            self.conflicts[code] += 1

    def as_dict(self):
        return {
            'source': self.source,
            'ip_version': self.ip_version,
            'packets': self.packets,
            'datagrams': self.datagrams,
            'complete_packets': self.complete_packets,
            'truncated_packets': self.truncated_packets,
            'fragmented_packets': self.fragmented_packets,
            'non_initial_fragments': self.non_initial_fragments,
            'malformed_packets': self.malformed_packets,
            'unknown_declared_packets': self.unknown_declared_packets,
            'declared_payload_bytes': self.declared_payload_bytes,
            'captured_payload_bytes': self.captured_payload_bytes,
            'decodable_payload_bytes': self.decodable_payload_bytes,
            'truncated_payload_bytes': self.truncated_payload_bytes,
            'capture_excess_bytes': self.capture_excess_bytes,
            'fragment_payload_bytes': self.fragment_payload_bytes,
            'conflicts': dict(self.conflicts),
        }

    def __repr__(self):
        return ('SourceStats(source=%r, packets=%r, declared=%r, captured=%r, '
                'decodable=%r, truncated=%r)' %
                (self.source, self.packets, self.declared_payload_bytes,
                 self.captured_payload_bytes, self.decodable_payload_bytes,
                 self.truncated_payload_bytes))


class UDPReport(object):
    """UDP payload report grouped by source address.

    ``report.sources`` maps a printable source address to :class:`SourceStats`.
    ``report.records`` keeps per-packet evidence, including raw IP/UDP length
    fields for every conflict.  ``report.totals`` aggregates all sources.
    """

    def __init__(self):
        self.sources = OrderedDict()
        self.records = []
        self.totals = SourceStats('*')
        self.errors = []

    def add(self, record):
        self.records.append(record)
        if record.src is None or not record.is_udp:
            return
        stats = self.sources.get(record.src)
        if stats is None:
            stats = SourceStats(record.src, record.ip_version)
            self.sources[record.src] = stats
        stats.add(record)
        self.totals.add(record)

    @property
    def summary(self):
        return self.totals.as_dict()

    def rows(self):
        return list(self.sources.values())

    def __iter__(self):
        return iter(self.sources.values())

    def __len__(self):
        return len(self.sources)

    def __getitem__(self, source):
        return self.sources[source]

    def as_dict(self, include_records=True):
        data = {
            'totals': self.totals.as_dict(),
            'sources': [stats.as_dict() for stats in self.sources.values()],
        }
        if include_records:
            data['records'] = [record.as_dict() for record in self.records]
        data['errors'] = list(self.errors)
        return data

    def format_text(self):
        """Return a human-readable table, one row per source address."""
        headers = ('Source', 'IP', 'Packets', 'Datagrams', 'Declared',
                   'Captured', 'Decodable', 'Truncated', 'Conflicts')
        rows = [headers]
        for stats in self.sources.values():
            rows.append((
                stats.source,
                'v%d' % stats.ip_version if stats.ip_version else '?',
                str(stats.packets),
                str(stats.datagrams),
                str(stats.declared_payload_bytes),
                str(stats.captured_payload_bytes),
                str(stats.decodable_payload_bytes),
                str(stats.truncated_payload_bytes),
                str(sum(stats.conflicts.values())),
            ))
        totals = self.totals
        rows.append((
            'TOTAL',
            '',
            str(totals.packets),
            str(totals.datagrams),
            str(totals.declared_payload_bytes),
            str(totals.captured_payload_bytes),
            str(totals.decodable_payload_bytes),
            str(totals.truncated_payload_bytes),
            str(sum(totals.conflicts.values())),
        ))

        widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
        lines = []
        for row in rows:
            lines.append('  '.join(value.ljust(widths[i])
                                   for i, value in enumerate(row)).rstrip())
        return '\n'.join(lines)

    def to_csv(self):
        out = StringIO()
        writer = csv.writer(out, lineterminator='\n')
        writer.writerow(('source', 'ip_version', 'packets', 'datagrams',
                         'declared_payload_bytes', 'captured_payload_bytes',
                         'decodable_payload_bytes', 'truncated_payload_bytes',
                         'capture_excess_bytes', 'conflicts'))
        for stats in self.sources.values():
            writer.writerow((stats.source, stats.ip_version, stats.packets,
                             stats.datagrams, stats.declared_payload_bytes,
                             stats.captured_payload_bytes,
                             stats.decodable_payload_bytes,
                             stats.truncated_payload_bytes,
                             stats.capture_excess_bytes,
                             ';'.join(sorted(stats.conflicts))))
        return out.getvalue()

    def __repr__(self):
        return 'UDPReport(sources=%d, records=%d)' % (len(self.sources), len(self.records))


_SUPPORTED_LINKTYPES = frozenset((
    pcap.DLT_RAW, pcap.DLT_NULL, pcap.DLT_LOOP, pcap.DLT_EN10MB,
    pcap.DLT_LINUX_SLL, pcap.DLT_LINUX_SLL2, pcap.DLT_PPP,
    pcap.DLT_PPP_SERIAL, pcap.DLT_IPV4, pcap.DLT_IPV6,
))


def _is_supported_linktype(linktype):
    return linktype in _SUPPORTED_LINKTYPES or linktype in (14, 101)


def _link_offset(frame, linktype):
    """Return the offset of the IP header, or ``None`` if unsupported."""
    if linktype in (pcap.DLT_RAW, 14, 101, pcap.DLT_IPV4, pcap.DLT_IPV6):
        return 0
    if linktype in (pcap.DLT_NULL, pcap.DLT_LOOP):
        return 4 if len(frame) >= 4 else None
    if linktype == pcap.DLT_LINUX_SLL:
        return 16 if len(frame) >= 16 else None
    if linktype == pcap.DLT_LINUX_SLL2:
        return 20 if len(frame) >= 20 else None
    if linktype == pcap.DLT_EN10MB:
        if len(frame) < 14:
            return None
        offset = 14
        ethertype = _u16(frame, 12)
        while ethertype in (0x8100, 0x88a8, 0x9100, 0x9200):
            if len(frame) < offset + 4:
                return None
            ethertype = _u16(frame, offset + 2)
            offset += 4
        if ethertype == 0x8864:  # PPPoE session
            if len(frame) < offset + 8:
                return None
            ppp_proto = _u16(frame, offset + 6)
            offset += 8
            if ppp_proto in (0x0021, 0x0057):
                return offset
            return None
        if ethertype in (0x8847, 0x8848):  # MPLS
            while len(frame) >= offset + 4:
                label = _u32(frame, offset)
                offset += 4
                if label & 0x100:
                    break
            else:
                return None
            if len(frame) <= offset:
                return None
            version = compat_ord(frame[offset]) >> 4
            return offset if version in (4, 6) else None
        return offset if ethertype in (0x0800, 0x86dd) else None
    # A few PPP variants have a 4-byte header before the IP payload.
    if linktype in (pcap.DLT_PPP, pcap.DLT_PPP_SERIAL):
        return 4 if len(frame) >= 4 else None
    return None


def _ipv6_jumbo_option(buf, start, end):
    """Return the Jumbo Payload option value, or ``None``.

    ``buf`` is the full frame, ``start`` points at the first option byte and
    ``end`` is the end of the option area.
    """
    index = start
    while index < end:
        opt_type = compat_ord(buf[index])
        if opt_type == 0:  # Pad1
            index += 1
            continue
        if index + 1 >= end:
            return None
        opt_len = compat_ord(buf[index + 1])
        if opt_type == 0xc2:  # Jumbo Payload
            if opt_len != 4 or index + 6 > end:
                return None
            return _u32(buf, index + 2)
        index += opt_len + 2
    return None


def _walk_ipv6_extensions(frame, next_header, start, declared_payload_len):
    """Walk IPv6 extension headers.

    Returns ``(next_header, transport_start, extension_captured_len,
    extension_declared_len, jumbo_len, fragmented, non_initial, truncated)``.
    """
    nxt = next_header
    index = start
    ext_captured = 0
    ext_declared = 0
    jumbo_len = None
    fragmented = False
    non_initial = False
    truncated = False

    while nxt in (0, 43, 44, 50, 51, 60):
        if index >= len(frame):
            truncated = True
            break
        header_next = compat_ord(frame[index])
        if nxt == 50:  # ESP: encrypted, cannot find UDP
            if declared_payload_len is not None:
                ext_declared = declared_payload_len
            return nxt, None, ext_captured, ext_declared, jumbo_len, fragmented, non_initial, truncated
        if index + 2 > len(frame):
            truncated = True
            break

        if nxt == 44:  # Fragment header
            hdr_len = 8
        elif nxt == 51:  # Authentication Header
            hdr_len = (compat_ord(frame[index + 1]) + 2) * 4
        else:  # Hop-by-Hop, Routing, Destination Options
            hdr_len = (compat_ord(frame[index + 1]) + 1) * 8

        if hdr_len < 8:
            truncated = True
            break
        if index + hdr_len > len(frame):
            truncated = True
            ext_captured += max(0, len(frame) - index)
            ext_declared += hdr_len
            break

        if nxt == 44:
            frag_field = _u16(frame, index + 2)
            frag_off = frag_field >> 3
            fragmented = True
            if frag_off:
                non_initial = True
                ext_declared += hdr_len
                index += hdr_len
                # A non-initial fragment contains no transport header, so stop
                # walking: the next bytes are fragment data, not IPv6 headers.
                return header_next, index, ext_captured, ext_declared, jumbo_len, \
                    fragmented, non_initial, truncated
        elif nxt == 0:
            value = _ipv6_jumbo_option(frame, index + 2, index + hdr_len)
            if value is not None:
                jumbo_len = value

        ext_captured += hdr_len
        ext_declared += hdr_len
        index += hdr_len
        nxt = header_next

    return nxt, index, ext_captured, ext_declared, jumbo_len, fragmented, non_initial, truncated


def _set_declared_payload(record, value, source):
    record.declared_payload_len = value
    record.declared_payload_source = source


def _finish_udp(record, udp_header, transport_captured_len,
                transport_declared_len, fragmented, non_initial, jumbo_len,
                ip_version, capture_short=False):
    """Parse a captured UDP header and compute the report counters."""
    record.is_udp = True
    record.fragmented = fragmented
    record.non_initial_fragment = non_initial

    if non_initial:
        record.fragment_payload_len = max(0, transport_captured_len)
        record.status = _STATUS_FRAGMENTED
        record.add_conflict(NON_INITIAL_FRAGMENT,
                            captured_fragment_payload=record.fragment_payload_len)
        return

    if udp_header is None:
        record.status = _STATUS_TRUNCATED
        record.add_conflict(UDP_HEADER_TRUNCATED,
                            captured_transport_len=transport_captured_len,
                            declared_transport_len=transport_declared_len)
        if transport_declared_len is not None and transport_declared_len >= 8:
            _set_declared_payload(record, transport_declared_len - 8, 'ip-derived')
        return

    record.udp_header_captured = True
    record.src_port, record.dst_port, record.udp_declared_total_len, _ = struct.unpack(
        '>HHHH', udp_header)
    ulen = record.udp_declared_total_len
    captured_payload = max(0, transport_captured_len - 8)

    if ulen == 0:
        if ip_version == 4:
            record.add_conflict(UDP_LEN_ZERO_IPV4,
                                udp_len=0,
                                ipv4_total_len=record.ip_declared_total_len,
                                ipv4_payload_len=transport_declared_len)
            if transport_declared_len is not None and transport_declared_len >= 8:
                payload = transport_declared_len - 8
                record.udp_declared_payload_len = payload
                _set_declared_payload(record, payload, 'ipv4-derived')
            else:
                _set_declared_payload(record, None, None)
        else:
            if jumbo_len is not None:
                # RFC 2675: a jumbogram encodes the UDP length as zero.
                if transport_declared_len is not None and transport_declared_len >= 8:
                    payload = transport_declared_len - 8
                    record.udp_declared_payload_len = payload
                    _set_declared_payload(record, payload, 'ipv6-jumbo')
                else:
                    _set_declared_payload(record, None, None)
            else:
                # Outside a jumbogram UDP length 0 is malformed.  IPv6 has a
                # 32-bit length in the Jumbo Payload option, but no valid UDP
                # length field here; keep both raw lengths as evidence and do
                # not substitute the IPv6 payload length.
                record.add_conflict(UDP_LEN_ZERO_IPV6_NON_JUMBO,
                                    udp_len=0,
                                    ipv6_payload_len=record.ipv6_payload_len,
                                    ipv6_transport_len=transport_declared_len)
                _set_declared_payload(record, None, None)
    elif ulen < 8:
        record.add_conflict(UDP_LEN_BELOW_HEADER,
                            udp_len=ulen,
                            ip_transport_len=transport_declared_len)
        _set_declared_payload(record, None, None)
    else:
        record.udp_declared_payload_len = ulen - 8
        _set_declared_payload(record, ulen - 8, 'udp')

    if record.declared_payload_len is not None and not fragmented:
        # Bytes past the UDP datagram boundary are not payload, even when
        # they are physically present (link padding or an overstated IP
        # length).  Bytes inside both boundaries are captured payload.  A
        # fragment's UDP header describes the reassembled datagram, so it
        # cannot bound the bytes carried by this fragment.
        record.captured_payload_len = min(captured_payload, record.declared_payload_len)
        record.capture_excess_len = max(0, captured_payload - record.declared_payload_len)
        if record.capture_excess_len:
            record.add_conflict(CAPTURE_EXCEEDS_UDP_LEN,
                                udp_payload_len=record.declared_payload_len,
                                captured_payload_len=captured_payload,
                                excess=record.capture_excess_len)
    else:
        # No trustworthy declaration: report the bytes we actually have, but
        # do not claim they are a complete datagram.
        record.captured_payload_len = captured_payload

    if ulen > 0 and transport_declared_len is not None and not fragmented:
        if ulen > transport_declared_len:
            record.add_conflict(UDP_LEN_EXCEEDS_IP_PAYLOAD,
                                udp_len=ulen,
                                ip_transport_len=transport_declared_len,
                                difference=ulen - transport_declared_len)
        elif ulen < transport_declared_len:
            record.add_conflict(IP_PAYLOAD_EXCEEDS_UDP_LEN,
                                udp_len=ulen,
                                ip_transport_len=transport_declared_len,
                                difference=transport_declared_len - ulen)

    if fragmented:
        record.add_conflict(FRAGMENTED_DATAGRAM,
                            fragment_payload_len=record.captured_payload_len)
        record.add_conflict(FRAGMENT_PAYLOAD_INCOMPLETE,
                            captured_fragment_payload=record.captured_payload_len,
                            declared_payload_len=record.declared_payload_len)
        record.fragment_payload_len = record.captured_payload_len
        record.truncated_payload_len = 0
        record.status = _STATUS_FRAGMENTED
        record.complete = False
        record.decodable_payload_len = 0
        return

    if record.declared_payload_len is not None:
        record.truncated_payload_len = max(0, record.declared_payload_len -
                                           record.captured_payload_len)
        if record.truncated_payload_len:
            if capture_short:
                # Only a genuinely short capture is "truncated".  A datagram
                # shortened by contradictory length fields keeps its own
                # conflict code (e.g. udp_len_exceeds_ip_payload) and is not
                # reported as capture loss.
                record.add_conflict(UDP_PAYLOAD_TRUNCATED_BY_CAPTURE,
                                    udp_payload_len=record.declared_payload_len,
                                    captured_payload_len=record.captured_payload_len,
                                    missing=record.truncated_payload_len)
            else:
                record.conflict_evidence.setdefault('payload_not_decodable', {}).update({
                    'declared_payload_len': record.declared_payload_len,
                    'captured_payload_len': record.captured_payload_len,
                    'missing': record.truncated_payload_len,
                    'reason': 'length_conflict',
                })
    length_conflict = (UDP_LEN_EXCEEDS_IP_PAYLOAD in record.conflicts or
                       IPV6_JUMBO_LENGTH_INVALID in record.conflicts or
                       IPV6_EXTENSION_TRUNCATED in record.conflicts)
    if (record.udp_header_captured and
            record.declared_payload_len is not None and
            record.truncated_payload_len == 0 and
            not length_conflict):
        record.complete = True
        record.decodable_payload_len = record.declared_payload_len
        record.status = _STATUS_COMPLETE
    elif record.truncated_payload_len and capture_short:
        # Only a genuinely short capture is reported as truncation.
        record.complete = False
        record.decodable_payload_len = 0
        record.status = _STATUS_TRUNCATED
    else:
        # Contradictory length fields (or a missing declaration) leave the
        # payload undecodable without blaming the capture.
        record.complete = False
        record.decodable_payload_len = 0
        record.status = _STATUS_MALFORMED


def _new_record(index, timestamp, linktype, frame_len, ip_offset, wire_len=None):
    record = PacketRecord()
    record.index = index
    record.timestamp = timestamp
    record.linktype = linktype
    record.frame_len = frame_len
    record.wire_len = wire_len
    record.ip_offset = ip_offset
    return record


def _allowable_padding(linktype, ip_len, captured_len, ip_offset):
    """Return how many trailing bytes may legitimately be link padding.

    Only Ethernet-style frames pad short packets.  The 46-byte minimum
    applies to the Ethernet payload, so VLAN/MPLS/PPPoE headers reduce the
    minimum IP length accordingly.  For other link types any excess bytes are
    reported as a conflict.
    """
    if linktype != pcap.DLT_EN10MB:
        return 0
    link_header_len = (ip_offset - 14) if ip_offset is not None and ip_offset >= 14 else 0
    minimum_ip_len = max(0, 46 - link_header_len)
    if ip_len >= minimum_ip_len:
        return 0
    return min(minimum_ip_len - ip_len, max(0, captured_len - ip_len))


def _parse_ipv4(frame, offset, record):
    available = len(frame) - offset
    if available < 20:
        record.add_conflict(IP_HEADER_TRUNCATED, available=available, needed=20)
        record.status = _STATUS_MALFORMED
        return
    vhl = compat_ord(frame[offset])
    version = vhl >> 4
    ihl = (vhl & 0x0f) * 4
    record.ip_version = version
    record.ip_header_len = ihl
    record.ip_declared_total_len = _u16(frame, offset + 2)
    flags_offset = _u16(frame, offset + 6)
    frag_offset = flags_offset & 0x1fff
    more_fragments = bool(flags_offset & 0x2000)
    protocol = compat_ord(frame[offset + 9])
    record.src = _inet_ntop(frame[offset + 12:offset + 16])
    record.dst = _inet_ntop(frame[offset + 16:offset + 20])

    if protocol != 17:
        record.status = _STATUS_MALFORMED
        record.add_conflict(NON_UDP_PACKET, protocol=protocol)
        return
    record.is_udp = True

    if ihl < 20:
        record.add_conflict(IP_HEADER_LEN_INVALID, ip_header_len=ihl)
        record.status = _STATUS_MALFORMED
        return

    if record.ip_declared_total_len == 0:
        record.add_conflict(IP_TOTAL_LEN_ZERO, field='ipv4.total_len')
        declared_total = None
        record.ip_payload_len_source = 'capture'
    else:
        declared_total = record.ip_declared_total_len
        if declared_total < ihl:
            record.add_conflict(IP_TOTAL_LEN_BELOW_HEADER,
                                ip_header_len=ihl, ip_total_len=declared_total)
            record.ip_declared_payload_len = None
            record.ip_payload_len_source = None
            record.status = _STATUS_MALFORMED
            return
        record.ip_declared_payload_len = declared_total - ihl
        record.ip_payload_len_source = 'ipv4.total_len'

    # The captured extent is what is physically present, independent of the
    # length declarations.  Conflicts are recorded, never used to shorten or
    # lengthen the measurement.
    if declared_total is not None:
        if available < declared_total:
            record.add_conflict(IP_TRUNCATED_BY_CAPTURE,
                                ip_total_len=declared_total,
                                captured_ip_len=available,
                                missing=declared_total - available)
        elif available > declared_total:
            # Ethernet/802.3 frames may carry link-layer padding after the IP
            # packet.  Padding is not UDP payload and is not a length
            # conflict; only non-padding excess is reported.
            excess = available - declared_total
            allowed_padding = _allowable_padding(record.linktype, declared_total,
                                                 available, record.ip_offset)
            if excess > allowed_padding:
                record.add_conflict(CAPTURE_EXCEEDS_IP_TOTAL_LEN,
                                    ip_total_len=declared_total,
                                    captured_ip_len=available,
                                    excess=excess,
                                    padding=allowed_padding)
    record.ip_captured_total_len = available
    capture_short = declared_total is not None and available < declared_total

    if available < ihl:
        record.add_conflict(IP_HEADER_TRUNCATED,
                            ip_header_len=ihl, captured_ip_len=available)
        record.status = _STATUS_TRUNCATED
        record.declared_payload_len = record.ip_declared_payload_len
        record.declared_payload_source = 'ipv4.total_len' if record.ip_declared_payload_len is not None else None
        return

    # Physical bytes after the IP header.  The UDP length bounds how many of
    # them count as UDP payload; the IP declaration only adds conflict
    # evidence and never inflates or shrinks the capture measurement.
    transport_captured = max(0, available - ihl)
    record.ip_captured_payload_len = transport_captured
    fragmented = frag_offset > 0 or more_fragments
    if frag_offset > 0:
        _finish_udp(record, None, transport_captured,
                    record.ip_declared_payload_len, True, True, None, 4,
                    capture_short)
        return

    if transport_captured < 8:
        _finish_udp(record, None, transport_captured,
                    record.ip_declared_payload_len, fragmented, False, None, 4,
                    capture_short)
        return

    udp_header = frame[offset + ihl:offset + ihl + 8]
    _finish_udp(record, udp_header, transport_captured,
                record.ip_declared_payload_len, fragmented, False, None, 4,
                capture_short)


def _parse_ipv6(frame, offset, record):
    available = len(frame) - offset
    if available < 40:
        record.add_conflict(IP_HEADER_TRUNCATED, available=available, needed=40)
        record.status = _STATUS_MALFORMED
        return
    record.ip_version = 6
    record.ip_header_len = 40
    record.ipv6_payload_len = _u16(frame, offset + 4)
    next_header = compat_ord(frame[offset + 6])
    record.src = _inet_ntop(frame[offset + 8:offset + 24])
    record.dst = _inet_ntop(frame[offset + 24:offset + 40])
    record.ip_offset = offset

    # IPv6 payload length zero is a jumbogram marker (RFC 2675) or an
    # offload/TSO "length not supplied" value.  It is not treated as a
    # literal zero-length payload; a valid Jumbo Payload option supersedes it
    # below, otherwise the capture extent is used for availability only.
    declared_payload = record.ipv6_payload_len if record.ipv6_payload_len else None
    if record.ipv6_payload_len == 0:
        record.add_conflict(IPV6_PAYLOAD_LEN_ZERO, field='ipv6.plen')

    (result_nxt, transport_start, ext_captured, ext_declared, jumbo_len,
     fragmented, non_initial, ext_truncated) = _walk_ipv6_extensions(
        frame, next_header, offset + 40, declared_payload)

    record.ipv6_jumbo_payload_len = jumbo_len
    record.ipv6_extension_captured_len = ext_captured
    record.ipv6_extension_len = ext_declared

    if jumbo_len is not None:
        # RFC 2675: the Hop-by-Hop Jumbo Payload option supersedes plen=0.
        declared_payload = jumbo_len
        # RFC 2675 supersedes plen=0; the zero is not a conflict when the
        # Jumbo Payload option supplies the real length.
        record.conflicts = [code for code in record.conflicts
                            if code != IPV6_PAYLOAD_LEN_ZERO]
    if declared_payload is not None:
        record.ip_declared_payload_len = declared_payload
        record.ip_payload_len_source = ('ipv6.jumbo' if jumbo_len is not None
                                        else 'ipv6.plen')
    else:
        record.ip_declared_payload_len = None

    if ext_truncated:
        record.add_conflict(IPV6_EXTENSION_TRUNCATED,
                            captured=ext_captured, declared=ext_declared)
        record.status = _STATUS_TRUNCATED
        if declared_payload is not None:
            transport_declared = max(0, declared_payload - ext_declared)
            record.declared_payload_len = max(0, transport_declared - 8)
            record.declared_payload_source = 'ipv6-derived'
        return

    if jumbo_len is not None and jumbo_len < ext_declared + 8:
        record.add_conflict(IPV6_JUMBO_LENGTH_INVALID,
                            jumbo_payload_len=jumbo_len,
                            extension_len=ext_declared)
        record.status = _STATUS_MALFORMED
        return

    if result_nxt is not None and result_nxt != 17:
        record.status = _STATUS_MALFORMED
        record.add_conflict(NON_UDP_PACKET, protocol=result_nxt)
        return

    if transport_start is None:
        record.status = _STATUS_MALFORMED
        return

    # The captured extent is physical; declarations only produce evidence.
    if declared_payload is not None:
        declared_total = 40 + declared_payload
        if available < declared_total:
            record.add_conflict(IP_TRUNCATED_BY_CAPTURE,
                                ip_total_len=declared_total,
                                captured_ip_len=available,
                                missing=declared_total - available)
        elif available > declared_total:
            excess = available - declared_total
            allowed_padding = _allowable_padding(record.linktype, declared_total,
                                                 available, record.ip_offset)
            if excess > allowed_padding:
                record.add_conflict(CAPTURE_EXCEEDS_IP_TOTAL_LEN,
                                    ip_total_len=declared_total,
                                    captured_ip_len=available,
                                    excess=excess,
                                    padding=allowed_padding)
    record.ip_captured_total_len = available
    capture_short = declared_payload is not None and available < 40 + declared_payload
    # ``ip_captured_payload_len`` counts captured bytes after the fixed IPv6
    # header, extension headers included.
    record.ip_captured_payload_len = max(0, available - 40)

    transport_captured = max(0, available - 40 - ext_captured)
    transport_declared = None
    if declared_payload is not None and declared_payload >= ext_declared:
        transport_declared = declared_payload - ext_declared

    if non_initial:
        _finish_udp(record, None, transport_captured, transport_declared,
                    True, True, jumbo_len, 6, capture_short)
        return

    if transport_captured < 8:
        _finish_udp(record, None, transport_captured, transport_declared,
                    fragmented, False, jumbo_len, 6, capture_short)
        return

    udp_header = frame[transport_start:transport_start + 8]
    _finish_udp(record, udp_header, transport_captured, transport_declared,
                fragmented, False, jumbo_len, 6, capture_short)


def parse_packet(frame, linktype=pcap.DLT_EN10MB, index=None, timestamp=None,
                 wire_len=None):
    """Parse one captured frame and return a :class:`PacketRecord`.

    ``None`` is returned for frames that do not contain an IP packet.  A
    record is returned for non-UDP IP packets and unsupported link layers as
    well, with a conflict code explaining why it has no UDP counters; callers
    that only want UDP statistics can ignore records with
    ``udp_header_captured == False``.
    """
    frame = _as_bytes(frame)
    offset = _link_offset(frame, linktype)
    record = _new_record(index, timestamp, linktype, len(frame), offset, wire_len)
    if offset is None:
        if _is_supported_linktype(linktype):
            record.add_conflict(TRUNCATED_LINK_HEADER,
                                linktype=linktype, frame_len=len(frame))
        else:
            record.add_conflict(UNSUPPORTED_LINKTYPE, linktype=linktype)
        record.status = _STATUS_MALFORMED
        return record
    if wire_len is not None:
        if wire_len < len(frame):
            record.add_conflict(WIRE_LEN_BELOW_CAPTURED_LEN,
                                wire_len=wire_len, captured_len=len(frame))
        elif wire_len > len(frame):
            record.add_conflict(CAPTURE_TRUNCATED_BY_SNAPLEN,
                                wire_len=wire_len, captured_len=len(frame),
                                missing=wire_len - len(frame))
    if offset >= len(frame):
        record.add_conflict(TRUNCATED_LINK_HEADER,
                            linktype=linktype, frame_len=len(frame))
        record.status = _STATUS_MALFORMED
        return record

    version = compat_ord(frame[offset]) >> 4
    if version == 4:
        _parse_ipv4(frame, offset, record)
        return record
    if version == 6:
        _parse_ipv6(frame, offset, record)
        return record

    record.add_conflict(NON_IP_PACKET, version=version)
    record.status = _STATUS_MALFORMED
    return record


def _iter_pcap(stream):
    """Yield capture records from a libpcap stream, including the wire length."""
    buf = stream.read(pcap.FileHdr.__hdr_len__)
    if len(buf) < pcap.FileHdr.__hdr_len__:
        return
    file_hdr = pcap.FileHdr(buf)
    magic = file_hdr.magic
    if magic in (pcap.PMUDPCT_MAGIC, pcap.PMUDPCT_MAGIC_NANO, pcap.PACPDOM_MAGIC):
        file_hdr = pcap.LEFileHdr(buf)
    if magic not in pcap.MAGIC_TO_PKT_HDR:
        raise ValueError('invalid tcpdump header')
    packet_hdr_cls = pcap.MAGIC_TO_PKT_HDR[magic]
    divisor = Decimal('1E9') if magic in (pcap.TCPDUMP_MAGIC_NANO,
                                          pcap.PMUDPCT_MAGIC_NANO) else 1E6
    while True:
        buf = stream.read(packet_hdr_cls.__hdr_len__)
        if len(buf) < packet_hdr_cls.__hdr_len__:
            return
        hdr = packet_hdr_cls(buf)
        frame = stream.read(hdr.caplen)
        # ``hdr.len`` is the original on-wire length; ``hdr.caplen`` is what
        # the file actually contains.  Keep both even if the record is short.
        yield (hdr.tv_sec + (hdr.tv_usec / divisor), frame,
               file_hdr.linktype, hdr.len)
        if len(frame) < hdr.caplen:
            return


def _iter_pcapng(stream):
    """Yield capture records from a pcapng stream, including the wire length."""
    interfaces = []
    little_endian = None

    while True:
        head = stream.read(8)
        if len(head) < 8:
            return
        # The SHB magic is byte-order independent (0x0A0D0D0A), so it can be
        # recognised before the section byte order is known.
        if struct.unpack('>I', head[:4])[0] == pcapng.PCAPNG_BT_SHB:
            rest = stream.read(4)
            if len(rest) < 4:
                return
            bom = struct.unpack('>I', rest)[0]
            little_endian = bom == pcapng.BYTE_ORDER_MAGIC_LE
            if bom not in (pcapng.BYTE_ORDER_MAGIC, pcapng.BYTE_ORDER_MAGIC_LE):
                raise ValueError('unknown pcapng endianness')
            # The block length follows the BOM, so it must be decoded with the
            # byte order the BOM just revealed.
            block_len = struct.unpack(('<' if little_endian else '>') + 'I', head[4:8])[0]
            if block_len < 12:
                return
            body = head + rest + stream.read(block_len - 12)
            # Each section carries its own interface table.
            interfaces = []
            # Section header itself carries no packet data.
            continue

        if little_endian is None:
            raise ValueError('pcapng packet block before section header')
        byte_order = '<' if little_endian else '>'
        blk_type = struct.unpack(byte_order + 'I', head[:4])[0]
        block_len = struct.unpack(byte_order + 'I', head[4:8])[0]
        if block_len < 12:
            return
        body = head + stream.read(block_len - 8)
        if len(body) < block_len:
            return

        if blk_type == pcapng.PCAPNG_BT_IDB:
            idb = (pcapng.InterfaceDescriptionBlockLE(body) if little_endian
                   else pcapng.InterfaceDescriptionBlock(body))
            idb._divisor = 1000000
            for opt in idb.opts:
                if opt.code == pcapng.PCAPNG_OPT_IF_TSRESOL:
                    value = struct.unpack('b', opt.data)[0]
                    base = 2 if value & 0x80 else 10
                    idb._divisor = base ** (value & 0x7f)
            interfaces.append(idb)
        elif blk_type in (pcapng.PCAPNG_BT_EPB, pcapng.PCAPNG_BT_PB):
            cls = {pcapng.PCAPNG_BT_EPB: (pcapng.EnhancedPacketBlockLE
                                         if little_endian else pcapng.EnhancedPacketBlock),
                   pcapng.PCAPNG_BT_PB: (pcapng.PacketBlockLE if little_endian
                                        else pcapng.PacketBlock)}[blk_type]
            block = cls(body)
            iface_id = getattr(block, 'iface_id', 0)
            linktype = (interfaces[iface_id].linktype
                        if iface_id < len(interfaces) else pcap.DLT_EN10MB)
            ts = block.ts_high << 32 | block.ts_low
            divisor = (getattr(interfaces[iface_id], '_divisor', 1000000)
                       if iface_id < len(interfaces) else 1000000)
            yield (ts / divisor, block.pkt_data, linktype, block.pkt_len)
        elif blk_type == pcapng.PCAPNG_BT_SPB:
            orig_len = struct.unpack(byte_order + 'I', body[8:12])[0]
            linktype = (interfaces[0].linktype if interfaces else pcap.DLT_EN10MB)
            snaplen = interfaces[0].snaplen if interfaces else 0xffff
            caplen = min(orig_len, snaplen, max(0, len(body) - 16))
            yield (None, body[12:12 + caplen], linktype, orig_len)


def _iter_capture(stream):
    """Yield records from a pcap/pcapng stream, using the format magic."""
    head = stream.read(4)
    if len(head) < 4:
        return
    try:
        stream.seek(0)
    except (AttributeError, IOError):
        # Non-seekable streams cannot be sniffed reliably; defer to dpkt.
        reader = pcap.UniversalReader(stream)
        linktype = reader.datalink() if hasattr(reader, 'datalink') else pcap.DLT_EN10MB
        for timestamp, frame in reader:
            yield timestamp, frame, linktype, None
        return
    magic = struct.unpack('>I', head)[0]
    if magic in pcap.MAGIC_TO_PKT_HDR:
        for item in _iter_pcap(stream):
            yield item
    elif magic == pcapng.PCAPNG_BT_SHB:
        for item in _iter_pcapng(stream):
            yield item
    else:
        raise ValueError('unknown pcap format')


def _iter_frames(source):
    """Yield ``(timestamp, frame, linktype, wire_len)`` from a source.

    Files and file-like objects are read directly so that both ``caplen`` and
    the original on-wire length from each capture record are available.
    Iterables may yield ``frame``, ``(timestamp, frame)`` or
    ``(timestamp, frame, wire_len)`` items.
    """
    if isinstance(source, _PATH_TYPES):
        # ``bytes`` is treated as a file path only when it looks like one and
        # the file exists; otherwise the caller passed a single frame.
        try:
            is_path = os.path.exists(source)
        except (TypeError, ValueError):
            is_path = False
        if is_path:
            with open(source, 'rb') as stream:
                for item in _iter_capture(stream):
                    yield item
            return
        yield None, source, pcap.DLT_EN10MB, None
        return

    if hasattr(source, 'read'):
        for item in _iter_capture(source):
            yield item
        return

    linktype = getattr(source, 'datalink', lambda: pcap.DLT_EN10MB)()
    for item in source:
        if isinstance(item, tuple):
            if len(item) == 2:
                yield item[0], item[1], linktype, None
            elif len(item) == 3:
                yield item[0], item[1], linktype, item[2]
            else:
                raise ValueError('frame items must be (frame), (ts, frame) or (ts, frame, wire_len)')
        else:
            yield None, item, linktype, None


def analyze(source, linktype=None):
    """Build a :class:`UDPReport` from a pcap path/reader or frame iterable.

    Args:
        source: A filesystem path, a file-like object understood by
            :class:`dpkt.pcap.UniversalReader`, or an iterable of
            ``(timestamp, frame)`` pairs.
        linktype: Link-layer type for raw frame iterables.  Readers provide
            their own value; the default for iterables is Ethernet.
    """
    report = UDPReport()
    if linktype is not None:
        frames = ((ts, frame, linktype, wire_len)
                  for ts, frame, _, wire_len in _iter_frames(source))
    else:
        frames = _iter_frames(source)

    for index, (timestamp, frame, frame_linktype, wire_len) in enumerate(frames):
        record = parse_packet(frame, frame_linktype, index=index,
                              timestamp=timestamp, wire_len=wire_len)
        if record is None:
            continue
        report.add(record)
        if record.conflicts and record.ip_version is None:
            report.errors.append({'index': index, 'conflicts': list(record.conflicts)})
    return report


def udp_report(source, linktype=None):
    """Alias for :func:`analyze`, kept as the user-facing report entry point."""
    return analyze(source, linktype=linktype)


def build_report(source, linktype=None):
    """Alias for :func:`analyze`."""
    return analyze(source, linktype=linktype)


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description='Summarise UDP payload accounting by source address.')
    parser.add_argument('capture', help='pcap or pcapng file')
    parser.add_argument('--csv', action='store_true', help='emit CSV instead of a table')
    parser.add_argument('--json', action='store_true', help='emit JSON instead of a table')
    parser.add_argument('--records', action='store_true',
                        help='include per-packet evidence (JSON only)')
    args = parser.parse_args(argv)

    report = analyze(args.capture)
    if args.json:
        import json
        print(json.dumps(report.as_dict(include_records=args.records),
                         ensure_ascii=False, indent=2))
    elif args.csv:
        sys.stdout.write(report.to_csv())
    else:
        print(report.format_text())
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(_main())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _ethernet(payload, ethertype=0x0800):
    return (b'\x00\x11\x22\x33\x44\x55' + b'\x66\x77\x88\x99\xaa\xbb' +
            struct.pack('>H', ethertype) + payload)


def _ipv4_udp(payload=b'0123456789', sport=1000, dport=2000, ulen=None,
              ip_len=None, src=b'\x0a\x00\x00\x01', dst=b'\x0a\x00\x00\x02',
              flags_offset=0, ip_options=b''):
    if ulen is None:
        ulen = 8 + len(payload)
    udp = struct.pack('>HHHH', sport, dport, ulen, 0) + payload
    ihl = 20 + len(ip_options)
    total = ihl + len(udp) if ip_len is None else ip_len
    hdr = struct.pack('>BBHHHBBH4s4s', 0x40 | (ihl // 4), 0, total, 0,
                      flags_offset, 64, 17, 0, src, dst) + ip_options
    return hdr + udp


def _ipv6_udp(payload=b'0123456789', sport=1000, dport=2000, ulen=None,
              plen=None, src=b'\x20\x01\x0d\xb8' + b'\x00' * 11 + b'\x01',
              dst=b'\x20\x01\x0d\xb8' + b'\x00' * 11 + b'\x02', nxt=17,
              extensions=b'', jumbo_len=None):
    if ulen is None:
        ulen = 8 + len(payload)
    udp = struct.pack('>HHHH', sport, dport, ulen, 0) + payload
    body = extensions + udp
    if plen is None:
        plen = len(body)
    return struct.pack('>IHBB16s16s', 0x60000000, plen, nxt, 64, src, dst) + body


def test_udp_report_complete_ipv4():
    frame = _ethernet(_ipv4_udp(b'abcdefghij'))
    report = analyze([(1.0, frame)])
    stats = report.sources['10.0.0.1']
    assert stats.packets == 1
    assert stats.datagrams == 1
    assert stats.declared_payload_bytes == 10
    assert stats.captured_payload_bytes == 10
    assert stats.decodable_payload_bytes == 10
    assert stats.truncated_payload_bytes == 0
    record = report.records[0]
    assert record.status == 'complete'
    assert record.src_port == 1000 and record.dst_port == 2000
    assert record.conflicts == []


def test_udp_report_truncated_capture_is_not_loss():
    frame = _ethernet(_ipv4_udp(b'0123456789'))[:-8]
    report = analyze([(1.0, frame)])
    stats = report.sources['10.0.0.1']
    assert stats.declared_payload_bytes == 10
    assert stats.captured_payload_bytes == 2
    assert stats.decodable_payload_bytes == 0
    assert stats.truncated_payload_bytes == 8
    assert stats.truncated_packets == 1
    record = report.records[0]
    assert 'ip_truncated_by_capture' in record.conflicts
    assert 'udp_payload_truncated_by_capture' in record.conflicts
    assert record.conflict_evidence['ip_truncated_by_capture']['missing'] == 8


def test_udp_report_udp_len_exceeds_ip_payload_preserves_both():
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=28, ip_len=30))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.ip_declared_total_len == 30
    assert record.udp_declared_total_len == 28
    assert record.declared_payload_len == 20
    assert record.captured_payload_len == 10
    assert 'udp_len_exceeds_ip_payload' in record.conflicts
    # The IP length is not replaced by the larger UDP length.
    assert record.ip_declared_payload_len == 10
    assert record.udp_declared_payload_len == 20


def test_udp_report_ip_payload_exceeds_udp_len():
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=12, ip_len=38))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.declared_payload_len == 4
    assert record.captured_payload_len == 4
    assert record.truncated_payload_len == 0
    assert 'ip_payload_exceeds_udp_len' in record.conflicts


def test_udp_report_ipv4_zero_total_len():
    frame = _ethernet(_ipv4_udp(b'0123456789', ip_len=0))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'ip_total_len_zero' in record.conflicts
    assert record.ip_declared_total_len == 0
    assert record.declared_payload_len == 10
    assert record.captured_payload_len == 10
    assert record.decodable_payload_len == 10


def test_udp_report_ipv4_zero_udp_len():
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=0, ip_len=38))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'udp_len_zero_ipv4' in record.conflicts
    assert record.udp_declared_total_len == 0
    assert record.declared_payload_len == 10
    assert record.declared_payload_source == 'ipv4-derived'
    assert record.decodable_payload_len == 10


def test_udp_report_ipv6_complete():
    frame = _ethernet(_ipv6_udp(b'abcdefghij'), ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    stats = report.sources['2001:db8::1']
    assert stats.declared_payload_bytes == 10
    assert stats.captured_payload_bytes == 10
    assert stats.decodable_payload_bytes == 10


def test_udp_report_ipv6_zero_udp_len_without_jumbo():
    # Outside a jumbogram a zero UDP length is malformed.  Do not replace it
    # with the IPv6 payload length; keep the captured bytes and the conflict.
    frame = _ethernet(_ipv6_udp(b'0123456789', ulen=0), ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'udp_len_zero_ipv6_non_jumbo' in record.conflicts
    assert record.declared_payload_len is None
    assert record.captured_payload_len == 10
    assert record.decodable_payload_len == 0
    assert record.status != 'complete'


def test_udp_report_ipv6_zero_plen():
    # plen=0 without a Jumbo Payload option is not a jumbogram.  It is kept
    # as evidence; the UDP length still provides the payload declaration.
    frame = _ethernet(_ipv6_udp(b'0123456789', plen=0), ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'ipv6_payload_len_zero' in record.conflicts
    # plen=0 is an unset/jumbogram marker, not a literal zero payload.
    assert record.ip_declared_payload_len is None
    assert record.declared_payload_len == 10
    assert record.captured_payload_len == 10
    assert record.decodable_payload_len == 10
    assert 'ipv6_payload_len_zero' in record.conflicts


def test_udp_report_ipv6_jumbogram():
    payload = b'0123456789'
    udp_len = 8 + len(payload)
    # Hop-by-Hop header, 8 bytes: next=UDP, len=0, Jumbo Payload option.
    jumbo_value = 8 + udp_len
    hop = struct.pack('>BB', 17, 0) + struct.pack('>BB', 0xc2, 4) + \
        struct.pack('>I', jumbo_value)
    assert len(hop) == 8
    frame = _ethernet(_ipv6_udp(payload, ulen=0, plen=0, nxt=0, extensions=hop),
                      ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.ipv6_jumbo_payload_len == jumbo_value
    assert record.ip_payload_len_source == 'ipv6.jumbo'
    assert record.declared_payload_len == 10
    assert record.declared_payload_source == 'ipv6-jumbo'
    assert record.decodable_payload_len == 10
    assert 'ipv6_payload_len_zero' not in record.conflicts


def test_udp_report_first_fragment_is_not_truncation():
    # First fragment: MF set, UDP header present, only part of the payload.
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=108, ip_len=128,
                                flags_offset=0x2000))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.fragmented
    assert not record.non_initial_fragment
    assert record.truncated_payload_len == 0
    assert record.decodable_payload_len == 0
    assert record.fragment_payload_len == 10
    assert 'fragmented_datagram' in record.conflicts
    assert 'fragment_payload_incomplete' in record.conflicts


def test_udp_report_non_initial_fragment():
    frame = _ethernet(_ipv4_udp(b'0123456789', flags_offset=100))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.non_initial_fragment
    assert record.status == 'fragmented'
    assert record.src == '10.0.0.1'
    assert report.sources['10.0.0.1'].non_initial_fragments == 1


def test_udp_report_groups_by_source():
    frames = [
        (1.0, _ethernet(_ipv4_udp(b'aaaa', src=b'\x0a\x00\x00\x01'))),
        (2.0, _ethernet(_ipv4_udp(b'bbbbbb', src=b'\x0a\x00\x00\x01'))),
        (3.0, _ethernet(_ipv4_udp(b'cc', src=b'\x0a\x00\x00\x03'))),
    ]
    report = analyze(frames)
    assert len(report.sources) == 2
    assert report.sources['10.0.0.1'].declared_payload_bytes == 10
    assert report.sources['10.0.0.3'].declared_payload_bytes == 2
    assert report.totals.declared_payload_bytes == 12
    text = report.format_text()
    assert '10.0.0.1' in text and 'TOTAL' in text
    assert report.as_dict(include_records=False)['sources'][0]['source'] == '10.0.0.1'


def test_udp_report_udp_header_truncated():
    full = _ethernet(_ipv4_udp(b'0123456789'))
    frame = full[:14 + 20 + 4]  # only half of the UDP header
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert not record.udp_header_captured
    assert 'udp_header_truncated' in record.conflicts
    assert record.src == '10.0.0.1'


def test_udp_report_raw_linktype():
    frame = _ipv4_udp(b'0123456789')
    report = analyze([(1.0, frame)], linktype=pcap.DLT_RAW)
    assert report.sources['10.0.0.1'].decodable_payload_bytes == 10


def test_udp_report_unsupported_linktype():
    report = analyze([(1.0, b'\x00' * 20)], linktype=9999)
    record = report.records[0]
    assert 'unsupported_linktype' in record.conflicts
    assert report.sources == {}


def test_udp_report_csv():
    report = analyze([(1.0, _ethernet(_ipv4_udp(b'0123456789')))])
    out = report.to_csv()
    assert out.startswith('source,ip_version,packets')
    assert '10.0.0.1' in out


def test_udp_report_empty():
    report = analyze([])
    assert len(report.sources) == 0
    assert report.totals.declared_payload_bytes == 0
    assert 'TOTAL' in report.format_text()


def test_udp_report_pcap_roundtrip_preserves_wire_len(tmp_path=None):
    from .compat import BytesIO

    frame = _ethernet(_ipv4_udp(b'0123456789'))
    # Pretend the on-wire frame was longer than what was saved.
    stream = BytesIO()
    writer = pcap.Writer(stream, snaplen=40)
    writer.writepkt_time(frame, 1.0)
    stream.seek(0)
    report = analyze(stream)
    record = report.records[0]
    assert record.frame_len == len(frame)
    assert record.wire_len == len(frame)
    assert record.declared_payload_len == 10
    assert record.decodable_payload_len == 10


def test_udp_report_pcap_wire_len_truncation():
    from .compat import BytesIO

    frame = _ethernet(_ipv4_udp(b'0123456789'))
    trimmed = frame[:14 + 20 + 8 + 2]
    stream = BytesIO()
    # Write a record header claiming the full original length but only the
    # captured prefix, as tcpdump does when snaplen truncates a packet.
    stream.write(bytes(pcap.FileHdr(snaplen=len(frame), linktype=pcap.DLT_EN10MB)))
    stream.write(bytes(pcap.PktHdr(tv_sec=1, tv_usec=0, caplen=len(trimmed),
                                   len=len(frame))))
    stream.write(trimmed)
    stream.seek(0)
    report = analyze(stream)
    record = report.records[0]
    assert record.wire_len == len(frame)
    assert record.frame_len == len(trimmed)
    assert 'capture_truncated_by_snaplen' in record.conflicts
    assert record.declared_payload_len == 10
    assert record.captured_payload_len == 2
    assert record.decodable_payload_len == 0


def test_udp_report_ethernet_padding_is_not_conflict():
    # A 38-byte IP packet is padded to Ethernet's 46-byte minimum payload.
    frame = _ethernet(_ipv4_udp(b'0123456789'))
    frame += b'\x00' * (46 - 38)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'capture_exceeds_ip_total_len' not in record.conflicts
    assert record.captured_payload_len == 10
    assert record.decodable_payload_len == 10


def test_udp_report_non_udp_is_not_counted():
    tcp_like = _ipv4_udp(b'0123456789')
    # Change the protocol byte from UDP (17) to TCP (6).
    tcp_like = tcp_like[:9] + b'\x06' + tcp_like[10:]
    frame = _ethernet(tcp_like)
    report = analyze([(1.0, frame)])
    assert report.sources == {}
    assert report.records[0].conflicts == ['non_udp_packet']


def test_udp_report_pcapng_roundtrip():
    from .compat import BytesIO
    from . import pcapng

    frame = _ethernet(_ipv4_udp(b'0123456789'))
    stream = BytesIO()
    writer = pcapng.Writer(stream, linktype=pcap.DLT_EN10MB)
    writer.writepkt(frame, ts=1.0)
    stream.seek(0)
    report = analyze(stream)
    record = report.records[0]
    assert record.src == '10.0.0.1'
    assert record.wire_len == len(frame)
    assert record.decodable_payload_len == 10


def test_udp_report_ipv6_extension_chain():
    # Hop-by-Hop (8 bytes) followed by Destination Options (8 bytes).
    payload = b'0123456789'
    dst_opts = struct.pack('>BB', 17, 0) + b'\x00' * 6
    hop = struct.pack('>BB', 60, 0) + b'\x00' * 6
    extensions = hop + dst_opts
    frame = _ethernet(_ipv6_udp(payload, nxt=0, extensions=extensions),
                      ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.ipv6_extension_len == 16
    assert record.declared_payload_len == 10
    assert record.captured_payload_len == 10
    assert record.decodable_payload_len == 10


def test_udp_report_ipv6_truncated_extension():
    payload = b'0123456789'
    # Claim a 16-byte Hop-by-Hop header but only provide 8 bytes before the
    # capture ends.
    hop = struct.pack('>BB', 17, 1) + b'\x00' * 6
    frame = _ethernet(_ipv6_udp(payload, nxt=0, plen=len(hop) + 18,
                                extensions=hop)[:-12], ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'ipv6_extension_truncated' in record.conflicts
    assert record.status == 'truncated'
    assert record.declared_payload_len == 2  # 26 - 16 extensions - 8 UDP
    assert record.decodable_payload_len == 0


def test_udp_report_malformed_udp_len_below_header():
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=4))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'udp_len_below_header' in record.conflicts
    assert record.udp_declared_total_len == 4
    assert record.declared_payload_len is None
    assert record.captured_payload_len == 10
    assert record.decodable_payload_len == 0


def test_udp_report_analysis_is_repeatable():
    frames = [
        (1.0, _ethernet(_ipv4_udp(b'aaaa'))),
        (2.0, _ethernet(_ipv4_udp(b'bb', src=b'\x0a\x00\x00\x09'))),
    ]
    first = analyze(frames)
    second = analyze(frames)
    assert first.as_dict(include_records=False) == second.as_dict(include_records=False)


def test_udp_report_truncated_link_header():
    report = analyze([(1.0, b'\x00\x11\x22')])
    record = report.records[0]
    assert record.conflicts == ['truncated_link_header']


def test_udp_report_ipv4_options_header_length():
    frame = _ethernet(_ipv4_udp(b'0123456789', ip_options=b'\x01\x01\x01\x00'))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.ip_header_len == 24
    assert record.ip_declared_payload_len == 18
    assert record.declared_payload_len == 10
    assert record.decodable_payload_len == 10


def test_udp_report_alternate_octet_stream():
    import io

    frame = _ethernet(_ipv4_udp(b'0123456789'))
    stream = io.BytesIO()
    writer = pcap.Writer(stream)
    writer.writepkt_time(frame, 1.0)
    stream.seek(0)
    report = analyze(stream)
    assert report.totals.declared_payload_bytes == 10


def test_udp_report_truncated_ipv6_header():
    frame = _ethernet(b'\x60\x00\x00\x00', ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'ip_header_truncated' in record.conflicts
    assert record.ip_version is None


def test_udp_report_capture_exceeds_udp_len_reports_excess():
    # UDP says 12 bytes total (4 payload) but 10 payload bytes are present.
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=12))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.declared_payload_len == 4
    assert record.captured_payload_len == 4
    assert record.capture_excess_len == 6
    assert 'capture_exceeds_udp_len' in record.conflicts
    assert record.decodable_payload_len == 4


def test_udp_report_snaplen_conflict_is_preserved_with_ip_and_udp_evidence():
    frame = _ethernet(_ipv4_udp(b'0123456789'))
    trimmed = frame[:14 + 20 + 8 + 1]
    report = analyze([(1.0, trimmed, len(frame))])
    record = report.records[0]
    assert record.wire_len == len(frame)
    evidence = record.conflict_evidence
    assert evidence['capture_truncated_by_snaplen']['missing'] == len(frame) - len(trimmed)
    assert evidence['ip_truncated_by_capture']['ip_total_len'] == 38
    assert evidence['udp_payload_truncated_by_capture']['udp_payload_len'] == 10
    assert evidence['udp_payload_truncated_by_capture']['captured_payload_len'] == 1


def test_udp_report_length_conflict_is_not_capture_truncation():
    # UDP declares 20 payload bytes but IP declares only 10 and 10 are
    # captured.  This is a length conflict, not a truncated capture.
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=28, ip_len=30))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.truncated_payload_len == 10
    assert 'udp_payload_truncated_by_capture' not in record.conflicts
    assert record.status == 'malformed'
    assert report.sources['10.0.0.1'].truncated_payload_bytes == 10
    assert report.sources['10.0.0.1'].truncated_packets == 0


def test_udp_report_capture_truncation_counts_packet():
    frame = _ethernet(_ipv4_udp(b'0123456789'))[:-8]
    report = analyze([(1.0, frame)])
    stats = report.sources['10.0.0.1']
    assert stats.truncated_payload_bytes == 8
    assert stats.truncated_packets == 1


def test_udp_report_ipv4_zero_lengths_have_no_declaration():
    frame = _ethernet(_ipv4_udp(b'0123456789', ulen=0, ip_len=0))
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert 'ip_total_len_zero' in record.conflicts
    assert 'udp_len_zero_ipv4' in record.conflicts
    assert record.declared_payload_len is None
    assert record.captured_payload_len == 10
    assert record.decodable_payload_len == 0


def test_udp_report_ipv6_non_initial_fragment():
    # Fragment header with a non-zero offset, followed by fragment data that
    # must not be mistaken for a UDP header.
    frag = struct.pack('>BBHI', 17, 0, 100 << 3, 1)
    frame = _ethernet(_ipv6_udp(b'0123456789', nxt=44, extensions=frag)[:40 + 8 + 10],
                      ethertype=0x86dd)
    report = analyze([(1.0, frame)])
    record = report.records[0]
    assert record.non_initial_fragment
    assert not record.udp_header_captured
    assert record.src_port is None
    assert report.sources['2001:db8::1'].non_initial_fragments == 1


def test_udp_report_accepts_pathlike(tmp_path):
    frame = _ethernet(_ipv4_udp(b'0123456789'))
    path = tmp_path / 'one.pcap'
    with open(str(path), 'wb') as stream:
        writer = pcap.Writer(stream)
        writer.writepkt_time(frame, 1.0)
    report = analyze(path)
    assert report.totals.declared_payload_bytes == 10


def test_udp_report_pcapng_timestamp_resolution():
    from .compat import BytesIO
    from . import pcapng

    frame = _ethernet(_ipv4_udp(b'0123456789'))
    stream = BytesIO()
    writer = pcapng.Writer(stream, linktype=pcap.DLT_EN10MB)
    writer.writepkt_time(frame, 12.5)
    stream.seek(0)
    records = list(_iter_pcapng(stream))
    assert len(records) == 1
    assert abs(records[0][0] - 12.5) < 1e-6
    assert records[0][3] == len(frame)
