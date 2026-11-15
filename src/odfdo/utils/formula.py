# Copyright 2018-2026 Jérôme Dumonteil
# Copyright (c) 2009-2010 Ars Aperta, Itaapy, Pierlis, Talend.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
# Authors (odfdo project): jerome.dumonteil@gmail.com
# The odfdo project is a derivative work of the lpod-python project:
# https://github.com/lpod/lpod-python
# Authors: David Versmisse <david.versmisse@itaapy.com>
#          Hervé Cauwelier <herve@itaapy.com>
#          Romain Gauthier <romain@itaapy.com>
"""Formula conversion utilities for ODF documents.

This module provides helper functions for converting spreadsheet formulas
between different formats, such as from OpenOffice.org Calc to Writer.
"""

from __future__ import annotations

import re

_SIMPLE_TABLE_NAME = re.compile(r"(?!\d)\w+")


def formula_table_name(name: str) -> str:
    """Return a table (sheet) name as it must appear in a formula.

    Simple names (letters, digits, underscore, not starting with a digit) are
    used as is, other names (e.g. containing spaces) are enclosed in single
    quotes.

    Args:
        name: The table name to format.

    Returns:
        str: The table name, quoted with single quotes if required.

    """
    if _SIMPLE_TABLE_NAME.fullmatch(name):
        return name
    return f"'{name}'"


def rename_table_in_formula(formula: str, old_name: str, new_name: str) -> str:
    """Rewrite references to a table (sheet) name inside a formula.

    Every local reference to `old_name` (quoted like `'My Sheet'.A1` or bare
    like `Sheet1.A1`, with optional `$` markers) is replaced by a reference to
    `new_name`, which is quoted if it requires it.

    Text inside string literals ("...") is never modified, even if identical
    to the table name. External workbook references (e.g.
    `'file:///report.ods'#$Sheet1.A1`) are left untouched: their sheet part is
    not a local table.

    Args:
        formula: The formula string to rewrite (e.g., "of:=[Sheet1.A1]").
        old_name: The current name of the referenced table.
        new_name: The new name of the referenced table.

    Returns:
        str: The formula with local references to `old_name` renamed.

    """
    if not formula or not old_name or not new_name or old_name == new_name:
        return formula
    pattern = re.compile(
        r'"(?:[^"]|"")*"'  # string literal, protected
        r"|'[^']*'(?=#)"  # external workbook URI, protected
        rf"|(?P<ref>(?P<ext>#?)(?P<abs>\$?)"
        rf"(?:{re.escape(f"'{old_name}'")}"
        rf"|(?<![\w.]){re.escape(old_name)})"
        rf"(?=\.))"
    )
    new_ref = formula_table_name(new_name)

    def _replace(match: re.Match) -> str:
        if match.group("ref") is None or match.group("ext"):
            return match.group(0)
        return f"{match.group('abs')}{new_ref}"

    return pattern.sub(_replace, formula)


def oooc_to_ooow(formula: str) -> str:
    """Convert an OpenOffice.org Calc formula to a Writer-compatible format.

    This function translates cell address syntax and common functions from the
    `oooc` (Calc) namespace to the `ooow` (Writer) namespace.

    Args:
        formula: The Calc formula string to convert (e.g., "oooc:=SUM([.A1:.A5])").

    Returns:
        str: The converted formula in Writer format (e.g., "ooow:sum <A1:A5>").

    """
    _prefix, formula = formula.split(":=", 1)
    # assert "oooc" in prefix
    # Convert cell addresses
    formula = formula.replace("[.", "<").replace(":.", ":").replace("]", ">")
    # Convert functions
    formula = formula.replace("SUM(", "sum ").replace(")", "")
    return f"ooow:{formula}"
