#!/usr/bin/env python
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
Dump the EventData/UserData body of an EVTX file as a stable long table.

One row is emitted per binary-XML element.  The fixed columns are listed in
`Evtx.Fields.FIELD_COLUMNS`.  Rows keep namespaces, document order, duplicate
name occurrences, null/empty state and the declared value type.
"""

import json
import sys

import Evtx.Evtx as evtx
import Evtx.Fields as e_fields


def _record_numbers(values):
    numbers = set()
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-", 1)
                numbers.update(range(int(start), int(end) + 1))
            else:
                numbers.add(int(part))
    return numbers


def _write_json(rows, out):
    out.write("[")
    for index, row in enumerate(rows):
        if index:
            out.write(",")
        out.write("\n  ")
        out.write(json.dumps(row.as_dict(), ensure_ascii=False))
    out.write("\n]\n")


def _write_jsonl(rows, out):
    for row in rows:
        out.write(json.dumps(row.as_dict(), ensure_ascii=False))
        out.write("\n")


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Dump EVTX EventData/UserData fields as a stable long table.")
    parser.add_argument("evtx", type=str, help="Path to the Windows EVTX event log file")
    parser.add_argument(
        "-f",
        "--format",
        choices=("csv", "json", "jsonl"),
        default="csv",
        help="Output format (default: csv)",
    )
    parser.add_argument("-o", "--output", type=str, help="Write to this file instead of stdout")
    parser.add_argument(
        "--records",
        action="append",
        help="Only export these record numbers (comma separated, ranges allowed, e.g. 1-10,15)",
    )
    parser.add_argument(
        "--leaves-only",
        action="store_true",
        help="Omit section roots and elements that contain child elements",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check that rebuilding XML from each record's rows preserves the element sequence",
    )
    parser.add_argument(
        "--rebuild",
        metavar="RECORD",
        type=int,
        help="Print the rebuilt EventData/UserData fragment for this record instead of the table",
    )
    args = parser.parse_args(argv)

    wanted = _record_numbers(args.records)
    output = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout

    try:
        with evtx.Evtx(args.evtx) as log:
            if args.rebuild is not None:
                record = log.get_record(args.rebuild)
                if record is None:
                    parser.error("record %d not found" % args.rebuild)
                output.write(e_fields.rebuild_body_xml(record.fields()))
                output.write("\n")
                return 0

            records = [record for record in log.records() if not wanted or record.record_num() in wanted]
            if args.format == "csv":
                output.write(e_fields.rows_to_csv(header_only=True))
                for record in records:
                    rows = e_fields.record_fields(record, include_containers=not args.leaves_only)
                    if args.verify:
                        e_fields.verify_roundtrip(record, rows)
                    output.write(e_fields.rows_to_csv(rows, include_header=False))
            elif args.format == "jsonl":
                for record in records:
                    rows = e_fields.record_fields(record, include_containers=not args.leaves_only)
                    if args.verify:
                        e_fields.verify_roundtrip(record, rows)
                    _write_jsonl(rows, output)
            else:
                all_rows = []
                for record in records:
                    rows = e_fields.record_fields(record, include_containers=not args.leaves_only)
                    if args.verify:
                        e_fields.verify_roundtrip(record, rows)
                    all_rows.extend(rows)
                _write_json(all_rows, output)
    finally:
        if args.output:
            output.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
