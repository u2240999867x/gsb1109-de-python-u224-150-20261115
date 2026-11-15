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
"""Plain text and structured text fragment extraction.

Two extraction channels are provided for any element holding text content
(paragraph, header, cell, frame, document body, ...):

* :func:`get_plain_text` returns a single plain text string, expanding
  ``text:s`` spacers, ``text:tab`` tabs and ``text:line-break`` line breaks
  and joining the paragraphs of multi-paragraph content with new lines.
* :func:`get_text_fragments` returns the same content as an ordered list of
  :class:`TextFragment` objects, keeping links (display text separated from
  the target URL), formatting marks and tracked change information. Every
  fragment keeps a reference to the ODF node it originates from, so the
  source element or text node can be reached again.

Tracked changes are handled according to a :class:`ChangeVisibility` rule:
the deleted text is never reported as a current value, it only shows up when
the rejected (original) state, or every change, is requested explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from .element import Element, EText

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .link import Link
    from .tracked_changes import TextChange, TextChangedRegion, TextDeletion

__all__ = [
    "ChangeVisibility",
    "TextFragment",
    "get_plain_text",
    "get_text_fragments",
]

_BLOCK_TAGS = ("text:p", "text:h")
_DELETION_ROLE = "deletion"
_INSERTION_ROLE = "insertion"


class ChangeVisibility(str, Enum):
    """Visibility rule applying to the text enclosed in tracked changes.

    Attributes:
        ACCEPTED: Current value of the document. Inserted and format-changed
            text is visible, deleted text is excluded. This is the default.
        REJECTED: Value of the document before the changes. Deleted text is
            restored from the tracked-changes part while inserted text is
            excluded; format changes do not hide any text.
        ALL: Both the current and the removed text are reported; each
            fragment is tagged with the change it belongs to.

    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ALL = "all"


@dataclass(frozen=True)
class TextFragment:
    """A piece of extracted text tied back to its source node.

    Attributes:
        kind: Nature of the fragment: ``"text"``, ``"link"``, ``"tab"``,
            ``"line-break"``, ``"spacer"``, ``"deletion"`` or
            ``"paragraph-break"``.
        text: Visible characters of the fragment.
        start: Offset of the fragment in the plain text output.
        end: Offset of the first character after the fragment.
        source: ODF element or text node the fragment is built from,
            allowing navigation back to the original node.
        url: Target URL for a ``"link"`` fragment, None otherwise.
        change_id: Identifier of the tracked change region when the
            fragment belongs to a change, None otherwise.
        change: Role of the tracked change (``"insertion"``,
            ``"deletion"`` or ``"format-change"``), None otherwise.

    """

    kind: str
    text: str
    start: int = 0
    end: int = 0
    source: Element | EText | None = None
    url: str | None = None
    change_id: str | None = None
    change: str | None = None

    @property
    def source_tag(self) -> str | None:
        """Return the qualified tag of the source node, if an element."""
        if isinstance(self.source, Element):
            return self.source.tag
        if isinstance(self.source, EText):
            parent = self.source.parent
            if parent is not None:
                return parent.tag
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the fragment to a plain dictionary.

        Returns:
            dict: A dictionary holding the fragment data without the live
            XML source node.

        """
        return {
            "kind": self.kind,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "url": self.url,
            "change_id": self.change_id,
            "change": self.change,
            "source_tag": self.source_tag,
        }


def get_plain_text(
    element: Element,
    change_visibility: ChangeVisibility | str = ChangeVisibility.ACCEPTED,
) -> str:
    """Return the visible text of an element as a plain string.

    Spacers, tabs and line breaks are expanded to spaces, tabs and new
    lines; multi-paragraph content is joined with new lines. The tracked
    change visibility rule decides which change-marked text is included.

    Args:
        element: The element (paragraph, header, cell, ...) to read.
        change_visibility: Rule applied to tracked changes, see
            :class:`ChangeVisibility`.

    Returns:
        str: The reconstructed plain text of the element.

    """
    fragments = get_text_fragments(element, change_visibility)
    return "".join(fragment.text for fragment in fragments)


def get_text_fragments(
    element: Element,
    change_visibility: ChangeVisibility | str = ChangeVisibility.ACCEPTED,
) -> list[TextFragment]:
    """Return the element content as ordered, source-aware text fragments.

    The concatenation of the fragment ``text`` attributes is identical to
    the result of :func:`get_plain_text`. Links keep their display text and
    target URL on separate attributes, and text enclosed in tracked changes
    is tagged or filtered according to ``change_visibility``.

    Args:
        element: The element (paragraph, header, cell, ...) to read.
        change_visibility: Rule applied to tracked changes, see
            :class:`ChangeVisibility`.

    Returns:
        list[TextFragment]: The structured fragments covering the element
        text.

    """
    visibility = ChangeVisibility(change_visibility)
    builder = _FragmentBuilder(element, visibility)
    if element.tag in _BLOCK_TAGS:
        builder.scan(element)
    else:
        blocks = element.get_elements(
            ".//text:p[not(ancestor::text:tracked-changes)] "
            "| .//text:h[not(ancestor::text:tracked-changes)]"
        )
        if not blocks:
            builder.scan(element)
        else:
            for index, block in enumerate(blocks):
                if index:
                    builder.emit("paragraph-break", "\n", source=block)
                builder.scan(block)
    return builder.fragments


class _FragmentBuilder:
    """Accumulate text fragments while walking an XML subtree."""

    def __init__(self, root: Element, visibility: ChangeVisibility) -> None:
        self._visibility = visibility
        self._roles = self._collect_roles(root)
        self._open_changes: list[tuple[str, str]] = []
        self._offset = 0
        self.fragments: list[TextFragment] = []

    @staticmethod
    def _collect_roles(root: Element) -> dict[str, str]:
        tracked = root.get_element("//text:tracked-changes")
        if tracked is None:
            return {}
        roles: dict[str, str] = {}
        regions = tracked.get_elements("text:changed-region")
        for raw_region in regions:
            region = cast("TextChangedRegion", raw_region)
            change_id = region.get_id()
            change_element = region.get_change_element()
            if change_id and change_element is not None:
                roles[change_id] = change_element.tag.split(":")[-1]
        return roles

    def emit(
        self,
        kind: str,
        text: str,
        source: Element | EText | None,
        url: str | None = None,
        change_override: tuple[str, str] | None = None,
    ) -> bool:
        change_id, change = change_override or self._current_change()
        if change == _INSERTION_ROLE and self._visibility is ChangeVisibility.REJECTED:
            return False
        start = self._offset
        self._offset += len(text)
        self.fragments.append(
            TextFragment(
                kind=kind,
                text=text,
                start=start,
                end=self._offset,
                source=source,
                url=url,
                change_id=change_id,
                change=change,
            )
        )
        return True

    def _current_change(self) -> tuple[str | None, str | None]:
        if not self._open_changes:
            return None, None
        return self._open_changes[-1]

    def scan(self, element: Element) -> None:
        for obj in element.xpath("*|text()"):
            if isinstance(obj, EText):
                text = str(obj)
                if text:
                    self.emit("text", text, obj)
                continue
            self._scan_element(obj)

    def _scan_element(self, obj: Element) -> None:
        tag = obj.tag
        if tag == "text:tracked-changes":
            # The change store holds removed content, it is not current text.
            return
        if tag == "text:change":
            self._scan_deletion_marker(cast("TextChange", obj))
            return
        if tag == "text:change-start":
            change_id = cast("TextChange", obj).get_id() or ""
            role = self._roles.get(change_id, _INSERTION_ROLE)
            self._open_changes.append((change_id, role))
            return
        if tag == "text:change-end":
            self._close_change(cast("TextChange", obj).get_id())
            return
        if tag == "text:line-break":
            self.emit("line-break", "\n", obj)
            return
        if tag == "text:tab":
            self.emit("tab", "\t", obj)
            return
        if tag == "text:s":
            self.emit("spacer", obj.text, obj)
            return
        if tag == "text:a":
            self._scan_link(obj)
            return
        # Generic container (span, field, note, ...): walk its own text
        # node and children with the current change context unchanged.
        self.scan(obj)

    def _close_change(self, change_id: str | None) -> None:
        if not self._open_changes:
            return
        if change_id is None:
            self._open_changes.pop()
            return
        for index in range(len(self._open_changes) - 1, -1, -1):
            if self._open_changes[index][0] == change_id:
                del self._open_changes[index:]
                return
        self._open_changes.pop()

    def _scan_deletion_marker(self, marker: TextChange) -> None:
        change_id = marker.get_id() or ""
        role = self._roles.get(change_id, _DELETION_ROLE)
        if role != _DELETION_ROLE:
            return
        if self._visibility is ChangeVisibility.ACCEPTED:
            return
        deleted = self._deleted_paragraphs(marker, change_id)
        for index, paragraph in enumerate(deleted):
            if index:
                self.emit("paragraph-break", "\n", marker)
            for fragment in _deleted_fragments(paragraph):
                self.emit(
                    fragment.kind,
                    fragment.text,
                    fragment.source,
                    url=fragment.url,
                    change_override=(change_id, _DELETION_ROLE),
                )

    def _deleted_paragraphs(
        self, marker: TextChange, change_id: str
    ) -> Sequence[Element]:
        region = cast(
            "TextChangedRegion | None",
            marker.root.get_element(f'//text:changed-region[@text:id="{change_id}"]'),
        )
        if region is None:
            return ()
        change_element = region.get_change_element()
        if change_element is None or change_element.tag != "text:deletion":
            return ()
        deleted = cast("TextDeletion", change_element).get_deleted()
        if not isinstance(deleted, list):
            return ()
        return deleted

    def _scan_link(self, link: Element) -> None:
        sub_builder = _FragmentBuilder(link.root, self._visibility)
        sub_builder._roles = self._roles
        sub_builder._open_changes = list(self._open_changes)
        sub_builder.scan(link)
        text = "".join(fragment.text for fragment in sub_builder.fragments)
        if not text:
            return
        url = cast("Link", link).url
        self.emit("link", text, link, url=url)


def _deleted_fragments(paragraph: Element) -> list[TextFragment]:
    """Build fragments from a stored deleted paragraph."""
    sub_builder = _FragmentBuilder(paragraph.root, ChangeVisibility.ACCEPTED)
    sub_builder.scan(paragraph)
    fragments = [
        TextFragment(
            kind="deletion",
            text=fragment.text,
            start=0,
            end=0,
            source=fragment.source,
            url=fragment.url,
        )
        for fragment in sub_builder.fragments
    ]
    if fragments:
        first = fragments[0]
        fragments[0] = TextFragment(
            kind="deletion",
            text=first.text,
            start=0,
            end=0,
            source=paragraph,
            url=None,
        )
    return fragments
