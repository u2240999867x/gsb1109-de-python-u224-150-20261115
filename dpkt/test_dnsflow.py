# -*- coding: utf-8 -*-
"""Tests for DNS request/response correlation."""
from __future__ import print_function
from __future__ import absolute_import

import io
import json
import struct

import dpkt
from dpkt.compat import BytesIO
from . import dnsflow
from .dns import DNS, DNS_A, DNS_AAAA, DNS_IN

CLIENT = b'\xc0\xa8\x01\x01'
SERVER = b'\xc0\xa8\x01\x02'
CLIENT_PORT = 40000
SERVER_PORT = 53


def _question(name, qtype=DNS_A, qclass=DNS_IN):
    return DNS.Q(name=name, type=qtype, cls=qclass)


def _answer(name, rdata=b'\x7f\x00\x00\x01', qtype=DNS_A):
    return DNS.RR(name=name, type=qtype, cls=DNS_IN, ttl=60, rdata=rdata)


def _dns(msg_id, questions, answers=(), op=0x0100, rcode=0, tc=False):
    if tc:
        op |= dpkt.dns.DNS_TC
    if answers:
        op |= dpkt.dns.DNS_QR
    return bytes(DNS(id=msg_id, op=op, qd=list(questions), an=list(answers),
                     rcode=rcode))


def _udp_packet(payload, sport, dport, src=CLIENT, dst=SERVER):
    """Build a raw Ethernet/IPv4/UDP frame without recomputing checksums."""
    udp = struct.pack('>HHHH', sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack('>BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 0, 0, 64,
                     dpkt.ip.IP_PROTO_UDP, 0, src, dst) + udp
    eth = b'\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x08\x00'
    return eth + ip


def _tcp_packet(payload, seq, sport, dport, src=CLIENT, dst=SERVER,
                flags=dpkt.tcp.TH_ACK | dpkt.tcp.TH_PUSH):
    tcp = struct.pack('>HHIIBBHHH', sport, dport, seq, 0, 5 << 4, flags,
                      65535, 0, 0) + payload
    ip = struct.pack('>BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 0, 0, 64,
                     dpkt.ip.IP_PROTO_TCP, 0, src, dst) + tcp
    eth = b'\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x08\x00'
    return eth + ip


def _table(*packets):
    table = dnsflow.DNSFlowTable()
    for index, packet in enumerate(packets):
        table.add_packet(index * 0.01, packet)
    table.finish()
    return table


def _query(name, msg_id=0x1234, qtype=DNS_A, **kwargs):
    return _dns(msg_id, [_question(name, qtype)], **kwargs)


def _response(name, msg_id=0x1234, answers=None, qtype=DNS_A, **kwargs):
    if answers is None:
        answers = [_answer(name, qtype=qtype)]
    return _dns(msg_id, [_question(name, qtype)],
                answers=answers, op=dpkt.dns.DNS_QR, **kwargs)


def test_simple_match():
    packet = _udp_packet(_query('example.test'), CLIENT_PORT, SERVER_PORT)
    reply = _udp_packet(_response('example.test'), SERVER_PORT, CLIENT_PORT,
                        src=SERVER, dst=CLIENT)
    table = _table(packet, reply)

    assert len(table.requests) == 1
    request = table.requests[0]
    assert request.status == dnsflow.MATCHED
    assert request.response.index == 1
    assert table.responses[0].status == dnsflow.MATCHED


def test_retransmission_is_folded_into_one_request():
    query = _query('retry.test')
    reply = _response('retry.test')
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    assert len(table.requests) == 1
    request = table.requests[0]
    assert request.status == dnsflow.MATCHED
    assert [t.kind for t in request.transmissions] == [
        dnsflow.ORIGINAL, dnsflow.RETRANSMISSION]
    assert request.transmissions[0].segment.index == 0
    assert request.transmissions[1].segment.index == 1


def test_reused_id_with_different_question_is_a_new_query():
    first = _query('first.test', msg_id=0x2222)
    second = _query('second.test', msg_id=0x2222)
    first_reply = _response('second.test', msg_id=0x2222)

    table = _table(_udp_packet(first, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(second, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(first_reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    assert len(table.requests) == 2
    assert table.requests[0].classification == dnsflow.NEW_QUERY
    assert table.requests[0].status == dnsflow.UNANSWERED
    assert table.requests[1].classification == dnsflow.REUSED_ID
    assert table.requests[1].reused_id is True
    assert table.requests[1].previous_request == 0
    assert table.requests[1].status == dnsflow.MATCHED
    assert table.requests[1].response.index == 2
    assert table.table()[1]['classification'] == dnsflow.REUSED_ID


def test_same_id_and_question_from_two_ports_needs_evidence():
    query = _query('ambiguous.test', msg_id=0x3333)
    reply = _response('ambiguous.test', msg_id=0x3333)

    table = _table(_udp_packet(query, 41000, SERVER_PORT),
                   _udp_packet(query, 42000, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, 43000,
                               src=SERVER, dst=CLIENT))

    assert len(table.requests) == 2
    assert all(r.status == dnsflow.CANDIDATE for r in table.requests)
    assert all(r.response is None for r in table.requests)
    assert all(r.candidates for r in table.requests)


def test_response_without_question_kept_as_candidate():
    query = _query('noquestion.test', msg_id=0x4444)
    header = struct.pack('>HHHHHH', 0x4444, 0x8180, 0, 1, 0, 0)
    reply = _udp_packet(header, SERVER_PORT, CLIENT_PORT,
                        src=SERVER, dst=CLIENT)

    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT), reply)

    request = table.requests[0]
    assert request.status == dnsflow.CANDIDATE
    assert request.response is None
    assert request.candidates[0].index == 1


def test_duplicate_response_is_not_counted_twice():
    query = _query('dup.test', msg_id=0x5555)
    reply = _response('dup.test', msg_id=0x5555)
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    assert len(table.requests) == 1
    assert table.requests[0].status == dnsflow.MATCHED
    assert len(table.requests[0].duplicates) == 1
    assert table.responses[1].status == dnsflow.DUPLICATE


def test_two_sequential_queries_get_their_own_responses():
    query = _query('again.test', msg_id=0x6767)
    reply = _response('again.test', msg_id=0x6767)
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT),
                   _udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    assert len(table.requests) == 2
    assert [r.status for r in table.requests] == [
        dnsflow.MATCHED, dnsflow.MATCHED]
    assert table.requests[0].response.index == 1
    assert table.requests[1].response.index == 3
    assert table.requests[0].duplicates == []


def test_one_response_cannot_count_for_two_requests():
    query = _query('shared.test', msg_id=0x6666)
    reply = _response('shared.test', msg_id=0x6666)

    table = _table(_udp_packet(query, 41000, SERVER_PORT),
                   _udp_packet(query, 42000, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, 41000,
                               src=SERVER, dst=CLIENT))

    assert len(table.requests) == 2
    assert sum(r.status == dnsflow.MATCHED for r in table.requests) == 1
    assert table.requests[0].status == dnsflow.MATCHED
    assert table.requests[1].status == dnsflow.UNANSWERED


def test_truncated_udp_response_then_tcp_fallback():
    query = _query('fallback.test', msg_id=0x7777)
    truncated = _response('fallback.test', msg_id=0x7777, answers=[],
                          tc=True)
    full = _response('fallback.test', msg_id=0x7777)

    messages = (
        _udp_packet(query, CLIENT_PORT, SERVER_PORT),
        _udp_packet(truncated, SERVER_PORT, CLIENT_PORT,
                    src=SERVER, dst=CLIENT),
        _tcp_packet(struct.pack('>H', len(query)) + query, 1000,
                    CLIENT_PORT + 1, SERVER_PORT),
        _tcp_packet(struct.pack('>H', len(full)) + full, 1000 + 2 + len(query),
                    SERVER_PORT, CLIENT_PORT + 1, src=SERVER, dst=CLIENT),
    )
    table = _table(*messages)

    assert len(table.requests) == 1
    request = table.requests[0]
    assert request.status == dnsflow.MATCHED
    assert request.fallback is True
    assert [t.kind for t in request.transmissions] == [
        dnsflow.ORIGINAL, dnsflow.TCP_FALLBACK]
    assert request.response.index == 3
    assert [r.status for r in request.truncated_responses] == [
        dnsflow.TRUNCATED]


def test_tcp_fallback_may_change_transaction_id():
    query = _query('renumbered.test', msg_id=0x7788)
    truncated = _response('renumbered.test', msg_id=0x7788, answers=[],
                          tc=True)
    retry = _query('renumbered.test', msg_id=0x9999)
    full = _response('renumbered.test', msg_id=0x9999)

    table = _table(
        _udp_packet(query, CLIENT_PORT, SERVER_PORT),
        _udp_packet(truncated, SERVER_PORT, CLIENT_PORT,
                    src=SERVER, dst=CLIENT),
        _tcp_packet(struct.pack('>H', len(retry)) + retry, 1,
                    CLIENT_PORT + 1, SERVER_PORT),
        _tcp_packet(struct.pack('>H', len(full)) + full, 1,
                    SERVER_PORT, CLIENT_PORT + 1, src=SERVER, dst=CLIENT))

    assert len(table.requests) == 1
    request = table.requests[0]
    assert request.status == dnsflow.MATCHED
    assert request.fallback is True
    assert request.id == 0x7788
    assert request.response.id == 0x9999


def test_response_seen_before_request_is_recovered():
    query = _query('outoforder.test', msg_id=0xabcd)
    reply = _response('outoforder.test', msg_id=0xabcd)

    table = _table(_udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT),
                   _udp_packet(query, CLIENT_PORT, SERVER_PORT))

    assert table.requests[0].status == dnsflow.MATCHED
    assert table.responses[0].status == dnsflow.MATCHED


def test_plain_tcp_query_is_not_a_fallback():
    query = _query('tcp.test', msg_id=0x8888)
    reply = _response('tcp.test', msg_id=0x8888)
    table = _table(
        _tcp_packet(struct.pack('>H', len(query)) + query, 1,
                    CLIENT_PORT, SERVER_PORT),
        _tcp_packet(struct.pack('>H', len(reply)) + reply, 1,
                    SERVER_PORT, CLIENT_PORT, src=SERVER, dst=CLIENT))

    assert len(table.requests) == 1
    assert table.requests[0].status == dnsflow.MATCHED
    assert table.requests[0].fallback is False


def test_tcp_message_split_across_segments():
    query = _query('split.test', msg_id=0x9999)
    reply = _response('split.test', msg_id=0x9999)
    framed_query = struct.pack('>H', len(query)) + query
    framed_reply = struct.pack('>H', len(reply)) + reply

    table = _table(
        _tcp_packet(framed_query[:10], 1, CLIENT_PORT, SERVER_PORT),
        _tcp_packet(framed_query[10:], 11, CLIENT_PORT, SERVER_PORT),
        _tcp_packet(framed_reply[:5], 1, SERVER_PORT, CLIENT_PORT,
                    src=SERVER, dst=CLIENT),
        _tcp_packet(framed_reply[5:], 6, SERVER_PORT, CLIENT_PORT,
                    src=SERVER, dst=CLIENT))

    assert len(table.requests) == 1
    assert table.requests[0].status == dnsflow.MATCHED


def test_same_name_different_type_is_a_different_query():
    query_a = _query('typed.test', msg_id=0xaaaa, qtype=DNS_A)
    query_aaaa = _query('typed.test', msg_id=0xaaaa, qtype=DNS_AAAA)
    reply_aaaa = _response('typed.test', msg_id=0xaaaa, qtype=DNS_AAAA,
                           answers=[_answer('typed.test', b'\x20' * 16,
                                            qtype=DNS_AAAA)])

    table = _table(_udp_packet(query_a, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(query_aaaa, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply_aaaa, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    assert len(table.requests) == 2
    assert table.requests[0].status == dnsflow.UNANSWERED
    assert table.requests[1].status == dnsflow.MATCHED
    assert table.requests[1].questions[0][1] == DNS_AAAA


def test_case_insensitive_question_match():
    query = _query('MiXeD.test')
    reply = _response('mixed.TEST')
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    assert table.requests[0].status == dnsflow.MATCHED


def test_answer_for_a_different_question_is_only_a_candidate():
    query = _query('asked.test', msg_id=0xbbbb)
    reply = _response('other.test', msg_id=0xbbbb)
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    request = table.requests[0]
    assert request.status == dnsflow.CANDIDATE
    assert request.response is None
    assert request.candidates[0].index == 1
    assert 'question differs' in request.candidates[0].reason


def test_orphan_response_without_request():
    reply = _response('orphan.test', msg_id=0xcccc)
    table = _table(_udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    assert table.requests == []
    assert len(table.responses) == 1
    assert table.responses[0].status == dnsflow.ORPHAN


def test_table_rows_and_json_shape():
    query = _query('row.test', msg_id=0xdddd)
    reply = _response('row.test', msg_id=0xdddd)
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    rows = table.table()
    assert rows == [{
        'index': 0,
        'timestamp': 0.0,
        'id': 0xdddd,
        'client': '192.168.1.1',
        'server': '192.168.1.2',
        'client_port': CLIENT_PORT,
        'server_port': SERVER_PORT,
        'transport': 'udp',
        'name': 'row.test',
        'questions': [{'name': 'row.test', 'type': DNS_A, 'class': DNS_IN}],
        'kind': dnsflow.ORIGINAL,
        'classification': dnsflow.NEW_QUERY,
        'reused_id': False,
        'previous_request': None,
        'status': dnsflow.MATCHED,
        'fallback': False,
        'response_index': 1,
        'response_rcode': 0,
        'response_partial': False,
        'candidates': [],
        'duplicates': [],
        'truncated': [],
        'transmissions': [
            {'index': 0, 'transport': 'udp', 'kind': dnsflow.ORIGINAL},
        ],
    }]

    messages = table.message_table()
    assert [m['classification'] for m in messages] == [
        dnsflow.ORIGINAL, dnsflow.MATCHED]


def test_message_table_marks_truncated_and_retransmitted():
    query = _query('messages.test', msg_id=0xeeee)
    truncated = _response('messages.test', msg_id=0xeeee, answers=[], tc=True)
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(truncated, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    messages = table.message_table()
    assert messages[0]['classification'] == dnsflow.ORIGINAL
    assert messages[1]['classification'] == dnsflow.RETRANSMISSION
    assert messages[2]['classification'] == dnsflow.TRUNCATED


def test_snaplen_truncated_answer_is_kept_as_partial():
    query = _query('cut.test', msg_id=0xffff)
    reply = _response('cut.test', msg_id=0xffff)
    # Drop the last two bytes, leaving a truncated resource record.
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply[:-2], SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    request = table.requests[0]
    # The question is intact, so the response can be paired; the truncated
    # answer body is still reported for the caller's judgement.
    assert request.status == dnsflow.MATCHED
    assert request.response.partial is True
    assert table.table()[0]['response_partial'] is True


def test_from_capture_reads_pcap_file():
    packets = [
        (0.0, _udp_packet(_query('pcap.test'), CLIENT_PORT, SERVER_PORT)),
        (0.1, _udp_packet(_response('pcap.test'), SERVER_PORT, CLIENT_PORT,
                          src=SERVER, dst=CLIENT)),
    ]
    fobj = BytesIO()
    writer = dpkt.pcap.Writer(fobj, linktype=dpkt.pcap.DLT_EN10MB)
    writer.writepkts(packets)
    fobj.seek(0)

    table = dnsflow.DNSFlowTable.from_capture(fobj)
    assert table.packet_count == 2
    assert table.requests[0].status == dnsflow.MATCHED


def test_print_table_and_messages(capsys):
    query = _query('print.test', msg_id=0x1234)
    reply = _response('print.test', msg_id=0x1234)
    table = _table(_udp_packet(query, CLIENT_PORT, SERVER_PORT),
                   _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                               src=SERVER, dst=CLIENT))

    dnsflow.print_table(table)
    out = capsys.readouterr().out
    assert 'print.test' in out
    assert 'matched' in out

    dnsflow.print_messages(table)
    out = capsys.readouterr().out
    assert 'query' in out
    assert 'response' in out


def test_main_json(tmp_path):
    query = _query('cli.test', msg_id=0x1234)
    reply = _response('cli.test', msg_id=0x1234)
    capture = tmp_path / 'capture.pcap'
    with open(str(capture), 'wb') as fobj:
        writer = dpkt.pcap.Writer(fobj)
        writer.writepkts([
            (0.0, _udp_packet(query, CLIENT_PORT, SERVER_PORT)),
            (0.1, _udp_packet(reply, SERVER_PORT, CLIENT_PORT,
                              src=SERVER, dst=CLIENT)),
        ])

    output = io.StringIO()
    import sys
    old = sys.stdout
    sys.stdout = output
    try:
        assert dnsflow.main([str(capture), '--json']) == 0
        rows = json.loads(output.getvalue())
    finally:
        sys.stdout = old
    assert rows[0]['status'] == dnsflow.MATCHED
