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
"""Tests for named range scope resolution and range coordinate shifts."""

from __future__ import annotations

import pytest

from odfdo import Document, NamedRange, Table
from odfdo.named_range import SCOPE_GLOBAL, SCOPE_LOCAL


def _three_sheet_document() -> tuple[Document, Table, Table, Table]:
    document = Document("ods")
    body = document.body
    sheet_one = body.get_table(0)
    sheet_one.name = "Sheet1"
    sheet_two = Table("Sheet2")
    sheet_three = Table("Sheet3")
    body.append(sheet_two)
    body.append(sheet_three)
    for sheet in (sheet_one, sheet_two, sheet_three):
        sheet.set_value("E6", 1)
    return document, sheet_one, sheet_two, sheet_three


def _input_ranges(
    document: Document, sheet_one: Table, sheet_two: Table, sheet_three: Table
) -> None:
    body = document.body
    sheet_one.set_named_range("Input", "A2", global_scope=False)
    sheet_two.set_named_range("Input", "B3:C4", global_scope=False)
    sheet_three.set_named_range("Input", "D5", global_scope=False)
    body.set_named_range("Input", "Z9", table_name="Sheet2")


# --- scope and source declaration -------------------------------------------


def test_local_scope_properties():
    table = Table("Sheet1")
    table.set_named_range("Input", "A2", global_scope=False)
    named_range = table._local_named_range("Input")
    assert named_range is not None
    assert named_range.scope == SCOPE_LOCAL
    assert named_range.is_local
    assert named_range.source_table_name == "Sheet1"
    assert named_range.address == "A2"
    assert named_range.source == "local Input @ Sheet1 -> Sheet1.A2"


def test_global_scope_properties():
    document, _, _, _ = _three_sheet_document()
    document.body.set_named_range("Input", "B3:C4", table_name="Sheet1")
    named_range = document.body.get_named_range("Input")
    assert named_range is not None
    assert named_range.scope == SCOPE_GLOBAL
    assert not named_range.is_local
    assert named_range.source_table_name is None
    assert named_range.address == "B3:C4"
    assert named_range.source == "global Input -> Sheet1.B3:C4"


def test_unbound_named_range_has_no_scope():
    named_range = NamedRange("Input", "A1", "Sheet1")
    assert named_range.scope is None
    assert not named_range.is_local
    assert named_range.source_table_name is None
    assert named_range.source == "unbound Input -> Sheet1.A1"


def test_resolution_prefers_local_range_of_calling_table():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    expected = {
        "Sheet1": (0, 1, 0, 1),
        "Sheet2": (1, 2, 2, 3),
        "Sheet3": (3, 4, 3, 4),
    }
    for sheet in (sheet_one, sheet_two, sheet_three):
        named_range = sheet.get_named_range("Input")
        assert named_range is not None
        assert named_range.is_local
        assert named_range.source_table_name == sheet.name
        assert named_range.crange == expected[sheet.name]


def test_resolution_falls_back_to_global_range():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    sheet_four = Table("Sheet4")
    document.body.append(sheet_four)
    named_range = sheet_four.get_named_range("Input")
    assert named_range is not None
    assert named_range.scope == SCOPE_GLOBAL
    assert named_range.table_name == "Sheet2"
    assert named_range.crange == (25, 8, 25, 8)


def test_other_sheet_local_range_never_fills_gap():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    sheet_two.set_named_range("Only_Local", "K1", global_scope=False)
    # no global declaration and no local declaration on Sheet1
    assert sheet_one.get_named_range("Only_Local") is None
    assert sheet_one.get_named_range("Only_Local", global_scope=False) is None
    assert document.body.get_named_range("Only_Local") is None


def test_local_only_query_ignores_global_range():
    document, sheet_one, _, _ = _three_sheet_document()
    document.body.set_named_range("Input", "Z9", table_name="Sheet1")
    assert sheet_one.get_named_range("Input", global_scope=False) is None
    assert sheet_one.get_named_ranges(global_scope=False) == []


def test_body_queries_exclude_table_local_ranges():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    global_ranges = document.body.get_named_ranges()
    assert len(global_ranges) == 1
    assert global_ranges[0].scope == SCOPE_GLOBAL
    global_input = document.body.get_named_range("Input")
    assert global_input is not None
    assert global_input.table_name == "Sheet2"
    assert global_input.crange == (25, 8, 25, 8)


def test_resolve_named_range_alias():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    named_range = sheet_two.resolve_named_range("Input")
    other = sheet_two.get_named_range("Input")
    assert named_range is not None and other is not None
    assert named_range._xml_element is other._xml_element
    assert named_range.is_local


# --- row insertion and deletion ----------------------------------------------


def test_insert_row_shifts_local_and_global_ranges():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    sheet_two.insert_row(1)
    assert sheet_two._local_named_range("Input").crange == (1, 3, 2, 4)
    assert document.body.get_named_range("Input").crange == (25, 9, 25, 9)
    # ranges of other tables are untouched
    assert sheet_one._local_named_range("Input").crange == (0, 1, 0, 1)
    assert sheet_three._local_named_range("Input").crange == (3, 4, 3, 4)


def test_insert_row_at_start_boundary_keeps_start_moves_end():
    named_range = NamedRange("Input", "B3:C4", "Sheet1")
    assert named_range.shift_rows(2, 1)
    assert named_range.crange == (1, 2, 2, 4)


def test_insert_row_moves_point_range_together():
    _document, sheet_one, _, _ = _three_sheet_document()
    sheet_one.set_named_range("Input", "A2", global_scope=False)
    sheet_one.insert_row(0)
    assert sheet_one._local_named_range("Input").crange == (0, 2, 0, 2)


def test_insert_row_at_table_end_does_not_shift():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    before = sheet_two._local_named_range("Input").crange
    sheet_two.insert_row(sheet_two.height)
    assert sheet_two._local_named_range("Input").crange == before


def test_delete_row_shifts_ranges_back():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    sheet_two.insert_row(1)
    sheet_two.delete_row(1)
    assert sheet_two._local_named_range("Input").crange == (1, 2, 2, 3)
    assert document.body.get_named_range("Input").crange == (25, 8, 25, 8)


def test_delete_row_outside_table_is_noop():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    before = sheet_two._local_named_range("Input").crange
    sheet_two.delete_row(sheet_two.height + 3)
    assert sheet_two._local_named_range("Input").crange == before


# --- column insertion and deletion -------------------------------------------


def test_insert_column_shifts_local_and_global_ranges():
    document, sheet_one, sheet_two, _ = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, _)
    document.body.set_named_range("Other", "A1:A3", table_name="Sheet1")
    sheet_one.insert_column(0)
    assert sheet_one._local_named_range("Input").crange == (1, 1, 1, 1)
    assert document.body.get_named_range("Other").crange == (1, 0, 1, 2)
    # ranges on another table are untouched
    assert sheet_two._local_named_range("Input").crange == (1, 2, 2, 3)


def test_insert_column_at_table_end_does_not_shift():
    document, sheet_one, _, _ = _three_sheet_document()
    _input_ranges(document, sheet_one, _, _)
    before = sheet_one._local_named_range("Input").crange
    sheet_one.insert_column(sheet_one.width)
    assert sheet_one._local_named_range("Input").crange == before


def test_delete_column_shifts_ranges_back():
    document, sheet_one, _, _ = _three_sheet_document()
    _input_ranges(document, sheet_one, _, _)
    sheet_one.insert_column(0)
    sheet_one.delete_column(0)
    assert sheet_one._local_named_range("Input").crange == (0, 1, 0, 1)


# --- NamedRange shift unit behavior ------------------------------------------


def test_shift_rows_noop_returns_false():
    named_range = NamedRange("Input", "A1:B2", "Sheet1")
    assert not named_range.shift_rows(5, 0)
    assert not named_range.shift_rows(5, 1)
    assert named_range.crange == (0, 0, 1, 1)


def test_shift_columns_noop_returns_false():
    named_range = NamedRange("Input", "A1:B2", "Sheet1")
    assert not named_range.shift_columns(5, 0)
    assert not named_range.shift_columns(5, 1)
    assert named_range.crange == (0, 0, 1, 1)


def test_delete_rows_collapses_covered_boundaries():
    named_range = NamedRange("Input", (0, 2, 0, 5), "Sheet1")
    assert named_range.shift_rows(3, -2)
    assert named_range.crange == (0, 2, 0, 3)


def test_delete_columns_collapses_covered_boundaries():
    named_range = NamedRange("Input", (2, 0, 5, 0), "Sheet1")
    assert named_range.shift_columns(3, -2)
    assert named_range.crange == (2, 0, 3, 0)


def test_range_shift_never_negative():
    named_range = NamedRange("Input", "A1", "Sheet1")
    named_range.shift_rows(0, -1)
    named_range.shift_columns(0, -1)
    assert named_range.crange == (0, 0, 0, 0)


# --- table rename keeps both scopes consistent -------------------------------


def test_rename_table_updates_local_and_global_ranges():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    document.body.set_named_range("Other", "A1", table_name="Sheet1")
    sheet_one.name = "Renamed"
    assert sheet_one._local_named_range("Input").table_name == "Renamed"
    assert document.body.get_named_range("Other").table_name == "Renamed"


def test_rename_table_keeps_other_table_ranges():
    document, sheet_one, sheet_two, sheet_three = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, sheet_three)
    sheet_one.name = "Renamed"
    assert sheet_two._local_named_range("Input").table_name == "Sheet2"
    assert document.body.get_named_range("Input").table_name == "Sheet2"


# --- renaming a range respects declaration scope -----------------------------


def test_rename_local_range_keeps_other_scopes():
    document, sheet_one, sheet_two, _ = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, _)
    document.body.set_named_range("Input", "C3", table_name="Sheet1")
    sheet_one._local_named_range("Input").name = "Renamed"
    assert sheet_one._local_named_range("Renamed") is not None
    assert sheet_one._local_named_range("Input") is None
    # same-named local range of another table and global range survive
    assert sheet_two._local_named_range("Input") is not None
    assert document.body.get_named_range("Input") is not None


def test_rename_local_range_replaces_same_name_in_same_table():
    _document, sheet_one, _, _ = _three_sheet_document()
    sheet_one.set_named_range("Renamed", "A1", global_scope=False)
    sheet_one.set_named_range("Dup", "D4", global_scope=False)
    sheet_one._local_named_range("Dup").name = "Renamed"
    local_ranges = sheet_one.get_named_ranges(global_scope=False)
    assert len(local_ranges) == 1
    assert local_ranges[0].crange == (3, 3, 3, 3)


def test_rename_global_range_keeps_local_ranges():
    document, sheet_one, sheet_two, _ = _three_sheet_document()
    _input_ranges(document, sheet_one, sheet_two, _)
    document.body.get_named_range("Input").name = "Global_Renamed"
    assert document.body.get_named_range("Global_Renamed") is not None
    assert document.body.get_named_range("Input") is None
    assert sheet_one._local_named_range("Input") is not None
    assert sheet_two._local_named_range("Input") is not None


if __name__ == "__main__":
    pytest.main()
