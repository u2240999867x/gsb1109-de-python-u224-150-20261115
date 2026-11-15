import json
import os
import stat
import struct

import pytest

import pefile


def _make_debug_pe(debug_entries):
    """Build a minimal PE32 with an IMAGE_DIRECTORY_ENTRY_DEBUG directory.

    ``debug_entries`` is a list of ``(type, payload)`` tuples. Each entry's
    payload is placed in the single .debug section immediately after the debug
    directory entries.
    """

    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 60, 64)

    file_header = struct.pack(
        "<HHIIIHH",
        0x14C,
        1,
        0,
        0,
        0,
        0xE0,
        0x0102,
    )

    section_alignment = 0x1000
    file_alignment = 0x200
    section_rva = 0x1000
    section_raw = 0x400
    debug_entries_size = 28 * len(debug_entries)
    payload_base = section_rva + debug_entries_size

    directory_entries = bytearray()
    payload = bytearray()
    for type_id, data in debug_entries:
        payload_offset = payload_base + len(payload)
        directory_entries += struct.pack(
            "<IIHHIIII",
            0,
            0,
            0,
            0,
            type_id,
            len(data),
            payload_offset,
            section_raw + (payload_offset - section_rva),
        )
        payload += data

    content = bytes(directory_entries) + bytes(payload)
    raw_size = (len(content) + file_alignment - 1) & ~(file_alignment - 1)
    size_of_image = section_rva + max(raw_size, section_alignment)

    optional_header = struct.pack("<H", 0x10B)
    optional_header += struct.pack("<BB", 14, 0)
    optional_header += struct.pack("<I", 0)
    optional_header += struct.pack("<II", 0, 0)
    optional_header += struct.pack("<I", section_rva)
    optional_header += struct.pack("<II", section_rva, 0)
    optional_header += struct.pack("<I", 0x400000)
    optional_header += struct.pack("<II", section_alignment, file_alignment)
    optional_header += struct.pack("<HHHHHH", 6, 0, 0, 0, 6, 0)
    optional_header += struct.pack("<I", 0)
    optional_header += struct.pack("<I", size_of_image)
    optional_header += struct.pack("<I", file_alignment)
    optional_header += struct.pack("<I", 0)
    optional_header += struct.pack("<HH", 2, 0x8100)
    optional_header += struct.pack("<IIII", 0x100000, 0x1000, 0x100000, 0x1000)
    optional_header += struct.pack("<II", 0, 16)

    directories = [(0, 0)] * 16
    directories[6] = (section_rva, debug_entries_size)
    for rva, size in directories:
        optional_header += struct.pack("<II", rva, size)
    assert len(optional_header) == 0xE0

    section_header = (
        b".debug\x00\x00"
        + struct.pack("<IIII", raw_size, section_rva, raw_size, section_raw)
        + struct.pack("<IIHHI", 0, 0, 0, 0, 0x42000040)
    )

    headers = bytes(dos) + b"PE\x00\x00" + file_header + optional_header + section_header
    headers = headers.ljust(section_raw, b"\x00")
    return headers + content.ljust(raw_size, b"\x00")


def _rsds(pdb_path, guid=(0x12345678, 0x1234, 0x1234), age=1):
    payload = struct.pack(
        "<4sIHHBB6sI",
        b"RSDS",
        guid[0],
        guid[1],
        guid[2],
        0x41,
        0x42,
        b"CDEFGH",
        age,
    )
    return payload + pdb_path


def _nb10(pdb_path, signature=0x12345678, age=1):
    payload = struct.pack("<IIII", 0x3031424E, 0, signature, age)
    return payload + pdb_path


def _pe(debug_entries):
    return pefile.PE(data=_make_debug_pe(debug_entries))


def test_rsds_identity_is_independent_of_path_and_never_leaks_it():
    original = _pe([(2, _rsds(b"C:\\build\\agent\\project.pdb\0"))])
    anonymized = _pe([(2, _rsds(b"C:\\build\\agent\\project.pdb\0"))])

    original_record = original.get_debug_records(path_policy="redacted")[0]
    anonymized_record = anonymized.get_debug_records(path_policy="basename")[0]

    assert original_record["identity"] == anonymized_record["identity"]
    assert original_record["identity"]["guid"] == "12345678123412344142434445464748"
    assert original_record["identity"]["age"] == 1
    assert original_record["fields"]["PdbFileName"] == "<redacted>"
    assert anonymized_record["fields"]["PdbFileName"] == "project.pdb"
    assert "build" not in json.dumps(original_record)
    assert "agent" not in json.dumps(anonymized_record)


def test_nb10_identity_is_timestamp_and_age():
    with _pe([(2, _nb10(b"D:\\out\\sample.pdb\0"))]) as pe:
        record = pe.get_debug_records()[0]

        assert record["summary"] == "CodeView PDB 2.0 (NB10)"
        assert record["identity"] == {
            "kind": "codeview",
            "format": "timestamp-age",
            "value": "123456781",
            "signature": 0x12345678,
            "age": 1,
        }
        assert record["fields"]["PdbFileName"] == "sample.pdb"


def test_unknown_debug_type_keeps_summary_and_range():
    unknown_type = 0x7F
    payload = b"opaque-debug-data"

    with _pe([(unknown_type, payload)]) as pe:
        record = pe.get_debug_records()[0]

        assert record["type"] == unknown_type
        assert record["type_name"] == "UNKNOWN"
        assert record["summary"] == "Unknown debug type 0x7f"
        assert record["range"]["size"] == len(payload)
        assert record["range"]["available"] == len(payload)
        assert record["range"]["complete"] is True
        assert record["identity"] is None


def test_truncated_payload_range_reports_only_available_bytes():
    payload = b"RSDS" + struct.pack("<IHHBB6sI", 1, 2, 3, 4, 5, b"CDEFGH", 1) + b"x.pdb\0"
    data = bytearray(_make_debug_pe([(2, payload)]))
    # End the file inside the payload, past the 28-byte directory entry.
    del data[0x400 + 28 + len(payload) - 1 :]

    with pefile.PE(data=bytes(data)) as pe:
        record = pe.get_debug_records()[0]

    assert record["range"]["size"] == len(payload)
    assert record["range"]["available"] == len(payload) - 1
    assert record["range"]["complete"] is False
    assert record["range"]["end"] == record["range"]["start"] + len(payload) - 1


def test_path_looking_value_in_non_path_fields_is_preserved():
    payload = b"RSDS" + struct.pack("<IHHBB6sI", 1, 2, 3, 4, 5, b"CDEFGH", 1) + b"a.pdb\0"
    with _pe([(2, payload)]) as pe:
        record = pe.get_debug_records()[0]

    # Signature_Data6 contains the last six bytes of the GUID field;
    # it is not a path field and must not be rewritten.
    assert record["fields"]["Signature_Data6"] == "CDEFGH"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"C:\\build\\a.pdb\0", "a.pdb"),
        (b"/home/user/build/a.pdb\0", "a.pdb"),
        (b"C:a.pdb\0", "a.pdb"),
        (b"a.pdb\0", "a.pdb"),
        (b"  \t\0", ""),
    ],
)
def test_anonymize_debug_path_basename(raw, expected):
    assert pefile.anonymize_debug_path(raw, "basename") == expected


def test_anonymize_debug_path_policies_are_path_free():
    raw = b"D:\\Users\\alice\\secret\\project.pdb\0"

    redacted = pefile.anonymize_debug_path(raw, "redacted")
    hashed = pefile.anonymize_debug_path(raw, "hash")

    assert redacted == "<redacted>"
    assert hashed.startswith("<pdb-") and hashed.endswith(">")
    for value in (redacted, hashed):
        assert "alice" not in value
        assert "secret" not in value
        assert "\\" not in value and "/" not in value
        assert ":" not in value


def test_export_writes_structured_document_and_private_archive(tmp_path):
    archive_dir = tmp_path / "archive"
    with _pe([(2, _rsds(b"C:\\build\\agent\\project.pdb\0")), (0x7F, b"unknown")]) as pe:
        document = pe.export_debug_records(
            output=tmp_path / "records.json",
            raw_archive=archive_dir,
        )
        archive_path = next(archive_dir.glob("*.raw.json"))

        exported = json.loads((tmp_path / "records.json").read_text())

    assert exported == document
    assert exported["record_count"] == 2
    assert "C:\\build" not in json.dumps(exported)
    assert exported["raw_archive"]["sha256"] == pefile.sha256(
        archive_path.read_bytes()
    ).hexdigest()

    archive = json.loads(archive_path.read_text())
    # The archive keeps the original bytes exactly, including the trailing NUL.
    assert archive["records"][0]["entry"]["PdbFileName"] == (
        "C:\\build\\agent\\project.pdb\x00"
    )
    assert archive["records"][0]["payload_base64"]
    assert archive_path.name == f"{document['source']['sha256']}.raw.json"

    mode = stat.S_IMODE(archive_path.stat().st_mode)
    assert mode == 0o600


def test_directory_export_reports_build_relative_paths_only(tmp_path):
    build = tmp_path / "build" / "agent"
    build.mkdir(parents=True)
    (build / "one.exe").write_bytes(_make_debug_pe([(2, _rsds(b"C:\\build\\one.pdb\0"))]))
    (build / "two.dll").write_bytes(_make_debug_pe([(2, _nb10(b"D:\\build\\two.pdb\0"))]))
    (build / "not-pe.txt").write_text("ignored")
    (tmp_path / "archive").mkdir()

    document = pefile.export_debug_records(
        build,
        raw_archive=tmp_path / "archive",
    )

    assert document["sample_count"] == 2
    assert [sample["path"] for sample in document["samples"]] == [
        "one.exe",
        "two.dll",
    ]
    serialized = json.dumps(document)
    assert str(tmp_path) not in serialized
    assert "build/agent" not in serialized
    assert "C:\\build" not in serialized
    assert len(list((tmp_path / "archive").glob("*.raw.json"))) == 2


def test_directory_export_isolates_unreadable_samples(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    (build / "good.exe").write_bytes(
        _make_debug_pe([(2, _rsds(b"C:\\build\\good.pdb\0"))])
    )
    (build / "broken.exe").write_bytes(b"MZ" + b"\x00" * 32)
    archive = tmp_path / "archive"

    document = pefile.export_debug_records(build, raw_archive=archive)

    assert [sample["path"] for sample in document["samples"]] == [
        "broken.exe",
        "good.exe",
    ]
    broken, good = document["samples"]
    assert broken["error"] == "PEFormatError"
    assert broken["record_count"] == 0
    assert "records" not in broken or broken["records"] == []
    assert good["record_count"] == 1
    serialized = json.dumps(document)
    assert str(tmp_path) not in serialized


def test_export_default_archive_location_is_under_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    with _pe([(2, _rsds(b"C:\\build\\a.pdb\0"))]) as pe:
        pe.export_debug_records()

    archives = list((tmp_path / "state" / "pefile" / "debug-archive").glob("*.raw.json"))
    assert len(archives) == 1
    assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600


def test_unknown_path_policy_is_rejected():
    with _pe([(2, _rsds(b"C:\\build\\a.pdb\0"))]) as pe:
        with pytest.raises(ValueError):
            pe.get_debug_records(path_policy="unsafe")


def test_no_debug_directory_yields_empty_records():
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 60, 64)
    file_header = struct.pack("<HHIIIHH", 0x14C, 0, 0, 0, 0, 0xE0, 0x0102)
    optional_header = struct.pack("<H", 0x10B)
    optional_header += b"\x00" * (0xE0 - 2)
    data = bytes(dos) + b"PE\x00\x00" + file_header + optional_header

    with pefile.PE(data=data) as pe:
        assert pe.get_debug_records() == []
        assert not hasattr(pe, "DIRECTORY_ENTRY_DEBUG")


def test_fast_load_parses_debug_directory_on_demand(tmp_path):
    path = tmp_path / "sample.exe"
    path.write_bytes(_make_debug_pe([(2, _rsds(b"C:\\build\\a.pdb\0"))]))

    with pefile.PE(path, fast_load=True) as pe:
        assert not hasattr(pe, "DIRECTORY_ENTRY_DEBUG")
        records = pe.get_debug_records()
        assert len(records) == 1
        assert records[0]["fields"]["PdbFileName"] == "a.pdb"
