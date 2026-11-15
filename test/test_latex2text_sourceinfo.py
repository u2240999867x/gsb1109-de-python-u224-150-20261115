# -*- coding: utf-8 -*-

#
# Tests for the source mapping produced alongside the plain-text conversion,
# i.e. the code in `pylatexenc/latex2text/_sourceinfo.py` and its integration
# into `LatexNodes2Text`.
#
# Like test_latex2text.py, this file is compiled to JavaScript and run under
# node, so it sticks to the restricted python subset that Transcrypt
# understands and only uses ASCII output (the unicode math alphabets would not
# index the same way in code units as in code points).
#

import unittest

from pylatexenc import macrospec
from pylatexenc.macrospec._spechelpers import std_macro

from pylatexenc.latex2text import (
    LatexNodes2Text,
    TextSourceInfo,
    TextSourceSpan,
    MacroTextSpec,
    SOURCE_KIND_SOURCE,
    SOURCE_KIND_MACRO,
    SOURCE_KIND_COMMENT,
)

# The JavaScript build has no function that builds the default specification
# databases; import them from where they are defined and pass them explicitly.
from pylatexenc.latexwalker._get_defaultspecs import (
    get_default_latex_context_db as get_latexwalker_default_context_db
)
from pylatexenc.latex2text._get_defaultspecs import (
    get_default_latex_context_db as get_latex2text_default_context_db
)


def make_default_l2t(math_mode='text', math_fontstyle=None,
                     text_fontstyle=False, **options):
    options['math_mode'] = math_mode
    options['math_fontstyle'] = math_fontstyle
    options['text_fontstyle'] = text_fontstyle
    return LatexNodes2Text(latex_context=get_latex2text_default_context_db(),
                           **options)


def default_convert(latex, **options):
    l2t = make_default_l2t(**options)
    return l2t.latex_to_text_with_source_info(
        latex, latex_context=get_latexwalker_default_context_db())


class TestSourceInfoBasic(unittest.TestCase):

    def test_plain_text_maps_to_source_ranges(self):
        src = "hello world"
        info = default_convert(src)
        self.assertEqual(info.text, src)
        spans = info.spans
        self.assertTrue(len(spans) >= 1)
        for span in spans:
            self.assertEqual(span.kind, SOURCE_KIND_SOURCE)
            # the rendered text of a characters node is the source text itself
            self.assertEqual(
                info.text[span.text_pos:span.text_pos_end],
                src[span.pos:span.pos_end])

    def test_rendered_text_matches_plain_conversion(self):
        src = "a \\textbf{b} c % comment\nd"
        l2t = make_default_l2t()
        plain = l2t.latex_to_text(
            src, latex_context=get_latexwalker_default_context_db())
        info = default_convert(src)
        self.assertEqual(info.text, plain)
        self.assertIsInstance(info, TextSourceInfo)

    def test_every_output_position_is_covered(self):
        src = r"see \ref{lab} and \textit{x} now"
        info = default_convert(src)
        for i in range(len(info.text)):
            self.assertTrue(
                len(info.sources_at(i)) > 0,
                "output position {} has no source span".format(i))


class TestMacroGeneratedText(unittest.TestCase):

    def _custom_l2t(self):
        text_db = macrospec.LatexContextDb()
        text_db.add_context_category(
            'tests',
            macros=[
                MacroTextSpec('wrap', simplify_repl='[%(1)s]'),
                MacroTextSpec('lit', simplify_repl='LITERAL'),
            ],
            environments=[],
            specials=[],
        )
        walker_db = macrospec.LatexContextDb()
        walker_db.add_context_category(
            'tests',
            macros=[std_macro('wrap', False, 1), std_macro('lit', False, 0)],
            environments=[],
            specials=[],
        )
        return LatexNodes2Text(latex_context=text_db), walker_db

    def test_macro_span_covers_whole_invocation(self):
        l2t, walker_db = self._custom_l2t()
        src = r"a\wrap{XY}b"
        info = l2t.latex_to_text_with_source_info(src, latex_context=walker_db)
        self.assertEqual(info.text, "a[XY]b")
        macro_spans = info.macro_spans()
        self.assertTrue(len(macro_spans) >= 1)
        # every macro span points at the entire invocation, never at a made-up
        # sub-fragment of it, even though several output regions share it
        for span in macro_spans:
            self.assertEqual(span.macroname, 'wrap')
            self.assertEqual(src[span.pos:span.pos_end], r"\wrap{XY}")
        # the distinct source ranges collapse to the one invocation range
        ranges = []
        for s in macro_spans:
            pair = (s.pos, s.pos_end)
            if pair not in ranges:
                ranges.append(pair)
        invoke_start = src.index(r"\wrap{XY}")
        self.assertEqual(ranges,
                         [(invoke_start, invoke_start + len(r"\wrap{XY}"))])

    def test_argument_is_placed_precisely(self):
        l2t, walker_db = self._custom_l2t()
        src = r"\wrap{XY}"
        info = l2t.latex_to_text_with_source_info(src, latex_context=walker_db)
        # the rendered 'XY' points back at the argument source 'XY'
        pos = info.text.index('XY')
        arg_ranges = [
            (s.pos, s.pos_end) for s in info.sources_at(pos)
            if s.kind == SOURCE_KIND_SOURCE
        ]
        self.assertIn((src.index('XY'), src.index('XY') + 2), arg_ranges)

    def test_partial_expansion_hit_does_not_claim_source_fragment(self):
        # The '[' and ']' exist only because the macro replacement inserted
        # them; the original TeX has no independent fragment there.  They must
        # be covered by the macro span and not by a 'source' span.
        l2t, walker_db = self._custom_l2t()
        src = r"\wrap{XY}"
        info = l2t.latex_to_text_with_source_info(src, latex_context=walker_db)
        bracket_pos = info.text.index('[')
        kinds = [s.kind for s in info.sources_at(bracket_pos)]
        self.assertIn(SOURCE_KIND_MACRO, kinds)
        self.assertNotIn(SOURCE_KIND_SOURCE, kinds)

    def test_pure_literal_replacement_attributes_to_invocation(self):
        l2t, walker_db = self._custom_l2t()
        src = r"\lit"
        info = l2t.latex_to_text_with_source_info(src, latex_context=walker_db)
        self.assertEqual(info.text, "LITERAL")
        for span in info.spans:
            self.assertEqual(span.kind, SOURCE_KIND_MACRO)
            self.assertEqual(span.macroname, 'lit')

    def test_repeated_argument_is_many_to_many(self):
        text_db = macrospec.LatexContextDb()
        text_db.add_context_category(
            'tests',
            macros=[MacroTextSpec('rep', simplify_repl='%(1)s|%(1)s')],
            environments=[], specials=[])
        walker_db = macrospec.LatexContextDb()
        walker_db.add_context_category(
            'tests',
            macros=[std_macro('rep', False, 1)],
            environments=[], specials=[])
        l2t = LatexNodes2Text(latex_context=text_db)
        src = r"\rep{XY}"
        info = l2t.latex_to_text_with_source_info(src, latex_context=walker_db)
        self.assertEqual(info.text, "XY|XY")
        arg_src = (src.index('XY'), src.index('XY') + 2)
        # both occurrences of 'XY' map back to the same source range
        first = info.text.index('XY')
        second = info.text.index('XY', first + 1)
        self.assertIn(arg_src, info.source_ranges_at(first))
        self.assertIn(arg_src, info.source_ranges_at(second))
        # ... and they are distinct output regions
        self.assertNotEqual(first, second)

    def test_nested_arguments_remain_traceable(self):
        text_db = macrospec.LatexContextDb()
        text_db.add_context_category(
            'tests',
            macros=[
                MacroTextSpec('outer', simplify_repl='[%(1)s]'),
                MacroTextSpec('inner', simplify_repl='I'),
            ],
            environments=[], specials=[])
        walker_db = macrospec.LatexContextDb()
        walker_db.add_context_category(
            'tests',
            macros=[std_macro('outer', False, 1), std_macro('inner', False, 0)],
            environments=[], specials=[])
        l2t = LatexNodes2Text(latex_context=text_db)
        src = r"\outer{a\inner b}"
        info = l2t.latex_to_text_with_source_info(src, latex_context=walker_db)
        self.assertEqual(info.text, "[aIb]")
        inner_pos = info.text.index('I')
        spans = info.sources_at(inner_pos)
        names = [s.macroname for s in spans if s.macroname is not None]
        # the generated 'I' is known to come from the inner macro, and still
        # lies within the outer invocation
        self.assertIn('inner', names)
        self.assertIn('outer', names)
        # the surrounding literal characters keep the outer span
        self.assertEqual(
            [s.macroname for s in info.sources_at(info.text.index('a'))
             if s.macroname is not None],
            ['outer'])


class TestFracPartialHit(unittest.TestCase):

    def test_engine_inserted_slash_has_no_source_fragment(self):
        src = r"$\frac{a+b}{c}$"
        info = default_convert(src)
        self.assertEqual(info.text, "(a+b)/c")
        slash = info.text.index('/')
        # The '/' is the engine's notation, not a character of the source.  It
        # may still be covered by the span over the whole formula and by the
        # macro invocation, but no narrow source fragment may be claimed for
        # it: every 'source' span at the slash must span the whole math node.
        source_spans = [s for s in info.sources_at(slash)
                        if s.kind == SOURCE_KIND_SOURCE]
        self.assertTrue(len(source_spans) > 0)
        for span in source_spans:
            self.assertEqual((span.pos, span.pos_end), (0, len(src)))
        macro = [s for s in info.sources_at(slash) if s.macroname == 'frac']
        self.assertEqual(len(macro), 1)
        # a real source character, the denominator 'c', is located exactly
        c_pos = info.text.index('c')
        c_src = src.rindex('c')
        self.assertIn((c_src, c_src + 1), info.source_ranges_at(c_pos))


class TestComments(unittest.TestCase):

    def test_dropped_comment_is_recorded_separately(self):
        src = "a % hidden\nb"
        info = default_convert(src)
        self.assertEqual(len(info.discarded_comments), 1)
        comment_span = info.discarded_comments[0]
        self.assertEqual(comment_span.kind, SOURCE_KIND_COMMENT)
        self.assertIsNone(comment_span.text_pos)
        self.assertIn('hidden', comment_span.source_text(src))
        # the dropped comment never backs any output text
        for span in info.spans:
            self.assertNotEqual(span.kind, SOURCE_KIND_COMMENT)

    def test_kept_comment_appears_as_comment_span(self):
        src = "a % hidden\nb"
        info = default_convert(src, keep_comments=True)
        self.assertIn('% hidden', info.text)
        pct = info.text.index('%')
        kinds = [s.kind for s in info.sources_at(pct)]
        self.assertIn(SOURCE_KIND_COMMENT, kinds)
        self.assertEqual(len(info.discarded_comments), 0)

    def test_surviving_post_space_points_at_its_source(self):
        src = "a % hidden\n  b"
        info = default_convert(src)
        # the indentation that survives is source whitespace following the
        # comment, not text generated by the comment
        indent = info.text.index('  ')
        for span in info.sources_at(indent):
            self.assertEqual(span.kind, SOURCE_KIND_SOURCE)
            self.assertEqual(
                src[span.pos:span.pos_end],
                info.text[span.text_pos:span.text_pos_end])


class TestSourceInfoQueries(unittest.TestCase):

    def test_sources_for_range_and_ranges(self):
        text_db = macrospec.LatexContextDb()
        text_db.add_context_category(
            'tests', macros=[MacroTextSpec('wrap', simplify_repl='[%(1)s]')],
            environments=[], specials=[])
        walker_db = macrospec.LatexContextDb()
        walker_db.add_context_category(
            'tests', macros=[std_macro('wrap', False, 1)],
            environments=[], specials=[])
        l2t = LatexNodes2Text(latex_context=text_db)
        src = r"\wrap{XY}"
        info = l2t.latex_to_text_with_source_info(src, latex_context=walker_db)
        hits = info.sources_for_range(0, len(info.text))
        self.assertTrue(len(hits) > 0)
        # every returned span really intersects the queried region
        for span in hits:
            self.assertTrue(span.text_pos_end > 0)
            self.assertTrue(span.text_pos < len(info.text))
        ranges = info.source_ranges_at(info.text.index('X'))
        self.assertTrue(all(pair[0] is not None for pair in ranges))

    def test_span_identity_helpers(self):
        text_db = macrospec.LatexContextDb()
        text_db.add_context_category(
            'tests', macros=[MacroTextSpec('wrap', simplify_repl='[%(1)s]')],
            environments=[], specials=[])
        walker_db = macrospec.LatexContextDb()
        walker_db.add_context_category(
            'tests', macros=[std_macro('wrap', False, 1)],
            environments=[], specials=[])
        l2t = LatexNodes2Text(latex_context=text_db)
        info = l2t.latex_to_text_with_source_info(
            r"\wrap{XY}", latex_context=walker_db)
        span = info.macro_spans()[0]
        self.assertIsInstance(span, TextSourceSpan)
        self.assertEqual(span.macroname, 'wrap')
        self.assertIsNone(span.environmentname)
        self.assertIsNone(span.specials_chars)


if __name__ == '__main__':
    unittest.main()
