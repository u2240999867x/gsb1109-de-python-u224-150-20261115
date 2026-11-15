# -*- coding: utf-8 -*-
"""Tests for TLS handshake archival (dpkt/tls_archive.py).

The captures are synthesized from TLS records/TCP segments so every
framing edge case can be exercised deterministically:

* a TLS record spread over several TCP segments / packets,
* several handshake messages inside one record,
* a handshake message spread over several records (header and body),
* sequence gaps that must never be bridged by plausible-looking bytes,
* retransmissions that must not create a second handshake entry,
* independent numbering of the two directions.
"""
from __future__ import absolute_import

import io
import socket
import struct

import pytest

import dpkt
from dpkt import tcp as tcp_mod
from dpkt import tls_archive

CLIENT_IP = socket.inet_aton('10.0.0.1')
SERVER_IP = socket.inet_aton('10.0.0.2')
CLIENT_PORT = 11111
SERVER_PORT = 443
CLIENT_DIRECTION = ((CLIENT_IP, CLIENT_PORT), (SERVER_IP, SERVER_PORT))
SERVER_DIRECTION = ((SERVER_IP, SERVER_PORT), (CLIENT_IP, CLIENT_PORT))

ACK_PUSH = tcp_mod.TH_ACK | tcp_mod.TH_PUSH


def handshake_bytes(msg_type, body):
    """Build a TLS handshake message: type (1) + length (3) + body."""
    return struct.pack('!B', msg_type) + struct.pack('!I', len(body))[1:] + body


def record_bytes(content_type, payload, version=0x0303):
    """Build a TLS record: type (1) + version (2) + length (2) + payload."""
    return struct.pack('!BHH', content_type, version, len(payload)) + payload


def handshake_record(body_messages, version=0x0303):
    return record_bytes(22, b''.join(body_messages), version)


class CaptureBuilder(object):
    """Builds an Ethernet/IP/TCP capture while tracking TCP sequence numbers."""

    def __init__(self):
        self.packets = []
        self.payloads = {}
        self._seq = {
            CLIENT_DIRECTION: 1000,
            SERVER_DIRECTION: 5000,
        }

    def _frame(self, direction, seq, flags, payload):
        (src_ip, src_port), (dst_ip, dst_port) = direction
        tcp_segment = dpkt.tcp.TCP(
            sport=src_port, dport=dst_port, seq=seq, flags=flags,
            data=payload)
        ip_packet = dpkt.ip.IP(
            src=src_ip, dst=dst_ip, p=dpkt.ip.IP_PROTO_TCP,
            data=tcp_segment)
        frame = dpkt.ethernet.Ethernet(
            type=dpkt.ethernet.ETH_TYPE_IP, data=ip_packet)
        packet_index = len(self.packets) + 1
        self.packets.append((float(packet_index), bytes(frame)))
        if payload:
            self.payloads[packet_index] = payload
        return packet_index

    def syn(self, direction=CLIENT_DIRECTION):
        seq = self._seq[direction]
        index = self._frame(direction, seq, tcp_mod.TH_SYN, b'')
        self._seq[direction] = (seq + 1) & 0xffffffff
        return index

    def send(self, direction, payload, seq=None, flags=ACK_PUSH):
        """Send one segment; advances the direction sequence number."""
        if seq is None:
            seq = self._seq.setdefault(direction, 2000)
        index = self._frame(direction, seq, flags, payload)
        if seq == self._seq[direction]:
            self._seq[direction] = (seq + len(payload)) & 0xffffffff
        return index

    def send_parts(self, direction, data, sizes, flags=ACK_PUSH):
        """Split ``data`` into several in-order TCP segments."""
        indices = []
        cursor = 0
        seq = self._seq[direction]
        for size in sizes:
            part = data[cursor:cursor + size]
            indices.append(self.send(direction, part, seq=seq, flags=flags))
            seq = (seq + len(part)) & 0xffffffff
            cursor += size
        self._seq[direction] = seq
        return indices

    def archive(self):
        connections = tls_archive.archive_packets(
            self.packets, dpkt.pcap.DLT_EN10MB)
        assert len(connections) == 1
        return connections[0]


def rebuild_from_origins(builder, origins):
    return b''.join(builder.payloads[p][offset:offset + length]
                    for p, offset, length in origins)


class TestTcpReassembler(object):
    def setup_method(self):
        self.reassembler = tls_archive._TcpReassembler()
        self.reassembler.handle_syn(99)

    def drain_all(self):
        return [(pkt, data) for pkt, _offset, data in
                self.reassembler.drain()]

    def test_in_order_segments_drain_immediately(self):
        self.reassembler.add_data(100, b'aaaa', 1, 0)
        self.reassembler.add_data(104, b'bbbb', 2, 0)
        assert self.drain_all() == [(1, b'aaaa'), (2, b'bbbb')]
        assert self.reassembler.gap is False

    def test_gap_holds_later_segment_until_filled(self):
        self.reassembler.add_data(100, b'aaaa', 1, 0)
        self.reassembler.add_data(110, b'cccc', 3, 0)
        assert self.drain_all() == [(1, b'aaaa')]
        assert self.reassembler.gap is True
        assert self.reassembler.gap_events == 1
        # Filling the missing range releases both segments, in order.
        self.reassembler.add_data(104, b'bbbbbb', 4, 0)
        assert self.drain_all() == [(4, b'bbbbbb'), (3, b'cccc')]
        assert self.reassembler.gap is False

    def test_retransmission_after_consumption_is_dropped(self):
        self.reassembler.add_data(100, b'aaaa', 1, 0)
        self.drain_all()
        self.reassembler.add_data(100, b'aaaa', 2, 0)
        assert self.reassembler.drain() == []

    def test_overlapping_retransmission_only_keeps_new_bytes(self):
        self.reassembler.add_data(100, b'aaaa', 1, 0)
        self.drain_all()
        # 4 retransmitted bytes + 4 new bytes.
        self.reassembler.add_data(100, b'aaaabbbb', 2, 0)
        pkt, offset, data = self.reassembler.drain()[0]
        assert (pkt, offset, data) == (2, 4, b'bbbb')

    def test_fully_covered_segment_is_dropped(self):
        # Arrives first with a gap, then a wider in-order segment covers it.
        self.reassembler.add_data(110, b'cc', 3, 0)
        self.reassembler.add_data(100, b'aaaabbbbbbcc', 4, 0)
        assert self.reassembler.drain() == [(4, 0, b'aaaabbbbbbcc')]


class TestRecordFraming(object):
    def test_record_spans_multiple_packets(self):
        builder = CaptureBuilder()
        builder.syn()
        hello = handshake_bytes(1, b'\x03\x03' + b'C' * 20)
        record = handshake_record([hello])
        indices = builder.send_parts(CLIENT_DIRECTION, record, [7, 13, 100])

        archive = builder.archive()
        parser = archive.parsers[CLIENT_DIRECTION]
        assert len(parser.records) == 1
        record_info = parser.records[0]
        assert record_info.length == len(hello)
        assert record_info.packets == tuple(indices)

        entries = parser.handshakes.entries
        assert len(entries) == 1
        entry = entries[0]
        assert entry.name == 'ClientHello'
        assert entry.packets == tuple(indices)
        assert entry.raw == hello
        assert rebuild_from_origins(builder, entry.origins) == hello

    def test_several_records_in_one_segment(self):
        builder = CaptureBuilder()
        builder.syn()
        first = handshake_record([handshake_bytes(14, b'')])
        second = handshake_record([handshake_bytes(20, b'F' * 12)])
        builder.send(CLIENT_DIRECTION, first + second)

        parser = builder.archive().parsers[CLIENT_DIRECTION]
        assert [record.index for record in parser.records] == [1, 2]
        assert [entry.index for entry in parser.handshakes.entries] == [1, 2]
        assert [entry.type for entry in parser.handshakes.entries] == [14, 20]
        # Both records live in the single packet.
        assert parser.records[0].packets == (2,)
        assert parser.records[1].packets == (2,)

    def test_record_header_itself_split_across_packets(self):
        builder = CaptureBuilder()
        builder.syn()
        hello = handshake_bytes(1, b'H' * 10)
        record = handshake_record([hello])
        builder.send_parts(CLIENT_DIRECTION, record, [3, 2, 40])

        entries = builder.archive().parsers[CLIENT_DIRECTION].handshakes.entries
        assert len(entries) == 1
        assert entries[0].raw == hello

    def test_bad_version_bytes_is_not_framed(self):
        builder = CaptureBuilder()
        builder.syn()
        # Type 22, but bogus version 0x0400.
        builder.send(CLIENT_DIRECTION, b'\x16\x04\x00\x00\x05abcde')
        parser = builder.archive().parsers[CLIENT_DIRECTION]
        assert parser.corrupt is True
        assert parser.records == []

    def test_plausible_length_but_illegal_body_is_flagged_not_faked(self):
        builder = CaptureBuilder()
        builder.syn()
        # A ClientHello whose declared length looks perfectly plausible but
        # whose 12-byte body cannot hold the 34-byte hello header, followed
        # by a legal message in the same record.
        bogus = handshake_bytes(1, b'X' * 12)
        legal = handshake_bytes(14, b'')
        builder.send(CLIENT_DIRECTION,
                     handshake_record([bogus, legal]))

        entries = builder.archive().parsers[CLIENT_DIRECTION] \
            .handshakes.entries
        # Both messages are framed and archived; the bogus one is never
        # presented as a successfully parsed ClientHello.
        assert len(entries) == 2
        assert entries[0].type == 1
        assert entries[0].valid is False
        assert entries[0].handshake is None
        assert entries[1].valid is True
        assert entries[1].name == 'ServerHelloDone'


class TestHandshakeFraming(object):
    def test_multiple_handshakes_in_one_record(self):
        builder = CaptureBuilder()
        builder.syn()
        done = handshake_bytes(14, b'')
        finished = handshake_bytes(20, b'F' * 8)
        builder.send(CLIENT_DIRECTION, handshake_record([done, finished]))

        entries = builder.archive().parsers[CLIENT_DIRECTION].handshakes.entries
        assert [entry.index for entry in entries] == [1, 2]
        assert entries[0].name == 'ServerHelloDone'
        assert entries[1].name == 'Finished'
        assert entries[0].records == (entries[1].records[0],)
        assert entries[0].length == 0
        assert entries[1].length == 8

    def test_handshake_spans_two_records_with_body_split(self):
        builder = CaptureBuilder()
        builder.syn()
        message = handshake_bytes(11, b'Z' * 30)
        first_record = handshake_record([message[:12]])
        second_record = handshake_record([message[12:]])
        builder.send(CLIENT_DIRECTION, first_record)
        builder.send(CLIENT_DIRECTION, second_record)

        parser = builder.archive().parsers[CLIENT_DIRECTION]
        entries = parser.handshakes.entries
        assert len(entries) == 1
        entry = entries[0]
        assert entry.raw == message
        assert [record.index for record in entry.records] == [1, 2]
        assert entry.packets == (2, 3)

    def test_handshake_header_split_across_records(self):
        builder = CaptureBuilder()
        builder.syn()
        message = handshake_bytes(2, b'S' * 16)
        # Split inside the 4-byte handshake header.
        builder.send(CLIENT_DIRECTION, handshake_record([message[:2]]))
        builder.send(CLIENT_DIRECTION, handshake_record([message[2:]]))

        entries = builder.archive().parsers[CLIENT_DIRECTION].handshakes.entries
        assert len(entries) == 1
        assert entries[0].raw == message
        assert [record.index for record in entries[0].records] == [1, 2]

    def test_second_handshake_spans_record_boundary(self):
        builder = CaptureBuilder()
        builder.syn()
        first_message = handshake_bytes(14, b'')
        second_message = handshake_bytes(20, b'Q' * 20)
        combined = first_message + second_message
        builder.send(CLIENT_DIRECTION, handshake_record([combined[:7]]))
        builder.send(CLIENT_DIRECTION, handshake_record([combined[7:]]))

        entries = builder.archive().parsers[CLIENT_DIRECTION].handshakes.entries
        assert len(entries) == 2
        assert [record.index for record in entries[1].records] == [1, 2]
        assert entries[1].raw == second_message


class TestGapsAndRetransmission(object):
    def test_plausible_bytes_after_gap_are_not_consumed(self):
        builder = CaptureBuilder()
        builder.syn()
        complete = handshake_bytes(1, b'A' * 10)
        builder.send(CLIENT_DIRECTION, handshake_record([complete]))

        # A second record declaring a 30-byte payload, of which only the
        # header and the first 10 payload bytes are present.
        second_header = b'\x16\x03\x03' + struct.pack('!H', 30)
        builder.send(CLIENT_DIRECTION, second_header + b'1234567890')
        gap_start = builder._seq[CLIENT_DIRECTION]
        # Perfectly valid-looking record+handshake bytes behind the gap.
        plausible = handshake_record([handshake_bytes(14, b'')])
        builder.send(CLIENT_DIRECTION, plausible,
                     seq=(gap_start + 10) & 0xffffffff)

        archive = builder.archive()
        parser = archive.parsers[CLIENT_DIRECTION]
        reassembler = parser.reassembler
        assert reassembler.gap is True
        # The second record never finalizes and no second handshake appears.
        assert len(parser.records) == 1
        assert len(parser.handshakes.entries) == 1
        assert parser.handshakes.entries[0].raw == complete

    def test_filling_gap_completes_handshake_with_full_provenance(self):
        builder = CaptureBuilder()
        builder.syn()
        message = handshake_bytes(14, b'Z' * 26)  # 30 bytes total
        assert len(message) == 30
        record = handshake_record([message])

        head_index = builder.send(CLIENT_DIRECTION, record[:5] + message[:10])
        gap_fill_seq = builder._seq[CLIENT_DIRECTION]
        tail_index = builder.send(
            CLIENT_DIRECTION, message[20:],
            seq=(gap_fill_seq + 10) & 0xffffffff)
        # Still blocked before the missing middle arrives.
        blocked = builder.archive().parsers[CLIENT_DIRECTION]
        assert blocked.reassembler.gap is True
        assert len(blocked.handshakes.entries) == 0

        fill_index = builder.send(CLIENT_DIRECTION, message[10:20],
                                  seq=gap_fill_seq)
        parser = builder.archive().parsers[CLIENT_DIRECTION]
        entries = parser.handshakes.entries
        assert len(entries) == 1
        entry = entries[0]
        assert entry.raw == message
        assert entry.packets == tuple(sorted((head_index, fill_index,
                                              tail_index)))
        assert rebuild_from_origins(builder, entry.origins) == message
        assert parser.records[0].packets == entry.packets

    def test_retransmission_does_not_create_second_handshake(self):
        builder = CaptureBuilder()
        builder.syn()
        record = handshake_record([handshake_bytes(1, b'A' * 8)])
        first_index = builder.send(CLIENT_DIRECTION, record)
        # Identical segment, identical sequence number.
        builder.send(CLIENT_DIRECTION, record,
                     seq=builder._seq[CLIENT_DIRECTION] - len(record))

        parser = builder.archive().parsers[CLIENT_DIRECTION]
        assert len(parser.records) == 1
        entries = parser.handshakes.entries
        assert len(entries) == 1
        assert entries[0].packets == (first_index,)

    def test_retransmission_spanning_records_adds_nothing(self):
        builder = CaptureBuilder()
        builder.syn()
        message = handshake_bytes(11, b'Z' * 30)
        first_record = handshake_record([message[:12]])
        second_record = handshake_record([message[12:]])
        first_index = builder.send(CLIENT_DIRECTION, first_record)
        second_index = builder.send(CLIENT_DIRECTION, second_record)
        # Retransmit both record segments at their original sequence numbers.
        builder.send(CLIENT_DIRECTION, first_record, seq=1001)
        builder.send(CLIENT_DIRECTION, second_record,
                     seq=(1001 + len(first_record)) & 0xffffffff)

        parser = builder.archive().parsers[CLIENT_DIRECTION]
        assert len(parser.records) == 2
        entries = parser.handshakes.entries
        assert len(entries) == 1
        assert entries[0].raw == message
        assert entries[0].packets == (first_index, second_index)

    def test_data_after_fin_is_ignored(self):
        builder = CaptureBuilder()
        builder.syn()
        record = handshake_record([handshake_bytes(14, b'')])
        first_index = builder.send(CLIENT_DIRECTION, record)
        seq = builder._seq[CLIENT_DIRECTION]
        builder.send(CLIENT_DIRECTION, b'', seq=seq,
                     flags=tcp_mod.TH_ACK | tcp_mod.TH_FIN)
        # Segment after the FIN must be discarded.
        builder.send(CLIENT_DIRECTION, handshake_record(
            [handshake_bytes(20, b'X' * 4)]), seq=seq)

        parser = builder.archive().parsers[CLIENT_DIRECTION]
        assert parser.reassembler.fin_seen is True
        assert [entry.packets for entry in
                parser.handshakes.entries] == [(first_index,)]

    def test_retransmitted_syn_keeps_queued_data(self):
        reassembler = tls_archive._TcpReassembler()
        reassembler.handle_syn(99)
        reassembler.add_data(100, b'abc', 1, 0)
        # Retransmitted SYN with the same ISN must not drop the data.
        reassembler.handle_syn(99)
        assert [data for _, _, data in reassembler.drain()] == [b'abc']

    def test_malformed_frame_does_not_abort_archive(self):
        builder = CaptureBuilder()
        builder.syn()
        builder.send(CLIENT_DIRECTION,
                     handshake_record([handshake_bytes(14, b'')]))
        # A truncated, unparseable frame in the middle of the capture.
        builder.packets.append((99.0, b'\x45\x00\x00\x14\x00'))
        connections = tls_archive.archive_packets(
            builder.packets, dpkt.pcap.DLT_EN10MB)
        assert len(connections[0].all_entries()) == 1


class TestDirectionsAndConnections(object):
    def _two_direction_capture(self):
        builder = CaptureBuilder()
        builder.syn()
        client_hello = handshake_bytes(1, b'C' * 12)
        builder.send(CLIENT_DIRECTION, handshake_record([client_hello]))
        server_hello = handshake_bytes(2, b'S' * 12)
        server_done = handshake_bytes(14, b'')
        builder.send(SERVER_DIRECTION,
                     handshake_record([server_hello, server_done]))
        return builder

    def test_directions_are_numbered_independently(self):
        archive = self._two_direction_capture().archive()
        assert archive.client == CLIENT_DIRECTION[0]
        assert archive.server == SERVER_DIRECTION[0]
        assert archive.direction_label(CLIENT_DIRECTION) == 'client -> server'
        assert archive.direction_label(SERVER_DIRECTION) == 'server -> client'

        client_parser = archive.parsers[CLIENT_DIRECTION]
        server_parser = archive.parsers[SERVER_DIRECTION]
        assert [entry.index for entry in
                client_parser.handshakes.entries] == [1]
        assert [entry.index for entry in
                server_parser.handshakes.entries] == [1, 2]
        assert client_parser.records[0].index == 1
        assert server_parser.records[0].index == 1

    def test_separate_connections_are_separated(self):
        builder = self._two_direction_capture()
        other_port = 5555
        other_direction = ((CLIENT_IP, other_port), (SERVER_IP, SERVER_PORT))
        builder.send(other_direction,
                     handshake_record([handshake_bytes(1, b'N' * 4)]))
        connections = tls_archive.archive_packets(
            builder.packets, dpkt.pcap.DLT_EN10MB)
        assert len(connections) == 2
        assert [len(connection.all_entries()) for connection in
                connections] == [3, 1]

    def test_client_inferred_from_client_hello_without_syn(self):
        builder = CaptureBuilder()
        builder.send(CLIENT_DIRECTION,
                     handshake_record([handshake_bytes(1, b'C' * 4)]))
        archive = builder.archive()
        assert archive.client == CLIENT_DIRECTION[0]


class TestPcapIntegration(object):
    def test_pcap_roundtrip(self):
        builder = CaptureBuilder()
        builder.syn()
        message = handshake_bytes(1, b'A' * 16)
        builder.send_parts(CLIENT_DIRECTION,
                           handshake_record([message]), [5, 9, 30])

        buffer = io.BytesIO()
        writer = dpkt.pcap.Writer(buffer, linktype=dpkt.pcap.DLT_EN10MB)
        for timestamp, frame in builder.packets:
            writer.writepkt_time(frame, timestamp)
        buffer.seek(0)

        reader = dpkt.pcap.Reader(buffer)
        connections = tls_archive.archive_packets(
            iter(reader), reader.datalink())
        entries = connections[0].parsers[CLIENT_DIRECTION].handshakes.entries
        assert len(entries) == 1
        assert entries[0].raw == message

    def test_pcapng_roundtrip(self):
        builder = CaptureBuilder()
        builder.syn()
        builder.send(CLIENT_DIRECTION,
                     handshake_record([handshake_bytes(1, b'A' * 6)]))

        buffer = io.BytesIO()
        writer = dpkt.pcapng.Writer(buffer,
                                    linktype=dpkt.pcap.DLT_EN10MB)
        for timestamp, frame in builder.packets:
            writer.writepkt_time(frame, timestamp)
        buffer.seek(0)

        connections = tls_archive.archive_pcap_file(buffer)
        assert len(connections[0].all_entries()) == 1

    def test_archive_pcap_file(self, tmpdir):
        builder = CaptureBuilder()
        builder.syn()
        builder.send(CLIENT_DIRECTION,
                     handshake_record([handshake_bytes(14, b'')]))
        path = str(tmpdir.join('handshakes.pcap'))
        with open(path, 'wb') as fileobj:
            writer = dpkt.pcap.Writer(fileobj,
                                      linktype=dpkt.pcap.DLT_EN10MB)
            for timestamp, frame in builder.packets:
                writer.writepkt_time(frame, timestamp)

        connections = tls_archive.archive_pcap_file(path)
        assert len(connections) == 1
        assert len(connections[0].all_entries()) == 1

    def test_ipv6_capture(self):
        message = handshake_bytes(1, b'6' * 6)
        record = handshake_record([message])
        tcp_segment = dpkt.tcp.TCP(
            sport=CLIENT_PORT, dport=SERVER_PORT, seq=100, flags=ACK_PUSH,
            data=record)
        ip_packet = dpkt.ip6.IP6(
            src=b'\x20\x01' + b'\x00' * 13 + b'\x01',
            dst=b'\x20\x01' + b'\x00' * 13 + b'\x02',
            nxt=dpkt.ip.IP_PROTO_TCP, data=tcp_segment)
        frame = dpkt.ethernet.Ethernet(
            type=dpkt.ethernet.ETH_TYPE_IP6, data=ip_packet)
        connections = tls_archive.archive_packets(
            [(1.0, bytes(frame))], dpkt.pcap.DLT_EN10MB)
        assert len(connections[0].all_entries()) == 1

    def test_unsupported_linktype_raises(self):
        with pytest.raises(tls_archive.TLSArchiveError):
            tls_archive.archive_packets(
                [(1.0, b'\x00' * 20)], dpkt.pcap.DLT_FDDI)

    def test_non_handshake_records_are_framed_but_not_handshakes(self):
        builder = CaptureBuilder()
        builder.syn()
        change_cipher_spec = record_bytes(20, b'\x01')
        alert = record_bytes(21, b'\x02\x28')
        hello = handshake_record([handshake_bytes(1, b'A' * 4)])
        builder.send(CLIENT_DIRECTION,
                     change_cipher_spec + alert + hello)

        parser = builder.archive().parsers[CLIENT_DIRECTION]
        assert [record.content_type for record in parser.records] == [20, 21, 22]
        assert parser.records[0].content_type_name == 'TLSChangeCipherSpec'
        assert parser.records[1].content_type_name == 'TLSAlert'
        assert len(parser.handshakes.entries) == 1

    def test_handshake_entry_parses_reassembled_message(self):
        builder = CaptureBuilder()
        builder.syn()
        # Minimal legal ClientHello body: version, random, empty session id,
        # one cipher suite, one compression method.
        client_hello_body = (b'\x03\x03' + b'R' * 32 + b'\x00' +
                             b'\x00\x02\x00\x2f' + b'\x01\x00')
        message = handshake_bytes(1, client_hello_body)
        builder.send_parts(CLIENT_DIRECTION,
                           handshake_record([message]), [6, 44])
        entry = builder.archive().parsers[CLIENT_DIRECTION] \
            .handshakes.entries[0]
        parsed = entry.handshake
        assert isinstance(parsed, dpkt.ssl.TLSHandshake)
        assert isinstance(parsed.data, dpkt.ssl.TLSClientHello)

    def test_format_archive_is_readable(self):
        builder = CaptureBuilder()
        builder.syn()
        builder.send(CLIENT_DIRECTION,
                     handshake_record([handshake_bytes(1, b'A' * 4)]))
        report = tls_archive.format_archive([builder.archive()])
        assert 'Connection #1' in report
        assert 'ClientHello' in report
        assert 'client -> server' in report
