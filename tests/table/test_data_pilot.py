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
"""Tests for the data pilot (pivot table) parsing and auditing module."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from odfdo import (
    DataPilotTable,
    Document,
    PilotAuditor,
    audit_pilot_tables,
    get_pilot_tables,
)
from odfdo.data_pilot import (
    ORIENTATION_COLUMN,
    ORIENTATION_DATA,
    ORIENTATION_PAGE,
    ORIENTATION_ROW,
    STATUS_MATCH,
    STATUS_MISMATCH,
    STATUS_MISSING_CACHE,
    STATUS_UNEXPECTED_CACHE,
    STATUS_UNVERIFIABLE,
    SUPPORTED_FUNCTIONS,
    parse_cell_range_address,
)

from .pilot_fixtures import build_pilot_document

SOURCE = [
    ["Region", "Product", "Amount", "Amount"],
    ["North", "A", 10, 1],
    ["North", "A", 20, 2],
    ["North", "B", 30, 3],
    ["South", "A", 40, 4],
    ["South", "B", 50, 5],
    ["South", "B", 60, 6],
    ["East", "A", 70, 7],
    ["East", "B", 80, 8],
]

ROW_COL_SUM_FIELDS = [
    {"name": "Region", "orientation": "row"},
    {"name": "Product", "orientation": "column"},
    {"name": "Amount", "orientation": "data", "function": "sum"},
]

ROW_COL_SUM_CACHE = [
    [None, "Product", None, None],
    [None, "A", "B", "Total Result"],
    ["North", 30, 30, 60],
    ["South", 40, 110, 150],
    ["East", 70, 80, 150],
    ["Total Result", 140, 220, 360],
]


def _first_report(document: Document):
    reports = PilotAuditor(document).audit_all()
    assert len(reports) == 1
    return reports[0]


def test_parse_cell_range_address_full():
    sheet, cell_range = parse_cell_range_address("$Data.$A$1:$Data.$D$9")
    assert sheet == "Data"
    assert cell_range == (0, 0, 3, 8)


def test_parse_cell_range_address_relative_continuation():
    sheet, cell_range = parse_cell_range_address("$Data.$A$1:.$D$9")
    assert sheet == "Data"
    assert cell_range == (0, 0, 3, 8)


def test_parse_cell_range_address_single_cell():
    sheet, cell_range = parse_cell_range_address("$Pivot.$B$4")
    assert sheet == "Pivot"
    assert cell_range == (1, 3, 1, 3)


def test_parse_cell_range_quoted_sheet():
    sheet, cell_range = parse_cell_range_address("$'My Sheet'.$B$4:$C$5")
    assert sheet == "My Sheet"
    assert cell_range == (1, 3, 2, 4)


def test_parse_cell_range_bad_address():
    with pytest.raises(ValueError):
        parse_cell_range_address("not an address")


def test_document_without_pilot():
    document = Document("spreadsheet")
    assert get_pilot_tables(document) == []
    assert audit_pilot_tables(document) == []


def test_pilot_definition_parsed():
    document = build_pilot_document(
        SOURCE, ROW_COL_SUM_FIELDS, ROW_COL_SUM_CACHE
    )
    pilots = get_pilot_tables(document)
    assert len(pilots) == 1
    pilot = pilots[0]
    assert isinstance(pilot, DataPilotTable)
    assert pilot.name == "Pivot1"
    assert pilot.source_cell_range_address == "$Data.$A$1:$Data.$D$9"
    fields = pilot.get_fields()
    assert fields[0].source_field_name == "Region"
    assert fields[0].orientation == ORIENTATION_ROW
    assert fields[1].orientation == ORIENTATION_COLUMN
    assert fields[2].orientation == ORIENTATION_DATA
    assert fields[2].function == "sum"


def test_report_exposes_fields_and_ranges():
    document = build_pilot_document(
        SOURCE, ROW_COL_SUM_FIELDS, ROW_COL_SUM_CACHE
    )
    report = _first_report(document)
    assert report.source_table == "Data"
    assert report.source_range == (0, 0, 3, 8)
    assert report.target_table == "Pivot"
    assert [f.source_column.name for f in report.row_fields] == ["Region"]
    assert [f.source_column.name for f in report.column_fields] == [
        "Product"
    ]
    assert report.data_fields[0].function == "sum"
    assert report.fully_supported is True
    assert report.unsupported_functions == []


def test_duplicate_header_names_resolve_to_distinct_columns():
    fields = [
        {"name": "Region", "orientation": "row"},
        {"name": "Amount", "orientation": "data", "function": "sum"},
        {"name": "Amount", "orientation": "data", "function": "average"},
    ]
    document = build_pilot_document(SOURCE, fields, ROW_COL_SUM_CACHE)
    report = _first_report(document)
    indexes = [f.source_column.index for f in report.data_fields]
    names = [f.source_column.name for f in report.data_fields]
    assert names == ["Amount", "Amount"]
    assert indexes == [2, 3]
    assert report.data_fields[0].source_column.key != (
        report.data_fields[1].source_column.key
    )


def test_fresh_cache_matches_recomputation():
    document = build_pilot_document(
        SOURCE, ROW_COL_SUM_FIELDS, ROW_COL_SUM_CACHE
    )
    report = _first_report(document)
    assert report.is_stale is False
    assert report.mismatch_count == 0
    assert report.match_count == 12
    assert report.unverifiable_count == 0


def test_stale_cache_points_to_discrepant_cells():
    stale = [row[:] for row in ROW_COL_SUM_CACHE]
    stale[2][1] = 999  # North / A cached value
    document = build_pilot_document(SOURCE, ROW_COL_SUM_FIELDS, stale)
    report = _first_report(document)
    assert report.is_stale is True
    mismatches = report.mismatches
    assert len(mismatches) == 1
    discrepancy = mismatches[0]
    assert discrepancy.status == STATUS_MISMATCH
    assert discrepancy.address == "$Pivot.$B$3"
    assert discrepancy.coordinate == (1, 2)
    assert discrepancy.cached == 999
    assert discrepancy.expected == 30
    assert discrepancy.row_labels == ("North",)
    assert discrepancy.column_labels == ("A",)


def test_changed_source_detected_against_unchanged_cache():
    changed = [row[:] for row in SOURCE]
    changed[1][2] = 15  # first North/A detail row
    document = build_pilot_document(
        changed, ROW_COL_SUM_FIELDS, ROW_COL_SUM_CACHE
    )
    report = _first_report(document)
    addresses = {item.address for item in report.mismatches}
    # North/A, North total and both grand totals change
    assert "$Pivot.$B$3" in addresses
    assert "$Pivot.$D$3" in addresses
    assert "$Pivot.$B$6" in addresses
    assert "$Pivot.$D$6" in addresses
    mismatch = next(
        item for item in report.mismatches if item.address == "$Pivot.$B$3"
    )
    assert mismatch.cached == 30
    assert mismatch.expected == 35


def test_unknown_aggregation_is_unverifiable_not_zero():
    fields = [
        {"name": "Region", "orientation": "row"},
        {"name": "Amount", "orientation": "data", "function": "product"},
    ]
    cached = [
        [None, "Amount"],
        ["North", 6000],
        ["South", 120000],
        ["East", 5600],
        ["Total Result", 4032000000],
    ]
    document = build_pilot_document(SOURCE, fields, cached)
    report = _first_report(document)
    assert report.fully_supported is False
    assert report.unsupported_functions == ["product"]
    assert report.mismatch_count == 0
    assert report.unverifiable_count == 4
    for discrepancy in report.discrepancies:
        assert discrepancy.status == STATUS_UNVERIFIABLE
        assert discrepancy.expected is not None
        assert repr(discrepancy.expected) == "<unverifiable>"
        assert discrepancy.reason is not None


def test_missing_cache_cell_reported():
    broken = [row[:] for row in ROW_COL_SUM_CACHE]
    broken[3][2] = None  # South / B cache erased
    document = build_pilot_document(SOURCE, ROW_COL_SUM_FIELDS, broken)
    report = _first_report(document)
    missing = [
        item
        for item in report.discrepancies
        if item.status == STATUS_MISSING_CACHE
    ]
    assert len(missing) == 1
    assert missing[0].address == "$Pivot.$C$4"
    assert missing[0].expected == 110
    assert missing[0].cached is None


def test_unexpected_cache_value_reported():
    # No "West" member exists in the source but the cache carries one.
    cache = [row[:] for row in ROW_COL_SUM_CACHE]
    cache.insert(
        4, ["West", 1, 2, 3]
    )
    fields = list(ROW_COL_SUM_FIELDS)
    document = build_pilot_document(SOURCE, fields, cache)
    report = _first_report(document)
    unexpected = [
        item
        for item in report.discrepancies
        if item.status == STATUS_UNEXPECTED_CACHE
    ]
    assert unexpected
    for discrepancy in unexpected:
        assert discrepancy.row_labels == ("West",)
        assert repr(discrepancy.expected) == "<no-cache>"


@pytest.mark.parametrize(
    ("function", "expected_values"),
    [
        ("sum", {"North": 60, "South": 150, "East": 150}),
        ("count", {"North": 3, "South": 3, "East": 2}),
        ("countnums", {"North": 3, "South": 3, "East": 2}),
        ("min", {"North": 10, "South": 40, "East": 70}),
        ("max", {"North": 30, "South": 60, "East": 80}),
    ],
)
def test_supported_aggregations_row_only(function, expected_values):
    fields = [
        {"name": "Region", "orientation": "row"},
        {"name": "Amount", "orientation": "data", "function": function},
    ]
    rows = [["Region", function]]
    for region, value in expected_values.items():
        rows.append([region, value])
    total = (
        sum(expected_values.values())
        if function in {"sum", "count", "countnums"}
        else None
    )
    if function in {"min", "max"}:
        values = list(expected_values.values())
        total = min(values) if function == "min" else max(values)
    rows.append(["Total Result", total])
    document = build_pilot_document(SOURCE, fields, rows)
    report = _first_report(document)
    assert report.mismatch_count == 0
    assert report.unverifiable_count == 0


def test_average_uses_decimal_and_tolerance():
    fields = [
        {"name": "Region", "orientation": "row"},
        {"name": "Amount", "orientation": "data", "function": "average"},
    ]
    cached = [
        [None, "average"],
        ["North", 20],
        ["South", 50],
        ["East", 75],
        ["Total Result", 45],
    ]
    document = build_pilot_document(SOURCE, fields, cached)
    report = _first_report(document)
    assert report.mismatch_count == 0
    discrepancy = next(
        item
        for item in report.discrepancies
        if item.address == "$Pivot.$B$5"
    )
    assert discrepancy.expected == Decimal(45)


def test_float_tolerance_close_values_match():
    fields = [
        {"name": "Region", "orientation": "row"},
        {"name": "Amount", "orientation": "data", "function": "average"},
    ]
    cached = [
        [None, "average"],
        ["North", 20.0000000001],
        ["South", 50],
        ["East", 75],
        ["Total Result", 45],
    ]
    document = build_pilot_document(SOURCE, fields, cached)
    report = _first_report(document)
    assert report.is_stale is False


def test_page_filter_recomputed_from_selected_member():
    fields = [
        {
            "name": "Region",
            "orientation": "page",
            "selected_page": "North",
        },
        {"name": "Product", "orientation": "row"},
        {"name": "Amount", "orientation": "data", "function": "sum"},
    ]
    cached = [
        [None, None],
        ["Product", "Sum - Amount"],
        ["A", 30],
        ["B", 30],
        ["Total Result", 60],
    ]
    document = build_pilot_document(SOURCE, fields, cached)
    report = _first_report(document)
    assert report.page_fields[0].orientation == ORIENTATION_PAGE
    assert report.page_fields[0].selected_page == "North"
    assert report.mismatch_count == 0
    assert report.match_count == 3
    assert all(
        item.status == STATUS_MATCH for item in report.discrepancies
    )


def test_hidden_members_are_excluded():
    fields = [
        {
            "name": "Region",
            "orientation": "row",
            "hidden_members": ["East"],
        },
        {"name": "Amount", "orientation": "data", "function": "sum"},
    ]
    cached = [
        [None, "Amount"],
        ["North", 60],
        ["South", 150],
        ["Total Result", 210],
    ]
    document = build_pilot_document(SOURCE, fields, cached)
    report = _first_report(document)
    assert report.row_fields[0].hidden_members == ["East"]
    assert report.mismatch_count == 0
    grand_total = next(
        item
        for item in report.discrepancies
        if item.row_labels == ("Total Result",)
    )
    assert grand_total.expected == 210


def test_multiple_data_fields_distinguish_duplicate_columns():
    fields = [
        {"name": "Region", "orientation": "row"},
        {"name": "Amount", "orientation": "data", "function": "sum"},
        {"name": "Amount", "orientation": "data", "function": "average"},
    ]
    # Two data columns: sum of column C then average of column D.
    cached = [
        [None, "Sum - Amount", "Average - Amount"],
        ["North", 60, 2],
        ["South", 150, 5],
        ["East", 150, 7.5],
        ["Total Result", 360, 4.5],
    ]
    document = build_pilot_document(SOURCE, fields, cached)
    report = _first_report(document)
    assert report.mismatch_count == 0
    sum_cell = next(
        item
        for item in report.discrepancies
        if item.address == "$Pivot.$B$2"
    )
    average_cell = next(
        item
        for item in report.discrepancies
        if item.address == "$Pivot.$C$2"
    )
    assert sum_cell.data_field == ("Amount", 2)
    assert average_cell.data_field == ("Amount", 3)
    assert average_cell.expected == 2


def test_missing_source_range_marks_cache_unverifiable():
    document = build_pilot_document(
        SOURCE, ROW_COL_SUM_FIELDS, ROW_COL_SUM_CACHE
    )
    pilot = get_pilot_tables(document)[0]
    pilot.set_attribute(
        "table:source-cell-range-address", "$Missing.$A$1:$D$9"
    )
    report = _first_report(document)
    assert report.source_available is False
    assert report.reason is not None
    assert all(
        item.status == STATUS_UNVERIFIABLE
        for item in report.discrepancies
    )


def test_report_to_dict_is_json_serializable():
    document = build_pilot_document(
        SOURCE, ROW_COL_SUM_FIELDS, ROW_COL_SUM_CACHE
    )
    report = _first_report(document)
    payload = json.dumps(report.to_dict())
    assert '"mismatch_count": 0' in payload
    decoded = json.loads(payload)
    assert decoded["source_range"] == [0, 0, 3, 8]
    assert decoded["row_fields"][0]["source_column"]["name"] == "Region"


def test_supported_function_set_matches_contract():
    assert SUPPORTED_FUNCTIONS == frozenset(
        {"sum", "count", "countnums", "average", "min", "max"}
    )


def test_target_origin_offset_is_respected():
    cache = [
        [None, "Product", None, None],
        [None, "A", "B", "Total Result"],
        ["North", 30, 30, 60],
        ["South", 40, 110, 150],
        ["East", 70, 80, 150],
        ["Total Result", 140, 220, 360],
    ]
    document = build_pilot_document(
        SOURCE,
        ROW_COL_SUM_FIELDS,
        cache,
        target_origin=(2, 3),
        target_range=(2, 3, 5, 8),
    )
    report = _first_report(document)
    assert report.target_range == (2, 3, 5, 8)
    assert report.is_stale is False
    addresses = {item.address for item in report.discrepancies}
    assert "$Pivot.$D$6" in addresses
    assert "$Pivot.$F$9" in addresses


def test_grand_total_flags_parsed():
    document = build_pilot_document(
        SOURCE,
        ROW_COL_SUM_FIELDS,
        ROW_COL_SUM_CACHE,
        row_grand_total=False,
        column_grand_total=False,
    )
    pilot = get_pilot_tables(document)[0]
    assert pilot.row_grand_total is False
    assert pilot.column_grand_total is False


def test_filtered_member_display_parsing():
    fields = [
        {
            "name": "Region",
            "orientation": "row",
            "hidden_members": ["East"],
        },
        {"name": "Amount", "orientation": "data", "function": "sum"},
    ]
    document = build_pilot_document(SOURCE, fields, ROW_COL_SUM_CACHE)
    pilot = get_pilot_tables(document)[0]
    field = pilot.get_fields()[0]
    assert field.get_hidden_members() == ["East"]
    member = field.get_levels()[0].get_members()[0]
    assert member.display is False
    assert member.name == "East"


def test_empty_aggregate_returns_none_not_zero():
    # Hide every region, so every bucket is empty.
    fields = [
        {
            "name": "Region",
            "orientation": "row",
            "hidden_members": ["North", "South", "East"],
        },
        {"name": "Amount", "orientation": "data", "function": "sum"},
    ]
    cached = [
        [None, "Amount"],
        ["Total Result", None],
    ]
    document = build_pilot_document(SOURCE, fields, cached)
    report = _first_report(document)
    # No cached numeric value and empty aggregate: nothing mismatches.
    assert report.mismatch_count == 0
    assert report.unverifiable_count == 0
