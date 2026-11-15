# -*- coding: utf-8 -*-
"""TLS handshake archival from packet captures.

A capture is re-assembled in three strictly separated layers:

1. TCP sequence number re-assembly (per direction). Bytes are only handed
   upwards when every preceding sequence number is present. A gap in the
   capture can therefore never be silently bridged: data sitting behind a
   missing sequence range stays queued, even when its first bytes happen to
   look like a plausible TLS record header.
2. TLS record framing on top of the gap-free byte stream. A record may span
   several TCP segments / packets and one segment may contain several
   records.
3. TLS handshake framing on top of handshake (content type 22) records. A
   handshake message may span several records (including the middle of its
   4-byte header) and one record may contain several handshake messages.

Every emitted handshake message keeps the provenance of its bytes: the
original 1-based capture packet numbers of every TLS record it touched, and
the per-record provenance. The two directions of a connection are numbered
independently. TCP retransmissions occupy sequence numbers that have already
been consumed, so they never produce a second handshake entry.
"""
from __future__ import absolute_import

import struct

from . import dpkt
from . import ethernet
from . import ip
from . import ip6
from . import loopback
from . import pcap
from . import pcapng
from . import sll
from . import sll2
from . import ssl
from . import tcp as tcp_mod
from .compat import compat_ord
from .utils import inet_to_str

# This implementation assumes a single TCP direction never carries more than
# 2**31 bytes at once, which holds for every real TLS capture.
_SEQ_MASK = 0xffffffff
_SEQ_HALF = 0x80000000

_TLS_RECORD_HEADER_LEN = 5
_TLS_HANDSHAKE_HEADER_LEN = 4
_TLS_CONTENT_HANDSHAKE = 22

_PCAPNG_MAGIC = b'\x0a\x0d\x0d\x0a'


class TLSArchiveError(dpkt.Error):
    """Raised when a capture cannot be archived (bad link type, ...)."""


def _seq_less(left, right):
    """Modular 32-bit TCP sequence comparison: left < right."""
    return (left - right) & _SEQ_MASK >= _SEQ_HALF


class _OriginBuffer(object):
    """Byte buffer that remembers where every byte came from.

    Chunks are ``[packet_index, payload_offset, data, consumed, record_index]``
    lists; ``consumed`` counts how many leading bytes of ``data`` are used.
    ``record_index`` is the 1-based TLS record the bytes belong to, or
    ``None`` for bytes consumed at record-header level.
    """

    def __init__(self):
        self._chunks = []
        self._length = 0

    def append(self, packet_index, payload_offset, data, record_index=None):
        if data:
            self._chunks.append(
                [packet_index, payload_offset, data, 0, record_index])
            self._length += len(data)

    def __len__(self):
        return self._length

    def peek_bytes(self, count):
        out = bytearray()
        for chunk in self._chunks:
            remaining = len(chunk[2]) - chunk[3]
            take = min(remaining, count - len(out))
            out += chunk[2][chunk[3]:chunk[3] + take]
            if len(out) == count:
                break
        return bytes(out)

    def consume(self, count):
        """Remove ``count`` bytes.

        Returns ``(data, spans, record_indices)`` where ``spans`` is a tuple
        of ``(packet_index, payload_offset, span_bytes)`` triples (adjacent
        spans of the same packet are merged) and ``record_indices`` is the
        sorted tuple of TLS record indices the bytes belonged to.
        """
        if count > self._length:
            raise dpkt.NeedData('not enough buffered bytes')
        data = bytearray()
        raw_spans = []
        record_ids = set()
        remaining = count
        while remaining > 0:
            chunk = self._chunks[0]
            available = len(chunk[2]) - chunk[3]
            take = min(available, remaining)
            span = chunk[2][chunk[3]:chunk[3] + take]
            data += span
            raw_spans.append((chunk[0], chunk[1] + chunk[3], span,
                              chunk[4]))
            if chunk[4] is not None:
                record_ids.add(chunk[4])
            chunk[3] += take
            self._length -= take
            remaining -= take
            if chunk[3] == len(chunk[2]):
                self._chunks.pop(0)

        spans = []
        for packet_index, offset, span, record_index in raw_spans:
            if spans and spans[-1][0] == packet_index and \
                    spans[-1][1] + len(spans[-1][2]) == offset and \
                    spans[-1][3] == record_index:
                previous = spans[-1]
                spans[-1] = (previous[0], previous[1],
                             previous[2] + span, record_index)
            else:
                spans.append((packet_index, offset, span, record_index))
        return bytes(data), tuple(spans), tuple(sorted(record_ids))


class _TcpReassembler(object):
    """Re-assembles one TCP direction strictly by sequence number.

    Segment positions are stored relative to ``base``, the next expected
    sequence number. ``drain()`` only releases segments that start at
    position 0, so data behind a gap is never released until the gap has been
    filled (typically by a retransmission of the missing range).
    Retransmitted bytes overlap already consumed/queued ranges and are merged
    away here, surfacing at most once to the layers above.
    """

    def __init__(self):
        self.base = None
        self.syn_seen = False
        self.fin_seen = False
        self.reset_seen = False
        self.gap_events = 0
        self._in_gap = False
        # each segment: [pos, end, data, packet_index, payload_offset]
        self._segments = []

    def handle_syn(self, seq):
        new_base = (seq + 1) & _SEQ_MASK
        if self.syn_seen and self.base == new_base:
            # Retransmitted SYN with the same initial sequence number.
            return
        self.syn_seen = True
        if self.base != new_base:
            # A different ISN means a new connection incarnation; data queued
            # for the old one cannot belong to it.
            self.base = new_base
            self._segments = []
        self._in_gap = False

    def add_data(self, seq, data, packet_index, payload_offset):
        if not data or self.reset_seen or self.fin_seen:
            return
        if self.base is None:
            # Capture started mid-connection: anchor on the first segment.
            self.base = seq

        end_seq = (seq + len(data)) & _SEQ_MASK
        if end_seq == self.base or _seq_less(end_seq, self.base):
            # Fully behind the consumed point: pure retransmission.
            self._refresh_gap_state()
            return
        if _seq_less(seq, self.base):
            # The head of the segment was already consumed; trim it.
            cut = (self.base - seq) & _SEQ_MASK
            payload_offset += cut
            data = data[cut:]
            seq = self.base

        position = (seq - self.base) & _SEQ_MASK
        end_position = position + len(data)

        # Queued segments fully covered by the new one are superseded; only
        # the surviving segments may carve bytes out of the new contribution.
        survivors = [segment for segment in self._segments
                     if not (position <= segment[0] and
                             segment[1] <= end_position)]

        # The contribution of the new segment, as relative-position pieces:
        # [pos, end) with the matching slice of the payload.
        pieces = [(position, end_position, data, payload_offset)]
        for segment in survivors:
            covered_start, covered_end = segment[0], segment[1]
            remaining_pieces = []
            for piece_start, piece_end, piece_data, piece_offset in pieces:
                left_end = min(piece_end, covered_start)
                right_start = max(piece_start, covered_end)
                if piece_start < left_end:
                    remaining_pieces.append((
                        piece_start, left_end,
                        piece_data[:left_end - piece_start],
                        piece_offset))
                if right_start < piece_end:
                    remaining_pieces.append((
                        right_start, piece_end,
                        piece_data[right_start - piece_start:],
                        piece_offset + (right_start - piece_start)))
            pieces = remaining_pieces
            if not pieces:
                # Every byte is already queued: pure retransmission.
                self._refresh_gap_state()
                return

        kept = survivors
        for piece_start, piece_end, piece_data, piece_offset in pieces:
            kept.append([piece_start, piece_end, piece_data,
                         packet_index, piece_offset])
        kept.sort(key=lambda segment: segment[0])
        self._segments = kept
        self._refresh_gap_state()

    def drain(self):
        """Release contiguous chunks starting at the base sequence number."""
        chunks = []
        while self._segments and self._segments[0][0] == 0:
            segment = self._segments.pop(0)
            chunks.append((segment[3], segment[4], segment[2]))
            advanced = segment[1]
            self.base = (self.base + advanced) & _SEQ_MASK
            for remaining in self._segments:
                remaining[0] -= advanced
                remaining[1] -= advanced
        self._refresh_gap_state()
        return chunks

    def _refresh_gap_state(self):
        gap = bool(self._segments) and self._segments[0][0] > 0
        if gap and not self._in_gap:
            self.gap_events += 1
        self._in_gap = gap

    @property
    def gap(self):
        """True when queued data exists but the base sequence is missing."""
        return bool(self._segments) and self._segments[0][0] > 0


class TLSRecordInfo(object):
    """One TLS record and the original packets carrying its bytes."""

    __slots__ = ('index', 'direction', 'content_type', 'version', 'length',
                 'origins', 'packets')

    def __init__(self, index, direction, content_type, version, length,
                 origins):
        self.index = index
        self.direction = direction
        self.content_type = content_type
        self.version = version
        self.length = length
        self.origins = tuple(origins)
        self.packets = tuple(sorted(set(
            packet_index for packet_index, _, _ in origins)))

    @property
    def content_type_name(self):
        record_class = ssl.RECORD_TYPES.get(self.content_type)
        if record_class is None:
            return 'Unknown(%d)' % self.content_type
        return record_class.__name__


class HandshakeEntry(object):
    """One complete TLS handshake message with full byte provenance."""

    __slots__ = ('index', 'direction', 'type', 'name', 'length', 'raw',
                 'origins', 'records', 'packets', '_parsed')

    def __init__(self, index, direction, msg_type, raw, origins,
                 record_indices, record_info_by_index):
        self.index = index
        self.direction = direction
        self.type = msg_type
        self.name = ssl.HANDSHAKE_TYPES[msg_type][0]
        self.length = len(raw) - _TLS_HANDSHAKE_HEADER_LEN
        self.raw = raw
        self.origins = tuple(origins)
        self.records = tuple(record_info_by_index[idx]
                             for idx in record_indices)
        self.packets = tuple(sorted(set(
            packet_index for packet_index, _, _ in origins)))
        self._parsed = None

    @property
    def handshake(self):
        """The reassembled message parsed as :class:`ssl.TLSHandshake`.

        Returns ``None`` when the framed bytes do not parse as the declared
        handshake type; the entry itself is still archived with its raw
        bytes and provenance rather than being silently dropped.
        """
        if self._parsed is None:
            try:
                self._parsed = ssl.TLSHandshake(self.raw)
            except (dpkt.UnpackError, ssl.SSL3Exception, struct.error):
                self._parsed = False
        return self._parsed if self._parsed is not False else None

    @property
    def valid(self):
        """True when the reassembled bytes deep-parse as their handshake type."""
        return self.handshake is not None


class _HandshakeStream(object):
    """Parses handshake messages out of handshake-record payloads."""

    def __init__(self, direction):
        self.direction = direction
        self._buffer = _OriginBuffer()
        self.entries = []
        # Set once payloads can no longer be framed (e.g. encrypted records).
        self.stopped = False
        self.stop_reason = None
        self._record_info = {}

    def add_record_payload(self, record_info, chunks):
        if self.stopped:
            return
        for packet_index, offset, data in chunks:
            self._buffer.append(packet_index, offset, data,
                                record_info.index)
        self._record_info[record_info.index] = record_info
        self._parse_available()

    def _parse_available(self):
        while len(self._buffer) >= _TLS_HANDSHAKE_HEADER_LEN:
            header = self._buffer.peek_bytes(_TLS_HANDSHAKE_HEADER_LEN)
            msg_type = compat_ord(header[0])
            body_length = struct.unpack('!I', b'\x00' + header[1:4])[0]
            if msg_type not in ssl.HANDSHAKE_TYPES:
                # No legal handshake boundary can be found here; stop rather
                # than guess one from an apparently plausible length.
                self.stopped = True
                self.stop_reason = \
                    'unknown handshake type %d (encrypted or malformed data)' \
                    % msg_type
                return
            total_length = _TLS_HANDSHAKE_HEADER_LEN + body_length
            if len(self._buffer) < total_length:
                # The message continues in a later handshake record.
                return
            raw, spans, record_indices = self._buffer.consume(total_length)
            origins = tuple((packet_index, offset, len(span))
                            for packet_index, offset, span, _ in spans)
            self.entries.append(HandshakeEntry(
                len(self.entries) + 1, self.direction, msg_type, raw,
                origins, record_indices, self._record_info))


class _DirectionParser(object):
    """Record-layer and handshake-layer parser for one direction."""

    def __init__(self, direction):
        self.direction = direction
        self.reassembler = _TcpReassembler()
        self.records = []
        self.handshakes = _HandshakeStream(direction)
        self._record_buffer = _OriginBuffer()
        self._pending_length = None
        self.corrupt = False

    def handle_segment(self, packet_index, seq, payload, flags):
        reassembler = self.reassembler
        if flags & tcp_mod.TH_SYN:
            reassembler.handle_syn(seq)
            if payload:
                # The SYN occupies one sequence number itself, so any data
                # carried in the same segment starts one position later.
                seq = (seq + 1) & _SEQ_MASK
        if payload:
            reassembler.add_data(seq, payload, packet_index, 0)
        if flags & tcp_mod.TH_RST:
            reassembler.reset_seen = True
        if flags & tcp_mod.TH_FIN:
            reassembler.fin_seen = True

        for chunk_packet, chunk_offset, chunk_data in \
                reassembler.drain():
            self._record_buffer.append(chunk_packet, chunk_offset,
                                       chunk_data)
        self._parse_records()

    def _parse_records(self):
        while not self.corrupt:
            if self._pending_length is None:
                if len(self._record_buffer) < _TLS_RECORD_HEADER_LEN:
                    return
                header = self._record_buffer.peek_bytes(
                    _TLS_RECORD_HEADER_LEN)
                if header[1:3] not in ssl.SSL3_VERSION_BYTES:
                    # Refuse to frame on an implausible version byte; taking
                    # the following length field at face value would be a
                    # guess, not parsing.
                    self.corrupt = True
                    self.handshakes.stopped = True
                    self.handshakes.stop_reason = \
                        'bad TLS record version bytes %r' % header[1:3]
                    return
                content_type = compat_ord(header[0])
                version = struct.unpack('!H', header[1:3])[0]
                self._pending_type = content_type
                self._pending_version = version
                self._pending_length = struct.unpack('!H', header[3:5])[0]
                _, header_spans, _ = self._record_buffer.consume(
                    _TLS_RECORD_HEADER_LEN)
                self._header_origins = tuple(
                    (packet_index, offset, len(span))
                    for packet_index, offset, span, _ in header_spans)

            needed = self._pending_length
            if len(self._record_buffer) < needed:
                return

            payload, payload_spans, _ = self._record_buffer.consume(needed)
            payload_origins = tuple(
                (packet_index, offset, len(span))
                for packet_index, offset, span, _ in payload_spans)
            origins = self._header_origins + payload_origins
            record_info = TLSRecordInfo(
                len(self.records) + 1, self.direction, self._pending_type,
                self._pending_version, needed, origins)
            self.records.append(record_info)
            self._pending_length = None

            if record_info.content_type == _TLS_CONTENT_HANDSHAKE:
                payload_chunks = [
                    (packet_index, offset, span)
                    for packet_index, offset, span, _ in payload_spans
                ]
                # Stopping handshake parsing (e.g. encrypted records) does
                # not stop record-layer framing in this direction.
                self.handshakes.add_record_payload(record_info,
                                                   payload_chunks)


class ConnectionArchive(object):
    """All TLS records and handshake messages found on one connection."""

    def __init__(self, key, first_packet):
        self.key = key
        self.endpoints = key
        self.first_packet = first_packet
        self.last_packet = first_packet
        self.client = None
        self.server = None
        # direction key: ((src ip, src port), (dst ip, dst port))
        self.parsers = {}
        self.syn_seen = False
        self.packet_count = 0

    def direction_label(self, direction):
        if self.client is None:
            return '%s -> %s' % (
                _format_endpoint(direction[0]),
                _format_endpoint(direction[1]))
        if direction[0] == self.client:
            return 'client -> server'
        return 'server -> client'

    def entries(self, direction):
        return self.parsers[direction].handshakes.entries

    def all_entries(self):
        result = []
        for direction in sorted(self.parsers):
            result.extend(self.parsers[direction].handshakes.entries)
        return result


def _endpoint_key(src_ip, src_port, dst_ip, dst_port):
    return tuple(sorted(((src_ip, src_port), (dst_ip, dst_port))))


class _ConnectionTracker(object):
    """Holds per-connection state while a capture is walked."""

    def __init__(self):
        self.connections = {}

    def process(self, packet_index, src_ip, dst_ip, tcp_segment):
        src_endpoint = (src_ip, tcp_segment.sport)
        dst_endpoint = (dst_ip, tcp_segment.dport)
        key = _endpoint_key(src_ip, tcp_segment.sport, dst_ip,
                            tcp_segment.dport)
        direction = (src_endpoint, dst_endpoint)
        archive = self.connections.get(key)
        if archive is None:
            archive = ConnectionArchive(key, packet_index)
            self.connections[key] = archive
        archive.last_packet = packet_index
        archive.packet_count += 1

        parser = archive.parsers.get(direction)
        if parser is None:
            parser = _DirectionParser(direction)
            archive.parsers[direction] = parser

        flags = tcp_segment.flags
        if flags & tcp_mod.TH_SYN and not (flags & tcp_mod.TH_ACK):
            # A pure SYN identifies the TLS client endpoint.
            archive.client = src_endpoint
            archive.server = dst_endpoint
            archive.syn_seen = True

        payload = bytes(tcp_segment.data)
        parser.handle_segment(packet_index, tcp_segment.seq, payload, flags)


def _extract_tcp(frame):
    """Return ``(src_ip, dst_ip, TCP)`` from a link-layer frame, or ``None``."""
    network = frame.data
    if isinstance(network, ip.IP):
        if network.offset != 0 or network.mf:
            # IP fragments do not expose a parsable TCP segment; ignore them
            # rather than inventing payload.
            return None
        src_ip, dst_ip = network.src, network.dst
    elif isinstance(network, ip6.IP6):
        src_ip, dst_ip = network.src, network.dst
    else:
        return None
    transport = network.data
    if not isinstance(transport, tcp_mod.TCP):
        return None
    return src_ip, dst_ip, transport


class _RawFrame(object):
    """Adapter so _extract_tcp handles raw-IP captures like link frames."""

    def __init__(self, network):
        self.data = network


def _parse_frame(linktype, frame_bytes):
    if linktype == pcap.DLT_EN10MB:
        frame = ethernet.Ethernet(frame_bytes)
    elif linktype == pcap.DLT_RAW:
        network = ip.IP(frame_bytes) \
            if compat_ord(frame_bytes[0]) >> 4 == 4 else ip6.IP6(frame_bytes)
        return _extract_tcp(_RawFrame(network))
    elif linktype == pcap.DLT_LOOP:
        frame = loopback.Loopback(frame_bytes)
    elif linktype == pcap.DLT_LINUX_SLL:
        frame = sll.SLL(frame_bytes)
    elif linktype == pcap.DLT_LINUX_SLL2:
        frame = sll2.SLL2(frame_bytes)
    elif linktype == pcap.DLT_IPV4:
        return _extract_tcp(_RawFrame(ip.IP(frame_bytes)))
    elif linktype == pcap.DLT_IPV6:
        return _extract_tcp(_RawFrame(ip6.IP6(frame_bytes)))
    else:
        raise TLSArchiveError('unsupported pcap linktype %d' % linktype)
    return _extract_tcp(frame)


def _infer_clients(connections):
    """For captures joined mid-stream, derive client/server from handshakes."""
    for archive in connections:
        if archive.client is not None:
            continue
        for direction in sorted(archive.parsers):
            for entry in archive.parsers[direction].handshakes.entries:
                if entry.type == 1:  # ClientHello
                    archive.client = direction[0]
                    archive.server = direction[1]
                    break
            if archive.client is not None:
                break


def archive_packets(packets, linktype):
    """Archive TLS handshakes from an iterable of raw link-layer packets.

    Args:
        packets: iterable of ``(timestamp, frame_bytes)`` tuples as produced
            by :class:`dpkt.pcap.Reader` and :class:`dpkt.pcapng.Reader`.
        linktype: capture link type, e.g. ``dpkt.pcap.DLT_EN10MB``.

    Returns:
        list of :class:`ConnectionArchive`, ordered by first appearance.
    """
    tracker = _ConnectionTracker()
    for packet_index, (_timestamp, frame_bytes) in enumerate(packets,
                                                              start=1):
        try:
            extracted = _parse_frame(linktype, frame_bytes)
        except (dpkt.UnpackError, struct.error, IndexError, ValueError):
            # Malformed or truncated frame: skip it rather than aborting the
            # whole archival run.
            continue
        if extracted is None:
            continue
        src_ip, dst_ip, tcp_segment = extracted
        tracker.process(packet_index, src_ip, dst_ip, tcp_segment)

    connections = list(tracker.connections.values())
    _infer_clients(connections)
    return connections


def archive_pcap_file(path):
    """Archive TLS handshakes from a pcap or pcapng file path or file object."""
    if hasattr(path, 'read'):
        return _archive_pcap_stream(path)
    with open(path, 'rb') as fileobj:
        return _archive_pcap_stream(fileobj)


def _archive_pcap_stream(fileobj):
    magic = fileobj.read(4)
    fileobj.seek(0)
    if magic == _PCAPNG_MAGIC:
        reader = pcapng.Reader(fileobj)
    else:
        reader = pcap.Reader(fileobj)
    return archive_packets(iter(reader), reader.datalink())


def _format_endpoint(endpoint):
    host, port = endpoint
    return '%s:%d' % (inet_to_str(host), port)


def format_archive(connections):
    """Render connection archives as a human-readable report string."""
    lines = []
    for connection_number, archive in enumerate(connections, start=1):
        endpoints = archive.endpoints
        lines.append('Connection #%d  %s <-> %s  (packets %d..%d)' % (
            connection_number,
            _format_endpoint(endpoints[0]),
            _format_endpoint(endpoints[1]),
            archive.first_packet, archive.last_packet))
        for direction in sorted(archive.parsers):
            parser = archive.parsers[direction]
            label = archive.direction_label(direction)
            stream = parser.handshakes
            notes = []
            if parser.reassembler.gap_events:
                notes.append('%d tcp gap event(s)'
                             % parser.reassembler.gap_events)
            if parser.reassembler.gap:
                notes.append('unrecovered gap')
            if stream.stopped:
                notes.append(stream.stop_reason or 'handshake parsing stopped')
            suffix = ('; ' + ', '.join(notes)) if notes else ''
            lines.append('  [%s] tls records: %d, handshake messages: %d%s'
                         % (label, len(parser.records),
                            len(stream.entries), suffix))
            for record_info in parser.records:
                packets = ', '.join(str(number)
                                    for number in record_info.packets)
                lines.append('    record #%d %s len=%d packets=[%s]' % (
                    record_info.index, record_info.content_type_name,
                    record_info.length, packets))
            for entry in stream.entries:
                packets = ', '.join(str(number) for number in entry.packets)
                record_ids = ', '.join(str(record.index)
                                       for record in entry.records)
                lines.append(
                    '    handshake #%d %s (type=%d, len=%d) '
                    'records=[%s] packets=[%s]' % (
                        entry.index, entry.name, entry.type, entry.length,
                        record_ids, packets))
        lines.append('')
    return '\n'.join(lines)
