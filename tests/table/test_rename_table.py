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
import pytest

from odfdo.document import Document
from odfdo.table import Table


@pytest.fixture
def document():
    doc = Document("ods")
    body = doc.body
    january = body.get_table()
    january.name = "January"
    january.set_value("A1", 10)
    january.set_value("B1", 20, formula="of:=[.A1]*2")
    summary = Table("Summary")
    body.append(summary)
    summary.set_value("A1", 30, formula="of:=[January.A1]+['January'.B1]")
    summary.set_value("A2", "x", formula='of:=CONCAT("January", [January.A1])')
    summary.set_value(
        "A3",
        5,
        formula="of:=['file:///tmp/jan.ods'#$January.A1]+[January.B1]",
    )
    body.set_named_range("jan_total", "A1:B1", "January")
    return doc


def test_rename_table_changes_table_name(document):
    document.rename_table("January", "Jan 2026")
    names = [table.name for table in document.body.tables]
    assert names == ["Jan 2026", "Summary"]


def test_rename_table_migrates_formulas(document):
    document.rename_table("January", "Jan 2026")
    summary = document.body.get_table(name="Summary")
    assert summary.get_cell("A1").formula == "of:=['Jan 2026'.A1]+['Jan 2026'.B1]"


def test_rename_table_keeps_string_literals(document):
    document.rename_table("January", "Jan 2026")
    summary = document.body.get_table(name="Summary")
    assert summary.get_cell("A2").formula == (
        'of:=CONCAT("January", [\'Jan 2026\'.A1])'
    )


def test_rename_table_keeps_external_references(document):
    document.rename_table("January", "Jan 2026")
    summary = document.body.get_table(name="Summary")
    assert summary.get_cell("A3").formula == (
        "of:=['file:///tmp/jan.ods'#$January.A1]+['Jan 2026'.B1]"
    )


def test_rename_table_migrates_named_ranges(document):
    document.rename_table("January", "Jan 2026")
    named_range = document.body.get_named_range("jan_total")
    assert named_range.table_name == "Jan 2026"
    assert named_range.get_attribute_string("table:cell-range-address") == (
        "$'Jan 2026'.$A$1:.$B$1"
    )


def test_rename_table_simple_new_name(document):
    document.rename_table("January", "February")
    summary = document.body.get_table(name="Summary")
    assert summary.get_cell("A1").formula == "of:=[February.A1]+[February.B1]"


def test_rename_table_not_found(document):
    with pytest.raises(ValueError, match="Table not found"):
        document.rename_table("NoSuchTable", "NewName")


def test_rename_table_name_conflict(document):
    with pytest.raises(ValueError, match="Table already exists"):
        document.rename_table("January", "Summary")


def test_rename_table_invalid_new_name(document):
    with pytest.raises(ValueError, match="not allowed"):
        document.rename_table("January", "Bad/Name")


def test_rename_table_same_name_is_noop(document):
    document.rename_table("January", "January")
    summary = document.body.get_table(name="Summary")
    assert summary.get_cell("A1").formula == "of:=[January.A1]+['January'.B1]"
