#!/usr/bin/env python
#
# Copyright (c), 2018-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import io
import json
import tempfile
import unittest
import pathlib

from elementpath import XPath1Parser, XPath2Parser
from elementpath.xpath_compat import EvaluationProfile, ResultItem, \
    EvaluationOutcome, compare_rule, audit_rules, \
    compatibility_profiles, evaluate_rule, format_report, main, \
    EMPTY_VS_FALSE, NUMERIC_VS_STRING, ITEM_COUNT, ITEM_ORDER, ITEM_TYPE, \
    COMPILE_ERROR, TYPE_ERROR, RUNTIME_ERROR, NODE_IDENTITY, NODE_VALUE

XML_SAMPLE = """\
<root>
    <item id="1">10</item>
    <item id="2">2</item>
    <item id="3">x</item>
    <item id="4"></item>
</root>"""


class XPathCompatibilityAuditTest(unittest.TestCase):

    def test_profiles(self):
        profiles = compatibility_profiles()
        self.assertEqual([p.name for p in profiles], ['legacy', 'current', 'strict'])
        self.assertIs(profiles[0].parser_class, XPath1Parser)
        self.assertIs(profiles[1].parser_class, XPath2Parser)
        self.assertTrue(profiles[1].compatibility_mode)
        self.assertFalse(profiles[2].compatibility_mode)

        profile = EvaluationProfile('custom', XPath2Parser, True, '2.0')
        self.assertEqual(str(profile), 'custom')

    def test_typed_results_are_not_stringified(self):
        legacy = evaluate_rule('//item[1]', XML_SAMPLE, 'legacy')
        current = evaluate_rule('//item[1]', XML_SAMPLE, 'current')
        self.assertTrue(legacy.ok and current.ok)
        self.assertEqual(len(legacy.items), 1)
        self.assertTrue(legacy.items[0].is_node)
        self.assertEqual(legacy.items[0].node_kind, 'element')
        self.assertEqual(legacy.items[0].path, '/Q{}root[1]/Q{}item[1]')
        self.assertEqual(legacy.items[0].name, 'item')

        integer = evaluate_rule('1', XML_SAMPLE, 'current')
        self.assertEqual(integer.items[0].type_name, 'xs:integer')
        self.assertEqual(integer.items[0].value_repr, '1')

        decimal = evaluate_rule('1.0', XML_SAMPLE, 'current')
        self.assertEqual(decimal.items[0].type_name, 'xs:decimal')
        self.assertNotEqual(decimal.items[0].identity, integer.items[0].identity)

        boolean = evaluate_rule('false()', XML_SAMPLE, 'current')
        self.assertTrue(boolean.is_false_value)
        self.assertEqual(boolean.items[0].type_name, 'xs:boolean')

        empty = evaluate_rule('//missing', XML_SAMPLE, 'current')
        self.assertTrue(empty.is_empty_sequence)
        self.assertEqual(empty.items, [])
        self.assertNotEqual(empty.items, [ResultItem('xs:boolean', 'False')])

    def test_node_identity_is_preserved(self):
        legacy = evaluate_rule('//item', XML_SAMPLE, 'legacy')
        current = evaluate_rule('//item', XML_SAMPLE, 'current')
        self.assertEqual(
            [item.path for item in legacy.items],
            ['/Q{}root[1]/Q{}item[1]', '/Q{}root[1]/Q{}item[2]',
             '/Q{}root[1]/Q{}item[3]', '/Q{}root[1]/Q{}item[4]']
        )
        self.assertEqual([item.identity for item in legacy.items],
                         [item.identity for item in current.items])

    def test_compile_error_category(self):
        comparison = compare_rule('//item[. castable as xs:integer]', XML_SAMPLE)
        self.assertIn('error-phase', comparison.categories)
        self.assertEqual(comparison.legacy.status, COMPILE_ERROR)
        self.assertEqual(comparison.current.status, 'ok')
        self.assertTrue(comparison.legacy.error_code.endswith('XPST0003'))

        comparison = compare_rule('//item[. eq 2]', XML_SAMPLE)
        self.assertEqual(comparison.legacy.status, COMPILE_ERROR)
        self.assertEqual(comparison.current.status, TYPE_ERROR)
        self.assertIn('error-phase', comparison.categories)

    def test_type_error_category(self):
        comparison = compare_rule('starts-with("1999", 19)', XML_SAMPLE)
        self.assertEqual(comparison.legacy.status, 'ok')
        self.assertEqual(comparison.current.status, 'ok')
        self.assertEqual(comparison.strict.status, TYPE_ERROR)
        self.assertTrue(comparison.strict.error_code.endswith('XPTY0004'))
        self.assertTrue(any('strict XPath 2.0' in note for note in comparison.notes))

        comparison = compare_rule('sum(//item)', XML_SAMPLE)
        self.assertIn('error-phase', comparison.categories)
        self.assertEqual(comparison.legacy.items[0].type_name, 'xs:double')
        self.assertEqual(comparison.current.status, TYPE_ERROR)
        self.assertTrue(comparison.current.error_code.endswith('FORG0006'))

    def test_count_difference_category(self):
        comparison = compare_rule('//item[@id = 2]', XML_SAMPLE)
        self.assertIn(ITEM_COUNT, comparison.categories)
        self.assertFalse(comparison.legacy.items)
        self.assertEqual(len(comparison.current.items), 1)
        self.assertEqual(comparison.current.items[0].path,
                         '/Q{}root[1]/Q{}item[2]')

        comparison = compare_rule('//missing', XML_SAMPLE)
        self.assertFalse(comparison.incompatible)
        self.assertEqual(comparison.legacy.count, 0)
        self.assertEqual(comparison.current.count, 0)

    def test_order_difference_category(self):
        from elementpath import XPathContext
        from elementpath.xpath_nodes import EtreeElementNode

        from xml.etree import ElementTree
        root = XPathContext(ElementTree.XML(XML_SAMPLE)).root
        assert isinstance(root, EtreeElementNode)

        elements = [node for node in root.children
                    if node.node_kind == 'element']
        order = [elements[2], elements[0], elements[1]]
        context_variables = {'seq': order}

        comparison = compare_rule('$seq', XML_SAMPLE, variables=context_variables)
        self.assertFalse(comparison.incompatible)
        self.assertEqual([item.path for item in comparison.legacy.items],
                         [item.path for item in comparison.current.items])

        # Directly exercise the order detector with two outcomes that have the
        # same item types and count but a different node order.
        from elementpath.xpath_compat import compare_evaluation
        legacy = evaluation_order(elements[1], elements[0])
        current = evaluation_order(elements[0], elements[1])
        result = compare_evaluation('$seq', legacy, current)
        self.assertIn(ITEM_ORDER, result.categories)
        self.assertEqual(result.differences[0].kind, ITEM_ORDER)

    def test_type_difference_category(self):
        from elementpath.xpath_compat import compare_evaluation
        legacy = evaluation_item(ResultItem('xs:integer', '1'))
        current = evaluation_item(ResultItem('xs:decimal', '1'))
        result = compare_evaluation('1', legacy, current)
        self.assertIn(ITEM_TYPE, result.categories)

        comparison = compare_rule('//item[1]/@id = 1', XML_SAMPLE)
        self.assertIn('value', comparison.categories)
        self.assertFalse(comparison.legacy.items[0].value_repr ==
                         comparison.current.items[0].value_repr)

    def test_numeric_vs_string_bucket(self):
        comparison = compare_rule('//item = 2', XML_SAMPLE)
        kinds = {d.kind for d in comparison.special_differences}
        self.assertIn(NUMERIC_VS_STRING, kinds)
        difference = next(d for d in comparison.special_differences
                          if d.kind == NUMERIC_VS_STRING)
        self.assertIn('xs:untypedAtomic', difference.operands)
        self.assertIn('xs:integer', difference.operands)

        comparison = compare_rule('//item[1] = "10"', XML_SAMPLE)
        self.assertNotIn(NUMERIC_VS_STRING,
                         {d.kind for d in comparison.special_differences})

    def test_empty_vs_false_bucket(self):
        comparison = compare_rule('() = false()', XML_SAMPLE)
        kinds = {d.kind for d in comparison.special_differences}
        self.assertIn(EMPTY_VS_FALSE, kinds)
        self.assertEqual(comparison.legacy.status, COMPILE_ERROR)
        self.assertFalse(comparison.current.items[0].value_repr == 'empty')

        comparison = compare_rule('//missing', XML_SAMPLE)
        self.assertNotIn(EMPTY_VS_FALSE,
                         {d.kind for d in comparison.special_differences})

    def test_special_categories_are_reported_separately(self):
        report = audit_rules([
            '//item = 2',
            '() = false()',
            '//item',
            'count(//item)',
        ], XML_SAMPLE)
        summary = report.to_dict()['summary']
        self.assertEqual(summary['special'][NUMERIC_VS_STRING], 1)
        self.assertEqual(summary['special'][EMPTY_VS_FALSE], 1)
        self.assertNotIn(EMPTY_VS_FALSE,
                         {d.kind for r in report.rules
                          if r.rule == '//item = 2' for d in r.special_differences})

    def test_batch_report(self):
        rules = ['//item', '//item[@id = 2]', '//item = 2']
        report = audit_rules(rules, XML_SAMPLE)
        self.assertEqual(len(report.rules), 3)
        self.assertEqual(len(report.incompatible_rules), 2)
        self.assertEqual([r.rule for r in report.rules], rules)
        self.assertEqual(report.category_counts[ITEM_COUNT], 1)
        self.assertEqual(report.special_counts[NUMERIC_VS_STRING], 2)
        self.assertIsNone(report.source)

        data = report.to_dict()
        json.dumps(data)
        self.assertEqual(data['summary']['rules'], 3)

        parallel_report = audit_rules(rules, XML_SAMPLE, max_workers=3)
        self.assertEqual([r.rule for r in parallel_report.rules], rules)
        self.assertEqual([r.categories for r in parallel_report.rules],
                         [r.categories for r in report.rules])

    def test_invalid_sample(self):
        with self.assertRaises(ValueError):
            audit_rules(['//item'], '<broken>')
        with self.assertRaises(ValueError):
            audit_rules(['//item'], '/no/such/sample.xml')

    def test_audit_from_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            rules_path = pathlib.Path(tmp_dir) / 'rules.txt'
            sample_path = pathlib.Path(tmp_dir) / 'sample.xml'
            output_path = pathlib.Path(tmp_dir) / 'report.json'

            rules_path.write_text(
                '# comment\n//item\n//item = 2\n\n() = false()\n', encoding='utf-8'
            )
            sample_path.write_text(XML_SAMPLE, encoding='utf-8')

            report = audit_rules(
                [line.strip() for line in rules_path.read_text().splitlines()
                 if line.strip() and not line.startswith('#')],
                str(sample_path)
            )
            self.assertEqual(len(report.rules), 3)
            self.assertEqual(report.source, str(sample_path))

            self.assertEqual(main([str(rules_path), str(sample_path),
                                   '-o', str(output_path)]), 0)
            data = json.loads(output_path.read_text(encoding='utf-8'))
            self.assertEqual(data['summary']['rules'], 3)

            stdout = io.StringIO()
            text = format_report(report, verbose=True)
            self.assertIn('numeric vs string equality', text)
            self.assertIn('empty sequence vs false()', text)
            self.assertEqual(stdout.getvalue(), '')

    def test_json_rules_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            rules_path = pathlib.Path(tmp_dir) / 'rules.json'
            rules_path.write_text(json.dumps({'rules': ['//item', '//item = 2']}),
                                  encoding='utf-8')
            from elementpath.xpath_compat import _read_rules
            self.assertEqual(_read_rules(str(rules_path)), ['//item', '//item = 2'])

    def test_missing_difference_does_not_hide_incompatibility(self):
        comparison = compare_rule('count(//item)', XML_SAMPLE)
        self.assertFalse(comparison.incompatible)
        self.assertEqual(comparison.legacy.items[0].type_name, 'xs:integer')
        self.assertEqual(comparison.current.items[0].type_name, 'xs:integer')

        comparison = compare_rule('//item = 2', XML_SAMPLE)
        self.assertTrue(comparison.incompatible)
        self.assertNotEqual(comparison.legacy.items[0].value_repr,
                            comparison.current.items[0].value_repr)
        self.assertNotEqual(comparison.legacy.items[0].identity,
                            comparison.current.items[0].identity)

    def test_strict_profile_is_reported(self):
        comparison = compare_rule('false() = //missing', XML_SAMPLE)
        self.assertIsNotNone(comparison.strict)
        self.assertEqual(comparison.strict.profile, 'strict')
        self.assertTrue(any('strict XPath 2.0' in note for note in comparison.notes))

    def test_runtime_error_category(self):
        comparison = compare_rule('//item[. = 2]', XML_SAMPLE)
        self.assertEqual(comparison.current.status, RUNTIME_ERROR)
        self.assertTrue(comparison.current.error_code.endswith('FORG0001'))
        self.assertIn('error-phase', comparison.categories)

    def test_node_value_and_identity_differences(self):
        from elementpath.xpath_compat import compare_evaluation

        outcome = evaluation_item(ResultItem(
            'node:element', "<Element 'item' at 0x1>", is_node=True,
            node_kind='element', path='/Q{}root[1]/Q{}item[1]', name='item',
            string_value='1'))
        other_value = evaluation_item(ResultItem(
            'node:element', "<Element 'item' at 0x2>", is_node=True,
            node_kind='element', path='/Q{}root[1]/Q{}item[1]', name='item',
            string_value='2'))
        other_path = evaluation_item(ResultItem(
            'node:element', "<Element 'item' at 0x3>", is_node=True,
            node_kind='element', path='/Q{}root[1]/Q{}item[2]', name='item',
            string_value='1'))

        result = compare_evaluation('//item', outcome, other_value)
        self.assertIn(NODE_VALUE, result.categories)
        result = compare_evaluation('//item', outcome, other_path)
        self.assertIn(NODE_IDENTITY, result.categories)

    def test_parser_args_and_namespaces(self):
        comparison = compare_rule('. = "1"', '<root>1</root>',
                                  namespaces={'eg': 'http://www.example.com/ns'})
        self.assertFalse(comparison.incompatible)

        comparison = compare_rule('//eg:item', '<eg:root xmlns:eg="http://www.example.com/ns">'
                                                '<eg:item/></eg:root>',
                                  namespaces={'eg': 'http://www.example.com/ns'})
        self.assertFalse(comparison.incompatible)
        self.assertEqual(comparison.current.items[0].path,
                         '/Q{http://www.example.com/ns}root[1]/Q{http://www.example.com/ns}item[1]')

    def test_unknown_profile(self):
        with self.assertRaises(ValueError):
            evaluate_rule('//item', XML_SAMPLE, 'unknown')


def evaluation_order(*nodes):
    from elementpath.xpath_compat import EvaluationOutcome, _result_item
    outcome = EvaluationOutcome(profile='legacy')
    outcome.items = [_result_item(node) for node in nodes]
    outcome.sequence_is_empty = False
    return outcome


def evaluation_item(item):
    from elementpath.xpath_compat import EvaluationOutcome
    outcome = EvaluationOutcome(profile='legacy')
    outcome.items = [item]
    outcome.sequence_is_empty = False
    return outcome


if __name__ == '__main__':
    unittest.main()
