# -*- coding: utf-8 -*-
"""Tests for TCP session generation tracking."""
from __future__ import absolute_import

import os

import pytest

from dpkt import session
from dpkt import tcp

SYN = tcp.TH_SYN
ACK = tcp.TH_ACK
FIN = tcp.TH_FIN
RST = tcp.TH_RST
PSH = tcp.TH_PUSH

CLIENT = ('10.1.1.1', 40000)
SERVER = ('10.1.1.2', 80)
KEY = ('10.1.1.1', 40000, '10.1.1.2', 80)


def pkt(src, dst, seq, ack, flags, payload_len=0):
    return (src[0], src[1], dst[0], dst[1], seq, ack, flags, payload_len)


def handshake(client=CLIENT, server=SERVER, client_isn=100, server_isn=900, ts=0.0):
    return [
        (ts, pkt(client, server, client_isn, 0, SYN)),
        (ts + 0.01, pkt(server, client, server_isn, client_isn + 1, SYN | ACK)),
        (ts + 0.02, pkt(client, server, client_isn + 1, server_isn + 1, ACK)),
    ]


def close(client=CLIENT, server=SERVER, client_seq=101, server_seq=901, ts=1.0):
    return [
        (ts, pkt(client, server, client_seq, server_seq, FIN | ACK)),
        (ts + 0.01, pkt(server, client, server_seq, client_seq + 1, FIN | ACK)),
    ]


def table(packets, timeout=60.0):
    result = session.SessionTable(timeout=timeout)
    for ts, packet in packets:
        result.add_packet(packet, ts)
    return result


def test_canonical_key_is_bidirectional():
    assert session.SessionKey(*KEY) == session.SessionKey(
        SERVER[0], SERVER[1], CLIENT[0], CLIENT[1])
    assert session.canonical_key(*KEY) == session.canonical_key(
        SERVER[0], SERVER[1], CLIENT[0], CLIENT[1])
    assert session.canonical_key(CLIENT, SERVER) == KEY
    assert session.SessionKey(*KEY).matches(KEY)
    assert session.SessionKey(*KEY).matches(
        (SERVER[0], SERVER[1], CLIENT[0], CLIENT[1]))


def test_single_handshake_is_one_generation_with_known_boundaries():
    packets = handshake() + [(0.1, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 5))]
    packets += close(client_seq=106)
    sessions = table(packets)
    assert len(sessions) == 1
    generation = sessions[0]
    assert generation.generation == 1
    assert generation.start_reason == session.START_SYN
    assert generation.start_known is True
    assert generation.end_reason == session.END_FIN
    assert generation.end_known is True
    assert generation.state == session.STATE_CLOSED
    assert generation.client == CLIENT
    assert generation.server == SERVER
    assert generation.client_isn == 100
    assert generation.server_isn == 900


def test_four_tuple_reuse_creates_traceable_generations():
    packets = handshake()
    packets += close()
    packets += handshake(client_isn=5000, server_isn=8000, ts=2.0)
    sessions = table(packets)
    assert len(sessions) == 2
    first, second = sessions
    assert first.generation == 1
    assert second.generation == 2
    assert first.key == second.key == session.SessionKey(*KEY)
    assert first.end_reason == session.END_SUPERSEDED or first.end_reason == session.END_FIN
    assert second.start_reason == session.START_SYN
    assert second.start_known is True
    assert len(sessions.get_sessions(KEY)) == 2
    assert sessions.get(KEY) is second


def test_retransmitted_syn_and_data_do_not_create_new_generation():
    packets = handshake()
    packets += [
        (0.03, pkt(CLIENT, SERVER, 100, 0, SYN)),
        (0.04, pkt(SERVER, CLIENT, 900, 101, SYN | ACK)),
        (0.1, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10)),
        (0.2, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10)),
    ]
    sessions = table(packets)
    assert len(sessions) == 1
    assert sessions[0].retransmission_count == 3
    assert sessions[0].packet_count == 7


def test_timeout_only_proposes_candidate_and_is_rejected_by_liveness():
    packets = handshake()
    packets += [
        (1.0, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10)),
        (100.0, pkt(CLIENT, SERVER, 111, 901, PSH | ACK, 10)),
        (101.0, pkt(SERVER, CLIENT, 901, 121, ACK)),
    ]
    sessions = table(packets, timeout=5.0)
    assert len(sessions) == 1
    candidates = sessions.candidates
    assert len(candidates) == 1
    assert candidates[0].candidate is True
    assert candidates[0].status == session.CANDIDATE_REJECTED
    assert candidates[0].resolution == 'liveness_sequence_continuity'
    assert sessions[0].packet_count == 6
    assert sessions[0].superseded is False


def test_timeout_with_new_syn_keeps_candidate_separate_from_confirmed_split():
    packets = handshake()
    packets += [(1.0, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10))]
    packets += handshake(client_isn=5000, server_isn=8000, ts=100.0)
    sessions = table(packets, timeout=5.0)
    assert len(sessions) == 2
    assert [s.start_reason for s in sessions.sessions] == [session.START_SYN, session.START_SYN]
    assert len(sessions.candidates) == 1
    assert sessions.candidates[0].status == session.CANDIDATE_SUPERSEDED
    assert sessions.boundaries()[-1].generation == 2


def test_midstream_capture_has_unknown_start_and_stays_one_generation():
    packets = [
        (0.0, pkt(CLIENT, SERVER, 1000, 2000, PSH | ACK, 10)),
        (0.1, pkt(SERVER, CLIENT, 2000, 1010, ACK)),
        (0.2, pkt(CLIENT, SERVER, 1010, 2000, PSH | ACK, 10)),
    ]
    sessions = table(packets, timeout=0.01)
    assert len(sessions) == 1
    generation = sessions[0]
    assert generation.start_reason == session.START_MIDSTREAM
    assert generation.start_known is False
    assert generation.start_unknown is True
    assert generation.state == session.STATE_ESTABLISHED
    # The tiny timeout cannot override sequence evidence of liveness.
    assert all(c.status == session.CANDIDATE_REJECTED for c in sessions.candidates)


def test_midstream_then_handshake_marks_new_generation_unknown_only_for_old():
    packets = [
        (0.0, pkt(CLIENT, SERVER, 1000, 2000, PSH | ACK, 10)),
        (0.1, pkt(SERVER, CLIENT, 2000, 1010, ACK)),
    ] + handshake(client_isn=5000, server_isn=8000, ts=2.0)
    sessions = table(packets)
    assert len(sessions) == 2
    assert sessions[0].start_known is False
    assert sessions[1].start_known is True
    assert sessions[0].end_reason == session.END_SUPERSEDED
    assert sessions[1].start_reason == session.START_SYN


def test_capture_starting_with_syn_ack_has_unknown_client_start():
    packets = [
        (0.0, pkt(SERVER, CLIENT, 900, 101, SYN | ACK)),
        (0.1, pkt(CLIENT, SERVER, 101, 901, ACK)),
        (0.2, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10)),
    ]
    sessions = table(packets)
    assert len(sessions) == 1
    assert sessions[0].start_reason == session.START_SYN_ACK
    assert sessions[0].start_known is False
    assert sessions[0].saw_syn is False
    assert sessions[0].server_isn == 900
    assert sessions[0].client_isn == 100
    assert sessions[0].state == session.STATE_ESTABLISHED


def test_simultaneous_open_is_one_connection():
    packets = [
        (0.0, pkt(CLIENT, SERVER, 100, 0, SYN)),
        (0.01, pkt(SERVER, CLIENT, 900, 0, SYN)),
        (0.02, pkt(CLIENT, SERVER, 101, 901, SYN | ACK)),
        (0.03, pkt(SERVER, CLIENT, 901, 101, SYN | ACK)),
        (0.04, pkt(CLIENT, SERVER, 101, 901, ACK)),
    ]
    sessions = table(packets)
    assert len(sessions) == 1
    generation = sessions[0]
    assert generation.simultaneous_open is True
    assert generation.state == session.STATE_ESTABLISHED
    assert generation.start_reason == session.START_SYN


def test_rst_creates_known_end_and_reuse_without_syn_has_unknown_start():
    packets = handshake()
    packets += [(1.0, pkt(CLIENT, SERVER, 101, 901, RST))]
    assert table(packets)[0].end_reason == session.END_RST

    packets = handshake()
    packets += [(1.0, pkt(CLIENT, SERVER, 101, 901, RST))]
    packets += [(2.0, pkt(CLIENT, SERVER, 7000, 9000, PSH | ACK, 10))]
    sessions = table(packets)
    assert len(sessions) == 2
    assert sessions[1].start_reason == session.START_AFTER_RESET
    assert sessions[1].start_known is False


def test_sequence_jump_without_closure_is_recorded_not_split():
    packets = handshake()
    packets += [
        (0.1, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10)),
        (0.2, pkt(CLIENT, SERVER, 500000, 901, PSH | ACK, 10)),
    ]
    sessions = table(packets, timeout=1.0)
    assert len(sessions) == 1
    assert sessions[0].anomalies
    assert sessions[0].anomalies[-1]['reason'] in ('sequence_jump', 'ack_sequence_jump')


def test_finalize_marks_unclosed_session_without_inventing_closure():
    sessions = table(handshake())
    sessions.finalize(10.0)
    generation = sessions[0]
    assert generation.end_reason == session.END_CAPTURE
    assert generation.end_known is False
    assert generation.end_time == 10.0


def test_boundary_records_are_serializable_and_traceable():
    sessions = table(handshake() + close())
    boundaries = sessions.boundaries()
    assert [b.kind for b in boundaries] == ['start', 'end']
    data = sessions.to_dict()
    assert data['summary']['sessions'] == 1
    assert data['sessions'][0]['start_known'] is True
    assert data['sessions'][0]['end_known'] is True
    assert boundaries[0].evidence['reason'] == session.START_SYN


def test_dpkt_packet_objects_are_decoded():
    def frame(src, sport, dst, dport, seq, ack, flags):
        t = tcp.TCP(sport=sport, dport=dport, seq=seq, ack=ack, flags=flags)
        i = __import__('dpkt').ip.IP(src=src, dst=dst, p=6, data=t,
                                    len=20 + len(bytes(t)))
        i.len = 20 + len(bytes(t))
        return i

    sessions = session.SessionTable(timeout=60)
    sessions.add_packet(frame(b'\x0a\x01\x01\x01', 40000, b'\x0a\x01\x01\x02', 80, 100, 0, SYN), 0.0)
    sessions.add_packet(frame(b'\x0a\x01\x01\x02', 80, b'\x0a\x01\x01\x01', 40000, 900, 101, SYN | ACK), 0.1)
    sessions.add_packet(frame(b'\x0a\x01\x01\x01', 40000, b'\x0a\x01\x01\x02', 80, 101, 901, ACK), 0.2)
    assert len(sessions) == 1
    assert sessions[0].client == CLIENT
    assert sessions[0].server == SERVER


def test_read_sessions_from_pcap():
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'examples', 'data', 'http.pcap')
    sessions = session.read_sessions(path, timeout=300)
    assert len(sessions) >= 1
    for generation in sessions:
        assert generation.key == session.canonical_key(*generation.key)


def _ethernet_frame(src, sport, dst, dport, seq, ack, flags, payload=b''):
    import dpkt
    t = tcp.TCP(sport=sport, dport=dport, seq=seq, ack=ack, flags=flags, data=payload)
    t.off = 5
    i = dpkt.ip.IP(src=ip_to_bytes(src), dst=ip_to_bytes(dst), p=6, data=t)
    i.len = 20 + len(bytes(t))
    i.v_hl = 0x45
    e = dpkt.ethernet.Ethernet(src=b'\x00\x01\x02\x03\x04\x05',
                               dst=b'\x06\x07\x08\x09\x0a\x0b',
                               type=dpkt.ethernet.ETH_TYPE_IP, data=i)
    return bytes(e)


def ip_to_bytes(address):
    import socket
    return socket.inet_aton(address)


def test_pcap_roundtrip_preserves_generation_boundaries(tmp_path):
    import dpkt

    frames = [
        (0.0, _ethernet_frame(CLIENT[0], CLIENT[1], SERVER[0], SERVER[1], 100, 0, SYN)),
        (0.1, _ethernet_frame(SERVER[0], SERVER[1], CLIENT[0], CLIENT[1], 900, 101, SYN | ACK)),
        (0.2, _ethernet_frame(CLIENT[0], CLIENT[1], SERVER[0], SERVER[1], 101, 901, ACK)),
        (0.3, _ethernet_frame(CLIENT[0], CLIENT[1], SERVER[0], SERVER[1], 101, 901, PSH | ACK, b'hello')),
        (1.0, _ethernet_frame(CLIENT[0], CLIENT[1], SERVER[0], SERVER[1], 106, 901, FIN | ACK)),
        (1.1, _ethernet_frame(SERVER[0], SERVER[1], CLIENT[0], CLIENT[1], 901, 107, FIN | ACK)),
        (2.0, _ethernet_frame(CLIENT[0], CLIENT[1], SERVER[0], SERVER[1], 7000, 0, SYN)),
        (2.1, _ethernet_frame(SERVER[0], SERVER[1], CLIENT[0], CLIENT[1], 9000, 7001, SYN | ACK)),
        (2.2, _ethernet_frame(CLIENT[0], CLIENT[1], SERVER[0], SERVER[1], 7001, 9001, ACK)),
    ]
    path = tmp_path / 'reuse.pcap'
    with open(str(path), 'wb') as f:
        writer = dpkt.pcap.Writer(f)
        for timestamp, frame in frames:
            writer.writepkt(frame, timestamp)
    sessions = session.read_sessions(str(path), timeout=300)
    assert len(sessions) == 2
    assert [s.generation for s in sessions] == [1, 2]
    assert sessions[0].end_reason == session.END_FIN
    assert sessions[1].start_reason == session.START_SYN
    assert sessions[1].packet_count == 3
    assert sessions[1].packets[0].payload_len == 0
    assert sessions[0].retransmission_count == 0


def test_symbol_is_exported_from_package():
    import dpkt
    assert dpkt.session.SessionTable is session.SessionTable


def test_store_packets_false_keeps_state_but_not_history():
    sessions = session.SessionTable(timeout=60, store_packets=False)
    for timestamp, packet in handshake() + close(client_seq=101):
        sessions.add_packet(packet, timestamp)
    assert len(sessions) == 1
    generation = sessions[0]
    assert generation.packet_count == 0
    assert generation.state == session.STATE_CLOSED
    assert generation.end_reason == session.END_FIN
    assert generation.start_reason == session.START_SYN


def test_sessions_for_unknown_key_and_generation_lookup():
    sessions = table(handshake() + close() + handshake(client_isn=5000, server_isn=8000, ts=2.0))
    assert sessions.get_sessions(('1.2.3.4', 1, '5.6.7.8', 2)) == []
    assert sessions.generation(KEY, 2).generation == 2
    with pytest.raises(KeyError):
        sessions.generation(KEY, 99)


def test_fin_and_final_ack_stay_in_closed_generation():
    packets = handshake()
    packets += close(client_seq=101)
    packets += [(1.1, pkt(CLIENT, SERVER, 102, 902, ACK))]
    sessions = table(packets)
    assert len(sessions) == 1
    assert sessions[0].packet_count == 6
    assert sessions[0].end_reason == session.END_FIN
    assert sessions[0].end_time == 1.01


def test_reuse_after_reset_without_syn_is_unknown_start():
    packets = handshake()
    packets += [(1.0, pkt(CLIENT, SERVER, 101, 901, RST))]
    packets += [(2.0, pkt(CLIENT, SERVER, 7000, 9000, PSH | ACK, 10))]
    sessions = table(packets)
    assert len(sessions) == 2
    assert sessions[1].start_reason == session.START_AFTER_RESET
    assert sessions[1].start_known is False
    assert sessions[0].end_reason == session.END_RST


def test_timeout_exactly_at_threshold_is_not_a_candidate():
    packets = handshake()
    packets += [
        (1.0, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10)),
        (6.0, pkt(CLIENT, SERVER, 111, 901, PSH | ACK, 10)),
    ]
    sessions = table(packets, timeout=5.0)
    assert len(sessions) == 1
    assert sessions.candidates == []


def test_overlapping_segment_retransmission_is_not_new_data():
    packets = handshake()
    packets += [
        (0.1, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 100)),
        (0.2, pkt(CLIENT, SERVER, 151, 901, PSH | ACK, 100)),
        (0.3, pkt(CLIENT, SERVER, 151, 901, PSH | ACK, 100)),
    ]
    sessions = table(packets)
    assert len(sessions) == 1
    assert sessions[0].retransmission_count == 1
    assert sessions[0].packet_count == 6


def test_timeout_candidate_packet_is_not_lost_and_is_auditable():
    packets = handshake()
    packets += [
        (1.0, pkt(CLIENT, SERVER, 101, 901, PSH | ACK, 10)),
        (100.0, pkt(CLIENT, SERVER, 500000, 901, PSH | ACK, 10)),
    ]
    sessions = table(packets, timeout=5.0)
    assert len(sessions) == 1
    assert len(sessions.candidates) == 1
    candidate = sessions.candidates[0]
    assert candidate.status == session.CANDIDATE_PENDING
    assert candidate.packet_count == 1
    assert candidate.gap == 99.0
    assert sessions[0].packet_count == 4
    assert sessions[0].state != session.STATE_SUPERSEDED
    sessions.finalize(101.0)
    assert candidate.status == session.CANDIDATE_UNRESOLVED


def test_same_isn_reuse_after_close_is_a_new_generation():
    packets = handshake()
    packets += close(client_seq=101)
    # A random ISN collision must not merge connections when a close was seen.
    packets += handshake(client_isn=100, server_isn=900, ts=2.0)
    sessions = table(packets)
    assert len(sessions) == 2
    assert sessions[0].end_reason == session.END_FIN
    assert sessions[1].start_reason == session.START_SYN
    assert sessions[1].start_known is True


def test_add_packet_accepts_packet_first_and_timestamp_first():
    packets = handshake()
    sessions = session.SessionTable()
    timestamp, packet = packets[0]
    sessions.add_packet(packet, timestamp)
    for timestamp, packet in packets[1:]:
        sessions.add_packet(timestamp, packet)
    assert len(sessions) == 1
    assert sessions[0].packet_count == 3


def test_sessionify_accepts_timestamp_packet_pairs():
    sessions = session.sessionize(handshake(), timeout=60)
    assert len(sessions) == 1
    assert sessions[0].start_reason == session.START_SYN
