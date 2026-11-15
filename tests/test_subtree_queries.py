#!/usr/bin/env python
#
# Copyright (c), 2018-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
Tests for the subtree query pattern: a context rooted at a node of a shared
node tree (a rooted subtree) confines absolute paths, the parent axis and the
root() function to the subtree root, preserving the original node identity.
"""
import unittest
import xml.etree.ElementTree as ElementTree

try:
    import lxml.etree as lxml_etree
except ImportError:
    lxml_etree = None

from elementpath import XPath2Parser, XPathContext, get_node_tree, select
from elementpath.xpath_nodes import ElementNode


class RootedSubtreeTest(unittest.TestCase):
    """
    Two tenants upload different subtrees of the same XML package. Each tenant
    gets a context rooted at its authorized node of a shared node tree.
    """
    @classmethod
    def setUpClass(cls):
        cls.etree = ElementTree
        cls.xml = '<pkg><tenantA><a1/><a2><x/></a2></tenantA>' \
                  '<tenantB><b1/><b2/></tenantB></pkg>'
        cls.parser = XPath2Parser()

    def setUp(self):
        self.root = self.etree.XML(self.xml)
        self.shared_tree = get_node_tree(self.root)
        self.subtree_a = self.shared_tree[0]
        self.subtree_b = self.shared_tree[1]

    def evaluate(self, expr, node):
        context = XPathContext(node)
        return self.parser.parse(expr).evaluate(context)

    def test_context_root_type(self):
        context = XPathContext(self.subtree_a)
        self.assertFalse(context.is_document())
        self.assertTrue(context.is_fragment())
        self.assertTrue(context.is_rooted_subtree())
        self.assertIsNone(context.document)
        self.assertIsNotNone(context.root.parent)

    def test_absolute_path(self):
        result = self.evaluate('/a1', self.subtree_a)
        self.assertEqual(result, [self.subtree_a[0]])

        result = self.evaluate('/', self.subtree_a)
        self.assertEqual(result, [self.subtree_a])

        result = self.evaluate('//x', self.subtree_a)
        self.assertEqual(result, [self.subtree_a[1][0]])

        # The package root is not addressable with an absolute path
        self.assertEqual(self.evaluate('/tenantB', self.subtree_a), [])
        self.assertEqual(self.evaluate('//b1', self.subtree_a), [])

    def test_parent_axis(self):
        self.assertEqual(self.evaluate('..', self.subtree_a), [])
        self.assertEqual(self.evaluate('parent::node()', self.subtree_a), [])

        # The parent axis works within the subtree and stops at its root
        result = self.evaluate('a2/..', self.subtree_a)
        self.assertEqual(result, [self.subtree_a])
        result = self.evaluate('//x/../..', self.subtree_a)
        self.assertEqual(result, [self.subtree_a])

    def test_ancestor_axes(self):
        result = self.evaluate('//x/ancestor::*', self.subtree_a)
        self.assertEqual(result, [self.subtree_a, self.subtree_a[1]])

        result = self.evaluate('//x/ancestor-or-self::*', self.subtree_a)
        self.assertEqual(
            result, [self.subtree_a, self.subtree_a[1], self.subtree_a[1][0]]
        )

    def test_root_function(self):
        result = self.evaluate('root()', self.subtree_a)
        self.assertEqual(result, self.subtree_a)

        result = self.evaluate('root(//x)', self.subtree_a)
        self.assertEqual(result, self.subtree_a)

        result = self.evaluate('root()/a1', self.subtree_a)
        self.assertEqual(result, [self.subtree_a[0]])

    def test_no_access_to_sibling_tenants(self):
        # The original parent reference cannot be used to reach sibling subtrees
        self.assertEqual(self.evaluate('../tenantB', self.subtree_a), [])
        self.assertEqual(self.evaluate('../tenantB/b1', self.subtree_a), [])
        self.assertEqual(self.evaluate('//x/ancestor::pkg', self.subtree_a), [])
        self.assertEqual(self.evaluate('//x/ancestor::tenantB', self.subtree_a), [])
        self.assertEqual(self.evaluate('following-sibling::*', self.subtree_a), [])
        self.assertEqual(self.evaluate('preceding-sibling::*', self.subtree_b), [])
        self.assertEqual(self.evaluate('//a2/following::b1', self.subtree_a), [])
        self.assertEqual(self.evaluate('//b1/preceding::a1', self.subtree_b), [])

    def test_node_identity_preserved(self):
        # Results are the nodes of the shared tree, so identity is traceable
        result = self.evaluate('//x', self.subtree_a)
        self.assertIs(result[0], self.subtree_a[1][0])
        self.assertIs(result[0].value, self.root[0][1][0])

    def test_shared_tree_not_mutated(self):
        self.evaluate('/a1', self.subtree_a)
        self.evaluate('..', self.subtree_a)
        self.assertIsNone(self.shared_tree.parent)
        self.assertIs(self.subtree_a.parent, self.shared_tree)
        self.assertIs(self.subtree_b.parent, self.shared_tree)

    def test_fragment_argument(self):
        context = XPathContext(self.subtree_a, fragment=True)
        self.assertTrue(context.is_rooted_subtree())
        self.assertEqual(self.parser.parse('..').evaluate(context), [])
        result = self.parser.parse('/a1').evaluate(context)
        self.assertEqual(result, [self.subtree_a[0]])

    def test_select_function(self):
        result = select(self.subtree_a, '/a1')
        self.assertEqual(result, [self.root[0][0]])
        self.assertEqual(select(self.subtree_a, '../tenantB'), [])

    def test_tenants_are_isolated_from_each_other(self):
        result_a = self.evaluate('//*/name(..)', self.subtree_a)
        result_b = self.evaluate('//*/name(..)', self.subtree_b)
        self.assertEqual(set(result_a), {'tenantA', 'a2'})
        self.assertEqual(set(result_b), {'tenantB'})


@unittest.skipIf(lxml_etree is None, "The lxml library is not installed")
class LxmlRootedSubtreeTest(RootedSubtreeTest):
    @classmethod
    def setUpClass(cls):
        cls.etree = lxml_etree
        cls.xml = '<pkg><tenantA><a1/><a2><x/></a2></tenantA>' \
                  '<tenantB><b1/><b2/></tenantB></pkg>'
        cls.parser = XPath2Parser()


if __name__ == '__main__':
    unittest.main()
