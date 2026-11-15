#!/usr/bin/env python
"""Collect EVTX records with a resumable, generation-aware checkpoint."""

import json
import argparse
import sys

from Evtx.Collector import CollectorCheckpoint, CollectorError, DeliveryError, EvtxCollector


def _event_line(event, include_data, include_xml):
    return json.dumps(
        event.to_dict(include_data=include_data, include_xml=include_xml),
        ensure_ascii=False,
        sort_keys=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Collect EVTX records, emitting only records not yet delivered from this file generation."
    )
    parser.add_argument("evtx", type=str, help="Path to the Windows EVTX event log file")
    parser.add_argument(
        "--checkpoint",
        type=str,
        required=True,
        help="Path to the JSON checkpoint; it records generations, verified frontiers and delivered records",
    )
    parser.add_argument("-o", "--output", type=str, help="Write newline-delimited event JSON here (default: stdout)")
    parser.add_argument("--reset", action="store_true", help="Discard checkpoint history before collecting")
    parser.add_argument("--include-data", action="store_true", help="Include the raw record bytes as hex")
    parser.add_argument("--no-xml", action="store_true", help="Do not render or include record XML")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a JSON summary instead of events",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Inspect chunks beyond those declared by the file header",
    )
    args = parser.parse_args()

    checkpoint = CollectorCheckpoint.load(args.checkpoint)
    if args.reset:
        checkpoint.reset()

    collector = EvtxCollector(args.evtx, checkpoint_path=args.checkpoint, include_inactive=args.include_inactive)
    output = None
    stream = sys.stdout

    try:
        if args.output:
            output = open(args.output, "w", encoding="utf-8")
            stream = output

        def write_event(event):
            if not args.summary:
                stream.write(_event_line(event, args.include_data, not args.no_xml))
                stream.write("\n")
                stream.flush()

        result = collector.collect(checkpoint=checkpoint, on_record=write_event)

        if args.summary:
            json.dump(result.to_dict(include_events=False), sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            stream.flush()
    except DeliveryError as e:
        print("error: delivery failed; checkpoint records completed records: {}".format(e), file=sys.stderr)
        return 4
    except CollectorError as e:
        print("error: {}".format(e), file=sys.stderr)
        return 3
    except BrokenPipeError:
        return 0
    finally:
        if output is not None:
            output.close()

    if not result.header_verified:
        print(
            "error: file header failed verification; no checkpoint state was advanced",
            file=sys.stderr,
        )
        return 3
    if result.barrier is not None:
        print(
            "warning: stopped at {:#x}: {}".format(result.barrier.offset, result.barrier.reason),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
