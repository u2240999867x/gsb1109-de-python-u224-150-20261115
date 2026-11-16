#!/usr/bin/env python
"""Build an ODS with a data pilot (pivot table) and audit its cached result.

The source detail lives in a first sheet. A second sheet stores both the
``table:data-pilot-table`` definition and the cached pivot cells, exactly
like a spreadsheet application stores the last computed result. The recipe
then recomputes the supported aggregations from the current source range and
prints the discrepancies, including a deliberately stale cached cell.
"""

import os
from pathlib import Path

from odfdo import Document, PilotAuditor, Table
from odfdo.data_pilot import (
    ORIENTATION_COLUMN,
    ORIENTATION_DATA,
    ORIENTATION_ROW,
    DataPilotField,
    DataPilotLevel,
    DataPilotTable,
    DataPilotTables,
    format_address,
)

OUTPUT_DIR = Path(__file__).parent / "recipes_output" / "pilot_audit"
TARGET = "audited_pivot.ods"

SOURCE_ROWS = [
    ["Region", "Product", "Amount"],
    ["North", "A", 10],
    ["North", "A", 20],
    ["North", "B", 30],
    ["South", "A", 40],
    ["South", "B", 50],
    ["South", "B", 60],
]

# Cached output as last computed by the spreadsheet. One cell is deliberately
# stale (North / A kept at 5 while the source now sums to 30).
CACHED_GRID = [
    [None, "A", "B", "Total Result"],
    ["North", 5, 30, 60],
    ["South", 40, 110, 150],
    ["Total Result", 75, 140, 210],
]


def _field(name: str, orientation: str, function: str | None = None):
    field = DataPilotField()
    field.set_attribute("table:source-field-name", name)
    field.set_attribute("table:orientation", orientation)
    if function:
        field.set_attribute("table:function", function)
    field.append(DataPilotLevel())
    return field


def build_document() -> Document:
    """Create the source sheet and the cached pilot result sheet."""
    document = Document("spreadsheet")
    source = document.body.get_sheet(0)
    source.name = "Data"
    source.set_values(SOURCE_ROWS, coord="A1")

    pivot_sheet = Table("Pivot", width=4)
    document.body.append(pivot_sheet)
    for row_index, row in enumerate(CACHED_GRID):
        for column_index, value in enumerate(row):
            if value is not None:
                pivot_sheet.set_value((column_index, row_index), value)

    pilot = DataPilotTable()
    pilot.set_attribute("table:name", "Pivot1")
    pilot.set_attribute(
        "table:source-cell-range-address",
        format_address("Data", 0, 0) + ":" + format_address("Data", 2, 6),
    )
    pilot.set_attribute(
        "table:target-range-address",
        format_address("Pivot", 0, 0) + ":" + format_address("Pivot", 3, 3),
    )
    pilot.append(_field("Region", ORIENTATION_ROW))
    pilot.append(_field("Product", ORIENTATION_COLUMN))
    pilot.append(_field("Amount", ORIENTATION_DATA, "sum"))

    container = DataPilotTables()
    container.append(pilot)
    pivot_sheet.append(container)
    return document


def save_new(document: Document, name: str) -> None:
    """Save a recipe result Document."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    new_path = OUTPUT_DIR / name
    print("Saving:", new_path)
    document.save(new_path, pretty=True)


def main() -> None:
    document = build_document()
    report = PilotAuditor(document).audit_all()[0]

    print(f"Pilot table: {report.name}")
    print(
        "data field:",
        report.data_fields[0].source_column.name,
        report.data_fields[0].function,
    )
    print(
        "cells:",
        f"{report.match_count} match,",
        f"{report.mismatch_count} stale,",
        f"{report.unverifiable_count} unverifiable",
    )
    for discrepancy in report.mismatches:
        print(
            "stale cell",
            discrepancy.address,
            "cached=",
            discrepancy.cached,
            "recomputed=",
            discrepancy.expected,
        )

    test_unit(document)
    save_new(document, TARGET)


def test_unit(document: Document) -> None:
    # only for test suite:
    if "ODFDO_TESTING" not in os.environ:
        return
    report = PilotAuditor(document).audit_all()[0]
    assert report.is_stale is True
    # The stale North/A cell also invalidates its grand total column.
    assert report.mismatch_count == 2
    addresses = {discrepancy.address for discrepancy in report.mismatches}
    assert addresses == {"$Pivot.$B$2", "$Pivot.$B$4"}
    stale = next(
        discrepancy
        for discrepancy in report.mismatches
        if discrepancy.address == "$Pivot.$B$2"
    )
    assert stale.cached == 5
    assert stale.expected == 30


if __name__ == "__main__":
    main()
