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


# Internal module. Internal API may move, disappear or otherwise change at any
# time and without notice.


class LatexIncompleteRange(object):
    r"""
    The source range of the part of a LaTeX document that could not be parsed
    completely.

    The range is half-open: it covers the characters `source[pos:pos_end]`.
    The `source` attribute holds exactly those characters.  The range can be
    empty, which means that the document was parsed completely.

    .. py:attribute:: pos

       The character position at which the incomplete part starts.

    .. py:attribute:: pos_end

       The character position immediately after the incomplete part.  This is
       normally the end of the input string.

    .. py:attribute:: source

       The exact source text in this range.

    .. py:attribute:: len

       (Read-only attribute.)  The number of characters in this range.

    .. versionadded:: 3.0
    """

    def __init__(self, pos, pos_end, source=None):
        super(LatexIncompleteRange, self).__init__()
        self.pos = pos
        self.pos_end = pos_end
        self.source = source

    @property
    def len(self):
        if self.pos is None or self.pos_end is None:
            return None
        return self.pos_end - self.pos

    @property
    def start(self):
        r"""
        An alias for :py:attr:`pos`.
        """
        return self.pos

    @property
    def end(self):
        r"""
        An alias for :py:attr:`pos_end`.
        """
        return self.pos_end

    @property
    def text(self):
        r"""
        An alias for :py:attr:`source`.
        """
        return self.source

    @property
    def is_empty(self):
        r"""
        Whether this range contains no characters.
        """
        return self.pos == self.pos_end

    def __bool__(self):
        return not self.is_empty

    def __iter__(self):
        return iter((self.pos, self.pos_end,))

    def __eq__(self, other):
        if isinstance(other, LatexIncompleteRange):
            return self.pos == other.pos and self.pos_end == other.pos_end
        if isinstance(other, tuple):
            return (self.pos, self.pos_end,) == other
        return False

    __hash__ = None

    def __repr__(self):
        return "LatexIncompleteRange(pos={!r}, pos_end={!r}, source={!r})".format(
            self.pos,
            self.pos_end,
            self.source,
        )



class LatexPartialParseResult(object):
    r"""
    The result of a partial parse, see
    :py:meth:`pylatexenc.latexwalker.LatexWalker.parse_partial()`.

    The result deliberately keeps the strict and tolerant parses apart:

    - `nodelist` (also available as `trusted_nodes`) contains only complete
      top-level nodes obtained by strict parsing.  Every node in this list has a
      source span that is fully contained in the trusted prefix, and its
      `latex_verbatim()` is the exact original source text.

    - `tolerant_nodelist` (also available as `tolerant_nodes`) is the result of
      a separate tolerant parse of the entire input.  It is useful to inspect
      what the input might mean, but its recovered nodes are not part of the
      trusted prefix and must not be mixed with `trusted_nodes`.

    - `incomplete_range` gives the exact source range that was not consumed by
      the trusted prefix.  For a complete document, this range is empty and
      located at the end of the input.

    .. py:attribute:: nodelist

       The strict, trusted top-level nodes.

    .. py:attribute:: incomplete_range

       The source range that was not part of the trusted prefix.

    .. py:attribute:: tolerant_nodelist

       The separate tolerant parse result, or `None` if tolerant parsing was
       not requested or failed.

    .. py:attribute:: strict_error

       The strict parse error that terminated the trusted prefix, or `None` if
       the input was parsed completely.

    .. py:attribute:: tolerant_error

       The error raised by the tolerant parse, if any.

    .. py:attribute:: source

       The full input string.

    .. versionadded:: 3.0
    """

    def __init__(self, nodelist, incomplete_range,
                 tolerant_nodelist=None, strict_error=None,
                 tolerant_error=None, source=None, start_pos=0):
        super(LatexPartialParseResult, self).__init__()
        self.nodelist = nodelist
        self.incomplete_range = incomplete_range
        self.tolerant_nodelist = tolerant_nodelist
        self.strict_error = strict_error
        self.tolerant_error = tolerant_error
        self.source = source
        self.start_pos = start_pos

    @property
    def trusted_nodes(self):
        r"""
        An alias for :py:attr:`nodelist`, emphasizing that these nodes come
        from the strict parse and are safe to use as the readable prefix.
        """
        return self.nodelist

    @property
    def strict_nodes(self):
        r"""
        An alias for :py:attr:`nodelist`.
        """
        return self.nodelist

    @property
    def tolerant_nodes(self):
        r"""
        An alias for :py:attr:`tolerant_nodelist`.
        """
        return self.tolerant_nodelist

    @property
    def incomplete_source(self):
        r"""
        The exact source text in :py:attr:`incomplete_range`.
        """
        return self.incomplete_range.source

    @property
    def trusted_source(self):
        r"""
        The exact source text that corresponds to :py:attr:`trusted_nodes`.

        This is the original string from :py:attr:`start_pos` up to the start
        of the incomplete range.
        """
        if self.source is None:
            return None
        return self.source[self.start_pos:self.incomplete_range.pos]

    @property
    def is_complete(self):
        r"""
        Whether the whole input was parsed by the strict parse.
        """
        return self.strict_error is None and self.incomplete_range.is_empty

    @property
    def complete(self):
        r"""
        An alias for :py:attr:`is_complete`.
        """
        return self.is_complete

    @property
    def incomplete(self):
        r"""
        The opposite of :py:attr:`is_complete`.
        """
        return not self.is_complete

    def __iter__(self):
        # Convenient tuple-style unpacking:
        #     trusted_nodes, incomplete_range = result
        return iter((self.nodelist, self.incomplete_range,))

    def __len__(self):
        return 2

    def __getitem__(self, index):
        return (self.nodelist, self.incomplete_range,)[index]

    def __repr__(self):
        return (
            "LatexPartialParseResult(nodelist={!r}, incomplete_range={!r}, "
            "strict_error={!r})".format(
                self.nodelist,
                self.incomplete_range,
                self.strict_error,
            )
        )


__all__ = [
    'LatexIncompleteRange',
    'LatexPartialParseResult',
]
