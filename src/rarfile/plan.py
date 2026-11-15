"""Plan extraction of archive members into an empty directory.

The planner walks members in archive order and assigns each one a
destination relative to the extraction root, recording conflicts
instead of silently resolving them:

- the same file name appearing more than once,
- names that collide once letter case is folded,
- symlink entries, whose targets are validated to stay inside the root,
- members whose path would escape the root through a previously
  planned link, even if the path itself looks legal.

How collisions are resolved is chosen by the caller via the
``overwrite`` policy.  Unless the caller explicitly allows
overwriting, every member keeps its own copy: conflicting members
are renamed (or skipped), never silently replaced by the last one.
"""

import os
import warnings
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

from . import config
from .bits import RAR_OS_UNIX, RAR5_XREDIR_WINDOWS_JUNCTION
from .errors import BadRarFile, BadSymLinkError, UnsupportedWarning
from .info import RarInfo
from .utils import sanitize_filename

if TYPE_CHECKING:
    from .archive import RarFile

__all__ = ("ExtractPlan", "PlanConflict", "PlanEntry", "plan_members")

#: How to resolve destination collisions between members.
#:
#: - "rename": keep all copies, later ones get a " (N)" suffix.
#: - "skip": keep the first copy, skip later ones.
#: - "overwrite": caller authorizes the last copy to win.
#: - "error": raise :class:`BadRarFile` on any collision.
OverwritePolicy: TypeAlias = Literal["rename", "overwrite", "skip", "error"]

#: What a planned entry will do when the plan is applied.
PlanAction: TypeAlias = Literal["file", "dir", "symlink", "skip"]

#: Why two members collide.
#:
#: - "duplicate": exact same destination path.
#: - "casefold": destinations differ only in letter case.
ConflictKind: TypeAlias = Literal["duplicate", "casefold"]


@dataclass
class PlanConflict:
    """A collision between two members, found while planning.

    Attributes:

        kind
            One of "duplicate" or "casefold".
        member
            Archive name of the member that was placed second.
        other
            Archive name (or path) of the member already occupying
            the destination.
    """

    kind: ConflictKind
    member: str
    other: str


@dataclass
class PlanEntry:
    """Planned extraction of a single archive member.

    Attributes:

        info
            The archive member.
        action
            One of "file", "dir", "symlink", "skip".
        dest
            Destination path relative to the extraction root,
            using the local path separator.  Differs from the
            member name if it was renamed to avoid a conflict.
        target
            For symlinks, the validated link target.  Guaranteed
            to stay inside the extraction root.
        conflicts
            Collisions this member was involved in.
    """

    info: RarInfo
    action: PlanAction
    dest: str
    target: str | None = None
    conflicts: list[PlanConflict] = field(default_factory=list)


@dataclass
class ExtractPlan:
    """Ordered plan for extracting members into an empty directory.

    Entries must be applied in order: a symlink entry may redirect
    the destination of later entries (always within the root).
    """

    entries: list[PlanEntry] = field(default_factory=list)

    def __iter__(self) -> Iterator[PlanEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def conflicts(self) -> list[PlanConflict]:
        """All collisions found while planning, in order."""
        return [c for e in self.entries for c in e.conflicts]


def plan_members(rf: "RarFile",
                 members: Iterable[str | Path | RarInfo] | None = None,
                 pwd: str | None = None,
                 overwrite: OverwritePolicy = "rename",
                 case_sensitive: bool = False) -> ExtractPlan:
    """Build an extraction plan for archive members.

    Parameters:

        rf
            Open :class:`RarFile` to plan for.
        members
            Optional subset of members to plan for, as names or
            :class:`RarInfo` instances.  Default is all members.
        pwd
            Optional password, needed to read symlink targets from
            encrypted archives.
        overwrite
            Collision policy, see :data:`OverwritePolicy`.
            The default "rename" keeps every copy.
        case_sensitive
            Set to True if the destination filesystem is
            case-sensitive.  By default names that differ only in
            letter case are treated as colliding.

    The plan only reads archive metadata (and symlink targets);
    nothing is written to disk.  Symlink targets that point outside
    the extraction root raise :class:`BadSymLinkError`, and member
    paths crossing a non-directory raise :class:`BadRarFile`,
    regardless of the overwrite policy.
    """
    if overwrite not in ("rename", "overwrite", "skip", "error"):
        raise ValueError("Invalid value for overwrite= parameter.")
    if members is None:
        infos = list(rf.infolist())
    else:
        infos = [rf.getinfo(m) for m in members]
    planner = _Planner(rf, pwd, overwrite, case_sensitive)
    return planner.run(infos)


class _Planner:
    """Single-pass extraction planner."""

    def __init__(self, rf: "RarFile", pwd: str | None,
                 overwrite: OverwritePolicy, case_sensitive: bool):
        self._rf = rf
        self._pwd = pwd
        self._overwrite = overwrite
        self._case_sensitive = case_sensitive
        # resolved destination -> (kind, original name, entry or None for implicit dirs)
        self._used: dict[tuple[str, ...], tuple[PlanAction, str, PlanEntry | None]] = {}
        # resolved symlink path -> resolved link target, both relative to root
        self._links: dict[tuple[str, ...], tuple[str, ...]] = {}
        self.plan = ExtractPlan()

    def run(self, infos: list[RarInfo]) -> ExtractPlan:
        for info in infos:
            self._place(info)
        return self.plan

    def _key(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        if self._case_sensitive:
            return parts
        return tuple(p.casefold() for p in parts)

    def _resolve(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        """Resolve path through previously planned symlinks."""
        cur: tuple[str, ...] = ()
        for part in parts:
            cand = cur + (part,)
            cur = self._links.get(self._key(cand), cand)
        return cur

    def _place(self, info: RarInfo) -> None:
        fname = sanitize_filename(info.filename, os.path.sep, config.WIN32)
        if not fname:
            raise BadRarFile("Invalid filename in archive: %r" % info.filename)
        parts = tuple(fname.split(os.path.sep))

        if info.is_symlink():
            kind: PlanAction = "symlink"
        elif info.is_dir():
            kind = "dir"
        elif info.is_file():
            kind = "file"
        else:
            return

        # Where do the parent directories really end up, once links
        # planned earlier are followed?  All planned link targets are
        # validated to stay inside the root, so the result can never
        # escape it either - verify anyway.
        parent = self._resolve(parts[:-1])
        if ".." in parent:
            raise BadRarFile(
                "Refusing to extract entry that escapes destination: %r" % info.filename
            )
        self._require_dirs(parent, info)

        entry = PlanEntry(info=info, action=kind, dest=fname)
        final = self._avoid_collision(parts, parent, kind, entry)
        if final is None:
            self.plan.entries.append(entry)
            return

        dest = parent + (final[-1],)
        self._used[self._key(dest)] = (kind, final[-1], entry)
        entry.dest = os.path.sep.join(parts[:-1] + (final[-1],))

        if kind == "symlink":
            target = self._link_target(info)
            if target is None:
                entry.action = "skip"
            else:
                self._links[self._key(dest)] = self._check_target(parent, target)
                entry.target = target

        self.plan.entries.append(entry)

    def _require_dirs(self, parent: tuple[str, ...], info: RarInfo) -> None:
        """Register implicit parent directories of a resolved path."""
        for i in range(1, len(parent) + 1):
            prefix = parent[:i]
            key = self._key(prefix)
            have = self._used.get(key)
            if have is None:
                self._used[key] = ("dir", prefix[-1], None)
            elif have[0] != "dir":
                raise BadRarFile(
                    "Member path crosses a non-directory entry: %r" % info.filename
                )

    def _avoid_collision(self, parts: tuple[str, ...], parent: tuple[str, ...],
                         kind: PlanAction, entry: PlanEntry) -> tuple[str, ...] | None:
        """Apply overwrite policy.  Returns final name parts or None if skipped."""
        key = self._key(parent + (parts[-1],))
        have = self._used.get(key)
        if have is None:
            return parts

        have_kind, have_name, have_entry = have
        if kind == "dir" and have_kind == "dir":
            # directories merge, this is not a conflict
            return parts

        other = have_entry.info.filename if have_entry is not None else have_name
        ckind: ConflictKind = "duplicate" if have_name == parts[-1] else "casefold"
        entry.conflicts.append(PlanConflict(kind=ckind, member=entry.info.filename, other=other))

        if self._overwrite == "error":
            raise BadRarFile("Duplicate member in archive: %r" % entry.info.filename)
        if self._overwrite == "skip":
            entry.action = "skip"
            return None
        if self._overwrite == "overwrite":
            if kind == "dir" or have_kind == "dir":
                raise BadRarFile(
                    "Refusing to overwrite directory: %r" % entry.info.filename
                )
            # caller authorized last-copy-wins; drop any link planned here
            self._links.pop(key, None)
            return parts

        # rename: find a free "name (N).ext" next to the original
        stem, ext = os.path.splitext(parts[-1])
        count = 1
        while True:
            cand_name = "%s (%d)%s" % (stem, count, ext)
            if self._key(parent + (cand_name,)) not in self._used:
                return parts[:-1] + (cand_name,)
            count += 1

    def _link_target(self, info: RarInfo) -> str | None:
        if info.host_os == RAR_OS_UNIX:
            return self._rf.read(info, self._pwd).decode("utf8", "replace")
        if info.file_redir:
            redir_type, _redir_flags, link_name = info.file_redir
            if redir_type == RAR5_XREDIR_WINDOWS_JUNCTION:
                warnings.warn(f"Windows junction not supported - {info.filename}",
                              UnsupportedWarning)
                return None
            return link_name
        warnings.warn(f"Unsupported link type - {info.filename}", UnsupportedWarning)
        return None

    def _check_target(self, parent: tuple[str, ...], target: str) -> tuple[str, ...]:
        """Validate link target, return it resolved relative to root."""
        if os.path.isabs(target) or os.path.splitdrive(target)[0]:
            raise BadSymLinkError("Absolute links not allowed")
        cur = list(parent)
        for seg in target.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not cur:
                    raise BadSymLinkError("Link to outside not allowed")
                cur.pop()
            else:
                cur.append(seg)
        return tuple(cur)
