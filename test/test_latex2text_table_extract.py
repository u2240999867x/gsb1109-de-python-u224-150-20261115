# -*- coding: utf-8 -*-
#
# Tests for extracting the grid of a tabular-like table, see
# `pylatexenc/latex2text/_table_extract.py`.
#
# The point of these tests is the separation between the table's own
# separators and the same characters elsewhere.  A column separator is only the
# `&` that sits directly in the table body: an `&` inside a macro argument, a
# nested environment, a braced group, a comment or the escape `\&` is content,
# not a separator.  A row separator is only the `\\` that sits directly in the
# body.  The tests therefore build tables that put the separators in each of
# those places and check that the grid is unchanged.
#
# The other subject is the broken table.  When the `\end{...}` is missing, the
# rows that could be established must survive, and the prose that follows must
# not be collected into a cell.  Whitespace-only cells and cells that were not
# written at all are also kept apart, because a whitespace cell has a source
# position and an unwritten cell does not.
#

### BEGIN_TEST_PYLATEXENC_SKIP
import unittest
import json

from pylatexenc.latex2text import LatexNodes2Text, extract_tables


def extract(latex, **options):
    # The converter is created first so that the cell text follows its options,
    # which is what a caller who has a converter in hand would want.
    if options:
        l2t = LatexNodes2Text(**options)
        from pylatexenc.latex2text import _table_extract
        return _table_extract.extract_tables_from_nodelist(
            _parse(latex), l2tobj=l2t
        )
    return extract_tables(latex)


def _parse(latex):
    from pylatexenc.latexwalker import LatexWalker
    nodelist, _ = LatexWalker(latex).parse_content()
    return nodelist


def one_table(latex, **options):
    r"""
    The single table in `latex`.  A nested table counts as part of its parent,
    so the outermost table is the one that is returned; the tests that are
    about nested tables ask for the full list instead.
    """
    tables = extract(latex, **options)
    if not tables:
        raise AssertionError("expected a table, got none")
    outer = tables[0]
    for t in tables[1:]:
        if t.pos is not None and outer.pos is not None and t.pos < outer.pos:
            outer = t
    return outer


def grid(table):
    r"""
    The table's text as a list of rows of `(column, text)` pairs.
    """
    return [
        [(cell.column, cell.text) for cell in row.cells]
        for row in table.rows
    ]


# ------------------------------------------------------------------------------
# The basic grid.
# ------------------------------------------------------------------------------

class TestBasicGrid(unittest.TestCase):

    def test_simple_table(self):
        table = one_table(r'''\begin{tabular}{lll}
            a & b & c \\
            d & e & f
            \end{tabular}''')
        self.assertEqual(table.environmentname, 'tabular')
        self.assertEqual(table.column_count, 3)
        self.assertEqual(table.declared_column_count, 3)
        self.assertTrue(table.terminated)
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b'), (2, 'c')],
            [(0, 'd'), (1, 'e'), (2, 'f')],
        ])

    def test_cell_positions_and_source(self):
        latex = r'''\begin{tabular}{ll}a & bb\end{tabular}'''
        table = one_table(latex)
        a, bb = table.rows[0].cells
        self.assertEqual(a.text, 'a')
        self.assertEqual(latex[a.pos:a.pos_end], 'a')
        self.assertEqual(a.source, 'a')
        self.assertEqual(bb.text, 'bb')
        self.assertEqual(latex[bb.pos:bb.pos_end], 'bb')
        self.assertEqual(bb.source, 'bb')

    def test_line_and_column_numbers(self):
        # the cell positions are reported as line and column of the source, so
        # that a caller can show the user where a cell came from
        latex = ("\\begin{tabular}{ll}\n"
                 "one & two \\\\\n"
                 "three & four\n"
                 "\\end{tabular}")
        table = one_table(latex)
        one = table.rows[0].cells[0]
        three = table.rows[1].cells[0]
        self.assertEqual((one.lineno, one.colno), (2, 0))
        self.assertEqual((three.lineno, three.colno), (3, 0))
        self.assertEqual(table.lineno, 1)

    def test_table_outer_position(self):
        latex = 'HEAD ' + r'\begin{tabular}{l}a\end{tabular}' + ' TAIL'
        table = one_table(latex)
        self.assertEqual(latex[table.pos:table.pos_end],
                         r'\begin{tabular}{l}a\end{tabular}')


# ------------------------------------------------------------------------------
# Column specifications.
# ------------------------------------------------------------------------------

class TestColumnSpecification(unittest.TestCase):

    def test_rules_and_widths_count_columns(self):
        table = one_table(r'\begin{tabular}{|l|c|p{3cm}|}a&b&c\end{tabular}')
        self.assertEqual(table.declared_column_count, 3)
        self.assertEqual(table.column_count, 3)

    def test_repeated_specification(self):
        table = one_table(r'\begin{tabular}{*{3}{c}}a&b&c\end{tabular}')
        self.assertEqual(table.declared_column_count, 3)

    def test_repeated_specification_with_several_columns(self):
        table = one_table(r'\begin{tabular}{*{2}{cc}}a&b&c&d\end{tabular}')
        self.assertEqual(table.declared_column_count, 4)

    def test_intercolumn_material_is_not_a_column(self):
        table = one_table(r'\begin{tabular}{@{}l!{x}r}a&b\end{tabular}')
        self.assertEqual(table.declared_column_count, 2)

    def test_siunitx_numeric_column(self):
        table = one_table(
            r'\begin{tabular}{lS[table-format=2.1]}a&1.0\end{tabular}'
        )
        self.assertEqual(table.declared_column_count, 2)

    def test_unreadable_specification_falls_back_to_the_rows(self):
        # the specification is not something this module can count; the grid
        # is then as wide as the widest row, and the declared count is unknown
        table = one_table(r'\begin{tabular}{l?}a&b\end{tabular}')
        self.assertIs(table.declared_column_count, None)
        self.assertEqual(table.column_count, 2)

    def test_matrix_environments_have_no_specification(self):
        table = one_table(r'\begin{pmatrix}1&2\\3&4\end{pmatrix}')
        self.assertIs(table.declared_column_count, None)
        self.assertEqual(table.column_count, 2)
        self.assertEqual(grid(table), [
            [(0, '1'), (1, '2')],
            [(0, '3'), (1, '4')],
        ])

    def test_wide_rows_widen_the_grid(self):
        table = one_table(r'\begin{tabular}{ll}a&b&c\end{tabular}')
        self.assertEqual(table.declared_column_count, 2)
        self.assertEqual(table.column_count, 3)
        self.assertEqual(grid(table), [[(0, 'a'), (1, 'b'), (2, 'c')]])


# ------------------------------------------------------------------------------
# Separators that are not separators.
# ------------------------------------------------------------------------------

class TestSeparatorsInsideConstructs(unittest.TestCase):

    def test_ampersand_in_macro_argument_is_content(self):
        table = one_table(r'\begin{tabular}{ll}a & \textbf{x & y}\end{tabular}')
        self.assertEqual(len(table.rows), 1)
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1])
        self.assertEqual(table.rows[0].cells[1].text, '𝐱     𝐲')

    def test_ampersand_in_nested_environment_is_content(self):
        table = one_table(
            r'\begin{tabular}{ll}'
            r'a & \begin{tabular}{cc}1&2\\3&4\end{tabular}'
            r'\end{tabular}'
        )
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1])
        self.assertEqual(len(table.rows), 1)
        # the nested table is a single cell of the outer one
        self.assertEqual(table.rows[0].cells[1].text, '1   2\n3   4')

    def test_nested_table_is_extracted_on_its_own(self):
        latex = (r'\begin{tabular}{ll}'
                 r'a & \begin{tabular}{cc}1&2\\3&4\end{tabular}'
                 r'\end{tabular}')
        tables = extract(latex)
        self.assertEqual(len(tables), 2)
        # source order: the outer table starts before the inner one
        self.assertEqual(tables[0].environmentname, 'tabular')
        self.assertEqual(tables[0].declared_column_count, 2)
        self.assertEqual(len(tables[0].rows), 1)
        inner = tables[1]
        self.assertEqual(grid(inner), [
            [(0, '1'), (1, '2')],
            [(0, '3'), (1, '4')],
        ])

    def test_ampersand_in_braced_group_is_content(self):
        table = one_table(r'\begin{tabular}{ll}{a & b} & c\end{tabular}')
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1])
        self.assertEqual(table.rows[0].cells[0].text, 'a     b')

    def test_escaped_ampersand_is_content(self):
        table = one_table(r'\begin{tabular}{ll}a\&b & c\end{tabular}')
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1])
        self.assertEqual(table.rows[0].cells[0].text, 'a&b')

    def test_ampersand_in_comment_is_not_a_separator(self):
        table = one_table('\\begin{tabular}{lll}a&% x & y\n&b\\end{tabular}')
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1, 2])
        self.assertEqual(table.rows[0].cells[1].text, '')

    def test_ampersand_in_verbatim_is_content(self):
        table = one_table(r'\begin{tabular}{ll}\verb|a&b| & c\end{tabular}')
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1])
        self.assertEqual(table.rows[0].cells[0].source, r'\verb|a&b|')

    def test_row_separator_in_macro_argument_is_content(self):
        table = one_table(
            r'\begin{tabular}{ll}a & \parbox{3cm}{x \\ y}\end{tabular}'
        )
        self.assertEqual(len(table.rows), 1)
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1])

    def test_ampersand_in_math_is_content(self):
        table = one_table(
            r'\begin{tabular}{ll}'
            r'a & $\begin{matrix}1&2\end{matrix}$'
            r'\end{tabular}'
        )
        self.assertEqual([c.column for c in table.rows[0].cells], [0, 1])
        self.assertEqual(table.rows[0].cells[1].text, '1   2')


# ------------------------------------------------------------------------------
# Rules and decorations.
# ------------------------------------------------------------------------------

class TestRulesAndDecorations(unittest.TestCase):

    def test_horizontal_rules_are_not_cells(self):
        table = one_table(
            r'''\begin{tabular}{|l|c|}
                \hline
                a & b \\
                \hline
                c & d \\
                \hline
                \end{tabular}'''
        )
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])

    def test_rule_position_arguments_are_not_cells(self):
        table = one_table(
            r'\begin{tabular}{ll}'
            r'\cline{1-2} a & b \\'
            r'\cmidrule(lr){1-2} c & d'
            r'\end{tabular}'
        )
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])
        for row in table.rows:
            for cell in row.cells:
                self.assertNotIn('cline', cell.source)
                self.assertNotIn('midrule', cell.source)

    def test_booktabs_rules(self):
        table = one_table(
            r'''\begin{tabular}{ll}
                \toprule
                a & b \\
                \midrule
                c & d \\
                \bottomrule
                \end{tabular}'''
        )
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])

    def test_row_color_is_not_a_cell(self):
        table = one_table(
            r'\begin{tabular}{ll}\rowcolor{red} a & b\end{tabular}'
        )
        self.assertEqual(grid(table), [[(0, 'a'), (1, 'b')]])

    def test_label_is_not_part_of_the_cell_text(self):
        table = one_table(
            r'\begin{tabular}{ll}a & \label{x}b\end{tabular}'
        )
        self.assertEqual(table.rows[0].cells[1].text, 'b')


# ------------------------------------------------------------------------------
# Columns and rows that cells span.
# ------------------------------------------------------------------------------

class TestSpanningCells(unittest.TestCase):

    def test_multicolumn_spans_columns(self):
        table = one_table(
            r'''\begin{tabular}{lll}
                a & \multicolumn{2}{c}{wide} \\
                b & c & d
                \end{tabular}'''
        )
        wide = table.rows[0].cells[1]
        self.assertEqual(wide.column, 1)
        self.assertEqual(wide.colspan, 2)
        self.assertEqual(wide.rowspan, 1)
        self.assertEqual(wide.text, 'wide')
        self.assertEqual(grid(table)[1],
                         [(0, 'b'), (1, 'c'), (2, 'd')])

    def test_multicolumn_covers_the_whole_row(self):
        table = one_table(
            r'''\begin{tabular}{lll}
                \multicolumn{3}{c}{only} \\
                a & b & c
                \end{tabular}'''
        )
        only = table.rows[0].cells[0]
        self.assertEqual((only.column, only.colspan), (0, 3))

    def test_multicolumn_arguments_do_not_leak_into_the_text(self):
        table = one_table(
            r'\begin{tabular}{lll}a & \multicolumn{2}{c}{value}\end{tabular}'
        )
        cell = table.rows[0].cells[1]
        self.assertEqual(cell.text, 'value')
        self.assertNotIn('c}', cell.text)
        self.assertNotIn('2', cell.text)

    def test_multirow_spans_rows(self):
        table = one_table(
            r'''\begin{tabular}{ll}
                \multirow{2}{*}{tall} & b \\
                 & c \\
                d & e
                \end{tabular}'''
        )
        tall = table.rows[0].cells[0]
        self.assertEqual((tall.row, tall.column), (0, 0))
        self.assertEqual(tall.rowspan, 2)
        self.assertEqual(tall.colspan, 1)
        self.assertEqual(tall.text, 'tall')
        # the continuation row has its field, but the spanning cell is reported
        # at its origin only
        self.assertEqual(grid(table)[1], [(1, 'c')])
        self.assertEqual(grid(table)[2], [(0, 'd'), (1, 'e')])

    def test_cell_at_returns_the_spanning_cell(self):
        table = one_table(
            r'''\begin{tabular}{ll}
                \multirow{2}{*}{tall} & b \\
                 & c
                \end{tabular}'''
        )
        self.assertIs(table.cell_at(1, 0), table.rows[0].cells[0])
        self.assertIs(table.cell_at(1, 1), table.rows[1].cells[0])


# ------------------------------------------------------------------------------
# Empty cells and whitespace.
# ------------------------------------------------------------------------------

class TestEmptyAndWhitespaceCells(unittest.TestCase):

    def test_adjacent_separators_give_an_empty_cell(self):
        table = one_table(r'\begin{tabular}{lll}a&&b\end{tabular}')
        middle = table.rows[0].cells[1]
        self.assertTrue(middle.empty)
        self.assertEqual(middle.text, '')
        self.assertEqual(middle.source, '')
        self.assertIs(middle.pos, None)
        self.assertIs(middle.pos_end, None)

    def test_a_space_is_a_written_cell(self):
        table = one_table(r'\begin{tabular}{lll}a& &b\end{tabular}')
        middle = table.rows[0].cells[1]
        self.assertFalse(middle.empty)
        self.assertTrue(middle.whitespace_only)
        self.assertEqual(middle.text, '')
        self.assertEqual(middle.source, ' ')
        self.assertIsNot(middle.pos, None)

    def test_line_break_between_separators_is_a_written_cell(self):
        table = one_table('\\begin{tabular}{ll}a&\n&b\\end{tabular}')
        middle = table.rows[0].cells[1]
        self.assertFalse(middle.empty)
        self.assertTrue(middle.whitespace_only)
        self.assertEqual(middle.source, '\n')

    def test_a_row_of_empty_fields(self):
        table = one_table(r'\begin{tabular}{lll} & & \end{tabular}')
        self.assertEqual(len(table.rows), 1)
        cells = table.rows[0].cells
        self.assertEqual(len(cells), 3)
        self.assertEqual([c.whitespace_only for c in cells], [True, True, False])
        self.assertTrue(cells[2].empty)

    def test_trailing_row_separator_does_not_add_a_row(self):
        table = one_table(
            r'\begin{tabular}{ll}a & b \\ c & d \\ \end{tabular}'
        )
        self.assertEqual(len(table.rows), 2)

    def test_multiple_trailing_row_separators_do_not_add_rows(self):
        table = one_table('\\begin{tabular}{ll}a & b \\\\\n\n\\end{tabular}')
        self.assertEqual(len(table.rows), 1)

    def test_rule_only_body_has_no_rows(self):
        table = one_table(r'\begin{tabular}{ll}\hline\end{tabular}')
        self.assertEqual(table.rows, [])


# ------------------------------------------------------------------------------
# Tables whose structure is incomplete.
# ------------------------------------------------------------------------------

class TestIncompleteStructure(unittest.TestCase):

    def test_unterminated_table_keeps_its_complete_rows(self):
        latex = "\\begin{tabular}{ll}\n" \
                "a & b \\\\\n" \
                "c & d\n"
        table = one_table(latex)
        self.assertFalse(table.terminated)
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])
        self.assertTrue(table.rows[0].complete)
        self.assertFalse(table.rows[-1].complete)
        self.assertTrue(table.warnings)

    def test_following_prose_is_not_table_content(self):
        # the walker cannot know where the table was meant to stop, so it hands
        # the following paragraph to the table; the extractor has to leave it
        # out instead of turning it into a row
        latex = ("\\begin{tabular}{ll}\n"
                 "a & b \\\\\n"
                 "c & d\n\n"
                 "This is the next paragraph, not a table row.\n")
        table = one_table(latex)
        self.assertFalse(table.terminated)
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])
        for row in table.rows:
            for cell in row.cells:
                self.assertNotIn('paragraph', cell.text)

    def test_sectioning_command_is_not_table_content(self):
        latex = ("\\begin{tabular}{ll}\n"
                 "a & b \\\\\n"
                 "c & d\n"
                 "\\section{Next}\n")
        table = one_table(latex)
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])
        self.assertNotIn('Next', json.dumps(table.to_json_object(),
                                            ensure_ascii=False))

    def test_unterminated_inside_another_environment(self):
        latex = ("\\begin{table}"
                 "\\begin{tabular}{ll}\n"
                 "a & b \\\\\n"
                 "c & d\n"
                 "\\end{table}")
        table = one_table(latex)
        self.assertFalse(table.terminated)
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])

    def test_mismatched_end_environment_is_not_terminated(self):
        latex = ("\\begin{tabular}{ll}\n"
                 "a & b \\\\\n"
                 "c & d\n"
                 "\\end{tabularx}")
        table = one_table(latex)
        self.assertFalse(table.terminated)
        self.assertEqual(grid(table), [
            [(0, 'a'), (1, 'b')],
            [(0, 'c'), (1, 'd')],
        ])

    def test_rowspan_past_the_last_row_is_clamped(self):
        table = one_table(
            r'''\begin{tabular}{ll}
                \multirow{5}{*}{tall} & b \\
                c & d
                \end{tabular}'''
        )
        cell = table.rows[0].cells[0]
        self.assertEqual(cell.rowspan, 2)
        self.assertTrue(table.warnings)


# ------------------------------------------------------------------------------
# The interface around the extraction.
# ------------------------------------------------------------------------------

class TestExtractionInterface(unittest.TestCase):

    def test_environment_variants(self):
        for latex, name, columns in [
            (r'\begin{tabular*}{0.8\textwidth}{ll}a&b\end{tabular*}',
             'tabular*', 2),
            (r'\begin{tabularx}{\textwidth}{lX}a&b\end{tabularx}',
             'tabularx', 2),
            (r'\begin{array}{cc}a&b\end{array}', 'array', 2),
            (r'\begin{pmatrix}a&b\end{pmatrix}', 'pmatrix', 2),
        ]:
            table = one_table(latex)
            self.assertEqual(table.environmentname, name)
            self.assertEqual(table.column_count, columns)

    def test_non_table_environment_is_rejected(self):
        latex = r'\begin{itemize}\item x\end{itemize}'
        from pylatexenc.latexwalker import LatexWalker
        nodelist, _ = LatexWalker(latex).parse_content()
        from pylatexenc.latex2text import extract_table
        with self.assertRaises(ValueError):
            extract_table(nodelist[0])

    def test_extract_table_on_a_node(self):
        from pylatexenc.latexwalker import LatexWalker
        from pylatexenc.latex2text import extract_table
        latex = r'\begin{tabular}{ll}a&b\end{tabular}'
        nodelist, _ = LatexWalker(latex).parse_content()
        table = extract_table(nodelist[0])
        self.assertEqual(grid(table), [[(0, 'a'), (1, 'b')]])

    def test_extract_tables_finds_several_tables_in_order(self):
        latex = (r'T \begin{tabular}{l}a\end{tabular} '
                 r'U \begin{tabular}{l}b\end{tabular}')
        tables = extract(latex)
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0].rows[0].cells[0].text, 'a')
        self.assertEqual(tables[1].rows[0].cells[0].text, 'b')
        self.assertLess(tables[0].pos, tables[1].pos)

    def test_extract_tables_from_an_environment_argument(self):
        latex = r'\begin{table}\begin{tabular}{l}a\end{tabular}\end{table}'
        tables = extract(latex)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows[0].cells[0].text, 'a')

    def test_json_object_is_serializable(self):
        table = one_table(r'\begin{tabular}{ll}a & b\end{tabular}')
        obj = table.to_json_object()
        json.dumps(obj)
        self.assertEqual(obj['column_count'], 2)
        self.assertEqual(len(obj['cells']), 2)
        self.assertEqual(obj['cells'][0]['text'], 'a')

    def test_no_tables(self):
        self.assertEqual(extract('just text'), [])

    def test_cell_text_follows_the_converter_options(self):
        latex = r'\begin{tabular}{l}\textbf{bold}\end{tabular}'
        unicode_table = one_table(latex)
        plain_table = one_table(latex, text_fontstyle=False)
        self.assertNotEqual(unicode_table.rows[0].cells[0].text,
                            plain_table.rows[0].cells[0].text)
        self.assertEqual(plain_table.rows[0].cells[0].text, 'bold')



# ------------------------------------------------------------------------------
# Rule arguments and content that share a node.
# ------------------------------------------------------------------------------

class TestRuleArguments(unittest.TestCase):

    def test_optional_rule_argument_is_not_content(self):
        table = one_table(
            r'\begin{tabular}{ll}\toprule[1pt] a & b\end{tabular}'
        )
        self.assertEqual(grid(table), [[(0, 'a'), (1, 'b')]])
        self.assertEqual(table.rows[0].cells[0].source, 'a')

    def test_bracketed_content_after_hline_is_kept(self):
        # `\hline` takes no argument, so `[a]` is a cell's value
        table = one_table(
            r'\begin{tabular}{ll}\hline [a] & b\end{tabular}'
        )
        self.assertEqual(grid(table), [[(0, '[a]'), (1, 'b')]])

    def test_rule_argument_does_not_swallow_the_content(self):
        table = one_table(
            r'\begin{tabular}{ll}\toprule[1pt] & b\end{tabular}'
        )
        self.assertEqual(grid(table), [[(0, ''), (1, 'b')]])
        self.assertTrue(table.rows[0].cells[0].empty)

    def test_cmidrule_parenthesized_part_is_skipped(self):
        table = one_table(
            r'\begin{tabular}{ll}\cmidrule(lr){1-2} a & b\end{tabular}'
        )
        self.assertEqual(grid(table), [[(0, 'a'), (1, 'b')]])

    def test_multicolumn_content_with_a_nested_multirow(self):
        table = one_table(
            r'\begin{tabular}{lll}'
            r'a & \multicolumn{2}{l}{\multirow{2}{*}{x}}'
            r'\end{tabular}'
        )
        cell = table.rows[0].cells[1]
        self.assertEqual(cell.text, 'x')
        self.assertEqual(cell.colspan, 2)

### END_TEST_PYLATEXENC_SKIP
