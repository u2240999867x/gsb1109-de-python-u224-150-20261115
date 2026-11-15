"""Test parsing of multi-volume archives with missing middle volumes.
"""

import os
import shutil
import struct
from binascii import crc32

import pytest

import rarfile


def _copy_vols(dst: str, src_names: list[str], dst_names: list[str] | None = None) -> None:
    for i, src in enumerate(src_names):
        name = dst_names[i] if dst_names else src
        shutil.copy(os.path.join("test/files", src), os.path.join(dst, name))


def _rows(rf: rarfile.RarFile) -> dict[str, rarfile.SupplyEntry]:
    return {row.filename: row for row in rf.supply_table()}


def _check_gap_set(rf: rarfile.RarFile) -> None:
    """Common checks for a 3-volume set with missing volume 1."""
    assert rf.missing_volumes() == [1]
    assert rf.strerror() is not None

    table = _rows(rf)
    big = table["vols/bigfile.txt"]
    assert big.header_visible is True
    assert big.payload_complete is False
    assert big.missing_volumes == [1]
    small = table["vols/smallfile.txt"]
    assert small.header_visible is True
    assert small.payload_complete is True
    assert small.missing_volumes == []

    # independent file in later volume is deliverable
    assert len(rf.read("vols/smallfile.txt")) == 2050

    # split file is not
    with pytest.raises(rarfile.MissingVolumeError) as exp:
        rf.read("vols/bigfile.txt")
    assert exp.value.volume == 1


def _check_filled_set(rf: rarfile.RarFile, small_info: rarfile.RarInfo) -> None:
    """Common checks after the missing volume was supplied."""
    assert rf.missing_volumes() == []
    assert rf.strerror() is None
    table = _rows(rf)
    assert table["vols/bigfile.txt"].payload_complete is True
    assert table["vols/bigfile.txt"].missing_volumes == []
    # unaffected entries were not re-processed
    assert rf.getinfo("vols/smallfile.txt") is small_info
    # affected entry now knows full size
    assert rf.getinfo("vols/bigfile.txt").compress_size == 205000
    # and reads fine, with crc check
    assert len(rf.read("vols/bigfile.txt")) == 205000


def test_missing_vol_rar5(tmp_path: object) -> None:
    dst = str(tmp_path)
    _copy_vols(dst, ["rar5-vols.part1.rar", "rar5-vols.part3.rar"])
    rf = rarfile.RarFile(os.path.join(dst, "rar5-vols.part1.rar"))
    _check_gap_set(rf)
    assert [os.path.basename(v) for v in rf.volumelist()] == [
        "rar5-vols.part1.rar", "rar5-vols.part3.rar"]

    small_info = rf.getinfo("vols/smallfile.txt")
    _copy_vols(dst, ["rar5-vols.part2.rar"])
    assert rf.update_volumes() is True
    assert [os.path.basename(v) for v in rf.volumelist()] == [
        "rar5-vols.part1.rar", "rar5-vols.part2.rar", "rar5-vols.part3.rar"]
    _check_filled_set(rf, small_info)


def test_missing_vol_rar3(tmp_path: object) -> None:
    dst = str(tmp_path)
    _copy_vols(dst, ["rar3-vols.part1.rar", "rar3-vols.part3.rar"])
    rf = rarfile.RarFile(os.path.join(dst, "rar3-vols.part1.rar"))
    _check_gap_set(rf)

    small_info = rf.getinfo("vols/smallfile.txt")
    _copy_vols(dst, ["rar3-vols.part2.rar"])
    assert rf.update_volumes() is True
    _check_filled_set(rf, small_info)


def test_missing_vol_rar3_oldstyle(tmp_path: object) -> None:
    dst = str(tmp_path)
    _copy_vols(dst, ["rar3-old.rar", "rar3-old.r01"])
    rf = rarfile.RarFile(os.path.join(dst, "rar3-old.rar"))
    _check_gap_set(rf)

    small_info = rf.getinfo("vols/smallfile.txt")
    _copy_vols(dst, ["rar3-old.r00"])
    assert rf.update_volumes() is True
    _check_filled_set(rf, small_info)


def test_missing_vol_last_volumes(tmp_path: object) -> None:
    dst = str(tmp_path)
    _copy_vols(dst, ["rar5-vols.part1.rar"])
    rf = rarfile.RarFile(os.path.join(dst, "rar5-vols.part1.rar"))
    # only the immediate next volume is certainly missing
    assert rf.missing_volumes() == [1]
    table = _rows(rf)
    # smallfile sits in an unavailable volume, it is not even visible
    assert "vols/smallfile.txt" not in table
    big = table["vols/bigfile.txt"]
    assert big.header_visible is True
    assert big.payload_complete is False
    assert big.missing_volumes == [1]


def test_missing_vol_sequence_mismatch(tmp_path: object) -> None:
    dst = str(tmp_path)
    # foreign volume under expected name: right name, wrong sequence evidence
    _copy_vols(dst,
               ["rar5-vols.part1.rar", "rar5-vols.part1.rar", "rar5-vols.part3.rar"],
               ["rar5-vols.part1.rar", "rar5-vols.part2.rar", "rar5-vols.part3.rar"])
    rf = rarfile.RarFile(os.path.join(dst, "rar5-vols.part1.rar"))
    # foreign volume was not joined, genuine later volume still works
    assert rf.missing_volumes() == [1]
    assert "Volume sequence mismatch" in (rf.strerror() or "")
    assert len(rf.read("vols/smallfile.txt")) == 2050
    with pytest.raises(rarfile.MissingVolumeError):
        rf.read("vols/bigfile.txt")

    # foreign volume is refused again, genuine one is accepted
    assert rf.update_volumes() is False
    assert rf.missing_volumes() == [1]
    _copy_vols(dst, ["rar5-vols.part2.rar"])
    assert rf.update_volumes() is True
    assert rf.missing_volumes() == []
    assert len(rf.read("vols/bigfile.txt")) == 205000


def test_missing_vol_rar3_endarc_mismatch(tmp_path: object) -> None:
    dst = str(tmp_path)
    # part3 content under part2 name: ENDARC volnr does not match
    _copy_vols(dst,
               ["rar3-vols.part1.rar", "rar3-vols.part3.rar", "rar3-vols.part3.rar"],
               ["rar3-vols.part1.rar", "rar3-vols.part2.rar", "rar3-vols.part3.rar"])
    rf = rarfile.RarFile(os.path.join(dst, "rar3-vols.part1.rar"))
    # foreign volume rejected, genuine part3 still joined
    assert rf.missing_volumes() == [1]
    assert len(rf.read("vols/smallfile.txt")) == 2050
    with pytest.raises(rarfile.MissingVolumeError):
        rf.read("vols/bigfile.txt")
    # refused on rescan as well
    assert rf.update_volumes() is False
    assert rf.missing_volumes() == [1]
    _copy_vols(dst, ["rar3-vols.part2.rar"])
    assert rf.update_volumes() is True
    assert len(rf.read("vols/bigfile.txt")) == 205000


def test_missing_vol_testrar(tmp_path: object) -> None:
    dst = str(tmp_path)
    _copy_vols(dst, ["rar5-vols.part1.rar", "rar5-vols.part3.rar"])
    rf = rarfile.RarFile(os.path.join(dst, "rar5-vols.part1.rar"))
    with pytest.raises(rarfile.MissingVolumeError):
        rf.testrar()


##
## synthetic RAR5 volume set with a file starting in the missing volume
##

RAR5_ID = b"Rar!\x1a\x07\x01\x00"
R5_MAIN, R5_FILE, R5_ENDARC = 1, 2, 5
R5_DATA, R5_SPLIT_BEFORE, R5_SPLIT_AFTER = 0x02, 0x08, 0x10


def _vint(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _r5_block(btype: int, bflags: int, body: bytes, data: bytes = b"") -> bytes:
    if data:
        bflags |= R5_DATA
    payload = _vint(btype) + _vint(bflags)
    if data:
        payload += _vint(len(data))
    payload += body
    hdr = _vint(len(payload)) + payload
    return struct.pack("<L", crc32(hdr)) + hdr + data


def _r5_main(volnr: int | None) -> bytes:
    flags = 0x01      # ISVOL
    body = b""
    if volnr is not None:
        flags |= 0x02  # HAS_VOLNR
        body = _vint(volnr)
    return _r5_block(R5_MAIN, 0, _vint(flags) + body)


def _r5_file(name: bytes, size: int, data: bytes, crc: int, split: int = 0) -> bytes:
    body = _vint(0x04)            # HAS_CRC32
    body += _vint(size)           # full unpacked size
    body += _vint(0o100644)       # mode
    body += struct.pack("<L", crc)
    body += _vint(0)              # compress flags: stored
    body += _vint(1)              # host os: unix
    body += _vint(len(name)) + name
    return _r5_block(R5_FILE, split, body, data)


def _r5_endarc(next_vol: bool) -> bytes:
    return _r5_block(R5_ENDARC, 0, _vint(1 if next_vol else 0))


def test_missing_vol_orphan_part(tmp_path: object) -> None:
    dst = str(tmp_path)
    data_a = b"A" * 100
    data_x1 = b"X" * 100
    data_x2 = b"x" * 50
    data_x = data_x1 + data_x2
    data_y = b"Y" * 60
    data_z = b"Z" * 70

    vol0 = RAR5_ID + _r5_main(None) \
        + _r5_file(b"fileA.txt", len(data_a), data_a, crc32(data_a)) \
        + _r5_endarc(True)
    vol1 = RAR5_ID + _r5_main(1) \
        + _r5_file(b"fileX.txt", len(data_x), data_x1, crc32(data_x1), R5_SPLIT_AFTER) \
        + _r5_file(b"fileY.txt", len(data_y), data_y, crc32(data_y)) \
        + _r5_endarc(True)
    vol2 = RAR5_ID + _r5_main(2) \
        + _r5_file(b"fileX.txt", len(data_x), data_x2, crc32(data_x), R5_SPLIT_BEFORE) \
        + _r5_file(b"fileZ.txt", len(data_z), data_z, crc32(data_z)) \
        + _r5_endarc(False)

    with open(os.path.join(dst, "synth.part1.rar"), "wb") as f:
        f.write(vol0)
    with open(os.path.join(dst, "synth.part3.rar"), "wb") as f:
        f.write(vol2)

    rf = rarfile.RarFile(os.path.join(dst, "synth.part1.rar"))
    assert rf.missing_volumes() == [1]

    # fileX starts in the missing volume, fileY sits fully inside it
    assert rf.namelist() == ["fileA.txt", "fileZ.txt"]
    table = _rows(rf)
    assert table["fileA.txt"].payload_complete is True
    assert table["fileZ.txt"].payload_complete is True
    assert "fileY.txt" not in table
    # continuation part of fileX is visible, but not deliverable
    row_x = table["fileX.txt"]
    assert row_x.header_visible is True
    assert row_x.payload_complete is False
    assert row_x.missing_volumes == [1]

    # independent files are deliverable
    assert rf.read("fileA.txt") == data_a
    assert rf.read("fileZ.txt") == data_z

    # supply the missing volume, only it gets processed
    info_a = rf.getinfo("fileA.txt")
    with open(os.path.join(dst, "synth.part2.rar"), "wb") as f:
        f.write(vol1)
    assert rf.update_volumes() is True
    assert rf.missing_volumes() == []
    assert rf.getinfo("fileA.txt") is info_a
    assert rf.namelist() == ["fileA.txt", "fileX.txt", "fileY.txt", "fileZ.txt"]
    assert rf.read("fileX.txt") == data_x
    assert rf.read("fileY.txt") == data_y
    table = _rows(rf)
    assert table["fileX.txt"].payload_complete is True
    assert table["fileX.txt"].missing_volumes == []


def test_missing_vol_cascading_fill(tmp_path: object) -> None:
    dst = str(tmp_path)
    data_x1 = b"X" * 100
    data_x2 = b"x" * 50
    data_x = data_x1 + data_x2
    data_y = b"Y" * 60
    data_z = b"Z" * 70

    vol0 = RAR5_ID + _r5_main(None) + _r5_endarc(True)
    vol1 = RAR5_ID + _r5_main(1) \
        + _r5_file(b"fileX.txt", len(data_x), data_x1, crc32(data_x1), R5_SPLIT_AFTER) \
        + _r5_file(b"fileY.txt", len(data_y), data_y, crc32(data_y)) \
        + _r5_endarc(True)
    vol2 = RAR5_ID + _r5_main(2) \
        + _r5_file(b"fileX.txt", len(data_x), data_x2, crc32(data_x), R5_SPLIT_BEFORE) \
        + _r5_file(b"fileZ.txt", len(data_z), data_z, crc32(data_z)) \
        + _r5_endarc(False)

    with open(os.path.join(dst, "synth.part1.rar"), "wb") as f:
        f.write(vol0)
    rf = rarfile.RarFile(os.path.join(dst, "synth.part1.rar"))
    assert rf.missing_volumes() == [1]
    assert rf.namelist() == []

    # middle volume arrives first, tail is still missing
    with open(os.path.join(dst, "synth.part2.rar"), "wb") as f:
        f.write(vol1)
    assert rf.update_volumes() is True
    assert rf.missing_volumes() == [2]
    assert rf.namelist() == ["fileX.txt", "fileY.txt"]
    assert rf.read("fileY.txt") == data_y
    with pytest.raises(rarfile.MissingVolumeError):
        rf.read("fileX.txt")

    # last volume arrives, set is complete
    with open(os.path.join(dst, "synth.part3.rar"), "wb") as f:
        f.write(vol2)
    assert rf.update_volumes() is True
    assert rf.missing_volumes() == []
    assert rf.read("fileX.txt") == data_x
    assert rf.read("fileZ.txt") == data_z
