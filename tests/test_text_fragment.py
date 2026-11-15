"""Tests for plain text and structured fragment extraction."""

from collections.abc import Iterable
from dataclasses import FrozenInstanceError

import pytest

from odfdo import (
    Cell,
    ChangeInfo,
    ChangeVisibility,
    Document,
    Header,
    LineBreak,
    Link,
    Paragraph,
    Span,
    Tab,
    TextChangedRegion,
    TextChangeEnd,
    TextChangeStart,
    TextFormatChange,
    TextFragment,
    get_plain_text,
    get_text_fragments,
)
from odfdo.element import EText


@pytest.fixture
def tracked_document(samples) -> Iterable[Document]:
    yield Document(samples("tracked_changes.odt"))


def test_plain_text_simple_paragraph():
    paragraph = Paragraph("Some plain text")
    assert get_plain_text(paragraph) == "Some plain text"
    assert paragraph.get_plain_text() == "Some plain text"


def test_plain_text_keeps_consecutive_spaces():
    paragraph = Paragraph("a   b")
    assert get_plain_text(paragraph) == "a   b"
    kinds = [
        (fragment.kind, fragment.text) for fragment in get_text_fragments(paragraph)
    ]
    assert ("spacer", "  ") in kinds


def test_plain_text_expands_tab_and_line_break():
    paragraph = Paragraph()
    paragraph.append_plain_text("line one")
    paragraph.append(LineBreak())
    paragraph.append(Tab())
    paragraph.append_plain_text("line two")
    assert get_plain_text(paragraph) == "line one\n\tline two"
    kinds = [fragment.kind for fragment in get_text_fragments(paragraph)]
    assert "line-break" in kinds
    assert "tab" in kinds


def test_fragments_offsets_match_plain_text():
    paragraph = Paragraph()
    paragraph.append_plain_text("See ")
    paragraph.append(Link("https://example.org", text="the doc"))
    paragraph.append(LineBreak())
    paragraph.append_plain_text("end")
    plain = get_plain_text(paragraph)
    fragments = get_text_fragments(paragraph)
    assert "".join(fragment.text for fragment in fragments) == plain
    for fragment in fragments:
        assert plain[fragment.start : fragment.end] == fragment.text
        assert fragment.end >= fragment.start


def test_link_separates_display_text_and_target():
    link = Link("https://example.org/path", text="displayed label")
    paragraph = Paragraph()
    paragraph.append(link)
    fragments = get_text_fragments(paragraph)
    link_fragment = next(fragment for fragment in fragments if fragment.kind == "link")
    assert link_fragment.text == "displayed label"
    assert link_fragment.url == "https://example.org/path"
    assert link_fragment.source._xml_element is link._xml_element
    assert link_fragment.source_tag == "text:a"


def test_text_fragment_traces_back_to_source_node():
    paragraph = Paragraph()
    span = Span("inner", style="style1")
    paragraph.append_plain_text("outer ")
    paragraph.append(span)
    fragment = next(
        fragment
        for fragment in get_text_fragments(paragraph)
        if fragment.source_tag == "text:span"
    )
    assert isinstance(fragment.source, EText)
    assert fragment.source.parent._xml_element is span._xml_element


def test_multi_paragraph_container_joins_with_newlines():
    cell = Cell()
    cell.append(Paragraph("one"))
    cell.append(Paragraph("two"))
    assert get_plain_text(cell) == "one\ntwo"
    kinds = [fragment.kind for fragment in get_text_fragments(cell)]
    assert "paragraph-break" in kinds


def test_header_extraction():
    header = Header(2, "Chapter title")
    assert get_plain_text(header) == "Chapter title"
    assert get_text_fragments(header)[0].text == "Chapter title"


def test_body_extraction_matches_its_blocks(tracked_document):
    body = tracked_document.body
    plain = get_plain_text(body)
    blocks = body.get_elements(
        ".//text:p[not(ancestor::text:tracked-changes)] "
        "| .//text:h[not(ancestor::text:tracked-changes)]"
    )
    assert plain == "\n".join(get_plain_text(block) for block in blocks)
    for fragment in get_text_fragments(body):
        assert plain[fragment.start : fragment.end] == fragment.text


def test_body_extraction_ignores_tracked_changes_store(tracked_document):
    body = tracked_document.body
    plain = get_plain_text(body)
    # "les" and "amis," only exist inside the text:tracked-changes store.
    assert "les" not in plain
    assert "amis," not in plain
    rejected = get_plain_text(body, ChangeVisibility.REJECTED)
    assert rejected == "Bonjour les amis, ça va ?"


def test_accepted_change_is_current_value(tracked_document):
    paragraph = tracked_document.body.get_paragraphs()[-1]
    plain = get_plain_text(paragraph)
    assert plain == "Bonjour  los bonitos amigos  ça va ?"
    assert "les" not in plain
    assert "amis," not in plain
    deletion_fragments = [
        fragment
        for fragment in get_text_fragments(paragraph)
        if fragment.change == "deletion"
    ]
    assert deletion_fragments == []


def test_rejected_change_restores_deleted_text(tracked_document):
    paragraph = tracked_document.body.get_paragraphs()[-1]
    plain = get_plain_text(paragraph, ChangeVisibility.REJECTED)
    assert plain == "Bonjour les amis, ça va ?"
    assert "los bonitos amigos" not in plain
    fragments = get_text_fragments(paragraph, "rejected")
    deleted = [fragment for fragment in fragments if fragment.change == "deletion"]
    assert [fragment.text for fragment in deleted] == ["les", "amis,"]
    assert all(fragment.kind == "deletion" for fragment in deleted)


def test_all_visibility_reports_every_change(tracked_document):
    paragraph = tracked_document.body.get_paragraphs()[-1]
    fragments = get_text_fragments(paragraph, "all")
    changes = {
        (fragment.change, fragment.text) for fragment in fragments if fragment.change
    }
    assert ("deletion", "les") in changes
    assert ("deletion", "amis,") in changes
    assert any(fragment.change == "insertion" for fragment in fragments)
    plain = get_plain_text(paragraph, ChangeVisibility.ALL)
    assert "".join(fragment.text for fragment in fragments) == plain


def test_deleted_text_never_current_value(tracked_document):
    paragraph = tracked_document.body.get_paragraphs()[-1]
    accepted = get_plain_text(paragraph)
    rejected = get_plain_text(paragraph, ChangeVisibility.REJECTED)
    assert accepted != rejected
    assert "les" in rejected
    assert "les" not in accepted


def test_insertion_fragments_tagged_with_change_id(tracked_document):
    paragraph = tracked_document.body.get_paragraphs()[-1]
    insertion = next(
        fragment
        for fragment in get_text_fragments(paragraph)
        if fragment.change == "insertion"
    )
    assert insertion.change_id == "ct140266193096576"
    assert insertion.text == "los bonitos amigos "


def test_format_change_keeps_text_in_every_mode(tracked_document):
    tracked_changes = tracked_document.body.tracked_changes
    region = TextChangedRegion()
    region.set_id("ctFMT")
    format_change = TextFormatChange()
    format_change.set_change_info(ChangeInfo("tester"))
    region.append(format_change)
    tracked_changes.append(region)

    paragraph = Paragraph()
    paragraph.append_plain_text("a ")
    start = TextChangeStart()
    start.set_id("ctFMT")
    end = TextChangeEnd()
    end.set_id("ctFMT")
    paragraph.append(start)
    paragraph.append_plain_text("styled")
    paragraph.append(end)
    paragraph.append_plain_text(" b")
    tracked_document.body.append(paragraph)

    for visibility in ChangeVisibility:
        assert get_plain_text(paragraph, visibility) == "a styled b"
    tagged = [f for f in get_text_fragments(paragraph) if f.change]
    assert all(f.change == "format-change" for f in tagged)
    assert "".join(f.text for f in tagged) == "styled"


def test_link_inside_insertion_hidden_when_rejected():
    paragraph = Paragraph("before ")
    start = TextChangeStart()
    start.set_id("ct1")
    end = TextChangeEnd()
    end.set_id("ct1")
    paragraph.append(start)
    paragraph.append(Link("https://x.test", text="inserted link"))
    paragraph.append(end)
    paragraph.append_plain_text(" after")
    assert get_plain_text(paragraph) == "before inserted link after"
    assert get_plain_text(paragraph, "rejected") == "before  after"


def test_fragment_to_dict_does_not_leak_live_node():
    paragraph = Paragraph()
    paragraph.append(Link("https://example.org", text="label"))
    fragment = get_text_fragments(paragraph)[0]
    data = fragment.to_dict()
    assert data == {
        "kind": "link",
        "text": "label",
        "start": 0,
        "end": 5,
        "url": "https://example.org",
        "change_id": None,
        "change": None,
        "source_tag": "text:a",
    }


def test_fragment_is_frozen():
    fragment = TextFragment(kind="text", text="abc")
    with pytest.raises(FrozenInstanceError):
        fragment.text = "other"


def test_invalid_visibility_raises():
    paragraph = Paragraph("abc")
    with pytest.raises(ValueError, match="nope"):
        get_plain_text(paragraph, "nope")
