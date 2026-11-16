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
import os
import pathlib
import tempfile
import threading
import time
import unittest

import xmlschema
from xmlschema import XMLSchema10, XMLSchema11, SchemaBuildCache
from xmlschema.exceptions import XMLSchemaValueError
from xmlschema.testing import XMLSchemaTestCase, run_xmlschema_tests

XSD_TEMPLATE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="http://example.com/{0}"
    xmlns="http://example.com/{0}" elementFormDefault="qualified">
  <xs:element name="root" type="xs:string"/>
</xs:schema>
"""

XSD_WITH_INCLUDE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="http://example.com/{0}"
    xmlns="http://example.com/{0}" elementFormDefault="qualified">
  <xs:include schemaLocation="common.xsd"/>
  <xs:element name="root" type="includedType"/>
</xs:schema>
"""

XSD_INCLUDED = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="http://example.com/{0}"
    xmlns="http://example.com/{0}" elementFormDefault="qualified">
  <xs:simpleType name="includedType">
    <xs:restriction base="xs:string"/>
  </xs:simpleType>
</xs:schema>
"""

XSD_INVALID = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:unknownType"/>
</xs:schema>
"""


class TestSchemaBuildCache(XMLSchemaTestCase):

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.base_dir = pathlib.Path(cls.tempdir.name)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        self.cache = SchemaBuildCache()

    def make_schema_file(self, dirname, filename='schema.xsd', content=None, namespace=None):
        directory = self.base_dir / dirname
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(content if content is not None else XSD_TEMPLATE.format(namespace))
        return path

    def test_reuse_same_schema(self):
        path = self.make_schema_file('case1', namespace='case1')
        schema1 = self.cache.get_or_build(XMLSchema10, str(path))
        schema2 = self.cache.get_or_build(XMLSchema10, str(path))
        self.assertIs(schema1, schema2)
        self.assertEqual(len(self.cache), 1)

    def test_same_name_different_vendors(self):
        # Same-named schemas from different vendors are distinct entries
        path_a = self.make_schema_file('vendor_a', namespace='vendor_a')
        path_b = self.make_schema_file('vendor_b', namespace='vendor_b')

        schema_a = self.cache.get_or_build(XMLSchema10, str(path_a))
        schema_b = self.cache.get_or_build(XMLSchema10, str(path_b))
        self.assertIsNot(schema_a, schema_b)
        self.assertEqual(len(self.cache), 2)

        # Consecutive validations reuse the build cost of each vendor schema
        for _ in range(3):
            self.assertIs(self.cache.get_or_build(XMLSchema10, str(path_a)), schema_a)
            self.assertIs(self.cache.get_or_build(XMLSchema10, str(path_b)), schema_b)
        self.assertEqual(len(self.cache), 2)

    def test_schema_class_distinguishes_entries(self):
        path = self.make_schema_file('case2', namespace='case2')
        schema10 = self.cache.get_or_build(XMLSchema10, str(path))
        schema11 = self.cache.get_or_build(XMLSchema11, str(path))
        self.assertIsNot(schema10, schema11)
        self.assertEqual(len(self.cache), 2)

    def test_settings_distinguish_entries(self):
        path = self.make_schema_file('case3', namespace='case3')
        schema1 = self.cache.get_or_build(XMLSchema10, str(path), validation='strict')
        schema2 = self.cache.get_or_build(XMLSchema10, str(path), validation='lax')
        schema3 = self.cache.get_or_build(XMLSchema10, str(path), validation='strict')
        self.assertIsNot(schema1, schema2)
        self.assertIs(schema1, schema3)
        self.assertEqual(len(self.cache), 2)

    def test_root_update_invalidates_entry(self):
        path = self.make_schema_file('case4', namespace='case4')
        schema1 = self.cache.get_or_build(XMLSchema10, str(path))
        path.write_text(XSD_TEMPLATE.format('case4-updated'))
        schema2 = self.cache.get_or_build(XMLSchema10, str(path))
        self.assertIsNot(schema1, schema2)
        self.assertEqual(len(self.cache), 1)

    def test_dependency_update_invalidates_only_related_closure(self):
        dep = self.make_schema_file('case5', 'common.xsd', XSD_INCLUDED.format('inc'))
        root_a = self.make_schema_file('case5', 'root_a.xsd', XSD_WITH_INCLUDE.format('inc'))
        root_b = self.make_schema_file('case5', 'root_b.xsd', XSD_TEMPLATE.format('other'))

        schema_a = self.cache.get_or_build(XMLSchema10, str(root_a))
        schema_b = self.cache.get_or_build(XMLSchema10, str(root_b))
        self.assertEqual(len(self.cache), 2)

        # Update the local dependency: only the related closure is invalidated
        dep.write_text(XSD_INCLUDED.format('inc') + '\n<!-- updated -->\n')
        self.assertIsNot(self.cache.get_or_build(XMLSchema10, str(root_a)), schema_a)
        self.assertIs(self.cache.get_or_build(XMLSchema10, str(root_b)), schema_b)
        self.assertEqual(len(self.cache), 2)

    def test_invalidate_dependency(self):
        dep = self.make_schema_file('case6', 'common.xsd', XSD_INCLUDED.format('inc6'))
        root_a = self.make_schema_file('case6', 'root_a.xsd', XSD_WITH_INCLUDE.format('inc6'))
        root_b = self.make_schema_file('case6', 'root_b.xsd', XSD_TEMPLATE.format('other6'))

        self.cache.get_or_build(XMLSchema10, str(root_a))
        self.cache.get_or_build(XMLSchema10, str(root_b))
        self.assertEqual(len(self.cache), 2)

        self.assertEqual(self.cache.invalidate_dependency(str(dep)), 1)
        self.assertEqual(len(self.cache), 1)
        self.assertEqual(self.cache.invalidate_dependency(str(dep)), 0)

    def test_invalidate(self):
        path = self.make_schema_file('case7', namespace='case7')
        self.cache.get_or_build(XMLSchema10, str(path))
        self.assertTrue(self.cache.invalidate(XMLSchema10, str(path)))
        self.assertFalse(self.cache.invalidate(XMLSchema10, str(path)))
        self.assertEqual(len(self.cache), 0)

    def test_failed_build_does_not_pollute_cache(self):
        path = self.make_schema_file('case8', content=XSD_INVALID)
        for _ in range(2):
            with self.assertRaises(xmlschema.XMLSchemaException):
                self.cache.get_or_build(XMLSchema10, str(path))
            self.assertEqual(len(self.cache), 0)
            self.assertEqual(len(self.cache._building), 0)

        # Fix the schema: the cache is not polluted by previous failures
        path.write_text(XSD_TEMPLATE.format('case8'))
        schema = self.cache.get_or_build(XMLSchema10, str(path))
        self.assertIs(self.cache.get_or_build(XMLSchema10, str(path)), schema)
        self.assertEqual(len(self.cache), 1)

    def test_concurrent_builds_share_completed_schema(self):
        path = self.make_schema_file('case9', namespace='case9')
        builds = []
        results = []
        errors = []

        def builder():
            builds.append(1)
            time.sleep(0.05)
            return XMLSchema10(str(path))

        def worker():
            try:
                results.append(self.cache.get_or_build(XMLSchema10, str(path),
                                                       builder=builder))
            except Exception as exc:  # noqa
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(errors)
        self.assertEqual(len(builds), 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(schema is results[0] for schema in results))
        self.assertEqual(len(self.cache), 1)

    def test_concurrent_failed_build(self):
        path = self.make_schema_file('case10', content=XSD_INVALID)
        results = []
        errors = []

        def worker():
            try:
                results.append(self.cache.get_or_build(XMLSchema10, str(path)))
            except Exception as exc:  # noqa
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(results)
        self.assertEqual(len(errors), 8)
        self.assertTrue(all(isinstance(exc, xmlschema.XMLSchemaException) for exc in errors))
        self.assertEqual(len(self.cache), 0)
        self.assertEqual(len(self.cache._building), 0)

    def test_maxsize_eviction(self):
        cache = SchemaBuildCache(maxsize=2)
        paths = [self.make_schema_file('case11', f'schema{k}.xsd',
                                       XSD_TEMPLATE.format(f'case11-{k}'))
                 for k in range(3)]
        for path in paths:
            cache.get_or_build(XMLSchema10, str(path))
        self.assertEqual(len(cache), 2)

    def test_uncacheable_builds_bypass_cache(self):
        path = self.make_schema_file('case12', namespace='case12')

        schema1 = self.cache.get_or_build(XMLSchema10, str(path), build=False)
        schema2 = self.cache.get_or_build(XMLSchema10, str(path), build=False)
        self.assertIsNot(schema1, schema2)

        schema3 = self.cache.get_or_build(XMLSchema10, str(path), partial=True)
        schema4 = self.cache.get_or_build(XMLSchema10, str(path), partial=True)
        self.assertIsNot(schema3, schema4)

        self.assertEqual(len(self.cache), 0)

    def test_clear(self):
        path = self.make_schema_file('case13', namespace='case13')
        self.cache.get_or_build(XMLSchema10, str(path))
        self.assertEqual(len(self.cache), 1)
        self.cache.clear()
        self.assertEqual(len(self.cache), 0)

    def test_invalid_maxsize(self):
        with self.assertRaises(XMLSchemaValueError):
            SchemaBuildCache(maxsize=0)


if __name__ == '__main__':
    run_xmlschema_tests("caching module")
