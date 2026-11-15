import os
import struct
import shutil

import pytest

import Evtx.Evtx as evtx


FILE_HEADER_SIZE = evtx.FILE_HEADER_SIZE
CHUNK_SIZE = evtx.CHUNK_SIZE
CHUNK_HEADER_SIZE = evtx.CHUNK_HEADER_SIZE
RECORD_MAGIC = evtx.RECORD_MAGIC


def chunk_offset(index):
    return FILE_HEADER_SIZE + index * CHUNK_SIZE


def record_offsets(path, chunk_index):
    """
    Walk the record boundaries of one chunk using only its layout and the
    declared record sizes, returning absolute offsets and sizes.
    """
    with open(path, "rb") as f:
        data = f.read()
    start = chunk_offset(chunk_index)
    next_record_offset = struct.unpack_from("<I", data, start + 0x30)[0]
    boundary = start + CHUNK_HEADER_SIZE
    end = start + next_record_offset
    out = []
    while boundary + 0x18 <= end:
        magic, size = struct.unpack_from("<II", data, boundary)
        assert magic == RECORD_MAGIC
        out.append((boundary, size))
        boundary += size
    return out


@pytest.fixture
def security_copy(data_path, tmp_path):
    """
    A writable, per-test copy of security.evtx and its path.
    """
    src = os.path.join(data_path, "security.evtx")
    dst = tmp_path / "security.evtx"
    shutil.copy(src, dst)
    return str(dst)


def patch(path, offset, payload):
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(payload)


def flip_byte(path, offset):
    with open(path, "r+b") as f:
        f.seek(offset)
        (byte,) = struct.unpack("B", f.read(1))
        f.seek(offset)
        f.write(struct.pack("B", byte ^ 0xFF))


def recover(path, render=False):
    with evtx.Evtx(path) as log:
        result = log.recover()
        if render:
            result.verify_renderable_records()
        return result


def test_recover_intact_file_matches_normal_walk(security_copy):
    """
    On an undamaged file, layout driven recovery yields exactly the same
    records as the normal iterator and reports nothing skipped.
    """
    with evtx.Evtx(security_copy) as log:
        normal_offsets = [r.offset() for r in log.records()]

    result = recover(security_copy, render=True)
    recovered_offsets = [rr.record.offset() for rr in result.record_results if rr.is_valid]

    assert recovered_offsets == normal_offsets
    assert result.total_skipped_bytes == 0
    assert result.skipped_ranges == []
    active = [c for c in result.chunk_results if c.status != evtx.ChunkStatus.EMPTY]
    assert all(c.status == evtx.ChunkStatus.VALID for c in active)
    assert all(c.header_checksum_ok and c.data_checksum_ok for c in active)
    assert all(rr.renders for rr in result.record_results if rr.is_valid)


def test_interior_record_corruption_stops_at_boundary_and_skips_exact_range(security_copy):
    """
    A bad block in the middle: records before it are returned, the bad
    boundary is rejected with a specific reason, and the skipped range is the
    remainder of that chunk only.  Following chunks remain fully readable.
    """
    boundaries = record_offsets(security_copy, 1)
    second_record, _ = boundaries[1]

    patch(security_copy, second_record, struct.pack("<I", 0xDEADBEEF))

    result = recover(security_copy)
    chunk_result = result.chunk_results[1]

    assert chunk_result.records[0].is_valid
    assert chunk_result.records[1].status == evtx.RecordStatus.INVALID_MAGIC
    assert chunk_result.records[1].offset == second_record

    expected_skip = (second_record, chunk_offset(1) + CHUNK_SIZE)
    assert chunk_result.skipped_range == expected_skip
    assert chunk_result.skipped_length == expected_skip[1] - expected_skip[0]

    # chunks before and after the damaged one are untouched
    assert result.chunk_results[0].status == evtx.ChunkStatus.VALID
    assert result.chunk_results[2].status == evtx.ChunkStatus.VALID
    assert result.chunk_results[2].skipped_range is None

    # the skipped range is expressed against the original file layout
    assert all(0 <= start <= end for start, end in result.skipped_ranges)


def test_destroyed_chunk_header_does_not_block_later_chunks(security_copy):
    """
    Even when a chunk header is unreadable, later chunks are reached through
    fixed geometry, not by scanning.
    """
    target = chunk_offset(2)
    patch(security_copy, target, b"BADMAGIC")

    result = recover(security_copy)
    damaged = result.chunk_results[2]

    assert damaged.status == evtx.ChunkStatus.INVALID_MAGIC
    assert damaged.skipped_range == (target + CHUNK_HEADER_SIZE, target + CHUNK_SIZE)
    assert damaged.records == []

    assert result.chunk_results[1].status == evtx.ChunkStatus.VALID
    assert result.chunk_results[3].status == evtx.ChunkStatus.VALID
    assert len(result.chunk_results[3].records) > 0


def test_record_magic_inside_payload_is_not_a_boundary(security_copy):
    """
    The record signature appearing by chance inside a payload must never be
    used as a recovery anchor: the set of boundaries is identical to the
    intact file.
    """
    baseline = recover(security_copy)
    baseline_offsets = [rr.offset for rr in baseline.record_results]

    first_record, _ = record_offsets(security_copy, 0)[0]
    injection = first_record + 0x60  # well inside the first record's payload
    patch(security_copy, injection, struct.pack("<I", RECORD_MAGIC))

    result = recover(security_copy)
    recovered_offsets = [rr.offset for rr in result.record_results]

    assert injection not in recovered_offsets
    assert recovered_offsets == baseline_offsets
    assert result.total_skipped_bytes == 0


def test_data_checksum_failure_is_distinct_from_header_checksum(security_copy):
    """
    A flipped data byte invalidates the data CRC but not the header CRC, and
    does not disturb any record boundary.
    """
    first_record, _ = record_offsets(security_copy, 1)[0]
    flip_byte(security_copy, first_record + 0x30)

    result = recover(security_copy)
    chunk_result = result.chunk_results[1]

    assert chunk_result.header_checksum_ok is True
    assert chunk_result.data_checksum_ok is False
    assert chunk_result.status == evtx.ChunkStatus.DATA_CHECKSUM_FAILED
    assert all(rr.is_valid for rr in chunk_result.records)


def test_header_checksum_failure_is_detected(security_copy):
    """
    A flipped header byte (outside the data area) fails only the header CRC.
    """
    flip_byte(security_copy, chunk_offset(1) + 0x10)

    result = recover(security_copy)
    chunk_result = result.chunk_results[1]

    assert chunk_result.header_checksum_ok is False
    assert chunk_result.data_checksum_ok is True
    assert chunk_result.status == evtx.ChunkStatus.HEADER_CHECKSUM_FAILED


def test_invalid_declared_size_is_reported_and_walk_stops(security_copy):
    second_record, _ = record_offsets(security_copy, 1)[1]
    patch(security_copy, second_record + 4, struct.pack("<I", CHUNK_SIZE * 2))

    result = recover(security_copy)
    chunk_result = result.chunk_results[1]

    assert chunk_result.records[1].status == evtx.RecordStatus.INVALID_SIZE
    assert chunk_result.skipped_range[0] == second_record


def test_trailing_size_copy_mismatch_is_reported(security_copy):
    second_record, size = record_offsets(security_copy, 1)[1]
    patch(security_copy, second_record + size - 4, struct.pack("<I", size + 8))

    result = recover(security_copy)
    chunk_result = result.chunk_results[1]

    assert chunk_result.records[1].status == evtx.RecordStatus.SIZE_MISMATCH
    assert chunk_result.skipped_range[0] == second_record


def test_truncated_final_chunk_is_marked_inaccessible(security_copy):
    cut = chunk_offset(3) + 0x80  # truncated inside the chunk header
    with open(security_copy, "r+b") as f:
        f.truncate(cut)

    result = recover(security_copy)
    last = result.chunk_results[-1]

    assert last.status == evtx.ChunkStatus.INACCESSIBLE
    # no data area exists in the file, so there is no in-file range to skip
    assert last.skipped_range is None
    assert result.chunk_results[2].status == evtx.ChunkStatus.VALID


def test_truncated_chunk_inside_data_reports_existing_range(security_copy):
    cut = chunk_offset(3) + 0x300  # 0x100 bytes of data area present
    with open(security_copy, "r+b") as f:
        f.truncate(cut)

    result = recover(security_copy)
    last = result.chunk_results[-1]

    assert last.status == evtx.ChunkStatus.INACCESSIBLE
    assert last.skipped_range == (chunk_offset(3) + CHUNK_HEADER_SIZE, cut)


def test_structural_validity_is_separate_from_payload_rendering(data_path):
    """
    Content corruption leaves headers and boundaries intact: every record is
    structurally recovered, but the undecodable payloads carry a render error
    and are not conflated with skipped raw ranges.
    """
    path = os.path.join(data_path, "dns_log_malformed.evtx")
    result = recover(path, render=True)

    valid = [rr for cr in result.chunk_results for rr in cr.records if rr.is_valid]
    assert len(valid) == 5
    assert sum(1 for rr in valid if not rr.renders) == 4
    assert result.total_skipped_bytes == 0
    failed = next(rr for rr in valid if not rr.renders)
    assert "UnicodeDecodeError" in failed.render_error


def test_recovered_records_render_to_xml(data_path):
    path = os.path.join(data_path, "security.evtx")
    with evtx.Evtx(path) as log:
        result = log.recover()
        record = next(result.records)
        assert record.xml().startswith("<Event")
        assert record.record_num() >= 1
