# -*- coding: utf-8 -*-
"""TCP session generation tracking for offline packet captures.

A capture may contain the same TCP four-tuple many times (for example when a
client reuses an ephemeral port).  A four-tuple therefore identifies a *flow
bucket*, not a connection.  This module keeps every observed generation of a
four-tuple separate and records why each generation boundary was accepted.

The important rule is that only positive evidence may create a confirmed
generation.  Positive evidence is a handshake (SYN/SYN+ACK), a closure
(FIN in both directions or RST), or a sequence-space discontinuity observed
together with a closure.  An idle timeout is deliberately weaker: it creates a
:class:`GenerationCandidate` that can be inspected, but it never splits a
confirmed session by itself.  If later traffic continues the sequence space of
the old session, the candidate is rejected.

A capture can start in the middle of a connection.  Such a generation has
``start_known == False`` and retains that fact for its lifetime.  Likewise, a
connection that is inferred after a closure but whose SYN was not captured has
an unknown start boundary.

The table is transport-oriented and does not attempt TCP reassembly.  It can be
fed ``dpkt`` Ethernet/IP/TCP objects directly, or packets read from a pcap with
:func:`read_sessions`.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import socket

from . import dpkt
from . import ethernet
from . import ip
from . import ip6
from . import pcap
from . import tcp

try:  # pragma: no cover - Python 2 compatibility
    string_types = (basestring,)  # noqa: F821
except NameError:  # pragma: no cover
    string_types = (str,)

UINT32 = 1 << 32
SEQ_HALF = 1 << 31

# A sequence jump larger than this is considered a discontinuity when no
# handshake or closure was observed.  It is intentionally much larger than a
# normal receive window; the value is only used to describe evidence, never to
# confirm a boundary by itself.
SEQ_DISCONTINUITY_WINDOW = 1 << 20

# For the "does this packet continue the old session?" test, ordinary
# reordering/retransmission is expected within a few segments, not a megabyte.
# A large forward jump after a timeout or close is therefore evidence of new
# traffic even though it may still be within the same 32-bit sequence space.
SEQ_CONTINUITY_WINDOW = 1 << 16

# Session states.  "superseded" is a terminal state for a generation which was
# replaced by a later, explicitly observed SYN while it was still open.
STATE_UNKNOWN = 'unknown'
STATE_SYN_SENT = 'syn_sent'
STATE_SIMULTANEOUS_OPEN = 'simultaneous_open'
STATE_ESTABLISHED = 'established'
STATE_CLOSING = 'closing'
STATE_CLOSED = 'closed'
STATE_RESET = 'reset'
STATE_SUPERSEDED = 'superseded'

# Start/end boundary reasons.  These strings are part of the traceability API.
START_SYN = 'syn'
START_SYN_ACK = 'syn_ack'
START_MIDSTREAM = 'midstream'
START_AFTER_CLOSE = 'midstream_after_close'
START_AFTER_RESET = 'midstream_after_reset'
START_AFTER_TIMEOUT = 'midstream_after_timeout'
START_SIMULTANEOUS_OPEN = 'simultaneous_open'
START_NEW_ISN = 'new_isn'

END_FIN = 'fin_exchange'
END_RST = 'reset'
END_SUPERSEDED = 'superseded_by_new_syn'
END_CAPTURE = 'capture_end'

CANDIDATE_TIMEOUT = 'idle_timeout'

CANDIDATE_PENDING = 'pending'
CANDIDATE_REJECTED = 'rejected'
CANDIDATE_SUPERSEDED = 'superseded'
CANDIDATE_UNRESOLVED = 'unresolved'


def _address_text(value):
    """Return a stable text representation of an IPv4/IPv6 address."""
    if isinstance(value, bytes):
        if len(value) == 4:
            return socket.inet_ntoa(value)
        if len(value) == 16:
            return socket.inet_ntop(socket.AF_INET6, value)
    if isinstance(value, bytearray):
        return _address_text(bytes(value))
    return str(value)


def _endpoint(value, port=None):
    if port is not None:
        return (_address_text(value), int(port))
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return (_address_text(value[0]), int(value[1]))
    raise TypeError('endpoint must be (address, port)')


class SessionKey(tuple):
    """Canonical, bidirectional TCP four-tuple.

    The two endpoints are sorted so that the key is identical for both traffic
    directions.  Instances compare equal to ordinary four-tuples in the
    canonical order, and :meth:`matches` accepts either orientation.
    """

    __slots__ = ()

    def __new__(cls, src, sport, dst, dport):
        a = (_address_text(src), int(sport))
        b = (_address_text(dst), int(dport))
        if b < a:
            a, b = b, a
        return tuple.__new__(cls, (a[0], a[1], b[0], b[1]))

    @property
    def first(self):
        return (self[0], self[1])

    @property
    def second(self):
        return (self[2], self[3])

    @property
    def endpoints(self):
        return (self.first, self.second)

    @property
    def src(self):
        return self[0]

    @property
    def sport(self):
        return self[1]

    @property
    def dst(self):
        return self[2]

    @property
    def dport(self):
        return self[3]

    def matches(self, other):
        try:
            return self == canonical_key(other)
        except (TypeError, ValueError):
            return False

    def reverse(self):
        return SessionKey(self[2], self[3], self[0], self[1])

    def __repr__(self):
        return 'SessionKey(%r, %r, %r, %r)' % self


def canonical_key(*args):
    """Build a canonical :class:`SessionKey`.

    Accepted forms are ``canonical_key(src, sport, dst, dport)``,
    ``canonical_key((src, sport), (dst, dport))``, an existing key, or an
    object exposing ``src``, ``sport``, ``dst`` and ``dport``.
    """
    if len(args) == 1:
        value = args[0]
        if isinstance(value, SessionKey):
            return value
        if isinstance(value, (tuple, list)):
            if len(value) == 4:
                return SessionKey(*value)
            if len(value) == 2:
                return SessionKey(value[0][0], value[0][1], value[1][0], value[1][1])
        if all(hasattr(value, name) for name in ('src', 'sport', 'dst', 'dport')):
            return SessionKey(value.src, value.sport, value.dst, value.dport)
        raise TypeError('cannot build a session key from %r' % (value,))
    if len(args) == 2:
        return SessionKey(args[0][0], args[0][1], args[1][0], args[1][1])
    if len(args) == 4:
        return SessionKey(*args)
    raise TypeError('canonical_key expects 1, 2 or 4 arguments')


make_key = canonical_key
build_key = canonical_key


def _seq_delta(a, b):
    """Signed distance from ``b`` to ``a`` in 32-bit TCP sequence space."""
    return ((int(a) - int(b) + SEQ_HALF) % UINT32) - SEQ_HALF


class PacketInfo(object):
    """Normalized information extracted from one TCP packet."""

    __slots__ = ('packet', 'timestamp', 'src', 'dst', 'sport', 'dport',
                 'seq', 'ack', 'flags', 'payload_len', 'key', 'direction',
                 'is_retransmission', 'session')

    def __init__(self, packet, timestamp, src, sport, dst, dport, seq, ack,
                 flags, payload_len=0):
        self.packet = packet
        self.timestamp = timestamp
        self.src = _address_text(src)
        self.dst = _address_text(dst)
        self.sport = int(sport)
        self.dport = int(dport)
        self.seq = int(seq) & 0xffffffff
        self.ack = int(ack) & 0xffffffff
        self.flags = int(flags)
        self.payload_len = int(payload_len)
        self.key = SessionKey(self.src, self.sport, self.dst, self.dport)
        self.direction = 0 if self.key.first == (self.src, self.sport) else 1
        self.is_retransmission = False
        self.session = None

    @property
    def src_endpoint(self):
        return (self.src, self.sport)

    @property
    def dst_endpoint(self):
        return (self.dst, self.dport)

    @property
    def syn(self):
        return bool(self.flags & tcp.TH_SYN)

    @property
    def syn_only(self):
        return bool(self.flags & tcp.TH_SYN) and not bool(self.flags & tcp.TH_ACK)

    @property
    def syn_ack(self):
        return bool(self.flags & tcp.TH_SYN) and bool(self.flags & tcp.TH_ACK)

    @property
    def ack_only(self):
        return (bool(self.flags & tcp.TH_ACK) and
                not bool(self.flags & tcp.TH_SYN) and
                not self.payload_len and
                not bool(self.flags & (tcp.TH_FIN | tcp.TH_RST)))

    @property
    def fin(self):
        return bool(self.flags & tcp.TH_FIN)

    @property
    def rst(self):
        return bool(self.flags & tcp.TH_RST)

    def fingerprint(self):
        # Payload bytes are not available for every input form; the sequence
        # and control fields are sufficient for retransmission detection.
        return (self.src, self.sport, self.dst, self.dport, self.seq,
                self.ack, self.flags, self.payload_len)

    def as_dict(self):
        return {
            'timestamp': self.timestamp,
            'src': self.src,
            'sport': self.sport,
            'dst': self.dst,
            'dport': self.dport,
            'seq': self.seq,
            'ack': self.ack,
            'flags': self.flags,
            'payload_len': self.payload_len,
            'retransmission': self.is_retransmission,
        }

    def __repr__(self):
        return ('PacketInfo(%s:%s -> %s:%s seq=%s ack=%s flags=%s)' %
                (self.src, self.sport, self.dst, self.dport, self.seq,
                 self.ack, tcp.tcp_flags_to_str(self.flags)))


class Boundary(object):
    """A traceable start, end, or candidate generation boundary."""

    __slots__ = ('kind', 'key', 'generation', 'timestamp', 'reason', 'known',
                 'candidate', 'status', 'evidence', 'resolution')

    def __init__(self, kind, key, generation, timestamp, reason, known=True,
                 candidate=False, status='confirmed', evidence=None,
                 resolution=None):
        self.kind = kind
        self.key = key
        self.generation = generation
        self.timestamp = timestamp
        self.reason = reason
        self.known = bool(known)
        self.candidate = bool(candidate)
        self.status = status
        self.evidence = dict(evidence or {})
        self.resolution = resolution

    @property
    def confirmed(self):
        return not self.candidate and self.status == 'confirmed'

    def as_dict(self):
        return {
            'kind': self.kind,
            'key': tuple(self.key),
            'generation': self.generation,
            'timestamp': self.timestamp,
            'reason': self.reason,
            'known': self.known,
            'candidate': self.candidate,
            'status': self.status,
            'evidence': dict(self.evidence),
            'resolution': self.resolution,
        }

    def __repr__(self):
        return ('Boundary(kind=%r, key=%r, generation=%r, reason=%r, '
                'known=%r, candidate=%r, status=%r)' %
                (self.kind, tuple(self.key), self.generation, self.reason,
                 self.known, self.candidate, self.status))


GenerationBoundary = Boundary


class GenerationCandidate(Boundary):
    """A suspected generation boundary which is not yet confirmed.

    Timeout handling only ever produces these objects.  They are exposed by
    :attr:`SessionTable.candidates` so callers can audit the heuristic without
    mistaking it for an observed TCP connection boundary.
    """

    __slots__ = ('packets', 'gap', 'first_timestamp', 'last_timestamp',
                 '_direction_state')

    def __init__(self, key, generation, timestamp, gap, evidence=None):
        Boundary.__init__(self, 'candidate', key, generation, timestamp,
                          CANDIDATE_TIMEOUT, known=False, candidate=True,
                          status=CANDIDATE_PENDING, evidence=evidence)
        self.packets = []
        self.gap = gap
        self.first_timestamp = timestamp
        self.last_timestamp = timestamp
        self._direction_state = {}

    def add_packet(self, info):
        self.packets.append(info)
        info.session = None
        self.last_timestamp = info.timestamp
        state = self._direction_state.get(info.src_endpoint)
        if state is None:
            state = {
                'first_seq': info.seq,
                'max_end': 0,
                'last_seq': info.seq,
                'last_ack': info.ack,
            }
            self._direction_state[info.src_endpoint] = state
        offset = _seq_delta(info.seq, state['first_seq'])
        end_offset = offset + info.payload_len + (1 if info.fin else 0)
        if end_offset > state['max_end']:
            state['max_end'] = end_offset
        state['last_seq'] = info.seq
        state['last_ack'] = info.ack

    @property
    def packet_count(self):
        return len(self.packets)

    def continues(self, info):
        state = self._direction_state.get(info.src_endpoint)
        if state is None:
            # A reverse packet can still belong to the candidate if its ACK
            # acknowledges sequence space already seen in the candidate.
            reverse = self._direction_state.get(info.dst_endpoint)
            if reverse is not None:
                off = _seq_delta(info.ack, reverse['first_seq'])
                return -SEQ_CONTINUITY_WINDOW <= off <= reverse['max_end'] + SEQ_CONTINUITY_WINDOW
            return len(self.packets) == 0
        if info.fingerprint() in set(p.fingerprint() for p in self.packets):
            return True
        off = _seq_delta(info.seq, state['first_seq'])
        end_off = off + info.payload_len + (1 if info.fin else 0)
        if -SEQ_CONTINUITY_WINDOW <= off <= state['max_end'] + SEQ_CONTINUITY_WINDOW:
            return True
        if info.ack_only:
            if _seq_delta(info.ack, state['first_seq']) >= -SEQ_CONTINUITY_WINDOW:
                return True
        return False

    def resolve(self, status, reason=None, evidence=None):
        self.status = status
        if reason is not None:
            self.resolution = reason
        if evidence:
            self.evidence.update(evidence)

    def as_dict(self):
        data = Boundary.as_dict(self)
        data['packet_count'] = len(self.packets)
        data['gap'] = self.gap
        data['first_timestamp'] = self.first_timestamp
        data['last_timestamp'] = self.last_timestamp
        return data


class Session(object):
    """One observed generation of a TCP four-tuple."""

    def __init__(self, key, generation, start_boundary):
        self.key = key
        self.four_tuple = key
        self.generation = generation
        self.index = generation - 1
        self.generation_id = generation
        self.start_boundary = start_boundary
        self.end_boundary = None
        self.boundaries = [start_boundary]
        self.packets = []
        self.candidates = []
        self.state = STATE_UNKNOWN
        self.client = None
        self.server = None
        self.client_isn = None
        self.server_isn = None
        self.saw_syn = False
        self.saw_syn_ack = False
        self.saw_ack = False
        self.simultaneous_open = False
        self.superseded = False
        self.closed = False
        self.reset = False
        self.fin_endpoints = set()
        self.first_timestamp = None
        self.last_timestamp = None
        self.retransmission_count = 0
        self.duplicate_ack_count = 0
        self.anomalies = []
        self._fingerprints = set()
        self._directions = {}

    # -- basic properties -------------------------------------------------
    @property
    def label(self):
        return '%s:%s-%s:%s#%d' % (self.key[0], self.key[1], self.key[2],
                                   self.key[3], self.generation)

    @property
    def start_time(self):
        return self.start_boundary.timestamp

    @property
    def start_reason(self):
        return self.start_boundary.reason

    @property
    def end_reason(self):
        return self.end_boundary.reason if self.end_boundary else None

    @property
    def end_time(self):
        return self.end_boundary.timestamp if self.end_boundary else None

    @property
    def start_known(self):
        return self.start_boundary.known

    @property
    def start_boundary_known(self):
        return self.start_known

    @property
    def start_unknown(self):
        return not self.start_known

    @property
    def end_known(self):
        return self.end_boundary is not None and self.end_boundary.known

    @property
    def end_boundary_known(self):
        return self.end_known

    @property
    def boundary_known(self):
        # Kept for callers which ask the common "was the beginning observed?"
        # question.  Both dimensions are available separately above.
        return self.start_known

    @property
    def packet_count(self):
        return len(self.packets)

    @property
    def retransmissions(self):
        return self.retransmission_count

    @property
    def active(self):
        return not self.closed and not self.superseded

    @property
    def handshake_complete(self):
        return self.saw_syn and self.saw_syn_ack and (self.saw_ack or self.simultaneous_open)

    @property
    def endpoints(self):
        return self.key.endpoints

    def __len__(self):
        return len(self.packets)

    def __iter__(self):
        return iter(self.packets)

    # -- internals --------------------------------------------------------
    def _dir(self, endpoint):
        state = self._directions.get(endpoint)
        if state is None:
            state = {
                'first_seq': None,
                'max_end': 0,
                'last_seq': None,
                'last_ack': None,
                'syn_seq': None,
                'fin_seq': None,
                'syn_ack_seq': None,
                'last_timestamp': None,
            }
            self._directions[endpoint] = state
        return state

    def _offset(self, state, seq):
        if state['first_seq'] is None:
            state['first_seq'] = seq
            return 0
        return _seq_delta(seq, state['first_seq'])

    def _is_retransmission(self, info):
        if info.fingerprint() in self._fingerprints:
            return True
        state = self._directions.get(info.src_endpoint)
        if state is None:
            return False
        if info.syn_only and state['syn_seq'] == info.seq:
            return True
        if info.syn_ack and state['syn_ack_seq'] == info.seq:
            return True
        if (info.payload_len or info.fin) and state['first_seq'] is not None:
            offset = _seq_delta(info.seq, state['first_seq'])
            end_offset = offset + info.payload_len + (1 if info.fin else 0)
            # Only a segment wholly inside already-covered sequence space is a
            # retransmission.  A segment which starts at the old high-water
            # mark is new data even if its ACK is unchanged.
            if offset < state['max_end'] and end_offset <= state['max_end']:
                return True
        if info.ack_only and not info.syn and not info.fin and not info.rst:
            if info.seq == state['last_seq'] and info.ack == state['last_ack']:
                return True
        return False
    def _continuity(self, info):
        """Describe whether ``info`` continues this generation's sequence space.

        Returns ``('same', detail)``, ``('different', detail)``, or
        ``('unknown', detail)``.  This is evidence only; it does not create a
        boundary.
        """
        state = self._directions.get(info.src_endpoint)
        reverse = self._directions.get(info.dst_endpoint)
        if state is None and reverse is None:
            return 'unknown', {'reason': 'no_sequence_history'}

        if state is not None:
            if self._is_retransmission(info):
                return 'same', {'reason': 'retransmission'}
            if info.syn:
                if state['syn_seq'] == info.seq:
                    return 'same', {'reason': 'syn_retransmission'}
                return 'different', {'reason': 'new_isn', 'old_isn': state['syn_seq'],
                                     'new_isn': info.seq}
            offset = _seq_delta(info.seq, state['first_seq'])
            if info.payload_len or info.fin:
                end_offset = offset + info.payload_len + (1 if info.fin else 0)
                if -SEQ_CONTINUITY_WINDOW <= offset <= state['max_end'] + SEQ_CONTINUITY_WINDOW:
                    if end_offset <= state['max_end']:
                        return 'same', {'reason': 'retransmission_overlap'}
                    return 'same', {'reason': 'sequence_continues',
                                    'offset': offset}
                if offset > state['max_end'] + SEQ_CONTINUITY_WINDOW:
                    return 'different', {'reason': 'sequence_jump',
                                         'jump': offset - state['max_end']}
                return 'different', {'reason': 'sequence_before_history',
                                     'jump': offset}
            else:
                # A bare ACK carries no data sequence space of its own.  Its
                # acknowledgement number is the useful evidence; the sequence
                # number is normally unchanged and may legitimately be old.
                if reverse is not None and reverse['first_seq'] is not None:
                    ack_offset = _seq_delta(info.ack, reverse['first_seq'])
                    if -SEQ_CONTINUITY_WINDOW <= ack_offset <= reverse['max_end'] + SEQ_CONTINUITY_WINDOW:
                        return 'same', {'reason': 'acknowledges_known_sequence',
                                        'ack_offset': ack_offset}
                if (-SEQ_CONTINUITY_WINDOW <= offset <=
                        state['max_end'] + SEQ_CONTINUITY_WINDOW):
                    return 'same', {'reason': 'ack_sequence_continues',
                                    'offset': offset}
                if offset > state['max_end'] + SEQ_CONTINUITY_WINDOW:
                    return 'different', {'reason': 'ack_sequence_jump',
                                         'jump': offset - state['max_end']}

        # A reverse-direction ACK can be decisive even if this direction has
        # not been seen yet.
        if reverse is not None and reverse['first_seq'] is not None:
            ack_offset = _seq_delta(info.ack, reverse['first_seq'])
            if -SEQ_CONTINUITY_WINDOW <= ack_offset <= reverse['max_end'] + SEQ_CONTINUITY_WINDOW:
                return 'same', {'reason': 'acknowledges_known_sequence',
                                'ack_offset': ack_offset}
            if info.ack == 0 and reverse['syn_seq'] is not None:
                return 'different', {'reason': 'zero_ack'}
        return 'unknown', {'reason': 'no_comparable_sequence'}

    def _update_sequence_state(self, info):
        state = self._dir(info.src_endpoint)
        offset = self._offset(state, info.seq)
        end_offset = offset + info.payload_len + (1 if info.fin else 0)
        if end_offset > state['max_end']:
            state['max_end'] = end_offset
        state['last_seq'] = info.seq
        state['last_ack'] = info.ack
        state['last_timestamp'] = info.timestamp
        if info.syn:
            state['syn_seq'] = info.seq
        if info.syn_ack:
            state['syn_ack_seq'] = info.seq
        if info.fin and state['fin_seq'] is None:
            state['fin_seq'] = info.seq
        self._fingerprints.add(info.fingerprint())

    def _set_client_server(self, info):
        if self.client is None and info.syn_only:
            self.client = info.src_endpoint
            self.server = info.dst_endpoint
            self.client_isn = info.seq
        elif self.client is None and info.syn_ack:
            # The SYN+ACK identifies the server.  The client's original SYN is
            # before the capture, but the acknowledgement number reveals its
            # ISN and lets retransmitted SYNs be recognized.  This does not
            # mean the handshake's beginning was observed.
            self.server = info.src_endpoint
            self.client = info.dst_endpoint
            self.server_isn = info.seq
            self.client_isn = (info.ack - 1) & 0xffffffff
        elif self.server is None and self.client is not None:
            self.server = info.dst_endpoint if info.src_endpoint == self.client else info.src_endpoint

    def _handle_syn(self, info):
        self._set_client_server(info)
        if info.syn_ack:
            self.saw_syn_ack = True
            if info.src_endpoint == self.server:
                self.server_isn = info.seq
            # A retransmitted SYN+ACK or a response to a SYN observed earlier
            # in the capture completes the handshake.  A SYN+ACK which is the
            # first packet does not make the connection's start known.
            client_state = self._directions.get(self.client) if self.client else None
            if (client_state is not None and
                    client_state.get('syn_seq') is not None and
                    info.ack == ((client_state['syn_seq'] + 1) & 0xffffffff)):
                self.saw_syn = True
            elif self.simultaneous_open:
                self.saw_syn = True
            if self.saw_syn:
                self.state = STATE_ESTABLISHED
            return

        if not self.saw_syn:
            self.saw_syn = True
            self.state = STATE_SYN_SENT
            return

        if info.src_endpoint == self.client:
            # Same-direction SYN with a different ISN in an active handshake is
            # a fresh attempt; the caller decides whether to split.
            self.client_isn = info.seq
            if not self.simultaneous_open:
                self.state = STATE_SYN_SENT
            return

        # A SYN from the peer before any SYN+ACK is TCP simultaneous open.
        # Both SYNs are one connection and must never produce two generations.
        self.simultaneous_open = True
        self.server = info.src_endpoint
        self.client = info.dst_endpoint
        self.server_isn = info.seq
        self.state = STATE_SIMULTANEOUS_OPEN

    def _handle_close(self, info):
        if info.rst:
            self.reset = True
            self.closed = True
            self.state = STATE_RESET
            return END_RST
        if info.fin:
            self.fin_endpoints.add(info.src_endpoint)
            if len(self.fin_endpoints) >= 2 or len(self.fin_endpoints) >= len(self.endpoints):
                self.closed = True
                self.state = STATE_CLOSED
                return END_FIN
            self.state = STATE_CLOSING
        return None

    def observe(self, info, retransmission=False, store=True):
        """Record ``info`` in this generation and update its TCP state."""
        info.session = self
        info.is_retransmission = retransmission
        if store:
            self.packets.append(info)
        if self.first_timestamp is None or info.timestamp < self.first_timestamp:
            self.first_timestamp = info.timestamp
        if self.last_timestamp is None or info.timestamp >= self.last_timestamp:
            self.last_timestamp = info.timestamp
        if retransmission:
            self.retransmission_count += 1
            # A retransmission is evidence about an already-recorded segment;
            # replaying its flags must not move the session backwards.
            return self

        self._update_sequence_state(info)

        if info.syn:
            self._handle_syn(info)
        elif info.ack_only:
            self.saw_ack = True
            if self.saw_syn and self.saw_syn_ack and self.state in (STATE_SYN_SENT, STATE_SIMULTANEOUS_OPEN):
                self.state = STATE_ESTABLISHED
            elif self.saw_syn_ack and self.server_isn is not None:
                if info.ack == ((self.server_isn + 1) & 0xffffffff):
                    self.state = STATE_ESTABLISHED
            elif self.start_unknown and not self.saw_syn:
                # A pure ACK in a capture which began midstream proves the
                # connection was already established, but not when it began.
                self.state = STATE_ESTABLISHED
        else:
            if info.flags & tcp.TH_ACK:
                self.saw_ack = True
            if self.state in (STATE_UNKNOWN, STATE_SYN_SENT) and not self.saw_syn:
                # Midstream traffic: a data packet carrying ACK is enough to
                # know that the connection was already established, but not
                # when it started.
                if info.flags & tcp.TH_ACK:
                    self.state = STATE_ESTABLISHED

        end_reason = self._handle_close(info)
        if end_reason and self.end_boundary is None:
            evidence = info.as_dict()
            evidence['retransmission'] = retransmission
            self.end_boundary = Boundary(
                'end', self.key, self.generation, info.timestamp,
                end_reason, known=True, evidence=evidence)
            self.boundaries.append(self.end_boundary)
        return self

    def close(self, timestamp, reason=END_CAPTURE, known=False, evidence=None):
        """Attach an end boundary if one is not already present."""
        if self.end_boundary is None:
            self.end_boundary = Boundary(
                'end', self.key, self.generation, timestamp, reason,
                known=known, evidence=evidence)
            self.boundaries.append(self.end_boundary)
        return self.end_boundary

    def add_candidate(self, candidate):
        self.candidates.append(candidate)
        return candidate

    def to_dict(self):
        return {
            'key': tuple(self.key),
            'generation': self.generation,
            'index': self.index,
            'label': self.label,
            'state': self.state,
            'client': self.client,
            'server': self.server,
            'client_isn': self.client_isn,
            'server_isn': self.server_isn,
            'simultaneous_open': self.simultaneous_open,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'start_known': self.start_known,
            'end_known': self.end_known,
            'start_reason': self.start_boundary.reason,
            'end_reason': self.end_boundary.reason if self.end_boundary else None,
            'packet_count': self.packet_count,
            'retransmissions': self.retransmission_count,
            'closed': self.closed,
            'reset': self.reset,
        }

    def __repr__(self):
        return ('Session(%s, state=%r, start_known=%r, end_known=%r, '
                'packets=%d)' % (self.label, self.state, self.start_known,
                                 self.end_known, self.packet_count))


class SessionTable(object):
    """Track TCP connection generations for offline packets.

    Args:
        timeout: Idle seconds after which a *candidate* boundary may be
            proposed.  ``None`` or a non-positive value disables timeout
            candidates.  A timeout never changes the confirmed generation.
        store_packets: Keep normalized packet records for traceability.
    """

    def __init__(self, timeout=300.0, store_packets=True):
        self.timeout = timeout
        self.store_packets = bool(store_packets)
        self._sessions = {}
        self._all = []
        self._candidates = []
        self._implicit_timestamp = 0.0
        self.finalized = False

    # -- collection protocol ---------------------------------------------
    def __len__(self):
        return len(self._all)

    def __iter__(self):
        return iter(self._all)

    def __getitem__(self, item):
        if isinstance(item, slice):
            return self._all[item]
        if isinstance(item, int):
            return self._all[item]
        return self.get_sessions(item)

    def __contains__(self, key):
        try:
            return canonical_key(key) in self._sessions
        except (TypeError, ValueError):
            return False

    @property
    def sessions(self):
        """All materialized generations in observation order."""
        return list(self._all)

    @property
    def generations(self):
        return list(self._all)

    @property
    def candidates(self):
        """All timeout proposals, including rejected ones."""
        return list(self._candidates)

    @property
    def pending_candidates(self):
        return [c for c in self._candidates if c.status == CANDIDATE_PENDING]

    @property
    def unresolved_candidates(self):
        return [c for c in self._candidates if c.status in (CANDIDATE_PENDING, CANDIDATE_UNRESOLVED)]

    def all_sessions(self):
        return list(self._all)

    def keys(self):
        return list(self._sessions.keys())

    def get(self, key, default=None):
        sessions = self.get_sessions(key)
        if not sessions:
            return default
        return sessions[-1]

    def get_sessions(self, key):
        try:
            return list(self._sessions.get(canonical_key(key), ()))
        except (TypeError, ValueError):
            return []

    def sessions_for(self, key):
        return self.get_sessions(key)

    def generations_for(self, key):
        return self.get_sessions(key)

    def generation(self, key, generation):
        for session in self.get_sessions(key):
            if session.generation == generation:
                return session
        raise KeyError((canonical_key(key), generation))

    def boundaries(self, key=None):
        """Return confirmed boundaries in observation order.

        Candidate proposals are intentionally excluded; use
        :meth:`candidate_boundaries` or :attr:`candidates` to audit them.
        """
        result = []
        for session in self._all:
            if key is not None and not session.key.matches(key):
                continue
            result.extend(session.boundaries)
        result.sort(key=lambda boundary: (boundary.timestamp, boundary.generation,
                                          boundary.kind == 'end'))
        return result

    def candidate_boundaries(self, key=None):
        if key is None:
            return self.candidates
        return [c for c in self._candidates if c.key.matches(key)]

    # -- ingestion --------------------------------------------------------
    def _coerce_timestamp(self, timestamp, info):
        if timestamp is None:
            if info is not None:
                value = getattr(info, 'timestamp', None)
                if isinstance(value, (int, float)):
                    return float(value)
                value = getattr(info, 'ts', None)
                if isinstance(value, (int, float)):
                    return float(value)
            self._implicit_timestamp += 1e-6
            return self._implicit_timestamp
        return float(timestamp)

    def add_packet(self, *args, **kwargs):
        """Add a packet and return the session which owns it.

        ``add_packet(packet, timestamp)`` and ``add_packet(timestamp, packet)``
        are both accepted, as is ``add_packet(packet)``.  A normalized
        :class:`PacketInfo` can also be supplied.
        """
        timestamp = kwargs.pop('timestamp', None)
        explicit_key = kwargs.pop('key', None)
        if kwargs:
            raise TypeError('unexpected keyword argument: %s' % sorted(kwargs)[0])
        if len(args) == 1:
            packet = args[0]
        elif len(args) == 2:
            first, second = args
            if isinstance(first, (int, float)):
                timestamp, packet = first, second
            else:
                packet, first_timestamp = first, second
                timestamp = first_timestamp if timestamp is None else timestamp
        else:
            raise TypeError('add_packet expects a packet and optional timestamp')

        if isinstance(packet, PacketInfo):
            info = packet
            if timestamp is not None:
                info.timestamp = float(timestamp)
        else:
            timestamp = self._coerce_timestamp(timestamp, packet)
            info = self._extract(packet, timestamp, explicit_key)
        return self._add_info(info)

    # Useful aliases for callers embedding the table in a pipeline.
    process = add_packet
    process_packet = add_packet
    feed = add_packet
    add = add_packet

    def add_info(self, info, timestamp=None):
        if not isinstance(info, PacketInfo):
            raise TypeError('add_info expects PacketInfo')
        if timestamp is not None:
            info.timestamp = float(timestamp)
        return self._add_info(info)

    def _extract(self, packet, timestamp, explicit_key=None):
        if isinstance(packet, PacketInfo):
            if timestamp is not None:
                packet.timestamp = timestamp
            return packet
        if isinstance(packet, dict):
            return PacketInfo(
                packet.get('packet', packet), timestamp,
                packet.get('src', packet.get('saddr', '0.0.0.0')),
                packet.get('sport', 0),
                packet.get('dst', packet.get('daddr', '0.0.0.0')),
                packet.get('dport', 0),
                packet.get('seq', 0), packet.get('ack', 0),
                packet.get('flags', 0), packet.get('payload_len', 0))
        if isinstance(packet, (tuple, list)) and not isinstance(packet, bytes):
            if len(packet) >= 7:
                payload_len = packet[7] if len(packet) > 7 else 0
                return PacketInfo(packet, timestamp, packet[0], packet[1],
                                  packet[2], packet[3], packet[4], packet[5],
                                  packet[6], payload_len)
            raise TypeError('packet tuple must be (src, sport, dst, dport, seq, ack, flags[, payload_len])')
        if isinstance(packet, (bytes, bytearray)):
            packet = _decode_packet(bytes(packet), explicit_key=explicit_key)
        if packet is None:
            raise ValueError('packet does not contain TCP/IP')

        # Explicit endpoint information is useful for raw TCP objects or
        # synthetic packet-like objects.
        if explicit_key is not None:
            key = canonical_key(explicit_key)
            tcp_pkt = _find_tcp(packet)
            if tcp_pkt is None:
                raise ValueError('packet does not contain TCP')
            # Decide which key endpoint is the source using packet attributes.
            src, sport, dst, dport = _raw_endpoints(packet, key)
            return PacketInfo(packet, timestamp, src, sport, dst, dport,
                              tcp_pkt.seq, tcp_pkt.ack, tcp_pkt.flags,
                              _payload_len(tcp_pkt))

        tcp_pkt = _find_tcp(packet)
        if tcp_pkt is None:
            raise ValueError('packet does not contain TCP')
        ip_pkt = _find_ip(packet)
        if ip_pkt is None:
            raise ValueError('packet does not contain IPv4/IPv6')
        src, dst = _ip_addresses(ip_pkt)
        payload_len = _payload_len(tcp_pkt)
        return PacketInfo(packet, timestamp, src, tcp_pkt.sport, dst,
                          tcp_pkt.dport, tcp_pkt.seq, tcp_pkt.ack,
                          tcp_pkt.flags, payload_len)

    def _add_info(self, info):
        key = info.key
        sessions = self._sessions.setdefault(key, [])
        if not sessions:
            session = self._start_session(info, None)
            return self._observe(session, info, retransmission=False)

        current = sessions[-1]
        gap = None
        if info.timestamp is not None and current.last_timestamp is not None:
            gap = info.timestamp - current.last_timestamp

        # A timeout is evaluated independently of the packet classification.
        # At most one pending proposal exists per generation: repeated idle
        # gaps are the same unresolved question, not new boundaries.
        timeout_candidate = None
        for candidate in current.candidates:
            if candidate.status == CANDIDATE_PENDING:
                timeout_candidate = candidate
                break
        if (timeout_candidate is None and self.timeout is not None and
                self.timeout > 0 and gap is not None and gap > self.timeout and
                not current.closed and not current.superseded):
            timeout_candidate = self._propose_timeout(current, info, gap)

        if info.syn_only:
            decision = self._syn_decision(current, info, gap)
            if decision == 'same':
                if timeout_candidate is not None:
                    timeout_candidate.resolve(
                        CANDIDATE_REJECTED, reason='retransmitted_handshake',
                        evidence={'syn': info.as_dict()})
                return self._observe(current, info,
                                     retransmission=current._is_retransmission(info))
            if decision == 'simultaneous':
                if timeout_candidate is not None:
                    timeout_candidate.resolve(
                        CANDIDATE_REJECTED, reason='simultaneous_open_continues_generation',
                        evidence={'syn': info.as_dict()})
                return self._observe(current, info,
                                     retransmission=current._is_retransmission(info))
            self._supersede(current, info, timeout_candidate)
            session = self._start_session(info, current)
            return self._observe(session, info)

        if info.syn_ack:
            if current._is_retransmission(info):
                if timeout_candidate is not None:
                    timeout_candidate.resolve(
                        CANDIDATE_REJECTED, reason='retransmitted_handshake',
                        evidence={'syn_ack': info.as_dict()})
                return self._observe(current, info, retransmission=True)
            decision = self._syn_ack_decision(current, info)
            if decision == 'same':
                if timeout_candidate is not None:
                    timeout_candidate.resolve(
                        CANDIDATE_REJECTED, reason='retransmitted_handshake',
                        evidence={'syn_ack': info.as_dict()})
                return self._observe(current, info)
            self._supersede(current, info, timeout_candidate)
            session = self._start_session(info, current)
            return self._observe(session, info)

        if current._is_retransmission(info):
            if timeout_candidate is not None:
                timeout_candidate.resolve(
                    CANDIDATE_REJECTED, reason='retransmission_continues_old_generation',
                    evidence={'retransmission': info.as_dict()})
            return self._observe(current, info, retransmission=True)

        continuity, detail = current._continuity(info)
        if timeout_candidate is not None and not current.closed:
            if continuity == 'same':
                timeout_candidate.resolve(CANDIDATE_REJECTED,
                                          reason='liveness_sequence_continuity',
                                          evidence=detail)
                return self._observe(current, info)
            # A non-SYN packet with a sequence discontinuity is not enough to
            # confirm a generation; retain it in the candidate.
            timeout_candidate.add_packet(info)
            current.anomalies.append({
                'timestamp': info.timestamp,
                'reason': 'pending_timeout_candidate',
                'evidence': detail,
                'packet': info.as_dict(),
            })
            return current

        if current.closed:
            # After an observed close, revisit whether this is merely the final
            # ACK belonging to the closed generation.
            if info.ack_only:
                reverse = current._directions.get(info.dst_endpoint)
                if reverse is not None and reverse.get('first_seq') is not None:
                    ack_offset = _seq_delta(info.ack, reverse['first_seq'])
                    if -SEQ_CONTINUITY_WINDOW <= ack_offset <= reverse['max_end'] + SEQ_CONTINUITY_WINDOW:
                        return self._observe(current, info)
            # Any other packet after an observed close belongs to a later
            # connection.  Its SYN was not captured, so the new start boundary
            # remains unknown.
            reason = START_AFTER_RESET if current.reset else START_AFTER_CLOSE
            session = self._start_session(info, current, reason=reason,
                                          known=False)
            return self._observe(session, info)

        if current.superseded:
            session = self._start_session(info, current, reason=START_MIDSTREAM,
                                          known=False)
            return self._observe(session, info)

        # An open, non-expired generation normally owns all further packets.
        # Sequence discontinuities without a handshake or closure are recorded
        # as anomalies, not as new connections.
        if continuity == 'different':
            current.anomalies.append({
                'timestamp': info.timestamp,
                'reason': detail.get('reason', 'sequence_discontinuity'),
                'evidence': detail,
                'packet': info.as_dict(),
            })
        return self._observe(current, info)

    def _observe(self, session, info, retransmission=False):
        if not self.store_packets:
            info.packet = None
        session.observe(info, retransmission=retransmission,
                        store=self.store_packets)
        return session

    def _start_session(self, info, previous, reason=None, known=None):
        key = info.key
        if reason is None:
            if info.syn_only:
                reason = START_SYN
                known = True
            elif info.syn_ack:
                # A capture beginning with SYN+ACK saw the server's response
                # but not the connection's initial SYN.
                reason = START_SYN_ACK
                known = False
            else:
                reason = START_MIDSTREAM
                known = False
        if known is None:
            known = reason in (START_SYN, START_NEW_ISN, START_SIMULTANEOUS_OPEN)
        generation = len(self._sessions.get(key, ())) + 1
        evidence = info.as_dict()
        evidence.update({
            'reason': reason,
            'previous_generation': previous.generation if previous else None,
        })
        boundary = Boundary('start', key, generation, info.timestamp, reason,
                            known=known, evidence=evidence)
        session = Session(key, generation, boundary)
        self._sessions.setdefault(key, []).append(session)
        self._all.append(session)
        session._set_client_server(info)
        return session

    def _syn_decision(self, current, info, gap):
        current_direction = current._directions.get(info.src_endpoint)
        known_isn = current_direction.get('syn_seq') if current_direction else None

        # A closure is explicit evidence that the old generation ended.  A SYN
        # arriving after that boundary starts a new generation even if the
        # random ISN happens to repeat.
        if (current.closed and current.end_boundary is not None and
                info.timestamp >= current.end_boundary.timestamp):
            return 'new'

        same_isn = known_isn is not None and known_isn == info.seq
        if same_isn:
            # A byte-for-byte SYN retransmission is old-generation evidence
            # unless a new handshake was already observed.  After an idle gap
            # the timeout only proposes a candidate; the SYN itself is still
            # ambiguous, so keep it with the live generation.
            return 'same'

        # Simultaneous open: two bare SYNs from opposite sides, before either
        # side has sent a SYN+ACK, are one connection.
        if (current.saw_syn and not current.saw_syn_ack and
                current.client is not None and
                info.src_endpoint != current.client and
                not current.simultaneous_open):
            return 'simultaneous'
        if current.simultaneous_open and not current.saw_syn_ack:
            if info.src_endpoint == current.server or info.src_endpoint == current.client:
                return 'simultaneous'

        # A repeated SYN in the same direction with a different ISN is a new
        # connection attempt, even if the previous attempt never completed its
        # handshake.
        if current_direction is not None and current_direction.get('syn_seq') is not None:
            if current_direction['syn_seq'] != info.seq:
                return 'new'

        # A handshake captured for a generation whose beginning was unknown
        # means the earlier midstream generation is over.
        if current.start_unknown:
            return 'new'

        # A fresh SYN for a generation whose handshake is still in progress
        # starts the next generation.
        if current.superseded:
            return 'new'
        if current.state in (STATE_SYN_SENT, STATE_SIMULTANEOUS_OPEN):
            return 'new'

        # Reaching this point means no ISN for this direction was observed.  A
        # SYN is positive handshake evidence, so it starts a new generation.
        return 'new'

    def _syn_ack_decision(self, current, info):
        # A SYN+ACK matching the current handshake is part of this generation.
        if current.client_isn is not None:
            if info.src_endpoint == current.server and info.ack == ((current.client_isn + 1) & 0xffffffff):
                state = current._directions.get(info.src_endpoint)
                if state is None or state.get('syn_ack_seq') in (None, info.seq):
                    return 'same'
            if current.simultaneous_open:
                return 'same'
        state = current._directions.get(info.src_endpoint)
        if state is not None and state.get('syn_ack_seq') == info.seq:
            return 'same'
        if current.closed or current.superseded:
            return 'new'
        if current.state == STATE_SYN_SENT:
            # A SYN+ACK with a different server ISN means a new connection.
            return 'new'
        # A midstream generation followed by a SYN+ACK also means a new
        # connection whose handshake is now visible.
        return 'new'

    def _supersede(self, current, info, timeout_candidate=None):
        current.superseded = True
        if current.end_boundary is None:
            evidence = info.as_dict()
            evidence.update({
                'previous_generation': current.generation,
                'new_generation': current.generation + 1,
            })
            current.end_boundary = Boundary(
                'end', current.key, current.generation, info.timestamp,
                END_SUPERSEDED, known=True, evidence=evidence)
            current.boundaries.append(current.end_boundary)
        current.state = STATE_SUPERSEDED if not current.closed else current.state
        if timeout_candidate is not None:
            timeout_candidate.resolve(CANDIDATE_SUPERSEDED,
                                      reason='explicit_handshake_boundary',
                                      evidence={'timestamp': info.timestamp})

    def _propose_timeout(self, current, info, gap):
        for candidate in current.candidates:
            if candidate.status == CANDIDATE_PENDING:
                return candidate
        evidence = {
            'gap': gap,
            'timeout': self.timeout,
            'last_timestamp': current.last_timestamp,
            'next_timestamp': info.timestamp,
            'last_generation': current.generation,
            'next_packet': info.as_dict(),
        }
        candidate = GenerationCandidate(
            current.key, current.generation, info.timestamp, gap, evidence)
        current.add_candidate(candidate)
        self._candidates.append(candidate)
        return candidate

    # -- finishing --------------------------------------------------------
    def finalize(self, timestamp=None):
        """Mark unclosed generations and pending candidates as unresolved.

        This does not invent a closure.  It records that the capture ended
        while the generation was still open, which is useful when auditing a
        session table.
        """
        if timestamp is None:
            if self._all:
                timestamp = max((s.last_timestamp or 0.0) for s in self._all)
            else:
                timestamp = 0.0
        for session in self._all:
            if session.end_boundary is None:
                session.close(timestamp, reason=END_CAPTURE, known=False,
                              evidence={'reason': 'capture_ended_while_open'})
        for candidate in self._candidates:
            if candidate.status == CANDIDATE_PENDING:
                candidate.resolve(CANDIDATE_UNRESOLVED,
                                  reason='capture_ended_without_resolving_evidence')
        self.finalized = True
        return self

    close = finalize

    def summary(self):
        return {
            'sessions': len(self._all),
            'keys': len(self._sessions),
            'candidates': len(self._candidates),
            'pending_candidates': len(self.pending_candidates),
            'boundaries': len(self.boundaries()),
            'retransmissions': sum(s.retransmission_count for s in self._all),
        }

    def to_dict(self):
        return {
            'timeout': self.timeout,
            'summary': self.summary(),
            'sessions': [s.to_dict() for s in self._all],
            'candidates': [c.as_dict() for c in self._candidates],
        }


SessionTracker = SessionTable


# ---------------------------------------------------------------------------
# Packet extraction helpers
# ---------------------------------------------------------------------------

def _find_tcp(packet):
    seen = set()
    current = packet
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, tcp.TCP):
            return current
        found = getattr(current, 'tcp', None)
        if isinstance(found, tcp.TCP):
            return found
        if isinstance(current, (ip.IP, ip6.IP6)):
            child = getattr(current, 'data', None)
            if isinstance(child, tcp.TCP):
                return child
            current = child
            continue
        if isinstance(current, ethernet.Ethernet):
            current = getattr(current, 'data', None)
            continue
        child = getattr(current, 'data', None)
        if isinstance(child, (bytes, bytearray)):
            return None
        current = child
    return None


def _find_ip(packet):
    seen = set()
    current = packet
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ip.IP, ip6.IP6)):
            return current
        found = getattr(current, 'ip', None)
        if isinstance(found, (ip.IP, ip6.IP6)):
            return found
        found = getattr(current, 'ip6', None)
        if isinstance(found, ip6.IP6):
            return found
        current = getattr(current, 'data', None)
        if isinstance(current, (bytes, bytearray)):
            return None
    return None


def _ip_addresses(ip_packet):
    if isinstance(ip_packet, ip.IP):
        return ip_packet.src, ip_packet.dst
    if isinstance(ip_packet, ip6.IP6):
        return ip_packet.src, ip_packet.dst
    return ip_packet.src, ip_packet.dst


def _payload_len(tcp_packet):
    data = getattr(tcp_packet, 'data', b'')
    try:
        return len(data)
    except TypeError:
        return len(bytes(data))


def _raw_endpoints(packet, key):
    """Choose endpoint orientation for an explicitly supplied key."""
    ip_packet = _find_ip(packet)
    if ip_packet is not None:
        return ip_packet.src, _find_tcp(packet).sport, ip_packet.dst, _find_tcp(packet).dport
    src = getattr(packet, 'src', None)
    dst = getattr(packet, 'dst', None)
    tcp_packet = _find_tcp(packet)
    if src is not None and dst is not None:
        return src, tcp_packet.sport, dst, tcp_packet.dport
    # Fall back to the key's own orientation.
    return key[0], key[1], key[2], key[3]


def _decode_packet(buf, explicit_key=None, linktype=None):
    """Try to decode a raw capture frame.

    Ethernet is the common offline capture link type.  Raw IP and a few other
    common link types are accepted as a convenience; pcap reading uses the
    datalink type when it is available.
    """
    candidates = []
    if linktype is not None:
        if linktype == pcap.DLT_EN10MB:
            candidates = [ethernet.Ethernet]
        elif linktype in (pcap.DLT_RAW, pcap.DLT_IPV4):
            candidates = [ip.IP]
        elif linktype == pcap.DLT_IPV6:
            candidates = [ip6.IP6]
        elif linktype == pcap.DLT_LINUX_SLL:
            candidates = [ethernet.Ethernet]  # best effort, see below
        elif linktype == pcap.DLT_NULL or linktype == pcap.DLT_LOOP:
            candidates = [ip.IP, ip6.IP6]
    else:
        candidates = [ethernet.Ethernet, ip.IP, ip6.IP6]
    for cls in candidates:
        try:
            packet = cls(buf)
            if _find_tcp(packet) is not None:
                return packet
        except (dpkt.UnpackError, dpkt.NeedData, ValueError, IndexError):
            continue
    # Headerless synthetic TCP is only usable with an explicit key.
    if explicit_key is not None:
        try:
            return tcp.TCP(buf)
        except (dpkt.UnpackError, dpkt.NeedData, ValueError):
            return None
    return None


def _read_frames(source):
    close = False
    if isinstance(source, string_types):
        source = open(source, 'rb')
        close = True
    try:
        if hasattr(source, 'read') and not hasattr(source, '__iter__'):
            reader = pcap.UniversalReader(source)
        elif hasattr(source, 'read'):
            try:
                reader = pcap.UniversalReader(source)
            except (ValueError, AttributeError):
                reader = source
        else:
            reader = source
        linktype = None
        if hasattr(reader, 'datalink'):
            try:
                linktype = reader.datalink()
            except Exception:  # pragma: no cover - defensive for custom readers
                linktype = None
        for item in reader:
            if len(item) == 2:
                ts, frame = item
            else:  # pragma: no cover - unusual custom readers
                ts, frame = item[0], item[1]
            yield ts, frame, linktype
    finally:
        if close:
            source.close()


def read_sessions(source, timeout=300.0, store_packets=True, linktype=None):
    """Read a pcap/pcapng file (or path) into a :class:`SessionTable`.

    ``source`` may be a path, a binary file object, or an iterable of
    ``(timestamp, packet)`` pairs.  Packets may already be decoded dpkt
    objects.  The returned table is finalized with unknown end boundaries for
    connections which were still open at EOF.
    """
    table = SessionTable(timeout=timeout, store_packets=store_packets)
    last_timestamp = 0.0
    for ts, value, datalink in _read_frames(source):
        last_timestamp = ts
        if isinstance(value, (bytes, bytearray)):
            packet = _decode_packet(bytes(value), linktype=linktype if linktype is not None else datalink)
            if packet is None:
                continue
        else:
            packet = value
        try:
            table.add_packet(packet, ts)
        except ValueError:
            continue
    table.finalize(last_timestamp)
    return table


def track_sessions(source, timeout=300.0, **kwargs):
    return read_sessions(source, timeout=timeout, **kwargs)


def sessions_from_pcap(source, timeout=300.0, **kwargs):
    return read_sessions(source, timeout=timeout, **kwargs)


def sessionize(packets, timeout=300.0, **kwargs):
    """Build a session table from an iterable of packets or (ts, packet)."""
    return read_sessions(packets, timeout=timeout, **kwargs)
