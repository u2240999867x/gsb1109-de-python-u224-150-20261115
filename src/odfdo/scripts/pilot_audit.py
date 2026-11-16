# Copyright 2018-2026 Jérôme Dumonteil
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Command-line script auditing data pilot (pivot table) caches in an ODS.

The script reads the pivot table definitions stored in an OpenDocument
spreadsheet, recomputes the supported aggregations from the current source
range and compares them with the cached result cells. Stale cells (the source
range changed but the cache was not refreshed) are reported with their
address, cached value and recomputed value.
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace

from odfdo import __version__
from odfdo.data_pilot import (
    STATUS_MISMATCH,
    STATUS_MISSING_CACHE,
    STATUS_UNEXPECTED_CACHE,
    STATUS_UNVERIFIABLE,
    AuditReport,
    PilotAuditor,
)
from odfdo.utils.script_utils import read_document

PROG = "odfdo-pilot-audit"


def configure_parser() -> ArgumentParser:
    """Configure the command-line argument parser."""
    description = (
        "Audit the cached result of every data pilot (pivot table) of an "
        "ODS spreadsheet against its current source range."
    )
    epilog = (
        "Recomputable aggregations: sum, count, countnums, average, min, "
        "max. Other aggregations are reported as unverifiable."
    )
    parser = ArgumentParser(prog=PROG, description=description, epilog=epilog)
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} v{__version__}",
    )
    parser.add_argument(
        "-i",
        "--input",
        action="store",
        dest="input_file",
        metavar="INPUT",
        required=False,
        default=None,
        help="input .ods file, if option not present, read from stdin",
    )
    parser.add_argument(
        "-o",
        "--output",
        action="store",
        dest="output_file",
        metavar="OUTPUT",
        required=False,
        help="write the report to a file instead of standard output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=False,
        help="emit the report as JSON instead of human readable text",
    )
    parser.add_argument(
        "--fail-on-stale",
        action="store_true",
        dest="fail_on_stale",
        default=False,
        help="return a non zero exit code when a stale cache is detected",
    )
    return parser


def parse_cli_args(cli_args: list[str] | None = None) -> Namespace:
    """Parse command-line arguments."""
    parser = configure_parser()
    return parser.parse_args(cli_args)


def _format_field(info) -> str:
    column = info.source_column
    base = f"{column.name!r} (source column #{column.index + 1})"
    if info.orientation == "data":
        return f"{base} -> {info.function or 'unknown'}"
    text = f"{base} [{info.orientation}]"
    if info.selected_page is not None:
        text += f" page={info.selected_page!r}"
    if info.hidden_members:
        text += f" hidden={info.hidden_members}"
    return text


def render_text_report(report: AuditReport) -> str:
    """Render one audit report as human readable text."""
    lines = [f"Pivot table: {report.name or '<unnamed>'}"]
    lines.append(
        f"  source: {report.source_table} {report.source_range or '?'}"
    )
    lines.append(
        f"  target: {report.target_table} {report.target_range or '?'}"
    )
    lines.append("  row fields:")
    lines.extend(f"    - {_format_field(info)}" for info in report.row_fields)
    lines.append("  column fields:")
    lines.extend(
        f"    - {_format_field(info)}" for info in report.column_fields
    )
    lines.append("  page (filter) fields:")
    if report.page_fields:
        lines.extend(
            f"    - {_format_field(info)}" for info in report.page_fields
        )
    else:
        lines.append("    - none")
    lines.append("  data fields:")
    lines.extend(f"    - {_format_field(info)}" for info in report.data_fields)
    if report.unsupported_functions:
        lines.append(
            "  unverifiable aggregations: "
            + ", ".join(report.unsupported_functions)
        )
    if not report.source_available:
        lines.append(f"  source unavailable: {report.reason}")
    lines.append(
        "  cells: "
        f"{report.match_count} match, {report.mismatch_count} stale, "
        f"{report.unverifiable_count} unverifiable"
    )
    problems = [
        item
        for item in report.discrepancies
        if item.status
        in {
            STATUS_MISMATCH,
            STATUS_MISSING_CACHE,
            STATUS_UNEXPECTED_CACHE,
            STATUS_UNVERIFIABLE,
        }
    ]
    if problems:
        lines.append("  discrepancies:")
        for item in problems:
            lines.append(
                f"    - {item.address} [{item.status}] "
                f"cached={item.cached!r} expected={item.expected!r}"
                + (f" ({item.reason})" if item.reason else "")
            )
    else:
        lines.append("  no discrepancy")
    return "\n".join(lines)


def pilot_audit(args: Namespace) -> int:
    """Run the audit and write the report."""
    document = read_document(args.input_file)
    if document.get_type() not in {"spreadsheet", "spreadsheet-template"}:
        msg = "Document must be of Spreadsheet type"
        raise TypeError(msg)
    reports = PilotAuditor(document).audit_all()
    if args.as_json:
        content = json.dumps(
            [report.to_dict() for report in reports],
            ensure_ascii=False,
            indent=2,
        )
    else:
        content = "\n\n".join(
            render_text_report(report) for report in reports
        ) or "No data pilot table found."
    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.write("\n")
    else:
        sys.stdout.write(content)
        sys.stdout.write("\n")
    if args.fail_on_stale and any(report.is_stale for report in reports):
        return 2
    return 0


def main_to_pilot_audit(args: Namespace) -> int:
    """Run the CLI logic with error handling."""
    try:
        return pilot_audit(args)
    except Exception:
        configure_parser().print_help()
        print()
        raise


def main() -> int:
    """Execute the CLI entry point."""
    return main_to_pilot_audit(parse_cli_args())


if __name__ == "__main__":
    raise SystemExit(main())
