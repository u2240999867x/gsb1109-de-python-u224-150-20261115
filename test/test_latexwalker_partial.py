# -*- coding: utf-8 -*-

#
# Tests for `LatexWalker.parse_partial_content()` — the interface for LaTeX
# code whose end is cut in the middle of a construct.
#
# This file is not part of the JavaScript build's test set.
#

import unittest

from pylatexenc.latexwalker import (
    LatexWalker,
    LatexWalkerParseError,
    LatexWalkerPartialParseResult,
    LatexCharsNode,
    LatexMacroNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMathNode,
    LatexCommentNode,
)
from pylatexenc.latexnodes.nodes import LatexNodeList


def _flatten(nodes):
    out = []
    for node in nodes:
        out.append(node)
        if isinstance(node, LatexNodeList):
            out.extend(_flatten(node.nodelist))
            continue
        nodelist = getattr(node, 'nodelist', None)
        if nodelist is not None:
            out.extend(_flatten(nodelist))
        nodeargd = getattr(node, 'nodeargd', None)
        if nodeargd is not None:
            for arg in (getattr(nodeargd, 'argnlist', None) or []):
                if isinstance(arg, LatexNodeList):
                    out.extend(_flatten(arg.nodelist))
                elif hasattr(arg, 'pos') and hasattr(arg, 'pos_end'):
                    out.extend(_flatten([ arg ]))
    return out


class TestParsePartialContent(unittest.TestCase):

    def parse_partial(self, latextext):
        lw = LatexWalker(latextext, tolerant_parsing=True)
        return lw, lw.parse_partial_content()

    def assertTrustedPrefixConsistency(self, latextext, result):
        # the trusted nodes start at the beginning, are contiguous, and their
        # text is exactly the corresponding prefix of the source
        self.assertEqual(result.trusted_nodes.pos, 0)
        self.assertEqual(result.trusted_range[0], 0)
        self.assertEqual(result.trusted_range[1], result.trusted_nodes.pos_end)

        pos = 0
        for node in result.trusted_nodes:
            self.assertEqual(node.pos, pos)
            self.assertGreaterEqual(node.pos_end, node.pos)
            self.assertLessEqual(node.pos_end, len(latextext))
            self.assertEqual(
                node.latex_verbatim(),
                latextext[node.pos:node.pos_end],
            )
            pos = node.pos_end
        self.assertEqual(pos, result.trusted_range[1])

        if result.complete:
            self.assertIsNone(result.incomplete_range)
            self.assertIsNone(result.incomplete_latex)
            self.assertEqual(result.trusted_range, (0, len(latextext)))
        else:
            self.assertEqual(result.incomplete_range, (pos, len(latextext)))
            self.assertEqual(result.incomplete_latex, latextext[pos:])
            self.assertIsNotNone(result.strict_error)
            self.assertIsNotNone(result.tolerant_nodes)

    def test_complete_input(self):
        latextext = r'Hello \textbf{world}, and $x+y$.'
        lw, result = self.parse_partial(latextext)

        self.assertIsInstance(result, LatexWalkerPartialParseResult)
        self.assertTrue(result.complete)
        self.assertEqual(result.trusted_range, (0, len(latextext)))
        self.assertIsNone(result.incomplete_range)
        self.assertIsNone(result.strict_error)
        self.assertIsNone(result.tolerant_nodes)

        nodelist, parsing_state_delta = lw.parse_content()
        self.assertEqual(len(result.trusted_nodes), len(nodelist))
        self.assertEqual(result.trusted_nodes.pos_end, nodelist.pos_end)

        self.assertTrustedPrefixConsistency(latextext, result)

    def test_complete_input_keeps_parsing_state_delta(self):
        # A complete parse reports whatever parsing state delta the strict
        # parse produced.  (Most default macros, including the plain
        # \newcommand, do not alter the parsing state, so we exercise the
        # attribute against a fresh walker rather than depend on one here.)

        latextext = r'Hello \textbf{world}.'

        lw = LatexWalker(latextext, tolerant_parsing=False)
        result = lw.parse_partial_content()
        fresh_delta = lw.parse_content()[1]

        self.assertTrue(result.complete)
        self.assertIs(result.parsing_state_delta, fresh_delta)

    def test_unterminated_macro_argument(self):
        latextext = r'Hello \textbf{world'
        lw, result = self.parse_partial(latextext)

        self.assertFalse(result.complete)
        self.assertTrustedPrefixConsistency(latextext, result)

        # the complete chars node before the truncated macro is trusted
        self.assertEqual(len(result.trusted_nodes), 1)
        self.assertTrue(result.trusted_nodes[0].isNodeType(LatexCharsNode))
        self.assertEqual(result.trusted_nodes[0].chars, 'Hello ')
        self.assertEqual(result.trusted_range, (0, 6))
        self.assertEqual(result.incomplete_range, (6, len(latextext)))
        self.assertEqual(result.incomplete_latex, r'\textbf{world')

        # the truncated macro is recovered only by the tolerant parse
        self.assertTrue(
            any(n.isNodeType(LatexMacroNode) and n.macroname == 'textbf'
                for n in result.tolerant_nodes)
        )
        self.assertFalse(
            any(n.isNodeType(LatexMacroNode)
                for n in result.trusted_nodes)
        )

    def test_unterminated_group(self):
        latextext = r'A {group {nested'
        lw, result = self.parse_partial(latextext)

        self.assertFalse(result.complete)
        self.assertTrustedPrefixConsistency(latextext, result)
        self.assertEqual(result.trusted_range, (0, 2))
        self.assertEqual(result.incomplete_latex, r'{group {nested')

    def test_unterminated_math(self):
        for latextext in [r'A $x + y', r'A $$x + y', r'A \[ x + y']:
            with self.subTest(latextext=latextext):
                lw, result = self.parse_partial(latextext)
                self.assertFalse(result.complete)
                self.assertTrustedPrefixConsistency(latextext, result)
                self.assertEqual(result.trusted_range, (0, 2))
                self.assertTrue(
                    any(n.isNodeType(LatexMathNode) for n in result.tolerant_nodes)
                )

    def test_unterminated_environment(self):
        latextext = 'A \\begin{equation} x+y'
        lw, result = self.parse_partial(latextext)

        self.assertFalse(result.complete)
        self.assertTrustedPrefixConsistency(latextext, result)
        self.assertEqual(result.trusted_range, (0, 2))
        self.assertTrue(
            any(n.isNodeType(LatexEnvironmentNode) and n.environmentname == 'equation'
                for n in result.tolerant_nodes)
        )

    def test_trailing_escape_character(self):
        # A lone trailing backslash begins a macro name that never came.  The
        # strict recovery reports only the text before the preceding
        # whitespace, because the whitespace could still belong to the macro
        # token that is being read.
        latextext = 'A \\'
        lw, result = self.parse_partial(latextext)

        self.assertFalse(result.complete)
        self.assertTrustedPrefixConsistency(latextext, result)
        self.assertEqual(result.trusted_range, (0, 1))
        self.assertEqual(result.incomplete_range, (1, 3))

    def test_incomplete_environment_name(self):
        for latextext in [r'A \begin', r'A \begin{', r'A \begin{verbatim']:
            with self.subTest(latextext=latextext):
                lw, result = self.parse_partial(latextext)
                self.assertFalse(result.complete)
                self.assertTrustedPrefixConsistency(latextext, result)
                self.assertLessEqual(result.trusted_range[1], 1)

    def test_completed_tail_matches_trusted_prefix(self):
        # Completing the input and parsing it again must leave the trusted
        # prefix unchanged, both structurally and textually.
        cases = [
            (r'Hello \textbf{world', '}'),
            (r'A {group {nested', '}}'),
            (r'A \frac{1}{2', '}'),
            (r'A \begin{equation} x+y', r'\end{equation}'),
            (r'A $x+y', '$'),
            (r'A \verb+unterminated', '+'),
            ('A \\begin{verbatim}\ncode \\foo{bar}', '\n\\end{verbatim}'),
            ('A \\begin{lstlisting}\ncode \\foo{bar}', '\n\\end{lstlisting}'),
        ]

        for latextext, tail in cases:
            with self.subTest(latextext=latextext):
                lw, result = self.parse_partial(latextext)
                self.assertFalse(result.complete)
                self.assertTrustedPrefixConsistency(latextext, result)

                completed = latextext + tail
                full_nodes, _ = LatexWalker(completed, tolerant_parsing=False).parse_content()

                self.assertEqual(full_nodes.latex_verbatim(), completed)

                end = result.trusted_range[1]
                self.assertEqual(completed[:end], latextext[:end])

                # every trusted node still occurs, with the same type and
                # extent, in the completed parse
                all_nodes = _flatten(full_nodes)
                for node in result.trusted_nodes:
                    matches = [
                        n for n in all_nodes
                        if n.nodeType() is node.nodeType()
                        and n.pos == node.pos
                        and n.pos_end == node.pos_end
                    ]
                    self.assertTrue(
                        matches,
                        "trusted node {!r} is not present in the completed parse"
                        .format(node),
                    )
                    if isinstance(node, LatexCharsNode):
                        self.assertEqual(matches[0].chars, node.chars)

    def test_unterminated_verbatim_slashes_are_not_macros(self):
        # Backslashes inside an unterminated verbatim environment are literal
        # text.  They must not appear as macro calls in the tolerant parse,
        # and nothing from the verbatim contents may be trusted.
        for latextext, endcode in [
                ('A \\begin{verbatim}\ncode \\foo{bar} \\baz', '\n\\end{verbatim}'),
                ('A \\begin{lstlisting}\nint x = 1; \\foo', '\n\\end{lstlisting}'),
        ]:
            with self.subTest(latextext=latextext):
                lw, result = self.parse_partial(latextext)

                self.assertFalse(result.complete)
                self.assertTrustedPrefixConsistency(latextext, result)
                self.assertEqual(result.trusted_range, (0, 2))
                self.assertEqual(
                    result.incomplete_latex,
                    latextext[2:],
                )

                # no macro nodes anywhere in the tolerant parse: the
                # backslashes are verbatim content
                tolerant_nodes = _flatten(result.tolerant_nodes)
                self.assertFalse(
                    [ n for n in tolerant_nodes if isinstance(n, LatexMacroNode) ],
                    "backslashes in unterminated verbatim content must not be "
                    "parsed as macros",
                )

                # the verbatim contents are reachable as literal text
                environment = [
                    n for n in result.tolerant_nodes
                    if n.isNodeType(LatexEnvironmentNode)
                ][0]
                verbatim_text = environment.nodeargd.verbatim_text
                self.assertIn(verbatim_text, latextext)
                self.assertIsNone(environment.nodeargd.verbatim_delimiters)

                # the literal verbatim content keeps the backslashes of the
                # source text, character for character
                body = environment.nodelist[0]
                self.assertEqual(body.chars, verbatim_text)
                self.assertEqual(body.chars, latextext[body.pos:body.pos_end])

                # and they do not appear in the trusted nodes
                for node in result.trusted_nodes:
                    self.assertNotIsInstance(node, LatexEnvironmentNode)

                # completing the environment keeps the trusted prefix as is
                full_nodes = _flatten(
                    LatexWalker(latextext + endcode, tolerant_parsing=False)
                    .parse_content()[0]
                )
                for node in result.trusted_nodes:
                    self.assertTrue(any(
                        n.nodeType() is node.nodeType()
                        and n.pos == node.pos
                        and n.pos_end == node.pos_end
                        for n in full_nodes
                    ))

    def test_genuine_syntax_error_is_raised(self):
        # A mismatched closing environment is malformed input, not a
        # truncation, so the strict error is reported as usual.
        for latextext in [
                r'\begin{foo} x \end{bar}',
                r'A $x } y',
        ]:
            with self.subTest(latextext=latextext):
                lw = LatexWalker(latextext, tolerant_parsing=True)
                with self.assertRaises(LatexWalkerParseError):
                    lw.parse_partial_content()

    def test_stray_closing_at_end_of_input(self):
        # A closing brace right at the end can belong to an unfinished inner
        # group, so it is treated like any other unfinished construct.
        for latextext in [r'A $x }', r'A {a ] b']:
            with self.subTest(latextext=latextext):
                lw, result = self.parse_partial(latextext)
                self.assertFalse(result.complete)
                self.assertTrustedPrefixConsistency(latextext, result)
                self.assertEqual(result.trusted_range, (0, 2))

    def test_tolerant_parsing_setting_is_restored(self):
        latextext = r'Hello \textbf{world'

        lw = LatexWalker(latextext, tolerant_parsing=False)
        result = lw.parse_partial_content()
        self.assertFalse(result.complete)
        self.assertFalse(lw.tolerant_parsing)

        lw = LatexWalker(latextext, tolerant_parsing=True)
        result = lw.parse_partial_content()
        self.assertFalse(result.complete)
        self.assertTrue(lw.tolerant_parsing)

    def test_ignores_trailing_space(self):
        latextext = 'A \\textbf{one} B '
        lw, result = self.parse_partial(latextext)

        self.assertTrue(result.complete)
        self.assertTrustedPrefixConsistency(latextext, result)

    def test_empty_input(self):
        lw, result = self.parse_partial('')

        self.assertTrue(result.complete)
        self.assertEqual(result.trusted_range, (0, 0))
        self.assertEqual(len(result.trusted_nodes), 0)
        self.assertIsNone(result.incomplete_range)

    def test_comment_at_end(self):
        latextext = 'A % trailing comment'
        lw, result = self.parse_partial(latextext)

        self.assertTrue(result.complete)
        self.assertTrue(result.trusted_nodes[-1].isNodeType(LatexCommentNode))


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
