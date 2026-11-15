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
"""Tests for compression aware row insertion."""

from __future__ import annotations

import pytest

from odfdo import (
    SPAN_POLICY_EXPAND,
    SPAN_POLICY_REJECT,
    Document,
    Row,
    RowInsertError,
    SpanInsertionError,
    Table,
)

HUGE = 100_000


def _compressed_table() -> Table:
    table = Table("Big")
    head = Row(width=3)
    head.set_value(0, "head")
    table.append_row(head, clone=False)
    table.append_row(Row(repeated=HUGE), clone=False)
    tail = Row(width=3)
    tail.set_value(0, "tail")
    table.append_row(tail, clone=False)
    return table


def _records() -> list[Row]:
    rows = []
    for value in ("r1", "r2", "r3"):
        row = Row(width=3)
        row.set_value(0, value)
        rows.append(row)
    return rows


def test_physical_nodes_do_not_grow_with_repetition():
    table = _compressed_table()
    assert len(table._get_rows()) == 3
    table.insert_rows(50_000, rows=_records())
    # head + 2 split empty segments + 3 records + tail
    assert len(table._get_rows()) == 7
    assert table.height == HUGE + 5


def test_logical_coordinates_are_correct():
    table = _compressed_table()
    table.insert_rows(50_000, rows=_records())
    assert [table.get_value((0, 50_000 + i)) for i in range(3)] == [
        "r1",
        "r2",
        "r3",
    ]
    assert table.get_value((0, 0)) == "head"
    assert table.get_value((0, table.height - 1)) == "tail"
    assert table.get_value((0, 49_999)) is None
    assert table.get_value((0, 50_003)) is None


def test_cache_stays_consistent():
    table = _compressed_table()
    table.insert_rows(50_000, rows=_records())
    for y in (0, 25_000, 49_999, 50_000, 50_002, 50_003, 75_000, table.height - 1):
        assert table.get_row(y).y == y


def test_insert_at_boundaries():
    table = _compressed_table()
    table.insert_rows(0, rows=_records())
    assert [table.get_value((0, i)) for i in range(3)] == ["r1", "r2", "r3"]
    assert table.get_value((0, 3)) == "head"
    before = table.height
    table.insert_rows(table.height, amount=2)
    assert table.height == before + 2


def test_amount_argument_inserts_empty_rows():
    table = _compressed_table()
    table.insert_rows(10, amount=3)
    assert table.height == HUGE + 5
    for y in range(10, 13):
        assert table.get_row(y).is_empty(aggressive=True)


def test_invalid_arguments():
    table = _compressed_table()
    with pytest.raises(RowInsertError):
        table.insert_rows(0, amount=0)
    with pytest.raises(RowInsertError):
        table.insert_rows(0, rows=[])
    with pytest.raises(RowInsertError):
        table.insert_rows(table.height + 5)
    with pytest.raises(RowInsertError):
        table.insert_rows(0, span_policy="nope")


def test_named_ranges_are_shifted():
    doc = Document("ods")
    body = doc.body
    table = body.get_table(0)
    for row in list(table._get_rows()):
        row.delete()
    for y in range(6):
        row = Row(width=3)
        row.set_value(0, y)
        table.append_row(row, clone=False)
    body.set_named_range("RANGE_ONE", "A2:B4", table.name)
    table._local_set_named_range("RANGE_TWO", "A5")

    table.insert_rows(3, amount=2)

    assert body.get_named_range("RANGE_ONE").crange == (0, 1, 1, 5)
    assert table._local_named_range("RANGE_TWO").crange == (0, 6, 0, 6)


def test_named_range_above_insertion_is_unchanged():
    doc = Document("ods")
    body = doc.body
    table = body.get_table(0)
    for y in range(6):
        table.append_row(Row(width=2), clone=False)
    body.set_named_range("RANGE_TOP", "A1:B2", table.name)
    table.insert_rows(4, amount=2)
    assert body.get_named_range("RANGE_TOP").crange == (0, 0, 1, 1)


def test_formulas_are_rewritten():
    table = Table("Formulas")
    for y in range(8):
        table.append_row(Row(width=3), clone=False)
    table.set_value((2, 0), 0, formula="of:=SUM([.A1:.A8])")
    table.set_value((2, 1), 0, formula="of:=[.A$2]+[.A3]")
    table.set_value((2, 2), 0, formula='of:=CONCATENATE("A1";[.A5])')

    table.insert_rows(3, amount=2)

    # A1 is above the insertion point, A8 shifts to A10.
    assert table.get_cell((2, 0)).formula == "of:=SUM([.A1:.A10])"
    # Absolute row and references above the point are preserved.
    assert table.get_cell((2, 1)).formula == "of:=[.A$2]+[.A3]"
    assert table.get_cell((2, 2)).formula == 'of:=CONCATENATE("A1";[.A7])'


def test_cross_sheet_formula_is_untouched():
    doc = Document("ods")
    body = doc.body
    table = body.get_table(0)
    for y in range(40):
        table.append_row(Row(width=2), clone=False)
    other = Table("Other")
    body.append(other)
    table.set_value((0, 0), 0, formula="of:=['Other'.A10]+[.A30]")

    table.insert_rows(3, amount=2)

    assert table.get_cell((0, 0)).formula == "of:=['Other'.A10]+[.A32]"


def test_span_reject_policy():
    table = Table("Span")
    for y in range(5):
        row = Row(width=3)
        row.set_value(0, y)
        table.append_row(row, clone=False)
    table.set_span("B2:C4")
    with pytest.raises(SpanInsertionError):
        table.insert_rows(2, span_policy=SPAN_POLICY_REJECT)
    assert table.height == 5


def test_span_expand_policy():
    table = Table("Span")
    for y in range(5):
        row = Row(width=3)
        row.set_value(0, y)
        table.append_row(row, clone=False)
    table.set_span("B2:C4")

    table.insert_rows(2, amount=2, span_policy=SPAN_POLICY_EXPAND)

    anchor = table.get_cell("B2", clone=False)
    assert anchor.span_area() == (2, 5)
    for y in range(1, 6):
        cell = table.get_cell((1, y))
        assert cell.is_spanned()
    assert table.height == 7


def test_horizontal_span_allows_insertion():
    table = Table("Span")
    for y in range(4):
        row = Row(width=3)
        table.append_row(row, clone=False)
    table.set_span("A1:C1")
    table.insert_rows(1, span_policy=SPAN_POLICY_REJECT)
    assert table.height == 5


def test_insert_row_integration_with_policy():
    table = _compressed_table()
    row = Row(width=3)
    row.set_value(0, "single")
    returned = table.insert_row(50_000, row, span_policy=SPAN_POLICY_REJECT)
    assert returned.y == 50_000
    assert table.get_value((0, 50_000)) == "single"
    assert len(table._get_rows()) == 5


def test_roundtrip(tmp_path):
    doc = Document("ods")
    body = doc.body
    table = body.get_table(0)
    for row in list(table._get_rows()):
        row.delete()
    head = Row(width=2)
    head.set_value(0, "head")
    table.append_row(head, clone=False)
    table.append_row(Row(repeated=10_000), clone=False)
    table.insert_rows(5_000, rows=_records())
    path = tmp_path / "roundtrip.ods"
    doc.save(path)

    reloaded = Document(path).body.get_table(name=table.name)
    assert reloaded.height == 10_004
    assert len(reloaded._get_rows()) == 6
    assert [reloaded.get_value((0, 5_000 + i)) for i in range(3)] == [
        "r1",
        "r2",
        "r3",
    ]
