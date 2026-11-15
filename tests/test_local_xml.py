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
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from elementpath import LocalXMLCatalog, CatalogEntry, CatalogReport, XPathContext, \
    select, Selector, MissingDocumentError, MissingCollectionError, \
    DocumentAccessError, OutOfScopeError, InvalidDocumentURIError, DocumentParseError
from elementpath.xpath3 import XPath30Parser, XPath31Parser


class LocalXMLCatalogTest(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp_dir.name)
        self.write('a.xml', '<a><v>1</v><link href="b.xml"/></a>')
        self.write('b.xml', '<b><v>2</v></b>')
        self.write('c.xml', '<c><v>3</v></c>')
        self.write('sub/d.xml', '<d><v>4</v></d>')
        self.write('sub/e.xml', '<e><v>5</v></e>')

    def tearDown(self):
        self._tmp_dir.cleanup()

    def write(self, name, xml):
        path = self.base_dir.joinpath(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(xml, encoding='utf-8')
        return path

    def catalog(self, locked=None, **kwargs):
        if locked is None:
            locked = ['a.xml', 'b.xml', 'c.xml', 'sub/d.xml', 'sub/e.xml']
        return LocalXMLCatalog(self.base_dir, locked, **kwargs)

    def uri(self, name):
        return self.base_dir.joinpath(name).as_uri()

    #
    # Catalog construction and URI canonicalisation
    def test_catalog_entries_and_uri_forms(self):
        catalog = self.catalog(batch='B1')

        self.assertEqual(len(catalog), 5)
        self.assertIn(self.uri('a.xml'), catalog)
        self.assertIn('a.xml', catalog)
        self.assertIn(self.base_dir.joinpath('a.xml'), catalog)
        self.assertNotIn('unknown.xml', catalog)

        doc1 = catalog.get_document('a.xml')
        doc2 = catalog.get_document(self.uri('a.xml'))
        doc3 = catalog.get_document(str(self.base_dir.joinpath('a.xml')))
        self.assertIs(doc1, doc2)
        self.assertIs(doc1, doc3)
        self.assertEqual(doc1.uri, self.uri('a.xml'))
        self.assertEqual(doc1.document_uri, self.uri('a.xml'))

        entry = catalog.resolve('b.xml')
        self.assertIsInstance(entry, CatalogEntry)
        self.assertEqual(entry.uri, self.uri('b.xml'))
        self.assertEqual(entry.path, self.base_dir.joinpath('b.xml').resolve())
        self.assertEqual(catalog.get_path('b.xml'), entry.path)
        self.assertEqual(catalog.get_document_uri('b.xml'), entry.uri)

    def test_catalog_mapping_api(self):
        catalog = self.catalog(batch='B1', collections={'first': ['a.xml', 'b.xml']})

        self.assertEqual(len(catalog.keys()), 5)
        self.assertEqual([x for x in catalog], list(catalog.keys()))
        documents = catalog.values()
        self.assertEqual([doc.uri for doc in documents], list(catalog.keys()))
        self.assertEqual(catalog['a.xml'], documents[0])
        self.assertEqual(dict(catalog.items())[self.uri('b.xml')], documents[1])
        self.assertEqual(catalog.collections, {'first': (self.uri('a.xml'), self.uri('b.xml'))})
        self.assertEqual(catalog.entries[0].name, 'a.xml')

    def test_default_locked_directory_scan(self):
        catalog = LocalXMLCatalog(self.base_dir, batch='B1')
        self.assertEqual(len(catalog), 5)
        self.assertEqual(
            {Path(x).name for x in catalog.keys()},
            {'a.xml', 'b.xml', 'c.xml', 'd.xml', 'e.xml'}
        )

    def test_from_directory_and_manifest(self):
        catalog = LocalXMLCatalog.from_directory(self.base_dir, batch='B1')
        self.assertEqual(len(catalog), 5)

        manifest = self.base_dir.joinpath('catalog.json')
        manifest.write_text(json.dumps({
            'batch': 'B2',
            'documents': ['a.xml', 'b.xml'],
            'collections': {'first': ['a.xml', 'b.xml']},
            'default_collection': 'first',
        }), encoding='utf-8')

        catalog = LocalXMLCatalog.from_manifest(manifest)
        self.assertEqual(catalog.batch, 'B2')
        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog.default_collection,
                         (self.uri('a.xml'), self.uri('b.xml')))

        line_manifest = self.base_dir.joinpath('catalog.txt')
        line_manifest.write_text('a.xml\n# comment\nb.xml\n', encoding='utf-8')
        catalog = LocalXMLCatalog.from_manifest(line_manifest, batch='B3')
        self.assertEqual(len(catalog), 2)

    #
    # Lock enforcement: missing and out-of-scope resources
    def test_missing_locked_document(self):
        catalog = self.catalog(['a.xml', 'missing.xml'], batch='B1')

        with self.assertRaises(MissingDocumentError) as ctx:
            catalog.get_document('missing.xml')
        self.assertEqual(ctx.exception.code, 'FODC0002')
        self.assertEqual(ctx.exception.kind, 'missing')
        self.assertEqual(ctx.exception.uri, self.uri('missing.xml'))

        report = catalog.report()
        self.assertIsInstance(report, CatalogReport)
        self.assertEqual(report.missing, (self.uri('missing.xml'),))
        self.assertEqual(report.out_of_scope, ())

    def test_out_of_bounds_document(self):
        outside = self.base_dir.parent.joinpath('outside-catalog-file.xml')
        outside.write_text('<outside/>', encoding='utf-8')
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

        catalog = self.catalog(batch='B1')
        with self.assertRaises(OutOfScopeError) as ctx:
            catalog.get_document(str(outside))
        self.assertEqual(ctx.exception.kind, 'out_of_scope')
        self.assertEqual(ctx.exception.reason, 'outside')

        report = catalog.report()
        self.assertEqual(report.out_of_bounds, (str(outside),))
        self.assertEqual(report.not_locked, ())
        self.assertEqual(report.out_of_scope, (str(outside),))

    def test_not_locked_document(self):
        catalog = self.catalog(['a.xml'], batch='B1')

        with self.assertRaises(DocumentAccessError) as ctx:
            catalog.get_document('b.xml')
        self.assertEqual(ctx.exception.code, 'FODC0005')
        self.assertEqual(ctx.exception.reason, 'not_locked')

        report = catalog.report()
        self.assertEqual(report.not_locked, ('b.xml',))
        self.assertEqual(report.out_of_bounds, ())

    def test_invalid_document_uri(self):
        catalog = self.catalog(batch='B1')

        with self.assertRaises(InvalidDocumentURIError):
            catalog.get_document('a.xml#fragment')
        with self.assertRaises(InvalidDocumentURIError):
            catalog.get_document('')
        with self.assertRaises(InvalidDocumentURIError):
            catalog.get_document('a.xml?query=1')

    def test_directory_is_not_a_document(self):
        catalog = self.catalog(['sub'], batch='B1')

        with self.assertRaises(InvalidDocumentURIError):
            catalog.get_document('sub')

    def test_malformed_locked_document(self):
        self.write('broken.xml', '<broken>')
        catalog = self.catalog(['broken.xml'], batch='B1')

        with self.assertRaises(DocumentParseError) as ctx:
            catalog.get_document('broken.xml')
        self.assertEqual(ctx.exception.code, 'FODC0002')

    def test_forbidden_entities(self):
        self.write('entity.xml', '<!DOCTYPE root [<!ENTITY e "text">]><root>&e;</root>')
        catalog = self.catalog(['entity.xml'], batch='B1')

        with self.assertRaises(DocumentParseError):
            catalog.get_document('entity.xml')

    def test_tolerant_locked_set_and_strict_mode(self):
        outside = self.base_dir.parent.joinpath('outside-catalog-strict.xml')
        outside.write_text('<outside/>', encoding='utf-8')
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

        catalog = self.catalog(['a.xml', str(outside), 'b.xml'], batch='B1')
        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog.report().out_of_bounds, (str(outside),))

        with self.assertRaises(OutOfScopeError):
            LocalXMLCatalog(self.base_dir, ['a.xml', str(outside)], batch='B1', strict=True)

    #
    # Batch isolation and single parsing of shared documents
    def test_batch_cache_isolation(self):
        batch1 = self.catalog(batch='B1')
        batch2 = self.catalog(batch='B2')

        doc1 = batch1.get_document('a.xml')
        doc2 = batch2.get_document('a.xml')
        self.assertIsNot(doc1, doc2)
        self.assertEqual(doc1.uri, doc2.uri)

        self.assertIs(batch1.get_document('a.xml'), doc1)
        self.assertIs(batch2.get_document('a.xml'), doc2)

    def test_shared_cache_keeps_batches_separate(self):
        cache = {}
        batch1 = self.catalog(batch='B1', cache=cache)
        batch2 = self.catalog(batch='B2', cache=cache)

        doc1 = batch1.get_document('a.xml')
        doc2 = batch2.get_document('a.xml')
        self.assertIsNot(doc1, doc2)
        self.assertEqual(set(cache), {
            ('B1', self.uri('a.xml')), ('B2', self.uri('a.xml'))
        })

        batch1.clear_cache()
        self.assertNotIn(('B1', self.uri('a.xml')), cache)
        self.assertIn(('B2', self.uri('a.xml')), cache)

    def test_new_batch_shares_entries_but_not_cache(self):
        batch1 = self.catalog(batch='B1')
        doc1 = batch1.get_document('a.xml')

        batch2 = batch1.new_batch('B2')
        doc2 = batch2.get_document('a.xml')
        self.assertIsNot(doc1, doc2)
        self.assertEqual(batch2.keys(), batch1.keys())

    def test_shared_document_is_parsed_once_per_batch(self):
        original = LocalXMLCatalog._parse_document
        parsing = []

        def parse_document(catalog, path, uri):
            parsing.append((catalog.batch, uri))
            return original(catalog, path, uri)

        with patch.object(LocalXMLCatalog, '_parse_document', parse_document):
            catalog = self.catalog(['a.xml', 'b.xml'], batch='B1',
                                   collections={'all': ['a.xml', 'b.xml']})
            self.assertIs(catalog.get_document('a.xml'), catalog.get_document('a.xml'))

            root = ElementTree.XML('<root/>')
            self.assertEqual(
                select(root, "fn:doc('a.xml')//v/text()", catalog=catalog), ['1']
            )
            self.assertEqual(
                select(root, "fn:collection('all')//v/text()", catalog=catalog), ['1', '2']
            )
            self.assertEqual(
                select(root, "fn:collection()//v/text()", catalog=catalog), ['1', '2']
            )
            self.assertEqual(parsing, [('B1', self.uri('a.xml')), ('B1', self.uri('b.xml'))])

    def test_concurrent_access_parses_once(self):
        import threading

        cache = {}
        catalogs = [self.catalog(['a.xml'], batch='B1', cache=cache) for _ in range(2)]
        barrier = threading.Barrier(8)
        results = []

        def work(index):
            barrier.wait()
            results.append(catalogs[index % 2].get_document('a.xml'))

        threads = [threading.Thread(target=work, args=(x,)) for x in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len({id(x) for x in results}), 1)

    def test_document_nodes_keep_original_uri_across_documents(self):
        catalog = self.catalog(batch='B1', collections={'all': ['a.xml', 'b.xml']})
        root = ElementTree.XML('<root/>')

        result = select(
            root,
            "for $u in ('a.xml', 'b.xml') return "
            "fn:document-uri(fn:doc($u))",
            parser=XPath31Parser,
            catalog=catalog,
        )
        self.assertEqual(result, [self.uri('a.xml'), self.uri('b.xml')])

        result = select(
            root,
            "for $d in fn:collection('all') return fn:document-uri($d)",
            parser=XPath31Parser,
            catalog=catalog,
        )
        self.assertEqual(result, [self.uri('a.xml'), self.uri('b.xml')])

    def test_cross_document_references(self):
        self.write('main.xml', '<main><part ref="a.xml"/><part ref="b.xml"/></main>')
        catalog = LocalXMLCatalog(
            self.base_dir, ['main.xml', 'a.xml', 'b.xml'], batch='B1'
        )
        root = ElementTree.XML('<root/>')

        result = select(
            root,
            "for $ref in fn:doc('main.xml')/main/part/@ref "
            "return fn:document-uri(fn:doc(string($ref)))",
            parser=XPath31Parser,
            catalog=catalog,
        )
        self.assertEqual(result, [self.uri('a.xml'), self.uri('b.xml')])

        # The shared documents are the same node instances reached via refs.
        self.assertIs(catalog.get_document('a.xml'), catalog.get_document(self.uri('a.xml')))
        self.assertIs(catalog.get_document('b.xml'), catalog.get_document('b.xml'))

    #
    # fn:doc / fn:doc-available integration
    def test_doc_function_resolution_and_errors(self):
        catalog = self.catalog(['a.xml', 'missing.xml'], batch='B1')
        root = ElementTree.XML('<root/>')

        self.assertEqual(
            select(root, "fn:doc('a.xml')/a/v/text()", catalog=catalog), ['1']
        )
        self.assertEqual(select(root, "fn:doc-available('a.xml')", catalog=catalog), True)
        self.assertEqual(select(root, "fn:doc-available('b.xml')", catalog=catalog), False)
        self.assertEqual(select(root, "fn:doc(())", catalog=catalog), [])
        self.assertEqual(select(root, "fn:doc-available(())", catalog=catalog), False)

        with self.assertRaises(ValueError) as ctx:
            select(root, "fn:doc('missing.xml')", catalog=catalog)
        self.assertIn('FODC0002', str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            select(root, "fn:doc('b.xml')", catalog=catalog)
        self.assertIn('FODC0005', str(ctx.exception))

        self.assertEqual(select(root, "fn:doc-available('b.xml')", catalog=catalog), False)

    def test_doc_respects_static_base_uri(self):
        catalog = self.catalog(['a.xml'], batch='B1')
        parser = XPath31Parser(base_uri=self.base_dir.as_uri() + '/')
        root = ElementTree.XML('<root/>')
        context = XPathContext(root, catalog=catalog)

        token = parser.parse('fn:doc("a.xml")/a/v/text()')
        self.assertEqual(token.get_results(context), ['1'])
        self.assertEqual(
            parser.parse('fn:document-uri(fn:doc("a.xml"))').get_results(context),
            self.uri('a.xml')
        )

    #
    # fn:collection / fn:uri-collection integration
    def test_collection_named_directory_and_default(self):
        catalog = self.catalog(
            batch='B1',
            collections={'first': ['a.xml', 'b.xml']},
            default_collection=['c.xml'],
        )
        root = ElementTree.XML('<root/>')

        self.assertEqual(
            select(root, "fn:collection('first')//v/text()", catalog=catalog), ['1', '2']
        )
        self.assertEqual(
            select(root, "fn:collection()//v/text()", catalog=catalog), ['3']
        )
        self.assertEqual(
            select(root, "fn:collection('sub')//v/text()", catalog=catalog), ['4', '5']
        )

        result = select(
            root, "fn:uri-collection('first')", parser=XPath30Parser, catalog=catalog
        )
        self.assertEqual([str(x) for x in result], [self.uri('a.xml'), self.uri('b.xml')])

        result = select(root, "fn:uri-collection()", parser=XPath30Parser, catalog=catalog)
        self.assertEqual([str(x) for x in result], [self.uri('c.xml')])

    def test_collection_errors(self):
        catalog = self.catalog(batch='B1', collections={'empty': []})
        root = ElementTree.XML('<root/>')

        self.assertEqual(select(root, "fn:collection('empty')", catalog=catalog), [])

        with self.assertRaises(ValueError) as ctx:
            select(root, "fn:collection('unknown')", catalog=catalog)
        self.assertIn('FODC0002', str(ctx.exception))

        empty_dir = self.base_dir.joinpath('empty-dir')
        empty_dir.mkdir()
        with self.assertRaises(MissingCollectionError):
            catalog.get_collection(str(empty_dir))

        outside = self.base_dir.parent
        with self.assertRaises(DocumentAccessError):
            catalog.get_collection(str(outside))

    def test_collection_returns_document_nodes_with_uris(self):
        catalog = self.catalog(
            batch='B1', collections={'first': ['a.xml', 'b.xml']}
        )
        collection = select(
            ElementTree.XML('<root/>'),
            "for $d in fn:collection('first') return fn:document-uri($d)",
            parser=XPath31Parser,
            catalog=catalog,
        )
        self.assertEqual(collection, [self.uri('a.xml'), self.uri('b.xml')])

        documents = catalog.get_collection('first')
        self.assertIs(documents[0], catalog.get_document('a.xml'))
        self.assertIs(documents[1], catalog.get_document('b.xml'))

    #
    # Context and selector API
    def test_context_catalog(self):
        catalog = self.catalog(batch='B1')
        root = ElementTree.XML('<root/>')
        context = XPathContext(root, catalog=catalog)
        self.assertIs(context.catalog, catalog)

        with self.assertRaises(TypeError):
            XPathContext(root, catalog='not-a-catalog')

    def test_selector_with_catalog(self):
        catalog = self.catalog(batch='B1')
        selector = Selector("fn:doc('a.xml')/a/v/text()")
        root = ElementTree.XML('<root/>')

        self.assertEqual(selector.select(root, catalog=catalog), ['1'])
        self.assertEqual(list(selector.iter_select(root, catalog=catalog)), ['1'])

    def test_catalog_with_lxml_documents(self):
        try:
            import lxml.etree as lxml_etree
        except ImportError:
            self.skipTest('lxml library is not installed')

        with patch.object(LocalXMLCatalog, '_parse_document',
                          lambda self, path, uri: __import__(
                              'elementpath').get_node_tree(
                              lxml_etree.parse(str(path)), uri=uri)):
            catalog = self.catalog(['a.xml'], batch='B1')
            root = lxml_etree.XML('<root/>')
            self.assertEqual(
                select(root, "fn:doc('a.xml')/a/v/text()", catalog=catalog), ['1']
            )

    def test_catalog_repr(self):
        catalog = self.catalog(['a.xml'], batch='B1')
        self.assertIn("batch='B1'", repr(catalog))
        self.assertIn('locked=1', repr(catalog))

    def test_public_aliases_and_helpers(self):
        from elementpath.local_xml import load_catalog, create_catalog, \
            resolve_document, resolve_collection, LocalDocumentCatalog

        catalog = load_catalog(self.base_dir, ['a.xml', 'b.xml'], batch='B1')
        self.assertIsInstance(catalog, LocalDocumentCatalog)
        self.assertEqual(
            create_catalog(self.base_dir, ['a.xml'], batch='B2').get_document('a.xml').uri,
            self.uri('a.xml')
        )

        context = XPathContext(ElementTree.XML('<root/>'), catalog=catalog)
        self.assertEqual(
            select(context.root.value, "fn:doc('a.xml')/a/v/text()", catalog=catalog), ['1']
        )
        self.assertEqual(resolve_document(catalog, 'a.xml').uri, self.uri('a.xml'))
        self.assertEqual(
            [doc.uri for doc in resolve_collection(catalog)], list(catalog.keys())
        )


if __name__ == '__main__':
    unittest.main()
