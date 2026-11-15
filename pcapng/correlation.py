"""Correlate packet observations from two captures.

The two captures are called *first* and *second*.  Their clocks are related
by an affine model::

    first_time = slope * second_time + offset

The model is estimated from synchronization anchors, which are pairs of
timestamps referring to the same event.  A candidate correspondence is kept
only when the clock-adjusted timestamps differ by an amount inside the
allowed propagation window and when the captured payloads are compatible.

Payload compatibility compares only the bytes that both observations actually
captured.  Captures may use different snapshot lengths, so one side may expose
only a prefix of the other side's bytes.  The captured ranges of both
observations are preserved in the result; they are never extended or trimmed
to make a match.

Correspondence is a one-to-one relation.  Two real transmissions carrying the
same bytes stay two distinct observations: payload equality is never used to
merge or de-duplicate observations.  When the candidate graph admits more than
one maximum matching, no arbitrary choice is made; the viable candidates are
returned as a :class:`CandidateGroup`.  File order only gives results a stable
presentation order; it is never used to choose a correspondence.
"""

import bisect
import math
import numbers
import os

from pcapng.blocks import Block as _Block


__all__ = [
    "Candidate",
    "CandidateGroup",
    "ClockAnchor",
    "ClockModel",
    "Correlation",
    "CorrelationResult",
    "Match",
    "PacketMatch",
    "PacketObservation",
    "SyncAnchor",
    "correlate",
    "correlate_captures",
    "correlate_packets",
    "match_captures",
    "match_observations",
    "match_packets",
    "observations_from_blocks",
    "read_observations",
]


if hasattr(os, "PathLike"):  # Python 3.6+
    _PATH_TYPES = (str, bytes, os.PathLike)
else:  # pragma: no cover - Python 3.5
    _PATH_TYPES = (str, bytes)


def _is_real(value):
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _as_timestamp(value):
    """Return a finite float timestamp from a number or an observation."""
    if hasattr(value, "timestamp"):
        value = value.timestamp
    if not _is_real(value):
        raise TypeError("timestamp must be a real number, got {0!r}".format(value))
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("timestamp must be finite, got {0!r}".format(value))
    return result


def _as_length(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError("{0} must be an integer, got {1!r}".format(name, value))
    value = int(value)
    if value < 0:
        raise ValueError("{0} must not be negative".format(name))
    return value


def _as_window(window):
    """Normalize a scalar or an interval to ``(earliest, latest)``."""
    if _is_real(window):
        width = float(window)
        if not math.isfinite(width) or width < 0:
            raise ValueError("window must be finite and non-negative")
        return (-width, width)

    try:
        earliest, latest = window
    except (TypeError, ValueError):
        raise TypeError("window must be a number or a (earliest, latest) pair")

    if not _is_real(earliest) or not _is_real(latest):
        raise TypeError("window bounds must be real numbers")
    earliest = float(earliest)
    latest = float(latest)
    if not math.isfinite(earliest) or not math.isfinite(latest):
        raise ValueError("window bounds must be finite")
    if earliest > latest:
        raise ValueError("window earliest bound must not exceed latest bound")
    return (earliest, latest)


def _anchor_timestamps(anchor):
    if isinstance(anchor, ClockAnchor):
        return anchor.first_timestamp, anchor.second_timestamp
    if hasattr(anchor, "first_timestamp") and hasattr(anchor, "second_timestamp"):
        return (
            _as_timestamp(anchor.first_timestamp),
            _as_timestamp(anchor.second_timestamp),
        )
    try:
        first, second = anchor
    except (TypeError, ValueError):
        raise TypeError(
            "anchor must be a (first_timestamp, second_timestamp) pair"
        )
    return _as_timestamp(first), _as_timestamp(second)


def _coerce_anchors(anchors):
    if anchors is None:
        return []
    if isinstance(anchors, ClockAnchor):
        return [anchors]
    if hasattr(anchors, "first_timestamp") and hasattr(
        anchors, "second_timestamp"
    ):
        return [anchors]

    try:
        anchors = list(anchors)
    except TypeError:
        raise TypeError("anchors must be an iterable of anchor pairs")

    return anchors


class ClockAnchor(object):
    """A pair of timestamps known to refer to the same event.

    The first timestamp is on the first capture's clock and the second one is
    on the second capture's clock.
    """

    __slots__ = ("first_timestamp", "second_timestamp")

    def __init__(self, first_timestamp, second_timestamp):
        self.first_timestamp = _as_timestamp(first_timestamp)
        self.second_timestamp = _as_timestamp(second_timestamp)

    def __iter__(self):
        yield self.first_timestamp
        yield self.second_timestamp

    def __eq__(self, other):
        return (
            isinstance(other, ClockAnchor)
            and self.first_timestamp == other.first_timestamp
            and self.second_timestamp == other.second_timestamp
        )

    def __repr__(self):
        return "ClockAnchor({0!r}, {1!r})".format(
            self.first_timestamp, self.second_timestamp
        )


SyncAnchor = ClockAnchor


class ClockModel(object):
    """Affine map from the second capture's clock to the first capture's clock."""

    __slots__ = ("slope", "offset")

    def __init__(self, slope=1.0, offset=0.0):
        slope = float(slope)
        offset = float(offset)
        if not math.isfinite(slope) or not math.isfinite(offset):
            raise ValueError("clock parameters must be finite")
        if slope <= 0:
            raise ValueError("clock slope must be positive")
        self.slope = slope
        self.offset = offset

    @property
    def skew(self):
        """Alias for :attr:`slope`."""
        return self.slope

    @classmethod
    def from_anchors(cls, anchors):
        """Estimate a clock model from at least one anchor pair.

        A least-squares line is used when the anchors have more than one
        distinct second-clock timestamp.  One anchor, or several anchors all
        sharing the same second-clock timestamp, can only determine an offset;
        the slope then defaults to 1.
        """
        points = [_anchor_timestamps(anchor) for anchor in _coerce_anchors(anchors)]
        if not points:
            raise ValueError("at least one synchronization anchor is required")

        count = len(points)
        mean_first = sum(point[0] for point in points) / count
        mean_second = sum(point[1] for point in points) / count

        denominator = sum((point[1] - mean_second) ** 2 for point in points)
        if denominator == 0:
            return cls(1.0, mean_first - mean_second)

        numerator = sum(
            (point[1] - mean_second) * (point[0] - mean_first) for point in points
        )
        slope = numerator / denominator
        if slope <= 0:
            raise ValueError(
                "synchronization anchors imply a non-positive clock slope"
            )
        return cls(slope, mean_first - slope * mean_second)

    fit = from_anchors

    def to_first(self, second_timestamp):
        """Map a timestamp from the second clock to the first clock."""
        return self.slope * _as_timestamp(second_timestamp) + self.offset

    def to_second(self, first_timestamp):
        """Map a timestamp from the first clock to the second clock."""
        return (_as_timestamp(first_timestamp) - self.offset) / self.slope

    def delay(self, first_timestamp, second_timestamp):
        """Return ``first - to_first(second)`` for two timestamps."""
        return _as_timestamp(first_timestamp) - self.to_first(second_timestamp)

    def __eq__(self, other):
        return (
            isinstance(other, ClockModel)
            and self.slope == other.slope
            and self.offset == other.offset
        )

    def __repr__(self):
        return "ClockModel(slope={0!r}, offset={1!r})".format(
            self.slope, self.offset
        )


class PacketObservation(object):
    """One captured packet observation.

    :param timestamp: timestamp on this capture's own clock
    :param packet_data: bytes actually captured
    :param packet_len: original on-wire packet length, if known
    :param captured_len: number of bytes actually captured; defaults to
        ``len(packet_data)``.  If it is smaller than ``packet_data``, the
        extra bytes are ignored.
    :param index: caller-provided identifier, used only for reporting
    :param interface_id: capture interface identifier, if known
    :param link_type: link-layer type, if known

    ``captured_range`` is always ``(0, captured_len)``: it describes the range
    actually received by this capture.  Bytes not captured are not invented.
    """

    __slots__ = (
        "timestamp",
        "packet_data",
        "captured_len",
        "packet_len",
        "index",
        "interface_id",
        "link_type",
    )

    def __init__(
        self,
        timestamp,
        packet_data,
        packet_len=None,
        captured_len=None,
        index=None,
        interface_id=None,
        link_type=None,
    ):
        self.timestamp = _as_timestamp(timestamp)
        try:
            data = bytes(packet_data)
        except TypeError:
            raise TypeError("packet_data must be bytes-like")

        captured_len = _as_length(captured_len, "captured_len")
        if captured_len is None:
            captured_len = len(data)
        elif captured_len > len(data):
            raise ValueError(
                "captured_len {0} exceeds available packet_data length {1}".format(
                    captured_len, len(data)
                )
            )

        self.packet_data = data[:captured_len]
        self.captured_len = captured_len
        self.packet_len = _as_length(packet_len, "packet_len")
        self.index = index
        self.interface_id = interface_id
        self.link_type = link_type

    @classmethod
    def from_block(cls, block, index=None):
        """Build an observation from a pcap-ng packet block."""
        try:
            timestamp = block.timestamp
            packet_data = block.packet_data
        except (AttributeError, KeyError):
            raise TypeError("block does not look like a packet block")

        try:
            packet_len = block.packet_len
        except (AttributeError, KeyError):
            packet_len = None
        try:
            captured_len = block.captured_len
        except (AttributeError, KeyError):
            captured_len = None
        try:
            interface_id = block.interface_id
        except (AttributeError, KeyError):
            interface_id = None
        try:
            link_type = block.interface.link_type
        except (AttributeError, KeyError):
            link_type = None
        return cls(
            timestamp=timestamp,
            packet_data=packet_data,
            packet_len=packet_len,
            captured_len=captured_len,
            index=index,
            interface_id=interface_id,
            link_type=link_type,
        )

    @property
    def captured_range(self):
        """The byte range actually received by this capture."""
        return (0, self.captured_len)

    observed_range = captured_range
    received_range = captured_range

    @property
    def wire_range(self):
        """The original on-wire byte range, when the packet length is known."""
        if self.packet_len is None:
            return None
        return (0, self.packet_len)

    @property
    def payload(self):
        return self.packet_data

    @property
    def wire_len(self):
        return self.packet_len

    @property
    def is_truncated(self):
        return self.packet_len is not None and self.captured_len < self.packet_len

    truncated = is_truncated

    def _with_index(self, index):
        return PacketObservation(
            timestamp=self.timestamp,
            packet_data=self.packet_data,
            packet_len=self.packet_len,
            captured_len=self.captured_len,
            index=index,
            interface_id=self.interface_id,
            link_type=self.link_type,
        )

    def __eq__(self, other):
        return (
            isinstance(other, PacketObservation)
            and self.timestamp == other.timestamp
            and self.packet_data == other.packet_data
            and self.captured_len == other.captured_len
            and self.packet_len == other.packet_len
            and self.index == other.index
            and self.interface_id == other.interface_id
            and self.link_type == other.link_type
        )

    __hash__ = None

    def __repr__(self):
        return (
            "PacketObservation(timestamp={0!r}, captured_len={1!r}, "
            "packet_len={2!r}, index={3!r})"
        ).format(
            self.timestamp,
            self.captured_len,
            self.packet_len,
            self.index,
        )


def _is_packet_block(value):
    try:
        return hasattr(value, "packet_data") and hasattr(value, "timestamp")
    except (AttributeError, KeyError):
        return False


def _is_plain_block(value):
    return isinstance(value, _Block)


def _coerce_observation(value, index):
    if isinstance(value, PacketObservation):
        if value.index is None:
            return value._with_index(index)
        return value

    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            timestamp, packet_data = value
            packet_len = None
        elif len(value) == 3:
            timestamp, packet_data, packet_len = value
        else:
            raise TypeError("observation tuples must contain 2 or 3 values")
        return PacketObservation(
            timestamp=timestamp,
            packet_data=packet_data,
            packet_len=packet_len,
            index=index,
        )

    if _is_packet_block(value):
        return PacketObservation.from_block(value, index=index)

    raise TypeError(
        "observation must be a PacketObservation, a pcap-ng packet block, "
        "or a (timestamp, data[, packet_len]) tuple"
    )


def _coerce_observations(values):
    observations = []
    for index, value in enumerate(values):
        if _is_plain_block(value) and not _is_packet_block(value):
            continue
        observations.append(_coerce_observation(value, index))
    return observations


def observations_from_blocks(blocks):
    """Return observations for packet blocks, skipping other block types."""
    return _coerce_observations(blocks)


def read_observations(source):
    """Read packet observations from a path, stream, scanner, or block iterable."""
    if isinstance(source, _PATH_TYPES):
        from pcapng.scanner import FileScanner

        with open(source, "rb") as stream:
            return _coerce_observations(FileScanner(stream))

    if hasattr(source, "read"):
        from pcapng.scanner import FileScanner

        return _coerce_observations(FileScanner(source))

    try:
        return _coerce_observations(iter(source))
    except TypeError:
        raise TypeError(
            "capture source must be a path, a readable stream, a scanner, "
            "or an iterable of blocks/observations"
        )


def _payloads_compatible(first, second):
    if (
        first.link_type is not None
        and second.link_type is not None
        and first.link_type != second.link_type
    ):
        return False

    compared_len = min(first.captured_len, second.captured_len)
    if first.packet_data[:compared_len] != second.packet_data[:compared_len]:
        return False

    for known, other in ((first, second), (second, first)):
        if known.packet_len is not None:
            if other.captured_len > known.packet_len:
                return False
            if (
                other.packet_len is not None
                and other.packet_len != known.packet_len
            ):
                return False
    return True


class Candidate(object):
    """A possible correspondence between two observations."""

    __slots__ = ("first", "second", "delay")

    def __init__(self, first, second, delay):
        self.first = first
        self.second = second
        self.delay = float(delay)

    @property
    def compared_len(self):
        """Number of payload bytes available on both sides for comparison."""
        return min(self.first.captured_len, self.second.captured_len)

    @property
    def first_range(self):
        return self.first.captured_range

    @property
    def second_range(self):
        return self.second.captured_range

    @property
    def shorter_capture(self):
        """Which side captured fewer bytes: ``"first"``, ``"second"`` or None."""
        if self.first.captured_len < self.second.captured_len:
            return "first"
        if self.second.captured_len < self.first.captured_len:
            return "second"
        return None

    @property
    def truncated_sides(self):
        sides = []
        if self.first.is_truncated:
            sides.append("first")
        if self.second.is_truncated:
            sides.append("second")
        return tuple(sides)

    def __iter__(self):
        yield self.first
        yield self.second

    def __eq__(self, other):
        return (
            isinstance(other, Candidate)
            and self.first == other.first
            and self.second == other.second
            and self.delay == other.delay
        )

    def __repr__(self):
        return "Candidate(first={0!r}, second={1!r}, delay={2!r})".format(
            self.first,
            self.second,
            self.delay,
        )


PacketMatch = Candidate
Match = Candidate


class CandidateGroup(object):
    """An unresolved component of the candidate graph.

    Every edge in :attr:`candidates` can occur in a maximum one-to-one
    matching, but the component does not have a unique maximum matching.  The
    group is returned instead of choosing one edge based on file order.
    """

    __slots__ = ("first", "second", "candidates")

    def __init__(self, first, second, candidates):
        self.first = tuple(first)
        self.second = tuple(second)
        self.candidates = tuple(candidates)

    @property
    def edges(self):
        return self.candidates

    @property
    def possible_pairs(self):
        return self.candidates

    def __iter__(self):
        return iter(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]

    def __len__(self):
        return len(self.candidates)

    def __repr__(self):
        return (
            "CandidateGroup(first={0} observations, second={1} observations, "
            "candidates={2} edges)"
        ).format(len(self.first), len(self.second), len(self.candidates))


class CorrelationResult(object):
    """Resolved pairs, ambiguous candidate groups, and unmatched observations."""

    __slots__ = (
        "clock",
        "window",
        "pairs",
        "candidate_groups",
        "unmatched_first",
        "unmatched_second",
    )

    def __init__(
        self,
        clock,
        window,
        pairs=(),
        candidate_groups=(),
        unmatched_first=(),
        unmatched_second=(),
    ):
        self.clock = clock
        self.window = tuple(window)
        self.pairs = tuple(pairs)
        self.candidate_groups = tuple(candidate_groups)
        self.unmatched_first = tuple(unmatched_first)
        self.unmatched_second = tuple(unmatched_second)

    @property
    def matches(self):
        return self.pairs

    @property
    def matched_pairs(self):
        return self.pairs

    @property
    def resolved(self):
        return self.pairs

    @property
    def candidates(self):
        return self.candidate_groups

    @property
    def ambiguous(self):
        return self.candidate_groups

    @property
    def has_ambiguity(self):
        return bool(self.candidate_groups)

    @property
    def unmatched(self):
        return self.unmatched_first + self.unmatched_second

    def iter_candidates(self):
        for pair in self.pairs:
            yield pair
        for group in self.candidate_groups:
            for pair in group.candidates:
                yield pair

    def __iter__(self):
        return iter(self.pairs)

    def __len__(self):
        return len(self.pairs)

    def __repr__(self):
        return (
            "CorrelationResult(pairs={0}, candidate_groups={1}, "
            "unmatched_first={2}, unmatched_second={3})"
        ).format(
            len(self.pairs),
            len(self.candidate_groups),
            len(self.unmatched_first),
            len(self.unmatched_second),
        )


Correlation = CorrelationResult


def _candidate_edges(first_observations, second_observations, clock, window):
    earliest, latest = window
    by_wire_len = {}
    for second_pos, observation in enumerate(second_observations):
        by_wire_len.setdefault(observation.packet_len, []).append(
            (clock.to_first(observation.timestamp), second_pos, observation)
        )

    prepared = {}
    for wire_len, rows in by_wire_len.items():
        rows.sort(key=lambda row: (row[0], row[1]))
        prepared[wire_len] = ([row[0] for row in rows], rows)

    edges = []
    for first_pos, observation in enumerate(first_observations):
        if observation.packet_len is None:
            keys = list(prepared)
        else:
            keys = [observation.packet_len]
            if None in prepared:
                keys.append(None)

        lower = observation.timestamp - latest
        upper = observation.timestamp - earliest
        for wire_len in keys:
            if wire_len not in prepared:
                continue
            times, rows = prepared[wire_len]
            start = bisect.bisect_left(times, lower)
            stop = bisect.bisect_right(times, upper)
            for mapped_time, second_pos, second_observation in rows[start:stop]:
                if not _payloads_compatible(observation, second_observation):
                    continue
                delay = observation.timestamp - mapped_time
                edges.append(
                    (
                        first_pos,
                        second_pos,
                        Candidate(observation, second_observation, delay),
                    )
                )
    return edges


def _maximum_matching(adjacency, left_nodes):
    """Return a maximum matching as ``{left_position: right_position}``.

    This is an iterative augmenting-path search, so long captures cannot hit
    Python's recursion limit.
    """
    matched_left = {}
    matched_right = {}

    for start in left_nodes:
        if start in matched_left:
            continue

        parent = {}
        visited_left = {start}
        visited_right = set()
        stack = [start]
        free_right = None

        while stack and free_right is None:
            left = stack.pop()
            for right in adjacency.get(left, ()):
                if right in visited_right:
                    continue
                visited_right.add(right)
                parent[right] = left
                owner = matched_right.get(right)
                if owner is None:
                    free_right = right
                    break
                if owner not in visited_left:
                    visited_left.add(owner)
                    stack.append(owner)

        if free_right is None:
            continue

        right = free_right
        while True:
            left = parent[right]
            previous_right = matched_left.get(left)
            matched_left[left] = right
            matched_right[right] = left
            if left == start:
                break
            right = previous_right

    return matched_left


def _components(adjacency, reverse, first_count):
    components = []
    seen_first = set()
    seen_second = set()

    for start in range(first_count):
        if start in seen_first or not adjacency.get(start):
            continue

        stack = [("first", start)]
        component_first = set()
        component_second = set()

        while stack:
            side, position = stack.pop()
            if side == "first":
                if position in component_first:
                    continue
                component_first.add(position)
                for other in adjacency.get(position, ()):
                    if other not in component_second:
                        stack.append(("second", other))
            else:
                if position in component_second:
                    continue
                component_second.add(position)
                for other in reverse.get(position, ()):
                    if other not in component_first:
                        stack.append(("first", other))

        seen_first.update(component_first)
        seen_second.update(component_second)
        components.append((sorted(component_first), sorted(component_second)))

    return components


def _strongly_connected_components(nodes, successors):
    """Return SCCs of a directed graph (iterative Tarjan)."""
    index_of = {}
    lowlink = {}
    on_stack = set()
    stack = []
    components = []
    counter = [0]

    for root in nodes:
        if root in index_of:
            continue

        work = [(root, 0)]
        while work:
            node, child_position = work[-1]

            if child_position == 0:
                index_of[node] = counter[0]
                lowlink[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)

            children = list(successors.get(node, ()))
            if child_position < len(children):
                child = children[child_position]
                work[-1] = (node, child_position + 1)
                if child not in index_of:
                    work.append((child, 0))
                elif child in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[child])
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)

    return components


def _reachable(starts, successors):
    seen = set(starts)
    stack = list(starts)
    while stack:
        node = stack.pop()
        for child in successors.get(node, ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _decompose_matching(adjacency, left_nodes):
    """Return forced pairs and ambiguous components of a bipartite graph.

    This is the Dulmage-Mendelsohn construction.  With a maximum matching M,
    direct unmatched edges left-to-right and matched edges right-to-left.
    Matched edges inside a nontrivial strongly connected component, or
    reachable from a free vertex along an alternating path, are not forced.
    All other matched edges occur in every maximum matching.
    """
    left_nodes = sorted(set(left_nodes))
    matching = _maximum_matching(adjacency, left_nodes)

    successors = {}
    predecessors = {}

    def add_edge(source, target):
        successors.setdefault(source, []).append(target)
        predecessors.setdefault(target, []).append(source)

    for left in left_nodes:
        for right in adjacency.get(left, ()):
            if matching.get(left) == right:
                add_edge(("R", right), ("L", left))
            else:
                add_edge(("L", left), ("R", right))

    nodes = set(successors)
    for targets in successors.values():
        nodes.update(targets)

    components = _strongly_connected_components(sorted(nodes), successors)
    component_of = {}
    for position, component in enumerate(components):
        for node in component:
            component_of[node] = position

    free_lefts = [("L", left) for left in left_nodes if left not in matching]
    matched_rights = set(matching.values())
    free_rights = [
        ("R", right)
        for right in {r for left in left_nodes for r in adjacency.get(left, ())}
        - matched_rights
    ]

    from_free_left = _reachable(free_lefts, successors)
    to_free_right = _reachable(free_rights, predecessors)

    forced_pairs = []
    ambiguous_edges = set()
    for left in left_nodes:
        for right in adjacency.get(left, ()):
            left_node = ("L", left)
            right_node = ("R", right)
            if matching.get(left) == right:
                if (
                    right_node not in from_free_left
                    and left_node not in to_free_right
                    and component_of.get(left_node) != component_of.get(right_node)
                ):
                    forced_pairs.append((left, right))
                else:
                    ambiguous_edges.add((left, right))
                continue
            if (
                component_of.get(left_node) == component_of.get(right_node)
                or left_node in from_free_left
                or right_node in to_free_right
            ):
                ambiguous_edges.add((left, right))

    ambiguous_edges = sorted(ambiguous_edges)
    ambiguous_adjacency = {}
    for left, right in ambiguous_edges:
        ambiguous_adjacency.setdefault(left, []).append(right)
    ambiguous_reverse = {}
    for left, right in ambiguous_edges:
        ambiguous_reverse.setdefault(right, []).append(left)

    ambiguous = []
    for group_first, group_second in _components(
        ambiguous_adjacency, ambiguous_reverse, len(adjacency)
    ):
        edges = [
            (left, right)
            for left in group_first
            for right in ambiguous_adjacency.get(left, ())
            if right in group_second
        ]
        ambiguous.append((group_first, group_second, edges))

    return matching, forced_pairs, ambiguous


def _match_candidates(candidate_edges, first_observations, second_observations):
    adjacency = {position: [] for position in range(len(first_observations))}
    reverse = {}
    candidates_by_edge = {}

    for first_pos, second_pos, candidate in candidate_edges:
        adjacency[first_pos].append(second_pos)
        reverse.setdefault(second_pos, []).append(first_pos)
        candidates_by_edge[(first_pos, second_pos)] = candidate

    for edges in adjacency.values():
        edges.sort()
    for edges in reverse.values():
        edges.sort()

    resolved = []
    ambiguous = []
    matched_first = set()
    matched_second = set()

    for component_first, component_second in _components(
        adjacency, reverse, len(first_observations)
    ):
        _, forced, component_ambiguous = _decompose_matching(
            adjacency, component_first
        )

        for left, right in forced:
            resolved.append((left, right, candidates_by_edge[(left, right)]))
            matched_first.add(left)
            matched_second.add(right)

        for group_first, group_second, edges in component_ambiguous:
            group_candidates = [
                (left, right, candidates_by_edge[(left, right)])
                for left, right in edges
            ]
            group_candidates.sort(key=lambda edge: (edge[0], edge[1]))
            ambiguous.append((group_first, group_second, group_candidates))
            matched_first.update(group_first)
            matched_second.update(group_second)

    resolved.sort(key=lambda edge: (edge[0], edge[1]))
    ambiguous.sort(key=lambda group: (group[0][0], group[1][0]))

    unmatched_first = [
        first_observations[position]
        for position in range(len(first_observations))
        if position not in matched_first
    ]
    unmatched_second = [
        second_observations[position]
        for position in range(len(second_observations))
        if position not in matched_second
    ]

    pairs = [candidate for _, _, candidate in resolved]
    groups = [
        CandidateGroup(
            [first_observations[position] for position in group_first],
            [second_observations[position] for position in group_second],
            [candidate for _, _, candidate in group_candidates],
        )
        for group_first, group_second, group_candidates in ambiguous
    ]
    return pairs, groups, unmatched_first, unmatched_second


def correlate(
    first,
    second,
    anchors=None,
    window=None,
    clock=None,
    propagation_window=None,
):
    """Correlate observations from two captures.

    :param first: observations from the first capture.  Each item may be a
        :class:`PacketObservation`, a pcap-ng packet block, or a tuple of
        ``(timestamp, packet_data[, packet_len])``.  Non-packet pcap-ng blocks
        are ignored.
    :param second: observations from the second capture, in the same accepted
        forms as ``first``.
    :param anchors: synchronization anchor pairs, each a
        ``(first_timestamp, second_timestamp)`` pair, a
        :class:`ClockAnchor`, or an object exposing those two attributes.  An
        anchor pair may also contain observations instead of timestamps.
        Anchors are not required when ``clock`` is supplied.
    :param window: allowed propagation delay, either a non-negative scalar
        ``w`` meaning ``[-w, +w]`` or an explicit ``(earliest, latest)``
        interval.  The delay is ``first_timestamp - clock.to_first(
        second_timestamp)``.  ``propagation_window`` is an alias.
    :param clock: an explicit :class:`ClockModel`, or a ``(slope, offset)``
        pair.  When supplied it overrides anchor estimation.
    :return: a :class:`CorrelationResult`

    The returned ``pairs`` are a genuine one-to-one matching.  Any component
    with more than one possible maximum matching is left in
    ``candidate_groups`` instead of being resolved by file order.  The
    observations in each pair and group retain their own ``captured_range``.
    """
    if window is None:
        window = propagation_window
    if window is None:
        raise ValueError("window is required")
    window = _as_window(window)

    if clock is None:
        if anchors is None:
            raise ValueError("anchors or an explicit clock is required")
        clock = ClockModel.from_anchors(anchors)
    elif not isinstance(clock, ClockModel):
        try:
            slope, offset = clock
        except (TypeError, ValueError):
            raise TypeError("clock must be a ClockModel or a (slope, offset) pair")
        clock = ClockModel(slope, offset)

    first_observations = _coerce_observations(first)
    second_observations = _coerce_observations(second)

    if not first_observations or not second_observations:
        return CorrelationResult(
            clock=clock,
            window=window,
            unmatched_first=first_observations,
            unmatched_second=second_observations,
        )

    candidate_edges = _candidate_edges(
        first_observations, second_observations, clock, window
    )
    pairs, groups, unmatched_first, unmatched_second = _match_candidates(
        candidate_edges, first_observations, second_observations
    )
    return CorrelationResult(
        clock=clock,
        window=window,
        pairs=pairs,
        candidate_groups=groups,
        unmatched_first=unmatched_first,
        unmatched_second=unmatched_second,
    )


def correlate_captures(
    first,
    second,
    anchors=None,
    window=None,
    clock=None,
    propagation_window=None,
):
    """Correlate two capture sources (paths, streams, scanners, or blocks)."""
    return correlate(
        read_observations(first),
        read_observations(second),
        anchors=anchors,
        window=window,
        clock=clock,
        propagation_window=propagation_window,
    )


match_observations = correlate
match_packets = correlate
correlate_packets = correlate
match_captures = correlate_captures
