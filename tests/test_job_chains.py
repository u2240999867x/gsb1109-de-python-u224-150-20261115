"""Tests for rebuilding collection job chains from correlated events."""

import json
import subprocess
import sys
import os

from Evtx.JobChain import (
    END,
    START,
    TRANSFER,
    UNKNOWN,
    JobChainBuilder,
    build_job_chains,
    canonical_id,
    event_from_xml,
    normalize_role,
)

A = "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}"
B = "{bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb}"
C = "{cccccccc-cccc-cccc-cccc-cccccccccccc}"
D = "{dddddddd-dddd-dddd-dddd-dddddddddddd}"
E = "{eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee}"


def event(activity, related=None, kind=None, uid=None, provider="Provider", event_id=None, related_ids=None):
    payload = {
        "activity_id": activity,
        "kind": kind,
        "provider": provider,
        "event_id": event_id,
    }
    if related is not None:
        payload["related_activity_id"] = related
    if related_ids is not None:
        payload["related_activity_ids"] = related_ids
    if uid is not None:
        payload["uid"] = uid
    return payload


def test_normalize_role():
    assert normalize_role("Start") == START
    assert normalize_role("COLLECTION_BEGIN") == START
    assert normalize_role("handed off") == TRANSFER
    assert normalize_role("JobCompleted") == END
    assert normalize_role("采集作业开始") == START
    assert normalize_role("作业转交") == TRANSFER
    assert normalize_role("采集结束") == END
    assert normalize_role("bogus") == UNKNOWN
    assert normalize_role(None) == UNKNOWN


def test_canonical_id():
    assert canonical_id("{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}") == canonical_id(A)
    assert canonical_id("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") == canonical_id(A)
    assert canonical_id("") is None
    assert canonical_id("{00000000-0000-0000-0000-000000000000}") is None


def test_linear_chain():
    report = build_job_chains(
        [
            event(A, kind="start", uid="1"),
            event(B, related=A, kind="transfer", uid="2"),
            event(B, kind="end", uid="3"),
        ]
    )
    assert len(report) == 1
    chain = report[0]
    assert list(chain.nodes) == [canonical_id(A), canonical_id(B)]
    assert [(edge.source, edge.target) for edge in chain.edges] == [(canonical_id(A), canonical_id(B))]
    assert chain.roots == [A]
    assert chain.leaves == [B]
    assert chain.complete
    assert chain.workload == 3
    assert chain.paths() == [[A, B]]


def test_branch_and_merge():
    report = build_job_chains(
        [
            event(A, kind="start", uid="1"),
            event(B, related=A, kind="transfer", uid="2"),
            event(C, related=A, kind="transfer", uid="3"),
            event(D, related_ids=[B, C], kind="end", uid="4"),
        ]
    )
    chain = report[0]
    assert set(chain.nodes) == {canonical_id(value) for value in (A, B, C, D)}
    assert chain.node(B).parents == {canonical_id(A)}
    assert chain.node(C).parents == {canonical_id(A)}
    assert chain.node(D).parents == {canonical_id(B), canonical_id(C)}
    assert sorted(chain.paths()) == [[A, B, D], [A, C, D]]
    assert chain.branched_activities == [A]
    assert chain.merged_activities == [D]
    assert chain.workload == 4
    assert chain.complete


def test_xmltodict_style_json():
    payload = {
        "Event": {
            "System": {
                "Provider": {"Name": "Collector"},
                "EventID": "100",
                "EventRecordID": "9",
                "TimeCreated": {"SystemTime": "2026-01-01 00:00:00+00:00"},
                "Correlation": {"ActivityID": A, "RelatedActivityID": B},
            },
            "EventData": {
                "Data": [
                    {"@Name": "Stage", "#text": "start"},
                    {"@Name": "CorrelationID", "#text": C},
                ]
            },
        }
    }
    report = build_job_chains([payload])
    record = report[0].node(A).events[0]
    assert record.activity_id == A
    assert record.related_activity_ids == [B, C]
    assert record.kind == START
    assert record.provider == "Collector"
    assert record.record_id == "9"


def test_missing_start_is_distinct_from_missing_end():
    report = build_job_chains(
        [
            event(B, related=A, kind="transfer", uid="1"),
            event(C, related=B, kind="transfer", uid="2"),
            event(E, related=C, kind="transfer", uid="3"),
            event(E, kind="end", uid="4"),
        ]
    )
    chain = report[0]
    assert chain.missing_start
    assert not chain.missing_end
    assert chain.missing_start_nodes == [A]
    assert chain.activities_missing_start == [A, B, C, E]
    assert not chain.complete


def test_missing_end_is_distinct_from_missing_start():
    report = build_job_chains(
        [
            event(A, kind="start", uid="1"),
            event(B, related=A, kind="transfer", uid="2"),
            event(C, related=B, kind="transfer", uid="3"),
        ]
    )
    chain = report[0]
    assert not chain.missing_start
    assert chain.missing_end
    assert chain.missing_end_nodes == [C]
    assert not chain.complete


def test_cycle_is_bounded_and_reported():
    report = build_job_chains(
        [
            event(A, kind="start", uid="1"),
            event(B, related=A, kind="transfer", uid="2"),
            event(C, related=B, kind="transfer", uid="3"),
            event(A, related=C, kind="transfer", uid="4"),
        ]
    )
    chain = report[0]
    assert chain.cyclic
    assert chain.cycles == [[canonical_id(A), canonical_id(B), canonical_id(C)]]
    assert chain.workload == 4
    paths = chain.paths()
    assert all(len(path) <= 4 for path in paths)
    assert len(paths) == 3


def test_self_relation_does_not_create_new_activity():
    report = build_job_chains(
        [
            event(A, related=A, kind="start", uid="1"),
            event(A, kind="end", uid="2"),
        ]
    )
    chain = report[0]
    assert len(chain.nodes) == 1
    assert chain.cycles == [[canonical_id(A)]]
    assert chain.workload == 2


def test_repeated_event_counts_once():
    payload = event(A, related=B, kind="transfer", uid="same-uid")
    report = build_job_chains([payload, dict(payload), dict(payload)])
    assert report.workload == 1
    assert report[0].workload == 1
    assert report[0].nodes[canonical_id(A)].event_count == 1


def test_record_identity_deduplicates_without_uid():
    event_a = event(A, kind="start", provider="Collector", event_id="100")
    event_a["record_id"] = "7"
    event_a["timestamp"] = "2026-01-01 00:00:00+00:00"
    event_b = dict(event_a)
    report = build_job_chains([event_a, event_b])
    assert report.workload == 1


def test_same_event_referenced_from_multiple_chains_counts_once():
    shared = event(A, kind="start", uid="shared")
    report = build_job_chains(
        [
            shared,
            event(B, related=A, kind="transfer", uid="b"),
            event(C, kind="start", uid="c"),
            event(D, related=C, kind="transfer", uid="d"),
        ]
    )
    assert len(report) == 2
    assert report.workload == 4


def test_disconnected_activities_form_separate_chains():
    report = build_job_chains(
        [
            event(A, kind="start", uid="1"),
            event(B, related=A, kind="end", uid="2"),
            event(C, kind="start", uid="3"),
            event(D, related=C, kind="end", uid="4"),
        ]
    )
    assert len(report) == 2
    assert report.workload == 4


def test_provider_role_mapping():
    report = build_job_chains(
        [
            event(A, provider="Collector.Start", event_id="100", uid="1"),
            event(B, related=A, provider="Collector.Handoff", event_id="200", uid="2"),
            event(B, provider="Collector.End", event_id="300", uid="3"),
        ],
        provider_roles={
            "Collector.Start": START,
            "Collector.Handoff": TRANSFER,
            "Collector.End": END,
        },
    )
    assert report[0].node(A).has_start
    assert report[0].node(B).has_end
    assert report[0].start_count == 1
    assert report[0].end_count == 1


def test_event_id_role_mapping():
    report = build_job_chains(
        [
            event(A, event_id="100", uid="1"),
            event(B, related=A, event_id="200", uid="2"),
            event(B, event_id="300", uid="3"),
        ],
        event_id_roles={"100": START, "200": TRANSFER, "300": END},
    )
    assert report[0].complete


def test_data_role_hint():
    report = build_job_chains(
        [
            {"uid": "1", "activity_id": A, "event_id": "1", "data": {"Stage": "start"}},
            {"uid": "2", "activity_id": B, "related_activity_id": A, "event_id": "2", "data": {"Stage": "transfer"}},
            {"uid": "3", "activity_id": B, "event_id": "3", "data": {"Stage": "completed"}},
        ]
    )
    assert report[0].complete


def test_event_from_xml():
    xml = """<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
      <System>
        <Provider Name="Collector"/>
        <EventID>100</EventID>
        <TimeCreated SystemTime="2026-01-01 00:00:00+00:00"/>
        <EventRecordID>42</EventRecordID>
        <Channel>Ops</Channel>
        <Computer>host1</Computer>
        <Correlation ActivityID="{A}" RelatedActivityID="{B}"/>
      </System>
      <EventData>
        <Data Name="Stage">start</Data>
        <Data Name="CorrelationID">B</Data>
        <Data Name="CorrelationID">C</Data>
      </EventData>
    </Event>"""
    record = event_from_xml(xml)
    assert record.activity_id == "{A}"
    assert record.related_activity_ids == ["{B}", "C"]
    assert record.provider == "Collector"
    assert record.event_id == "100"
    assert record.record_id == "42"
    assert record.channel == "Ops"
    assert record.computer == "host1"
    assert record.kind == START


def test_builder_deduplicates_on_add():
    builder = JobChainBuilder()
    builder.add(event(A, kind="start", uid="1"))
    builder.add(event(A, kind="start", uid="1"))
    builder.add(event(B, related=A, kind="end", uid="2"))
    report = builder.build()
    assert report.workload == 2
    assert report[0].complete


def test_report_serializes_to_json():
    report = build_job_chains(
        [
            event(A, kind="start", uid="1"),
            event(B, related=A, kind="transfer", uid="2"),
            event(C, related_ids=[A, B], kind="end", uid="3"),
        ]
    )
    payload = report.to_dict()
    assert payload["chain_count"] == 1
    assert payload["workload"] == 3
    assert payload["chains"][0]["id"] == "chain-1"
    assert json.loads(json.dumps(payload))["chains"][0]["workload"] == 3


def test_cli_json_output(tmp_path):
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            [
                {"uid": "1", "activity_id": A, "kind": "start"},
                {"uid": "2", "activity_id": B, "related_activity_id": A, "kind": "transfer"},
                {"uid": "3", "activity_id": C, "related_activity_ids": [A, B], "kind": "end"},
            ]
        ),
        encoding="utf-8",
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    completed = subprocess.run(
        [sys.executable, os.path.join(root, "evtx_scripts", "evtx_job_chains.py"), str(path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["workload"] == 3
    assert payload["missing_start"] is False
    assert payload["missing_end"] is False


def test_cli_strict_exit_code(tmp_path):
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            [
                {"uid": "1", "activity_id": A, "kind": "start"},
                {"uid": "2", "activity_id": B, "related_activity_id": A, "kind": "transfer"},
            ]
        ),
        encoding="utf-8",
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    completed = subprocess.run(
        [sys.executable, os.path.join(root, "evtx_scripts", "evtx_job_chains.py"), str(path), "--strict"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "MISSING END" in completed.stdout


def test_evtx_round_trip(data_path):
    from Evtx.Evtx import Evtx
    from Evtx.JobChain import event_from_record

    path = os.path.join(data_path, "system.evtx")
    with Evtx(path) as log:
        records = [event_from_record(record) for record in log.records()]
    assert records
    report = build_job_chains(records)
    assert report.workload <= len(records)
