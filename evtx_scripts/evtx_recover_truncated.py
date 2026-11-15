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
"""Salvage verified records from a truncated EVTX and report what is missing."""
import sys

from Evtx import Recovery, RecoveryReport


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Recover verifiable records from a truncated EVTX file. "
        "Records cut by the truncation are reported, never completed or extrapolated."
    )
    parser.add_argument("evtx", type=str, help="Path to the possibly truncated EVTX file")
    parser.add_argument(
        "-f",
        "--format",
        choices=("summary", "manifest", "manifest-json", "xml", "jsonl"),
        default="summary",
        help="summary: coverage report; manifest: one row per verified record; "
        "xml/jsonl: record data for renderable records (default: summary)",
    )
    parser.add_argument("-o", "--output", type=str, help="Write to this file instead of STDOUT")
    args = parser.parse_args(argv)

    report = Recovery.analyze(args.evtx)
    if args.format == "summary":
        text = RecoveryReport.summary_text(report)
    elif args.format == "manifest":
        text = RecoveryReport.manifest_text(report)
    elif args.format == "manifest-json":
        text = RecoveryReport.manifest_json(report)
    elif args.format == "xml":
        text = RecoveryReport.xml_text(report)
    else:
        text = RecoveryReport.jsonl_text(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
