"""Archive verification with explicit evidence levels.

Each archive member is checked at up to three levels, and the result
records exactly how far the check went:

- ``header_ok``: member header was found and parsed, header CRC valid.
- ``data_complete``: all compressed data bytes are present and readable -
  all needed volumes exist and nothing is truncated.
- ``hash_verified``: decompressed content was hashed and the hash matches
  the value stored in the archive directory.

A member is reported as :data:`VerifyStatus.VERIFIED` only when all three
levels hold.  Anything that prevents a definitive answer - missing volumes,
missing password, no hash stored in archive, or a backend tool that does
not support the compression method - is reported as
:data:`VerifyStatus.UNVERIFIED`, never as corruption.
:data:`VerifyStatus.CORRUPT` is reported only on positive evidence:
the content hash was checked and did not match.

In particular, when a multi-volume archive is only partially present,
hashes stored in the archive directory are *not* treated as verified.

.. versionadded:: 5.0
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from . import config
from .backend import empty_read
from .bits import (
    RAR_BLOCK_MAIN, RAR_BLOCK_MARK, RAR_FILE_SPLIT_AFTER, RAR_FILE_SPLIT_BEFORE,
)
from .errors import (
    BadRarFile, BadRarName, Error, PasswordRequired, RarCannotExec,
    RarCRCError, RarWrongPassword,
)
from .info import RarInfo
from .utils import FileLike, PathLike, XFile, is_filelike

if TYPE_CHECKING:
    from .archive import RarFile
    from .format import CommonParser

__all__ = ("VerifyStatus", "VerifyResult", "ArchiveVerification", "verify_archive")


class VerifyStatus(Enum):
    """Overall verification outcome for one archive member."""

    #: Content hash was checked and matches.
    VERIFIED = "verified"

    #: Could not be checked: missing data, missing password, no hash
    #: stored in archive, or compression method not supported by the
    #: available backend tool.
    UNVERIFIED = "unverified"

    #: Content hash was checked and does not match.
    CORRUPT = "corrupt"


@dataclass
class VerifyResult:
    """Verification outcome for one archive member.

    Attributes:

        filename
            Archive member name.

        status
            Overall outcome, one of :class:`VerifyStatus` values.

        header_ok
            Member header was parsed and header CRC is valid.

        data_complete
            All compressed data bytes are present and readable.
            None if the member has no file data (directory, symlink).

        hash_verified
            Decompressed content hash matches the directory value.
            None if the hash was not checked.

        hash_kind
            Hash algorithm stored in archive: "crc32" or "blake2sp".
            None if archive stores no content hash for this member.

        reason
            Human-readable explanation for non-VERIFIED status.
    """

    filename: str
    status: VerifyStatus
    header_ok: bool
    data_complete: bool | None
    hash_verified: bool | None
    hash_kind: str | None = None
    reason: str | None = None

    def __str__(self) -> str:
        parts = ["header ok" if self.header_ok else "header broken"]
        if self.data_complete is None:
            parts.append("no file data")
        elif self.data_complete:
            parts.append("data complete")
        else:
            parts.append("data incomplete")
        if self.hash_verified is None:
            parts.append("hash not checked")
        elif self.hash_verified:
            parts.append("%s verified" % (self.hash_kind or "hash"))
        else:
            parts.append("%s mismatch" % (self.hash_kind or "hash"))
        msg = "%s: %s (%s)" % (self.filename, self.status.value, ", ".join(parts))
        if self.reason:
            msg += " - " + self.reason
        return msg


@dataclass
class ArchiveVerification:
    """Verification report for a whole archive.

    Attributes:

        results
            Per-member outcomes, in archive order.

        parse_error
            Archive-level parsing problem, if any
            (eg. missing volume, broken trailing header).
    """

    results: list[VerifyResult] = field(default_factory=list)
    parse_error: str | None = None

    def __iter__(self) -> Iterator[VerifyResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> bool:
        """True when archive parsed cleanly and every member is verified."""
        if self.parse_error is not None:
            return False
        return all(r.status is VerifyStatus.VERIFIED for r in self.results)


def verify_archive(rf: "RarFile", pwd: str | None = None) -> ArchiveVerification:
    """Verify all members of an open archive.

    Parameters:

        rf
            Open :class:`RarFile` instance.
        pwd
            Optional password for encrypted members.
    """
    parser = rf._file_parser
    report = ArchiveVerification(parse_error=rf.strerror())
    if parser is None:
        return report
    for inf in rf.infolist():
        report.results.append(_verify_entry(rf, parser, inf, pwd))
    return report


def _verify_entry(rf: "RarFile", parser: "CommonParser", inf: RarInfo,
                  pwd: str | None) -> VerifyResult:
    if not inf.is_file():
        return VerifyResult(filename=inf.filename, status=VerifyStatus.VERIFIED,
                            header_ok=True, data_complete=None, hash_verified=None,
                            reason="no file data")

    hash_kind = _hash_kind(inf)
    data_complete, data_reason = _check_data_complete(parser, inf)
    if not data_complete:
        # Directory hash must not be reported as verified when
        # the compressed data is not all there.
        return VerifyResult(filename=inf.filename, status=VerifyStatus.UNVERIFIED,
                            header_ok=True, data_complete=False, hash_verified=None,
                            hash_kind=hash_kind, reason=data_reason)

    # All compressed bytes are present, try full decompression.
    try:
        with rf.open(inf, "r", pwd) as f:
            empty_read(f, inf.file_size, config.BSIZE)
    except RarCRCError as ex:
        return _corrupt(inf, hash_kind, str(ex))
    except BadRarFile as ex:
        if "CRC check failed" in str(ex):
            return _corrupt(inf, hash_kind, str(ex))
        return _unverified(inf, hash_kind, str(ex))
    except PasswordRequired:
        return _unverified(inf, hash_kind, "password required")
    except RarWrongPassword:
        return _unverified(inf, hash_kind, "wrong password")
    except RarCannotExec as ex:
        return _unverified(inf, hash_kind, "backend unavailable: %s" % ex)
    except Error as ex:
        # Backend could not handle the compression method, or some
        # other non-hash failure - not evidence of corruption.
        return _unverified(inf, hash_kind, str(ex))
    except OSError as ex:
        return _unverified(inf, hash_kind, "read error: %s" % ex)

    if hash_kind is None:
        return VerifyResult(filename=inf.filename, status=VerifyStatus.UNVERIFIED,
                            header_ok=True, data_complete=True, hash_verified=None,
                            reason="no content hash stored in archive")
    return VerifyResult(filename=inf.filename, status=VerifyStatus.VERIFIED,
                        header_ok=True, data_complete=True, hash_verified=True,
                        hash_kind=hash_kind)


def _corrupt(inf: RarInfo, hash_kind: str | None, reason: str) -> VerifyResult:
    return VerifyResult(filename=inf.filename, status=VerifyStatus.CORRUPT,
                        header_ok=True, data_complete=True, hash_verified=False,
                        hash_kind=hash_kind, reason=reason)


def _unverified(inf: RarInfo, hash_kind: str | None, reason: str) -> VerifyResult:
    return VerifyResult(filename=inf.filename, status=VerifyStatus.UNVERIFIED,
                        header_ok=True, data_complete=True, hash_verified=None,
                        hash_kind=hash_kind, reason=reason)


def _hash_kind(inf: RarInfo) -> str | None:
    """Hash algorithm stored in archive for this entry, if any."""
    exp = inf._md_expect
    if exp is None:
        return None
    if isinstance(exp, bytes):
        return "blake2sp"
    return "crc32"


def _check_data_complete(parser: "CommonParser", inf: RarInfo) -> tuple[bool, str | None]:
    """Check that all compressed data bytes of an entry are present.

    Walks the raw data areas over volume boundaries without
    decompressing, so the result does not depend on backend tools.
    """
    total = inf.compress_size
    if total is None:
        return False, "compressed size unknown"
    if inf.flags & RAR_FILE_SPLIT_BEFORE:
        return False, "file data starts in an earlier volume"
    if inf.volume_file is None:
        return False, "volume file unknown"

    remaining = total
    cur = inf
    volfile: PathLike | FileLike = inf.volume_file
    try:
        fd = XFile(volfile, 0)
    except OSError as ex:
        return False, "cannot open volume %s: %s" % (_volname(volfile), ex)

    avail = cur.add_size
    try:
        while remaining > 0:
            if avail == 0:
                if (cur.flags & RAR_FILE_SPLIT_AFTER) == 0:
                    return False, "compressed data truncated in volume %s" % _volname(volfile)
                nxt = _next_volume(parser, volfile)
                if nxt is None:
                    return False, "cannot determine next volume name"
                fd.close()
                try:
                    fd = XFile(nxt, 0)
                except OSError:
                    return False, "missing volume: %s" % nxt
                volfile = nxt
                sig = fd.read(len(parser._expect_sig))
                if sig != parser._expect_sig:
                    return False, "invalid signature in volume: %s" % nxt
                nxt_cur = _find_continuation(parser, fd, inf)
                if nxt_cur is None:
                    return False, "continuation header not found in volume: %s" % nxt
                cur = nxt_cur
                avail = cur.add_size
                continue
            take = avail if avail < remaining else remaining
            fd.seek(cur.data_offset + (cur.add_size - avail), 0)
            while take > 0:
                chunk = fd.read(take)
                if not chunk:
                    return False, "compressed data truncated in volume %s" % _volname(volfile)
                take -= len(chunk)
                avail -= len(chunk)
                remaining -= len(chunk)
        if cur.flags & RAR_FILE_SPLIT_AFTER:
            # Directory only covers the volumes that were parsed;
            # the data continues into a volume we do not have.
            nxt = _next_volume(parser, volfile)
            if nxt is None:
                return False, "compressed data continues in another volume"
            return False, "compressed data continues in missing volume: %s" % nxt
        return True, None
    except (Error, OSError) as ex:
        return False, str(ex)
    finally:
        fd.close()


def _volname(volfile: PathLike | FileLike) -> str:
    if is_filelike(volfile):
        return "<stream>"
    return str(volfile)


def _next_volume(parser: "CommonParser", volfile: PathLike | FileLike) -> str | None:
    try:
        return parser._next_volname(volfile)
    except (OSError, BadRarName):
        return None


def _find_continuation(parser: "CommonParser", fd: XFile, inf: RarInfo) -> RarInfo | None:
    """Locate continuation file header in freshly opened volume."""
    while True:
        cur = parser._parse_header(fd)
        if cur is None:
            return None
        if cur.type in (RAR_BLOCK_MARK, RAR_BLOCK_MAIN):
            if cur.add_size:
                fd.seek(cur.add_size, 1)
            continue
        if not isinstance(cur, RarInfo):
            return None
        if cur.orig_filename != inf.orig_filename:
            return None
        return cur
