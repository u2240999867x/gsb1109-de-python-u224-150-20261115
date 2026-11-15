"""
Amount collection from XML documents through XPath expressions.

The collection rule is made of two XPath expressions: a *row path* that selects
the repeated record nodes and a *field path* that, evaluated with a row node as
the context item, selects the amount node of each record. An optional *value
expression* (default ``xs:decimal(.)``) performs the explicit type conversion
of the selected node.

Three failure conditions are handled separately and never abort the processing
of the following rows:

* empty sequence -- the row has no matching amount node (a missing or empty
  field);
* invalid number -- the type conversion fails (e.g. ``xs:decimal('abc')``);
* multi-value -- more than one node matches the field path.

Legal numeric zeros are kept distinct from empty sequences: only rows that
actually produce a value contribute to the summary denominator.

The original XML nodes are never modified. Source line numbers are recorded in
a side mapping by :class:`LineNumberingParser` instead of being written on the
elements, and each result/error carries both the XPath rule position (the
failing token) and the XML node location (source line and node path).
"""
import argparse
import json
import os
import xml.parsers.expat as expat
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Union
from xml.etree import ElementTree as ET

from elementpath.exceptions import ElementPathError, ElementPathValueError, \
    XMLResourceForbidden
from elementpath.tree_builders import get_node_tree
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath_context import XPathContext
from elementpath.xpath_nodes import AttributeNode, ElementNode, XPathNode

DEFAULT_VALUE_EXPRESSION = 'xs:decimal(.)'

# XPath error codes raised by numeric constructors/casts on bad lexical values.
INVALID_NUMBER_CODES = frozenset({
    'FORG0001',  # invalid value for cast/constructor
    'FOCA0001',  # input value too large for decimal
    'FOCA0002',  # invalid lexical value
    'FOCA0003',  # input value too large for integer
    'FOCA0005',  # NaN supplied as float/double value
    'FOCA0006',  # too many digits of precision for decimal
})

STATUS_VALUE = 'value'
STATUS_EMPTY = 'empty'
STATUS_ERROR = 'error'

ERROR_INVALID_NUMBER = 'invalid-number'
ERROR_MULTI_VALUE = 'multi-value'
ERROR_EXPRESSION = 'expression-error'


class LineNumberingParser:
    """An ElementTree-compatible parser that records each element's source line.

    Built on pyexpat plus the standard :class:`~xml.etree.ElementTree.TreeBuilder`,
    because the C-accelerated :class:`~xml.etree.ElementTree.XMLParser` does not
    expose the expat callbacks. Line numbers are stored in :attr:`source_lines`,
    keyed by the parsed element objects, so the XML tree itself is left untouched.

    Entity declarations and external entity references are rejected, matching the
    package's safe parsing policy.
    """

    def __init__(self, encoding: Optional[str] = None) -> None:
        self.source_lines: dict[ET.Element, int] = {}
        self.entity: dict[str, Any] = {}
        self.version = 'Expat %d.%d.%d' % expat.version_info

        self._parser = expat.ParserCreate(encoding, '}')
        self._target = ET.TreeBuilder()
        self._parser.StartElementHandler = self._start
        self._parser.EndElementHandler = self._target.end
        self._parser.CharacterDataHandler = self._target.data
        if hasattr(self._target, 'comment'):
            self._parser.CommentHandler = self._target.comment
        if hasattr(self._target, 'pi'):
            self._parser.ProcessingInstructionHandler = self._target.pi
        self._parser.buffer_text = 1

        self._parser.EntityDeclHandler = self._forbid_entity_declaration
        self._parser.UnparsedEntityDeclHandler = self._forbid_unparsed_entity
        self._parser.ExternalEntityRefHandler = self._forbid_external_reference

    @staticmethod
    def _forbid_entity_declaration(entity_name: str, *args: Any) -> None:
        raise XMLResourceForbidden(
            'Entities are forbidden (entity_name=%r)' % entity_name)

    @staticmethod
    def _forbid_unparsed_entity(entity_name: str, *args: Any) -> None:
        raise XMLResourceForbidden(
            'Unparsed entities are forbidden (entity_name=%r)' % entity_name)

    @staticmethod
    def _forbid_external_reference(context: object, base: str,
                                   system_id: str, public_id: str) -> None:
        raise XMLResourceForbidden(
            'External references are forbidden (system_id=%r, public_id=%r)'
            % (system_id, public_id))

    def _start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        # expat reports Clark names without the leading '{' when a namespace
        # separator is configured; restore the canonical ElementTree form.
        if '}' in tag and not tag.startswith('{'):
            tag = '{' + tag
        element = self._target.start(tag, attrs)
        self.source_lines[element] = self._parser.CurrentLineNumber
        return element

    def feed(self, data: Union[str, bytes]) -> None:
        self._parser.Parse(data, False)

    def close(self) -> ET.Element:
        self._parser.Parse(b'', True)
        return self._target.close()


@dataclass
class ParsedDocument:
    """An ElementTree paired with the side mapping of source line numbers."""
    tree: ET.ElementTree
    source_lines: dict[ET.Element, int]


@dataclass(frozen=True)
class RuleLocation:
    """Location of a node in the XML document or of a token in an XPath rule."""
    line: Optional[int]
    path: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {'line': self.line, 'path': self.path, 'kind': self.kind}


@dataclass(frozen=True)
class AmountCollectionError:
    """A per-row collection error."""
    kind: str
    message: str
    code: Optional[str]
    rule: Optional[RuleLocation]
    node: RuleLocation
    matched_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            'kind': self.kind,
            'message': self.message,
            'code': self.code,
            'rule': None if self.rule is None else self.rule.to_dict(),
            'node': self.node.to_dict(),
            'matched_paths': list(self.matched_paths),
        }


@dataclass(frozen=True)
class AmountValue:
    """A collected amount together with the XML node it comes from."""
    value: Decimal
    node: RuleLocation
    element: ET.Element

    def to_dict(self) -> dict[str, Any]:
        return {'value': str(self.value), 'node': self.node.to_dict()}


@dataclass
class AmountRowResult:
    """The outcome of the collection for a single row."""
    index: int
    status: str
    location: Optional[RuleLocation]
    row_element: Optional[ET.Element]
    value: Optional[AmountValue] = None
    errors: list[AmountCollectionError] = field(default_factory=list)

    @property
    def is_value(self) -> bool:
        return self.status == STATUS_VALUE

    @property
    def is_empty(self) -> bool:
        return self.status == STATUS_EMPTY

    @property
    def is_error(self) -> bool:
        return self.status == STATUS_ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            'index': self.index,
            'status': self.status,
            'location': None if self.location is None else self.location.to_dict(),
            'value': None if self.value is None else self.value.to_dict(),
            'errors': [err.to_dict() for err in self.errors],
        }


@dataclass
class CollectionSummary:
    """Aggregate counters; zeros and empty sequences stay separate."""
    total_rows: int = 0
    value_rows: int = 0
    zero_value_rows: int = 0
    empty_rows: int = 0
    error_rows: int = 0
    total: Decimal = Decimal(0)

    @property
    def denominator(self) -> int:
        """Rows that produced a value (zeros included, empties excluded)."""
        return self.value_rows

    @property
    def average(self) -> Optional[Decimal]:
        if self.value_rows == 0:
            return None
        return self.total / Decimal(self.value_rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            'total_rows': self.total_rows,
            'value_rows': self.value_rows,
            'zero_value_rows': self.zero_value_rows,
            'empty_rows': self.empty_rows,
            'error_rows': self.error_rows,
            'total': str(self.total),
            'denominator': self.denominator,
            'average': None if self.average is None else str(self.average),
        }


@dataclass
class AmountCollectionReport:
    rows: list[AmountRowResult]
    summary: CollectionSummary

    @property
    def values(self) -> list[AmountValue]:
        return [row.value for row in self.rows if row.value is not None]

    @property
    def errors(self) -> list[AmountCollectionError]:
        return [err for row in self.rows for err in row.errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            'rows': [row.to_dict() for row in self.rows],
            'summary': self.summary.to_dict(),
        }


class AmountCollector:
    """Collect and convert amounts from an XML document row by row."""

    def __init__(self,
                 row_path: str,
                 field_path: str,
                 value_expression: str = DEFAULT_VALUE_EXPRESSION,
                 namespaces: Optional[dict[str, str]] = None,
                 parser_class: type[XPath2Parser] = XPath2Parser,
                 **parser_kwargs: Any) -> None:
        self.row_path = row_path
        self.field_path = field_path
        self.value_expression = value_expression
        self.namespaces = dict(namespaces) if namespaces else {}

        parser = parser_class(self.namespaces, **parser_kwargs)
        self.row_token = parser.parse(row_path)
        self.field_token = parser.parse(field_path)
        self.value_token = parser.parse(value_expression)

    def collect(self,
                source: Any,
                source_lines: Optional[dict[ET.Element, int]] = None
                ) -> AmountCollectionReport:
        node_tree, line_map = self._build_node_tree(source, source_lines)
        rows = self._select_rows(node_tree)

        results: list[AmountRowResult] = []
        summary = CollectionSummary(total_rows=len(rows))

        for index, row_node in enumerate(rows, start=1):
            row_location = self._node_location(row_node, line_map)
            row_element = row_node.value if isinstance(row_node, ElementNode) else None
            result = AmountRowResult(
                index=index,
                status=STATUS_EMPTY,
                location=row_location,
                row_element=row_element,
            )
            try:
                self._process_row(node_tree, line_map, row_node, result)
            except ElementPathError as exc:
                result.status = STATUS_ERROR
                result.errors.append(self._expression_error(exc, row_location))
            except Exception as exc:  # pragma: no cover - defensive safety net
                # A single row must never abort the processing of the others.
                result.status = STATUS_ERROR
                result.errors.append(AmountCollectionError(
                    kind=ERROR_EXPRESSION,
                    message='%s: %s' % (type(exc).__name__, exc),
                    code=None,
                    rule=None,
                    node=row_location,
                ))

            self._update_summary(summary, result)
            results.append(result)

        return AmountCollectionReport(rows=results, summary=summary)

    # Helpers ----------------------------------------------------------------

    @staticmethod
    def _build_node_tree(source: Any,
                         source_lines: Optional[dict[ET.Element, int]]
                         ) -> tuple[XPathNode, dict[Any, int]]:
        if isinstance(source, ParsedDocument):
            return get_node_tree(source.tree), dict(source.source_lines)
        if isinstance(source, XPathNode):
            line_map = {} if source_lines is None else dict(source_lines)
            return source, line_map
        if isinstance(source, (str, bytes, os.PathLike)):
            document = parse_xml(source)
            return get_node_tree(document.tree), dict(document.source_lines)
        line_map = {} if source_lines is None else dict(source_lines)
        return get_node_tree(source), line_map

    def _select_rows(self, node_tree: XPathNode) -> list[XPathNode]:
        context = XPathContext(node_tree, namespaces=self.namespaces)
        return [item for item in self.row_token.select(context)
                if isinstance(item, XPathNode)]

    def _process_row(self,
                     node_tree: XPathNode,
                     line_map: dict[Any, int],
                     row_node: XPathNode,
                     result: AmountRowResult) -> None:
        field_context = XPathContext(node_tree, namespaces=self.namespaces,
                                     item=row_node)
        field_items = [item for item in self.field_token.select(field_context)
                       if isinstance(item, XPathNode)]

        if not field_items:
            result.status = STATUS_EMPTY
            return

        if len(field_items) > 1:
            result.status = STATUS_ERROR
            result.errors.append(self._multi_value_error(field_items, line_map))
            return

        field_node = field_items[0]
        node_location = self._node_location(field_node, line_map)

        # An existing node whose value is empty/whitespace is an empty sequence
        # for conversion purposes, distinct from a present but illegal number.
        if not field_node.string_value.strip():
            result.status = STATUS_EMPTY
            return

        value_context = XPathContext(node_tree, namespaces=self.namespaces,
                                     item=field_node)
        try:
            raw_results = list(self.value_token.select(value_context))
        except ElementPathError as exc:
            result.status = STATUS_ERROR
            result.errors.append(self._conversion_error(exc, node_location))
            return

        if not raw_results:
            result.status = STATUS_ERROR
            result.errors.append(AmountCollectionError(
                kind=ERROR_EXPRESSION,
                message='value expression returned an empty sequence',
                code=None,
                rule=self._token_location(self.value_token),
                node=node_location,
            ))
            return

        if len(raw_results) > 1:
            result.status = STATUS_ERROR
            result.errors.append(AmountCollectionError(
                kind=ERROR_MULTI_VALUE,
                message='value expression returned %d items, a single amount '
                        'is required' % len(raw_results),
                code=None,
                rule=self._token_location(self.value_token),
                node=node_location,
            ))
            return

        try:
            amount = self._to_decimal(raw_results[0])
        except ElementPathError as exc:
            result.status = STATUS_ERROR
            result.errors.append(self._conversion_error(exc, node_location))
            return

        result.status = STATUS_VALUE
        result.value = AmountValue(
            value=amount,
            node=node_location,
            element=field_node.value if isinstance(field_node, ElementNode)
            else field_node.parent.value,
        )

    @staticmethod
    def _to_decimal(item: Any) -> Decimal:
        if isinstance(item, bool) or not isinstance(item, (int, float, Decimal)):
            raise ElementPathValueError(
                'value expression produced a non-numeric result: %r' % (item,)
            )
        if isinstance(item, Decimal):
            decimal_value = item
        elif isinstance(item, int):
            decimal_value = Decimal(item)
        else:
            if item != item or item in (float('inf'), float('-inf')):
                raise ElementPathValueError(
                    'value expression produced a non-finite number: %r' % (item,)
                )
            decimal_value = Decimal(str(item))
        return decimal_value

    def _multi_value_error(self,
                           field_items: list[XPathNode],
                           line_map: dict[Any, int]) -> AmountCollectionError:
        locations = [self._node_location(node, line_map) for node in field_items]
        return AmountCollectionError(
            kind=ERROR_MULTI_VALUE,
            message='field path matched %d nodes, a single amount node is '
                    'required' % len(field_items),
            code=None,
            rule=self._token_location(self.field_token),
            node=locations[0],
            matched_paths=tuple(location.path for location in locations),
        )

    def _conversion_error(self,
                          exc: ElementPathError,
                          node_location: RuleLocation) -> AmountCollectionError:
        kind = ERROR_INVALID_NUMBER if exc.code and \
            exc.code.split(':')[-1] in INVALID_NUMBER_CODES else ERROR_EXPRESSION
        return AmountCollectionError(
            kind=kind,
            message=exc.message,
            code=exc.code,
            rule=self._token_location(exc.token),
            node=node_location,
        )

    def _expression_error(self,
                          exc: ElementPathError,
                          node_location: RuleLocation) -> AmountCollectionError:
        return AmountCollectionError(
            kind=ERROR_EXPRESSION,
            message=exc.message,
            code=exc.code,
            rule=self._token_location(exc.token),
            node=node_location,
        )

    def _node_location(self,
                       node: XPathNode,
                       line_map: dict[Any, int]) -> RuleLocation:
        kind = getattr(node, 'node_kind', 'node')
        if isinstance(node, AttributeNode):
            owner = node.parent
            element = owner.value if isinstance(owner, ElementNode) else None
            return RuleLocation(line=line_map.get(element),
                                path=node.path, kind='attribute')

        if isinstance(node, ElementNode):
            return RuleLocation(line=line_map.get(node.value),
                                path=node.path, kind=kind)

        # text/comment/processing-instruction nodes: fall back to the
        # source line of the owning element.
        owner = getattr(node, 'parent', None)
        line = line_map.get(owner.value) if isinstance(owner, ElementNode) \
            else None
        return RuleLocation(line=line, path=node.path, kind=kind)

    @staticmethod
    def _token_location(token: Any) -> Optional[RuleLocation]:
        if token is None or not hasattr(token, 'position'):
            return None
        line, column = token.position
        return RuleLocation(line=line, path='%s (column %d)' % (token, column),
                            kind='xpath-token')

    @staticmethod
    def _update_summary(summary: CollectionSummary,
                        result: AmountRowResult) -> None:
        if result.status == STATUS_VALUE:
            assert result.value is not None
            summary.value_rows += 1
            summary.total += result.value.value
            if result.value.value == 0:
                summary.zero_value_rows += 1
        elif result.status == STATUS_EMPTY:
            summary.empty_rows += 1
        else:
            summary.error_rows += 1


def parse_xml(source: Union[str, bytes, os.PathLike[str], Any]) -> ParsedDocument:
    """Parse *source* recording element source lines without touching nodes.

    *source* may be a file path, a file-like object, an XML byte string or,
    when it does not point to an existing file, an XML text string.
    """
    parser = LineNumberingParser()
    if isinstance(source, (str, os.PathLike)) and os.path.isfile(source):
        tree = ET.parse(source, parser=parser)
    elif isinstance(source, (bytes, str)):
        tree = ET.ElementTree(ET.fromstring(source, parser=parser))
    else:
        tree = ET.parse(source, parser=parser)
    return ParsedDocument(tree=tree, source_lines=dict(parser.source_lines))


def collect_amounts(source: Any,
                    row_path: str,
                    field_path: str,
                    value_expression: str = DEFAULT_VALUE_EXPRESSION,
                    namespaces: Optional[dict[str, str]] = None,
                    source_lines: Optional[dict[ET.Element, int]] = None
                    ) -> AmountCollectionReport:
    """Convenience wrapper around :class:`AmountCollector`."""
    collector = AmountCollector(
        row_path=row_path,
        field_path=field_path,
        value_expression=value_expression,
        namespaces=namespaces,
    )
    return collector.collect(source, source_lines=source_lines)


def _location_text(location: Optional[RuleLocation]) -> str:
    if location is None:
        return '-'
    if location.line is None:
        return location.path
    return 'line %d %s' % (location.line, location.path)


def _format_report(report: AmountCollectionReport) -> str:
    lines: list[str] = []
    for row in report.rows:
        if row.is_value:
            assert row.value is not None
            lines.append('row %d [value] %s = %s'
                         % (row.index, _location_text(row.value.node),
                            row.value.value))
        elif row.is_empty:
            lines.append('row %d [empty] %s'
                         % (row.index, _location_text(row.location)))
        else:
            for err in row.errors:
                rule = '-' if err.rule is None else err.rule.path
                lines.append(
                    'row %d [error:%s] node=%s rule=%s code=%s %s'
                    % (row.index, err.kind, _location_text(err.node), rule,
                       err.code or '-', err.message)
                )
    summary = report.summary
    lines.append('---')
    lines.append('rows=%d values=%d (zeros=%d) empty=%d errors=%d total=%s '
                 'denominator=%d average=%s'
                 % (summary.total_rows, summary.value_rows,
                    summary.zero_value_rows, summary.empty_rows,
                    summary.error_rows, summary.total, summary.denominator,
                    summary.average if summary.average is not None else '-'))
    return '\n'.join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Collect amounts from an XML file.')
    parser.add_argument('xml_file', help='path to the XML document')
    parser.add_argument('--row-path', required=True,
                        help='XPath selecting the repeated row nodes')
    parser.add_argument('--field-path', required=True,
                        help='XPath (relative to a row) selecting the amount node')
    parser.add_argument('--value-expression', default=DEFAULT_VALUE_EXPRESSION,
                        help='XPath type-conversion expression applied to the field')
    parser.add_argument('--json', action='store_true',
                        help='emit the report as JSON instead of text')
    args = parser.parse_args(argv)

    document = parse_xml(args.xml_file)
    report = collect_amounts(
        document,
        row_path=args.row_path,
        field_path=args.field_path,
        value_expression=args.value_expression,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_format_report(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
