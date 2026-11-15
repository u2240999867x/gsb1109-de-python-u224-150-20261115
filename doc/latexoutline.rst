`latexoutline` — Hierarchical Document Outlines
-----------------------------------------------

.. automodule:: pylatexenc.latexoutline
   :no-undoc-members:

.. contents:: Contents:
   :local:


Building an outline
~~~~~~~~~~~~~~~~~~~

.. autofunction:: pylatexenc.latexoutline.latex_outline

.. autofunction:: pylatexenc.latexoutline.outline_for_nodelist


Outline objects
~~~~~~~~~~~~~~~

.. autoclass:: pylatexenc.latexoutline.LatexOutline
   :members:

.. autoclass:: pylatexenc.latexoutline.OutlineEntry
   :members:

.. autodata:: pylatexenc.latexoutline.sectioning_levels
   :annotation:


How sections are recognized and numbered
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Only real sectioning commands create entries.  A title that is merely referred
to in the running text, as in ``\ref{...}`` or ``\nameref{...}``, or that is
added manually with ``\addcontentsline``, does not.  Neither does a
``\section`` that sits in a macro definition or another macro argument: it is
part of the definition, not of the document structure.

Each entry records the position of its sectioning command
(:py:attr:`~OutlineEntry.heading_pos`) and the extent of its body.  The body
runs from the end of the heading to the next heading of the same or a higher
level, so it contains the bodies of all subsections.
:py:attr:`~OutlineEntry.own_body_end` marks where the first subsection starts,
which gives the section's own text apart from its subsections.  When the source
has a ``document`` environment, bodies never extend past its end, and nothing
in the preamble or after ``\end{document}`` is part of the outline.

The display number is computed from the section counters, taking into account
the automatic reset of deeper counters at each numbered heading, the
``\setcounter``, ``\addtocounter`` and ``\stepcounter`` commands, the
``\appendix`` switch to letters, and ``secnumdepth``.  It is reported in
:py:attr:`~OutlineEntry.number` for information only: entries are identified by
:py:attr:`~OutlineEntry.key`, which is built from the entry's position in the
hierarchy and is therefore still unique when counter resets make numbers repeat
or when two sections share a title.
