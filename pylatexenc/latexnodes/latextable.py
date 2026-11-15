# -*- coding: utf-8 -*-

r"""
Extract tabular data from LaTeX code.

This module turns the contents of table-like environments (``tabular``,
``array`` and friends) into structured rows and cells that are convenient to
store in a data warehouse.  Parsing is delegated to
:class:`~pylatexenc.latexwalker.LatexWalker`, so the structure of the LaTeX
code is understood rather than guessed with regular expressions.

In particular, a column separator ``&`` or a row separator ``\\`` that appears
inside a macro argument or a nested braced group is *not* treated as a
separator, because it never reaches the top level of the table body.  The
escaped ampersand ``\&`` is likewise an ordinary character.

Typical use::

    >>> from pylatexenc.latexnodes.latextable import extract_latex_tables
    >>> tables = extract_latex_tables(r'''
    ... \begin{tabular}{l|c}
    ... Name & Value \\
    ... \hline
    ... A \& B & \multicolumn{2}{c}{n/a} \\
    ... \end{tabular}''')
    >>> table = tables[0]
    >>> table.environmentname
    'tabular'
    >>> cell = table.rows[0].cells[0]
    >>> cell.text
    'Name'
    >>> table.rows[1].cells[1].colspan
    2

Each returned object also records where it was found in the source, both as
raw character positions (``pos`` / ``pos_end``) and as 1-based line / column
numbers (``lineno`` / ``colno``; columns are 0-based, matching
:meth:`~pylatexenc.latexwalker.LatexWalker.pos_to_lineno_colno`).

Spaces and empty cells are reported separately: every cell exposes
``raw_text`` (the exact source slice, with surrounding spaces preserved),
``text`` (the trimmed cell text), and an ``is_empty`` flag that is only set
when the cell contains no content at all.  A cell holding spaces only is not
considered empty.

When a table is not properly closed, only the rows that could be read before
the first paragraph break are kept, and the situation is reported on
:attr:`ExtractedTable.is_truncated`.  Body text that follows the incomplete
table is never appended to it.

Tables are found wherever they occur, including inside a floating ``table``
environment or another container.  A tabular nested inside another tabular is
part of the outer cell content and is not extracted a second time.
"""


import logging
logger = logging.getLogger(__name__)


from .nodes import (
    LatexEnvironmentNode,
    LatexCharsNode,
    LatexCommentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexSpecialsNode,
)



DEFAULT_TABLE_ENVIRONMENT_NAMES = (
    'tabular',
    'array',
    'longtable',
    'tblr',
)


ROW_END_MACRONAMES = (
    '\\',
    'cr',
    'tabularnewline',
)


class LatexTableExtractionError(Exception):
    r"""
    Raised when table extraction encounters a situation it cannot represent.
    """
    pass



class SourcePosition(object):
    r"""
    A location in the parsed LaTeX source.

    .. py:attribute:: pos

       Raw character position where the item starts.

    .. py:attribute:: pos_end

       Raw character position immediately after the item.

    .. py:attribute:: lineno

       1-based line number of `pos`.

    .. py:attribute:: colno

       0-based column number of `pos`, matching the convention used by
       :meth:`~pylatexenc.latexwalker.LatexWalker.pos_to_lineno_colno`.

    .. py:attribute:: end_lineno

       1-based line number of `pos_end`.

    .. py:attribute:: end_colno

       0-based column number of `pos_end`.
    """
    def __init__(self, pos, pos_end, lineno, colno,
                 end_lineno, end_colno):
        self.pos = pos
        self.pos_end = pos_end
        self.lineno = lineno
        self.colno = colno
        self.end_lineno = end_lineno
        self.end_colno = end_colno

    def __repr__(self):
        return (
            "SourcePosition(pos={!r}, pos_end={!r}, lineno={!r}, colno={!r}, "
            "end_lineno={!r}, end_colno={!r})".format(
                self.pos, self.pos_end, self.lineno, self.colno,
                self.end_lineno, self.end_colno,
            )
        )

    def __eq__(self, other):
        return (
            isinstance(other, SourcePosition)
            and self.pos == other.pos
            and self.pos_end == other.pos_end
            and self.lineno == other.lineno
            and self.colno == other.colno
            and self.end_lineno == other.end_lineno
            and self.end_colno == other.end_colno
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None



class ExtractedTableCell(object):
    r"""
    A single cell of an extracted table.

    .. py:attribute:: row_index

       Zero-based index of the row the cell belongs to within the table.

    .. py:attribute:: column

       Zero-based index of the column where the cell *starts*.

    .. py:attribute:: colspan

       Number of columns spanned by the cell.  It is ``1`` for an ordinary
       cell and greater than one for a ``\multicolumn`` cell.

    .. py:attribute:: raw_text

       The exact LaTeX source of the cell, including surrounding whitespace and
       any cell-level macros.  This is a slice of the parsed string.

    .. py:attribute:: text

       The cell text with leading and trailing whitespace removed.  Spaces that
       appear inside the cell are kept.

    .. py:attribute:: is_empty

       ``True`` if the cell has no content at all (an empty column slot, as in
       the middle cell of ``a & & b``).  A cell that contains spaces only is
       not empty: its `text` is the empty string but `is_empty` is ``False``.

    .. py:attribute:: is_whitespace_only

       ``True`` if the cell contains at least one character but only
       whitespace.

    .. py:attribute:: source_position

       A :class:`SourcePosition` locating the cell in the source.
    """
    def __init__(self, row_index, column, colspan, raw_text, text,
                 is_empty, is_whitespace_only, source_position):
        self.row_index = row_index
        self.column = column
        self.colspan = colspan
        self.raw_text = raw_text
        self.text = text
        self.is_empty = is_empty
        self.is_whitespace_only = is_whitespace_only
        self.source_position = source_position

    @property
    def column_end(self):
        r"""
        Index of the column immediately after the cell, i.e.
        ``column + colspan``.
        """
        return self.column + self.colspan

    def __repr__(self):
        return (
            "ExtractedTableCell(row_index={!r}, column={!r}, colspan={!r}, "
            "text={!r}, is_empty={!r})".format(
                self.row_index, self.column, self.colspan,
                self.text, self.is_empty,
            )
        )

    def __eq__(self, other):
        return (
            isinstance(other, ExtractedTableCell)
            and self.row_index == other.row_index
            and self.column == other.column
            and self.colspan == other.colspan
            and self.raw_text == other.raw_text
            and self.text == other.text
            and self.is_empty == other.is_empty
            and self.is_whitespace_only == other.is_whitespace_only
            and self.source_position == other.source_position
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None



class ExtractedTableRow(object):
    r"""
    A row of an extracted table.

    .. py:attribute:: row_index

       Zero-based row index within the table.

    .. py:attribute:: cells

       List of :class:`ExtractedTableCell` objects, in source order.  A
       ``\multicolumn`` cell appears once and carries its span in
       :attr:`ExtractedTableCell.colspan`; covered columns do not produce
       additional cells.

    .. py:attribute:: source_position

       A :class:`SourcePosition` for the whole row, from the first cell
       content up to (but excluding) the row-ending macro.  For a row with no
       cells at all it spans whatever whitespace was found there.
    """
    def __init__(self, row_index, cells, source_position):
        self.row_index = row_index
        self.cells = cells
        self.source_position = source_position

    def __repr__(self):
        return (
            "ExtractedTableRow(row_index={!r}, cells={!r})".format(
                self.row_index, self.cells,
            )
        )

    def __eq__(self, other):
        return (
            isinstance(other, ExtractedTableRow)
            and self.row_index == other.row_index
            and self.cells == other.cells
            and self.source_position == other.source_position
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None



class ExtractedTable(object):
    r"""
    A table extracted from a LaTeX environment.

    .. py:attribute:: environmentname

       Name of the environment, e.g. ``'tabular'``.

    .. py:attribute:: rows

       List of :class:`ExtractedTableRow` objects.

    .. py:attribute:: n_columns

       The number of columns of the table, taken as the widest row seen in the
       body (counting ``\multicolumn`` spans).  Rows shorter than this width
       simply have fewer cells and are not padded.

    .. py:attribute:: is_truncated

       ``True`` if the environment was not properly closed and the extraction
       stopped at a paragraph break to avoid swallowing following prose.  In
       that case only the trustworthy rows are present.

    .. py:attribute:: source_position

       A :class:`SourcePosition` for the whole environment (or, for a
       truncated table, up to the point where extraction stopped).

    .. py:attribute:: body_source_position

       A :class:`SourcePosition` for the table body between
       ``\begin{...}`` and ``\end{...}``.
    """
    def __init__(self, environmentname, rows, n_columns, is_truncated,
                 source_position, body_source_position):
        self.environmentname = environmentname
        self.rows = rows
        self.n_columns = n_columns
        self.is_truncated = is_truncated
        self.source_position = source_position
        self.body_source_position = body_source_position

    def __repr__(self):
        return (
            "ExtractedTable(environmentname={!r}, rows={!r}, "
            "n_columns={!r}, is_truncated={!r})".format(
                self.environmentname, self.rows,
                self.n_columns, self.is_truncated,
            )
        )

    def __eq__(self, other):
        return (
            isinstance(other, ExtractedTable)
            and self.environmentname == other.environmentname
            and self.rows == other.rows
            and self.n_columns == other.n_columns
            and self.is_truncated == other.is_truncated
            and self.source_position == other.source_position
            and self.body_source_position == other.body_source_position
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None



def _is_table_environment(environmentname, table_environment_names):
    if environmentname in table_environment_names:
        return True
    for base in table_environment_names:
        if environmentname == base + '*':
            return True
        if environmentname == base + 'x':
            return True
    return False



def _environment_is_closed(env_node, latex_walker):
    end_marker = '\\end{' + env_node.environmentname + '}'
    span = latex_walker.s[env_node.pos:env_node.pos_end]
    return span.rstrip().endswith(end_marker)



def _make_source_position(latex_walker, pos, pos_end):
    if pos is None or pos_end is None:
        return SourcePosition(pos, pos_end, None, None, None, None)
    lineno, colno = latex_walker.pos_to_lineno_colno(pos)
    end_lineno, end_colno = latex_walker.pos_to_lineno_colno(pos_end)
    return SourcePosition(pos, pos_end, lineno, colno,
                          end_lineno, end_colno)



def _node_is_row_end(node):
    return (
        isinstance(node, LatexMacroNode)
        and node.macroname in ROW_END_MACRONAMES
    )



def _node_is_paragraph_break(node):
    return (
        isinstance(node, LatexSpecialsNode)
        and '\n\n' in (node.specials_chars or '')
    )



def _parse_int_group(group_node):
    text = ''
    for child in group_node.nodelist:
        if isinstance(child, LatexCharsNode):
            text += child.chars
        else:
            return None
    text = text.strip()
    if not text.isdigit():
        return None
    return int(text)



def _nodelist_content_text(nodelist, latex_walker):
    r"""
    Concatenate the source spans of a node list, skipping comments.
    """
    parts = []
    for node in nodelist:
        if isinstance(node, LatexCommentNode):
            continue
        parts.append(latex_walker.s[node.pos:node.pos_end])
    return ''.join(parts)



def _detect_multicolumn(macro_node, following_nodes, latex_walker):
    r"""
    Detect a ``\multicolumn{n}{align}{content}`` construct.

    The default latex context does not register ``\multicolumn``, so the
    walker reports the macro itself followed by three sibling brace groups.
    If a custom context does register the macro, the same information is
    instead available through the macro's parsed arguments.  Both shapes are
    handled here.

    Return a tuple ``(colspan, content_text, content_pos, content_pos_end)``
    or ``None`` if this node is not a usable multicolumn construct.
    """
    if not (isinstance(macro_node, LatexMacroNode)
            and macro_node.macroname == 'multicolumn'):
        return None

    argnlist = None
    nodeargd = getattr(macro_node, 'nodeargd', None)
    if nodeargd is not None:
        argnlist = getattr(nodeargd, 'argnlist', None)

    if argnlist is not None and len(argnlist) >= 3:
        span_group = argnlist[0]
        content_group = argnlist[2]
    else:
        groups = []
        for candidate in following_nodes:
            if isinstance(candidate, LatexGroupNode):
                groups.append(candidate)
            elif isinstance(candidate, (LatexCharsNode, LatexCommentNode)):
                continue
            else:
                break
            if len(groups) == 3:
                break
        if len(groups) < 3:
            return None
        span_group = groups[0]
        content_group = groups[2]

    if not isinstance(span_group, LatexGroupNode) \
            or not isinstance(content_group, LatexGroupNode):
        return None

    colspan = _parse_int_group(span_group)
    if colspan is None or colspan < 1:
        return None

    content_text = _nodelist_content_text(
        content_group.nodelist, latex_walker
    )
    inner_nodelist = content_group.nodelist
    if inner_nodelist is None or len(inner_nodelist) == 0:
        content_pos = content_group.pos_end
        content_pos_end = content_group.pos_end
    else:
        content_pos = inner_nodelist[0].pos
        content_pos_end = inner_nodelist[-1].pos_end
    return colspan, content_text, content_pos, content_pos_end



class _RowBuilder(object):
    r"""
    Accumulate the top-level nodes of a table body into rows and cells.
    """

    def __init__(self, latex_walker, is_closed):
        self.latex_walker = latex_walker
        self.is_closed = is_closed
        self.rows = []
        self.n_columns = 0

        self._current_cells = []
        self._cell_start_pos = None
        self._cell_has_content = False
        self._cell_segments = []
        self._cell_multicolumn = None
        self._current_column = 0
        self._row_start_pos = None
        self._row_saw_separator = False

    def _open_row(self, pos):
        if self._row_start_pos is None:
            self._row_start_pos = pos

    def _open_cell(self, pos):
        if self._cell_start_pos is None:
            self._cell_start_pos = pos

    def _append_segment(self, node):
        if self._cell_start_pos is None:
            self._cell_start_pos = node.pos
        self._cell_segments.append(node)
        if not isinstance(node, LatexCommentNode):
            self._cell_has_content = True

    def _finish_cell(self, end_pos, row_index):
        start_pos = self._cell_start_pos
        if start_pos is None:
            start_pos = end_pos

        raw_text = self.latex_walker.s[start_pos:end_pos]
        multicolumn = self._cell_multicolumn

        if multicolumn is not None:
            colspan, content_text = multicolumn
            text = content_text.strip()
            is_empty = len(content_text) == 0
            is_whitespace_only = (
                not is_empty and len(text) == 0
            )
        else:
            colspan = 1
            content_text = _nodelist_content_text(
                self._cell_segments, self.latex_walker
            )
            text = content_text.strip()
            has_visible_content = False
            for segment in self._cell_segments:
                if not isinstance(segment, LatexCommentNode):
                    has_visible_content = True
                    break
            is_empty = not has_visible_content
            is_whitespace_only = (
                has_visible_content and len(text) == 0
            )

        cell = ExtractedTableCell(
            row_index=row_index,
            column=self._current_column,
            colspan=colspan,
            raw_text=raw_text,
            text=text,
            is_empty=is_empty,
            is_whitespace_only=is_whitespace_only,
            source_position=_make_source_position(
                self.latex_walker, start_pos, end_pos
            ),
        )
        self._current_cells.append(cell)
        self._current_column += colspan

        self._cell_start_pos = None
        self._cell_has_content = False
        self._cell_segments = []
        self._cell_multicolumn = None

    def _row_has_meaningful_content(self):
        for cell in self._current_cells:
            if not cell.is_empty and not cell.is_whitespace_only:
                return True
        return False

    def _finish_row(self, end_pos, is_implicit_tail=False):
        row_index = len(self.rows)

        if self._cell_start_pos is not None or self._cell_has_content:
            self._finish_cell(end_pos, row_index)

        keep_row = True
        if not self._current_cells:
            keep_row = False
        elif is_implicit_tail and not self._row_saw_separator \
                and not self._row_has_meaningful_content():
            keep_row = False

        if keep_row:
            row = ExtractedTableRow(
                row_index=row_index,
                cells=list(self._current_cells),
                source_position=_make_source_position(
                    self.latex_walker,
                    self._row_start_pos if self._row_start_pos is not None
                    else end_pos,
                    end_pos,
                ),
            )
            self.rows.append(row)
            if self._current_column > self.n_columns:
                self.n_columns = self._current_column

        self._current_cells = []
        self._cell_start_pos = None
        self._cell_has_content = False
        self._cell_segments = []
        self._cell_multicolumn = None
        self._current_column = 0
        self._row_start_pos = None
        self._row_saw_separator = False

    def add_nodes(self, body_nodelist, stop_pos):
        r"""
        Consume the top-level nodes of a table body.

        `stop_pos` is where processing stopped: the end of the body for a
        closed table, or the end of the recovered environment span otherwise.
        Return a tuple ``(actual_stop_pos, is_truncated)``.
        """
        index = 0
        n_nodes = len(body_nodelist)
        while index < n_nodes:
            node = body_nodelist[index]
            index += 1

            if _node_is_paragraph_break(node) and not self.is_closed:
                self._finish_row(node.pos, is_implicit_tail=True)
                return node.pos, True

            if _node_is_row_end(node):
                self._open_row(node.pos)
                self._finish_row(node.pos)
                continue

            if isinstance(node, LatexSpecialsNode) \
                    and node.specials_chars == '&':
                self._open_row(node.pos)
                self._row_saw_separator = True
                self._finish_cell(node.pos, len(self.rows))
                continue

            self._open_row(node.pos)

            if isinstance(node, LatexMacroNode) \
                    and node.macroname == 'multicolumn':
                following = body_nodelist[index:index + 6]
                info = _detect_multicolumn(
                    node, following, self.latex_walker
                )
                if info is not None:
                    colspan, content_text, _cpos, cpos_end = info

                    nodeargd = getattr(node, 'nodeargd', None)
                    argnlist = getattr(nodeargd, 'argnlist', None)
                    consumed_siblings = not argnlist
                    if consumed_siblings:
                        groups_seen = 0
                        while index < n_nodes and groups_seen < 3:
                            sibling = body_nodelist[index]
                            if isinstance(sibling, LatexGroupNode):
                                groups_seen += 1
                                index += 1
                            elif isinstance(sibling,
                                            (LatexCharsNode, LatexCommentNode)):
                                index += 1
                            else:
                                break

                    self._open_cell(node.pos)
                    self._cell_has_content = True
                    self._cell_multicolumn = (colspan, content_text)
                    continue

            self._open_cell(node.pos)
            self._append_segment(node)

        self._finish_row(stop_pos, is_implicit_tail=True)
        return stop_pos, False



def extract_table_from_environment_node(env_node, latex_walker=None):
    r"""
    Extract the rows and cells of a single tabular environment node.

    The node is typically obtained by parsing a document with
    :class:`~pylatexenc.latexwalker.LatexWalker` and looking for
    :class:`~pylatexenc.latexnodes.nodes.LatexEnvironmentNode` instances.
    """
    if not isinstance(env_node, LatexEnvironmentNode):
        raise LatexTableExtractionError(
            "Expected a LatexEnvironmentNode, got: {!r}".format(env_node)
        )

    if latex_walker is None:
        latex_walker = env_node.latex_walker
    if latex_walker is None:
        raise LatexTableExtractionError(
            "Cannot extract table without a latex walker: the node carries no "
            "source string reference."
        )

    body_nodelist = env_node.nodelist
    if body_nodelist is None:
        body_nodelist = []
    body_nodelist = list(body_nodelist)

    # Environments that the latex context does not know about have their
    # mandatory alignment argument (the "{cc}" after \begin{longtable}{cc})
    # parsed as an ordinary brace group at the start of the body.  Recognized
    # environments expose it through nodeargd instead.  Skip exactly that one
    # leading group so it does not end up in the first cell.
    nodeargd = getattr(env_node, 'nodeargd', None)
    has_parsed_arguments = bool(
        nodeargd is not None and getattr(nodeargd, 'argnlist', None)
    )
    if not has_parsed_arguments and body_nodelist \
            and isinstance(body_nodelist[0], LatexGroupNode):
        body_nodelist = body_nodelist[1:]

    body_start = env_node.pos
    body_end = env_node.pos_end
    if len(body_nodelist):
        body_start = body_nodelist[0].pos
        body_end = body_nodelist[-1].pos_end

    is_closed = _environment_is_closed(env_node, latex_walker)

    builder = _RowBuilder(latex_walker, is_closed)
    stop_pos, stopped_at_paragraph_break = builder.add_nodes(
        list(body_nodelist), body_end
    )
    is_truncated = stopped_at_paragraph_break or not is_closed

    if stopped_at_paragraph_break:
        table_pos_end = stop_pos
        body_pos_end = stop_pos
    else:
        table_pos_end = env_node.pos_end
        body_pos_end = body_end

    return ExtractedTable(
        environmentname=env_node.environmentname,
        rows=builder.rows,
        n_columns=builder.n_columns,
        is_truncated=is_truncated,
        source_position=_make_source_position(
            latex_walker, env_node.pos, table_pos_end
        ),
        body_source_position=_make_source_position(
            latex_walker, body_start, body_pos_end
        ),
    )



def _collect_table_environment_nodes(nodelist, table_environment_names):
    r"""
    Walk a node tree and return every table environment in source order.

    The walk descends through floating environments such as ``table``, other
    nesting environments, braced groups and macro arguments, so that a tabular
    wrapped in such a construct is still found.  Once a matching table is
    found, its own body is not searched again: a tabular nested in another
    tabular stays part of the outer cell content instead of being reported a
    second time.
    """
    result = []
    _collect_table_environment_nodes_into(
        nodelist, table_environment_names, result
    )
    return result



def _collect_table_environment_nodes_into(nodelist,
                                          table_environment_names, result):
    for node in nodelist:
        if isinstance(node, LatexEnvironmentNode):
            if _is_table_environment(
                    node.environmentname, table_environment_names):
                result.append(node)
                continue

        child_nodelists = []
        nested_nodelist = getattr(node, 'nodelist', None)
        if nested_nodelist is not None:
            child_nodelists.append(nested_nodelist)
        nodeargd = getattr(node, 'nodeargd', None)
        if nodeargd is not None:
            argnlist = getattr(nodeargd, 'argnlist', None)
            if argnlist is not None:
                for argument_node in argnlist:
                    if argument_node is not None:
                        child_nodelists.append([argument_node])

        for child_nodelist in child_nodelists:
            _collect_table_environment_nodes_into(
                child_nodelist, table_environment_names, result
            )



def extract_latex_tables(latex_text, latex_walker=None,
                         table_environment_names=None,
                         tolerant_parsing=True):
    r"""
    Parse `latex_text` and extract all table-like environments it contains.

    Arguments:

    - `latex_text` is the LaTeX source to parse.

    - `latex_walker` may be a pre-configured
      :class:`~pylatexenc.latexwalker.LatexWalker` instance (for instance one
      with a custom latex context).  If given, `latex_text` must be the same
      string that the walker was built with.

    - `table_environment_names` restricts which environments are extracted.
      It defaults to :data:`DEFAULT_TABLE_ENVIRONMENT_NAMES`; suffixed
      variants like ``tabular*`` or ``tabularx`` match automatically.

    - `tolerant_parsing` is forwarded to :class:`LatexWalker` when no walker
      is supplied.  It is enabled by default so that an unclosed environment
      does not abort the whole extraction; the incomplete table is then
      reported with :attr:`ExtractedTable.is_truncated` set.

    Return a list of :class:`ExtractedTable` objects, in source order.
    Tables nested inside a floating ``table`` environment, another container
    environment, a group or a macro argument are found.  A tabular nested
    inside another extracted tabular is not returned separately; it remains
    part of the enclosing cell content.
    """
    if table_environment_names is None:
        table_environment_names = DEFAULT_TABLE_ENVIRONMENT_NAMES
    else:
        table_environment_names = tuple(table_environment_names)

    if latex_walker is None:
        from ..latexwalker import LatexWalker
        latex_walker = LatexWalker(
            latex_text, tolerant_parsing=tolerant_parsing
        )
    elif latex_text is not None and latex_walker.s != latex_text:
        raise LatexTableExtractionError(
            "The given latex_text does not match the string of the given "
            "latex_walker."
        )

    nodelist, _parsing_state_delta = latex_walker.parse_content()

    environment_nodes = _collect_table_environment_nodes(
        nodelist, table_environment_names
    )

    tables = []
    for node in environment_nodes:
        tables.append(
            extract_table_from_environment_node(
                node, latex_walker=latex_walker
            )
        )

    return tables
