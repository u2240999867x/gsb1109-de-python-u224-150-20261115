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
import unittest
import warnings
import concurrent.futures
import xml.etree.ElementTree as ElementTree

from elementpath import select, iter_select, Selector, XPath2Parser
from elementpath.xpath3 import XPath30Parser

try:
    import lxml.etree as lxml_etree
except ImportError:
    lxml_etree = None


class XPathSelectorsTest(unittest.TestCase):
    etree = ElementTree

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = cls.etree.XML('<author>Dickens</author>')

    def test_select_function(self):
        self.assertEqual(select(self.root, 'text()'), ['Dickens'])
        self.assertEqual(select(self.root, '$a', variables={'a': 1}), 1)

        self.assertEqual(
            select(self.root, '$a', variables={'a': 1}, variable_types={'a': 'xs:decimal'}), 1
        )

    def test_iter_select_function(self):
        self.assertEqual(list(iter_select(self.root, 'text()')), ['Dickens'])
        self.assertEqual(list(iter_select(self.root, '$a', variables={'a': True})), [True])

    def test_selector_class(self):
        selector = Selector('/A')
        self.assertEqual(repr(selector), "Selector(path='/A', parser=XPath2Parser)")
        self.assertEqual(selector.namespaces, XPath2Parser.DEFAULT_NAMESPACES)

        selector = Selector('text()')
        self.assertEqual(selector.select(self.root), ['Dickens'])
        self.assertEqual(list(selector.iter_select(self.root)), ['Dickens'])

        selector = Selector('$a')
        self.assertEqual(selector.select(self.root, variables={'a': 1}), 1)
        self.assertEqual(list(selector.iter_select(self.root, variables={'a': 1})), [1])

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            selector = Selector('$a', variables={'a': 1})
            self.assertEqual(len(w), 1)
            self.assertEqual(w[0].category, DeprecationWarning)

        self.assertEqual(selector.select(self.root), 1)
        self.assertEqual(list(selector.iter_select(self.root)), [1])

    def test_issue_001(self):
        selector = Selector("//FullPath[ends-with(., 'Temp')]")
        self.assertEqual(selector.select(self.etree.XML('<A/>')), [])
        self.assertEqual(selector.select(self.etree.XML('<FullPath/>')), [])
        root = self.etree.XML('<FullPath>High Temp</FullPath>')
        self.assertEqual(selector.select(root), [root])

    def test_issue_042(self):
        selector1 = Selector('text()')
        selector2 = Selector('sup[last()]/preceding-sibling::text()')
        root = self.etree.XML('<root>a<sup>1</sup>b<sup>2</sup>c<sup>3</sup></root>')
        self.assertEqual(selector1.select(root), selector2.select(root))

        selector2 = Selector('sup[1]/following-sibling::text()')
        root = self.etree.XML('<root><sup>1</sup>b<sup>2</sup>c<sup>3</sup>d</root>')
        self.assertEqual(selector1.select(root), selector2.select(root))

    def test_fragment_argument__issue_081(self):
        # xml1 contains the xml-stylesheet tag
        xml1 = b"""<?xml version="1.0" encoding="UTF-8"?>
        <?xml-stylesheet type='text/xsl' href='test.xsl'?>
        <root>
            <first>
                <second>
                    value
                </second>
            </first>
        </root>
        """

        # the same as xml1, but without the xml-stylesheet tag
        xml2 = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
        <root>
            <first>
                <second>
                    value
                </second>
            </first>
        </root>
        """

        root1 = self.etree.XML(xml1)
        root2 = self.etree.XML(xml2)
        query = "first/second"

        if hasattr(root1, 'xpath'):
            self.assertEqual(select(root1, query), [])
        else:
            self.assertEqual(select(root1, query), root1[0][:])

        self.assertEqual(select(root1, query, fragment=True), root1[0][:])
        self.assertEqual(select(root2, query), root2[0][:])

        self.assertEqual(select(root1, query, fragment=False), [])
        self.assertEqual(select(root2, query, fragment=False), [])

        query = "root/first/second"

        self.assertEqual(select(root1, query, fragment=False), root1[0][:])
        self.assertEqual(select(root2, query, fragment=False), root2[0][:])

    def test_reused_selector_focus_isolation(self):
        # A rule that temporarily moves the focus to an attribute node must
        # leave no trace on the next record processed by the same selector.
        selector = Selector('./@id')
        rows = self.etree.XML('<root><row id="1"/><row id="2"/><row id="3"/></root>')
        self.assertEqual([selector.select(row) for row in rows],
                         [['1'], ['2'], ['3']])

        # position()/last() are recomputed for each record's own focus
        selector = Selector('./row[position()=last()]/@id')
        for _ in range(3):
            root = self.etree.XML('<root><row id="1"/><row id="2"/></root>')
            self.assertEqual(selector.select(root), ['2'])

    def test_reused_selector_timezone_isolation(self):
        selector = Selector('implicit-timezone()')
        root = self.etree.XML('<a/>')
        self.assertEqual(
            selector.select(root, timezone='Z'),
            selector.select(root, timezone='Z')
        )
        self.assertNotEqual(
            selector.select(root, timezone='Z'),
            selector.select(root, timezone='-05:00')
        )

    def test_reused_selector_parallel_focus_isolation(self):
        # Two batches running the same compiled expression concurrently must
        # keep their position(), last() and context item independent.
        selector = Selector('./row[position()=last()]/@id')
        xml_doc = '<root><row id="1"/><row id="2"/><row id="3"/></root>'

        def batch(_):
            return [selector.select(self.etree.XML(xml_doc)) for _ in range(100)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            batches = list(executor.map(batch, (0, 1)))

        for results in batches:
            self.assertTrue(all(result == ['3'] for result in results))

    def test_reused_selector_variable_isolation(self):
        # Variable bindings produced inside an evaluation must not leak across
        # records sharing the same compiled expression.
        selector = Selector(
            "for $r in ./row return string($r/@id)", parser=XPath30Parser
        )
        root = self.etree.XML('<root><row id="1"/><row id="2"/></root>')
        self.assertEqual(selector.select(root), ['1', '2'])
        self.assertEqual(selector.select(root), ['1', '2'])

    def test_inline_function_variable_isolation__reused_selector(self):
        # An inline function parameter shadowing an outer variable must not
        # overwrite the outer binding for subsequent iterations/records.
        selector = Selector(
            "for $x in (1, 2) return (function($x) { $x }(10), $x)",
            parser=XPath30Parser,
        )
        root = self.etree.XML('<a/>')
        self.assertEqual(selector.select(root), [10, 1, 10, 2])
        self.assertEqual(selector.select(root), [10, 1, 10, 2])


@unittest.skipIf(lxml_etree is None, "The lxml library is not installed")
class LxmlXPathSelectorsTest(XPathSelectorsTest):
    etree = lxml_etree

    def test_issue_058(self):
        tei = """<?xml version='1.0' encoding='UTF8'?>
        <?xml-model type="application/xml"?>
        <TEI xmlns="http://www.tei-c.org/ns/1.0">
          <text>
                <pb n="page1"/>
                <pb n="page2"/>
          </text>
        </TEI>
        """

        doc = self.etree.XML(tei.encode())
        namespaces = {'': "http://www.tei-c.org/ns/1.0"}
        k = None
        for k, p in enumerate(select(doc, '//pb', namespaces), start=1):
            self.assertEqual(p.attrib['n'], f'page{k}')
            self.assertEqual(p.xpath('./@n'), [f'page{k}'])
            self.assertEqual(select(doc, './@n'), [])
            self.assertEqual(select(p, './@n'), [f'page{k}'])
            self.assertEqual(select(doc, './@n', item=p), [f'page{k}'])
        else:
            self.assertEqual(k, 2)

    def test_issue_074(self):
        root = lxml_etree.XML("<root><trunk><branch></branch></trunk></root>")

        result = select(root, "trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]

        result = select(root, "/root/trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]

        root = lxml_etree.XML("<!--comment--><root><trunk><branch></branch></trunk></root>")

        result = select(root, "trunk")
        self.assertEqual(result, [])

        result = select(root, "root/trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]

        result = select(root, "/root/trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]


if __name__ == '__main__':
    unittest.main()
