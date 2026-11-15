"""Capability preflight tests.
"""

import io
import os
import sys
from collections.abc import Iterator

import pytest

import rarfile
from rarfile import backend, config
from rarfile.stream import PipeReader

TOOL_VARS = (
    "UNRAR_TOOL", "UNAR_TOOL", "BSDTAR_TOOL", "TAR_TOOL",
    "SEVENZIP_TOOL", "SEVENZIP2_TOOL",
)


def have_tool(name: str) -> bool:
    for dn in os.get_exec_path():
        if os.path.isfile(os.path.join(dn, name)):
            return True
        if os.path.isfile(os.path.join(dn, name + ".exe")):
            return True
    return False


def have_any_tool() -> bool:
    return backend.find_tool() is not None


@pytest.fixture
def no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a machine without any extraction tools."""
    for var in TOOL_VARS:
        monkeypatch.setattr(config, var, "missing-tool-xyz")
    backend.find_tool(force=True)


@pytest.fixture
def bsdtar_only() -> Iterator[None]:
    """Force bsdtar as the only extraction tool."""
    setup = backend.find_tool(unrar=False, unar=False, sevenzip=False, force=True)
    if setup is None:
        pytest.skip("bsdtar not installed")
    try:
        yield
    finally:
        backend.find_tool(force=True)


def test_public_api() -> None:
    assert rarfile.find_tool is backend.find_tool
    assert rarfile.plan_extraction is backend.plan_extraction
    assert rarfile.ExtractPlan is backend.ExtractPlan


def test_find_tool_none_without_tools(no_tools: None) -> None:
    assert backend.find_tool(force=True) is None
    with pytest.raises(rarfile.RarCannotExec):
        backend.tool_setup(force=True)


def test_metadata_without_tools(no_tools: None) -> None:
    # rar3 archive with compressed comment: comment needs a tool, listing must not
    with rarfile.RarFile("test/files/rar3-comment-plain.rar") as rf:
        assert rf.namelist() == ["file1.txt", "file2.txt"]
        assert rf.comment is None
        assert rf.strerror() is None

    # rar2 archive with old-style comment
    with rarfile.RarFile("test/files/rar202-comment-nopsw.rar") as rf:
        assert rf.namelist() == ["FILE1.TXT", "FILE2.TXT"]
        assert rf.comment is None
        assert rf.strerror() is None

    # rar5 archive with stored comment: fully readable without tool
    with rarfile.RarFile("test/files/rar5-crc.rar") as rf:
        assert rf.comment == "RAR5 archive - crc\n"

    # stored entries are readable without any tool
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        inf = rf.getinfo("stest2.txt")
        assert inf.compress_type == rarfile.RAR_M0
        data = rf.read("stest2.txt")
        assert len(data) == inf.file_size


def test_plan_direct_without_tools(no_tools: None) -> None:
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        plan = rf.extraction_plan("stest2.txt")
        assert plan.ok
        assert plan
        assert plan.direct
        assert plan.tool is None
        assert plan.features == frozenset()
        assert plan.problems == ()


def test_plan_no_tool(no_tools: None) -> None:
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        plan = rf.extraction_plan()
        assert not plan.ok
        assert not plan
        assert not plan.direct
        assert plan.tool is None
        assert plan.features == frozenset({"compressed"})
        assert plan.problems == ("Cannot find working tool",)

        plan = rf.extraction_plan("stest1.txt")
        assert not plan.ok
        assert plan.features == frozenset({"compressed"})


@pytest.mark.skipif(not have_any_tool(), reason="no extraction tool installed")
def test_plan_with_tool() -> None:
    setup = backend.find_tool()
    assert setup is not None
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        plan = rf.extraction_plan()
        assert plan.ok
        assert not plan.direct
        assert plan.tool == setup.executable
        assert plan.problems == ()


def test_plan_features() -> None:
    with rarfile.RarFile("test/files/rar5-psw.rar") as rf:
        plan = rf.extraction_plan()
        assert plan.features == frozenset({"compressed", "encrypted"})
    with rarfile.RarFile("test/files/rar5-solid.rar") as rf:
        plan = rf.extraction_plan()
        assert plan.features == frozenset({"compressed", "solid"})


def test_plan_bsdtar_caps(bsdtar_only: None) -> None:
    with rarfile.RarFile("test/files/rar3-comment-psw.rar") as rf:
        plan = rf.extraction_plan()
        assert not plan.ok
        assert plan.tool is not None
        assert len(plan.problems) == 1
        assert "encrypted" in plan.problems[0]

    with rarfile.RarFile("test/files/rar5-solid.rar") as rf:
        plan = rf.extraction_plan()
        assert not plan.ok
        assert len(plan.problems) == 1
        assert "solid" in plan.problems[0]

    with rarfile.RarFile("test/files/seektest.rar") as rf:
        assert rf.extraction_plan().ok


@pytest.mark.skipif(not have_any_tool(), reason="no extraction tool installed")
def test_wrong_password_not_cannot_exec() -> None:
    with rarfile.RarFile("test/files/rar5-psw.rar") as rf:
        with pytest.raises(rarfile.Error) as exc_info:
            rf.read("stest1.txt", "wrong-password")
    assert not isinstance(exc_info.value, rarfile.RarCannotExec)


@pytest.mark.skipif(not have_any_tool(), reason="no extraction tool installed")
def test_corrupt_data_not_cannot_exec() -> None:
    with open("test/files/seektest.rar", "rb") as f:
        data = bytearray(f.read())
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        inf = rf.getinfo("stest1.txt")
        assert inf.data_offset is not None
        data[inf.data_offset + 5] ^= 0xFF
    with rarfile.RarFile(io.BytesIO(bytes(data))) as rf:
        with pytest.raises(rarfile.Error) as exc_info:
            rf.read("stest1.txt")
    assert not isinstance(exc_info.value, rarfile.RarCannotExec)


def test_tool_error_not_masked_by_missing_tool(no_tools: None) -> None:
    """Tool exit codes must map to tool errors even if the tool
    configuration is broken by the time the error is handled."""
    with rarfile.RarFile("test/files/seektest.rar") as rf:
        inf = rf.getinfo("stest1.txt")

        # unrar exit code 11 is "wrong password"
        pr = PipeReader(inf, [sys.executable, "-c", "raise SystemExit(11)"],
                        None, backend.UNRAR_CONFIG["errmap"])
        pr.close()
        with pytest.raises(rarfile.RarWrongPassword):
            pr._check_returncode()

        # unrar exit code 3 is "CRC error"
        pr = PipeReader(inf, [sys.executable, "-c", "raise SystemExit(3)"],
                        None, backend.UNRAR_CONFIG["errmap"])
        pr.close()
        with pytest.raises(rarfile.RarCRCError):
            pr._check_returncode()

        # 7z exit code 2 is "fatal error"
        pr = PipeReader(inf, [sys.executable, "-c", "raise SystemExit(2)"],
                        None, backend.SEVENZIP_CONFIG["errmap"])
        pr.close()
        with pytest.raises(rarfile.RarFatalError):
            pr._check_returncode()
