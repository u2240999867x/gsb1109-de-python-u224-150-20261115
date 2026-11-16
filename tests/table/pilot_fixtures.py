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
"""Helpers building ODS documents with ``table:data-pilot-table`` definitions.

LibreOffice is not scriptable in the test environment, so the pivot XML is
assembled directly with odfdo elements while the cached pivot output is
written as ordinary cells, exactly like a real spreadsheet stores it.
"""

from __future__ import annotations

from typing import Any

from odfdo import Document
from odfdo.data_pilot import (
    ORIENTATION_COLUMN,
    ORIENTATION_DATA,
    ORIENTATION_HIDDEN,
    ORIENTATION_PAGE,
    ORIENTATION_ROW,
    DataPilotField,
    DataPilotLevel,
    DataPilotMember,
    DataPilotTable,
    DataPilotTables,
    format_address,
)
from odfdo.element import Element
from odfdo.table import Table


def _address(
    sheet: str,
    x1: int,
    y1: int,
    x2: int | None = None,
    y2: int | None = None,
) -> str:
    start = format_address(sheet, x1, y1)
    if x2 is None or y2 is None:
        return start
    end = format_address(sheet, x2, y2)
    return f"{start}:{end}"


def _make_field(
    name: str,
    orientation: str,
    function: str | None = None,
    selected_page: str | None = None,
    hidden_members: list[str] | None = None,
    subtotals: list[str] | None = None,
) -> DataPilotField:
    field = DataPilotField()
    field.set_attribute("table:source-field-name", name)
    field.set_attribute("table:orientation", orientation)
    if function:
        field.set_attribute("table:function", function)
    if selected_page is not None:
        field.set_attribute("table:selected-page", selected_page)
    level = DataPilotLevel()
    for function_name in subtotals or []:
        subtotal = Element.from_tag("table:data-pilot-subtotal")
        subtotal.set_attribute("table:function", function_name)
        level.append(subtotal)
    if hidden_members:
        members = Element.from_tag("table:data-pilot-members")
        for member_name in hidden_members:
            member = DataPilotMember()
            member.set_attribute("table:name", member_name)
            member.set_attribute("table:display", False)
            members.append(member)
        level.append(members)
    field.append(level)
    return field


def build_pilot_document(
    source_rows: list[list[Any]],
    fields: list[dict[str, Any]],
    cached_grid: list[list[Any]],
    *,
    source_sheet: str = "Data",
    target_sheet: str = "Pivot",
    pilot_name: str = "Pivot1",
    target_origin: tuple[int, int] = (0, 0),
    source_range: tuple[int, int, int, int] | None = None,
    target_range: tuple[int, int, int, int] | None = None,
    row_grand_total: bool = True,
    column_grand_total: bool = True,
) -> Document:
    """Build a spreadsheet containing source detail and a cached pivot table.

    Args:
        source_rows: Full source matrix including the header row.
        fields: Pivot field dicts with keys ``name``, ``orientation``,
            optional ``function``, ``selected_page``, ``hidden_members``
            and ``subtotals``.
        cached_grid: Matrix written into the target sheet at
            ``target_origin``; it represents the cached pivot output.
    """
    document = Document("spreadsheet")
    source_table = document.body.get_sheet(0)
    source_table.name = source_sheet
    source_table.set_values(source_rows, coord="A1")

    target_width = max(1, len(cached_grid[0]) if cached_grid else 1)
    target_table = Table(target_sheet, width=target_width)
    document.body.append(target_table)
    origin_x, origin_y = target_origin
    for row_index, row in enumerate(cached_grid):
        for column_index, value in enumerate(row):
            if value is None:
                continue
            target_table.set_value(
                (origin_x + column_index, origin_y + row_index), value
            )

    if source_range is None:
        source_range = (
            0,
            0,
            max(0, len(source_rows[0]) - 1),
            max(0, len(source_rows) - 1),
        )
    if target_range is None:
        target_range = (
            origin_x,
            origin_y,
            origin_x + max(0, len(cached_grid[0]) - 1),
            origin_y + max(0, len(cached_grid) - 1),
        )

    pilot = DataPilotTable()
    pilot.set_attribute("table:name", pilot_name)
    pilot.set_attribute(
        "table:application-data",
        f"oooc:{pilot_name}:1234567890",
    )
    pilot.set_attribute(
        "table:source-cell-range-address",
        _address(source_sheet, *source_range),
    )
    pilot.set_attribute(
        "table:target-range-address",
        _address(target_sheet, *target_range),
    )
    pilot.set_attribute("table:row-grand-total", row_grand_total)
    pilot.set_attribute("table:column-grand-total", column_grand_total)
    pilot.set_attribute("table:buttons", "0")

    for field_spec in fields:
        pilot.append(
            _make_field(
                name=field_spec["name"],
                orientation=field_spec["orientation"],
                function=field_spec.get("function"),
                selected_page=field_spec.get("selected_page"),
                hidden_members=field_spec.get("hidden_members"),
                subtotals=field_spec.get("subtotals"),
            )
        )

    container = DataPilotTables()
    container.append(pilot)
    target_table.append(container)
    return document


ORIENTATIONS = {
    "row": ORIENTATION_ROW,
    "column": ORIENTATION_COLUMN,
    "data": ORIENTATION_DATA,
    "page": ORIENTATION_PAGE,
    "hidden": ORIENTATION_HIDDEN,
}
