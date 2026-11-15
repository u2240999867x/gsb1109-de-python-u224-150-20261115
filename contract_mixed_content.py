"""Mixed-content extraction for contract paragraphs.

A contract paragraph can interleave plain text, inline formatting tags
(``<b>``, ``<i>`` ...), links (``<a href="...">``) and monetary amounts.
This module keeps the *sequence* of nodes fully reconstructible -- empty
text nodes and adjacent text nodes included, strictly in document order --
while also exposing the amounts as a structured column.

Amounts can be changed in place by amount id: only the single text span of
that amount is rewritten. Every other token keeps its object identity,
position and wording, so nothing is re-joined, re-serialized or re-ordered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from xml.etree import ElementTree as ET

__all__ = [
    "TextToken",
    "ElementToken",
    "Amount",
    "MixedContent",
    "parse_contract_paragraph",
]

# e.g. "USD 1,250.00", "€1.234,56", "RMB 100", "CNY 1,200.50 元"
_AMOUNT_RE = re.compile(
    r"(?P<currency>[A-Z]{3}|[$€£¥￥])\s*"
    r"(?P<number>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)",
    re.UNICODE,
)


@dataclass(frozen=True)
class TextToken:
    """A text node.

    ``text`` is the exact string, including the empty string when the text
    node exists but is empty, so empty and adjacent text nodes are
    distinguishable and verifiable.
    """

    text: str
    kind: str = "text"


@dataclass(frozen=True)
class ElementToken:
    """A child element with its own ordered inner token sequence."""

    tag: str
    attrib: dict[str, str]
    children: tuple["TextToken | ElementToken", ...]
    kind: str = "element"


@dataclass(frozen=True)
class Amount:
    """One structured amount row in the amount column."""

    amount_id: int        # unique id, assigned in document order
    index: int            # position of the owning top-level token
    path: tuple[int, ...]  # child-index path inside an element; () = top text
    start: int            # span start inside the owning TextToken
    end: int              # span end inside the owning TextToken
    raw: str              # verbatim source substring, e.g. "USD 1,250.00"
    currency: str
    value: Decimal        # numeric value, original scale preserved
    formatted: str        # canonical re-render of this single amount
    kind: str = "amount"


@dataclass(frozen=True)
class MixedContent:
    """The reconstructible sequence plus the structured amount column."""

    sequence: tuple["TextToken | ElementToken", ...]
    amounts: tuple[Amount, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ views
    def render(self) -> str:
        """Rebuild the text/element layout, preserving order."""

        return "".join(
            token.text if isinstance(token, TextToken) else _render_element(token)
            for token in self.sequence
        )

    def amount_column(self) -> list[dict[str, object]]:
        """Structured amount rows in document order."""

        return [
            {
                "amount_id": item.amount_id,
                "index": item.index,
                "currency": item.currency,
                "value": str(item.value),
                "raw": item.raw,
                "formatted": item.formatted,
            }
            for item in self.amounts
        ]

    # --------------------------------------------------------------- mutation
    def replace_amount(self, amount_id: int, new_value: Decimal | str) -> "MixedContent":
        """Return a copy where one amount is rewritten.

        Only the text span of the addressed amount changes. All other
        tokens are reused by identity, keeping wording and order intact.
        """

        target = self._amount(amount_id)
        replacement = _format_amount(target.currency, Decimal(str(new_value)))

        new_sequence = list(self.sequence)
        if not target.path:
            old_token = self.sequence[target.index]
            assert isinstance(old_token, TextToken)
            new_sequence[target.index] = _splice(old_token, target, replacement)
        else:
            old_token = self.sequence[target.index]
            assert isinstance(old_token, ElementToken)
            new_sequence[target.index] = _rebuild_element(
                old_token, target.path, target, replacement
            )

        new_amount = replace(
            target,
            value=Decimal(str(new_value)),
            raw=replacement,
            formatted=replacement,
            end=target.start + len(replacement),
        )
        delta = len(replacement) - (target.end - target.start)
        new_amounts = tuple(
            new_amount
            if item.amount_id == amount_id
            else _shift_span(item, target, delta)
            for item in self.amounts
        )
        return MixedContent(tuple(new_sequence), new_amounts)

    def _amount(self, amount_id: int) -> Amount:
        for item in self.amounts:
            if item.amount_id == amount_id:
                return item
        raise KeyError(f"no amount with amount_id={amount_id}")


def _shift_span(item: Amount, target: Amount, delta: int) -> Amount:
    """Adjust spans of later amounts sharing the same text node."""

    same_node = item.index == target.index and item.path == target.path
    if not same_node or item.start < target.end:
        return item
    return replace(item, start=item.start + delta, end=item.end + delta)


# --------------------------------------------------------------------- parsing
def parse_contract_paragraph(
    source: str | bytes | ET.Element,
) -> MixedContent:
    """Parse one contract paragraph into an ordered token sequence."""

    if isinstance(source, ET.Element):
        root = source
    elif _looks_like_xml(source):
        root = ET.fromstring(source)
    else:
        root = ET.fromstring(f"<p>{source}</p>")

    sequence: list[TextToken | ElementToken] = []
    amounts: list[Amount] = []
    counter = 0

    if root.text is not None:
        counter = _append_text(sequence, amounts, root.text, (), counter)

    for child in root:
        position = len(sequence)
        element_token = _build_element_token(child)
        sequence.append(element_token)
        counter = _scan_element(element_token, position, (), amounts, counter)

        if child.tail is not None:
            counter = _append_text(sequence, amounts, child.tail, (), counter)

    return MixedContent(tuple(sequence), tuple(amounts))


def _looks_like_xml(source: str | bytes) -> bool:
    head = source.lstrip()
    if isinstance(head, bytes):
        return head[:1] == b"<"
    return head[:1] == "<"


def _append_text(sequence, amounts, text, path, counter):
    position = len(sequence)
    sequence.append(TextToken(text))
    for match in _AMOUNT_RE.finditer(text):
        amounts.append(_make_amount(counter, position, path, match))
        counter += 1
    return counter


def _build_element_token(element: ET.Element) -> ElementToken:
    children: list[TextToken | ElementToken] = []
    if element.text is not None:  # explicit "" is an empty text node
        children.append(TextToken(element.text))
    for nested in element:
        children.append(_build_element_token(nested))
        if nested.tail is not None:
            children.append(TextToken(nested.tail))
    return ElementToken(
        tag=element.tag,
        attrib=dict(element.attrib),
        children=tuple(children),
    )


def _scan_element(token: ElementToken, position, prefix, amounts, counter):
    for child_index, child in enumerate(token.children):
        path = prefix + (child_index,)
        if isinstance(child, TextToken):
            for match in _AMOUNT_RE.finditer(child.text):
                amounts.append(_make_amount(counter, position, path, match))
                counter += 1
        else:
            counter = _scan_element(child, position, path, amounts, counter)
    return counter


def _make_amount(amount_id, position, path, match: re.Match[str]) -> Amount:
    currency = match.group("currency")
    value = _parse_number(match.group("number"))
    return Amount(
        amount_id=amount_id,
        index=position,
        path=path,
        start=match.start(),
        end=match.end(),
        raw=match.group(0),
        currency=currency,
        value=value,
        formatted=_format_amount(currency, value),
    )


def _parse_number(number: str) -> Decimal:
    # European style: the last separator is the decimal one.
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        whole, _, fraction = number.partition(",")
        number = f"{whole}.{fraction}" if fraction else whole
    return Decimal(number)


def _format_amount(currency: str, value: Decimal) -> str:
    return f"{currency} {value}"


# -------------------------------------------------------------------- rendering
def _render_element(token: ElementToken) -> str:
    attrs = "".join(
        f' {name}="{value}"' for name, value in token.attrib.items()
    )
    inner = "".join(
        child.text if isinstance(child, TextToken) else _render_element(child)
        for child in token.children
    )
    return f"<{token.tag}{attrs}>{inner}</{token.tag}>"


# ------------------------------------------------------------------ replacement
def _splice(token: TextToken, target: Amount, replacement: str) -> TextToken:
    return TextToken(
        token.text[: target.start] + replacement + token.text[target.end :]
    )


def _rebuild_element(
    token: ElementToken,
    path: tuple[int, ...],
    target: Amount,
    replacement: str,
) -> ElementToken:
    head, *rest = path
    new_children = list(token.children)
    child = new_children[head]
    if not rest:
        assert isinstance(child, TextToken)
        new_children[head] = _splice(child, target, replacement)
    else:
        assert isinstance(child, ElementToken)
        new_children[head] = _rebuild_element(
            child, tuple(rest), target, replacement
        )
    return ElementToken(token.tag, dict(token.attrib), tuple(new_children))
