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
import csv
import pathlib
import tempfile

from xmlschema import XMLSchema10, XMLSchema11, \
    export_relational_tables, RelationalReferenceError, TypedKey
from xmlschema.testing import XMLSchemaTestCase

CASES_DIR = pathlib.Path(__file__).absolute().parent.joinpath('test_cases')


class TestRelationalExport(XMLSchemaTestCase):
    cases_dir = CASES_DIR
    schema_class = XMLSchema10

    @classmethod
    def setUpClass(cls):
        cls.orders_xsd = cls.casepath('features/relational/orders.xsd')
        cls.orders_xml = cls.casepath('features/relational/orders.xml')
        cls.typecheck_xsd = cls.casepath('features/relational/typecheck.xsd')
        cls.typecheck_xml = cls.casepath('features/relational/typecheck.xml')
        cls.orders_schema = cls.schema_class(cls.orders_xsd)

    def _table(self, result, name, scope_substring):
        matches = [
            table for table in result.tables
            if table.name == name and scope_substring in table.scope_label
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_scoped_line_keys_are_renumbered_per_order(self):
        result = export_relational_tables(self.orders_schema, self.orders_xml)

        line_a = self._table(result, 'line_key', "order[1]")
        line_b = self._table(result, 'line_key', "order[2]")

        # Line numbers restart inside every order: order A and order B
        # both have a (1, WH1) line, kept in separate scope tables.
        keys_a = [row.key.parts for row in line_a.rows]
        keys_b = [row.key.parts for row in line_b.rows]
        self.assertEqual(keys_a, [(1, 'WH1'), (2, 'WH1'), (2, 'WH2')])
        self.assertEqual(keys_b, [(1, 'WH1')])

        labels = {table.scope_label for table in result.tables}
        self.assertIn('/orders/order[1]', labels)
        self.assertIn('/orders/order[2]', labels)

    def test_composite_key_compares_typed_components(self):
        result = export_relational_tables(self.orders_schema, self.orders_xml)
        line_a = self._table(result, 'line_key', "order[1]")

        composite = TypedKey((2, 'WH1'),
                             ('{http://www.w3.org/2001/XMLSchema}decimal',
                              '{http://www.w3.org/2001/XMLSchema}string'))
        self.assertIn(composite, line_a)

        # Same displayed line number but a different warehouse is a different key.
        other = TypedKey((2, 'WH9'),
                         ('{http://www.w3.org/2001/XMLSchema}decimal',
                          '{http://www.w3.org/2001/XMLSchema}string'))
        self.assertNotIn(other, line_a)

        type_names = {type_name for row in line_a.rows for type_name in row.key.types}
        self.assertEqual(type_names, {
            '{http://www.w3.org/2001/XMLSchema}decimal',
            '{http://www.w3.org/2001/XMLSchema}string',
        })

    def test_reference_resolves_only_within_own_order(self):
        result = export_relational_tables(self.orders_schema, self.orders_xml)

        # Order B references line 2 (missing in B) even though order A has a
        # line 2: it must not be silently satisfied by the other order.
        failures = result.failures
        broken = [failure for failure in failures
                  if failure.constraint.local_name == 'line_ref']
        self.assertEqual(len(broken), 1)
        failure = broken[0]
        self.assertIn('order[2]', failure.scope_label)
        self.assertEqual(failure.key.parts, (2, 'WH1'))
        self.assertIn('line_ref', str(failure))
        self.assertIn('line_key', str(failure))
        self.assertIn("2", str(failure))

        # The valid references of both orders resolve inside their own scope.
        line_ref_b = self._table(result, 'line_ref', "order[2]")
        dangling_keys = {failure.key for failure in failures}
        resolved = {row.key.parts for row in line_ref_b.rows
                    if row.key not in dangling_keys}
        self.assertEqual(resolved, {(1, 'WH1')})
        # The repeated (1, WH1) reference is legal on a keyref and is retained.
        self.assertEqual(len(line_ref_b.rows), 3)
        self.assertEqual(len(line_ref_b.duplicates), 1)

        # Part P2 is missing inside order B even though order A has it.
        part_failures = [failure for failure in failures
                         if failure.constraint.local_name == 'part_ref']
        self.assertEqual(len(part_failures), 1)
        self.assertEqual(part_failures[0].key.parts, ('P2',))
        self.assertIn('order[2]', part_failures[0].scope_label)

    def test_strict_mode_raises_with_scope_and_missing_key(self):
        with self.assertRaises(RelationalReferenceError) as ctx:
            export_relational_tables(self.orders_schema, self.orders_xml, strict=True)
        message = str(ctx.exception)
        self.assertIn('order[2]', message)
        self.assertIn('line_ref', message)
        self.assertIn('line_key', message)

    def test_same_display_string_different_type_does_not_match(self):
        schema = self.schema_class(self.typecheck_xsd)
        result = export_relational_tables(schema, self.typecheck_xml)

        code_ref = self._table(result, 'pointer_code_ref', 'typecheck')
        active_ref = self._table(result, 'pointer_active_ref', 'typecheck')
        link_ref = self._table(result, 'link_code_ref', 'typecheck')

        # The string pointer renders "1"/"true" but the key fields are typed
        # integer/boolean: no connection may be created.
        code_failures = [f for f in result.failures
                         if f.constraint.local_name == 'pointer_code_ref']
        active_failures = [f for f in result.failures
                           if f.constraint.local_name == 'pointer_active_ref']
        self.assertEqual(len(code_failures), 1)
        self.assertEqual(len(active_failures), 1)
        self.assertEqual(code_failures[0].key.types,
                         ('{http://www.w3.org/2001/XMLSchema}string',))
        self.assertEqual(active_failures[0].key.types,
                         ('{http://www.w3.org/2001/XMLSchema}string',))

        # Integer "01" decodes to 1 and matches the integer key "1":
        # same type space, different lexical representation.
        self.assertEqual([row.key.parts for row in link_ref.rows], [(1,)])
        self.assertEqual(
            [f for f in result.failures if f.constraint.local_name == 'link_code_ref'],
            []
        )
        self.assertEqual(len(code_ref.rows), 1)
        self.assertEqual(len(active_ref.rows), 1)

    def test_as_dict_keeps_scope_separated(self):
        result = export_relational_tables(self.orders_schema, self.orders_xml)
        data = result.as_dict()
        table_names = list(data)
        self.assertTrue(any('line_key@/orders/order[1]' in name for name in table_names))
        self.assertTrue(any('line_key@/orders/order[2]' in name for name in table_names))
        first = data['line_key@/orders/order[1]']
        self.assertEqual(first[0]['lineNo'], 1)
        self.assertEqual(first[0]['warehouse'], 'WH1')

    def test_write_csv_emits_one_file_per_scope_table(self):
        result = export_relational_tables(self.orders_schema, self.orders_xml)
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = result.write_csv(tmpdir)
            self.assertEqual(len(paths), len(result.tables))
            line_path = next(p for p in paths if 'line_key.csv' in p.name)
            with line_path.open(encoding='utf-8') as fp:
                rows = list(csv.reader(fp))
        self.assertEqual(rows[0], ['_scope', 'lineNo', 'warehouse'])
        scopes = {row[0] for row in rows[1:]}
        self.assertTrue(any('order[1]' in scope for scope in scopes))

    def test_schema_convenience_method(self):
        result = self.orders_schema.export_relational_tables(self.orders_xml)
        names = {table.name for table in result.tables}
        self.assertEqual(
            names, {'line_key', 'part_key', 'line_ref', 'part_ref'}
        )

    def test_duplicate_key_is_parked_and_first_stays_canonical(self):
        xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="doc">
            <xs:complexType><xs:sequence>
              <xs:element name="row" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:attribute name="id" type="xs:integer" use="required"/>
                </xs:complexType>
              </xs:element>
            </xs:sequence></xs:complexType>
            <xs:key name="row_key">
              <xs:selector xpath="row"/>
              <xs:field xpath="@id"/>
            </xs:key>
          </xs:element>
        </xs:schema>"""
        xml = '<doc><row id="1"/><row id="2"/><row id="1"/></doc>'

        result = export_relational_tables(self.schema_class(xsd), xml)
        table = result.tables[0]
        self.assertEqual([row.key.parts for row in table.rows], [(1,), (2,)])
        self.assertEqual([row.key.parts for row in table.duplicates], [(1,)])
        # The canonical row is the first occurrence in document order.
        self.assertIs(table.get(table.rows[0].key), table.rows[0])


class TestRelationalExportXsd11(TestRelationalExport):
    schema_class = XMLSchema11


if __name__ == '__main__':
    from xmlschema.testing import run_xmlschema_tests
    run_xmlschema_tests('relational export module')
