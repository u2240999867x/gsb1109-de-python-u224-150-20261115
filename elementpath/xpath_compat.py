#
# Copyright (c), 2018-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
Evaluation and diffing of XPath rules between the old XPath 1.0 behaviour and
the XPath 2.0 parser with XPath 1.0 compatibility rules enabled.

The module is intended for batch audits of XPath rules that are migrated to
the newer compatibility mode. Expressions are evaluated on the same local XML
sample with different parser profiles and the outcomes are compared without
collapsing sequence items to strings. Compilation errors, static/dynamic type
errors, item type changes, count changes, item order changes and node identity
changes are reported separately. Two further buckets are always reported:
empty sequence versus ``false()`` and numeric versus string equality.

Ref: https://www.w3.org/TR/xpath20/#id-compatibility
"""
import json
import os
import sys
from copy import copy
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any, Optional, Union, cast
from collections.abc import Iterable, Iterator, Mapping, Sequence

from elementpath.exceptions import ElementPathError
from elementpath.datatypes import AnyAtomicType, AnyURI, UntypedAtomic
from elementpath.xpath_context import XPathContext
from elementpath.xpath_nodes import XPathNode
from elementpath.xpath_tokens import XPathToken
from elementpath.xpath1 import XPath1Parser
from elementpath.xpath2 import XPath2Parser

__all__ = ['LEGACY', 'CURRENT', 'STRICT', 'PROFILE_NAMES', 'OK', 'COMPILE_ERROR',
           'TYPE_ERROR', 'RUNTIME_ERROR', 'ITEM_TYPE', 'ITEM_VALUE', 'ITEM_COUNT',
           'ITEM_ORDER', 'NODE_IDENTITY', 'NODE_VALUE', 'ERROR_PHASE', 'ERROR_CODE',
           'EMPTY_VS_FALSE', 'NUMERIC_VS_STRING', 'EvaluationProfile',
           'EvaluationOutcome', 'ComparisonOperand', 'ResultItem', 'ItemDifference',
           'SpecialDifference', 'RuleDifference', 'RuleComparison', 'BatchReport',
           'compatibility_profiles', 'evaluate_rule', 'compare_evaluation',
           'compare_rule', 'compare_rules', 'audit_rules', 'format_report']

LEGACY = 'legacy'
CURRENT = 'current'
STRICT = 'strict'

PROFILE_NAMES = (LEGACY, CURRENT, STRICT)

# Evaluation statuses.
OK = 'ok'
COMPILE_ERROR = 'compile-error'
TYPE_ERROR = 'type-error'
RUNTIME_ERROR = 'runtime-error'

# Difference kinds.
ITEM_TYPE = 'type'
ITEM_VALUE = 'value'
ITEM_COUNT = 'count'
ITEM_ORDER = 'order'
NODE_IDENTITY = 'node-identity'
NODE_VALUE = 'node-value'
ERROR_PHASE = 'error-phase'
ERROR_CODE = 'error-code'

# Special difference kinds.
EMPTY_VS_FALSE = 'empty-vs-false'
NUMERIC_VS_STRING = 'numeric-vs-string'

_COMPARISON_SYMBOLS = frozenset(('=', '!=', '<', '<=', '>', '>=',
                                 'eq', 'ne', 'lt', 'le', 'gt', 'ge'))
_NUMERIC_NAMES = frozenset(('xs:integer', 'xs:decimal', 'xs:double', 'xs:float',
                            'xs:numeric'))
_STRING_NAMES = frozenset(('xs:string', 'xs:anyURI', 'xs:untypedAtomic'))


@dataclass(frozen=True)
class EvaluationProfile:
    """A parser profile used for evaluating a rule."""
    name: str
    parser_class: type[XPath1Parser] = XPath1Parser
    compatibility_mode: bool = False
    version: str = '1.0'

    def __str__(self) -> str:
        return self.name

    @property
    def is_legacy(self) -> bool:
        return self.name == LEGACY


def compatibility_profiles() -> tuple[EvaluationProfile, ...]:
    """Return the default legacy/current/strict audit profiles."""
    return (
        EvaluationProfile(LEGACY, XPath1Parser, True, '1.0'),
        EvaluationProfile(CURRENT, XPath2Parser, True, '2.0'),
        EvaluationProfile(STRICT, XPath2Parser, False, '2.0'),
    )


@dataclass(frozen=True)
class ResultItem:
    """
    A typed representation of an XPath result item.

    Atomic values keep their XPath type name and their Python representation.
    Nodes are described by node kind, node path and string value, so that
    results can be correlated with the original document nodes. The item is
    never reduced to a string for comparison purposes.
    """
    type_name: str
    value_repr: str
    is_node: bool = False
    node_kind: Optional[str] = None
    path: Optional[str] = None
    name: Optional[str] = None
    string_value: Optional[str] = None
    xsd_type: Optional[str] = None

    @property
    def identity(self) -> tuple[Any, ...]:
        if self.is_node:
            return ('node', self.node_kind, self.path, self.name, self.xsd_type)
        return ('atomic', self.type_name, self.value_repr)

    @property
    def comparison_key(self) -> tuple[Any, ...]:
        if self.is_node:
            return ('node', self.node_kind, self.path, self.name,
                    self.string_value, self.xsd_type)
        return ('atomic', self.type_name, self.value_repr)

    @property
    def display(self) -> str:
        if self.is_node:
            if self.node_kind in ('text', 'attribute'):
                return f'{self.node_kind}({self.path})={self.string_value!r}'
            return f'{self.node_kind}({self.path})'
        elif self.xsd_type:
            return f'{self.xsd_type}({self.value_repr})'
        return f'{self.type_name}({self.value_repr})'


@dataclass(frozen=True)
class ComparisonOperand:
    """Typed summary of one operand of a comparison in a rule."""
    source: str
    is_empty: bool
    atomic_types: tuple[str, ...]
    sequence_type: Optional[str] = None
    is_explicit_empty: bool = False

    @property
    def is_boolean(self) -> bool:
        return self.atomic_types == ('xs:boolean',)

    @property
    def is_numeric(self) -> bool:
        return bool(self.atomic_types) and \
            all(name in _NUMERIC_NAMES for name in self.atomic_types)

    @property
    def is_string_like(self) -> bool:
        return bool(self.atomic_types) and \
            all(name in _STRING_NAMES for name in self.atomic_types)

    @property
    def is_node_like(self) -> bool:
        return self.sequence_type is not None and \
            _is_node_sequence_type(self.sequence_type)

    @property
    def is_statically_empty(self) -> bool:
        if self.sequence_type == 'empty-sequence()':
            return True
        return self.sequence_type is None and self.is_empty

    @property
    def display(self) -> str:
        if self.is_statically_empty:
            return 'empty-sequence()'
        elif len(self.atomic_types) == 1:
            return self.atomic_types[0]
        elif self.atomic_types:
            return '[' + ', '.join(self.atomic_types) + ']'
        elif self.sequence_type:
            return self.sequence_type
        return 'untyped'


@dataclass(frozen=True)
class ComparisonRecord:
    """A comparison found in a rule, with both operands."""
    symbol: str
    left: ComparisonOperand
    right: ComparisonOperand

    @property
    def has_empty_operand(self) -> bool:
        return self.left.is_statically_empty or self.right.is_statically_empty or \
            self.left.is_empty or self.right.is_empty

    @property
    def has_explicit_empty_operand(self) -> bool:
        return self.left.is_explicit_empty or self.right.is_explicit_empty

    @property
    def has_boolean_operand(self) -> bool:
        return self.left.is_boolean or self.right.is_boolean

    @property
    def mixes_numeric_and_string(self) -> bool:
        if self.left.is_numeric and self.right.is_string_like:
            return True
        elif self.left.is_string_like and self.right.is_numeric:
            return True
        elif self.left.is_numeric and self.right.is_node_like:
            return True
        elif self.left.is_node_like and self.right.is_numeric:
            return True
        return False


@dataclass(frozen=True)
class SpecialDifference:
    """
    A mandated semantic difference that must not be hidden by serialization.

    ``kind`` is either ``empty-vs-false`` or ``numeric-vs-string``. The
    ``operands`` tuple contains the typed operand displays of the comparison
    that exposes the difference, when one can be detected.
    """
    kind: str
    profile: str
    detail: str
    operands: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.kind == EMPTY_VS_FALSE:
            return 'empty sequence vs false()'
        return 'numeric vs string equality'


@dataclass(frozen=True)
class ItemDifference:
    """Difference between two result items at the same sequence position."""
    kind: str
    profile: str
    position: int
    legacy: str
    current: str
    detail: str


@dataclass
class EvaluationOutcome:
    """The outcome of evaluating a rule with a parser profile."""
    profile: str
    status: str = OK
    error_code: Optional[str] = None
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    items: list[ResultItem] = field(default_factory=list)
    sequence_is_empty: Optional[bool] = None
    is_boolean: bool = False
    comparisons: list[ComparisonRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def count(self) -> Optional[int]:
        if not self.ok:
            return None
        return len(self.items)

    @property
    def types(self) -> list[str]:
        return [item.type_name for item in self.items]

    @property
    def paths(self) -> list[str]:
        return [cast(str, item.path) for item in self.items if item.is_node]

    @property
    def is_empty_sequence(self) -> bool:
        if not self.ok:
            return False
        return self.sequence_is_empty is True

    @property
    def is_false_value(self) -> bool:
        return self.ok and self.is_boolean and len(self.items) == 1 and \
            self.items[0].type_name == 'xs:boolean' and self.items[0].value_repr == 'False'


@dataclass(frozen=True)
class RuleDifference:
    """A non-special difference between the legacy and the current profile."""
    kind: str
    legacy: str
    current: str
    detail: str
    position: Optional[int] = None


@dataclass
class RuleComparison:
    """Comparison of one rule across the audit profiles."""
    rule: str
    legacy: EvaluationOutcome
    current: EvaluationOutcome
    strict: Optional[EvaluationOutcome] = None
    differences: list[RuleDifference] = field(default_factory=list)
    item_differences: list[ItemDifference] = field(default_factory=list)
    special_differences: list[SpecialDifference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def incompatible(self) -> bool:
        return bool(self.differences or self.item_differences or self.special_differences)

    @property
    def categories(self) -> list[str]:
        kinds = {d.kind for d in self.differences}
        kinds.update(d.kind for d in self.item_differences)
        kinds.update(d.kind for d in self.special_differences)
        return sorted(kinds)

    @property
    def difference_kinds(self) -> list[str]:
        return self.categories

    def to_dict(self) -> dict[str, Any]:
        def outcome_dict(outcome: Optional[EvaluationOutcome]) -> Optional[dict[str, Any]]:
            if outcome is None:
                return None
            return {
                'profile': outcome.profile,
                'status': outcome.status,
                'error_code': outcome.error_code,
                'error_class': outcome.error_class,
                'error_message': outcome.error_message,
                'sequence_is_empty': outcome.sequence_is_empty,
                'is_boolean': outcome.is_boolean,
                'count': outcome.count,
                'types': outcome.types,
                'items': [asdict(item) for item in outcome.items],
                'comparisons': [
                    {
                        'symbol': record.symbol,
                        'left': asdict(record.left),
                        'right': asdict(record.right),
                    } for record in outcome.comparisons
                ],
            }

        return {
            'rule': self.rule,
            'legacy': outcome_dict(self.legacy),
            'current': outcome_dict(self.current),
            'strict': outcome_dict(self.strict),
            'incompatible': self.incompatible,
            'categories': self.categories,
            'differences': [asdict(d) for d in self.differences],
            'item_differences': [asdict(d) for d in self.item_differences],
            'special_differences': [asdict(d) for d in self.special_differences],
            'notes': self.notes,
        }


@dataclass
class BatchReport:
    """Report of a batch of rule comparisons."""
    rules: list[RuleComparison] = field(default_factory=list)
    source: Optional[str] = None
    profiles: tuple[str, ...] = PROFILE_NAMES

    @property
    def incompatible_rules(self) -> list[RuleComparison]:
        return [rule for rule in self.rules if rule.incompatible]

    @property
    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rule in self.rules:
            for category in rule.categories:
                counts[category] = counts.get(category, 0) + 1
        return counts

    @property
    def special_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {EMPTY_VS_FALSE: 0, NUMERIC_VS_STRING: 0}
        for rule in self.rules:
            kinds = {d.kind for d in rule.special_differences}
            for kind in counts:
                if kind in kinds:
                    counts[kind] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            'source': self.source,
            'profiles': list(self.profiles),
            'summary': {
                'rules': len(self.rules),
                'incompatible': len(self.incompatible_rules),
                'categories': self.category_counts,
                'special': self.special_counts,
            },
            'rules': [rule.to_dict() for rule in self.rules],
        }


def _xsd_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return 'xs:boolean'
    elif isinstance(value, int):
        return 'xs:integer'
    elif isinstance(value, float):
        return 'xs:double'
    elif isinstance(value, Decimal):
        return 'xs:decimal'
    elif isinstance(value, UntypedAtomic):
        return 'xs:untypedAtomic'
    elif isinstance(value, AnyURI):
        return 'xs:anyURI'
    elif isinstance(value, str):
        return 'xs:string'

    type_name = getattr(type(value), 'name', None)
    if isinstance(type_name, str) and type_name:
        return f'xs:{type_name}'
    return type(value).__name__


def _value_repr(value: Any) -> str:
    if isinstance(value, bool):
        return 'True' if value else 'False'
    elif isinstance(value, float):
        if value != value:  # NaN
            return 'NaN'
        elif value == float('inf'):
            return 'INF'
        elif value == float('-inf'):
            return '-INF'
        return repr(value)
    elif isinstance(value, Decimal):
        return str(value)
    elif isinstance(value, AnyAtomicType):
        return str(value)
    return repr(value)


def _result_item(item: Any) -> ResultItem:
    if isinstance(item, XPathNode):
        try:
            name = item.node_name.qname if item.node_name is not None else None
        except (ElementPathError, KeyError):
            name = getattr(item, 'name', None)
        return ResultItem(
            type_name=f'node:{item.node_kind}',
            value_repr=repr(item.value),
            is_node=True,
            node_kind=item.node_kind,
            path=item.path,
            name=name,
            string_value=item.string_value,
            xsd_type=getattr(item, 'type_name', None),
        )
    elif isinstance(item, XPathToken):
        return ResultItem(type_name=item.label, value_repr=item.source)

    return ResultItem(
        type_name=_xsd_type_name(item),
        value_repr=_value_repr(item),
    )


_COMPILE_ERROR_CODES = frozenset((
    'XPST0001', 'XPST0003', 'XPST0005', 'XPST0008', 'XPST0010', 'XPST0017',
    'XPST0051', 'XPST0080', 'XPST0081',
))
_TYPE_ERROR_CODES = frozenset((
    'XPTY0004', 'XPTY0018', 'XPTY0019', 'XPTY0020', 'XPTY0117', 'XPDY0050',
    'FORG0006',
))


def _error_code(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    return code[4:] if code.startswith('err:') else code


def _error_outcome(profile: str, err: Exception) -> EvaluationOutcome:
    if isinstance(err, ElementPathError):
        code = _error_code(err.code)
        if isinstance(err, SyntaxError) or code in _COMPILE_ERROR_CODES:
            status = COMPILE_ERROR
        elif isinstance(err, TypeError) or code in _TYPE_ERROR_CODES:
            status = TYPE_ERROR
        else:
            status = RUNTIME_ERROR
        return EvaluationOutcome(
            profile=profile,
            status=status,
            error_code=code,
            error_class=type(err).__name__,
            error_message=str(err),
        )

    return EvaluationOutcome(
        profile=profile,
        status=RUNTIME_ERROR,
        error_class=type(err).__name__,
        error_message=str(err),
    )


def _make_parser(profile: EvaluationProfile, namespaces: Optional[Mapping[str, str]],
                 parser_args: Optional[Mapping[str, Any]]) -> XPath1Parser:
    kwargs: dict[str, Any] = dict(parser_args or {})
    if profile.parser_class is XPath2Parser:
        kwargs['compatibility_mode'] = profile.compatibility_mode
    else:
        kwargs.pop('compatibility_mode', None)

    parser = profile.parser_class(dict(namespaces or {}), **kwargs)
    if profile.is_legacy:
        parser.compatibility_mode = True
    return parser


def _static_type(token: XPathToken) -> Optional[str]:
    """Infer an operand sequence type from its token, without evaluating it."""
    literal_types = {
        '(string)': 'xs:string', '(integer)': 'xs:integer',
        '(decimal)': 'xs:decimal', '(float)': 'xs:double',
    }
    if token.symbol in literal_types:
        return literal_types[token.symbol]
    elif token.label == 'variable':
        return None
    elif token.symbol in ('true', 'false'):
        return 'xs:boolean'
    elif token.symbol in ('@', 'attribute'):
        return 'attribute()?'
    elif token.label in ('axis', 'kind test'):
        return 'node()*'
    elif token.symbol in ('//', '/', '[', '.', '..'):
        return 'node()*'
    elif token.symbol == '(':
        if not token:
            return 'empty-sequence()'
        return _static_type(token[0])

    sequence_types = getattr(token, 'sequence_types', None)
    if sequence_types:
        return sequence_types[-1]
    elif token.label == 'function':
        return None
    elif token.symbol in _COMPARISON_SYMBOLS or token.symbol in (
            'and', 'or', 'not', 'instance', 'treat', 'castable', 'cast'):
        return 'xs:boolean'
    return None


def _operand_info(token: XPathToken, context: XPathContext) -> ComparisonOperand:
    sequence_type = _static_type(token)
    try:
        values = [x for x in token.atomization(copy(context))]
    except Exception:
        try:
            value = token.evaluate(copy(context))
        except Exception:
            atomic_types: tuple[str, ...] = ()
            if sequence_type is not None and _is_node_sequence_type(sequence_type):
                atomic_types = ('xs:untypedAtomic',)
            return ComparisonOperand(
                source=token.source,
                is_empty=False,
                atomic_types=atomic_types,
                sequence_type=sequence_type,
                is_explicit_empty=token.symbol == '(' and len(token) == 0,
            )
        if isinstance(value, list):
            values = list(value)
        else:
            values = [value]

    atomic_types = tuple(_xsd_type_name(value) for value in values)
    if sequence_type is not None and _is_node_sequence_type(sequence_type):
        # Node operands atomize to untyped atomic values in XPath 1.0
        # compatibility mode, independently of the current focus context.
        atomic_types = ('xs:untypedAtomic',)

    return ComparisonOperand(
        source=token.source,
        is_empty=not values,
        atomic_types=atomic_types,
        sequence_type=sequence_type,
        is_explicit_empty=token.symbol == '(' and len(token) == 0,
    )


def _is_node_sequence_type(sequence_type: str) -> bool:
    return sequence_type.endswith('node()') or \
        sequence_type.endswith('node()*') or \
        sequence_type.endswith('node()?') or \
        'element(' in sequence_type or \
        'attribute(' in sequence_type or \
        'text()' in sequence_type or \
        'document-node()' in sequence_type


def _comparisons(token: XPathToken, context: XPathContext) -> list[ComparisonRecord]:
    records: list[ComparisonRecord] = []
    for tk in token.iter():
        if tk.symbol not in _COMPARISON_SYMBOLS or len(tk) != 2:
            continue
        records.append(ComparisonRecord(
            symbol=tk.symbol,
            left=_operand_info(tk[0], context),
            right=_operand_info(tk[1], context),
        ))
    return records


def _load_xml(source: Union[Any, str, bytes]) -> Any:
    if not isinstance(source, (str, bytes)):
        return source

    from pathlib import Path
    from xml.etree import ElementTree

    if isinstance(source, bytes):
        data = source
    elif source.lstrip().startswith('<'):
        data = source
    else:
        try:
            data = Path(source).read_bytes()
        except OSError as err:
            raise ValueError(f'cannot read XML sample {source!r}: {err}') from None

    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as err:
        raise ValueError(f'invalid XML sample: {err}') from None


def _evaluate(rule: str, root: Any, profile: EvaluationProfile,
              namespaces: Optional[Mapping[str, str]],
              variables: Optional[Mapping[str, Any]],
              parser_args: Optional[Mapping[str, Any]]) -> EvaluationOutcome:
    try:
        parser = _make_parser(profile, namespaces, parser_args)
    except Exception as err:
        return _error_outcome(profile.name, err)

    try:
        token = parser.parse(rule)
    except Exception as err:
        return _error_outcome(profile.name, err)

    try:
        context = XPathContext(
            root, namespaces=dict(namespaces or {}), variables=dict(variables or {})
        )
        value = token.evaluate(context)
    except Exception as err:
        return _error_outcome(profile.name, err)

    records: list[ComparisonRecord] = []
    try:
        records = _comparisons(token, context)
    except Exception:
        pass

    if isinstance(value, list):
        items = [_result_item(item) for item in value]
        is_boolean = len(value) == 1 and isinstance(value[0], bool)
        empty = not value
    else:
        items = [_result_item(value)]
        is_boolean = isinstance(value, bool)
        empty = False

    return EvaluationOutcome(
        profile=profile.name,
        items=items,
        sequence_is_empty=empty,
        is_boolean=is_boolean,
        comparisons=records,
    )


def evaluate_rule(rule: str,
                  root: Union[Any, str, bytes],
                  profile: Union[str, EvaluationProfile],
                  namespaces: Optional[Mapping[str, str]] = None,
                  variables: Optional[Mapping[str, Any]] = None,
                  parser_args: Optional[Mapping[str, Any]] = None) -> EvaluationOutcome:
    """
    Evaluate an XPath rule on a local XML sample with a parser profile.

    :param rule: the XPath expression to evaluate.
    :param root: an Element, an ElementTree, or a string/bytes XML source.
    :param profile: a profile name (``legacy``, ``current`` or ``strict``) or \
    an :class:`EvaluationProfile` instance.
    :param namespaces: optional mapping from prefixes to namespace URIs.
    :param variables: optional mapping of variable values for the dynamic context.
    :param parser_args: additional keyword arguments for the parser constructor.
    :return: an :class:`EvaluationOutcome` instance.
    """
    if isinstance(profile, str):
        for item in compatibility_profiles():
            if item.name == profile:
                profile = item
                break
        else:
            raise ValueError(f'unknown evaluation profile {profile!r}')

    return _evaluate(rule, _load_xml(root), profile, namespaces, variables, parser_args)


def _result_changed(legacy: EvaluationOutcome, current: EvaluationOutcome) -> bool:
    if legacy.ok != current.ok:
        return True
    elif legacy.ok and current.ok:
        return [item.comparison_key for item in legacy.items] != \
            [item.comparison_key for item in current.items]
    return False


def _outcomes_differ(outcome1: EvaluationOutcome,
                     outcome2: EvaluationOutcome) -> bool:
    if outcome1.status != outcome2.status:
        return True
    elif not outcome1.ok:
        return outcome1.error_code != outcome2.error_code
    else:
        return [item.comparison_key for item in outcome1.items] != \
            [item.comparison_key for item in outcome2.items]


def _iter_comparison_records(outcomes: Mapping[str, EvaluationOutcome],
                             profile_names: Sequence[str]) -> Iterator[ComparisonRecord]:
    for profile_name in profile_names:
        outcome = outcomes.get(profile_name)
        if outcome is not None:
            yield from outcome.comparisons


def _detect_empty_vs_false(rule: str,
                           outcomes: Mapping[str, EvaluationOutcome],
                           legacy: EvaluationOutcome,
                           current: EvaluationOutcome) -> list[SpecialDifference]:
    differences: list[SpecialDifference] = []

    def add(detail: str, operands: tuple[str, ...]) -> None:
        if not differences:
            differences.append(SpecialDifference(
                kind=EMPTY_VS_FALSE,
                profile=CURRENT,
                detail=detail,
                operands=operands,
            ))

    if legacy.ok and current.ok:
        if legacy.is_empty_sequence and current.is_false_value:
            add('legacy evaluates to the empty sequence, current compatibility '
                'mode evaluates to false()', (rule,))
        elif legacy.is_false_value and current.is_empty_sequence:
            add('legacy evaluates to false(), current compatibility mode '
                'evaluates to the empty sequence', (rule,))

    # An explicit empty sequence compared with a boolean operand. This is the
    # case where XPath 1.0 compatibility rules convert () to false().
    if _result_changed(legacy, current):
        for record in _iter_comparison_records(outcomes, (CURRENT, LEGACY)):
            if record.has_explicit_empty_operand and record.has_boolean_operand:
                add(f'comparison {record.symbol!r} combines () with a boolean operand; '
                    f'legacy={_format_outcome(legacy)}, '
                    f'current={_format_outcome(current)}',
                    (record.left.display, record.right.display))
                break

    return differences


def _detect_numeric_vs_string(outcomes: Mapping[str, EvaluationOutcome],
                              legacy: EvaluationOutcome,
                              current: EvaluationOutcome) -> list[SpecialDifference]:
    differences: list[SpecialDifference] = []

    if not _result_changed(legacy, current):
        return differences

    for record in _iter_comparison_records(outcomes, (CURRENT, LEGACY, STRICT)):
        if record.mixes_numeric_and_string:
            differences.append(SpecialDifference(
                kind=NUMERIC_VS_STRING,
                profile=CURRENT,
                detail=f'comparison {record.symbol!r} mixes '
                       f'{record.left.display} with {record.right.display}',
                operands=(record.left.display, record.right.display),
            ))
            return differences
    return differences


def _compare_items(legacy: EvaluationOutcome,
                   current: EvaluationOutcome) -> list[ItemDifference]:
    differences: list[ItemDifference] = []
    if not legacy.ok or not current.ok:
        return differences

    for k, (item1, item2) in enumerate(zip(legacy.items, current.items), start=1):
        if item1.comparison_key == item2.comparison_key:
            continue
        elif item1.is_node != item2.is_node:
            kind = ITEM_TYPE
        elif item1.type_name != item2.type_name:
            kind = ITEM_TYPE
        elif item1.is_node and item1.path != item2.path:
            kind = NODE_IDENTITY
        elif item1.is_node and item1.xsd_type != item2.xsd_type:
            kind = ITEM_TYPE
        elif item1.is_node and item1.string_value != item2.string_value:
            kind = NODE_VALUE
        else:
            kind = ITEM_VALUE

        differences.append(ItemDifference(
            kind=kind,
            profile=CURRENT,
            position=k,
            legacy=item1.display,
            current=item2.display,
            detail=f'item {k}: legacy={item1.display}, current={item2.display}',
        ))
    return differences


def compare_evaluation(rule: str,
                       legacy: EvaluationOutcome,
                       current: EvaluationOutcome,
                       strict: Optional[EvaluationOutcome] = None,
                       outcomes: Optional[Mapping[str, EvaluationOutcome]] = None) -> \
        RuleComparison:
    """Compare the outcomes of a rule under the legacy and current profiles."""
    comparison = RuleComparison(rule=rule, legacy=legacy, current=current, strict=strict)
    if outcomes is None:
        outcomes = {LEGACY: legacy, CURRENT: current}

    if legacy.status != current.status:
        comparison.differences.append(RuleDifference(
            kind=ERROR_PHASE,
            legacy=legacy.status,
            current=current.status,
            detail=f'legacy status {legacy.status!r}, current status {current.status!r}',
        ))
    elif not legacy.ok and legacy.error_code != current.error_code:
        comparison.differences.append(RuleDifference(
            kind=ERROR_CODE,
            legacy=legacy.error_code or legacy.error_class or '',
            current=current.error_code or current.error_class or '',
            detail=f'legacy error {legacy.error_code}, current error {current.error_code}',
        ))

    if legacy.ok and current.ok:
        if len(legacy.items) != len(current.items):
            comparison.differences.append(RuleDifference(
                kind=ITEM_COUNT,
                legacy=str(len(legacy.items)),
                current=str(len(current.items)),
                detail=f'legacy count={len(legacy.items)}, '
                       f'current count={len(current.items)}',
            ))
        else:
            legacy_types = legacy.types
            current_types = current.types
            if legacy_types != current_types:
                comparison.differences.append(RuleDifference(
                    kind=ITEM_TYPE,
                    legacy=', '.join(legacy_types),
                    current=', '.join(current_types),
                    detail=f'legacy types=[{", ".join(legacy_types)}], '
                           f'current types=[{", ".join(current_types)}]',
                ))

            legacy_paths = legacy.paths
            current_paths = current.paths
            if legacy_types == current_types and \
                    len(legacy_paths) == len(legacy.items) and \
                    len(current_paths) == len(current.items) and \
                    legacy_paths != current_paths:
                comparison.differences.append(RuleDifference(
                    kind=ITEM_ORDER,
                    legacy=', '.join(legacy_paths),
                    current=', '.join(current_paths),
                    detail='same node count but different node order',
                ))

        comparison.item_differences.extend(_compare_items(legacy, current))

    comparison.special_differences.extend(
        _detect_empty_vs_false(rule, outcomes, legacy, current)
    )
    comparison.special_differences.extend(
        _detect_numeric_vs_string(outcomes, legacy, current)
    )

    if strict is not None and _outcomes_differ(strict, current):
        comparison.notes.append(
            f'strict XPath 2.0 result differs from current compatibility mode: '
            f'{_format_outcome(strict)} vs {_format_outcome(current)}'
        )

    return comparison


def compare_rule(rule: str,
                 root: Union[Any, str, bytes],
                 namespaces: Optional[Mapping[str, str]] = None,
                 variables: Optional[Mapping[str, Any]] = None,
                 parser_args: Optional[Mapping[str, Any]] = None,
                 with_strict: bool = True) -> RuleComparison:
    """Evaluate and compare a rule with the legacy, current and strict profiles."""
    root = _load_xml(root)
    outcomes = {
        LEGACY: evaluate_rule(rule, root, LEGACY, namespaces, variables, parser_args),
        CURRENT: evaluate_rule(rule, root, CURRENT, namespaces, variables, parser_args),
    }
    strict = evaluate_rule(rule, root, STRICT, namespaces, variables, parser_args) \
        if with_strict else None
    if strict is not None:
        outcomes[STRICT] = strict
    return compare_evaluation(rule, outcomes[LEGACY], outcomes[CURRENT], strict, outcomes)


def compare_rules(rules: Iterable[str],
                  root: Union[Any, str, bytes],
                  namespaces: Optional[Mapping[str, str]] = None,
                  variables: Optional[Mapping[str, Any]] = None,
                  parser_args: Optional[Mapping[str, Any]] = None,
                  with_strict: bool = True,
                  max_workers: Optional[int] = None) -> BatchReport:
    """
    Compare a batch of rules, preserving the input order.

    If *max_workers* is greater than one the rules are evaluated in parallel
    using a thread pool. The local XML sample is parsed once and shared in
    read-only mode; each evaluation builds its own parser and node tree.
    """
    root = _load_xml(root)
    rules = list(rules)
    if len(rules) <= 1 or max_workers == 1:
        return BatchReport(
            rules=[compare_rule(rule, root, namespaces, variables, parser_args,
                                with_strict) for rule in rules]
        )

    if max_workers is None:
        max_workers = min(32, (os.cpu_count() or 1) + 4, len(rules))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(compare_rule, rule, root, namespaces, variables,
                            parser_args, with_strict)
            for rule in rules
        ]
        return BatchReport(rules=[future.result() for future in futures])


def audit_rules(rules: Iterable[str],
                source: Union[Any, str, bytes],
                namespaces: Optional[Mapping[str, str]] = None,
                variables: Optional[Mapping[str, Any]] = None,
                parser_args: Optional[Mapping[str, Any]] = None,
                with_strict: bool = True,
                max_workers: Optional[int] = None) -> BatchReport:
    """
    Audit a batch of rules against a local XML sample, without collapsing
    results to strings. This is the batch entry point used by the CLI.
    """
    root = _load_xml(source)
    report = compare_rules(rules, root, namespaces, variables, parser_args,
                           with_strict, max_workers)
    if isinstance(source, str) and not source.lstrip().startswith('<'):
        report.source = source
    return report


def _format_outcome(outcome: Optional[EvaluationOutcome]) -> str:
    if outcome is None:
        return '-'
    elif not outcome.ok:
        return f'{outcome.status}:{outcome.error_code or outcome.error_class}'
    elif outcome.sequence_is_empty:
        return 'empty-sequence()'
    elif len(outcome.items) == 1:
        return outcome.items[0].display
    else:
        return '[' + ', '.join(item.display for item in outcome.items) + ']'


def format_report(report: BatchReport, verbose: bool = False) -> str:
    """Format a batch report as a human-readable text block."""
    lines = [
        f"rules: {len(report.rules)}",
        f"incompatible: {len(report.incompatible_rules)}",
    ]
    for category, count in sorted(report.category_counts.items()):
        lines.append(f'  {category}: {count}')
    for category, count in sorted(report.special_counts.items()):
        lines.append(f'  special/{category}: {count}')

    for rule in report.rules:
        if not verbose and not rule.incompatible:
            continue

        lines.append('')
        lines.append(f'rule: {rule.rule}')
        lines.append(f'  legacy : {_format_outcome(rule.legacy)}')
        lines.append(f'  current: {_format_outcome(rule.current)}')
        if rule.strict is not None:
            lines.append(f'  strict : {_format_outcome(rule.strict)}')

        for diff in rule.differences:
            lines.append(f'  [{diff.kind}] {diff.detail}')
        for diff in rule.item_differences:
            lines.append(f'  [{diff.kind}@{diff.position}] {diff.detail}')
        for diff in rule.special_differences:
            label = f'{diff.kind}: {diff.label}'
            lines.append(f'  [{label}] {diff.detail}')
        for note in rule.notes:
            lines.append(f'  note: {note}')
    return '\n'.join(lines)


def _read_rules(path: str) -> list[str]:
    with open(path, encoding='utf-8') as fp:
        if path.endswith('.json'):
            data = json.load(fp)
            if isinstance(data, dict):
                data = data.get('rules', data.get('xpath_rules', []))
            if not isinstance(data, list):
                raise ValueError('a JSON rules file must contain a list of strings')
            return [str(rule) for rule in data]

        return [line.strip() for line in fp
                if line.strip() and not line.lstrip().startswith('#')]


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description='Audit XPath rules across legacy and compatibility parser profiles.'
    )
    parser.add_argument('rules', help='a text or JSON file with the XPath rules')
    parser.add_argument('sample', help='a local XML sample file')
    parser.add_argument('-o', '--output', help='write a JSON report to this file')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='print also the rules without differences')
    parser.add_argument('--namespace', action='append', default=[],
                        metavar='PREFIX=URI', help='add a namespace mapping')
    parser.add_argument('-j', '--jobs', type=int, default=None,
                        help='number of parallel rule evaluations')
    args = parser.parse_args(argv)

    namespaces: dict[str, str] = {}
    for item in args.namespace:
        prefix, sep, uri = item.partition('=')
        if not sep:
            parser.error(f'invalid namespace mapping {item!r}')
        namespaces[prefix] = uri

    report = audit_rules(_read_rules(args.rules), args.sample, namespaces,
                         max_workers=args.jobs)
    text = format_report(report, verbose=args.verbose)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as fp:
            json.dump(report.to_dict(), fp, indent=2, ensure_ascii=False)
            fp.write('\n')
    else:
        sys.stdout.write(text + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
