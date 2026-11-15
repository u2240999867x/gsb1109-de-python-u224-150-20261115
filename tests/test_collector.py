import binascii
import os
import shutil
import struct

import pytest

from Evtx.Collector import (
    ChangeKind,
    CollectorCheckpoint,
    CollectorError,
    DeliveryError,
    EvtxCollector,
    collect_evtx,
)
from Evtx.Evtx import FileHeader


def _refresh_chunk_checksums(buf, chunk):
    base = chunk.offset()
    next_offset = struct.unpack_from("<I", buf, base + 0x30)[0]
    struct.pack_into("<I", buf, base + 0x34, binascii.crc32(bytes(buf[base + 0x200 : base + next_offset])) & 0xFFFFFFFF)
    header = bytes(buf[base : base + 0x78]) + bytes(buf[base + 0x80 : base + 0x200])
    struct.pack_into("<I", buf, base + 0x7C, binascii.crc32(header) & 0xFFFFFFFF)


def _refresh_file_header_checksum(buf):
    struct.pack_into("<I", buf, 0x7C, binascii.crc32(bytes(buf[0:0x78])) & 0xFFFFFFFF)


def _copy_fixture(data_path, tmp_path, name="Security.evtx"):
    target = tmp_path / name
    shutil.copy(os.path.join(data_path, "security.evtx"), target)
    return str(target)


def _append_record(path, record_number):
    """Append a copy of an existing record to the log's current chunk."""

    buf = bytearray(open(path, "rb").read())
    fh = FileHeader(buf, 0)
    chunks = list(fh.chunks())
    current = chunks[fh.current_chunk_number() % len(chunks)]
    template = next(current.records()).data()

    data = bytearray(template)
    struct.pack_into("<Q", data, 8, record_number)

    end = current.next_record_offset()
    assert end + len(data) <= 0x10000
    buf[current.offset() + end : current.offset() + end + len(data)] = data
    struct.pack_into("<I", buf, current.offset() + 0x30, end + len(data))
    struct.pack_into("<I", buf, current.offset() + 0x2C, end)
    struct.pack_into("<Q", buf, current.offset() + 0x20, record_number)
    struct.pack_into("<Q", buf, current.offset() + 0x10, record_number)
    struct.pack_into("<Q", buf, 0x18, record_number + 1)

    _refresh_chunk_checksums(buf, current)
    _refresh_file_header_checksum(buf)
    with open(path, "wb") as f:
        f.write(buf)


def _insert_record(path, chunk_index, after_record_number, record_number):
    """Insert a record ahead of the delivered frontier and shift the rest."""

    buf = bytearray(open(path, "rb").read())
    fh = FileHeader(buf, 0)
    chunk = list(fh.chunks())[chunk_index]
    records = list(chunk.records())
    anchor = next(record for record in records if record.record_num() == after_record_number)

    data = bytearray(anchor.data())
    struct.pack_into("<Q", data, 8, record_number)
    insert_at = anchor.offset() + anchor.size()
    shift = len(data)
    used = chunk.next_record_offset()

    buf[insert_at + shift : chunk.offset() + used + shift] = buf[insert_at : chunk.offset() + used]
    buf[insert_at : insert_at + shift] = data
    struct.pack_into("<I", buf, chunk.offset() + 0x30, used + shift)
    struct.pack_into("<I", buf, chunk.offset() + 0x2C, chunk.last_record_offset() + shift)
    struct.pack_into("<Q", buf, chunk.offset() + 0x20, record_number)
    struct.pack_into("<Q", buf, chunk.offset() + 0x10, record_number)

    _refresh_chunk_checksums(buf, chunk)
    _refresh_file_header_checksum(buf)
    with open(path, "wb") as f:
        f.write(buf)


def _corrupt_byte(path, chunk_index, relative_offset=0x300):
    buf = bytearray(open(path, "rb").read())
    absolute = 0x1000 + chunk_index * 0x10000 + relative_offset
    buf[absolute] ^= 0xFF
    with open(path, "wb") as f:
        f.write(buf)


def _truncate_last_record(path):
    buf = bytearray(open(path, "rb").read())
    fh = FileHeader(buf, 0)
    chunk = list(fh.chunks())[fh.current_chunk_number()]
    records = list(chunk.records())
    last, previous = records[-1], records[-2]

    struct.pack_into("<I", buf, chunk.offset() + 0x30, last.offset() - chunk.offset())
    struct.pack_into("<I", buf, chunk.offset() + 0x2C, previous.offset() - chunk.offset())
    struct.pack_into("<Q", buf, chunk.offset() + 0x20, previous.record_num())
    struct.pack_into("<Q", buf, chunk.offset() + 0x10, previous.record_num())
    buf[last.offset() : chunk.offset() + 0x10000] = b"\x00" * (chunk.offset() + 0x10000 - last.offset())

    _refresh_chunk_checksums(buf, chunk)
    _refresh_file_header_checksum(buf)
    with open(path, "wb") as f:
        f.write(buf)


def test_first_scan_delivers_every_record(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")

    result = EvtxCollector(path, checkpoint).collect()

    assert result.change is ChangeKind.NEW
    assert result.header_verified is True
    assert result.barrier is None
    assert len(result.emitted) == 2261
    assert [event.record_number for event in result.emitted] == list(range(1, 2262))
    assert result.historical_emitted == 0
    assert os.path.exists(checkpoint)


def test_unchanged_reread_emits_nothing(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    EvtxCollector(path, checkpoint).collect()

    result = EvtxCollector(path, checkpoint).collect()

    assert result.change is ChangeKind.UNCHANGED
    assert result.emitted == []
    assert result.skipped == 2261


def test_append_emits_only_the_new_record(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    EvtxCollector(path, checkpoint).collect()

    _append_record(path, 3000)
    appended = EvtxCollector(path, checkpoint).collect()

    assert appended.change is ChangeKind.APPENDED
    assert [event.record_number for event in appended.emitted] == [3000]
    assert appended.historical_emitted == 0

    again = EvtxCollector(path, checkpoint).collect()
    assert again.change is ChangeKind.UNCHANGED
    assert again.emitted == []


def test_backfilled_history_is_delivered_once(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    EvtxCollector(path, checkpoint).collect()

    _insert_record(path, 3, 300, 500)
    backfill = EvtxCollector(path, checkpoint).collect()

    assert backfill.change is ChangeKind.APPENDED
    assert [event.record_number for event in backfill.emitted] == [500]
    assert backfill.historical_emitted == 1
    assert backfill.emitted[0].historical is True

    # The insert moved every later record; those replays must not be emitted.
    assert backfill.skipped == 2261

    again = EvtxCollector(path, checkpoint).collect()
    assert again.emitted == []


def test_rotated_file_starts_a_new_generation(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    first = EvtxCollector(path, checkpoint).collect()

    replacement = tmp_path / "replacement.evtx"
    shutil.copy(os.path.join(data_path, "system.evtx"), replacement)
    os.replace(replacement, path)

    rotated = EvtxCollector(path, checkpoint).collect()

    assert rotated.change is ChangeKind.ROTATED
    assert rotated.generation != first.generation
    assert len(rotated.emitted) == 1601
    assert rotated.checkpoint.current_generation == rotated.generation
    assert len(rotated.checkpoint.generations) == 2

    again = EvtxCollector(path, checkpoint).collect()
    assert again.change is ChangeKind.UNCHANGED
    assert again.emitted == []


def test_returning_to_an_older_generation_does_not_replay(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    first = EvtxCollector(path, checkpoint).collect()

    replacement = tmp_path / "replacement.evtx"
    shutil.copy(os.path.join(data_path, "system.evtx"), replacement)
    os.replace(replacement, path)
    EvtxCollector(path, checkpoint).collect()

    restored = tmp_path / "restored.evtx"
    shutil.copy(os.path.join(data_path, "security.evtx"), restored)
    os.replace(restored, path)

    back = EvtxCollector(path, checkpoint).collect()

    assert back.generation == first.generation
    assert back.emitted == []
    assert back.skipped == 2261


def test_in_place_rebuild_is_a_new_generation(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    first = EvtxCollector(path, checkpoint).collect()

    with open(path, "r+b") as f:
        f.seek(0)
        f.truncate()
        f.write(open(os.path.join(data_path, "system.evtx"), "rb").read())

    rebuilt = EvtxCollector(path, checkpoint).collect()

    assert rebuilt.change is ChangeKind.ROTATED
    assert rebuilt.generation != first.generation
    assert len(rebuilt.emitted) == 1601

    again = EvtxCollector(path, checkpoint).collect()
    assert again.change is ChangeKind.UNCHANGED
    assert again.emitted == []


def test_truncation_rolls_the_frontier_back(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    EvtxCollector(path, checkpoint).collect()

    _truncate_last_record(path)
    truncated = EvtxCollector(path, checkpoint).collect()

    assert truncated.change is ChangeKind.TRUNCATED
    assert truncated.emitted == []
    state = truncated.checkpoint.generations[truncated.generation]
    assert state.last_record.record_number == 2260
    assert state.chunks_verified == 26

    stable = EvtxCollector(path, checkpoint).collect()
    assert stable.change is ChangeKind.UNCHANGED
    assert stable.emitted == []


def test_corrupt_chunk_blocks_the_checkpoint_frontier(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")

    _corrupt_byte(path, 5)
    blocked = EvtxCollector(path, checkpoint).collect()

    assert blocked.change is ChangeKind.NEW
    assert blocked.barrier is not None
    assert blocked.barrier.chunk_index == 5
    assert "checksum" in blocked.barrier.reason
    # Five intact chunks contain 441 records; the checkpoint must not advance
    # past the damaged chunk even though later chunks are readable.
    assert [event.record_number for event in blocked.emitted] == list(range(1, 442))
    assert blocked.verified_through == 0x1000 + 5 * 0x10000
    state = blocked.checkpoint.generations[blocked.generation]
    assert state.last_record.record_number == 441
    assert state.chunks_verified == 5

    _corrupt_byte(path, 5)
    recovered = EvtxCollector(path, checkpoint).collect()

    assert recovered.barrier is None
    assert [event.record_number for event in recovered.emitted] == list(range(442, 2262))

    again = EvtxCollector(path, checkpoint).collect()
    assert again.change is ChangeKind.UNCHANGED
    assert again.emitted == []


def test_later_corrupt_chunk_does_not_redeliver(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    EvtxCollector(path, checkpoint).collect()

    _corrupt_byte(path, 5)
    blocked = EvtxCollector(path, checkpoint).collect()

    assert blocked.change is ChangeKind.BLOCKED
    assert blocked.emitted == []
    assert blocked.verified_through == 0x1000 + 5 * 0x10000
    state = blocked.checkpoint.generations[blocked.generation]
    assert state.chunks_verified == 5
    assert state.last_record.record_number == 441

    _corrupt_byte(path, 5)
    recovered = EvtxCollector(path, checkpoint).collect()

    assert recovered.barrier is None
    assert recovered.emitted == []
    assert recovered.skipped == 2261


def test_delivery_failure_resumes_at_the_failed_record(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    delivered = []

    def fail_at_101(event):
        if event.record_number == 101:
            raise RuntimeError("sink unavailable")
        delivered.append(event.record_number)

    with pytest.raises(DeliveryError):
        EvtxCollector(path, checkpoint).collect(on_record=fail_at_101)

    assert delivered == list(range(1, 101))

    resumed = []
    result = EvtxCollector(path, checkpoint).collect(on_record=lambda event: resumed.append(event.record_number))

    assert result.change is ChangeKind.APPENDED
    assert resumed == list(range(101, 2262))
    assert set(delivered).isdisjoint(resumed)


def test_corrupt_file_header_is_not_trusted(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    buf = bytearray(open(path, "rb").read())
    buf[0x7C] ^= 0xFF
    with open(path, "wb") as f:
        f.write(buf)

    result = EvtxCollector(path, checkpoint).collect()

    assert result.header_verified is False
    assert result.emitted == []
    assert result.barrier is not None
    assert result.barrier.offset == 0
    assert result.checkpoint.generations == {}


def test_checkpoint_is_atomic_and_versioned(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    EvtxCollector(path, checkpoint).collect()

    loaded = CollectorCheckpoint.load(checkpoint)
    assert loaded.source_path == os.path.abspath(path)
    assert loaded.version == 1
    assert loaded.current_generation in loaded.generations
    assert not glob_checkpoint_temps(tmp_path)


def glob_checkpoint_temps(tmp_path):
    return list(tmp_path.glob(".evtx-checkpoint-*.tmp"))


def test_checkpoint_refuses_a_different_source(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    EvtxCollector(path, checkpoint).collect()

    other = tmp_path / "Other.evtx"
    shutil.copy(os.path.join(data_path, "system.evtx"), other)

    with pytest.raises(CollectorError):
        EvtxCollector(str(other), checkpoint).collect()


def test_collect_evtx_convenience_api(data_path, tmp_path):
    path = _copy_fixture(data_path, tmp_path)
    checkpoint = str(tmp_path / "checkpoint.json")
    seen = []

    result = collect_evtx(path, checkpoint, on_record=lambda event: seen.append(event.record_number))

    assert result.change is ChangeKind.NEW
    assert seen == [event.record_number for event in result.emitted]
    assert CollectorCheckpoint.load(checkpoint).current_generation == result.generation
