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
Revalidation impact analysis for schema evolution.

When a schema vendor publishes a new version of an XSD, historical records
that were decoded with the old version don't all need to be read again:

- a record that used a declaration or type whose semantics changed *may* be
  affected and has to be revalidated;
- a record that used only unchanged components can keep its old decoding;
- a record with incomplete evidence (a wildcard, an unresolved ``xsi:type``
  or an unknown dynamic type) cannot be declared safe.

The comparison is semantic. Annotations are ignored and the effective facet
set of a type includes inherited facets, so tightening a base type is
detected also for its derived types.

The module supports two workflows:

1. the caller provides, for each historical record, the declarations and the
   actual types used by the old decoding (:class:`DeclarationUse`);
2. the caller decodes again with a :class:`DecodeUsageCollector` used as
   *element_hook*, which records the same information automatically.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import xmlschema.names as nm
from xmlschema.aliases import BaseXsdType, SchemaType
from xmlschema.exceptions import XMLSchemaTypeError, XMLSchemaValueError
from xmlschema.translation import gettext as _
from xmlschema.utils.qnames import local_name

from xmlschema.validators.attributes import XsdAttribute
from xmlschema.validators.elements import XsdElement
from xmlschema.validators.groups import XsdGroup
from xmlschema.validators.simple_types import XsdSimpleType
from xmlschema.validators.wildcards import XsdAnyAttribute, XsdAnyElement

__all__ = [
    'REVALIDATE', 'REUSE', 'MISSING_EVIDENCE', 'NEEDS_REVALIDATION',
    'CAN_REUSE', 'UNKNOWN',
    'DeclarationUse', 'UsedDeclaration', 'DeclarationUsage',
    'RecordEvidence', 'DecodeEvidence', 'RecordDecision', 'SchemaChange',
    'RevalidationReport', 'RevalidationResult', 'SchemaEvolution',
    'DecodeUsageCollector', 'analyze_schema_change', 'classify_records',
    'revalidation_impact',
]


REVALIDATE = 'revalidate'
"""The old decoding may be affected: decode the record again with the new schema."""

REUSE = 'reuse'
"""The old decoding is provably unaffected by the schema changes."""

MISSING_EVIDENCE = 'missing_evidence'
"""The old decoding didn't record enough information to decide safely."""

NEEDS_REVALIDATION = REVALIDATE
CAN_REUSE = REUSE
UNKNOWN = MISSING_EVIDENCE


_XSD_NS = nm.XSD_NAMESPACE
_ANY_TYPE = nm.XSD_ANY_TYPE
_ANY_SIMPLE_TYPE = nm.XSD_ANY_SIMPLE_TYPE
_ANY_ATOMIC_TYPE = nm.XSD_ANY_ATOMIC_TYPE
_UNKNOWN_TYPES = frozenset((_ANY_TYPE, _ANY_SIMPLE_TYPE, _ANY_ATOMIC_TYPE))


@dataclass(frozen=True)
class DeclarationUse:
    """
    A declaration and the type that were actually used for decoding a record.

    :param element: expanded name of the element declaration.
    :param type: expanded name of the type actually used. For an anonymous \
    type leave it to `None` and provide *anonymous*.
    :param schema_path: path of the declaration in the schema. For local \
    declarations use the vendor source path (annotations skipped), which is \
    stable across schema versions.
    :param anonymous: a stable identity for an anonymous type, so that the \
    same declaration can be matched between the two schema versions.
    :param dynamic: `True` if the actual type was dynamic and hasn't been \
    recorded. The record is then classified as missing evidence.
    """
    element: Optional[str] = None
    type: Optional[str] = None
    schema_path: Optional[str] = None
    anonymous: Optional[str] = None
    dynamic: bool = False

    def __post_init__(self) -> None:
        if self.element is None and self.schema_path is None:
            raise XMLSchemaValueError(
                _("a declaration use requires an element name or a schema path")
            )

    @property
    def used_type(self) -> Optional[str]:
        return self.type or self.anonymous

    def __str__(self) -> str:
        if self.schema_path:
            return f'{self.element or self.schema_path} ({self.schema_path})'
        return self.element or self.schema_path or '?'


UsedDeclaration = DeclarationUse
DeclarationUsage = DeclarationUse


@dataclass(frozen=True)
class RecordEvidence:
    """
    Evidence about the old decoding of one historical record.

    :param declarations: the declarations and actual types used by the record \
    and by its descendants.
    :param complete: `False` if the record was decoded through a wildcard, a \
    ``processContents="skip"`` declaration or an unresolved dynamic type.
    :param reference: an identifier of the record, used in the report.
    """
    declarations: tuple[DeclarationUse, ...] = ()
    complete: bool = True
    reference: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'declarations', tuple(self.declarations))

    def __bool__(self) -> bool:
        return bool(self.declarations)


DecodeEvidence = RecordEvidence


@dataclass(frozen=True)
class RecordDecision:
    """The classification of a single record, with the reasons for it."""
    reference: Any
    status: str
    reasons: tuple[str, ...] = ()
    evidence: Optional[RecordEvidence] = None

    @property
    def revalidate(self) -> bool:
        return self.status == REVALIDATE

    @property
    def reuse(self) -> bool:
        return self.status == REUSE

    @property
    def missing_evidence(self) -> bool:
        return self.status == MISSING_EVIDENCE

    def as_dict(self) -> dict[str, Any]:
        """Returns the decision in a JSON-friendly form."""
        return {
            'reference': self.reference,
            'status': self.status,
            'reasons': list(self.reasons),
        }


@dataclass(frozen=True)
class SchemaChange:
    """A semantic change to a schema component."""
    kind: str
    name: str
    display_name: str
    affects: bool = True
    reason: str = 'changed'
    paths: tuple[str, ...] = ()
    derived_types: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    @property
    def removed(self) -> bool:
        return self.reason == 'removed'

    def as_dict(self) -> dict[str, Any]:
        """Returns the change in a JSON-friendly form."""
        return {
            'kind': self.kind,
            'name': self.name,
            'display_name': self.display_name,
            'affects': self.affects,
            'reason': self.reason,
            'paths': list(self.paths),
            'derived_types': list(self.derived_types),
            'details': list(self.details),
        }

    def __str__(self) -> str:
        if self.reason == 'removed':
            return _("%(kind)s %(name)s was removed") % {
                'kind': self.kind, 'name': self.display_name
            }
        elif self.derived_types:
            return _("%(kind)s %(name)s changed (%(paths)s), affecting derived "
                     "types %(derived)s") % {
                'kind': self.kind,
                'name': self.display_name,
                'paths': ', '.join(self.paths) or 'semantic change',
                'derived': ', '.join(self.derived_types),
            }
        else:
            return _("%(kind)s %(name)s changed (%(paths)s)") % {
                'kind': self.kind,
                'name': self.display_name,
                'paths': ', '.join(self.paths) or 'semantic change',
            }


class RevalidationReport:
    """
    The result of a revalidation impact analysis.

    :ivar revalidate: references of records that may be affected.
    :ivar reuse: references of records whose old decoding remains valid.
    :ivar missing_evidence: references of records that can't be classified.
    :ivar decisions: the :class:`RecordDecision` for every input record.
    :ivar changes: the semantic schema changes that were found.
    """

    def __init__(self,
                 old_schema: SchemaType,
                 new_schema: SchemaType,
                 changes: Sequence[SchemaChange],
                 decisions: Sequence[RecordDecision],
                 ignored_annotations: bool = False) -> None:
        self.old_schema = old_schema
        self.new_schema = new_schema
        self.changes = list(changes)
        self.decisions = list(decisions)
        self.ignored_annotations = ignored_annotations

        self.revalidate: list[Any] = []
        self.reuse: list[Any] = []
        self.missing_evidence: list[Any] = []
        self._by_reference: dict[Any, RecordDecision] = {}

        for decision in self.decisions:
            getattr(self, decision.status).append(decision.reference)
            self._by_reference[decision.reference] = decision

    def __len__(self) -> int:
        return len(self.decisions)

    def __iter__(self) -> Iterator[Any]:
        yield from self.revalidate
        yield from self.reuse
        yield from self.missing_evidence

    def __contains__(self, reference: Any) -> bool:
        return reference in self._by_reference

    def __getitem__(self, reference: Any) -> RecordDecision:
        return self._by_reference[reference]

    def get(self, reference: Any, default: Any = None) -> Any:
        return self._by_reference.get(reference, default)

    @property
    def affected(self) -> list[Any]:
        """Alias of :attr:`revalidate`."""
        return self.revalidate

    @property
    def reusable(self) -> list[Any]:
        """Alias of :attr:`reuse`."""
        return self.reuse

    @property
    def unsafe(self) -> list[Any]:
        """Alias of :attr:`missing_evidence`."""
        return self.missing_evidence

    @property
    def total(self) -> int:
        return len(self.decisions)

    def reasons_for(self, reference: Any) -> list[str]:
        """Returns the reasons for the classification of a record."""
        return list(self._by_reference[reference].reasons)

    def as_dict(self) -> dict[str, list[Any]]:
        """Returns the three buckets as a plain dictionary."""
        return {
            REVALIDATE: list(self.revalidate),
            REUSE: list(self.reuse),
            MISSING_EVIDENCE: list(self.missing_evidence),
        }

    def to_dict(self) -> dict[str, Any]:
        """Returns the whole report in a JSON-friendly form."""
        return {
            'summary': {
                'total': self.total,
                'revalidate': len(self.revalidate),
                'reuse': len(self.reuse),
                'missing_evidence': len(self.missing_evidence),
            },
            'ignored_annotations': self.ignored_annotations,
            'changes': [change.as_dict() for change in self.changes],
            'records': [decision.as_dict() for decision in self.decisions],
            'revalidate': list(self.revalidate),
            'reuse': list(self.reuse),
            'missing_evidence': list(self.missing_evidence),
        }

    def summary(self) -> str:
        return _(
            "%(total)d record(s): %(revalidate)d to revalidate, "
            "%(reuse)d reusable, %(missing)d missing evidence"
        ) % {
            'total': len(self.decisions),
            'revalidate': len(self.revalidate),
            'reuse': len(self.reuse),
            'missing': len(self.missing_evidence),
        }

    def __str__(self) -> str:
        return self.summary()


RevalidationResult = RevalidationReport


class _Diff:
    """An internal semantic difference between two canonical values."""

    __slots__ = ('path', 'old', 'new', 'affects', 'detail')

    def __init__(self, path: str, old: Any, new: Any,
                 affects: bool = True, detail: str = '') -> None:
        self.path = path
        self.old = old
        self.new = new
        self.affects = affects
        self.detail = detail

    def __repr__(self) -> str:
        return f'_Diff({self.path!r}, affects={self.affects!r})'


def _resolve_qname(name: Optional[str], schema: SchemaType) -> Optional[str]:
    """Resolves a local, prefixed or expanded QName using the schema namespaces."""
    if not name:
        return None
    elif name.startswith('{'):
        return name
    elif ':' in name:
        prefix, local = name.split(':', 1)
        try:
            namespace = schema.namespaces[prefix]
        except KeyError:
            return name
        return f'{{{namespace}}}{local}'
    elif f'{{{schema.target_namespace}}}{name}' in schema.maps.types:
        return f'{{{schema.target_namespace}}}{name}'
    else:
        return name


def _expanded_name(name: Optional[str], schema: SchemaType) -> Optional[str]:
    resolved = _resolve_qname(name, schema)
    return resolved if resolved and resolved.startswith('{') else name


def _display_name(name: Optional[str], namespaces: Mapping[str, str]) -> str:
    if not name:
        return '(anonymous)'
    elif not name.startswith('{'):
        return name

    namespace, _, local = name[1:].partition('}')
    for prefix, uri in namespaces.items():
        if uri == namespace and prefix:
            return f'{prefix}:{local}'
    if not namespace:
        return local
    return name


def _is_builtin(xsd_type: BaseXsdType) -> bool:
    name = getattr(xsd_type, 'name', None)
    return bool(name and name.startswith(f'{{{_XSD_NS}}}'))


def _iter_base_chain(xsd_type: BaseXsdType) -> Iterator[BaseXsdType]:
    seen = set()
    current: Optional[BaseXsdType] = xsd_type
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if _is_builtin(current):
            return
        current = getattr(current, 'base_type', None)


def _effective_facets(xsd_type: BaseXsdType) -> dict[str, tuple[str, ...]]:
    """Returns the effective facets, inherited facets included."""
    facets: dict[str, tuple[str, ...]] = {}
    if not getattr(xsd_type, 'is_simple', lambda: False)():
        simple_type = getattr(xsd_type, 'simple_type', None)
        if simple_type is None:
            return facets
        xsd_type = simple_type

    get_facet = getattr(xsd_type, 'get_facet', lambda tag: None)
    for tag in (nm.XSD_WHITE_SPACE, nm.XSD_LENGTH, nm.XSD_MIN_LENGTH, nm.XSD_MAX_LENGTH,
                nm.XSD_MIN_INCLUSIVE, nm.XSD_MAX_INCLUSIVE, nm.XSD_MIN_EXCLUSIVE,
                nm.XSD_MAX_EXCLUSIVE, nm.XSD_TOTAL_DIGITS, nm.XSD_FRACTION_DIGITS,
                nm.XSD_EXPLICIT_TIMEZONE):
        facet = get_facet(tag)
        if facet is not None:
            facets[local_name(tag)] = (str(getattr(facet, 'value', '')),)

    patterns = get_facet(nm.XSD_PATTERN)
    if patterns is not None:
        facets['pattern'] = tuple(sorted(getattr(patterns, 'regexps', ())))

    enumeration = get_facet(nm.XSD_ENUMERATION)
    if enumeration is not None:
        facets['enumeration'] = tuple(sorted(
            repr(value) for value in getattr(enumeration, 'enumeration', ())
        ))

    assertions = []
    for facet in getattr(xsd_type, 'facets', {}).values():
        items = facet if isinstance(facet, list) else [facet]
        for item in items:
            if getattr(item, 'elem', None) is not None and \
                    item.elem.tag == nm.XSD_ASSERTION:
                assertions.append(getattr(item, 'path', item.elem.get('test', '')))
    if assertions:
        facets['assertion'] = tuple(sorted(assertions))

    return facets


def _simple_type_snapshot(xsd_type: BaseXsdType, stack: set[int]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        'variety': getattr(xsd_type, 'variety', None),
        'root_type': getattr(getattr(xsd_type, 'root_type', None), 'name', None),
        'facets': _effective_facets(xsd_type),
    }

    if snapshot['variety'] == 'list':
        item_type = None
        for candidate in _iter_base_chain(xsd_type):
            if hasattr(candidate, 'item_type'):
                item_type = candidate.item_type
                break
        snapshot['item_type'] = getattr(item_type, 'name', None)
    elif snapshot['variety'] == 'union':
        member_types = ()
        for candidate in _iter_base_chain(xsd_type):
            if hasattr(candidate, 'member_types'):
                member_types = tuple(candidate.member_types)
                break
        # Keep the declaration order: it determines which member type decodes
        # a value first when more than one member accepts it.
        snapshot['member_types'] = tuple(
            getattr(member, 'name', None) or repr(member) for member in member_types
        )

    base_type = getattr(xsd_type, 'base_type', None)
    snapshot['base_type'] = getattr(base_type, 'name', None)
    return snapshot


def _type_snapshot(xsd_type: Optional[BaseXsdType], stack: Optional[set[int]] = None
                   ) -> Optional[dict[str, Any]]:
    if xsd_type is None:
        return None
    if stack is None:
        stack = set()

    name = getattr(xsd_type, 'name', None)
    if id(xsd_type) in stack:
        return {'ref': name or '(anonymous)'}
    stack.add(id(xsd_type))
    try:
        if getattr(xsd_type, 'is_simple', lambda: False)():
            return _simple_type_snapshot(xsd_type, stack)

        from xmlschema.validators.complex_types import XsdComplexType
        if not isinstance(xsd_type, XsdComplexType):
            return _simple_type_snapshot(xsd_type, stack)

        simple_content = getattr(xsd_type, 'simple_type', None)
        snapshot: dict[str, Any] = {
            'kind': 'complexType',
            'base_type': getattr(getattr(xsd_type, 'base_type', None), 'name', None),
            'derivation': getattr(xsd_type, 'derivation', None),
            'mixed': xsd_type.mixed,
            'abstract': xsd_type.abstract,
            'content_type': xsd_type.content_type_label,
            'sequence_type': getattr(xsd_type, 'sequence_type', None),
            'simple_content': _simple_type_snapshot(simple_content, stack)
            if simple_content is not None else None,
            'model': _particle_snapshot(getattr(xsd_type, 'model_group', None), stack),
            'attributes': _attributes_snapshot(xsd_type.attributes, stack),
            'assertions': tuple(sorted(
                assertion.path for assertion in getattr(xsd_type, 'assertions', ())
            )),
            'open_content': None,
        }

        open_content = getattr(xsd_type, 'open_content', None)
        if open_content is not None:
            any_element = getattr(open_content, 'any_element', None)
            snapshot['open_content'] = {
                'mode': getattr(open_content, 'mode', None),
                'namespace': tuple(sorted(getattr(any_element, 'namespace', ()))),
                'not_namespace': tuple(sorted(
                    getattr(any_element, 'not_namespace', ())
                )),
                'process_contents': getattr(any_element, 'process_contents', None),
            }
        return snapshot
    finally:
        stack.discard(id(xsd_type))


def _element_snapshot(xsd_element: XsdElement, stack: Optional[set[int]] = None
                      ) -> dict[str, Any]:
    if stack is None:
        stack = set()

    xsd_type = xsd_element.type
    snapshot: dict[str, Any] = {
        'kind': 'element',
        'name': xsd_element.name,
        'type': getattr(xsd_type, 'name', None),
        'type_snapshot': None if getattr(xsd_type, 'name', None) is not None
        else _type_snapshot(xsd_type, stack),
        'min_occurs': xsd_element.min_occurs,
        'max_occurs': xsd_element.max_occurs,
        'nillable': xsd_element.nillable,
        'abstract': xsd_element.abstract,
        'default': xsd_element.default,
        'fixed': xsd_element.fixed,
        'substitution_group': xsd_element.substitution_group,
        'alternatives': tuple(
            (alternative.path, getattr(alternative.type, 'name', None))
            for alternative in getattr(xsd_element, 'alternatives', ())
        ),
        'identities': tuple(
            (
                getattr(identity, 'name', None),
                getattr(getattr(identity, 'selector', None), 'path', None),
                tuple(getattr(field, 'path', None) for field in
                      getattr(identity, 'fields', ())),
            )
            for identity in getattr(xsd_element, 'identities', ())
        ),
    }
    return snapshot


def _attribute_snapshot(xsd_attribute: Any, stack: set[int]) -> dict[str, Any]:
    xsd_type = getattr(xsd_attribute, 'type', None)
    return {
        'kind': 'attribute',
        'name': getattr(xsd_attribute, 'name', None),
        'use': getattr(xsd_attribute, 'use', 'optional'),
        'default': getattr(xsd_attribute, 'default', None),
        'fixed': getattr(xsd_attribute, 'fixed', None),
        'inheritable': getattr(xsd_attribute, 'inheritable', False),
        'type': getattr(xsd_type, 'name', None),
        'type_snapshot': None if getattr(xsd_type, 'name', None) is not None
        else _type_snapshot(xsd_type, stack),
    }


def _attributes_snapshot(attribute_group: Any, stack: set[int]) -> tuple[dict[str, Any], ...]:
    if attribute_group is None:
        return ()

    items = []
    for attribute in attribute_group.values():
        if isinstance(attribute, XsdAnyAttribute):
            items.append({
                'kind': 'anyAttribute',
                'name': None,
                'namespace': tuple(sorted(attribute.namespace)),
                'not_namespace': tuple(sorted(attribute.not_namespace)),
                'process_contents': attribute.process_contents,
            })
        else:
            items.append(_attribute_snapshot(attribute, stack))

    return tuple(sorted(items, key=lambda x: (x['kind'], x.get('name') or '')))


def _particle_snapshot(particle: Any, stack: set[int]) -> Optional[dict[str, Any]]:
    if particle is None:
        return None
    elif isinstance(particle, XsdGroup):
        content = getattr(particle, 'content', None)
        if content is None:
            content = particle
        return {
            'kind': 'group',
            'model': particle.model,
            'min_occurs': particle.min_occurs,
            'max_occurs': particle.max_occurs,
            'content': tuple(
                item for item in (
                    _particle_snapshot(child, stack) for child in content
                    if getattr(child, 'min_occurs', 1) != 0
                ) if item is not None
            ),
        }
    elif isinstance(particle, XsdAnyElement):
        return {
            'kind': 'any',
            'name': None,
            'namespace': tuple(sorted(particle.namespace)),
            'not_namespace': tuple(sorted(particle.not_namespace)),
            'process_contents': particle.process_contents,
            'min_occurs': particle.min_occurs,
            'max_occurs': particle.max_occurs,
        }
    elif isinstance(particle, XsdElement):
        return _element_snapshot(particle, stack)
    else:
        return {'kind': type(particle).__name__, 'repr': repr(particle)}


def _canonical_component(component: Any) -> dict[str, Any]:
    if isinstance(component, XsdElement):
        return _element_snapshot(component)
    elif isinstance(component, XsdAttribute):
        return _attribute_snapshot(component, set())
    elif getattr(component, 'is_complex', lambda: False)():
        return _type_snapshot(component) or {}
    elif getattr(component, 'is_simple', lambda: False)():
        return _simple_type_snapshot(component, set())
    elif isinstance(component, XsdGroup):
        return _particle_snapshot(component, set()) or {}
    elif hasattr(component, 'values') and hasattr(component, 'items'):
        return {'attributes': _attributes_snapshot(component, set())}
    elif hasattr(component, 'type'):
        return _element_snapshot(component)
    else:
        return {'repr': repr(component)}


def _compare_union_members(old: Sequence[str], new: Sequence[str],
                           path: str) -> list[_Diff]:
    """
    Compares union member lists.

    Adding a member widens the value space, but it can also change which member
    decodes a value first, so the precedence is part of the comparison. Removing
    a member narrows the value space and always requires revalidation.
    """
    if list(old) == list(new):
        return []

    old_set, new_set = set(old), set(new)
    if old_set.issubset(new_set):
        if old == tuple(item for item in new if item in old_set):
            return [_Diff(path, old, new, affects=False, detail='member added')]
        return [_Diff(path, old, new, affects=True, detail='member precedence changed')]
    elif new_set.issubset(old_set):
        return [_Diff(path, old, new, affects=True, detail='member removed')]

    return [_Diff(path, old, new, affects=True, detail='members changed')]


def _compare_facets(old: Mapping[str, tuple[str, ...]],
                    new: Mapping[str, tuple[str, ...]], path: str) -> list[_Diff]:
    differences = []
    for name in sorted(set(old) | set(new)):
        old_value = old.get(name)
        new_value = new.get(name)
        if old_value == new_value:
            continue

        child_path = f'{path}.{name}'
        if old_value is None:
            # A new facet can only restrict the value space.
            if name in ('minLength', 'maxLength', 'length', 'minInclusive',
                        'minExclusive', 'maxInclusive', 'maxExclusive',
                        'totalDigits', 'fractionDigits', 'enumeration',
                        'pattern', 'assertion'):
                continues = name in ('minLength',) and new_value == ('0',)
                differences.append(_Diff(child_path, old_value, new_value,
                                         affects=not continues, detail='narrowed'))
            else:
                differences.append(_Diff(child_path, old_value, new_value,
                                         affects=True, detail='facet added'))
        elif new_value is None:
            # Removing a facet widens the value space.
            differences.append(_Diff(child_path, old_value, new_value,
                                     affects=False, detail='widened'))
        else:
            differences.append(_facet_difference(name, old_value, new_value, child_path))
    return differences


def _facet_difference(name: str, old_value: tuple[str, ...],
                      new_value: tuple[str, ...], path: str) -> _Diff:
    old = old_value[0] if len(old_value) == 1 else old_value
    new = new_value[0] if len(new_value) == 1 else new_value

    if name in ('minLength', 'minInclusive', 'minExclusive'):
        try:
            affects = float(new) > float(old)
        except (TypeError, ValueError):
            affects = True
        return _Diff(path, old, new, affects=affects,
                     detail='tightened' if affects else 'widened')
    elif name in ('maxLength', 'maxInclusive', 'maxExclusive'):
        try:
            affects = float(new) < float(old)
        except (TypeError, ValueError):
            affects = True
        return _Diff(path, old, new, affects=affects,
                     detail='tightened' if affects else 'widened')
    elif name in ('totalDigits', 'fractionDigits'):
        try:
            affects = int(new) < int(old)
        except (TypeError, ValueError):
            affects = True
        return _Diff(path, old, new, affects=affects,
                     detail='tightened' if affects else 'widened')
    elif name == 'enumeration':
        old_set, new_set = set(old_value), set(new_value)
        if old_set.issuperset(new_set):
            return _Diff(path, old, new, affects=True, detail='tightened')
        elif old_set.issubset(new_set):
            return _Diff(path, old, new, affects=False, detail='widened')
        return _Diff(path, old, new, affects=True, detail='changed')
    elif name == 'pattern':
        if set(old_value).issubset(new_value):
            return _Diff(path, old, new, affects=False, detail='widened')
        return _Diff(path, old, new, affects=True, detail='changed')
    elif name == 'assertion':
        if set(old_value).issubset(new_value):
            return _Diff(path, old, new, affects=False, detail='widened')
        return _Diff(path, old, new, affects=True, detail='tightened')
    else:
        return _Diff(path, old, new, affects=True, detail='changed')


def _compare_attributes(old: Sequence[Mapping[str, Any]],
                        new: Sequence[Mapping[str, Any]], path: str) -> list[_Diff]:
    old_map = {(item['kind'], item.get('name')): item for item in old}
    new_map = {(item['kind'], item.get('name')): item for item in new}
    differences: list[_Diff] = []

    for key in sorted(set(old_map) | set(new_map), key=lambda x: (x[0], x[1] or '')):
        old_item = old_map.get(key)
        new_item = new_map.get(key)
        item_path = f'{path}.{key[1] or key[0]}'

        if old_item is None:
            if key[0] == 'attribute' and new_item.get('use') == 'required':
                differences.append(_Diff(item_path, None, new_item,
                                         affects=True, detail='required attribute added'))
            elif key[0] == 'anyAttribute':
                differences.append(_Diff(item_path, None, new_item,
                                         affects=False, detail='wildcard added'))
            else:
                differences.append(_Diff(item_path, None, new_item,
                                         affects=False, detail='optional item added'))
            continue
        elif new_item is None:
            differences.append(_Diff(item_path, old_item, None,
                                     affects=True, detail='removed'))
            continue

        differences.extend(_compare(old_item, new_item, item_path))
    return differences


def _compare_model(old: Optional[Mapping[str, Any]],
                   new: Optional[Mapping[str, Any]], path: str) -> list[_Diff]:
    if old is None and new is None:
        return []
    elif old is None:
        return [_Diff(path, old, new, affects=True, detail='model added')]
    elif new is None:
        return [_Diff(path, old, new, affects=True, detail='model removed')]

    differences: list[_Diff] = []
    if old.get('kind') != new.get('kind') or old.get('model') != new.get('model'):
        differences.append(_Diff(f'{path}.model', old.get('model'), new.get('model'),
                                 affects=True, detail='model changed'))
        return differences

    differences.extend(_compare_occurs(old, new, path))
    old_content = old.get('content') or ()
    new_content = new.get('content') or ()

    old_names = [item.get('name') for item in old_content]
    new_names = [item.get('name') for item in new_content]

    if old_names == new_names and len(old_names) == len(new_names):
        for index, (old_item, new_item) in enumerate(zip(old_content, new_content)):
            differences.extend(_compare(old_item, new_item, f'{path}.{index}'))
        return differences

    if old.get('model') == 'choice':
        old_keys = {(item.get('kind'), item.get('name')) for item in old_content}
        new_keys = {(item.get('kind'), item.get('name')) for item in new_content}
        if old_keys.issubset(new_keys):
            differences.append(_Diff(f'{path}.content', old_names, new_names,
                                     affects=False, detail='choice widened'))
        else:
            differences.append(_Diff(f'{path}.content', old_names, new_names,
                                     affects=True, detail='choice narrowed'))
    elif old.get('model') == 'all':
        differences.extend(_compare_all_content(old_content, new_content, path))
    else:
        differences.extend(_compare_sequence_content(old_content, new_content, path))
    return differences


def _particle_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    if item.get('kind') == 'any':
        return ('any', item.get('namespace'), item.get('not_namespace'),
                item.get('process_contents'))
    return (item.get('kind'), item.get('name'))


def _is_subsequence(short: Sequence[Any], long: Sequence[Any]) -> bool:
    iterator = iter(long)
    return all(item in iterator for item in short)


def _compare_sequence_content(old_content: Sequence[Mapping[str, Any]],
                              new_content: Sequence[Mapping[str, Any]],
                              path: str) -> list[_Diff]:
    """
    Compares sequence contents preserving order.

    Adding an optional particle widens the accepted language, so old records
    remain valid. Adding a required particle (or removing one) tightens it.
    """
    old_keys = [_particle_key(item) for item in old_content]
    new_keys = [_particle_key(item) for item in new_content]

    if _is_subsequence(old_keys, new_keys):
        added = [item for item in new_content
                 if _particle_key(item) not in set(old_keys)]
        if not added:
            return []
        elif any(item.get('min_occurs', 1) for item in added):
            return [_Diff(f'{path}.content', old_keys, new_keys,
                          affects=True, detail='required particle added')]
        return [_Diff(f'{path}.content', old_keys, new_keys,
                      affects=False, detail='optional particle added')]

    if _is_subsequence(new_keys, old_keys):
        return [_Diff(f'{path}.content', old_keys, new_keys,
                      affects=True, detail='particle removed')]

    return [_Diff(f'{path}.content', old_keys, new_keys,
                  affects=True, detail='content changed')]


def _compare_all_content(old_content: Sequence[Mapping[str, Any]],
                         new_content: Sequence[Mapping[str, Any]],
                         path: str) -> list[_Diff]:
    old_map = {_particle_key(item): item for item in old_content}
    new_map = {_particle_key(item): item for item in new_content}

    if set(old_map).issubset(new_map):
        added = [new_map[key] for key in set(new_map) - set(old_map)]
        if not added:
            return []
        elif any(item.get('min_occurs', 1) for item in added):
            return [_Diff(f'{path}.content', tuple(old_map), tuple(new_map),
                          affects=True, detail='required particle added')]
        return [_Diff(f'{path}.content', tuple(old_map), tuple(new_map),
                      affects=False, detail='optional particle added')]

    if set(new_map).issubset(old_map):
        return [_Diff(f'{path}.content', tuple(old_map), tuple(new_map),
                      affects=True, detail='particle removed')]

    return [_Diff(f'{path}.content', tuple(old_map), tuple(new_map),
                  affects=True, detail='content changed')]


def _compare_occurs(old: Mapping[str, Any], new: Mapping[str, Any],
                    path: str) -> list[_Diff]:
    differences: list[_Diff] = []
    old_min = old.get('min_occurs')
    new_min = new.get('min_occurs')
    if old_min != new_min:
        affects = new_min is not None and old_min is not None and new_min > old_min
        differences.append(_Diff(f'{path}.min_occurs', old_min, new_min,
                                 affects=affects, detail='tightened'
                                 if affects else 'widened'))

    old_max = old.get('max_occurs')
    new_max = new.get('max_occurs')
    if old_max != new_max:
        if new_max is None:
            affects = False
        elif old_max is None:
            affects = True
        else:
            affects = new_max < old_max
        differences.append(_Diff(f'{path}.max_occurs', old_max, new_max,
                                 affects=affects, detail='tightened'
                                 if affects else 'widened'))
    return differences


def _compare(old: Any, new: Any, path: str = '') -> list[_Diff]:
    if old == new:
        return []
    elif isinstance(old, Mapping) and isinstance(new, Mapping):
        differences: list[_Diff] = []
        for key in sorted(set(old) | set(new), key=str):
            old_value = old.get(key)
            new_value = new.get(key)
            if old_value == new_value:
                continue

            child_path = f'{path}.{key}' if path else key
            if key == 'facets' and isinstance(old_value, Mapping) and \
                    isinstance(new_value, Mapping):
                differences.extend(_compare_facets(old_value, new_value, child_path))
            elif key == 'attributes' and isinstance(old_value, Sequence) and \
                    isinstance(new_value, Sequence):
                differences.extend(_compare_attributes(old_value, new_value, child_path))
            elif key == 'model':
                differences.extend(_compare_model(old_value, new_value, child_path))
            elif key == 'mixed':
                affects = bool(old_value) and not bool(new_value)
                differences.append(_Diff(child_path, old_value, new_value,
                                         affects=affects, detail='tightened'
                                         if affects else 'widened'))
            elif key == 'abstract':
                affects = not bool(old_value) and bool(new_value)
                differences.append(_Diff(child_path, old_value, new_value,
                                         affects=affects, detail='tightened'
                                         if affects else 'widened'))
            elif key == 'member_types':
                differences.extend(_compare_union_members(old_value, new_value,
                                                          child_path))
            elif key == 'use':
                affects = old_value != 'required' and new_value == 'required' or \
                    new_value == 'prohibited'
                differences.append(_Diff(child_path, old_value, new_value,
                                         affects=affects, detail='tightened'
                                         if affects else 'widened'))
            elif key in ('nillable',):
                affects = bool(old_value) and not bool(new_value)
                differences.append(_Diff(child_path, old_value, new_value,
                                         affects=affects, detail='tightened'
                                         if affects else 'widened'))
            elif key == 'min_occurs' or key == 'max_occurs':
                differences.extend(_compare_occurs(
                    {key: old_value}, {key: new_value}, path or key
                ))
            else:
                differences.extend(_compare(old_value, new_value, child_path))
        return differences
    elif isinstance(old, Sequence) and isinstance(new, Sequence) and \
            not isinstance(old, (str, bytes)) and not isinstance(new, (str, bytes)):
        old_keys = [item.get('name') if isinstance(item, Mapping) else None
                    for item in old]
        new_keys = [item.get('name') if isinstance(item, Mapping) else None
                    for item in new]
        if old_keys == new_keys and len(old) == len(new):
            differences = []
            for index, (old_item, new_item) in enumerate(zip(old, new)):
                differences.extend(_compare(old_item, new_item, f'{path}.{index}'))
            return differences
        return [_Diff(path, old, new, affects=True, detail='structure changed')]
    elif isinstance(old, Mapping) or isinstance(new, Mapping):
        return [_Diff(path, old, new, affects=True, detail='structure changed')]
    else:
        return [_Diff(path, old, new, affects=True, detail='changed')]


@dataclass
class _ComponentChange:
    kind: str
    name: str
    affects: bool
    reason: str = 'changed'
    differences: list[_Diff] = field(default_factory=list)


def _iter_types(schema: SchemaType, names: Iterable[str]) -> Iterator[tuple[str, BaseXsdType]]:
    for name in names:
        xsd_type = schema.maps.types.get(name)
        if xsd_type is None or xsd_type.schema.maps is not schema.maps:
            continue
        yield name, xsd_type


def _iter_elements(schema: SchemaType, names: Iterable[str]) -> Iterator[tuple[str, XsdElement]]:
    for name in names:
        xsd_element = schema.maps.elements.get(name)
        if xsd_element is None or xsd_element.schema.maps is not schema.maps:
            continue
        yield name, xsd_element


def _component_names(schema: SchemaType, kind: str) -> set[str]:
    mapping = getattr(schema.maps, kind)
    names = set()
    for name, component in mapping.items():
        if component.schema.maps is schema.maps and name is not None:
            names.add(name)
    return names


def _iter_particles(group: Any) -> Iterator[Any]:
    """Iterates the particles of a model group, following group references."""
    seen = set()
    stack = [group]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        content = getattr(current, 'content', None)
        if content is None:
            content = current
        if not isinstance(content, (list, tuple)):
            continue

        for particle in content:
            if isinstance(particle, XsdGroup):
                stack.append(particle)
            else:
                yield particle

        reference = getattr(current, 'ref', None)
        if reference is not None:
            stack.append(reference)


def _iter_model_elements(model_group: Any) -> Iterator[XsdElement]:
    for particle in _iter_particles(model_group):
        if isinstance(particle, XsdElement):
            yield particle


def _referenced_type_names(xsd_type: Optional[BaseXsdType],
                           seen: Optional[set[int]] = None) -> set[str]:
    """
    Returns the named types referenced by a type, directly or through
    anonymous types.

    Attributes and content-model elements are included because changing their
    types narrows/extends the value space of the containing type even if its
    own declaration text is unchanged.
    """
    if xsd_type is None:
        return set()
    if seen is None:
        seen = set()
    if id(xsd_type) in seen:
        return set()
    seen.add(id(xsd_type))

    names: set[str] = set()
    type_name = getattr(xsd_type, 'name', None)
    if type_name is not None and type_name not in _UNKNOWN_TYPES:
        names.add(type_name)

    attributes = getattr(xsd_type, 'attributes', None)
    if attributes is not None:
        for attribute in attributes.values():
            attribute_type = getattr(attribute, 'type', None)
            attribute_name = getattr(attribute_type, 'name', None)
            if attribute_name is not None:
                names.add(attribute_name)
            else:
                names.update(_referenced_type_names(attribute_type, seen))

    model_group = getattr(xsd_type, 'model_group', None)
    if model_group is not None:
        for particle in _iter_particles(model_group):
            if not isinstance(particle, XsdElement):
                continue
            element_type = particle.type
            element_type_name = getattr(element_type, 'name', None)
            if element_type_name is not None:
                names.add(element_type_name)
            else:
                names.update(_referenced_type_names(element_type, seen))

    simple_content = getattr(xsd_type, 'simple_type', None)
    if simple_content is not None:
        names.update(_referenced_type_names(simple_content, seen))

    return {name for name in names if name not in _UNKNOWN_TYPES}


def _derivation_index(schema: SchemaType) -> dict[str, set[str]]:
    """
    Builds a map type name -> names of the types whose value space depends on it.

    Dependencies include base types, list item types, union member types and
    the types used by attributes or content-model elements: tightening any of
    them narrows the dependent type's value space.
    """
    derived: dict[str, set[str]] = {}

    def add_dependency(source: Any, dependent: str) -> None:
        while source is not None and not _is_builtin(source):
            source_name = getattr(source, 'name', None)
            if source_name is not None:
                derived.setdefault(source_name, set()).add(dependent)
            return

    for name in _component_names(schema, 'types'):
        xsd_type = schema.maps.types.get(name)
        if xsd_type is None:
            continue

        add_dependency(getattr(xsd_type, 'base_type', None), name)

        variety = getattr(xsd_type, 'variety', None)
        if variety == 'list':
            for candidate in _iter_base_chain(xsd_type):
                if hasattr(candidate, 'item_type'):
                    add_dependency(candidate.item_type, name)
                    break
        elif variety == 'union':
            member_types: Iterable[Any] = ()
            for candidate in _iter_base_chain(xsd_type):
                if hasattr(candidate, 'member_types'):
                    member_types = candidate.member_types
                    break
            for member in member_types:
                add_dependency(member, name)

        for referenced in _referenced_type_names(xsd_type):
            if referenced != name:
                derived.setdefault(referenced, set()).add(name)

    return derived


def _type_descendants(type_name: str, derived: Mapping[str, set[str]]) -> set[str]:
    descendants = set()
    pending = [type_name]
    while pending:
        current = pending.pop()
        for child in derived.get(current, ()):
            if child not in descendants:
                descendants.add(child)
                pending.append(child)
    return descendants


def _is_annotation_tag(tag: Any) -> bool:
    return tag in (nm.XSD_ANNOTATION, nm.XSD_APPINFO, nm.XSD_DOCUMENTATION)


def _source_paths(schema: SchemaType) -> dict[Any, str]:
    """
    Maps each XSD element of the schema source to a stable source path.

    The path uses the declared names and, only for unnamed or duplicated
    declarations, a positional index. Annotation elements are skipped, so
    editing documentation never changes the identity of a declaration. The
    identity doesn't depend on the prefix or on the file position, so it can
    be recorded with the old schema and resolved on the new one.
    """
    cache = schema.__dict__.get('_revalidation_source_paths')
    if cache is not None:
        return cache

    paths: dict[Any, str] = {}
    root = schema.source.root

    def walk(elem: Any, path: str) -> None:
        paths[elem] = path

        children = [child for child in elem
                    if not _is_annotation_tag(child.tag) and not callable(child.tag)]
        keys = []
        for child in children:
            name = child.get('name')
            tag = local_name(child.tag)
            keys.append((tag, name))

        occurrences: dict[tuple[str, Optional[str]], int] = {}
        for child, (tag, name) in zip(children, keys):
            occurrences[(tag, name)] = occurrences.get((tag, name), 0) + 1

        counters: dict[tuple[str, Optional[str]], int] = {}
        for child, (tag, name) in zip(children, keys):
            key = (tag, name)
            counters[key] = counters.get(key, 0) + 1
            if name is not None:
                segment = f'{tag}[name="{name}"]'
            else:
                segment = tag
            if occurrences[key] > 1:
                segment += f'[{counters[key]}]'
            walk(child, f'{path}/{segment}')

    walk(root, f'/{local_name(root.tag)}')
    schema.__dict__['_revalidation_source_paths'] = paths
    return paths


def _declaration_identity(xsd_element: XsdElement) -> str:
    """Returns the stable source path of an element declaration."""
    try:
        paths = _source_paths(xsd_element.schema)
    except (AttributeError, TypeError):
        return f'{xsd_element.schema.target_namespace}#{xsd_element.name}'
    return paths.get(xsd_element.elem, f'{xsd_element.schema.target_namespace}#{xsd_element.name}')


def _same_without_annotations(old_elem: Any, new_elem: Any) -> bool:
    """Compares two XSD Element trees ignoring annotation subtrees."""
    if old_elem.tag != new_elem.tag or old_elem.text != new_elem.text:
        return False
    old_attrib = {k: v for k, v in old_elem.attrib.items()
                  if not _is_annotation_tag(k)}
    new_attrib = {k: v for k, v in new_elem.attrib.items()
                  if not _is_annotation_tag(k)}
    if old_attrib != new_attrib:
        return False

    old_children = [e for e in old_elem if not _is_annotation_tag(e.tag)]
    new_children = [e for e in new_elem if not _is_annotation_tag(e.tag)]
    if len(old_children) != len(new_children):
        return False
    return all(_same_without_annotations(e1, e2)
               for e1, e2 in zip(old_children, new_children))


def _same_tree(old_elem: Any, new_elem: Any) -> bool:
    """Compares two XSD Element trees, annotations included."""
    if old_elem.tag != new_elem.tag or old_elem.text != new_elem.text or \
            old_elem.tail != new_elem.tail or old_elem.attrib != new_elem.attrib:
        return False
    if len(old_elem) != len(new_elem):
        return False
    return all(_same_tree(e1, e2) for e1, e2 in zip(old_elem, new_elem))


def _annotation_only(old_component: Any, new_component: Any) -> bool:
    old_elem = getattr(old_component, 'elem', None)
    new_elem = getattr(new_component, 'elem', None)
    if old_elem is None or new_elem is None:
        return False
    return not _same_tree(old_elem, new_elem) and \
        _same_without_annotations(old_elem, new_elem)


class SchemaEvolution:
    """
    Compares two schema versions and classifies historical records.

    :param old_schema: the schema instance used for the historical decoding.
    :param new_schema: the new schema instance published by the vendor.
    :param namespaces: optional prefix map used for messages. Defaults to the \
    namespace map of the new schema.

    The comparison ignores annotations. A change to a base type propagates to
    its derived types, even when their own declaration is textually unchanged,
    because base facets constrain the derived value space.
    """

    def __init__(self, old_schema: SchemaType, new_schema: SchemaType,
                 namespaces: Optional[Mapping[str, str]] = None) -> None:
        if not hasattr(old_schema, 'maps') or not hasattr(new_schema, 'maps'):
            raise XMLSchemaTypeError(_("schema instances required"))

        self.old_schema = old_schema
        self.new_schema = new_schema
        self.namespaces = dict(namespaces or new_schema.namespaces)
        self.ignored_annotations = False

        self._changes: dict[tuple[str, str], _ComponentChange] = {}
        self._type_changes: dict[str, _ComponentChange] = {}
        self._element_changes: dict[str, _ComponentChange] = {}
        self._compare_maps('types', _canonical_component)
        self._compare_maps('elements', _canonical_component)
        self._compare_maps('attributes', _canonical_component)
        self._compare_maps('groups', _canonical_component)
        self._compare_maps('attribute_groups', _canonical_component)

        self._derived = _derivation_index(new_schema)
        self._affected_types: dict[str, _ComponentChange] = {}
        for name, change in self._type_changes.items():
            if not change.affects:
                continue
            self._affected_types[name] = change
            for descendant in _type_descendants(name, self._derived):
                if descendant not in self._affected_types:
                    self._affected_types[descendant] = change

        # Elements affected directly, plus elements whose type changed.
        self._affected_elements = {
            name for name, change in self._element_changes.items() if change.affects
        }
        for name, xsd_element in _iter_elements(
                new_schema, _component_names(new_schema, 'elements')):
            type_name = getattr(xsd_element.type, 'name', None)
            if type_name in self._affected_types:
                self._affected_elements.add(name)

        self.changes = self._build_public_changes()

    def _compare_maps(self, kind: str, snapshot: Any) -> None:
        old_names = _component_names(self.old_schema, kind)
        new_names = _component_names(self.new_schema, kind)

        for name in sorted(old_names | new_names):
            old_component = getattr(self.old_schema.maps, kind).get(name)
            new_component = getattr(self.new_schema.maps, kind).get(name)

            if old_component is None:
                # An added component doesn't invalidate a record decoded with
                # the old schema; it can only be reached through a wildcard.
                continue
            elif new_component is None:
                self._changes[(kind, name)] = _ComponentChange(
                    kind, name, affects=True, reason='removed'
                )
                if kind == 'types':
                    self._type_changes[name] = self._changes[(kind, name)]
                elif kind == 'elements':
                    self._element_changes[name] = self._changes[(kind, name)]
                continue

            old_snapshot = snapshot(old_component)
            new_snapshot = snapshot(new_component)
            differences = _compare(old_snapshot, new_snapshot)

            if not differences:
                if _annotation_only(old_component, new_component):
                    self.ignored_annotations = True
                continue

            affects = any(difference.affects for difference in differences)
            change = _ComponentChange(kind, name, affects, differences=differences)
            self._changes[(kind, name)] = change
            if kind == 'types':
                self._type_changes[name] = change
            elif kind == 'elements':
                self._element_changes[name] = change

    def _build_public_changes(self) -> list[SchemaChange]:
        changes = []
        for (kind, name), change in self._changes.items():
            derived_types: tuple[str, ...] = ()
            if kind == 'types':
                derived_types = tuple(sorted(_type_descendants(name, self._derived)))
            changes.append(SchemaChange(
                kind=kind,
                name=name,
                display_name=_display_name(name, self.namespaces),
                affects=change.affects,
                reason=change.reason,
                paths=tuple(sorted({difference.path for difference in change.differences})),
                derived_types=derived_types,
                details=tuple(
                    sorted({difference.detail for difference in change.differences})
                ),
            ))
        changes.sort(key=lambda x: (x.kind, x.name))
        return changes

    @property
    def changed(self) -> bool:
        """`True` if at least one semantic change was found."""
        return bool(self._changes)

    @property
    def has_impact(self) -> bool:
        """`True` if at least one change can invalidate an old decoding."""
        return any(change.affects for change in self._changes.values())

    def classify(self, records: Iterable[Union[RecordEvidence, Mapping[str, Any]]]
                 ) -> RevalidationReport:
        """Classifies records into revalidate, reuse and missing evidence."""
        decisions: list[RecordDecision] = []
        for index, record in enumerate(records):
            evidence = self._coerce_evidence(record, index)
            decisions.append(self._classify_record(evidence))
        return RevalidationReport(
            self.old_schema, self.new_schema, self.changes,
            decisions, self.ignored_annotations
        )

    analyze = classify
    report = classify

    def _coerce_evidence(self, record: Union[RecordEvidence, Mapping[str, Any]],
                         index: int) -> RecordEvidence:
        if isinstance(record, RecordEvidence):
            if record.reference is None:
                return RecordEvidence(record.declarations, record.complete, index)
            return record
        elif not isinstance(record, Mapping):
            raise XMLSchemaTypeError(
                _("record evidence must be a RecordEvidence or a mapping")
            )

        declarations = []
        raw_declarations = record.get('declarations')
        if raw_declarations is None:
            raw_declarations = record.get('used_declarations', ())
            if not raw_declarations and 'element' in record:
                raw_declarations = (record,)

        for item in raw_declarations:
            if isinstance(item, DeclarationUse):
                declarations.append(item)
            elif isinstance(item, Mapping):
                declarations.append(DeclarationUse(
                    element=item.get('element'),
                    type=item.get('type'),
                    schema_path=item.get('schema_path', item.get('path')),
                    anonymous=item.get('anonymous'),
                    dynamic=bool(item.get('dynamic', False)),
                ))
            else:
                raise XMLSchemaTypeError(_("invalid declaration evidence %r") % (item,))

        reference = record.get('reference', record.get('id', index))
        return RecordEvidence(
            declarations=tuple(declarations),
            complete=bool(record.get('complete', True)),
            reference=reference,
        )

    def _classify_record(self, evidence: RecordEvidence) -> RecordDecision:
        if not self.has_impact:
            # No change can narrow the accepted value space, so the old
            # decoding remains valid even when its evidence is incomplete.
            return RecordDecision(evidence.reference, REUSE, (), evidence)

        affected: list[str] = []
        missing: list[str] = []

        if not evidence.declarations:
            missing.append(_("no declaration evidence was recorded"))

        for declaration in evidence.declarations:
            declaration_affected, declaration_missing = self._check_declaration(declaration)
            affected.extend(declaration_affected)
            missing.extend(declaration_missing)

        if affected:
            return RecordDecision(evidence.reference, REVALIDATE,
                                  tuple(dict.fromkeys(affected)), evidence)
        elif missing or not evidence.complete:
            if not missing:
                missing.append(_("the old decoding didn't record a complete dynamic type"))
            return RecordDecision(evidence.reference, MISSING_EVIDENCE,
                                  tuple(dict.fromkeys(missing)), evidence)
        else:
            return RecordDecision(evidence.reference, REUSE, (), evidence)

    def _check_declaration(self, declaration: DeclarationUse
                           ) -> tuple[list[str], list[str]]:
        affected: list[str] = []
        missing: list[str] = []

        old_elements = self._find_elements(self.old_schema, declaration)
        new_elements = self._find_elements(self.new_schema, declaration)

        if not old_elements:
            missing.append(_("declaration %s is not part of the old schema") % declaration)
            return affected, missing

        if declaration.dynamic and declaration.used_type is None:
            missing.append(_("dynamic type of %s was not recorded") % declaration)
            return affected, missing

        if not new_elements:
            affected.append(_("declaration %s was removed from the new schema") % declaration)
            return affected, missing

        if self._is_ambiguous(declaration, old_elements):
            missing.append(_("declaration %s matches more than one local declaration; "
                             "record a schema_path to disambiguate") % declaration)
            return affected, missing

        old_element = old_elements[0]
        new_element = self._match_new_element(old_element, new_elements)
        if new_element is None:
            affected.append(_("declaration %s has no matching counterpart in the new "
                              "schema") % declaration)
            return affected, missing

        element_affected, element_missing = self._check_element_pair(
            old_element, declaration, new_element
        )
        affected.extend(element_affected)
        missing.extend(element_missing)
        return affected, missing

    def _is_ambiguous(self, declaration: DeclarationUse,
                      old_elements: Sequence[XsdElement]) -> bool:
        """
        Detects same-named local declarations that the evidence can't resolve.

        A name that selects a global declaration is unambiguous: global
        declarations are unique. For local declarations the recorded source
        path is required, but a missing path is accepted when all candidates
        contain the same schema path.
        """
        if len(old_elements) <= 1 or declaration.schema_path:
            return False
        if any(old_element.parent is None for old_element in old_elements):
            return False
        return True

    def _check_element_pair(self, old_element: XsdElement, declaration: DeclarationUse,
                            new_element: Optional[XsdElement] = None
                            ) -> Optional[tuple[list[str], list[str]]]:
        """
        Compares one old declaration with its counterpart in the new schema.

        Returns `None` when the old declaration has no counterpart that can be
        unambiguously matched.
        """
        affected: list[str] = []
        missing: list[str] = []

        if new_element is None:
            new_elements = self._find_elements(self.new_schema, declaration)
            new_element = self._match_new_element(old_element, new_elements)
            if new_element is None:
                return None

        old_snapshot = _element_snapshot(old_element)
        new_snapshot = _element_snapshot(new_element)
        element_differences = _compare(old_snapshot, new_snapshot)
        if any(difference.affects for difference in element_differences):
            affected.append(_("declaration %s changed") % declaration)

        if declaration.type is None and declaration.anonymous is not None:
            # An anonymous type has no QName in the global map: its structure is
            # embedded in the element snapshot, which was already compared above.
            if getattr(old_element.type, 'name', None) != \
                    getattr(new_element.type, 'name', None):
                affected.append(_("anonymous type of %s changed") % declaration)
            return affected, missing

        type_name = declaration.used_type
        if type_name is None:
            declared_type = getattr(old_element.type, 'name', None)
            if declared_type in _UNKNOWN_TYPES or declared_type is None:
                missing.append(_("type of %s was not recorded and is unknown") % declaration)
                return affected, missing
            type_name = declared_type
        else:
            old_type_name = _resolve_qname(type_name, self.old_schema)
            if old_type_name not in self.old_schema.maps.types:
                missing.append(_("type %s was not part of the old schema") %
                               _display_name(type_name, self.namespaces))
                return affected, missing
            type_name = _resolve_qname(type_name, self.new_schema)

        if type_name in _UNKNOWN_TYPES:
            missing.append(_("type %s is unknown") % type_name)
            return affected, missing

        new_type = self.new_schema.maps.types.get(type_name)
        if new_type is None:
            affected.append(_("type %s was removed from the new schema") %
                            _display_name(type_name, self.namespaces))
            return affected, missing

        if type_name in self._affected_types:
            change = self._affected_types[type_name]
            source = _display_name(change.name, self.namespaces)
            target = _display_name(type_name, self.namespaces)
            if source != target:
                affected.append(_("type %s depends on changed type %s") %
                                (target, source))
            else:
                affected.append(_("type %s changed") % target)

        return affected, missing

    def _find_elements(self, schema: SchemaType,
                       declaration: DeclarationUse) -> list[XsdElement]:
        if declaration.schema_path:
            index = _element_index(schema)
            if _looks_like_source_path(declaration.schema_path):
                by_path = index['by_source_path'].get(declaration.schema_path, [])
            else:
                by_path = index['by_schema_path'].get(declaration.schema_path, [])

            if by_path:
                return list(by_path)

            selected = self._resolve_path(schema, declaration.schema_path)
            if isinstance(selected, XsdElement):
                return [selected]

            # A declared path is authoritative: don't fall back to name
            # matching, because another declaration with the same name could
            # have been the one actually used.
            return []

        if declaration.element is None:
            return []

        name = _expanded_name(declaration.element, schema)
        if name is None:
            return []

        return list(_element_index(schema)['by_name'].get(name, ()))

    def _match_new_element(self, old_element: XsdElement,
                           new_elements: Sequence[XsdElement]) -> Optional[XsdElement]:
        if len(new_elements) == 1:
            return new_elements[0]

        old_path = _safe_path(old_element)
        for candidate in new_elements:
            if _safe_path(candidate) == old_path:
                return candidate

        globals_ = [e for e in new_elements if e.parent is None]
        if len(globals_) == 1:
            return globals_[0]
        return None

    def _resolve_path(self, schema: SchemaType, schema_path: str) -> Optional[XsdElement]:
        try:
            selected = schema.find(schema_path, self.namespaces)
        except (KeyError, ValueError):
            return None
        return selected if isinstance(selected, XsdElement) else None


def _safe_path(xsd_element: XsdElement) -> Optional[str]:
    try:
        return xsd_element.get_path()
    except (AttributeError, ValueError):
        return None


def _element_index(schema: SchemaType) -> dict[str, dict[Any, list[XsdElement]]]:
    """Indexes the element declarations by name, schema path and source path."""
    cache = schema.__dict__.get('_revalidation_element_index')
    if cache is not None:
        return cache

    by_name: dict[Any, list[XsdElement]] = {}
    by_schema_path: dict[Any, list[XsdElement]] = {}
    by_source_path: dict[Any, list[XsdElement]] = {}
    for xsd_element in _iter_all_elements(schema):
        by_name.setdefault(xsd_element.name, []).append(xsd_element)

        path = _safe_path(xsd_element)
        if path is not None:
            by_schema_path.setdefault(path, []).append(xsd_element)

        try:
            identity = _declaration_identity(xsd_element)
        except (AttributeError, TypeError, ValueError):
            continue
        by_source_path.setdefault(identity, []).append(xsd_element)

    cache = {
        'by_name': by_name,
        'by_schema_path': by_schema_path,
        'by_source_path': by_source_path,
    }
    schema.__dict__['_revalidation_element_index'] = cache
    return cache


def _looks_like_source_path(path: str) -> bool:
    """
    Tells a vendor source path from an XPath path.

    Source paths are rooted at the ``xs:schema`` element and use declared
    names, e.g. ``/schema/complexType[name="R"]/sequence/element[name="x"]``.
    XPath paths recorded by callers are rooted at a declaration name instead.
    """
    return path == '/schema' or path.startswith('/schema/')


def _iter_all_elements(schema: SchemaType) -> Iterator[XsdElement]:
    """Iterates global and local element declarations of a schema."""
    seen_types: set[int] = set()
    seen_elements: set[int] = set()

    def walk_type(xsd_type: Optional[BaseXsdType]) -> Iterator[XsdElement]:
        if xsd_type is None or id(xsd_type) in seen_types:
            return
        seen_types.add(id(xsd_type))

        model_group = getattr(xsd_type, 'model_group', None)
        if model_group is not None:
            yield from walk_group(model_group)

        simple_type = getattr(xsd_type, 'simple_type', None)
        if simple_type is not None and isinstance(simple_type, XsdSimpleType):
            pass

        base_type = getattr(xsd_type, 'base_type', None)
        if base_type is not None and not _is_builtin(base_type):
            yield from walk_type(base_type)

    def walk_group(group: Any) -> Iterator[XsdElement]:
        for particle in getattr(group, 'content', group):
            if isinstance(particle, XsdGroup):
                yield from walk_group(particle)
            elif isinstance(particle, XsdElement):
                if id(particle) not in seen_elements:
                    seen_elements.add(id(particle))
                    yield particle
                yield from walk_type(particle.type)

    for name, xsd_element in _iter_elements(
            schema, _component_names(schema, 'elements')):
        if id(xsd_element) not in seen_elements:
            seen_elements.add(id(xsd_element))
            yield xsd_element
        yield from walk_type(xsd_element.type)

    for name, xsd_type in _iter_types(schema, _component_names(schema, 'types')):
        yield from walk_type(xsd_type)


class DecodeUsageCollector:
    """
    Collects the declarations and actual types used while decoding a record.

    Use an instance as the *element_hook* of :meth:`XMLSchemaBase.iter_decode`
    or :meth:`XMLSchemaBase.decode`::

        collector = DecodeUsageCollector()
        data = schema.decode(xml_source, element_hook=collector.collect)
        evidence = collector.evidence(reference='record-1')

    The collector registers the dynamic type chosen through ``xsi:type`` or
    XSD 1.1 alternatives, because the hook receives the effective *xsd_type*.
    A wildcard, a skipped declaration or an unresolved dynamic type marks the
    evidence as incomplete, so the record can't be classified as safe.

    When decoding a file with more than one record, set *root=...* for the
    name of the record element: a new :class:`RecordEvidence` is started at
    each root occurrence, so :attr:`records` can be analyzed in one pass.
    """

    def __init__(self, root: Any = None) -> None:
        self._declarations: dict[
            tuple[Optional[str], Optional[str], Optional[str]], DeclarationUse
        ] = {}
        self._complete = True
        self._root = _resolve_root_name(root) if root is not None else None
        self._records: list[RecordEvidence] = []
        self._current_root: Optional[object] = None

    def __call__(self, element_data: Any, xsd_element: XsdElement,
                 xsd_type: Optional[BaseXsdType] = None) -> Any:
        return self.collect(element_data, xsd_element, xsd_type)

    def collect(self, element_data: Any, xsd_element: XsdElement,
                xsd_type: Optional[BaseXsdType] = None) -> Any:
        if not isinstance(xsd_element, XsdElement):
            self._complete = False
            return element_data

        if getattr(xsd_element, 'name', None) is None:
            self._complete = False
            return element_data

        is_root = self._root is not None and xsd_element.local_name == self._root

        actual_type = xsd_type if xsd_type is not None else xsd_element.type
        type_name = getattr(actual_type, 'name', None)

        # The hook receives the effective type for resolved xsi:type values,
        # but an unresolved one leaves the declared type in place. Read the
        # attribute too, so an unknown dynamic type can't be mistaken for a
        # static one.
        xsi_type = None
        for name, value in getattr(element_data, 'attributes', ()):
            if name == nm.XSI_TYPE:
                xsi_type = value
                break

        if xsi_type is not None:
            try:
                resolved = xsd_element.schema.resolve_qname(xsi_type)
                dynamic_type = xsd_element.maps.types[resolved]
            except (KeyError, ValueError, RuntimeError):
                self._complete = False
            else:
                if getattr(dynamic_type, 'name', None) in _UNKNOWN_TYPES:
                    self._complete = False
                else:
                    actual_type = dynamic_type
                    type_name = dynamic_type.name

        if type_name in _UNKNOWN_TYPES:
            self._complete = False

        schema_path = None
        if getattr(xsd_element, 'ref', None) is not None:
            source_element = xsd_element.ref
        else:
            source_element = xsd_element

        # The source path is stable across schema versions and disambiguates
        # local declarations with the same expanded name.
        try:
            schema_path = _declaration_identity(source_element)
        except (AttributeError, TypeError, ValueError):
            try:
                schema_path = source_element.get_path()
            except (AttributeError, ValueError):
                pass

        declaration = DeclarationUse(
            element=xsd_element.name,
            type=type_name if type_name not in _UNKNOWN_TYPES else None,
            schema_path=schema_path,
            anonymous=None if type_name is not None else schema_path,
            dynamic=False,
        )
        key = (declaration.element, declaration.type, declaration.schema_path)
        self._declarations[key] = declaration

        if is_root:
            # The hook is called on the way out of an element, so the root is
            # the last declaration of the record: close it immediately.
            self._records.append(self.evidence(reference=len(self._records)))
            self._declarations.clear()
            self._complete = True
        return element_data

    def reset(self) -> None:
        """Clears the collected evidence."""
        self._declarations.clear()
        self._complete = True
        self._records.clear()
        self._current_root = None

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def declarations(self) -> tuple[DeclarationUse, ...]:
        return tuple(self._declarations.values())

    @property
    def records(self) -> list[RecordEvidence]:
        """
        Evidence grouped by record. With *root* provided, every record is
        closed when its root element completes. Without *root*, a single open
        record is returned until :meth:`close` is called.
        """
        if self._root is None and self._declarations:
            return self._records + [self.evidence(reference=len(self._records))]
        return list(self._records)

    def close(self, reference: Any = None) -> list[RecordEvidence]:
        """Closes the current record and returns all the collected records."""
        if self._declarations:
            if reference is None:
                reference = len(self._records)
            self._records.append(self.evidence(reference=reference))
            self._declarations.clear()
            self._complete = True
        self._current_root = None
        return self.records

    def evidence(self, reference: Any = None, complete: Optional[bool] = None
                 ) -> RecordEvidence:
        """Returns the collected evidence for a record."""
        return RecordEvidence(
            declarations=self.declarations,
            complete=self._complete if complete is None else bool(complete),
            reference=reference,
        )


def _resolve_root_name(root: Any) -> Optional[str]:
    if isinstance(root, XsdElement):
        return root.local_name
    elif isinstance(root, str):
        return local_name(root)
    else:
        raise XMLSchemaTypeError(_("root must be an XsdElement or a name"))


def analyze_schema_change(old_schema: SchemaType, new_schema: SchemaType,
                          records: Iterable[Union[RecordEvidence, Mapping[str, Any]]],
                          namespaces: Optional[Mapping[str, str]] = None
                          ) -> RevalidationReport:
    """
    Compares two schemas and classifies historical records for revalidation.

    :param old_schema: the schema used to decode the historical records.
    :param new_schema: the new schema published by the vendor.
    :param records: evidence of the declarations/types used by each record.
    :param namespaces: optional prefix map used for names and paths.
    :return: a :class:`RevalidationReport` with the three buckets.
    """
    return SchemaEvolution(old_schema, new_schema, namespaces).classify(records)


def classify_records(old_schema: SchemaType, new_schema: SchemaType,
                     records: Iterable[Union[RecordEvidence, Mapping[str, Any]]],
                     namespaces: Optional[Mapping[str, str]] = None
                     ) -> RevalidationReport:
    """Alias of :func:`analyze_schema_change`."""
    return analyze_schema_change(old_schema, new_schema, records, namespaces)


def revalidation_impact(old_schema: SchemaType, new_schema: SchemaType,
                        records: Iterable[Union[RecordEvidence, Mapping[str, Any]]],
                        namespaces: Optional[Mapping[str, str]] = None
                        ) -> RevalidationReport:
    """Alias of :func:`analyze_schema_change`."""
    return analyze_schema_change(old_schema, new_schema, records, namespaces)
