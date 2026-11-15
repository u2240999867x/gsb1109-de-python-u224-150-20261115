import errno
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from subprocess import DEVNULL, PIPE, STDOUT, Popen
from typing import Literal, Protocol, TypeAlias, TypedDict, TypeVar

from . import config
from .errors import (
    BadRarFile, RarCannotExec, RarCRCError, RarCreateError, RarFatalError,
    RarLockedArchiveError, RarMemoryError, RarNoFilesError, RarOpenError,
    RarSignalExit, RarUnknownError, RarUserBreak, RarUserError,
    RarWarning, RarWriteError, RarWrongPassword,
)

if sys.version_info < (3, 11):
    from typing_extensions import NotRequired
else:
    from typing import NotRequired


T_co = TypeVar("T_co", str, bytes, covariant=True)


class Reader(Protocol[T_co]):
    def read(self, n: int = -1, /) -> T_co: ...


__all__ = (
    "empty_read", "custom_popen", "check_returncode", "ToolSetup", "tool_setup",
    "find_tool", "plan_extraction", "ExtractPlan",
    "FEAT_COMPRESSED", "FEAT_ENCRYPTED", "FEAT_SOLID",
    "CAP_PASSWORD", "CAP_SOLID",
)

CmdLine: TypeAlias = tuple[str, ...]
ErrMap: TypeAlias = Sequence[type[Exception] | None]
CmdLineKeys: TypeAlias = Literal["open_cmd", "check_cmd"]

#: Archive feature: entries are compressed, external tool is required.
FEAT_COMPRESSED = "compressed"

#: Archive feature: entries are encrypted, tool must support passwords.
FEAT_ENCRYPTED = "encrypted"

#: Archive feature: solid compression is used, tool must handle solid archives.
FEAT_SOLID = "solid"

#: Tool capability: can extract password-protected entries.
CAP_PASSWORD = "password"

#: Tool capability: can extract from solid archives.
CAP_SOLID = "solid"

#: Map archive feature to tool capability it requires.
_FEATURE_TOOL_CAPS = {
    FEAT_ENCRYPTED: CAP_PASSWORD,
    FEAT_SOLID: CAP_SOLID,
}


class ToolConfig(TypedDict):
    open_cmd: CmdLine
    check_cmd: CmdLine
    password: str | tuple[str, ...] | None
    no_password: Sequence[str]
    errmap: ErrMap
    check_output: NotRequired[str]
    executables: NotRequired[Sequence[str]]
    caps: NotRequired[Sequence[str]]


def empty_read(src: Reader[bytes], size: int, blklen: int) -> None:
    """Read and drop fixed amount of data.
    """
    while size > 0:
        if size > blklen:
            res = src.read(blklen)
        else:
            res = src.read(size)
        if not res:
            raise BadRarFile("cannot load data")
        size -= len(res)


def custom_popen(cmd: Sequence[str]) -> Popen[bytes]:
    """Disconnect cmd from parent fds, read only from stdout.
    """
    creationflags = 0x08000000 if config.WIN32 else 0  # CREATE_NO_WINDOW
    try:
        p = Popen(cmd, bufsize=0, stdout=PIPE, stderr=STDOUT, stdin=DEVNULL,
                  creationflags=creationflags)
    except OSError as ex:
        if ex.errno == errno.ENOENT:
            raise RarCannotExec("Tool not installed: %s" % cmd[0]) from None
        if ex.errno == errno.EACCES or ex.errno == errno.EPERM:
            raise RarCannotExec("Cannot execute tool: %s" % cmd[0]) from None
        raise
    return p


def check_returncode(code: int, out: str, errmap: ErrMap) -> None:
    """Raise exception according to unrar exit code.
    """
    if code == 0:
        return

    if code > 0 and code < len(errmap):
        exc = errmap[code] or RarUnknownError
    elif code == 255:
        exc = RarUserBreak
    elif code < 0:
        exc = RarSignalExit
    else:
        exc = RarUnknownError

    # format message
    if out:
        msg = "%s [%d]: %s" % (exc.__doc__, code, out)
    else:
        msg = "%s [%d]" % (exc.__doc__, code)

    raise exc(msg)


class ToolSetup:
    def __init__(self, setup: ToolConfig):
        self.setup = setup
        self.executable: str | None = None

    def check(self) -> bool:
        if "executables" in self.setup:
            for varname in self.setup["executables"]:
                tool = getattr(config, varname, None)
                if tool is None:
                    continue
                cmdline = [tool] + list(self.setup["check_cmd"])
                try:
                    p = custom_popen(cmdline)
                    out, _ = p.communicate()
                    if p.returncode != 0:
                        continue
                    pattern = self.setup.get("check_output")
                    if pattern and not re.search(pattern, out.decode("utf8", errors="replace")):
                        continue
                    self.executable = tool
                    return True
                except RarCannotExec:
                    continue
            return False
        # Legacy single-executable path
        cmdline = self.get_cmdline("check_cmd", None)
        self.executable = cmdline[0]
        try:
            p = custom_popen(cmdline)
            out, _ = p.communicate()
            return p.returncode == 0
        except RarCannotExec:
            return False

    def open_cmdline(self, pwd: str | None, rarfn: str, filefn: str | None = None) -> list[str]:
        cmdline = self.get_cmdline("open_cmd", pwd)
        cmdline.append(rarfn)
        if filefn:
            self.add_file_arg(cmdline, filefn)
        return cmdline

    def get_errmap(self) -> ErrMap:
        return self.setup["errmap"]

    def supports(self, cap: str) -> bool:
        """Check whether the tool supports a capability.

        Capabilities are :data:`CAP_PASSWORD` and :data:`CAP_SOLID`.
        Tools without declared capabilities are assumed to support everything.
        """
        caps = self.setup.get("caps")
        if caps is None:
            return True
        return cap in caps

    def missing_caps(self, needed: Iterable[str]) -> list[str]:
        """Return capabilities from ``needed`` that the tool does not support.
        """
        return [cap for cap in needed if not self.supports(cap)]

    def get_cmdline(self, key: CmdLineKeys, pwd: str | bytes | None, nodash: bool = False) -> list[str]:
        cmdline: list[str]
        if "executables" in self.setup and self.executable:
            cmdline = [self.executable] + list(self.setup[key])
        else:
            cmdline = list(self.setup[key])
            cmdline[0] = getattr(config, self.setup[key][0])
        if key == "check_cmd":
            return cmdline
        self.add_password_arg(cmdline, pwd)
        if not nodash:
            cmdline.append("--")
        return cmdline

    def add_file_arg(self, cmdline: list[str], filename: str) -> None:
        cmdline.append(filename)

    def add_password_arg(self, cmdline: list[str], pwd: str | bytes | None) -> None:
        """Append password switch to commandline.
        """
        if pwd is not None:
            if not isinstance(pwd, str):
                pwd = pwd.decode("utf8")
            args = self.setup["password"]
            if args is None:
                tool = self.executable or self.setup["open_cmd"][0]
                raise RarCannotExec(f"{tool} does not support passwords")
            elif isinstance(args, str):
                cmdline.append(args + pwd)
            else:
                cmdline.extend(args)
                cmdline.append(pwd)
        else:
            cmdline.extend(self.setup["no_password"])


UNRAR_CONFIG: ToolConfig = {
    "open_cmd": ("UNRAR_TOOL", "p", "-inul"),
    "check_cmd": ("UNRAR_TOOL", "-inul", "-?"),
    "password": "-p",
    "no_password": ("-p-",),
    "caps": (CAP_PASSWORD, CAP_SOLID),
    # map return code to exception class, codes from rar.txt
    "errmap": [None,
               RarWarning, RarFatalError, RarCRCError, RarLockedArchiveError,    # 1..4
               RarWriteError, RarOpenError, RarUserError, RarMemoryError,        # 5..8
               RarCreateError, RarNoFilesError, RarWrongPassword]                # 9..11
}

# Problems with unar RAR backend:
# - Does not support RAR2 locked files [fails to read]
# - Does not support RAR5 Blake2sp hash [reading works]
UNAR_CONFIG: ToolConfig = {
    "open_cmd": ("UNAR_TOOL", "-q", "-o", "-"),
    "check_cmd": ("UNAR_TOOL", "-version"),
    "password": ("-p",),
    "no_password": ("-p", ""),
    "caps": (CAP_PASSWORD, CAP_SOLID),
    "errmap": [None],
}

# Problems with libarchive RAR backend:
# - Does not support solid archives.
# - Does not support password-protected archives.
# - Does not support RARVM-based compression filters.
BSDTAR_CONFIG: ToolConfig = {
    "executables": ("BSDTAR_TOOL", "TAR_TOOL"),
    "open_cmd": ("-x", "--to-stdout", "-f"),
    "check_cmd": ("--version",),
    "check_output": "bsdtar|libarchive",
    "password": None,
    "no_password": (),
    "caps": (),
    "errmap": [None],
}

SEVENZIP_CONFIG: ToolConfig = {
    "executables": ("SEVENZIP_TOOL", "SEVENZIP2_TOOL"),
    "open_cmd": ("e", "-so", "-bb0"),
    "check_cmd": ("i",),
    "check_output": "Rar3",  # rar plugin appears in "Codec" not "Format"
    "password": "-p",
    "no_password": ("-p",),
    "caps": (CAP_PASSWORD, CAP_SOLID),
    "errmap": [None,
               RarWarning, RarFatalError, None, None,           # 1..4
               None, None, RarUserError, RarMemoryError]        # 5..8
}

CURRENT_SETUP: ToolSetup | None = None


def find_tool(unrar: bool = True, unar: bool = True, bsdtar: bool = True,
              sevenzip: bool = True, force: bool = False) -> ToolSetup | None:
    """Pick a tool, return cached ToolSetup.

    Returns None if no configured tool works, without raising.

    .. versionadded:: 5.0
    """
    global CURRENT_SETUP
    if force:
        CURRENT_SETUP = None
    if CURRENT_SETUP is not None:
        return CURRENT_SETUP
    lst: list[ToolConfig] = []
    if unrar:
        lst.append(UNRAR_CONFIG)
    if unar:
        lst.append(UNAR_CONFIG)
    if sevenzip:
        lst.append(SEVENZIP_CONFIG)
    if bsdtar:
        lst.append(BSDTAR_CONFIG)

    for conf in lst:
        setup = ToolSetup(conf)
        if setup.check():
            CURRENT_SETUP = setup
            break
    return CURRENT_SETUP


def tool_setup(unrar: bool = True, unar: bool = True, bsdtar: bool = True,
               sevenzip: bool = True, force: bool = False) -> ToolSetup:
    """Pick a tool, return cached ToolSetup.

    Raises :exc:`RarCannotExec` if no configured tool works.
    """
    setup = find_tool(unrar, unar, bsdtar, sevenzip, force)
    if setup is None:
        raise RarCannotExec("Cannot find working tool")
    return setup


@dataclass(frozen=True)
class ExtractPlan:
    """Result of matching archive features against locally available tools.

    .. versionadded:: 5.0

    Attributes:

        ok
            True if the requested entries can be extracted locally.

        tool
            Resolved tool executable that would be used for extraction.
            None if no external tool is needed or no working tool was found.

        direct
            True if the requested entries can be read directly,
            without running an external tool.

        features
            Archive features that affect extraction:
            ``"compressed"``, ``"encrypted"``, ``"solid"``.

        problems
            Human-readable reasons why extraction cannot proceed.
            Empty when the plan is executable.
    """

    ok: bool
    tool: str | None
    direct: bool
    features: frozenset[str]
    problems: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.ok


def plan_extraction(features: Iterable[str]) -> ExtractPlan:
    """Match archive features against locally configured tools.

    Runs only local capability checks, no extraction is attempted.
    Features are ``"compressed"``, ``"encrypted"`` and ``"solid"``,
    see :data:`FEAT_COMPRESSED`, :data:`FEAT_ENCRYPTED`, :data:`FEAT_SOLID`.

    .. versionadded:: 5.0
    """
    feats = frozenset(features)
    if FEAT_COMPRESSED not in feats and FEAT_ENCRYPTED not in feats:
        # stored and unencrypted entries are read directly
        return ExtractPlan(ok=True, tool=None, direct=True, features=feats, problems=())
    setup = find_tool()
    if setup is None:
        return ExtractPlan(ok=False, tool=None, direct=False, features=feats,
                           problems=("Cannot find working tool",))
    problems = []
    for feat in sorted(feats):
        cap = _FEATURE_TOOL_CAPS.get(feat)
        if cap is not None and not setup.supports(cap):
            problems.append("Tool %r does not support %s archives" % (setup.executable, feat))
    return ExtractPlan(ok=not problems, tool=setup.executable, direct=False,
                       features=feats, problems=tuple(problems))
