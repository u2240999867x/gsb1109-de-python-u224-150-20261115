"""Correlate DNS queries with responses in an offline packet capture.

DNS clients reuse transaction IDs, and a query may be retransmitted unchanged
or repeated over TCP after a truncated UDP response. Matching on the
transaction ID alone is therefore not enough: a response is accepted only
when its question section also agrees with the request and the endpoints
identify a single outstanding request.

The module works on the wire representation of DNS messages (UDP payloads or
reassembled DNS-over-TCP byte streams), so it does not depend on how a capture
was decoded into higher layers.

Typical use::

    with open('capture.pcap', 'rb') as f:
        table = DNSFlowTable.from_capture(f)
    for row in table.table():
        print(row)

Or from the command line::

    python -m dpkt.dnsflow capture.pcap
"""
from __future__ import print_function
from __future__ import absolute_import

import argparse
import json
import struct
import sys

from . import dpkt
from . import dns
from . import ethernet
from . import ip
from . import ip6
from . import loopback
from . import pcap
from . import ppp
from . import pppoe
from . import sll
from . import sll2
from . import tcp
from . import udp

DNS_PORT = 53

# How an attempt relates to the logical query it belongs to
ORIGINAL = 'original'
RETRANSMISSION = 'retransmission'
TCP_FALLBACK = 'tcp_fallback'

# How a logical request relates to earlier traffic on the same endpoints
NEW_QUERY = 'new_query'
REUSED_ID = 'reused_id'

# Status of a logical request
MATCHED = 'matched'
CANDIDATE = 'candidate'
UNANSWERED = 'unanswered'

# Status of an individual response
TRUNCATED = 'truncated'
DUPLICATE = 'duplicate'
ORPHAN = 'orphan'


def question_key(question):
    """Return a comparable identity for a DNS question.

    Names are case-insensitive per RFC 1035. The type and class are part of
    the identity: the same transaction ID with a different name or type is a
    different query.
    """
    name = question.name.lower()
    if name.endswith('.'):
        name = name[:-1]
    return (name, question.type, question.cls)


def questions_key(questions):
    """Return the identity of a potentially multi-question DNS message."""
    return tuple(question_key(q) for q in questions)


def _addr(value):
    if not isinstance(value, bytes):
        return str(value)
    try:
        if len(value) == 4:
            return '.'.join(str(b) for b in bytearray(value))
        import socket
        return socket.inet_ntop(socket.AF_INET6, value)
    except (ValueError, OSError):
        return repr(value)


class _DNSHeader(object):
    """The fixed DNS header, available even when the rest is truncated."""

    __slots__ = ('id', 'op', 'qd', 'an', 'ns', 'ar', 'qr', 'tc', 'rcode')

    def __init__(self, buf):
        self.id, self.op, self.qd, self.an, self.ns, self.ar = struct.unpack(
            '>HHHHHH', buf[:12])
        self.qr = bool(self.op & dns.DNS_QR)
        self.tc = bool(self.op & dns.DNS_TC)
        self.rcode = self.op & 0xf


class DNSSegment(object):
    """One DNS message found in a capture.

    Attributes:
        ts: capture timestamp
        index: packet number in the capture (the first packet for TCP)
        src, dst: source and destination IP address bytes
        sport, dport: source and destination ports
        transport: ``'udp'`` or ``'tcp'``
        payload: raw DNS message bytes
        header: parsed fixed header, or ``None`` when fewer than 12 bytes
        msg: fully parsed :class:`dpkt.dns.DNS`, or ``None``
        questions: tuple of ``(name, type, class)`` identities
        questions_complete: whether all declared questions were decoded
        truncated: whether the DNS header has the TC bit set
        partial: whether the message could not be fully parsed
        error: exception raised while parsing the message, if any
    """

    __slots__ = ('ts', 'index', 'src', 'dst', 'sport', 'dport', 'transport',
                 'payload', 'header', 'msg', 'questions',
                 'questions_complete', 'truncated', 'partial', 'error')

    def __init__(self, ts, index, src, dst, sport, dport, transport, payload):
        self.ts = ts
        self.index = index
        self.src = src
        self.dst = dst
        self.sport = sport
        self.dport = dport
        self.transport = transport
        self.payload = payload
        self.header = None
        self.msg = None
        self.questions = ()
        self.questions_complete = False
        self.truncated = False
        self.partial = False
        self.error = None
        self._unpack()

    def _unpack(self):
        if len(self.payload) < 12:
            self.partial = True
            return
        try:
            self.header = _DNSHeader(self.payload)
        except struct.error:
            self.partial = True
            return
        self.truncated = self.header.tc
        self._unpack_questions()
        try:
            self.msg = dns.DNS(self.payload)
        except Exception as exc:  # NeedData/UnpackError or malformed RRs
            self.error = exc
            self.partial = True
            if self.header.qd == 0:
                # There was nothing left to validate; the header is usable.
                self.partial = False
                self.questions_complete = True
            return
        self.questions = questions_key(self.msg.qd)
        self.questions_complete = True
        self.partial = self._is_incomplete()

    def _is_incomplete(self):
        for name, count in (('an', self.header.an), ('ns', self.header.ns),
                            ('ar', self.header.ar)):
            records = getattr(self.msg, name)
            if len(records) != count:
                return True
            for rr in records:
                if len(rr.rdata) != rr.rlen:
                    return True
        return False

    def _unpack_questions(self):
        questions = []
        off = 12
        for _ in range(self.header.qd):
            try:
                name, off = dns.unpack_name(self.payload, off)
                qtype, qclass = struct.unpack('>HH', self.payload[off:off + 4])
            except (dpkt.NeedData, dpkt.UnpackError, struct.error):
                break
            off += 4
            questions.append(dns.DNS.Q(name=name, type=qtype, cls=qclass))
        self.questions = questions_key(questions)
        self.questions_complete = len(questions) == self.header.qd

    @property
    def id(self):
        return None if self.header is None else self.header.id

    @property
    def is_response(self):
        return self.header is not None and self.header.qr

    @property
    def is_query(self):
        return self.header is not None and not self.header.qr

    @property
    def rcode(self):
        return None if self.header is None else self.header.rcode

    @property
    def name(self):
        return self.questions[0][0] if self.questions else ''

    def __repr__(self):
        kind = 'response' if self.is_response else 'query'
        state = ' partial' if self.partial else ''
        return 'DNSSegment(index=%d, %s:%d -> %s:%d, id=%s, %s%s)' % (
            self.index, _addr(self.src), self.sport, _addr(self.dst),
            self.dport, self.id, kind, state)


class DNSTransmission(object):
    """One attempt to send a logical request.

    A retransmission keeps the same endpoint tuple and transaction ID. A TCP
    fallback normally uses a new client port, so it is represented as another
    transmission of the same logical request.
    """

    __slots__ = ('segment', 'key', 'kind')

    def __init__(self, segment, key, kind):
        self.segment = segment
        self.key = key
        self.kind = kind

    @property
    def index(self):
        return self.segment.index

    @property
    def transport(self):
        return self.segment.transport

    @property
    def ts(self):
        return self.segment.ts

    def __repr__(self):
        return 'DNSTransmission(index=%d, id=%s, %s)' % (
            self.index, self.segment.id, self.kind)


class DNSRequest(object):
    """A logical DNS query and all of its transmissions.

    Attributes:
        index: packet number of the first transmission
        ts: timestamp of the first transmission
        id: DNS transaction ID
        questions: tuple of ``(name, type, class)`` identities
        transmissions: list of :class:`DNSTransmission`
        response: the accepted :class:`DNSResponse`
        candidates: responses not accepted for lack of evidence
        duplicates: later copies of the accepted response
        truncated_responses: responses with TC set
        fallbacks: TCP transmissions made after a truncated response
    """

    __slots__ = ('index', 'ts', 'id', 'questions', 'transmissions',
                 'response', 'candidates', 'duplicates',
                 'truncated_responses', 'fallbacks', 'reused_id',
                 'previous_request')

    def __init__(self, transmission):
        segment = transmission.segment
        self.index = segment.index
        self.ts = segment.ts
        self.id = segment.id
        self.questions = segment.questions
        self.transmissions = [transmission]
        self.response = None
        self.candidates = []
        self.duplicates = []
        self.truncated_responses = []
        self.fallbacks = []
        self.reused_id = False
        self.previous_request = None

    @property
    def status(self):
        if self.response is not None:
            return MATCHED
        if self.candidates:
            return CANDIDATE
        return UNANSWERED

    @property
    def answered(self):
        return self.response is not None

    @property
    def first(self):
        return self.transmissions[0].segment

    @property
    def last_ts(self):
        return max(t.ts for t in self.transmissions)

    @property
    def transport(self):
        return self.first.transport

    @property
    def name(self):
        return self.questions[0][0] if self.questions else ''

    @property
    def client(self):
        first = self.first
        return first.src if _is_forward(first) else first.dst

    @property
    def server(self):
        first = self.first
        return first.dst if _is_forward(first) else first.src

    @property
    def client_port(self):
        first = self.first
        return first.sport if _is_forward(first) else first.dport

    @property
    def server_port(self):
        first = self.first
        return first.dport if _is_forward(first) else first.sport

    @property
    def fallback(self):
        return bool(self.fallbacks)

    @property
    def classification(self):
        """Return ``new_query``, ``reused_id``, ``retransmission``, or ``tcp_fallback``.

        ``reused_id`` takes precedence because it is the case that makes
        transaction-ID-only matching fail.
        """
        if self.reused_id:
            return REUSED_ID
        if self.fallback:
            return TCP_FALLBACK
        if any(t.kind == RETRANSMISSION for t in self.transmissions):
            return RETRANSMISSION
        return NEW_QUERY

    @property
    def partial(self):
        return any(t.segment.partial for t in self.transmissions)

    def add_transmission(self, transmission):
        self.transmissions.append(transmission)
        if transmission.kind == TCP_FALLBACK:
            self.fallbacks.append(transmission)

    def __repr__(self):
        return 'DNSRequest(index=%d, %s:%d -> %s:%d, id=%s, name=%r, %s)' % (
            self.index, _addr(self.client), self.client_port,
            _addr(self.server), self.server_port, self.id, self.name,
            self.status)


class DNSResponse(object):
    """A DNS response and the request it was considered for.

    Once accepted, a response belongs to exactly one request. The same wire
    response is never counted as a success for two requests.

    Attributes:
        segment: the wire message
        status: ``matched``, ``truncated``, ``candidate``, or ``duplicate``
        request: request the response was assigned or offered to
        reason: explanation for a candidate, truncated, or duplicate response
    """

    __slots__ = ('segment', 'status', 'request', 'reason')

    def __init__(self, segment, status=CANDIDATE, request=None, reason=''):
        self.segment = segment
        self.status = status
        self.request = request
        self.reason = reason

    @property
    def index(self):
        return self.segment.index

    @property
    def id(self):
        return self.segment.id

    @property
    def questions(self):
        return self.segment.questions

    @property
    def rcode(self):
        return self.segment.rcode

    @property
    def truncated(self):
        return self.segment.truncated

    @property
    def partial(self):
        return self.segment.partial

    def __repr__(self):
        return 'DNSResponse(index=%d, id=%s, %s%s)' % (
            self.index, self.id, self.status,
            (', ' + self.reason) if self.reason else '')


class DNSFlowTable(object):
    """Build a DNS request/response table from an offline capture.

    Feed raw link-layer packets with :meth:`add_packet`, or pass a pcap/pcapng
    file object or reader to :meth:`from_capture`.

    Args:
        timeout: optional seconds after which an unanswered request is no
            longer eligible for a normal match. Expired responses remain
            available as candidates. ``None`` keeps all requests eligible.
        data_link_type: optional pcap/pcapng ``linktype``. When reading a
            :class:`dpkt.pcap.Reader` or :class:`dpkt.pcapng.Reader` it is
            discovered automatically.
    """

    def __init__(self, timeout=None, data_link_type=None):
        if timeout is not None and timeout < 0:
            raise ValueError('timeout must be non-negative')
        self.timeout = timeout
        self.data_link_type = data_link_type
        self.requests = []
        self.responses = []
        self.segments = []
        self.malformed = []
        self.packet_count = 0
        self._by_key = {}
        self._tcp_streams = {}

    @classmethod
    def from_capture(cls, capture, **kwargs):
        """Read a pcap/pcapng file object or reader and return a table."""
        if hasattr(capture, 'read'):
            capture = pcap.UniversalReader(capture)
        datalink = getattr(capture, 'datalink', None)
        if datalink is not None:
            try:
                kwargs.setdefault('data_link_type', datalink())
            except (OSError, ValueError):
                pass
        table = cls(**kwargs)
        packets = iter(capture)
        while True:
            try:
                ts, buf = next(packets)
            except StopIteration:
                break
            except (dpkt.NeedData, struct.error, ValueError):
                # A capture may end in the middle of its last record.
                break
            table.add_packet(ts, buf)
        table.finish()
        return table

    def add_packet(self, ts, buf):
        """Add one captured packet.

        Args:
            ts: capture timestamp in seconds
            buf: raw link-layer frame

        Returns:
            list of :class:`DNSSegment` decoded from the frame
        """
        index = self.packet_count
        self.packet_count += 1

        segments = []
        self._walk_frame(bytes(buf), segments, ts, index)
        for segment in segments:
            self._add_segment(segment)
        return segments

    def finish(self):
        """Flush DNS-over-TCP reassembly state at the end of a capture."""
        for flow in list(self._tcp_streams):
            self._drain_stream(flow, final=True)

    # -- message handling -------------------------------------------------

    def _add_segment(self, segment):
        self.segments.append(segment)
        if segment.header is None:
            self.malformed.append(segment)
            return
        if segment.is_response:
            self._add_response(segment)
        elif segment.is_query:
            self._add_request(segment)

    def _add_request(self, segment):
        transmission = DNSTransmission(segment, _request_key(segment), ORIGINAL)

        prior = self._find_same_query(segment)
        if prior is not None:
            if prior.response is not None:
                # The previous query was answered; an exact repeat is a new
                # query, not a retransmission.
                prior = None
            elif self._expired(prior, segment.ts):
                prior = None
            elif any(t.transport == segment.transport and t.key == transmission.key
                     for t in prior.transmissions):
                transmission.kind = RETRANSMISSION
            elif segment.transport == 'tcp' and prior.truncated_responses:
                transmission.kind = TCP_FALLBACK
            else:
                transmission.kind = RETRANSMISSION

        if prior is not None:
            prior.add_transmission(transmission)
            self._index_request(prior, segment)
            return

        fallback = self._find_fallback_target(segment)
        if fallback is not None:
            transmission.kind = TCP_FALLBACK
            fallback.add_transmission(transmission)
            self._index_request(fallback, segment)
            return

        request = DNSRequest(transmission)
        self._mark_reuse(request, segment)
        self.requests.append(request)
        self._index_request(request, segment)
        self._retry_responses(request)

    def _find_same_query(self, segment):
        """Return an earlier request with the same endpoints and question.

        A query from a different client port is not a retransmission even
        when the transaction ID and question happen to match.
        """
        key = _request_key(segment)
        candidates = []
        for req in self._by_key.get(key, ()):
            if req.questions != segment.questions:
                continue
            if any(t.transport == segment.transport for t in req.transmissions):
                candidates.append(req)
        if not candidates:
            return None
        return candidates[-1]

    def _mark_reuse(self, request, segment):
        """Record whether an earlier request used the same id on these endpoints."""
        for prior in self.requests:
            if prior.id != segment.id:
                continue
            if not _same_client_server(prior.first, segment):
                continue
            request.previous_request = prior.index
            if prior.questions != segment.questions:
                request.reused_id = True
                return

    def _find_fallback_target(self, segment):
        """Find a UDP request that received TC and is being retried over TCP.

        The TCP retry usually keeps the transaction ID but may change it. It
        is only linked when exactly one unanswered UDP request with the same
        question has a truncated response and no better-priority match
        exists.
        """
        if segment.transport != 'tcp':
            return None
        targets = []
        for req in self.requests:
            if req.questions != segment.questions:
                continue
            if req.response is not None or req.transport != 'udp':
                continue
            if not req.truncated_responses:
                continue
            if not _same_client_server(req.first, segment):
                continue
            last_tc = req.truncated_responses[-1]
            if last_tc.segment.ts <= segment.ts:
                targets.append((last_tc.segment.ts, req))
        if len(targets) != 1:
            # Zero targets means this is an unrelated query. More than one
            # means the capture cannot identify which request is retried.
            return None
        return targets[0][1]

    def _retry_responses(self, request):
        """Try responses seen before their request in an offline capture."""
        for response in self.responses:
            if response.status not in (ORPHAN, CANDIDATE):
                continue
            if response.request is not None and response.request is not request:
                continue
            if response.segment.id != request.id:
                continue
            self._match_response(response, requests=[request])

    def _index_request(self, request, segment):
        key = _request_key(segment)
        requests = self._by_key.setdefault(key, [])
        if request not in requests:
            requests.append(request)

    # -- response matching ------------------------------------------------

    def _add_response(self, segment):
        response = DNSResponse(segment)
        self.responses.append(response)
        self._match_response(response)

    def _match_response(self, response, requests=None):
        """Try to place a response, including responses seen before requests."""
        segment = response.segment
        if requests is None:
            requests = self.requests
        exact = []
        same_id = []
        for req in requests:
            if req.questions == segment.questions:
                if self._any_attempt_matches(req, segment):
                    exact.append(req)
                elif _server_client_match(req, segment):
                    same_id.append(req)
            elif req.id == segment.id and _server_client_match(req, segment):
                same_id.append(req)
        exact.sort(key=lambda r: (r.ts, r.index))

        if segment.truncated:
            self._handle_truncated(response, exact, same_id, segment)
            return

        open_exact = [req for req in exact if req.response is None]
        open_endpoint = [req for req in open_exact
                         if self._any_attempt_matches(req, segment)]
        answered = [req for req in exact if req.response is not None]
        answered_endpoint = [req for req in answered
                             if self._any_attempt_matches(req, segment)]

        if len(open_endpoint) == 1:
            request = open_endpoint[0]
            if self._expired(request, segment.ts):
                response.reason = 'request expired before this response'
                self._add_candidate(request, response)
            else:
                self._accept(response, request)
            return
        if len(open_endpoint) > 1:
            response.reason = 'multiple outstanding requests share id and question'
            for req in open_endpoint:
                self._add_candidate(req, response)
            return

        for req in answered_endpoint:
            if _same_payload(req.response.segment, segment):
                response.status = DUPLICATE
                response.request = req
                response.reason = 'later copy of the accepted response'
                req.duplicates.append(response)
                return

        if open_exact:
            response.reason = 'question ok but endpoints do not identify one request'
            for req in open_exact:
                self._add_candidate(req, response)
            return

        if answered_endpoint:
            response.reason = 'additional response for an already answered request'
            for req in answered_endpoint:
                self._add_candidate(req, response)
            return

        if same_id:
            if not segment.questions_complete:
                response.reason = 'response question section is incomplete'
            elif not segment.questions:
                response.reason = 'response has no question section'
            else:
                response.reason = 'transaction id matches but question differs'
            for req in same_id:
                self._add_candidate(req, response)
            return

        response.status = ORPHAN
        response.reason = 'no request with this transaction id'

    def _handle_truncated(self, response, exact, same_id, segment):
        open_exact = [req for req in exact if req.response is None]
        open_endpoint = [req for req in open_exact
                         if self._any_attempt_matches(req, segment)]
        if len(open_endpoint) == 1:
            request = open_endpoint[0]
            if self._expired(request, segment.ts):
                response.reason = 'truncated response arrived after the request expired'
                self._add_candidate(request, response)
                return
            response.status = TRUNCATED
            response.request = request
            response.reason = 'truncated response; client may retry over TCP'
            request.truncated_responses.append(response)
            return
        if len(open_endpoint) > 1:
            response.reason = 'truncated response matches several outstanding requests'
            for req in open_endpoint:
                self._add_candidate(req, response)
            return
        if open_exact:
            response.reason = 'truncated response endpoints do not identify one request'
            for req in open_exact:
                self._add_candidate(req, response)
            return
        answered = [req for req in exact if req.response is not None]
        if answered:
            response.reason = 'truncated response for an already answered request'
            for req in answered:
                self._add_candidate(req, response)
            return
        if same_id:
            response.reason = 'truncated response question does not match'
            for req in same_id:
                self._add_candidate(req, response)
            return
        response.status = ORPHAN
        response.reason = 'truncated response with no matching request'

    def _accept(self, response, request):
        for other in self.requests:
            if response in other.candidates:
                other.candidates.remove(response)
        response.status = MATCHED
        response.request = request
        response.reason = ''
        request.response = response

    def _add_candidate(self, request, response):
        if response not in request.candidates:
            request.candidates.append(response)

    def _expired(self, request, ts):
        return self.timeout is not None and ts - request.last_ts > self.timeout

    def _any_attempt_matches(self, request, segment):
        return any(_attempt_matches_response(attempt, segment)
                   for attempt in request.transmissions)

    def _endpoints_match(self, request, segment):
        return self._any_attempt_matches(request, segment)

    # -- packet decoding --------------------------------------------------

    def _walk_frame(self, buf, segments, ts, index):
        link = self.data_link_type
        if link is None:
            link = _guess_link_type(buf)

        try:
            if link in (pcap.DLT_RAW, getattr(pcap, 'DLT_IPV4', 228),
                        getattr(pcap, 'DLT_IPV6', 229)):
                self._walk_ip(buf, segments, ts, index)
            elif link in (pcap.DLT_NULL, pcap.DLT_LOOP):
                data = loopback.Loopback(buf).data
                if isinstance(data, (ip.IP, ip6.IP6)):
                    self._walk_ip(bytes(data), segments, ts, index)
                else:
                    self._walk_frame(bytes(data), segments, ts, index)
            elif link == pcap.DLT_LINUX_SLL:
                self._walk_ip(bytes(sll.SLL(buf).data), segments, ts, index)
            elif getattr(pcap, 'DLT_LINUX_SLL2', None) == link:
                self._walk_ip(bytes(sll2.SLL2(buf).data), segments, ts, index)
            elif link == pcap.DLT_PPP:
                self._walk_ppp(ppp.PPP(buf), segments, ts, index)
            elif link == pcap.DLT_PPP_ETHER:
                self._walk_pppoe(pppoe.PPPoE(buf), segments, ts, index)
            else:
                self._walk_ethernet(buf, segments, ts, index)
        except (dpkt.UnpackError, dpkt.NeedData, ValueError, IndexError,
                struct.error):
            return

    def _walk_ethernet(self, buf, segments, ts, index):
        data = ethernet.Ethernet(buf).data
        if isinstance(data, (ip.IP, ip6.IP6)):
            self._walk_ip(bytes(data), segments, ts, index)
        elif isinstance(data, (pppoe.PPPoE, ppp.PPP)):
            self._walk_pppoe(data, segments, ts, index)
        elif isinstance(data, ethernet.Ethernet):
            self._walk_frame(bytes(data), segments, ts, index)
        elif isinstance(data, bytes):
            self._walk_frame(data, segments, ts, index)

    def _walk_pppoe(self, packet, segments, ts, index):
        if isinstance(packet, pppoe.PPPoE):
            if packet.code != pppoe.PPPoE_SESSION:
                return
            packet = packet.ppp
        self._walk_ppp(packet, segments, ts, index)

    def _walk_ppp(self, packet, segments, ts, index):
        while isinstance(packet, ppp.PPP):
            packet = packet.data
        if isinstance(packet, (ip.IP, ip6.IP6)):
            self._walk_ip(bytes(packet), segments, ts, index)

    def _walk_ip(self, buf, segments, ts, index):
        if not buf:
            return
        version = bytearray(buf)[0] >> 4
        if version == 4:
            try:
                ipp = ip.IP(buf)
            except (dpkt.UnpackError, dpkt.NeedData):
                return
            if ipp.offset:
                return
            src, dst, proto, payload = ipp.src, ipp.dst, ipp.p, ipp.data
        elif version == 6:
            try:
                ipp = ip6.IP6(buf)
            except (dpkt.UnpackError, dpkt.NeedData):
                return
            src, dst, proto, payload = ipp.src, ipp.dst, ipp.p, ipp.data
        else:
            return

        if proto == ip.IP_PROTO_UDP:
            if not isinstance(payload, udp.UDP):
                try:
                    payload = udp.UDP(payload)
                except (dpkt.UnpackError, dpkt.NeedData):
                    return
            if _is_dns_port(payload.sport, payload.dport):
                data = bytes(payload.data)
                if payload.ulen and payload.ulen >= udp.UDP_HDR_LEN:
                    data = data[:payload.ulen - udp.UDP_HDR_LEN]
                segments.append(DNSSegment(ts, index, src, dst, payload.sport,
                                           payload.dport, 'udp', data))
        elif proto == ip.IP_PROTO_TCP:
            if not isinstance(payload, tcp.TCP):
                try:
                    payload = tcp.TCP(payload)
                except (dpkt.UnpackError, dpkt.NeedData):
                    return
            if not _is_dns_port(payload.sport, payload.dport):
                return
            flow = (src, payload.sport, dst, payload.dport)
            stream = self._tcp_streams.get(flow)
            if stream is None or (payload.flags & tcp.TH_SYN):
                stream = _TCPStream(flow)
                self._tcp_streams[flow] = stream
            stream.add(payload.seq + 1 if payload.flags & tcp.TH_SYN else payload.seq,
                       bytes(payload.data), ts, index)
            self._drain_stream(flow, final=False)

    def _drain_stream(self, flow, final=False):
        stream = self._tcp_streams.get(flow)
        if stream is None:
            return
        if final:
            stream.flush()
        src, sport, dst, dport = stream.endpoints
        while True:
            message = stream.next_message()
            if message is None:
                break
            segment = DNSSegment(message.ts, message.index, src, dst, sport,
                                 dport, 'tcp', message.payload)
            self._add_segment(segment)

    # -- reporting --------------------------------------------------------

    def table(self):
        """Return per-request rows suitable for reporting or JSON output."""
        rows = []
        for request in sorted(self.requests, key=lambda r: (r.index, r.ts)):
            rows.append({
                'index': request.index,
                'timestamp': request.ts,
                'id': request.id,
                'client': _addr(request.client),
                'server': _addr(request.server),
                'client_port': request.client_port,
                'server_port': request.server_port,
                'transport': request.transport,
                'name': request.name,
                'questions': [{'name': name, 'type': qtype, 'class': qclass}
                              for name, qtype, qclass in request.questions],
                'kind': request.transmissions[0].kind,
                'classification': request.classification,
                'reused_id': request.reused_id,
                'previous_request': request.previous_request,
                'status': request.status,
                'fallback': request.fallback,
                'response_index': None if request.response is None else request.response.index,
                'response_rcode': None if request.response is None else request.response.rcode,
                'response_partial': (request.response is not None and
                                     request.response.partial),
                'candidates': [c.index for c in request.candidates],
                'duplicates': [d.index for d in request.duplicates],
                'truncated': [t.index for t in request.truncated_responses],
                'transmissions': [{'index': t.index,
                                   'transport': t.transport,
                                   'kind': t.kind}
                                  for t in request.transmissions],
            })
        return rows

    def message_table(self):
        """Return one row per DNS message with its classification."""
        rows = []
        for segment in self.segments:
            if segment.is_response:
                response = self._response_for(segment)
                status = response.status if response is not None else ORPHAN
                request = response.request if response is not None else None
                reason = response.reason if response is not None else ''
            else:
                transmission = self._transmission_for(segment)
                status = transmission.kind if transmission is not None else ORIGINAL
                request = self._request_for(segment)
                reason = ''
            rows.append({
                'index': segment.index,
                'timestamp': segment.ts,
                'id': segment.id,
                'direction': 'response' if segment.is_response else 'query',
                'transport': segment.transport,
                'client': _addr(segment.dst if segment.is_response else segment.src),
                'server': _addr(segment.src if segment.is_response else segment.dst),
                'name': segment.name,
                'classification': status,
                'request_index': None if request is None else request.index,
                'partial': segment.partial,
                'truncated': segment.truncated,
                'reason': reason,
            })
        return rows

    def _response_for(self, segment):
        for response in self.responses:
            if response.segment is segment:
                return response
        return None

    def _request_for(self, segment):
        for request in self.requests:
            for transmission in request.transmissions:
                if transmission.segment is segment:
                    return request
        return None

    def _transmission_for(self, segment):
        for request in self.requests:
            for transmission in request.transmissions:
                if transmission.segment is segment:
                    return transmission
        return None


def _request_key(segment):
    return (segment.src, segment.sport, segment.dst, segment.dport, segment.id)


def _is_forward(segment):
    return segment.dport == DNS_PORT and segment.sport != DNS_PORT


def _attempt_matches_response(attempt, response):
    """Whether a request attempt is addressed by this response.

    The response travels in the opposite direction and uses the same
    transport as the attempt it answers.
    """
    if attempt.transport != response.transport:
        return False
    query = attempt.segment
    return (query.src == response.dst and
            query.dst == response.src and
            query.sport == response.dport and
            query.dport == response.sport and
            query.id == response.id)


def _same_client_server(left, right):
    if _is_forward(left):
        client, server = left.src, left.dst
    else:
        client, server = left.dst, left.src
    if _is_forward(right):
        other_client, other_server = right.src, right.dst
    else:
        other_client, other_server = right.dst, right.src
    return client == other_client and server == other_server


def _server_client_match(request, segment):
    """Whether request and segment share client/server IP and transaction ID."""
    first = request.first
    if request.id != segment.id:
        return False
    if _is_forward(first):
        client, server = first.src, first.dst
    else:
        client, server = first.dst, first.src
    if _is_forward(segment):
        seg_client, seg_server = segment.src, segment.dst
    else:
        seg_client, seg_server = segment.dst, segment.src
    return client == seg_client and server == seg_server


def _is_dns_port(sport, dport):
    return sport == DNS_PORT or dport == DNS_PORT


def _same_payload(left, right):
    return left is right or left.payload == right.payload


def _guess_link_type(buf):
    if not buf:
        return pcap.DLT_EN10MB
    if len(buf) >= 14:
        ether_type = (buf[12] << 8) | buf[13]
        if ether_type in (ethernet.ETH_TYPE_IP, ethernet.ETH_TYPE_IP6,
                          ethernet.ETH_TYPE_PPPoE):
            return pcap.DLT_EN10MB
    first = bytearray(buf)[0]
    if first >> 4 in (4, 6):
        return pcap.DLT_RAW
    if len(buf) >= 4:
        family_le = buf[0] | (buf[1] << 8) | (buf[2] << 16) | (buf[3] << 24)
        family_be = ((buf[0] << 24) | (buf[1] << 16) |
                     (buf[2] << 8) | buf[3])
        if family_le in (2, 24, 28, 30) or family_be in (2, 24, 28, 30):
            return pcap.DLT_NULL
    return pcap.DLT_EN10MB


class _TCPStream(object):
    """Reassemble DNS-over-TCP messages for one direction of a connection."""

    __slots__ = ('endpoints', '_chunks', '_next_seq', '_queue', '_pending',
                 '_pending_ts', '_pending_index')

    def __init__(self, endpoints):
        self.endpoints = endpoints
        self._chunks = {}
        self._next_seq = None
        self._queue = []
        self._pending = b''
        self._pending_ts = 0.0
        self._pending_index = 0

    def add(self, seq, payload, ts, index):
        if not payload:
            return
        self._chunks[seq] = (payload, ts, index)
        if self._next_seq is None:
            self._next_seq = seq
        self._reassemble()

    def _reassemble(self):
        while True:
            chunk = self._chunks.get(self._next_seq)
            if chunk is None:
                break
            payload, ts, index = chunk
            del self._chunks[self._next_seq]
            self._next_seq += len(payload)
            if not self._pending:
                self._pending_ts = ts
                self._pending_index = index
            self._pending += payload
            while len(self._pending) >= 2:
                length = (self._pending[0] << 8) | self._pending[1]
                if len(self._pending) < 2 + length:
                    break
                body = self._pending[2:2 + length]
                if body:
                    self._queue.append(_TCPMessage(body, self._pending_ts,
                                                   self._pending_index))
                self._pending = self._pending[2 + length:]
                self._pending_ts = ts
                self._pending_index = index

    def next_message(self):
        if self._queue:
            return self._queue.pop(0)
        return None

    def flush(self):
        # A capture may end mid-message; that tail is not a DNS message.
        self._pending = b''


class _TCPMessage(object):
    __slots__ = ('payload', 'ts', 'index')

    def __init__(self, payload, ts, index):
        self.payload = payload
        self.ts = ts
        self.index = index


def print_table(table, stream=None):
    """Write a human readable request/response table."""
    if stream is None:
        stream = sys.stdout
    headers = ('#', 'client', 'server', 'id', 'name', 'classification',
               'attempts', 'status', 'resp', 'rcode', 'candidates', 'fallback')
    rows = table.table()
    widths = [len(h) for h in headers]
    values = []
    for row in rows:
        attempts = ','.join(
            '%d:%s' % (t['index'], t['kind']) for t in row['transmissions'])
        value = (
            str(row['index']),
            '%s:%d' % (row['client'], row['client_port']),
            '%s:%d' % (row['server'], row['server_port']),
            '0x%04x' % row['id'],
            row['name'],
            row['classification'],
            attempts,
            row['status'],
            '-' if row['response_index'] is None else str(row['response_index']),
            '-' if row['response_rcode'] is None else str(row['response_rcode']),
            ','.join(str(i) for i in row['candidates']) or '-',
            'yes' if row['fallback'] else 'no',
        )
        values.append(value)
        widths = [max(w, len(v)) for w, v in zip(widths, value)]

    def emit(fields):
        stream.write('  '.join(v.ljust(w) for v, w in zip(fields, widths)).rstrip() + '\n')

    emit(headers)
    emit(tuple('-' * w for w in widths))
    for value in values:
        emit(value)


def print_messages(table, stream=None):
    """Write one line per DNS message with its classification."""
    if stream is None:
        stream = sys.stdout
    headers = ('#', 'dir', 'transport', 'id', 'client', 'server', 'name',
               'classification', 'request', 'reason')
    rows = table.message_table()
    widths = [len(h) for h in headers]
    values = []
    for row in rows:
        value = (
            str(row['index']),
            row['direction'],
            row['transport'],
            '0x%04x' % row['id'] if row['id'] is not None else '-',
            row['client'],
            row['server'],
            row['name'],
            row['classification'],
            '-' if row['request_index'] is None else str(row['request_index']),
            row['reason'],
        )
        values.append(value)
        widths = [max(w, len(v)) for w, v in zip(widths, value)]

    def emit(fields):
        stream.write('  '.join(v.ljust(w) for v, w in zip(fields, widths)).rstrip() + '\n')

    emit(headers)
    emit(tuple('-' * w for w in widths))
    for value in values:
        emit(value)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build a DNS request/response table from a pcap or pcapng file.')
    parser.add_argument('capture', help='path to a pcap or pcapng file')
    parser.add_argument('--timeout', type=float, default=None,
                        help='seconds before an unanswered request expires')
    parser.add_argument('--messages', action='store_true',
                        help='print one row per DNS message instead of per request')
    parser.add_argument('--json', action='store_true', dest='as_json',
                        help='write the table as JSON')
    args = parser.parse_args(argv)

    with open(args.capture, 'rb') as capture:
        table = DNSFlowTable.from_capture(capture, timeout=args.timeout)

    if args.as_json:
        payload = table.message_table() if args.messages else table.table()
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write('\n')
    elif args.messages:
        print_messages(table)
    else:
        print_table(table)
    return 0


if __name__ == '__main__':
    sys.exit(main())
