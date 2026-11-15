# -*- coding: utf-8 -*-
#
# Tests for `pylatexenc.latexoutline`, the hierarchical document outline built
# from the sectioning commands of a LaTeX document.
#

import unittest

from pylatexenc.latexoutline import (
    latex_outline,
    outline_for_nodelist,
    sectioning_levels,
)
from pylatexenc.latexwalker import LatexWalker


def _titles(outline):
    return [entry.title for entry in outline.entries]


def _numbers(outline):
    return [entry.number for entry in outline.entries]


class TestOutlineBasics(unittest.TestCase):

    def test_empty_document_has_no_entries(self):
        outline = latex_outline('')
        self.assertEqual(outline.entries, [])
        self.assertEqual(outline.roots, [])
        self.assertEqual(len(outline), 0)

    def test_sectioning_levels(self):
        self.assertEqual(sectioning_levels[0], 'part')
        self.assertEqual(sectioning_levels[-1], 'subparagraph')

    def test_flat_document(self):
        outline = latex_outline(r'''
\section{One}
text
\section{Two}
text
\section{Three}
''')
        self.assertEqual(_titles(outline), ['One', 'Two', 'Three'])
        self.assertEqual(_numbers(outline), ['1', '2', '3'])
        self.assertTrue(all(entry.parent is None for entry in outline.entries))
        self.assertEqual([entry.index for entry in outline.entries], [0, 1, 2])

    def test_hierarchy_and_body_ranges(self):
        source = r'''
\documentclass{report}
\begin{document}
\chapter{Top}
chapter body
\section{Child}
child body
\subsection{Grandchild}
grandchild body
\section{Second child}
second body
\end{document}
'''
        outline = latex_outline(source)
        chapter, child, grandchild, second = outline.entries

        self.assertEqual(chapter.title, 'Top')
        self.assertEqual(child.parent, chapter)
        self.assertEqual(grandchild.parent, child)
        self.assertEqual(second.parent, chapter)
        self.assertEqual(chapter.children, [child, second])
        self.assertEqual(child.children, [grandchild])

        # A body includes its subsections, and stops where the next heading of
        # the same or a higher level begins.
        self.assertEqual(child.body_end, second.heading_pos)
        self.assertEqual(chapter.body_end, source.index(r'\end{document}'))
        self.assertEqual(child.own_body_end, grandchild.heading_pos)
        self.assertTrue(child.body_start <= child.body_end)
        self.assertTrue(grandchild.body_start <= grandchild.body_end)

    def test_body_range_positions_are_source_offsets(self):
        source = r'''\section{One}
alpha
\section{Two}
beta
'''
        outline = latex_outline(source)
        first, second = outline.entries
        self.assertEqual(source[first.heading_pos:first.heading_pos_end],
                         r'\section{One}')
        self.assertEqual(source[first.body_start:first.body_end],
                         '\nalpha\n')
        self.assertEqual(source[second.body_start:second.body_end], '\nbeta\n')

    def test_entry_at(self):
        source = r'''\section{One}
alpha
\subsection{Sub}
beta
\section{Two}
gamma
'''
        outline = latex_outline(source)
        pos = source.index('beta')
        self.assertEqual(outline.entry_at(pos).title, 'Sub')
        self.assertEqual(outline.entry_at(source.index('gamma')).title, 'Two')


class TestStarredAndShortTitles(unittest.TestCase):

    def test_starred_heading_is_unnumbered(self):
        outline = latex_outline(r'''
\section*{Preface}
text
\section{Real}
text
''')
        preface, real = outline.entries
        self.assertTrue(preface.starred)
        self.assertIsNone(preface.number)
        self.assertIsNone(preface.number_parts)
        self.assertEqual(real.number, '1')

    def test_starred_heading_does_not_advance_the_counter(self):
        outline = latex_outline(r'''
\section{One}
\section*{Unnumbered}
\section{Two}
''')
        self.assertEqual(_numbers(outline), ['1', None, '2'])

    def test_starred_heading_does_not_reset_deeper_counters(self):
        # A starred section is not numbered, so it must not reset the
        # subsection counter either: the second subsection is '1.2'.
        outline = latex_outline(r'''
\section{One}
\subsection{First}
\section*{Unnumbered}
\subsection{Second}
''')
        self.assertEqual(_numbers(outline), ['1', '1.1', None, '1.2'])

    def test_short_title_is_used_for_the_table_of_contents(self):
        outline = latex_outline(
            r'\section[Short]{The full and rather long section title}'
        )
        entry = outline.entries[0]
        self.assertEqual(entry.title, 'Short')
        self.assertEqual(entry.short_title, 'Short')
        self.assertEqual(entry.full_title, 'The full and rather long section title')

    def test_without_short_title_the_full_title_is_used(self):
        outline = latex_outline(r'\section{Just one title}')
        entry = outline.entries[0]
        self.assertEqual(entry.title, 'Just one title')
        self.assertIsNone(entry.short_title)
        self.assertEqual(entry.full_title, 'Just one title')

    def test_star_and_short_title_together(self):
        outline = latex_outline(r'\section*[Short]{Long}')
        entry = outline.entries[0]
        self.assertTrue(entry.starred)
        self.assertIsNone(entry.number)
        self.assertEqual(entry.title, 'Short')
        self.assertEqual(entry.full_title, 'Long')

    def test_title_markup_is_rendered_as_text(self):
        outline = latex_outline(r'\section{The \textbf{bold} and the \emph{italic}}')
        self.assertEqual(outline.entries[0].title, 'The bold and the italic')

    def test_titles_collapse_whitespace(self):
        outline = latex_outline('\\section{A\n   title}')
        self.assertEqual(outline.entries[0].title, 'A title')


class TestNumbering(unittest.TestCase):

    def test_article_style_numbering(self):
        outline = latex_outline(r'''
\section{A}
\subsection{B}
\subsubsection{C}
\paragraph{D}
\subparagraph{E}
''')
        self.assertFalse(outline.chapter_style)
        self.assertEqual(_numbers(outline),
                         ['1', '1.1', '1.1.1', None, None])

    def test_chapter_style_numbering_from_documentclass(self):
        outline = latex_outline(r'''
\documentclass{book}
\begin{document}
\chapter{A}
\section{B}
\subsection{C}
\end{document}
''')
        self.assertTrue(outline.chapter_style)
        self.assertEqual(_numbers(outline), ['1', '1.1', '1.1.1'])

    def test_chapter_style_is_detected_from_an_actual_chapter(self):
        # The class is unknown, but the document uses \chapter; sections must
        # be numbered 'chapter.section'.
        outline = latex_outline(r'''
\documentclass{mycustomclass}
\begin{document}
\chapter{A}
\section{B}
\end{document}
''')
        self.assertTrue(outline.chapter_style)
        self.assertEqual(_numbers(outline), ['1', '1.1'])

    def test_part_is_numbered_with_roman_numerals(self):
        outline = latex_outline(r'''
\part{First}
\part{Second}
''')
        self.assertEqual(_numbers(outline), ['I', 'II'])
        self.assertEqual(outline.entries[0].number_parts, (1,))

    def test_part_does_not_reset_chapter_counter(self):
        outline = latex_outline(r'''
\documentclass{book}
\begin{document}
\part{One}
\chapter{A}
\part{Two}
\chapter{B}
\end{document}
''')
        self.assertEqual([entry.number for entry in outline.entries],
                         ['I', '1', 'II', '2'])

    def test_deeper_counters_reset_on_a_new_section(self):
        outline = latex_outline(r'''
\section{A}
\subsection{A1}
\subsubsection{A1a}
\section{B}
\subsection{B1}
''')
        self.assertEqual(_numbers(outline), ['1', '1.1', '1.1.1', '2', '2.1'])

    def test_counter_reset_through_setcounter(self):
        outline = latex_outline(r'''
\section{One}
\section{Two}
\setcounter{section}{0}
\section{Restarted}
''')
        self.assertEqual(_numbers(outline), ['1', '2', '1'])

    def test_counter_reset_does_not_merge_entries(self):
        # Two entries with the same displayed number are still two distinct
        # outline entries with distinct structural keys.
        outline = latex_outline(r'''
\section{A}
\setcounter{section}{0}
\section{B}
''')
        first, second = outline.entries
        self.assertEqual(first.number, '1')
        self.assertEqual(second.number, '1')
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(len(outline.find(number='1')), 2)
        self.assertEqual(outline.get(second.key), second)

    def test_addtocounter_and_stepcounter(self):
        outline = latex_outline(r'''
\section{A}
\addtocounter{section}{4}
\section{B}
\stepcounter{section}
\section{C}
''')
        self.assertEqual(_numbers(outline), ['1', '6', '8'])

    def test_appendix_switches_to_letters(self):
        outline = latex_outline(r'''
\documentclass{book}
\begin{document}
\chapter{Main}
\appendix
\chapter{Extra}
\section{Detail}
\end{document}
''')
        self.assertEqual([entry.number for entry in outline.entries],
                         ['1', 'A', 'A.1'])

    def test_secnumdepth_suppresses_deeper_numbers(self):
        outline = latex_outline(r'''
\section{A}
\setcounter{secnumdepth}{1}
\subsection{B}
\subsubsection{C}
''')
        self.assertEqual(_numbers(outline), ['1', None, None])

    def test_number_parts(self):
        outline = latex_outline(r'''
\documentclass{book}
\begin{document}
\chapter{A}
\section{B}
\subsection{C}
\end{document}
''')
        self.assertEqual([entry.number_parts for entry in outline.entries],
                         [(1,), (1, 1), (1, 1, 1)])

    def test_number_is_not_the_unique_key(self):
        # The same displayed number can occur more than once; the structural
        # key and the index remain distinct.
        outline = latex_outline(r'''
\section{First}
\section{Second}
\setcounter{section}{0}
\section{Third}
''')
        numbers = [entry.number for entry in outline.entries]
        self.assertEqual(numbers, ['1', '2', '1'])
        self.assertEqual(len(set(numbers)), 2)
        self.assertEqual(len(set(entry.key for entry in outline.entries)), 3)
        self.assertEqual([entry.index for entry in outline.entries], [0, 1, 2])


class TestDuplicateTitles(unittest.TestCase):

    def test_duplicate_subsection_titles_are_both_kept(self):
        outline = latex_outline(r'''
\section{Main}
\subsection{Methods}
First version.
\subsection{Methods}
Second version.
''')
        self.assertEqual(_titles(outline), ['Main', 'Methods', 'Methods'])
        matches = outline.find(title='Methods')
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0].number, '1.1')
        self.assertEqual(matches[1].number, '1.2')
        self.assertNotEqual(matches[0].key, matches[1].key)
        # Each duplicate has its own body range.
        self.assertNotEqual(matches[0].body_start, matches[1].body_start)

    def test_duplicate_titles_at_different_levels(self):
        outline = latex_outline(r'''
\section{Same}
\subsection{Same}
''')
        self.assertEqual(_titles(outline), ['Same', 'Same'])
        self.assertEqual(len(outline.find(title='Same')), 2)


class TestMissingParents(unittest.TestCase):

    def test_level_jump_reports_the_missing_level(self):
        outline = latex_outline(r'''
\section{A}
\subsubsection{Jumped}
''')
        jumped = outline.entries[1]
        self.assertEqual(jumped.missing_parent_levels, ('subsection',))
        self.assertEqual(jumped.missing_parent_level, 'subsection')
        # The entry still attaches to the nearest existing ancestor.
        self.assertEqual(jumped.parent.title, 'A')
        self.assertEqual(jumped.depth, 1)

    def test_level_jump_reports_all_missing_levels(self):
        outline = latex_outline(r'''
\documentclass{report}
\begin{document}
\chapter{A}
\subsubsection{Deep jump}
\end{document}
''')
        deep = outline.entries[1]
        self.assertEqual(deep.missing_parent_levels, ('section', 'subsection'))
        self.assertEqual(deep.parent.title, 'A')

    def test_no_missing_parents_in_a_complete_hierarchy(self):
        outline = latex_outline(r'''
\section{A}
\subsection{B}
\subsubsection{C}
''')
        for entry in outline.entries:
            self.assertEqual(entry.missing_parent_levels, ())

    def test_missing_parents_are_shown_in_the_formatted_output(self):
        outline = latex_outline(r'''
\section{A}
\subsubsection{Jumped}
''')
        self.assertIn('missing parent', outline.format())
        self.assertIn('subsection', outline.format())


class TestReferencedTitles(unittest.TestCase):

    def test_ref_and_nameref_do_not_create_entries(self):
        outline = latex_outline(r'''
\section{Real}
See \ref{sec:fake}, \nameref{sec:maybe} and \autoref{fig:thing}.
''')
        self.assertEqual(_titles(outline), ['Real'])

    def test_addcontentsline_does_not_create_an_entry(self):
        outline = latex_outline(r'''
\section{Real}
\addcontentsline{toc}{section}{A manually added title}
''')
        self.assertEqual(_titles(outline), ['Real'])

    def test_sectioning_macro_in_a_macro_definition_is_not_an_entry(self):
        outline = latex_outline(r'''
\section{Real}
\newcommand{\fake}{\section{In a macro definition}}
''')
        self.assertEqual(_titles(outline), ['Real'])

    def test_sectioning_macro_in_an_environment_argument_is_not_an_entry(self):
        outline = latex_outline(r'''
\section{Real}
\begin{figure}
\caption{Not a section}
\section{Inside the figure argument?}
\end{figure}
''')
        # \section inside the figure body is a real sectioning command; only
        # the argument of \caption is skipped.
        self.assertEqual(_titles(outline), ['Real', 'Inside the figure argument?'])


class TestDocumentBoundaries(unittest.TestCase):

    def test_preamble_is_ignored(self):
        outline = latex_outline(r'''
\documentclass{book}
\section{Preamble heading}
\begin{document}
\chapter{Real}
\end{document}
''')
        self.assertEqual(_titles(outline), ['Real'])
        self.assertEqual(_numbers(outline), ['1'])

    def test_content_after_end_document_is_ignored(self):
        outline = latex_outline(r'''
\begin{document}
\section{Inside}
\end{document}
\section{Outside}
''')
        self.assertEqual(_titles(outline), ['Inside'])

    def test_counter_changes_outside_the_document_body_are_ignored(self):
        outline = latex_outline(r'''
\documentclass{article}
\setcounter{section}{7}
\begin{document}
\section{First}
\end{document}
\setcounter{section}{0}
''')
        self.assertEqual(_numbers(outline), ['1'])

    def test_without_document_environment_the_whole_input_is_used(self):
        outline = latex_outline(r'''
\section{One}
text
\section{Two}
''')
        self.assertEqual(_titles(outline), ['One', 'Two'])

    def test_document_environment_without_sections(self):
        outline = latex_outline(r'''
\begin{document}
Just text.
\end{document}
''')
        self.assertEqual(outline.entries, [])


class TestOutlineApi(unittest.TestCase):

    def test_outline_for_nodelist(self):
        walker = LatexWalker(r'\section{A}text\subsection{B}more')
        nodelist, _ = walker.parse_content()
        outline = outline_for_nodelist(nodelist, latex_walker=walker)
        self.assertEqual(_titles(outline), ['A', 'B'])
        self.assertEqual(outline.entries[1].parent.title, 'A')
        self.assertEqual(outline.entries[0].body_end, len(walker.s))

    def test_plain_node_list_is_accepted(self):
        walker = LatexWalker(r'\section{A}')
        nodelist, _ = walker.parse_content()
        outline = outline_for_nodelist(list(nodelist))
        self.assertEqual(_titles(outline), ['A'])

    def test_line_numbers(self):
        outline = latex_outline('\\section{A}\ntext\n\\section{B}\n')
        self.assertEqual([entry.lineno for entry in outline.entries], [1, 3])
        self.assertEqual(outline.entries[0].colno, 0)

    def test_to_dict_round_trips_the_structure(self):
        outline = latex_outline(r'''
\documentclass{book}
\begin{document}
\chapter{A}
\section{B}
\end{document}
''')
        data = outline.to_dict()
        self.assertTrue(data['chapter_style'])
        self.assertEqual(len(data['entries']), 2)
        second = data['entries'][1]
        self.assertEqual(second['title'], 'B')
        self.assertEqual(second['parent_key'], ((('chapter', 1)),))
        self.assertEqual(second['key'], (('chapter', 1), ('section', 1)))

    def test_custom_sectioning_macro(self):
        outline = latex_outline(
            r'\section{A}\mysec{B}\subsection{C}',
            sectioning_macros={
                'section': 'section',
                'mysec': 'section',
                'subsection': 'subsection',
            }
        )
        self.assertEqual([entry.macroname for entry in outline.entries],
                         ['section', 'mysec', 'subsection'])
        self.assertEqual(_titles(outline), ['A', 'B', 'C'])
        self.assertEqual(_numbers(outline), ['1', '2', '2.1'])

    def test_custom_mapping_with_unknown_level_raises(self):
        with self.assertRaises(ValueError):
            latex_outline(r'\section{A}', sectioning_macros={'section': 'bogus'})

    def test_format_indentation_follows_the_tree_depth(self):
        outline = latex_outline(r'''
\section{A}
\subsection{B}
\subsubsection{C}
''')
        lines = outline.format(include_ranges=False).split('\n')
        self.assertTrue(lines[0].startswith('SECTION'))
        self.assertTrue(lines[1].startswith('  SUBSECTION'))
        self.assertTrue(lines[2].startswith('    SUBSUBSECTION'))


if __name__ == '__main__':
    unittest.main()
