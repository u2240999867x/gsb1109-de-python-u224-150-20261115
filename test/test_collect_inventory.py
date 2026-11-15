"""Tests for collect_inventory layered registry."""

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from collect_inventory import (  # noqa: E402
    CHECK_NEEDS_PASSWORD,
    CHECK_OK,
    ENC_CONTENT,
    ENC_HEADERS,
    ENC_NONE,
    STATUS_CORRUPT,
    STATUS_NEEDS_PASSWORD,
    STATUS_VERIFIED,
    Inventory,
)
from rarfile.crypto import AES_CBC_Decrypt  # noqa: E402

have_crypto = AES_CBC_Decrypt.have_crypto()

FILES = REPO_ROOT / "test" / "files"
PASSWORD = "password"


@pytest.fixture()
def inv(tmp_path):
    return Inventory(tmp_path / "registry.json")


def test_scan_plain_archive_is_verified(inv):
    rec = inv.scan_archive(FILES / "ctime0.rar")
    assert rec.status == STATUS_VERIFIED
    assert rec.encryption == ENC_NONE
    assert rec.file_count == 1
    assert rec.entries[0]["filename"] == "afile.txt"
    assert rec.content_check == CHECK_OK


def test_scan_content_encrypted_keeps_verified_directory(inv):
    rec = inv.scan_archive(FILES / "rar5-psw.rar")
    assert rec.status == STATUS_NEEDS_PASSWORD
    assert rec.encryption == ENC_CONTENT
    # directory fields are verifiable even though contents need a password
    assert rec.file_count == 2
    assert {e["filename"] for e in rec.entries} == {"stest1.txt", "stest2.txt"}
    assert all(e["needs_password"] for e in rec.entries)
    assert rec.content_check == CHECK_NEEDS_PASSWORD


def test_scan_header_encrypted_is_not_registered_as_zero_files(inv):
    rec = inv.scan_archive(FILES / "rar5-hpsw.rar")
    assert rec.status == STATUS_NEEDS_PASSWORD
    assert rec.encryption == ENC_HEADERS
    # directory cannot be opened: unknown count, never zero files
    assert rec.file_count is None
    assert rec.entries is None


def test_scan_corrupt_archive(inv, tmp_path):
    broken = tmp_path / "broken.rar"
    data = (FILES / "ctime0.rar").read_bytes()
    broken.write_bytes(data[: len(data) // 2])
    rec = inv.scan_archive(broken)
    assert rec.status == STATUS_CORRUPT
    assert rec.error
    assert rec.file_count is None
    assert rec.entries is None


def test_scan_not_rar_is_corrupt(inv, tmp_path):
    junk = tmp_path / "junk.rar"
    junk.write_bytes(b"not a rar archive at all")
    rec = inv.scan_archive(junk)
    assert rec.status == STATUS_CORRUPT


def test_failed_password_attempt_keeps_pending_directory(inv):
    rec = inv.scan_archive(FILES / "rar5-hpsw.rar")
    ok = inv.try_password(FILES / "rar5-hpsw.rar", "wrong-password")
    assert not ok
    rec = inv.records[str(FILES / "rar5-hpsw.rar")]
    # failed attempt must not turn "unknown" into "zero files"
    assert rec.status == STATUS_NEEDS_PASSWORD
    assert rec.file_count is None
    assert rec.entries is None
    assert len(rec.password_attempts) == 1
    assert rec.password_attempts[0]["ok"] is False


def test_failed_password_attempt_keeps_verified_directory(inv):
    path = FILES / "rar5-psw.rar"
    before = inv.scan_archive(path)
    verified_entries = [dict(e) for e in before.entries]
    ok = inv.try_password(path, "wrong-password")
    assert not ok
    rec = inv.records[str(path)]
    # verified metadata must survive a wrong password attempt
    assert rec.entries == verified_entries
    assert rec.file_count == 2
    assert rec.status == STATUS_NEEDS_PASSWORD
    assert rec.password_attempts[-1]["ok"] is False


@pytest.mark.skipif(not have_crypto, reason="no crypto backend")
def test_correct_password_promotes_header_encrypted_to_verified(inv):
    path = FILES / "rar5-hpsw.rar"
    inv.scan_archive(path)
    ok = inv.try_password(path, PASSWORD)
    assert ok
    rec = inv.records[str(path)]
    assert rec.status == STATUS_VERIFIED
    assert rec.file_count == 2
    assert {e["filename"] for e in rec.entries} == {"stest1.txt", "stest2.txt"}
    assert rec.content_check == CHECK_OK
    assert rec.password_attempts[-1]["ok"] is True


@pytest.mark.skipif(not have_crypto, reason="no crypto backend")
def test_correct_password_verifies_content_encrypted(inv):
    path = FILES / "rar5-psw.rar"
    inv.scan_archive(path)
    assert inv.try_password(path, PASSWORD)
    rec = inv.records[str(path)]
    assert rec.status == STATUS_VERIFIED
    assert rec.file_count == 2


def test_registry_roundtrip(inv, tmp_path):
    inv.scan_archive(FILES / "rar5-hpsw.rar")
    inv.scan_archive(FILES / "ctime0.rar")
    inv.save()
    inv2 = Inventory(tmp_path / "registry.json")
    rec = inv2.records[str(FILES / "rar5-hpsw.rar")]
    assert rec.file_count is None
    assert rec.entries is None
    assert inv2.records[str(FILES / "ctime0.rar")].file_count == 1


def test_scan_directory_of_packages(inv, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(FILES / "ctime0.rar", inbox)
    shutil.copy(FILES / "rar5-psw.rar", inbox)
    shutil.copy(FILES / "rar5-hpsw.rar", inbox)
    inv.scan_paths([str(inbox)])
    statuses = {Path(p).name: r.status for p, r in inv.records.items()}
    assert statuses == {
        "ctime0.rar": STATUS_VERIFIED,
        "rar5-psw.rar": STATUS_NEEDS_PASSWORD,
        "rar5-hpsw.rar": STATUS_NEEDS_PASSWORD,
    }
