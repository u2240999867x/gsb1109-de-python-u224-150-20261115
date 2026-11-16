# -*- coding: utf-8 -*-

r"""
Build a hierarchical outline (table of contents) of a LaTeX document.

The outline is derived from the sectioning macros that the LaTeX walker already
knows about.  The ordering of the levels follows the usual LaTeX convention:

    ``\part``, ``\chapter``, ``\section``, ``\subsection``,
    ``\subsubsection``, ``\paragraph``, ``\subparagraph``.

The macros are declared with the standard ``*[{`` argument convention: an
optional star, an optional short title (used for the table of contents), and a
mandatory full title.  A starred section is included in the outline but has no
displayed number.  Counter changes from ``\setcounter``, ``\addtocounter``,
``\stepcounter``, ``\refstepcounter`` and ``\appendix`` are taken into account,
so two sections may legitimately display the same number.  Entries therefore
carry a stable key of their own; the displayed number is never used to identify
an entry.

The body range of an entry starts immediately after its sectioning macro and
ends immediately before the next sectioning macro of the same or of a higher
level, or at the end of the enclosing document/environment.

The main entry point is :py:func:`get_outline`.  It accepts LaTeX source code, a
:py:class:`~pylatexenc.latexwalker.LatexWalker` instance, or an already parsed
node list.  The :py:class:`LatexOutline` result is a list of
:py:class:`LatexSectionEntry` objects in document order, with the tree available
through the ``roots`` attribute and through each entry's ``children`` list.
"""


import logging
logger = logging.getLogger(__name__)


from ..latexnodes.nodes import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexNode,
    LatexNodeList,
    LatexSpecialsNode,
)


__all__ = [
    'SECTIONING_MACROS',
    'DEFAULT_SECTIONING_MACROS',
    'LatexSectionEntry',
    'SectionEntry',
    'LatexOutline',
    'SectionOutline',
    'LatexOutlineBuilder',
    'get_outline',
    'build_outline',
    'get_section_outline',
    'get_section_outlines',
    'get_table_of_contents',
    'make_outline',
    'parse_outline',
]


#: The sectioning macros known to the default LaTeX context, together with
#: their level.  Lower values are higher up in the document hierarchy.
SECTIONING_MACROS = {
    'part': 0,
    'chapter': 1,
    'section': 2,
    'subsection': 3,
    'subsubsection': 4,
    'paragraph': 5,
    'subparagraph': 6,
}

DEFAULT_SECTIONING_MACROS = SECTIONING_MACROS


_SECTIONING_ORDER = (
    'part',
    'chapter',
    'section',
    'subsection',
    'subsubsection',
    'paragraph',
    'subparagraph',
)


_VERBATIM_ENVIRONMENTS = (
    'verbatim',
    'verbatim*',
    'lstlisting',
    'minted',
    'comment',
)


_FORMATTING_MACROS = (
    'emph',
    'textbf',
    'textit',
    'textsl',
    'textsc',
    'textrm',
    'textsf',
    'texttt',
    'textup',
    'textmd',
    'text',
    'mathrm',
    'mathbf',
    'mathit',
    'mathsf',
    'mathtt',
    'mathcal',
    'mathbb',
    'mathfrak',
    'operatorname',
    'mbox',
    'hbox',
    'href',
    'url',
    'texorpdfstring',
)


_ACCENT_MACROS = (
    "'", '"', '`', '^', '~', '=', '.',
    'u', 'v', 'H', 'c', 'd', 'b', 'k', 'r',
)


_SIMPLE_MACRO_TEXT = {
    'LaTeX': 'LaTeX',
    'TeX': 'TeX',
    'ldots': u'\N{HORIZONTAL ELLIPSIS}',
    'dots': u'\N{HORIZONTAL ELLIPSIS}',
    'textellipsis': u'\N{HORIZONTAL ELLIPSIS}',
    'textendash': u'\N{EN DASH}',
    'textemdash': u'\N{EM DASH}',
    'textquotedblleft': u'\N{LEFT DOUBLE QUOTATION MARK}',
    'textquotedblright': u'\N{RIGHT DOUBLE QUOTATION MARK}',
    'textquoteleft': u'\N{LEFT SINGLE QUOTATION MARK}',
    'textquoteright': u'\N{RIGHT SINGLE QUOTATION MARK}',
    'copyright': u'\N{COPYRIGHT SIGN}',
    'textcopyright': u'\N{COPYRIGHT SIGN}',
    'textregistered': u'\N{REGISTERED SIGN}',
    'pounds': u'\N{POUND SIGN}',
    'textsterling': u'\N{POUND SIGN}',
    'euro': u'\N{EURO SIGN}',
    'texteuro': u'\N{EURO SIGN}',
    'S': u'\N{SECTION SIGN}',
    'P': u'\N{PILCROW SIGN}',
    '&': '&',
    '%': '%',
    '$': '$',
    '#': '#',
    '_': '_',
    '{': '{',
    '}': '}',
}


def _node_text(node):
    r"""
    Return a plain-text rendering of a node, suitable for a table-of-contents
    title.  This is deliberately small: it handles character nodes, groups,
    math chunks, common formatting macros and a handful of symbols.  Unknown
    macros are ignored rather than reported as literal LaTeX code.
    """
    if node is None:
        return ''

    if isinstance(node, str):
        return node

    if node.isNodeType(LatexCharsNode):
        return node.chars

    if node.isNodeType(LatexCommentNode):
        return ''

    if node.isNodeType(LatexGroupNode):
        return _nodelist_text(node.nodelist)

    if node.isNodeType(LatexMathNode):
        return _nodelist_text(node.nodelist)

    if node.isNodeType(LatexSpecialsNode):
        chars = node.specials_chars
        if chars == '~' or chars == '\n\n':
            return ' '
        if chars in ('^', '_') and node.nodeargd is not None:
            args = node.nodeargd.argnlist or []
            if args and args[-1] is not None:
                return chars + _node_text(args[-1])
            return chars
        return chars

    if node.isNodeType(LatexEnvironmentNode):
        return _nodelist_text(node.nodelist)

    if node.isNodeType(LatexMacroNode):
        name = node.macroname
        if name in _SIMPLE_MACRO_TEXT:
            return _SIMPLE_MACRO_TEXT[name]
        if name == '\\':
            return ' '
        if name in _FORMATTING_MACROS:
            args = _get_non_none_args(node)
            for arg in reversed(args):
                if arg is not None:
                    return _node_text(arg)
            return ''
        if name in _ACCENT_MACROS:
            args = _get_non_none_args(node)
            if args:
                return _node_text(args[0])
            return ''
        return ''

    return ''


def _nodelist_text(nodelist):
    if nodelist is None:
        return ''
    if isinstance(nodelist, LatexNode):
        return _node_text(nodelist)
    chunks = []
    for node in nodelist:
        chunks.append(_node_text(node))
    return ''.join(chunks)


def _normalize_text(text):
    return ' '.join(text.split())


def _format_title(nodelist, normalize=True):
    text = _nodelist_text(nodelist)
    if normalize:
        return _normalize_text(text)
    return text


def _roman(number):
    if number is None or number <= 0:
        return ''
    values = (
        (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
        (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
        (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
    )
    result = ''
    remaining = number
    for value, numeral in values:
        while remaining >= value:
            result += numeral
            remaining -= value
    return result


def _alph(number):
    if number is None or number <= 0:
        return ''
    result = ''
    remaining = number
    while remaining > 0:
        remaining = remaining - 1
        result = chr(ord('A') + (remaining % 26)) + result
        remaining = remaining // 26
    return result


def _get_non_none_args(node):
    if node.nodeargd is None or node.nodeargd.argnlist is None:
        return []
    return [arg for arg in node.nodeargd.argnlist if arg is not None]


def _get_heading_arguments(node):
    r"""
    Return ``(starred, short_title_nodelist, full_title_nodelist)`` for a
    sectioning macro node, or `None` if the node does not have the expected
    ``*[{`` argument layout.
    """
    if node.nodeargd is None:
        return None
    args = node.nodeargd.argnlist
    if args is None:
        return None

    if len(args) >= 3:
        return (args[0] is not None, args[1], args[2])

    # Be forgiving with a custom (or older) context that declares only the
    # optional short title and the mandatory title.
    if len(args) == 2:
        return (False, args[0], args[1])

    if len(args) == 1:
        return (False, None, args[0])

    return None


def _counter_name_and_value(node):
    r"""
    Return ``(counter_name, value)`` for ``\setcounter`` and
    ``\addtocounter`` nodes, or ``(None, None)``.
    """
    args = _get_non_none_args(node)
    if len(args) < 2:
        return (None, None)
    name = _normalize_text(_node_text(args[-2]))
    value_text = _normalize_text(_node_text(args[-1]))
    if not name or not value_text:
        return (None, None)
    try:
        value = int(value_text.strip())
    except ValueError:
        return (None, None)
    return (name, value)


def _reset_lower_counters(counters, level_name, sectioning_macros):
    level = sectioning_macros.get(level_name)
    if level is None:
        return
    for name, name_level in sectioning_macros.items():
        if name_level > level:
            counters[name] = 0


def _apply_counter_event(node, counters, state, sectioning_macros):
    name = node.macroname

    if name == 'appendix':
        state['appendix'] = True
        for counter_name in counters:
            counters[counter_name] = 0
        return

    if name in ('setcounter', 'addtocounter'):
        counter_name, value = _counter_name_and_value(node)
        if counter_name == 'secnumdepth':
            state['secnumdepth'] = value
            return
        if counter_name == 'tocdepth':
            state['tocdepth'] = value
            return
        if counter_name not in counters:
            return
        if name == 'setcounter':
            counters[counter_name] = value
        else:
            counters[counter_name] = counters[counter_name] + value
        return

    if name in ('stepcounter', 'refstepcounter'):
        args = _get_non_none_args(node)
        if not args:
            return
        counter_name = _normalize_text(_node_text(args[-1]))
        if counter_name not in counters:
            return
        counters[counter_name] = counters[counter_name] + 1
        if counter_name != 'part':
            _reset_lower_counters(counters, counter_name, sectioning_macros)
        return


def _format_heading_number(level_name, counters, state, sectioning_macros):
    if level_name == 'part':
        return _roman(counters.get('part', 0))

    if state.get('appendix'):
        if level_name == 'chapter':
            return _alph(counters.get('chapter', 0))

        ordered = list(_SECTIONING_ORDER)
        try:
            level = ordered.index(level_name)
        except ValueError:
            return ''

        chapter = counters.get('chapter', 0)
        start = 2  # section, or the appendix letter
        if chapter > 0:
            start = 1  # chapter letter

        if level < start:
            start = level

        parts = []
        for name in ordered[start:level + 1]:
            if name == 'chapter':
                parts.append(_alph(counters.get(name, 0)))
            elif name == 'section' and chapter == 0:
                parts.append(_alph(counters.get(name, 0)))
            else:
                parts.append(str(counters.get(name, 0)))
        return '.'.join(parts)

    ordered = list(_SECTIONING_ORDER)
    try:
        level = ordered.index(level_name)
    except ValueError:
        return ''

    # \part is a structural division and does not prefix the chapter/section
    # numbers in the usual classes.
    start = 2  # section
    if counters.get('chapter', 0) > 0:
        start = 1  # chapter

    if level < start:
        start = level

    parts = []
    for name in ordered[start:level + 1]:
        parts.append(str(counters.get(name, 0)))
    return '.'.join(parts)


def _heading_number(level_name, starred, counters, state, sectioning_macros):
    if starred:
        return ''

    # An unstarred section steps its counter even when \secnumdepth prevents the
    # number from being displayed; later sections are numbered as if it had
    # been.  Starred sections, in contrast, leave all counters untouched.
    counters[level_name] = counters.get(level_name, 0) + 1

    # \part is a division of its own; unlike \chapter and \section, it does not
    # reset the numbering of the levels below it.
    if level_name != 'part':
        _reset_lower_counters(counters, level_name, sectioning_macros)
    secnumdepth = state.get('secnumdepth')
    if secnumdepth is not None:
        level = sectioning_macros.get(level_name)
        if level is not None and level > secnumdepth + 1:
            return ''

    return _format_heading_number(level_name, counters, state, sectioning_macros)


class LatexSectionEntry(object):
    r"""
    One entry of a hierarchical LaTeX outline.

    The entry records both the full title and, if present, the short title that
    LaTeX would use in the table of contents.  The ``title`` attribute is the
    title that should be displayed in the outline: the short title when one was
    provided, and the full title otherwise.  The full title is always available
    as ``full_title``.

    A section is identified by ``key`` (also available as ``id``), which is
    derived from the macro name and position in the source.  The displayed
    ``number`` is intentionally *not* used as a key: counter resets and starred
    sections make displayed numbers repeat, and duplicate section titles are
    kept as separate entries.

    .. py:attribute:: body_start

       Position immediately after the sectioning macro.  Together with
       ``body_end`` this describes the section's body range (the heading itself
       is not part of the body).

    .. py:attribute:: body_end

       Position immediately before the next sectioning macro of the same or of
       a higher level, or the end of the enclosing document/environment.

    .. py:attribute:: missing_parent

       `True` when the section skips one or more intermediate levels, for
       instance when a ``\subsubsection`` directly follows a ``\section``.
       ``missing_parent_levels`` then lists the absent levels (here,
       ``['subsection']``).  The entry is still attached to the nearest
       available ancestor, so no synthetic section is invented.

    .. py:attribute:: parent

       The parent entry, or `None` for a top-level entry.

    .. py:attribute:: children

       The list of direct child entries.
    """

    def __init__(self, macroname, level, starred, title, full_title,
                 short_title, number, node,
                 sectioning_macros=None, index=None):
        self.macroname = macroname
        self.level_name = macroname
        self.level = level
        self.depth = None
        self.starred = starred
        self.title = title
        self.full_title = full_title
        self.short_title = short_title
        self.number = number
        self.node = node

        self.heading_start = node.pos
        self.heading_end = node.pos_end
        self.body_start = node.pos_end
        self.body_end = None
        #: End of this section's own content, i.e. up to the next heading of any
        #: level.  ``body_end``, in contrast, includes subsections and ends at
        #: the next heading of the same or a higher level.
        self.own_body_end = None
        self.scope_end = None

        self.parent = None
        self.children = []
        self.missing_parent = False
        self.missing_parent_levels = []

        self.index = index
        self.key = '{}@{}'.format(macroname, node.pos)
        self.id = self.key

        self.latex_walker = getattr(node, 'latex_walker', None)

    @property
    def pos(self):
        return self.heading_start

    @property
    def pos_end(self):
        return self.heading_end

    @property
    def heading_range(self):
        return (self.heading_start, self.heading_end)

    @property
    def body_range(self):
        return (self.body_start, self.body_end)

    @property
    def own_body_range(self):
        return (self.body_start, self.own_body_end)

    @property
    def full_body_range(self):
        return (self.body_start, self.body_end)

    @property
    def full_title_text(self):
        return self.full_title

    @property
    def short_title_text(self):
        return self.short_title

    @property
    def lineno(self):
        if self.latex_walker is None:
            return None
        return self.latex_walker.pos_to_lineno_colno(self.heading_start)[0]

    @property
    def end_lineno(self):
        if self.latex_walker is None or self.body_end is None:
            return None
        return self.latex_walker.pos_to_lineno_colno(self.body_end)[0]

    @property
    def heading_latex(self):
        if self.latex_walker is None:
            return None
        return self.latex_walker.s[self.heading_start:self.heading_end]

    @property
    def body_latex(self):
        if self.latex_walker is None or self.body_end is None:
            return None
        return self.latex_walker.s[self.body_start:self.body_end]

    @property
    def own_body_latex(self):
        if self.latex_walker is None or self.own_body_end is None:
            return None
        return self.latex_walker.s[self.body_start:self.own_body_end]

    @property
    def toc_title(self):
        return self.title

    @property
    def display_title(self):
        return self.title

    @property
    def display_number(self):
        return self.number

    @property
    def is_starred(self):
        return self.starred

    @property
    def parent_name(self):
        if self.parent is None:
            return None
        return self.parent.level_name

    @property
    def missing_parent_level(self):
        if not self.missing_parent_levels:
            return None
        return self.missing_parent_levels[-1]

    def to_dict(self, include_children=True):
        result = {
            'key': self.key,
            'id': self.id,
            'index': self.index,
            'level': self.level,
            'level_name': self.level_name,
            'depth': self.depth,
            'title': self.title,
            'full_title': self.full_title,
            'short_title': self.short_title,
            'number': self.number,
            'starred': self.starred,
            'heading_start': self.heading_start,
            'heading_end': self.heading_end,
            'body_start': self.body_start,
            'body_end': self.body_end,
            'own_body_end': self.own_body_end,
            'lineno': self.lineno,
            'end_lineno': self.end_lineno,
            'missing_parent': self.missing_parent,
            'missing_parent_levels': list(self.missing_parent_levels),
            'parent': self.parent.key if self.parent is not None else None,
        }
        if include_children:
            result['children'] = [
                child.to_dict(include_children=True) for child in self.children
            ]
        return result
    def __getitem__(self, key):
        if key == 'body_range':
            return self.body_range
        if key == 'heading_range':
            return self.heading_range
        if key == 'toc_title':
            return self.toc_title
        if key == 'display_number':
            return self.display_number
        if key == 'is_starred':
            return self.is_starred
        return getattr(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except (AttributeError, KeyError, TypeError):
            return default

    def keys(self):
        return self.to_dict(include_children=False).keys()

    def __repr__(self):
        return (
            'LatexSectionEntry(level_name={!r}, number={!r}, title={!r})'
            .format(self.level_name, self.number, self.title)
        )


SectionEntry = LatexSectionEntry


class LatexOutline(list):
    r"""
    A hierarchical outline of a LaTeX document.

    The object is a list of :py:class:`LatexSectionEntry` objects in document
    order.  The top-level entries are available as ``roots``; every entry also
    has a ``children`` list.  ``sections`` and ``entries`` are aliases for the
    flat list itself.

    ``document_start`` and ``document_end`` delimit the part of the source that
    was considered (normally the body of the ``document`` environment, or the
    whole source if there is no such environment).
    """

    def __init__(self, entries, walker=None,
                 document_start=None, document_end=None):
        super(LatexOutline, self).__init__(entries)
        self.walker = walker
        self.document_start = document_start
        self.document_end = document_end

    @property
    def entries(self):
        return self

    @property
    def sections(self):
        return self

    @property
    def flat(self):
        return list(self)

    @property
    def roots(self):
        return [entry for entry in self if entry.parent is None]

    @property
    def top_level(self):
        return self.roots

    def walk(self, include_roots=True):
        r"""
        Yield all entries depth-first.  With `include_roots=True` (the default)
        the top-level entries are yielded too; otherwise only their descendants
        are produced.
        """
        def walk_entry(entry):
            for child in entry.children:
                yield child
                for subchild in walk_entry(child):
                    yield subchild

        for root in self.roots:
            if include_roots:
                yield root
            for descendant in walk_entry(root):
                yield descendant

    def to_dict(self):
        return {
            'document_start': self.document_start,
            'document_end': self.document_end,
            'sections': [entry.to_dict(include_children=True)
                         for entry in self.roots],
        }

    def to_json(self, **kwargs):
        import json
        return json.dumps(self.to_dict(), **kwargs)

    def keys(self):
        return ('document_start', 'document_end', 'sections')

    def __getitem__(self, item):
        if isinstance(item, str):
            if item == 'sections':
                return self
            if item == 'entries':
                return self
            return getattr(self, item)
        return super(LatexOutline, self).__getitem__(item)

    def __repr__(self):
        return 'LatexOutline({} entries)'.format(len(self))


SectionOutline = LatexOutline


class LatexOutlineBuilder(object):
    r"""
    Small convenience wrapper around :py:func:`get_outline`.

    It holds the options once and can then build outlines for several sources::

        builder = LatexOutlineBuilder(include_starred=False)
        outline = builder.build(latex_source)

    The builder is stateless; calling :py:meth:`build` repeatedly does not carry
    counters or hierarchy over from one document to the next.
    """

    def __init__(self, sectioning_macros=None, include_starred=True,
                 use_short_titles=True, latex_context=None,
                 respect_tocdepth=False, **walker_kwargs):
        self.sectioning_macros = sectioning_macros
        self.include_starred = include_starred
        self.use_short_titles = use_short_titles
        self.latex_context = latex_context
        self.respect_tocdepth = respect_tocdepth
        self.walker_kwargs = walker_kwargs

    def build(self, source):
        return get_outline(
            source,
            sectioning_macros=self.sectioning_macros,
            include_starred=self.include_starred,
            use_short_titles=self.use_short_titles,
            latex_context=self.latex_context,
            respect_tocdepth=self.respect_tocdepth,
            **self.walker_kwargs
        )

    def __call__(self, source):
        return self.build(source)


def _find_document_body(nodelist):
    r"""
    Return ``(body_nodelist, scope_end, document_start, document_end)`` for the
    content of the first ``document`` environment, or the given node list if
    there is no such environment.
    """
    for node in nodelist:
        if node is None:
            continue
        if node.isNodeType(LatexEnvironmentNode) \
           and node.environmentname == 'document' \
           and node.nodelist is not None:
            body = node.nodelist
            end = body.pos_end
            if end is None:
                end = node.pos_end
            return (body, end, node.pos, node.pos_end)

    end = getattr(nodelist, 'pos_end', None)
    return (nodelist, end, 0, end)


def _make_entry(node, macroname, level, starred, short_title, full_title,
                counters, state, sectioning_macros, index,
                use_short_titles):
    full_title_text = _format_title(full_title)
    short_title_text = None
    if short_title is not None:
        short_title_text = _format_title(short_title)

    if use_short_titles and short_title_text:
        title = short_title_text
    else:
        title = full_title_text

    number = _heading_number(
        macroname, starred, counters, state, sectioning_macros
    )

    return LatexSectionEntry(
        macroname=macroname,
        level=level,
        starred=starred,
        title=title,
        full_title=full_title_text,
        short_title=short_title_text,
        number=number,
        node=node,
        sectioning_macros=sectioning_macros,
        index=index,
    )


def _collect_entries(nodelist, scope_end, walker, sectioning_macros,
                     counters, state, entries, include_starred,
                     use_short_titles):
    if nodelist is None:
        return

    for node in nodelist:
        if node is None:
            continue

        if node.isNodeType(LatexMacroNode):
            name = node.macroname

            if name in sectioning_macros:
                heading = _get_heading_arguments(node)
                if heading is None:
                    continue

                starred, short_title, full_title = heading
                if full_title is None:
                    continue
                if starred and not include_starred:
                    continue

                entry = _make_entry(
                    node=node,
                    macroname=name,
                    level=sectioning_macros[name],
                    starred=starred,
                    short_title=short_title,
                    full_title=full_title,
                    counters=counters,
                    state=state,
                    sectioning_macros=sectioning_macros,
                    index=len(entries),
                    use_short_titles=use_short_titles,
                )
                entry.scope_end = scope_end
                entries.append(entry)
                continue

            if name in ('setcounter', 'addtocounter', 'stepcounter',
                        'refstepcounter', 'appendix'):
                _apply_counter_event(node, counters, state, sectioning_macros)
                continue

            # Do not descend into macro arguments.  This is what keeps a
            # \section command inside a \newcommand definition (or inside a
            # footnote, caption, etc.) from becoming an outline entry.
            continue

        if node.isNodeType(LatexEnvironmentNode):
            if node.environmentname in _VERBATIM_ENVIRONMENTS:
                continue
            parsing_state = getattr(node, 'parsing_state', None)
            if parsing_state is not None and getattr(parsing_state, 'in_math_mode', False):
                continue
            body = node.nodelist
            if body is None:
                continue
            child_end = body.pos_end
            if child_end is None:
                child_end = node.pos_end
            _collect_entries(
                body, child_end, walker, sectioning_macros,
                counters, state, entries, include_starred, use_short_titles
            )
            continue

        if node.isNodeType(LatexGroupNode):
            parsing_state = getattr(node, 'parsing_state', None)
            if parsing_state is not None and getattr(parsing_state, 'in_math_mode', False):
                continue
            body = node.nodelist
            if body is None:
                continue
            child_end = body.pos_end
            if child_end is None:
                child_end = scope_end
            _collect_entries(
                body, child_end, walker, sectioning_macros,
                counters, state, entries, include_starred, use_short_titles
            )
            continue

        if node.isNodeType(LatexMathNode):
            continue

        # Chars, specials and comments don't introduce outline entries.


def _assign_body_ranges(entries):
    for index, entry in enumerate(entries):
        # The section's own body runs to the very next heading, whatever its
        # level; its full body (``body_end``) runs to the next heading of the
        # same or a higher level, and therefore includes its subsections.
        own_end = entry.scope_end
        for later in entries[index + 1:]:
            if later.heading_start >= entry.scope_end:
                break
            own_end = later.heading_start
            break
        entry.own_body_end = own_end

        end = entry.scope_end
        for later in entries[index + 1:]:
            if later.heading_start >= entry.scope_end:
                break
            if later.level <= entry.level:
                end = later.heading_start
                break
        entry.body_end = end


def _build_tree(entries, sectioning_macros):
    roots = []
    stack = []

    for entry in entries:
        entry.parent = None
        entry.children = []
        entry.depth = None
        entry.missing_parent = False
        entry.missing_parent_levels = []

    for entry in entries:
        while stack and stack[-1].level >= entry.level:
            stack.pop()

        if not stack:
            entry.parent = None
            entry.depth = 1
            roots.append(entry)
        else:
            parent = stack[-1]
            entry.parent = parent
            entry.depth = parent.depth + 1
            if entry.level > parent.level + 1:
                entry.missing_parent = True
                entry.missing_parent_levels = [
                    name for name in _SECTIONING_ORDER
                    if parent.level < sectioning_macros.get(name, parent.level)
                    and sectioning_macros.get(name) < entry.level
                ]
            parent.children.append(entry)

        stack.append(entry)

    return roots


def _outline_from_nodelist(walker, nodelist, sectioning_macros,
                           include_starred, use_short_titles,
                           respect_tocdepth=False):
    if sectioning_macros is None:
        sectioning_macros = SECTIONING_MACROS

    sectioning_macros = dict(sectioning_macros)

    body, scope_end, document_start, document_end = _find_document_body(nodelist)
    if scope_end is None and walker is not None:
        scope_end = len(walker.s)

    counters = {}
    for name in sectioning_macros:
        counters[name] = 0

    state = {
        'appendix': False,
        'secnumdepth': None,
        'tocdepth': None,
    }

    entries = []
    _collect_entries(
        body, scope_end, walker, sectioning_macros,
        counters, state, entries, include_starred, use_short_titles
    )

    _assign_body_ranges(entries)
    _build_tree(entries, sectioning_macros)

    if respect_tocdepth and state.get('tocdepth') is not None:
        max_level = state['tocdepth'] + 1
        entries = [entry for entry in entries if entry.level <= max_level]
        # Recompute the body ranges of the remaining entries; removing deep
        # entries must not shorten the body of their ancestors.
        _assign_body_ranges(entries)
        _build_tree(entries, sectioning_macros)

    return LatexOutline(
        entries,
        walker=walker,
        document_start=document_start,
        document_end=document_end,
    )


def _custom_sectioning_macro_specs(sectioning_macros, latex_context):
    r"""
    Return ``MacroSpec`` objects for custom sectioning macros that are not
    already known to `latex_context` (or to the default context, when no explicit
    context was given).
    """
    if sectioning_macros is None:
        return []

    from ..macrospec import MacroSpec

    specs = []
    for macroname in sectioning_macros:
        spec = None
        if latex_context is not None:
            spec = latex_context.get_macro_spec(macroname)
        if spec is None or getattr(spec, 'macroname', None) != macroname:
            specs.append(MacroSpec(macroname, '*[{'))
    return specs


def get_outline(source, sectioning_macros=None, include_starred=True,
                use_short_titles=True, latex_context=None,
                respect_tocdepth=False, **kwargs):
    r"""
    Build a hierarchical outline (table of contents) from LaTeX code.

    `source` may be:

      - a string of LaTeX code;
      - a :py:class:`~pylatexenc.latexwalker.LatexWalker` instance;
      - a :py:class:`~pylatexenc.latexnodes.nodes.LatexNodeList` (or a plain
        list of nodes) that has already been parsed;
      - a single :py:class:`~pylatexenc.latexnodes.nodes.LatexNode`, typically
        a ``document`` environment node.

    `sectioning_macros` optionally maps macro names to levels, overriding the
    default :py:data:`SECTIONING_MACROS`.  The macros are expected to follow the
    standard ``*[{`` argument convention (optional star, optional short title,
    mandatory full title), as in the default LaTeX context.  Custom macros are
    registered in the parsing context automatically when the source is parsed
    here; when you pass an already parsed node list, the macros must already be
    known to the context that parsed it.

    `include_starred` controls whether starred sections (``\section*``) appear
    in the outline.  They are included by default, but carry no displayed
    number.  `use_short_titles` controls whether the optional short title is
    used as the displayed title; it defaults to `True`.

    The returned :py:class:`LatexOutline` is a list of
    :py:class:`LatexSectionEntry` objects in document order.  Its `roots`
    attribute and each entry's `children` attribute expose the hierarchy.

    The displayed ``number`` is not unique: starred sections have no number and
    counter resets can make numbered sections repeat a number.  Use each
    entry's ``key`` (or ``id``) as the stable identifier instead.
    """
    if isinstance(source, LatexOutline):
        return source

    if isinstance(source, str):
        from ..latexwalker import LatexWalker, get_default_latex_context_db

        walker_kwargs = dict(kwargs)
        context = latex_context
        custom_macros = _custom_sectioning_macro_specs(sectioning_macros, context)
        if custom_macros:
            if context is None:
                context = get_default_latex_context_db()
            if not getattr(context, 'frozen', False):
                context = context.filtered_context()
                context.freeze()
            context = context.extended_with(category='outline', macros=custom_macros)
        if context is not None:
            walker_kwargs['latex_context'] = context
        walker = LatexWalker(source, **walker_kwargs)
        nodelist, _ = walker.parse_content()
        return _outline_from_nodelist(
            walker, nodelist, sectioning_macros,
            include_starred, use_short_titles,
            respect_tocdepth=respect_tocdepth,
        )

    if hasattr(source, 'parse_content') and hasattr(source, 's'):
        walker = source
        nodelist, _ = walker.parse_content()
        return _outline_from_nodelist(
            walker, nodelist, sectioning_macros,
            include_starred, use_short_titles,
            respect_tocdepth=respect_tocdepth,
        )

    if isinstance(source, LatexEnvironmentNode):
        walker = getattr(source, 'latex_walker', None)
        body = source.nodelist
        if body is None:
            body = LatexNodeList([])
        return _outline_from_nodelist(
            walker, body, sectioning_macros,
            include_starred, use_short_titles,
            respect_tocdepth=respect_tocdepth,
        )

    if isinstance(source, LatexNode):
        walker = getattr(source, 'latex_walker', None)
        nodelist = LatexNodeList([source])
        return _outline_from_nodelist(
            walker, nodelist, sectioning_macros,
            include_starred, use_short_titles,
            respect_tocdepth=respect_tocdepth,
        )

    if isinstance(source, LatexNodeList) or isinstance(source, (list, tuple)):
        nodelist = source
        walker = getattr(source, 'latex_walker', None)
        if walker is None and len(source):
            walker = getattr(source[0], 'latex_walker', None)
        return _outline_from_nodelist(
            walker, nodelist, sectioning_macros,
            include_starred, use_short_titles,
            respect_tocdepth=respect_tocdepth,
        )

    raise TypeError(
        'Cannot build an outline from {!r}'.format(type(source).__name__)
    )


build_outline = get_outline
get_section_outline = get_outline
get_table_of_contents = get_outline
make_outline = get_outline
parse_outline = get_outline


def get_section_outlines(source, **kwargs):
    r"""
    Like :py:func:`get_outline`, but return a plain list of the entries in
    document order.  This is a convenience for callers that only need the flat
    sequence.
    """
    return list(get_outline(source, **kwargs))
