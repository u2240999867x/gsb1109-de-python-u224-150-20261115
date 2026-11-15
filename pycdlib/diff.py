# Copyright (C) 2026  Chris Lalancette <clalancette@gmail.com>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation;
# version 2.1 of the License.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

"""
Compare two ISO images by content rather than by layout.

An ISO can be re-mastered so that every file lands at a different extent
while the directory tree, the file contents, and the file attributes stay
the same.  A byte-for-byte or extent-for-extent comparison of such images
reports an enormous amount of noise.  This module instead builds a
namespace-level snapshot of each image, hashes every file, and then matches
entries between the snapshots using two kinds of evidence:

* explicit directory evidence -- the entry has the same path (or the same
  directory subtree signature) in both images;
* a content digest -- the entry's data hashes to the same value.

Neither kind of evidence is trusted on its own.  A same-path pair whose data
changed is a content update (the path proves identity, the digest proves the
change).  A different-path pair with the same digest is a path migration.
When several entries share one digest and the directory evidence cannot pick
one, the entry is reported as ambiguous rather than guessed at.

The result keeps three requested change classes separate -- content updates,
path migrations, and attribute-only changes -- and additionally reports
changes in which files share data blocks (hard links or overlapping extents),
since re-mastering often changes those relationships.

If no complete old image is available, the result never claims that an old
file was deleted; unmatched entries are reported as unverified instead.
"""

import hashlib
import posixpath
from typing import TYPE_CHECKING

from pycdlib import inode
from pycdlib import pycdlibexception
from pycdlib import udf as udfmod

if TYPE_CHECKING:
    from typing import Any, Dict, Iterable, List, Optional, Set, Tuple  # noqa: F401
    from pycdlib import pycdlib  # noqa: F401

# The namespaces in preference order.  Rock Ridge carries the richest POSIX
# metadata and is usually the namespace a caller cares about; UDF is next,
# then Joliet, and ISO9660 is always present as the fallback.
_NAMESPACES = ('rr', 'udf', 'joliet', 'iso')
_PATH_KEY = {'rr': 'rr_path', 'udf': 'udf_path',
             'joliet': 'joliet_path', 'iso': 'iso_path'}

_FILE = 'file'
_DIR = 'dir'
_SYMLINK = 'symlink'

_DIGEST_CHUNK = 65536


def _namespace_available(iso, namespace):
    # type: (pycdlib.PyCdlib, str) -> bool
    """Return whether the ISO carries the requested namespace."""
    if namespace == 'rr':
        return iso.has_rock_ridge()
    if namespace == 'udf':
        return iso.has_udf()
    if namespace == 'joliet':
        return iso.has_joliet()
    return True


def _offset_string(minutes):
    # type: (int) -> str
    """Render a signed offset expressed in minutes as +HH:MM."""
    sign = '+'
    if minutes < 0:
        sign = '-'
        minutes = -minutes
    return '%s%02d:%02d' % (sign, minutes // 60, minutes % 60)


def _date_string(value):
    # type: (Any) -> Optional[str]
    """Render any of pycdlib's date objects as a stable, comparable string.

    The ISO9660 date classes keep the GMT offset in 15-minute units (Ecma-119
    9.1.5 and 8.4.26.1); UDF keeps it in minutes.  Both are rendered as a
    conventional +HH:MM so the strings are readable and, more importantly,
    compare correctly.
    """
    if value is None:
        return None
    if hasattr(value, 'years_since_1900'):
        return '%04d-%02d-%02dT%02d:%02d:%02d%s' % (
            value.years_since_1900 + 1900, value.month, value.day_of_month,
            value.hour, value.minute, value.second,
            _offset_string(value.gmtoffset * 15))
    if hasattr(value, 'dayofmonth'):
        return '%04d-%02d-%02dT%02d:%02d:%02d.%02d%s' % (
            value.year, value.month, value.dayofmonth, value.hour,
            value.minute, value.second, value.hundredthsofsecond,
            _offset_string(value.gmtoffset * 15))
    if hasattr(value, 'centiseconds'):
        return '%04d-%02d-%02dT%02d:%02d:%02d.%02d%s' % (
            value.year, value.month, value.day, value.hour, value.minute,
            value.second, value.centiseconds, _offset_string(value.tz))
    return str(value)


class Entry:
    """One path-visible file, directory, or symlink in a snapshot."""
    __slots__ = ('path', 'namespace', 'kind', 'size', 'digest', 'attributes',
                 'extents', 'readable', 'read_error')

    def __init__(self, path, namespace, kind):
        # type: (str, str, str) -> None
        self.path = path
        self.namespace = namespace
        self.kind = kind
        self.size = 0
        self.digest = None  # type: Optional[str]
        self.attributes = {}  # type: Dict[str, Any]
        self.extents = ()  # type: Tuple[Tuple[int, int], ...]
        self.readable = True
        self.read_error = None  # type: Optional[str]

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of this entry."""
        return {'path': self.path,
                'namespace': self.namespace,
                'kind': self.kind,
                'size': self.size,
                'digest': self.digest,
                'attributes': dict(self.attributes),
                'extents': [list(extent) for extent in self.extents],
                'readable': self.readable}


class Snapshot:
    """All entries of one ISO in one namespace, plus whether it is complete."""
    __slots__ = ('path', 'namespace', 'entries', 'complete', 'notes',
                 'logical_block_size')

    def __init__(self, path, namespace, logical_block_size=2048):
        # type: (Optional[str], str, int) -> None
        self.path = path
        self.namespace = namespace
        self.entries = {}  # type: Dict[str, Entry]
        self.complete = True
        self.notes = []  # type: List[str]
        self.logical_block_size = logical_block_size

    def files(self):
        # type: () -> Iterable[Entry]
        """Yield the non-directory entries."""
        for entry in self.entries.values():
            if entry.kind != _DIR:
                yield entry

    def directory_signature(self, directory, include_digests=True):
        # type: (Entry, bool) -> Tuple[Any, ...]
        """Return a signature of every entry below a directory.

        With include_digests=True the signature proves that the whole subtree
        is byte-identical.  With it False the signature is the directory
        evidence alone: the same relative names, kinds, and tree shape, which
        stays valid when file contents were updated in place.
        """
        prefix = directory.path.rstrip('/') + '/'
        signature = []
        for path, entry in self.entries.items():
            if path != directory.path and path.startswith(prefix):
                if include_digests:
                    signature.append((path[len(prefix):], entry.kind, entry.digest))
                else:
                    signature.append((path[len(prefix):], entry.kind))
        signature.sort()
        return tuple(signature)

    def sharing_edges(self):
        # type: () -> Set[Tuple[str, str]]
        """Return unordered path pairs that share at least one data block.

        Sharing is evaluated per logical block, not per byte: a UDF file may
        start at an offset inside a block, and two such files can share that
        block without their byte ranges overlapping.  Adjacent blocks are not
        shared.
        """
        block_size = self.logical_block_size
        intervals = []
        for entry in self.entries.values():
            if entry.kind != _FILE:
                continue
            for (offset, length) in entry.extents:
                if length > 0:
                    first = offset // block_size
                    last = (offset + length - 1) // block_size
                    intervals.append((first, last + 1, entry.path))
        intervals.sort()

        edges = set()  # type: Set[Tuple[str, str]]
        active = []  # type: List[Tuple[int, str]]
        for (start, end, path) in intervals:
            # An earlier block range is still active when it ends *after* the
            # current one starts; a range ending exactly at the start covers
            # the previous block, so it is not shared.
            active = [item for item in active if item[0] > start]
            for (_active_end, other) in active:
                if other != path:
                    edges.add((other, path) if other < path else (path, other))
            active.append((end, path))
        return edges

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of this snapshot."""
        return {'path': self.path,
                'namespace': self.namespace,
                'complete': self.complete,
                'notes': list(self.notes),
                'entries': [self.entries[path].to_dict()
                            for path in sorted(self.entries)]}


def _rr_px_record(rec):
    # type: (Any) -> Any
    """Return the Rock Ridge PX record, whether it is in the DR or the CE."""
    rr = rec.rock_ridge
    if rr is None:
        return None
    px = rr.dr_entries.px_record
    if px is None and rr.ce_entries is not None:
        px = rr.ce_entries.px_record
    return px


def _rr_tf_record(rec):
    # type: (Any) -> Any
    """Return the Rock Ridge TF record, whether it is in the DR or the CE."""
    rr = rec.rock_ridge
    if rr is None:
        return None
    tf = rr.dr_entries.tf_record
    if tf is None and rr.ce_entries is not None:
        tf = rr.ce_entries.tf_record
    return tf


def _attributes(rec):
    # type: (Any) -> Dict[str, Any]
    """Collect the directory-record attributes of one entry.

    Only attributes that carry across a namespace are named consistently;
    UDF and ISO9660/Rock Ridge each keep their own key names because their
    permission and timestamp encodings are not interchangeable.
    """
    attributes = {}  # type: Dict[str, Any]

    if isinstance(rec, udfmod.UDFFileEntry):
        attributes['udf_perms'] = rec.perms
        attributes['udf_uid'] = rec.uid
        attributes['udf_gid'] = rec.gid
        attributes['udf_link_count'] = rec.file_link_count
        for (key, field_name) in (('udf_atime', 'access_time'),
                                  ('udf_mtime', 'mod_time'),
                                  ('udf_ctime', 'attr_time'),
                                  ('udf_btime', 'creation_time')):
            value = getattr(rec, field_name, None)
            if value is not None:
                attributes[key] = _date_string(value)
    else:
        attributes['iso_flags'] = rec.file_flags
        attributes['iso_date'] = _date_string(rec.date)
        if rec.rock_ridge is not None:
            try:
                attributes['rr_mode'] = rec.rock_ridge.get_file_mode()
            except pycdlibexception.PyCdlibException:
                pass
            px = _rr_px_record(rec)
            if px is not None:
                attributes['rr_uid'] = px.posix_user_id
                attributes['rr_gid'] = px.posix_group_id
                attributes['rr_link_count'] = px.posix_file_links
            tf = _rr_tf_record(rec)
            if tf is not None:
                for (key, field_name) in (('rr_atime', 'access_time'),
                                          ('rr_mtime', 'modification_time'),
                                          ('rr_ctime', 'attribute_change_time'),
                                          ('rr_btime', 'backup_time'),
                                          ('rr_creation_time', 'creation_time')):
                    value = getattr(tf, field_name, None)
                    if value is not None:
                        attributes[key] = _date_string(value)
            if rec.rock_ridge.is_symlink():
                attributes['symlink_target'] = rec.rock_ridge.symlink_path().decode('utf-8', 'replace')

    return attributes


def _entry_kind(rec):
    # type: (Any) -> str
    """Classify a directory record without relying on namespace quirks."""
    if rec.is_symlink():
        return _SYMLINK
    if rec.is_dir():
        return _DIR
    return _FILE


def _digest_file(iso, namespace, path, rec):
    # type: (pycdlib.PyCdlib, str, str, Any) -> Tuple[Optional[str], int, bool, Optional[str]]
    """Hash a file's content through the public read API.

    Returns (digest, size, readable, error).  A short read produces a None
    digest instead of a digest of a prefix, because a prefix digest would make
    two different partial reads look like equal content.
    """
    digest = hashlib.sha256()
    total = 0
    try:
        expected = rec.get_data_length()
        with iso.open_file_from_iso(**{_PATH_KEY[namespace]: path}) as infp:
            while True:
                chunk = infp.read(_DIGEST_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except Exception as exc:  # pylint: disable=broad-except
        return None, total, False, str(exc)

    if total != expected:
        return None, total, False, 'expected %d bytes but read %d' % (expected, total)
    return digest.hexdigest(), total, True, None


def _build_entry(iso, namespace, path, rec):
    # type: (pycdlib.PyCdlib, str, str, Any) -> Entry
    """Build one Entry, including its digest, extents, and attributes."""
    kind = _entry_kind(rec)
    entry = Entry(path, namespace, kind)
    entry.attributes = _attributes(rec)

    if kind == _DIR:
        return entry

    if kind == _SYMLINK:
        if namespace == 'udf':
            # UDF stores a symlink target as file data with file type 12;
            # the public file-open and extent APIs only accept regular files,
            # so read the inode and its allocation descriptors directly.
            if rec.inode is not None:
                with inode.InodeOpenData(rec.inode, iso.logical_block_size) as (infp, length):
                    data = infp.read(length)
                entry.size = len(data)
                entry.digest = hashlib.sha256(b'symlink\0' + data).hexdigest()
                part_start = iso.udf_main_descs.partitions[0].part_start_location
                entry.extents = tuple(((part_start + desc.log_block_num) * iso.logical_block_size + desc.offset,
                                       desc.extent_length)
                                      for desc in rec.alloc_descs)
        else:
            target = rec.rock_ridge.symlink_path()
            entry.size = len(target)
            entry.digest = hashlib.sha256(b'symlink\0' + target).hexdigest()
        return entry

    (entry.digest, entry.size, entry.readable,
     entry.read_error) = _digest_file(iso, namespace, path, rec)
    try:
        entry.extents = tuple(iso.get_file_byte_extents(**{_PATH_KEY[namespace]: path}))
    except Exception:  # pylint: disable=broad-except
        # A missing extent list only disables sharing analysis for this entry;
        # it must not make an otherwise readable file look changed.
        entry.extents = ()
    return entry


def build_snapshot(iso, namespace='rr', path=None):
    # type: (pycdlib.PyCdlib, str, Optional[str]) -> Snapshot
    """Walk one open ISO and build a content snapshot in one namespace.

    Args:
        iso: An opened :class:`pycdlib.pycdlib.PyCdlib` object.
        namespace: One of 'rr', 'udf', 'joliet', or 'iso'.
        path: Optional source path used only for reporting.
    Returns:
        A Snapshot of every entry reachable from the namespace root.
    """
    if namespace not in _NAMESPACES:
        raise pycdlibexception.PyCdlibInvalidInput("Unknown namespace '%s'" % (namespace))
    if not _namespace_available(iso, namespace):
        raise pycdlibexception.PyCdlibInvalidInput("ISO does not contain the '%s' namespace" % (namespace))

    snapshot = Snapshot(path, namespace, iso.logical_block_size)
    for (root, dirnames, filenames) in iso.walk(**{_PATH_KEY[namespace]: '/'}):
        names = list(dirnames) + list(filenames)
        if root == '/':
            child_paths = ['/' + name for name in names]
        else:
            child_paths = [posixpath.join(root, name) for name in names]

        for child_path in child_paths:
            if child_path in snapshot.entries:
                continue
            try:
                rec = iso.get_record(**{_PATH_KEY[namespace]: child_path})
            except pycdlibexception.PyCdlibException as exc:
                snapshot.complete = False
                snapshot.notes.append('could not read directory record %s: %s' % (child_path, exc))
                continue
            entry = _build_entry(iso, namespace, child_path, rec)
            snapshot.entries[child_path] = entry

    # The root itself carries attributes (mode, ownership, timestamps), so it
    # participates in attribute-only comparisons even though it has no name.
    try:
        root_rec = iso.get_record(**{_PATH_KEY[namespace]: '/'})
        snapshot.entries['/'] = _build_entry(iso, namespace, '/', root_rec)
    except pycdlibexception.PyCdlibException as exc:
        snapshot.complete = False
        snapshot.notes.append('could not read the root directory record: %s' % (exc))

    unreadable = [entry.path for entry in snapshot.files() if not entry.readable]
    if unreadable:
        snapshot.complete = False
        snapshot.notes.append('could not read the complete contents of %d file(s)' % (len(unreadable)))
    return snapshot


class Match:
    """One old/new entry pair and the evidence that tied it together."""
    __slots__ = ('old', 'new', 'evidence')

    def __init__(self, old, new, evidence):
        # type: (Entry, Entry, str) -> None
        self.old = old
        self.new = new
        self.evidence = evidence

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of this match."""
        return {'old_path': self.old.path, 'new_path': self.new.path,
                'kind': self.new.kind, 'evidence': self.evidence}


class ContentUpdate:
    """An entry whose identity is proven but whose data digest changed."""
    __slots__ = ('old', 'new', 'evidence')

    def __init__(self, old, new, evidence):
        # type: (Entry, Entry, str) -> None
        self.old = old
        self.new = new
        self.evidence = evidence

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of this update."""
        return {'old_path': self.old.path, 'new_path': self.new.path,
                'kind': self.new.kind, 'old_size': self.old.size,
                'new_size': self.new.size, 'old_digest': self.old.digest,
                'new_digest': self.new.digest,
                'path_changed': self.old.path != self.new.path,
                'evidence': self.evidence}


class PathMigration:
    """An entry whose content is unchanged but whose path moved."""
    __slots__ = ('old', 'new', 'evidence')

    def __init__(self, old, new, evidence):
        # type: (Entry, Entry, str) -> None
        self.old = old
        self.new = new
        self.evidence = evidence

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of this migration."""
        return {'old_path': self.old.path, 'new_path': self.new.path,
                'kind': self.new.kind, 'size': self.new.size,
                'digest': self.new.digest, 'evidence': self.evidence}


class AttributeChange:
    """A matched entry whose data and path are unchanged but metadata moved.

    Most reported entries are directories, but a file whose mode or ownership
    changed without its contents changing lands here too.
    """
    __slots__ = ('entry', 'changes')

    def __init__(self, entry, changes):
        # type: (Match, Dict[str, Tuple[Any, Any]]) -> None
        self.entry = entry
        self.changes = changes

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of this change."""
        return {'path': self.entry.new.path,
                'kind': self.entry.new.kind,
                'changes': {key: {'old': value[0], 'new': value[1]}
                            for (key, value) in sorted(self.changes.items())}}


class SharingChange:
    """Two files that used to share data blocks and now do not, or vice versa."""
    __slots__ = ('change', 'old_paths', 'new_paths', 'old_extents', 'new_extents')

    def __init__(self, change, old_paths, new_paths, old_extents, new_extents):
        # type: (str, Tuple[str, ...], Tuple[str, ...], Any, Any) -> None
        self.change = change
        self.old_paths = old_paths
        self.new_paths = new_paths
        self.old_extents = old_extents
        self.new_extents = new_extents

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of this change."""
        return {'change': self.change,
                'old_paths': list(self.old_paths),
                'new_paths': list(self.new_paths),
                'old_extents': _render_extents(self.old_extents),
                'new_extents': _render_extents(self.new_extents)}


def _render_extents(extents):
    # type: (Any) -> List[Any]
    """Render per-path extent tuples as JSON-friendly lists."""
    rendered = []
    for per_path in extents:
        if per_path is None:
            rendered.append(None)
        else:
            rendered.append([[offset, length] for (offset, length) in per_path])
    return rendered


class IsoDiff:
    """The result of comparing two snapshots."""
    __slots__ = ('old', 'new', 'matches', 'content_updates', 'path_migrations',
                 'attribute_only_changes', 'shared_block_changes', 'added',
                 'removed', 'unverified', 'ambiguous', 'notes')

    def __init__(self, old, new):
        # type: (Optional[Snapshot], Snapshot) -> None
        self.old = old
        self.new = new
        self.matches = []  # type: List[Match]
        self.content_updates = []  # type: List[ContentUpdate]
        self.path_migrations = []  # type: List[PathMigration]
        self.attribute_only_changes = []  # type: List[AttributeChange]
        self.shared_block_changes = []  # type: List[SharingChange]
        self.added = []  # type: List[Entry]
        self.removed = []  # type: List[Entry]
        self.unverified = []  # type: List[Tuple[str, Entry]]
        self.ambiguous = []  # type: List[Tuple[str, str, Optional[str]]]
        self.notes = []  # type: List[str]

    @property
    def deletion_verified(self):
        # type: () -> bool
        """Whether the diff had enough evidence to claim deletions."""
        return self.old is not None and self.old.complete and self.new.complete

    def has_differences(self):
        # type: () -> bool
        """Return whether any reported category contains a change."""
        return bool(self.content_updates or self.path_migrations or
                    self.attribute_only_changes or self.shared_block_changes or
                    self.added or self.removed)

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Return a JSON-friendly representation of the whole diff."""
        old_info = None
        if self.old is not None:
            old_info = {'path': self.old.path, 'namespace': self.old.namespace,
                        'complete': self.old.complete, 'notes': list(self.old.notes)}
        return {'old': old_info,
                'new': {'path': self.new.path, 'namespace': self.new.namespace,
                        'complete': self.new.complete, 'notes': list(self.new.notes)},
                'namespace': self.new.namespace,
                'deletion_verified': self.deletion_verified,
                'content_updates': [change.to_dict() for change in self.content_updates],
                'path_migrations': [change.to_dict() for change in self.path_migrations],
                'attribute_only_changes': [change.to_dict() for change in self.attribute_only_changes],
                'shared_block_changes': [change.to_dict() for change in self.shared_block_changes],
                'added': [entry.to_dict() for entry in self.added],
                'removed': [entry.to_dict() for entry in self.removed],
                'unverified': [{'side': side, 'path': entry.path,
                                'kind': entry.kind, 'digest': entry.digest}
                               for (side, entry) in self.unverified],
                'ambiguous': [{'side': side, 'path': path, 'digest': digest}
                              for (side, path, digest) in self.ambiguous],
                'notes': list(self.notes)}


def _pair_directory_groups(old_dirs, new_dirs, evidence, matches,
                           matched_old, matched_new):
    # type: (Dict[Any, List[Entry]], Dict[Any, List[Entry]], str, List[Match], Set[str], Set[str]) -> None
    """Pair unique directory groups with equal signatures."""
    for signature, old_group in old_dirs.items():
        new_group = new_dirs.get(signature)
        if new_group is None or len(old_group) != 1 or len(new_group) != 1:
            continue
        old_entry = old_group[0]
        new_entry = new_group[0]
        if old_entry.path == new_entry.path or old_entry.path in matched_old or new_entry.path in matched_new:
            continue
        matches.append(Match(old_entry, new_entry, evidence))
        matched_old.add(old_entry.path)
        matched_new.add(new_entry.path)


def _match_directories(old, new, matches, matched_old, matched_new):
    # type: (Snapshot, Snapshot, List[Match], Set[str], Set[str]) -> None
    """Pair leftover directories by their subtree evidence.

    Two passes, strongest evidence first.  A directory whose entire subtree is
    byte-identical matches on its content signature.  A directory whose
    relative names and kinds are unchanged but whose files were rewritten
    matches on its structural signature; the differing child digests then
    surface as content updates instead of making the whole directory look
    added plus removed.
    """
    for (include_digests, evidence) in ((True, 'directory-content-signature'),
                                        (False, 'directory-structure-signature')):
        old_dirs = {}  # type: Dict[Any, List[Entry]]
        new_dirs = {}  # type: Dict[Any, List[Entry]]
        for entry in old.entries.values():
            if entry.kind != _DIR or entry.path in matched_old:
                continue
            signature = old.directory_signature(entry, include_digests)
            if signature:
                old_dirs.setdefault(signature, []).append(entry)
        for entry in new.entries.values():
            if entry.kind != _DIR or entry.path in matched_new:
                continue
            signature = new.directory_signature(entry, include_digests)
            if signature:
                new_dirs.setdefault(signature, []).append(entry)
        _pair_directory_groups(old_dirs, new_dirs, evidence, matches,
                               matched_old, matched_new)


def _candidate_score(old_entry, new_entry, parent_map):
    # type: (Entry, Entry, Dict[str, str]) -> int
    """Score a digest-equal candidate pair using directory evidence."""
    score = 0
    if posixpath.basename(old_entry.path) == posixpath.basename(new_entry.path):
        score += 4
    old_parent = posixpath.dirname(old_entry.path) or '/'
    new_parent = posixpath.dirname(new_entry.path) or '/'
    if parent_map.get(old_parent) == new_parent:
        score += 2
    if posixpath.basename(old_parent) == posixpath.basename(new_parent):
        score += 1
    return score


def _pair_unique(left, right, score_func):
    # type: (List[Entry], List[Entry], Any) -> List[Tuple[Entry, Entry]]
    """Greedily pair candidates only when the choice is unambiguous.

    A pair is accepted only when it is the best partner for both entries and
    no other candidate ties that best score.  Everything else is deliberately
    left unpaired so the caller can report it as ambiguous instead of
    inventing an identity.
    """
    scored = []
    for old_entry in left:
        for new_entry in right:
            score = score_func(old_entry, new_entry)
            if score > 0:
                scored.append((score, old_entry, new_entry))

    pairs = []
    paired_old = set()
    paired_new = set()
    while scored:
        scored.sort(key=lambda item: (-item[0], item[1].path, item[2].path))
        (score, old_entry, new_entry) = scored[0]
        old_ties = [item for item in scored
                    if item[1] is old_entry and item[0] == score]
        new_ties = [item for item in scored
                    if item[2] is new_entry and item[0] == score]
        if len(old_ties) == 1 and len(new_ties) == 1:
            pairs.append((old_entry, new_entry))
            paired_old.add(old_entry.path)
            paired_new.add(new_entry.path)
            scored = [item for item in scored
                      if item[1].path not in paired_old and item[2].path not in paired_new]
        else:
            # Drop this old entry's candidates; it stays unmatched, and its
            # equally-good partners remain available to other entries.
            scored = [item for item in scored if item[1] is not old_entry]
    return pairs


def _match_digests(old, new, matches, matched_old, matched_new, diff):
    # type: (Snapshot, Snapshot, List[Match], Set[str], Set[str], IsoDiff) -> None
    """Pair leftover files and symlinks that carry the same content digest."""
    old_by_digest = {}  # type: Dict[str, List[Entry]]
    new_by_digest = {}  # type: Dict[str, List[Entry]]
    for entry in old.entries.values():
        # An empty file has no content to prove identity with; every empty
        # file shares the same digest, so using it would only create noise.
        if entry.path in matched_old or entry.kind == _DIR or \
           entry.digest is None or entry.size == 0:
            continue
        old_by_digest.setdefault(entry.digest, []).append(entry)
    for entry in new.entries.values():
        if entry.path in matched_new or entry.kind == _DIR or \
           entry.digest is None or entry.size == 0:
            continue
        new_by_digest.setdefault(entry.digest, []).append(entry)

    parent_map = {}  # type: Dict[str, str]
    for match in matches:
        if match.old.kind == _DIR and match.new.kind == _DIR:
            parent_map[match.old.path] = match.new.path

    for digest in sorted(old_by_digest):
        old_group = old_by_digest[digest]
        new_group = new_by_digest.get(digest)
        if not new_group:
            continue
        if len(old_group) == 1 and len(new_group) == 1:
            old_entry = old_group[0]
            new_entry = new_group[0]
            evidence = 'digest'
            if posixpath.basename(old_entry.path) == posixpath.basename(new_entry.path):
                evidence = 'digest+basename'
            match = Match(old_entry, new_entry, evidence)
            matches.append(match)
            matched_old.add(old_entry.path)
            matched_new.add(new_entry.path)
            continue

        pairs = _pair_unique(old_group, new_group,
                             lambda old_entry, new_entry: _candidate_score(old_entry, new_entry, parent_map))
        for (old_entry, new_entry) in pairs:
            match = Match(old_entry, new_entry, 'digest+directory-evidence')
            matches.append(match)
            matched_old.add(old_entry.path)
            matched_new.add(new_entry.path)

        for old_entry in old_group:
            if old_entry.path not in matched_old:
                diff.ambiguous.append(('old', old_entry.path, digest))
        for new_entry in new_group:
            if new_entry.path not in matched_new:
                diff.ambiguous.append(('new', new_entry.path, digest))


_TIMESTAMP_KEYS = frozenset(('iso_date', 'rr_atime', 'rr_mtime', 'rr_ctime',
                             'rr_btime', 'rr_creation_time', 'udf_atime',
                             'udf_mtime', 'udf_ctime', 'udf_btime'))


def _attribute_changes(match, include_timestamps=False):
    # type: (Match, bool) -> Dict[str, Tuple[Any, Any]]
    """Return the attribute keys whose values differ for a matched pair.

    Timestamps are ignored by default: re-mastering an unchanged tree with
    pycdlib stamps fresh times into every directory record, so including them
    would turn a pure layout change into an attribute change everywhere.
    """
    changes = {}
    for key in sorted(set(match.old.attributes) | set(match.new.attributes)):
        if not include_timestamps and key in _TIMESTAMP_KEYS:
            continue
        old_value = match.old.attributes.get(key)
        new_value = match.new.attributes.get(key)
        if old_value != new_value:
            changes[key] = (old_value, new_value)
    return changes


def _classify_matches(diff, matches, include_timestamps=False):
    # type: (IsoDiff, List[Match], bool) -> None
    """Split matches into content updates, migrations, and attribute changes."""
    for match in matches:
        if match.old.kind == _DIR:
            # Directories carry no file data.  A directory that kept its path
            # and only changed metadata is an attribute-only change; one that
            # moved while its subtree signature stayed equal is a migration.
            if match.old.path == match.new.path:
                changes = _attribute_changes(match, include_timestamps)
                if changes:
                    diff.attribute_only_changes.append(AttributeChange(match, changes))
            elif match.evidence != 'path':
                diff.path_migrations.append(PathMigration(match.old, match.new, match.evidence))
            continue

        if match.old.path == match.new.path:
            if match.old.digest is not None and match.new.digest is not None and \
               match.old.digest != match.new.digest:
                diff.content_updates.append(ContentUpdate(match.old, match.new, match.evidence))
            elif match.old.digest is None or match.new.digest is None:
                # A digest is missing because one side could not be read
                # completely.  Do not claim a content update, and do not
                # claim the entry is unchanged.
                diff.unverified.append(('both', match.new))
            else:
                changes = _attribute_changes(match, include_timestamps)
                if changes:
                    diff.attribute_only_changes.append(AttributeChange(match, changes))
        else:
            if match.old.digest is not None and match.old.digest == match.new.digest:
                diff.path_migrations.append(PathMigration(match.old, match.new, match.evidence))
            elif match.evidence.endswith('digest-differs'):
                # Directory evidence proved the entry is the same one, and the
                # digests prove its bytes changed, so the move and the rewrite
                # are one content update rather than a migration plus a new
                # file.
                diff.content_updates.append(ContentUpdate(match.old, match.new, match.evidence))
            else:
                changes = _attribute_changes(match, include_timestamps)
                if changes:
                    diff.attribute_only_changes.append(AttributeChange(match, changes))


def _match_directory_evidence(old, new, matches, matched_old, matched_new):
    # type: (Snapshot, Snapshot, List[Match], Set[str], Set[str]) -> None
    """Pair leftover entries that keep their name under a matched directory.

    This is the case that neither a path nor a digest can settle on its own:
    a file moved to a renamed directory *and* had its content rewritten.  The
    parent directory match is the explicit directory evidence; the differing
    digests then make the pair a content update rather than a migration.
    Only unique candidates are paired, so a reused name is not guessed at.
    """
    parent_map = {'/': '/'}  # type: Dict[str, str]
    for match in matches:
        if match.old.kind == _DIR and match.new.kind == _DIR:
            parent_map[match.old.path] = match.new.path

    old_groups = {}  # type: Dict[Tuple[str, str], List[Entry]]
    new_groups = {}  # type: Dict[Tuple[str, str], List[Entry]]
    for entry in old.entries.values():
        if entry.path in matched_old or entry.kind == _DIR:
            continue
        parent = posixpath.dirname(entry.path) or '/'
        if parent not in parent_map:
            continue
        key = (parent_map[parent], posixpath.basename(entry.path))
        old_groups.setdefault(key, []).append(entry)
    for entry in new.entries.values():
        if entry.path in matched_new or entry.kind == _DIR:
            continue
        parent = posixpath.dirname(entry.path) or '/'
        key = (parent, posixpath.basename(entry.path))
        new_groups.setdefault(key, []).append(entry)

    for key, old_group in old_groups.items():
        new_group = new_groups.get(key)
        if new_group is None or len(old_group) != 1 or len(new_group) != 1:
            continue
        old_entry = old_group[0]
        new_entry = new_group[0]
        if old_entry.kind != new_entry.kind:
            continue
        if old_entry.readable and new_entry.readable and \
           old_entry.digest == new_entry.digest and old_entry.size > 0:
            # Equal content would already have been caught by the digest
            # phase; an equal digest here means the file was not moved.
            continue
        if old_entry.size == 0 and new_entry.size == 0:
            # Empty files have no content to compare, so a unique name under
            # a matched parent is the only identity evidence there is.
            evidence = 'directory-empty-file'
        else:
            evidence = 'directory+digest-differs'
            if old_entry.path == new_entry.path:
                evidence = 'path+digest-differs'
        matches.append(Match(old_entry, new_entry, evidence))
        matched_old.add(old_entry.path)
        matched_new.add(new_entry.path)


def _sharing_changes(diff, matches):
    # type: (IsoDiff, List[Match]) -> None
    """Compare data-block sharing relationships through the identity map."""
    old = diff.old
    new = diff.new
    if old is None:
        diff.notes.append('No old snapshot; shared-block relationships cannot be compared.')
        return

    old_edges = old.sharing_edges()
    new_edges = new.sharing_edges()

    mapping = {match.old.path: match.new.path for match in matches}
    reverse = {match.new.path: match.old.path for match in matches}

    old_extents = {entry.path: entry.extents for entry in old.entries.values()}
    new_extents = {entry.path: entry.extents for entry in new.entries.values()}

    for (old_a, old_b) in sorted(old_edges):
        if old_a not in mapping or old_b not in mapping:
            continue
        new_pair = (mapping[old_a], mapping[old_b])
        canonical = new_pair if new_pair[0] < new_pair[1] else (new_pair[1], new_pair[0])
        if canonical not in new_edges:
            diff.shared_block_changes.append(
                SharingChange('separated', (old_a, old_b), new_pair,
                              (old_extents[old_a], old_extents[old_b]),
                              (new_extents[mapping[old_a]], new_extents[mapping[old_b]])))

    for (new_a, new_b) in sorted(new_edges):
        if new_a not in reverse and new_b not in reverse:
            continue
        old_pair = (reverse.get(new_a), reverse.get(new_b))
        if old_pair[0] is not None and old_pair[1] is not None:
            canonical = old_pair if old_pair[0] < old_pair[1] else (old_pair[1], old_pair[0])
            if canonical in old_edges:
                continue
        diff.shared_block_changes.append(
            SharingChange('joined', (old_pair[0], old_pair[1]), (new_a, new_b),
                          (old_extents.get(old_pair[0]), old_extents.get(old_pair[1])),
                          (new_extents[new_a], new_extents[new_b])))


def _unmatched(diff, matches):
    # type: (IsoDiff, List[Match]) -> None
    """Place entries that no evidence paired into added/removed/unverified."""
    old = diff.old
    new = diff.new
    matched_old = {match.old.path for match in matches}
    matched_new = {match.new.path for match in matches}

    new_left = [entry for entry in new.entries.values() if entry.path not in matched_new]
    old_left = [entry for entry in old.entries.values() if entry.path not in matched_old] if old is not None else []

    new_left.sort(key=lambda entry: entry.path)
    old_left.sort(key=lambda entry: entry.path)

    if old is not None and diff.deletion_verified:
        diff.added.extend(new_left)
        diff.removed.extend(old_left)
        return

    if old is None:
        diff.notes.append('No old image was supplied; entries that are new cannot be '
                          'distinguished from entries that are unchanged, and no '
                          'deletion is inferred.')
        return

    if not old.complete:
        diff.notes.append('The old image is incomplete; entries that are missing on the new side are not reported as deletions.')
    if not new.complete:
        diff.notes.append('The new image is incomplete; entries that are missing on the old side are not reported as additions.')

    for entry in old_left:
        if entry.path != '/':
            diff.unverified.append(('old', entry))
    for entry in new_left:
        if entry.path != '/':
            diff.unverified.append(('new', entry))


def diff_snapshots(old, new, include_timestamps=False):
    # type: (Optional[Snapshot], Snapshot, bool) -> IsoDiff
    """Compare two snapshots that were built in the same namespace."""
    result = IsoDiff(old, new)

    if old is None:
        _unmatched(result, [])
        return result

    if old.namespace != new.namespace:
        raise pycdlibexception.PyCdlibInvalidInput(
            'Snapshots must use the same namespace (%s != %s)' % (old.namespace, new.namespace))

    matches = []  # type: List[Match]
    matched_old = set()  # type: Set[str]
    matched_new = set()  # type: Set[str]

    # Phase 1: explicit directory evidence.  A path that exists on both
    # sides identifies the same entry even if its bytes changed.
    for path, old_entry in old.entries.items():
        new_entry = new.entries.get(path)
        if new_entry is None or new_entry.kind != old_entry.kind:
            continue
        matches.append(Match(old_entry, new_entry, 'path'))
        matched_old.add(path)
        matched_new.add(path)

    # Phase 2: a renamed directory whose subtree is byte-identical, or whose
    # tree shape survived a rewrite, is still the same directory.  Matching it
    # gives phase 3 a parent map and keeps directory renames out of the
    # added/removed lists.
    _match_directories(old, new, matches, matched_old, matched_new)

    # Phase 3: content digests identify files that moved.
    _match_digests(old, new, matches, matched_old, matched_new, result)

    # Phase 4: explicit directory evidence pairs entries that both moved and
    # changed content.  This is intentionally last so a digest match (which
    # proves the content is unchanged) always wins.
    _match_directory_evidence(old, new, matches, matched_old, matched_new)

    result.matches = matches
    _classify_matches(result, matches, include_timestamps)
    _sharing_changes(result, matches)
    _unmatched(result, matches)

    return result


def diff_isos(old_iso, new_iso, namespace='auto', include_timestamps=False,
              old_path=None, new_path=None):
    # type: (Optional[pycdlib.PyCdlib], pycdlib.PyCdlib, str, bool, Optional[str], Optional[str]) -> IsoDiff
    """Compare two already-open ISO objects.

    Args:
        old_iso: The old ISO, or None when no complete old image is available.
        new_iso: The new ISO.
        namespace: 'auto', or one of 'rr', 'udf', 'joliet', 'iso'.
    Returns:
        An IsoDiff.  When the old image is absent or incomplete, the result
        explicitly refuses to infer deletions.
    """
    if namespace == 'auto':
        candidates = [name for name in _NAMESPACES
                      if _namespace_available(new_iso, name) and
                      (old_iso is None or _namespace_available(old_iso, name))]
        if not candidates:
            candidates = ['iso']
        namespace = candidates[0]

    old_snapshot = None
    if old_iso is not None:
        try:
            old_snapshot = build_snapshot(old_iso, namespace, path=old_path)
        except pycdlibexception.PyCdlibException as exc:
            # A structurally valid but unreadable old image is no better than
            # a missing one for deletion purposes.
            old_snapshot = None
            new_snapshot = build_snapshot(new_iso, namespace, path=new_path)
            result = diff_snapshots(None, new_snapshot, include_timestamps)
            result.notes.append('The old image could not be read completely and is treated as unavailable: %s' % (exc))
            result.notes.append('Deletions were not inferred because the old image is unavailable.')
            return result
    new_snapshot = build_snapshot(new_iso, namespace, path=new_path)
    return diff_snapshots(old_snapshot, new_snapshot, include_timestamps)


def diff_paths(old_path, new_path, namespace='auto', old_complete=None,
               include_timestamps=False):
    # type: (Optional[str], str, str, Optional[bool], bool) -> IsoDiff
    """Compare two ISO files on disk.

    Args:
        old_path: Path to the old ISO.  May be None when only the new image
            is available; deletions are then not inferred.
        new_path: Path to the new ISO.
        namespace: 'auto', or one of 'rr', 'udf', 'joliet', 'iso'.
        old_complete: Optional override for the old snapshot's completeness.
            Passing False forces the conservative no-deletion behavior even
            if every readable entry produced a digest.
        include_timestamps: When True, timestamp differences count as
            attribute changes.  Off by default, since re-mastering restamps
            every directory record even when nothing else changed.
    Returns:
        An IsoDiff.
    """
    # Imported here so that ``from pycdlib import diff`` does not require the
    # whole facade module to be imported first.
    import pycdlib  # pylint: disable=import-outside-toplevel

    old_iso = None
    old_fp = None
    new_iso = pycdlib.PyCdlib()
    new_fp = open(new_path, 'rb')  # pylint: disable=consider-using-with
    old_note = None
    try:
        new_iso.open_fp(new_fp)
        if old_path is not None:
            old_fp = open(old_path, 'rb')  # pylint: disable=consider-using-with
            old_iso = pycdlib.PyCdlib()
            try:
                old_iso.open_fp(old_fp)
            except pycdlibexception.PyCdlibException as exc:
                # A truncated, unparseable, or otherwise incomplete old image
                # cannot prove that an entry was deleted.  Treat it as
                # unavailable instead of failing the whole comparison.
                old_iso = None
                old_fp.close()
                old_fp = None
                old_note = 'The old image could not be parsed and is treated as unavailable: %s' % (exc)

        result = diff_isos(old_iso, new_iso, namespace, include_timestamps,
                           old_path=old_path, new_path=new_path)
        if old_note is not None:
            result.notes.append(old_note)
            result.notes.append('Deletions were not inferred because the old image is unavailable.')
        if old_complete is False and result.old is not None:
            result.old.complete = False
            result.notes.append('The old image was declared incomplete by the caller; no deletion is inferred.')
            result.unverified.extend(('old', entry) for entry in result.removed)
            result.removed = []
        return result
    finally:
        if old_iso is not None:
            old_iso.close()
        if old_fp is not None:
            old_fp.close()
        new_iso.close()
        new_fp.close()


def _format_value(key, value):
    # type: (str, Any) -> str
    """Format one attribute value for the text report."""
    if value is None:
        return '-'
    if key in ('rr_mode', 'mode') and isinstance(value, int):
        return '0o%o' % (value)
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def format_text(result):
    # type: (IsoDiff) -> str
    """Render a diff as a human-readable report."""
    lines = []  # type: List[str]
    old_desc = 'none'
    if result.old is not None:
        old_desc = result.old.path or '<open ISO>'
        if not result.old.complete:
            old_desc += ' (incomplete)'
    new_desc = result.new.path or '<open ISO>'
    if not result.new.complete:
        new_desc += ' (incomplete)'
    lines.append('ISO content diff')
    lines.append('  old: %s' % (old_desc))
    lines.append('  new: %s' % (new_desc))
    lines.append('  namespace: %s' % (result.new.namespace))
    lines.append('')

    lines.append('Content updates (%d):' % (len(result.content_updates)))
    for change in result.content_updates:
        lines.append('  %s -> %s  %s -> %s  (%d -> %d bytes)  [%s]' % (
            change.old.path, change.new.path,
            change.old.digest or 'unreadable', change.new.digest or 'unreadable',
            change.old.size, change.new.size, change.evidence))
    lines.append('')

    lines.append('Path migrations (%d):' % (len(result.path_migrations)))
    for change in result.path_migrations:
        if change.new.kind == _DIR:
            detail = 'directory'
        else:
            detail = 'sha256:%s  (%d bytes)' % (change.new.digest, change.new.size)
        lines.append('  %s -> %s  %s  [%s]' % (
            change.old.path, change.new.path, detail, change.evidence))
    lines.append('')

    lines.append('Attribute-only changes (%d):' % (len(result.attribute_only_changes)))
    for change in result.attribute_only_changes:
        rendered = '; '.join('%s: %s -> %s' % (key,
                                               _format_value(key, values[0]),
                                               _format_value(key, values[1]))
                             for (key, values) in sorted(change.changes.items()))
        lines.append('  %s  %s' % (change.entry.new.path, rendered))
    lines.append('')

    lines.append('Shared-block changes (%d):' % (len(result.shared_block_changes)))
    for change in result.shared_block_changes:
        old_pair = ' <-> '.join(path or '<new>' for path in change.old_paths)
        new_pair = ' <-> '.join(change.new_paths)
        lines.append('  %-9s %s -> %s' % (change.change, old_pair or '-', new_pair))
    lines.append('')

    if result.deletion_verified:
        lines.append('Added (%d):' % (len(result.added)))
        for entry in result.added:
            lines.append('  %s' % (entry.path))
        lines.append('Removed (%d):' % (len(result.removed)))
        for entry in result.removed:
            lines.append('  %s' % (entry.path))
    else:
        lines.append('Added/removed: not determined (deletions cannot be inferred without a complete old image)')

    if result.unverified:
        lines.append('')
        lines.append('Unverified (%d):' % (len(result.unverified)))
        for (side, entry) in result.unverified:
            lines.append('  %-3s %s' % (side, entry.path))
    if result.ambiguous:
        lines.append('')
        lines.append('Ambiguous identity (%d):' % (len(result.ambiguous)))
        for (side, path, digest) in result.ambiguous:
            lines.append('  %-3s %s  sha256:%s' % (side, path, digest or '-'))
    if result.notes or (result.old is not None and result.old.notes) or result.new.notes:
        lines.append('')
        lines.append('Notes:')
        for note in result.notes:
            lines.append('  - %s' % (note))
        if result.old is not None:
            for note in result.old.notes:
                lines.append('  - old: %s' % (note))
        for note in result.new.notes:
            lines.append('  - new: %s' % (note))
    return '\n'.join(lines) + '\n'
