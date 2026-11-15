# Copyright 2018-2026 Jérôme Dumonteil
# Copyright (c) 2009-2010 Ars Aperta, Itaapy, Pierlis, Talend.
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
#
#
# Authors (odfdo project): jerome.dumonteil@gmail.com
# The odfdo project is a derivative work of the lpod-python project:
# https://github.com/lpod/lpod-python
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from odfdo import SpanInfo, Table
from odfdo.document import Document

if TYPE_CHECKING:
    from collections.abc import Iterable


@pytest.fixture
def table(samples) -> Iterable[Table]:
    # spanned_cells.ods, 10 columns x 6 rows:
    #   B3:C5 anchored at B3, value "foo"
    #   G3:J3 anchored at G3, value "horiz"
    #   E4:E6 anchored at E4, value "vert"
    document = Document(samples("spanned_cells.ods"))
    yield document.body.tables[0]


@pytest.fixture
def report() -> Table:
    table = Table("report", width=3, height=5)
    table.set_values(
        [
            ["项目", "月份", "金额"],
            ["阿尔法", "1月", 100],
            [None, "2月", 200],
            ["贝塔", "1月", 50],
            [None, "2月", 75],
        ]
    )
    table.set_span("A2:A3")
    table.set_span("A4:A5")
    return table


def test_get_spans_anchors(table):
    spans = table.get_spans()
    assert len(spans) == 3
    assert all(isinstance(span, SpanInfo) for span in spans)
    by_coord = {span.anchor_coord: span for span in spans}
    foo = by_coord["B3"]
    assert foo.anchor == (1, 2)
    assert foo.end == (2, 4)
    assert foo.area_coord == "B3:C5"
    assert foo.cols == 2
    assert foo.rows == 3
    assert foo.value == "foo"
    horiz = by_coord["G3"]
    assert horiz.area_coord == "G3:J3"
    assert (horiz.cols, horiz.rows) == (4, 1)
    assert horiz.value == "horiz"
    vert = by_coord["E4"]
    assert vert.area_coord == "E4:E6"
    assert (vert.cols, vert.rows) == (1, 3)
    assert vert.value == "vert"


def test_get_spans_covered_cells(table):
    spans = {span.anchor: span for span in table.get_spans()}
    foo = spans[(1, 2)]
    assert foo.covered == (
        (2, 2),
        (1, 3),
        (2, 3),
        (1, 4),
        (2, 4),
    )
    horiz = spans[(6, 2)]
    assert horiz.covered == ((7, 2), (8, 2), (9, 2))
    vert = spans[(4, 3)]
    assert vert.covered == ((4, 4), (4, 5))


def test_get_spans_to_dict(table):
    span = table.get_spans()[0]
    data = span.to_dict()
    assert data == {
        "anchor": (1, 2),
        "anchor_coord": "B3",
        "end": (2, 4),
        "area_coord": "B3:C5",
        "covered": [(2, 2), (1, 3), (2, 3), (1, 4), (2, 4)],
        "value": "foo",
    }


def test_span_info_is_frozen():
    info = SpanInfo(anchor=(0, 0), end=(1, 1), covered=((1, 0),), value="x")
    with pytest.raises((AttributeError, Exception)):
        info.value = "y"  # type: ignore[misc]


def test_get_span_coverage(table):
    coverage = table.get_span_coverage()
    # only covered cells are keys, anchors are excluded
    assert (1, 2) not in coverage
    assert coverage[(2, 2)] == (1, 2)
    assert coverage[(1, 3)] == (1, 2)
    assert coverage[(2, 4)] == (1, 2)
    assert coverage[(9, 2)] == (6, 2)
    assert coverage[(4, 4)] == (4, 3)
    assert coverage[(4, 5)] == (4, 3)
    assert len(coverage) == 10


def test_get_span_anchor_from_covered_cell(table):
    assert table.get_span_anchor("C4") == (1, 2)
    assert table.get_span_anchor((2, 3)) == (1, 2)
    assert table.get_span_anchor("J3") == (6, 2)
    assert table.get_span_anchor("E6") == (4, 3)


def test_get_span_anchor_from_anchor_cell(table):
    assert table.get_span_anchor("B3") == (1, 2)
    assert table.get_span_anchor("G3") == (6, 2)
    assert table.get_span_anchor("E4") == (4, 3)


def test_get_span_anchor_plain_cell(table):
    assert table.get_span_anchor("A1") is None
    assert table.get_span_anchor("K20") is None


def test_get_spanned_values_default_is_master_only(table):
    values = table.get_spanned_values()
    # covered cells resolve to None, anchors keep their single value
    assert values[2][1] == "foo"
    assert values[2][2] is None
    assert values[3][1] is None
    assert values[2][6] == "horiz"
    assert values[2][7] is None
    assert values[3][4] == "vert"
    assert values[4][4] is None


def test_get_spanned_values_default_matches_get_values(table):
    assert table.get_spanned_values() == table.get_values()


def test_get_spanned_values_fill(table):
    values = table.get_spanned_values(fill=True)
    # the whole B3:C5 area displays the anchor value
    for row in range(2, 5):
        for col in range(1, 3):
            assert values[row][col] == "foo"
    for col in range(6, 10):
        assert values[2][col] == "horiz"
    for row in range(3, 6):
        assert values[row][4] == "vert"
    # unrelated cells stay empty
    assert values[0][0] is None


def test_get_spanned_values_partial_area_only_covered(table):
    # selection cuts only through covered cells, the anchor B3 is outside
    values = table.get_spanned_values("C4:C5")
    assert values == [[None], [None]]
    filled = table.get_spanned_values("C4:C5", fill=True)
    assert filled == [["foo"], ["foo"]]
    # the original anchor remains locatable from the covered selection
    anchor = table.get_span_anchor("C4")
    assert anchor == (1, 2)
    assert table.get_value(anchor) == "foo"


def test_get_spanned_values_partial_area_horizontal(table):
    filled = table.get_spanned_values("H3:I3", fill=True)
    assert filled == [["horiz", "horiz"]]


def test_default_export_counts_each_value_once(table):
    values = table.get_spanned_values()
    flat = [value for row in values for value in row if value is not None]
    assert sorted(flat) == ["foo", "horiz", "vert"]


def test_filled_export_is_display_duplicate(table):
    default = table.get_spanned_values()
    filled = table.get_spanned_values(fill=True)
    default_count = sum(value is not None for row in default for value in row)
    filled_count = sum(value is not None for row in filled for value in row)
    assert default_count == 3
    assert filled_count == 13


def test_report_master_only(report):
    values = report.get_spanned_values()
    assert values == [
        ["项目", "月份", "金额"],
        ["阿尔法", "1月", 100],
        [None, "2月", 200],
        ["贝塔", "1月", 50],
        [None, "2月", 75],
    ]
    spans = report.get_spans()
    assert [span.anchor_coord for span in spans] == ["A2", "A4"]
    assert [span.value for span in spans] == ["阿尔法", "贝塔"]


def test_report_fill(report):
    values = report.get_spanned_values(fill=True)
    assert values == [
        ["项目", "月份", "金额"],
        ["阿尔法", "1月", 100],
        ["阿尔法", "2月", 200],
        ["贝塔", "1月", 50],
        ["贝塔", "2月", 75],
    ]


def test_report_amount_column_independent_and_sum_safe(report):
    values = report.get_spanned_values()
    amounts = [row[2] for row in values[1:]]
    assert amounts == [100, 200, 50, 75]
    assert sum(amounts) == 425


def test_report_locate_master_from_covered(report):
    assert report.get_span_anchor("A3") == (0, 1)
    assert report.get_value(report.get_span_anchor("A3")) == "阿尔法"
    assert report.get_span_anchor("A5") == (0, 3)


def test_report_coverage(report):
    coverage = report.get_span_coverage()
    assert coverage == {(0, 2): (0, 1), (0, 4): (0, 3)}


def test_no_span_table():
    table = Table("plain", width=2, height=2)
    table.set_values([["a", 1], ["b", 2]])
    assert table.get_spans() == []
    assert table.get_span_coverage() == {}
    assert table.get_span_anchor("A1") is None
    assert table.get_spanned_values() == [["a", 1], ["b", 2]]
    assert table.get_spanned_values(fill=True) == [["a", 1], ["b", 2]]


def test_empty_table():
    table = Table("empty")
    assert table.get_spans() == []
    assert table.get_span_coverage() == {}
    assert table.get_span_anchor("A1") is None
    assert table.get_spanned_values(fill=True) == []


def test_span_with_missing_row_dimension():
    # ODF allows a single span dimension attribute; the missing one
    # defaults to one cell.
    table = Table("malformed", width=3, height=1)
    table.set_value("A1", "x")
    cell = table.get_cell("A1", clone=False)
    cell.set_attribute("table:number-columns-spanned", 3)
    span = table.get_spans()[0]
    assert span.anchor == (0, 0)
    assert span.end == (2, 0)
    assert (span.cols, span.rows) == (3, 1)
    assert span.covered == ((1, 0), (2, 0))
    assert table.get_spanned_values(fill=True) == [["x", "x", "x"]]
