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
"""Tests for the formula cache: references, dependencies and staleness."""

from __future__ import annotations

from decimal import Decimal

import pytest

from odfdo import Document, Table
from odfdo.formula import (
    FormulaCache,
    FormulaError,
    FormulaReference,
    extract_references,
    formula_functions,
    parse_formula,
)


@pytest.fixture
def document() -> Document:
    document = Document("spreadsheet")
    document.body.clear()
    return document


@pytest.fixture
def sheet(document: Document) -> Table:
    table = Table("Sheet1", width=4, height=4)
    document.body.append(table)
    return table


# ---------------------------------------------------------------------------
# Parsing and references
# ---------------------------------------------------------------------------


def test_extract_single_reference():
    refs = extract_references("of:=[.A1]")
    assert refs == [FormulaReference(0, 0)]


def test_extract_range_reference():
    refs = extract_references("of:=SUM([.A1:.B3])")
    assert len(refs) == 1
    assert (refs[0].x, refs[0].y, refs[0].z, refs[0].t) == (0, 0, 1, 2)


def test_extract_absolute_reference():
    ref = extract_references("of:=[.$B$2]")[0]
    assert (ref.x, ref.y) == (1, 1)
    assert ref.absolute_column is True
    assert ref.absolute_row is True
    assert ref.is_absolute is True
    assert ref.is_relative is False


def test_extract_relative_reference():
    ref = extract_references("of:=[.B2]")[0]
    assert ref.is_absolute is False
    assert ref.is_relative is True


def test_extract_mixed_absolute_reference():
    ref = extract_references("of:=[.$B2]")[0]
    assert ref.is_absolute is False
    assert ref.is_relative is False


def test_extract_cross_sheet_reference():
    ref = extract_references("of:=[Other.$A$1]")[0]
    assert ref.sheet == "Other"
    assert ref.is_cross_sheet is True
    assert ref.address == "$A$1"
    assert ref.qualified_address == "Other.$A$1"


def test_extract_quoted_sheet_reference():
    ref = extract_references("of:=[.'My Sheet'.$A$1]")[0]
    assert ref.sheet == "My Sheet"
    assert ref.qualified_address == "'My Sheet'.$A$1"


def test_extract_malformed_reference():
    with pytest.raises(FormulaError):
        extract_references("of:=[.1A]")


def test_formula_functions_masks_references():
    assert formula_functions("of:=SUM([.A1])+NOSUCH([.B1])") == ["SUM", "NOSUCH"]


def test_parse_precedence():
    node = parse_formula("of:=1+2*3")
    assert node is not None
    assert node.operator == "+"
    assert node.right.operator == "*"


# ---------------------------------------------------------------------------
# Dependency tracking
# ---------------------------------------------------------------------------


def test_formula_cache_direct_dependency():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 2, 2, "of:=[.A1]+[.B1]", 3)
    assert [cell.key for cell in cache.dependents("Sheet1", 0, 0)] == [
        ("Sheet1", 2, 2)
    ]


def test_formula_cache_range_dependency():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 3, 3, "of:=SUM([.A1:.B2])", 6)
    for x, y in ((0, 0), (1, 0), (0, 1), (1, 1)):
        assert cache.dependents("Sheet1", x, y), (x, y)


def test_formula_cache_cross_sheet_dependency():
    cache = FormulaCache()
    cache.set_formula("Sheet2", 0, 0, "of:=[Sheet1.$A$1]*2", 4)
    assert cache.dependents("Sheet1", 0, 0)
    assert cache.dependents("Sheet2", 0, 0) == []


def test_formula_cache_indirect_dependency():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 1, 0, "of:=[.A1]*2", 2)
    cache.set_formula("Sheet1", 2, 0, "of:=[.B1]+1", 3)
    indirect = cache.dependents("Sheet1", 0, 0, recursive=True)
    assert {cell.key for cell in indirect} == {("Sheet1", 1, 0), ("Sheet1", 2, 0)}


def test_invalidation_marks_indirect_dependents():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 1, 0, "of:=[.A1]*2", 2)
    cache.set_formula("Sheet1", 2, 0, "of:=[.B1]+1", 3)
    changed = cache.invalidate_cell("Sheet1", 0, 0)
    assert {cell.key for cell in changed} == {("Sheet1", 1, 0), ("Sheet1", 2, 0)}
    assert all(cell.stale for cell in changed)


def test_formula_unchanged_becomes_stale_when_dependency_changes(sheet):
    sheet.set_value("A1", 1)
    sheet.set_value("B1", 2, formula="of:=[.A1]*2")
    sheet.recalculate()
    assert sheet.get_cell("B1").formula_is_stale is False
    sheet.set_value("A1", 5)
    assert sheet.get_cell("B1").formula_is_stale is True
    assert sheet.get_cell("B1").formula_stale_reason == "cell changed"
    # The formula text itself did not change.
    assert sheet.get_cell("B1").formula == "of:=[.A1]*2"


# ---------------------------------------------------------------------------
# Table / document integration
# ---------------------------------------------------------------------------


def test_table_set_value_formula_is_fresh(sheet):
    sheet.set_value("A1", 3, formula="of:=SUM([.B1:.B3])")
    assert sheet.get_cell("A1").formula_is_stale is False


def test_table_recalculate_updates_cache(sheet):
    sheet.set_value("B1", 1)
    sheet.set_value("B2", 2)
    sheet.set_value("A1", 3, formula="of:=SUM([.B1:.B3])")
    sheet.recalculate()
    sheet.set_value("B2", 20)
    assert sheet.get_cell("A1").formula_is_stale is True
    refreshed = sheet.recalculate()
    assert [cell.key for cell in refreshed] == [("Sheet1", 0, 0)]
    assert sheet.get_value("A1") == 21
    assert sheet.get_cell("A1").formula == "of:=SUM([.B1:.B3])"
    assert sheet.get_cell("A1").formula_is_stale is False


def test_document_recalculate_cross_sheet(document):
    first = Table("First", width=3, height=3)
    second = Table("Second", width=3, height=3)
    document.body.append(first)
    document.body.append(second)
    first.set_value("B1", 1)
    first.set_value("A1", 3, formula="of:=[.B1]*3")
    second.set_value("A1", 4, formula="of:=[First.A1]+1")
    assert document.stale_formula_cells() == []
    first.set_value("B1", 2)
    assert document.stale_formula_cells()
    document.recalculate()
    assert first.get_value("A1") == 6
    assert second.get_value("A1") == 7


def test_unknown_function_never_guessed(sheet):
    sheet.set_value("B1", 1)
    sheet.set_value("A1", 99, formula="of:=MYSTERY([.B1])")
    sheet.recalculate()
    sheet.set_value("B1", 2)
    refreshed = sheet.recalculate()
    assert refreshed == []
    assert sheet.get_value("A1") == 99
    assert sheet.get_cell("A1").formula_is_stale is True


def test_division_by_zero_not_guessed(sheet):
    sheet.set_value("A1", 7, formula="of:=[.B1]/[.C1]")
    sheet.set_value("B1", 1)
    sheet.set_value("C1", 0)
    sheet.get_cell("A1").mark_formula_stale()
    assert sheet.recalculate() == []
    assert sheet.get_value("A1") == 7


def test_structure_change_invalidates_all(sheet):
    sheet.set_value("B1", 1)
    sheet.set_value("A1", 1, formula="of:=SUM([.B1:.B3])")
    assert sheet.get_cell("A1").formula_is_stale is False
    sheet.insert_row(1)
    assert sheet.get_cell("A1").formula_is_stale is True
    assert sheet.get_cell("A1").formula_stale_reason == "structure changed"


def test_values_roundtrip_preserves_formula(sheet):
    sheet.set_value("A1", 5, formula="of:=[.B1]+1")
    sheet.set_value("A1", 8)
    assert sheet.get_cell("A1").formula == "of:=[.B1]+1"
    assert sheet.get_value("A1") == 8
    assert sheet.get_cell("A1").formula_is_stale is True


def test_formula_dependents_of_plain_cell(sheet):
    sheet.set_value("B1", 1)
    sheet.set_value("A1", 2, formula="of:=[.B1]*2")
    dependents = sheet.get_cell("B1").formula_dependents()
    assert {dep.key for dep in dependents} == {("Sheet1", 0, 0)}


def test_evaluator_supports_common_functions():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 0, 0, "of:=SUM([.B1:.B3])", 0)
    cache.set_formula("Sheet1", 0, 1, "of:=IF([.B1]>0;10;20)", 0)
    values = {("Sheet1", 1, 0): 1, ("Sheet1", 1, 1): 2, ("Sheet1", 1, 2): 3}

    def getter(table: str | None, x: int, y: int):
        return values.get((table, x, y))

    assert cache.evaluate("Sheet1", 0, 0, getter).value == Decimal(6)
    assert cache.evaluate("Sheet1", 0, 1, getter).value == 10


def test_evaluate_unknown_function_returns_unsupported():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 0, 0, "of:=WHAT([.A1])", 5)
    result = cache.evaluate("Sheet1", 0, 0)
    assert result.ok is False
    assert result.supported is False
    assert result.error == "unsupported"


def test_evaluate_circular_reference():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 0, 0, "of:=[.A1]+1", 1, fresh=False)
    result = cache.evaluate("Sheet1", 0, 0)
    assert result.error == "#CIRC!"


def test_evaluate_stale_precedent():
    cache = FormulaCache()
    cache.set_formula("Sheet1", 1, 0, "of:=[.A1]*2", 2)
    cache.set_formula("Sheet1", 2, 0, "of:=[.B1]+1", 3)
    cache.invalidate_cell("Sheet1", 0, 0)
    result = cache.evaluate("Sheet1", 2, 0)
    assert result.error == "#STALE!"


def test_formula_removal_invalidates_dependents(sheet):
    sheet.set_value("B1", 1)
    sheet.set_value("A1", 2, formula="of:=[.B1]+1")
    sheet.set_value("C1", 3, formula="of:=[.A1]+1")
    sheet.get_cell("A1", clone=False).formula = None
    assert sheet.get_cell("C1").formula_is_stale is True
    assert sheet.get_cell("C1").formula_stale_reason == "formula removed"


def test_direct_cell_mutation_invalidates(sheet):
    sheet.set_value("B1", 1)
    sheet.set_value("A1", 2, formula="of:=[.B1]*2")
    sheet.get_cell("B1", clone=False).int = 7
    assert sheet.get_cell("A1").formula_is_stale is True
    sheet.recalculate()
    assert sheet.get_value("A1") == 14


def test_repeated_direct_mutation_keeps_stale(sheet):
    sheet.set_value("B1", 1)
    sheet.set_value("A1", 2, formula="of:=[.B1]*2")
    sheet.get_cell("B1", clone=False).int = 7
    sheet.get_cell("B1", clone=False).int = 8
    assert sheet.get_cell("A1").formula_is_stale is True
    sheet.recalculate()
    assert sheet.get_value("A1") == 16


def test_absolute_reference_reported_separately(sheet):
    sheet.set_value("A1", 2, formula="of:=[.$B$1]+[.C1]")
    cache = sheet.formula_cache
    entry = cache.get_formula("Sheet1", 0, 0)
    assert entry is not None
    assert [ref.address for ref in entry.absolute_references] == ["$B$1"]
    assert [ref.address for ref in entry.relative_references] == ["C1"]


def test_unparsable_formula_is_never_trusted(sheet):
    sheet.set_value("A1", 5, formula="of:=[.B1")
    assert sheet.get_cell("A1").formula_is_stale is True
    assert sheet.recalculate() == []
    assert sheet.get_value("A1") == 5


def test_circular_reference_not_computed(sheet):
    sheet.set_value("B1", 2, formula="of:=[.A1]+1")
    sheet.set_value("A1", 1, formula="of:=[.B1]+1")
    sheet.recalculate()
    # Neither formula is guessed: both cached values are preserved.
    assert sheet.get_value("A1") == 1
    assert sheet.get_value("B1") == 2
    result = sheet.formula_cache.evaluate("Sheet1", 0, 0)
    assert result.ok is False
    assert result.error in {"#CIRC!", "#STALE!"}
