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

# The implementation of the table-extraction functions that `latex2text`
# re-exports.  The module is kept separate because it is Python-only: the
# JavaScript build has no use for it, and `latex2text` guards the import.

r"""
Extract the grid of a tabular-like LaTeX table from its parsed node tree.

The extraction works on the node tree produced by the LaTeX walker and not on
the raw text.  A column separator is only the ``&`` *specials* node that sits
directly in the table body, so an ``&`` that belongs to a macro argument, to
another environment, to a braced group, to a comment, or to the escape
sequence ``\&`` cannot be mistaken for a separator.  A row separator is only
the ``\\`` (``\tabularnewline``, ``\cr``) macro node that sits directly in the
body, so the same macro nested in a cell does not end a row.

Each cell reports its plain text, its exact source region (position and line
and column numbers), and the number of columns it spans.  A cell that contains
only whitespace is kept apart from a cell that was not written at all: the
former has a source position and the whitespace is reported, the latter is
marked `empty` and has no position.

A table whose ``\end{...}`` is missing is reported with the rows that could
legitimately be established.  Content that follows the last row separator is
attached to the table by the parser, because it cannot know where the table
was meant to stop; the extractor recognizes the start of a new paragraph or
of a sectioning command in that content and leaves it out of the table instead
of presenting it as table cells.

.. versionadded:: 3.0
"""


from ..latexnodes import nodes as latexnodes_nodes


# ------------------------------------------------------------------------------
#
# The data structures that are returned.
#


class TableCell(object):
    r"""
    One cell of an extracted table.

    .. py:attribute:: row

       The zero-based row index of the cell's origin, that is, of the position
       where the cell starts.  A cell that a ``\multicolumn`` widened or a
       ``\multirow`` lengthened still reports the row where it begins.

    .. py:attribute:: column

       The zero-based column index of the cell's origin.

    .. py:attribute:: rowspan

       How many rows the cell covers, one for an ordinary cell.  A
       ``\multirow{2}{...}{...}`` reports two.

    .. py:attribute:: colspan

       How many columns the cell covers, one for an ordinary cell.  A
       ``\multicolumn{2}{...}{...}`` reports two.

    .. py:attribute:: text

       The cell content as plain text, converted with the same rules that
       `latex2text` uses elsewhere and with the outer whitespace trimmed.  A
       cell that holds the content of ``\multicolumn`` reports that content;
       the width and column specification arguments of the macro are not part
       of the text.

    .. py:attribute:: source

       The exact LaTeX code of the cell content, with the outer whitespace
       included as it appears in the source.  An unwritten cell reports an
       empty string.

    .. py:attribute:: pos

       The position of the first character of the cell content in the parsed
       string.  It is `None` for a cell that was not written at all.

    .. py:attribute:: pos_end

       The position just after the last character of the cell content.  It is
       `None` for a cell that was not written at all.

    .. py:attribute:: lineno

       The one-based line number of `pos` in the parsed string, or `None`.

    .. py:attribute:: colno

       The zero-based column number of `pos` on its line, or `None`.

    .. py:attribute:: empty

       `True` if nothing was written between the cell's delimiters, so the cell
       has content neither in the source nor in the output.  A cell that holds
       a single space is *not* empty: it has `whitespace_only=True` and reports
       the source that produced the space.

    .. py:attribute:: whitespace_only

       `True` if the cell's source consists only of whitespace (characters such
       as spaces, tabs and newlines).  Such a cell has a source position and
       an empty `text`, but it is distinct from an `empty` cell.

    .. versionadded:: 3.0
    """

    def __init__(self, row, column, rowspan=1, colspan=1,
                 text='', source='', pos=None, pos_end=None,
                 lineno=None, colno=None, empty=False, whitespace_only=False):
        self.row = row
        self.column = column
        self.rowspan = rowspan
        self.colspan = colspan
        self.text = text
        self.source = source
        self.pos = pos
        self.pos_end = pos_end
        self.lineno = lineno
        self.colno = colno
        self.empty = empty
        self.whitespace_only = whitespace_only

    def to_json_object(self):
        r"""
        Return a dictionary that can be serialized to JSON.
        """
        return {
            'row': self.row,
            'column': self.column,
            'rowspan': self.rowspan,
            'colspan': self.colspan,
            'text': self.text,
            'source': self.source,
            'pos': self.pos,
            'pos_end': self.pos_end,
            'lineno': self.lineno,
            'colno': self.colno,
            'empty': self.empty,
            'whitespace_only': self.whitespace_only,
        }

    def __repr__(self):
        return ("TableCell(row={!r}, column={!r}, rowspan={!r}, colspan={!r}, "
                "text={!r}, pos={!r}, pos_end={!r})").format(
                    self.row, self.column, self.rowspan, self.colspan,
                    self.text, self.pos, self.pos_end)


class TableRow(object):
    r"""
    One row of an extracted table.

    .. py:attribute:: index

       The zero-based row index.

    .. py:attribute:: cells

       The cells whose origin lies on this row, ordered by column.

    .. py:attribute:: pos

       The position of the first character of the row's first cell, or `None`
       if the row holds no cell with a source position.  It points at the
       row's content, not at the row separator that precedes it.

    .. py:attribute:: pos_end

       The position just after the row's content, that is, the position of the
       row separator that ended the row, or the end of the table body for the
       last row.

    .. py:attribute:: lineno

       The one-based line number of `pos`, or `None`.

    .. py:attribute:: colno

       The zero-based column number of `pos`, or `None`.

    .. py:attribute:: complete

       `True` if the row ended with a row separator or with the ``\end{...}``
       of the table.  `False` flags the last row of a table whose structure
       broke off; its cells are the ones whose content was trustworthy.

    .. versionadded:: 3.0
    """

    def __init__(self, index, cells, pos=None, pos_end=None,
                 lineno=None, colno=None, complete=True):
        self.index = index
        self.cells = cells
        self.pos = pos
        self.pos_end = pos_end
        self.lineno = lineno
        self.colno = colno
        self.complete = complete

    def to_json_object(self):
        r"""
        Return a dictionary that can be serialized to JSON.
        """
        return {
            'index': self.index,
            'cells': [c.to_json_object() for c in self.cells],
            'pos': self.pos,
            'pos_end': self.pos_end,
            'lineno': self.lineno,
            'colno': self.colno,
            'complete': self.complete,
        }

    def __repr__(self):
        return ("TableRow(index={!r}, pos={!r}, pos_end={!r}, complete={!r}, "
                "cells={!r})").format(
                    self.index, self.pos, self.pos_end, self.complete, self.cells)


class ExtractedTable(object):
    r"""
    A tabular-like table as a rectangular grid of cells, along with the
    information needed to locate it in the source.

    .. py:attribute:: environmentname

       The name of the environment that was extracted, e.g. ``'tabular'``.

    .. py:attribute:: column_count

       The number of columns of the grid.  It is the number of columns declared
       by the environment's column specification when that specification can
       be read, and otherwise as many columns as the widest row holds.  A
       ``\multicolumn`` that reaches beyond the declared columns widens it.

    .. py:attribute:: declared_column_count

       The number of columns declared by the environment's column
       specification, or `None` if the specification could not be read.

    .. py:attribute:: rows

       The rows of the table, each a :py:class:`TableRow`.

    .. py:attribute:: cells

       The cells of the table, ordered by row and then by column.  A cell that
       spans several rows or columns appears once, at its origin.

    .. py:attribute:: pos

       The position of the ``\begin{...}`` of the table in the parsed string.

    .. py:attribute:: pos_end

       The position just after the table, or `None` if its ``\end{...}`` was
       not found.

    .. py:attribute:: lineno

       The one-based line number of `pos`.

    .. py:attribute:: colno

       The zero-based column number of `pos`.

    .. py:attribute:: body_pos

       The position of the first character of the table body, that is, after
       the arguments that follow ``\begin{...}``.

    .. py:attribute:: body_pos_end

       The position just after the last character of the table body.

    .. py:attribute:: terminated

       `True` if the table's ``\end{...}`` was found.  If it was not, the rows
       that could be established are still reported, but the content that
       follows them is left out of the table; `warnings` records the problem.

    .. py:attribute:: warnings

       A list of human-readable strings that describe the structure problems
       that were encountered, if any.  A warning does not by itself invalidate
       the cells that are reported.

    .. versionadded:: 3.0
    """

    def __init__(self, environmentname, column_count, declared_column_count,
                 rows, cells, pos=None, pos_end=None, lineno=None, colno=None,
                 body_pos=None, body_pos_end=None, terminated=True,
                 warnings=None):
        self.environmentname = environmentname
        self.column_count = column_count
        self.declared_column_count = declared_column_count
        self.rows = rows
        self.cells = cells
        self.pos = pos
        self.pos_end = pos_end
        self.lineno = lineno
        self.colno = colno
        self.body_pos = body_pos
        self.body_pos_end = body_pos_end
        self.terminated = terminated
        self.warnings = warnings if warnings is not None else []

    def to_json_object(self):
        r"""
        Return a dictionary that can be serialized to JSON.  The cells are
        reported inside their rows and also as a flat list, so that a consumer
        can choose either without walking the structure again.
        """
        return {
            'environmentname': self.environmentname,
            'column_count': self.column_count,
            'declared_column_count': self.declared_column_count,
            'rows': [r.to_json_object() for r in self.rows],
            'cells': [c.to_json_object() for c in self.cells],
            'pos': self.pos,
            'pos_end': self.pos_end,
            'lineno': self.lineno,
            'colno': self.colno,
            'body_pos': self.body_pos,
            'body_pos_end': self.body_pos_end,
            'terminated': self.terminated,
            'warnings': list(self.warnings),
        }

    def cell_at(self, row, column):
        r"""
        Return the cell that covers the grid position `(row, column)`, whether
        it is the cell's origin or a position that the cell spans, or `None`
        if no cell covers that position.
        """
        for cell in self.cells:
            if cell.row <= row < cell.row + cell.rowspan \
               and cell.column <= column < cell.column + cell.colspan:
                return cell
        return None

    def __repr__(self):
        return ("ExtractedTable(environmentname={!r}, column_count={!r}, "
                "rows={!r}, terminated={!r})").format(
                    self.environmentname, self.column_count,
                    len(self.rows), self.terminated)


# ------------------------------------------------------------------------------
#
# Reading the column specification.
#


# The environments that can be laid out as a table field by field.  The value
# is the index, in the environment's argument list, of the argument that holds
# the column specification, or `None` when the environment has no such
# argument and its number of columns must be read from the widest row.
#
_TABLE_ENVIRONMENTS = {
    'tabular': 0,
    'tabular*': 1,
    'tabularx': 2,
    'xltabular': 2,
    'array': 1,
}

# Matrix environments: no column specification, the width is the widest row.
_MATRIX_ENVIRONMENTS = {
    'matrix': True,
    'pmatrix': True,
    'bmatrix': True,
    'Bmatrix': True,
    'vmatrix': True,
    'Vmatrix': True,
    'smallmatrix': True,
    'psmallmatrix': True,
    'bsmallmatrix': True,
}


def _group_plain_content(node):
    r"""
    Return the string content of a braced group node, or `None` if `node` is
    not a braced group or its content is not plain characters and comments.
    """
    if node is None or not node.isNodeType(latexnodes_nodes.LatexGroupNode):
        return None
    out = []
    for n in node.nodelist:
        if n is None:
            continue
        if n.isNodeType(latexnodes_nodes.LatexCharsNode):
            out.append(n.chars)
            continue
        if n.isNodeType(latexnodes_nodes.LatexCommentNode):
            # a comment contributes no characters of its own
            out.append(n.comment_post_space)
            continue
        return None
    return ''.join(out)


def _read_span(node):
    r"""
    Read the width of a ``\multicolumn`` or the height of a ``\multirow``.

    Both accept a plain positive integer, or the special value ``*`` which lets
    LaTeX determine the number from the surrounding structure.  A ``*``, like
    an argument that cannot be read, is reported as `None`.
    """
    s = _group_plain_content(node)
    if s is None:
        return None
    s = s.strip()
    if s == '*' or not s.isdigit():
        return None
    value = int(s)
    if value <= 0:
        return None
    return value


def _skip_braced_group(s, pos):
    r"""
    Return the position just after the braced group that starts at `pos`, or
    `None` if there is no well-formed braced group there.  Escaped braces
    (``\{``, ``\}``) do not count as braces.
    """
    num = len(s)
    if pos >= num or s[pos] != '{':
        return None
    depth = 0
    while pos < num:
        c = s[pos]
        if c == '\\' and pos + 1 < num:
            pos += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return None


def _parse_column_spec(s, pos, end_char, depth):
    r"""
    Parse a column specification, returning ``(position, column_count, ok)``.

    The specification is a sequence such as ``|l|c|p{3cm}|*{2}{c}``:

    - ``l``, ``c``, ``r``, ``X`` and ``S`` each declare one column;
    - ``p``, ``m`` and ``b`` declare one column and are followed by a braced
      width, which may itself contain braces;
    - ``|`` draws a vertical rule and declares no column;
    - ``@{}``, ``!{}``, ``>{}`` and ``<{}`` insert material between columns and
      declare no column of their own;
    - ``*{n}{spec}`` repeats `spec` `n` times.

    Anything else makes the result `None`, because a count that was guessed
    would put every following cell in the wrong column.  The `depth` argument
    stops a pathological nesting of repetitions from being followed.
    """
    if depth > 8:
        return pos, 0, False

    count = 0
    num = len(s)
    while pos < num:
        c = s[pos]
        if end_char is not None and c == end_char:
            return pos + 1, count, True
        if c.isspace():
            pos += 1
            continue
        if c == '|':
            pos += 1
            continue
        if c in ('@', '!', '>', '<'):
            pos += 1
            next_pos = _skip_braced_group(s, pos)
            if next_pos is None:
                return pos, count, False
            pos = next_pos
            continue
        if c == '*':
            pos += 1
            num_pos = _skip_braced_group(s, pos)
            if num_pos is None:
                return pos, count, False
            repetitions = _parse_plain_int(s[pos + 1:num_pos - 1])
            pos = num_pos
            spec_pos = _skip_braced_group(s, pos)
            if spec_pos is None:
                return pos, count, False
            inner = s[pos + 1:spec_pos - 1]
            pos = spec_pos
            if repetitions is None:
                return pos, count, False
            inner_pos, inner_count, ok = _parse_column_spec(inner, 0, None, depth + 1)
            if not ok or inner_pos != len(inner):
                return pos, count, False
            count += inner_count * repetitions
            continue
        if c in ('l', 'c', 'r', 'X'):
            count += 1
            pos += 1
            continue
        if c in ('p', 'm', 'b'):
            count += 1
            pos += 1
            if pos < num and s[pos] == '{':
                next_pos = _skip_braced_group(s, pos)
                if next_pos is None:
                    return pos, count, False
                pos = next_pos
            continue
        if c == 'S':
            # siunitx's numeric column: `S` optionally followed by a bracketed
            # key-value list, as in `S[table-format=2.1]`
            count += 1
            pos += 1
            if pos < num and s[pos] == '[':
                close = s.find(']', pos)
                if close == -1:
                    return pos, count, False
                pos = close + 1
            continue
        return pos, count, False

    if end_char is not None:
        return pos, count, False
    return pos, count, True


def _parse_plain_int(s):
    s = s.strip()
    if not s.isdigit():
        return None
    return int(s)


def _group_source_text(node):
    r"""
    Return the text between the delimiters of a braced group node.

    The column specification of ``\begin{tabular}{p{3cm}m{2cm}}`` contains
    nested groups, so reading its node list would have to put the nested groups
    back together.  The source text of the group is the specification as it was
    written, which is what a column specification is read from.
    """
    if node is None or not node.isNodeType(latexnodes_nodes.LatexGroupNode):
        return None
    walker = node.latex_walker
    if walker is None or node.pos is None or node.pos_end is None:
        return None
    delimiters = node.delimiters
    if delimiters is None:
        return None
    open_delim, close_delim = delimiters
    start = node.pos + len(open_delim)
    end = node.pos_end - len(close_delim)
    if end < start:
        return None
    return walker.s[start:end]


def _read_column_spec(node):
    r"""
    Return the number of columns declared by a table environment's column
    specification argument, or `None` if it cannot be read.
    """
    s = _group_source_text(node)
    if s is None:
        return None
    pos, count, ok = _parse_column_spec(s, 0, None, 0)
    if not ok or pos != len(s):
        return None
    return count


def _read_declared_columns(envnode):
    r"""
    Return ``(declared_count, indices)`` for a table environment: the number of
    columns its specification declares (or `None`), and the index of the
    argument that holds the specification (or `None`).
    """
    name = envnode.environmentname
    arg_index = _TABLE_ENVIRONMENTS.get(name, None)
    if name in _MATRIX_ENVIRONMENTS:
        return None, None
    if arg_index is None:
        return None, None
    argnlist = envnode.nodeargd.argnlist if envnode.nodeargd is not None else []
    if arg_index >= len(argnlist):
        return None, arg_index
    return _read_column_spec(argnlist[arg_index]), arg_index


# ------------------------------------------------------------------------------
#
# Recognizing the constructs that surround cells.
#


# Macros that end a row when they sit directly in the body.
_ROW_SEPARATOR_MACROS = {
    '\\': True,
    'tabularnewline': True,
    'cr': True,
}

# Macros that draw a rule and are not part of any cell.  They carry no
# arguments of their own; the ones that accept an optional vertical-space
# argument are listed separately, so that `[a]` right after `\hline` is content
# and `[1pt]` right after `\toprule` is not.
_RULE_MACROS = {
    'hline': True,
    'toprule': True,
    'midrule': True,
    'bottomrule': True,
    'addlinespace': True,
}

# The rules that accept an optional bracketed argument, as in `\toprule[1pt]`.
_RULE_MACROS_WITH_OPTIONAL_ARGUMENT = {
    'toprule': True,
    'midrule': True,
    'bottomrule': True,
    'addlinespace': True,
}

# Macros whose arguments say where a rule goes, e.g. `\cline{1-2}`.
_RULE_ARGUMENT_MACROS = {
    'cline': True,
    'cmidrule': True,
    'hhline': True,
    'cdashline': True,
    'morecmidrules': True,
}

# Macros that decorate a cell or a row without contributing text to it.  Their
# arguments carry formatting information, labels or indices, not content.
_DECORATION_MACROS = {
    'label': True,
    'index': True,
    'glossary': True,
    'rowcolor': True,
    'rowcolors': True,
    'cellcolor': True,
    'color': True,
    'noalign': True,
    'vspace': True,
    'rule': True,
}

# Sectioning and structural commands that never belong to a table's cells.
# They mark the point where a broken table body has run into the following
# text.
_STRUCTURAL_MACROS = {
    'section': True,
    'subsection': True,
    'subsubsection': True,
    'paragraph': True,
    'subparagraph': True,
    'chapter': True,
    'part': True,
    'begin': True,
    'end': True,
    'item': True,
    'caption': True,
    'documentclass': True,
    'maketitle': True,
}

# Macros that are followed by a braced group that is an argument of theirs.
# Used where the default definitions do not declare the arguments.
_DECLARED_NO_ARGUMENT_MACROS_WITH_GROUPS = {
    'multicolumn': True,
    'multirow': True,
}


def _is_macro(node, names):
    return (
        node is not None
        and node.isNodeType(latexnodes_nodes.LatexMacroNode)
        and names.get(node.macroname, False)
    )


def _is_column_separator(node):
    # Only the '&' specials node directly in the body separates columns.  The
    # macro `\&` is a macro node, not a specials node: it produces a literal
    # ampersand and is content.
    return (
        node is not None
        and node.isNodeType(latexnodes_nodes.LatexSpecialsNode)
        and node.specials_chars == '&'
    )


def _is_paragraph_specials(node):
    return (
        node is not None
        and node.isNodeType(latexnodes_nodes.LatexSpecialsNode)
        and node.specials_chars == '\n\n'
    )


def _collect_group_arguments(nodelist, index, count):
    r"""
    Read `count` braced-group arguments that follow position `index` in
    `nodelist`.

    The default definitions leave the arguments of ``\multicolumn`` and
    ``\multirow`` as separate group nodes after the macro, rather than attaching
    them to the macro's `nodeargd`.  Whitespace and comments between the groups
    are skipped.  Returns ``(groups, next_index)``; `groups` is `None` when
    fewer than `count` groups are present, and `next_index` is where reading
    should continue (only meaningful when `groups` is not `None`).
    """
    groups = []
    num = len(nodelist)
    while index < num and len(groups) < count:
        node = nodelist[index]
        if node is None:
            index += 1
            continue
        if node.isNodeType(latexnodes_nodes.LatexGroupNode):
            groups.append(node)
            index += 1
            continue
        if node.isNodeType(latexnodes_nodes.LatexCommentNode):
            index += 1
            continue
        if node.isNodeType(latexnodes_nodes.LatexCharsNode) and not node.chars.strip():
            index += 1
            continue
        return None, index
    if len(groups) != count:
        return None, index
    return groups, index


def _skip_following_arguments(nodelist, index, macro_name=None):
    r"""
    Find the end of the arguments that follow a macro.

    Returns ``(next_index, resume_pos)``.  `next_index` is the index of the
    first node after the macro's arguments, and `resume_pos` is the position in
    the source where the following content starts.  `resume_pos` is `None` when
    the arguments end exactly at a node boundary.

    A macro such as ``\cline{1-2}`` leaves its braced argument as a separate
    group node, and ``\cmidrule(lr){1-2}`` additionally leaves the
    parenthesized part as a characters node.  The arguments are skipped by this
    function so that they do not end up in a cell.

    `macro_name` selects which optional bracketed argument the macro accepts:
    only the rules that take a vertical-space argument (``\toprule[1pt]``) skip
    a leading ``[...]``.  Without it, a bracketed piece right after the macro is
    content, as in ``\hline [a]``.

    The caller must call :py:func:`_split_leading_text()` when `resume_pos` is
    not `None`, so that the node at `next_index` no longer carries the skipped
    text.
    """
    num = len(nodelist)
    brackets_allowed = (
        macro_name is not None and _RULE_MACROS_WITH_OPTIONAL_ARGUMENT.get(macro_name, False)
    )
    resume_pos = None
    while index < num:
        node = nodelist[index]
        if node is None:
            index += 1
            continue
        if node.isNodeType(latexnodes_nodes.LatexGroupNode):
            index += 1
            continue
        if node.isNodeType(latexnodes_nodes.LatexCommentNode):
            index += 1
            continue
        if node.isNodeType(latexnodes_nodes.LatexCharsNode):
            s = node.chars
            stripped = s.strip()
            if not stripped:
                index += 1
                continue
            if brackets_allowed and stripped.startswith('['):
                close = stripped.find(']')
                if close != -1:
                    lead = len(s) - len(s.lstrip())
                    # the position of the first character after the bracket
                    resume_pos = node.pos + lead + close + 1
                    # if nothing but whitespace follows the bracket in this
                    # node, the whole node is the argument
                    if not s[lead + close + 1:].strip():
                        index += 1
                        resume_pos = None
                    return index, resume_pos
            if _looks_like_rule_argument_text(stripped, brackets_allowed):
                index += 1
                continue
        break
    return index, resume_pos


def _split_leading_text(nodelist, index, resume_pos):
    r"""
    Replace the characters node at `nodelist[index]` with a copy that starts at
    `resume_pos`, and return the new list.

    The node list that the walker produced is left untouched: only the shallow
    copy is changed.  This is what lets a rule's optional argument and the cell
    content that follows it be separated when the parser put both in the same
    node.
    """
    node = nodelist[index]
    if node is None or node.pos is None or resume_pos is None:
        return nodelist
    if resume_pos <= node.pos or resume_pos >= node.pos_end:
        return nodelist
    # build a copy of the list with a trimmed copy of the node
    new_node = node.__class__(
        chars=node.latex_walker.s[resume_pos:node.pos_end],
        parsing_state=node.parsing_state,
        pos=resume_pos,
        pos_end=node.pos_end,
        latex_walker=node.latex_walker,
    )
    new_list = list(nodelist)
    new_list[index] = new_node
    return new_list


def _looks_like_rule_argument_text(s, brackets_allowed):
    # `\cmidrule(lr)` leaves `(lr)` as characters, and `\toprule[1pt]` leaves
    # `[1pt]`.  Only these parenthesized or bracketed fragments are skipped.
    if not s:
        return False
    if s[0] == '(' and s[-1] == ')':
        return True
    if brackets_allowed and s[0] == '[' and s[-1] == ']':
        return True
    return False


# ------------------------------------------------------------------------------
#
# Reading the body into rows and cells.
#


class _RawCell(object):
    r"""
    A cell as it is read from the body, before its grid column is known.

    .. py:attribute:: nodes

       The nodes that make up the cell's content, in source order.  This
       excludes the rules, the decorations and the arguments of
       ``\multicolumn`` and ``\multirow``: only what belongs to the cell's
       value is collected.

    .. py:attribute:: colspan

       The width the cell declares through ``\multicolumn``, or one.

    .. py:attribute:: rowspan

       The height the cell declares through ``\multirow``, or one.

    .. py:attribute:: pos

       The start of the cell's source region, or `None` for a cell that was
       not written out.

    .. py:attribute:: pos_end

       The end of the cell's source region, or `None`.

    .. py:attribute:: written

       `True` if the cell was written out at all, even as an empty field
       between two separators.
    """

    def __init__(self, pos=None):
        self.nodes = []
        self.colspan = 1
        self.rowspan = 1
        # A cell starts out as "not written": between two separators there may
        # be no characters at all, and then the cell has no place in the source.
        # `pos` and `pos_end` are filled in as soon as there is something to
        # point at, either content or an explicitly written empty field.
        self.pos = None
        self.pos_end = None
        self.written = False
        self._start_pos = pos

    def add_node(self, node):
        self.nodes.append(node)
        if node.pos is not None:
            if self.pos is None or node.pos < self.pos:
                self.pos = node.pos
            if self.pos_end is None or node.pos_end > self.pos_end:
                self.pos_end = node.pos_end
            self.written = True

    def set_region(self, pos, pos_end):
        r"""
        Set the cell's source region without adding content.

        This is what an empty but written field uses: it has a place in the
        source and in the grid, but nothing between that place's delimiters.
        """
        self.pos = pos
        self.pos_end = pos_end
        self.written = True

    def set_empty(self):
        r"""
        Mark the cell as not written at all.

        Nothing was found between its separators, so the cell has no source
        position; it is still part of the grid, which is what makes a row have
        the right number of cells.
        """
        self.pos = None
        self.pos_end = None
        self.written = False

    def set_region_from_nodes(self):
        r"""
        Set the cell's source region to the extent of the nodes it holds.

        This is what a cell uses after its whitespace margins have been
        trimmed: the region then covers the value rather than the layout around
        it, and the empty field on either side of a value is not included.
        """
        if not self.nodes:
            return
        start = None
        end = None
        for node in self.nodes:
            if node.pos is None:
                continue
            if start is None or node.pos < start:
                start = node.pos
            if end is None or node.pos_end > end:
                end = node.pos_end
        if start is not None:
            self.pos = start
            self.pos_end = end

    def has_content(self):
        return len(self.nodes) > 0


class _RawRow(object):
    r"""
    A row as it is read from the body.

    `pos` is the position where the row starts, which is the end of the previous
    row separator or the start of the body.  `pos_end` is the position of the
    row separator that ended the row, or the end of the body for the last row.
    `complete` is `False` for a row that was cut short by a structure problem.
    """

    def __init__(self, pos, pos_end, cells, complete):
        self.pos = pos
        self.pos_end = pos_end
        self.cells = cells
        self.complete = complete


class _BodyReader(object):
    r"""
    Split a table body into rows of raw cells.

    Only the nodes directly in the body are looked at.  A nested environment is
    a single node there, so the separators it contains are invisible to this
    reader; the same is true, for the same reason, of a group or a macro
    argument.  The result is that only a separator at the table's own level
    splits a cell or ends a row.

    Rules and decorations say something about the appearance of the table, not
    about its contents.  They are treated as boundaries of the cell that
    surrounds them: they contribute neither text nor source to it, and the
    whitespace on either side of a rule is outside the cell's value.
    """

    def __init__(self):
        self.rows = []
        self.warnings = []

    def read(self, body, body_pos, body_end_pos, terminated):
        # The node list is copied so that this module can replace a node with a
        # trimmed copy when a rule's optional argument and the cell content that
        # follows it were put in the same characters node.  The walker's own
        # node list is never modified.
        nodelist = list(body) if body is not None else []
        row_start = body_pos
        cell_start = body_pos
        cells = []
        cell = _RawCell(pos=cell_start)
        index = 0
        num = len(nodelist)

        while index < num:
            node = nodelist[index]

            if node is None:
                index += 1
                continue

            # A paragraph break or a sectioning command is not table content.
            # It only appears here when the table was not terminated: the
            # walker had no `\end{tabular}` to stop at and kept reading.  The
            # rows that were already complete remain; this row and the rest of
            # the body are left out of the table.
            if _is_paragraph_specials(node) or _is_macro(node, _STRUCTURAL_MACROS):
                self.warnings.append(
                    "table body ran into following content at position {}; "
                    "the rows before it were kept".format(node.pos)
                )
                terminated = False
                break

            if _is_column_separator(node):
                self._finish_cell(cell, cell_start, node.pos)
                cells.append(cell)
                cell_start = node.pos_end
                cell = _RawCell(pos=cell_start)
                index += 1
                continue

            if _is_macro(node, _ROW_SEPARATOR_MACROS):
                self._finish_cell(cell, cell_start, node.pos)
                cells.append(cell)
                self.rows.append(_RawRow(row_start, node.pos, cells, True))
                row_start = node.pos_end
                cell_start = node.pos_end
                cells = []
                cell = _RawCell(pos=cell_start)
                index += 1
                continue

            if _is_macro(node, _RULE_MACROS) or _is_macro(node, _RULE_ARGUMENT_MACROS):
                next_index, resume_pos = _skip_following_arguments(
                    nodelist, index + 1, node.macroname
                )
                if resume_pos is not None:
                    nodelist = _split_leading_text(nodelist, next_index, resume_pos)
                # A rule is a boundary, not content: the cell that precedes it
                # ends at the rule, and the next cell starts after the rule's
                # arguments.  The whitespace before the rule is its margin and
                # is dropped from the cell.
                self._trim_cell(cell, hard=True)
                if not cell.has_content():
                    cell.set_empty()
                if next_index < len(nodelist):
                    next_node = nodelist[next_index]
                    if next_node is not None and next_node.pos is not None:
                        cell_start = next_node.pos
                    else:
                        cell_start = node.pos_end
                else:
                    cell_start = node.pos_end
                index = next_index
                continue

            if _is_macro(node, _DECORATION_MACROS):
                next_index, resume_pos = _skip_following_arguments(
                    nodelist, index + 1, node.macroname
                )
                if resume_pos is not None:
                    nodelist = _split_leading_text(nodelist, next_index, resume_pos)
                # the decoration is not content, but the whitespace around it
                # belongs to the cell all the same
                index = next_index
                continue

            if _is_macro(node, _DECLARED_NO_ARGUMENT_MACROS_WITH_GROUPS) \
               and node.macroname == 'multicolumn':
                groups, next_index = _collect_group_arguments(nodelist, index + 1, 3)
                if groups is not None:
                    width_node, _spec_node, content_node = groups
                    new_cell = _RawCell()
                    new_cell.colspan = _read_span(width_node) or 1
                    _add_content_nodes(new_cell, content_node.nodelist)
                    if new_cell.has_content():
                        new_cell.set_region_from_nodes()
                        # the content group's braces are not part of the value,
                        # and neither is the whitespace just inside them
                        _trim_source_region(new_cell)
                    else:
                        # an explicitly empty multicolumn still has a place in
                        # the grid; its region is the empty content group
                        new_cell.set_region(content_node.pos, content_node.pos_end)
                    # The multicolumn is the cell: the whitespace that precedes
                    # it separates the `\multicolumn` from the cell boundary and
                    # is not part of the value.
                    cell = new_cell
                    cell_start = new_cell.pos
                    index = next_index
                    continue
                cell.add_node(node)
                index += 1
                continue

            if _is_macro(node, _DECLARED_NO_ARGUMENT_MACROS_WITH_GROUPS) \
               and node.macroname == 'multirow':
                groups, next_index = _collect_group_arguments(nodelist, index + 1, 3)
                if groups is not None:
                    height_node, _width_node, content_node = groups
                    cell.rowspan = _read_span(height_node) or 1
                    _add_content_nodes(cell, content_node.nodelist)
                    index = next_index
                    continue
                cell.add_node(node)
                index += 1
                continue

            cell.add_node(node)
            index += 1

        # the last row is closed by the end of the body.  Its trailing
        # whitespace is the layout before the table's `\end{...}`, not a value;
        # dropping it is what keeps a final row separator from producing a row
        # that the author never wrote.
        self._trim_cell(cell, hard=True)
        if not cell.has_content():
            cell.set_empty()
        cells.append(cell)
        self.rows.append(_RawRow(row_start, body_end_pos, cells, terminated))
        self._drop_trailing_empty_row()
        return self.rows

    def _finish_cell(self, cell, start, end):
        r"""
        Close a cell whose source region runs from `start` to `end`.

        A cell that holds content has the region that its content occupies.  A
        cell with no content is an empty field: if the field has whitespace
        written in it, the whitespace is the cell's source and it is reported
        as a whitespace-only cell; if the field has nothing at all, the cell
        is empty and has no source position, which keeps it distinct from
        the whitespace field.
        """
        if cell.has_content():
            self._trim_cell(cell)
            if cell.has_content():
                # `_trim_cell()` already narrowed the region to the value
                return
            # only whitespace: the field was written, and the whitespace is
            # what it holds
            cell.set_region(start, end)
            return
        cell.set_empty()

    def _trim_cell(self, cell, hard=False):
        r"""
        Remove the whitespace at the two ends of a cell's content.

        A cell's value is the text between its separators, without the
        indentation and line breaks that the table's source uses to lay the
        cells out.  The cell's source region is narrowed to match: it covers the
        value, not the layout around it, so that a caller who cuts the region
        out of the source gets exactly the cell's content.

        A cell whose content is only whitespace keeps both its content and its
        region, so that it remains distinguishable from a cell that was not
        written at all.  With `hard=True`, which the caller uses when a rule
        follows, that whitespace is dropped as well: the whitespace before a
        rule is the rule's margin, not the value of the cell.
        """
        if not cell.nodes:
            return
        first = 0
        last = len(cell.nodes) - 1
        while first <= last and _node_is_whitespace_only(cell.nodes[first]):
            first += 1
        while last >= first and _node_is_whitespace_only(cell.nodes[last]):
            last -= 1
        if first > last:
            if hard:
                cell.nodes = []
            # else: only whitespace, keep it so that the cell's text reports it
            return
        cell.nodes = cell.nodes[first:last + 1]
        cell.set_region_from_nodes()
        _trim_source_region(cell, hard=hard)
        if cell.pos is not None:
            cell.written = True

    def _drop_trailing_empty_row(self):
        # The last row is added unconditionally.  When the body ends with a row
        # separator, that row holds no cell content: it is the fields that follow
        # the last separator, and nothing was written between them.  A row whose
        # only cell is a written field of whitespace is a real row, though: the
        # author wrote it, and dropping it would lose a row of the table.
        if not self.rows:
            return
        last = self.rows[-1]
        if len(last.cells) != 1:
            return
        cell = last.cells[0]
        if cell.has_content():
            return
        if cell.written:
            return
        self.rows.pop()


def _trim_source_region(cell, hard=False):
    r"""
    Narrow a cell's region past the whitespace that its first and last
    characters nodes hold.

    A cell's content is a sequence of nodes, and the first and last of them can
    be characters nodes that begin or end with whitespace.  The whitespace is
    part of the node, so trimming it means moving the region, not dropping
    nodes.  A cell whose every character node is whitespace keeps its region:
    it was written, and the whitespace is what it holds.
    """
    if cell.pos is None or cell.pos_end is None:
        return
    walker = _cell_walker(cell)
    if walker is None:
        return
    s = walker.s
    start = cell.pos
    end = cell.pos_end
    if start >= end:
        return
    new_start = start
    while new_start < end and s[new_start].isspace():
        new_start += 1
    new_end = end
    while new_end > new_start and s[new_end - 1].isspace():
        new_end -= 1
    if new_start >= new_end:
        if hard:
            cell.pos = None
            cell.pos_end = None
            cell.written = False
        return
    cell.pos = new_start
    cell.pos_end = new_end


def _cell_walker(cell):
    for node in cell.nodes:
        walker = getattr(node, 'latex_walker', None)
        if walker is not None:
            return walker
    return None


def _node_is_whitespace_only(node):
    if node is None:
        return True
    if node.isNodeType(latexnodes_nodes.LatexCharsNode):
        return not node.chars.strip()
    return False


def _add_content_nodes(cell, content_nodelist):
    r"""
    Add the nodes of a ``\multicolumn`` or ``\multirow`` content group to a cell.

    The content group can itself hold a ``\multirow`` (a multicolumn whose
    content is a multirow), whose arguments would otherwise leak into the cell
    as separate group nodes.  Reading it here keeps the cell's value to the
    multirow's content only.
    """
    if content_nodelist is None:
        return
    nodelist = list(content_nodelist)
    index = 0
    num = len(nodelist)
    while index < num:
        node = nodelist[index]
        if node is not None and _is_macro(node, _DECLARED_NO_ARGUMENT_MACROS_WITH_GROUPS) \
           and node.macroname == 'multirow':
            groups, next_index = _collect_group_arguments(nodelist, index + 1, 3)
            if groups is not None:
                height_node, _width_node, content_node = groups
                cell.rowspan = max(cell.rowspan, _read_span(height_node) or 1)
                _add_content_nodes(cell, content_node.nodelist)
                index = next_index
                continue
        if node is not None:
            cell.add_node(node)
        index += 1


def _check_terminated(envnode, body_pos_end):
    r"""
    Return `True` if the table's ``\end{...}`` follows its body.

    An environment whose ``\end{...}`` is missing has a body that keeps reading
    until the enclosing construct ends.  The body's end is then not followed by
    the table's own end delimiter, which is what this checks.  Whitespace is
    allowed between ``\end`` and the opening brace, as LaTeX allows it.
    """
    walker = envnode.latex_walker
    if walker is None or body_pos_end is None:
        # without the source text the only safe answer is "we don't know"; the
        # body's end is taken as the end of the table
        return True
    s = walker.s
    pos = body_pos_end
    num = len(s)
    while pos < num and s[pos].isspace():
        pos += 1
    if not s.startswith('\\end', pos):
        return False
    pos += len('\\end')
    while pos < num and s[pos].isspace():
        pos += 1
    if pos >= num or s[pos] != '{':
        return False
    close = s.find('}', pos)
    if close == -1:
        return False
    name = s[pos + 1:close].strip()
    return name == envnode.environmentname


# ------------------------------------------------------------------------------
#
# Turning the raw rows into a grid.
#


def _cell_text(nodes, l2tobj):
    if not nodes:
        return ''
    text = l2tobj.nodelist_to_text(nodes)
    # `nodelist_to_text` may return a math piece in the 'fancy' math mode
    text = str(text)
    return text.strip()


def _cell_source(nodes, cell_pos, cell_pos_end, walker):
    if walker is None or cell_pos is None or cell_pos_end is None:
        return ''
    return walker.s[cell_pos:cell_pos_end]


def _raw_cell_is_whitespace(raw_cell):
    r"""
    `True` if a raw cell holds only whitespace characters.

    The check is made on the nodes and not on the source text, so that it does
    not need the walker.  A cell whose nodes are characters nodes that are all
    whitespace is a whitespace cell; a cell that also holds a comment is not,
    because the comment is something the author wrote.
    """
    if not raw_cell.nodes:
        return False
    for node in raw_cell.nodes:
        if not _node_is_whitespace_only(node):
            return False
    return True


def _raw_cell_is_blank(raw_cell):
    r"""
    `True` if a raw cell holds nothing that could be a value of its own: no
    nodes, or only whitespace.
    """
    if raw_cell.has_content():
        for node in raw_cell.nodes:
            if not _node_is_whitespace_only(node):
                return False
        return True
    return True


def _build_grid(raw_rows, declared_column_count, l2tobj, walker):
    r"""
    Assign each raw cell its grid position and produce the public rows and cells.

    The assignment walks the rows in order and keeps a cursor per row: a cell
    is placed at the first free column of its row, and it occupies as many
    columns as its `colspan` says.  A `multirow` occupies the same columns in
    the following rows, so those rows' cursors skip them.

    Returns ``(rows, cells, column_count, warnings)``.
    """
    # `occupied` marks, for each row index, the grid columns already taken by a
    # cell that spans into that row.  The list grows as rows are added.
    occupied = []
    warnings = []

    for row_index in range(len(raw_rows)):
        occupied.append({})

    built_rows = []
    built_cells = []
    max_column = 0

    for row_index, raw_row in enumerate(raw_rows):
        cursor = 0
        row_cells = []
        for raw_cell in raw_row.cells:
            # A row that continues a `\multirow` still writes out the fields
            # that the multirow covers, as empty fields: ` & c \\` after a cell
            # that spans the first column.  Such a field is not a cell of its
            # own; it stands for the part of the spanning cell that reaches into
            # this row.  Recognize it by the position being taken by a cell from
            # above and the field being blank.
            if occupied[row_index].get(cursor, False) and _raw_cell_is_blank(raw_cell):
                # consume the placeholder; the spanning cell is already there
                while occupied[row_index].get(cursor, False):
                    cursor += 1
                continue

            # skip positions that a cell from an earlier row already occupies
            while occupied[row_index].get(cursor, False):
                cursor += 1
            column = cursor
            colspan = max(1, raw_cell.colspan)
            rowspan = max(1, raw_cell.rowspan)

            if row_index + rowspan > len(raw_rows):
                warnings.append(
                    "rowspan of cell at row {} reaches past the last row; "
                    "clamped to the table".format(row_index)
                )
                rowspan = len(raw_rows) - row_index

            for r in range(row_index, min(row_index + rowspan, len(raw_rows))):
                for c in range(column, column + colspan):
                    occupied[r][c] = True

            cursor = column + colspan
            max_column = max(max_column, column + colspan)

            source = _cell_source(raw_cell.nodes, raw_cell.pos, raw_cell.pos_end,
                                  walker)
            text = _cell_text(raw_cell.nodes, l2tobj)

            cell = TableCell(
                row=row_index,
                column=column,
                rowspan=rowspan,
                colspan=colspan,
                text=text,
                source=source,
                pos=raw_cell.pos,
                pos_end=raw_cell.pos_end,
            )

            if not raw_cell.has_content() and not raw_cell.written:
                # nothing was written between the separators
                cell.empty = True
                cell.pos = None
                cell.pos_end = None
                cell.source = ''
                cell.text = ''
            elif not raw_cell.has_content():
                # a field that was written as whitespace only
                cell.whitespace_only = True
                cell.text = ''
            elif _raw_cell_is_whitespace(raw_cell):
                cell.whitespace_only = True
                cell.text = ''
            else:
                cell.text = text

            if cell.pos is not None and walker is not None:
                lineno, colno = walker.pos_to_lineno_colno(cell.pos)
                cell.lineno = lineno
                cell.colno = colno

            row_cells.append(cell)
            built_cells.append(cell)

        # A row's position is where its content starts, so that it points at
        # something a reader can look at.  The raw row's `pos` is the end of
        # the previous row separator, which can be several blank lines above
        # the first cell.
        row_pos = None
        for cell in row_cells:
            if cell.pos is not None:
                row_pos = cell.pos
                break
        row_pos_end = raw_row.pos_end
        lineno = colno = None
        if row_pos is not None and walker is not None:
            lineno, colno = walker.pos_to_lineno_colno(row_pos)
        built_rows.append(TableRow(
            index=row_index,
            cells=row_cells,
            pos=row_pos,
            pos_end=row_pos_end,
            lineno=lineno,
            colno=colno,
            complete=raw_row.complete,
        ))

    column_count = max_column
    if declared_column_count is not None:
        column_count = max(declared_column_count, max_column)

    return built_rows, built_cells, column_count, warnings


# ------------------------------------------------------------------------------
#
# The public entry point.
#


def extract_table(envnode, l2tobj=None):
    r"""
    Extract the grid of a table environment, given its
    :py:class:`~pylatexenc.latexnodes.nodes.LatexEnvironmentNode`.

    The `l2tobj` argument is the :py:class:`~pylatexenc.latex2text.LatexNodes2Text`
    object used to turn each cell's nodes into plain text; if it is `None`, a
    default converter is created.  Passing the converter that is already in use
    is what makes the cell text follow the same options as the surrounding
    conversion.

    The environment must be one of the tabular-like environments that this
    module knows how to read: ``tabular``, ``tabular*``, ``tabularx``,
    ``xltabular``, ``array`` and the matrix environments.  A different
    environment raises :py:exc:`ValueError`, because its contents are not
    necessarily a grid of cells.

    Returns an :py:class:`ExtractedTable`.

    The extraction never raises on malformed table structure: a missing
    ``\end{...}``, a row that is wider than the declared columns, or a spanning
    cell that reaches past the table are reported through the table's
    `warnings` and through the `terminated` and `complete` flags, while the
    cells that could be established are still returned.

    .. versionadded:: 3.0
    """
    if envnode is None or not envnode.isNodeType(latexnodes_nodes.LatexEnvironmentNode):
        raise TypeError("extract_table() expects a LatexEnvironmentNode, got {!r}"
                        .format(envnode))

    name = envnode.environmentname
    if name not in _TABLE_ENVIRONMENTS and name not in _MATRIX_ENVIRONMENTS:
        raise ValueError(
            "Environment ‘{}’ is not a table environment that can be extracted; "
            "expected one of {}".format(
                name,
                ", ".join(sorted(list(_TABLE_ENVIRONMENTS.keys())
                                 + list(_MATRIX_ENVIRONMENTS.keys())))
            )
        )

    if l2tobj is None:
        from . import LatexNodes2Text
        l2tobj = LatexNodes2Text()

    walker = envnode.latex_walker

    body = envnode.nodelist
    body_pos = body.pos if body is not None else None
    body_pos_end = body.pos_end if body is not None else None

    declared_column_count, _arg_index = _read_declared_columns(envnode)

    terminated = _check_terminated(envnode, body_pos_end)

    reader = _BodyReader()
    raw_rows = reader.read(body, body_pos, body_pos_end, terminated)

    built_rows, built_cells, column_count, grid_warnings = _build_grid(
        raw_rows, declared_column_count, l2tobj, walker
    )

    warnings = list(reader.warnings) + list(grid_warnings)
    if not terminated:
        warnings.append(
            "the table's \\end{{{}}} was not found; only the rows that could be "
            "established are reported".format(name)
        )

    pos_end = envnode.pos_end if terminated else None
    pos = envnode.pos
    lineno = colno = None
    if pos is not None and walker is not None:
        lineno, colno = walker.pos_to_lineno_colno(pos)

    return ExtractedTable(
        environmentname=name,
        column_count=column_count,
        declared_column_count=declared_column_count,
        rows=built_rows,
        cells=built_cells,
        pos=pos,
        pos_end=pos_end,
        lineno=lineno,
        colno=colno,
        body_pos=body_pos,
        body_pos_end=body_pos_end,
        terminated=terminated,
        warnings=warnings,
    )


def extract_tables(latex, latex_context=None, latex_walker_init_args=None,
                   **parse_flags):
    r"""
    Parse `latex` and extract every tabular-like table it contains, including
    the ones nested in other environments, groups and macro arguments.

    This is a convenience wrapper that parses the given code and then calls
    :py:func:`extract_tables_from_nodelist()`.  The `latex_context` is the
    :py:class:`~pylatexenc.macrospec.LatexContextDb` to parse with, and the
    `parse_flags` are passed on to the
    :py:class:`~pylatexenc.latexwalker.LatexWalker` constructor.

    .. versionadded:: 3.0
    """
    from .. import latexwalker
    from ..latexnodes import parsers as latexnodes_parsers

    if latex_walker_init_args is not None:
        parse_flags = dict(latex_walker_init_args, **parse_flags)
    if latex_context is not None:
        parse_flags['latex_context'] = latex_context

    lw = latexwalker.LatexWalker(latex, **parse_flags)
    nodelist, _ = lw.parse_content(latexnodes_parsers.LatexGeneralNodesParser())
    return extract_tables_from_nodelist(nodelist)


def extract_tables_from_nodelist(nodelist, l2tobj=None):
    r"""
    Find the tabular-like environments in `nodelist`, including the ones nested
    in other environments, groups and macro arguments, and extract each of them.

    The tables are returned in source order.  A nested table appears in the
    result in its own right, and also as a single node of its parent table;
    the two extractions are independent, and the nested table's separators do
    not affect the parent's grid.

    .. versionadded:: 3.0
    """
    if nodelist is None:
        return []

    if l2tobj is None:
        from . import LatexNodes2Text
        l2tobj = LatexNodes2Text()

    tables = []
    _collect_tables(nodelist, l2tobj, tables)
    tables.sort(key=lambda t: (t.pos is None, t.pos))
    return tables


def _collect_tables(nodelist, l2tobj, tables):
    for node in nodelist:
        if node is None:
            continue
        if node.isNodeType(latexnodes_nodes.LatexEnvironmentNode):
            if node.environmentname in _TABLE_ENVIRONMENTS \
               or node.environmentname in _MATRIX_ENVIRONMENTS:
                tables.append(extract_table(node, l2tobj))
            if node.nodelist is not None:
                _collect_tables(node.nodelist, l2tobj, tables)
            continue
        if node.isNodeType(latexnodes_nodes.LatexGroupNode):
            if node.nodelist is not None:
                _collect_tables(node.nodelist, l2tobj, tables)
            continue
        if node.isNodeType(latexnodes_nodes.LatexMacroNode):
            nodeargd = getattr(node, 'nodeargd', None)
            if nodeargd is not None and nodeargd.argnlist:
                for arg in nodeargd.argnlist:
                    if arg is None:
                        continue
                    if arg.isNodeType(latexnodes_nodes.LatexGroupNode) \
                       and arg.nodelist is not None:
                        _collect_tables(arg.nodelist, l2tobj, tables)
            continue
        if node.isNodeType(latexnodes_nodes.LatexMathNode):
            if node.nodelist is not None:
                _collect_tables(node.nodelist, l2tobj, tables)
            continue


__all__ = [
    'TableCell',
    'TableRow',
    'ExtractedTable',
    'extract_table',
    'extract_tables',
    'extract_tables_from_nodelist',
]
