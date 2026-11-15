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
import json
import sys

import Evtx.EventTable as event_table


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Normalize versioned EVTX duration declarations into one event table."
    )
    parser.add_argument("evtx", type=str, help="Path to the Windows EVTX event log file")
    parser.add_argument(
        "-m",
        "--mapping",
        type=str,
        required=True,
        help="JSON file with explicit (provider, version) duration mappings",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("jsonl", "json", "csv"),
        default="jsonl",
        help="Output format (default: jsonl)",
    )
    parser.add_argument("-o", "--output", type=str, help="Write output to this file instead of stdout")
    parser.add_argument(
        "--canonical-unit",
        type=str,
        default=None,
        help="Unit of the unified duration column (default: mapping file value, else ms)",
    )
    parser.add_argument(
        "--show-conversions",
        action="store_true",
        help="Write one conversion record per event to stderr",
    )
    args = parser.parse_args()

    with open(args.mapping, "r", encoding="utf-8") as handle:
        mapping = json.load(handle)

    canonical_unit = args.canonical_unit or mapping.get("canonical_unit", "ms")
    table = event_table.EventTable(event_table.VersionMap(mapping), canonical_unit=canonical_unit)
    table.normalize_many(event_table.iter_declared_events(args.evtx))

    if args.format == "jsonl":
        payload = "".join(json.dumps(row, default=str) + "\n" for row in table.rows)
    elif args.format == "json":
        payload = json.dumps(table.rows, indent=2, default=str)
    else:
        payload = table.to_csv()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        sys.stdout.write(payload)

    if args.show_conversions:
        for conversion in table.conversions:
            sys.stderr.write(json.dumps(conversion, default=str) + "\n")


if __name__ == "__main__":
    main()
