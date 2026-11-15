# Copyright 2018-2026 Jérôme Dumonteil
# Copyright (c) 2009-2012 Ars Aperta, Itaapy, Pierlis, Talend.
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
# Authors: David Versmisse <david.versmisse@itaapy.com>
#          Hervé Cauwelier <herve@itaapy.com>
#          Romain Gauthier <romain@itaapy.com>
#          Jerome Dumonteil <jerome.dumonteil@itaapy.com>
"""NamedRange class for "table:named-range" tag."""

from __future__ import annotations

import contextlib
import re
import string
from functools import cache
from typing import TYPE_CHECKING, Any, cast

from .element import Element, register_element_class
from .utils import convert_coordinates, digit_to_alpha

if TYPE_CHECKING:
    from .table import Table

_RE_TABLE_NAME = re.compile(r"^\'|[\n\\/\*\?:\][]|\'$")

# Declaration scopes of a named range: local to a table or global to the document.
SCOPE_LOCAL = "local"
SCOPE_GLOBAL = "global"

# ODF tags used to identify the declaration scope of a named range.
_TAG_NAMED_EXPRESSIONS = "table:named-expressions"
_TAG_TABLE = "table:table"


def table_name_check(name: Any) -> str:
    """Validate a table name for use in ODF documents.

    Ensures the name is a non-empty string and does not contain forbidden
    characters like single quotes, slashes, asterisks, question marks, or
    brackets.

    Args:
        name: The name to validate.

    Returns:
        str: The validated and stripped table name.

    Raises:
        TypeError: If `name` is not a string.
        ValueError: If `name` is empty or contains forbidden characters.

    """
    if not isinstance(name, str):
        msg = "String required"
        raise TypeError(msg)
    table_name: str = name.strip()
    if not table_name:
        msg = "Empty name not allowed"
        raise ValueError(msg)
    if match := _RE_TABLE_NAME.search(table_name):
        msg = f"Character {match.group()!r} not allowed"
        raise ValueError(msg)
    return table_name


@cache
def _forbidden_in_named_range() -> set[str]:
    """Return a set of characters forbidden in named range names.

    This set is computed once and cached. Forbidden characters include
    most punctuation and symbols, excluding underscore.

    Returns:
        set[str]: A set of forbidden characters.

    """
    return {
        char
        for char in string.printable
        if char not in string.ascii_letters
        and char not in string.digits
        and char != "_"
    }


class NamedRange(Element):
    """Named range of cells in a table, "table:named-range".

    Identifies inside the spreadsheet a range of cells of a table by a name
    and the name of the table.

    Name Ranges have the following attributes:

        name -- name of the named range

        table_name -- name of the table

        start -- first cell of the named range, tuple (x, y)

        end -- last cell of the named range, tuple (x, y)

        crange -- range of the named range, tuple (x, y, z, t)

        usage -- None or str, usage of the named range.
    """

    _tag = "table:named-range"

    def __init__(
        self,
        name: str | None = None,
        crange: str | tuple | list | None = None,
        table_name: str | None = None,
        usage: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a NamedRange element.

        A NamedRange element identifies a range of cells in a table by a name
        and the table's name. The `name` must be alphanumeric with
        underscores, and not formatted like a cell coordinate (e.g., "A1").
        The `table_name` must be a valid table name (without single quotes or
        slashes).

        Args:
            name: The name of the named range.
            crange: The cell or area coordinate, e.g., "A1", "A1:B2", (0, 0),
                or (0, 0, 1, 1).
            table_name: The name of the table the range belongs to.
            usage: The usage of the named range, one of "print-range",
                "filter", "repeat-column", "repeat-row", or None.
            **kwargs: Additional keyword arguments for the parent `Element`
                class.

        """
        super().__init__(**kwargs)
        self.usage: str | None = None
        self.table_name: str = ""
        self.start: tuple[int, int] = 0, 0
        self.end: tuple[int, int] = 0, 0
        self.crange: tuple[int, int, int, int] = 0, 0, 0, 0
        self.usage = None
        if self._do_init:
            self.name = name or ""
            self.table_name = table_name_check(table_name)
            self.set_range(crange or "")
            self.set_usage(usage)
        cell_range_address = self.get_attribute_string("table:cell-range-address") or ""
        if not cell_range_address:
            return
        self.usage = self.get_attribute_string("table:range-usable-as")
        name_range = cell_range_address.replace("$", "")
        name, crange = name_range.split(".", 1)
        if name.startswith("'") and name.endswith("'"):
            name = name[1:-1]
        self.table_name = name
        crange = crange.replace(".", "")
        self._set_range(crange)

    def set_usage(self, usage: str | None = None) -> None:
        """Set the usage type for the named range.

        The usage specifies how the named range is intended to be used (e.g.,
        for printing, filtering, or repeating columns/rows).

        Args:
            usage: The usage type. Can be "print-range", "filter",
                "repeat-column", "repeat-row", or None to clear the usage.

        """
        if usage is not None:
            usage = usage.strip().lower()
            if usage not in ("print-range", "filter", "repeat-column", "repeat-row"):
                usage = None
        if usage is None:
            with contextlib.suppress(KeyError):
                self.del_attribute("table:range-usable-as")
            self.usage = None
        else:
            self.set_attribute("table:range-usable-as", usage)
            self.usage = usage

    @staticmethod
    def _check_nr_name(name: str) -> str:
        """Validate a named range name.

        Ensures the name is not empty, contains only alphanumeric characters
        and underscores, and does not resemble a cell coordinate (e.g., "A1").

        Args:
            name: The name to validate.

        Returns:
            str: The validated name.

        Raises:
            ValueError: If the name is empty, contains forbidden characters,
                or is formatted like a cell coordinate.

        """
        name = name.strip()
        if not name:
            msg = "Named Range name can't be empty"
            raise ValueError(msg)
        for x in name:
            if x in _forbidden_in_named_range():
                msg = f"Character forbidden in Named Range name: {x!r}"
                raise ValueError(msg)
        step = ""
        for x in name:
            if x in string.ascii_letters and step in ("", "A"):
                step = "A"
                continue
            if step in ("A", "A1") and x in string.digits:
                step = "A1"
                continue
            step = ""
            break
        if step == "A1":
            msg = f"Name of the type 'ABC123' is not allowed for Named Range: {name!r}"
            raise ValueError(msg)
        return name

    @property
    def name(self) -> str | None:
        """Get the name of the named range.

        The name is mandatory, must be alphanumeric with underscores, and can
        not be formatted like a cell coordinate (e.g., "A1").

        Returns:
            str | None: The name of the named range.

        """
        return self.get_attribute_string("table:name")

    @name.setter
    def name(self, name: str) -> None:
        """Set the name of the named range.

        If a named range with the same name already exists in the same
        declaration scope, it will be replaced. A same-named range of another
        scope is left untouched: local and global declarations are
        independent.

        Args:
            name: The new name for the named range.

        """
        name = self._check_nr_name(name)
        with contextlib.suppress(Exception):
            # we are not on an NR inserted in a document.
            # We know the body should contains NR mixin if
            # not exception.
            scope = self.scope
            if scope == SCOPE_LOCAL:
                named_expressions = self.parent
                owner = (
                    named_expressions.parent if named_expressions is not None else None
                )
                if owner is None:
                    raise ValueError
                owner_table = cast("Table", owner)
                existing = owner_table._local_named_range(name)
                if (
                    existing is not None
                    and existing._xml_element is not self._xml_element
                ):
                    existing.delete()
            elif scope == SCOPE_GLOBAL and (body := self.document_body):
                existing = body.get_named_range(name)
                if (
                    existing is not None
                    and existing._xml_element is not self._xml_element
                ):
                    existing.delete()
        self.set_attribute("table:name", name)

    @property
    def scope(self) -> str | None:
        """Return the declaration scope of the named range.

        A named range is local when declared inside a
        ``table:named-expressions`` element child of a ``table:table``
        element, and global when declared at the document body level.

        Returns:
            str | None: ``SCOPE_LOCAL`` or ``SCOPE_GLOBAL`` for a named range
            attached to a document, or None for an orphaned element.

        """
        parent = self.parent
        if parent is None or parent.tag != _TAG_NAMED_EXPRESSIONS:
            return None
        grand_parent = parent.parent
        if grand_parent is not None and grand_parent.tag == _TAG_TABLE:
            return SCOPE_LOCAL
        return SCOPE_GLOBAL

    @property
    def is_local(self) -> bool:
        """Return True if the named range is local to a table."""
        return self.scope == SCOPE_LOCAL

    @property
    def source_table_name(self) -> str | None:
        """Return the name of the table that declares the named range.

        For a local named range this is the owning table. For a global named
        range this is None, even when the range points at cells of some table:
        the declaration itself is not local to any table.

        Returns:
            str | None: The declaring table name, or None for a global or
            orphaned named range.

        """
        parent = self.parent
        if parent is None or parent.tag != _TAG_NAMED_EXPRESSIONS:
            return None
        grand_parent = parent.parent
        if grand_parent is not None and grand_parent.tag == _TAG_TABLE:
            return grand_parent.get_attribute_string("table:name")
        return None

    @property
    def source(self) -> str:
        """Return a human readable description of the range declaration.

        The description identifies the declaration scope, the owning table
        when local, the named range name and its cell range. It is intended
        for tracing which declaration is actually resolved by the collection
        rules.

        Returns:
            str: A description such as ``"global Input -> Sheet2.Z9"`` or
            ``"local Input @ Sheet1 -> Sheet1.A2"``.

        """
        scope = self.scope
        if scope == SCOPE_LOCAL:
            origin = f"local {self.name} @ {self.source_table_name}"
        elif scope == SCOPE_GLOBAL:
            origin = f"global {self.name}"
        else:
            origin = f"unbound {self.name}"
        return f"{origin} -> {self.table_name}.{self.address}"

    @property
    def address(self) -> str:
        """Return the cell range address without sheet name or ``$`` markers.

        Returns:
            str: An address such as ``"A1"`` or ``"A1:C2"``.

        """
        start = f"{digit_to_alpha(self.start[0])}{self.start[1] + 1}"
        if self.start == self.end:
            return start
        end = f"{digit_to_alpha(self.end[0])}{self.end[1] + 1}"
        return f"{start}:{end}"

    def set_table_name(self, name: str) -> None:
        """Set the name of the table associated with the named range.

        Args:
            name: The name of the table.

        Raises:
            TypeError: If `name` is not a string (propagated from
                `table_name_check`).
            ValueError: If `name` is empty or contains forbidden characters
                (propagated from `table_name_check`).

        """
        self.table_name = table_name_check(name)
        self._update_attributes()

    def _set_range(self, coord: tuple | list | str) -> None:
        """Set the cell range coordinates (internal helper).

        Args:
            coord: The cell or area coordinate, e.g., "A1", "A1:B2", (0, 0),
                or (0, 0, 1, 1).

        Raises:
            ValueError: If the coordinate format is incorrect.

        """
        digits = convert_coordinates(coord)
        if len(digits) == 4:
            x, y, z, t = digits
        else:
            x, y = digits
            z, t = digits
        if x is None or y is None or z is None or t is None:
            msg = f"Wrong format for cell range: {coord!r}"
            raise ValueError(msg)
        self.start = x, y
        self.end = z, t
        self.crange = x, y, z, t

    def set_range(
        self,
        crange: str | tuple[int, int] | tuple[int, int, int, int] | list[int],
    ) -> None:
        """Set the cell range for the named range.

        The range can be specified as a single cell (e.g., "A1", (0, 0)) or
        an area (e.g., "A1:B2", (0, 0, 1, 1)).

        Args:
            crange: The cell or area coordinate.

        Raises:
            ValueError: If the coordinate format is incorrect (propagated from
                `_set_range`).

        """
        self._set_range(crange)
        self._update_attributes()

    @staticmethod
    def _shift_coordinate(
        coordinate: int,
        position: int,
        delta: int,
        keep_first: bool,
    ) -> int:
        """Shift one boundary coordinate after rows or columns are inserted
        or deleted.

        Args:
            coordinate: The current 0-based boundary value.
            position: The 0-based position of the structural change.
            delta: The number of inserted (positive) or deleted (negative)
                rows or columns.
            keep_first: If True, the start boundary keeps its position when
                the change happens exactly at the boundary; the end boundary
                moves instead.

        Returns:
            int: The updated boundary value, never negative.

        """
        if delta > 0:
            if coordinate > position or (coordinate == position and not keep_first):
                return coordinate + delta
            return coordinate
        shift = -delta
        if coordinate < position:
            return coordinate
        if coordinate >= position + shift:
            return max(coordinate - shift, 0)
        return max(position, 0)

    def shift_rows(self, y: int, delta: int) -> bool:
        """Shift the row boundaries of the range after rows are inserted or
        deleted at position ``y``.

        Inserting rows (``delta`` > 0) before a boundary moves it downward;
        deleting rows (``delta`` < 0) moves it upward and may collapse the
        range. The start boundary is not moved when rows are inserted exactly
        at the start, while the end boundary is.

        Args:
            y: The 0-based position of the inserted or deleted rows.
            delta: The count of inserted (positive) or deleted (negative)
                rows.

        Returns:
            bool: True if the range coordinates changed.

        """
        if not delta:
            return False
        start_x, start_y, end_x, end_y = self.crange
        # A single-row range has coincident row boundaries that must move
        # together, otherwise the point range would be expanded.
        keep_first = start_y != end_y
        new_start_y = self._shift_coordinate(start_y, y, delta, keep_first)
        new_end_y = self._shift_coordinate(end_y, y, delta, keep_first=False)
        if (new_start_y, new_end_y) == (start_y, end_y):
            return False
        self.set_range((start_x, new_start_y, end_x, new_end_y))
        return True

    def shift_columns(self, x: int, delta: int) -> bool:
        """Shift the column boundaries of the range after columns are inserted
        or deleted at position ``x``.

        Inserting columns (``delta`` > 0) before a boundary moves it rightward;
        deleting columns (``delta`` < 0) moves it leftward and may collapse the
        range. The start boundary is not moved when columns are inserted
        exactly at the start, while the end boundary is.

        Args:
            x: The 0-based position of the inserted or deleted columns.
            delta: The count of inserted (positive) or deleted (negative)
                columns.

        Returns:
            bool: True if the range coordinates changed.

        """
        if not delta:
            return False
        start_x, start_y, end_x, end_y = self.crange
        # A single-column range has coincident column boundaries that must
        # move together, otherwise the point range would be expanded.
        keep_first = start_x != end_x
        new_start_x = self._shift_coordinate(start_x, x, delta, keep_first)
        new_end_x = self._shift_coordinate(end_x, x, delta, keep_first=False)
        if (new_start_x, new_end_x) == (start_x, end_x):
            return False
        self.set_range((new_start_x, start_y, new_end_x, end_y))
        return True

    def _update_attributes(self) -> None:
        """Update the `table:base-cell-address` and `table:cell-range-address`
        attributes based on the current named range's properties.
        """
        self.set_attribute("table:base-cell-address", self._make_base_cell_address())
        self.set_attribute("table:cell-range-address", self._make_cell_range_address())

    def _make_base_cell_address(self) -> str:
        """Construct the `table:base-cell-address` string for the named range.

        Returns:
            str: The formatted base cell address (e.g., "$'Sheet Name'.A1").

        """
        # assuming we got table_name and range
        if " " in self.table_name:
            name = f"'{self.table_name}'"
        else:
            name = self.table_name
        return f"${name}.${digit_to_alpha(self.start[0])}${self.start[1] + 1}"

    def _make_cell_range_address(self) -> str:
        """Construct the `table:cell-range-address` string for the named
        range.

        Returns:
            str: The formatted cell range address (e.g.,
                "$'Sheet Name'.A1:$'Sheet Name'.B2").

        """
        # assuming we got table_name and range
        if " " in self.table_name:
            name = f"'{self.table_name}'"
        else:
            name = self.table_name
        if self.start == self.end:
            return self._make_base_cell_address()
        return (
            f"${name}.${digit_to_alpha(self.start[0])}${self.start[1] + 1}:"
            f".${digit_to_alpha(self.end[0])}${self.end[1] + 1}"
        )

    def get_values(
        self,
        cell_type: str | None = None,
        complete: bool = True,
        get_type: bool = False,
        flat: bool = False,
    ) -> list:
        """Retrieve the values of all cells within the named range.

        This is a shortcut to `Table.get_values()` method, applied to the
        table and range defined by this named range.

        Args:
            cell_type: Filter cells by their type (e.g., "string", "float").
            complete: If True, returns a rectangular list, filling empty
                cells with None. If False, returns only non-empty cells.
            get_type: If True, returns (value, type) tuples for each cell.
            flat: If True, returns a flat list of values.

        Returns:
            list: A list of cell values, formatted according to the arguments.

        Raises:
            ValueError: If the named range's table is not found or not inside
                a document.

        """
        body = self.document_body
        if not body:
            msg = "Table is not inside a document"
            raise ValueError(msg)
        table = body.get_table(name=self.table_name)
        if table is None:
            msg = f"Table not found: {self.table_name!r}"
            raise ValueError(msg)
        return table.get_values(self.crange, cell_type, complete, get_type, flat)

    def get_value(self, get_type: bool = False) -> Any:
        """Retrieve the value of the first cell of the named range.

        This is a shortcut to `Table.get_value()` method, applied to the
        first cell defined by this named range.

        Args:
            get_type: If True, returns a tuple of (value, type) for the cell.

        Returns:
            Any | tuple[Any, str]: The cell's value, or a tuple of (value,
                type) if `get_type` is True.

        Raises:
            ValueError: If the named range's table is not found or not inside
                a document.

        """
        body = self.document_body
        if not body:
            msg = "Table is not inside a document"
            raise ValueError(msg)
        table: Table | None = body.get_table(name=self.table_name)
        if table is None:
            msg = f"Table not found: {self.table_name!r}"
            raise ValueError(msg)
        return table.get_value(self.start, get_type)

    def set_values(
        self,
        values: list,
        style: str | None = None,
        cell_type: str | None = None,
        currency: str | None = None,
    ) -> None:
        """Set the values of a range of cells within the named range.

        This is a shortcut to `Table.set_values()` method, applied to the
        table and range defined by this named range.

        Args:
            values: A list of lists representing the new values for the cells.
            style: The style name to apply to the cells.
            cell_type: The type to set for the cells (e.g., "string",
                "float").
            currency: The currency symbol to use for "currency" type cells.

        Raises:
            ValueError: If the named range's table is not found or not inside
                a document.

        """
        body = self.document_body
        if not body:
            msg = "Table is not inside a document"
            raise ValueError(msg)
        table = body.get_table(name=self.table_name)
        if table is None:
            msg = f"Table not found: {self.table_name!r}"
            raise ValueError(msg)
        table.set_values(
            values,
            coord=self.crange,
            style=style,
            cell_type=cell_type,
            currency=currency,
        )

    def set_value(
        self,
        value: Any,
        cell_type: str | None = None,
        currency: str | None = None,
        style: str | None = None,
    ) -> None:
        """Set the value of the first cell within the named range.

        This is a shortcut to `Table.set_value()` method, applied to the first
        cell defined by this named range.

        Args:
            value: The value to set for the cell.
            cell_type: The type to set for the cell (e.g., "string", "float").
            currency: The currency symbol to use for "currency" type cells.
            style: The style name to apply to the cell.

        Raises:
            ValueError: If the named range's table is not found or not inside
                a document.

        """
        body = self.document_body
        if not body:
            msg = "Table is not inside a document"
            raise ValueError(msg)
        table = body.get_table(name=self.table_name)
        if table is None:
            msg = f"Table not found: {self.table_name!r}"
            raise ValueError(msg)
        table.set_value(
            coord=self.start,
            value=value,
            cell_type=cell_type,
            currency=currency,
            style=style,
        )


register_element_class(NamedRange)
