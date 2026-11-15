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
import pathlib
import unittest
import warnings
from unittest import mock

from xmlschema import XMLSchema10, XMLSchema11
from xmlschema.exceptions import XMLSchemaValueError, XMLResourceOSError
from xmlschema.packages import SchemaPackage, PackageIssue, SchemaReference
from xmlschema.loaders import PackageSchemaLoader
from xmlschema.testing import XMLSchemaTestCase, run_xmlschema_tests


class TestSchemaPackage(XMLSchemaTestCase):
    cases_dir = pathlib.Path(__file__).absolute().parent.joinpath('test_cases')

    def test_manifest_entries(self):
        package = SchemaPackage(self.casepath('packages/pkg'))
        self.assertEqual(package.version, 'v1')
        self.assertEqual(package.get_root(), 'http://example.com/pkg/xsd/main.xsd')
        self.assertEqual(len(package.entries), 3)
        self.assertTrue(package.is_offline())

    def test_python_manifest(self):
        package = SchemaPackage(self.casepath('packages/pkg/manifest.py'))
        self.assertEqual(package.version, 'v1')
        self.assertEqual(len(package.entries), 3)

    def test_broken_package(self):
        package = SchemaPackage(self.casepath('packages/bad'))
        self.assertFalse(package.is_offline())
        with self.assertRaises(XMLSchemaValueError):
            package.get_root_path()
        with self.assertRaises(XMLSchemaValueError):
            package.create_schema()

    def test_path_escape(self):
        with self.assertRaises(XMLSchemaValueError) as ctx:
            SchemaPackage(self.casepath('packages/escape'))
        self.assertIn("outside the package directory", str(ctx.exception))

    def test_relative_locations(self):
        package = SchemaPackage(self.casepath('packages/pkg/manifest.json'))
        self.assertEqual(
            package.resolve('../common/types.xsd', 'http://example.com/pkg/xsd/main.xsd'),
            'http://example.com/pkg/common/types.xsd'
        )
        self.assertEqual(
            package.resolve('../common/other.xsd', 'http://example.com/pkg/xsd/main.xsd'),
            'http://example.com/pkg/common/other.xsd'
        )

    def test_missing_dependency_chain(self):
        package = SchemaPackage(self.casepath('packages/missing'))
        issues = package.iter_missing()
        self.assertEqual(len(issues), 1)
        self.assertIsInstance(issues[0], PackageIssue)
        self.assertEqual(
            issues[0].location,
            'http://example.com/miss/xsd/absent/level2.xsd'
        )
        chain = issues[0].chain
        self.assertEqual([x.kind for x in chain], ['include', 'import'])
        self.assertEqual(chain[0].location, 'level1.xsd')
        self.assertEqual(chain[1].location, 'absent/level2.xsd')
        self.assertEqual(chain[1].namespace, 'urn:absent')
        self.assertIn("include -> 'level1.xsd'", issues[0].message)
        self.assertIn("import 'urn:absent' -> 'absent/level2.xsd'", issues[0].message)

    def test_build_package_schema(self):
        package = SchemaPackage(self.casepath('packages/pkg'))
        schema = package.create_schema()
        self.assertIsInstance(schema, XMLSchema10)
        self.assertTrue(schema.built)
        self.assertIn('{urn:pkg:v1}RootType', schema.maps.types)
        self.assertIn('{urn:pkg:other}Code', schema.maps.types)

        self.assertTrue(schema.is_valid('<root xmlns="urn:pkg:v1">abc</root>'))
        self.assertFalse(schema.is_valid('<root xmlns="urn:pkg:v1">toolong</root>'))

        self.assertEqual(list(schema.includes), ['../common/types.xsd'])
        self.assertEqual(len(schema.imports), 1)

    def test_selected_sources_summary(self):
        package = SchemaPackage(self.casepath('packages/pkg'))
        schema = package.create_schema()

        summary = package.summary(schema)
        self.assertEqual(len(summary), 3)
        self.assertEqual(
            {x['location'] for x in summary},
            {
                'http://example.com/pkg/xsd/main.xsd',
                'http://example.com/pkg/common/types.xsd',
                'http://example.com/pkg/common/other.xsd',
            }
        )
        self.assertTrue(all(x['loaded'] for x in summary))
        self.assertTrue(all(x['path'].startswith('v1/') for x in summary))
        self.assertEqual(
            {x['namespace'] for x in summary}, {'urn:pkg:v1', 'urn:pkg:other'}
        )

    def test_missing_dependency_loading(self):
        package = SchemaPackage(self.casepath('packages/missing'))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            schema = package.create_schema(validation='lax')

        issues = schema.maps.loader.issues
        self.assertEqual(len(issues), 1)
        self.assertEqual([x.kind for x in issues[0].chain], ['import'])
        self.assertEqual(issues[0].location,
                         'http://example.com/miss/xsd/absent/level2.xsd')

    def test_offline_no_network_access(self):
        package = SchemaPackage(self.casepath('packages/pkg'))
        with mock.patch('urllib.request.urlopen') as urlopen:
            schema = package.create_schema()
            self.assertTrue(schema.built)

            # An unmapped remote import must be a missing dependency, not a fetch.
            missing = SchemaPackage(self.casepath('packages/missing'))
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                self.assertEqual(len(missing.iter_missing()), 1)

            xml = '<root xmlns="urn:miss"><!-- extra --></root>'
            self.assertFalse(schema.is_valid(xml))

        urlopen.assert_not_called()

    def test_unmapped_remote_location(self):
        package = SchemaPackage(self.casepath('packages/missing'))
        self.assertIsNone(
            package.get_filepath('https://example.com/not-mapped.xsd')
        )
        self.assertEqual(
            package.resolve('https://example.com/not-mapped.xsd'),
            'https://example.com/not-mapped.xsd'
        )

    def test_loaders_are_registered(self):
        package = SchemaPackage(self.casepath('packages/pkg'))
        schema = package.create_schema()
        self.assertIsInstance(schema.maps.loader, PackageSchemaLoader)
        self.assertIs(schema.maps.loader.package, package)

    def test_root_schema_errors(self):
        package = SchemaPackage(self.casepath('packages/pkg'))
        with self.assertRaises(XMLSchemaValueError):
            package.create_schema(loader_class=type('Loader', (), {}))

    def test_no_cross_version_filling(self):
        first = SchemaPackage(self.casepath('packages/pkg/manifest-v1.json'))
        second = SchemaPackage(self.casepath('packages/pkg/manifest-v2.json'))

        self.assertEqual(first.version, 'v1')
        self.assertEqual(second.version, 'v2')
        self.assertNotEqual(first.entries, second.entries)
        self.assertTrue(all('/v1/' in p for p in first.entries.values()))
        self.assertTrue(all('/v2/' in p for p in second.entries.values()))

        schema1 = first.create_schema()
        schema2 = second.create_schema()
        self.assertTrue(schema1.is_valid('<root xmlns="urn:pkg:v1">abcde</root>'))
        self.assertFalse(schema1.is_valid('<root xmlns="urn:pkg:v1">abcdef</root>'))
        self.assertTrue(schema2.is_valid('<root xmlns="urn:pkg:v2">abcdefghij</root>'))
        self.assertFalse(schema2.is_valid('<root xmlns="urn:pkg:v2">abcdefghijk</root>'))

    def test_loader_requires_package(self):
        schema = XMLSchema10(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
        )
        with self.assertRaises(XMLSchemaValueError):
            PackageSchemaLoader(schema.maps)

    def test_package_with_global_maps(self):
        package = SchemaPackage(self.casepath('packages/pkg'))
        schema = package.create_schema()
        with self.assertRaises(XMLSchemaValueError):
            XMLSchema10(
                package.get_root_path(), global_maps=schema.maps, package=package
            )

    def test_version_isolation_builtin_meta_schemas(self):
        xsd10 = XMLSchema10(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" type="xs:string"/></xs:schema>',
            use_meta=False,
        )
        xsd11 = XMLSchema11(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" type="xs:string"/></xs:schema>',
            use_meta=False,
        )

        urls10 = {s.url for s in xsd10.maps.owned_schemas if s.url}
        urls11 = {s.url for s in xsd11.maps.owned_schemas if s.url}

        self.assertTrue(any('/XSD_1.0/' in u for u in urls10))
        self.assertFalse(any('/XSD_1.1/' in u for u in urls10))
        self.assertTrue(any('/XSD_1.1/' in u for u in urls11))
        self.assertFalse(any('/XSD_1.0/' in u for u in urls11))


if __name__ == '__main__':
    run_xmlschema_tests('packages.py module')
