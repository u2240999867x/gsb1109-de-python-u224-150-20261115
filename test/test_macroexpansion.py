import io
import json
import unittest
from contextlib import redirect_stdout

from pylatexenc import macroexpansion as me


def _analyze(source):
    return me.analyze(source)


class TestParsing(unittest.TestCase):

    def test_plain_text_without_macros(self):
        result = _analyze("hello world")
        self.assertTrue(result.is_finite)
        self.assertEqual(result.total_chars, 11)
        self.assertEqual(result.char_contributions, {None: 11})
        self.assertEqual(result.call_counts, {})

    def test_newlines_are_counted(self):
        result = _analyze("a\nb")
        self.assertEqual(result.total_chars, 3)

    def test_hash_is_literal_outside_bodies(self):
        result = _analyze("a#1b")
        self.assertEqual(result.total_chars, 4)
        self.assertEqual(me.expand_text("a#1b"), "a#1b")

    def test_separator_and_comment_lines(self):
        source = "# a comment\n\n\\a{xy}\n---\n\\a\n"
        result = _analyze(source)
        self.assertEqual(result.total_chars, 2)

    def test_text_section_starts_at_first_non_definition_line(self):
        result = _analyze("\\a{xy}\nsome text \\a\n")
        self.assertEqual(result.total_chars, len("some text xy"))

    def test_forward_reference(self):
        source = "\\a{[\\b]}\n\\b{z}\n---\n\\a"
        result = _analyze(source)
        self.assertEqual(result.total_chars, 3)
        self.assertEqual(me.expand_text(source), "[z]")

    def test_undelimited_arguments(self):
        source = "\\add[2]{#1+#2}\n---\n\\add ab"
        result = _analyze(source)
        self.assertEqual(result.total_chars, 3)
        self.assertEqual(me.expand_text(source), "a+b")

    def test_braces_do_not_contribute(self):
        source = "\\g[1]{[#1]}\n---\n\\g{ab}"
        result = _analyze(source)
        self.assertEqual(result.total_chars, 4)
        self.assertEqual(me.expand_text(source), "[ab]")

    def test_syntax_errors(self):
        with self.assertRaises(me.MacroSyntaxError):
            _analyze("\\a{xy\ntext")
        with self.assertRaises(me.MacroSyntaxError):
            _analyze("\\a{xy}\ntext }")
        with self.assertRaises(me.MacroSyntaxError):
            _analyze("\\add[2]{#1}\n---\n\\add a")
        with self.assertRaises(me.MacroSyntaxError):
            _analyze("\\a[1]{#2}\n---\n\\a{x}")
        with self.assertRaises(me.MacroSyntaxError):
            _analyze("\\a{x}\n\\a{y}\n---\n\\a")

    def test_line_with_trailing_text_starts_text_section(self):
        # The definition section ends as soon as a line is not exactly one
        # definition, even if it begins with a backslash.
        source = "\\a{x}\n\\b{y} trailing\n"
        result = _analyze(source)
        self.assertEqual(result.unsupported_macros, ["b"])
        self.assertEqual(me.expand_text(source), "\\by trailing")

    def test_excessive_nesting_raises_a_clean_syntax_error(self):
        depth = me._MAX_PARSE_NESTING + 5
        source = "\\a[1]{#1}\n---\n" + "\\a{" * depth + "q" + "}" * depth
        with self.assertRaises(me.MacroSyntaxError):
            _analyze(source)

    def test_nesting_at_the_parse_limit_is_accepted(self):
        depth = me._MAX_PARSE_NESTING
        source = "\\a[1]{[#1]}\n---\n" + "\\a{" * depth + "q" + "}" * depth
        result = _analyze(source)
        self.assertTrue(result.is_finite)
        self.assertEqual(result.total_chars, 2 * depth + 1)


class TestExpansionLengths(unittest.TestCase):

    def test_zero_argument_macro(self):
        result = _analyze("\\a{AB}\n---\n\\a")
        self.assertEqual(result.total_chars, 2)
        self.assertEqual(result.call_counts, {"a": 1})
        self.assertEqual(result.char_contributions, {"a": 2})

    def test_parameter_used_twice_contributes_twice(self):
        source = "\\d[1]{#1#1}\n---\n\\d{ab}"
        result = _analyze(source)
        self.assertEqual(result.total_chars, 4)
        self.assertEqual(me.expand_text(source), "abab")
        self.assertEqual(result.char_contributions, {"d": 4})

    def test_unused_parameter_contributes_nothing(self):
        source = "\\u[1]{XY}\n---\n\\u{abcdef}"
        result = _analyze(source)
        self.assertEqual(result.total_chars, 2)
        self.assertEqual(me.expand_text(source), "XY")

    def test_nested_calls_and_attribution(self):
        source = (
            "\\greet[1]{Hi #1}\n"
            "\\twice[1]{#1 #1}\n"
            "---\n"
            "\\twice{\\greet{bo}}\n"
        )
        result = _analyze(source)
        self.assertEqual(me.expand_text(source), "Hi bo Hi bo")
        self.assertEqual(result.total_chars, 11)
        self.assertEqual(result.call_counts, {"twice": 1, "greet": 2})
        # greet owns "Hi " (3) and its substituted argument "bo" (2), twice:
        # 2 * 5 = 10; twice owns the single separating space.
        self.assertEqual(result.char_contributions, {"greet": 10, "twice": 1})
        self.assertEqual(
            sum(result.char_contributions.values()), result.total_chars
        )

    def test_macro_used_twice_in_text_is_not_recursion(self):
        source = "\\t[1]{(#1)}\n---\n\\t{x}\\t{y}"
        result = _analyze(source)
        self.assertTrue(result.is_finite)
        self.assertEqual(result.total_chars, 6)
        self.assertEqual(me.expand_text(source), "(x)(y)")
        self.assertEqual(result.cycles, [])

    def test_unsupported_macro_is_kept_verbatim(self):
        source = "pre \\p{zz} post"
        result = _analyze(source)
        self.assertEqual(result.unsupported_macros, ["p"])
        self.assertEqual(result.call_counts, {"p": 1})
        # "\\p" itself (2 chars) plus the braced argument text "zz" and the
        # surrounding literal text.
        self.assertEqual(me.expand_text(source), "pre \\pzz post")
        self.assertEqual(result.total_chars, len("pre \\pzz post"))
        self.assertEqual(result.char_contributions["p"], 2)
        self.assertEqual(
            sum(result.char_contributions.values()), result.total_chars
        )

    def test_unsupported_macro_consumes_no_arguments(self):
        result = _analyze("\\q{a}{b}")
        self.assertEqual(result.unsupported_macros, ["q"])
        self.assertEqual(me.expand_text("\\q{a}{b}"), "\\qab")
        self.assertEqual(result.total_chars, 4)

    def test_contribution_sum_equals_total_on_finite_inputs(self):
        sources = [
            "\\a{x}\\b[1]{y#1z}\n---\n\\a \\b{ww} end",
            "\\a[2]{#2-#1-#2}\n---\n\\a{uv}{w}",
            "\\a[1]{[#1]}\\b[1]{<#1>}\n---\n\\a{\\b{q}}",
            "plain text only",
        ]
        for source in sources:
            result = _analyze(source)
            self.assertTrue(result.is_finite, source)
            self.assertEqual(
                sum(result.char_contributions.values()),
                result.total_chars,
                source,
            )
            self.assertEqual(
                result.total_chars,
                len(me.expand_text(source)),
                source,
            )

    def test_length_matches_reference_expansion(self):
        sources = [
            "\\greet[1]{Hello #1!}\n\\twice[1]{#1 #1}\n"
            "---\n\\twice{\\greet{world}}",
            "\\a[2]{#1##1#2}\n---\n\\a{xy}{z}",
            "\\wrap[1]{((#1))}\n---\n\\wrap{\\wrap{q}}",
            "\\a{AB}\\b{CD}\n---\n\\a-\\b-\\a",
        ]
        for source in sources:
            result = _analyze(source)
            expanded = me.expand_text(source)
            self.assertEqual(result.total_chars, len(expanded), source)
            self.assertEqual(
                sum(result.char_contributions.values()),
                len(expanded),
                source,
            )


class TestRecursion(unittest.TestCase):

    def test_direct_self_recursion_reports_cycle(self):
        source = "\\r[1]{x\\r{#1}}\n---\n\\r{a}"
        result = _analyze(source)
        self.assertFalse(result.is_finite)
        self.assertIsNone(result.total_chars)
        self.assertEqual(result.cyclic_macros, {"r"})
        self.assertEqual(result.cycles, [["r", "r"]])
        with self.assertRaises(me.CyclicMacroError):
            me.expand_text(source)

    def test_mutual_recursion_reports_canonical_cycle(self):
        source = (
            "\\a[1]{\\b{#1}}\n"
            "\\b[1]{\\a{#1}}\n"
            "---\n"
            "\\a{x}\n"
        )
        result = _analyze(source)
        self.assertFalse(result.is_finite)
        self.assertEqual(result.cyclic_macros, {"a", "b"})
        self.assertEqual(result.cycles, [["a", "b", "a"]])

    def test_all_reachable_cycles_are_reported(self):
        source = (
            "\\a{\\b\\d}\n"
            "\\b{\\a}\n"
            "\\d{\\c}\n"
            "\\c{\\d}\n"
            "---\n"
            "\\a\n"
        )
        result = _analyze(source)
        self.assertFalse(result.is_finite)
        self.assertEqual(
            sorted(result.cycles),
            [["a", "b", "a"], ["c", "d", "c"]],
        )
        self.assertEqual(result.cyclic_macros, {"a", "b", "c", "d"})

    def test_unreachable_cycle_is_not_reported(self):
        source = "\\x{\\x}\n\\a{ok}\n---\n\\a"
        result = _analyze(source)
        self.assertTrue(result.is_finite)
        self.assertEqual(result.total_chars, 2)
        self.assertEqual(result.cycles, [])
        self.assertEqual(result.cyclic_macros, set())

    def test_nested_call_through_argument_is_not_recursion(self):
        # The inner \a arrives through parameter substitution, so it is a
        # fresh nested invocation rather than body-level self-recursion.
        source = "\\a[1]{x#1}\n---\n\\a{\\a{y}}"
        result = _analyze(source)
        self.assertTrue(result.is_finite)
        self.assertEqual(me.expand_text(source), "xxy")
        self.assertEqual(result.total_chars, 3)
        self.assertEqual(result.cycles, [])

    def test_recursion_plumbed_through_another_macro_parameter(self):
        # \b's body contains a literal \b call, so the static call graph
        # reports the self-cycle even though \a only forwards its argument.
        source = (
            "\\a[1]{#1}\n"
            "\\b[1]{\\a{\\b{#1}}}\n"
            "---\n"
            "\\b{x}\n"
        )
        result = _analyze(source)
        self.assertFalse(result.is_finite)
        self.assertEqual(result.cyclic_macros, {"b"})
        self.assertEqual(result.cycles, [["b", "b"]])
        with self.assertRaises(me.CyclicMacroError):
            me.expand_text(source)

    def test_finite_chain_plumbed_through_parameters(self):
        source = (
            "\\a[1]{[#1]}\n"
            "\\b[1]{<#1>}\n"
            "---\n"
            "\\a{\\b{\\a{q}}}\n"
        )
        result = _analyze(source)
        self.assertTrue(result.is_finite)
        self.assertEqual(me.expand_text(source), "[<[q]>]")
        self.assertEqual(result.total_chars, 7)

    def test_recursive_and_unsupported_macro_reported_together(self):
        source = "\\r{\\r}\n---\n\\r then \\keep{x}"
        result = _analyze(source)
        self.assertFalse(result.is_finite)
        self.assertEqual(result.cycles, [["r", "r"]])
        self.assertEqual(result.unsupported_macros, ["keep"])
        self.assertEqual(result.call_counts.get("keep"), 1)

    def test_evaluation_cap_never_yields_a_fabricated_total(self):
        # An acyclic call graph whose expansion simply exceeds the evaluation
        # budget must report None and mark itself truncated rather than print
        # a partial count as if it were exact.
        source = "\\a[1]{[#1]}\n---\n" + "\\a{" * 8 + "q" + "}" * 8
        saved = me._MAX_CYCLIC_ENTRIES
        me._MAX_CYCLIC_ENTRIES = 5
        try:
            result = _analyze(source)
        finally:
            me._MAX_CYCLIC_ENTRIES = saved
        self.assertTrue(result.truncated)
        self.assertIsNone(result.total_chars)
        self.assertEqual(result.cycles, [])
        # With the normal budget the same input is exact.
        exact = _analyze(source)
        self.assertTrue(exact.is_finite)
        self.assertFalse(exact.truncated)
        self.assertEqual(exact.total_chars, 8 * 2 + 1)

    def test_finite_results_are_not_marked_truncated(self):
        result = _analyze("\\a[1]{#1#1}\n---\n\\a{ab}")
        self.assertFalse(result.truncated)


class TestReporting(unittest.TestCase):

    def test_to_dict_round_trip(self):
        result = _analyze("\\a[1]{#1#1}\n---\n\\a{ab}")
        data = result.to_dict()
        self.assertTrue(data["finite"])
        self.assertEqual(data["total_chars"], 4)
        self.assertEqual(data["call_counts"], {"a": 1})
        self.assertEqual(data["char_contributions"], {"a": 4})
        json.dumps(data)

    def test_human_report_mentions_cycle_and_unsupported_macro(self):
        source = "\\r{\\r}\n---\n\\r and \\missing{x}"
        result = _analyze(source)
        report = me._format_human(result)
        self.assertIn("UNBOUNDED", report)
        self.assertIn("r -> r", report)
        self.assertIn("\\missing", report)

    def test_doubled_hash_in_body_is_literal_hash(self):
        source = "\\a[2]{#1##1#2}\n---\n\\a{xy}{z}"
        self.assertEqual(me.expand_text(source), "xy#1z")
        result = _analyze(source)
        self.assertEqual(result.total_chars, 5)

    def test_cli_main_on_file(self):
        import tempfile
        import os

        handle, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as target:
                target.write("\\a[1]{#1#1}\n---\n\\a{ab}\n")
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = me.main([path, "--json"])
            self.assertEqual(code, 0)
            data = json.loads(stream.getvalue())
            self.assertEqual(data["total_chars"], 4)
        finally:
            os.remove(path)

    def test_cli_main_reports_syntax_error(self):
        import tempfile
        import os

        handle, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as target:
                target.write("\\a{unterminated\n")
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = me.main([path, "--json"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
