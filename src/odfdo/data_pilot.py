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
"""Data pilot (pivot table) parsing and auditing for ODF spreadsheets.

An ODS document stores the definition of a pivot table inside a
``table:data-pilot-tables`` element, itself a child of the ``table:table``
element that displays the pivot result:

    table:data-pilot-table
        table:data-pilot-field (orientation = row / column / page / data)
            table:data-pilot-level
                table:data-pilot-subtotal
                table:data-pilot-members
                    table:data-pilot-member

The spreadsheet stores the last computed result as ordinary cached cells in
the target table. This module parses the pivot definition, recomputes every
supported aggregation from the current source range and compares the
recomputed values with the cached cells.

Only a subset of the ODF aggregation functions is recomputable:
``sum``, ``count``, ``countnums``, ``average``, ``min`` and ``max``.
Any other function (``product``, ``stddev``, ``var`` ...) is reported as
unverifiable, never replaced by zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

from .element import Element, register_element_class
from .utils.coordinates import alpha_to_digit, digit_to_alpha

if TYPE_CHECKING:
    from .document import Document
    from .table import Table

# ---------------------------------------------------------------------------
# Constants and sentinels
# ---------------------------------------------------------------------------

ORIENTATION_ROW = "row"
ORIENTATION_COLUMN = "column"
ORIENTATION_PAGE = "page"
ORIENTATION_DATA = "data"
ORIENTATION_HIDDEN = "hidden"

ORIENTATIONS = {
    ORIENTATION_ROW,
    ORIENTATION_COLUMN,
    ORIENTATION_PAGE,
    ORIENTATION_DATA,
    ORIENTATION_HIDDEN,
}

# Aggregation functions that can be recomputed from the source detail.
SUPPORTED_FUNCTIONS = frozenset(
    {"sum", "count", "countnums", "average", "min", "max"}
)

# Status values reported for each audited cached cell.
STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_UNVERIFIABLE = "unverifiable"
STATUS_MISSING_CACHE = "missing-cache"
STATUS_UNEXPECTED_CACHE = "unexpected-cache"

# Relative tolerance used when a numeric result is compared with a cache.
TOLERANCE = 1e-9
ABS_TOLERANCE = 1e-12

_TOTAL_TEXTS = frozenset(
    {
        "total result",
        "grand total",
        "total",
        "总计",
        "汇总",
        "合計",
    }
)


class _Unverifiable:
    """Sentinel: the cell cannot be recomputed (unknown aggregation ...)."""

    _instance: "_Unverifiable | None" = None

    def __new__(cls) -> "_Unverifiable":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<unverifiable>"

    def __bool__(self) -> bool:
        return False


class _NoCache:
    """Sentinel: the pivot definition has no cached cell at that position."""

    _instance: "_NoCache | None" = None

    def __new__(cls) -> "_NoCache":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<no-cache>"

    def __bool__(self) -> bool:
        return False


UNVERIFIABLE = _Unverifiable()
NO_CACHE = _NoCache()


# ---------------------------------------------------------------------------
# ODF element wrappers
# ---------------------------------------------------------------------------


class DataPilotSubtotal(Element):
    """``table:data-pilot-subtotal`` element, a subtotal declaration."""

    _tag = "table:data-pilot-subtotal"

    @property
    def function(self) -> str | None:
        """Aggregation function of the subtotal ("sum", "average", ...)."""
        return self.get_attribute_string("table:function")


register_element_class(DataPilotSubtotal)


class DataPilotMember(Element):
    """``table:data-pilot-member`` element, one member of a field."""

    _tag = "table:data-pilot-member"

    @property
    def name(self) -> str | None:
        """Name of the member, matching a source cell value."""
        return self.get_attribute_string("table:name")

    @property
    def display(self) -> bool:
        """True when the member is visible (not filtered out)."""
        value = self.get_attribute_string("table:display")
        return value is None or value.lower() == "true"


register_element_class(DataPilotMember)


class DataPilotLevel(Element):
    """``table:data-pilot-level`` element, hierarchy level of a field."""

    _tag = "table:data-pilot-level"

    @property
    def display_empty(self) -> bool:
        """True when empty members are displayed."""
        value = self.get_attribute_string("table:display-empty")
        return value is None or value.lower() == "true"

    def get_subtotals(self) -> list[DataPilotSubtotal]:
        """Return the subtotal declarations attached to the level."""
        return cast(
            "list[DataPilotSubtotal]",
            self.get_elements("table:data-pilot-subtotal"),
        )

    def get_members(self) -> list[DataPilotMember]:
        """Return the explicit members of the level, if any."""
        return cast(
            "list[DataPilotMember]",
            self.get_elements(
                "table:data-pilot-members/table:data-pilot-member"
            ),
        )


register_element_class(DataPilotLevel)


class DataPilotField(Element):
    """``table:data-pilot-field`` element, one field of a pivot table."""

    _tag = "table:data-pilot-field"

    @property
    def source_field_name(self) -> str | None:
        """Name of the source column referenced by the field."""
        return self.get_attribute_string("table:source-field-name")

    @property
    def orientation(self) -> str:
        """Field orientation: row, column, page, data or hidden."""
        value = self.get_attribute_string("table:orientation")
        if value not in ORIENTATIONS:
            return ORIENTATION_HIDDEN
        return value

    @property
    def function(self) -> str | None:
        """Aggregation function for a data field, None otherwise."""
        return self.get_attribute_string("table:function")

    @property
    def selected_page(self) -> str | None:
        """Selected member of a page (filter) field, if any."""
        return self.get_attribute_string("table:selected-page")

    @property
    def is_data_layout_field(self) -> str | None:
        """Layout direction of multiple data fields ("columns" or "rows")."""
        return self.get_attribute_string("table:is-data-layout-field")

    def get_levels(self) -> list[DataPilotLevel]:
        """Return the hierarchy levels of the field."""
        return cast(
            "list[DataPilotLevel]",
            self.get_elements("table:data-pilot-level"),
        )

    def get_hidden_members(self) -> list[str]:
        """Return the names of the members explicitly hidden by a filter."""
        hidden: list[str] = []
        for level in self.get_levels():
            for member in level.get_members():
                if not member.display and member.name is not None:
                    hidden.append(member.name)
        return hidden


register_element_class(DataPilotField)


class DataPilotTable(Element):
    """``table:data-pilot-table`` element, definition of one pivot table."""

    _tag = "table:data-pilot-table"

    @property
    def name(self) -> str | None:
        """Name of the pivot table."""
        return self.get_attribute_string("table:name")

    @property
    def source_cell_range_address(self) -> str | None:
        """Raw ``table:source-cell-range-address`` attribute."""
        return self.get_attribute_string("table:source-cell-range-address")

    @property
    def target_range_address(self) -> str | None:
        """Raw ``table:target-range-address`` attribute."""
        return self.get_attribute_string("table:target-range-address")

    @property
    def row_grand_total(self) -> bool:
        """True when the grand total column (over rows) is displayed."""
        value = self.get_attribute_string("table:row-grand-total")
        if value is not None:
            return value.lower() == "true"
        legacy = self.get_attribute_string("table:grand-total")
        return legacy is None or legacy.lower() == "true"

    @property
    def column_grand_total(self) -> bool:
        """True when the grand total row (over columns) is displayed."""
        value = self.get_attribute_string("table:column-grand-total")
        if value is not None:
            return value.lower() == "true"
        legacy = self.get_attribute_string("table:grand-total")
        return legacy is None or legacy.lower() == "true"

    def get_fields(self) -> list[DataPilotField]:
        """Return all the fields of the pivot table."""
        return cast(
            "list[DataPilotField]",
            self.get_elements("table:data-pilot-field"),
        )


register_element_class(DataPilotTable)


class DataPilotTables(Element):
    """``table:data-pilot-tables`` container element."""

    _tag = "table:data-pilot-tables"

    def get_pilot_tables(self) -> list[DataPilotTable]:
        """Return the pivot tables declared in the container."""
        return cast(
            "list[DataPilotTable]",
            self.get_elements("table:data-pilot-table"),
        )


register_element_class(DataPilotTables)


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SourceColumn:
    """One column of the source range, identified by its 0-based position.

    Two source columns may share the same header name; they are always
    distinguished through ``index`` (the actual source column position).
    """

    index: int
    name: str

    @property
    def key(self) -> tuple[str, int]:
        """Unique key (name, source column index)."""
        return self.name, self.index


@dataclass
class PilotFieldInfo:
    """Parsed description of one pivot field."""

    source_column: SourceColumn
    orientation: str
    function: str | None = None
    subtotal_functions: list[str] = field(default_factory=list)
    selected_page: str | None = None
    hidden_members: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        """True when the data field function belongs to the supported set."""
        if self.orientation != ORIENTATION_DATA:
            return True
        return self.function in SUPPORTED_FUNCTIONS


@dataclass
class CellDiscrepancy:
    """Comparison of one cached pivot cell with its recomputed value."""

    address: str
    coordinate: tuple[int, int]
    data_field: tuple[str, int] | None
    row_labels: tuple[Any, ...]
    column_labels: tuple[Any, ...]
    cached: Any
    expected: Any
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the discrepancy to plain JSON compatible data."""
        return {
            "address": self.address,
            "coordinate": list(self.coordinate),
            "data_field": list(self.data_field) if self.data_field else None,
            "row_labels": _jsonable(self.row_labels),
            "column_labels": _jsonable(self.column_labels),
            "cached": _jsonable(self.cached),
            "expected": _jsonable(self.expected),
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class AuditReport:
    """Result of the audit of one pivot table."""

    name: str | None
    source_table: str | None
    source_range: tuple[int, int, int, int] | None
    target_table: str | None
    target_range: tuple[int, int, int, int] | None
    row_fields: list[PilotFieldInfo]
    column_fields: list[PilotFieldInfo]
    data_fields: list[PilotFieldInfo]
    page_fields: list[PilotFieldInfo]
    discrepancies: list[CellDiscrepancy] = field(default_factory=list)
    unsupported_functions: list[str] = field(default_factory=list)
    source_available: bool = True
    reason: str | None = None

    @property
    def target_cell(self) -> tuple[int, int] | None:
        """Top left cell of the target range."""
        if self.target_range is None:
            return None
        return self.target_range[0], self.target_range[1]

    @property
    def fully_supported(self) -> bool:
        """True when every data field uses a recomputable function."""
        return not self.unsupported_functions and self.source_available

    @property
    def mismatch_count(self) -> int:
        """Number of cached cells that do not match the recomputed values."""
        return sum(
            item.status == STATUS_MISMATCH for item in self.discrepancies
        )

    @property
    def unverifiable_count(self) -> int:
        """Number of cached cells that could not be recomputed."""
        return sum(
            item.status == STATUS_UNVERIFIABLE
            for item in self.discrepancies
        )

    @property
    def match_count(self) -> int:
        """Number of cached cells that match the recomputed values."""
        return sum(
            item.status == STATUS_MATCH for item in self.discrepancies
        )

    @property
    def is_stale(self) -> bool:
        """True when at least one cached cell differs from the source data."""
        return any(
            item.status == STATUS_MISMATCH for item in self.discrepancies
        )

    @property
    def mismatches(self) -> list[CellDiscrepancy]:
        """Only the discrepancy cells whose cache is stale."""
        return [
            item
            for item in self.discrepancies
            if item.status == STATUS_MISMATCH
        ]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report to plain JSON compatible data."""
        return {
            "name": self.name,
            "source_table": self.source_table,
            "source_range": list(self.source_range)
            if self.source_range
            else None,
            "target_table": self.target_table,
            "target_range": list(self.target_range)
            if self.target_range
            else None,
            "row_fields": [_field_to_dict(item) for item in self.row_fields],
            "column_fields": [
                _field_to_dict(item) for item in self.column_fields
            ],
            "data_fields": [_field_to_dict(item) for item in self.data_fields],
            "page_fields": [_field_to_dict(item) for item in self.page_fields],
            "fully_supported": self.fully_supported,
            "source_available": self.source_available,
            "unsupported_functions": list(self.unsupported_functions),
            "match_count": self.match_count,
            "mismatch_count": self.mismatch_count,
            "unverifiable_count": self.unverifiable_count,
            "is_stale": self.is_stale,
            "reason": self.reason,
            "discrepancies": [item.to_dict() for item in self.discrepancies],
        }


def _field_to_dict(info: PilotFieldInfo) -> dict[str, Any]:
    return {
        "source_column": {
            "index": info.source_column.index,
            "name": info.source_column.name,
        },
        "orientation": info.orientation,
        "function": info.function,
        "subtotal_functions": list(info.subtotal_functions),
        "selected_page": info.selected_page,
        "hidden_members": list(info.hidden_members),
        "supported": info.supported,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_Unverifiable, _NoCache)):
        return repr(value)
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Address parsing helpers
# ---------------------------------------------------------------------------


def _split_address(address: str) -> tuple[str | None, str]:
    """Split a cell/range address into (sheet name, local address)."""
    text = address.strip()
    if "." not in text:
        return None, text
    sheet, _, local = text.partition(".")
    sheet = sheet.strip().lstrip("$")
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1]
    return sheet, local


def _parse_cell(token: str) -> tuple[int, int]:
    """Parse a single cell token such as "$A$1" or "C12" into (x, y)."""
    cleaned = token.replace("$", "")
    letters = "".join(char for char in cleaned if char.isalpha())
    digits = "".join(char for char in cleaned if char.isdigit())
    if not letters or not digits:
        msg = f"Malformed cell address: {token!r}"
        raise ValueError(msg)
    return alpha_to_digit(letters), int(digits) - 1


def parse_cell_range_address(
    address: str,
) -> tuple[str | None, tuple[int, int, int, int]]:
    """Parse a cell range address.

    Returns the optional sheet name and the 0-based range tuple
    ``(start_col, start_row, end_col, end_row)``. A single cell is expanded
    to a one-cell range.
    """
    sheet, local = _split_address(address)
    local = local.lstrip("$")
    if ":" in local:
        start_text, end_text = local.split(":", 1)
        # The end part may repeat the "$Sheet." prefix.
        if "." in end_text:
            end_text = end_text.split(".", 1)[1]
        start_x, start_y = _parse_cell(start_text)
        end_x, end_y = _parse_cell(end_text)
    else:
        start_x, start_y = _parse_cell(local)
        end_x, end_y = start_x, start_y
    return sheet, (start_x, start_y, end_x, end_y)


def format_address(sheet: str | None, x: int, y: int) -> str:
    """Build an ODF style absolute cell address."""
    cell = f"${digit_to_alpha(x)}${y + 1}"
    if not sheet:
        return cell
    if any(char in sheet for char in " '"):
        return f"$'{sheet}'.{cell}"
    return f"${sheet}.{cell}"


# ---------------------------------------------------------------------------
# Aggregation engine
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
    )


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return Decimal(str(value))
    return None


def _aggregate(function: str, values: list[Any]) -> Any:
    """Compute one aggregation over the source values."""
    if function == "count":
        return sum(1 for value in values if value is not None)
    numeric = [value for value in values if _is_number(value)]
    if function == "countnums":
        return len(numeric)
    if not numeric:
        return None
    if function == "min":
        return min(numeric)
    if function == "max":
        return max(numeric)
    decimals = [
        decimal
        for value in numeric
        if (decimal := _as_decimal(value)) is not None
    ]
    if len(decimals) == len(numeric):
        if function == "sum":
            return sum(decimals, Decimal(0))
        if function == "average":
            return sum(decimals, Decimal(0)) / Decimal(len(decimals))
    numbers = [float(value) for value in numeric]
    if function == "sum":
        return math.fsum(numbers)
    if function == "average":
        return math.fsum(numbers) / len(numbers)
    return UNVERIFIABLE


def _values_close(cached: Any, expected: Any) -> bool:
    """Compare a cached numeric value with the recomputed one."""
    if isinstance(cached, bool) or not _is_number(cached):
        return False
    cached_decimal = _as_decimal(cached)
    expected_decimal = _as_decimal(expected)
    if cached_decimal is not None and expected_decimal is not None:
        if cached_decimal == expected_decimal:
            return True
        delta = abs(cached_decimal - expected_decimal)
        scale = max(
            Decimal(1),
            abs(cached_decimal),
            abs(expected_decimal),
        )
        return delta <= Decimal(str(TOLERANCE)) * scale or (
            delta <= Decimal(str(ABS_TOLERANCE))
        )
    try:
        cached_float = float(cached)
        expected_float = float(expected)
    except (TypeError, ValueError, OverflowError):
        return False
    if math.isnan(cached_float) and math.isnan(expected_float):
        return True
    delta = abs(cached_float - expected_float)
    scale = max(1.0, abs(cached_float), abs(expected_float))
    return delta <= TOLERANCE * scale or delta <= ABS_TOLERANCE


def _member_matches(value: Any, member_name: str) -> bool:
    """Check whether a source value matches a pivot member name."""
    if value is None:
        return member_name == ""
    if str(value) == member_name:
        return True
    if _is_number(value):
        try:
            return Decimal(str(value)) == Decimal(member_name)
        except (InvalidOperation, ValueError):
            return False
    return False


def _is_total_text(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TOTAL_TEXTS:
        return True
    return text.endswith(" total") or text.endswith(" total result")


# ---------------------------------------------------------------------------
# Pivot auditor
# ---------------------------------------------------------------------------


class PilotAuditor:
    """Audit one or all the pivot tables of an ODS document."""

    def __init__(self, document: Document) -> None:
        self.document = document

    # -- discovery ---------------------------------------------------------

    def get_pilot_tables(self) -> list[DataPilotTable]:
        """Return every ``table:data-pilot-table`` element of the document."""
        return cast(
            "list[DataPilotTable]",
            self.document.body.get_elements(
                "descendant::table:data-pilot-tables/table:data-pilot-table"
            ),
        )

    def audit_all(self) -> list[AuditReport]:
        """Audit all the pivot tables found in the document."""
        return [self.audit(pilot) for pilot in self.get_pilot_tables()]

    # -- parsing -----------------------------------------------------------

    def audit(self, pilot: DataPilotTable) -> AuditReport:
        """Build the audit report of one pivot table definition."""
        source_sheet, source_range = self._parse_source(pilot)
        target_sheet, target_range = self._parse_target(pilot)
        source_table = self._table_by_name(source_sheet)
        target_table = self._table_by_name(target_sheet)
        source_columns = self._source_columns(source_table, source_range)

        field_infos = self._parse_fields(pilot, source_columns)
        row_fields = [
            info for info in field_infos if info.orientation == ORIENTATION_ROW
        ]
        column_fields = [
            info
            for info in field_infos
            if info.orientation == ORIENTATION_COLUMN
        ]
        data_fields = [
            info for info in field_infos if info.orientation == ORIENTATION_DATA
        ]
        page_fields = [
            info for info in field_infos if info.orientation == ORIENTATION_PAGE
        ]

        report = AuditReport(
            name=pilot.name,
            source_table=source_sheet,
            source_range=source_range,
            target_table=target_sheet,
            target_range=target_range,
            row_fields=row_fields,
            column_fields=column_fields,
            data_fields=data_fields,
            page_fields=page_fields,
        )
        report.unsupported_functions = sorted(
            {
                info.function or "<none>"
                for info in data_fields
                if info.function not in SUPPORTED_FUNCTIONS
            }
        )

        if source_table is None or source_range is None:
            report.source_available = False
            report.reason = (
                f"Source range {pilot.source_cell_range_address!r} "
                "is not available in the document"
            )
            self._mark_unverifiable_cells(report, target_table)
            return report

        engine = _PilotEngine(report, source_table, source_columns)
        engine.run(target_table)
        return report

    def _parse_source(
        self, pilot: DataPilotTable
    ) -> tuple[str | None, tuple[int, int, int, int] | None]:
        address = pilot.source_cell_range_address
        if not address:
            return None, None
        try:
            return parse_cell_range_address(address)
        except ValueError:
            return None, None

    def _parse_target(
        self, pilot: DataPilotTable
    ) -> tuple[str | None, tuple[int, int, int, int] | None]:
        address = pilot.target_range_address
        if not address:
            return None, None
        try:
            return parse_cell_range_address(address)
        except ValueError:
            return None, None

    def _table_by_name(self, name: str | None) -> Table | None:
        if name is None:
            return None
        return self.document.body.get_table(name=name)

    def _source_columns(
        self,
        source_table: Table | None,
        source_range: tuple[int, int, int, int] | None,
    ) -> list[SourceColumn]:
        if source_table is None or source_range is None:
            return []
        start_x, start_y, end_x, _end_y = source_range
        headers = source_table.get_row_values(start_y)
        columns: list[SourceColumn] = []
        for index in range(start_x, end_x + 1):
            header = headers[index] if index < len(headers) else None
            name = "" if header is None else str(header)
            columns.append(SourceColumn(index=index, name=name))
        return columns

    def _parse_fields(
        self,
        pilot: DataPilotTable,
        source_columns: list[SourceColumn],
    ) -> list[PilotFieldInfo]:
        by_name: dict[str, list[SourceColumn]] = {}
        for column in source_columns:
            by_name.setdefault(column.name, []).append(column)
        infos: list[PilotFieldInfo] = []
        name_cursor: dict[str, int] = {}
        for field in pilot.get_fields():
            field_name = field.source_field_name or ""
            candidates = by_name.get(field_name, [])
            cursor = name_cursor.get(field_name, 0)
            if cursor < len(candidates):
                column = candidates[cursor]
                name_cursor[field_name] = cursor + 1
            else:
                column = SourceColumn(index=-1, name=field_name)
            subtotal_functions = [
                subtotal.function or ""
                for level in field.get_levels()
                for subtotal in level.get_subtotals()
                if subtotal.function
            ]
            infos.append(
                PilotFieldInfo(
                    source_column=column,
                    orientation=field.orientation,
                    function=field.function,
                    subtotal_functions=subtotal_functions,
                    selected_page=field.selected_page,
                    hidden_members=field.get_hidden_members(),
                )
            )
        return infos

    def _mark_unverifiable_cells(
        self,
        report: AuditReport,
        target_table: Table | None,
    ) -> None:
        if target_table is None or report.target_range is None:
            return
        start_x, start_y, end_x, end_y = report.target_range
        n_row_fields = len(report.row_fields)
        n_col_fields = len(report.column_fields)
        for y in range(start_y + n_col_fields, min(end_y + 1, target_table.height)):
            for x in range(
                start_x + n_row_fields,
                min(end_x + 1, target_table.width),
            ):
                cached = target_table.get_value((x, y))
                if cached is None:
                    continue
                report.discrepancies.append(
                    CellDiscrepancy(
                        address=format_address(report.target_table, x, y),
                        coordinate=(x, y),
                        data_field=None,
                        row_labels=(),
                        column_labels=(),
                        cached=cached,
                        expected=UNVERIFIABLE,
                        status=STATUS_UNVERIFIABLE,
                        reason=report.reason,
                    )
                )


class _PilotEngine:
    """Recompute the pivot values and align them with the cached output."""

    def __init__(
        self,
        report: AuditReport,
        source_table: Table,
        source_columns: list[SourceColumn],
    ) -> None:
        self.report = report
        self.source_table = source_table
        self.source_columns = source_columns
        # bucket key: (row tuple or None, column tuple or None, data index)
        self.buckets: dict[tuple[Any, Any, int], Any] = {}
        # subtotal key: (row prefix, column prefix, data index, function)
        self.subtotals: dict[tuple[Any, Any, int, str], Any] = {}
        self._cached_records: list[dict[tuple[str, int], Any]] | None = None

    # -- filtering and record extraction -----------------------------------

    def _records(self) -> list[dict[tuple[str, int], Any]]:
        if self._cached_records is not None:
            return self._cached_records
        assert self.report.source_range is not None
        matrix = self.source_table.get_values(self.report.source_range)
        records: list[dict[tuple[str, int], Any]] = []
        for row in matrix[1:]:
            if all(value is None for value in row):
                continue
            record: dict[tuple[str, int], Any] = {}
            for offset, column in enumerate(self.source_columns):
                record[column.key] = (
                    row[offset] if offset < len(row) else None
                )
            records.append(record)
        for info in self.report.page_fields:
            records = self._apply_page_filter(info, records)
        for info in [*self.report.row_fields, *self.report.column_fields]:
            records = self._apply_hidden_members(info, records)
        self._cached_records = records
        return records

    @staticmethod
    def _apply_page_filter(
        info: PilotFieldInfo,
        records: list[dict[tuple[str, int], Any]],
    ) -> list[dict[tuple[str, int], Any]]:
        if info.selected_page is None:
            return records
        key = info.source_column.key
        return [
            record
            for record in records
            if _member_matches(record.get(key), info.selected_page or "")
        ]

    @staticmethod
    def _apply_hidden_members(
        info: PilotFieldInfo,
        records: list[dict[tuple[str, int], Any]],
    ) -> list[dict[tuple[str, int], Any]]:
        if not info.hidden_members:
            return records
        key = info.source_column.key
        return [
            record
            for record in records
            if not any(
                _member_matches(record.get(key), member)
                for member in info.hidden_members
            )
        ]

    # -- computation -------------------------------------------------------

    def run(self, target_table: Table | None) -> None:
        records = self._records()
        row_keys = [
            info.source_column.key for info in self.report.row_fields
        ]
        col_keys = [
            info.source_column.key for info in self.report.column_fields
        ]
        data_keys = [
            info.source_column.key for info in self.report.data_fields
        ]
        groups: dict[tuple[tuple[Any, ...], tuple[Any, ...]], list[dict[Any, Any]]] = {}
        for record in records:
            row_part = tuple(record.get(key) for key in row_keys)
            col_part = tuple(record.get(key) for key in col_keys)
            groups.setdefault((row_part, col_part), []).append(record)

        for (row_part, col_part), group_records in groups.items():
            for data_index, info in enumerate(self.report.data_fields):
                function = info.function
                values = [
                    record.get(data_keys[data_index]) for record in group_records
                ]
                self.buckets[(row_part, col_part, data_index)] = (
                    UNVERIFIABLE
                    if function not in SUPPORTED_FUNCTIONS
                    else _aggregate(function, values)
                )

        self._compute_totals(records, row_keys, col_keys, data_keys)
        self._compute_subtotals(records, row_keys, col_keys, data_keys)
        if target_table is not None:
            self.run_compare(target_table)

    def _compute_totals(
        self,
        records: list[dict[tuple[str, int], Any]],
        row_keys: list[tuple[str, int]],
        col_keys: list[tuple[str, int]],
        data_keys: list[tuple[str, int]],
    ) -> None:
        for data_index, info in enumerate(self.report.data_fields):
            function = info.function
            values = [
                record.get(data_keys[data_index]) for record in records
            ]
            self.buckets[(None, None, data_index)] = (
                UNVERIFIABLE
                if function not in SUPPORTED_FUNCTIONS
                else _aggregate(function, values)
            )
            row_groups: dict[tuple[Any, ...], list[Any]] = {}
            col_groups: dict[tuple[Any, ...], list[Any]] = {}
            for record in records:
                row_part = tuple(record.get(key) for key in row_keys)
                col_part = tuple(record.get(key) for key in col_keys)
                row_groups.setdefault(row_part, []).append(
                    record.get(data_keys[data_index])
                )
                col_groups.setdefault(col_part, []).append(
                    record.get(data_keys[data_index])
                )
            for row_part, group_values in row_groups.items():
                self.buckets[(row_part, None, data_index)] = (
                    UNVERIFIABLE
                    if function not in SUPPORTED_FUNCTIONS
                    else _aggregate(function, group_values)
                )
            for col_part, group_values in col_groups.items():
                self.buckets[(None, col_part, data_index)] = (
                    UNVERIFIABLE
                    if function not in SUPPORTED_FUNCTIONS
                    else _aggregate(function, group_values)
                )

    def _compute_subtotals(
        self,
        records: list[dict[tuple[str, int], Any]],
        row_keys: list[tuple[str, int]],
        col_keys: list[tuple[str, int]],
        data_keys: list[tuple[str, int]],
    ) -> None:
        for field_level in range(1, len(row_keys)):
            prefix_groups: dict[tuple[Any, ...], list[dict[Any, Any]]] = {}
            for record in records:
                prefix = tuple(
                    record.get(key) for key in row_keys[:field_level]
                )
                prefix_groups.setdefault(prefix, []).append(record)
            self._store_subtotal_groups(
                prefix_groups,
                col_keys,
                data_keys,
                is_row=True,
                prefix_level=field_level,
            )
        for field_level in range(1, len(col_keys)):
            prefix_groups = {}
            for record in records:
                prefix = tuple(
                    record.get(key) for key in col_keys[:field_level]
                )
                prefix_groups.setdefault(prefix, []).append(record)
            self._store_subtotal_groups(
                prefix_groups,
                col_keys,
                data_keys,
                is_row=False,
                prefix_level=field_level,
            )

    def _store_subtotal_groups(
        self,
        prefix_groups: dict[tuple[Any, ...], list[dict[Any, Any]]],
        col_keys: list[tuple[str, int]],
        data_keys: list[tuple[str, int]],
        is_row: bool,
        prefix_level: int,
    ) -> None:
        for prefix, group_records in prefix_groups.items():
            for data_index, info in enumerate(self.report.data_fields):
                functions = (
                    self.report.row_fields[prefix_level - 1].subtotal_functions
                    if is_row
                    else self.report.column_fields[prefix_level - 1].subtotal_functions
                )
                functions = functions or ["sum"]
                for function in functions:
                    values = [
                        record.get(data_keys[data_index])
                        for record in group_records
                    ]
                    if is_row:
                        key = (prefix, None, data_index, function)
                    else:
                        key = (None, prefix, data_index, function)
                    self.subtotals[key] = (
                        UNVERIFIABLE
                        if function not in SUPPORTED_FUNCTIONS
                        else _aggregate(function, values)
                    )

    # -- cache alignment ---------------------------------------------------

    def run_compare(self, target_table: Table) -> None:
        """Locate the cached output area and compare it with recomputation."""
        assert self.report.target_range is not None
        start_x, start_y, end_x, end_y = self.report.target_range
        end_x = min(end_x, target_table.width - 1)
        end_y = min(end_y, target_table.height - 1)
        row_members = self._distinct_members(self.report.row_fields)
        col_members = self._distinct_members(self.report.column_fields)

        data_first_row = self._first_data_row(
            target_table, start_x, start_y, end_x, end_y, row_members
        )
        data_first_col = self._first_data_column(
            target_table, start_x, start_y, end_x, col_members
        )
        if data_first_row is None or data_first_col is None:
            return

        n_row_fields = len(self.report.row_fields)
        n_col_fields = len(self.report.column_fields)
        label_first_col = data_first_col - n_row_fields
        label_first_row = data_first_row - n_col_fields

        row_grid = self._row_axis(
            target_table, label_first_col, data_first_col, data_first_row, end_y
        )
        col_grid = self._column_axis(
            target_table, label_first_row, data_first_row, data_first_col, end_x
        )
        n_data_fields = max(1, len(self.report.data_fields))

        for y, row_info in row_grid.items():
            if self._blank_row(
                target_table, y, data_first_col, end_x
            ):
                continue
            for x, col_info in col_grid.items():
                data_index = (x - data_first_col) % n_data_fields
                if data_index >= len(self.report.data_fields):
                    continue
                cached = target_table.get_value((x, y))
                address = format_address(self.report.target_table, x, y)
                self._evaluate_cell(
                    address=address,
                    coordinate=(x, y),
                    data_index=data_index,
                    row_labels=row_info[0],
                    column_labels=col_info[0],
                    row_total=row_info[1],
                    col_total=col_info[1],
                    row_subtotal=row_info[2],
                    col_subtotal=col_info[2],
                    cached=cached,
                )

    def _distinct_members(
        self, fields: list[PilotFieldInfo]
    ) -> list[set[Any]]:
        """Return, for each field, the member values kept after filtering."""
        members: list[set[Any]] = []
        records = self._records()
        for info in fields:
            key = info.source_column.key
            values = {record.get(key) for record in records}
            members.append(values)
        return members

    def _first_data_row(
        self,
        table: Table,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        row_members: list[set[Any]],
    ) -> int | None:
        if not row_members:
            # No row field: the unique data row follows the column headers.
            for y in range(start_y, end_y + 1):
                if self._row_has_number(table, y, start_x, end_x):
                    return y
            return None
        known = row_members[0]
        label_end_x = min(end_x, start_x + len(row_members))
        for y in range(start_y, end_y + 1):
            for x in range(start_x, label_end_x + 1):
                value = table.get_value((x, y))
                if value is not None and (
                    value in known or _is_total_text(value)
                ):
                    return y
        return None

    def _first_data_column(
        self,
        table: Table,
        start_x: int,
        start_y: int,
        end_x: int,
        col_members: list[set[Any]],
    ) -> int | None:
        if not col_members:
            # No column field: the data columns start after the row labels.
            last_row = table.height - 1
            for x in range(start_x, end_x + 1):
                if self._column_has_number(table, start_y, last_row, x):
                    return x
            return None
        known = col_members[0]
        header_end_y = start_y + len(col_members)
        for x in range(start_x, end_x + 1):
            for y in range(start_y, header_end_y + 1):
                value = table.get_value((x, y))
                if value is not None and (
                    value in known or _is_total_text(value)
                ):
                    return x
        return None

    @staticmethod
    def _row_has_number(
        table: Table, y: int, start_x: int, end_x: int
    ) -> bool:
        return any(
            _is_number(table.get_value((x, y)))
            for x in range(start_x, end_x + 1)
        )

    @staticmethod
    def _column_has_number(
        table: Table, start_y: int, end_y: int, x: int
    ) -> bool:
        return any(
            _is_number(table.get_value((x, y)))
            for y in range(start_y, end_y + 1)
        )

    def _row_axis(
        self,
        table: Table,
        start_x: int,
        data_first_col: int,
        first_row: int,
        end_y: int,
    ) -> dict[int, tuple[tuple[Any, ...], bool, tuple[Any, ...] | None]]:
        n_row_fields = len(self.report.row_fields)
        grid: dict[
            int, tuple[tuple[Any, ...], bool, tuple[Any, ...] | None]
        ] = {}
        if n_row_fields == 0:
            for y in range(first_row, end_y + 1):
                if self._row_has_number(table, y, data_first_col, table.width - 1):
                    grid[y] = ((), False, None)
            return grid
        previous: list[Any] = [None] * n_row_fields
        for y in range(first_row, end_y + 1):
            current = [
                table.get_value((start_x + level, y))
                for level in range(n_row_fields)
            ]
            total_hit = any(_is_total_text(value) for value in current)
            subtotal_prefix: tuple[Any, ...] | None = None
            if total_hit:
                for level, value in enumerate(current):
                    if _is_total_text(value):
                        subtotal_prefix = tuple(previous[:level])
                        break
                if not subtotal_prefix:
                    grid[y] = (tuple(current), True, None)
                    continue
            else:
                for level, value in enumerate(current):
                    if value is None:
                        current[level] = previous[level]
                previous = list(current)
            grid[y] = (tuple(current), total_hit, subtotal_prefix)
        return grid

    def _column_axis(
        self,
        table: Table,
        start_y: int,
        first_row: int,
        first_col: int,
        end_x: int,
    ) -> dict[int, tuple[tuple[Any, ...], bool, tuple[Any, ...] | None]]:
        n_col_fields = len(self.report.column_fields)
        grid: dict[
            int, tuple[tuple[Any, ...], bool, tuple[Any, ...] | None]
        ] = {}
        if n_col_fields == 0:
            for x in range(first_col, end_x + 1):
                grid[x] = ((), False, None)
            return grid
        previous: list[Any] = [None] * n_col_fields
        for x in range(first_col, end_x + 1):
            current = [
                table.get_value((x, start_y + level))
                for level in range(n_col_fields)
            ]
            total_hit = any(_is_total_text(value) for value in current)
            subtotal_prefix: tuple[Any, ...] | None = None
            if total_hit:
                for level, value in enumerate(current):
                    if _is_total_text(value):
                        subtotal_prefix = tuple(previous[:level])
                        break
                if not subtotal_prefix:
                    grid[x] = (tuple(current), True, None)
                    continue
            else:
                for level, value in enumerate(current):
                    if value is None:
                        current[level] = previous[level]
                previous = list(current)
            grid[x] = (tuple(current), total_hit, subtotal_prefix)
        return grid

    @staticmethod
    def _blank_row(
        table: Table, y: int, start_x: int, end_x: int
    ) -> bool:
        return all(
            table.get_value((x, y)) is None
            for x in range(start_x, end_x + 1)
        )

    def _evaluate_cell(
        self,
        address: str,
        coordinate: tuple[int, int],
        data_index: int | None,
        row_labels: tuple[Any, ...],
        column_labels: tuple[Any, ...],
        row_total: bool,
        col_total: bool,
        row_subtotal: tuple[Any, ...] | None,
        col_subtotal: tuple[Any, ...] | None,
        cached: Any,
    ) -> None:
        if data_index is None or not self.report.data_fields:
            if cached is None:
                return
            self.report.discrepancies.append(
                CellDiscrepancy(
                    address=address,
                    coordinate=coordinate,
                    data_field=None,
                    row_labels=row_labels,
                    column_labels=column_labels,
                    cached=cached,
                    expected=UNVERIFIABLE,
                    status=STATUS_UNVERIFIABLE,
                    reason="data field cannot be identified",
                )
            )
            return
        info = self.report.data_fields[data_index]
        expected = self._lookup_expected(
            data_index,
            info,
            row_labels,
            column_labels,
            row_total,
            col_total,
            row_subtotal,
            col_subtotal,
        )
        discrepancy = self._build_discrepancy(
            address=address,
            coordinate=coordinate,
            info=info,
            row_labels=row_labels,
            column_labels=column_labels,
            cached=cached,
            expected=expected,
        )
        if discrepancy is not None:
            self.report.discrepancies.append(discrepancy)

    def _lookup_expected(
        self,
        data_index: int,
        info: PilotFieldInfo,
        row_labels: tuple[Any, ...],
        column_labels: tuple[Any, ...],
        row_total: bool,
        col_total: bool,
        row_subtotal: tuple[Any, ...] | None,
        col_subtotal: tuple[Any, ...] | None,
    ) -> Any:
        function = info.function
        if function not in SUPPORTED_FUNCTIONS:
            return UNVERIFIABLE
        if row_subtotal is not None:
            return self.subtotals.get(
                (row_subtotal, None, data_index, function), NO_CACHE
            )
        if col_subtotal is not None:
            return self.subtotals.get(
                (None, col_subtotal, data_index, function), NO_CACHE
            )
        row_part = None if row_total else tuple(row_labels)
        col_part = None if col_total else tuple(column_labels)
        return self.buckets.get(
            (row_part, col_part, data_index), NO_CACHE
        )

    def _build_discrepancy(
        self,
        address: str,
        coordinate: tuple[int, int],
        info: PilotFieldInfo,
        row_labels: tuple[Any, ...],
        column_labels: tuple[Any, ...],
        cached: Any,
        expected: Any,
    ) -> CellDiscrepancy | None:
        data_field = info.source_column.key
        if isinstance(expected, _Unverifiable):
            return CellDiscrepancy(
                address=address,
                coordinate=coordinate,
                data_field=data_field,
                row_labels=row_labels,
                column_labels=column_labels,
                cached=cached,
                expected=UNVERIFIABLE,
                status=STATUS_UNVERIFIABLE,
                reason=f"unsupported aggregation {info.function!r}",
            )
        if isinstance(expected, _NoCache):
            if cached is None:
                # No source bucket and no cached value: nothing to report.
                return None
            return CellDiscrepancy(
                address=address,
                coordinate=coordinate,
                data_field=data_field,
                row_labels=row_labels,
                column_labels=column_labels,
                cached=cached,
                expected=NO_CACHE,
                status=STATUS_UNEXPECTED_CACHE,
                reason="cached value has no matching source bucket",
            )
        if cached is None:
            return CellDiscrepancy(
                address=address,
                coordinate=coordinate,
                data_field=data_field,
                row_labels=row_labels,
                column_labels=column_labels,
                cached=cached,
                expected=expected,
                status=STATUS_MISSING_CACHE,
            )
        if expected is None:
            status = STATUS_MATCH if cached is None else STATUS_MISMATCH
        elif _values_close(cached, expected):
            status = STATUS_MATCH
        else:
            status = STATUS_MISMATCH
        return CellDiscrepancy(
            address=address,
            coordinate=coordinate,
            data_field=data_field,
            row_labels=row_labels,
            column_labels=column_labels,
            cached=cached,
            expected=expected,
            status=status,
        )


def get_pilot_tables(document: Document) -> list[DataPilotTable]:
    """Shortcut returning all the pivot tables of a document."""
    return PilotAuditor(document).get_pilot_tables()


def audit_pilot_tables(document: Document) -> list[AuditReport]:
    """Shortcut auditing all the pivot tables of a document."""
    return PilotAuditor(document).audit_all()
