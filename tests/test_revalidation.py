#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""Tests for schema evolution and revalidation impact analysis."""
import unittest
from textwrap import dedent

from xmlschema import XMLSchema10, XMLSchema11, \
    DeclarationUse, RecordEvidence, SchemaEvolution, DecodeUsageCollector, \
    analyze_schema_change
from xmlschema.exceptions import XMLSchemaTypeError
from xmlschema.revalidation import REVALIDATE, REUSE, MISSING_EVIDENCE


class RevalidationTestCase(unittest.TestCase):

    def schema(self, source: str, schema_class=XMLSchema10):
        return schema_class(dedent(source))


class TestSchemaDiff(RevalidationTestCase):

    def test_identical_schemas_have_no_change(self):
        xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string">
                  <xs:maxLength value="8"/>
                </xs:restriction>
              </xs:simpleType>
              <xs:complexType name="Record">
                <xs:sequence>
                  <xs:element name="code" type="Code" minOccurs="0"/>
                </xs:sequence>
                <xs:attribute name="id" type="xs:int" use="required"/>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
            </xs:schema>"""

        evolution = SchemaEvolution(self.schema(xsd), self.schema(xsd))
        self.assertFalse(evolution.changed)
        self.assertFalse(evolution.has_impact)
        self.assertEqual(evolution.changes, [])

    def test_annotation_only_change_is_ignored(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:annotation><xs:documentation>old</xs:documentation></xs:annotation>
                <xs:restriction base="xs:string">
                  <xs:maxLength value="8"/>
                </xs:restriction>
              </xs:simpleType>
              <xs:complexType name="Record">
                <xs:sequence>
                  <xs:element name="code" type="Code" minOccurs="0">
                    <xs:annotation><xs:documentation>old code</xs:documentation></xs:annotation>
                  </xs:element>
                </xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace('>old<', '>new<').replace(
            '>old code<', '>new code<'
        ).replace(
            '<xs:element name="root" type="Record"/>',
            '<!-- vendor note -->\n  <xs:element name="root" type="Record"/>'
        )

        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))
        self.assertFalse(evolution.changed)
        self.assertTrue(evolution.ignored_annotations)

        report = evolution.classify([{
            'reference': 'r1',
            'declarations': [{'element': 'root', 'type': 'Record'}],
        }])
        self.assertEqual(report.as_dict(), {
            REVALIDATE: [], REUSE: ['r1'], MISSING_EVIDENCE: [],
        })

    def test_base_type_tightening_affects_derived_types(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Base">
                <xs:restriction base="xs:string"><xs:maxLength value="10"/></xs:restriction>
              </xs:simpleType>
              <xs:simpleType name="Derived">
                <xs:restriction base="Base"><xs:pattern value="[a-z]*"/></xs:restriction>
              </xs:simpleType>
              <xs:complexType name="Record">
                <xs:sequence>
                  <xs:element name="code" type="Derived" minOccurs="0"/>
                  <xs:element name="count" type="xs:int" minOccurs="0"/>
                </xs:sequence>
              </xs:complexType>
              <xs:complexType name="Other">
                <xs:sequence><xs:element name="count" type="xs:int"/></xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
              <xs:element name="other" type="Other"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace('maxLength value="10"', 'maxLength value="6"')
        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))

        self.assertTrue(evolution.changed)
        self.assertTrue(evolution.has_impact)

        derived_change = next(
            change for change in evolution.changes if change.name == 'Base'
        )
        self.assertEqual(derived_change.derived_types, ('Derived', 'Record'))

        report = evolution.classify([
            {'reference': 'derived', 'declarations': [
                {'element': 'root', 'type': 'Record'},
                {'element': 'code', 'type': 'Derived'},
            ]},
            {'reference': 'other', 'declarations': [
                {'element': 'other', 'type': 'Other'},
            ]},
        ])

        self.assertEqual(report.revalidate, ['derived'])
        self.assertEqual(report.reuse, ['other'])
        self.assertEqual(report['derived'].status, REVALIDATE)
        self.assertIn('Derived', ' '.join(report.reasons_for('derived')))

    def test_widening_does_not_require_revalidation(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string"><xs:maxLength value="8"/></xs:restriction>
              </xs:simpleType>
              <xs:element name="root" type="Code"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace('maxLength value="8"', 'maxLength value="16"')
        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))

        self.assertTrue(evolution.changed)
        self.assertFalse(evolution.has_impact)

        report = evolution.classify([{
            'reference': 'r1',
            'declarations': [{'element': 'root', 'type': 'Code'}],
        }])
        self.assertEqual(report.reuse, ['r1'])

    def test_optional_particle_addition_is_a_widening(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:complexType name="Record">
                <xs:sequence>
                  <xs:element name="a" type="xs:string"/>
                </xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
            </xs:schema>"""

        optional_xsd = old_xsd.replace(
            '<xs:element name="a" type="xs:string"/>',
            '<xs:element name="a" type="xs:string"/>'
            '<xs:element name="b" type="xs:string" minOccurs="0"/>'
        )
        required_xsd = old_xsd.replace(
            '<xs:element name="a" type="xs:string"/>',
            '<xs:element name="a" type="xs:string"/>'
            '<xs:element name="b" type="xs:string"/>'
        )

        optional = SchemaEvolution(self.schema(old_xsd), self.schema(optional_xsd))
        required = SchemaEvolution(self.schema(old_xsd), self.schema(required_xsd))

        self.assertFalse(optional.has_impact)
        self.assertTrue(required.has_impact)

    def test_complex_type_restriction_affects_extensions(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:complexType name="Base">
                <xs:sequence><xs:element name="a" type="xs:string" minOccurs="0"/></xs:sequence>
              </xs:complexType>
              <xs:complexType name="Derived">
                <xs:complexContent>
                  <xs:extension base="Base">
                    <xs:sequence><xs:element name="b" type="xs:int" minOccurs="0"/></xs:sequence>
                  </xs:extension>
                </xs:complexContent>
              </xs:complexType>
              <xs:element name="base" type="Base"/>
              <xs:element name="derived" type="Derived"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace(
            '<xs:element name="a" type="xs:string" minOccurs="0"/>',
            '<xs:element name="a" type="xs:string"/>'
        )
        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))
        report = evolution.classify([
            {'reference': 'base', 'declarations': [{'element': 'base', 'type': 'Base'}]},
            {'reference': 'derived',
             'declarations': [{'element': 'derived', 'type': 'Derived'}]},
        ])

        self.assertEqual(report.revalidate, ['base', 'derived'])

    def test_list_item_and_union_member_changes_propagate(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Base">
                <xs:restriction base="xs:string"><xs:maxLength value="8"/></xs:restriction>
              </xs:simpleType>
              <xs:simpleType name="List">
                <xs:list itemType="Base"/>
              </xs:simpleType>
              <xs:simpleType name="Union">
                <xs:union memberTypes="Base xs:int"/>
              </xs:simpleType>
              <xs:element name="list" type="List"/>
              <xs:element name="union" type="Union"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace('maxLength value="8"', 'maxLength value="6"')
        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))

        base_change = next(
            change for change in evolution.changes if change.name == 'Base'
        )
        self.assertEqual(base_change.derived_types, ('List', 'Union'))

        report = evolution.classify([
            {'reference': 'list', 'declarations': [{'element': 'list', 'type': 'List'}]},
            {'reference': 'union', 'declarations': [{'element': 'union', 'type': 'Union'}]},
        ])
        self.assertEqual(report.revalidate, ['list', 'union'])

    def test_union_member_changes(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Value">
                <xs:union memberTypes="xs:string xs:int"/>
              </xs:simpleType>
              <xs:element name="root" type="Value"/>
            </xs:schema>"""

        added = old_xsd.replace(
            'memberTypes="xs:string xs:int"',
            'memberTypes="xs:string xs:int xs:date"'
        )
        removed = old_xsd.replace(
            'memberTypes="xs:string xs:int"', 'memberTypes="xs:string"'
        )
        reordered = old_xsd.replace(
            'memberTypes="xs:string xs:int"', 'memberTypes="xs:int xs:string"'
        )

        old_schema = self.schema(old_xsd)
        record = {'reference': 'r1',
                  'declarations': [{'element': 'root', 'type': 'Value'}]}

        report = SchemaEvolution(old_schema, self.schema(added)).classify([record])
        self.assertEqual(report.reuse, ['r1'])

        report = SchemaEvolution(old_schema, self.schema(removed)).classify([record])
        self.assertEqual(report.revalidate, ['r1'])

        report = SchemaEvolution(old_schema, self.schema(reordered)).classify([record])
        self.assertEqual(report.revalidate, ['r1'])

    def test_attribute_and_nested_content_types_propagate(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string"><xs:maxLength value="8"/></xs:restriction>
              </xs:simpleType>
              <xs:complexType name="Inner">
                <xs:sequence><xs:element name="v" type="Code"/></xs:sequence>
              </xs:complexType>
              <xs:complexType name="Outer">
                <xs:sequence><xs:element name="inner" type="Inner"/></xs:sequence>
                <xs:attribute name="code" type="Code"/>
              </xs:complexType>
              <xs:element name="root" type="Outer"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace('maxLength value="8"', 'maxLength value="4"')
        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))

        base_change = next(
            change for change in evolution.changes if change.name == 'Code'
        )
        self.assertIn('Inner', base_change.derived_types)
        self.assertIn('Outer', base_change.derived_types)

        report = evolution.classify([{
            'reference': 'r1',
            'declarations': [{'element': 'root', 'type': 'Outer'}],
        }])
        self.assertEqual(report.revalidate, ['r1'])
        self.assertIn('Outer', ' '.join(report.reasons_for('r1')))

    def test_group_reference_change_propagates_to_type(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:group name="Content">
                <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
              </xs:group>
              <xs:complexType name="Record">
                <xs:sequence><xs:group ref="Content"/></xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace(
            '<xs:element name="a" type="xs:string"/>',
            '<xs:element name="a" type="xs:string"/><xs:element name="b" type="xs:int"/>'
        )
        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))
        report = evolution.classify([{
            'reference': 'r1',
            'declarations': [{'element': 'root', 'type': 'Record'}],
        }])
        self.assertEqual(report.revalidate, ['r1'])

    def test_removed_element_is_revalidation(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:complexType name="Record">
                <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
              <xs:element name="extra" type="xs:string"/>
            </xs:schema>"""

        new_xsd = old_xsd.replace(
            '<xs:element name="extra" type="xs:string"/>', ''
        )
        evolution = SchemaEvolution(self.schema(old_xsd), self.schema(new_xsd))
        report = evolution.classify([
            {'reference': 'extra', 'declarations': [{'element': 'extra', 'type': 'xs:string'}]},
            {'reference': 'root', 'declarations': [{'element': 'root', 'type': 'Record'}]},
        ])
        self.assertEqual(report.revalidate, ['extra'])
        self.assertEqual(report.reuse, ['root'])


class TestEvidenceClassification(RevalidationTestCase):

    def setUp(self):
        self.old_schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string"><xs:maxLength value="8"/></xs:restriction>
              </xs:simpleType>
              <xs:complexType name="Record">
                <xs:sequence>
                  <xs:element name="code" type="Code"/>
                  <xs:element name="count" type="xs:int"/>
                </xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
            </xs:schema>""")
        self.new_schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string"><xs:maxLength value="6"/></xs:restriction>
              </xs:simpleType>
              <xs:complexType name="Record">
                <xs:sequence>
                  <xs:element name="code" type="Code"/>
                  <xs:element name="count" type="xs:int"/>
                </xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
            </xs:schema>""")
        self.evolution = SchemaEvolution(self.old_schema, self.new_schema)

    def test_missing_evidence_for_unknown_dynamic_type(self):
        report = self.evolution.classify([{
            'reference': 'r1',
            'declarations': [{'element': 'count', 'dynamic': True}],
        }])
        self.assertEqual(report.missing_evidence, ['r1'])
        self.assertIn('dynamic type', report.reasons_for('r1')[0])

    def test_incomplete_evidence_is_never_safe(self):
        report = self.evolution.classify([{
            'reference': 'r1',
            'declarations': [{'element': 'count', 'type': 'xs:int'}],
            'complete': False,
        }])
        self.assertEqual(report.missing_evidence, ['r1'])

    def test_no_impact_reuses_even_incomplete_evidence(self):
        report = SchemaEvolution(
            self.old_schema, self.old_schema
        ).classify([{
            'reference': 'r1',
            'declarations': [{'element': 'count', 'dynamic': True}],
            'complete': False,
        }])
        self.assertEqual(report.reuse, ['r1'])

    def test_missing_evidence_for_unknown_declaration(self):
        report = self.evolution.classify([{
            'reference': 'r1',
            'declarations': [{'element': 'absent', 'type': 'Code'}],
        }])
        self.assertEqual(report.missing_evidence, ['r1'])

    def test_unknown_type_is_missing_evidence(self):
        report = self.evolution.classify([{
            'reference': 'r1',
            'declarations': [{
                'element': 'count',
                'type': '{http://www.w3.org/2001/XMLSchema}anyType',
            }],
        }])
        self.assertEqual(report.missing_evidence, ['r1'])

    def test_declaration_use_requires_identity(self):
        with self.assertRaises(ValueError):
            DeclarationUse()

    def test_report_lookup_and_aliases(self):
        report = self.evolution.classify([
            {'reference': 'r1', 'declarations': [{'element': 'root', 'type': 'Record'},
                                                 {'element': 'code', 'type': 'Code'}]},
            {'reference': 'r2', 'declarations': [{'element': 'count', 'type': 'xs:int'}],
             'complete': False},
        ])
        self.assertEqual(report.affected, ['r1'])
        self.assertEqual(report.reusable, [])
        self.assertEqual(report.unsafe, ['r2'])
        self.assertEqual(len(report), 2)
        self.assertIn('r1', report)
        self.assertIsNone(report.get('missing'))
        with self.assertRaises(KeyError):
            report['missing']

        data = report.to_dict()
        self.assertEqual(data['summary']['total'], 2)
        self.assertEqual(data['summary']['revalidate'], 1)
        self.assertEqual(data['summary']['missing_evidence'], 1)
        self.assertEqual(data['records'][0]['status'], REVALIDATE)
        self.assertFalse(report.ignored_annotations)
        self.assertIn('2 record(s)', report.summary())

    def test_bad_record_type(self):
        with self.assertRaises(XMLSchemaTypeError):
            self.evolution.classify([42])

    def test_analyze_schema_change_function(self):
        report = analyze_schema_change(
            self.old_schema, self.new_schema,
            [RecordEvidence(
                declarations=(DeclarationUse(element='root', type='Code'),),
                reference='r1',
            )]
        )
        self.assertEqual(report.revalidate, ['r1'])


class TestDecodeUsageCollector(RevalidationTestCase):

    def test_collect_dynamic_type(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                xmlns:t="urn:test" targetNamespace="urn:test"
                elementFormDefault="qualified">
              <xs:complexType name="Record">
                <xs:sequence><xs:element name="v" type="xs:string"/></xs:sequence>
              </xs:complexType>
              <xs:complexType name="Special">
                <xs:complexContent>
                  <xs:extension base="t:Record">
                    <xs:sequence>
                      <xs:element name="extra" type="xs:int" minOccurs="0"/>
                    </xs:sequence>
                  </xs:extension>
                </xs:complexContent>
              </xs:complexType>
              <xs:element name="root" type="t:Record"/>
            </xs:schema>""")

        collector = DecodeUsageCollector()
        schema.decode("""\
            <t:root xmlns:t="urn:test"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:type="t:Special">
              <t:v>ok</t:v>
              <t:extra>3</t:extra>
            </t:root>""", element_hook=collector.collect)

        declarations = {(d.element, d.type) for d in collector.declarations}
        self.assertIn(('{urn:test}root', '{urn:test}Special'), declarations)
        self.assertTrue(collector.complete)

        evidence = collector.evidence('rec1')
        self.assertEqual(evidence.reference, 'rec1')

    def test_collector_marks_skipped_wildcard_incomplete(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:complexType name="Record">
                <xs:sequence>
                  <xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>
                </xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="Record"/>
            </xs:schema>""")

        collector = DecodeUsageCollector()
        schema.decode('<root><unknown a="1"/></root>',
                      element_hook=collector.collect, process_skipped=True)
        self.assertFalse(collector.complete)
        self.assertFalse(collector.evidence('r1').complete)

    def test_collector_marks_unknown_xsi_type_incomplete(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                xmlns:t="urn:test" targetNamespace="urn:test"
                elementFormDefault="qualified">
              <xs:complexType name="Record">
                <xs:sequence><xs:element name="v" type="xs:string"/></xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="t:Record"/>
            </xs:schema>""")

        collector = DecodeUsageCollector()
        schema.decode("""\
            <t:root xmlns:t="urn:test"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:type="t:Unknown">
              <t:v>ok</t:v>
            </t:root>""", element_hook=collector.collect, validation='lax')
        self.assertFalse(collector.complete)

    def test_collector_marks_xsi_any_type_incomplete(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                xmlns:t="urn:test" targetNamespace="urn:test"
                elementFormDefault="qualified">
              <xs:complexType name="Record">
                <xs:sequence><xs:element name="v" type="xs:string"/></xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="t:Record"/>
            </xs:schema>""")

        collector = DecodeUsageCollector()
        schema.decode("""\
            <t:root xmlns:t="urn:test"
                xmlns:xs="http://www.w3.org/2001/XMLSchema"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:type="xs:anyType">
              <t:v>ok</t:v>
            </t:root>""", element_hook=collector.collect, validation='lax')
        self.assertFalse(collector.complete)

    def test_collector_reset(self):
        collector = DecodeUsageCollector()
        collector.reset()
        self.assertTrue(collector.complete)
        self.assertEqual(collector.declarations, ())

    def test_collector_invalid_root(self):
        with self.assertRaises(XMLSchemaTypeError):
            DecodeUsageCollector(root=42)

    def test_collector_open_record_is_returned(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:element name="root" type="xs:string"/>
            </xs:schema>""")

        collector = DecodeUsageCollector()
        schema.decode('<root>x</root>', element_hook=collector.collect)
        self.assertEqual(len(collector.records), 1)
        self.assertEqual(collector.records[0].reference, 0)
        self.assertEqual(len(collector.close()), 1)
        self.assertEqual(len(collector.records), 1)

    def test_collector_groups_records(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string"><xs:maxLength value="3"/></xs:restriction>
              </xs:simpleType>
              <xs:complexType name="Rec">
                <xs:sequence><xs:element name="v" type="Code"/></xs:sequence>
              </xs:complexType>
              <xs:element name="record" type="Rec"/>
              <xs:element name="batch">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element ref="record" maxOccurs="unbounded"/>
                  </xs:sequence>
                </xs:complexType>
              </xs:element>
            </xs:schema>""")

        collector = DecodeUsageCollector(root='record')
        schema.decode("""\
            <batch>
              <record><v>a</v></record>
              <record><v>bb</v></record>
            </batch>""", path='//record', element_hook=collector.collect)

        self.assertEqual(len(collector.records), 2)
        for evidence in collector.records:
            self.assertTrue(evidence.complete)
            self.assertEqual(
                {d.element for d in evidence.declarations}, {'record', 'v'}
            )

    def test_collector_local_declaration_identity(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string"><xs:maxLength value="3"/></xs:restriction>
              </xs:simpleType>
              <xs:complexType name="A">
                <xs:sequence><xs:element name="item" type="Code"/></xs:sequence>
              </xs:complexType>
              <xs:complexType name="B">
                <xs:sequence><xs:element name="item" type="xs:int"/></xs:sequence>
              </xs:complexType>
              <xs:element name="a" type="A"/>
              <xs:element name="b" type="B"/>
            </xs:schema>""")

        collector = DecodeUsageCollector()
        schema.decode('<a><item>x</item></a>', element_hook=collector.collect)

        item = next(d for d in collector.declarations if d.element == 'item')
        self.assertEqual(
            item.schema_path,
            '/schema/complexType[name="A"]/sequence/element[name="item"]'
        )

        new_schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:simpleType name="Code">
                <xs:restriction base="xs:string"><xs:maxLength value="2"/></xs:restriction>
              </xs:simpleType>
              <xs:complexType name="A">
                <xs:sequence><xs:element name="item" type="Code"/></xs:sequence>
              </xs:complexType>
              <xs:complexType name="B">
                <xs:sequence><xs:element name="item" type="xs:int"/></xs:sequence>
              </xs:complexType>
              <xs:element name="a" type="A"/>
              <xs:element name="b" type="B"/>
            </xs:schema>""")

        report = SchemaEvolution(schema, new_schema).classify(
            [collector.evidence('r1')]
        )
        self.assertEqual(report.revalidate, ['r1'])

    def test_collector_ambiguous_local_declaration(self):
        old_xsd = """\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:complexType name="A">
                <xs:sequence><xs:element name="item" type="xs:string"/></xs:sequence>
              </xs:complexType>
              <xs:complexType name="B">
                <xs:sequence><xs:element name="item" type="xs:string"/></xs:sequence>
              </xs:complexType>
              <xs:element name="a" type="A"/>
              <xs:element name="b" type="B"/>
            </xs:schema>"""

        # The change creates an impact, but the same-named local declarations
        # can't be resolved without a schema_path.
        new_xsd = old_xsd.replace(
            '<xs:sequence><xs:element name="item" type="xs:string"/></xs:sequence>\n'
            '              </xs:complexType>\n'
            '              <xs:complexType name="B">',
            '<xs:sequence><xs:element name="item" type="xs:string"/>'
            '<xs:element name="extra" type="xs:int"/></xs:sequence>\n'
            '              </xs:complexType>\n'
            '              <xs:complexType name="B">'
        )
        self.assertNotEqual(old_xsd, new_xsd)

        report = SchemaEvolution(
            self.schema(old_xsd), self.schema(new_xsd)
        ).classify([{
            'reference': 'r1',
            'declarations': [{'element': 'item', 'type': 'xs:string'}],
        }])
        self.assertEqual(report.missing_evidence, ['r1'])
        self.assertIn('more than one local declaration',
                      report.reasons_for('r1')[0])


class TestXsd11Revalidation(RevalidationTestCase):

    def test_type_alternative_is_used_as_actual_type(self):
        schema = self.schema("""\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                xmlns:t="urn:test" targetNamespace="urn:test"
                elementFormDefault="qualified">
              <xs:complexType name="Small">
                <xs:sequence><xs:element name="v" type="xs:int"/></xs:sequence>
                <xs:attribute name="kind" type="xs:string"/>
              </xs:complexType>
              <xs:complexType name="Large">
                <xs:complexContent>
                  <xs:extension base="t:Small">
                    <xs:sequence><xs:element name="extra" type="xs:string"/></xs:sequence>
                  </xs:extension>
                </xs:complexContent>
              </xs:complexType>
              <xs:element name="root" type="t:Small">
                <xs:alternative test="@kind='large'" type="t:Large"/>
              </xs:element>
            </xs:schema>""", XMLSchema11)

        collector = DecodeUsageCollector()
        schema.decode(
            '<t:root xmlns:t="urn:test" kind="large"><t:v>1</t:v>'
            '<t:extra>x</t:extra></t:root>',
            element_hook=collector.collect
        )
        self.assertIn(
            ('{urn:test}root', '{urn:test}Large'),
            {(d.element, d.type) for d in collector.declarations}
        )
