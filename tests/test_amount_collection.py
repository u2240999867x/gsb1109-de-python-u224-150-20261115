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
import os
import tempfile
import unittest
from decimal import Decimal
from xml.etree import ElementTree as ET

from elementpath.exceptions import XMLResourceForbidden

from amount_collection import AmountCollector, CollectionSummary, \
    DEFAULT_VALUE_EXPRESSION, ERROR_EXPRESSION, ERROR_INVALID_NUMBER, \
    ERROR_MULTI_VALUE, LineNumberingParser, STATUS_EMPTY, STATUS_ERROR, \
    STATUS_VALUE, collect_amounts, main, parse_xml

LEDGER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ledger>
  <row id="r1"><amount>12.50</amount></row>
  <row id="r2"><amount></amount></row>
  <row id="r3"><amount>abc</amount></row>
  <row id="r4"/>
  <row id="r5"><amount>1</amount><amount>2</amount></row>
  <row id="r6"><amount>0</amount></row>
  <row id="r7" amount="42.00"/>
  <row id="r8"><amount>   </amount></row>
  <row id="r9"><amount>-7.25</amount></row>
</ledger>"""

FIELD_PATH = "amount | @amount"


class LineNumberingParserTest(unittest.TestCase):

    def test_records_source_lines_without_touching_nodes(self):
        parser = LineNumberingParser()
        root = ET.fromstring(LEDGER_XML, parser=parser)
        self.assertEqual(parser.source_lines[root], 2)
        first_amount = root.find("row[@id='r1']/amount")
        self.assertEqual(parser.source_lines[first_amount], 3)

        for element in root.iter():
            self.assertFalse(
                any(key.startswith('_') for key in element.attrib),
                'parser must not add attributes to the XML nodes',
            )

    def test_namespace_tags_keep_clark_notation(self):
        xml = "<r xmlns:n='urn:x'><n:a>1</n:a></r>"
        custom = ET.fromstring(xml, parser=LineNumberingParser())
        standard = ET.fromstring(xml)
        self.assertEqual(custom.tag, standard.tag)
        self.assertEqual(custom[0].tag, standard[0].tag)
        self.assertEqual(ET.tostring(custom), ET.tostring(standard))

    def test_entity_declarations_are_forbidden(self):
        xxe = ("<!DOCTYPE r [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>"
               "<r><a>&xxe;</a></r>")
        with self.assertRaises(XMLResourceForbidden):
            parse_xml(xxe)

    def test_parse_file_like_object(self):
        document = parse_xml(io.BytesIO(LEDGER_XML.encode()))
        self.assertTrue(document.source_lines)
        self.assertIsInstance(document.tree, ET.ElementTree)


class AmountCollectorClassificationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.document = parse_xml(LEDGER_XML)
        cls.report = collect_amounts(
            cls.document, "//row", FIELD_PATH
        )

    def test_every_row_is_processed_in_order(self):
        self.assertEqual(
            [row.index for row in self.report.rows],
            list(range(1, 10)),
        )

    def test_statuses_are_classified_separately(self):
        by_index = {row.index: row for row in self.report.rows}

        self.assertTrue(by_index[1].is_value)
        self.assertEqual(by_index[1].value.value, Decimal('12.50'))

        # empty element, missing node and whitespace-only node are all empty
        for index in (2, 4, 8):
            self.assertEqual(by_index[index].status, STATUS_EMPTY)
            self.assertEqual(by_index[index].errors, [])
            self.assertIsNone(by_index[index].value)

        # illegal lexical value is an invalid-number error
        self.assertEqual(by_index[3].status, STATUS_ERROR)
        invalid = by_index[3].errors[0]
        self.assertEqual(invalid.kind, ERROR_INVALID_NUMBER)
        self.assertEqual(invalid.code, 'err:FORG0001')

        # multiple matching nodes are a multi-value error
        self.assertEqual(by_index[5].status, STATUS_ERROR)
        multi = by_index[5].errors[0]
        self.assertEqual(multi.kind, ERROR_MULTI_VALUE)
        self.assertEqual(len(multi.matched_paths), 2)

        # a legal zero is a value, never grouped with empty sequences
        self.assertEqual(by_index[6].status, STATUS_VALUE)
        self.assertEqual(by_index[6].value.value, Decimal('0'))

        # attributes are valid amount nodes too
        self.assertEqual(by_index[7].value.value, Decimal('42.00'))
        self.assertEqual(by_index[7].value.node.kind, 'attribute')

        self.assertEqual(by_index[9].value.value, Decimal('-7.25'))

    def test_failure_does_not_swallow_following_rows(self):
        # invalid row is index 3 and multi-value row is index 5; rows after
        # them must still be collected
        after_invalid = self.report.rows[3]
        self.assertEqual(after_invalid.index, 4)
        self.assertEqual(after_invalid.status, STATUS_EMPTY)
        self.assertEqual(self.report.rows[5].value.value, Decimal('0'))
        self.assertEqual(self.report.rows[8].value.value, Decimal('-7.25'))

    def test_locations_point_to_rule_and_xml_node(self):
        invalid = self.report.rows[2].errors[0]
        self.assertEqual(invalid.rule.line, 1)
        self.assertEqual(invalid.rule.kind, 'xpath-token')
        self.assertEqual(invalid.node.line, 5)
        self.assertIn('amount', invalid.node.path)

        multi = self.report.rows[4].errors[0]
        self.assertEqual(multi.rule.line, 1)
        self.assertEqual(multi.node.line, 7)
        self.assertTrue(all('amount' in path for path in multi.matched_paths))

        value = self.report.rows[0].value
        self.assertEqual(value.node.line, 3)

    def test_original_xml_nodes_are_preserved(self):
        root = self.document.tree.getroot()
        original = ET.tostring(root)
        # collecting twice must keep the tree identical and expose the same
        # element objects on the results
        collect_amounts(self.document, "//row", FIELD_PATH)
        self.assertEqual(ET.tostring(root), original)

        value_row = next(row for row in self.report.rows if row.is_value)
        self.assertIs(value_row.row_element, root.find("row[@id='r1']"))
        self.assertIs(value_row.value.element,
                      root.find("row[@id='r1']/amount"))

    def test_empty_attribute_is_an_empty_sequence(self):
        document = parse_xml("<r><row amount=''/></r>")
        report = collect_amounts(document, "//row", "@amount")
        self.assertEqual(report.rows[0].status, STATUS_EMPTY)

    def test_text_node_field_resolves_owner_line(self):
        xml = "<r>\n  <row><a>9.5</a></row>\n  <row><a>nope</a></row>\n</r>"
        report = collect_amounts(parse_xml(xml), "//row", "a/text()")
        value_node = report.rows[0].value.node
        self.assertEqual(value_node.kind, 'text')
        self.assertEqual(value_node.line, 2)
        self.assertTrue(value_node.path.endswith('/text()[1]'))
        self.assertIsInstance(report.rows[0].value.element, ET.Element)
        self.assertEqual(report.rows[1].errors[0].node.line, 3)


class AmountCollectorSummaryTest(unittest.TestCase):

    def setUp(self):
        self.report = collect_amounts(parse_xml(LEDGER_XML), "//row", FIELD_PATH)

    def test_zeros_and_empty_sequences_are_not_merged(self):
        summary = self.report.summary
        self.assertIsInstance(summary, CollectionSummary)
        self.assertEqual(summary.total_rows, 9)
        self.assertEqual(summary.value_rows, 4)
        self.assertEqual(summary.zero_value_rows, 1)
        self.assertEqual(summary.empty_rows, 3)
        self.assertEqual(summary.error_rows, 2)

    def test_denominator_counts_values_only(self):
        summary = self.report.summary
        # denominator includes the legal zero but excludes every empty row
        self.assertEqual(summary.denominator, summary.value_rows)
        self.assertEqual(summary.denominator, 4)
        self.assertEqual(summary.total, Decimal('47.25'))
        self.assertEqual(summary.average, Decimal('11.8125'))

    def test_zero_denominator_on_all_empty_document(self):
        document = parse_xml("<r><row/><row><a>  </a></row></r>")
        summary = collect_amounts(document, "//row", "a").summary
        self.assertEqual(summary.denominator, 0)
        self.assertEqual(summary.value_rows, 0)
        self.assertEqual(summary.empty_rows, 2)
        self.assertIsNone(summary.average)
        self.assertEqual(summary.total, Decimal('0'))


class AmountCollectorExpressionsTest(unittest.TestCase):

    def test_custom_value_expressions(self):
        document = parse_xml("<r><row><a>3</a></row>"
                             "<row><a>2.5</a></row></r>")

        integer_report = AmountCollector(
            "//row", "a", "xs:integer(.)").collect(document)
        self.assertEqual(integer_report.rows[0].value.value, Decimal(3))

        double_report = AmountCollector(
            "//row", "a", "xs:double(.)").collect(document)
        self.assertEqual(double_report.rows[1].value.value, Decimal('2.5'))

    def test_padded_numeric_value_is_converted(self):
        report = collect_amounts(
            parse_xml("<r><row><a>  7.00  </a></row></r>"), "//row", "a")
        self.assertEqual(report.rows[0].value.value, Decimal('7.00'))

    def test_non_numeric_result_is_an_expression_error(self):
        document = parse_xml("<r><row><a>true</a></row></r>")
        report = AmountCollector(
            "//row", "a", "boolean(.)").collect(document)
        self.assertEqual(report.rows[0].status, STATUS_ERROR)
        self.assertEqual(report.rows[0].errors[0].kind, ERROR_EXPRESSION)

    def test_non_finite_number_is_rejected(self):
        document = parse_xml("<r><row><a>NaN</a></row></r>")
        report = AmountCollector(
            "//row", "a", "xs:double(.)").collect(document)
        self.assertEqual(report.rows[0].errors[0].kind, ERROR_EXPRESSION)

    def test_value_expression_returning_multiple_items_is_multi_value(self):
        document = parse_xml("<r><row><a>1</a></row></r>")
        report = AmountCollector(
            "//row", "a", "(., .)").collect(document)
        self.assertEqual(report.rows[0].errors[0].kind, ERROR_MULTI_VALUE)

    def test_namespaces_are_supported(self):
        xml = ("<bk:ledger xmlns:bk='urn:bk'>"
               "<bk:row><bk:amount>1.5</bk:amount></bk:row>"
               "<bk:row><bk:amount>bad</bk:amount></bk:row>"
               "</bk:ledger>")
        report = AmountCollector(
            "//bk:row", "bk:amount",
            namespaces={'bk': 'urn:bk'}).collect(parse_xml(xml))
        self.assertEqual(report.rows[0].value.value, Decimal('1.5'))
        self.assertEqual(report.rows[1].errors[0].kind, ERROR_INVALID_NUMBER)

    def test_default_value_expression_is_decimal_cast(self):
        self.assertEqual(DEFAULT_VALUE_EXPRESSION, 'xs:decimal(.)')


class AmountCollectorInputsTest(unittest.TestCase):

    XML = "<r>\n <row><a>5</a></row>\n <row><a>bad</a></row>\n</r>"

    def test_string_and_bytes_inputs_get_line_numbers(self):
        for source in (self.XML, self.XML.encode()):
            report = collect_amounts(source, "//row", "a")
            self.assertEqual(report.rows[0].value.node.line, 2)
            self.assertEqual(report.rows[1].errors[0].node.line, 3)

    def test_file_path_input_gets_line_numbers(self):
        with tempfile.NamedTemporaryFile('w', suffix='.xml',
                                         delete=False) as handle:
            handle.write(self.XML)
            path = handle.name
        try:
            report = collect_amounts(path, "//row", "a")
            self.assertEqual(report.rows[0].value.node.line, 2)
        finally:
            os.unlink(path)

    def test_raw_element_without_line_map(self):
        root = ET.fromstring(self.XML)
        report = collect_amounts(root, "//row", "a")
        self.assertEqual(report.rows[0].value.node.line, None)
        self.assertEqual(report.rows[0].status, STATUS_VALUE)
        self.assertEqual(report.rows[1].status, STATUS_ERROR)

    def test_report_dict_serialization(self):
        report = collect_amounts(parse_xml(self.XML), "//row", "a")
        payload = report.to_dict()
        self.assertEqual(set(payload), {'rows', 'summary'})
        self.assertEqual(payload['summary']['denominator'], 1)
        json.dumps(payload)  # must be JSON serializable


class CommandLineTest(unittest.TestCase):

    def test_main_text_and_json_output(self):
        with tempfile.NamedTemporaryFile('w', suffix='.xml',
                                         delete=False) as handle:
            handle.write("<r>\n <row><a>1</a></row>\n <row/>\n</r>")
            path = handle.name
        try:
            self.assertEqual(
                main([path, '--row-path', '//row', '--field-path', 'a']), 0)
            self.assertEqual(
                main([path, '--row-path', '//row',
                      '--field-path', 'a', '--json']), 0)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
