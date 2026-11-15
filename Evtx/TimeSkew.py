#!/usr/bin/python
#    This file is part of python-evtx.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""
Timeline reconstruction across host clock adjustments (NTP recalibration,
manual time changes, and so on).

A clock change makes the timestamp written into a new event older than the
timestamps of events that were physically written earlier.  Sorting purely on
the declared timestamp then places fresh events into an old shift.  This module
keeps three independent facts about every event and lets the caller decide
which interpretation to query:

1. the original recording order (a dense per-file sequence plus the record
   number, which remains a distinct identity even for equal timestamps);
2. the declared time, exactly as stored in the record;
3. the clock-adjustment intervals, each an evidence-bearing half-open window
   ``[start, end)`` in declared time together with the offset that reconstructs
   the normalized time.

Without an adjustment interval covering an event, its time is never rewritten:
no clock-adjustment evidence means no forced correction.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator, Optional

# ordering selectable by the caller
ORDER_RECORDED = "recorded"
ORDER_DECLARED = "declared"
ORDER_NORMALIZED = "normalized"
ORDERS = (ORDER_RECORDED, ORDER_DECLARED, ORDER_NORMALIZED)

# time basis selectable by the caller
TIME_DECLARED = "declared"
TIME_NORMALIZED = "normalized"
TIME_BASES = (TIME_DECLARED, TIME_NORMALIZED)

# skew status of a single event
SKEW_UNKNOWN = "unknown"
SKEW_NONE = "none"
SKEW_ADJUSTED = "adjusted"
SKEW_STATUSES = (SKEW_UNKNOWN, SKEW_NONE, SKEW_ADJUSTED)


@dataclass(frozen=True)
class ClockAdjustment:
    """
    An evidence-bearing clock-adjustment interval.

    The interval is the half-open declared-time window
    ``[start, end)`` exposed by a backward step: ``end`` is the timestamp of
    the last event written before the calibration and ``start`` is the older
    timestamp of the first event written after it.  ``offset`` reconstructs the
    normalized time as ``declared_time + offset``; it is positive when the host
    clock was moved backwards and may be negative for a forward change.

    A timestamp inside the fold is ambiguous on its own, because an event
    written before the calibration can carry the same declared time as an event
    written after it.  ``boundary`` removes the ambiguity: it is the physical
    sequence at which the skewed clock regime begins, so only events written at
    or after the boundary (and whose declared time lies in the interval) are
    rewritten.  Pre-calibration events inside the window keep their declared
    time.  ``evidence`` stores the provenance of the interval and keeps the
    correction auditable.
    """

    start: datetime
    end: datetime
    offset: timedelta
    boundary: int
    evidence: Any = None

    def __post_init__(self):
        if self.start > self.end:
            raise ValueError("ClockAdjustment start ({}) is after end ({})".format(self.start, self.end))
        if not isinstance(self.offset, timedelta):
            raise ValueError("ClockAdjustment offset must be a timedelta")
        if not isinstance(self.boundary, int) or self.boundary < 0:
            raise ValueError("ClockAdjustment boundary must be a non-negative sequence number")

    def contains(self, when: datetime) -> bool:
        """
        half-open time membership: ``start <= when < end``.

        the end boundary is the last timestamp written before the adjustment,
        so it must not itself be rewritten.
        """
        return self.start <= when < self.end

    def covers(self, event: "TimedEvent") -> bool:
        """
        decide whether this adjustment reconstructs the given event.

        the event must have been written at or after the calibration boundary
        and carry a declared timestamp inside the fold interval.
        """
        return event.sequence >= self.boundary and self.contains(event.declared_time)


@dataclass
class TimedEvent:
    """
    One event together with the three facts needed to query it under any basis.

    ``sequence`` is the dense physical recording order (0-based across the
    whole file), ``record_num`` is the EVTX record number and therefore the
    stable identity of the event, ``declared_time`` is the timestamp as stored,
    and ``normalized_time`` is the reconstructed time (identical to the
    declared time until a covering adjustment is applied).
    """

    sequence: int
    record_num: int
    declared_time: datetime
    normalized_time: datetime
    skew_status: str = SKEW_UNKNOWN
    record: Any = None

    @property
    def identity(self) -> tuple[int, int]:
        return self.record_num, self.sequence

    def time(self, time_basis: str = TIME_DECLARED) -> datetime:
        if time_basis == TIME_NORMALIZED:
            return self.normalized_time
        if time_basis == TIME_DECLARED:
            return self.declared_time
        raise ValueError("unknown time basis: {!r}; expected one of {}".format(time_basis, TIME_BASES))


def _event_from_record(record: Any, sequence: int) -> TimedEvent:
    return TimedEvent(
        sequence=sequence,
        record_num=record.record_num(),
        declared_time=record.timestamp(),
        normalized_time=record.timestamp(),
        skew_status=SKEW_UNKNOWN,
        record=record,
    )


def iter_record_events(source: Any) -> Iterator[TimedEvent]:
    """
    Yield every record as a :class:`TimedEvent` in physical recording order.

    Accepts an :class:`Evtx.Evtx.Evtx` instance, a
    :class:`Evtx.Evtx.FileHeader`, or an iterable that yields records or
    :class:`TimedEvent` objects.  No correction is performed: normalized and
    declared times are identical at this stage.
    """
    if hasattr(source, "get_file_header"):
        source = source.get_file_header()

    if hasattr(source, "chunks"):
        sequence = 0
        for chunk in source.chunks():
            for record in chunk.records():
                yield _event_from_record(record, sequence)
                sequence += 1
        return

    for sequence, item in enumerate(source):
        if isinstance(item, TimedEvent):
            yield item
        else:
            yield _event_from_record(item, sequence)


def detect_clock_adjustments(
    events: Iterable[TimedEvent], tolerance: timedelta = timedelta(0)
) -> list[ClockAdjustment]:
    """
    Propose clock-adjustment intervals from observed backward steps.

    A backward step is a pair of physically consecutive events where the later
    event declares an earlier timestamp than the previous one (by more than
    ``tolerance``).  Equal timestamps are not adjustments: the events keep
    their order through the sequence number.

    Each backward step produces a half-open fold interval
    ``[later_time, earlier_time)`` with an offset equal to the backward
    duration, so adding the offset pins the later-written event back to its
    position immediately after the earlier-written one.  These are proposals
    backed by concrete record evidence; callers are free to edit, replace, or
    ignore them.  Nothing is corrected until an interval is supplied to
    :func:`apply_adjustments` or :class:`EventTimeline`.
    """
    events = list(events)
    adjustments: list[ClockAdjustment] = []
    seen: set[tuple[datetime, datetime, timedelta, int]] = set()

    for previous, current in zip(events, events[1:]):
        backward = previous.declared_time - current.declared_time
        if backward <= tolerance:
            continue

        key = (current.declared_time, previous.declared_time, backward, current.sequence)
        if key in seen:
            continue
        seen.add(key)

        evidence = {
            "trigger": "backward_timestamp",
            "previous_record_num": previous.record_num,
            "record_num": current.record_num,
            "previous_time": previous.declared_time,
            "time": current.declared_time,
            "backward_duration": backward,
            "boundary_sequence": current.sequence,
            "sequence": (previous.sequence, current.sequence),
        }
        adjustments.append(
            ClockAdjustment(
                start=current.declared_time,
                end=previous.declared_time,
                offset=backward,
                boundary=current.sequence,
                evidence=evidence,
            )
        )

    return adjustments


def apply_adjustments(
    events: Iterable[TimedEvent], adjustments: Optional[Iterable[ClockAdjustment]]
) -> list[TimedEvent]:
    """
    Return copies of the events with normalized times and skew status filled in.

    The first adjustment whose interval covers an event wins.  When no
    adjustments are supplied at all, every event keeps its declared time and is
    marked :data:`SKEW_UNKNOWN` (there is no evidence either way).  When
    adjustments exist but none covers an event, it is marked :data:`SKEW_NONE`;
    a covered event is marked :data:`SKEW_ADJUSTED`.
    """
    adjustments = list(adjustments or ())
    normalized: list[TimedEvent] = []

    for event in events:
        covering = next((adj for adj in adjustments if adj.covers(event)), None)
        if covering is None:
            normalized.append(
                replace(
                    event,
                    normalized_time=event.declared_time,
                    skew_status=SKEW_NONE if adjustments else SKEW_UNKNOWN,
                )
            )
        else:
            normalized.append(
                replace(
                    event,
                    normalized_time=event.declared_time + covering.offset,
                    skew_status=SKEW_ADJUSTED,
                )
            )

    return normalized


def sort_events(events: Iterable[TimedEvent], order: str = ORDER_RECORDED) -> list[TimedEvent]:
    """
    Sort events under the requested ordering while preserving identity.

    every tie (equal declared or normalized timestamps) is resolved by the
    physical sequence, so equal-time records keep their original order and
    their distinct record numbers.
    """
    events = list(events)

    def recorded_key(event: TimedEvent) -> tuple[Any, ...]:
        return (event.sequence,)

    def declared_key(event: TimedEvent) -> tuple[Any, ...]:
        return (event.declared_time, event.sequence)

    def normalized_key(event: TimedEvent) -> tuple[Any, ...]:
        return (event.normalized_time, event.sequence)

    keys = {
        ORDER_RECORDED: recorded_key,
        ORDER_DECLARED: declared_key,
        ORDER_NORMALIZED: normalized_key,
    }
    if order not in keys:
        raise ValueError("unknown order: {!r}; expected one of {}".format(order, ORDERS))
    return sorted(events, key=keys[order])


def filter_events_by_time(
    events: Iterable[TimedEvent],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    time_basis: str = TIME_DECLARED,
) -> Iterator[TimedEvent]:
    """
    Yield events whose selected time lies in the inclusive ``[start, end]``
    window, comparing against either the declared or normalized time.
    """
    if time_basis not in TIME_BASES:
        raise ValueError("unknown time basis: {!r}; expected one of {}".format(time_basis, TIME_BASES))

    for event in events:
        when = event.time(time_basis)
        if start is not None and when < start:
            continue
        if end is not None and when > end:
            continue
        yield event


def build_timeline(
    source: Any,
    adjustments: Optional[Iterable[ClockAdjustment]] = None,
    *,
    time_basis: str = TIME_DECLARED,
    order: str = ORDER_RECORDED,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list[TimedEvent]:
    """
    Functional one-shot query.

    ``adjustments=None`` means no correction is attempted.  Pass the result of
    :func:`detect_clock_adjustments` (or hand-built intervals) to enable the
    normalized basis.  ``time_basis`` controls which time the window filter and
    output use, while ``order`` independently controls the ordering, so the
    caller picks both interpretations explicitly.
    """
    if time_basis not in TIME_BASES:
        raise ValueError("unknown time basis: {!r}; expected one of {}".format(time_basis, TIME_BASES))
    if order not in ORDERS:
        raise ValueError("unknown order: {!r}; expected one of {}".format(order, ORDERS))

    events = apply_adjustments(iter_record_events(source), adjustments)
    events = sort_events(events, order)
    return list(filter_events_by_time(events, start, end, time_basis))


class EventTimeline:
    """
    Reusable, evidence-aware view over a set of events.

    The timeline is built once with (or without) clock-adjustment intervals and
    queried repeatedly under whichever basis and ordering each caller needs.
    Building it without adjustments never rewrites a timestamp.
    """

    def __init__(
        self,
        events: Iterable[TimedEvent],
        adjustments: Optional[Iterable[ClockAdjustment]] = None,
    ):
        self._adjustments = tuple(adjustments or ())
        self._events = apply_adjustments(events, self._adjustments)

    @classmethod
    def from_source(
        cls,
        source: Any,
        adjustments: Optional[Iterable[ClockAdjustment]] = None,
    ) -> "EventTimeline":
        return cls(iter_record_events(source), adjustments)

    @property
    def adjustments(self) -> tuple[ClockAdjustment, ...]:
        return self._adjustments

    def events(self) -> list[TimedEvent]:
        return list(self._events)

    def query(
        self,
        *,
        time_basis: str = TIME_DECLARED,
        order: str = ORDER_RECORDED,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[TimedEvent]:
        if time_basis not in TIME_BASES:
            raise ValueError("unknown time basis: {!r}; expected one of {}".format(time_basis, TIME_BASES))
        if order not in ORDERS:
            raise ValueError("unknown order: {!r}; expected one of {}".format(order, ORDERS))

        ordered = sort_events(self._events, order)
        return list(filter_events_by_time(ordered, start, end, time_basis))

    def __iter__(self) -> Iterator[TimedEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)
