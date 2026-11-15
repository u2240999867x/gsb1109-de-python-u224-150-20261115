#!/usr/bin/python
"""
Report EVTX events under a caller-selected time/ordering basis.

Host clock calibration can move the clock backwards, so a freshly written event
carries an older timestamp than events written before it.  A report that sorts
only on the declared timestamp then files the new event under an old shift.

This tool keeps the three facts separate:

- the physical recording order (sequence and record number);
- the declared timestamp, exactly as stored;
- clock-adjustment intervals detected from backward steps (only when asked, so
  timestamps are never rewritten without evidence).

The caller chooses both the time basis (``declared`` or ``normalized``) and the
ordering (``recorded``, ``declared`` or ``normalized``).  Equal timestamps are
always tie-broken by recording order, so distinct events stay distinct.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from Evtx.Evtx import Evtx
from Evtx.TimeSkew import (
    ORDER_RECORDED,
    ORDERS,
    SKEW_ADJUSTED,
    TIME_BASES,
    TIME_DECLARED,
    TIME_NORMALIZED,
    EventTimeline,
    detect_clock_adjustments,
    iter_record_events,
)


def parse_datetime(value):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError("unsupported date/time format: {!r}".format(value))


def build_parser():
    parser = argparse.ArgumentParser(description="Report events under a selected time basis and ordering.")
    parser.add_argument("evtx", type=str, help="Path to the Windows EVTX file")
    parser.add_argument(
        "--basis",
        choices=TIME_BASES,
        default=TIME_DECLARED,
        help="Time basis used for filtering and the printed time column (default: declared).",
    )
    parser.add_argument(
        "--order",
        choices=ORDERS,
        default=ORDER_RECORDED,
        help="Row ordering; recorded preserves the original write order (default: recorded).",
    )
    parser.add_argument(
        "--detect-adjustments",
        action="store_true",
        help="Infer clock-adjustment intervals from backward steps and enable normalized time.",
    )
    parser.add_argument("--start", type=parse_datetime, help="Inclusive window start, YYYY-mm-dd HH:MM:SS(.f)")
    parser.add_argument("--end", type=parse_datetime, help="Inclusive window end, YYYY-mm-dd HH:MM:SS(.f)")
    parser.add_argument(
        "--show-adjustments",
        action="store_true",
        help="Print the detected clock-adjustment intervals on stderr before the report.",
    )
    return parser


def main():
    args = build_parser().parse_args()

    with Evtx(args.evtx) as evtx:
        events = list(iter_record_events(evtx))

    adjustments = detect_clock_adjustments(events) if args.detect_adjustments else None

    if args.show_adjustments:
        if adjustments:
            for adj in adjustments:
                sys.stderr.write(
                    "adjustment [{}, {}) offset={} evidence={}\n".format(
                        adj.start.isoformat(" "),
                        adj.end.isoformat(" "),
                        adj.offset,
                        adj.evidence,
                    )
                )
        else:
            sys.stderr.write("no clock-adjustment evidence found; timestamps are not corrected\n")

    if args.basis == TIME_NORMALIZED and not adjustments:
        raise SystemExit(
            "normalized basis requires clock-adjustment evidence; "
            "pass --detect-adjustments or supply intervals via the Evtx.TimeSkew API"
        )

    timeline = EventTimeline(events, adjustments)
    rows = timeline.query(time_basis=args.basis, order=args.order, start=args.start, end=args.end)

    header = "{:>6}  {:>10}  {:>32}  {:>32}  {:>9}".format(
        "seq", "record", "declared_time", "normalized_time", "skew"
    )
    print(header)
    for event in rows:
        print(
            "{:>6}  {:>10}  {:>32}  {:>32}  {:>9}".format(
                event.sequence,
                event.record_num,
                event.declared_time.isoformat(" "),
                event.normalized_time.isoformat(" "),
                event.skew_status,
            )
        )

    if any(event.skew_status == SKEW_ADJUSTED for event in rows):
        sys.stderr.write("{} event(s) normalized using clock-adjustment evidence\n".format(
            sum(event.skew_status == SKEW_ADJUSTED for event in rows)
        ))


if __name__ == "__main__":
    main()
