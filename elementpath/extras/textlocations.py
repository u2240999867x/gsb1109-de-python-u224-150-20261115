#
# Copyright (c), 2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
Helpers for locating the text nodes matched by an XPath expression.

The standard :func:`elementpath.select` API unwraps each matched text node into
its bare string value, so two text nodes containing the same string become
indistinguishable and the originating element (an element's ``text`` property or
the ``tail`` of a preceding child) is lost.

The functions in this module keep the matched :class:`~elementpath.TextNode`
objects and describe each one with its exact provenance:

* the kind of source, i.e. an element *text* or a child's *tail*;
* the element that owns the string in the ElementTree data model;
* the position of the text node among its parent's text nodes;
* the document order position of the node and of its owning element.

Two text nodes holding the same string keep different identities and are
reported separately; the full text of a parent element is never returned in
place of a single matched text node.
"""
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Optional, Union

import elementpath.aliases as ta

from elementpath.exceptions import ElementPathTypeError
from elementpath.xpath1 import XPath1Parser
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath_context import XPathContext
from elementpath.xpath_nodes import TextNode, ElementNode, EtreeElementNode, \
    CommentNode, ProcessingInstructionNode

__all__ = ['TextMatch', 'locate_text', 'iter_locate_text', 'text_node_source']

#: Matched text comes from the ``text`` property of an element.
TEXT = 'text'

#: Matched text is the ``tail`` of a preceding child element, comment or PI.
TAIL = 'tail'

_PrecedingNode = Union[EtreeElementNode, CommentNode, ProcessingInstructionNode]


@dataclass(frozen=True)
class TextMatch:
    """
    A text node matched by an XPath expression, together with its provenance.

    :param node: the matched :class:`~elementpath.TextNode`.
    :param value: the text content, equal to ``node.value``.
    :param source: :data:`TEXT` if the content is the parent element's
        ``text``, :data:`TAIL` if it is the ``tail`` of a preceding child.
    :param owner: the element that owns the string. For a tail this is the
        parent of the child carrying the ``tail`` property.
    :param anchor: for a tail, the child node whose wrapped object carries the
        tail (an element, comment or processing instruction); `None` for a text.
    :param text_position: the 1-based position of the text node among the text
        nodes of its parent, i.e. the predicate used by ``text()[N]``.
    :param fragment_order: the 1-based order of the match within the returned
        sequence, following XPath document order.
    :param node_position: the document total-order position of the text node.
    :param element_position: the document total-order position of *owner*.
    """
    node: TextNode
    value: str
    source: str
    owner: ElementNode
    anchor: Optional[_PrecedingNode]
    text_position: int
    fragment_order: int
    node_position: int
    element_position: int

    @property
    def is_tail(self) -> bool:
        """`True` if the match is the tail of a preceding child node."""
        return self.source == TAIL

    @property
    def path(self) -> str:
        """The XPath path of the matched text node."""
        return self.node.path


def text_node_source(node: TextNode) -> tuple[str, ElementNode, Optional[_PrecedingNode]]:
    """
    Determines the exact ElementTree source of a text node.

    :returns: a tuple ``(source, owner, anchor)`` where *source* is
        :data:`TEXT` or :data:`TAIL`, *owner* is the element node that owns
        the string and *anchor* is the preceding child carrying the tail
        (`None` for an element text).
    """
    parent = node.parent
    if not isinstance(parent, ElementNode):
        msg = f'text node {node!r} has no parent element'
        raise ElementPathTypeError(msg)

    index = parent.children.index(node)
    if index > 0:
        preceding = parent.children[index - 1]
        if isinstance(preceding, (EtreeElementNode, CommentNode, ProcessingInstructionNode)) \
                and preceding.value.tail is not None:
            return TAIL, parent, preceding

    return TEXT, parent, None


def _iter_text_matches(root: ta.RootArgType,
                       path: str,
                       namespaces: Optional[ta.NamespacesType],
                       parser: Optional[ta.ParserClassType],
                       uri: Optional[str],
                       fragment: Optional[bool],
                       kwargs: dict[str, Any]) -> Iterator[TextMatch]:
    if parser is None:
        parser = XPath2Parser
    elif not issubclass(parser, XPath1Parser):
        msg = "argument 'parser' must be an XPath1Parser subclass"
        raise ElementPathTypeError(msg)

    root_token = parser(namespaces, **kwargs).parse(path)
    context = XPathContext(root, namespaces, uri, fragment)

    fragment_order = 0
    for item in root_token.select(context):
        if not isinstance(item, TextNode):
            msg = (f"expression {path!r} matched a non-text item of type "
                   f"{type(item).__name__}; use an expression ending with text()")
            raise ElementPathTypeError(msg)

        source, owner, anchor = text_node_source(item)
        fragment_order += 1

        text_position = 0
        for child in owner.children:
            if isinstance(child, TextNode):
                text_position += 1
                if child is item:
                    break
        else:  # pragma: no cover
            msg = f'text node {item!r} not found in its parent children'
            raise ElementPathTypeError(msg)

        yield TextMatch(
            node=item,
            value=item.value,
            source=source,
            owner=owner,
            anchor=anchor,
            text_position=text_position,
            fragment_order=fragment_order,
            node_position=item.position,
            element_position=owner.position,
        )


def iter_locate_text(root: ta.RootArgType,
                     path: str,
                     namespaces: Optional[ta.NamespacesType] = None,
                     parser: Optional[ta.ParserClassType] = None,
                     uri: Optional[str] = None,
                     fragment: Optional[bool] = None,
                     **kwargs: Any) -> Iterator[TextMatch]:
    """
    Iterates the text nodes matched by *path*, preserving node identity.

    Each yielded :class:`TextMatch` refers to a distinct text node, even when
    several matches contain the same string. Matches are generated in XPath
    document order.

    :param root: an Element, an ElementTree, a schema or an already built node tree.
    :param path: an XPath expression that selects text nodes (e.g. ``//text()``).
    :param namespaces: an optional mapping from prefixes to namespace URIs.
    :param parser: the parser class to use, :class:`~elementpath.XPath2Parser` by default.
    :param uri: an optional URI associated with the root element or the document.
    :param fragment: whether the root has to be considered a fragment.
    :param kwargs: other optional parameters for the parser instance.
    """
    yield from _iter_text_matches(root, path, namespaces, parser, uri, fragment, kwargs)


def locate_text(root: ta.RootArgType,
                path: str,
                namespaces: Optional[ta.NamespacesType] = None,
                parser: Optional[ta.ParserClassType] = None,
                uri: Optional[str] = None,
                fragment: Optional[bool] = None,
                **kwargs: Any) -> list[TextMatch]:
    """
    Returns the text nodes matched by *path* as a list of :class:`TextMatch`.

    The list keeps the document order of the matches and each entry records the
    text node source, the owning element and the relative positions.
    """
    return list(_iter_text_matches(root, path, namespaces, parser, uri, fragment, kwargs))
