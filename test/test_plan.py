"""Extraction plan tests.
"""

import os
from typing import Any, cast

import pytest

import rarfile
from rarfile import Rar3Info


def mkinfo(name: str, is_dir: bool = False, link: bool = False) -> Rar3Info:
    if is_dir:
        flags, mode = rarfile.RAR_FILE_DIRECTORY, 0o40755
    elif link:
        flags, mode = 0, 0o120777
    else:
        flags, mode = 0, 0o100644
    return Rar3Info(
        filename=name, orig_filename=name.encode(), file_size=1,
        flags=flags, header_crc=0, header_size=0, header_offset=0,
        data_offset=0, add_size=0, extract_version=20, mode=mode,
        host_os=rarfile.RAR_OS_UNIX, compress_type=0x30,
        _name_size=len(name), salt=None)


class FakeRar:
    """Minimal RarFile stand-in for plan_members."""

    def __init__(self, infos: list[Rar3Info], targets: dict[str, bytes] | None = None):
        self._infos = infos
        self._targets = targets or {}

    def infolist(self) -> list[Rar3Info]:
        return self._infos

    def getinfo(self, member: object) -> Rar3Info:
        if isinstance(member, Rar3Info):
            return member
        for inf in self._infos:
            if inf.filename == member:
                return inf
        raise rarfile.NoRarEntry(str(member))

    def read(self, info: Rar3Info, pwd: str | None = None) -> bytes:
        return self._targets[info.filename]


def plan_of(rf: FakeRar, **kwargs: Any) -> rarfile.ExtractPlan:
    return rarfile.plan_members(cast(rarfile.RarFile, rf), **kwargs)


def test_plan_simple() -> None:
    rf = FakeRar([mkinfo("sub", is_dir=True), mkinfo("sub/a.txt"), mkinfo("b.txt")])
    plan = plan_of(rf)
    assert [e.action for e in plan] == ["dir", "file", "file"]
    assert [e.dest for e in plan] == ["sub", os.path.join("sub", "a.txt"), "b.txt"]
    assert plan.conflicts == []
    assert len(plan) == 3


def test_plan_duplicate_rename_keeps_all_copies() -> None:
    rf = FakeRar([mkinfo("a.txt"), mkinfo("a.txt"), mkinfo("a.txt")])
    plan = plan_of(rf)
    assert [e.dest for e in plan] == ["a.txt", "a (1).txt", "a (2).txt"]
    assert all(e.action == "file" for e in plan)
    assert [c.kind for c in plan.conflicts] == ["duplicate", "duplicate"]
    assert plan.conflicts[0].member == "a.txt"
    assert plan.conflicts[0].other == "a.txt"


def test_plan_duplicate_skip_keeps_first() -> None:
    rf = FakeRar([mkinfo("a.txt"), mkinfo("a.txt")])
    plan = plan_of(rf, overwrite="skip")
    assert [e.action for e in plan] == ["file", "skip"]
    assert [e.dest for e in plan] == ["a.txt", "a.txt"]
    assert len(plan.conflicts) == 1


def test_plan_duplicate_overwrite_is_explicit() -> None:
    rf = FakeRar([mkinfo("a.txt"), mkinfo("a.txt")])
    plan = plan_of(rf, overwrite="overwrite")
    assert [e.action for e in plan] == ["file", "file"]
    assert [e.dest for e in plan] == ["a.txt", "a.txt"]
    assert len(plan.conflicts) == 1


def test_plan_duplicate_error() -> None:
    rf = FakeRar([mkinfo("a.txt"), mkinfo("a.txt")])
    with pytest.raises(rarfile.BadRarFile):
        plan_of(rf, overwrite="error")


def test_plan_bad_policy() -> None:
    rf = FakeRar([mkinfo("a.txt")])
    with pytest.raises(ValueError):
        plan_of(rf, overwrite="bogus")


def test_plan_casefold_conflict() -> None:
    rf = FakeRar([mkinfo("Foo.txt"), mkinfo("foo.txt"), mkinfo("BAR/b.txt"), mkinfo("bar/c.txt")])
    plan = plan_of(rf)
    assert [e.dest for e in plan] == [
        "Foo.txt", "foo (1).txt",
        os.path.join("BAR", "b.txt"), os.path.join("bar", "c.txt"),
    ]
    # file/file casefold collides, dir/dir casefold merges silently
    assert [c.kind for c in plan.conflicts] == ["casefold"]


def test_plan_casefold_allowed_on_case_sensitive_fs() -> None:
    rf = FakeRar([mkinfo("Foo.txt"), mkinfo("foo.txt")])
    plan = plan_of(rf, case_sensitive=True)
    assert [e.dest for e in plan] == ["Foo.txt", "foo.txt"]
    assert plan.conflicts == []


def test_plan_dir_file_conflict() -> None:
    rf = FakeRar([mkinfo("a"), mkinfo("a", is_dir=True)])
    plan = plan_of(rf)
    assert [e.action for e in plan] == ["file", "dir"]
    assert [e.dest for e in plan] == ["a", "a (1)"]

    with pytest.raises(rarfile.BadRarFile):
        plan_of(rf, overwrite="overwrite")


def test_plan_path_through_file_rejected() -> None:
    rf = FakeRar([mkinfo("a"), mkinfo("a/b.txt")])
    with pytest.raises(rarfile.BadRarFile):
        plan_of(rf)


def test_plan_bad_name_rejected() -> None:
    rf = FakeRar([mkinfo("..")])
    with pytest.raises(rarfile.BadRarFile):
        plan_of(rf)


def test_plan_symlink_target_recorded() -> None:
    infos = [mkinfo("data.txt"), mkinfo("data_link", link=True)]
    rf = FakeRar(infos, {"data_link": b"data.txt"})
    plan = plan_of(rf)
    link = plan.entries[1]
    assert link.action == "symlink"
    assert link.target == "data.txt"
    assert plan.conflicts == []


def test_plan_symlink_escape_rejected() -> None:
    infos = [mkinfo("up", link=True), mkinfo("up/pwned.txt")]
    rf = FakeRar(infos, {"up": b".."})
    with pytest.raises(rarfile.BadSymLinkError):
        plan_of(rf)


def test_plan_absolute_symlink_rejected() -> None:
    rf = FakeRar([mkinfo("abs", link=True)], {"abs": b"/etc/passwd"})
    with pytest.raises(rarfile.BadSymLinkError):
        plan_of(rf)


def test_plan_no_escape_through_previous_link() -> None:
    # "a/x" alone is a legal path, but "a" is a planned link and
    # the target of "a/x" resolves outside the root through it.
    infos = [
        mkinfo("sub", is_dir=True),
        mkinfo("a", link=True),
        mkinfo("a/x", link=True),
    ]
    rf = FakeRar(infos, {"a": b"sub", "a/x": b"../.."})
    with pytest.raises(rarfile.BadSymLinkError):
        plan_of(rf)


def test_plan_file_through_link_stays_in_root() -> None:
    infos = [
        mkinfo("sub", is_dir=True),
        mkinfo("a", link=True),
        mkinfo("a/f.txt"),
    ]
    rf = FakeRar(infos, {"a": b"sub"})
    plan = plan_of(rf)
    assert [e.action for e in plan] == ["dir", "symlink", "file"]
    assert plan.entries[2].dest == os.path.join("a", "f.txt")
    assert plan.conflicts == []


def test_plan_members_subset() -> None:
    rf = FakeRar([mkinfo("a.txt"), mkinfo("b.txt")])
    plan = plan_of(rf, members=["b.txt"])
    assert [e.dest for e in plan] == ["b.txt"]


@pytest.mark.parametrize("fn", [
    "test/files/rar3-subdirs.rar",
    "test/files/rar5-subdirs.rar",
])
def test_plan_real_archive(fn: str) -> None:
    with rarfile.RarFile(fn) as rf:
        plan = rf.plan_extractall()
        assert plan.conflicts == []
        dests = [e.dest for e in plan]
        assert os.path.join("sub", "dir1", "file1.txt") in dests
        assert len(plan) == len(rf.infolist())


@pytest.mark.parametrize("fn", [
    "test/files/rar3-symlink-unix.rar",
    "test/files/rar5-symlink-unix.rar",
])
def test_plan_real_symlinks(fn: str) -> None:
    with rarfile.RarFile(fn) as rf:
        plan = rf.plan_extractall(members=["data.txt", "data_link"])
        assert [e.action for e in plan] == ["file", "symlink"]
        assert plan.entries[1].target == "data.txt"

        with pytest.raises(rarfile.BadSymLinkError):
            rf.plan_extractall(members=["random_link"])


def test_plan_real_evil_symlink() -> None:
    with rarfile.RarFile("test/files/rar5-evil-symlink-traversal.rar") as rf:
        with pytest.raises(rarfile.BadSymLinkError):
            rf.plan_extractall()


def test_plan_junction_skipped() -> None:
    with rarfile.RarFile("test/files/rar5-symlink-win.rar") as rf:
        with pytest.warns(rarfile.UnsupportedWarning):
            plan = rf.plan_extractall(members=["links/dir_junction"])
        assert [e.action for e in plan] == ["skip"]
