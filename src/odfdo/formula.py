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
"""Formula parsing, dependencies and cached values for spreadsheets.

OpenDocument spreadsheets store, for each formula cell, the formula itself
(``table:formula``) and the most recently computed result (``office:value``,
``office:string-value``, ...). odfdo never runs a spreadsheet application, so
it can not recompute formulas. It can however:

- parse formulas to find the cells they reference, including references to
  other sheets, ranges, and absolute/relative row and column markers,
- record which formula depends on which cell,
- mark the cached results of directly and indirectly affected formulas as
  stale as soon as a referenced cell changes,
- evaluate a small, documented subset of formulas so simple caches can be
  refreshed safely.

Anything not explicitly supported (unknown function, unparsable formula,
circular reference, stale precedent, ...) is reported as ``unknown`` or as an
error instead of being guessed.

This module is independent from the XML layer: :class:`FormulaCache` works
with table names and 0-based ``(column, row)`` coordinates. The
:class:`~odfdo.table.Table` and :class:`~odfdo.document.Document` classes
provide thin adapters.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from .element import _get_lxml_tag, xpath_compile

if TYPE_CHECKING:
    from .cell import Cell
    from .element import Element
    from .table import Table

# Maximum number of cells expanded for a whole column or whole row reference.
_MAX_EXPAND = 4096

_FORMULA_PREFIX = re.compile(r"^\s*(?:of|oooc)?:\s*(?:=)?", re.IGNORECASE)


class FormulaError(ValueError):
    """Raised when a formula can not be parsed."""


class UnknownFunctionError(FormulaError):
    """Raised when a formula uses a function odfdo does not evaluate."""

    def __init__(self, name: str) -> None:
        """Initialize the error for the given function name."""
        super().__init__(f"Unknown function: {name!r}")
        self.name = name


class FormulaUnsupportedError(Exception):
    """Internal error signalling an unsupported formula construct."""


class FormulaEvaluationError(Exception):
    """Internal error signalling e.g. division by zero."""

    def __init__(self, code: str) -> None:
        """Initialize with an OpenFormula error code."""
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Formula text parsing
# ---------------------------------------------------------------------------

_CELL_RE = re.compile(
    r"""
    ^
    (?P<cabs>\$)?(?P<col>[A-Za-z]{1,3})
    (?P<rabs>\$)?(?P<row>[0-9]+)
    $
    """,
    re.VERBOSE,
)
_OPEN_REFERENCE = re.compile(r"\[(?P<body>[^\]]*)\]")
_QUOTED_SHEET = re.compile(r"^'(?P<name>(?:[^']|'')+)'$")


def _split_sheet(text: str) -> tuple[str | None, str]:
    """Split an OpenFormula address into its sheet name and cell part.

    Handles ``.A1``, ``$Sheet1.$B$2`` and ``'My Sheet'.$A$1``. The ``.``
    prefix means "the current sheet"; a leading ``$`` is a sheet marker only
    when followed by a dot, otherwise it belongs to the cell address.
    """
    text = text.strip()
    if text.startswith("."):
        text = text[1:]
    if text.startswith("'"):
        index = 1
        while index < len(text):
            if text[index] == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                break
            index += 1
        if index >= len(text):
            return None, ""
        sheet_text = text[: index + 1]
        rest = text[index + 1 :]
        if not rest.startswith("."):
            return None, ""
        match = _QUOTED_SHEET.match(sheet_text)
        if match is None:
            return None, ""
        return match.group("name").replace("''", "'"), rest[1:]
    position = text.rfind(".")
    if position < 0:
        return None, text
    sheet_text = text[:position]
    if sheet_text.startswith("$"):
        sheet_text = sheet_text[1:]
    if not sheet_text:
        return None, text[position + 1 :]
    if "$" in sheet_text or "'" in sheet_text:
        return None, ""
    return sheet_text, text[position + 1 :]


def _parse_address(text: str) -> tuple[FormulaReference | None, bool]:
    """Parse a full address (start cell or range).

    Returns the reference and whether a sheet was explicitly given.
    """
    text = text.strip()
    if ":" in text:
        start_text, end_text = text.split(":", 1)
        sheet, start_cell = _split_sheet(start_text)
        end_sheet, end_cell = _split_sheet(end_text)
        if end_sheet is not None:
            if sheet is None:
                sheet = end_sheet
            elif end_sheet != sheet:
                return None, False
        explicit_sheet = sheet is not None
    else:
        sheet, start_cell = _split_sheet(text)
        end_cell = start_cell
        explicit_sheet = sheet is not None
    start = _parse_cell_address(start_cell)
    end = _parse_cell_address(end_cell)
    if start is None or end is None:
        return None, False
    x, y, abs_col, abs_row = start
    z, t, end_abs_col, end_abs_row = end
    if z < x:
        x, z = z, x
        abs_col, end_abs_col = end_abs_col, abs_col
    if t < y:
        y, t = t, y
        abs_row, end_abs_row = end_abs_row, abs_row
    reference = FormulaReference(
        x,
        y,
        z,
        t,
        sheet,
        abs_col,
        abs_row,
        end_abs_col,
        end_abs_row,
    )
    return reference, explicit_sheet


def _column_to_number(letters: str) -> int:
    """Convert an alphabetic column name to a 0-based index."""
    column = 0
    for char in letters.upper():
        column = column * 26 + (ord(char) - ord("A") + 1)
    return column - 1


def _number_to_column(number: int) -> str:
    """Convert a 0-based column index to its alphabetic name."""
    number += 1
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _parse_cell_address(text: str) -> tuple[int, int, bool, bool] | None:
    """Parse ``B4``, ``$B$4``, ``$B4`` or ``B$4``."""
    match = _CELL_RE.match(text.strip())
    if not match:
        return None
    x = _column_to_number(match.group("col"))
    y = int(match.group("row")) - 1
    if y < 0:
        return None
    return x, y, bool(match.group("cabs")), bool(match.group("rabs"))


class FormulaReference:
    """A cell or range reference found in a formula.

    Coordinates are 0-based. A single cell has ``x == z`` and ``y == t``.
    ``sheet`` is None when the reference is relative to the formula's own
    sheet, and holds the sheet name for cross-sheet references.
    ``absolute_column`` and ``absolute_row`` record the ``$`` markers.
    """

    __slots__ = (
        "absolute_column",
        "absolute_row",
        "end_absolute_column",
        "end_absolute_row",
        "sheet",
        "t",
        "x",
        "y",
        "z",
    )

    def __init__(
        self,
        x: int,
        y: int,
        z: int | None = None,
        t: int | None = None,
        sheet: str | None = None,
        absolute_column: bool = False,
        absolute_row: bool = False,
        end_absolute_column: bool | None = None,
        end_absolute_row: bool | None = None,
    ) -> None:
        """Initialize a reference.

        ``end_absolute_column`` and ``end_absolute_row`` describe the end of
        a range; they default to the start markers for a single cell.
        """
        self.x = x
        self.y = y
        self.z = x if z is None else z
        self.t = y if t is None else t
        self.sheet = sheet
        self.absolute_column = absolute_column
        self.absolute_row = absolute_row
        if end_absolute_column is None:
            end_absolute_column = absolute_column
        if end_absolute_row is None:
            end_absolute_row = absolute_row
        self.end_absolute_column = end_absolute_column
        self.end_absolute_row = end_absolute_row

    def __repr__(self) -> str:
        """Return a debug representation."""
        return f"FormulaReference({self.qualified_address})"

    def __eq__(self, other: object) -> bool:
        """Compare two references."""
        if not isinstance(other, FormulaReference):
            return NotImplemented
        return (
            self.x,
            self.y,
            self.z,
            self.t,
            self.sheet,
            self.absolute_column,
            self.absolute_row,
            self.end_absolute_column,
            self.end_absolute_row,
        ) == (
            other.x,
            other.y,
            other.z,
            other.t,
            other.sheet,
            other.absolute_column,
            other.absolute_row,
            other.end_absolute_column,
            other.end_absolute_row,
        )

    def __hash__(self) -> int:
        """Hash a reference."""
        return hash(
            (
                self.x,
                self.y,
                self.z,
                self.t,
                self.sheet,
                self.absolute_column,
                self.absolute_row,
                self.end_absolute_column,
                self.end_absolute_row,
            )
        )

    @property
    def is_single(self) -> bool:
        """Return True if the reference designates a single cell."""
        return self.x == self.z and self.y == self.t

    @property
    def is_range(self) -> bool:
        """Return True if the reference designates more than one cell."""
        return not self.is_single

    @property
    def is_cross_sheet(self) -> bool:
        """Return True if the reference explicitly names a sheet."""
        return self.sheet is not None

    @property
    def is_absolute(self) -> bool:
        """Return True if the whole reference is absolute.

        A range is absolute only when both of its ends are fully absolute.
        """
        return (
            self.absolute_column
            and self.absolute_row
            and self.end_absolute_column
            and self.end_absolute_row
        )

    @property
    def is_relative(self) -> bool:
        """Return True if no part of the reference is absolute."""
        return (
            not self.absolute_column
            and not self.absolute_row
            and not self.end_absolute_column
            and not self.end_absolute_row
        )

    @property
    def address(self) -> str:
        """Return the A1 style address of the reference."""
        start = f"{'$' if self.absolute_column else ''}{_number_to_column(self.x)}"
        start += f"{'$' if self.absolute_row else ''}{self.y + 1}"
        if self.is_single:
            return start
        end = f"{'$' if self.end_absolute_column else ''}{_number_to_column(self.z)}"
        end += f"{'$' if self.end_absolute_row else ''}{self.t + 1}"
        return f"{start}:{end}"

    @property
    def qualified_address(self) -> str:
        """Return the address prefixed with the sheet name, if any."""
        if self.sheet is None:
            return self.address
        sheet = self.sheet
        if any(char in sheet for char in " .'$"):
            sheet = "'" + sheet.replace("'", "''") + "'"
        return f"{sheet}.{self.address}"

    def cells(
        self,
        max_columns: int = _MAX_EXPAND,
        max_rows: int = _MAX_EXPAND,
    ) -> Iterator[tuple[int, int]]:
        """Yield the ``(x, y)`` coordinates covered by the reference.

        Whole-column/whole-row references are clamped to a maximum size.
        """
        width = min(self.z - self.x + 1, max_columns)
        height = min(self.t - self.y + 1, max_rows)
        for y in range(self.y, self.y + height):
            for x in range(self.x, self.x + width):
                yield x, y


def extract_references(formula: str | None) -> list[FormulaReference]:
    """Return the cell/range references used by an OpenFormula formula.

    ODF stores all references with the bracket syntax (``[.A1]``,
    ``[$Sheet1.$B$2]``, ``[.A1:.B4]``), so both cross-sheet references and
    absolute markers are recovered exactly.

    Args:
        formula: The formula text, with or without the ``of:`` prefix.

    Returns:
        The references, in the order they appear.

    Raises:
        FormulaError: If a bracket reference can not be understood.

    """
    if not formula:
        return []
    text = _FORMULA_PREFIX.sub("", formula.strip())
    references: list[FormulaReference] = []
    remaining = text
    for match in _OPEN_REFERENCE.finditer(text):
        reference, _ = _parse_address(match.group("body"))
        if reference is None:
            msg = f"Malformed reference in formula: {match.group(0)!r}"
            raise FormulaError(msg)
        references.append(reference)
        remaining = remaining.replace(match.group(0), "", 1)
    if "[" in remaining or "]" in remaining:
        msg = f"Unbalanced reference in formula: {formula!r}"
        raise FormulaError(msg)
    return references


def formula_functions(formula: str | None) -> list[str]:
    """Return the names of the functions called by a formula.

    Function names are upper-cased. References are masked first so column
    names are never mistaken for function names.
    """
    if not formula:
        return []
    text = _FORMULA_PREFIX.sub("", formula.strip())
    text = _OPEN_REFERENCE.sub(" [] ", text)
    return [
        match.group(1).upper()
        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", text)
    ]


# ---------------------------------------------------------------------------
# Formula AST and parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumberLiteral:
    """A numeric literal."""

    value: Decimal


@dataclass(frozen=True)
class StringLiteral:
    """A string literal."""

    value: str


@dataclass(frozen=True)
class BooleanLiteral:
    """A boolean literal."""

    value: bool


@dataclass(frozen=True)
class ReferenceNode:
    """A parsed reference."""

    reference: FormulaReference


@dataclass(frozen=True)
class FunctionNode:
    """A function call."""

    name: str
    arguments: tuple[FormulaNode, ...]


@dataclass(frozen=True)
class BinaryOperation:
    """A binary operation."""

    operator: str
    left: FormulaNode
    right: FormulaNode


@dataclass(frozen=True)
class UnaryOperation:
    """A unary operation."""

    operator: str
    operand: FormulaNode


FormulaNode = (
    NumberLiteral
    | StringLiteral
    | BooleanLiteral
    | ReferenceNode
    | FunctionNode
    | BinaryOperation
    | UnaryOperation
)

_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<reference>\[[^\]]*\])
  | (?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)
  | (?P<string>"(?:[^"]|"")*")
  | (?P<identifier>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<operator><>|<=|>=|[-+*/^&=<>%])
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<semicolon>;)
    """,
    re.VERBOSE,
)


def _tokenize(formula: str) -> list[tuple[str, str]]:
    """Split a formula into ``(kind, text)`` tokens."""
    text = _FORMULA_PREFIX.sub("", formula.strip())
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if not match:
            msg = f"Unexpected character in formula at {position}: {text[position]!r}"
            raise FormulaError(msg)
        position = match.end()
        kind = match.lastgroup
        if kind is None:  # pragma: no cover
            msg = f"Unexpected token in formula: {match.group(0)!r}"
            raise FormulaError(msg)
        if kind != "space":
            tokens.append((kind, match.group(0)))
    return tokens


class _Parser:
    """Recursive descent parser for the supported OpenFormula subset."""

    def __init__(self, formula: str) -> None:
        """Initialize the parser with formula text."""
        self.tokens = _tokenize(formula)
        self.position = 0

    def peek(self) -> tuple[str, str] | None:
        """Return the next token without consuming it."""
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

    def pop(self) -> tuple[str, str]:
        """Consume and return the next token."""
        token = self.peek()
        if token is None:
            msg = "Unexpected end of formula"
            raise FormulaError(msg)
        self.position += 1
        return token

    def accept(self, kind: str) -> str | None:
        """Consume a token of the given kind if present."""
        token = self.peek()
        if token is not None and token[0] == kind:
            self.position += 1
            return token[1]
        return None

    def parse(self) -> FormulaNode:
        """Parse the whole formula and return its AST."""
        node = self.parse_comparison()
        if self.peek() is not None:
            msg = f"Unexpected token in formula: {self.peek()!r}"
            raise FormulaError(msg)
        return node

    def parse_comparison(self) -> FormulaNode:
        """Parse a comparison expression."""
        node = self.parse_additive()
        while True:
            token = self.peek()
            if token is None or token[1] not in ("=", "<>", "<", ">", "<=", ">="):
                return node
            operator = self.pop()[1]
            node = BinaryOperation(operator, node, self.parse_additive())

    def parse_additive(self) -> FormulaNode:
        """Parse addition and subtraction."""
        node = self.parse_multiplicative()
        while True:
            token = self.peek()
            if token is None or token[1] not in ("+", "-"):
                return node
            operator = self.pop()[1]
            node = BinaryOperation(operator, node, self.parse_multiplicative())

    def parse_multiplicative(self) -> FormulaNode:
        """Parse multiplication, division and concatenation."""
        node = self.parse_power()
        while True:
            token = self.peek()
            if token is None or token[1] not in ("*", "/", "&"):
                return node
            operator = self.pop()[1]
            node = BinaryOperation(operator, node, self.parse_power())

    def parse_power(self) -> FormulaNode:
        """Parse exponentiation."""
        node = self.parse_unary()
        token = self.peek()
        if token is not None and token[1] == "^":
            self.pop()
            return BinaryOperation("^", node, self.parse_power())
        return node

    def parse_unary(self) -> FormulaNode:
        """Parse unary plus/minus and percent suffix."""
        token = self.peek()
        if token is not None and token[1] in ("+", "-"):
            operator = self.pop()[1]
            return UnaryOperation(operator, self.parse_unary())
        node = self.parse_primary()
        while True:
            token = self.peek()
            if token is None or token[1] != "%":
                return node
            self.pop()
            node = BinaryOperation("/", node, NumberLiteral(Decimal(100)))

    def parse_primary(self) -> FormulaNode:
        """Parse a literal, reference, function call or parenthesized expr."""
        kind, text = self.pop()
        if kind == "number":
            return NumberLiteral(Decimal(text))
        if kind == "string":
            return StringLiteral(text[1:-1].replace('""', '"'))
        if kind == "reference":
            reference, _ = _parse_address(text[1:-1])
            if reference is None:
                msg = f"Malformed reference in formula: {text!r}"
                raise FormulaError(msg)
            return ReferenceNode(reference)
        if kind == "identifier":
            name = text.upper()
            if self.accept("lparen") is not None:
                arguments: list[FormulaNode] = []
                if self.accept("rparen") is None:
                    while True:
                        arguments.append(self.parse_comparison())
                        if self.accept("comma") or self.accept("semicolon"):
                            continue
                        if self.accept("rparen"):
                            break
                        msg = f"Malformed argument list for {name!r}"
                        raise FormulaError(msg)
                return FunctionNode(name, tuple(arguments))
            if name == "TRUE":
                return BooleanLiteral(True)
            if name == "FALSE":
                return BooleanLiteral(False)
            msg = f"Unexpected identifier in formula: {text!r}"
            raise FormulaError(msg)
        if kind == "lparen":
            node = self.parse_comparison()
            if self.accept("rparen") is None:
                msg = "Missing closing parenthesis in formula"
                raise FormulaError(msg)
            return node
        msg = f"Unexpected token in formula: {text!r}"
        raise FormulaError(msg)


def parse_formula(formula: str | None) -> FormulaNode | None:
    """Parse an OpenFormula formula into an AST.

    Args:
        formula: The formula text, with or without the ``of:`` prefix.

    Returns:
        The AST, or None when the formula is empty.

    Raises:
        FormulaError: If the formula can not be parsed.

    """
    if not formula or not formula.strip():
        return None
    return _Parser(formula).parse()


# ---------------------------------------------------------------------------
# Conservative evaluation
# ---------------------------------------------------------------------------

#: Functions evaluated by odfdo. Any other function makes the formula
#: unsupported: its cached value is never guessed.
SUPPORTED_FUNCTIONS = frozenset(
    {
        "ABS",
        "AND",
        "AVERAGE",
        "CONCAT",
        "CONCATENATE",
        "COUNT",
        "COUNTA",
        "FALSE",
        "IF",
        "INT",
        "MAX",
        "MIN",
        "MOD",
        "NOT",
        "OR",
        "PRODUCT",
        "ROUND",
        "SUM",
        "TRUE",
    }
)


@dataclass(frozen=True)
class EvaluationResult:
    """Outcome of the evaluation of a formula.

    Attributes:
        value: The computed value, or None when the formula could not be
            evaluated.
        error: ``None`` on success, ``"unsupported"`` when the formula uses
            something odfdo does not evaluate (unknown function, unsupported
            argument, ...), the name of the error otherwise (``#DIV/0!``,
            ``#VALUE!``, ``#CIRC!``, ``#STALE!``...).
        functions: Names of the functions used by the formula.

    """

    value: Any = None
    error: str | None = None
    functions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Return True if the formula was evaluated without error."""
        return self.error is None

    @property
    def supported(self) -> bool:
        """Return True if the formula is within the supported subset."""
        return self.error != "unsupported"


def _as_decimal(value: Any) -> Decimal:
    """Convert a value to Decimal, raising a formula error if impossible."""
    if isinstance(value, bool):
        return Decimal(1 if value else 0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise FormulaEvaluationError("#VALUE!") from exc
    if value is None:
        return Decimal(0)
    raise FormulaEvaluationError("#VALUE!")


def _as_text(value: Any) -> str:
    """Convert a value to its textual representation."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _flatten(values: Iterable[Any]) -> list[Any]:
    """Flatten nested iterables (ranges) into a list."""
    result: list[Any] = []
    for value in values:
        if isinstance(value, list):
            result.extend(_flatten(value))
        else:
            result.append(value)
    return result


class FormulaEvaluator:
    """Evaluate the supported subset of OpenFormula.

    The evaluator is given a ``resolver`` callable returning the value of a
    cell (a list for a range). It never guesses: unsupported constructs raise
    :class:`FormulaUnsupportedError`, reported as
    ``EvaluationResult(error="unsupported")``.
    """

    def __init__(self) -> None:
        """Initialize the evaluator."""
        self.functions: list[str] = []

    def evaluate(self, node: FormulaNode, resolver: Any) -> EvaluationResult:
        """Evaluate an AST with the given cell resolver."""
        try:
            value = self._eval(node, resolver)
        except FormulaUnsupportedError:
            return EvaluationResult(None, "unsupported", tuple(self.functions))
        except FormulaEvaluationError as error:
            return EvaluationResult(None, error.code, tuple(self.functions))
        except (ArithmeticError, InvalidOperation):
            return EvaluationResult(None, "#VALUE!", tuple(self.functions))
        return EvaluationResult(value, None, tuple(self.functions))

    def _eval(self, node: FormulaNode, resolver: Any) -> Any:
        if isinstance(node, NumberLiteral):
            return node.value
        if isinstance(node, StringLiteral):
            return node.value
        if isinstance(node, BooleanLiteral):
            return node.value
        if isinstance(node, ReferenceNode):
            return resolver(node.reference)
        if isinstance(node, UnaryOperation):
            value = _as_decimal(self._eval(node.operand, resolver))
            return value if node.operator == "+" else -value
        if isinstance(node, BinaryOperation):
            return self._eval_binary(node, resolver)
        if isinstance(node, FunctionNode):
            return self._eval_function(node, resolver)
        msg = f"Unsupported node: {node!r}"  # pragma: no cover
        raise FormulaUnsupportedError(msg)  # pragma: no cover

    def _eval_binary(self, node: BinaryOperation, resolver: Any) -> Any:
        operator = node.operator
        left = self._eval(node.left, resolver)
        right = self._eval(node.right, resolver)
        if operator == "&":
            return _as_text(left) + _as_text(right)
        if operator in ("=", "<>", "<", ">", "<=", ">="):
            return self._compare(operator, left, right)
        left_number = _as_decimal(left)
        right_number = _as_decimal(right)
        if operator == "+":
            return left_number + right_number
        if operator == "-":
            return left_number - right_number
        if operator == "*":
            return left_number * right_number
        if operator == "/":
            if right_number == 0:
                raise FormulaEvaluationError("#DIV/0!")
            return left_number / right_number
        if operator == "^":
            return left_number**right_number
        msg = f"Unsupported operator: {operator!r}"  # pragma: no cover
        raise FormulaUnsupportedError(msg)  # pragma: no cover

    @staticmethod
    def _compare(operator: str, left: Any, right: Any) -> bool:
        if isinstance(left, str) or isinstance(right, str):
            left_value: Any = _as_text(left)
            right_value: Any = _as_text(right)
        else:
            left_value = _as_decimal(left)
            right_value = _as_decimal(right)
        if operator == "=":
            return left_value == right_value
        if operator == "<>":
            return left_value != right_value
        if operator == "<":
            return left_value < right_value
        if operator == ">":
            return left_value > right_value
        if operator == "<=":
            return left_value <= right_value
        return left_value >= right_value

    def _eval_function(self, node: FunctionNode, resolver: Any) -> Any:
        name = node.name
        self.functions.append(name)
        if name not in SUPPORTED_FUNCTIONS:
            raise FormulaUnsupportedError(name)
        if name == "TRUE":
            return True
        if name == "FALSE":
            return False
        if name == "IF":
            if len(node.arguments) not in (2, 3):
                raise FormulaEvaluationError("#VALUE!")
            condition = self._eval(node.arguments[0], resolver)
            truth = condition if isinstance(condition, bool) else bool(_as_decimal(condition))
            if truth:
                return self._eval(node.arguments[1], resolver)
            if len(node.arguments) == 3:
                return self._eval(node.arguments[2], resolver)
            return False
        values = self._eval_arguments(node.arguments, resolver)
        if name == "AND":
            return all(bool(_as_decimal(value)) for value in values)
        if name == "OR":
            return any(bool(_as_decimal(value)) for value in values)
        if name == "NOT":
            self._require_arity(values, 1)
            return not bool(_as_decimal(values[0]))
        if name in ("CONCAT", "CONCATENATE"):
            return "".join(_as_text(value) for value in values)
        numbers = [
            _as_decimal(value) for value in values if not isinstance(value, str | bool)
        ]
        if name == "SUM":
            return sum(numbers, Decimal(0))
        if name == "PRODUCT":
            result = Decimal(1)
            for number in numbers:
                result *= number
            return result
        if name == "AVERAGE":
            if not numbers:
                raise FormulaEvaluationError("#DIV/0!")
            return sum(numbers, Decimal(0)) / Decimal(len(numbers))
        if name == "MIN":
            return min(numbers) if numbers else Decimal(0)
        if name == "MAX":
            return max(numbers) if numbers else Decimal(0)
        if name == "COUNT":
            return Decimal(len(numbers))
        if name == "COUNTA":
            return Decimal(len([value for value in values if value is not None]))
        if name == "ABS":
            self._require_arity(values, 1)
            return abs(_as_decimal(values[0]))
        if name == "INT":
            self._require_arity(values, 1)
            return Decimal(int(_as_decimal(values[0])))
        if name == "MOD":
            self._require_arity(values, 2)
            divisor = _as_decimal(values[1])
            if divisor == 0:
                raise FormulaEvaluationError("#DIV/0!")
            return _as_decimal(values[0]) % divisor
        if name == "ROUND":
            if len(values) not in (1, 2):
                raise FormulaEvaluationError("#VALUE!")
            digits = 0 if len(values) == 1 else int(_as_decimal(values[1]))
            return _as_decimal(values[0]).quantize(Decimal(1).scaleb(-digits))
        msg = f"Unsupported function: {name!r}"  # pragma: no cover
        raise FormulaUnsupportedError(msg)  # pragma: no cover

    def _eval_arguments(
        self,
        arguments: tuple[FormulaNode, ...],
        resolver: Any,
    ) -> list[Any]:
        values: list[Any] = []
        for argument in arguments:
            values.extend(_flatten([self._eval(argument, resolver)]))
        return values

    @staticmethod
    def _require_arity(values: list[Any], expected: int) -> None:
        if len(values) != expected:
            raise FormulaEvaluationError("#VALUE!")


# ---------------------------------------------------------------------------
# Dependency graph and cached values
# ---------------------------------------------------------------------------


@dataclass
class FormulaCell:
    """A formula cell tracked by a :class:`FormulaCache`.

    Attributes:
        table: Name of the table holding the cell (None if the table has no
            name).
        x: 0-based column index.
        y: 0-based row index.
        formula: The formula text.
        references: The references found in the formula.
        stale: True if the cached value can no longer be trusted.
        reason: Human readable reason for the staleness.
        cached_value: The last known cached value, possibly stale.
        value_type: ODF value type of the cached value, if known.
        fingerprint: Optional caller-provided snapshot of the cell state,
            kept for introspection.
        parse_error: True when the formula text could not be parsed. Such a
            formula has unknown dependencies, so its cached value is never
            trusted.

    """

    table: str | None
    x: int
    y: int
    formula: str
    references: list[FormulaReference] = field(default_factory=list)
    stale: bool = False
    reason: str | None = None
    cached_value: Any = None
    value_type: str | None = None
    fingerprint: tuple[Any, ...] | None = None
    parse_error: bool = False

    @property
    def key(self) -> tuple[str | None, int, int]:
        """Return the ``(table, x, y)`` identity of the cell."""
        return self.table, self.x, self.y

    @property
    def cross_sheet(self) -> bool:
        """Return True if the formula references another sheet."""
        return any(reference.is_cross_sheet for reference in self.references)

    @property
    def absolute_references(self) -> list[FormulaReference]:
        """Return the references using the ``$`` marker."""
        return [
            reference
            for reference in self.references
            if reference.absolute_column or reference.absolute_row
        ]

    @property
    def relative_references(self) -> list[FormulaReference]:
        """Return the references without any ``$`` marker."""
        return [
            reference
            for reference in self.references
            if not reference.absolute_column and not reference.absolute_row
        ]

    @property
    def unsupported_functions(self) -> list[str]:
        """Return the functions of the formula that odfdo can not evaluate."""
        return [
            name
            for name in formula_functions(self.formula)
            if name not in SUPPORTED_FUNCTIONS
        ]


class FormulaCache:
    """Track formulas, dependencies and cached values across tables.

    A formula cell is identified by ``(table_name, x, y)``. References
    prefixed with another sheet name are resolved against that sheet.
    Unqualified references point to the formula's own sheet.

    When a cell changes, every formula referencing it is marked stale, then
    staleness propagates to formulas depending on stale formulas. A formula
    whose text did not change is still marked stale when one of its
    precedents changed: the old cached value must not be reused.

    Example:
        >>> cache = FormulaCache()
        >>> cache.set_formula("Sheet1", 3, 3, "of:=[.A1]+[.B1]", 12)
        >>> cache.get_formula("Sheet1", 3, 3).stale
        False
        >>> cache.invalidate_cell("Sheet1", 0, 0)
        [FormulaCell(...)]
        >>> cache.get_formula("Sheet1", 3, 3).stale
        True

    """

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self._formulas: dict[tuple[str | None, int, int], FormulaCell] = {}
        # reverse index: cell key -> formula keys depending on it
        self._dependencies: dict[
            tuple[str | None, int, int], set[tuple[str | None, int, int]]
        ] = {}
        self._version = 0

    def __len__(self) -> int:
        """Return the number of tracked formulas."""
        return len(self._formulas)

    def __contains__(self, key: object) -> bool:
        """Return True if the given ``(table, x, y)`` key is a formula cell."""
        return key in self._formulas

    def __iter__(self) -> Iterator[FormulaCell]:
        """Iterate over the tracked formula cells."""
        return iter(self._formulas.values())

    def __repr__(self) -> str:
        """Return a debug representation."""
        return f"<FormulaCache formulas={len(self._formulas)}>"

    @property
    def version(self) -> int:
        """Return a monotonic version, incremented on every change."""
        return self._version

    def clear(self) -> None:
        """Forget all formulas, dependencies and cached values."""
        self._formulas.clear()
        self._dependencies.clear()
        self._version += 1

    def copy(self) -> FormulaCache:
        """Return an independent copy of this cache."""
        clone = FormulaCache()
        for key, cell in self._formulas.items():
            clone._formulas[key] = FormulaCell(
                table=cell.table,
                x=cell.x,
                y=cell.y,
                formula=cell.formula,
                references=list(cell.references),
                stale=cell.stale,
                reason=cell.reason,
                cached_value=cell.cached_value,
                value_type=cell.value_type,
                fingerprint=cell.fingerprint,
                parse_error=cell.parse_error,
            )
        for key, dependents in self._dependencies.items():
            clone._dependencies[key] = set(dependents)
        clone._version = self._version
        return clone

    # -- registration -------------------------------------------------------

    def set_formula(
        self,
        table: str | None,
        x: int,
        y: int,
        formula: str,
        cached_value: Any = None,
        value_type: str | None = None,
        *,
        fresh: bool = True,
        fingerprint: tuple[Any, ...] | None = None,
    ) -> FormulaCell:
        """Register or update a formula cell.

        ``fresh`` means the provided cached value has just been computed and
        matches the formula. Otherwise the cell is marked stale.
        """
        key = (table, x, y)
        previous = self._formulas.get(key)
        parse_error = False
        try:
            references = extract_references(formula)
        except FormulaError:
            references = []
            parse_error = True
        formula_changed = previous is not None and previous.formula != formula
        stale = formula_changed or not fresh or parse_error
        cell = FormulaCell(
            table=table,
            x=x,
            y=y,
            formula=formula,
            references=references,
            stale=stale,
            reason="formula changed" if formula_changed else None,
            cached_value=cached_value,
            value_type=value_type,
            fingerprint=fingerprint,
            parse_error=parse_error,
        )
        self._formulas[key] = cell
        self._rebuild_dependencies(key)
        self._version += 1
        if cell.stale:
            self.mark_stale(
                key,
                "formula changed"
                if formula_changed
                else "formula cannot be parsed"
                if parse_error
                else "value outdated",
            )
        return cell

    add_formula = set_formula

    def remove_formula(self, table: str | None, x: int, y: int) -> None:
        """Forget a formula cell.

        The reverse index (which formulas reference this cell) is kept: the
        cell still exists as a plain cell, and editing it must keep
        invalidating the formulas that reference it.
        """
        key = (table, x, y)
        if key not in self._formulas:
            return
        del self._formulas[key]
        self._version += 1

    def get_formula(self, table: str | None, x: int, y: int) -> FormulaCell | None:
        """Return the formula cell at the given coordinates."""
        return self._formulas.get((table, x, y))

    def formulas(self) -> list[FormulaCell]:
        """Return all tracked formula cells."""
        return list(self._formulas.values())

    def stale_formulas(self) -> list[FormulaCell]:
        """Return the formula cells whose cached value is stale."""
        return [cell for cell in self._formulas.values() if cell.stale]

    def is_stale(self, table: str | None, x: int, y: int) -> bool:
        """Return True if the formula cell at the coordinates is stale."""
        cell = self.get_formula(table, x, y)
        return bool(cell is not None and cell.stale)

    def _reference_table(self, reference: FormulaReference, owner: FormulaCell) -> str | None:
        if reference.sheet in (None, "."):
            return owner.table
        return reference.sheet

    def _rebuild_dependencies(self, key: tuple[str | None, int, int]) -> None:
        cell = self._formulas[key]
        for dependents in self._dependencies.values():
            dependents.discard(key)
        for reference in cell.references:
            table = self._reference_table(reference, cell)
            for x, y in reference.cells():
                self._dependencies.setdefault((table, x, y), set()).add(key)

    # -- dependency queries --------------------------------------------------

    def dependents(
        self,
        table: str | None,
        x: int,
        y: int,
        *,
        recursive: bool = False,
    ) -> list[FormulaCell]:
        """Return the formula cells depending on the given cell.

        With ``recursive=True``, indirect dependents are included too.
        """
        start = (table, x, y)
        found: list[FormulaCell] = []
        seen: set[tuple[str | None, int, int]] = set()
        queue = [start]
        while queue:
            current = queue.pop(0)
            for key in sorted(self._dependencies.get(current, ()), key=repr):
                if key in seen or key == start:
                    continue
                seen.add(key)
                cell = self._formulas.get(key)
                if cell is not None:
                    found.append(cell)
                if recursive:
                    queue.append(key)
        return found

    def precedents(self, table: str | None, x: int, y: int) -> list[FormulaCell]:
        """Return the formula cells referenced by the formula at the cell."""
        cell = self.get_formula(table, x, y)
        if cell is None:
            return []
        result = []
        for reference in cell.references:
            other = self.get_formula(
                self._reference_table(reference, cell),
                reference.x,
                reference.y,
            )
            if other is not None:
                result.append(other)
        return result

    # -- invalidation --------------------------------------------------------

    def mark_stale(
        self,
        key: tuple[str | None, int, int],
        reason: str = "dependency changed",
        *,
        _visited: set[tuple[str | None, int, int]] | None = None,
    ) -> list[FormulaCell]:
        """Mark a formula and all its transitive dependents as stale."""
        cell = self._formulas.get(key)
        if cell is None:
            return []
        if _visited is None:
            _visited = set()
        if key in _visited:
            return []
        _visited.add(key)
        changed: list[FormulaCell] = []
        if not cell.stale:
            cell.stale = True
            cell.reason = reason
            changed.append(cell)
            self._version += 1
        for dependent in self.dependents(cell.table, cell.x, cell.y):
            changed.extend(
                self.mark_stale(
                    dependent.key,
                    f"depends on {cell.table or ''}!{_number_to_column(cell.x)}{cell.y + 1}",
                    _visited=_visited,
                )
            )
        return changed

    def invalidate_cell(
        self,
        table: str | None,
        x: int,
        y: int,
        reason: str = "cell changed",
    ) -> list[FormulaCell]:
        """Mark every formula affected by a cell change as stale.

        The formula held by the changed cell (if any) is marked stale too,
        because its cached value may no longer match its formula. Use
        :meth:`set_formula` with ``fresh=True`` to refresh it.
        """
        affected: list[FormulaCell] = []
        seen: set[tuple[str | None, int, int]] = set()
        for changed in (
            *(
                self.mark_stale((table, x, y), reason)
                if (table, x, y) in self._formulas
                else []
            ),
            *self._mark_dependents(table, x, y, reason),
        ):
            if changed.key not in seen:
                seen.add(changed.key)
                affected.append(changed)
        return affected

    def _mark_dependents(
        self,
        table: str | None,
        x: int,
        y: int,
        reason: str,
    ) -> list[FormulaCell]:
        changed: list[FormulaCell] = []
        for dependent in self.dependents(table, x, y):
            changed.extend(self.mark_stale(dependent.key, reason))
        return changed

    def invalidate_all(self, reason: str = "structure changed") -> list[FormulaCell]:
        """Mark every tracked formula as stale."""
        changed: list[FormulaCell] = []
        for cell in self._formulas.values():
            if not cell.stale:
                cell.stale = True
                cell.reason = reason
                changed.append(cell)
                self._version += 1
        return changed

    def invalidate_region(
        self,
        table: str | None,
        x: int,
        y: int,
        z: int,
        t: int,
        reason: str = "region changed",
    ) -> list[FormulaCell]:
        """Mark formulas affected by a change covering the given region."""
        changed: list[FormulaCell] = []
        seen: set[tuple[str | None, int, int]] = set()
        for yy in range(y, t + 1):
            for xx in range(x, z + 1):
                for cell in self.invalidate_cell(table, xx, yy, reason):
                    if cell.key not in seen:
                        seen.add(cell.key)
                        changed.append(cell)
        return changed

    def invalidate_sheet(self, table: str | None, reason: str = "sheet changed") -> list[FormulaCell]:
        """Mark formulas depending on any cell of a sheet as stale."""
        return [
            cell
            for cell in self.invalidate_all(reason)
            if cell.table == table or any(
                self._reference_table(reference, cell) == table
                for reference in cell.references
            )
        ]

    # -- evaluation ----------------------------------------------------------

    def evaluate(
        self,
        table: str | None,
        x: int,
        y: int,
        value_getter: Callable[[str | None, int, int], Any] | None = None,
    ) -> EvaluationResult:
        """Evaluate the formula at a cell, using cached values only.

        ``value_getter`` is a callable ``(table, x, y) -> value`` used to read
        cells that are not tracked formula cells. By default those cells read
        as None.
        """
        cell = self.get_formula(table, x, y)
        if cell is None:
            return EvaluationResult(None, "unsupported", ())
        functions = tuple(formula_functions(cell.formula))
        try:
            node = parse_formula(cell.formula)
        except FormulaError:
            return EvaluationResult(None, "unsupported", functions)
        if node is None:
            return EvaluationResult(None, "unsupported", functions)
        visiting: set[tuple[str | None, int, int]] = {(table, x, y)}

        def resolver(reference: FormulaReference) -> Any:
            reference_table = self._reference_table(reference, cell)
            if reference.is_range:
                return [
                    resolve_cell(reference_table, rx, ry)
                    for rx, ry in reference.cells()
                ]
            return resolve_cell(reference_table, reference.x, reference.y)

        def resolve_cell(
            reference_table: str | None,
            rx: int,
            ry: int,
        ) -> Any:
            key = (reference_table, rx, ry)
            if key in visiting:
                raise FormulaEvaluationError("#CIRC!")
            other = self._formulas.get(key)
            if other is None:
                if value_getter is not None:
                    return value_getter(reference_table, rx, ry)
                return None
            if other.stale:
                raise FormulaEvaluationError("#STALE!")
            visiting.add(key)
            try:
                result = self.evaluate(reference_table, rx, ry, value_getter)
            finally:
                visiting.discard(key)
            if not result.ok:
                raise FormulaEvaluationError(result.error or "#VALUE!")
            return result.value

        result = FormulaEvaluator().evaluate(node, resolver)
        return EvaluationResult(result.value, result.error, functions)


# ---------------------------------------------------------------------------
# Document / table adapters
# ---------------------------------------------------------------------------

# Maximum number of XML trees whose cache is kept in memory. Entries keep the
# root element alive, so ids are never reused while an entry exists.
_REGISTRY_LIMIT = 128
_REGISTRY: OrderedDict[int, _CacheState] = OrderedDict()


class _CacheState:
    """Formula cache state attached to one XML tree (internal)."""

    __slots__ = ("cache", "initialised", "positions", "root")

    def __init__(self, root: Any) -> None:
        self.root = root
        self.cache = FormulaCache()
        self.initialised = False
        # lxml cell element -> (table_name, x, y, columns, rows)
        self.positions: dict[Any, tuple[str | None, int, int, int, int]] | None = None


def _root_of(element: Element) -> Any:
    return element._xml_element.getroottree().getroot()


def _state_for(element: Element) -> _CacheState:
    root = _root_of(element)
    key = id(root)
    state = _REGISTRY.get(key)
    if state is None or state.root is not root:
        state = _CacheState(root)
        _REGISTRY[key] = state
        while len(_REGISTRY) > _REGISTRY_LIMIT:
            _REGISTRY.popitem(last=False)
    else:
        _REGISTRY.move_to_end(key)
    return state


def _all_tables(element: Element) -> list[Table]:
    """Return the Table wrappers found in the element's XML tree."""
    from .element import Element
    from .table import Table

    wrapper = Element.from_tag(_root_of(element))
    return [
        Table(tag_or_elem=table._xml_element)
        for table in wrapper.get_elements("descendant::table:table")
        if getattr(table, "tag", "") == "table:table"
    ]


def _scan(element: Element) -> FormulaCache:
    """Build a fresh cache from the formula cells of the XML tree.

    Repetitions are not expanded: a repeated row/cell shares one formula, and
    expanding large repeated areas would be needlessly expensive.
    """
    cache = FormulaCache()
    for table in _all_tables(element):
        name = table.name
        y = 0
        for row in table._get_rows():
            row_repeated = row.repeated or 1
            row_y = y
            x = 0
            for cell in row._get_cells():
                cell_repeated = cell.repeated or 1
                formula = cell.formula
                if formula:
                    for yy in range(row_y, row_y + row_repeated):
                        for xx in range(x, x + cell_repeated):
                            try:
                                cache.set_formula(
                                    name,
                                    xx,
                                    yy,
                                    formula,
                                    cell.value,
                                    cell.type,
                                    fresh=True,
                                )
                            except FormulaError:
                                pass
                x += cell_repeated
            y += row_repeated
    return cache


def formula_cache_for(element: Element) -> FormulaCache:
    """Return the formula cache shared by the tables of a document.

    The cache is built lazily by scanning the ``table:formula`` attributes of
    every table in the XML tree. The cached values found in the document are
    trusted: they were computed by an office application. Afterwards, the
    write paths of :class:`Table`, :class:`Row` and :class:`Cell` invalidate
    the affected formulas precisely.
    """
    state = _state_for(element)
    if not state.initialised:
        state.cache = _scan(element)
        state.initialised = True
    return state.cache


def table_formula_cache(table: Table) -> FormulaCache:
    """Return the formula cache shared by the tables of a document."""
    return formula_cache_for(table)


# ---------------------------------------------------------------------------
# Cell positions
# ---------------------------------------------------------------------------


_XP_TABLE = xpath_compile("descendant::table:table")


def _raw_table_elements(element: Element) -> list[Any]:
    """Return the lxml ``table:table`` elements of the tree."""
    from .element import xpath_return_elements

    return xpath_return_elements(_XP_TABLE, _root_of(element))


def _raw_rows(table_element: Any) -> list[Any]:
    """Return the lxml row elements of a table, without wrappers."""
    from .element import xpath_return_elements
    from .table import _XP_ROW

    return xpath_return_elements(_XP_ROW, table_element)


_XP_CELLS = xpath_compile("(table:table-cell|table:covered-table-cell)")


def _raw_cells(row_element: Any) -> list[Any]:
    """Return the lxml cell elements of a row, without wrappers."""
    from .element import xpath_return_elements

    return xpath_return_elements(_XP_CELLS, row_element)


def build_positions(element: Element) -> dict[Any, tuple[str | None, int, int, int, int]]:
    """Map every cell XML element to its table position (internal).

    Built once per XML tree, walking raw lxml elements so repeated rows and
    cells are not expanded into Python objects.
    """
    positions: dict[Any, tuple[str | None, int, int, int, int]] = {}
    name_tag = _get_lxml_tag("table:name")
    repeat_rows = _get_lxml_tag("table:number-rows-repeated")
    repeat_cols = _get_lxml_tag("table:number-columns-repeated")
    for table_element in _raw_table_elements(element):
        name = table_element.get(name_tag)
        y = 0
        for row_element in _raw_rows(table_element):
            rows = int(row_element.get(repeat_rows) or 1)
            x = 0
            for cell_element in _raw_cells(row_element):
                columns = int(cell_element.get(repeat_cols) or 1)
                positions[cell_element] = (name, x, y, columns, rows)
                x += columns
            y += rows
    return positions


def cell_position(cell: Element) -> tuple[str | None, int, int, int, int] | None:
    """Return the position of a cell inside its table.

    The result is ``(table_name, x, y, columns, rows)`` where ``columns`` and
    ``rows`` are the number of cells/rows covered by the repeated attributes.
    Returns None if the cell is not attached to a table.

    Cells returned by :meth:`Table.get_cell` are clones, but they remember the
    name and coordinates of their source cell, so their formula state can
    still be queried.
    """
    from .table import Table

    origin = getattr(cell, "_formula_origin", None)
    if origin is not None:
        return origin.table_name, origin.x, origin.y, cell.repeated or 1, 1
    state = _state_for(cell)
    if state.positions is None:
        state.positions = build_positions(cell)
    position = state.positions.get(cell._xml_element)
    if position is not None:
        return position
    parent = cell.parent
    row_element = None
    while parent is not None:
        if getattr(parent, "tag", "") == "table:table-row":
            row_element = parent
            break
        parent = parent.parent
    if row_element is None:
        return None
    table_element = None
    current = row_element.parent
    while current is not None:
        if getattr(current, "tag", "") == "table:table":
            table_element = current
            break
        current = current.parent
    if table_element is None:
        return None
    table = Table(tag_or_elem=table_element._xml_element)
    y = 0
    rows = 1
    for row in table._get_rows():
        repeated = row.repeated or 1
        if row._xml_element is row_element._xml_element:
            rows = repeated
            break
        y += repeated
    else:
        return None
    x = 0
    columns = 1
    for candidate in row_element._get_cells():
        repeated = candidate.repeated or 1
        if candidate._xml_element is cell._xml_element:
            columns = repeated
            break
        x += repeated
    else:
        return None
    return table.name, x, y, columns, rows


class CellOrigin:
    """Coordinates of a cloned cell inside its source document (internal)."""

    __slots__ = ("root", "table_name", "x", "y")

    def __init__(
        self,
        table_name: str | None,
        x: int,
        y: int,
        root: Any,
    ) -> None:
        self.table_name = table_name
        self.x = x
        self.y = y
        self.root = root


def set_cell_origin(
    cell: Cell,
    table_name: str | None,
    x: int,
    y: int,
    table_element: Any = None,
) -> None:
    """Attach the source coordinates to a cloned cell (internal)."""
    root = None
    if table_element is not None:
        root = table_element.getroottree().getroot()
    cell._formula_origin = CellOrigin(table_name, x, y, root)


def anchor_element(cell: Cell) -> Element:
    """Return an element attached to the source document of a cell (internal).

    Cloned cells are detached from the XML tree, but they remember their
    source so their formula state can still be queried.
    """
    from .element import Element

    origin = getattr(cell, "_formula_origin", None)
    if origin is not None and origin.root is not None:
        return Element.from_tag(origin.root)
    return cell


# ---------------------------------------------------------------------------
# Invalidation, driven by the write paths
# ---------------------------------------------------------------------------


@contextmanager
def muted() -> Iterator[None]:
    """Suspend cache notifications (internal)."""
    global _MUTE
    _MUTE += 1
    try:
        yield
    finally:
        _MUTE -= 1


_MUTE = 0


def notifications_muted() -> bool:
    """Return True while inside a :func:`muted` block."""
    return _MUTE > 0


def _invalidate_region(
    cache: FormulaCache,
    table_name: str | None,
    x: int,
    y: int,
    z: int,
    t: int,
    reason: str,
) -> list[FormulaCell]:
    affected: list[FormulaCell] = []
    seen: set[tuple[str | None, int, int]] = set()
    for yy in range(y, t + 1):
        for xx in range(x, z + 1):
            for formula in cache.invalidate_cell(table_name, xx, yy, reason):
                if formula.key not in seen:
                    seen.add(formula.key)
                    affected.append(formula)
    return affected


def _stale_dependents(
    cache: FormulaCache,
    table_name: str | None,
    x: int,
    y: int,
    reason: str,
    affected: list[FormulaCell],
    seen: set[tuple[str | None, int, int]],
) -> None:
    for dependent in cache.dependents(table_name, x, y):
        for formula in cache.mark_stale(dependent.key, reason):
            if formula.key not in seen:
                seen.add(formula.key)
                affected.append(formula)


def refresh_table_caches(element: Element) -> None:
    """Refresh the row/cell maps of every table wrapper (internal).

    A table may be wrapped by several :class:`~odfdo.table.Table` instances
    (direct access, ``Body.get_table``, ...). When one wrapper replaces cells,
    the others keep a stale row cache. Recomputing the tables maps after a
    recalculation keeps them consistent.
    """
    for table in _all_tables(element):
        table._table_cache.clear_row_indexes()
        table._table_cache.clear_col_indexes()
        table._compute_table_cache()


def notify_cell_mutation(cell: Cell) -> list[FormulaCell]:
    """Notify the cache that a live cell was modified in place (internal).

    Called by the :class:`~odfdo.cell.Cell` value setters. The cell itself
    and its dependents are invalidated; the formula cache is refreshed from
    the XML tree first so formulas created since the last scan are known.
    """
    if _MUTE:
        return []
    position = cell_position(cell)
    if position is None:
        return []
    anchor = anchor_element(cell)
    cache = formula_cache_for(anchor)
    table_name, x, y, columns, rows = position
    key = (table_name, x, y)
    if cell.formula:
        cache.set_formula(
            table_name,
            x,
            y,
            cell.formula,
            cell.value,
            cell.type,
            fresh=False,
        )
    elif key in cache:
        # The formula was removed: formulas depending on this cell can no
        # longer reuse their cached value either.
        cache.remove_formula(*key)
        for yy in range(y, y + rows):
            for xx in range(x, x + columns):
                cache.invalidate_cell(table_name, xx, yy, "formula removed")
        _state_for(anchor).positions = None
        return []
    return record_cell_change(cell, position=position)


def record_cell_change(
    cell: Cell,
    reason: str = "cell changed",
    *,
    fresh_formula: bool = False,
    position: tuple[str | None, int, int, int, int] | None = None,
) -> list[FormulaCell]:
    """Record a cell change and invalidate the affected formulas (internal).

    Called by :class:`~odfdo.cell.Cell` value setters and by the table and row
    write methods. Direct and indirect dependents of the changed cell are
    marked stale, so a formula whose text did not change never reuses its old
    cached value after a precedent changed.

    When the changed cell holds a formula:

    - with ``fresh_formula=False`` (the default), the formula keeps its text
      but its cached value is marked stale,
    - with ``fresh_formula=True``, the provided value is recorded as the
      freshly computed cache of that formula (used by
      ``Table.set_value(value, formula=...)``).

    Args:
        cell: The changed cell.
        reason: The reason recorded in the stale markers.
        fresh_formula: True if the cached value is known to be current.
        position: Optional known position. The table write paths provide it,
            avoiding a scan of the table.

    Returns:
        The formula cells that became stale.

    """
    if _MUTE:
        return []
    if position is None:
        position = cell_position(cell)
    if position is None:
        return []
    anchor = anchor_element(cell)
    cache = formula_cache_for(anchor)
    table_name, x, y, columns, rows = position
    key = (table_name, x, y)
    affected: list[FormulaCell] = []
    seen: set[tuple[str | None, int, int]] = set()
    if cell.formula:
        entry = cache.get_formula(*key)
        if fresh_formula:
            cell_entry = cache.set_formula(
                table_name,
                x,
                y,
                cell.formula,
                cell.value,
                cell.type,
                fresh=True,
            )
            _ = cell_entry
        elif entry is None or entry.formula != cell.formula:
            cache.set_formula(
                table_name,
                x,
                y,
                cell.formula,
                cell.value,
                cell.type,
                fresh=False,
            )
            for formula in cache.mark_stale(key, reason):
                seen.add(formula.key)
                affected.append(formula)
        elif entry is not None and not entry.stale:
            entry.stale = True
            entry.reason = reason
            seen.add(key)
            affected.append(entry)
        _stale_dependents(cache, table_name, x, y, reason, affected, seen)
    else:
        if key in cache:
            cache.remove_formula(*key)
        _stale_dependents(cache, table_name, x, y, reason, affected, seen)
        for dependent in cache.dependents(table_name, x, y):
            for formula in cache.mark_stale(dependent.key, reason):
                if formula.key not in seen:
                    seen.add(formula.key)
                    affected.append(formula)
    # Repeated cells share the same XML element: the same change applies to
    # every covered position.
    for yy in range(y, y + rows):
        for xx in range(x, x + columns):
            if (table_name, xx, yy) == key:
                continue
            formula = cache.get_formula(table_name, xx, yy)
            if formula is not None:
                for changed in cache.mark_stale(
                    (table_name, xx, yy), reason
                ):
                    if changed.key not in seen:
                        seen.add(changed.key)
                        affected.append(changed)
            _stale_dependents(
                cache, table_name, xx, yy, reason, affected, seen
            )
    # Replacing a cell may split a repeated cell/row, invalidating the raw
    # position map.
    _state_for(anchor).positions = None
    return affected


def record_region(
    table: Table,
    x: int,
    y: int,
    z: int,
    t: int,
    reason: str = "cell changed",
) -> list[FormulaCell]:
    """Invalidate formulas affected by a rectangle of cells (internal)."""
    if _MUTE:
        return []
    cache = formula_cache_for(table)
    return _invalidate_region(cache, table.name, x, y, z, t, reason)


def record_structure_change(
    table: Table,
    reason: str = "structure changed",
) -> list[FormulaCell]:
    """Invalidate every formula after a row/column layout change (internal).

    Inserting, deleting or resizing rows and columns shifts coordinates, and
    changes what whole-row/whole-column references cover. Every cached value
    is therefore considered stale until recomputed.
    """
    if _MUTE:
        return []
    cache = formula_cache_for(table)
    _state_for(table).positions = None
    return cache.invalidate_all(reason)


# ---------------------------------------------------------------------------
# Public helpers on cells / tables
# ---------------------------------------------------------------------------


def cell_formula_entry(cell: Cell) -> FormulaCell | None:
    """Return the cache entry of a formula cell, or None."""
    if not cell.formula:
        return None
    position = cell_position(cell)
    if position is None:
        return None
    table_name, x, y, _columns, _rows = position
    return formula_cache_for(anchor_element(cell)).get_formula(table_name, x, y)


def formula_is_stale(cell: Cell) -> bool:
    """Return True if the formula of the cell has a stale cached value."""
    entry = cell_formula_entry(cell)
    return bool(entry is not None and entry.stale)


def formula_stale_reason(cell: Cell) -> str | None:
    """Return the reason why the formula cache of the cell is stale."""
    entry = cell_formula_entry(cell)
    if entry is None:
        return None
    return entry.reason


def formula_dependents(
    cell: Cell,
    *,
    recursive: bool = False,
) -> list[FormulaCell]:
    """Return the formulas depending on the given cell."""
    position = cell_position(cell)
    if position is None:
        return []
    table_name, x, y, _columns, _rows = position
    return formula_cache_for(anchor_element(cell)).dependents(
        table_name, x, y, recursive=recursive
    )


def mark_cell_formula_stale(
    cell: Cell,
    reason: str = "cell changed",
) -> list[FormulaCell]:
    """Explicitly mark the formulas affected by a cell change as stale.

    Useful when the caller knows a value changed even if the XML
    representation did not (for instance a volatile function result).
    """
    if _MUTE:
        return []
    position = cell_position(cell)
    if position is None:
        return []
    table_name, x, y, columns, rows = position
    cache = formula_cache_for(anchor_element(cell))
    return _invalidate_region(
        cache,
        table_name,
        x,
        y,
        x + columns - 1,
        y + rows - 1,
        reason,
    )


def recalculate(
    element: Element,
    value_getter: Callable[[str | None, int, int], Any] | None = None,
) -> list[FormulaCell]:
    """Recompute supported formulas and refresh their cached values.

    Formulas are processed until no progress is made, so chains of supported
    formulas are refreshed in dependency order. Formulas using an unknown
    function or an unsupported construct are left untouched and stay stale:
    their value is never guessed.

    Args:
        element: Any element of the document (usually the body).
        value_getter: Optional callable ``(table, x, y) -> value`` used to read
            cells that are not formula cells. The default reads them from the
            XML tree.

    Returns:
        The formula cells that were refreshed.

    """
    cache = formula_cache_for(element)
    tables = {table.name: table for table in _all_tables(element)}
    refreshed: list[FormulaCell] = []

    def default_getter(name: str | None, x: int, y: int) -> Any:
        table = tables.get(name)
        if table is None:
            return None
        return table.get_value((x, y))

    getter = value_getter or default_getter
    progress = True
    while progress:
        progress = False
        for formula in list(cache.formulas()):
            if not formula.stale:
                continue
            result = cache.evaluate(formula.table, formula.x, formula.y, getter)
            if result.error is not None:
                continue
            table = tables.get(formula.table)
            if table is None:
                continue
            cell = table.get_cell((formula.x, formula.y), clone=False)
            with muted():
                cell.set_value(_to_cell_value(result.value))
                if formula.formula and cell.formula != formula.formula:
                    cell.formula = formula.formula
            cache.set_formula(
                formula.table,
                formula.x,
                formula.y,
                cell.formula or formula.formula,
                cell.value,
                cell.type,
                fresh=True,
            )
            refreshed.append(formula)
            progress = True
    if refreshed:
        refresh_table_caches(element)
    return refreshed


def _to_cell_value(value: Any) -> Any:
    """Convert an evaluated Decimal to a cell friendly value."""
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


__all__ = [
    "SUPPORTED_FUNCTIONS",
    "CellOrigin",
    "EvaluationResult",
    "FormulaCache",
    "FormulaCell",
    "FormulaError",
    "FormulaEvaluationError",
    "FormulaEvaluator",
    "FormulaReference",
    "FormulaUnsupportedError",
    "UnknownFunctionError",
    "anchor_element",
    "cell_formula_entry",
    "cell_position",
    "extract_references",
    "formula_cache_for",
    "formula_dependents",
    "formula_functions",
    "formula_is_stale",
    "formula_stale_reason",
    "mark_cell_formula_stale",
    "muted",
    "notifications_muted",
    "notify_cell_mutation",
    "recalculate",
    "record_cell_change",
    "record_region",
    "record_structure_change",
    "refresh_table_caches",
    "set_cell_origin",
    "table_formula_cache",
]
