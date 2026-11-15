# -*- coding: utf-8 -*-
#
# The MIT License (MIT)
#
# Copyright (c) 2022 Philippe Faist
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


# Internal module. Internal API may move, disappear or otherwise change at any
# time and without notice.

import logging
logger = logging.getLogger(__name__)

from ..latexnodes._exctypes import (
    LatexWalkerError,
    LatexWalkerParseError,
)
from ..latexnodes.nodes import (
    LatexNode,
    LatexNodeList,
)


# Error types that mean that the input ended while a construct was still open.
# Only for these errors does the strict parse carry a recovery node list from
# which a trusted prefix can be taken.
_incomplete_error_types = (
    'verbatim_unexpected_end_of_stream',
    'token_end_of_stream_immediately_after_escape_character',
    'nodes_generalnodes_required_stop_condition_not_met',
)


class LatexWalkerPartialParseResult(object):
    r"""
    The result of :py:meth:`LatexWalker.parse_partial_content()
    <pylatexenc.latexwalker.LatexWalker.parse_partial_content>`.

    The trusted part and the tolerant part are kept in separate attributes:
    :py:attr:`trusted_nodes` always comes from a *strict* parse, whereas
    :py:attr:`tolerant_nodes` is the result of parsing the same input in
    tolerant mode and is offered only as a convenience.  In particular, no
    node of a construct that only the tolerant parse could recover is ever
    reported as trusted.

    .. py:attribute:: s

       The string that was parsed.

    .. py:attribute:: complete

       `True` if the entire string parsed successfully in strict mode.  In
       that case, :py:attr:`trusted_nodes` is the complete node list,
       :py:attr:`incomplete_range` is `None`, and
       :py:attr:`tolerant_nodes` is `None` as well.

    .. py:attribute:: trusted_nodes

       A :py:class:`~pylatexenc.latexnodes.nodes.LatexNodeList` with the
       complete top-level constructs that parsed successfully in strict mode
       and that end before the incomplete part of the input.

       The nodes are contiguous and start at position 0, so their combined
       text is exactly the prefix ``s[:trusted_range[1]]`` of the input.  This
       remains true after the incomplete part is completed and the whole
       string is parsed again.

       Nothing from inside an unterminated construct is included.  For an
       unterminated verbatim environment in particular, the contents are not
       part of :py:attr:`trusted_nodes`; whatever backslashes they contain are
       literal text and are never reported as macros there.

    .. py:attribute:: trusted_range

       The 2-item tuple ``(0, trusted_end)`` describing the part of the input
       covered by :py:attr:`trusted_nodes`.  It is ``(0, len(s))`` for a
       complete parse.

    .. py:attribute:: incomplete_range

       The 2-item tuple ``(trusted_end, len(s))`` describing the part of the
       input that could not be parsed completely, or `None` if the input is
       complete.

    .. py:attribute:: incomplete_latex

       The source text of the incomplete range, or `None` if the input is
       complete.

    .. py:attribute:: parsing_state_delta

       The parsing state delta returned by the strict parse, if the input is
       complete.  It is `None` for incomplete input, because parsing state
       changes caused by the incomplete tail cannot be trusted.

    .. py:attribute:: strict_error

       The :py:exc:`~pylatexenc.latexnodes.LatexWalkerParseError` that ended
       the strict parse, or `None` if the input is complete.  This attribute
       is what identifies the truncation; it is reported as-is and is not
       replaced by the tolerant parse's behavior.

    .. py:attribute:: tolerant_nodes

       The node list obtained by parsing the same input in tolerant mode, or
       `None` if the input is complete.  This is kept strictly separate from
       :py:attr:`trusted_nodes`.

    .. py:attribute:: tolerant_error

       A parse error raised by the tolerant parse, or `None`.  Tolerant
       parsing is designed not to raise on truncated input, but a custom latex
       context or parsing state might still cause an error.

    .. versionadded:: 3.0

       The :py:class:`LatexWalkerPartialParseResult` class was introduced in
       `pylatexenc 3`.
    """
    def __init__(self, s, trusted_nodes, complete, parsing_state_delta=None,
                 incomplete_range=None, strict_error=None,
                 tolerant_nodes=None, tolerant_error=None):
        self.s = s
        self.trusted_nodes = trusted_nodes
        self.complete = complete
        self.parsing_state_delta = parsing_state_delta
        self.incomplete_range = incomplete_range
        self.strict_error = strict_error
        self.tolerant_nodes = tolerant_nodes
        self.tolerant_error = tolerant_error

    @property
    def trusted_range(self):
        r"""
        The 2-item tuple ``(0, trusted_end)`` covered by
        :py:attr:`trusted_nodes`.
        """
        return (0, self.trusted_nodes.pos_end)

    @property
    def incomplete_latex(self):
        r"""
        The source text that is incomplete, or `None` if the input is
        complete.
        """
        if self.incomplete_range is None:
            return None
        return self.s[self.incomplete_range[0]:self.incomplete_range[1]]

    def __repr__(self):
        return (
            '<LatexWalkerPartialParseResult {} trusted=({}, {}) incomplete={}>'
            .format(
                'complete' if self.complete else 'incomplete',
                self.trusted_range[0],
                self.trusted_range[1],
                self.incomplete_range,
            )
        )


def _error_type_info(exc):
    info = getattr(exc, 'error_type_info', None)
    if info is None:
        return {}
    return info


def _has_following_source_text(exc, s):
    r"""
    Return `True` if there is ordinary source text after the offending
    closing token reported by `exc`, as in ``"$x } y"``.

    A closing token at the very end of the input, possibly followed only by
    whitespace, may simply be the latest thing that was written and can belong
    to an unfinished inner construct.  Ordinary text after it, on the other
    hand, means the document went on with a mismatched delimiter.
    """
    pos = exc.pos
    if pos is None or pos >= len(s):
        return False
    return bool(s[pos+1:].strip())


def _is_incomplete_error(exc, s):
    r"""
    Return `True` if the strict parse error `exc` on the string `s` is
    consistent with the input ending while a construct was still open.

    Any other error is a genuine syntax error and must not be treated as a
    truncation point.
    """
    if not isinstance(exc, LatexWalkerParseError):
        return False

    info = _error_type_info(exc)
    what = info.get('what', None)

    if what in _incomplete_error_types:
        return True

    if what == 'expression_required_got_unexpected' \
       and info.get('unexpected', None) == 'end_of_stream':
        # a required expression was expected right at the end of the input
        return True

    if what == 'nodes_generalnodes_required_stop_condition_not_met':
        # a group, math mode or environment never got its closing delimiter.
        # A closing environment with the wrong name is reported through the
        # dedicated 'nodes_unexpected_end_environment' error instead, so this
        # error really does mean that the input ended.
        return True

    if what in (
            'nodes_unexpected_closing_group_delimiter',
            'nodes_unexpected_end_environment',
            'nodes_unexpected_closing_math_delimiter',
    ):
        # a closing delimiter (or closing math-mode delimiter) appeared while
        # a construct was open.  With ordinary source text after it, the
        # document is malformed rather than merely cut short.  Without one, the
        # construct may simply be an unfinished inner group, so treat it like
        # any other unfinished construct.
        return not _has_following_source_text(exc, s)

    if what == 'nodes_delimited_expected_opening_delimiter_not_found' \
       and info.get('first_tokens', None) is None:
        # no token at all could be read where an opening delimiter was
        # required, i.e. the input ended there
        return True

    if what == 'token_error_parse_beginend_environment_name':
        # '\begin' or '\end' was read, but no complete '{environmentname}'
        # followed.  That is a truncation only if the input ends before the
        # closing brace.
        pos = exc.pos
        if pos is None:
            return False
        code = info.get('macro_beginend', None)
        if code is None:
            code = s[pos:pos+1+len(info.get('beginend', ''))]
        rest = s[pos+len(code):]
        brace = rest.find('{')
        if brace < 0:
            # nothing but whitespace after '\begin'/'\end': the environment
            # name is missing, and appending text could supply it
            return rest.strip() == ''
        return '}' not in rest[brace:]

    return False


def _recovery_node_items(recovery_nodes):
    if recovery_nodes is None:
        return []
    if isinstance(recovery_nodes, LatexNodeList):
        return list(recovery_nodes.nodelist)
    if isinstance(recovery_nodes, LatexNode):
        return [ recovery_nodes ]
    return list(recovery_nodes)


def _contiguous_trusted_prefix(recovery_nodes, s):
    r"""
    Keep the top-level nodes of `recovery_nodes` that are complete and that
    form a contiguous run starting at position 0.  Return the list of nodes
    and the position immediately after the last one.
    """
    kept = []
    pos = 0
    for node in _recovery_node_items(recovery_nodes):
        if node is None:
            break
        if node.pos != pos:
            break
        if node.pos_end is None or node.pos_end < pos or node.pos_end > len(s):
            break
        kept.append(node)
        pos = node.pos_end
    return kept, pos


def parse_partial_content(latex_walker):
    r"""
    Implementation of :py:meth:`LatexWalker.parse_partial_content()
    <pylatexenc.latexwalker.LatexWalker.parse_partial_content>`; refer to the
    documentation of that method.
    """
    s = latex_walker.s
    saved_tolerant_parsing = latex_walker.tolerant_parsing

    # --- 1. strict parse; this defines what is trusted ---

    strict_nodes = None
    strict_parsing_state_delta = None
    strict_error = None

    latex_walker.tolerant_parsing = False
    try:
        strict_nodes, strict_parsing_state_delta = latex_walker.parse_content()
    except LatexWalkerParseError as exc:
        strict_error = exc
    finally:
        latex_walker.tolerant_parsing = saved_tolerant_parsing

    if strict_error is None and strict_nodes is not None:
        # the whole input parsed strictly
        return LatexWalkerPartialParseResult(
            s=s,
            trusted_nodes=latex_walker.make_nodelist(
                list(strict_nodes),
                parsing_state=latex_walker.make_parsing_state(),
                pos=0,
                pos_end=len(s),
            ),
            complete=True,
            parsing_state_delta=strict_parsing_state_delta,
        )

    if strict_error is not None and not _is_incomplete_error(strict_error, s):
        # a genuine syntax error: report it, exactly like the strict parse
        raise strict_error

    # --- 2. derive the trusted prefix from the strict parse's recovery nodes ---

    if strict_error is None:
        # parse_content() swallowed an end-of-stream and returned no nodes;
        # nothing could be recovered from this parse
        recovery_nodes = None
    else:
        recovery_nodes = getattr(strict_error, 'recovery_nodes', None)

    kept, trusted_end = _contiguous_trusted_prefix(recovery_nodes, s)
    incomplete_range = (trusted_end, len(s))

    # --- 3. tolerant parse, reported separately ---

    tolerant_nodes = None
    tolerant_error = None

    latex_walker.tolerant_parsing = True
    try:
        tolerant_nodes, _ = latex_walker.parse_content()
    except LatexWalkerError as exc:
        tolerant_error = exc
    finally:
        latex_walker.tolerant_parsing = saved_tolerant_parsing

    return LatexWalkerPartialParseResult(
        s=s,
        trusted_nodes=latex_walker.make_nodelist(
            kept,
            parsing_state=latex_walker.make_parsing_state(),
            pos=0,
            pos_end=trusted_end,
        ),
        complete=False,
        incomplete_range=incomplete_range,
        strict_error=strict_error,
        tolerant_nodes=tolerant_nodes,
        tolerant_error=tolerant_error,
    )
