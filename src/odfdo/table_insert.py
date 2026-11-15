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
"""Compression aware insertion of rows in a Table.

A spreadsheet may store a huge number of empty rows as a single
``table:table-row`` node carrying a ``table:number-rows-repeated``
attribute. Inserting records in the middle of such a compressed segment
must not expand it: the repetition is split in place so the number of
physical XML nodes stays independent of the original repetition count.

The engine also keeps the surrounding model consistent:

* named ranges pointing at the table are shifted,
* formulas referencing the shifted rows are rewritten,
* an insertion landing on a vertically merged area is either rejected or
  used to extend the merge depending on the chosen policy.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .cell import Cell
from .row import Row

if TYPE_CHECKING:
    from .named_range import NamedRange
    from .table import Table

#: Insertion is refused when the insertion point belongs to a vertical span.
SPAN_POLICY_REJECT = "reject"
#: A vertical span covering the insertion point is extended by the number of
#: inserted rows.
SPAN_POLICY_EXPAND = "expand"

SPAN_POLICIES = (SPAN_POLICY_REJECT, SPAN_POLICY_EXPAND)

ROWS_SPANNED = "table:number-rows-spanned"
COLUMNS_SPANNED = "table:number-columns-spanned"
FORMULA_ATTR = "table:formula"

_FORMULA_PREFIX_RE = re.compile(r"^\s*(?:of|oooc|ooow|ooc)\s*:", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r'"(?:[^"]|"")*"'  # quoted string, left untouched
    r"|\[(?P<bracket>[^\]]*)\]"  # bracket reference
    r"|(?<![A-Za-z0-9_$])(?P<plain>\$?[A-Za-z]{1,3}\$?\d{1,7})(?![0-9])"
)
_REF_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<coldol>\$?)(?P<col>[A-Za-z]{1,3})"
    r"(?P<rowdol>\$?)(?P<row>\d+)$"
)


class RowInsertError(ValueError):
    """Base error for compression aware row insertion."""


class SpanInsertionError(RowInsertError):
    """Raised when an insertion point conflicts with a merged cell area."""


def _sheet_from_prefix(prefix: str) -> str:
    sheet = prefix.lstrip("[").rstrip(".")
    if sheet.startswith("$"):
        sheet = sheet[1:]
    if len(sheet) >= 2 and sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1]
    return sheet


def _shift_ref_text(
    ref: str,
    y: int,
    amount: int,
    current_table: str,
) -> str:
    match = _REF_RE.match(ref)
    if match is None:
        return ref
    parts = match.groupdict()
    if parts["rowdol"]:
        # Absolute row reference, never shifted.
        return ref
    if parts["prefix"] in ("", "."):
        target_table = current_table
    else:
        target_table = _sheet_from_prefix(parts["prefix"])
    # Rows are inserted in the current table only: references to other sheets
    # keep their coordinates whatever the row number.
    if target_table != current_table:
        return ref
    # Rows are 1-based in formulas, the insertion point is 0-based.
    if int(parts["row"]) - 1 < y:
        return ref
    parts["row"] = str(int(parts["row"]) + amount)
    return (
        parts["prefix"]
        + parts["coldol"]
        + parts["col"]
        + parts["rowdol"]
        + parts["row"]
    )


def _shift_formula(
    formula: str,
    y: int,
    amount: int,
    current_table: str,
) -> str:
    """Shift the row component of the relative references of a formula."""
    if not formula or not _FORMULA_PREFIX_RE.match(formula):
        return formula

    def repl(match: re.Match[str]) -> str:
        bracket = match.group("bracket")
        if bracket is not None:
            shifted = [
                _shift_ref_text(part, y, amount, current_table)
                for part in bracket.split(":")
            ]
            return "[" + ":".join(shifted) + "]"
        plain = match.group("plain")
        if plain is None:
            return match.group(0)
        return _shift_ref_text(plain, y, amount, current_table)

    return _TOKEN_RE.sub(repl, formula)


def _shift_named_range(
    named_range: NamedRange,
    table_name: str,
    y: int,
    amount: int,
) -> None:
    """Move a named range over the inserted rows when it belongs to the table."""
    if named_range.table_name != table_name:
        return
    x0, y0, x1, y1 = named_range.crange

    def move(row: int) -> int:
        return row + amount if row >= y else row

    new_y0 = move(y0)
    new_y1 = move(y1)
    if (new_y0, new_y1) == (y0, y1):
        return
    named_range.set_range((x0, new_y0, x1, new_y1))


def _named_ranges_to_shift(table: Table) -> list[NamedRange]:
    named_ranges: list[NamedRange] = list(table._local_named_ranges())
    body = table.document_body
    if body is not None and body.allow_named_range:
        named_ranges.extend(body.get_named_ranges())
    return named_ranges


def _formula_cells(table: Table):
    """Yield the physical cells of the table carrying a formula."""
    for row in table._get_rows():
        for cell in row._get_cells():
            if cell.get_attribute(FORMULA_ATTR) is not None:
                yield cell


def _vertical_spans(table: Table) -> list[tuple[int, int, int, int, Cell]]:
    """List the vertical merged areas as ``(y0, y1, x0, x1, anchor)``."""
    spans: list[tuple[int, int, int, Cell]] = []
    row_map = table._table_cache.row_map
    for idx, row in enumerate(table._get_rows()):
        anchor_y = (row_map[idx - 1] + 1) if idx else 0
        x = 0
        for cell in row._get_cells():
            nrows = cell.get_attribute_integer(ROWS_SPANNED)
            if nrows and nrows > 1:
                ncols = cell.get_attribute_integer(COLUMNS_SPANNED) or 1
                spans.append((anchor_y, anchor_y + nrows - 1, x, x + ncols - 1, cell))
            x += cell.repeated or 1
    return spans


def _span_at(table: Table, y: int) -> tuple[int, int, int, int, Cell] | None:
    for span in _vertical_spans(table):
        y0, y1, _x0, _x1, _cell = span
        if y0 <= y <= y1:
            return span
    return None


def _check_span_policy(table: Table, y: int, policy: str) -> None:
    if policy not in SPAN_POLICIES:
        msg = f"Unknown span policy: {policy!r}"
        raise RowInsertError(msg)
    if _span_at(table, y) is not None and policy == SPAN_POLICY_REJECT:
        msg = (
            f"Can not insert rows at row {y}: the position is inside a merged cell area"
        )
        raise SpanInsertionError(msg)


def _extend_vertical_span(table: Table, y: int, amount: int) -> tuple[int, int]:
    """Grow the vertical span covering ``y`` and return its column range."""
    span = _span_at(table, y)
    if span is None:
        return (-1, -1)
    _y0, _y1, x0, x1, anchor = span
    nrows = anchor.get_attribute_integer(ROWS_SPANNED) or 1
    anchor.set_attribute(ROWS_SPANNED, str(nrows + amount))
    return x0, x1


def _cover_inserted_rows(rows: list[Row], start_x: int, end_x: int) -> None:
    """Mark the span columns of the inserted rows as covered cells."""
    if start_x < 0:
        return
    for row in rows:
        for x in range(start_x, end_x + 1):
            covered = Cell()
            covered.tag = "table:covered-table-cell"
            row.set_cell(x, covered, clone=False)


def _normalize_inserted_rows(
    table: Table,
    rows: list[Row] | None,
    amount: int,
) -> list[Row]:
    width = table._table_cache.width()
    if rows is None:
        return [Row(width=width) for _idx in range(amount)]
    normalized: list[Row] = []
    for row in rows:
        new_row = row.clone
        new_row._set_repeated(None)
        if width and new_row.width < width:
            new_row.append_cell(Cell(repeated=width - new_row.width), clone=False)
        normalized.append(new_row)
    return normalized


def _rebuild_row_map(table: Table) -> None:
    sequence = [(idx, row.repeated or 1) for idx, row in enumerate(table._get_rows())]
    table._table_cache.clear_row_indexes()
    table._table_cache.make_row_map(sequence)


def _insert_rows_at(table: Table, y: int, rows: list[Row]) -> None:
    """Split the compressed segment at ``y`` and insert the rows in place."""
    cache = table._table_cache
    physical_rows = table._get_rows()
    if not physical_rows or y == cache.height():
        # Empty table or insertion after the last row: append the rows.
        for row in rows:
            table.append_row(row, clone=False)
        return

    idx = cache.row_idx(y)
    if idx is None:
        msg = f"Row position out of range: {y}"
        raise RowInsertError(msg)
    current_row = table._get_row2_base(y)
    if current_row is None:  # pragma: no cover
        msg = f"Row not found at position: {y}"
        raise RowInsertError(msg)

    if idx > 0:
        before_cache = cache.row_map[idx - 1]
    else:
        before_cache = -1
    current_pos = before_cache + 1
    current_repeated = cache.row_map[idx] - before_cache
    repeated_before = y - current_pos
    repeated_after = current_repeated - repeated_before

    current_xml = current_row._xml_element
    if repeated_before >= 1:
        current_row._set_repeated(repeated_before)
        after_row = current_row.clone
        after_row._set_repeated(repeated_after)
        current_xml.addnext(after_row._xml_element)
        anchor_xml = current_xml
    else:
        after_row = None
        anchor_xml = current_xml.getprevious()

    for row in rows:
        if anchor_xml is None:
            current_xml.addprevious(row._xml_element)
        else:
            anchor_xml.addnext(row._xml_element)
        anchor_xml = row._xml_element
    if after_row is not None:
        # The after segment must follow every inserted row.
        anchor_xml.addnext(after_row._xml_element)

    _rebuild_row_map(table)


def _shift_references(table: Table, y: int, amount: int) -> None:
    table_name = table.name or ""
    for cell in _formula_cells(table):
        formula = cell.formula
        if formula is None:
            continue
        shifted = _shift_formula(formula, y, amount, table_name)
        if shifted != formula:
            cell.formula = shifted
    for named_range in _named_ranges_to_shift(table):
        _shift_named_range(named_range, table_name, y, amount)


def insert_rows(
    table: Table,
    y: int | str,
    amount: int = 1,
    rows: list[Row] | None = None,
    span_policy: str = SPAN_POLICY_REJECT,
) -> list[Row]:
    """Insert rows before the logical position ``y`` without expanding repeats.

    The physical XML structure is edited directly: a repeated row segment is
    split around the insertion point instead of being materialized, so the
    number of resulting nodes does not depend on the original repetition
    count. Named ranges, merged areas and formulas referencing the table are
    kept consistent.

    Args:
        table: The table to edit.
        y: The 0-based logical row position (an "A1" style coordinate is
            accepted). Rows from ``y`` onward are shifted down.
        amount: Number of empty rows to insert, ignored when ``rows`` is
            provided.
        rows: Optional explicit Row elements to insert.
        span_policy: Either :data:`SPAN_POLICY_REJECT` (default) or
            :data:`SPAN_POLICY_EXPAND` when the position belongs to a
            vertically merged area.

    Returns:
        list[Row]: The inserted Row elements, with their ``y`` attribute set.

    Raises:
        SpanInsertionError: If the position belongs to a merged area and the
            policy is :data:`SPAN_POLICY_REJECT`.
        RowInsertError: If the position or the policy is invalid.

    """
    # Recompute from the physical nodes: the decision below must reflect the
    # real compressed segments, never a possibly stale cache.
    _rebuild_row_map(table)
    y = table._translate_y_from_any(y)
    if y < 0 or y > table.height:
        msg = f"Row position out of range: {y}"
        raise RowInsertError(msg)
    if rows is not None:
        if not rows:
            msg = "No row to insert"
            raise RowInsertError(msg)
        amount = len(rows)
    elif amount < 1:
        msg = f"amount must be a positive integer, got {amount!r}"
        raise RowInsertError(msg)

    _check_span_policy(table, y, span_policy)
    span_columns = (-1, -1)
    if span_policy == SPAN_POLICY_EXPAND:
        span_columns = _extend_vertical_span(table, y, amount)

    inserted_rows = _normalize_inserted_rows(table, rows, amount)
    _insert_rows_at(table, y, inserted_rows)
    _cover_inserted_rows(inserted_rows, *span_columns)

    for offset, row in enumerate(inserted_rows):
        row.y = y + offset
    table._update_width(inserted_rows[-1])
    _shift_references(table, y, amount)
    return inserted_rows
