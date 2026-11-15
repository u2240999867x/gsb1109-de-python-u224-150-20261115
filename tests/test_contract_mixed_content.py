"""Tests for contract mixed-content sequence and amount column handling."""

from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from contract_mixed_content import (
    Amount,
    ElementToken,
    MixedContent,
    TextToken,
    parse_contract_paragraph,
)

CONTRACT_XML = (
    "<p>甲方应于 <b>2026-12-01</b> 前支付合同款 USD 1,250.00"
    '（详见 <a href="https://example.com/contract#pay">付款条款 USD 99.00</a>），'
    "逾期按 €500.00 计违约金。</p>"
)


def _walk_text(token):
    if isinstance(token, TextToken):
        return [token.text]
    texts = []
    for child in token.children:
        texts.extend(_walk_text(child))
    return texts


def test_sequence_reconstructs_mixed_content_in_order():
    result = parse_contract_paragraph(CONTRACT_XML)

    kinds = [
        token.tag if isinstance(token, ElementToken) else token.kind
        for token in result.sequence
    ]
    assert kinds == ["text", "b", "text", "a", "text"]

    bold = result.sequence[1]
    assert isinstance(bold, ElementToken)
    assert bold.tag == "b"
    assert bold.children == (TextToken("2026-12-01"),)

    link = result.sequence[3]
    assert isinstance(link, ElementToken)
    assert link.attrib == {"href": "https://example.com/contract#pay"}
    assert link.children == (TextToken("付款条款 USD 99.00"),)

    assert result.render() == CONTRACT_XML[3:-4]


def test_empty_text_node_is_explicitly_preserved():
    root = ET.Element("p")
    root.text = ""  # explicit empty text node, distinct from None
    child = ET.SubElement(root, "b")
    child.text = None
    child.tail = ""

    result = parse_contract_paragraph(root)

    assert result.sequence[0] == TextToken("")
    assert isinstance(result.sequence[1], ElementToken)
    assert result.sequence[1].children == ()  # None text creates no token
    assert result.sequence[2] == TextToken("")  # empty tail preserved
    assert [token.text for token in result.sequence if isinstance(token, TextToken)] == [
        "",
        "",
    ]


def test_adjacent_text_nodes_are_not_merged():
    result = parse_contract_paragraph("<p>头文本<b>粗体</b>尾文本<i>斜体</i></p>")

    assert len(result.sequence) == 4
    assert isinstance(result.sequence[0], TextToken)
    assert result.sequence[0].text == "头文本"
    assert isinstance(result.sequence[2], TextToken)
    assert result.sequence[2].text == "尾文本"
    assert result.render() == "头文本<b>粗体</b>尾文本<i>斜体</i>"


def test_amount_column_is_structured_and_ordered():
    result = parse_contract_paragraph(CONTRACT_XML)
    column = result.amount_column()

    assert [row["amount_id"] for row in column] == [0, 1, 2]
    assert [row["index"] for row in column] == [2, 3, 4]

    first = result.amounts[0]
    assert first.raw == "USD 1,250.00"
    assert first.currency == "USD"
    assert first.value == Decimal("1250.00")
    assert first.path == ()
    assert result.sequence[2].text[first.start : first.end] == first.raw

    nested = result.amounts[1]
    assert nested.raw == "USD 99.00"
    assert nested.index == 3
    assert nested.path == (0,)
    assert nested.value == Decimal("99.00")

    euro = result.amounts[2]
    assert euro.currency == "€"
    assert euro.value == Decimal("500.00")


def test_european_decimal_and_thousands_separators():
    result = parse_contract_paragraph("<p>金额 €1.234,56 与 RMB 100</p>")

    values = [(item.currency, item.value) for item in result.amounts]
    assert values == [("€", Decimal("1234.56")), ("RMB", Decimal("100"))]


def test_replace_amount_keeps_every_other_token_identical_and_ordered():
    result = parse_contract_paragraph(CONTRACT_XML)
    before_tokens = list(result.sequence)
    before_nested_ids = {
        id(child)
        for token in before_tokens
        if isinstance(token, ElementToken)
        for child in token.children
    }

    updated = result.replace_amount(0, Decimal("2000.00"))

    assert len(updated.sequence) == len(before_tokens)
    for new, old in zip(updated.sequence, before_tokens):
        if old is not before_tokens[2]:
            assert new is old  # untouched tokens keep object identity
    updated_nested_ids = {
        id(child)
        for token in updated.sequence
        if isinstance(token, ElementToken)
        for child in token.children
    }
    assert before_nested_ids <= updated_nested_ids

    assert updated.amounts[0].raw == "USD 2000.00"
    assert updated.amounts[0].value == Decimal("2000.00")
    # Other amounts are untouched and still addressable.
    assert updated.amounts[1].raw == "USD 99.00"
    assert updated.amounts[2].raw == "€500.00"

    expected = result.render().replace("USD 1,250.00", "USD 2000.00", 1)
    assert updated.render() == expected
    # No re-splicing of surrounding wording.
    assert updated.sequence[0] is result.sequence[0]
    assert updated.sequence[4] is result.sequence[4]


def test_replace_amount_inside_link_only_rewrites_that_span():
    result = parse_contract_paragraph(CONTRACT_XML)
    updated = result.replace_amount(1, "88.00")

    link = updated.sequence[3]
    assert isinstance(link, ElementToken)
    assert link.attrib == {"href": "https://example.com/contract#pay"}
    assert link.children == (TextToken("付款条款 USD 88.00"),)
    assert updated.amounts[1].raw == "USD 88.00"
    # Sibling tokens untouched.
    assert updated.sequence[1] is result.sequence[1]
    assert updated.sequence[4] is result.sequence[4]


def test_repeated_replacement_with_two_amounts_in_one_text_node():
    result = parse_contract_paragraph("<p>首笔 USD 10.00，次笔 USD 20.00 结清</p>")
    once = result.replace_amount(0, "1.00")
    assert once.render() == "首笔 USD 1.00，次笔 USD 20.00 结清"

    twice = once.replace_amount(1, "2.00")
    assert twice.render() == "首笔 USD 1.00，次笔 USD 2.00 结清"
    assert [item.value for item in twice.amounts] == [
        Decimal("1.00"),
        Decimal("2.00"),
    ]


def test_fragment_without_wrapper_and_amount_free_paragraph():
    result = parse_contract_paragraph(" plain <i>note</i> tail ")
    assert result.render() == " plain <i>note</i> tail "
    assert result.amounts == ()

    no_amounts = parse_contract_paragraph("<p>无金额段落。</p>")
    assert no_amounts.amount_column() == []
    assert no_amounts.render() == "无金额段落。"


def test_unknown_amount_id_raises():
    result = parse_contract_paragraph("<p>USD 1.00</p>")
    with pytest.raises(KeyError):
        result.replace_amount(99, "2.00")
