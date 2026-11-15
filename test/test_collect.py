"""Bounded collector tests.
"""

import io

import pytest

import rarfile
from rarfile.collector import collect, collect_files


def test_head_limit_marks_truncated_but_continues() -> None:
    report = collect("test/files/seektest.rar", head_limit=64)
    assert [f.filename for f in report.files] == ["stest1.txt", "stest2.txt"]
    assert all(f.returned_size == 64 for f in report.files)
    assert all(f.truncated for f in report.files)
    # Clipped data must never be reported as complete original.
    assert not any(f.complete for f in report.files)
    assert report.complete is False
    assert report.returned_bytes == 128
    assert report.decode_limit_reached is False
    assert report.stopped_before is None


def test_small_file_is_complete() -> None:
    report = collect("test/files/seektest.rar", ["stest1.txt"], head_limit=1024 * 1024)
    assert len(report.files) == 1
    f = report.files[0]
    assert f.returned_size == f.declared_size == 2048
    assert f.complete is True
    assert f.truncated is False
    assert report.complete is True
    assert report.decode_used == 2048


def test_head_limit_zero_touches_no_data() -> None:
    report = collect("test/files/rar5-solid.rar", head_limit=0, decode_limit=0)
    assert [(f.filename, f.returned_size) for f in report.files] == [
        ("stest1.txt", 0), ("stest2.txt", 0)]
    assert report.decode_used == 0
    assert report.stopped_before is None
    assert report.complete is False


def test_solid_prefix_is_charged_separately() -> None:
    report = collect("test/files/rar5-solid.rar", head_limit=64, decode_limit=10_000)
    first, second = report.files
    assert first.prefix_bytes == 0
    assert second.prefix_bytes == first.declared_size
    assert first.decoded_bytes == 64
    # Requested bytes are capped by head_limit, prefix is extra.
    assert second.decoded_bytes == 2048 + 64
    assert second.returned_size == 64
    assert report.prefix_bytes == 2048
    assert report.decode_used == 64 + 2112
    assert report.returned_bytes == 128


def test_budget_stops_before_reported_file() -> None:
    # stest2 needs 2048 prefix + 64 returned bytes.
    report = collect("test/files/rar5-solid.rar", head_limit=64, decode_limit=2111)
    assert [f.filename for f in report.files] == ["stest1.txt"]
    assert report.stopped_before == "stest2.txt"
    assert report.stopped_before_info is not None
    assert report.stopped_before_info.filename == "stest2.txt"
    assert report.stopped_cost == 2048 + 64
    assert report.stopped_reason == "decode_limit"
    assert report.decode_limit_reached is True
    assert report.decode_used == 64
    assert report.returned_bytes == 64


def test_budget_exact_fit_is_allowed() -> None:
    # 64 for the first member, then 2048 prefix + 64 for the second.
    limit = 64 + 2048 + 64
    report = collect("test/files/rar5-solid.rar", head_limit=64, decode_limit=limit)
    assert [f.filename for f in report.files] == ["stest1.txt", "stest2.txt"]
    assert report.stopped_before is None
    assert report.decode_used == limit


def test_selected_later_solid_member_still_pays_prefix() -> None:
    report = collect("test/files/rar5-solid.rar", ["stest2.txt"], head_limit=64,
                     decode_limit=10_000)
    assert [f.filename for f in report.files] == ["stest2.txt"]
    assert report.files[0].prefix_bytes == 2048
    assert report.decode_used == 2112


def test_multi_member_solid_prefix_accumulates() -> None:
    report = collect("test/files/rar5-solid-qo.rar", head_limit=64, decode_limit=100_000)
    assert [(f.filename, f.prefix_bytes) for f in report.files] == [
        ("somedir/stest1.txt", 0),
        ("stest1.txt", 2048),
        ("somedir/stest2.txt", 4096),
        ("stest2.txt", 6144),
    ]
    assert report.decode_used == 64 + 2112 + 4160 + 6208
    assert report.prefix_bytes == 2048 + 4096 + 6144


def test_directory_records_are_never_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    orig_open = rarfile.RarFile.open

    def tracking_open(self: rarfile.RarFile, name: object, mode: str = "r",
                      pwd: str | None = None) -> rarfile.RarExtFile:
        opened.append(getattr(name, "filename", str(name)))
        return orig_open(self, name, mode, pwd)  # type: ignore[arg-type]

    monkeypatch.setattr(rarfile.RarFile, "open", tracking_open)
    report = collect("test/files/rar5-subdirs.rar", head_limit=1024 * 1024,
                     decode_limit=2)
    assert report.stopped_before == "sub/dir2/file2.txt"
    # The budget stopped the run before any file was read.
    assert opened == []
    assert report.files == []


def test_directory_skip_does_not_depend_on_declared_size(monkeypatch: pytest.MonkeyPatch) -> None:
    # A directory record is skipped even if selecting it is the only work
    # left and no decode budget remains for its (possibly bogus) size.
    opened: list[str] = []
    orig_open = rarfile.RarFile.open

    def tracking_open(self: rarfile.RarFile, name: object, mode: str = "r",
                      pwd: str | None = None) -> rarfile.RarExtFile:
        opened.append(getattr(name, "filename", str(name)))
        return orig_open(self, name, mode, pwd)  # type: ignore[arg-type]

    monkeypatch.setattr(rarfile.RarFile, "open", tracking_open)
    report = collect("test/files/rar5-subdirs.rar", ["sub/"],
                     head_limit=1024 * 1024, decode_limit=0)
    assert opened == []
    assert [s.filename for s in report.skipped] == ["sub/"]
    assert report.stopped_before is None
    assert report.decode_used == 0


def test_directory_records_reported_as_skipped() -> None:
    report = collect("test/files/rar5-subdirs.rar", ["sub/dir1/", "sub/dir1/file1.txt"],
                     head_limit=1024 * 1024, decode_limit=1024)
    assert [s.filename for s in report.skipped] == ["sub/dir1/"]
    assert [s.reason for s in report.skipped] == ["directory"]
    assert [f.filename for f in report.files] == ["sub/dir1/file1.txt"]
    assert report.files[0].complete is True


def test_symlinks_are_skipped() -> None:
    report = collect("test/files/rar5-symlink-unix.rar", head_limit=10)
    assert sorted(s.filename for s in report.skipped) == ["data_link", "random_link"]
    assert {s.reason for s in report.skipped} == {"symlink"}
    assert [f.filename for f in report.files] == ["data.txt"]


def test_clipped_result_never_claims_complete() -> None:
    report = collect("test/files/rar5-solid.rar", head_limit=64)
    for f in report.files:
        assert f.truncated is True
        assert f.complete is False
        assert f.returned_size < f.declared_size
    assert report.complete is False


def test_file_like_source() -> None:
    with open("test/files/seektest.rar", "rb") as f:
        data = f.read()
    report = collect(io.BytesIO(data), head_limit=16)
    assert [f.filename for f in report.files] == ["stest1.txt", "stest2.txt"]
    assert all(f.returned_size == 16 for f in report.files)


def test_open_rarfile_source() -> None:
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        report = collect_files(rf, head_limit=8)
        assert report.returned_bytes == 16
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        same = collect(rf, head_limit=8)
        assert [f.data for f in same.files] == [f.data for f in report.files]


def test_missing_member_raises() -> None:
    with pytest.raises(rarfile.NoRarEntry):
        collect("test/files/seektest.rar", ["missing.txt"])


@pytest.mark.parametrize("limit", [-1, -100])
def test_negative_limits_rejected(limit: int) -> None:
    with pytest.raises(ValueError):
        collect("test/files/seektest.rar", head_limit=limit)
    with pytest.raises(ValueError):
        collect("test/files/seektest.rar", decode_limit=limit)


def test_defaults_come_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rarfile.config, "COLLECT_HEAD_LIMIT", 32)
    monkeypatch.setattr(rarfile.config, "COLLECT_DECODE_LIMIT", 100)
    report = collect("test/files/seektest.rar")
    assert report.head_limit == 32
    assert report.decode_limit == 100
    assert all(f.returned_size == 32 for f in report.files)


def test_password_entry_reports_error_without_raising() -> None:
    report = collect("test/files/rar5-psw.rar", head_limit=8)
    assert [f.filename for f in report.files] == ["stest1.txt", "stest2.txt"]
    assert all(f.returned_size == 0 for f in report.files)
    assert all(f.error is not None and "PasswordRequired" in f.error for f in report.files)
    assert all(f.complete is False for f in report.files)
    assert report.complete is False


def test_password_via_path_source() -> None:
    report = collect("test/files/rar5-psw.rar", pwd="password", head_limit=8)
    assert [f.returned_size for f in report.files] == [8, 8]
    assert all(f.error is None for f in report.files)


def test_rarfile_method() -> None:
    with rarfile.RarFile("test/files/rar5-solid.rar") as rf:
        report = rf.collect(head_limit=64, decode_limit=2111)
    assert [f.filename for f in report.files] == ["stest1.txt"]
    assert report.stopped_before == "stest2.txt"
