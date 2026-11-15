# -*- coding: utf-8 -*-

import unittest

from pylatexenc.latexwalker import LatexWalker
from pylatexenc.latexnodes.nodes import (
    LatexMacroNode,
    LatexEnvironmentNode,
    LatexGroupNode,
)


def _parse(latex):
    lw = LatexWalker(latex)
    nodelist, _ = lw.parse_content()
    return nodelist


def _macro_nodes(nodelist, macroname):
    return [
        n for n in nodelist
        if n.isNodeType(LatexMacroNode) and n.macroname == macroname
    ]


def _nargs(node):
    if node.nodeargd is None:
        return None
    return len(node.nodeargd.argnlist)


class TestNewcommandUpdatesLatexContext(unittest.TestCase):

    def test_newcommand_defines_argument_signature(self):
        nodelist = _parse(r"\newcommand{\foo}[1]{AAA #1} and \foo{bar}")
        foo = _macro_nodes(nodelist, 'foo')[0]
        self.assertEqual(_nargs(foo), 1)
        self.assertEqual(foo.nodeargd.argnlist[0].nodelist[0].chars, 'bar')

    def test_newcommand_without_arguments(self):
        nodelist = _parse(r"\newcommand\foo{AAA} \foo rest")
        foo = _macro_nodes(nodelist, 'foo')[0]
        self.assertEqual(_nargs(foo), 0)

    def test_newcommand_unbraced_name(self):
        nodelist = _parse(r"\newcommand\foo[2]{AAA} \foo{x}{y}")
        foo = _macro_nodes(nodelist, 'foo')[0]
        self.assertEqual(_nargs(foo), 2)

    def test_newcommand_with_optional_argument_default(self):
        nodelist = _parse(
            r"\newcommand{\foo}[2][D]{AAA} \foo{bar} and \foo[x]{bar}"
        )
        foo_plain, foo_opt = _macro_nodes(nodelist, 'foo')
        self.assertEqual(
            [a is not None for a in foo_plain.nodeargd.argnlist],
            [False, True]
        )
        self.assertEqual(
            [a is not None for a in foo_opt.nodeargd.argnlist],
            [True, True]
        )
        self.assertEqual(foo_opt.nodeargd.argnlist[0].delimiters, ('[', ']'))

    def test_renewcommand_redefines_argument_signature(self):
        nodelist = _parse(
            r"\newcommand{\foo}[1]{A} \foo{x} \renewcommand{\foo}[2]{B} \foo{x}{y}"
        )
        foo_first, foo_second = _macro_nodes(nodelist, 'foo')
        self.assertEqual(_nargs(foo_first), 1)
        self.assertEqual(_nargs(foo_second), 2)

    def test_providecommand_defines_argument_signature(self):
        nodelist = _parse(r"\providecommand{\foo}[1]{A} \foo{x}")
        foo = _macro_nodes(nodelist, 'foo')[0]
        self.assertEqual(_nargs(foo), 1)

    def test_definition_applies_from_declaration_point_on(self):
        # a call that precedes the definition follows the rules in effect
        # at that time (here: unknown macro, no arguments)
        nodelist = _parse(r"\foo{x} \newcommand{\foo}[1]{A} \foo{y}")
        foo_before, foo_after = _macro_nodes(nodelist, 'foo')
        self.assertEqual(_nargs(foo_before), 0)
        self.assertEqual(_nargs(foo_after), 1)

    def test_redefinition_in_group_is_scoped_to_the_group(self):
        nodelist = _parse(
            r"\newcommand{\foo}[1]{A} {\renewcommand{\foo}[2]{B} \foo{x}{y}} \foo{z}"
        )
        group = [
            n for n in nodelist if n.isNodeType(LatexGroupNode)
        ][0]
        foo_in = _macro_nodes(group.nodelist, 'foo')[0]
        foo_out = _macro_nodes(nodelist, 'foo')[0]
        self.assertEqual(_nargs(foo_in), 2)
        self.assertEqual(_nargs(foo_out), 1)

    def test_redefinition_in_group_applies_to_nested_content(self):
        nodelist = _parse(
            r"\newcommand{\foo}[1]{A} "
            r"{\renewcommand{\emph}[2]{B} \foo{\emph{a}{b}}} "
            r"\foo{\emph{a}{b}}"
        )
        group = [
            n for n in nodelist if n.isNodeType(LatexGroupNode)
        ][0]
        foo_in = _macro_nodes(group.nodelist, 'foo')[0]
        foo_out = _macro_nodes(nodelist, 'foo')[0]

        # in the group, \emph takes two arguments -> the argument group holds
        # the macro alone; outside, it takes one argument and the second
        # braced group stands on its own
        emph_in = foo_in.nodeargd.argnlist[0].nodelist[0]
        self.assertEqual(_nargs(emph_in), 2)
        self.assertEqual(len(foo_in.nodeargd.argnlist[0].nodelist), 1)

        emph_out = foo_out.nodeargd.argnlist[0].nodelist[0]
        self.assertEqual(_nargs(emph_out), 1)
        self.assertEqual(len(foo_out.nodeargd.argnlist[0].nodelist), 2)

    def test_unknown_macros_keep_their_original_behavior(self):
        nodelist = _parse(
            r"\unknownmacro{arg} \newcommand{\foo}[1]{A} \otherunknown{x}"
        )
        for name in ('unknownmacro', 'otherunknown'):
            node = _macro_nodes(nodelist, name)[0]
            self.assertEqual(_nargs(node), 0)

    def test_newenvironment_defines_argument_signature(self):
        nodelist = _parse(
            r"\newenvironment{myenv}[1]{B}{E} "
            r"\begin{myenv}{a}text\end{myenv}"
        )
        env = [
            n for n in nodelist if n.isNodeType(LatexEnvironmentNode)
        ][0]
        self.assertEqual(env.environmentname, 'myenv')
        self.assertEqual(_nargs(env), 1)

    def test_newenvironment_redefinition_in_group_is_scoped(self):
        nodelist = _parse(
            r"\newenvironment{myenv}[1]{B}{E} "
            r"{\renewenvironment{myenv}[2]{B}{E} "
            r"\begin{myenv}{a}{b}text\end{myenv}} "
            r"\begin{myenv}{c}text\end{myenv}"
        )
        group = [
            n for n in nodelist if n.isNodeType(LatexGroupNode)
        ][0]
        env_in = [
            n for n in group.nodelist if n.isNodeType(LatexEnvironmentNode)
        ][0]
        env_out = [
            n for n in nodelist if n.isNodeType(LatexEnvironmentNode)
        ][0]
        self.assertEqual(_nargs(env_in), 2)
        self.assertEqual(_nargs(env_out), 1)

    def test_definitions_do_not_leak_between_documents(self):
        # parsing two documents in either order must give the same result
        # for each of them
        doc_a = r"\newcommand{\foo}[1]{A} \foo{x}"
        doc_b = r"\foo{y}"

        def parse_foo_nargs(latex):
            return [
                _nargs(n)
                for n in _macro_nodes(_parse(latex), 'foo')
            ]

        a_first = (parse_foo_nargs(doc_a), parse_foo_nargs(doc_b))
        b_first = (parse_foo_nargs(doc_b), parse_foo_nargs(doc_a))

        self.assertEqual(a_first[0], b_first[1])  # doc A identical
        self.assertEqual(a_first[1], b_first[0])  # doc B identical
        self.assertEqual(a_first[0], [1])   # doc A: \foo takes 1 arg
        self.assertEqual(a_first[1], [0])   # doc B: \foo unknown, no args


if __name__ == '__main__':
    unittest.main()
