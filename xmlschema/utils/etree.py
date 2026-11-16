#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import importlib
import io
import re
from collections.abc import Callable, Iterator
from typing import Any, Optional, Union
from xml.etree import ElementTree

from xmlschema.names import XSI_SCHEMA_LOCATION, XSI_NONS_SCHEMA_LOCATION, \
    SCHEMA_DECLARATION_TAGS, GLOBAL_TAGS, XSD_DEFAULT_OPEN_CONTENT
from xmlschema.aliases import ElementType, NsmapType
from xmlschema.utils.qnames import get_namespace, get_prefixed_qname

_NCNAME_PATTERN = r'[^\d\W][\w.\-]*'
_QNAME_TOKEN_PATTERN = re.compile(
    fr'^(?P<prefix>{_NCNAME_PATTERN}):(?P<local>{_NCNAME_PATTERN})$'
)


def is_etree_element(obj: object) -> bool:
    """A validator for ElementTree elements that excludes XsdElement objects."""
    return hasattr(obj, 'append') and hasattr(obj, 'tag') and hasattr(obj, 'attrib')


def is_like_etree_element(obj: Any) -> bool:
    """A validator for ElementTree elements that includes XsdElement objects."""
    return hasattr(obj, 'tag') and hasattr(obj, 'attrib') and hasattr(obj, 'text')


def is_etree_document(obj: object) -> bool:
    """A validator for ElementTree objects."""
    return hasattr(obj, 'getroot') and hasattr(obj, 'parse') and hasattr(obj, 'iter')


def is_lxml_element(obj: object) -> bool:
    """A validator for lxml elements."""
    return hasattr(obj, 'append') and hasattr(obj, 'tag') and hasattr(obj, 'attrib') \
        and hasattr(obj, 'getparent') and hasattr(obj, 'nsmap') and hasattr(obj, 'xpath')


def is_lxml_document(obj: Any) -> bool:
    return is_etree_document(obj) and hasattr(obj, 'xpath') and hasattr(obj, 'xslt')


def etree_get_ancestors(elem: ElementType, root: ElementType) -> Optional[list[ElementType]]:
    """
    Returns a list with ancestors of `elem`, `None` if `elem` is not a descendant of `root`.
    """
    if elem is root:
        return []
    else:
        ancestors = [root]

    children = iter(root)
    iterators = []
    while True:
        for child in children:
            if elem is child:
                return ancestors

            if len(child):
                ancestors.append(child)
                iterators.append(children)
                children = iter(child)
                break
        else:
            if not iterators:
                return None
            ancestors.pop()
            children = iterators.pop()


def etree_getpath(elem: ElementType,
                  root: ElementType,
                  namespaces: Optional[NsmapType] = None,
                  relative: bool = True,
                  add_position: bool = False,
                  parent_path: bool = False) -> Optional[str]:
    """
    Returns the XPath path from *root* to descendant *elem* element.

    :param elem: the descendant element.
    :param root: the root element.
    :param namespaces: an optional mapping from namespace prefix to URI.
    :param relative: returns a relative path.
    :param add_position: add context position to child elements that appear multiple times.
    :param parent_path: if set to `True` returns the parent path. Default is `False`.
    :return: An XPath expression or `None` if *elem* is not a descendant of *root*.
    """
    ancestors = etree_get_ancestors(elem, root)
    if ancestors is None:
        return None
    elif not parent_path:
        ancestors.append(elem)
    elif not ancestors:
        return None

    if relative:
        parts = ['.']
    elif namespaces:
        parts = ['', get_prefixed_qname(root.tag, namespaces)]
    else:
        parts = ['', root.tag]

    for k in range(len(ancestors) - 1):
        parent, child = ancestors[k:k+2]
        name = get_prefixed_qname(child.tag, namespaces) if namespaces else child.tag
        if add_position:
            position = siblings = 1
            for c in parent:
                if c is child:
                    position = siblings
                elif c.tag == child.tag:
                    siblings += 1

            if siblings != 1:
                parts.append(f'{name}[{position}]')
            else:
                parts.append(name)
        else:
            parts.append(name)

    return '/'.join(parts)


def iter_schema_location_hints(elem: ElementType) -> Iterator[tuple[Any, Any]]:
    """Yields schema location hints contained in the attributes of an element."""
    if XSI_SCHEMA_LOCATION in elem.attrib:
        locations = elem.attrib[XSI_SCHEMA_LOCATION].split()
        for ns, url in zip(locations[0::2], locations[1::2]):
            yield ns, url

    if XSI_NONS_SCHEMA_LOCATION in elem.attrib:
        for url in elem.attrib[XSI_NONS_SCHEMA_LOCATION].split():
            yield '', url


def iter_schema_namespaces(root: ElementType,
                           elem: Optional[ElementType] = None) -> Iterator[str]:
    """
    Yields namespaces of an ElementTree structure. If an *elem* is
    provided stops when found if during the iteration.
    """
    if root.tag != '{' and root is not elem:
        yield ''

    for e in root.iter():
        if e is elem:
            return
        elif e.tag[0] == '{':
            yield get_namespace(e.tag)

        if e.attrib:
            for name in e.attrib:
                if name[0] == '{':
                    yield get_namespace(name)


def iter_schema_declarations(root: ElementType) -> Iterator[ElementType]:
    for elem in root:
        if elem.tag in SCHEMA_DECLARATION_TAGS:
            yield elem
        elif elem.tag in GLOBAL_TAGS:
            return


def iter_schema_open_content(root: ElementType) -> Iterator[ElementType]:
    for elem in root:
        if elem.tag in XSD_DEFAULT_OPEN_CONTENT:
            yield elem
        elif elem.tag in GLOBAL_TAGS:
            return


def prune_etree(root: ElementType, selector: Callable[[ElementType], bool]) \
        -> Optional[bool]:
    """
    Removes from a tree structure the elements that verify the selector
    function. The checking and eventual removals are performed using a
    breadth-first visit method.

    :param root: the root element of the tree.
    :param selector: the single argument function to apply on each visited node.
    :return: `True` if the root node verify the selector function, `None` otherwise.
    """
    def _prune_subtree(elem: ElementType) -> None:
        for child in elem[:]:
            if selector(child):
                elem.remove(child)

        for child in elem:
            _prune_subtree(child)

    if selector(root):
        del root[:]
        return True
    _prune_subtree(root)
    return None


def etree_tostring(elem: ElementType,
                   namespaces: Optional[NsmapType] = None,
                   indent: str = '',
                   max_lines: Optional[int] = None,
                   spaces_for_tab: Optional[int] = 4,
                   xml_declaration: Optional[bool] = None,
                   encoding: str = 'unicode',
                   method: str = 'xml') -> Union[str, bytes]:
    """
    Serialize an Element tree to a string.

    :param elem: the Element instance.
    :param namespaces: is an optional mapping from namespace prefix to URI. \
    Provided namespaces are registered before serialization. Ignored if the \
    provided *elem* argument is a lxml Element instance.
    :param indent: the baseline indentation.
    :param max_lines: if truncate serialization after a number of lines \
    (default: do not truncate).
    :param spaces_for_tab: number of spaces for replacing tab characters. For \
    default tabs are replaced with 4 spaces, provide `None` to keep tab characters.
    :param xml_declaration: if set to `True` inserts the XML declaration at the head.
    :param encoding: if "unicode" (the default) the output is a string, \
    otherwise it’s binary.
    :param method: is either "xml" (the default), "html" or "text".
    :return: a Unicode string.
    """
    def reindent(line: str) -> str:
        if not line:
            return line
        elif line.startswith(min_indent):
            return line[start:] if start >= 0 else indent[start:] + line
        else:
            return indent + line

    etree_module: Any
    if isinstance(elem, ElementTree.Element):
        etree_module = ElementTree
    elif is_lxml_element(elem):
        etree_module = importlib.import_module('lxml.etree')
    else:
        raise TypeError(f"can't serialize {elem!r}")

    if namespaces and not hasattr(elem, 'nsmap'):
        default_namespace = namespaces.get('')
        for prefix, uri in namespaces.items():
            if prefix and not re.match(r'ns\d+$', prefix):
                etree_module.register_namespace(prefix, uri)
                if uri == default_namespace:
                    default_namespace = None

        if default_namespace:
            etree_module.register_namespace('', default_namespace)

    xml_text = etree_module.tostring(elem, encoding=encoding, method=method)
    if isinstance(xml_text, bytes):
        xml_text = xml_text.decode('utf-8')

    if spaces_for_tab is not None:
        xml_text = xml_text.replace('\t', ' ' * spaces_for_tab)

    if xml_text.startswith('<?xml '):
        if xml_declaration is False:
            lines = xml_text.splitlines()[1:]
        else:
            lines = xml_text.splitlines()
    elif xml_declaration and encoding.lower() != 'unicode':
        lines = ['<?xml version="1.0" encoding="{}"?>'.format(encoding)]
        lines.extend(xml_text.splitlines())
    else:
        lines = xml_text.splitlines()

    # Clear ending empty lines
    while lines and not lines[-1].strip():
        lines.pop(-1)

    if not lines or method == 'text' or (not indent and not max_lines):
        if encoding == 'unicode':
            return '\n'.join(lines)
        return '\n'.join(lines).encode(encoding)

    last_indent = ' ' * min(k for k in range(len(lines[-1])) if lines[-1][k] != ' ')
    if len(lines) > 2:
        try:
            child_indent = ' ' * min(
                k for line in lines[1:-1] for k in range(len(line)) if line[k] != ' '
            )
        except ValueError:
            child_indent = ''

        min_indent = min(child_indent, last_indent)
    else:
        min_indent = child_indent = last_indent

    start = len(min_indent) - len(indent)

    if max_lines is not None and len(lines) > max_lines + 2:
        lines = lines[:max_lines] + [child_indent + '...'] * 2 + lines[-1:]

    if encoding == 'unicode':
        return '\n'.join(reindent(line) for line in lines)
    return '\n'.join(reindent(line) for line in lines).encode(encoding)


def _canonical_qname_token(token: str, scope: NsmapType) -> str:
    """
    Normalizes a token that represents a QName, resolving the prefix with the
    namespace bindings in scope. Tokens that don't match the lexical QName
    pattern or that have an unbound prefix are returned unchanged, so colons
    in plain strings (eg. '12:30' or 'http://...') are not misinterpreted.
    """
    if not token or token[0] == '{':
        return token

    match = _QNAME_TOKEN_PATTERN.match(token)
    if match is None:
        return token

    try:
        uri = scope[match.group('prefix')]
    except KeyError:
        return token
    else:
        local = match.group('local')
        return f'{{{uri}}}{local}' if uri else local


def _canonical_name(name: str, scope: NsmapType) -> str:
    """Normalizes an element or attribute name to the extended QName form."""
    return _canonical_qname_token(name, scope)


def _canonical_value(value: Optional[str], scope: NsmapType) -> Optional[str]:
    """
    Normalizes the QName tokens of an attribute value or of a text chunk,
    leaving the other parts of the value unchanged.
    """
    if value is None:
        return None

    tokens = value.split()
    if not tokens:
        return value

    changed = False
    for k, token in enumerate(tokens):
        normalized = _canonical_qname_token(token, scope)
        if normalized != token:
            tokens[k] = normalized
            changed = True

    return ' '.join(tokens) if changed else value


def _canonical_attributes(elem: ElementType, scope: NsmapType) \
        -> tuple[tuple[str, Optional[str]], ...]:
    return tuple(sorted(
        (_canonical_name(k, scope), _canonical_value(v, scope))
        for k, v in elem.attrib.items()
    ))


def _canonical_key_from_element(elem: ElementType, scope: NsmapType) -> tuple[Any, ...]:
    nsmap = getattr(elem, 'nsmap', None)
    if nsmap:
        # An lxml element: use the namespace bindings in scope at its position
        scope = dict(scope)
        scope.update({prefix or '': uri for prefix, uri in nsmap.items() if uri})

    children = tuple(
        _canonical_key_from_element(child, scope)
        for child in elem
        if not callable(child.tag)
    )
    return (
        _canonical_name(elem.tag, scope),
        _canonical_attributes(elem, scope),
        _canonical_value(elem.text, scope),
        children,
        _canonical_value(elem.tail, scope),
    )


def _canonical_key_from_source(source: Any, namespaces: NsmapType) -> tuple[Any, ...]:
    """
    Builds the canonical key parsing a source with `iterparse`, tracking the
    namespace declarations in scope at each position, so that a prefix that
    is rebound by an inner element is not confused with the outer binding.
    """
    if isinstance(source, str) and source.lstrip().startswith('<'):
        source = io.StringIO(source)
    elif isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(bytes(source))

    root_key: Optional[tuple[Any, ...]] = None
    scope_stack = [dict(namespaces)]
    pending_ns: list[tuple[str, str]] = []
    elem_stack: list[list[Any]] = []

    events = ('start-ns', 'start', 'end')
    for event, obj in ElementTree.iterparse(source, events=events):
        if event == 'start-ns':
            pending_ns.append(obj)
        elif event == 'start':
            scope = dict(scope_stack[-1])
            for prefix, uri in pending_ns:
                if uri:
                    scope[prefix] = uri
                else:
                    scope.pop(prefix, None)  # namespace undeclaration
            pending_ns.clear()
            scope_stack.append(scope)
            elem_stack.append([
                _canonical_name(obj.tag, scope),
                _canonical_attributes(obj, scope),
                _canonical_value(obj.text, scope),
                [],
            ])
        else:
            tag, attributes, text, children = elem_stack.pop()
            scope_stack.pop()
            key = (
                tag,
                attributes,
                text,
                tuple(children),
                _canonical_value(obj.tail, scope_stack[-1]),
            )
            if elem_stack:
                elem_stack[-1][3].append(key)
            else:
                root_key = key

    if root_key is None:
        raise ElementTree.ParseError("no element found")
    return root_key


def etree_canonical_key(source: Any, namespaces: Optional[NsmapType] = None) \
        -> tuple[Any, ...]:
    """
    Builds a hashable canonical key for an XML source, suitable for comparing
    documents for content equality independently from the namespace prefixes
    in use. Element and attribute names and QName-valued tokens found in
    attribute values and text are normalized to the extended QName form, using
    the namespace bindings in scope at each position. Strings containing a
    colon that don't match the QName lexical pattern, or whose prefix is not
    bound in scope, are kept as plain strings. Comments and processing
    instructions are not part of the key.

    :param source: an Element, an ElementTree, a file path, a file-like object \
    or a string/bytes chunk containing XML data. For already parsed ElementTree \
    elements the namespace declarations are not available, so the optional \
    *namespaces* argument is used as the in-scope map for the whole tree; lxml \
    elements and parsed sources instead keep the original bindings of each position.
    :param namespaces: an optional mapping from prefixes to namespace URIs that \
    is used as baseline scope for name resolution.
    """
    base_scope: NsmapType = dict(namespaces) if namespaces else {}
    if is_etree_element(source):
        return _canonical_key_from_element(source, base_scope)
    elif is_etree_document(source):
        return _canonical_key_from_element(source.getroot(), base_scope)
    else:
        return _canonical_key_from_source(source, base_scope)


def etree_elements_equal(elem: Any, other: Any,
                         namespaces: Optional[NsmapType] = None,
                         other_namespaces: Optional[NsmapType] = None) -> bool:
    """
    Tests the content equality of two XML sources, ignoring namespace prefix
    renames. Two documents that differ only for the choice of the prefixes are
    considered equal, provided that QName values and names resolve to the same
    expanded names with the namespace bindings in scope at each position.

    :param elem: the reference XML source (an Element, an ElementTree, a file \
    path, a file-like object or a string/bytes chunk containing XML data).
    :param other: the other XML source to compare.
    :param namespaces: optional baseline namespace map for the first source.
    :param other_namespaces: optional baseline namespace map for the other source.
    """
    return etree_canonical_key(elem, namespaces) == \
        etree_canonical_key(other, other_namespaces)
