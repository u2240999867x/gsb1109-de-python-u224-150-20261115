"""Tests for RarFile.verify() evidence levels.
"""

import io
import os
import shutil

import pytest

import rarfile
from rarfile.verify import VerifyStatus


def by_name(rep: rarfile.ArchiveVerification) -> dict[str, rarfile.VerifyResult]:
    return {r.filename: r for r in rep}


def test_verify_crc() -> None:
    with rarfile.RarFile("test/files/rar5-crc.rar") as rf:
        rep = rf.verify()
    assert rep.ok
    assert rep.parse_error is None
    res = by_name(rep)
    assert res["stest1.txt"].status is VerifyStatus.VERIFIED
    assert res["stest1.txt"].hash_kind == "crc32"
    assert res["stest2.txt"].status is VerifyStatus.VERIFIED
    for r in res.values():
        assert r.header_ok
        assert r.data_complete
        assert r.hash_verified


def test_verify_blake2sp() -> None:
    with rarfile.RarFile("test/files/rar5-blake.rar") as rf:
        rep = rf.verify()
    assert rep.ok
    for r in rep:
        assert r.status is VerifyStatus.VERIFIED
        assert r.hash_kind == "blake2sp"


def test_verify_rar3() -> None:
    with rarfile.RarFile("test/files/rar3-comment-plain.rar") as rf:
        rep = rf.verify()
    assert rep.ok
    for r in rep:
        assert r.status is VerifyStatus.VERIFIED
        assert r.hash_kind == "crc32"


def test_verify_dirs_have_no_data() -> None:
    with rarfile.RarFile("test/files/rar3-subdirs.rar") as rf:
        rep = rf.verify()
    res = by_name(rep)
    dirs = [r for fn, r in res.items() if fn.endswith("/")]
    assert dirs
    for r in dirs:
        assert r.status is VerifyStatus.VERIFIED
        assert r.data_complete is None
        assert r.hash_verified is None


def test_verify_full_volumes(tmp_path) -> None:
    for n in range(1, 4):
        shutil.copy("test/files/rar5-vols.part%d.rar" % n, tmp_path)
    with rarfile.RarFile(os.path.join(tmp_path, "rar5-vols.part1.rar")) as rf:
        rep = rf.verify()
    assert rep.ok
    for r in rep:
        assert r.status is VerifyStatus.VERIFIED


def test_verify_partial_volumes(tmp_path) -> None:
    # only first volume present: directory hash must not count as verified
    shutil.copy("test/files/rar5-vols.part1.rar", tmp_path)
    with rarfile.RarFile(os.path.join(tmp_path, "rar5-vols.part1.rar")) as rf:
        rep = rf.verify()
    assert not rep.ok
    assert rep.parse_error is not None
    res = by_name(rep)
    big = res["vols/bigfile.txt"]
    assert big.status is VerifyStatus.UNVERIFIED
    assert big.header_ok
    assert big.data_complete is False
    assert big.hash_verified is not True
    assert "volume" in (big.reason or "")


def test_verify_partial_volumes_rar3(tmp_path) -> None:
    shutil.copy("test/files/rar3-vols.part1.rar", tmp_path)
    with rarfile.RarFile(os.path.join(tmp_path, "rar3-vols.part1.rar")) as rf:
        rep = rf.verify()
    assert not rep.ok
    res = by_name(rep)
    big = res["vols/bigfile.txt"]
    assert big.status is VerifyStatus.UNVERIFIED
    assert big.data_complete is False
    assert big.hash_verified is not True


def test_verify_truncated_data() -> None:
    with open("test/files/rar5-crc.rar", "rb") as f:
        data = f.read()
    with rarfile.RarFile(io.BytesIO(data[:-100])) as rf:
        rep = rf.verify()
    assert not rep.ok
    res = by_name(rep)
    assert res["stest1.txt"].status is VerifyStatus.VERIFIED
    bad = res["stest2.txt"]
    assert bad.status is VerifyStatus.UNVERIFIED
    assert bad.data_complete is False
    assert bad.hash_verified is not True


def test_verify_corrupt_data() -> None:
    with open("test/files/rar5-crc.rar", "rb") as f:
        data = bytearray(f.read())
    with rarfile.RarFile(io.BytesIO(bytes(data))) as rf:
        ofs = rf.getinfo("stest2.txt").data_offset
    data[ofs + 10] ^= 0xFF
    with rarfile.RarFile(io.BytesIO(bytes(data))) as rf:
        rep = rf.verify()
    assert not rep.ok
    res = by_name(rep)
    assert res["stest1.txt"].status is VerifyStatus.VERIFIED
    bad = res["stest2.txt"]
    assert bad.status is VerifyStatus.CORRUPT
    assert bad.data_complete is True
    assert bad.hash_verified is False
    assert bad.hash_kind == "crc32"


def test_verify_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # unsupported/unavailable backend is UNVERIFIED, not CORRUPT
    def no_tool(**kwargs) -> None:
        raise rarfile.RarCannotExec("Cannot find working tool")

    monkeypatch.setattr(rarfile.format, "tool_setup", no_tool)
    with rarfile.RarFile("test/files/rar5-crc.rar") as rf:
        rep = rf.verify()
    assert not rep.ok
    res = by_name(rep)
    # stored file is read directly, no backend needed
    assert res["stest2.txt"].status is VerifyStatus.VERIFIED
    # compressed file cannot be checked without backend
    comp = res["stest1.txt"]
    assert comp.status is VerifyStatus.UNVERIFIED
    assert comp.data_complete is True
    assert comp.hash_verified is not True
    assert "backend" in (comp.reason or "")


def test_verify_password_required() -> None:
    with rarfile.RarFile("test/files/rar5-psw.rar") as rf:
        rep = rf.verify()
    assert not rep.ok
    for r in rep:
        assert r.status is VerifyStatus.UNVERIFIED
        assert r.hash_verified is not True
        assert "password" in (r.reason or "")


def test_verify_no_hash_stored() -> None:
    # rar5-psw entries carry no usable content hash
    with rarfile.RarFile("test/files/rar5-psw.rar") as rf:
        rf.setpassword("password")
        rep = rf.verify()
    assert not rep.ok
    for r in rep:
        assert r.status is VerifyStatus.UNVERIFIED
        assert r.data_complete is True
        assert r.hash_verified is not True
        assert "no content hash" in (r.reason or "")


def test_verify_result_str() -> None:
    with rarfile.RarFile("test/files/rar5-crc.rar") as rf:
        rep = rf.verify()
    text = str(by_name(rep)["stest1.txt"])
    assert "stest1.txt" in text
    assert "verified" in text
    assert "crc32" in text
