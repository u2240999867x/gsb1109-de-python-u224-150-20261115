# -*- coding: utf-8 -*-
#
# The MIT License (MIT)
#
# Copyright (c) 2026 Philippe Faist
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

r"""
Build a hierarchical table of contents (an *outline*) from LaTeX code.

The outline is driven by the sectioning macros that the LaTeX parser knows
about: ``\part``, ``\chapter``, ``\section``, ``\subsection``,
``\subsubsection``, ``\paragraph`` and ``\subparagraph``.  Each of them accepts
the usual optional star and optional short title::

    \section*{Unnumbered section}
    \section[Short title for the table of contents]{The full section title}

Two sections with the same title are two outline entries, not one.  The
entries are identified by their position in the document structure (see
:py:attr:`OutlineEntry.key`), never by their displayed number: a
``\setcounter{section}{0}`` or an ``\appendix`` may well cause two different
sections to be numbered ``1.1``, and both still have to remain addressable.

A title that is merely *mentioned* in the running text is not a section.
Only an actual sectioning macro creates an entry; ``\ref``, ``\nameref``,
``\addcontentsline`` and friends do not, and a sectioning macro that appears in
the body of a macro definition (as in ``\newcommand{\foo}{\section{...}}``) does
not either.

The main entry points are :py:func:`latex_outline` for LaTeX source code and
:py:func:`outline_for_nodelist` for an already parsed node tree.  Both return a
:py:class:`LatexOutline` object whose :py:attr:`~LatexOutline.entries` are the
:py:class:`OutlineEntry` objects in document order.

A small example::

    >>> from pylatexenc.latexoutline import latex_outline
    >>> outline = latex_outline(r'''
    ... \chapter{Introduction}
    ... Some text.
    ... \section[Background]{A rather long background section}
    ... More text.
    ... \section*{Acknowledgements}
    ... Thanks.
    ... ''')
    >>> for entry in outline.entries:
    ...     print(entry.number, entry.title)
    1 Introduction
    1.1 Background
    None Acknowledgements

The ``\section*`` entry above is deliberately included.  Starred headings are
part of the document structure and they delimit body ranges; they are reported
as unnumbered entries, and callers who do not want them in a printed table of
contents can filter them out on :py:attr:`OutlineEntry.starred`.

The module is not part of the JavaScript build of the library.
"""

from ..latexwalker import LatexWalker
from ..latexnodes.nodes import (
    LatexCharsNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexEnvironmentNode,
)
from ..latex2text import LatexNodes2Text


__all__ = [
    'sectioning_levels',
    'OutlineEntry',
    'LatexOutline',
    'latex_outline',
    'outline_for_nodelist',
]


#: The sectioning levels, from the outermost to the innermost.  The index in
#: this tuple is what is called the *level index* of an entry in this module.
sectioning_levels = (
    'part',
    'chapter',
    'section',
    'subsection',
    'subsubsection',
    'paragraph',
    'subparagraph',
)

_level_index = dict([
    (level_name, index)
    for index, level_name in enumerate(sectioning_levels)
])

#: The sectioning macros that are recognized by default, and the level each of
#: them belongs to.  A caller may pass a different mapping to
#: :py:func:`latex_outline()` or :py:func:`outline_for_nodelist()` to recognize
#: additional sectioning commands, or to map a command to another level.
default_sectioning_macros = dict([
    (level_name, level_name)
    for level_name in sectioning_levels
])

# Document classes in which ``\chapter`` is a sectioning level.  The presence
# of an actual ``\chapter`` command in the document is also enough to select
# this style, which covers documents using a class that we do not know.
_chapter_classes = frozenset([
    'book', 'report', 'memoir', 'scrbook', 'scrreprt', 'amsbook', 'amsproc',
])

# Default values of LaTeX's ``secnumdepth`` counter in the standard classes.
# The value is expressed in LaTeX's own level numbering, where 3 numbers
# subsubsections in the article class and 2 numbers subsections in the book and
# report classes.
_default_secnumdepth = {
    'article': 3,
    'book': 2,
    'report': 2,
}


class OutlineEntry(object):
    r"""
    One entry of a LaTeX document outline, corresponding to a single
    sectioning macro call.

    Entries are created by :py:func:`latex_outline()` and
    :py:func:`outline_for_nodelist()`; there is normally no reason to
    instantiate this class directly.

    .. py:attribute:: macroname

       The sectioning macro that produced this entry, without the leading
       backslash, e.g. ``'section'``.

    .. py:attribute:: level

       The level name, e.g. ``'section'``.  This is the macro name unless a
       custom mapping was supplied (see `sectioning_macros=`).

    .. py:attribute:: level_index

       The numeric level, i.e. the index of :py:attr:`level` in
       :py:data:`sectioning_levels`.  ``\part`` is 0, ``\chapter`` is 1, and so
       on.

    .. py:attribute:: title

       The title to show in the table of contents, as plain text.  This is the
       short title if the macro had one (``\section[Short]{Long}``), and the
       full title otherwise.

    .. py:attribute:: short_title

       The short title given as an optional argument, or `None` if there was
       none.  This is the title that LaTeX puts in the table of contents.

    .. py:attribute:: full_title

       The mandatory title argument, regardless of whether a short title was
       given.

    .. py:attribute:: starred

       Whether the sectioning macro carried an optional star.  A starred entry
       is included in the outline but is not numbered.  It neither advances nor
       resets the section counters.

    .. py:attribute:: number

       The display number as a string, e.g. ``'1'``, ``'1.2'`` or ``'A.1'``, or
       `None` if the entry is unnumbered.  This number is **not** a unique key:
       counter resets can make several entries carry the same number, and
       starred entries have none at all.  Use :py:attr:`key` or
       :py:attr:`index` to identify an entry.

    .. py:attribute:: number_parts

       The counter values that make up :py:attr:`number`, as a tuple of
       integers, or `None` if the entry is unnumbered.  In a document with
       chapters, a subsection numbered ``1.2.3`` has ``(1, 2, 3)``.

    .. py:attribute:: key

       A structural key that identifies this entry independently of its
       displayed number.  It is a tuple of ``(level_name, ordinal)`` pairs,
       one per ancestor level, where `ordinal` counts the headings of that
       level among the children of the same parent, starting at one::

           (('chapter', 1), ('section', 2), ('subsection', 1))

       The key is unaffected by counter resets and by duplicate titles.

    .. py:attribute:: ordinal

       The position of this entry among the entries of the same level below
       the same parent, starting at one.

    .. py:attribute:: index

       The position of this entry in the flat list of all outline entries,
       starting at zero.  This is also a valid unique key for the outline.

    .. py:attribute:: parent

       The nearest enclosing :py:class:`OutlineEntry`, or `None` for a
       top-level entry.  This is the nearest heading of a higher level, even
       when some intermediate levels are missing; see
       :py:attr:`missing_parent_levels`.

    .. py:attribute:: parent_key

       The :py:attr:`key` of the parent entry, or `None` for a top-level
       entry.  This makes the structure easy to store and rebuild without
       object references.

    .. py:attribute:: children

       The entries directly below this one, in document order.

    .. py:attribute:: missing_parent_levels

       A tuple with the names of the levels that are missing between the
       nearest existing ancestor and this entry.  For instance, a
       ``\subsubsection`` that follows a ``\chapter`` directly has
       ``('section', 'subsection')`` here.  The tuple is empty when the
       hierarchy is complete.  This is how "level jumps" are reported.

    .. py:attribute:: missing_parent_level

       The first item of :py:attr:`missing_parent_levels`, i.e. the closest
       missing parent level, or `None` when no level is missing.

    .. py:attribute:: heading_pos

       The position of the sectioning macro in the LaTeX source.

    .. py:attribute:: heading_pos_end

       The position immediately after the sectioning macro (including all of
       its arguments).

    .. py:attribute:: body_start

       The position where the body of this section starts.  This is normally
       the same as :py:attr:`heading_pos_end`.

    .. py:attribute:: body_end

       The position where the body of this section ends.  The extent runs up
       to the next sectioning command of the same or a higher level, so it
       includes the bodies of all subsections.  It does not extend past the
       end of the ``document`` environment when there is one.

    .. py:attribute:: own_body_end

       The position where the text that belongs to this section itself ends,
       i.e. where the first subsection starts.  This is equal to
       :py:attr:`body_end` when the section has no subsections.  The ranges
       ``body_start:own_body_end`` and ``body_start:body_end`` are thus the
       section without its subsections and with them, respectively.

    .. py:attribute:: lineno

       The line number of :py:attr:`heading_pos`, starting at one, or `None`
       if no line information is available.

    .. py:attribute:: colno

       The column number of :py:attr:`heading_pos`, starting at zero, or
       `None` if no line information is available.
    """

    def __init__(self, macroname, level, title, short_title, full_title,
                 starred, number, number_parts, key, ordinal, index, parent,
                 missing_parent_levels, heading_pos, heading_pos_end,
                 lineno=None, colno=None):
        self.macroname = macroname
        self.level = level
        self.level_index = _level_index[level]
        self.title = title
        self.short_title = short_title
        self.full_title = full_title
        self.starred = starred
        self.number = number
        self.number_parts = number_parts
        self.key = key
        self.ordinal = ordinal
        self.index = index
        self.parent = parent
        self.children = []
        self.missing_parent_levels = missing_parent_levels
        self.heading_pos = heading_pos
        self.heading_pos_end = heading_pos_end
        self.body_start = heading_pos_end
        self.body_end = None
        self.own_body_end = None
        self.lineno = lineno
        self.colno = colno

    @property
    def parent_key(self):
        if self.parent is None:
            return None
        return self.parent.key

    @property
    def missing_parent_level(self):
        r"""
        The closest missing parent level, or `None` if the hierarchy is
        complete.  See :py:attr:`missing_parent_levels`.
        """
        if self.missing_parent_levels:
            return self.missing_parent_levels[0]
        return None

    @property
    def is_numbered(self):
        r"""
        Whether this entry carries a display number.
        """
        return self.number is not None

    @property
    def depth(self):
        r"""
        The depth of this entry in the outline tree, starting at zero for a
        top-level entry.  This is based on the actual parent chain, and not on
        the entry's level, so a ``\subsubsection`` that appears directly under
        a ``\chapter`` has depth one and reports the missing levels in
        :py:attr:`missing_parent_levels`.
        """
        if self.parent is None:
            return 0
        return self.parent.depth + 1

    def __repr__(self):
        return ('OutlineEntry(macroname={!r}, key={!r}, number={!r}, title={!r})'
                .format(self.macroname, self.key, self.number, self.title))


class LatexOutline(object):
    r"""
    A hierarchical outline of a LaTeX document.

    .. py:attribute:: entries

       All entries in document order, as a flat list of
       :py:class:`OutlineEntry` objects.  Each entry also appears exactly once
       in its parent's :py:attr:`OutlineEntry.children` list.

    .. py:attribute:: roots

       The top-level entries, in document order.  These are the entries whose
       :py:attr:`OutlineEntry.parent` is `None`.  For a well-formed document
       with chapters this is normally the list of chapters, but a level jump
       can put a deeper entry at the top.

    .. py:attribute:: source

       The LaTeX source string, or `None` if the outline was built from a node
       tree without keeping the source.

    .. py:attribute:: chapter_style

       Whether the document uses chapters.  This determines whether a section
       is numbered ``1`` or ``1.1`` (see :py:func:`latex_outline()`).

    .. py:attribute:: secnumdepth

       The value of LaTeX's ``secnumdepth`` counter that applies at the end of
       the document.  This is informational; the numbering of each entry has
       already been computed.
    """

    def __init__(self, entries, source=None, chapter_style=False,
                 secnumdepth=None):
        self.entries = list(entries)
        self.source = source
        self.chapter_style = chapter_style
        self.secnumdepth = secnumdepth
        self.roots = [e for e in self.entries if e.parent is None]

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, index):
        return self.entries[index]

    def get(self, key):
        r"""
        Return the entry whose :py:attr:`OutlineEntry.key` is `key`, or raise
        :py:exc:`KeyError`.
        """
        for entry in self.entries:
            if entry.key == key:
                return entry
        raise KeyError(key)

    def find(self, title=None, macroname=None, number=None, key=None):
        r"""
        Return the list of entries matching all of the given criteria.

        All arguments are optional.  `title` is compared against
        :py:attr:`OutlineEntry.title`; `macroname` against
        :py:attr:`OutlineEntry.macroname`; `number` against
        :py:attr:`OutlineEntry.number`; and `key` against
        :py:attr:`OutlineEntry.key`.  The result is a list because duplicate
        titles are allowed and each occurrence is a separate entry.  An empty
        list means that nothing matched.
        """
        result = []
        for entry in self.entries:
            if title is not None and entry.title != title:
                continue
            if macroname is not None and entry.macroname != macroname:
                continue
            if number is not None and entry.number != number:
                continue
            if key is not None and entry.key != key:
                continue
            result.append(entry)
        return result

    def entry_at(self, pos):
        r"""
        Return the deepest entry whose body contains the source position
        `pos`, or `None` if the position lies outside every section body.
        """
        best = None
        for entry in self.entries:
            if entry.body_start is None or entry.body_end is None:
                continue
            if entry.body_start <= pos < entry.body_end:
                if best is None or entry.level_index > best.level_index:
                    best = entry
        return best

    def to_dict(self):
        r"""
        Return the outline as a JSON-friendly dictionary.

        The dictionary has an ``'entries'`` key with the flat list of entries,
        each represented by a dictionary with the entry's public fields.  The
        ``parent_key`` field is the parent's key (or `None`), so that the
        structure can be rebuilt without recursion.  ``'source_length'`` is
        the length of the source, which lets a consumer check the body ranges
        without carrying the source along; it is `None` when the source is not
        available.
        """
        return {
            'source_length': (None if self.source is None else len(self.source)),
            'chapter_style': self.chapter_style,
            'secnumdepth': self.secnumdepth,
            'entries': [
                {
                    'index': e.index,
                    'macroname': e.macroname,
                    'level': e.level,
                    'level_index': e.level_index,
                    'title': e.title,
                    'short_title': e.short_title,
                    'full_title': e.full_title,
                    'starred': e.starred,
                    'number': e.number,
                    'number_parts': e.number_parts,
                    'key': e.key,
                    'ordinal': e.ordinal,
                    'parent_key': (None if e.parent is None else e.parent.key),
                    'missing_parent_levels': e.missing_parent_levels,
                    'heading_pos': e.heading_pos,
                    'heading_pos_end': e.heading_pos_end,
                    'body_start': e.body_start,
                    'body_end': e.body_end,
                    'own_body_end': e.own_body_end,
                    'lineno': e.lineno,
                    'colno': e.colno,
                }
                for e in self.entries
            ],
        }

    def format(self, include_ranges=True, include_levels=True,
               indent='  '):
        r"""
        Return a human-readable, indented rendering of the outline.

        By default each line shows the level, the display number (if any), the
        table-of-contents title, the body range, and a note about missing
        parent levels.  For example::

            CHAPTER 1 Introduction  [body 22..53]
              SECTION 1.1 Background  [body 48..53]

        Pass `include_ranges=False` or `include_levels=False` to leave those
        parts out, and `indent` to choose the indentation string.
        """
        lines = []
        for entry in self.entries:
            parts = []
            if include_levels:
                parts.append(entry.level.upper() + ('*' if entry.starred else ''))
            if entry.number is not None:
                parts.append(entry.number)
            parts.append(entry.title)
            line = indent * entry.depth + ' '.join(parts)
            notes = []
            if include_ranges:
                notes.append('body {}..{}'.format(entry.body_start, entry.body_end))
            if entry.missing_parent_levels:
                notes.append('missing parent{}: {}'.format(
                    '' if len(entry.missing_parent_levels) == 1 else 's',
                    ', '.join(entry.missing_parent_levels)
                ))
            if notes:
                line += '  [' + '; '.join(notes) + ']'
            lines.append(line)
        return '\n'.join(lines)


# ------------------------------------------------------------------------------
# Parsing the document structure


def _node_text(node, l2t):
    r"""
    Render a title argument node to normalized plain text.

    Runs of whitespace, including the no-break space that ``~`` stands for, are
    collapsed to a single ordinary space, and leading and trailing whitespace
    is removed.  This is what makes titles copied out of the source comparable
    to titles written in a table of contents.
    """
    if node is None:
        return ''
    if node.isNodeType(LatexGroupNode):
        text = l2t.nodelist_to_text(node.nodelist)
    else:
        text = l2t.node_to_text(node)
    text = str(text)
    return ' '.join(text.split())


def _macro_arguments(node):
    r"""
    Return the argument specification and the argument node list of a macro
    node, as two equally long lists (or two empty lists).
    """
    nodeargd = getattr(node, 'nodeargd', None)
    if nodeargd is None:
        return [], []
    argspec = getattr(nodeargd, 'argspec', None)
    argnlist = getattr(nodeargd, 'argnlist', None)
    if argspec is None or argnlist is None:
        return [], []
    return list(argspec), list(argnlist)


def _sectioning_arguments(node, l2t, argspec=None, argnlist=None):
    r"""
    Extract the star, the short title and the full title from a sectioning
    macro node.

    The default specifications declare the sectioning macros with the argument
    list ``'*[{'``.  We do not rely on the argument positions alone, because a
    custom latex context may declare the same macro with fewer arguments (for
    instance without the optional short title).  The mandatory title is the
    last argument of type ``'{'``, and the short title is the last argument of
    type ``'['`` before it.

    The `argspec` and `argnlist` arguments may be given explicitly, for a macro
    that the parser left without arguments but whose arguments follow it in
    the source.
    """
    if argspec is None or argnlist is None:
        argspec, argnlist = _macro_arguments(node)

    star_index = None
    short_index = None
    title_index = None

    for index, argtype in enumerate(argspec):
        if argtype == '*':
            star_index = index
        elif argtype == '[':
            short_index = index
        elif argtype == '{':
            title_index = index

    starred = False
    if star_index is not None and star_index < len(argnlist):
        star_node = argnlist[star_index]
        if star_node is not None and getattr(star_node, 'chars', None) == '*':
            starred = True

    short_title = None
    if short_index is not None and short_index < len(argnlist):
        short_node = argnlist[short_index]
        if short_node is not None:
            short_title = _node_text(short_node, l2t)

    full_title = ''
    if title_index is not None and title_index < len(argnlist):
        full_title = _node_text(argnlist[title_index], l2t)
    elif argnlist:
        # A custom context may declare the macro without a standard argument
        # specification; fall back onto the last argument, which is where the
        # title lives in all the usual declarations.
        full_title = _node_text(argnlist[-1], l2t)

    title = full_title
    if short_title:
        title = short_title

    return starred, short_title, full_title, title


def _lookahead_arguments(nodes, index, kind):
    r"""
    Recover the arguments of a macro that the parser did not attach any
    arguments to, by looking at the nodes that immediately follow it.

    A macro is parsed without arguments when the latex context does not declare
    it, or declares it without arguments.  That is the normal situation for a
    custom sectioning command, and for the standard ``\addtocounter`` and
    ``\stepcounter`` macros, which the default context does not know.  Their
    arguments are then ordinary group nodes that follow the macro in the node
    stream; this function collects the ones that are adjacent to the macro and
    that fit the expected shape.

    `kind` is ``'sectioning'`` (at most a star, an optional short title and a
    mandatory title, in that order) or ``'counter'`` (at most two mandatory
    arguments).  The return value is `(argspec, argnlist, pos_end)`, where
    `pos_end` is the end of the last consumed node (or the macro's own end when
    nothing was consumed).
    """
    argspec = []
    argnlist = []

    if index >= len(nodes):
        return argspec, argnlist, None
    pos_end = nodes[index].pos_end

    # the maximum number of each argument that this kind of macro accepts
    if kind == 'sectioning':
        limits = {'*': 1, '[': 1, '{': 1}
        order = ('*', '[', '{')
    elif kind == 'counter':
        limits = {'*': 0, '[': 0, '{': 2}
        order = ('{',)
    else:
        return argspec, argnlist, pos_end

    counts = {'*': 0, '[': 0, '{': 0}
    order_index = dict([
        (argtype, position)
        for position, argtype in enumerate(order)
    ])
    last_order = -1

    j = index + 1
    while j < len(nodes):
        nxt = nodes[j]
        if nxt is None or nxt.pos is None or nxt.pos != pos_end:
            break

        argtype = None
        if nxt.isNodeType(LatexCharsNode) and nxt.chars == '*':
            argtype = '*'
        elif nxt.isNodeType(LatexGroupNode) and nxt.delimiters[0] == '[':
            argtype = '['
        elif nxt.isNodeType(LatexGroupNode) and nxt.delimiters[0] == '{':
            argtype = '{'

        if argtype is None or counts[argtype] >= limits[argtype]:
            break

        # Arguments have to appear in their declared order; a mandatory title
        # cannot precede an optional short title, for instance.
        if order_index[argtype] < last_order:
            break
        last_order = order_index[argtype]

        counts[argtype] += 1
        argspec.append(argtype)
        argnlist.append(nxt)
        pos_end = nxt.pos_end
        j += 1

    return argspec, argnlist, pos_end


def _documentclass_name(nodelist):
    r"""
    Find the class name in a ``\documentclass`` declaration at the top level of
    `nodelist`, or return `None`.
    """
    for node in nodelist:
        if node is None or not node.isNodeType(LatexMacroNode):
            continue
        if node.macroname != 'documentclass':
            continue
        argspec, argnlist = _macro_arguments(node)
        for index in range(len(argspec) - 1, -1, -1):
            if argspec[index] == '{' and index < len(argnlist):
                text = ''
                argnode = argnlist[index]
                if argnode is not None and argnode.isNodeType(LatexGroupNode):
                    for child in argnode.nodelist:
                        if child is not None and child.isNodeType(LatexCharsNode):
                            text += child.chars
                return text.strip().lower()
    return None


def _walk_nodes(nodelist):
    r"""
    Yield the nodes of `nodelist` in document order, descending into
    environment bodies and group contents.

    Macro arguments are deliberately **not** descended into.  A sectioning
    command that appears in a macro argument is either a reference to an
    existing heading or part of a macro definition, and in neither case does it
    describe a new section of the document.
    """
    for node in nodelist:
        if node is None:
            continue
        yield node
        if node.isNodeType(LatexEnvironmentNode):
            for subnode in _walk_nodes(node.nodelist):
                yield subnode
        # Group contents are not descended into.  A brace group at this level
        # is nearly always a macro argument (a macro definition, a reference,
        # a table cell, ...), and looking inside it would both create entries
        # for sectioning commands that are not part of the document structure
        # and separate a macro from the arguments that follow it.  Environment
        # bodies, on the other hand, are real document content.


def _find_document_environment(nodelist):
    r"""
    Return the ``document`` environment node of the document, or `None`.
    """
    for node in nodelist:
        if node is None:
            continue
        if node.isNodeType(LatexEnvironmentNode) \
           and node.environmentname == 'document':
            return node
    return None


# ------------------------------------------------------------------------------
# Numbering


_roman_numbers = (
    (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
    (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
    (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
)


def _format_roman(value):
    r"""
    Format a positive integer as a Roman numeral.
    """
    if value <= 0:
        return str(value)
    result = ''
    remaining = value
    for number, letters in _roman_numbers:
        while remaining >= number:
            result += letters
            remaining -= number
    return result


def _format_alph(value):
    r"""
    Format a positive integer as a capital letter sequence: 1 is ``A``, 26 is
    ``Z``, 27 is ``AA``, and so on.
    """
    if value <= 0:
        return str(value)
    result = ''
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord('A') + remainder) + result
    return result


def _display_number(level_index, counters, appendix, chapter_style):
    r"""
    Build the display number for a heading at `level_index`, using the current
    counter values.  Return `(number, number_parts)`.

    The parts of the number are the counters from the top numbered level down
    to `level_index`.  In a chapter-style document a section is therefore
    ``chapter.section``, while in an article-style document it is just
    ``section``.  A part is numbered on its own, with Roman numerals.
    """
    if level_index == 0:
        # \part
        return _format_roman(counters['part']), (counters['part'],)

    top = 1 if chapter_style else 2
    if level_index < top:
        return None, None

    parts = []
    for index in range(top, level_index + 1):
        name = sectioning_levels[index]
        value = counters[name]
        if appendix and name == sectioning_levels[top]:
            parts.append(_format_alph(value))
        else:
            parts.append(str(value))

    number_parts = tuple([
        counters[sectioning_levels[index]]
        for index in range(top, level_index + 1)
    ])
    return '.'.join(parts), number_parts


class _NumberingState(object):
    r"""
    The section counters and related state while the outline is built.
    """

    def __init__(self, chapter_style, documentclass=None):
        self.chapter_style = chapter_style
        self.counters = dict([
            (name, 0)
            for name in sectioning_levels
        ])
        self.appendix = False
        self.secnumdepth = _default_secnumdepth.get(documentclass, None)
        if self.secnumdepth is None:
            self.secnumdepth = 2 if chapter_style else 3

    def numbered_cutoff(self):
        r"""
        The largest level index that is numbered under the current
        ``secnumdepth``.  The level indices of this module are shifted by one
        with respect to LaTeX's own numbering (``\part`` is 0 here and
        ``\chapter`` is 1), hence the ``+ 1``.
        """
        return self.secnumdepth + 1

    def is_numbered(self, level_index):
        if level_index == 0:
            # \part sits at LaTeX level -1, above all the others
            return self.secnumdepth >= -1
        return level_index <= self.numbered_cutoff()

    def step(self, level_index):
        r"""
        Advance the counter of the given level and reset all deeper counters,
        as LaTeX does when it encounters a sectioning command.

        ``\part`` is the exception: in the standard classes it advances its own
        counter but leaves the chapter and section counters alone, so chapters
        keep running across parts.
        """
        name = sectioning_levels[level_index]
        self.counters[name] += 1
        if level_index == 0:
            return
        for deeper_index in range(level_index + 1, len(sectioning_levels)):
            self.counters[sectioning_levels[deeper_index]] = 0

    def set_counter(self, name, value):
        if name in self.counters:
            self.counters[name] = value

    def add_counter(self, name, value):
        if name in self.counters:
            self.counters[name] += value

    def step_counter(self, name):
        if name not in self.counters:
            return
        index = _level_index[name]
        self.counters[name] += 1
        for deeper_index in range(index + 1, len(sectioning_levels)):
            self.counters[sectioning_levels[deeper_index]] = 0

    def set_appendix(self):
        r"""
        Apply ``\appendix``: reset the counters of the top numbered level and
        of everything below it, and switch that top level to letters.
        """
        top = 1 if self.chapter_style else 2
        for index in range(top, len(sectioning_levels)):
            self.counters[sectioning_levels[index]] = 0
        self.appendix = True


def _parse_int(text):
    r"""
    Parse an integer argument of ``\setcounter`` or ``\addtocounter``.
    """
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _nth_char_argument(node, n, l2t):
    r"""
    Return the rendered text of the `n`-th mandatory (``'{'``) argument of the
    macro node `node`, counting from zero, or `''` if it is absent.

    The mandatory arguments are selected by their declared argument type and
    not by their position, so that a leading optional argument in square
    brackets (as in ``\setcounter``) does not shift them.
    """
    argspec, argnlist = _macro_arguments(node)
    seen = 0
    for index, argtype in enumerate(argspec):
        if argtype != '{' or index >= len(argnlist):
            continue
        if seen == n:
            return _node_text(argnlist[index], l2t)
        seen += 1
    return ''


def _counter_arguments(node, l2t):
    r"""
    Return the counter name and the integer value of a counter-manipulation
    macro, or `(None, None)`.
    """
    name = _nth_char_argument(node, 0, l2t)
    value_text = _nth_char_argument(node, 1, l2t)
    return name, _parse_int(value_text)


class _MacroWithArguments(object):
    r"""
    A macro node together with an argument list recovered by lookahead.

    Only the attributes that the argument-rendering helpers need are provided.
    """

    def __init__(self, node, argspec, argnlist):
        self.macroname = node.macroname
        self.nodeargd = _ParsedArgumentsProxy(argspec, argnlist)


class _ParsedArgumentsProxy(object):
    r"""
    Minimal stand-in for :py:class:`~pylatexenc.latexnodes.ParsedArguments`
    holding an argument specification and an argument node list.
    """

    def __init__(self, argspec, argnlist):
        self.argspec = argspec
        self.argnlist = argnlist


def _handle_counter_macro(node, numbering, l2t, nodes=None, index=None):
    r"""
    Update `numbering` for the counter-related macro `node`, if it is one.

    When `nodes` and `index` are given, the arguments of a counter macro that
    the parser did not attach are recovered from the adjacent group nodes.
    """
    macroname = node.macroname

    if nodes is not None and index is not None:
        argspec, argnlist = _macro_arguments(node)
        if not argnlist and macroname in (
                'setcounter', 'addtocounter', 'addcounter',
                'stepcounter', 'refstepcounter'):
            argspec, argnlist, _ = _lookahead_arguments(nodes, index, 'counter')
            node = _MacroWithArguments(node, argspec, argnlist)

    if macroname == 'setcounter':
        name, value = _counter_arguments(node, l2t)
        if name == 'secnumdepth' and value is not None:
            numbering.secnumdepth = value
        elif name is not None and value is not None:
            numbering.set_counter(name, value)
    elif macroname in ('addtocounter', 'addcounter'):
        name, value = _counter_arguments(node, l2t)
        if name == 'secnumdepth' and value is not None:
            numbering.secnumdepth += value
        elif name is not None and value is not None:
            numbering.add_counter(name, value)
    elif macroname in ('stepcounter', 'refstepcounter'):
        name = _nth_char_argument(node, 0, l2t)
        if name == 'secnumdepth':
            numbering.secnumdepth += 1
        elif name:
            numbering.step_counter(name)
    elif macroname == 'appendix':
        numbering.set_appendix()


# ------------------------------------------------------------------------------
# Building the outline


class _NodeListAdapter(object):
    r"""
    Minimal stand-in for a :py:class:`~pylatexenc.latexnodes.nodes.LatexNodeList`
    around a plain python list of nodes, used when a caller passes a bare list.
    """

    def __init__(self, nodes, pos=None, pos_end=None):
        self.nodelist = nodes
        self.pos = pos
        self.pos_end = pos_end

    def __iter__(self):
        return iter(self.nodelist)

    def __len__(self):
        return len(self.nodelist)

    def __getitem__(self, index):
        return self.nodelist[index]



def _build_outline(nodelist, *, source=None, latex_walker=None,
                   sectioning_macros=None, text_converter=None):
    r"""
    Common implementation of :py:func:`outline_for_nodelist()` and
    :py:func:`latex_outline()`.
    """
    if nodelist is None:
        nodelist = []

    if not hasattr(nodelist, 'pos'):
        # An ordinary python list of nodes, without the position information
        # that a LatexNodeList carries.  Derive the span from the nodes.
        nodes = [node for node in nodelist if node is not None]
        positions = [node.pos for node in nodes if node.pos is not None]
        positions_end = [
            node.pos_end for node in nodes if node.pos_end is not None
        ]
        nodelist = _NodeListAdapter(
            nodes,
            pos=(min(positions) if positions else None),
            pos_end=(max(positions_end) if positions_end else None),
        )

    if sectioning_macros is None:
        sectioning_macros = default_sectioning_macros

    macro_levels = {}
    for macroname, level in sectioning_macros.items():
        if level not in _level_index:
            raise ValueError(
                "Unknown sectioning level {!r} for macro {!r}; expected one of {}"
                .format(level, macroname, ', '.join(sectioning_levels))
            )
        macro_levels[macroname] = level

    if text_converter is None:
        # Titles are ordinary running text, not formulas: use the simple math
        # rendering and no unicode alphabets, so that the outline stays
        # readable and comparable across documents.
        text_converter = LatexNodes2Text(math_mode='text',
                                         math_fontstyle=None,
                                         text_fontstyle=False)
    l2t = text_converter

    doc_env = _find_document_environment(nodelist)

    if doc_env is not None:
        body_start = doc_env.nodelist.pos
        body_end = doc_env.nodelist.pos_end
        if body_start is None:
            body_start = doc_env.pos
        if body_end is None:
            body_end = doc_env.pos_end
    else:
        body_start = nodelist.pos
        body_end = nodelist.pos_end
        if body_start is None:
            body_start = 0
        if body_end is None:
            body_end = len(source) if source is not None else 0

    documentclass = _documentclass_name(nodelist)
    chapter_style = documentclass in _chapter_classes

    all_nodes = list(_walk_nodes(nodelist))

    # A \chapter anywhere in the document body selects the chapter style, even
    # if the document class is one that we do not recognize.  (A \chapter in
    # the preamble, or in a macro definition, does not.)
    for node_index, node in enumerate(all_nodes):
        if not node.isNodeType(LatexMacroNode):
            continue
        if node.macroname not in macro_levels:
            continue
        if macro_levels[node.macroname] != 'chapter':
            continue
        if node.pos is not None and body_start is not None \
           and node.pos < body_start:
            continue
        if node.pos is not None and body_end is not None \
           and node.pos >= body_end:
            continue
        chapter_style = True
        break

    numbering = _NumberingState(chapter_style, documentclass=documentclass)

    entries = []
    stack = []
    child_ordinals = {}

    for node_index, node in enumerate(all_nodes):
        if not node.isNodeType(LatexMacroNode):
            continue

        # Nothing in the preamble (if there is a document environment) or after
        # \end{document} belongs to the outline, including counter changes.
        if body_start is not None and node.pos is not None \
           and node.pos < body_start:
            continue
        if body_end is not None and node.pos is not None \
           and node.pos >= body_end:
            continue

        if node.macroname not in macro_levels:
            _handle_counter_macro(node, numbering, l2t,
                                  nodes=all_nodes, index=node_index)
            continue

        level = macro_levels[node.macroname]
        level_index = _level_index[level]

        argspec, argnlist = _macro_arguments(node)
        heading_pos_end = node.pos_end
        if not argnlist:
            # The macro was parsed without arguments, as can happen for a
            # custom sectioning command that the latex context does not know.
            # Its arguments are then the adjacent group nodes that follow it.
            argspec, argnlist, lookahead_end = _lookahead_arguments(
                all_nodes, node_index, 'sectioning'
            )
            if lookahead_end is not None:
                heading_pos_end = lookahead_end

        starred, short_title, full_title, title = \
            _sectioning_arguments(node, l2t, argspec=argspec, argnlist=argnlist)

        # Compute the display number for numbered headings.  A starred heading
        # neither advances nor resets any counter.
        if starred:
            number = None
            number_parts = None
        elif numbering.is_numbered(level_index):
            # LaTeX skips the whole counter step for a heading that lies below
            # ``secnumdepth``: it neither prints a number nor advances (or
            # resets) the counters.  This matters for a later ``\setcounter``
            # raising the depth again.
            numbering.step(level_index)
            number, number_parts = _display_number(
                level_index, numbering.counters, numbering.appendix,
                numbering.chapter_style
            )
        else:
            number = None
            number_parts = None

        # Keep the nearest ancestor of a strictly higher level.  This is the
        # actual tree parent, even when intermediate levels are missing.
        while stack and stack[-1].level_index >= level_index:
            stack.pop()
        parent = stack[-1] if stack else None

        # Report which expected parents are absent.  A part is optional, and
        # so is a chapter in an article-style document; from the top level down,
        # every level is required.
        required_base = 1 if chapter_style else 2
        missing_parent_levels = ()
        if level_index > required_base:
            present_levels = set([
                entry.level_index
                for entry in stack
                if entry.level_index >= required_base
            ])
            missing = []
            for expected_index in range(required_base, level_index):
                if expected_index not in present_levels:
                    missing.append(sectioning_levels[expected_index])
            missing_parent_levels = tuple(missing)

        # The structural key uses ordinals among the children of the same
        # parent and of the same level.  It does not depend on the displayed
        # number, so counter resets and duplicate titles cannot collide.
        parent_key = parent.key if parent is not None else ()
        ordinal_key = (parent_key, level)
        ordinal = child_ordinals.get(ordinal_key, 0) + 1
        child_ordinals[ordinal_key] = ordinal
        key = parent_key + ((level, ordinal),)

        lineno = None
        colno = None
        if latex_walker is not None and node.pos is not None:
            try:
                lineno, colno = latex_walker.pos_to_lineno_colno(node.pos)
            except Exception:
                lineno = None
                colno = None

        entry = OutlineEntry(
            macroname=node.macroname,
            level=level,
            title=title,
            short_title=short_title,
            full_title=full_title,
            starred=starred,
            number=number,
            number_parts=number_parts,
            key=key,
            ordinal=ordinal,
            index=len(entries),
            parent=parent,
            missing_parent_levels=missing_parent_levels,
            heading_pos=node.pos,
            heading_pos_end=heading_pos_end,
            lineno=lineno,
            colno=colno,
        )
        entry.missing_parent_levels = missing_parent_levels

        if parent is not None:
            parent.children.append(entry)
            if parent.own_body_end is None:
                parent.own_body_end = node.pos
        entries.append(entry)
        stack.append(entry)

    # Body ranges: an entry's body runs up to the next heading of the same or
    # a higher level, and the last entries run up to the end of the document
    # body.  own_body_end marks where its first child starts.
    range_stack = []
    for entry in entries:
        while range_stack and range_stack[-1].level_index >= entry.level_index:
            finished = range_stack.pop()
            finished.body_end = entry.heading_pos
            if finished.own_body_end is None:
                finished.own_body_end = finished.body_end
        range_stack.append(entry)
    for entry in range_stack:
        entry.body_end = body_end
        if entry.own_body_end is None:
            entry.own_body_end = body_end

    return LatexOutline(entries,
                        source=source,
                        chapter_style=chapter_style,
                        secnumdepth=numbering.secnumdepth)


def outline_for_nodelist(nodelist, *, source=None, latex_walker=None,
                         sectioning_macros=None, text_converter=None):
    r"""
    Build a :py:class:`LatexOutline` from an already parsed node list.

    - `nodelist` is a :py:class:`~pylatexenc.latexnodes.nodes.LatexNodeList`
      (or an ordinary list of nodes) obtained from
      :py:meth:`~pylatexenc.latexwalker.LatexWalker.parse_content()`.

    - `source` is the LaTeX source string; it is stored on the outline and is
      used for the end position of the last body range when the node list does
      not provide one.  When omitted, the source is taken from the node list's
      latex walker if possible.

    - `latex_walker` is the walker that produced `nodelist`.  It is used to
      report line and column numbers.  When omitted, it is taken from the node
      list if possible.

    - `sectioning_macros` is a mapping from macro name to level name, which
      replaces :py:data:`default_sectioning_macros`.  Use this to recognize
      custom sectioning commands, or to map a command to another level.

    - `text_converter` is a :py:class:`pylatexenc.latex2text.LatexNodes2Text`
      instance used to render titles.  By default a converter is created that
      uses the simple text math mode and no unicode font alphabets, so that
      titles come out as readable plain text.

    The outline uses the sectioning macros that appear in the document body.
    Sectioning commands in macro arguments, in ``\ref``/``\nameref`` arguments
    and in ``\addcontentsline`` arguments do not create entries.
    """
    if source is None and nodelist is not None:
        source = getattr(nodelist, 'latex_walker', None)
        if source is not None:
            source = getattr(source, 's', None)

    if latex_walker is None and nodelist is not None:
        latex_walker = getattr(nodelist, 'latex_walker', None)

    return _build_outline(
        nodelist,
        source=source,
        latex_walker=latex_walker,
        sectioning_macros=sectioning_macros,
        text_converter=text_converter,
    )


def latex_outline(latex, *, latex_context=None, sectioning_macros=None,
                  text_converter=None, **parse_flags):
    r"""
    Parse `latex` and return its :py:class:`LatexOutline`.

    The code is parsed with :py:class:`pylatexenc.latexwalker.LatexWalker`
    using the given `latex_context=` and any additional `parse_flags` (such as
    ``tolerant_parsing=False``).  The `sectioning_macros=` and
    `text_converter=` arguments have the same meaning as in
    :py:func:`outline_for_nodelist()`.

    Only the document body is considered when the code contains a
    ``document`` environment.  A ``\documentclass`` declaration is used to
    decide whether chapters exist; an actual ``\chapter`` command is enough on
    its own.  In a chapter-style document a section is numbered
    ``chapter.section``; otherwise it is numbered on its own.

    Starred sectioning commands are included.  They carry a
    :py:attr:`OutlineEntry.title` but no :py:attr:`OutlineEntry.number`, and
    they do not advance the counters.  Short titles given in square brackets
    become the :py:attr:`OutlineEntry.title`, while the full title remains
    available as :py:attr:`OutlineEntry.full_title`.

    Counter changes made with ``\setcounter``, ``\addtocounter``,
    ``\stepcounter``, ``\refstepcounter`` and ``\appendix`` are taken into
    account, including the automatic reset of the deeper counters that occurs
    whenever a sectioning command advances a counter.  The resulting display
    numbers are reported in :py:attr:`OutlineEntry.number`, but entries are
    identified by :py:attr:`OutlineEntry.key`, which is structural and remains
    unique even when counter resets make numbers repeat.
    """
    walker = LatexWalker(latex, latex_context=latex_context, **parse_flags)
    nodelist, _ = walker.parse_content()

    return _build_outline(
        nodelist,
        source=latex,
        latex_walker=walker,
        sectioning_macros=sectioning_macros,
        text_converter=text_converter,
    )
