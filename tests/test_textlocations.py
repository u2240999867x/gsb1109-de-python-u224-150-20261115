#!/usr/bin/env python
#
# Copyright (c), 2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import unittest
import xml.etree.ElementTree as ElementTree

from elementpath import XPath1Parser, XPathContext, TextNode, \
    ElementNode, CommentNode, get_node_tree
from elementpath.exceptions import ElementPathTypeError
from elementpath.extras.textlocations import TextMatch, locate_text, \
    iter_locate_text, text_node_source, TEXT, TAIL


class TextLocationsTest(unittest.TestCase):

    def setUp(self):
        self.root = ElementTree.XML(
            '<root>head<a>same</a>same<b>same</b>tail</root>'
        )

    def test_matches_are_distinct_text_nodes(self):
        matches = locate_text(self.root, '//text()')
        self.assertEqual([m.value for m in matches],
                         ['head', 'same', 'same', 'same', 'tail'])
        self.assertTrue(all(isinstance(m, TextMatch) for m in matches))
        self.assertTrue(all(isinstance(m.node, TextNode) for m in matches))

        # Two equal strings still have different node identities.
        same = [m for m in matches if m.value == 'same']
        self.assertEqual(len(same), 3)
        self.assertEqual(len({id(m.node) for m in same}), 3)

    def test_text_and_tail_provenance(self):
        matches = locate_text(self.root, '//text()')
        sources = [(m.value, m.source, m.owner.name,
                    m.anchor.name if m.anchor is not None else None)
                   for m in matches]
        self.assertEqual(sources, [
            ('head', TEXT, 'root', None),
            ('same', TEXT, 'a', None),
            ('same', TAIL, 'root', 'a'),
            ('same', TEXT, 'b', None),
            ('tail', TAIL, 'root', 'b'),
        ])
        self.assertTrue(matches[2].is_tail)
        self.assertFalse(matches[0].is_tail)

        # The tail is exposed as a single node, not the parent full text.
        self.assertEqual(matches[2].value, 'same')
        self.assertEqual(matches[2].owner.string_value,
                         'headsamesamesametail')

    def test_positions_and_fragment_order(self):
        matches = locate_text(self.root, '//text()')
        self.assertEqual([m.fragment_order for m in matches], [1, 2, 3, 4, 5])
        positions = [m.node_position for m in matches]
        self.assertEqual(positions, sorted(positions))

        # text_position is the predicate of text()[N] within the owning element.
        root_matches = locate_text(self.root, '/root/text()')
        self.assertEqual([m.value for m in root_matches], ['head', 'same', 'tail'])
        self.assertEqual([m.text_position for m in root_matches], [1, 2, 3])
        self.assertEqual([m.path for m in root_matches], [
            '/Q{}root[1]/text()[1]',
            '/Q{}root[1]/text()[2]',
            '/Q{}root[1]/text()[3]',
        ])
        self.assertTrue(all(m.owner.name == 'root' for m in root_matches))
        self.assertTrue(all(m.element_position == root_matches[0].element_position
                            for m in root_matches))

    def test_iter_locate_text_order(self):
        generator = iter_locate_text(self.root, '//text()')
        matches = list(generator)
        self.assertEqual([m.value for m in matches],
                         ['head', 'same', 'same', 'same', 'tail'])

    def test_predicate_selection(self):
        matches = locate_text(self.root, '/root/text()[2]')
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match.value, 'same')
        self.assertEqual(match.source, TAIL)
        self.assertEqual(match.anchor.name, 'a')

        last = locate_text(self.root, '/root/text()[last()]')
        self.assertEqual([m.value for m in last], ['tail'])
        self.assertEqual(last[0].source, TAIL)

    def test_comment_and_processing_instruction_tails(self):
        root = ElementTree.Element('root')
        root.text = 't0'
        child = ElementTree.SubElement(root, 'c')
        child.tail = 'tail-c'
        comment = ElementTree.Comment('cmt')
        root.append(comment)
        comment.tail = 'tail-cmt'
        pi = ElementTree.ProcessingInstruction('pi', 'd')
        root.append(pi)
        pi.tail = 'tail-pi'

        matches = locate_text(root, '/root/text()')
        self.assertEqual([m.value for m in matches],
                         ['t0', 'tail-c', 'tail-cmt', 'tail-pi'])
        self.assertEqual([m.source for m in matches],
                         [TEXT, TAIL, TAIL, TAIL])
        self.assertIsInstance(matches[2].anchor, CommentNode)
        self.assertEqual(matches[1].anchor.name, 'c')
        self.assertEqual(matches[3].anchor.name, 'pi')
        self.assertEqual([m.text_position for m in matches], [1, 2, 3, 4])
        self.assertEqual(len({id(m.node) for m in matches}), 4)

    def test_nested_tail_owner(self):
        root = ElementTree.XML('<a><b>x</b>tail-b</a>')
        matches = locate_text(root, '//text()')
        self.assertEqual([(m.value, m.source, m.owner.name,
                           m.anchor.name if m.anchor else None) for m in matches],
                         [('x', TEXT, 'b', None),
                          ('tail-b', TAIL, 'a', 'b')])

    def test_empty_sequence(self):
        root = ElementTree.XML('<root><a/></root>')
        self.assertEqual(locate_text(root, '//text()'), [])
        self.assertEqual(list(iter_locate_text(root, '//text()')), [])

    def test_non_text_match_is_rejected(self):
        with self.assertRaises(ElementPathTypeError):
            locate_text(self.root, '/root')
        with self.assertRaises(ElementPathTypeError):
            locate_text(self.root, 'count(//text())')

    def test_invalid_parser_class(self):
        with self.assertRaises(ElementPathTypeError):
            locate_text(self.root, '//text()', parser=object)

    def test_xpath1_parser(self):
        matches = locate_text(self.root, '//text()', parser=XPath1Parser)
        self.assertEqual([m.value for m in matches],
                         ['head', 'same', 'same', 'same', 'tail'])

    def test_accepts_built_node_tree(self):
        tree = get_node_tree(self.root)
        matches = locate_text(tree, '//text()')
        self.assertEqual([m.value for m in matches],
                         ['head', 'same', 'same', 'same', 'tail'])
        self.assertIsInstance(matches[0].owner, ElementNode)

    def test_text_node_source_helper(self):
        context = XPathContext(self.root)
        text_nodes = [n for n in context.root.iter() if isinstance(n, TextNode)]
        source, owner, anchor = text_node_source(text_nodes[0])
        self.assertEqual(source, TEXT)
        self.assertIsNone(anchor)

        tail_node = text_nodes[2]  # tail of <a>
        source, owner, anchor = text_node_source(tail_node)
        self.assertEqual(source, TAIL)
        self.assertEqual(anchor.name, 'a')

    def test_value_equals_node_value(self):
        matches = locate_text(self.root, '//text()')
        self.assertTrue(all(m.value == m.node.value for m in matches))


if __name__ == '__main__':
    unittest.main()
