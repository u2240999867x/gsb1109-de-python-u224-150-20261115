import json
import textwrap

import pytest

from Evtx.EventTable import (
    AmbiguousDurationField,
    EventTable,
    STATUS_ALREADY_MIGRATED,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_MIGRATED,
    STATUS_UNKNOWN_VERSION,
    VersionMap,
    declared_event_from_xml,
    normalize_event,
    normalize_events,
    unit_factor,
)

SYNTHETIC_PROVIDER = "Contoso-Synthetic-Provider"


def event(version, fields, provider=SYNTHETIC_PROVIDER, event_id="100"):
    return {
        "provider": provider,
        "version": version,
        "event_id": event_id,
        "fields": fields,
    }


def field(name, value, index):
    return {"name": name, "value": value, "index": index, "path": "EventData/Data[{}]".format(index)}


MAPPING = {
    "canonical_unit": "ms",
    "providers": {
        SYNTHETIC_PROVIDER: {
            "1": {"field": "Duration", "source_unit": "ms", "occurrence": 1},
            "2": {"field": "Duration", "source_unit": "us"},
        }
    },
}


def duplicate_old_event(value="25"):
    return event(
        1,
        [
            field("Duration", "3", 0),
            field("Duration", value, 1),
        ],
    )


def test_old_millisecond_version_is_unchanged():
    table = EventTable(MAPPING)
    row = table.normalize(duplicate_old_event())
    assert row["duration_ms"] == 25
    assert row["migration"]["status"] == STATUS_MIGRATED
    assert row["migration"]["source_unit"] == "ms"
    assert row["migration"]["canonical_unit"] == "ms"
    assert row["migration"]["factor"] == 1


def test_new_microsecond_version_scales_down_to_milliseconds():
    row = normalize_event(event(2, [field("Duration", "25000", 0)]), MAPPING)
    assert row["duration_ms"] == pytest.approx(25)
    assert row["migration"]["source_unit"] == "us"
    assert row["migration"]["factor"] == 0.001


def test_duplicate_name_records_selected_and_other_positions():
    row = normalize_event(duplicate_old_event(), MAPPING)
    migration = row["migration"]
    assert migration["source_field"] == "Duration"
    assert migration["source_index"] == 1
    assert migration["source_occurrence"] == 1
    assert migration["source_path"] == "EventData/Data[1]"
    assert [source["index"] for source in migration["all_sources"]] == [0, 1]
    assert [source["occurrence"] for source in migration["all_sources"]] == [0, 1]
    assert len(row["declared_fields"]) == 2


def test_ambiguous_duplicate_requires_an_explicit_position():
    mapping = {"providers": {SYNTHETIC_PROVIDER: {"1": {"field": "Duration", "source_unit": "ms"}}}}
    with pytest.raises(AmbiguousDurationField):
        normalize_event(duplicate_old_event(), mapping)


def test_unknown_version_keeps_original_declarations_unchanged():
    row = normalize_event(event(9, [field("Duration", "17", 0)]), MAPPING)
    assert row["migration"]["status"] == STATUS_UNKNOWN_VERSION
    assert row["migration"]["factor"] is None
    assert row["migration"]["converted"] is False
    assert row["migration"]["all_sources"] == []
    assert row["declared_fields"] == [
        {"name": "Duration", "value": "17", "index": 0, "occurrence": 0, "path": "EventData/Data[0]"}
    ]
    assert row.get("duration_ms") is None


def test_missing_field_is_not_zero():
    missing = normalize_event(event(2, [field("Other", "0", 0)]), MAPPING)
    zero = normalize_event(event(2, [field("Duration", "0", 0)]), MAPPING)
    assert missing["migration"]["status"] == STATUS_MISSING
    assert missing.get("duration_ms") is None
    assert zero["migration"]["status"] == STATUS_MIGRATED
    assert zero["duration_ms"] == 0


def test_empty_field_is_invalid_and_not_zero():
    row = normalize_event(event(2, [field("Duration", "", 0)]), MAPPING)
    assert row["migration"]["status"] == STATUS_INVALID
    assert row.get("duration_ms") is None


def test_reprocessing_a_migrated_row_never_scales_again():
    table = EventTable(MAPPING)
    first = table.normalize(duplicate_old_event())
    assert first["duration_ms"] == 25

    second = table.normalize(first)
    assert second["duration_ms"] == 25
    assert second["migration"]["status"] == STATUS_ALREADY_MIGRATED
    assert second["migration"]["additional_factor"] == 1
    assert second["migration"]["source_index"] == 1
    assert second["migration"]["source_path"] == "EventData/Data[1]"


def test_reprocessing_does_not_rescale_a_third_time():
    table = EventTable(MAPPING)
    first = table.normalize(duplicate_old_event())
    second = table.normalize(first)
    third = table.normalize(second)
    assert third["duration_ms"] == 25


def test_reprocessing_missing_row_stays_missing():
    table = EventTable(MAPPING)
    missing = table.normalize(event(2, [field("Other", "0", 0)]))
    again = table.normalize(missing)
    assert again["migration"]["status"] == STATUS_MISSING
    assert again.get("duration_ms") is None


def test_reprocessing_unknown_version_row_does_not_invent_a_value():
    table = EventTable(MAPPING)
    unknown = table.normalize(event(9, [field("Duration", "17", 0)]))
    again = table.normalize(unknown)
    assert again["migration"]["status"] == STATUS_UNKNOWN_VERSION
    assert again.get("duration_ms") is None
    assert again["declared_fields"] == unknown["declared_fields"]


def test_tables_sharing_mapping_are_independent():
    first_table = EventTable(MAPPING)
    second_table = EventTable(MAPPING)
    first_table.normalize(duplicate_old_event())
    assert len(first_table.rows) == 1
    assert len(second_table.rows) == 0


def test_conversion_ledger_records_factor_and_source_position():
    table = EventTable(MAPPING)
    table.normalize(duplicate_old_event())
    table.normalize(event(2, [field("Duration", "25000", 0)]))
    assert [record["factor"] for record in table.conversions] == [1, 0.001]
    assert [record["source"]["index"] for record in table.conversions] == [1, 0]


def test_mapping_from_tuple_keys():
    mapping = {
        "canonical_unit": "ms",
        (SYNTHETIC_PROVIDER, "1"): {"field": "Duration", "source_unit": "ms", "occurrence": 1},
        (SYNTHETIC_PROVIDER, 2): {"field": "Duration", "source_unit": "us"},
    }
    version_map = VersionMap(mapping)
    assert version_map.lookup(SYNTHETIC_PROVIDER, 1).source_unit == "ms"
    assert version_map.lookup(SYNTHETIC_PROVIDER, "2").source_unit == "us"
    assert version_map.lookup(SYNTHETIC_PROVIDER, 3) is None


def test_unit_factor_is_exact():
    assert unit_factor("ms", "us") == 1000
    assert unit_factor("us", "ms") == pytest.approx(0.001)
    assert unit_factor("ms", "ms") == 1


def test_xml_declarations_preserve_order_and_duplicates():
    xml = textwrap.dedent(
        """\
        <Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
          <System>
            <Provider Name="Contoso-Synthetic-Provider" Guid="{00000000-0000-0000-0000-000000000000}"/>
            <EventID>100</EventID>
            <Version>1</Version>
            <EventRecordID>42</EventRecordID>
            <TimeCreated SystemTime="2026-11-15 00:00:00.000000+00:00"/>
            <Channel>Synthetic</Channel>
          </System>
          <EventData>
            <Data Name="Duration">3</Data>
            <Data Name="Duration">25</Data>
          </EventData>
        </Event>
        """
    )
    declared = declared_event_from_xml(xml)
    assert declared.provider == SYNTHETIC_PROVIDER
    assert declared.version == "1"
    assert [item.name for item in declared.fields] == ["Duration", "Duration"]
    assert [item.occurrence for item in declared.fields] == [0, 1]
    assert declared.field_dicts()[1]["path"] == "EventData/Data[1]"

    row = normalize_event(declared, MAPPING)
    assert row["duration_ms"] == 25
    assert row["migration"]["source_index"] == 1


def test_xml_unknown_version_keeps_declarations():
    xml = textwrap.dedent(
        """\
        <Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
          <System>
            <Provider Name="Contoso-Synthetic-Provider"/>
            <EventID>100</EventID>
            <Version>7</Version>
          </System>
          <EventData>
            <Data Name="Duration">25</Data>
          </EventData>
        </Event>
        """
    )
    row = normalize_event(xml, MAPPING)
    assert row["migration"]["status"] == STATUS_UNKNOWN_VERSION
    assert row["declared_fields"][0]["value"] == "25"
    assert row.get("duration_ms") is None


def test_normalize_events_returns_all_rows_in_order():
    rows = normalize_events(
        [
            duplicate_old_event(),
            event(2, [field("Duration", "25000", 0)]),
            event(9, [field("Duration", "17", 0)]),
        ],
        MAPPING,
    )
    assert len(rows) == 3
    assert [row["migration"]["status"] for row in rows] == [
        STATUS_MIGRATED,
        STATUS_MIGRATED,
        STATUS_UNKNOWN_VERSION,
    ]


def test_jsonl_output_round_trips_and_is_stable():
    table = EventTable(MAPPING)
    table.normalize(duplicate_old_event())
    payload = table.to_json()
    restored = json.loads(payload)
    assert restored[0]["duration_ms"] == 25
    assert restored[0]["migration"]["source_path"] == "EventData/Data[1]"


def test_csv_exposes_conversion_and_source_location():
    table = EventTable(MAPPING)
    table.normalize(duplicate_old_event())
    rows = list(__import__("csv").DictReader(table.to_csv().splitlines()))
    assert rows[0]["source_name"] == "Duration"
    assert rows[0]["source_index"] == "1"
    assert rows[0]["source_unit"] == "ms"
    assert rows[0]["factor"] == "1"
    assert rows[0]["duration_ms"] == "25"


def test_json_mapping_file_round_trip(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(MAPPING))
    table = EventTable.from_json_file(str(path))
    assert table.canonical_unit == "ms"
    assert table.normalize(duplicate_old_event())["duration_ms"] == 25


def test_cross_unit_aliases_are_lossless():
    row = normalize_event(event(2, [field("Duration", "25000", 0)]), MAPPING)
    assert row["duration_ms"] == pytest.approx(25)
    assert row["duration_us"] == pytest.approx(25000)
    assert row["duration_microseconds"] == pytest.approx(25000)
    assert "duration_ms" in row
    assert "duration_us" in row


def test_unknown_version_reprocessing_does_not_create_a_column():
    table = EventTable(MAPPING)
    unknown = table.normalize(event(9, [field("Duration", "17", 0)]))
    again = table.normalize(unknown)
    assert dict.__contains__(again, "duration_ms") is False
    assert again["migration"]["status"] == STATUS_UNKNOWN_VERSION
    assert again["declared_fields"] == unknown["declared_fields"]


def test_reprocessing_a_microsecond_row_keeps_the_original_source():
    table = EventTable(MAPPING)
    first = table.normalize(event(2, [field("Duration", "25000", 0)]))
    second = table.normalize(first)
    assert second["duration_ms"] == pytest.approx(25)
    assert second["migration"]["status"] == STATUS_ALREADY_MIGRATED
    assert second["migration"]["source_unit"] == "us"
    assert second["migration"]["factor"] == 0.001
    assert second["migration"]["additional_factor"] == 1


def test_xmltodict_style_event_data_is_understood():
    mapping = {"providers": {SYNTHETIC_PROVIDER: {"1": {"field": "Duration", "source_unit": "us"}}}}
    payload = {
        "provider": SYNTHETIC_PROVIDER,
        "version": 1,
        "EventData": {"Data": [{"@Name": "Duration", "#text": "25000"}]},
    }
    row = normalize_event(payload, mapping)
    assert row["duration_ms"] == pytest.approx(25)
    assert row["migration"]["source_path"] == "EventData/Data[0]"


def test_shorthand_mapping_requires_position_when_name_repeats():
    mapping = {"providers": {SYNTHETIC_PROVIDER: {"1": {"field": "Duration", "source_unit": "ms"}}}}
    with pytest.raises(AmbiguousDurationField):
        normalize_event({"provider": SYNTHETIC_PROVIDER, "version": 1, "fields": {"Duration": ["3", "25"]}}, mapping)
