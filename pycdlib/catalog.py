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
Catalog support for multi-namespace ISO9660 volumes.

An ISO9660 volume can expose the same on-disk data through up to three
independent name spaces:

* the base ISO9660 name space rooted at the Primary Volume Descriptor;
* the Joliet name space, a Supplementary Volume Descriptor whose names are
  encoded in UCS-2;
* the Rock Ridge name space, POSIX names carried in the system-use area of
  the ISO9660 directory records.

This module produces catalogs from an opened :class:`pycdlib.PyCdlib` object:

* a *file entity* catalog, with one row per distinct on-disk data object.
  Hard links (several directory records that point at one data area) collapse
  to a single entity, so the shared data is never counted twice;
* a *path reference* catalog, with one row per directory record in each name
  space, preserving the name exactly as that name space stored it.

Name spaces are deliberately never matched by display name.  A Joliet path
and an ISO9660 path only reference the same entity when their directory
records point at the same parsed object (an :class:`pycdlib.inode.Inode` for
files, or a directory extent for directories).  When several distinct
entities share one data area, the conflicting directory records are reported
in a conflict catalog instead of guessing a pairing from their names.
"""

import collections

from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional, Set, Tuple

from pycdlib import dr
from pycdlib import pycdlibexception
from pycdlib.pycdlib import _yield_children

if TYPE_CHECKING:
    from pycdlib import pycdlib  # noqa: F401  pylint: disable=cyclic-import

ISO_NAMESPACE = 'iso9660'
ROCK_RIDGE_NAMESPACE = 'rockridge'
JOLIET_NAMESPACE = 'joliet'

KIND_FILE = 'file'
KIND_DIRECTORY = 'directory'
KIND_SYMLINK = 'symlink'

ENTITY_FILE_PREFIX = 'F'
ENTITY_DIRECTORY_PREFIX = 'D'
ENTITY_SYMLINK_PREFIX = 'S'


class FileEntity:
    """A single distinct on-disk data object.

    Several directory records (hard links, possibly in different name spaces)
    can reference one FileEntity, but the data area is cataloged once.
    """
    __slots__ = ('entity_id', 'kind', 'extent_location', 'data_length',
                 'references')

    def __init__(self, entity_id, kind, extent_location, data_length):
        # type: (str, str, int, int) -> None
        self.entity_id = entity_id
        self.kind = kind
        self.extent_location = extent_location
        self.data_length = data_length
        self.references = []  # type: List[PathReference]


class PathReference:
    """A single directory record in one name space.

    Each reference keeps the name exactly as its name space recorded it, even
    when another name space stores a different name for the same entity.
    """
    __slots__ = ('namespace', 'path', 'original_name', 'entry_type',
                 'entity_id', 'extent_location', 'data_length')

    def __init__(self, namespace, path, original_name, entry_type,
                 entity_id, extent_location, data_length):
        # type: (str, str, str, str, str, int, int) -> None
        self.namespace = namespace
        self.path = path
        self.original_name = original_name
        self.entry_type = entry_type
        self.entity_id = entity_id
        self.extent_location = extent_location
        self.data_length = data_length


class SharedExtentConflict:
    """A data area referenced by two or more distinct file entities.

    The member records are the directory records of every entity involved, so
    callers can inspect the actual on-disk references instead of relying on a
    name-based match.
    """
    __slots__ = ('extent_location', 'records')

    def __init__(self, extent_location, records):
        # type: (int, List[PathReference]) -> None
        self.extent_location = extent_location
        self.records = records


class Catalog:
    """The parsed entity, reference, and conflict catalogs for one ISO."""
    __slots__ = ('entities', 'references', 'conflicts')

    def __init__(self, entities, references, conflicts):
        # type: (List[FileEntity], List[PathReference], List[SharedExtentConflict]) -> None
        self.entities = entities
        self.references = references
        self.conflicts = conflicts


def _record_kind(rec):
    # type: (dr.DirectoryRecord) -> str
    """Classify a directory record as a symlink, directory, or file."""
    if rec.is_symlink():
        return KIND_SYMLINK
    if rec.is_dir():
        return KIND_DIRECTORY
    return KIND_FILE


def _record_original_name(rec, vd_encoding, rockridge):
    # type: (dr.DirectoryRecord, str, bool) -> str
    """Return the name exactly as the walked name space stores it."""
    if rec.is_root:
        return '/'
    if rockridge and rec.rock_ridge is not None:
        return rec.rock_ridge.name().decode('utf-8')
    return rec.file_identifier().decode(vd_encoding)


def _walk_namespace(vd, rockridge):
    # type: (Any, bool) -> Generator[Tuple[dr.DirectoryRecord, str, str], None, None]
    """Yield (record, absolute path, original name) for one name space.

    The walk follows the same child selection as PyCdlib.walk(), so Rock
    Ridge relocated directories are resolved for the Rock Ridge name space
    while remaining ordinary directory records in the ISO9660 name space.
    """
    root = vd.root_directory_record()
    yield root, '/', _record_original_name(root, vd.encoding, rockridge)

    queue = collections.deque([(root, '')])  # type: collections.deque[Tuple[dr.DirectoryRecord, str]]
    seen_dirs = set([id(root)])  # type: Set[int]
    while queue:
        parent, prefix = queue.popleft()
        for child in _yield_children(parent, rockridge):
            if child is None or child.is_dot() or child.is_dotdot():
                continue

            name = _record_original_name(child, vd.encoding, rockridge)
            path = prefix + '/' + name
            yield child, path, name

            if child.is_dir() and id(child) not in seen_dirs:
                seen_dirs.add(id(child))
                queue.append((child, path))


def _safe_extent_location(current_extent, original_extent):
    # type: (int, Optional[int]) -> int
    """Return an extent location, using -1 for not-yet-assigned extents."""
    if current_extent >= 0:
        return current_extent
    if original_extent is not None:
        return original_extent
    return -1


def _entity_key(rec):
    # type: (dr.DirectoryRecord) -> Tuple[Any, ...]
    """Identity key for the data object a directory record points at.

    Regular files (including hard links and symlinks) are keyed on the parsed
    Inode, which the ISO parser shares for every record that references the
    same data extent.  Directories have no Inode, so they are keyed on their
    directory extent within their volume descriptor; the ISO9660 and Joliet
    directory hierarchies always get distinct entities even when their names
    match.  Before an ISO is written, directory extents are unassigned, so the
    record object identity is used instead.  Records without an Inode (e.g.
    entries that are not part of the directory tree) likewise fall back to
    their own object identity.
    """
    if rec.is_dir():
        extent = _safe_extent_location(rec.new_extent_loc, rec.orig_extent_loc)
        if extent >= 0:
            return ('directory', id(rec.vd), extent)
        return ('directory-record', id(rec))
    if rec.inode is not None:
        return ('inode', id(rec.inode))
    return ('record', id(rec))


def _entity_extent_and_length(rec):
    # type: (dr.DirectoryRecord) -> Tuple[int, int]
    """Return the data extent location and declared length for a record.

    Extents on an ISO that has not been written yet may be unassigned; those
    are reported as -1 rather than raising from the underlying Inode.
    """
    if rec.inode is not None:
        ino = rec.inode
        extent = _safe_extent_location(ino.new_extent_loc, ino.orig_extent_loc)
        return extent, ino.get_data_length()
    extent = _safe_extent_location(rec.new_extent_loc, rec.orig_extent_loc)
    return extent, rec.get_data_length()


def _namespaces_for_entity(entity):
    # type: (FileEntity) -> str
    """Return the sorted, comma-joined name spaces referencing an entity."""
    return ','.join(sorted(set(ref.namespace for ref in entity.references)))


def build_catalog(iso):
    # type: (pycdlib.PyCdlib) -> Catalog
    """Build the entity and path-reference catalogs for an opened ISO.

    Args:
        iso: An initialized :class:`pycdlib.PyCdlib` object.
    Returns:
        A :class:`Catalog` holding the file entities, path references, and
        shared-extent conflicts.
    """
    if not iso._initialized:  # pylint: disable=protected-access
        raise pycdlibexception.PyCdlibInvalidInput('This object is not initialized; call either open() or new() to create an ISO')

    namespaces = [(iso.pvd, ISO_NAMESPACE, False)]  # type: List[Tuple[Any, str, bool]]
    if iso.has_rock_ridge():
        namespaces.append((iso.pvd, ROCK_RIDGE_NAMESPACE, True))
    if iso.has_joliet():
        namespaces.append((iso.joliet_vd, JOLIET_NAMESPACE, False))

    entities_by_key = {}  # type: Dict[Tuple[Any, ...], FileEntity]
    entities_in_order = []  # type: List[FileEntity]
    references = []  # type: List[PathReference]
    counters = {KIND_FILE: 0, KIND_DIRECTORY: 0, KIND_SYMLINK: 0}

    for vd, namespace, rockridge in namespaces:
        for rec, path, original_name in _walk_namespace(vd, rockridge):
            kind = _record_kind(rec)
            extent, data_length = _entity_extent_and_length(rec)
            key = _entity_key(rec)

            entity = entities_by_key.get(key)
            if entity is None:
                counters[kind] += 1
                if kind == KIND_DIRECTORY:
                    prefix = ENTITY_DIRECTORY_PREFIX
                elif kind == KIND_SYMLINK:
                    prefix = ENTITY_SYMLINK_PREFIX
                else:
                    prefix = ENTITY_FILE_PREFIX
                entity_id = '%s%06d' % (prefix, counters[kind])
                entity = FileEntity(entity_id, kind, extent, data_length)
                entities_by_key[key] = entity
                entities_in_order.append(entity)

            reference = PathReference(namespace, path, original_name, kind,
                                      entity.entity_id, extent, data_length)
            entity.references.append(reference)
            references.append(reference)

    conflicts = _find_shared_extent_conflicts(entities_in_order)

    return Catalog(entities_in_order, references, conflicts)


def _find_shared_extent_conflicts(entities):
    # type: (List[FileEntity]) -> List[SharedExtentConflict]
    """Find distinct entities that claim the same non-empty data extent.

    Hard links show up as one entity with several references and are not
    conflicts.  A conflict requires two or more distinct entities backed by
    the same extent, at which point every directory record involved is listed
    so the caller can decide what to do.  Zero-length objects have no data
    extent and are never considered conflicting.
    """
    extent_to_entities = {}  # type: Dict[int, Dict[str, FileEntity]]
    for entity in entities:
        if entity.extent_location <= 0 or entity.data_length == 0:
            continue
        bucket = extent_to_entities.setdefault(entity.extent_location, {})
        bucket[entity.entity_id] = entity

    conflicts = []  # type: List[SharedExtentConflict]
    for extent in sorted(extent_to_entities):
        distinct = extent_to_entities[extent]
        if len(distinct) < 2:
            continue

        records = []  # type: List[PathReference]
        for entity in distinct.values():
            records.extend(entity.references)
        records.sort(key=lambda ref: (ref.namespace, ref.path))
        conflicts.append(SharedExtentConflict(extent, records))

    return conflicts


def format_entity_catalog(catalog):
    # type: (Catalog) -> str
    """Render the file entity catalog as tab-separated text."""
    lines = ['entity_id\tkind\textent_location\tdata_length\treference_count\tnamespaces']
    for entity in catalog.entities:
        lines.append('%s\t%s\t%d\t%d\t%d\t%s' % (
            entity.entity_id, entity.kind, entity.extent_location,
            entity.data_length, len(entity.references),
            _namespaces_for_entity(entity)))
    return '\n'.join(lines) + '\n'


def format_reference_catalog(catalog):
    # type: (Catalog) -> str
    """Render the per-namespace path reference catalog as TSV text."""
    lines = ['namespace\tpath\toriginal_name\tentry_type\tentity_id\textent_location\tdata_length']
    for ref in catalog.references:
        lines.append('%s\t%s\t%s\t%s\t%s\t%d\t%d' % (
            ref.namespace, ref.path, ref.original_name, ref.entry_type,
            ref.entity_id, ref.extent_location, ref.data_length))
    return '\n'.join(lines) + '\n'


def format_conflict_catalog(catalog):
    # type: (Catalog) -> str
    """Render the shared-extent conflict catalog as TSV text."""
    lines = ['extent_location\tnamespace\tpath\toriginal_name\tentry_type\tentity_id']
    for conflict in catalog.conflicts:
        for ref in conflict.records:
            lines.append('%d\t%s\t%s\t%s\t%s\t%s' % (
                conflict.extent_location, ref.namespace, ref.path,
                ref.original_name, ref.entry_type, ref.entity_id))
    return '\n'.join(lines) + '\n'
