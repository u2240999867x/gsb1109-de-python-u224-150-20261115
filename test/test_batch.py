"""Solid batch extraction tests.
"""

import io

import pytest

import rarfile
import rarfile.stream as stream
from rarfile.bits import RAR_FILE_SOLID


SOLID_R5 = "test/files/rar5-solid.rar"
SOLID_R3 = "test/files/rar3-solid.rar"
SOLID_QO = "test/files/rar5-solid-qo.rar"
DUPS = "test/files/rar5-dups.rar"
SUBDIRS = "test/files/rar5-subdirs.rar"
SEEKTEST = "test/files/seektest.rar"


class ToolCounter:
    """Count real extraction processes (skip tool self-checks)."""

    # action argument used by each backend's open command
    _EXTRACT_ACTIONS = {"p", "e", "-x", "-q"}

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.calls: list[list[str]] = []
        monkeypatch.setattr(stream, "custom_popen", self._popen)

    def _popen(self, cmd):
        argv = list(cmd)
        if len(argv) > 1 and argv[1] in self._EXTRACT_ACTIONS:
            self.calls.append(argv)
        from subprocess import Popen, DEVNULL, PIPE, STDOUT
        return Popen(argv, bufsize=0, stdout=PIPE, stderr=STDOUT, stdin=DEVNULL)

    @property
    def count(self) -> int:
        return len(self.calls)


def test_read_many_returns_request_order() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    names = ["stest2.txt", "somedir/stest1.txt", "stest1.txt"]
    got = rf.read_many(names)
    assert got == [rf.read(name) for name in names]


def test_read_many_single_tool_run(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = ToolCounter(monkeypatch)
    rf = rarfile.RarFile(SOLID_QO)
    names = ["stest2.txt", "somedir/stest1.txt", "stest1.txt", "somedir/stest2.txt"]
    got = rf.read_many(names)
    assert counter.count == 1
    assert got == [rf.read(name) for name in names]


def test_read_many_rar3_solid(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = ToolCounter(monkeypatch)
    rf = rarfile.RarFile(SOLID_R3)
    got = rf.read_many(["stest2.txt", "stest1.txt"])
    assert counter.count == 1
    assert got == [rf.read("stest2.txt"), rf.read("stest1.txt")]


def test_read_many_decodes_prefix_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # requesting only the last entry still needs the shared prefix once
    counter = ToolCounter(monkeypatch)
    rf = rarfile.RarFile(SOLID_R5)
    got = rf.read_many(["stest2.txt"])
    assert counter.count == 1
    assert got == [rf.read("stest2.txt")]


def test_read_many_repeated_request_independent_slots() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    got = rf.read_many(["stest1.txt", "stest1.txt", "stest2.txt"])
    assert len(got) == 3
    assert got[0] == got[1] == rf.read("stest1.txt")
    assert got[2] == rf.read("stest2.txt")
    # mutating one result must not corrupt the other slot
    mutated = bytearray(got[0])
    mutated[0] ^= 0xFF
    assert got[1][0] == rf.read("stest1.txt")[0]


def test_read_many_cancel_keeps_other_results() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    full = rf.read_many(["somedir/stest1.txt", "stest2.txt", "stest1.txt"])
    # cancel the middle request: remaining slots stay intact and in order
    cancelled = rf.read_many(["somedir/stest1.txt", "stest1.txt"])
    assert cancelled == [full[0], full[2]]


def test_read_many_distinguishes_duplicate_basename_by_identity() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    infos = {inf.filename: inf for inf in rf.infolist()}
    top = infos["stest1.txt"]
    sub = infos["somedir/stest1.txt"]
    got = rf.read_many([sub, top, sub])
    assert got == [rf.read(sub), rf.read(top), rf.read(sub)]


def test_read_many_foreign_identity_rejected() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    other = rarfile.RarFile(DUPS).infolist()[0]
    with pytest.raises(rarfile.NoRarEntry):
        rf.read_many([other])


def test_read_many_missing_name_rejected() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    with pytest.raises(rarfile.NoRarEntry):
        rf.read_many(["no-such-file.txt"])


def test_read_many_directory_rejected() -> None:
    rf = rarfile.RarFile(SUBDIRS)
    dirname = next(inf.filename for inf in rf.infolist() if inf.is_dir())
    with pytest.raises(io.UnsupportedOperation):
        rf.read_many([dirname])


def test_read_many_empty() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    assert rf.read_many([]) == []


def test_read_many_non_solid_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = ToolCounter(monkeypatch)
    rf = rarfile.RarFile(SEEKTEST)
    names = ["stest1.txt", "stest2.txt"]
    got = rf.read_many(names)
    assert got == [rf.read(name) for name in names]
    # no shared solid stream: one process per entry
    assert counter.count == 2


def test_read_many_redirect_archive_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    rf = rarfile.RarFile(DUPS)
    names = ["stest1.txt", "stest2.txt", "stest9.txt"]
    expected = [rf.read(name) for name in names]
    counter = ToolCounter(monkeypatch)
    got = rf.read_many(names)
    assert got == expected
    # layout cannot be split into a label-free stream: one run per entry
    assert counter.count == 3


def test_read_many_memory_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    data = open(SOLID_R5, "rb").read()
    counter = ToolCounter(monkeypatch)
    rf = rarfile.RarFile(io.BytesIO(data))
    got = rf.read_many(["stest2.txt", "stest1.txt"])
    assert counter.count == 1
    disk = rarfile.RarFile(SOLID_R5)
    assert got == [disk.read("stest2.txt"), disk.read("stest1.txt")]


def test_plan_solid_batches_groups_segments() -> None:
    rf = rarfile.RarFile(SOLID_QO)
    parser = rf._file_parser
    infos = list(rf.infolist())

    # one solid block: the first entry starts the segment, every
    # solid-marked entry continues the same prefix
    assert [parser.solid_segment_of(inf) for inf in infos] == [0, 0, 0, 0]
    assert [bool(inf.flags & RAR_FILE_SOLID) for inf in infos] == [
        False, True, True, True,
    ]
    assert parser.plan_solid_batches([infos[3], infos[0], infos[2]]) == [[0, 1, 2]]


def test_plan_solid_batches_multiple_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    rf = rarfile.RarFile(SOLID_QO)
    parser = rf._file_parser

    def entry(solid: bool) -> SimpleNamespace:
        flags = RAR_FILE_SOLID if solid else 0
        return SimpleNamespace(flags=flags)

    # segment boundaries occur at non-solid entries
    seq = [entry(False), entry(True), entry(False), entry(True), entry(True)]
    monkeypatch.setattr(parser, "_info_list", seq)
    assert [parser.solid_segment_of(inf) for inf in seq] == [0, 0, 1, 1, 1]

    # requests arrive out of order; batches follow archive order
    plan = parser.plan_solid_batches([seq[4], seq[0], seq[3]])
    assert plan == [[1], [0, 2]]


def test_solid_batch_ok_rejects_links_and_redirects() -> None:
    solid = rarfile.RarFile(SOLID_QO)._file_parser
    assert solid.solid_batch_ok(list(solid.infolist())) is True

    dups = rarfile.RarFile(DUPS)._file_parser
    assert dups.solid_batch_ok(list(dups.infolist())) is False


def test_solid_batch_ok_allows_zero_length_directories(
        monkeypatch: pytest.MonkeyPatch) -> None:
    rf = rarfile.RarFile(SOLID_R5)
    parser = rf._file_parser
    original = list(parser.infolist())

    def fake_is_dir(inf: rarfile.RarInfo):
        def is_dir() -> bool:
            return inf is original[0]
        return is_dir

    for inf in original:
        monkeypatch.setattr(inf, "is_dir", fake_is_dir(inf), raising=False)

    # directories carry no data and align trivially, so they must not
    # force the whole solid archive onto per-entry extraction
    assert parser.solid_batch_ok(original) is True
    assert all(parser._batch_streamable(inf) for inf in original)


def test_batch_stream_discard_zero_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # a zero-length directory slot must consume nothing from the pipe
    reads: list[int] = []

    class FakePipe:
        def read(self, n: int = -1) -> bytes:
            reads.append(n)
            return b""

    fake = stream.SolidBatchStream.__new__(stream.SolidBatchStream)
    fake._fd = FakePipe()  # type: ignore[attr-defined]
    fake.discard(0)
    assert reads == []
