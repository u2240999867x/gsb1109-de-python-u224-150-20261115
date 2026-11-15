"""Format details.
"""

# pylint: disable=use-implicit-booleaness-not-comparison

import io
import zlib
from datetime import datetime

import pytest

import rarfile
from rarfile.bits import (
    RAR_BLOCK_ENDARC, RAR_BLOCK_FILE, RAR_BLOCK_MAIN, RAR_BLOCK_OLD_COMMENT,
    RAR_BLOCK_SUB, RAR_FILE_SOLID, RAR_ID, RAR_LONG_BLOCK, RAR_M0, RAR_M3,
    RAR_MAIN_COMMENT, RAR_OS_UNIX,
)
from rarfile.crypto import AES_CBC_Decrypt
from rarfile.format import S_BLK_HDR, S_COMMENT_HDR, S_FILE_HDR
from rarfile.utils import DateTuple

have_crypto = AES_CBC_Decrypt.have_crypto()


def render_date(dt: datetime | DateTuple | None) -> str | None:
    if isinstance(dt, datetime):
        return dt.isoformat("T")
    elif isinstance(dt, tuple):
        return "%04d-%02d-%02d %02d:%02d:%02d" % dt
    else:
        return dt


def mkitem(**kwargs: object) -> dict[str, object]:
    res = {}
    for k, v in kwargs.items():
        if v is not None:
            res[k] = v
    return res


def dumparc(rf: rarfile.RarFile) -> list[dict[str, object]]:
    res = []
    for item in rf.infolist():
        info = mkitem(fn=item.filename,
                      file_size=item.file_size,
                      compress_size=item.compress_size,
                      CRC=item.CRC,
                      date_time=render_date(item.date_time),
                      arctime=render_date(item.arctime),
                      mtime=render_date(item.mtime),
                      atime=render_date(item.atime),
                      ctime=render_date(item.ctime),
                      comment=item.comment,
                      extract_version=item.extract_version,
                      compress_type=item.compress_type,
                      mode=item.mode,
                      host_os=item.host_os)
        res.append(info)
    return res


def diffs(a: list[dict[str, object]], b: list[dict[str, object]]) -> str:
    if len(a) != len(b):
        return "Different lengths"
    problems = []
    for i, xa in enumerate(a):
        xb = b[i]
        for k in xa:
            if k not in xb:
                problems.append("NewKey(%d,%s)=%r" % (i, k, xa[k]))
        for k in xb:
            if k not in xa:
                problems.append("MissingKey(%d,%s)=%r" % (i, k, xb[k]))
        for k in xa:
            if k in xb and xa[k] != xb[k]:
                problems.append("ErrValue(%d,%s):got=%r/exp=%r" % (i, k, xa[k], xb[k]))
    return "; ".join(problems)


def cmp_struct(a: list[dict[str, object]], b: list[dict[str, object]]) -> None:
    assert a == b, diffs(a, b)

#
# test start
#


@pytest.mark.skipif(not have_crypto, reason="No crypto")
def test_rar3_header_encryption() -> None:
    r = rarfile.RarFile("test/files/rar3-comment-hpsw.rar", "r")
    assert r.needs_password() is True
    assert r.comment is None
    assert r.namelist() == []

    r.setpassword("password")
    assert r.needs_password() is True
    assert r.namelist() == ["file1.txt", "file2.txt"]
    assert r.comment is not None
    assert r.comment == "RARcomment\n"


@pytest.mark.skipif(not have_crypto, reason="No crypto")
def test_rar5_header_encryption() -> None:
    r = rarfile.RarFile("test/files/rar5-hpsw.rar")
    assert r.needs_password() is True
    assert r.comment is None
    assert r.namelist() == []

    with pytest.raises(rarfile.RarWrongPassword):
        r.setpassword("password222")
    assert r.needs_password() is True

    r.setpassword("password")
    assert r.needs_password() is True
    assert r.namelist() == ["stest1.txt", "stest2.txt"]
    assert r.comment is not None
    assert r.comment == "RAR5 archive - hdr-password\n"
    r.close()


def get_vol_info(extver: int = 20, tz: str = "", hr: str = "11") -> list[dict[str, object]]:
    return [
        mkitem(CRC=1352324940,
               date_time="2016-05-24 %s:42:37%s" % (hr, ""),
               mtime="2016-05-24T%s:42:37%s" % (hr, tz),
               compress_type=48,
               compress_size=205000,
               extract_version=extver,
               file_size=205000,
               mode=33204,
               host_os=3,
               fn="vols/bigfile.txt"),
        mkitem(CRC=3498712966,
               date_time="2016-05-24 %s:42:43%s" % (hr, ""),
               mtime="2016-05-24T%s:42:43%s" % (hr, tz),
               extract_version=extver,
               compress_type=48,
               compress_size=2050,
               file_size=2050,
               mode=33204,
               host_os=3,
               fn="vols/smallfile.txt")]


def test_rar3_vols() -> None:
    r = rarfile.RarFile("test/files/rar3-vols.part1.rar")
    assert r.needs_password() is False
    assert r.comment is None
    assert r.strerror() is None
    cmp_struct(dumparc(r), get_vol_info())
    assert r.volumelist() == [
        "test/files/rar3-vols.part1.rar",
        "test/files/rar3-vols.part2.rar",
        "test/files/rar3-vols.part3.rar"]
    with pytest.raises(rarfile.NeedFirstVolume):
        rarfile.RarFile("test/files/rar3-vols.part2.rar")


def test_rar3_oldvols() -> None:
    r = rarfile.RarFile("test/files/rar3-old.rar")
    assert r.needs_password() is False
    assert r.comment is None
    assert r.strerror() is None
    cmp_struct(dumparc(r), get_vol_info())
    assert r.volumelist() == [
        "test/files/rar3-old.rar",
        "test/files/rar3-old.r00",
        "test/files/rar3-old.r01"]
    with pytest.raises(rarfile.NeedFirstVolume):
        rarfile.RarFile("test/files/rar3-old.r00")


def test_rar5_vols() -> None:
    r = rarfile.RarFile("test/files/rar5-vols.part1.rar")
    assert r.needs_password() is False
    assert r.comment is None
    assert r.strerror() is None
    cmp_struct(dumparc(r), get_vol_info(50, "+00:00", "08"))
    assert r.volumelist() == [
        "test/files/rar5-vols.part1.rar",
        "test/files/rar5-vols.part2.rar",
        "test/files/rar5-vols.part3.rar"]
    with pytest.raises(rarfile.NeedFirstVolume):
        rarfile.RarFile("test/files/rar5-vols.part2.rar")


def expect_ctime(mtime: str, ctime: str | None) -> list[dict[str, object]]:
    return [mkitem(
        mtime=mtime,
        date_time=mtime.split(".")[0].replace("T", " "),
        ctime=ctime,
        compress_size=0,
        file_size=0,
        CRC=0,
        fn="afile.txt",
        extract_version=29,
        compress_type=48,
        mode=32,
        host_os=2)]


def test_rar3_ctime0() -> None:
    r = rarfile.RarFile("test/files/ctime0.rar")
    cmp_struct(dumparc(r), expect_ctime("2011-05-10T21:28:47.899345100", None))


def test_rar3_ctime1() -> None:
    r = rarfile.RarFile("test/files/ctime1.rar")
    cmp_struct(dumparc(r), expect_ctime("2011-05-10T21:28:47.899345100", "2011-05-10T21:28:47"))


def test_rar3_ctime2() -> None:
    r = rarfile.RarFile("test/files/ctime2.rar")
    cmp_struct(dumparc(r), expect_ctime("2011-05-10T21:28:47.899345100", "2011-05-10T21:28:47.897843200"))


def test_rar3_ctime3() -> None:
    r = rarfile.RarFile("test/files/ctime3.rar")
    cmp_struct(dumparc(r), expect_ctime("2011-05-10T21:28:47.899345100", "2011-05-10T21:28:47.899328"))


def test_rar3_ctime4() -> None:
    r = rarfile.RarFile("test/files/ctime4.rar")
    cmp_struct(dumparc(r), expect_ctime("2011-05-10T21:28:47.899345100", "2011-05-10T21:28:47.899345100"))


def test_rar5_ctime5() -> None:
    r = rarfile.RarFile("test/files/ctime5.rar")
    inf = r.getinfo("timed.txt")
    assert inf.mtime is not None
    assert inf.ctime is not None
    assert inf.atime is not None
    assert inf.mtime.isoformat() == "2020-07-30T20:26:59.677675904+00:00"
    assert inf.ctime.isoformat() == "2020-07-30T20:28:19.398867888+00:00"
    assert inf.atime.isoformat() == "2020-07-30T20:27:10.121196721+00:00"


def test_rar5_times() -> None:
    r = rarfile.RarFile("test/files/rar5-times.rar")
    cmp_struct(dumparc(r), [mkitem(
        fn="stest1.txt",
        file_size=2048,
        compress_size=55,
        compress_type=RAR_M3,
        extract_version=50,
        host_os=RAR_OS_UNIX,
        mode=33188,
        date_time="2011-06-12 09:53:33",
        mtime="2011-06-12T09:53:33+00:00",
        atime="2016-05-22T09:12:36+00:00",
        CRC=3317163682
    )])


def test_oldvols() -> None:
    from rarfile.format import _next_oldvol
    assert _next_oldvol("archive") == "archive.r00"
    assert _next_oldvol("archive.rar/foo") == "archive.rar/foo.r00"
    assert _next_oldvol("archive.arr") == "archive.a00"
    assert _next_oldvol("archive.brar") == "archive.b00"
    assert _next_oldvol("qq00.part0.rar") == "qq00.part0.r00"
    assert _next_oldvol("qq00.part0.r00") == "qq00.part0.r01"
    assert _next_oldvol("qq00.part0.r29") == "qq00.part0.r30"
    assert _next_oldvol("qq00.part0.r99") == "qq00.part0.s00"


def test_newvols() -> None:
    from rarfile.format import _next_newvol
    assert _next_newvol("qq00.part0.rar") == "qq00.part1.rar"
    assert _next_newvol("qq00.part09.rar") == "qq00.part10.rar"
    assert _next_newvol("qq00.part99.rar") == "qq00.part100.rar"
    assert _next_newvol("part20") == "part21.rar"
    assert _next_newvol("qq00.part3.exe") == "qq00.part4.rar"
    assert _next_newvol("qq00.part5.sfx") == "qq00.part6.rar"
    assert _next_newvol("qq00.part6.bin") == "qq00.part7.bin"
    assert _next_newvol("99") == "100.rar"
    assert _next_newvol("dir/99.rar") == "dir/100.rar"
    with pytest.raises(rarfile.BadRarName):
        _next_newvol("qq00.part7.rar/foo")
    with pytest.raises(rarfile.BadRarName):
        _next_newvol("foo")


def test_newvols_err() -> None:
    from rarfile.format import _next_newvol
    with pytest.raises(rarfile.BadRarName):
        _next_newvol("xx.rar")


@pytest.mark.parametrize("fn", ["test/files/rar3-versions.rar", "test/files/rar5-versions.rar"])
def test_versions(fn: str) -> None:
    with rarfile.RarFile(fn) as rf:
        assert rf.namelist() == ["versioned.txt"]


def _rar3_block(btype: int, flags: int, body: bytes) -> bytes:
    size = S_BLK_HDR.size + len(body)
    hdr = S_BLK_HDR.pack(0, btype, flags, size) + body
    crc = zlib.crc32(hdr[2:]) & 0xFFFF
    return S_BLK_HDR.pack(crc, btype, flags, size) + body


def _rar3_old_comment(comment: bytes) -> bytes:
    body = b"\0" * 6
    sub = S_COMMENT_HDR.pack(len(comment), 20, RAR_M0, zlib.crc32(comment) & 0xFFFF) + comment
    subhdr = S_BLK_HDR.pack(0, RAR_BLOCK_OLD_COMMENT, 0, S_BLK_HDR.size + len(sub)) + sub
    size = S_BLK_HDR.size + len(body) + len(subhdr)
    hdr = S_BLK_HDR.pack(0, RAR_BLOCK_MAIN, RAR_MAIN_COMMENT, size)
    crc = zlib.crc32(hdr[2:] + body) & 0xFFFF
    return S_BLK_HDR.pack(crc, RAR_BLOCK_MAIN, RAR_MAIN_COMMENT, size) + body + subhdr


def _rar3_file(name: bytes, data: bytes) -> bytes:
    flags = RAR_LONG_BLOCK
    body = S_FILE_HDR.pack(len(data), len(data), RAR_OS_UNIX, zlib.crc32(data),
                           0, 20, RAR_M0, len(name), 0o100644) + name
    size = S_BLK_HDR.size + len(body)
    hdr = S_BLK_HDR.pack(0, RAR_BLOCK_FILE, flags, size) + body
    crc = zlib.crc32(hdr[2:]) & 0xFFFF
    return S_BLK_HDR.pack(crc, RAR_BLOCK_FILE, flags, size) + body + data


def _rar3_cmt_sub(comment: bytes) -> bytes:
    body = S_FILE_HDR.pack(len(comment), len(comment), RAR_OS_UNIX, zlib.crc32(comment),
                           0, 29, RAR_M0, 3, 0) + b"CMT"
    flags = RAR_LONG_BLOCK | RAR_FILE_SOLID
    size = S_BLK_HDR.size + len(body)
    hdr = S_BLK_HDR.pack(0, RAR_BLOCK_SUB, flags, size) + body
    crc = zlib.crc32(hdr[2:]) & 0xFFFF
    return S_BLK_HDR.pack(crc, RAR_BLOCK_SUB, flags, size) + body + comment


def test_rar3_comment_raw_and_invalid_bytes() -> None:
    raw = b"ok\x81end"
    buf = RAR_ID + _rar3_old_comment(raw) + _rar3_block(RAR_BLOCK_ENDARC, 0, b"")
    with rarfile.RarFile(io.BytesIO(buf), charset="cp1252") as rf:
        assert rf.orig_comment == raw
        assert rf.comment_encoding == "cp1252"
        assert rf.comment_errors == b"\x81"
        assert rf.comment == "ok\\x81end"
        assert "�" not in (rf.comment or "")


def test_rar3_comment_duplicate_names_stay_separate() -> None:
    buf = RAR_ID + _rar3_block(RAR_BLOCK_MAIN, 0, b"\0" * 6)
    buf += _rar3_file(b"same.txt", b"A") + _rar3_cmt_sub(b"first")
    buf += _rar3_file(b"same.txt", b"B") + _rar3_cmt_sub(b"second")
    buf += _rar3_block(RAR_BLOCK_ENDARC, 0, b"")
    with rarfile.RarFile(io.BytesIO(buf)) as rf:
        first, second = rf.infolist()
        assert first.filename == second.filename == "same.txt"
        assert first.comment == "first"
        assert second.comment == "second"
        assert first.orig_comment == b"first"
        assert second.orig_comment == b"second"
