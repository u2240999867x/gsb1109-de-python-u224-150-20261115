"""
Tests for cross-backup EVTX correlation.

The real EVTX fixtures supply end to end behavior, while small synthetic
ObservedEvent instances pin down the conservative matching rules that cannot
be constructed from the fixture data on demand.
"""
import json
import os
import shutil

import pytest

from Evtx.correlate import (
    EVIDENCE_BYTE_EXACT,
    EVIDENCE_EVENT_METADATA,
    REASON_DISTINCT_RECORD_ID,
    REASON_REUSED_RECORD_ID,
    CandidateLink,
    EventLocation,
    LogicalEvent,
    ObservedEvent,
    correlate,
    correlate_sources,
    scan_sources,
)


def make_observation(
    machine,
    channel,
    record_num,
    generation,
    source,
    offset,
    timestamp="2026-01-01T00:00:00+00:00",
    body_hash="body-1",
    raw_hash="raw-1",
    system_fingerprint=None,
):
    location = EventLocation(
        source=source,
        generation=generation,
        machine=machine,
        channel=channel,
        record_num=record_num,
        file_record_num=record_num,
        chunk_index=0,
        offset=offset,
    )
    return ObservedEvent(
        location=location,
        timestamp=timestamp,
        event_id="4624",
        body_hash=body_hash,
        raw_hash=raw_hash,
        system_fingerprint=system_fingerprint
        or "system-{}:{}-{}".format(machine, channel, record_num),
    )


def test_identical_backups_correlate_with_byte_exact_evidence(data_path, tmp_path):
    security = os.path.join(data_path, "security.evtx")
    first = tmp_path / "gen1-security.evtx"
    second = tmp_path / "gen2-security.evtx"
    shutil.copy(security, first)
    shutil.copy(security, second)

    baseline, errors = scan_sources([(str(first), "gen1")])
    assert errors == []

    result = correlate_sources([(str(first), "gen1"), (str(second), "gen2")])

    assert result.parse_errors == []
    assert len(result.logical_events) == len(baseline)
    assert all(event.status == "duplicate" for event in result.logical_events)
    assert all(len(event.observations) == 2 for event in result.logical_events)
    assert EVIDENCE_BYTE_EXACT in result.logical_events[0].evidence
    assert EVIDENCE_EVENT_METADATA in result.logical_events[0].evidence


def test_every_confirmed_copy_keeps_its_original_location(data_path, tmp_path):
    security = os.path.join(data_path, "security.evtx")
    first = tmp_path / "gen1-security.evtx"
    second = tmp_path / "gen2-security.evtx"
    shutil.copy(security, first)
    shutil.copy(security, second)

    result = correlate_sources([(str(first), "gen1"), (str(second), "gen2")])
    event = result.duplicates()[0]

    sources = {loc.source for loc in event.locations}
    generations = {loc.generation for loc in event.locations}
    assert sources == {str(first), str(second)}
    assert generations == {"gen1", "gen2"}
    offsets = {loc.offset for loc in event.locations}
    assert len(offsets) == 1
    assert all(loc.machine == event.machine and loc.channel == event.channel for loc in event.locations)


def test_recordid_reused_on_another_machine_is_never_merged():
    observations = [
        make_observation("HOST-A", "Security", 1, "gen1", "a.evtx", 0x1000),
        make_observation("HOST-B", "Security", 1, "gen1", "b.evtx", 0x2000, system_fingerprint="system-B"),
    ]

    result = correlate(observations)

    assert len(result.logical_events) == 2
    assert result.duplicates() == []
    machines = {event.machine for event in result.logical_events}
    assert machines == {"HOST-A", "HOST-B"}


def test_same_body_with_distinct_recordids_stays_two_rows():
    observations = [
        make_observation("HOST-A", "Security", 22, "gen1", "a.evtx", 0x1000, raw_hash="raw-22"),
        make_observation("HOST-A", "Security", 24, "gen1", "a.evtx", 0x2000, raw_hash="raw-24"),
    ]

    result = correlate(observations)

    assert len(result.logical_events) == 2
    assert all(event.status == "unique" for event in result.logical_events)
    assert [event.record_num for event in result.logical_events] == [22, 24]
    assert len(result.candidate_links) == 1
    link = result.candidate_links[0]
    assert link.reason == REASON_DISTINCT_RECORD_ID
    assert {loc.record_num for loc in link.locations} == {22, 24}


def test_field_equality_with_conflicting_evidence_is_candidate_only():
    observations = [
        make_observation(
            "HOST-A",
            "Security",
            5,
            "gen1",
            "a.evtx",
            0x1000,
            timestamp="2026-01-01T00:00:00+00:00",
        ),
        make_observation(
            "HOST-A",
            "Security",
            5,
            "gen2",
            "b.evtx",
            0x3000,
            timestamp="2026-02-02T00:00:00+00:00",
        ),
    ]

    result = correlate(observations)

    assert len(result.logical_events) == 2
    assert result.duplicates() == []
    assert len(result.candidate_links) == 1
    assert result.candidate_links[0].reason == REASON_REUSED_RECORD_ID
    assert {loc.generation for loc in result.candidate_links[0].locations} == {"gen1", "gen2"}


def test_partial_backup_overlap_confirms_only_shared_events():
    observations = [
        make_observation("HOST-A", "Security", 1, "gen1", "a.evtx", 0x1000, body_hash="body-a", raw_hash="raw-a"),
        make_observation(
            "HOST-A",
            "Security",
            2,
            "gen1",
            "a.evtx",
            0x2000,
            body_hash="body-b",
            raw_hash="raw-b",
            system_fingerprint="system-shared",
        ),
        make_observation(
            "HOST-A",
            "Security",
            2,
            "gen2",
            "b.evtx",
            0x2000,
            body_hash="body-b",
            raw_hash="raw-b",
            system_fingerprint="system-shared",
        ),
        make_observation("HOST-A", "Security", 3, "gen2", "b.evtx", 0x4000, body_hash="body-c", raw_hash="raw-c"),
    ]

    result = correlate(observations)

    assert len(result.logical_events) == 3
    duplicate = result.duplicates()
    assert len(duplicate) == 1
    assert duplicate[0].record_num == 2
    assert {loc.generation for loc in duplicate[0].locations} == {"gen1", "gen2"}
    assert sorted(event.record_num for event in result.unique_events()) == [1, 3]
    assert result.candidate_links == []


def test_rescanning_the_same_generation_does_not_double_count(data_path, tmp_path):
    security = os.path.join(data_path, "security.evtx")
    once, errors = scan_sources([(security, "gen1")])
    twice, errors_twice = scan_sources([(security, "gen1"), (security, "gen1")])

    assert errors == [] and errors_twice == []
    assert len(once) == len(twice)


def test_real_fixture_keeps_genuine_repeated_events_on_separate_rows(data_path):
    security = os.path.join(data_path, "security.evtx")
    observations, errors = scan_sources([(security, "only-generation")])
    result = correlate(observations, errors)

    assert result.duplicates() == []
    assert len(result.logical_events) == len(observations)
    assert all(isinstance(event, LogicalEvent) for event in result.logical_events)
    for event in result.logical_events:
        assert len({obs.location.record_num for obs in event.observations}) == 1

    reasons = {link.reason for link in result.candidate_links}
    assert REASON_DISTINCT_RECORD_ID in reasons
    for link in result.candidate_links:
        assert isinstance(link, CandidateLink)
        assert len({loc.record_num for loc in link.locations}) > 1 or link.reason == REASON_REUSED_RECORD_ID

    known_repeat = next(
        link
        for link in result.candidate_links
        if {loc.record_num for loc in link.locations} >= {22, 24}
    )
    assert known_repeat.reason == REASON_DISTINCT_RECORD_ID
    assert len({(loc.source, loc.offset) for loc in known_repeat.locations}) == len(known_repeat.locations)


def test_cli_writes_json_report(data_path, tmp_path, monkeypatch):
    security = os.path.join(data_path, "security.evtx")
    first = tmp_path / "gen1-security.evtx"
    second = tmp_path / "gen2-security.evtx"
    shutil.copy(security, first)
    shutil.copy(security, second)
    output = tmp_path / "report.json"

    from evtx_scripts import evtx_correlate

    monkeypatch.setattr(
        "sys.argv",
        [
            "evtx_correlate",
            "gen1={}".format(first),
            "gen2={}".format(second),
            "--output",
            str(output),
        ],
    )
    evtx_correlate.main()

    payload = json.loads(output.read_text())
    assert payload["parse_errors"] == []
    assert all(row["status"] == "duplicate" for row in payload["logical_events"])
    assert all({loc["generation"] for loc in row["locations"]} == {"gen1", "gen2"} for row in payload["logical_events"])


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("gen1=/tmp/Archive-Security.evtx", ("/tmp/Archive-Security.evtx", "gen1")),
        ("/tmp/Archive-Security.evtx", ("/tmp/Archive-Security.evtx", "Archive-Security.evtx")),
    ],
)
def test_parse_source_spec(spec, expected):
    from evtx_scripts.evtx_correlate import parse_source_spec

    assert parse_source_spec(spec) == expected
