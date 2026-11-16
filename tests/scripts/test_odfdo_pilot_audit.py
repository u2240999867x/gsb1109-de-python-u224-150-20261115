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
"""Tests for the odfdo-pilot-audit command-line script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from odfdo.document import Document
from odfdo.scripts import pilot_audit
from odfdo.scripts.pilot_audit import (
    main_to_pilot_audit,
    parse_cli_args,
    render_text_report,
)

from ..table.pilot_fixtures import build_pilot_document

SCRIPT = Path(pilot_audit.__file__)

SOURCE = [
    ["Region", "Product", "Amount"],
    ["North", "A", 10],
    ["North", "B", 30],
    ["South", "A", 40],
]
FIELDS = [
    {"name": "Region", "orientation": "row"},
    {"name": "Product", "orientation": "column"},
    {"name": "Amount", "orientation": "data", "function": "sum"},
]
FRESH = [
    [None, "A", "B", "Total Result"],
    ["North", 10, 30, 40],
    ["South", 40, None, 40],
    ["Total Result", 50, 30, 80],
]
STALE = [
    [None, "A", "B", "Total Result"],
    ["North", 999, 30, 40],
    ["South", 40, None, 40],
    ["Total Result", 50, 30, 80],
]


def _run(params: list[str]):
    command = [sys.executable, SCRIPT] + params
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = proc.communicate()
    return out, err, proc.returncode


def test_parser_defaults():
    args = parse_cli_args(["-i", "in.ods"])
    assert args.input_file == "in.ods"
    assert args.as_json is False
    assert args.fail_on_stale is False
    assert args.output_file is None


def test_cli_text_report_fresh(tmp_path):
    document = build_pilot_document(SOURCE, FIELDS, FRESH)
    path = tmp_path / "fresh.ods"
    document.save(str(path))
    out, err, code = _run(["-i", str(path)])
    assert code == 0
    assert err == b""
    text = out.decode()
    assert "Pivot table: Pivot1" in text
    assert "0 stale" in text
    assert "8 match" in text


def test_cli_json_report_stale(tmp_path):
    document = build_pilot_document(SOURCE, FIELDS, STALE)
    path = tmp_path / "stale.ods"
    document.save(str(path))
    out, err, code = _run(["-i", str(path), "--json"])
    assert code == 0
    assert err == b""
    payload = json.loads(out.decode())
    assert len(payload) == 1
    assert payload[0]["is_stale"] is True
    assert payload[0]["mismatch_count"] == 1


def test_cli_fail_on_stale_exit_code(tmp_path):
    document = build_pilot_document(SOURCE, FIELDS, STALE)
    path = tmp_path / "stale.ods"
    document.save(str(path))
    _out, _err, code = _run(
        ["-i", str(path), "--fail-on-stale"]
    )
    assert code == 2


def test_cli_fresh_with_fail_flag_succeeds(tmp_path):
    document = build_pilot_document(SOURCE, FIELDS, FRESH)
    path = tmp_path / "fresh.ods"
    document.save(str(path))
    _out, _err, code = _run(
        ["-i", str(path), "--fail-on-stale"]
    )
    assert code == 0


def test_cli_output_file(tmp_path):
    document = build_pilot_document(SOURCE, FIELDS, FRESH)
    input_path = tmp_path / "in.ods"
    output_path = tmp_path / "report.txt"
    document.save(str(input_path))
    args = parse_cli_args(
        ["-i", str(input_path), "-o", str(output_path)]
    )
    code = main_to_pilot_audit(args)
    assert code == 0
    text = output_path.read_text(encoding="utf-8")
    assert "Pivot table: Pivot1" in text


def test_cli_rejects_non_spreadsheet(tmp_path):
    document = Document("text")
    path = tmp_path / "note.odt"
    document.save(str(path))
    args = parse_cli_args(["-i", str(path)])
    try:
        main_to_pilot_audit(args)
    except TypeError as exc:
        assert "Spreadsheet" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("TypeError expected")


def test_render_text_report_handles_missing_source():
    document = build_pilot_document(SOURCE, FIELDS, FRESH)
    pilot = document.body.get_elements(
        "descendant::table:data-pilot-tables/table:data-pilot-table"
    )[0]
    pilot.set_attribute(
        "table:source-cell-range-address", "$Gone.$A$1:$C$4"
    )
    from odfdo import PilotAuditor

    report = PilotAuditor(document).audit_all()[0]
    text = render_text_report(report)
    assert "source unavailable" in text
    assert report.source_available is False
