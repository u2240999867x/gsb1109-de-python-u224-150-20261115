from datetime import datetime, timedelta, timezone

import pytest

import Evtx.Evtx as evtx
from Evtx.TimeSkew import (
    ORDER_DECLARED,
    ORDER_NORMALIZED,
    ORDER_RECORDED,
    SKEW_ADJUSTED,
    SKEW_NONE,
    SKEW_UNKNOWN,
    TIME_DECLARED,
    TIME_NORMALIZED,
    ClockAdjustment,
    EventTimeline,
    TimedEvent,
    apply_adjustments,
    build_timeline,
    detect_clock_adjustments,
    filter_events_by_time,
    iter_record_events,
    sort_events,
)

UTC = timezone.utc


def at(hour, minute=0, second=0):
    return datetime(2020, 1, 1, hour, minute, second, tzinfo=UTC)


def make_events(spec):
    """
    build TimedEvents from (record_num, declared_time) tuples, assigning the
    dense recording order automatically.
    """
    return [
        TimedEvent(sequence=i, record_num=rn, declared_time=ts, normalized_time=ts)
        for i, (rn, ts) in enumerate(spec)
    ]


@pytest.fixture
def calibrated_log():
    """
    a host writes three events, then its clock is calibrated backwards and two
    more events are stamped with earlier times.
    """
    return make_events(
        [
            (101, at(10, 0)),
            (102, at(10, 0)),  # equal timestamp, distinct identity
            (103, at(10, 5)),
            (104, at(9, 0)),  # written after the backward calibration
            (105, at(9, 1)),
        ]
    )


def test_recorded_order_preserves_physical_sequence(calibrated_log):
    rows = sort_events(calibrated_log, ORDER_RECORDED)
    assert [event.record_num for event in rows] == [101, 102, 103, 104, 105]
    assert [event.sequence for event in rows] == [0, 1, 2, 3, 4]


def test_declared_order_files_new_events_into_old_shift(calibrated_log):
    # this is the reported problem, retained as an explicit caller choice
    rows = sort_events(calibrated_log, ORDER_DECLARED)
    assert [event.record_num for event in rows] == [104, 105, 101, 102, 103]


def test_equal_timestamps_keep_order_and_identity(calibrated_log):
    rows = sort_events(calibrated_log, ORDER_DECLARED)
    tied = [event for event in rows if event.declared_time == at(10, 0)]
    assert [event.record_num for event in tied] == [101, 102]
    assert [event.sequence for event in tied] == [0, 1]
    assert len({event.identity for event in tied}) == 2


def test_detect_clock_adjustment_from_backward_step(calibrated_log):
    adjustments = detect_clock_adjustments(calibrated_log)
    assert len(adjustments) == 1
    adjustment = adjustments[0]
    assert adjustment.start == at(9, 0)
    assert adjustment.end == at(10, 5)
    assert adjustment.offset == timedelta(hours=1, minutes=5)
    assert adjustment.evidence["record_num"] == 104
    assert adjustment.evidence["previous_record_num"] == 103


def test_detect_ignores_equal_timestamps():
    events = make_events([(1, at(8)), (2, at(8)), (3, at(8))])
    assert detect_clock_adjustments(events) == []


def test_detect_respects_tolerance_and_equal_timestamps():
    events = make_events(
        [
            (1, at(10, 0, 10)),
            (2, at(10, 0, 9)),  # one second backwards -> one fold
            (3, at(10, 0, 9)),  # equal timestamp must not create another fold
        ]
    )
    adjustments = detect_clock_adjustments(events)
    assert len(adjustments) == 1
    assert adjustments[0].boundary == 1
    assert detect_clock_adjustments(events, tolerance=timedelta(seconds=2)) == []


def test_detect_deduplicates_identical_folds():
    events = make_events(
        [
            (1, at(10, 0, 10)),
            (2, at(10, 0, 9)),
            (3, at(10, 0, 8)),  # a distinct, smaller fold
        ]
    )
    adjustments = detect_clock_adjustments(events)
    assert [(adj.start, adj.end, adj.boundary) for adj in adjustments] == [
        (at(10, 0, 9), at(10, 0, 10), 1),
        (at(10, 0, 8), at(10, 0, 9), 2),
    ]
    repeated = make_events(
        [
            (1, at(10, 0, 10)),
            (2, at(10, 0, 9)),
            (3, at(10, 0, 10)),
            (4, at(10, 0, 9)),  # same fold interval and boundary value repeats
        ]
    )
    assert len(detect_clock_adjustments(repeated)) == 2


def test_no_adjustment_means_no_correction(calibrated_log):
    rows = apply_adjustments(calibrated_log, None)
    assert all(event.skew_status == SKEW_UNKNOWN for event in rows)
    assert all(event.normalized_time == event.declared_time for event in rows)


def test_non_covering_adjustment_marks_none_without_rewriting():
    events = make_events([(1, at(8)), (2, at(9))])
    adjustment = ClockAdjustment(at(10), at(11), timedelta(hours=1), boundary=0)
    rows = apply_adjustments(events, [adjustment])
    assert all(event.skew_status == SKEW_NONE for event in rows)
    assert all(event.normalized_time == event.declared_time for event in rows)


def test_pre_calibration_event_inside_fold_keeps_declared_time():
    # 103 is written before the calibration yet lands inside the fold window;
    # only the physical boundary prevents rewriting it.
    events = make_events([(103, at(10, 0)), (104, at(9, 0))])
    adjustment = ClockAdjustment(at(9), at(10, 5), timedelta(hours=1), boundary=1)
    rows = {event.record_num: event for event in apply_adjustments(events, [adjustment])}
    assert rows[103].skew_status == SKEW_NONE
    assert rows[103].normalized_time == at(10, 0)
    assert rows[104].skew_status == SKEW_ADJUSTED
    assert rows[104].normalized_time == at(10, 0)


def test_covering_adjustment_reconstructs_time_and_order(calibrated_log):
    adjustments = detect_clock_adjustments(calibrated_log)
    rows = apply_adjustments(calibrated_log, adjustments)

    statuses = {event.record_num: event.skew_status for event in rows}
    assert statuses[104] == SKEW_ADJUSTED
    assert statuses[105] == SKEW_ADJUSTED
    assert statuses[101] == SKEW_NONE

    by_num = {event.record_num: event for event in rows}
    assert by_num[104].normalized_time == at(10, 5)
    assert by_num[105].normalized_time == at(10, 6)

    ordered = sort_events(rows, ORDER_NORMALIZED)
    assert [event.record_num for event in ordered] == [101, 102, 103, 104, 105]
    # the tie between 103 and the corrected 104 is resolved by write order
    assert ordered[2].record_num == 103
    assert ordered[3].record_num == 104


def test_adjustment_interval_is_half_open():
    adjustment = ClockAdjustment(at(9), at(10), timedelta(hours=1), boundary=0)
    assert adjustment.contains(at(9)) is True
    assert adjustment.contains(at(9, 30)) is True
    assert adjustment.contains(at(10)) is False


def test_invalid_adjustment_rejected():
    with pytest.raises(ValueError):
        ClockAdjustment(at(10), at(9), timedelta(hours=1), boundary=0)
    with pytest.raises(ValueError):
        ClockAdjustment(at(9), at(10), object(), boundary=0)
    with pytest.raises(ValueError):
        ClockAdjustment(at(9), at(10), timedelta(hours=1), boundary=-1)


def test_window_filter_uses_selected_basis(calibrated_log):
    adjustments = detect_clock_adjustments(calibrated_log)
    timeline = EventTimeline(calibrated_log, adjustments)

    declared_rows = timeline.query(
        time_basis=TIME_DECLARED,
        order=ORDER_RECORDED,
        start=at(9),
        end=at(9, 30),
    )
    assert [event.record_num for event in declared_rows] == [104, 105]

    normalized_rows = timeline.query(
        time_basis=TIME_NORMALIZED,
        order=ORDER_RECORDED,
        start=at(10, 5),
        end=at(10, 6),
    )
    assert [event.record_num for event in normalized_rows] == [103, 104, 105]


def test_filter_helper_bounds_are_inclusive():
    events = make_events([(1, at(9)), (2, at(10)), (3, at(11))])
    rows = list(filter_events_by_time(events, start=at(9), end=at(11)))
    assert [event.record_num for event in rows] == [1, 2, 3]


def test_event_time_basis_accessor(calibrated_log):
    event = calibrated_log[0]
    assert event.time(TIME_DECLARED) == event.declared_time
    assert event.time(TIME_NORMALIZED) == event.normalized_time
    with pytest.raises(ValueError):
        event.time("bogus")


def test_timeline_query_validates_arguments(calibrated_log):
    timeline = EventTimeline(calibrated_log)
    with pytest.raises(ValueError):
        timeline.query(time_basis="bogus")
    with pytest.raises(ValueError):
        timeline.query(order="bogus")


def test_build_timeline_defaults_to_uncorrected_recorded_order(calibrated_log):
    rows = build_timeline(calibrated_log)
    assert [event.record_num for event in rows] == [101, 102, 103, 104, 105]
    assert all(event.normalized_time == event.declared_time for event in rows)


def test_build_timeline_normalized_basis(calibrated_log):
    adjustments = detect_clock_adjustments(calibrated_log)
    rows = build_timeline(calibrated_log, adjustments, time_basis=TIME_NORMALIZED, order=ORDER_NORMALIZED)
    assert [event.record_num for event in rows] == [101, 102, 103, 104, 105]


def test_iter_record_events_from_file_header(system):
    fh = evtx.FileHeader(system, 0x0)
    events = list(iter_record_events(fh))
    assert len(events) == 1601
    assert [event.sequence for event in events] == list(range(1601))
    records = [record for chunk in fh.chunks() for record in chunk.records()]
    assert [event.record_num for event in events] == [record.record_num() for record in records]
    assert [event.declared_time for event in events] == [record.timestamp() for record in records]
    assert all(event.skew_status == SKEW_UNKNOWN for event in events)
    assert all(event.record is not None for event in events)


def test_iter_record_events_accepts_iterable_of_events_and_records(system):
    fh = evtx.FileHeader(system, 0x0)
    records = [record for chunk in fh.chunks() for record in chunk.records()]

    from_events = list(iter_record_events(iter_record_events(fh)))
    from_records = list(iter_record_events(records))
    assert [event.record_num for event in from_events] == [record.record_num() for record in records]
    assert [event.record_num for event in from_records] == [record.record_num() for record in records]


def test_real_log_backward_step_is_repaired_only_with_evidence(system):
    fh = evtx.FileHeader(system, 0x0)
    events = list(iter_record_events(fh))

    # the first two records are a genuine backward timestamp step
    assert events[0].record_num == 12049
    assert events[1].record_num == 12050
    assert events[1].declared_time < events[0].declared_time

    # without evidence the declared ordering still misfiles 12050 before 12049
    declared_order = [event.record_num for event in sort_events(events, ORDER_DECLARED)]
    assert declared_order.index(12050) < declared_order.index(12049)

    adjustments = detect_clock_adjustments(events)
    assert len(adjustments) >= 1
    timeline = EventTimeline(events, adjustments)
    normalized = timeline.query(order=ORDER_NORMALIZED)
    positions = {event.record_num: index for index, event in enumerate(normalized)}
    assert positions[12049] < positions[12050]
    assert any(event.skew_status == SKEW_ADJUSTED for event in normalized)

    # evidence-free timeline never rewrites anything
    raw = EventTimeline(events)
    assert all(event.skew_status == SKEW_UNKNOWN for event in raw.events())
    assert all(event.normalized_time == event.declared_time for event in raw.events())


def test_real_log_equal_timestamps_remain_distinct_and_ordered(system):
    fh = evtx.FileHeader(system, 0x0)
    events = list(iter_record_events(fh))
    pair = [event for event in events if event.record_num in (12066, 12067)]
    assert len(pair) == 2
    assert pair[0].declared_time == pair[1].declared_time

    ordered = sort_events(pair, ORDER_DECLARED)
    assert [event.record_num for event in ordered] == [12066, 12067]
    assert [event.sequence for event in ordered] == [pair[0].sequence, pair[1].sequence]
