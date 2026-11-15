#!/usr/bin/python
#    This file is part of python-evtx.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""
Export an event body (`EventData` / `UserData`) as a stable long table.

The rows are built by walking the binary XML node tree, not by re-parsing the
rendered XML document.  That preserves details that an XML round trip would
otherwise lose:

* the namespace of every element and attribute,
* the original order of every element, including repeated ``Data`` names,
* the difference between a missing/null value and a present empty value, and
* the value type declared in the binary XML (for example ``WSTRINGARRAY``).

Values are always kept as text.  In particular, a ``WStringArray`` substitution
renders as ``<string>...</string>`` fragments, but it remains one text value of
type ``WSTRINGARRAY``; it is never parsed as nested XML.
"""

from __future__ import absolute_import

from collections import OrderedDict
import csv
import io
import json
import xml.etree.ElementTree as ET

from . import Nodes as e_nodes
from . import Views as e_views

# Fixed columns: consumers can rely on the same names for every record and file.
FIELD_COLUMNS = (
    "field_id",
    "record_number",
    "event_record_id",
    "section",
    "path",
    "parent_path",
    "depth",
    "position",
    "occurrence",
    "namespace",
    "prefix",
    "element",
    "name",
    "name_namespace",
    "value",
    "is_null",
    "is_empty",
    "empty_element",
    "has_children",
    "type_code",
    "type_name",
    "value_type",
    "attributes",
    "namespace_declarations",
)

SECTION_NAMES = ("EventData", "UserData")

_NODE_TYPE_NAMES = {
    value: name for name, value in vars(e_nodes.NODE_TYPES).items() if name.isupper() and isinstance(value, int)
}
for _value_type, _type_name_ in tuple(_NODE_TYPE_NAMES.items()):
    _NODE_TYPE_NAMES.setdefault(0x80 | _value_type, "%sARRAY" % _type_name_)
for _array_type in range(0x80, 0x100):
    _NODE_TYPE_NAMES.setdefault(_array_type, "ARRAY_0x%02X" % _array_type)


class FieldRow(dict):
    """
    One row of the long table.

    It is a plain ``dict`` so it serializes directly, and its columns are also
    readable as attributes: ``row.name`` is the same as ``row["name"]``.
    A handful of descriptive aliases are accepted for convenience, but the
    canonical column names in `FIELD_COLUMNS` are what gets serialized.
    """

    _ALIASES = {
        "ordinal": "occurrence",
        "name_occurrence": "occurrence",
        "order": "position",
        "sequence": "position",
        "index": "position",
        "type": "type_name",
        "original_type": "type_name",
        "original_type_code": "type_code",
        "field_type": "type_name",
        "field_type_code": "type_code",
        "data_type": "type_name",
        "record_id": "event_record_id",
        "event_id": "event_record_id",
        "tag": "element",
        "local_name": "element",
        "xml_namespace": "namespace",
        "field_namespace": "namespace",
        "field_name": "name",
        "data_name": "name",
        "field_value": "value",
        "data_value": "value",
        "null": "is_null",
        "is_missing": "is_null",
        "empty": "is_empty",
        "is_blank": "is_empty",
        "parent": "parent_path",
        "raw_type_code": "type_code",
        "raw_type": "type_name",
    }

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        alias = self._ALIASES.get(key)
        if alias is not None and dict.__contains__(self, alias):
            return dict.__getitem__(self, alias)
        raise KeyError(key)

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self):
        return OrderedDict((column, dict.get(self, column)) for column in FIELD_COLUMNS)


class _Scope(object):
    """A namespace scope: prefix to URI, plus the default namespace URI."""

    __slots__ = ("prefixes", "default")

    def __init__(self, prefixes=None, default=""):
        self.prefixes = dict(prefixes or {})
        self.default = default

    def child(self, declarations):
        prefixes = dict(self.prefixes)
        default = self.default
        for prefix, uri in declarations:
            if prefix == "":
                default = uri
            else:
                prefixes[prefix] = uri
        return _Scope(prefixes, default)

    def resolve_element(self, prefix):
        if prefix == "":
            return self.default
        return self.prefixes.get(prefix, "")

    def resolve_attribute(self, prefix):
        # An unprefixed attribute is in no namespace, even under a default
        # namespace declaration (XML Namespaces, section 6.2).
        if prefix == "":
            return ""
        return self.prefixes.get(prefix, "")


def _split_name(name):
    if ":" in name:
        return tuple(name.split(":", 1))
    return "", name


def _type_name(type_code):
    return _NODE_TYPE_NAMES.get(type_code, "UNKNOWN_0x%02X" % (type_code or 0))


def _node_value_type(node):
    return type(node).__name__ if node is not None else "NullTypeNode"


def _value_node_string(node):
    if node is None or isinstance(node, e_nodes.NullTypeNode):
        return None
    if isinstance(node, e_nodes.BXmlTypeNode):
        # Nested body content is exported as elements, not as parent text.
        return None
    return node.string()


def _resolve_content(node, parent_root):
    """
    Resolve an element/attribute value node.

    Returns ``(value, type_code, value_type, present)``.  ``present`` is False
    for a null substitution or when there is no value node at all.

    ``type_code`` is the type declared by the containing `ValueNode` / substitution
    token.  If that node does not declare a type (for example a bare `BXmlTypeNode`),
    the substitution definition's type byte is used when it is available.
    """
    if node is None:
        return None, None, "NullTypeNode", False

    if isinstance(node, (e_nodes.NormalSubstitutionNode, e_nodes.ConditionalSubstitutionNode)):
        declared_type = node.type()
        substitutions = parent_root.substitutions()
        try:
            value_node = substitutions[node.index()]
        except IndexError:
            raise e_nodes.ParseException("substitution index out of range: %d" % node.index())
        if isinstance(value_node, e_nodes.NullTypeNode):
            return None, declared_type, "NullTypeNode", False
        return _value_node_string(value_node), declared_type, _node_value_type(value_node), True

    if isinstance(node, e_nodes.ValueNode):
        declared_type = node.type()
        try:
            value_node = node.children()[0]
        except IndexError:
            return None, declared_type, "NullTypeNode", False
        if isinstance(value_node, e_nodes.NullTypeNode):
            return None, declared_type, "NullTypeNode", False
        return _value_node_string(value_node), declared_type, _node_value_type(value_node), True

    if isinstance(node, e_nodes.BXmlTypeNode):
        # Nested body content is exported as elements, not as parent text.
        return None, None, "BXmlTypeNode", False

    return None, None, _node_value_type(node), False


def _attribute_value(attribute, parent_root):
    value, _, _, present = _resolve_content(attribute.attribute_value(), parent_root)
    return value if present else None


def _own_scope(base_scope, element, parent_root):
    """Apply an element's xmlns declarations and return (scope, declarations)."""
    declarations = []
    for child in element.children():
        if not isinstance(child, e_nodes.AttributeNode):
            continue
        raw_name = child.attribute_name().string()
        value = _attribute_value(child, parent_root)
        if raw_name == "xmlns":
            declarations.append(("", value or ""))
        elif raw_name.startswith("xmlns:"):
            declarations.append((raw_name.split(":", 1)[1], value or ""))
    return base_scope.child(declarations), declarations


def _element_attributes(element, scope, parent_root):
    attributes = []
    for child in element.children():
        if not isinstance(child, e_nodes.AttributeNode):
            continue
        raw_name = child.attribute_name().string()
        if raw_name == "xmlns" or raw_name.startswith("xmlns:"):
            continue
        prefix, local = _split_name(raw_name)
        attributes.append(
            OrderedDict(
                (
                    ("name", raw_name),
                    ("prefix", prefix),
                    ("local_name", local),
                    ("namespace", scope.resolve_attribute(prefix)),
                    ("value", _attribute_value(child, parent_root)),
                )
            )
        )
    return attributes


def _element_content(element, parent_root):
    """Return (value, type_code, value_type, present) for an element's text."""
    content_nodes = []
    for child in element.children():
        if isinstance(child, (e_nodes.ValueNode, e_nodes.NormalSubstitutionNode, e_nodes.ConditionalSubstitutionNode)):
            content_nodes.append(child)
        elif isinstance(child, e_nodes.CDataSectionNode):
            content_nodes.append(child)

    if not content_nodes:
        for child in element.children():
            if not isinstance(child, (e_nodes.NormalSubstitutionNode, e_nodes.ConditionalSubstitutionNode)):
                continue
            try:
                nested = parent_root.substitutions()[child.index()]
            except IndexError:
                continue
            if isinstance(nested, e_nodes.BXmlTypeNode):
                # Body containers (EventData/UserData and nested UserData
                # trees) carry the declared BXML type and no scalar value.
                return "", child.type(), "BXmlTypeNode", True
        static_children = any(isinstance(child, e_nodes.OpenStartElementNode) for child in element.children())
        if static_children:
            return "", None, "", True
        return None, None, "NullTypeNode", False

    value, type_code, value_type, present = _resolve_content(content_nodes[0], parent_root)
    if len(content_nodes) > 1:
        parts = []
        for content_node in content_nodes:
            part, _, _, part_present = _resolve_content(content_node, parent_root)
            if part_present and part:
                parts.append(part)
        value = "".join(parts)
        present = True
    return value, type_code, value_type, present


def _iter_element_rows(
    element, parent_root, section, parent_path, depth, sibling_index, scope, rows, root_type_code=None
):
    raw_tag = element.tag_name()
    prefix, local = _split_name(raw_tag)

    scope, declarations = _own_scope(scope, element, parent_root)
    namespace = scope.resolve_element(prefix)
    attributes = _element_attributes(element, scope, parent_root)

    name_value = None
    name_namespace = None
    for attribute in attributes:
        if attribute["local_name"] == "Name" and attribute["prefix"] == "":
            name_value = attribute["value"]
            name_namespace = attribute["namespace"]
            break

    value, type_code, value_type, present = _element_content(element, parent_root)
    if root_type_code is not None and depth == 0:
        type_code = root_type_code
        if not value_type:
            value_type = "BXmlTypeNode"
    empty_element = any(isinstance(child, e_nodes.CloseEmptyElementNode) for child in element.children())
    static_children = [child for child in element.children() if isinstance(child, e_nodes.OpenStartElementNode)]
    nested_roots = _nested_bxml_roots(element, parent_root)
    has_children = bool(static_children or nested_roots)

    if parent_path:
        path = "%s/%s[%d]" % (parent_path, local, sibling_index)
    else:
        path = "/%s[%d]" % (local, sibling_index)

    rows.append(
        FieldRow(
            (
                ("field_id", None),
                ("record_number", None),
                ("event_record_id", None),
                ("section", section),
                ("path", path),
                ("parent_path", parent_path),
                ("depth", depth),
                ("position", None),
                ("occurrence", None),
                ("namespace", namespace),
                ("prefix", prefix),
                ("element", local),
                ("name", name_value),
                ("name_namespace", name_namespace),
                ("value", value if present else ""),
                ("is_null", not present),
                ("is_empty", (value is None or value == "") if present else True),
                ("empty_element", empty_element),
                ("has_children", has_children),
                ("type_code", type_code),
                ("type_name", _type_name(type_code) if type_code is not None else ""),
                ("value_type", value_type),
                ("attributes", attributes),
                ("namespace_declarations", declarations),
            )
        )
    )

    # Expand static children and nested BXML substitutions in document order.
    # The sibling index is per parent path and per local name, so repeated
    # `Data` names get `/EventData[1]/Data[1]`, `/Data[2]`, and so on.
    child_counts = {}

    def _emit_child(child_element, child_root):
        _, child_local = _split_name(child_element.tag_name())
        child_counts[child_local] = child_counts.get(child_local, 0) + 1
        _iter_element_rows(
            child_element,
            child_root,
            section,
            path,
            depth + 1,
            child_counts[child_local],
            scope,
            rows,
        )

    for child in element.children():
        if isinstance(child, e_nodes.OpenStartElementNode):
            _emit_child(child, parent_root)
        elif isinstance(child, (e_nodes.NormalSubstitutionNode, e_nodes.ConditionalSubstitutionNode)):
            try:
                nested = parent_root.substitutions()[child.index()]
            except IndexError:
                continue
            if not isinstance(nested, e_nodes.BXmlTypeNode):
                continue
            nested_root = nested.root()
            for nested_child in nested_root.template().children():
                if isinstance(nested_child, e_nodes.OpenStartElementNode):
                    _emit_child(nested_child, nested_root)


def _event_element(root):
    for child in root.template().children():
        if isinstance(child, e_nodes.OpenStartElementNode) and _split_name(child.tag_name())[1] == "Event":
            return child
    return None


def _event_scope(root):
    scope = _Scope()
    element = _event_element(root)
    if element is not None:
        scope, _ = _own_scope(scope, element, root)
    return scope


def _body_sections(root):
    """
    Yield ``(section_name, element_node, substitutions_root, scope, type_code)``
    for each `EventData` / `UserData` element in the event.

    The body is usually a `BXmlTypeNode` substitution on `Event`; a static body
    element is also supported.
    """
    event = _event_element(root)
    if event is None:
        return
    event_scope = _event_scope(root)

    for child in event.children():
        if isinstance(child, e_nodes.OpenStartElementNode):
            _, local = _split_name(child.tag_name())
            if local in SECTION_NAMES:
                yield local, child, root, event_scope, None
        elif isinstance(child, (e_nodes.NormalSubstitutionNode, e_nodes.ConditionalSubstitutionNode)):
            try:
                body = root.substitutions()[child.index()]
            except IndexError:
                continue
            if not isinstance(body, e_nodes.BXmlTypeNode):
                continue
            body_root = body.root()
            for body_child in body_root.template().children():
                if not isinstance(body_child, e_nodes.OpenStartElementNode):
                    continue
                _, local = _split_name(body_child.tag_name())
                if local in SECTION_NAMES:
                    yield local, body_child, body_root, event_scope, child.type()


def _event_record_id(root):
    event = _event_element(root)
    if event is None:
        return None
    for system in event.children():
        if not isinstance(system, e_nodes.OpenStartElementNode) or _split_name(system.tag_name())[1] != "System":
            continue
        for field in system.children():
            if (
                not isinstance(field, e_nodes.OpenStartElementNode)
                or _split_name(field.tag_name())[1] != "EventRecordID"
            ):
                continue
            value, _, _, present = _element_content(field, root)
            return value if present else None
    return None


def _nested_bxml_roots(element, parent_root):
    roots = []
    for child in element.children():
        if isinstance(child, (e_nodes.NormalSubstitutionNode, e_nodes.ConditionalSubstitutionNode)):
            try:
                nested = parent_root.substitutions()[child.index()]
            except IndexError:
                continue
            if isinstance(nested, e_nodes.BXmlTypeNode):
                roots.append(nested.root())
    return roots


def _is_container(row):
    return row["has_children"] or row["element"] in SECTION_NAMES


def _assign_stable_indexes(rows, record_number, event_record_id):
    """
    Fill document position, duplicate-name occurrence, record identifiers and
    field_id.  All of them derive only from the parsed structure.
    """
    for position, row in enumerate(rows, 1):
        row["position"] = position

    occurrences = {}
    for row in rows:
        key = (row["section"], row["parent_path"], row["namespace"], row["element"], row["name"])
        occurrences[key] = occurrences.get(key, 0) + 1
        row["occurrence"] = occurrences[key]

    record_key = event_record_id if event_record_id is not None else record_number
    for row in rows:
        row["record_number"] = record_number
        row["event_record_id"] = event_record_id
        row["field_id"] = "%s:%s" % (record_key, row["path"])


def record_fields(record, include_containers=True):
    """
    Return the long-form field table for one `Evtx.Evtx.Record`.

    Args:
      record (Evtx.Evtx.Record): the record to inspect.
      include_containers (bool): when False, drop the section root and any
        element that has children, keeping only leaf data fields.

    Returns:
      list[FieldRow]: rows in document order.
    """
    root = record.root()
    event_record_id = _event_record_id(root)
    rows = []

    for section, element, parent_root, inherited_scope, type_code in _body_sections(root):
        _iter_element_rows(element, parent_root, section, "", 0, 1, inherited_scope, rows, type_code)

    _assign_stable_indexes(rows, record.record_num(), event_record_id)

    if not include_containers:
        rows = [row for row in rows if not _is_container(row)]
    return rows


def field_table(evtx_file, include_containers=True):
    """
    Yield one list of `FieldRow` per record in an open `Evtx.Evtx.Evtx`.
    """
    for record in evtx_file.records():
        yield record_fields(record, include_containers=include_containers)


def fields_to_dicts(rows):
    """Convert rows to plain ordered dicts with the fixed columns."""
    return [row.as_dict() for row in rows]


def export_fields(record, include_containers=True):
    """Alias for `record_fields`, for callers that prefer an export-style name."""
    return record_fields(record, include_containers=include_containers)


def flatten_record(record, include_containers=True):
    """Alias for `record_fields`; emphasizes the long/flat row shape."""
    return record_fields(record, include_containers=include_containers)


def rebuild_xml(rows):
    """Alias for `rebuild_body_xml`."""
    return rebuild_body_xml(rows)


def verify(record, rows=None):
    """Alias for `verify_roundtrip`."""
    return verify_roundtrip(record, rows)


def rows_to_json(rows, indent=None):
    """Serialize rows as JSON; attributes and namespace declarations stay structured."""
    return json.dumps(fields_to_dicts(rows), ensure_ascii=False, indent=indent)


def rows_to_csv(rows=(), include_header=True, header_only=False):
    """
    Serialize rows as CSV with the fixed `FIELD_COLUMNS` header.

    Complex columns are compact JSON inside a single cell.  `header_only=True`
    writes just the header, which lets a caller stream record-by-record.
    """
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELD_COLUMNS, lineterminator="\n", extrasaction="ignore")
    if include_header or header_only:
        writer.writeheader()
    if header_only:
        return stream.getvalue()
    for row in rows:
        encoded = dict(row)
        encoded["attributes"] = json.dumps(row["attributes"], ensure_ascii=False, separators=(",", ":"))
        encoded["namespace_declarations"] = json.dumps(
            row["namespace_declarations"], ensure_ascii=False, separators=(",", ":")
        )
        writer.writerow(encoded)
    return stream.getvalue()


def _row_xml_name(row):
    return "%s:%s" % (row["prefix"], row["element"]) if row["prefix"] else row["element"]


def _attr_escape(value):
    """
    Escape an attribute value for a double-quoted attribute.

    `Evtx.Views.escape_attr` uses `quoteattr`, which may choose single quotes
    when the value contains a double quote.  The rebuild always uses double
    quotes, so strip the chosen quotes and normalize `"` to `&quot;`.
    """
    escaped = e_views.escape_attr(value or "")
    if len(escaped) >= 2 and escaped[0] == escaped[-1] and escaped[0] in "\"'":
        escaped = escaped[1:-1]
    escaped = escaped.replace('"', "&quot;")
    # XML parsers normalize literal CR/LF/TAB in attribute values; use
    # character references so the exported text survives the round trip.
    return escaped.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")


def _text_escape(value):
    """Escape element text, preserving CR/LF exactly across an XML parse."""
    escaped = e_views.escape_value(value or "")
    return escaped.replace("\r", "&#13;")


def _row_xml_attributes(row, scope):
    parts = []
    declared_prefixes = set(prefix for prefix, _ in row["namespace_declarations"])
    for prefix, uri in row["namespace_declarations"]:
        raw_name = "xmlns" if prefix == "" else "xmlns:%s" % prefix
        parts.append('%s="%s"' % (raw_name, _attr_escape(uri)))

    # The fragment may inherit declarations from outside (`Event` carries the
    # event namespace).  Re-declare what is needed so the rebuilt fragment
    # parses with the same namespaces on its own.
    if row["namespace"] and scope.get("", "") != row["namespace"] and "" not in declared_prefixes:
        parts.append('xmlns="%s"' % _attr_escape(row["namespace"]))
        declared_prefixes.add("")
    if row["prefix"] and scope.get(row["prefix"]) != row["namespace"] and row["prefix"] not in declared_prefixes:
        parts.append('xmlns:%s="%s"' % (row["prefix"], _attr_escape(row["namespace"])))
        declared_prefixes.add(row["prefix"])

    for attribute in row["attributes"]:
        if (
            attribute["prefix"]
            and attribute["namespace"]
            and scope.get(attribute["prefix"]) != attribute["namespace"]
            and attribute["prefix"] not in declared_prefixes
        ):
            parts.append('xmlns:%s="%s"' % (attribute["prefix"], _attr_escape(attribute["namespace"])))
            declared_prefixes.add(attribute["prefix"])
        parts.append('%s="%s"' % (attribute["name"], _attr_escape(attribute["value"])))
    return "".join(" " + part for part in parts)


def rebuild_body_xml(rows):
    """
    Rebuild the `<EventData>` / `<UserData>` fragment(s) from long-table rows.

    Rows must be in `position` order (sorting is applied here).  The depth
    stack restores element order, nesting, attributes and namespace
    declarations.  Null and empty values stay empty, and text that looks like
    XML is escaped as text because that was its original value type.

    If the event has both an `EventData` and a `UserData` section, both
    fragments are returned in row order.
    """
    if not rows:
        return ""

    ordered = sorted(rows, key=lambda row: row["position"])
    if ordered[0]["depth"] != 0:
        raise ValueError("the first row must be the section root")

    return "".join(_rebuild_section(section_rows) for section_rows in _split_sections(ordered))


def _split_sections(ordered):
    """Split position-ordered rows into one list per contiguous section run."""
    groups = []
    current = []
    for row in ordered:
        if row["depth"] == 0 and current:
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    return groups


def _rebuild_section(ordered):
    """Rebuild one EventData/UserData fragment; `ordered` starts at depth 0."""
    scopes = {}
    acc = []
    open_names = []
    for index, row in enumerate(ordered):
        depth = row["depth"]
        if depth > len(open_names):
            raise ValueError("non-contiguous depth at %s" % row["path"])
        while len(open_names) > depth:
            acc.append("</%s>" % open_names.pop())

        parent_scope = scopes.get(row["parent_path"], {})
        tag = _row_xml_name(row)
        attributes = _row_xml_attributes(row, parent_scope)
        has_children = index + 1 < len(ordered) and ordered[index + 1]["depth"] > depth
        if has_children:
            acc.append("<%s%s>" % (tag, attributes))
            open_names.append(tag)
        elif row["empty_element"] and row["is_null"]:
            acc.append("<%s%s />" % (tag, attributes))
        elif row["is_null"]:
            # No value node at all: render an explicit empty pair so the
            # element still appears in order.
            acc.append("<%s%s></%s>" % (tag, attributes, tag))
        else:
            acc.append("<%s%s>%s</%s>" % (tag, attributes, _text_escape(row["value"]), tag))

        scope = dict(parent_scope)
        for prefix, uri in row["namespace_declarations"]:
            scope[prefix] = uri
        if row["namespace"] and scope.get("", "") != row["namespace"]:
            scope[""] = row["namespace"]
        if row["prefix"] and scope.get(row["prefix"]) != row["namespace"]:
            scope[row["prefix"]] = row["namespace"]
        scopes[row["path"]] = scope

    while open_names:
        acc.append("</%s>" % open_names.pop())
    return "".join(acc)


def _clark_namespace(tag):
    if tag.startswith("{"):
        namespace, local = tag[1:].split("}", 1)
        return namespace, local
    return "", tag


def _xml_safe_text(value):
    """
    Apply the same restricted-character filter used by `Evtx.Views`.

    XML (even 1.1, as implemented by common parsers) cannot carry characters
    such as U+0003.  The long-table keeps the original text; this helper is
    only used when comparing a rebuilt XML document and makes that limitation
    explicit.
    """
    return e_views.RESTRICTED_CHARS.sub("", value or "")


def _parsed_elements(xml_text):
    root = ET.fromstring(xml_text)
    elements = []

    def walk(element, depth):
        namespace, local = _clark_namespace(element.tag)
        attributes = []
        for raw_name, value in element.attrib.items():
            attr_namespace, attr_local = _clark_namespace(raw_name)
            attributes.append((attr_namespace, attr_local, value))
        attributes.sort()
        children = list(element)
        elements.append(
            (
                depth,
                namespace,
                local,
                element.get("Name"),
                element.text or "",
                tuple(attributes),
                len(children),
            )
        )
        for child in children:
            walk(child, depth + 1)

    walk(root, 0)
    return elements


def _source_body_elements(record):
    """
    Parse the rendered body of a record into normalized element tuples.

    Returns ``[(section, [(depth, namespace, local, name, text, attrs, child_count), ...]), ...]``
    in document order.  Namespace declarations are implicit in `ElementTree`
    and are therefore not part of the comparison.
    """
    xml = record.xml()
    root = ET.fromstring("<Events>%s</Events>" % xml)
    if len(root) == 0:
        return []

    event = root[0]
    sections = []
    for child in event:
        namespace, local = _clark_namespace(child.tag)
        if local not in SECTION_NAMES:
            continue
        sections.append((local, _parsed_elements(ET.tostring(child, encoding="unicode"))))
    return sections


def _normalize_xml_newlines(value):
    """Apply the XML end-of-line normalization a parser performs on text."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _normalized_row(row, *, source_style=False):
    attrs = []
    for attribute in row["attributes"]:
        value = _xml_safe_text(attribute["value"])
        if source_style:
            value = _normalize_xml_newlines(value)
        attrs.append((attribute["namespace"], attribute["local_name"], value))
    attrs.sort()

    value = _xml_safe_text(row["value"])
    if source_style:
        value = _normalize_xml_newlines(value)
    return (
        row["depth"],
        row["namespace"],
        row["element"],
        row["name"],
        value,
        tuple(attrs),
    )


def verify_roundtrip(record, rows=None, *, check_source=True):
    """
    Verify that the long table is lossless for element order and content.

    Two checks are performed:

    1. `rebuild_body_xml(rows)` is parsed and compared against the rows
       (depth, namespace, local name, Name attribute, text, attribute values,
       child count, and element order).
    2. Unless ``check_source`` is False, the same normalized sequence is
       compared against the body of `record.xml()`.

    A value such as a WSTRINGARRAY's ``<string>...</string>`` fragments must
    still be text (child count zero) in both checks, which catches accidental
    nested parsing.

    Returns:
      bool: True when both checks match.

    Raises:
      ValueError: on the first difference, with the element index.
    """
    if rows is None:
        rows = record_fields(record)
    rows = list(rows)

    ordered = sorted(rows, key=lambda item: item["position"])
    expected_by_section = OrderedDict()
    source_by_section = OrderedDict()
    for row in ordered:
        expected_by_section.setdefault(row["section"], []).append(_normalized_row(row))
        source_by_section.setdefault(row["section"], []).append(_normalized_row(row, source_style=True))

    # Check 1: rebuild from rows and compare against rows.
    rebuilt_by_section = OrderedDict()
    current_section = None
    current_rows = []
    for row in ordered:
        if current_section is None:
            current_section = row["section"]
        if row["section"] != current_section:
            rebuilt_by_section[current_section] = _parsed_elements(rebuild_body_xml(current_rows))
            current_section = row["section"]
            current_rows = []
        current_rows.append(row)
    if current_section is not None:
        rebuilt_by_section[current_section] = _parsed_elements(rebuild_body_xml(current_rows))

    for section, expected in expected_by_section.items():
        actual = rebuilt_by_section[section]
        # Child counts are already part of `_parsed_elements`; rebuild the
        # expected child counts from the depth sequence.
        expected_with_children = _with_child_counts(expected)
        if expected_with_children != actual:
            for index, (expected_element, actual_element) in enumerate(zip(expected_with_children, actual)):
                if expected_element != actual_element:
                    raise ValueError(
                        "rebuilt element %d in %s differs: expected %r, got %r"
                        % (index, section, expected_element, actual_element)
                    )
            raise ValueError(
                "rebuilt element count for %s differs: expected %d, got %d"
                % (section, len(expected_with_children), len(actual))
            )

    # Check 2: compare rows against the original rendered body.
    if check_source:
        source_sections = _source_body_elements(record)
        actual_sections = OrderedDict(source_sections)
        if list(actual_sections.keys()) != list(source_by_section.keys()):
            raise ValueError(
                "sections differ: rows %r, source %r" % (list(source_by_section.keys()), list(actual_sections.keys()))
            )
        for section, source_elements in actual_sections.items():
            expected_with_children = _with_child_counts(source_by_section[section])
            if expected_with_children != source_elements:
                for index, (expected_element, source_element) in enumerate(
                    zip(expected_with_children, source_elements)
                ):
                    if expected_element != source_element:
                        raise ValueError(
                            "source element %d in %s differs from rows: expected %r, got %r"
                            % (index, section, expected_element, source_element)
                        )
                raise ValueError(
                    "source element count for %s differs: rows %d, source %d"
                    % (section, len(expected_with_children), len(source_elements))
                )
    return True


def _with_child_counts(elements):
    """Fill the child-count slot of normalized tuples from the depth sequence."""
    counts = [0] * len(elements)
    for index, element in enumerate(elements):
        depth = element[0]
        for following in elements[index + 1 :]:
            if following[0] <= depth:
                break
            if following[0] == depth + 1:
                counts[index] += 1
    return [element[:6] + (counts[index],) for index, element in enumerate(elements)]
