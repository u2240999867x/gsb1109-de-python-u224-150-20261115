#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
Relational export of XML instance data.

This module builds relational tables from the XSD identity constraints
(``xs:key``, ``xs:unique`` and ``xs:keyref``) declared in a schema. Every
constraint keeps its own *declaration scope*: the rows of a constraint are
collected independently within each element on which the constraint is
declared. This matters when a key is re-numbered inside sibling scopes (for
example line numbers restarted at 1 inside every order): a reference is only
resolved against the table of the same scope, so a dangling reference is never
silently satisfied by a row carrying the same displayed value in another
scope.

Keys are compared as *typed values*, not as raw strings. Each component of a
composite key carries the primitive XSD type of its field, so two fields that
render the same lexical string but belong to different type spaces (for
example ``xs:integer`` ``1`` and ``xs:string`` ``"1"``, or ``xs:boolean``
``true`` and ``xs:string`` ``"true"``) never match.
"""
import csv
import re
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

from xmlschema.aliases import BaseXsdType, SchemaType, XMLSourceType
from xmlschema.dataobjects import DataElement
from xmlschema.exceptions import XMLSchemaValueError
from xmlschema.utils.qnames import local_name
from xmlschema.validators.identities import XsdIdentity, XsdKey, XsdUnique, XsdKeyref

if TYPE_CHECKING:
    from xmlschema.validators import XMLSchemaBase
    from xmlschema.validators.simple_types import XsdSimpleType

__all__ = [
    'TypedKey', 'RelationalRow', 'RelationalTable', 'ReferenceFailure',
    'RelationalExport', 'RelationalReferenceError', 'export_relational_tables',
]


class RelationalReferenceError(XMLSchemaValueError):
    """Raised in strict mode when a keyref cannot be resolved within its scope."""


@dataclass(frozen=True)
class TypedKey:
    """
    An identity-constraint key made of typed components.

    :ivar parts: the decoded values of the key fields, in field order.
    :ivar types: the primitive XSD type name paired with each value, so that
        keys are compared by typed value rather than by lexical representation.
    """
    parts: tuple[Any, ...]
    types: tuple[Optional[str], ...]

    def display(self) -> str:
        """Return a readable representation showing value and type together."""
        chunks = []
        for value, type_name in zip(self.parts, self.types):
            if type_name is None:
                chunks.append(repr(value))
            else:
                chunks.append(f'{value!r}[{local_name(type_name)}]')
        return '(' + ', '.join(chunks) + ')'


@dataclass
class RelationalRow:
    """A row of a relational table."""
    key: TypedKey
    element: DataElement
    order: int


@dataclass
class RelationalTable:
    """
    The rows collected for an identity constraint inside a single scope.

    :ivar identity: the XSD identity constraint declaration.
    :ivar scope: the data element providing the declaration scope.
    :ivar scope_label: the positional path of the scope, used in messages and
        file names to tell apart sibling scopes with the same name.
    :ivar rows: the rows in document order.
    :ivar duplicates: rows rejected because their key was already present.
    """
    identity: XsdIdentity
    scope: DataElement
    scope_label: str
    rows: list[RelationalRow] = field(default_factory=list)
    duplicates: list[RelationalRow] = field(default_factory=list)
    _index: dict[TypedKey, RelationalRow] = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return self.identity.local_name or self.identity.__class__.__name__

    @property
    def kind(self) -> str:
        if isinstance(self.identity, XsdKeyref):
            return 'keyref'
        elif isinstance(self.identity, XsdKey):
            return 'key'
        return 'unique'

    @property
    def field_paths(self) -> list[str]:
        return [f.path for f in self.identity.fields]

    def add(self, row: RelationalRow) -> bool:
        """
        Add a row.

        For key/unique tables a duplicated key is rejected and parked in
        :attr:`duplicates` (the first occurrence stays canonical). Keyref
        tables keep every row because duplicate references are legal, while
        still recording the repetition in :attr:`duplicates`.
        """
        existing = self._index.get(row.key)
        if existing is not None:
            self.duplicates.append(row)
            if isinstance(self.identity, XsdKeyref):
                self.rows.append(row)
                return True
            return False
        self._index[row.key] = row
        self.rows.append(row)
        return True

    def get(self, key: TypedKey) -> Optional[RelationalRow]:
        return self._index.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._index

    def __len__(self) -> int:
        return len(self.rows)


@dataclass
class ReferenceFailure:
    """
    A keyref value without a matching key in its own declaration scope.

    :ivar constraint: the keyref constraint whose reference is dangling.
    :ivar refer: the key/unique constraint that should provide the target.
    :ivar scope: the scope element (for example the owning order) where the
        lookup failed.
    :ivar scope_label: positional path of the owning scope.
    :ivar source: the data element carrying the unresolved reference.
    :ivar key: the typed key that could not be resolved.
    """
    constraint: XsdKeyref
    refer: Union[XsdKey, XsdUnique]
    scope: DataElement
    scope_label: str
    source: DataElement
    key: TypedKey

    def __str__(self) -> str:
        refer_name = self.refer.local_name
        return (
            f"broken reference {self.constraint.local_name!r} -> {refer_name!r} "
            f"in {self.scope_label}: missing key {self.key.display()}"
        )


@dataclass
class RelationalExport:
    """Result of a relational export."""
    tables: list[RelationalTable]
    failures: list[ReferenceFailure]
    decode_errors: list[Any]
    root: DataElement

    def tables_for(self, identity: XsdIdentity) -> list[RelationalTable]:
        """Return every scope table built for the given constraint."""
        return [t for t in self.tables if t.identity is identity]

    def check_references(self) -> None:
        """Raise :class:`RelationalReferenceError` if any reference is dangling."""
        if self.failures:
            head = self.failures[0]
            msg = str(head)
            if len(self.failures) > 1:
                msg += f' (+{len(self.failures) - 1} more)'
            raise RelationalReferenceError(msg)

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        """
        Return the tables as plain dictionaries, keyed by a name that includes
        the scope label so sibling scopes never overwrite each other.
        """
        result: dict[str, list[dict[str, Any]]] = {}
        for table in self.tables:
            key_name = f'{table.name}@{table.scope_label}'
            columns = [_column_name(p) for p in table.field_paths]
            rows = []
            for row in table.rows:
                record = {col: value for col, value in zip(columns, row.key.parts)}
                record['_order'] = row.order
                rows.append(record)
            result[key_name] = rows
        return result

    def write_csv(self, target: Union[str, Path]) -> list[Path]:
        """
        Write one CSV file per scope table and return the written paths.

        Composite keys are expanded to one column per field; the owning scope
        label is repeated on every row to preserve the declaration scope.
        """
        target_path = Path(target)
        target_path.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        for index, table in enumerate(self.tables):
            columns = [_column_name(p) for p in table.field_paths]
            file_name = f'{index:03d}_{table.name}.csv'
            file_path = target_path.joinpath(file_name)
            with file_path.open('w', newline='', encoding='utf-8') as fp:
                writer = csv.writer(fp)
                writer.writerow(['_scope', *_as_header(columns)])
                for row in table.rows:
                    writer.writerow([table.scope_label, *_as_csv(row.key.parts)])
            written.append(file_path)
        return written


def _column_name(field_path: str) -> str:
    """Derive a column name from an identity field path (last step)."""
    steps = [segment for segment in re.split(r'[|/]', field_path)
             if segment and segment != '.']
    if not steps:
        return 'value'
    return local_name(steps[-1].lstrip('@'))


def _as_header(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    header: list[str] = []
    for name in columns:
        count_seen = seen.get(name, 0)
        seen[name] = count_seen + 1
        header.append(name if count_seen == 0 else f'{name}_{count_seen}')
    return header


def _as_csv(values: tuple[Any, ...]) -> list[Any]:
    return ['' if value is None else value for value in values]


def _resolve_qname(step: str, namespaces: Optional[dict[str, str]]) -> Optional[str]:
    """Resolve a possibly prefixed name step to an extended QName."""
    if ':' in step:
        prefix, local = step.split(':', 1)
        if namespaces is None or prefix not in namespaces:
            return None
        return f'{{{namespaces[prefix]}}}{local}'
    default_ns = namespaces.get('') if namespaces else None
    if default_ns:
        return f'{{{default_ns}}}{step}'
    return step


def _matches(element: DataElement, step: str,
             namespaces: Optional[dict[str, str]]) -> bool:
    if step == '*':
        return True
    expected = _resolve_qname(step, namespaces)
    if expected is None:
        return element.local_name == local_name(step)
    return element.tag == expected or element.local_name == local_name(expected)


def _parse_branch(path: str) -> tuple[bool, bool, list[str]]:
    """Split a restricted identity XPath branch into (self, deep, steps)."""
    text = path.strip()
    if text == '.':
        return True, False, []
    deep = text.startswith('.//')
    if deep:
        text = text[3:]
    elif text.startswith('./'):
        text = text[2:]
    steps = [segment for segment in text.split('/') if segment and segment != '.']
    return False, deep, steps


def _navigate_elements(context: DataElement, path: str,
                       namespaces: Optional[dict[str, str]]) -> list[DataElement]:
    """Select elements following a restricted identity selector/field path."""
    selected: list[DataElement] = []
    for branch in path.split('|'):
        is_self, deep, steps = _parse_branch(branch)
        if is_self:
            selected.append(context)
            continue
        if not steps:
            continue

        if deep:
            current = [
                descendant for descendant in context.iter()
                if isinstance(descendant, DataElement)
            ]
            first, *rest = steps
            current = [node for node in current if _matches(node, first, namespaces)]
        else:
            first, *rest = steps
            current = [node for node in context if _matches(node, first, namespaces)]

        for step in rest:
            next_current: list[DataElement] = []
            for node in current:
                next_current.extend(child for child in node if _matches(child, step, namespaces))
            current = next_current
        selected.extend(current)

    # De-duplicate while keeping document order (union branches may overlap).
    unique: list[DataElement] = []
    for node in selected:
        if not any(node is previous for previous in unique):
            unique.append(node)
    return unique


def _primitive_type_name(xsd_type: Optional[BaseXsdType]) -> Optional[str]:
    """Return the primitive XSD type name governing a field's value space."""
    if xsd_type is None:
        return None

    if xsd_type.is_simple():
        simple_type: Optional[XsdSimpleType] = xsd_type  # type: ignore[assignment]
    else:
        simple_type = getattr(xsd_type, 'simple_type', None)

    if simple_type is None:
        return None
    primitive = getattr(simple_type, 'primitive_type', None)
    if primitive is not None and primitive is not simple_type:
        return primitive.name
    return simple_type.name


def _attribute_type(element: DataElement, name: str) -> Optional[str]:
    xsd_type = element.xsd_type
    if xsd_type is None or not hasattr(xsd_type, 'attributes'):
        return None
    declaration = xsd_type.attributes.get(name)
    if declaration is None:
        declaration = xsd_type.attributes.get(local_name(name))
    if declaration is None or not hasattr(declaration, 'type'):
        return None
    return _primitive_type_name(declaration.type)


def _select_field(target: DataElement, field_path: str,
                  namespaces: Optional[dict[str, str]]) -> list[tuple[Any, Optional[str]]]:
    """Return the (typed value, primitive type name) pairs selected by a field."""
    values: list[tuple[Any, Optional[str]]] = []

    for branch in field_path.split('|'):
        is_self, _, steps = _parse_branch(branch)
        if is_self:
            values.append((target.value, _primitive_type_name(target.xsd_type)))
            continue
        if not steps:
            continue

        last = steps[-1]
        if last.startswith('@'):
            attr_name = last[1:]
            parents = _navigate_elements(
                target, '/'.join(steps[:-1]), namespaces
            ) if steps[:-1] else [target]
            for parent in parents:
                extended = _resolve_qname(attr_name, namespaces)
                key = extended if extended in parent.attrib else local_name(attr_name)
                if key in parent.attrib:
                    values.append((parent.attrib[key], _attribute_type(parent, key)))
            continue

        for node in _navigate_elements(target, branch, namespaces):
            values.append((node.value, _primitive_type_name(node.xsd_type)))

    return values


class RelationalExporter:
    """Walks a decoded data tree and materializes scoped identity tables."""

    def __init__(self, schema: 'XMLSchemaBase', source: Union[XMLSourceType, Any],
                 **kwargs: Any) -> None:
        self.schema = schema
        # Lax decoding: a dangling keyref must not abort the relational export,
        # because reporting it is precisely one of the export's duties.
        decoded = schema.to_objects(source, validation='lax', **kwargs)
        if isinstance(decoded, tuple):
            root, decode_errors = decoded
        else:  # pragma: no cover
            root, decode_errors = decoded, []
        if not isinstance(root, DataElement):
            msg = "the XML source cannot be decoded to a data object tree"
            raise XMLSchemaValueError(msg)
        self.root: DataElement = root
        self.decode_errors: list[Any] = list(decode_errors)
        self.tables: list[RelationalTable] = []
        self.failures: list[ReferenceFailure] = []
        self._table_index: dict[tuple[int, int], RelationalTable] = {}
        self._parent_map = self._build_parent_map(self.root)
        self._row_counter = count()

    @staticmethod
    def _build_parent_map(root: DataElement) -> dict[int, DataElement]:
        parent_map: dict[int, DataElement] = {}
        for parent in root.iter():
            for child in parent:
                if isinstance(child, DataElement):
                    parent_map[id(child)] = parent
        return parent_map

    def _scope_label(self, element: DataElement) -> str:
        parts: list[str] = []
        current: Optional[DataElement] = element
        while current is not None:
            parent = self._parent_map.get(id(current))
            if parent is None:
                parts.append(current.local_name or 'root')
                break
            siblings = [child for child in parent if child.local_name == current.local_name]
            position = next(
                (index for index, child in enumerate(siblings) if child is current), 0
            ) + 1
            parts.append(f'{current.local_name}[{position}]')
            current = parent
        return '/' + '/'.join(reversed(parts))

    def _get_table(self, identity: XsdIdentity, scope: DataElement) -> RelationalTable:
        key = (id(identity), id(scope))
        table = self._table_index.get(key)
        if table is None:
            table = RelationalTable(
                identity=identity,
                scope=scope,
                scope_label=self._scope_label(scope),
            )
            self._table_index[key] = table
            self.tables.append(table)
        return table

    def _build_key(self, identity: XsdIdentity,
                   target: DataElement) -> Optional[TypedKey]:
        parts: list[Any] = []
        types: list[Optional[str]] = []
        for field_selector in identity.fields:
            matches = _select_field(target, field_selector.path, target.nsmap)
            if len(matches) > 1:
                msg = f"field {field_selector.path!r} selects multiple values for {identity!r}"
                raise XMLSchemaValueError(msg)
            if not matches:
                parts.append(None)
                types.append(None)
            else:
                value, type_name = matches[0]
                parts.append(value)
                types.append(type_name)

        if all(part is None for part in parts):
            return None
        return TypedKey(tuple(parts), tuple(types))

    def _process_identity(self, identity: XsdIdentity, scope: DataElement) -> None:
        if identity.selector is None or not identity.fields:
            return

        table = self._get_table(identity, scope)
        targets = _navigate_elements(scope, identity.selector.path, scope.nsmap)
        for target in targets:
            typed_key = self._build_key(identity, target)
            if typed_key is None:
                # An all-nil keyref field set is ignored by XSD identity rules.
                continue
            table.add(RelationalRow(
                key=typed_key,
                element=target,
                order=next(self._row_counter),
            ))

    def _find_referenced_table(self, refer: Union[XsdKey, XsdUnique],
                               scope: DataElement) -> Optional[RelationalTable]:
        # Same scope first: the ordinary order/line arrangement declares both
        # the key and the keyref on the same element.
        table = self._table_index.get((id(refer), id(scope)))
        if table is not None:
            return table

        # Otherwise walk the ancestor chain only. Sibling scopes are never
        # consulted, so a missing key cannot be borrowed from another order.
        current: Optional[DataElement] = self._parent_map.get(id(scope))
        while current is not None:
            table = self._table_index.get((id(refer), id(current)))
            if table is not None:
                return table
            current = self._parent_map.get(id(current))
        return None

    def _resolve_references(self) -> None:
        for table in self.tables:
            identity = table.identity
            if not isinstance(identity, XsdKeyref):
                continue
            refer = identity.refer
            if not isinstance(refer, (XsdKey, XsdUnique)):
                continue

            referenced = self._find_referenced_table(refer, table.scope)
            for row in table.rows:
                if referenced is not None and row.key in referenced:
                    continue
                self.failures.append(ReferenceFailure(
                    constraint=identity,
                    refer=refer,
                    scope=table.scope,
                    scope_label=table.scope_label,
                    source=row.element,
                    key=row.key,
                ))

    def run(self) -> RelationalExport:
        for element in self.root.iter():
            if not isinstance(element, DataElement):
                continue
            xsd_element = element.xsd_element
            if xsd_element is None:
                continue
            for identity in xsd_element.identities:
                effective = identity.ref if identity.ref is not None else identity
                if isinstance(effective, XsdIdentity):
                    self._process_identity(effective, element)

        self._resolve_references()
        return RelationalExport(
            tables=self.tables,
            failures=self.failures,
            decode_errors=self.decode_errors,
            root=self.root,
        )


def export_relational_tables(schema: SchemaType,
                             source: Union[XMLSourceType, Any],
                             *,
                             strict: bool = False,
                             **kwargs: Any) -> RelationalExport:
    """
    Export XML instance data to relational tables based on the XSD identity
    constraints declared in the schema.

    :param schema: an XSD schema instance providing the identity constraints.
    :param source: the XML data (path, string, file-like or parsed element).
    :param strict: if ``True`` raise :class:`RelationalReferenceError` on the
        first unresolved reference; otherwise failures are collected in the
        returned :class:`RelationalExport`.
    :param kwargs: extra keyword arguments forwarded to the schema's
        ``to_objects`` decoding call (for example ``namespaces``).
    :return: a :class:`RelationalExport` holding one table per constraint and
        declaration scope, plus any unresolved references.
    """
    exporter = RelationalExporter(schema, source, **kwargs)
    result = exporter.run()
    if strict:
        result.check_references()
    return result
