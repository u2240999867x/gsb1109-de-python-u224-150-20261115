# -*- coding: utf-8 -*-

import unittest

from pylatexenc.latexwalker import (
    LatexWalker,
    LatexOutline,
    SECTIONING_MACROS,
    get_outline,
    build_outline,
    get_table_of_contents,
)


class TestLatexOutline(unittest.TestCase):

    maxDiff = None

    def assertHeadings(self, entries, expected):
        self.assertEqual(
            [(entry.level_name, entry.number, entry.title) for entry in entries],
            expected,
        )

    def test_simple_hierarchy(self):
        outline = get_outline(
            r'\section{One}'
            r'\subsection{One A}'
            r'\subsubsection{One A 1}'
            r'\subsection{One B}'
            r'\section{Two}'
            r'\subsection{Two A}'
        )

        self.assertIsInstance(outline, LatexOutline)
        self.assertEqual(len(outline), 6)
        self.assertHeadings(outline, [
            ('section', '1', 'One'),
            ('subsection', '1.1', 'One A'),
            ('subsubsection', '1.1.1', 'One A 1'),
            ('subsection', '1.2', 'One B'),
            ('section', '2', 'Two'),
            ('subsection', '2.1', 'Two A'),
        ])

        self.assertEqual([entry.title for entry in outline.roots], ['One', 'Two'])
        self.assertEqual(
            [entry.title for entry in outline[0].children],
            ['One A', 'One B'],
        )
        self.assertEqual(
            [entry.title for entry in outline[0].children[0].children],
            ['One A 1'],
        )
        self.assertEqual(
            [entry.title for entry in outline.walk()],
            [entry.title for entry in outline],
        )

    def test_body_ranges(self):
        source = (
            r'Preamble.'
            r'\section{One}'
            r'Body one.'
            r'\subsection{One A}'
            r'Body one A.'
            r'\section{Two}'
            r'Body two.'
        )
        outline = get_outline(source)

        one, one_a, two = outline

        # A section's body includes the bodies of its subsections; it ends
        # where the next section of the same or a higher level begins.
        self.assertEqual(source[one.body_start:one.body_end],
                         r'Body one.'
                         r'\subsection{One A}'
                         r'Body one A.')
        self.assertEqual(source[one_a.body_start:one_a.body_end],
                         r'Body one A.')
        self.assertEqual(source[two.body_start:two.body_end], r'Body two.')
        self.assertEqual(one_a.body_end, two.heading_start)
        self.assertEqual(two.body_end, len(source))

        self.assertEqual(one.body_range, (one.body_start, one.body_end))
        self.assertEqual(one.heading_range, (one.heading_start, one.heading_end))
        self.assertEqual(one.own_body_range,
                         (one.body_start, one_a.heading_start))
        self.assertEqual(one.full_body_range, one.body_range)
        self.assertEqual(one.own_body_latex, r'Body one.')
        self.assertEqual(one_a.own_body_latex, r'Body one A.')
        self.assertEqual(one.lineno, 1)
        self.assertEqual(one.end_lineno, 1)
        self.assertEqual(one.latex_walker.s, source)

    def test_heading_ranges_do_not_overlap_bodies(self):
        source = r'Intro \section{One} body \subsection{Sub} more'
        outline = get_outline(source)
        one, sub = outline

        self.assertEqual(source[one.heading_start:one.heading_end],
                         r'\section{One}')
        self.assertEqual(one.body_start, one.heading_end)
        self.assertEqual(one.body_end, len(source))
        self.assertEqual(source[sub.heading_start:sub.heading_end],
                         r'\subsection{Sub}')
        self.assertEqual(source[sub.body_start:sub.body_end], r' more')
        self.assertEqual(sub.body_end, len(source))
        self.assertTrue(one.body_start <= sub.heading_start < one.body_end)

    def test_starred_sections_are_included_without_numbers(self):
        outline = get_outline(
            r'\section{One}'
            r'\section*{Starred}'
            r'\subsection{Sub}'
            r'\section{Two}'
        )
        self.assertHeadings(outline, [
            ('section', '1', 'One'),
            ('section', '', 'Starred'),
            ('subsection', '1.1', 'Sub'),
            ('section', '2', 'Two'),
        ])
        self.assertTrue(outline[1].starred)
        self.assertTrue(outline[1].is_starred)
        self.assertEqual(outline[1].number, '')

    def test_starred_sections_can_be_excluded(self):
        outline = get_outline(
            r'\section{One}\section*{Starred}\subsection{Sub}',
            include_starred=False,
        )
        self.assertHeadings(outline, [
            ('section', '1', 'One'),
            ('subsection', '1.1', 'Sub'),
        ])

    def test_starred_section_does_not_step_the_counter(self):
        outline = get_outline(
            r'\section*{Starred}'
            r'\section{First}'
            r'\section*{Another}'
            r'\section{Second}'
        )
        self.assertEqual([entry.number for entry in outline], ['', '1', '', '2'])

    def test_short_titles(self):
        outline = get_outline(
            r'\section[Short one]{A rather long section title}'
            r'\subsection[Short two]{A rather long subsection title}'
        )
        self.assertEqual(outline[0].title, 'Short one')
        self.assertEqual(outline[0].full_title, 'A rather long section title')
        self.assertEqual(outline[0].short_title, 'Short one')
        self.assertEqual(outline[0].toc_title, 'Short one')

        self.assertEqual(outline[1].title, 'Short two')
        self.assertEqual(outline[1].full_title,
                         'A rather long subsection title')

        full = get_outline(
            r'\section[Short one]{A rather long section title}',
            use_short_titles=False,
        )
        self.assertEqual(full[0].title, 'A rather long section title')
        self.assertEqual(full[0].short_title, 'Short one')

    def test_starred_and_short_title_together(self):
        outline = get_outline(r'\section*[Short]{Full title}')
        self.assertEqual(outline[0].title, 'Short')
        self.assertEqual(outline[0].full_title, 'Full title')
        self.assertEqual(outline[0].number, '')
        self.assertTrue(outline[0].starred)

    def test_counter_reset_is_not_treated_as_a_key(self):
        outline = get_outline(
            r'\section{One}'
            r'\section{Two}'
            r'\setcounter{section}{0}'
            r'\section{Three}'
        )
        self.assertEqual([entry.number for entry in outline], ['1', '2', '1'])
        self.assertEqual(len(set(entry.key for entry in outline)), 3)
        self.assertEqual(len(set(entry.id for entry in outline)), 3)
        self.assertNotEqual(outline[0].key, outline[2].key)

    def test_addtocounter_and_stepcounter(self):
        outline = get_outline(
            r'\section{One}'
            r'\addtocounter{section}{2}'
            r'\section{Two}'
            r'\stepcounter{section}'
            r'\section{Three}'
        )
        self.assertEqual([entry.number for entry in outline], ['1', '4', '6'])

    def test_appendix_numbering(self):
        outline = get_outline(
            r'\chapter{First}'
            r'\section{One}'
            r'\appendix'
            r'\chapter{Appendix}'
            r'\section{Appendix section}'
        )
        self.assertHeadings(outline, [
            ('chapter', '1', 'First'),
            ('section', '1.1', 'One'),
            ('chapter', 'A', 'Appendix'),
            ('section', 'A.1', 'Appendix section'),
        ])

    def test_duplicate_titles_are_all_kept(self):
        outline = get_outline(
            r'\section{Same}'
            r'\subsection{Duplicate}'
            r'\subsection{Duplicate}'
            r'\section{Same}'
        )
        self.assertEqual(
            [entry.title for entry in outline],
            ['Same', 'Duplicate', 'Duplicate', 'Same'],
        )
        self.assertEqual(len(outline), 4)
        self.assertNotEqual(outline[1].key, outline[2].key)
        self.assertEqual(len(outline[0].children), 2)

    def test_skipped_level_marks_missing_parent(self):
        outline = get_outline(
            r'\section{Section}'
            r'\subsubsection{Deep}'
        )
        section, deep = outline

        self.assertEqual(section.level_name, 'section')
        self.assertEqual(deep.level_name, 'subsubsection')
        self.assertIs(deep.parent, section)
        self.assertTrue(deep.missing_parent)
        self.assertEqual(deep.missing_parent_levels, ['subsection'])
        self.assertEqual(deep.missing_parent_level, 'subsection')
        self.assertFalse(section.missing_parent)

    def test_skipped_multiple_levels(self):
        outline = get_outline(r'\section{Section}\paragraph{Deep}')
        deep = outline[1]
        self.assertTrue(deep.missing_parent)
        self.assertEqual(deep.missing_parent_levels,
                         ['subsection', 'subsubsection'])

    def test_no_synthetic_parent_is_created(self):
        outline = get_outline(r'\section{S}\subsubsection{Deep}')
        self.assertEqual(len(outline), 2)
        self.assertIs(outline[1].parent, outline[0])
        self.assertEqual(len(outline[0].children), 1)

        # A lone heading has no parent to skip; no placeholder is invented.
        alone = get_outline(r'\subsubsection{Alone}')
        self.assertEqual(len(alone), 1)
        self.assertIsNone(alone[0].parent)
        self.assertFalse(alone[0].missing_parent)
        self.assertEqual(alone[0].missing_parent_levels, [])

    def test_references_do_not_create_sections(self):
        outline = get_outline(
            r'\section{Real}'
            r'See \ref{sec:real} and \autoref{sec:real} and \cref{sec:real}.'
            r'\subsection{Real sub}'
        )
        self.assertEqual([entry.title for entry in outline], ['Real', 'Real sub'])

    def test_section_commands_in_other_arguments_do_not_create_entries(self):
        outline = get_outline(
            r'\section{Real}'
            r'\newcommand{\fake}{\section{Not a heading}}'
            r'\footnote{A footnote with \subsection{also not a heading}}'
            r'\section{Also real}'
        )
        self.assertEqual([entry.title for entry in outline],
                         ['Real', 'Also real'])

    def test_sections_in_verbatim_are_ignored(self):
        outline = get_outline(
            r'\section{Real}'
            r'\begin{verbatim}'
            r'\section{Not a heading}'
            r'\end{verbatim}'
            r'\section{Also real}'
        )
        self.assertEqual([entry.title for entry in outline],
                         ['Real', 'Also real'])

    def test_document_body_only(self):
        outline = get_outline(
            r'\documentclass{article}'
            r'\newcommand{\preamble}{\section{Preamble fake}}'
            r'\begin{document}'
            r'\section{Body}'
            r'\end{document}'
            r'\section{After document}'
        )
        self.assertEqual([entry.title for entry in outline], ['Body'])
        self.assertEqual(
            outline.document_start,
            outline[0].latex_walker.s.index(r'\begin{document}'),
        )

    def test_title_formatting_macros(self):
        outline = get_outline(
            r'\section{\textbf{Bold} and \emph{emphasized} and \texttt{code}}'
            r'\subsection{Math $x^2$ and \LaTeX}'
        )
        self.assertEqual(outline[0].title, 'Bold and emphasized and code')
        self.assertEqual(outline[1].title, 'Math x^2 and LaTeX')

    def test_nested_environments_are_searched(self):
        outline = get_outline(
            r'\begin{document}'
            r'\begin{abstract}Abstract text.\end{abstract}'
            r'\section{One}'
            r'\begin{quote}\subsection{Inside quote}\end{quote}'
            r'\end{document}'
        )
        self.assertEqual([entry.title for entry in outline],
                         ['One', 'Inside quote'])
        self.assertIs(outline[1].parent, outline[0])

    def test_accepts_walker_and_node_list(self):
        source = r'\section{One}\subsection{Sub}'
        walker = LatexWalker(source)
        nodelist, _ = walker.parse_content()

        from_walker = get_outline(walker)
        from_nodes = get_outline(nodelist)
        from_list = get_outline(list(nodelist))
        from_method = walker.get_outline()

        expected = ['One', 'Sub']
        self.assertEqual([entry.title for entry in from_walker], expected)
        self.assertEqual([entry.title for entry in from_nodes], expected)
        self.assertEqual([entry.title for entry in from_list], expected)
        self.assertEqual([entry.title for entry in from_method], expected)

    def test_aliases(self):
        source = r'\section{One}'
        self.assertEqual([entry.title for entry in build_outline(source)],
                         ['One'])
        self.assertEqual(
            [entry.title for entry in get_table_of_contents(source)],
            ['One'],
        )

    def test_custom_sectioning_macros(self):
        outline = get_outline(
            r'\myheading{One}\mysubheading{Sub}',
            sectioning_macros={'myheading': 0, 'mysubheading': 1},
        )
        self.assertEqual([entry.title for entry in outline], ['One', 'Sub'])
        self.assertIs(outline[1].parent, outline[0])
        self.assertFalse(outline[1].missing_parent)

    def test_unknown_macro_with_section_name_is_ignored(self):
        outline = get_outline(r'\section{Real}\sectioning{Not a heading}')
        self.assertEqual([entry.title for entry in outline], ['Real'])

    def test_empty_source(self):
        outline = get_outline('')
        self.assertEqual(len(outline), 0)
        self.assertEqual(outline.roots, [])

    def test_entry_dictionary_interface(self):
        outline = get_outline(r'\section{A long title}')
        entry = outline[0]
        self.assertEqual(entry['title'], 'A long title')
        self.assertEqual(entry['body_range'], entry.body_range)
        self.assertEqual(entry.get('missing_parent'), False)
        self.assertEqual(entry.get('does-not-exist', 'default'), 'default')

        data = entry.to_dict()
        self.assertEqual(data['title'], 'A long title')
        self.assertEqual(data['key'], entry.key)
        self.assertEqual(data['body_start'], entry.body_start)
        self.assertEqual(data['body_end'], entry.body_end)
        self.assertEqual(data['children'], [])

        outline_data = outline.to_dict()
        self.assertEqual(len(outline_data['sections']), 1)

    def test_sectioning_macros_constant(self):
        self.assertEqual(SECTIONING_MACROS['section'], 2)
        self.assertEqual(SECTIONING_MACROS['subsection'], 3)


if __name__ == '__main__':
    unittest.main()
