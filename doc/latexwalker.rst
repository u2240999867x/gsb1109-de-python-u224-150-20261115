`latexwalker` — Calling Parsers for LaTeX Code
----------------------------------------------

.. automodule:: pylatexenc.latexwalker
   :no-undoc-members:
   :show-inheritance:

.. contents:: Contents:
   :local:


The main `LatexWalker` class
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: pylatexenc.latexwalker.LatexWalker
   :members:


Parsing incomplete input
~~~~~~~~~~~~~~~~~~~~~~~~

When the LaTeX code is still being written or uploaded, its end may fall in
the middle of a macro, a group, math mode or an environment.  Strict parsing
rejects such input outright, and tolerant parsing recovers a tree in which
the truncated construct looks as if it had parsed successfully.
:py:meth:`LatexWalker.parse_partial_content()
<pylatexenc.latexwalker.LatexWalker.parse_partial_content>` reports what is
trustworthy and what is not, without conflating the two::

    from pylatexenc.latexwalker import LatexWalker

    result = LatexWalker(latex_code).parse_partial_content()

    if result.complete:
        nodelist = result.trusted_nodes             # the whole document
    else:
        trusted = result.trusted_nodes              # complete nodes, from pos 0
        s_incomplete = result.incomplete_latex      # the unfinished part

The trusted nodes are complete top-level constructs that end before the
unfinished one, and their combined text is exactly the prefix
``latex_code[:result.trusted_range[1]]``.  Nothing from inside an unfinished
construct is trusted.  In particular, the contents of an unterminated
``lstlisting`` or ``verbatim`` environment are literal text: whatever
backslashes they contain are never reported as macro calls.  A separate
tolerant parse of the same input is available as ``result.tolerant_nodes``;
it is never mixed into the trusted nodes.

A genuine syntax error, such as an environment closed with the wrong name,
is not an incomplete input: ``parse_partial_content()`` raises the same
:py:class:`~pylatexenc.latexnodes.LatexWalkerParseError` that a strict parse
would raise.

.. autoclass:: pylatexenc.latexwalker.LatexWalkerPartialParseResult
   :members:


.. autofunction:: pylatexenc.latexwalker.get_default_latex_context_db


Exception Classes
~~~~~~~~~~~~~~~~~

.. py:class:: LatexWalkerError

   Moved to :py:class:`pylatexenc.latexnodes.LatexWalkerError`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes` as
      :py:class:`pylatexenc.latexnodes.LatexWalkerError`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexWalkerParseError

   Moved to :py:class:`pylatexenc.latexnodes.LatexWalkerParseError`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes` as
      :py:class:`pylatexenc.latexnodes.LatexWalkerParseError`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexWalkerEndOfStream

   Moved to :py:class:`pylatexenc.latexnodes.LatexWalkerEndOfStream`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes` as
      :py:class:`pylatexenc.latexnodes.LatexWalkerEndOfStream`.  It is aliased
      in `pylatexenc.latexwalker` for backwards compatibility.


Data Node Classes
~~~~~~~~~~~~~~~~~

.. py:class:: LatexNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexNode`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexCharsNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexCharsNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexCharsNode`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexGroupNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexGroupNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexGroupNode`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexCommentNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexCommentNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexCommentNode`.  It is aliased
      in `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexMacroNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexMacroNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexMacroNode`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexEnvironmentNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexEnvironmentNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexEnvironmentNode`.  It is
      aliased in `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexSpecialsNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexSpecialsNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexSpecialsNode`.  It is aliased
      in `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexMathNode

   Moved to :py:class:`pylatexenc.latexnodes.nodes.LatexMathNode`.

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes.nodes` as
      :py:class:`pylatexenc.latexnodes.nodes.LatexMathNode`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.


Parsing helpers
~~~~~~~~~~~~~~~

.. py:class:: ParsingState

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.

.. py:class:: LatexToken

   .. deprecated:: 3.0

      Since Pylatexenc 3.0, this class now resides in the new module
      :py:mod:`pylatexenc.latexnodes`.  It is aliased in
      `pylatexenc.latexwalker` for backwards compatibility.



Legacy Macro Definitions (for `pylatexenc 1.x`)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: pylatexenc.latexwalker.MacrosDef

.. autodata:: pylatexenc.latexwalker.default_macro_dict
   :annotation:


