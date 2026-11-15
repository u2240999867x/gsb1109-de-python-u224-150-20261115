import json
import os

import Evtx.Evtx as evtx
import Evtx.Fields as e_fields


def open_log(data_path, name):
    return evtx.Evtx(os.path.join(data_path, name))


def rows_for(data_path, name, record_number, **kwargs):
    with open_log(data_path, name) as log:
        record = log.get_record(record_number)
        assert record is not None
        return e_fields.record_fields(record, **kwargs)


def test_columns_are_fixed():
    expected = [
        "field_id",
        "record_number",
        "event_record_id",
        "section",
        "path",
        "parent_path",
        "depth",
        "position",
        "occurrence",
        "namespace",
        "prefix",
        "element",
        "name",
        "name_namespace",
        "value",
        "is_null",
        "is_empty",
        "empty_element",
        "has_children",
        "type_code",
        "type_name",
        "value_type",
        "attributes",
        "namespace_declarations",
    ]
    assert list(e_fields.FIELD_COLUMNS) == expected


def test_named_data_fields_keep_order(data_path):
    rows = rows_for(data_path, "system.evtx", 12050)
    data = [row for row in rows if row["element"] == "Data"]

    assert [row["name"] for row in data] == ["param1", "param2"]
    assert [row["value"] for row in data] == ["Windows Modules Installer", "stopped"]
    assert [row["position"] for row in rows] == sorted(row["position"] for row in rows)
    assert [row["path"] for row in rows] == [
        "/EventData[1]",
        "/EventData[1]/Data[1]",
        "/EventData[1]/Data[2]",
        "/EventData[1]/Binary[1]",
    ]


def test_repeated_names_use_distinct_paths(data_path):
    # A synthetic body exercises the duplicate-name rule directly, because the
    # canonical fixtures happen to use unique names within one EventData.
    rows = duplicate_name_rows()
    data = [row for row in rows if row["element"] == "Data"]

    assert [row["path"] for row in data] == ["/EventData[1]/Data[1]", "/EventData[1]/Data[2]"]
    assert [row["name"] for row in data] == ["SubjectUserName", "SubjectUserName"]
    assert [row["occurrence"] for row in data] == [1, 2]
    assert len({row["field_id"] for row in rows}) == len(rows)

    rebuilt = e_fields.rebuild_body_xml(rows)
    assert rebuilt.count("<Data ") == 2
    assert rebuilt.index(">one<") < rebuilt.index(">two<")


def duplicate_name_rows():
    rows = []
    for position, (sibling, value) in enumerate((("first", "one"), ("second", "two")), 1):
        rows.append(
            e_fields.FieldRow(
                (
                    ("field_id", "7:/EventData[1]/Data[%d]" % position),
                    ("record_number", 7),
                    ("event_record_id", "7"),
                    ("section", "EventData"),
                    ("path", "/EventData[1]/Data[%d]" % position),
                    ("parent_path", "/EventData[1]"),
                    ("depth", 1),
                    ("position", position + 1),
                    ("occurrence", position),
                    ("namespace", "http://schemas.microsoft.com/win/2004/08/events/event"),
                    ("prefix", ""),
                    ("element", "Data"),
                    ("name", "SubjectUserName"),
                    ("name_namespace", ""),
                    ("value", value),
                    ("is_null", False),
                    ("is_empty", False),
                    ("empty_element", False),
                    ("has_children", False),
                    ("type_code", 1),
                    ("type_name", "WSTRING"),
                    ("value_type", "WstringTypeNode"),
                    (
                        "attributes",
                        [
                            {
                                "name": "Name",
                                "prefix": "",
                                "local_name": "Name",
                                "namespace": "",
                                "value": "SubjectUserName",
                            }
                        ],
                    ),
                    ("namespace_declarations", []),
                )
            )
        )
    rows.insert(
        0,
        e_fields.FieldRow(
            (
                ("field_id", "7:/EventData[1]"),
                ("record_number", 7),
                ("event_record_id", "7"),
                ("section", "EventData"),
                ("path", "/EventData[1]"),
                ("parent_path", ""),
                ("depth", 0),
                ("position", 1),
                ("occurrence", 1),
                ("namespace", "http://schemas.microsoft.com/win/2004/08/events/event"),
                ("prefix", ""),
                ("element", "EventData"),
                ("name", None),
                ("name_namespace", None),
                ("value", ""),
                ("is_null", False),
                ("is_empty", True),
                ("empty_element", False),
                ("has_children", True),
                ("type_code", 0x21),
                ("type_name", "BXML"),
                ("value_type", "BXmlTypeNode"),
                ("attributes", []),
                ("namespace_declarations", []),
            )
        ),
    )
    return rows


def test_unnamed_data_is_not_given_a_name(data_path):
    rows = rows_for(data_path, "system.evtx", 12091)
    data = [row for row in rows if row["element"] == "Data"]

    assert len(data) == 1
    assert data[0]["name"] is None
    assert data[0]["name_namespace"] is None
    assert data[0]["is_null"] is False
    assert data[0]["type_name"] == "WSTRINGARRAY"
    assert data[0]["value_type"] == "WstringArrayTypeNode"


def test_xml_looking_value_stays_text(data_path):
    record_number = 12091
    rows = rows_for(data_path, "system.evtx", record_number)
    data = next(row for row in rows if row["element"] == "Data")

    assert "<string>" in data["value"]
    assert data["value_type"] == "WstringArrayTypeNode"
    assert data["type_code"] == 0x81
    assert data["has_children"] is False

    with open_log(data_path, "system.evtx") as log:
        record = log.get_record(record_number)
        assert e_fields.verify_roundtrip(record, rows)

    rebuilt = e_fields.rebuild_body_xml(rows)
    assert "&lt;string&gt;" in rebuilt
    assert "<string>1039089</string>" not in rebuilt


def test_null_and_empty_are_distinct(data_path):
    rows = rows_for(data_path, "system.evtx", 12141)
    empty_data = next(row for row in rows if row["element"] == "Data")
    assert empty_data["is_null"] is True
    assert empty_data["is_empty"] is True
    assert empty_data["value"] == ""

    rows = rows_for(data_path, "system.evtx", 12050)
    populated = next(row for row in rows if row["element"] == "Data")
    assert populated["is_null"] is False
    assert populated["is_empty"] is False

    rows = rows_for(data_path, "system.evtx", 12182)
    perf_state = next(row for row in rows if row["name"] == "PerfState")
    assert perf_state["value"] == ""
    assert perf_state["is_empty"] is True
    idle_state = next(row for row in rows if row["name"] == "IdleState")
    assert idle_state["is_null"] is False
    assert idle_state["value"] == "AQ=="


def test_zero_length_value_is_empty_not_null(data_path):
    rows = rows_for(data_path, "system.evtx", 12182)
    perf_state = next(row for row in rows if row["name"] == "PerfState")
    # A zero-length binary value is present, just empty.
    assert perf_state["is_null"] is False
    assert perf_state["is_empty"] is True
    assert perf_state["type_code"] == 0x8E


def test_truly_missing_value_is_null(data_path):
    rows = rows_for(data_path, "system.evtx", 12152)
    binary = next(row for row in rows if row["element"] == "Binary")
    assert binary["is_null"] is True
    assert binary["is_empty"] is True
    assert binary["value"] == ""
    assert binary["value_type"] == "NullTypeNode"


def test_namespaces_are_preserved(data_path):
    rows = rows_for(data_path, "system.evtx", 12049)
    user_data = rows[0]
    assert user_data["section"] == "UserData"
    assert user_data["namespace"] == "http://schemas.microsoft.com/win/2004/08/events/event"

    auto_backup = next(row for row in rows if row["element"] == "AutoBackup")
    assert auto_backup["namespace"] == "http://manifests.microsoft.com/win/2004/08/windows/eventlog"
    declarations = dict(auto_backup["namespace_declarations"])
    assert declarations[""] == "http://manifests.microsoft.com/win/2004/08/windows/eventlog"
    assert declarations["auto-ns3"] == "http://schemas.microsoft.com/win/2004/08/events"

    channel = next(row for row in rows if row["element"] == "Channel")
    assert channel["namespace"] == "http://manifests.microsoft.com/win/2004/08/windows/eventlog"

    rebuilt = e_fields.rebuild_body_xml(rows)
    assert 'xmlns="http://manifests.microsoft.com/win/2004/08/windows/eventlog"' in rebuilt
    assert 'xmlns:auto-ns3="http://schemas.microsoft.com/win/2004/08/events"' in rebuilt


def test_rebuild_preserves_element_order_for_every_record(data_path):
    for name in ("system.evtx", "security.evtx", "issue_38.evtx", "issue_39.evtx"):
        with open_log(data_path, name) as log:
            for record in log.records():
                try:
                    rows = e_fields.record_fields(record)
                except UnicodeDecodeError:
                    continue
                assert e_fields.verify_roundtrip(record, rows), (name, record.record_num())

                rebuilt = e_fields.rebuild_body_xml(rows)
                assert rebuilt.startswith("<%s" % rows[0]["element"])
                section = rows[0]["element"]
                assert rebuilt.count("<%s" % section) == 1


def test_field_id_is_stable_and_unique(data_path):
    rows = rows_for(data_path, "security.evtx", 2)
    field_ids = [row["field_id"] for row in rows]
    assert len(field_ids) == len(set(field_ids))
    assert field_ids[0].startswith("2:")
    assert all(row["event_record_id"] == "2" for row in rows)

    again = rows_for(data_path, "security.evtx", 2)
    assert [row["field_id"] for row in again] == field_ids
    assert [row["path"] for row in again] == [row["path"] for row in rows]


def test_leaves_only_drops_containers(data_path):
    all_rows = rows_for(data_path, "system.evtx", 12049)
    leaves = rows_for(data_path, "system.evtx", 12049, include_containers=False)

    assert len(leaves) < len(all_rows)
    assert all(not row["has_children"] for row in leaves)
    assert {row["element"] for row in leaves} == {"Channel", "BackupPath"}


def test_record_and_file_level_helpers(data_path):
    with open_log(data_path, "system.evtx") as log:
        record = log.get_record(12050)
        assert record.fields() == e_fields.record_fields(record)

        tables = list(log.fields())
        assert tables
        assert all(rows for rows in tables)


def test_csv_and_json_exports(data_path):
    rows = rows_for(data_path, "system.evtx", 12050)

    csv_text = e_fields.rows_to_csv(rows)
    assert csv_text.splitlines()[0] == ",".join(e_fields.FIELD_COLUMNS)
    assert len(csv_text.splitlines()) == len(rows) + 1

    parsed = json.loads(e_fields.rows_to_json(rows))
    assert parsed[1]["attributes"][0]["name"] == "Name"
    assert parsed[1]["attributes"][0]["value"] == "param1"


def test_unknown_type_code_still_reports_code(data_path):
    rows = rows_for(data_path, "system.evtx", 12182)
    complex_data = next(row for row in rows if row["element"] == "ComplexData")
    # The binary XML declares an array-of-binary type (0x8E) even though the
    # stored value is a plain BinaryTypeNode.
    assert complex_data["type_code"] == 0x8E
    assert complex_data["type_name"] == "BINARYARRAY"
    assert complex_data["value_type"] == "BinaryTypeNode"


def test_aliases_do_not_change_serialized_columns(data_path):
    row = rows_for(data_path, "system.evtx", 12050)[1]
    assert row.name == row["name"] == row["field_name"]
    assert row.order == row["position"]
    assert row.original_type == row["type_name"]
    assert list(row.as_dict()) == list(e_fields.FIELD_COLUMNS)
