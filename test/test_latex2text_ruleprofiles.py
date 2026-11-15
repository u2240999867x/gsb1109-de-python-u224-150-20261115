# -*- coding: utf-8 -*-
#
# Tests for the named, prioritized groups of text-extraction rules in
# `pylatexenc/latex2text/_ruleprofiles.py`.
#
# A profile typically redefines only one or a few names and leaves every other
# default rule alone.  The tests pin down: that the three naming domains
# (macros, environments, specials) are resolved independently, that a
# higher-priority profile shadows a lower-priority one only while it is
# enabled, that disabling or removing the winner restores the lower-priority
# rule, and that contexts built for two documents never share any state.
#

import unittest

from pylatexenc import macrospec
from pylatexenc.latex2text import (
    LatexNodes2Text,
    MacroTextSpec,
    EnvironmentTextSpec,
    SpecialsTextSpec,
    RuleProfile,
    RuleProfileDb,
    ShadowedRule,
)

# The default definition databases are imported from the modules that hold
# them and threaded through explicitly, because the convenience
# `get_default_latex_context_db()` functions are left out of the JavaScript
# build.
from pylatexenc.latexwalker._get_defaultspecs import (
    get_default_latex_context_db as get_latexwalker_default_context_db
)
from pylatexenc.latex2text._get_defaultspecs import (
    get_default_latex_context_db as get_latex2text_default_context_db
)


# A macro that exists in the default catalogue, so that overriding it really
# is a shadowing exercise and removing the override can restore a default.
_OVERRIDDEN_MACRO = 'beta'
_DEFAULT_BETA_TEXT = u'\N{GREEK SMALL LETTER BETA}'


def _make_parser_profile_db():
    # The parser-side mirror of the text-side profiles below.  The parser only
    # needs to know the argument structure; our overrides take no arguments.
    db = RuleProfileDb()
    db.add_profile(
        RuleProfile(
            'journal-a',
            macros=[
                macrospec.MacroSpec(_OVERRIDDEN_MACRO, ''),
                macrospec.MacroSpec('journalamacro', ''),
            ],
            environments=[macrospec.EnvironmentSpec('journalbox', '')],
        )
    )
    db.add_profile(
        RuleProfile(
            'journal-b',
            macros=[macrospec.MacroSpec(_OVERRIDDEN_MACRO, '')],
        )
    )
    db.add_profile(
        RuleProfile(
            'journal-c',
            specials=[macrospec.SpecialsSpec('---')],
        )
    )
    return db


def _make_text_profile_db():
    db = RuleProfileDb()
    db.add_profile(
        RuleProfile(
            'journal-a',
            description='rules of journal A',
            macros=[
                MacroTextSpec(_OVERRIDDEN_MACRO, simplify_repl='BETA-A'),
                MacroTextSpec('journalamacro', simplify_repl='A-ONLY'),
            ],
            environments=[
                EnvironmentTextSpec('journalbox', simplify_repl='[%(body)s]'),
            ],
        ),
        priority=0,
    )
    db.add_profile(
        RuleProfile(
            'journal-b',
            # this profile only touches a single macro; no environment and no
            # specials definition may leak out of it
            macros=[
                MacroTextSpec(_OVERRIDDEN_MACRO, simplify_repl='BETA-B'),
            ],
        ),
        priority=10,
    )
    # a third, even higher-priority profile that lives in the specials domain
    db.add_profile(
        RuleProfile(
            'journal-c',
            specials=[
                SpecialsTextSpec('---', simplify_repl='CUSTOM-DASH'),
            ],
        ),
        priority=20,
    )
    return db


def _convert(latex, text_context, parser_context):
    l2t = LatexNodes2Text(latex_context=text_context)
    return l2t.latex_to_text(latex, latex_context=parser_context)


class TestRuleProfileValidation(unittest.TestCase):

    def test_name_is_required(self):
        with self.assertRaises(ValueError):
            RuleProfile('')
        with self.assertRaises(ValueError):
            RuleProfile(None)

    def test_spec_must_carry_the_indexing_attribute(self):
        with self.assertRaises(ValueError):
            RuleProfile('bad', macros=[object()])
        with self.assertRaises(ValueError):
            RuleProfile('bad', environments=[MacroTextSpec('x', '')])

    def test_duplicate_name_within_a_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            RuleProfile(
                'bad',
                macros=[
                    MacroTextSpec('x', simplify_repl='one'),
                    MacroTextSpec('x', simplify_repl='two'),
                ],
            )

    def test_three_domains_are_kept_apart_in_the_profile(self):
        profile = RuleProfile(
            'p',
            macros=[MacroTextSpec('m', simplify_repl='m')],
            environments=[EnvironmentTextSpec('e', simplify_repl='e')],
            specials=[SpecialsTextSpec('~', simplify_repl='~')],
        )
        self.assertEqual(profile.macro_names(), ['m'])
        self.assertEqual(profile.environment_names(), ['e'])
        self.assertEqual(profile.specials_chars(), ['~'])
        # the returned lists are copies, the profile stays immutable
        snapshot = profile.macros
        snapshot.append('sentinel')
        self.assertEqual(profile.macro_names(), ['m'])
        self.assertIsNone(profile.get_macro_spec('e'))
        self.assertIsNotNone(profile.get_environment_spec('e'))

    def test_profile_db_rejects_duplicates_and_unknown_names(self):
        db = RuleProfileDb()
        profile = RuleProfile('p', macros=[MacroTextSpec('m', '')])
        db.add_profile(profile)
        with self.assertRaises(ValueError):
            db.add_profile(RuleProfile('p'))
        with self.assertRaises(ValueError):
            db.add_profile('not-a-profile')
        with self.assertRaises(KeyError):
            db.remove_profile('missing')
        with self.assertRaises(KeyError):
            db.set_priority('missing', 5)
        with self.assertRaises(KeyError):
            db.build_context(get_latex2text_default_context_db(),
                             enabled=('missing',))


class TestPriorityAndShadowing(unittest.TestCase):

    def setUp(self):
        self.parser_db = _make_parser_profile_db()
        self.text_db = _make_text_profile_db()
        self.parser_base = get_latexwalker_default_context_db()
        self.text_base = get_latex2text_default_context_db()

    def test_base_context_is_not_modified(self):
        base_spec = self.text_base.get_macro_spec(_OVERRIDDEN_MACRO)
        self.text_db.build_context(self.text_base, enabled=('journal-a',))
        self.text_db.build_context(self.text_base, enabled=('journal-b',))
        self.assertIs(
            self.text_base.get_macro_spec(_OVERRIDDEN_MACRO), base_spec
        )
        self.assertEqual(
            [c for c in self.text_base.categories()
             if c.startswith('rule-profile:')],
            []
        )

    def test_profile_category_order_follows_priority(self):
        context = self.text_db.build_context(self.text_base)
        self.assertEqual(
            context.categories()[:3],
            ['rule-profile:journal-c',
             'rule-profile:journal-b',
             'rule-profile:journal-a'],
        )

    def test_higher_priority_wins(self):
        context = self.text_db.build_context(
            self.text_base, enabled=('journal-a', 'journal-b')
        )
        self.assertEqual(
            context.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl,
            'BETA-B',
        )

    def test_same_priority_uses_registration_order(self):
        db = RuleProfileDb()
        db.add_profile(RuleProfile(
            'first', macros=[MacroTextSpec('m', simplify_repl='one')]
        ), priority=5)
        db.add_profile(RuleProfile(
            'second', macros=[MacroTextSpec('m', simplify_repl='two')]
        ), priority=5)
        base = get_latex2text_default_context_db()
        context = db.build_context(base)
        self.assertEqual(
            context.get_macro_spec('m').simplify_repl, 'one'
        )
        self.assertEqual(
            [c for c in context.categories() if c.startswith('rule-profile:')],
            ['rule-profile:first', 'rule-profile:second'],
        )

    def test_set_priority_reorders_the_profiles(self):
        self.text_db.set_priority('journal-a', 50)
        context = self.text_db.build_context(
            self.text_base, enabled=('journal-a', 'journal-b')
        )
        self.assertEqual(
            context.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl,
            'BETA-A',
        )

    def test_disabling_the_winner_restores_the_lower_priority_rule(self):
        both = self.text_db.build_context(
            self.text_base, enabled=('journal-a', 'journal-b')
        )
        self.assertEqual(
            both.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl, 'BETA-B'
        )
        only_a = self.text_db.build_context(
            self.text_base, enabled=('journal-a',)
        )
        self.assertEqual(
            only_a.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl, 'BETA-A'
        )
        # the context built earlier keeps its own answer: documents do not
        # interfere with one another
        self.assertEqual(
            both.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl, 'BETA-B'
        )

    def test_removing_the_winner_restores_the_lower_priority_rule(self):
        self.text_db.remove_profile('journal-b')
        context = self.text_db.build_context(self.text_base)
        self.assertEqual(
            context.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl, 'BETA-A'
        )
        self.assertEqual(self.text_db.profile_names(),
                         ['journal-a', 'journal-c'])

    def test_unmentioned_names_keep_their_default_rule(self):
        context = self.text_db.build_context(
            self.text_base, enabled=('journal-a',)
        )
        # '\alpha' is not mentioned by any profile, its default rule survives
        self.assertEqual(
            context.get_macro_spec('alpha').simplify_repl,
            self.text_base.get_macro_spec('alpha').simplify_repl,
        )

    def test_three_naming_domains_are_independent(self):
        # journal-b overrides one macro and defines nothing in the other
        # domains; the environment and specials tables must be unchanged
        plain = self.text_db.build_context(
            self.text_base, enabled=('journal-b',)
        )
        self.assertEqual(
            plain.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl, 'BETA-B'
        )
        self.assertEqual(
            plain.d['rule-profile:journal-b']['environments'], {}
        )
        self.assertEqual(
            plain.d['rule-profile:journal-b']['specials'], {}
        )
        # the specials profile, in turn, never touches the macro tables
        dash = self.text_db.build_context(
            self.text_base, enabled=('journal-c',)
        )
        self.assertEqual(
            dash.get_specials_spec('---').simplify_repl, 'CUSTOM-DASH'
        )
        self.assertEqual(
            dash.d['rule-profile:journal-c']['macros'], {}
        )


class TestEndToEndConversion(unittest.TestCase):

    def setUp(self):
        self.parser_db = _make_parser_profile_db()
        self.text_db = _make_text_profile_db()
        self.parser_base = get_latexwalker_default_context_db()
        self.text_base = get_latex2text_default_context_db()

    def _build(self, enabled):
        return (
            self.text_db.build_context(self.text_base, enabled=enabled),
            self.parser_db.build_context(self.parser_base, enabled=enabled),
        )

    def test_override_and_default_coexist(self):
        text_context, parser_context = self._build(('journal-a',))
        self.assertEqual(
            _convert(r'\beta', text_context, parser_context), 'BETA-A'
        )
        # the macro the profile did not mention keeps the default rendering
        self.assertEqual(
            _convert(r'\alpha ', text_context, parser_context),
            u'\N{GREEK SMALL LETTER ALPHA}',
        )
        self.assertEqual(
            _convert(r'\journalamacro', text_context, parser_context),
            'A-ONLY',
        )

    def test_environment_rule_of_a_profile_applies(self):
        text_context, parser_context = self._build(('journal-a',))
        self.assertEqual(
            _convert(r'\begin{journalbox}hi\end{journalbox}',
                     text_context, parser_context),
            '[hi]',
        )

    def test_priority_changes_the_extracted_text(self):
        low_text, low_parser = self._build(('journal-a',))
        high_text, high_parser = self._build(('journal-a', 'journal-b'))
        self.assertEqual(
            _convert(r'\beta', low_text, low_parser), 'BETA-A'
        )
        self.assertEqual(
            _convert(r'\beta', high_text, high_parser), 'BETA-B'
        )

    def test_disabling_high_priority_profile_restores_the_text(self):
        high_text, high_parser = self._build(('journal-a', 'journal-b'))
        low_text, low_parser = self._build(('journal-a',))
        self.assertEqual(
            _convert(r'\beta', high_text, high_parser), 'BETA-B'
        )
        self.assertEqual(
            _convert(r'\beta', low_text, low_parser), 'BETA-A'
        )

    def test_removing_profile_restores_default_text(self):
        self.text_db.remove_profile('journal-a')
        self.text_db.remove_profile('journal-b')
        text_context, parser_context = self._build(None)
        self.assertEqual(
            _convert(r'\beta ', text_context, parser_context),
            _DEFAULT_BETA_TEXT,
        )

    def test_specials_override_is_independent_of_macro_profiles(self):
        text_context, parser_context = self._build(('journal-a', 'journal-c'))
        # the macro override and the specials override both apply, each in its
        # own domain
        self.assertEqual(
            _convert(r'a---b', text_context, parser_context),
            'aCUSTOM-DASHb',
        )
        self.assertEqual(
            _convert(r'\beta', text_context, parser_context), 'BETA-A'
        )

    def test_two_parallel_documents_do_not_interfere(self):
        doc1_text, doc1_parser = self._build(('journal-a', 'journal-b'))
        doc2_text, doc2_parser = self._build(('journal-a',))
        latex = r'\beta'
        self.assertEqual(
            _convert(latex, doc1_text, doc1_parser), 'BETA-B'
        )
        self.assertEqual(
            _convert(latex, doc2_text, doc2_parser), 'BETA-A'
        )
        # converting the first document again still gives its own answer
        self.assertEqual(
            _convert(latex, doc1_text, doc1_parser), 'BETA-B'
        )
        self.assertIsNot(doc1_text, doc2_text)
        self.assertIsNot(doc1_parser, doc2_parser)


class TestRuleExplanation(unittest.TestCase):

    def setUp(self):
        self.db = _make_text_profile_db()
        self.base = get_latex2text_default_context_db()

    def test_base_rule_is_attributed_to_the_base_context(self):
        resolution = self.db.explain_rule(
            'macros', 'alpha', self.base, enabled=()
        )
        self.assertIsNotNone(resolution)
        self.assertIsNone(resolution.profile)
        self.assertEqual(resolution.spec.macroname, 'alpha')
        self.assertEqual(resolution.shadowed, [])

    def test_winner_and_shadowed_chain_are_reported(self):
        resolution = self.db.explain_rule(
            'macros', _OVERRIDDEN_MACRO, self.base,
            enabled=('journal-a', 'journal-b'),
        )
        self.assertEqual(resolution.profile, 'journal-b')
        self.assertEqual(resolution.spec.simplify_repl, 'BETA-B')
        self.assertEqual(len(resolution.shadowed), 2)
        self.assertIsInstance(resolution.shadowed[0], ShadowedRule)
        # first the lower-priority profile, then the base default
        self.assertEqual(resolution.shadowed[0].profile, 'journal-a')
        self.assertEqual(
            resolution.shadowed[0].spec.simplify_repl, 'BETA-A'
        )
        self.assertIsNone(resolution.shadowed[1].profile)
        self.assertEqual(
            resolution.shadowed[1].spec.simplify_repl,
            self.base.get_macro_spec(_OVERRIDDEN_MACRO).simplify_repl,
        )

    def test_disabling_the_winner_rewrites_the_explanation(self):
        resolution = self.db.explain_rule(
            'macros', _OVERRIDDEN_MACRO, self.base,
            enabled=('journal-a',),
        )
        self.assertEqual(resolution.profile, 'journal-a')
        self.assertEqual(len(resolution.shadowed), 1)
        self.assertIsNone(resolution.shadowed[0].profile)

    def test_explain_covers_the_three_domains_separately(self):
        explanation = self.db.explain(
            self.base, enabled=('journal-a', 'journal-b', 'journal-c')
        )
        self.assertEqual(
            sorted(explanation.keys()),
            ['environments', 'macros', 'specials'],
        )
        self.assertEqual(
            explanation['macros'][_OVERRIDDEN_MACRO].profile, 'journal-b'
        )
        self.assertEqual(
            explanation['environments']['journalbox'].profile, 'journal-a'
        )
        self.assertEqual(
            explanation['specials']['---'].profile, 'journal-c'
        )

    def test_base_categories_keep_their_own_precedence(self):
        # a base context with two categories that define the same macro: the
        # earlier category is what the context database itself returns, and the
        # explanation must agree rather than reporting the later one
        base = macrospec.LatexContextDb()
        base.add_context_category(
            'first', macros=[MacroTextSpec('m', simplify_repl='FIRST')]
        )
        base.add_context_category(
            'second', macros=[MacroTextSpec('m', simplify_repl='SECOND')]
        )
        resolution = self.db.explain_rule('macros', 'm', base, enabled=())
        self.assertEqual(resolution.spec.simplify_repl, 'FIRST')
        self.assertIsNone(resolution.profile)
        self.assertEqual(len(resolution.shadowed), 1)
        self.assertEqual(
            resolution.shadowed[0].spec.simplify_repl, 'SECOND'
        )
        # and the built context must resolve the name the very same way
        built = self.db.build_context(base, enabled=())
        self.assertEqual(
            built.get_macro_spec('m').simplify_repl, 'FIRST'
        )

    def test_unknown_name_and_domain(self):
        self.assertIsNone(
            self.db.explain_rule('macros', 'no-such-macro', self.base)
        )
        with self.assertRaises(ValueError):
            self.db.explain_rule('wrong', 'x', self.base)


if __name__ == '__main__':
    unittest.main()
