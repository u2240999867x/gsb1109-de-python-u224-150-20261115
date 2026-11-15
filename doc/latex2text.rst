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


Grouping rules into named, prioritized profiles
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When several sources (for instance different journals) each provide their own
extraction rules, it is convenient to keep each source's rules in a named
group rather than editing one shared context database by hand.  The
:py:class:`RuleProfile` and :py:class:`RuleProfileDb` classes provide this
layer on top of the ordinary
:py:class:`~pylatexenc.macrospec.LatexContextDb` machinery.

A profile groups the rules of one source.  It typically redefines only a few
names -- a single macro is enough -- and leaves every other default rule
untouched.  Macro, environment and specials rules are three separate naming
domains, so a profile that overrides a macro contains no environment or
specials rule unless you give it some::

    from pylatexenc.latex2text import (
        LatexNodes2Text, MacroTextSpec, RuleProfile, RuleProfileDb,
    )
    from pylatexenc.latex2text._get_defaultspecs import (
        get_default_latex_context_db as get_text_defaults,
    )
    from pylatexenc.latexwalker._get_defaultspecs import (
        get_default_latex_context_db as get_parser_defaults,
    )

    profiles = RuleProfileDb()
    # journal A redefines one macro and keeps everything else at the default
    profiles.add_profile(
        RuleProfile('journal-a',
                    macros=[MacroTextSpec('beta', simplify_repl='BETA-A')]),
        priority=0,
    )
    # journal B redefines the same macro, with a higher priority
    profiles.add_profile(
        RuleProfile('journal-b',
                    macros=[MacroTextSpec('beta', simplify_repl='BETA-B')]),
        priority=10,
    )

A profile database does not convert anything by itself.  For each document you
build a concrete context database with
:py:meth:`RuleProfileDb.build_context()`, selecting the profiles that apply to
that document.  The base context is copied, never modified, and each document
gets its own database, so parallel documents cannot interfere with one
another::

    base_text_db = get_text_defaults()

    def convert(latex, enabled_profiles):
        text_db = profiles.build_context(base_text_db,
                                         enabled=enabled_profiles)
        return LatexNodes2Text(latex_context=text_db).latex_to_text(latex)

    convert(r'\beta', ('journal-a', 'journal-b'))  # -> 'BETA-B' (higher wins)
    convert(r'\beta', ('journal-a',))              # -> 'BETA-A'
    convert(r'\beta', ())                          # -> 'β' (the default)

Each enabled profile becomes one category of the resulting context database,
ordered by priority.  When several enabled profiles define the same name, the
highest-priority one wins only while it is enabled: leave it out of the
`enabled` selection (or call :py:meth:`RuleProfileDb.remove_profile()`) and
the lower-priority rule -- or, failing that, the default rule -- applies
again on the next build.  Priorities can also be changed after the fact with
:py:meth:`RuleProfileDb.set_priority()`.

Because the parser and the text converter keep separate context databases,
you typically maintain two profile databases in parallel, one holding
:py:class:`~pylatexenc.macrospec.MacroSpec` and friends and one holding
:py:class:`MacroTextSpec` and friends, and build both with the same `enabled`
selection.

To inspect where the rule that actually applies to a name comes from, use
:py:meth:`RuleProfileDb.explain_rule()` or :py:meth:`RuleProfileDb.explain()`.
They return a :py:class:`RuleResolution` that names the winning profile (or
`None` for a default rule) and lists the shadowed definitions in the order in
which they would take over.

.. autoclass:: pylatexenc.latex2text.RuleProfile
   :members:

.. autoclass:: pylatexenc.latex2text.RuleProfileDb
   :members:

.. autoclass:: pylatexenc.latex2text.RuleResolution
   :members:

.. autoclass:: pylatexenc.latex2text.ShadowedRule
   :members:


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
