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
# Authors: Romain Gauthier <romain@itaapy.com>
#          Hervé Cauwelier <herve@itaapy.com>
#          David Versmisse <david.versmisse@itaapy.com>

from odfdo.utils import formula_table_name, oooc_to_ooow, rename_table_in_formula


def test_addition():
    formula = "oooc:=[.A2]+[.A3]"
    excepted = "ooow:<A2>+<A3>"
    assert oooc_to_ooow(formula) == excepted


def test_sum():
    formula = "oooc:=SUM([.B2:.B4])"
    excepted = "ooow:sum <B2:B4>"
    assert oooc_to_ooow(formula) == excepted


def test_addition_sum():
    formula = "oooc:=[.A2]-[.A3]+SUM([.B2:.B4])*[.D4]"
    excepted = "ooow:<A2>-<A3>+sum <B2:B4>*<D4>"
    assert oooc_to_ooow(formula) == excepted


def test_formula_table_name_simple():
    assert formula_table_name("Sheet1") == "Sheet1"


def test_formula_table_name_with_space():
    assert formula_table_name("Jan 2026") == "'Jan 2026'"


def test_formula_table_name_starting_with_digit():
    assert formula_table_name("2026") == "'2026'"


def test_rename_table_in_formula_bare_name():
    formula = "of:=[Sheet1.A1]+[.B2]"
    assert rename_table_in_formula(formula, "Sheet1", "Report") == (
        "of:=[Report.A1]+[.B2]"
    )


def test_rename_table_in_formula_quoted_name():
    formula = "of:=['Jan 2025'.A1]+SUM(['Jan 2025'.B1:.B9])"
    assert rename_table_in_formula(formula, "Jan 2025", "Jan 2026") == (
        "of:=['Jan 2026'.A1]+SUM(['Jan 2026'.B1:.B9])"
    )


def test_rename_table_in_formula_quotes_new_name():
    formula = "of:=[Sheet1.A1]"
    assert rename_table_in_formula(formula, "Sheet1", "Jan 2026") == (
        "of:=['Jan 2026'.A1]"
    )


def test_rename_table_in_formula_absolute_reference():
    formula = "of:=$Sheet1.$A$1"
    assert rename_table_in_formula(formula, "Sheet1", "Report") == (
        "of:=$Report.$A$1"
    )


def test_rename_table_in_formula_range():
    formula = "of:=[Sheet1.A1:Sheet1.B2]"
    assert rename_table_in_formula(formula, "Sheet1", "Report") == (
        "of:=[Report.A1:Report.B2]"
    )


def test_rename_table_in_formula_string_literal_unchanged():
    formula = 'of:=CONCAT("Sheet1", [Sheet1.A1])'
    assert rename_table_in_formula(formula, "Sheet1", "Report") == (
        'of:=CONCAT("Sheet1", [Report.A1])'
    )


def test_rename_table_in_formula_external_reference_unchanged():
    formula = "of:=['file:///tmp/report.ods'#$Sheet1.A1]+[Sheet1.B2]"
    assert rename_table_in_formula(formula, "Sheet1", "Report") == (
        "of:=['file:///tmp/report.ods'#$Sheet1.A1]+[Report.B2]"
    )


def test_rename_table_in_formula_external_quoted_sheet_unchanged():
    formula = "of:=['file:///tmp/report.ods'#$'Jan 2025'.A1]"
    assert rename_table_in_formula(formula, "Jan 2025", "Feb 2025") == formula


def test_rename_table_in_formula_no_partial_match():
    formula = "of:=[MySheet1.A1]+[Sheet1.A1]"
    assert rename_table_in_formula(formula, "Sheet1", "Report") == (
        "of:=[MySheet1.A1]+[Report.A1]"
    )


def test_rename_table_in_formula_same_name():
    formula = "of:=[Sheet1.A1]"
    assert rename_table_in_formula(formula, "Sheet1", "Sheet1") == formula
