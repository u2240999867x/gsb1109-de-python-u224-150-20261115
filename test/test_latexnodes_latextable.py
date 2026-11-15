import unittest

from pylatexenc.latexwalker import LatexWalker
from pylatexenc.macrospec import MacroSpec
from pylatexenc.latexwalker._get_defaultspecs import (
    get_default_latex_context_db,
)

from pylatexenc.latexnodes.latextable import (
    DEFAULT_TABLE_ENVIRONMENT_NAMES,
    ExtractedTableCell,
    ExtractedTableRow,
    ExtractedTable,
    LatexTableExtractionError,
    extract_latex_tables,
    extract_table_from_environment_node,
)


class TestLatexTableExtractor(unittest.TestCase):

    def __init__(self, *args, **kwargs):
        super(TestLatexTableExtractor, self).__init__(*args, **kwargs)
        self.maxDiff = None

    def _cell_texts(self, table):
        return [
            [cell.text for cell in row.cells]
            for row in table.rows
        ]

    def test_basic_rows_and_cells(self):
        latex = (
            r"\begin{tabular}{lcr}"
            r"Name & Value & Note \\"
            r"a & b & c \\"
            r"\end{tabular}"
        )
        tables = extract_latex_tables(latex)
        self.assertEqual(len(tables), 1)
        table = tables[0]

        self.assertIsInstance(table, ExtractedTable)
        self.assertEqual(table.environmentname, 'tabular')
        self.assertFalse(table.is_truncated)
        self.assertEqual(table.n_columns, 3)
        self.assertEqual(len(table.rows), 2)
        self.assertEqual(
            self._cell_texts(table),
            [['Name', 'Value', 'Note'], ['a', 'b', 'c']],
        )

        for row in table.rows:
            self.assertIsInstance(row, ExtractedTableRow)
            for cell in row.cells:
                self.assertIsInstance(cell, ExtractedTableCell)
                self.assertEqual(cell.colspan, 1)
                self.assertEqual(
                    cell.column_end, cell.column + cell.colspan
                )

    def test_separator_inside_macro_argument_is_not_split(self):
        latex = (
            r"\begin{tabular}{cc}"
            r"a & \textbf{x & y} \\"
            r"c & d \\"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(
            self._cell_texts(table),
            [['a', r'\textbf{x & y}'], ['c', 'd']],
        )
        self.assertEqual(
            table.rows[0].cells[1].raw_text,
            r' \textbf{x & y} ',
        )

    def test_separator_inside_nested_group_is_not_split(self):
        latex = (
            r"\begin{tabular}{cc}"
            r"{x & y} & z \\"
            r"a & b \\"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(
            self._cell_texts(table),
            [[r'{x & y}', 'z'], ['a', 'b']],
        )

    def test_escaped_ampersand_is_ordinary_text(self):
        latex = (
            r"\begin{tabular}{cc}"
            r"A \& B & C \\"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(
            self._cell_texts(table),
            [[r'A \& B', 'C']],
        )
        self.assertEqual(len(table.rows[0].cells), 2)

    def test_multicolumn_records_column_span(self):
        latex = (
            r"\begin{tabular}{ccc}"
            r"\multicolumn{2}{c|}{wide} & z \\"
            r"a & b & c \\"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        first_row = table.rows[0]
        self.assertEqual(len(first_row.cells), 2)

        spanning = first_row.cells[0]
        self.assertEqual(spanning.text, 'wide')
        self.assertEqual(spanning.column, 0)
        self.assertEqual(spanning.colspan, 2)
        self.assertEqual(spanning.column_end, 2)

        tail = first_row.cells[1]
        self.assertEqual(tail.column, 2)
        self.assertEqual(tail.colspan, 1)
        self.assertEqual(tail.text, 'z')

        self.assertEqual(table.n_columns, 3)
        self.assertEqual(
            self._cell_texts(table)[1], ['a', 'b', 'c']
        )

    def test_multicolumn_with_registered_macro_spec(self):
        context = get_default_latex_context_db().filtered_context()
        context.add_context_category(
            'test-multicolumn',
            macros=[MacroSpec('multicolumn', '{{{')],
        )
        latex = (
            r"\begin{tabular}{cc}"
            r"\multicolumn{2}{c}{X} & Y \\"
            r"\end{tabular}"
        )
        walker = LatexWalker(latex, latex_context=context)
        table = extract_latex_tables(latex, latex_walker=walker)[0]

        spanning = table.rows[0].cells[0]
        self.assertEqual(spanning.colspan, 2)
        self.assertEqual(spanning.text, 'X')
        self.assertEqual(table.rows[0].cells[1].text, 'Y')

    def test_malformed_multicolumn_falls_back_to_content(self):
        latex = (
            r"\begin{tabular}{cc}"
            r"\multicolumn{x}{c}{z} & w \\"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(
            self._cell_texts(table),
            [[r'\multicolumn{x}{c}{z}', 'w']],
        )

    def test_empty_cell_distinct_from_whitespace_cell(self):
        latex = (
            r"\begin{tabular}{ccc}"
            "a&&b\\\\"
            "& & \\\\"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]

        empty_cell = table.rows[0].cells[1]
        self.assertTrue(empty_cell.is_empty)
        self.assertFalse(empty_cell.is_whitespace_only)
        self.assertEqual(empty_cell.text, '')
        self.assertEqual(empty_cell.raw_text, '')

        whitespace_cell = table.rows[1].cells[1]
        self.assertFalse(whitespace_cell.is_empty)
        self.assertTrue(whitespace_cell.is_whitespace_only)
        self.assertEqual(whitespace_cell.text, '')
        self.assertEqual(whitespace_cell.raw_text, ' ')

    def test_row_end_macro_variants(self):
        latex = (
            r"\begin{array}{cc}"
            "a & b \\cr\n"
            "c & d \\tabularnewline\n"
            r"e & f \\*[6pt]"
            r"g & h \\ "
            r"\end{array}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(
            self._cell_texts(table),
            [['a', 'b'], ['c', 'd'], ['e', 'f'], ['g', 'h']],
        )

    def test_trailing_row_without_row_end_macro_is_kept(self):
        latex = (
            r"\begin{tabular}{cc}"
            r"a & b \\"
            r"c & d"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(
            self._cell_texts(table),
            [['a', 'b'], ['c', 'd']],
        )

    def test_whitespace_before_end_is_not_a_phantom_row(self):
        latex = (
            r"\begin{tabular}{cc}"
            r"a & b \\"
            "\n"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(
            self._cell_texts(table), [['a', 'b']]
        )

    def test_unclosed_table_stops_before_following_paragraph(self):
        latex = (
            "\\begin{tabular}{cc}\n"
            "1 & 2 \\\\\n"
            "3 & 4\n"
            "\n"
            "This is the next paragraph of prose.\n"
        )
        table = extract_latex_tables(latex)[0]

        self.assertTrue(table.is_truncated)
        self.assertEqual(
            self._cell_texts(table),
            [['1', '2'], ['3', '4']],
        )
        self.assertNotIn(
            'next paragraph',
            latex[table.source_position.pos:table.source_position.pos_end],
        )
        self.assertLess(table.source_position.pos_end, len(latex))

    def test_unclosed_table_at_end_of_input_is_truncated(self):
        latex = (
            "\\begin{tabular}{cc}\n"
            "a & b \\\\\n"
            "c & d"
        )
        table = extract_latex_tables(latex)[0]
        self.assertTrue(table.is_truncated)
        self.assertEqual(
            self._cell_texts(table),
            [['a', 'b'], ['c', 'd']],
        )

    def test_blank_line_inside_closed_table_is_not_truncation(self):
        latex = (
            "\\begin{tabular}{cc}\n"
            "a & b \\\\\n"
            "\n"
            "c & d \\\\\n"
            "\\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertFalse(table.is_truncated)
        self.assertEqual(
            self._cell_texts(table),
            [['a', 'b'], ['c', 'd']],
        )

    def test_multiple_tables_and_non_table_environments(self):
        latex = (
            r"\begin{tabular}{cc} a & b \\ \end{tabular}"
            " prose with a & sign "
            r"\begin{tabular*}{10cm}{cc} x & y \end{tabular*}"
            r"\begin{itemize} \item not a table \end{itemize}"
        )
        tables = extract_latex_tables(latex)
        self.assertEqual(len(tables), 2)
        self.assertEqual(
            [table.environmentname for table in tables],
            ['tabular', 'tabular*'],
        )
        self.assertEqual(self._cell_texts(tables[1]), [['x', 'y']])

    def test_tabular_inside_table_float_is_found(self):
        latex = (
            r"\begin{table}[h]"
            r"\begin{tabular}{cc} a & b \\ \end{tabular}"
            r"\end{table}"
            "Some body text with an unrelated & symbol."
        )
        tables = extract_latex_tables(latex)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].environmentname, 'tabular')
        self.assertEqual(
            self._cell_texts(tables[0]), [['a', 'b']]
        )

    def test_nested_tabular_is_not_extracted_twice(self):
        latex = (
            r"\begin{tabular}{cc}"
            r"a & \begin{tabular}{c} n \end{tabular} \\"
            r"c & d \\"
            r"\end{tabular}"
        )
        tables = extract_latex_tables(latex)
        self.assertEqual(len(tables), 1)
        outer = tables[0]
        self.assertEqual(
            self._cell_texts(outer),
            [['a', r'\begin{tabular}{c} n \end{tabular}'], ['c', 'd']],
        )

    def test_tables_are_reported_in_source_order(self):
        latex = (
            r"\begin{table}\begin{tabular}{cc} 1 & 2 \\ \end{tabular}\end{table}"
            r"\begin{tabular}{cc} 3 & 4 \\ \end{tabular}"
        )
        tables = extract_latex_tables(latex)
        self.assertEqual(len(tables), 2)
        self.assertEqual(self._cell_texts(tables[0]), [['1', '2']])
        self.assertEqual(self._cell_texts(tables[1]), [['3', '4']])
        self.assertLess(
            tables[0].source_position.pos,
            tables[1].source_position.pos,
        )

    def test_custom_environment_name_filter(self):
        latex = (
            r"\begin{tabular}{cc} a & b \\ \end{tabular}"
            r"\begin{mytab}{cc} c & d \\ \end{mytab}"
        )
        tables = extract_latex_tables(
            latex, table_environment_names=('mytab',)
        )
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].environmentname, 'mytab')

    def test_unknown_table_environment_skips_alignment_argument(self):
        latex = (
            r"\begin{longtable}{cc}"
            "a & b \\\\\n"
            "c & d \\\\\n"
            r"\end{longtable}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(table.environmentname, 'longtable')
        self.assertEqual(
            self._cell_texts(table),
            [['a', 'b'], ['c', 'd']],
        )
        self.assertNotIn('cc', table.rows[0].cells[0].raw_text)

    def test_default_environment_names_include_known_tables(self):
        self.assertIn('tabular', DEFAULT_TABLE_ENVIRONMENT_NAMES)
        self.assertIn('array', DEFAULT_TABLE_ENVIRONMENT_NAMES)

    def test_source_positions_point_into_source(self):
        latex = (
            "\\begin{tabular}{cc}\n"
            "a & b \\\\\n"
            "c & d \\\\\n"
            "\\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]

        self.assertEqual(table.source_position.pos, 0)
        self.assertEqual(table.source_position.pos_end, len(latex))
        self.assertEqual(table.source_position.lineno, 1)
        self.assertEqual(table.source_position.end_lineno, 4)

        walker = LatexWalker(latex)
        for row in table.rows:
            for cell in row.cells:
                self.assertEqual(
                    latex[
                        cell.source_position.pos:
                        cell.source_position.pos_end
                    ],
                    cell.raw_text,
                )
                lineno, colno = walker.pos_to_lineno_colno(
                    cell.source_position.pos
                )
                self.assertEqual(cell.source_position.lineno, lineno)
                self.assertEqual(cell.source_position.colno, colno)

        second_cell = table.rows[0].cells[1]
        self.assertEqual(second_cell.source_position.lineno, 2)

        row = table.rows[1]
        self.assertEqual(
            latex[
                row.source_position.pos:row.source_position.pos_end
            ].strip(),
            'c & d',
        )

    def test_comments_do_not_break_rows(self):
        latex = (
            r"\begin{tabular}{cc}"
            "a & b \\\\\n"
            "% a comment line\n"
            "c & d \\\\\n"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        self.assertEqual(
            self._cell_texts(table),
            [['a', 'b'], ['c', 'd']],
        )

    def test_comment_only_cell_is_empty_but_kept_in_raw_text(self):
        latex = (
            r"\begin{tabular}{ccc}"
            "a &% only a comment\n"
            " & c \\\\\n"
            r"\end{tabular}"
        )
        table = extract_latex_tables(latex)[0]
        comment_cell = table.rows[0].cells[1]
        self.assertTrue(comment_cell.is_empty)
        self.assertFalse(comment_cell.is_whitespace_only)
        self.assertEqual(comment_cell.text, '')
        self.assertIn('%', comment_cell.raw_text)

    def test_no_tables_returns_empty_list(self):
        self.assertEqual(
            extract_latex_tables(r"just some prose with \textbf{macros}."),
            [],
        )

    def test_extract_from_environment_node_directly(self):
        latex = r"\begin{tabular}{cc} a & b \\ \end{tabular}"
        walker = LatexWalker(latex)
        nodelist, _ = walker.parse_content()
        table = extract_table_from_environment_node(
            nodelist[0], latex_walker=walker
        )
        self.assertEqual(table.environmentname, 'tabular')
        self.assertEqual(
            self._cell_texts(table), [['a', 'b']]
        )

    def test_error_on_non_environment_node(self):
        with self.assertRaises(LatexTableExtractionError):
            extract_table_from_environment_node(object())

    def test_error_on_walker_source_mismatch(self):
        walker = LatexWalker(r"\begin{tabular}{cc} a \end{tabular}")
        with self.assertRaises(LatexTableExtractionError):
            extract_latex_tables("different source", latex_walker=walker)


if __name__ == '__main__':
    unittest.main()
