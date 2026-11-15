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
"""Tests for the paired typed value / display value extraction used when
loading heterogeneous ODS sources into a warehouse.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from odfdo.cell import Cell
from odfdo.document import Document
from odfdo.element import Element
from odfdo.row import Row
from odfdo.table import Table


def test_cell_display_value_none_when_no_paragraph():
    cell = Cell()
    assert cell.display_value is None
    assert cell.get_value_and_display() == (None, None)


def test_cell_display_value_default_text():
    cell = Cell(date(2024, 1, 31))
    value, display = cell.get_value_and_display()
    assert value == date(2024, 1, 31)
    assert display == "2024-01-31"


def test_cell_display_value_keeps_source_format():
    # Two ODS may display the same typed date with different formats
    cell = Cell(date(2024, 1, 31), text="31/01/2024")
    value, display = cell.get_value_and_display()
    assert value == date(2024, 1, 31)
    assert display == "31/01/2024"


def test_cell_display_value_multiple_paragraphs():
    cell = Element.from_tag(
        '<table:table-cell office:value-type="string" '
        'office:string-value="x">'
        "<text:p>line1</text:p><text:p>line2</text:p>"
        "</table:table-cell>"
    )
    assert cell.display_value == "line1\nline2"
    # typed value comes from office:string-value, display from the paragraphs
    assert cell.get_value_and_display() == ("x", "line1\nline2")


def test_date_looking_text_is_not_a_date():
    # A format that looks like a date does not prove the original value is one:
    # office:value-type="string" wins over the shape of the display text.
    cell = Cell("2024-13-99", cell_type="string")
    assert cell.type == "string"
    value, display = cell.get_value_and_display()
    assert value == "2024-13-99"
    assert isinstance(value, str)
    assert display == "2024-13-99"


def test_plain_number_value_and_display_are_separate():
    cell = Cell(Decimal("1234.5"), text="1 234,50")
    value, display = cell.get_value_and_display()
    assert value == Decimal("1234.5")
    assert display == "1 234,50"


def test_integer_value_and_display():
    cell = Cell(42)
    value, display = cell.get_value_and_display()
    assert value == 42
    assert isinstance(value, int)
    assert display == "42"


def test_datetime_keeps_wall_clock_without_local_timezone():
    tz = timezone(timedelta(hours=5))
    moment = datetime(2024, 1, 31, 9, 5, tzinfo=tz)
    cell = Cell(moment)
    value, display = cell.get_value_and_display()
    # No conversion to the machine local timezone
    assert value == moment
    assert value.utcoffset() == timedelta(hours=5)
    assert display == "2024-01-31T09:05:00+05:00"


def test_negative_duration_keeps_sign():
    cell = Cell(timedelta(hours=-2))
    value, display = cell.get_value_and_display()
    assert value == timedelta(hours=-2)
    assert value.total_seconds() < 0
    assert display == "-PT02H00M00S"


def test_cross_day_duration_keeps_full_length():
    cell = Cell(timedelta(days=1, hours=25, minutes=70))
    value, display = cell.get_value_and_display()
    # 1 day + 25 hours + 70 minutes == 50 h 10 min, not folded into a clock time
    assert value == timedelta(hours=50, minutes=10)
    assert value.total_seconds() == 50 * 3600 + 600
    assert display == "PT50H10M00S"


def test_negative_cross_day_duration_roundtrip():
    cell = Cell(timedelta(days=-2, hours=-3))
    value, _display = cell.get_value_and_display()
    assert value == timedelta(days=-2, hours=-3)
    assert Cell(value).get_value_and_display()[0] == value


def test_row_pairs_include_empty_cells():
    table = Table("T", width=3, height=1)
    table.set_value((0, 0), date(2024, 1, 31))
    # column 1 left empty, column 2 filled
    table.set_value((2, 0), "ok")
    row = table.get_row(0)
    pairs = row.get_values_and_display()
    assert pairs == [
        (date(2024, 1, 31), "2024-01-31"),
        (None, None),
        ("ok", "ok"),
    ]
    assert row.values_and_display == pairs
    assert len(pairs) == row.width


def test_row_pairs_coordinate_slice():
    table = Table("T", width=4, height=1)
    for index in range(4):
        table.set_value((index, 0), index)
    row = table.get_row(0)
    assert row.get_values_and_display((1, 2)) == [(1, "1"), (2, "2")]


def test_table_matrix_shape_and_order():
    table = Table("T", width=2, height=2)
    table.set_value((0, 0), "name")
    table.set_value((1, 0), "when")
    table.set_value((0, 1), "event")
    table.set_value((1, 1), date(2024, 1, 31))
    matrix = table.get_values_and_display()
    assert matrix == [
        [("name", "name"), ("when", "when")],
        [("event", "event"), (date(2024, 1, 31), "2024-01-31")],
    ]
    assert table.values_and_display == matrix
    assert list(table.iter_values_and_display()) == matrix


def test_table_pairs_coordinate_slice():
    table = Table("T", width=3, height=3)
    for y in range(3):
        for x in range(3):
            table.set_value((x, y), f"{x},{y}")
    sub = table.get_values_and_display("B1:C2")
    assert sub == [
        [("1,0", "1,0"), ("2,0", "2,0")],
        [("1,1", "1,1"), ("2,1", "2,1")],
    ]


def test_different_ods_baselines_share_typed_value_not_display():
    # Same calendar day, two source applications with different date baselines
    # and display formats: the warehouse keeps one real typed column and one
    # display column per source.
    ods_a = Cell(date(2024, 1, 31), text="2024/01/31")
    ods_b = Cell(date(2024, 1, 31), text="31-Jan-24")
    typed_a, display_a = ods_a.get_value_and_display()
    typed_b, display_b = ods_b.get_value_and_display()
    assert typed_a == typed_b == date(2024, 1, 31)
    assert display_a != display_b
    assert display_a == "2024/01/31"
    assert display_b == "31-Jan-24"


def test_display_text_does_not_change_type_on_reload(tmp_path):
    doc = Document("spreadsheet")
    table = doc.body.tables[0]
    table.name = "Warehouse"
    table.set_value((0, 0), "d")
    table.set_value((1, 0), "elapsed")
    table.set_value((2, 0), "amount")
    table.set_cell((0, 1), Cell(date(2024, 1, 31), text="31/01/2024"))
    table.set_cell((1, 1), Cell(timedelta(hours=-2), text="-02:00:00"))
    table.set_cell((2, 1), Cell(Decimal("0.5"), cell_type="percentage", text="50 %"))
    target = tmp_path / "warehouse.ods"
    doc.save(target)

    reloaded = Document(target).body.get_table(name="Warehouse")
    pairs = reloaded.get_values_and_display()[1]
    assert pairs[0] == (date(2024, 1, 31), "31/01/2024")
    assert reloaded.get_cell((0, 1)).type == "date"
    assert pairs[1] == (timedelta(hours=-2), "-02:00:00")
    assert reloaded.get_cell((1, 1)).type == "time"
    assert pairs[2] == (Decimal("0.5"), "50 %")
    assert reloaded.get_cell((2, 1)).type == "percentage"


def test_row_builder_accepts_pairs_for_warehouse_columns():
    # Sanity check that a row exposes enough cells to fan out into two columns.
    row = Row()
    row.set_values([date(2024, 1, 31), timedelta(hours=-2), 7])
    pairs = row.values_and_display
    assert [value for value, _ in pairs] == [
        date(2024, 1, 31),
        timedelta(hours=-2),
        7,
    ]
    assert [display for _, display in pairs] == [
        "2024-01-31",
        "-PT02H00M00S",
        "7",
    ]
