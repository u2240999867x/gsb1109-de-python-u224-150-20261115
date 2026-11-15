"""Bounded collection of archive entry contents.

This module implements the "collector" style access pattern: return only
the leading bytes of selected files while keeping a separate, cumulative
limit on how much data may be decoded during one run.

Two limits are intentionally distinct:

``head_limit``
    Maximum number of uncompressed bytes returned for a single file.
    Hitting it only marks that entry as truncated, it does not stop the
    run.

``decode_limit``
    Maximum number of uncompressed bytes the run may consume in total.
    For solid archives, reading a later member also requires decoding the
    members it depends on.  That solid-prefix cost is charged to the
    budget even though those prefix bytes are not returned.  If a member
    cannot be reached within the remaining budget, collection stops
    *before* that member and reports its name.

Directories and other non-file records are never opened.  Their declared
size is ignored, so a corrupt directory record with an implausibly large
size cannot trigger unbounded reads or unbounded memory growth.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import config
from .bits import RAR_FILE_SOLID
from .errors import Error
from .utils import PathLike

if TYPE_CHECKING:
    from .archive import RarFile
    from .info import RarInfo

__all__ = (
    "CollectedFile",
    "CollectReport",
    "CollectSkip",
    "collect",
    "collect_files",
)


@dataclass(frozen=True, kw_only=True)
class CollectSkip:
    """A selected archive record that was not read.

    Attributes:

        info
            Record that was skipped.

        reason
            Why it was skipped: ``"directory"``, ``"symlink"`` or
            ``"not-a-file"``.
    """

    info: RarInfo
    reason: str

    @property
    def filename(self) -> str:
        """Entry name."""
        return self.info.filename

    @property
    def declared_size(self) -> int:
        """Uncompressed size claimed by the entry header."""
        return self.info.file_size


@dataclass(frozen=True, kw_only=True)
class CollectedFile:
    """Data collected from a single archive entry.

    Attributes:

        info
            Entry the data came from.

        data
            Leading bytes that were returned.  This may be shorter than
            :attr:`declared_size`.

        prefix_bytes
            Solid-prefix bytes that had to be decoded to reach this entry.
            These bytes are not part of :attr:`data`.

        decoded_bytes
            Total decoded bytes charged for this entry, that is
            ``prefix_bytes + returned_size``.  This is the amount actually
            consumed from the decode budget.

        requested_bytes
            Number of bytes requested from this entry, normally
            ``min(head_limit, declared_size)``.  The budget check reserves
            this amount before the read.

        error
            Error text if reading failed, otherwise None.
    """

    info: RarInfo
    data: bytes
    prefix_bytes: int = 0
    decoded_bytes: int = 0
    requested_bytes: int = 0
    error: str | None = None

    @property
    def filename(self) -> str:
        """Entry name."""
        return self.info.filename

    @property
    def declared_size(self) -> int:
        """Uncompressed size claimed by the entry header."""
        return self.info.file_size

    @property
    def returned_size(self) -> int:
        """Number of bytes in :attr:`data`."""
        return len(self.data)

    @property
    def truncated(self) -> bool:
        """True if the returned data is shorter than the declared size."""
        return len(self.data) < self.info.file_size

    @property
    def complete(self) -> bool:
        """True only if the returned data is the complete original entry."""
        return self.error is None and len(self.data) == self.info.file_size


@dataclass(frozen=True, kw_only=True)
class CollectReport:
    """Result of one bounded collection run.

    Attributes:

        files
            Entries that were successfully opened, in archive order.

        skipped
            Selected directories and other non-file records.

        head_limit
            Per-file return cap that was used.

        decode_limit
            Cumulative decode budget that was used.

        decode_used
            Decoded bytes charged during the run, including solid-prefix
            bytes.

        prefix_bytes
            Portion of ``decode_used`` spent on solid prefixes.

        returned_bytes
            Total number of bytes returned in ``files``.

        stopped_before
            Name of the first entry that was not attempted because its
            cost did not fit the decode budget.  None if the run was not
            stopped by the budget.

        stopped_before_info
            Record corresponding to :attr:`stopped_before`.

        stopped_cost
            Cost that would have been needed to collect that entry.

        stopped_reason
            ``"decode_limit"`` when the budget stopped the run, otherwise
            None.
    """

    files: list[CollectedFile]
    skipped: list[CollectSkip]
    head_limit: int
    decode_limit: int
    decode_used: int = 0
    prefix_bytes: int = 0
    returned_bytes: int = 0
    stopped_before: str | None = None
    stopped_before_info: RarInfo | None = None
    stopped_cost: int | None = None
    stopped_reason: str | None = None

    @property
    def decode_remaining(self) -> int:
        """Decode budget not yet charged."""
        return self.decode_limit - self.decode_used

    @property
    def decode_limit_reached(self) -> bool:
        """True if the run stopped because the decode budget ran out."""
        return self.stopped_reason == "decode_limit"

    @property
    def truncated_files(self) -> list[CollectedFile]:
        """Entries whose returned data is shorter than the original."""
        return [f for f in self.files if f.truncated]

    @property
    def complete(self) -> bool:
        """True if every collected entry is the complete original."""
        return (
            self.stopped_before is None
            and all(f.complete for f in self.files)
        )


def _entry_is_dir(info: RarInfo) -> bool:
    """Check for directory records without trusting their size fields."""
    return info.is_dir() or info.filename.endswith("/")


def _solid_prefix_costs(infos: Sequence[RarInfo]) -> list[int]:
    """Return bytes that must be decoded before each entry.

    Only entries linked by the solid flag contribute.  The computation
    works on declared sizes and never allocates or reads anything, so
    corrupt sizes cannot expand memory use.
    """
    costs: list[int] = []
    running = 0
    for info in infos:
        if not info.is_file() or _entry_is_dir(info):
            # Non-file records carry no stream data and must not break
            # the prefix chain of the surrounding solid stream.
            costs.append(0)
            continue
        if info.flags & RAR_FILE_SOLID:
            costs.append(running)
            running += info.file_size
        else:
            # A non-solid entry starts a fresh stream.
            costs.append(0)
            running = info.file_size
    return costs


def _read_head(rf: RarFile, info: RarInfo, count: int, pwd: str | None) -> tuple[bytes, str | None]:
    """Read at most ``count`` leading bytes from an entry.

    Partial data is kept if reading fails midway.  Any error is returned
    to the caller instead of being raised.
    """
    if count <= 0:
        return b"", None

    buf = bytearray()
    try:
        with rf.open(info, "r", pwd) as f:
            while len(buf) < count:
                chunk = f.read(min(config.BSIZE, count - len(buf)))
                if not chunk:
                    break
                buf += chunk
    except (Error, OSError) as ex:
        msg = str(ex)
        err = "%s: %s" % (type(ex).__name__, msg) if msg else type(ex).__name__
        return bytes(buf), err
    return bytes(buf), None


def _resolve_selected(rf: RarFile, members: Iterable[str | Path | RarInfo] | None) -> set[int] | None:
    """Turn a member list into a set of identity keys.

    Missing names raise :class:`~rarfile.NoRarEntry`, matching the rest of
    the API.  ``None`` means all records.
    """
    if members is None:
        return None
    selected: set[int] = set()
    for member in members:
        selected.add(id(rf.getinfo(member)))
    return selected


def collect_files(rf: RarFile,
                  members: Iterable[str | Path | RarInfo] | None = None,
                  *,
                  head_limit: int | None = None,
                  decode_limit: int | None = None,
                  pwd: str | None = None) -> CollectReport:
    """Collect leading bytes from selected entries.

    Entries are processed in archive order so that solid-prefix costs are
    accounted for in the same order the decoder sees them.

    Parameters:

        rf
            Open :class:`RarFile`.
        members
            Names or :class:`RarInfo` records to collect.  None means all
            records.
        head_limit
            Maximum bytes returned per file.  Defaults to
            :data:`rarfile.config.COLLECT_HEAD_LIMIT`.
        decode_limit
            Maximum uncompressed bytes decoded during this run, including
            solid prefixes.  Defaults to
            :data:`rarfile.config.COLLECT_DECODE_LIMIT`.
        pwd
            Optional password for encrypted entries.

    Raises:

        ValueError
            If a limit is negative.
    """
    if head_limit is None:
        head_limit = config.COLLECT_HEAD_LIMIT
    if decode_limit is None:
        decode_limit = config.COLLECT_DECODE_LIMIT
    if head_limit < 0:
        raise ValueError("head_limit must be non-negative")
    if decode_limit < 0:
        raise ValueError("decode_limit must be non-negative")

    infos = list(rf.infolist())
    selected = _resolve_selected(rf, members)
    prefix_costs = _solid_prefix_costs(infos)

    files: list[CollectedFile] = []
    skipped: list[CollectSkip] = []
    decode_used = 0
    prefix_used = 0
    returned_bytes = 0

    for index, info in enumerate(infos):
        if selected is not None and id(info) not in selected:
            continue

        # Never open directories or other non-file records, regardless of
        # the size their header claims.
        if _entry_is_dir(info):
            skipped.append(CollectSkip(info=info, reason="directory"))
            continue
        if info.is_symlink():
            skipped.append(CollectSkip(info=info, reason="symlink"))
            continue
        if not info.is_file():
            skipped.append(CollectSkip(info=info, reason="not-a-file"))
            continue

        requested = min(head_limit, info.file_size)
        # No output requested means no decoder work either, not even the
        # solid prefix needed to reach the entry.
        prefix = prefix_costs[index] if requested > 0 else 0
        # The return cap limits output bytes; the prefix is a separate
        # decode cost.  Both have to fit into what is left of the budget.
        cost = prefix + requested
        if cost > decode_limit - decode_used:
            return CollectReport(
                files=files,
                skipped=skipped,
                head_limit=head_limit,
                decode_limit=decode_limit,
                decode_used=decode_used,
                prefix_bytes=prefix_used,
                returned_bytes=returned_bytes,
                stopped_before=info.filename,
                stopped_before_info=info,
                stopped_cost=cost,
                stopped_reason="decode_limit",
            )

        data, err = _read_head(rf, info, requested, pwd)
        charged = prefix + len(data)
        decode_used += charged
        prefix_used += prefix
        returned_bytes += len(data)
        files.append(CollectedFile(
            info=info,
            data=data,
            prefix_bytes=prefix,
            decoded_bytes=charged,
            requested_bytes=requested,
            error=err,
        ))

    return CollectReport(
        files=files,
        skipped=skipped,
        head_limit=head_limit,
        decode_limit=decode_limit,
        decode_used=decode_used,
        prefix_bytes=prefix_used,
        returned_bytes=returned_bytes,
    )


def collect(source: RarFile | PathLike,
            members: Iterable[str | Path | RarInfo] | None = None,
            *,
            head_limit: int | None = None,
            decode_limit: int | None = None,
            pwd: str | None = None) -> CollectReport:
    """Collect leading bytes from an archive.

    ``source`` may be an open :class:`RarFile` or anything accepted by its
    constructor.  When a path or file object is passed, the archive is
    opened and closed by this function.
    """
    from .archive import RarFile

    if isinstance(source, RarFile):
        return collect_files(source, members, head_limit=head_limit,
                             decode_limit=decode_limit, pwd=pwd)

    with RarFile(source) as rf:
        if pwd is not None:
            rf.setpassword(pwd)
        return collect_files(rf, members, head_limit=head_limit,
                             decode_limit=decode_limit, pwd=pwd)
