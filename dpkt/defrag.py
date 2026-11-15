# -*- coding: utf-8 -*-
"""Offline IPv4/IPv6 fragment reassembly across capture domains.

Two capture cards can legitimately record datagrams that share the same IP
identification value.  This module therefore keys datagrams by:

    capture domain + interface + direction + protocol + identification

where the capture domain is the capture file (or a caller-supplied label),
interface is the pcapng interface index (so cards recorded into one file stay
separate), direction is ``src -> dst``, and protocol is the IPv4 protocol
number or the IPv6 payload protocol (next header after the fragment header).

Within a datagram, fragments are stored by absolute byte range:

* overlapping bytes must agree; a single disagreeing byte isolates the datagram
  (it is reported as ``conflict`` and is never emitted as complete);
* a fragment already covered by accepted bytes is a duplicate and adds no
  effective length;
* the total length is taken from the fragment whose ``more fragments`` flag is
  clear (the tail fragment).  If that fragment is absent, the datagram is
  reported ``incomplete`` with ``total_len == None`` -- never guessed.

Every reported datagram carries its provenance: the capture domain, interface,
per-domain record index (0-based) and frame number (1-based), timestamp and the
role of each record that touched it, including records rejected as duplicates
or taking part in a conflict.

Programmatic use::

    from dpkt.defrag import reassemble, reassemble_file

    report = reassemble_file('card-a.pcapng')
    report = reassemble(['card-a.pcapng', 'card-b.pcapng'])
    for datagram in report.datagrams:
        print(datagram.state, datagram.payload, datagram.records)

Command line use::

    python -m dpkt.defrag card-a.pcap card-b.pcap --json
"""
from __future__ import absolute_import, division, print_function

import argparse
import binascii
import json
import os
import struct
import sys
import tempfile

from . import pcap
from .compat import BytesIO

_PATH_TYPES = (str,) if bytes is str else (str, bytes)
_PATH_TYPES += tuple(t for t in (getattr(os, 'PathLike', None),) if t is not None)

# IPv4 fragment bits (same values as dpkt.ip)
IP_MF = 0x2000
IP_OFFMASK = 0x1fff

# Link types we can locate an IP header in without ambiguity.  ``DLT_RAW`` and
# ``DLT_LOOP`` are platform-dependent in libpcap, so use the library constants;
# LINKTYPE_RAW (101) and the single-family LINKTYPE_IPV4/IPV6 (228/229) are
# fixed on-disk values and are accepted everywhere.
_DLT_EN10MB = pcap.DLT_EN10MB
_RAW_LINKTYPES = frozenset((101, 228, 229, pcap.DLT_RAW))
_LOOP_LINKTYPES = frozenset((pcap.DLT_NULL, pcap.DLT_LOOP))
_DLT_LINUX_SLL = pcap.DLT_LINUX_SLL
_DLT_LINUX_SLL2 = getattr(pcap, 'DLT_LINUX_SLL2', 276)
_DLT_PPP = pcap.DLT_PPP

_VLAN_TYPES = frozenset((0x8100, 0x88a8, 0x9100, 0x9200))
_MPLS_TYPES = frozenset((0x8847, 0x8848))
_PPPOE_SESSION = 0x8864

STATE_COMPLETE = 'complete'
STATE_INCOMPLETE = 'incomplete'
STATE_CONFLICT = 'conflict'


def _u16(buf, off):
    return struct.unpack_from('>H', buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from('>I', buf, off)[0]


def _norm_domain(domain):
    if domain is None:
        raise ValueError('a capture domain label is required')
    return os.path.normpath(str(domain))


def locate_ethernet_ip(buf):
    """Return the byte offset of the IP header inside an Ethernet frame.

    Handles Ethernet II, stacked 802.1Q/802.1ad tags, MPLS label stacks and
    PPPoE session frames.  Returns None for anything else (LLC/SNAP, ARP, ...).
    """
    if len(buf) < 14:
        return None
    off = 12
    etype = _u16(buf, off)
    off += 2
    while etype in _VLAN_TYPES and len(buf) >= off + 4:
        etype = _u16(buf, off + 2)
        off += 4
    if etype in _MPLS_TYPES:
        while len(buf) >= off + 4:
            label = _u32(buf, off)
            off += 4
            if label & 0x100:  # bottom-of-stack
                break
        if len(buf) <= off:
            return None
        nibble = buf[off] >> 4
        if nibble in (4, 6):
            return off
        return None
    if etype == _PPPOE_SESSION and len(buf) >= off + 8:
        # PPPoE header: ver/type, code, session id, length = 6 bytes, then
        # the PPP protocol field.
        off += 6
        ppp_proto = _u16(buf, off)
        off += 2
        if ppp_proto in (0x0021, 0x0057):
            return off
        return None
    if etype in (0x0800, 0x86dd):
        return off
    return None


def locate_ip(buf, linktype):
    """Return the byte offset of an IPv4/IPv6 header for a capture record."""
    if linktype == _DLT_EN10MB:
        return locate_ethernet_ip(buf)
    if linktype in _RAW_LINKTYPES:
        return 0
    if linktype in _LOOP_LINKTYPES:
        # BSD/macOS loopback: 4-byte address family in the capturing host's
        # byte order, so accept either representation.
        if len(buf) < 4:
            return None
        family = struct.unpack_from('=I', buf, 0)[0]
        swapped = struct.unpack_from('>I' if sys.byteorder == 'little' else '<I', buf, 0)[0]
        if family in (2, 24, 28, 30) or swapped in (2, 24, 28, 30):
            return 4
        return None
    if linktype == _DLT_LINUX_SLL:
        if len(buf) >= 16 and _u16(buf, 14) in (0x0800, 0x86dd):
            return 16
        return None
    if linktype == _DLT_LINUX_SLL2:
        if len(buf) >= 20 and _u16(buf, 0) in (0x0800, 0x86dd):
            return 20
        return None
    if linktype == _DLT_PPP:
        if len(buf) >= 4 and buf[0:2] == b'\xff\x03':
            return 4 if _u16(buf, 2) in (0x0021, 0x0057) else None
        if len(buf) >= 2 and _u16(buf, 0) in (0x0021, 0x0057):
            return 2
        return None
    return None


def _parse_ipv4(buf, off):
    if len(buf) < off + 20:
        return None
    if (buf[off] >> 4) != 4:
        return None
    hdr_len = (buf[off] & 0x0f) * 4
    if hdr_len < 20 or len(buf) < off + hdr_len:
        return None
    total_len = _u16(buf, off + 2)
    if total_len < hdr_len:
        return None
    ident = _u16(buf, off + 4)
    flags_offset = _u16(buf, off + 6)
    offset = (flags_offset & IP_OFFMASK) * 8
    mf = bool(flags_offset & IP_MF)
    proto = buf[off + 9]
    declared_len = total_len - hdr_len
    start = off + hdr_len
    available = max(0, min(declared_len, len(buf) - start))
    payload = bytes(buf[start:start + available])
    return {
        'version': 4,
        'src': bytes(buf[off + 12:off + 16]),
        'dst': bytes(buf[off + 16:off + 20]),
        'ident': ident,
        'proto': proto,
        'offset': offset,
        'payload': payload,
        'declared_len': declared_len,
        'mf': mf,
        # Non-tail fragments must be a multiple of 8 bytes (RFC 791).  Keep
        # the flag for a structural check; the tail may be any length.
        'unaligned': bool(mf) and declared_len % 8 != 0,
        'truncated': available < declared_len,
    }


def _ipv6_chain(buf, off):
    """Walk IPv6 extension headers to the fragment header.

    Returns a fragment descriptor dict, or ``None`` when the packet is not a
    well-formed IPv6 packet carrying a fragment header.  Handles hop-by-hop,
    routing, destination-options and AH headers (including duplicates).
    """
    if len(buf) < off + 40 or (buf[off] >> 4) != 6:
        return None
    plen = _u16(buf, off + 4)
    nxt = buf[off + 6]
    # plen == 0 with a jumbogram option would need the hop-by-hop jumbo
    # length; fall back to the captured buffer rather than rejecting it.
    pkt_end = min(len(buf), off + 40 + plen) if plen else len(buf)
    pos = off + 40
    consumed = 0  # bytes of extension headers already walked
    while nxt in (0, 43, 44, 51, 60):
        if pos + 2 > pkt_end:
            return None
        if nxt == 44:  # fragment header, fixed 8 bytes
            if pos + 8 > pkt_end:
                return None
            frag_off_m = _u16(buf, pos + 2)
            frag_off = (frag_off_m >> 3) * 8
            m_flag = bool(frag_off_m & 0x1)
            ident = _u32(buf, pos + 4)
            frag_nxt = buf[pos]
            payload_off = pos + 8
            declared = plen - consumed - 8
            if declared < 0:
                return None
            avail = max(0, min(declared, pkt_end - payload_off))
            return {
                'payload_offset': payload_off,
                'payload': bytes(buf[payload_off:payload_off + avail]),
                'declared_len': declared,
                'truncated': avail < declared,
                'offset': frag_off,
                'mf': m_flag,
                'unaligned': bool(m_flag) and declared % 8 != 0,
                'ident': ident,
                'proto': frag_nxt,
                'version': 6,
                'src': bytes(buf[off + 8:off + 24]),
                'dst': bytes(buf[off + 24:off + 40]),
            }
        if nxt == 51:  # Authentication Header
            hlen = (buf[pos + 1] + 2) * 4
        else:  # hop-by-hop, routing, destination options
            hlen = (buf[pos + 1] + 1) * 8
        if hlen <= 0 or pos + hlen > pkt_end:
            return None
        consumed += hlen
        nxt = buf[pos]
        pos += hlen
    return None


def parse_record(buf, domain, record_index, timestamp, linktype, interface=0):
    """Extract fragment metadata from one raw capture record.

    Accepts an Ethernet/raw/cooked/loopback/PPP frame, returns a dict (or
    ``None`` when the record is not a fragment).  IPv4 fragments are
    recognized by MF or a non-zero offset; IPv6 fragments by a fragment header
    with MF or a non-zero offset (atomic fragments are skipped).  Payload
    bytes are sliced from the capture buffer so reassembly is byte-exact.
    """
    off = locate_ip(buf, linktype)
    if off is None or off >= len(buf):
        return None
    version = buf[off] >> 4
    if version == 4:
        info = _parse_ipv4(buf, off)
        if info is None:
            return None
        if not (info['mf'] or info['offset']):
            return None
    elif version == 6:
        info = _ipv6_chain(buf, off)
        if info is None:
            return None
        if not (info['mf'] or info['offset']):
            # Atomic fragment (RFC 6946): carries a fragment header but is a
            # complete datagram, not a member of a fragmented one.
            return None
    else:
        return None
    info['domain'] = _norm_domain(domain)
    info['interface'] = interface
    info['record_index'] = record_index
    info['timestamp'] = timestamp
    return info


class FragmentKey(object):
    """Capture domain + interface + direction + protocol + identification."""

    __slots__ = ('domain', 'interface', 'version', 'src', 'dst', 'ident', 'proto')

    def __init__(self, domain, interface, version, src, dst, ident, proto):
        self.domain = domain
        self.interface = interface
        self.version = version
        self.src = src
        self.dst = dst
        self.ident = ident
        self.proto = proto

    def as_tuple(self):
        return (self.domain, self.interface, self.version, self.src, self.dst,
                self.ident, self.proto)

    def __eq__(self, other):
        return isinstance(other, FragmentKey) and self.as_tuple() == other.as_tuple()

    def __hash__(self):
        return hash(self.as_tuple())

    def __repr__(self):
        return 'FragmentKey(domain=%r, iface=%r, v=%d, ident=%d, proto=%d)' % (
            self.domain, self.interface, self.version, self.ident, self.proto)


class SourceRef(object):
    """A capture record that touched a datagram."""

    __slots__ = ('domain', 'interface', 'index', 'timestamp', 'role', 'offset', 'length')

    def __init__(self, domain, interface, index, timestamp, role, offset=None, length=None):
        self.domain = domain
        self.interface = interface
        self.index = index
        self.timestamp = timestamp
        self.role = role
        self.offset = offset
        self.length = length

    def sort_key(self):
        return (self.domain, self.interface, self.timestamp, self.index, self.role)

    def to_dict(self):
        return {
            'domain': self.domain,
            # pcapng interface (capture card) index; 0 for libpcap files
            'interface': self.interface,
            # 0-based record index in the capture file
            'index': self.index,
            # 1-based frame number as shown by capture tools
            'frame': self.index + 1,
            'timestamp': self.timestamp,
            'role': self.role,
            'offset': self.offset,
            'length': self.length,
        }

    def __repr__(self):
        return 'SourceRef(%s[iface=%r] #%d %s)' % (self.domain, self.interface, self.index, self.role)


class FragGroup(object):
    """Accumulates fragments for one datagram identity."""

    def __init__(self, key):
        self.key = key
        self.fragments = []  # list of (start, bytes, SourceRef)
        self.sources = []    # all SourceRef, in arrival order
        self.duplicate_count = 0
        self.conflict = None
        self.conflict_fragments = []
        self.tail_seen = False
        self.total_len = None

    # ---- geometry helpers -------------------------------------------------
    def _accepted(self):
        return [f for f in self.fragments if f[2].role != 'conflict']

    def _accepted_coverage(self):
        return sorted((s, s + len(p)) for (s, p, _) in self._accepted())

    def effective_len(self):
        """Union length of accepted byte ranges; duplicates never add."""
        total = 0
        cursor = -1
        for (start, end) in self._accepted_coverage():
            if end <= cursor:
                continue
            if start > cursor:
                total += end - start
            else:
                total += end - cursor
            cursor = end
        return total

    def _covered(self, start, end):
        """True when [start, end) lies entirely inside accepted fragments."""
        ranges = self._accepted_coverage()
        span = start
        for (s, e) in ranges:
            if s > span:
                return False
            if e > span:
                span = e
            if span >= end:
                return True
        return span >= end

    def _overlap_conflict(self, start, payload):
        end = start + len(payload)
        for (s, old, ref) in self.fragments + self.conflict_fragments:
            oend = s + len(old)
            lo = max(start, s)
            hi = min(end, oend)
            if lo >= hi:
                continue
            new_slice = payload[lo - start:hi - start]
            old_slice = old[lo - s:hi - s]
            if new_slice != old_slice:
                return lo, hi, ref
        return None

    # ---- admission --------------------------------------------------------
    def _isolate(self, ref, message, start=None, payload=None):
        ref.role = 'conflict'
        if self.conflict is None:
            self.conflict = message
        if payload:
            self.conflict_fragments.append((start, payload, ref))
        return 'conflict'

    def _bounds_conflict(self, start, end, ref):
        """A fragment must fit inside a total length already fixed by a tail."""
        if self.total_len is None or end <= self.total_len:
            return None
        return ('fragment %s[iface=%d] #%d spans %d..%d beyond total length %d' %
                (ref.domain, ref.interface, ref.index, start, end - 1, self.total_len))

    def _reject_overruns(self):
        """Move accepted fragments that now exceed the total into the conflict set.

        This happens when the tail fragment arrives last and fixes a total
        shorter than bytes that were accepted earlier.  The datagram is
        isolated either way; moving the bytes keeps ``effective_len`` limited
        to non-conflicting data.
        """
        if self.total_len is None:
            return
        kept = []
        for frag in self.fragments:
            start, payload, ref = frag
            if start + len(payload) <= self.total_len:
                kept.append(frag)
                continue
            ref.role = 'conflict'
            self.conflict_fragments.append(frag)
            if self.conflict is None:
                self.conflict = ('fragment %s[iface=%d] #%d spans %d..%d beyond total '
                                 'length %d' %
                                 (ref.domain, ref.interface, ref.index, start,
                                  start + len(payload) - 1, self.total_len))
        self.fragments = kept

    def admit(self, info, ref):
        """Admit a fragment; returns 'new', 'duplicate' or 'conflict'."""
        start = info['offset']
        payload = info['payload']
        end = start + len(payload)
        if start % 8:
            return self._isolate(ref, 'fragment offset %d is not a multiple of 8 '
                                      '(%s[iface=%d] #%d)' %
                                 (start, ref.domain, ref.interface, ref.index))
        if info.get('unaligned'):
            return self._isolate(ref, 'non-tail fragment length %d is not a multiple '
                                      'of 8 (%s[iface=%d] #%d)' %
                                 (len(payload), ref.domain, ref.interface, ref.index))

        conflict = self._overlap_conflict(start, payload)
        if conflict is not None:
            lo, hi, other = conflict
            message = ('overlapping bytes disagree at %d..%d between %s[iface=%d] #%d '
                       'and %s[iface=%d] #%d' %
                       (lo, hi - 1, ref.domain, ref.interface, ref.index,
                        other.domain, other.interface, other.index))
            return self._isolate(ref, message, start, payload)

        if not info['mf']:
            new_total = end + max(0, info['declared_len'] - len(payload))
            if self.tail_seen and self.total_len is not None and new_total != self.total_len:
                message = ('tail fragments disagree on total length: %d != %d '
                           '(%s[iface=%d] #%d)' %
                           (self.total_len, new_total, ref.domain, ref.interface,
                            ref.index))
                return self._isolate(ref, message)
            self.tail_seen = True
            # Tail header length is authoritative; a truncated capture makes
            # the datagram incomplete, but the total length is still known.
            self.total_len = new_total
            self._reject_overruns()

        message = self._bounds_conflict(start, end, ref)
        if message is not None:
            return self._isolate(ref, message, start, payload)

        if payload and self._covered(start, end):
            self.duplicate_count += 1
            ref.role = 'duplicate'
            return 'duplicate'

        if payload:
            self.fragments.append((start, payload, ref))
        return 'new'

    # ---- output -----------------------------------------------------------
    def assemble(self):
        if self.conflict or not self.tail_seen or self.total_len is None:
            return None
        out = bytearray()
        for (start, payload, _) in sorted(self.fragments, key=lambda f: f[0]):
            if start > len(out):
                return None  # hole
            if start < len(out):
                payload = payload[len(out) - start:]
            out.extend(payload)
        if len(out) != self.total_len:
            return None
        return bytes(out)

    def gap_reason(self):
        if not self.tail_seen:
            return 'missing tail fragment: total length unknown'
        if self.total_len is None:
            return 'missing tail fragment: total length unknown'
        cursor = 0
        for (start, end) in self._accepted_coverage():
            if start > cursor:
                return 'hole at offset %d..%d' % (cursor, start - 1)
            cursor = max(cursor, end)
        if cursor < self.total_len:
            return 'capture truncated: %d of %d bytes present' % (cursor, self.total_len)
        elif cursor > self.total_len:
            return 'fragments extend past declared total length'
        return 'gap or overlap prevents assembly'

    def to_datagram(self):
        payload = self.assemble()
        if self.conflict:
            state = STATE_CONFLICT
            reason = self.conflict
        elif not self.tail_seen:
            state = STATE_INCOMPLETE
            reason = 'missing tail fragment: total length unknown'
        elif payload is None:
            state = STATE_INCOMPLETE
            reason = self.gap_reason()
        else:
            state = STATE_COMPLETE
            reason = None
        return Datagram(state, self.key, payload, self.total_len, self.effective_len(),
                        self.sources, self.duplicate_count, reason)


class Datagram(object):
    """A reassembled (or rejected) datagram with provenance."""

    __slots__ = ('state', 'key', 'payload', 'total_len', 'effective_len',
                 'sources', 'duplicate_count', 'reason')

    def __init__(self, state, key, payload, total_len, effective_len, sources,
                 duplicate_count, reason):
        self.state = state
        self.key = key
        self.payload = payload
        self.total_len = total_len
        self.effective_len = effective_len
        self.sources = sources
        self.duplicate_count = duplicate_count
        self.reason = reason

    @property
    def domain(self):
        return self.key.domain

    def records(self, role=None):
        refs = [s for s in self.sources if role is None or s.role == role]
        return sorted(refs, key=lambda r: r.sort_key())

    def transport_hdr(self):
        return parse_transport(self.payload, self.key.proto)

    def to_dict(self, include_payload=False):
        d = {
            'state': self.state,
            'reason': self.reason,
            # None means the tail fragment was absent; length was not guessed.
            'total_len': self.total_len,
            # Union of accepted byte ranges; replayed fragments add nothing.
            'effective_len': self.effective_len,
            'duplicate_fragments': self.duplicate_count,
            'key': {
                'domain': self.key.domain,
                'interface': self.key.interface,
                'version': self.key.version,
                'src': addr_to_str(self.key.src),
                'dst': addr_to_str(self.key.dst),
                'ident': self.key.ident,
                'proto': self.key.proto,
            },
            'transport': self.transport_hdr(),
            # Every capture record that touched this datagram, including
            # records rejected as duplicates or taking part in a conflict.
            'provenance': [s.to_dict() for s in self.records()],
        }
        if include_payload:
            d['payload_hex'] = (binascii.hexlify(self.payload).decode('ascii')
                                if self.payload is not None else None)
        return d


def addr_to_str(addr):
    from .utils import inet_to_str
    try:
        return inet_to_str(addr)
    except Exception:
        return repr(addr)


def parse_transport(payload, proto):
    """Return best-effort transport fields from a completed datagram."""
    if not payload:
        return None
    if proto == 6 and len(payload) >= 4:  # TCP
        hdr = {'proto': 'tcp', 'sport': _u16(payload, 0), 'dport': _u16(payload, 2)}
        if len(payload) >= 8:
            hdr['seq'] = _u32(payload, 4)
        return hdr
    if proto == 17 and len(payload) >= 4:  # UDP
        hdr = {'proto': 'udp', 'sport': _u16(payload, 0), 'dport': _u16(payload, 2)}
        if len(payload) >= 6:
            hdr['ulen'] = _u16(payload, 4)
        return hdr
    if proto == 1 and len(payload) >= 2:  # ICMP
        return {'proto': 'icmp', 'type': payload[0], 'code': payload[1]}
    if proto == 58 and len(payload) >= 2:  # ICMPv6
        return {'proto': 'icmp6', 'type': payload[0], 'code': payload[1]}
    return None


class ReassemblyReport(object):
    """Result of a reassembly pass."""

    def __init__(self, datagrams, records_seen, fragments_seen):
        self.datagrams = datagrams
        self.records_seen = records_seen
        self.fragments_seen = fragments_seen

    def datagrams_in(self, state):
        return [d for d in self.datagrams if d.state == state]

    def counts(self):
        return {
            STATE_COMPLETE: len(self.datagrams_in(STATE_COMPLETE)),
            STATE_INCOMPLETE: len(self.datagrams_in(STATE_INCOMPLETE)),
            STATE_CONFLICT: len(self.datagrams_in(STATE_CONFLICT)),
        }

    def to_dict(self, include_payload=False):
        return {
            'records_seen': self.records_seen,
            'fragments_seen': self.fragments_seen,
            'counts': self.counts(),
            'datagrams': [d.to_dict(include_payload=include_payload) for d in self.datagrams],
        }


class Reassembler(object):
    """Incremental reassembler over `(timestamp, buf)` capture records."""

    def __init__(self):
        self._groups = []
        self._index = {}
        self._domain_counts = {}
        self.records_seen = 0
        self.fragments_seen = 0

    def add_record(self, domain, timestamp, buf, linktype=_DLT_EN10MB, index=None,
                   interface=0):
        """Add one capture record.  Returns groups completed by this record.

        ``interface`` distinguishes capture interfaces/cards inside a pcapng
        file.  Two interfaces that carry the same identification value remain
        separate capture domains for grouping purposes.

        The returned list is provisional: a later conflicting fragment can
        invalidate a datagram that looked complete here.  Use ``report()``
        for the authoritative result of the whole capture.
        """
        domain = _norm_domain(domain)
        if index is None:
            # Provenance indices are per capture domain so they match frame
            # numbers in that capture file even when several files are mixed.
            idx = self._domain_counts.get(domain, 0)
        else:
            idx = index
        self._domain_counts[domain] = idx + 1
        self.records_seen += 1
        info = parse_record(buf, domain, idx, timestamp, linktype, interface=interface)
        if info is None:
            return []
        self.fragments_seen += 1

        key = FragmentKey(info['domain'], info['interface'], info['version'],
                          info['src'], info['dst'], info['ident'], info['proto'])
        group = self._index.get(key)
        if group is None:
            group = FragGroup(key)
            self._index[key] = group
            self._groups.append(group)

        ref = SourceRef(info['domain'], info['interface'], idx, timestamp, 'payload',
                        info['offset'], len(info['payload']))
        group.sources.append(ref)
        group.admit(info, ref)

        # Provisional completion only; ``report()`` is authoritative because
        # a later fragment may still turn this datagram into a conflict.
        probe = group.to_datagram()
        if probe.state == STATE_COMPLETE:
            return [probe]
        return []

    def report(self):
        return ReassemblyReport([g.to_datagram() for g in self._groups],
                                self.records_seen, self.fragments_seen)


PCAPNG_SHB_MAGIC = b'\x0a\x0d\x0d\x0a'
_PCAPNG_EPB = 0x00000006
_PCAPNG_PB = 0x00000002
_PCAPNG_SPB = 0x00000003
_PCAPNG_IDB = 0x00000001


def _iter_pcapng(f, path):
    """Iterate a pcapng stream, preserving interface (capture card) identity.

    Yields ``(domain, interface, index, timestamp, buf, linktype)``.  Each
    Interface Description Block contributes its own interface identifier, and
    two files/interfaces that happen to use the same IP identification value
    still form separate groups.
    """
    domain = _norm_domain(path)
    endian = None
    interfaces = []

    def read_block():
        head = f.read(8)
        if len(head) < 8:
            return None
        btype_le, blen_le = struct.unpack('<II', head)
        # The Section Header Block's type is endian-independent.
        if btype_le == 0x0A0D0D0A:
            bom_le = f.read(4)
            if len(bom_le) < 4:
                return None
            if struct.unpack('<I', bom_le)[0] == 0x1A2B3C4D:
                e = '<'
            elif struct.unpack('>I', bom_le)[0] == 0x1A2B3C4D:
                e = '>'
            else:
                raise ValueError('invalid pcapng byte order magic')
            blen = struct.unpack(e + 'I', head[4:8])[0]
            if blen < 12:
                raise ValueError('invalid pcapng section length')
            body = bom_le + f.read(blen - 12)
            if len(body) < blen - 12:
                return None
            return e, 0x0A0D0D0A, body
        if endian is None:
            raise ValueError('pcapng file does not start with a section header')
        btype = struct.unpack(endian + 'I', head[0:4])[0]
        blen = struct.unpack(endian + 'I', head[4:8])[0]
        if blen < 12:
            raise ValueError('invalid pcapng block length')
        body = f.read(blen - 8)
        if len(body) < blen - 8:
            return None
        return endian, btype, body

    index = 0
    while True:
        block = read_block()
        if block is None:
            return
        endian, btype, body = block

        if btype == 0x0A0D0D0A:
            interfaces = []
            continue
        if btype == _PCAPNG_IDB:
            if len(body) < 8:
                continue
            linktype, _reserved, _snaplen = struct.unpack(endian + 'HHI', body[:8])
            divisor = 1000000
            tsoffset = 0
            opt = body[8:len(body) - 4]
            while len(opt) >= 4:
                code, olen = struct.unpack(endian + 'HH', opt[:4])
                data = opt[4:4 + olen]
                if code == 0:
                    break
                if code == 9 and data:  # if_tsresol
                    val = data[0]
                    divisor = (2 if val & 0x80 else 10) ** (val & 0x7f)
                elif code == 14 and len(data) >= 8:  # if_tsoffset
                    tsoffset = struct.unpack(endian + 'q', data[:8])[0]
                opt = opt[4 + ((olen + 3) // 4) * 4:]
            interfaces.append((linktype, divisor, tsoffset))
            continue
        if btype == _PCAPNG_EPB and len(body) >= 20:
            iface, ts_high, ts_low, caplen, _pktlen = struct.unpack(endian + 'IIIII', body[:20])
            buf = body[20:20 + caplen]
            linktype, divisor, tsoffset = (interfaces[iface] if iface < len(interfaces)
                                           else (_DLT_EN10MB, 1000000, 0))
            ts = tsoffset + (((ts_high << 32) | ts_low) / divisor)
            yield domain, iface, index, ts, buf, linktype
            index += 1
            continue
        if btype == _PCAPNG_PB and len(body) >= 20:
            iface, _drops, ts_high, ts_low, caplen, _pktlen = struct.unpack(endian + 'HHIIII', body[:20])
            buf = body[20:20 + caplen]
            linktype, divisor, tsoffset = (interfaces[iface] if iface < len(interfaces)
                                           else (_DLT_EN10MB, 1000000, 0))
            ts = tsoffset + (((ts_high << 32) | ts_low) / divisor)
            yield domain, iface, index, ts, buf, linktype
            index += 1
            continue
        if btype == _PCAPNG_SPB and len(body) >= 4:
            # Simple Packet Block: no interface id or timestamp; interface 0.
            orig_len = struct.unpack(endian + 'I', body[:4])[0]
            buf = body[4:4 + min(orig_len, len(body) - 8)]
            linktype = interfaces[0][0] if interfaces else _DLT_EN10MB
            yield domain, 0, index, 0.0, buf, linktype
            index += 1
            continue


def _iter_path(path):
    with open(path, 'rb') as f:
        head = f.read(4)
        f.seek(0)
        if head == PCAPNG_SHB_MAGIC:
            for rec in _iter_pcapng(f, path):
                yield rec
            return
        reader = pcap.Reader(f)
        domain = _norm_domain(path)
        try:
            linktype = reader.datalink()
        except Exception:
            linktype = _DLT_EN10MB
        for index, (timestamp, buf) in enumerate(reader):
            yield domain, 0, index, timestamp, buf, linktype


def _iter_reader(reader, domain):
    domain = _norm_domain(domain)
    try:
        linktype = reader.datalink()
    except Exception:
        linktype = _DLT_EN10MB
    for index, (timestamp, buf) in enumerate(reader):
        yield domain, 0, index, timestamp, buf, linktype


def reassemble(captures, reorder=False):
    """Reassemble fragmented IP datagrams from capture sources.

    Args:
        captures: iterable whose items are
            * paths to pcap/pcapng files,
            * opened Reader objects (then the reader's ``name`` is the domain),
            * iterables of ``(timestamp, buffer)`` pairs; these are assigned a
              synthetic ``<stream-N>`` capture domain.
        reorder: sort records by ``(domain, timestamp, index)`` before
            reassembly.  The set of output datagrams is unchanged; only source
            ordering is canonicalized.

    Returns:
        ReassemblyReport
    """
    seen_domains = {}

    def records():
        for item in captures:
            if isinstance(item, _PATH_TYPES):
                for rec in _iter_path(item):
                    yield rec
            elif isinstance(item, pcap.Reader) or hasattr(item, 'datalink'):
                domain = getattr(item, 'name', '<reader>')
                for rec in _iter_reader(item, domain):
                    yield rec
            else:
                # iterable of (timestamp, buf)
                domain = '<stream-%d>' % len(seen_domains)
                seen_domains[domain] = True
                for index, pair in enumerate(item):
                    ts, buf = pair
                    yield domain, 0, index, ts, buf, _DLT_EN10MB

    stream = list(records())
    if reorder:
        # (domain, interface, timestamp, record index)
        stream.sort(key=lambda r: (r[0], r[1], r[3], r[2]))

    r = Reassembler()
    for domain, interface, index, ts, buf, linktype in stream:
        r.add_record(domain, ts, buf, linktype=linktype, index=index,
                     interface=interface)
    return r.report()


def reassemble_file(path, reorder=False):
    return reassemble([path], reorder=reorder)


def format_report(report, show_bytes=False):
    """Stable human-readable rendering of a report."""
    lines = ['capture records: %d, fragments: %d' % (report.records_seen, report.fragments_seen)]
    counts = report.counts()
    lines.append('complete: %d, incomplete: %d, conflict: %d' % (
        counts[STATE_COMPLETE], counts[STATE_INCOMPLETE], counts[STATE_CONFLICT]))
    for d in report.datagrams:
        k = d.key
        lines.append('%s %s -> %s id=%d proto=%d total=%s effective=%d dup=%d domain=%s iface=%d' % (
            d.state, addr_to_str(k.src), addr_to_str(k.dst), k.ident, k.proto,
            '-' if d.total_len is None else d.total_len, d.effective_len,
            d.duplicate_count, k.domain, k.interface))
        if d.reason:
            lines.append('  reason: %s' % d.reason)
        transport = d.transport_hdr()
        if transport:
            lines.append('  transport: %s' % json.dumps(transport, sort_keys=True))
        for s in d.records():
            lines.append('  from %s[iface=%d] #%d @ %r (%s @ %s+%d)' % (
                s.domain, s.interface, s.index, s.timestamp, s.role,
                '-' if s.offset is None else s.offset,
                '-' if s.length is None else s.length))
        if show_bytes and d.payload is not None:
            lines.append('  payload: %s' % binascii.hexlify(d.payload).decode('ascii'))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m dpkt.defrag',
        description='Offline IP fragment reassembly with capture provenance.')
    parser.add_argument('captures', nargs='+', help='pcap/pcapng capture files')
    parser.add_argument('--json', action='store_true', help='emit JSON')
    parser.add_argument('--bytes', action='store_true', help='include payload bytes')
    parser.add_argument('--reorder', action='store_true', help='sort records before reassembly')
    args = parser.parse_args(argv)

    report = reassemble(args.captures, reorder=args.reorder)
    if args.json:
        print(json.dumps(report.to_dict(include_payload=args.bytes), indent=2, sort_keys=True))
    else:
        print(format_report(report, show_bytes=args.bytes))
    return 0


if __name__ == '__main__':
    sys.exit(main())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
# These tests build frames byte-by-byte rather than using dpkt's serializers so
# fragment boundaries and headers stay exactly as intended.
_SRC = b'\x0a\x00\x00\x01'
_DST = b'\x0a\x00\x00\x02'
_SRC6 = b'\xfe\x80' + b'\x00' * 14
_DST6 = b'\xfe\x81' + b'\x00' * 14


def _ipv4_frame(payload, ident=1, proto=17, offset=0, mf=False, hdr_len=20,
                src=_SRC, dst=_DST, total_len=None, vlan=None):
    """Build an Ethernet/IPv4 frame with the given fragment fields."""
    if total_len is None:
        total_len = hdr_len + len(payload)
    flags = (IP_MF if mf else 0) | (offset // 8)
    hdr = struct.pack('>BBHHHBBH4s4s', (4 << 4) | (hdr_len // 4), 0, total_len,
                      ident, flags, 64, proto, 0, src, dst)
    hdr += b'\x00' * (hdr_len - 20)
    frame = b'\x00\x11\x22\x33\x44\x55' + b'\x66\x77\x88\x99\xaa\xbb'
    if vlan is not None:
        frame += struct.pack('>HH', 0x8100, vlan)
    frame += struct.pack('>H', 0x0800) + hdr + payload
    return frame


def _ipv6_frame(payload, ident=1, nxt=17, offset=0, mf=False, pre_ext=b'',
                first_nxt=44, src=_SRC6, dst=_DST6):
    frag_off_m = (offset // 8) << 3 | (1 if mf else 0)
    frag = struct.pack('>BBHI', nxt, 0, frag_off_m, ident)
    plen = len(pre_ext) + 8 + len(payload)
    hdr = struct.pack('>IHBB16s16s', 0x60000000, plen, first_nxt, 64, src, dst)
    return b'\x00\x11\x22\x33\x44\x55' + b'\x66\x77\x88\x99\xaa\xbb' + \
        struct.pack('>H', 0x86dd) + hdr + pre_ext + frag + payload


def _feed(reassembler, frames, domain='cap.pcap'):
    for i, frame in enumerate(frames):
        reassembler.add_record(domain, float(i), frame)
    return reassembler.report()


def test_ipv4_reassembly_and_provenance():
    data = bytes(range(48))
    frames = [
        _ipv4_frame(data[0:24], ident=77, offset=0, mf=True),
        _ipv4_frame(data[24:40], ident=77, offset=24, mf=True),
        _ipv4_frame(data[40:48], ident=77, offset=40, mf=False),
    ]
    report = _feed(Reassembler(), frames)
    assert report.counts() == {STATE_COMPLETE: 1, STATE_INCOMPLETE: 0, STATE_CONFLICT: 0}
    d = report.datagrams[0]
    assert d.state == STATE_COMPLETE
    assert d.payload == data
    assert d.total_len == 48
    assert d.effective_len == 48
    assert [r.index for r in d.records()] == [0, 1, 2]
    assert [r.offset for r in d.records()] == [0, 24, 40]
    assert [r.length for r in d.records()] == [24, 16, 8]
    # record references are 1-based in the "frame" field
    assert [r.to_dict()['frame'] for r in d.records()] == [1, 2, 3]


def test_order_independent():
    data = bytes(range(40))
    frames = [
        _ipv4_frame(data[32:40], ident=5, offset=32, mf=False),
        _ipv4_frame(data[0:16], ident=5, offset=0, mf=True),
        _ipv4_frame(data[16:32], ident=5, offset=16, mf=True),
    ]
    a = _feed(Reassembler(), frames).datagrams[0]
    b = _feed(Reassembler(), list(reversed(frames))).datagrams[0]
    assert a.state == b.state == STATE_COMPLETE
    assert a.payload == b.payload == data


def test_capture_domain_separates_same_ident():
    """Two capture cards using the same ID must not be merged."""
    data_a = b'A' * 32
    data_b = b'B' * 32
    r = Reassembler()
    r.add_record('card-a.pcap', 1.0, _ipv4_frame(data_a[0:16], ident=9, offset=0, mf=True))
    r.add_record('card-b.pcap', 1.0, _ipv4_frame(data_b[0:16], ident=9, offset=0, mf=True))
    r.add_record('card-a.pcap', 2.0, _ipv4_frame(data_a[16:32], ident=9, offset=16, mf=False))
    r.add_record('card-b.pcap', 2.0, _ipv4_frame(data_b[16:32], ident=9, offset=16, mf=False))
    report = r.report()
    assert report.counts()[STATE_COMPLETE] == 2
    by_domain = {d.key.domain: d.payload for d in report.datagrams}
    assert by_domain['card-a.pcap'] == data_a
    assert by_domain['card-b.pcap'] == data_b


def test_direction_separates_datagrams():
    payload = b'x' * 16
    r = Reassembler()
    r.add_record('cap', 1, _ipv4_frame(payload, ident=3, proto=17, offset=0, mf=True))
    r.add_record('cap', 2, _ipv4_frame(payload, ident=3, proto=17, offset=0, mf=True, src=_DST, dst=_SRC))
    report = r.report()
    assert report.counts()[STATE_INCOMPLETE] == 2
    assert {tuple(d.key.as_tuple()[3:5]) for d in report.datagrams} == {(_SRC, _DST), (_DST, _SRC)}


def test_protocol_separates_datagrams():
    payload = b'p' * 8
    r = Reassembler()
    r.add_record('cap', 1, _ipv4_frame(payload, ident=4, proto=17, offset=0, mf=True))
    r.add_record('cap', 2, _ipv4_frame(payload, ident=4, proto=6, offset=0, mf=True))
    report = r.report()
    assert report.counts()[STATE_INCOMPLETE] == 2
    assert {d.key.proto for d in report.datagrams} == {6, 17}


def test_overlap_conflict_isolated():
    r = Reassembler()
    r.add_record('cap', 1, _ipv4_frame(b'A' * 16, ident=8, offset=0, mf=True))
    r.add_record('cap', 2, _ipv4_frame(b'B' * 16, ident=8, offset=0, mf=True))
    r.add_record('cap', 3, _ipv4_frame(b'C' * 16, ident=8, offset=16, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_CONFLICT
    assert d.payload is None
    assert 'overlapping bytes disagree' in d.reason
    # both sides of the disagreement are named in provenance
    assert {rec.index for rec in d.records()} == {0, 1, 2}
    # conflicting bytes never inflate the effective length
    assert d.effective_len == 32


def test_partial_overlap_agreement_is_not_conflict():
    payload = bytes(range(40))
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(payload[0:24], ident=10, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(payload[16:32], ident=10, offset=16, mf=True))
    r.add_record('cap', 2, _ipv4_frame(payload[32:40], ident=10, offset=32, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_COMPLETE
    assert d.payload == payload
    assert d.effective_len == 40


def test_missing_tail_not_guessed():
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(b'Q' * 24, ident=12, offset=0, mf=True))
    d = r.report().datagrams[0]
    assert d.state == STATE_INCOMPLETE
    assert d.total_len is None
    assert d.payload is None
    assert d.effective_len == 24
    assert 'tail' in d.reason


def test_zero_length_tail_marks_complete():
    payload = b'Z' * 16
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(payload, ident=13, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(b'', ident=13, offset=16, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_COMPLETE
    assert d.payload == payload
    assert d.total_len == 16


def test_hole_before_tail_is_incomplete():
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(b'A' * 16, ident=14, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(b'C' * 8, ident=14, offset=32, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_INCOMPLETE
    assert d.total_len == 40
    assert d.payload is None
    assert 'hole' in d.reason


def test_duplicates_do_not_add_length():
    payload = b'D' * 32
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(payload[0:16], ident=15, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(payload[0:16], ident=15, offset=0, mf=True))
    r.add_record('cap', 2, _ipv4_frame(payload[0:8], ident=15, offset=0, mf=True))
    r.add_record('cap', 3, _ipv4_frame(payload[16:32], ident=15, offset=16, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_COMPLETE
    assert d.payload == payload
    assert d.effective_len == 32
    assert d.duplicate_count == 2
    assert len(d.records(role='duplicate')) == 2
    # duplicates still appear in provenance
    assert len(d.records()) == 4


def test_duplicate_tail_does_not_change_length():
    payload = b'E' * 24
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(payload[0:16], ident=16, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(payload[16:24], ident=16, offset=16, mf=False))
    r.add_record('cap', 2, _ipv4_frame(payload[16:24], ident=16, offset=16, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_COMPLETE
    assert d.total_len == 24
    assert d.effective_len == 24


def test_ipv6_reassembly():
    data = bytes(range(32))
    r = Reassembler()
    r.add_record('v6.pcap', 0, _ipv6_frame(data[0:8], ident=0xdeadbeef, offset=0, mf=True))
    r.add_record('v6.pcap', 1, _ipv6_frame(data[8:24], ident=0xdeadbeef, offset=8, mf=True))
    r.add_record('v6.pcap', 2, _ipv6_frame(data[24:32], ident=0xdeadbeef, offset=24, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_COMPLETE
    assert d.payload == data
    assert d.key.version == 6
    assert d.key.proto == 17


def test_ipv6_hop_by_hop_before_fragment():
    data = bytes(range(32))
    hop = bytes([44, 0]) + b'\x01\x04\x00\x00\x00\x00'  # PadN, next=fragment
    frames = [
        _ipv6_frame(data[0:16], ident=21, offset=0, mf=True, pre_ext=hop, first_nxt=0),
        _ipv6_frame(data[16:32], ident=21, offset=16, mf=False, pre_ext=hop, first_nxt=0),
    ]
    d = _feed(Reassembler(), frames).datagrams[0]
    assert d.state == STATE_COMPLETE
    assert d.payload == data


def test_ipv6_atomic_fragment_is_skipped():
    r = Reassembler()
    r.add_record('v6.pcap', 0, _ipv6_frame(b'atomic', ident=1, offset=0, mf=False))
    report = r.report()
    assert report.counts() == {STATE_COMPLETE: 0, STATE_INCOMPLETE: 0, STATE_CONFLICT: 0}
    assert report.fragments_seen == 0


def test_non_fragments_are_ignored():
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(b'not a fragment', ident=1, offset=0, mf=False))
    report = r.report()
    assert report.records_seen == 1
    assert report.fragments_seen == 0
    assert report.datagrams == []


def test_fragment_beyond_total_is_conflict():
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(b'A' * 8, ident=17, offset=8, mf=False))
    # 24 bytes at offset 0 agree on the overlap but extend past the 16-byte
    # total fixed by the tail fragment.
    overrun = b'B' * 8 + b'A' * 8 + b'X' * 8
    r.add_record('cap', 1, _ipv4_frame(overrun, ident=17, offset=0, mf=True))
    d = r.report().datagrams[0]
    assert d.state == STATE_CONFLICT
    assert 'beyond total length' in d.reason


def test_tail_length_disagreement_is_conflict():
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(b'A' * 8, ident=18, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(b'B' * 8, ident=18, offset=16, mf=False))
    r.add_record('cap', 2, _ipv4_frame(b'C' * 8, ident=18, offset=24, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_CONFLICT
    assert 'tail fragments disagree' in d.reason


def test_unaligned_non_tail_fragment_isolated():
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(b'A' * 12, ident=31, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(b'B' * 8, ident=31, offset=16, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_CONFLICT
    assert 'multiple of 8' in d.reason


def test_tail_arriving_last_isolates_earlier_overrun():
    r = Reassembler()
    # 24 bytes accepted before any tail is known.  Bytes 8..16 agree with the
    # tail that arrives later, so only the structural overrun is in question.
    r.add_record('cap', 0, _ipv4_frame(b'A' * 8 + b'B' * 16, ident=30, offset=0, mf=True))
    # The tail fixes the total at 16; the earlier fragment overruns it.
    r.add_record('cap', 1, _ipv4_frame(b'B' * 8, ident=30, offset=8, mf=False))
    d = r.report().datagrams[0]
    assert d.state == STATE_CONFLICT
    assert d.total_len == 16
    assert 'beyond total length' in d.reason
    # overrunning bytes are not counted as effective
    assert d.effective_len == 8


def test_truncated_capture_keeps_declared_total():
    r = Reassembler()
    frame = _ipv4_frame(b'B' * 8, ident=19, offset=16, mf=False, total_len=36)
    r.add_record('cap', 0, _ipv4_frame(b'A' * 16, ident=19, offset=0, mf=True))
    r.add_record('cap', 1, frame[:14 + 20 + 4])  # only 4 of 16 payload bytes captured
    d = r.report().datagrams[0]
    # The IP total-length field describes the tail fragment, so the datagram
    # total is 16 (offset) + 16 (declared payload) = 32, not the captured 20.
    assert d.state == STATE_INCOMPLETE
    assert d.total_len == 32
    assert d.effective_len == 20
    assert d.payload is None


def test_report_to_dict_is_json_serializable():
    import json
    data = b'J' * 16
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(data, ident=20, offset=0, mf=True))
    r.add_record('cap', 1, _ipv4_frame(b'', ident=20, offset=16, mf=False))
    out = r.report().to_dict(include_payload=True)
    json.dumps(out)  # must not raise
    entry = out['datagrams'][0]
    assert entry['state'] == STATE_COMPLETE
    assert entry['key']['src'] == '10.0.0.1'
    assert entry['total_len'] == 16
    assert entry['provenance'][0]['frame'] == 1
    assert entry['payload_hex'] == binascii.hexlify(data).decode('ascii')


def test_effective_len_replay_is_idempotent():
    payload = b'R' * 40
    frames = [
        _ipv4_frame(payload[0:16], ident=22, offset=0, mf=True),
        _ipv4_frame(payload[16:32], ident=22, offset=16, mf=True),
        _ipv4_frame(payload[32:40], ident=22, offset=32, mf=False),
    ]
    one = _feed(Reassembler(), frames).datagrams[0]
    two = _feed(Reassembler(), frames + frames).datagrams[0]
    assert one.effective_len == two.effective_len == 40
    assert two.duplicate_count == 3
    assert two.state == STATE_COMPLETE


def test_identical_captures_from_two_cards_are_separate():
    payload = b'S' * 24
    frames = [
        _ipv4_frame(payload[0:16], ident=23, offset=0, mf=True),
        _ipv4_frame(payload[16:24], ident=23, offset=16, mf=False),
    ]
    r = Reassembler()
    for domain in ('card-1.pcap', 'card-2.pcap'):
        for i, frame in enumerate(frames):
            r.add_record(domain, float(i), frame)
    report = r.report()
    assert report.counts()[STATE_COMPLETE] == 2
    assert {d.key.domain for d in report.datagrams} == {'card-1.pcap', 'card-2.pcap'}
    for d in report.datagrams:
        assert d.duplicate_count == 0
        assert [rec.index for rec in d.records()] == [0, 1]


def test_pcap_file_roundtrip():
    payload = b'T' * 32
    frames = [
        _ipv4_frame(payload[0:16], ident=24, offset=0, mf=True),
        _ipv4_frame(payload[16:32], ident=24, offset=16, mf=False),
    ]
    fobj = BytesIO()
    w = pcap.Writer(fobj)
    for i, frame in enumerate(frames):
        w.writepkt(frame, ts=100 + i)
    path = _write_temp(fobj.getvalue(), '.pcap')
    try:
        report = reassemble_file(path)
    finally:
        os.unlink(path)
    assert report.records_seen == 2
    assert report.datagrams[0].state == STATE_COMPLETE
    assert report.datagrams[0].payload == payload
    assert report.datagrams[0].key.domain == os.path.normpath(path)


def test_pcapng_file_roundtrip_with_two_interfaces():
    """Two capture cards in one pcapng file keep separate domains."""
    payload_a = b'U' * 24
    payload_b = b'V' * 24
    fobj = BytesIO()
    from . import pcapng
    idb1 = pcapng.InterfaceDescriptionBlockLE(snaplen=1500, linktype=pcap.DLT_EN10MB)
    idb2 = pcapng.InterfaceDescriptionBlockLE(snaplen=1500, linktype=pcap.DLT_EN10MB)
    w = pcapng.Writer(fobj, idb=[idb1, idb2])
    for iface, payload in ((0, payload_a), (1, payload_b)):
        for off, mf, chunk in ((0, True, payload[0:16]), (16, False, payload[16:24])):
            epb = pcapng.EnhancedPacketBlockLE(
                iface_id=iface, caplen=len(_ipv4_frame(chunk, ident=25, offset=off, mf=mf)),
                pkt_len=len(_ipv4_frame(chunk, ident=25, offset=off, mf=mf)),
                pkt_data=_ipv4_frame(chunk, ident=25, offset=off, mf=mf))
            w.writepkt(epb, ts=100.0)
    path = _write_temp(fobj.getvalue(), '.pcapng')
    try:
        report = reassemble_file(path)
    finally:
        os.unlink(path)
    assert report.counts()[STATE_COMPLETE] == 2
    assert {d.key.interface for d in report.datagrams} == {0, 1}
    assert {d.payload for d in report.datagrams} == {payload_a, payload_b}


def test_reassemble_accepts_reader_objects():
    payload = b'W' * 16
    fobj = BytesIO()
    w = pcap.Writer(fobj)
    w.writepkt(_ipv4_frame(payload, ident=26, offset=0, mf=True), ts=1.0)
    w.writepkt(_ipv4_frame(b'', ident=26, offset=16, mf=False), ts=2.0)
    fobj.seek(0)
    report = reassemble([pcap.Reader(fobj)])
    assert report.datagrams[0].state == STATE_COMPLETE
    assert report.datagrams[0].payload == payload


def test_vlan_tagged_fragments():
    payload = b'X' * 16
    r = Reassembler()
    r.add_record('cap', 0, _ipv4_frame(payload, ident=27, offset=0, mf=True, vlan=42))
    r.add_record('cap', 1, _ipv4_frame(b'', ident=27, offset=16, mf=False, vlan=42))
    assert r.report().datagrams[0].payload == payload


def test_malformed_records_do_not_raise():
    r = Reassembler()
    r.add_record('cap', 0, b'')
    r.add_record('cap', 1, b'\x00' * 4)
    r.add_record('cap', 2, b'\xff' * 100)
    report = r.report()
    assert report.fragments_seen == 0
    assert report.datagrams == []


def test_format_report_mentions_sources():
    payload = b'Y' * 16
    r = Reassembler()
    r.add_record('card.pcap', 0, _ipv4_frame(payload, ident=28, offset=0, mf=True))
    r.add_record('card.pcap', 1, _ipv4_frame(b'', ident=28, offset=16, mf=False))
    text = format_report(r.report())
    assert 'card.pcap' in text
    assert 'complete' in text
    assert 'from card.pcap' in text


def test_cli_json_output():
    payload = b'Z' * 16
    fobj = BytesIO()
    w = pcap.Writer(fobj)
    w.writepkt(_ipv4_frame(payload, ident=29, offset=0, mf=True), ts=1.0)
    w.writepkt(_ipv4_frame(b'', ident=29, offset=16, mf=False), ts=2.0)
    path = _write_temp(fobj.getvalue(), '.pcap')
    out = BytesIO()
    old = sys.stdout
    try:
        sys.stdout = _TextSink(out)
        main([path, '--json'])
    finally:
        sys.stdout = old
        os.unlink(path)
    parsed = json.loads(out.getvalue().decode('utf-8'))
    assert parsed['counts'][STATE_COMPLETE] == 1


class _TextSink(object):
    """Minimal text stream wrapper for capturing ``print`` in py2 and py3."""

    def __init__(self, raw):
        self.raw = raw

    def write(self, text):
        if not isinstance(text, bytes):
            text = text.encode('utf-8')
        self.raw.write(text)

    def flush(self):
        pass


def _write_temp(data, suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    return path
