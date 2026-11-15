`latex2text` — Simple Latex to Text Converter
---------------------------------------------

.. automodule:: pylatexenc.latex2text
   :no-undoc-members:

.. contents:: Contents:
   :local:


Custom latex conversion rules: A simple template
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Here is a short introduction on how to customize the way that
:py:class:`~pylatexenc.latex2text.LatexNodes2Text` converts LaTeX constructs
(macros, environments, and specials) to unicode text.  You can start off with
the example template below and adapt it to your needs.

Macros, environments and specials are parsed as corresponding node objects by
the parser (see :py:class:`pylatexenc.latexwalker.LatexMacroNode`,
:py:class:`pylatexenc.latexwalker.LatexEnvironmentNode`, and
:py:class:`pylatexenc.latexwalker.LatexSpecialsNode`).  These node objects are
then converted to unicode text by the
:py:class:`~pylatexenc.latex2text.LatexNodes2Text` object.

You can define new macros, environments, or specials, or override existing
definitions.  The definitions need to be provided twice.  First, at the level of
the parser using the :py:mod:`~pylatexenc.macrospec` module; the parser needs to
know the argument structure of your macros, environments, and specials, along
with which characters to recognize as "specials".  Second, at the level of
`latex2text`, you need to specify what the replacement strings are for the
different LaTeX constructs after they have been parsed into the latex node tree
by the parser.

The following template is a simple illustrative example that implements the
following definitions:

  - A new macro ``\putinquotes[`][']{text}`` that puts its mandatory argument
    into quotes defined by the two optional arguments.  Let's say that the
    default quotes that are used are the unicode double quotation marks ``“``
    and ``”``.  Another simpler macro ``\putindblquotes{text}`` is also provided
    for the sake of the example.

  - A new environment ``\begin{inquotes}[`]['] ... \end{inquotes}`` that does
    the same thing as its macro equivalent.  Another simpler environment
    ``\begin{indblquotes}...\end{indblquotes}`` is also provided for the sake of
    the example.

  - The usual LaTeX quote symbols `````, ``````, ``'``, and ``''`` for unicode
    quotes.  (See also issue :issue:`39`)

Here is the code (see also docs for :py:class:`pylatexenc.macrospec.MacroSpec`,
:py:class:`pylatexenc.macrospec.EnvironmentSpec`,
:py:class:`pylatexenc.macrospec.SpecialsSpec`, as well as
:py:class:`pylatexenc.latex2text.MacroTextSpec`,
:py:class:`pylatexenc.latex2text.EnvironmentTextSpec`,
:py:class:`pylatexenc.latex2text.SpecialsTextSpec`):

.. literalinclude:: example_latex2text_custom_quotes.py
   :language: python


Latex to Text Converter Class
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: pylatexenc.latex2text.LatexNodes2Text
   :members:

   .. py:attribute:: state

      The current conversion state, as a
      :py:class:`~pylatexenc.latex2text.TextConversionState` object.  It keeps
      track of the surroundings in which the node currently being converted
      appears.  Use :py:meth:`push_state()` to temporarily override some of its
      fields.

      .. versionadded:: 3.0

         This attribute was introduced in `pylatexenc 3.0`.


.. autoclass:: pylatexenc.latex2text.TextConversionState
   :members:


.. autofunction:: pylatexenc.latex2text.get_default_latex_context_db



Tracing rendered text back to the TeX source
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When the rendered text is used to index a document for full-text searching, a
search hit has to lead back to the place in the original ``.tex`` file that
produced it.  :py:meth:`LatexNodes2Text.latex_to_text_with_source_info()` (and
the node-list variant
:py:meth:`LatexNodes2Text.nodelist_to_text_with_source_info()`) return a
:py:class:`~pylatexenc.latex2text.TextSourceInfo` object instead of a bare
string.  It carries the rendered text in its `text` attribute — identical to
what :py:meth:`LatexNodes2Text.latex_to_text()` returns — together with the
mapping between regions of that text and ranges of the original LaTeX source.

The mapping is many-to-many.  One output position is usually covered by
several spans: an outer macro invocation, the nested macro arguments and the
verbatim characters of an argument all show up.  Conversely, one source range
can feed several output regions, for instance when a replacement string
embeds the same argument more than once.

Three kinds of origins are kept apart on purpose:

- Text copied verbatim from a characters node is a *source* span: those
  characters really occur, in the same order, in the TeX.
- Text produced by a macro, an environment or a specials conversion is a
  *macro* span over the **whole invocation**.  Hitting only part of an
  expansion — a bracket inserted by the replacement string, or the “/” of a
  rendered fraction — still reports the whole invocation, so that the index
  never claims the original source contains an independent fragment that is in
  fact generated.  The rendered arguments nested inside the invocation remain
  traceable on their own, including when they are themselves macro calls.
- Comments dropped by the conversion are recorded separately in
  `discarded_comments`, with no output position, so that a thrown-away comment
  can be inspected but is never mistaken for the source of some output text.

Use :py:meth:`TextSourceInfo.sources_at()` to jump from a position in the
rendered text back to every relevant source range.

.. autoclass:: pylatexenc.latex2text.TextSourceInfo
   :members:

.. autoclass:: pylatexenc.latex2text.TextSourceSpan
   :members:

.. autodata:: pylatexenc.latex2text.SOURCE_KIND_SOURCE
   :annotation:

.. autodata:: pylatexenc.latex2text.SOURCE_KIND_MACRO
   :annotation:

.. autodata:: pylatexenc.latex2text.SOURCE_KIND_SYNTHESIZED
   :annotation:

.. autodata:: pylatexenc.latex2text.SOURCE_KIND_COMMENT
   :annotation:



Define replacement texts
~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: pylatexenc.latex2text.MacroTextSpec
   :members:

.. autoclass:: pylatexenc.latex2text.EnvironmentTextSpec
   :members:

.. autoclass:: pylatexenc.latex2text.SpecialsTextSpec
   :members:


Formatting helpers
~~~~~~~~~~~~~~~~~~

Most of the following functions are meant to be used directly as the
`simplify_repl` callable of a :py:class:`~pylatexenc.latex2text.MacroTextSpec`,
:py:class:`~pylatexenc.latex2text.EnvironmentTextSpec` or
:py:class:`~pylatexenc.latex2text.SpecialsTextSpec`.  They implement the
default rendering of the corresponding LaTeX constructs, and can be reused (or
wrapped) when you define your own replacement rules.  The last two are plain
string helpers that convert text to the corresponding unicode styled or
superscript/subscript characters.

.. autofunction:: pylatexenc.latex2text.fmt_equation_environment

.. autofunction:: pylatexenc.latex2text.fmt_input_macro

.. autofunction:: pylatexenc.latex2text.placeholder_node_formatter

.. autofunction:: pylatexenc.latex2text.fmt_placeholder_node

.. autofunction:: pylatexenc.latex2text.fmt_matrix_environment_node

.. autofunction:: pylatexenc.latex2text.fmt_list_environment

.. autofunction:: pylatexenc.latex2text.fmt_list_item

.. autofunction:: pylatexenc.latex2text.fmt_item_macro

.. autofunction:: pylatexenc.latex2text.fmt_math_text_style

.. autofunction:: pylatexenc.latex2text.fmt_subsuperscript_text



Obsolete members
~~~~~~~~~~~~~~~~

.. autofunction:: pylatexenc.latex2text.EnvDef

.. autofunction:: pylatexenc.latex2text.MacroDef


.. autodata:: pylatexenc.latex2text.default_env_dict
   :annotation:

.. autodata:: pylatexenc.latex2text.default_macro_dict
   :annotation:

.. autodata:: pylatexenc.latex2text.default_text_replacements
   :annotation:
