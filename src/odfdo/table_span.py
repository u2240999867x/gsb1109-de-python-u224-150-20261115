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
"""Export helpers for spanned (merged) table cells.

A spanned area is made of one anchor cell (the "master" cell, carrying the
span attributes and the actual value) and one or more covered cells (the
"table:covered-table-cell" elements hidden behind the anchor).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .utils import digit_to_alpha

if TYPE_CHECKING:
    from .const import CellValue
    from .table import Table


@dataclass(frozen=True, slots=True)
class SpanInfo:
    """Description of one spanned (merged) area of a table.

    Attributes:
        anchor: 0-based (column, row) coordinates of the anchor cell, the
            only cell of the area holding the actual value.
        end: 0-based (column, row) coordinates of the bottom-right cell of
            the spanned area.
        covered: tuple of 0-based (column, row) coordinates of the covered
            cells hidden behind the anchor.
        value: actual Python value of the anchor cell.

    """

    anchor: tuple[int, int]
    end: tuple[int, int]
    covered: tuple[tuple[int, int], ...]
    value: CellValue | None

    @property
    def cols(self) -> int:
        """Number of columns covered by the spanned area."""
        return self.end[0] - self.anchor[0] + 1

    @property
    def rows(self) -> int:
        """Number of rows covered by the spanned area."""
        return self.end[1] - self.anchor[1] + 1

    @property
    def anchor_coord(self) -> str:
        """Spreadsheet coordinates of the anchor cell, e.g. "B3"."""
        return _coord_to_str(self.anchor)

    @property
    def area_coord(self) -> str:
        """Spreadsheet coordinates of the full area, e.g. "B3:C5"."""
        return f"{_coord_to_str(self.anchor)}:{_coord_to_str(self.end)}"

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation of the spanned area."""
        return {
            "anchor": self.anchor,
            "anchor_coord": self.anchor_coord,
            "end": self.end,
            "area_coord": self.area_coord,
            "covered": list(self.covered),
            "value": self.value,
        }


def _coord_to_str(coord: tuple[int, int]) -> str:
    x, y = coord
    return f"{digit_to_alpha(x)}{y + 1}"


def scan_spans(
    table: Table,
) -> tuple[list[SpanInfo], dict[tuple[int, int], tuple[int, int]]]:
    """Scan a table and index its spanned areas.

    Args:
        table: The table to scan.

    Returns:
        A tuple of:
            - the list of `SpanInfo` describing each spanned area,
            - a mapping of every covered cell coordinates to the
              coordinates of its anchor cell.

    """
    anchors: list[SpanInfo] = []
    covered_map: dict[tuple[int, int], tuple[int, int]] = {}
    for row in table.iter_rows():
        ay = row.y
        if ay is None:  # pragma: no cover
            continue
        for cell in row.iter_cells():
            ax = cell.x
            if ax is None or cell.is_covered():
                continue
            raw_cols, raw_rows = cell.span_area()
            # ODF defaults a missing span dimension to one cell
            nb_cols = max(raw_cols, 1)
            nb_rows = max(raw_rows, 1)
            if nb_cols < 2 and nb_rows < 2:
                continue
            anchor = (ax, ay)
            end = (ax + nb_cols - 1, ay + nb_rows - 1)
            covered = tuple(
                (cx, cy)
                for cy in range(ay, ay + nb_rows)
                for cx in range(ax, ax + nb_cols)
                if (cx, cy) != anchor
            )
            for position in covered:
                covered_map[position] = anchor
            anchors.append(
                SpanInfo(
                    anchor=anchor,
                    end=end,
                    covered=covered,
                    value=cell.get_value(),
                )
            )
    return anchors, covered_map
