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
Salvage support for damaged ISOs.

When an ISO arrives with a bad-block (missing-sector) table, the metadata
(directory records, path tables, UDF descriptors) may still parse while the
data those entries point at is partly or wholly unreadable.  This module
cross-references each parsed entry's extents against the bad-block table and
classifies every entry as:

* ``complete``  - every logical block the entry occupies is readable; an
  entry without any data blocks (a zero-length file) is always complete.
* ``partial``   - a file (or symlink payload) whose extents include one or
  more bad blocks, so only the remaining blocks can be recovered.
* ``untrusted`` - a directory whose own extent includes a bad block; the
  listing of that directory cannot be trusted because entries stored in the
  bad block are missing from the parse.

Classification is purely block based, so two entries that share an extent
(hard links, ISO9660/UDF bridges) are both affected when the shared block is
bad.
"""

import collections
import os

from pycdlib import dr
from pycdlib import pycdlibexception
from pycdlib import udf as udfmod
from pycdlib import utils

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import FrozenSet, Iterable, List, Optional, Tuple, Union  # noqa: F401
    from pycdlib.pycdlib import PyCdlib  # noqa: F401

FILE_COMPLETE = 'complete'
FILE_PARTIAL = 'partial'
DIR_UNTRUSTED = 'untrusted'

VALID_STATUSES = (FILE_COMPLETE, FILE_PARTIAL, DIR_UNTRUSTED)


class EntryAssessment:
    """
    The salvage verdict for a single entry on the ISO.

    Attributes:
        path: Absolute path of the entry in the selected path convention.
        name: Raw on-disk file identifier as a byte string.
        entry_type: One of 'file', 'dir', 'symlink', or 'empty'.
        status: One of :data:`FILE_COMPLETE`, :data:`FILE_PARTIAL`, or
            :data:`DIR_UNTRUSTED`.
        data_blocks: Tuple of absolute logical block numbers the entry's
            data occupies.
        bad_blocks: Tuple of bad logical block numbers overlapping the
            entry, in ascending order.
        total_bytes: Data length the metadata claims for the entry.
        missing_bytes: Number of bytes of the claimed length that sit in
            bad blocks (a whole block except for the final partial block).
        under_untrusted_dir: True when an ancestor directory is itself
            untrusted, so this entry's presence in the parse is not
            guaranteed even if its own blocks are fine.
    """

    __slots__ = ('path', 'name', 'entry_type', 'status', 'data_blocks',
                 'bad_blocks', 'total_bytes', 'missing_bytes',
                 'under_untrusted_dir')

    def __init__(self, path, name, entry_type, status, data_blocks,
                 bad_blocks, total_bytes, missing_bytes,
                 under_untrusted_dir):
        # type: (str, bytes, str, str, Tuple[int, ...], Tuple[int, ...], int, int, bool) -> None
        self.path = path
        self.name = name
        self.entry_type = entry_type
        self.status = status
        self.data_blocks = data_blocks
        self.bad_blocks = bad_blocks
        self.total_bytes = total_bytes
        self.missing_bytes = missing_bytes
        self.under_untrusted_dir = under_untrusted_dir

    @property
    def readable_bytes(self):
        # type: () -> int
        """The number of claimed bytes that live on readable blocks."""
        return self.total_bytes - self.missing_bytes

    @property
    def extractable(self):
        # type: () -> bool
        """
        Whether the entry can be ingested as-is.  Entries below an untrusted
        directory are never considered safely extractable, since the
        directory parse may have lost siblings or truncated this entry's
        metadata.
        """
        return self.status == FILE_COMPLETE and not self.under_untrusted_dir

    def __repr__(self):
        # type: () -> str
        return 'EntryAssessment(path=%r, status=%r, bad_blocks=%r)' % (self.path, self.status, self.bad_blocks)


class SalvagePlan:
    """
    The full salvage verdict for an ISO.

    Attributes:
        path_type: The path convention the walk used ('iso_path',
            'rr_path', 'joliet_path', or 'udf_path').
        block_size: The ISO logical block size in bytes.
        assessments: EntryAssessment objects in walk order.
    """

    __slots__ = ('path_type', 'block_size', 'assessments')

    def __init__(self, path_type, block_size, assessments):
        # type: (str, int, List[EntryAssessment]) -> None
        self.path_type = path_type
        self.block_size = block_size
        self.assessments = assessments

    def by_path(self):
        # type: () -> dict
        """Return a mapping of absolute path to its assessment."""
        return dict((assessment.path, assessment) for assessment in self.assessments)

    def entries_with_status(self, status):
        # type: (str) -> List[EntryAssessment]
        """Return every assessment carrying the given status."""
        if status not in VALID_STATUSES:
            raise pycdlibexception.PyCdlibInvalidInput("Status must be one of %r" % (VALID_STATUSES,))
        return [assessment for assessment in self.assessments if assessment.status == status]

    @property
    def extractable(self):
        # type: () -> List[EntryAssessment]
        """Return the assessments safe to ingest, in walk order."""
        return [assessment for assessment in self.assessments if assessment.extractable]

    def summary(self):
        # type: () -> dict
        """Return counts of each status, keyed by the status constant."""
        counts = collections.OrderedDict()
        for status in VALID_STATUSES:
            counts[status] = 0
        for assessment in self.assessments:
            counts[assessment.status] += 1
        return counts


def parse_bad_blocks(specs):
    # type: (Iterable[Union[int, str]]) -> FrozenSet[int]
    """
    Parse a bad-block table into a set of absolute logical block numbers.

    Each item may be a single block number (``12``) or an inclusive range
    (``12-15``); whitespace around tokens is ignored.  Duplicates are
    collapsed.

    Args:
        specs: Items describing bad blocks, each either an int or a string
            of the form ``N`` or ``START-END``.
    Returns:
        A frozenset of bad logical block numbers.
    """
    bad_blocks = set()
    for spec in specs:
        if isinstance(spec, int):
            if spec < 0:
                raise pycdlibexception.PyCdlibInvalidInput('Bad block numbers must be non-negative, got %d' % spec)
            bad_blocks.add(spec)
            continue

        token = str(spec).strip()
        if not token:
            continue
        try:
            if '-' in token:
                start_text, end_text = token.split('-', 1)
                start = int(start_text)
                end = int(end_text)
                if start < 0 or end < start:
                    raise pycdlibexception.PyCdlibInvalidInput('Invalid bad block range %r' % token)
                bad_blocks.update(range(start, end + 1))
            else:
                block = int(token)
                if block < 0:
                    raise pycdlibexception.PyCdlibInvalidInput('Bad block numbers must be non-negative, got %d' % block)
                bad_blocks.add(block)
        except ValueError:
            raise pycdlibexception.PyCdlibInvalidInput('Invalid bad block specification %r' % token) from None

    return frozenset(bad_blocks)


def _resolve_path_type(iso, path_type):
    # type: (PyCdlib, str) -> str
    """Resolve an 'auto' path type to a concrete path keyword."""
    if path_type != 'auto':
        return path_type

    if iso.has_udf():
        return 'udf_path'
    if iso.has_rock_ridge():
        return 'rr_path'
    if iso.has_joliet():
        return 'joliet_path'
    return 'iso_path'


def _blocks_for_range(extent, length, block_size):
    # type: (int, int, int) -> Tuple[int, ...]
    """Return the logical blocks covered by an extent of the given length."""
    if length <= 0:
        return tuple()
    return tuple(range(extent, extent + utils.ceiling_div(length, block_size)))


def _dr_entry_blocks(record, block_size):
    # type: (dr.DirectoryRecord, int) -> Tuple[int, ...]
    """
    Collect the data blocks of an ISO9660/Joliet/Rock Ridge record, walking
    the multi-extent continuation chain used for very large files.
    """
    blocks = []  # type: List[int]
    current = record  # type: Optional[dr.DirectoryRecord]
    while current is not None:
        blocks.extend(_blocks_for_range(current.extent_location(),
                                        current.data_length, block_size))
        current = current.data_continuation
    return tuple(blocks)


def _dr_total_length(record):
    # type: (dr.DirectoryRecord) -> int
    """
    Sum the data length across a multi-extent continuation chain.  The head
    record only reports its own chunk's length, so a file split across several
    directory records must add every chunk's length to get the true size.
    """
    total = 0
    current = record  # type: Optional[dr.DirectoryRecord]
    while current is not None:
        total += current.data_length
        current = current.data_continuation
    return total


def _entry_total_length(path_type, record):
    # type: (str, Union[dr.DirectoryRecord, udfmod.UDFFileEntry]) -> int
    """Return the full on-disk data length for an entry, across extents."""
    if path_type == 'udf_path':
        return record.get_data_length()
    assert isinstance(record, dr.DirectoryRecord)
    return _dr_total_length(record)


def _udf_entry_blocks(iso, entry, block_size):
    # type: (PyCdlib, udfmod.UDFFileEntry, int) -> Tuple[int, ...]
    """
    Collect the data blocks of a UDF File Entry from its allocation
    descriptors, converting partition-relative block numbers to absolute
    logical blocks.
    """
    part_start = iso.udf_main_descs.partitions[0].part_start_location
    blocks = []  # type: List[int]
    for desc in entry.alloc_descs:
        start = part_start + desc.log_block_num
        blocks.extend(_blocks_for_range(start, desc.extent_length, block_size))
    return tuple(blocks)


def _entry_blocks(iso, path_type, record, block_size):
    # type: (PyCdlib, str, Union[dr.DirectoryRecord, udfmod.UDFFileEntry], int) -> Tuple[int, ...]
    if path_type == 'udf_path':
        assert isinstance(record, udfmod.UDFFileEntry)
        return _udf_entry_blocks(iso, record, block_size)
    assert isinstance(record, dr.DirectoryRecord)
    return _dr_entry_blocks(record, block_size)


def _missing_bytes_in_blocks(blocks, bad_blocks, total_bytes, block_size):
    # type: (Tuple[int, ...], FrozenSet[int], int, int) -> int
    """
    Count the claimed bytes that fall on bad blocks.  Every bad block except
    possibly the final block of the entry contributes a full block size; a
    bad final block only contributes the file's tail rather than the whole
    padded block.
    """
    if not bad_blocks or not blocks:
        return 0

    last_block = blocks[-1]
    tail = total_bytes % block_size
    if tail == 0:
        tail = block_size

    missing = 0
    for block in blocks:
        if block in bad_blocks:
            if block == last_block:
                missing += tail
            else:
                missing += block_size
    return missing


def _classify(record, blocks, overlapping, total_bytes, missing_bytes):
    # type: (Union[dr.DirectoryRecord, udfmod.UDFFileEntry], Tuple[int, ...], Tuple[int, ...], int, int) -> EntryAssessment
    if record.is_dir():
        entry_type = 'dir'
        status = DIR_UNTRUSTED if overlapping else FILE_COMPLETE
    elif record.is_symlink():
        entry_type = 'symlink'
        status = FILE_PARTIAL if overlapping else FILE_COMPLETE
    elif total_bytes == 0 or not blocks:
        entry_type = 'empty'
        status = FILE_COMPLETE
    else:
        entry_type = 'file'
        status = FILE_PARTIAL if overlapping else FILE_COMPLETE

    return EntryAssessment(path='', name=record.file_identifier(),
                           entry_type=entry_type, status=status,
                           data_blocks=blocks, bad_blocks=overlapping,
                           total_bytes=total_bytes,
                           missing_bytes=missing_bytes,
                           under_untrusted_dir=False)


def _dr_children(record, use_rr):
    # type: (dr.DirectoryRecord, bool) -> List[dr.DirectoryRecord]
    """Return the real (non dot/dotdot) ISO9660 children of a directory."""
    from pycdlib.pycdlib import _yield_children

    children = []
    for child in _yield_children(record, use_rr):
        if child is None or child.is_dot() or child.is_dotdot():
            continue
        children.append(child)
    return children


def _udf_children(entry):
    # type: (udfmod.UDFFileEntry) -> List[udfmod.UDFFileEntry]
    """Return the real (non parent) UDF File Entries in a directory."""
    children = []
    for fi_desc in entry.fi_descs.values():
        if fi_desc.is_parent():
            continue
        if fi_desc.file_entry is not None:
            children.append(fi_desc.file_entry)
    return children


def _walk_entries(iso, path_type):
    # type: (PyCdlib, str) -> List[Union[dr.DirectoryRecord, udfmod.UDFFileEntry]]
    """Breadth-first walk of every entry reachable under the path convention."""
    if path_type == 'udf_path':
        if iso.udf_root is None:
            raise pycdlibexception.PyCdlibInvalidInput('Can only assess a UDF path on a UDF ISO')
        queue = collections.deque([iso.udf_root])  # type: collections.deque[Union[dr.DirectoryRecord, udfmod.UDFFileEntry]]
    else:
        root = iso.get_record(**{path_type: '/'})
        assert isinstance(root, dr.DirectoryRecord)
        queue = collections.deque([root])

    results = []  # type: List[Union[dr.DirectoryRecord, udfmod.UDFFileEntry]]
    while queue:
        record = queue.popleft()
        results.append(record)
        if not record.is_dir():
            continue
        if path_type == 'udf_path':
            assert isinstance(record, udfmod.UDFFileEntry)
            queue.extend(_udf_children(record))
        else:
            assert isinstance(record, dr.DirectoryRecord)
            queue.extend(_dr_children(record, path_type == 'rr_path'))
    return results


def _is_ancestor(ancestor_path, descendant_path):
    # type: (str, str) -> bool
    """Whether ancestor_path is an ancestor directory of descendant_path."""
    if ancestor_path == '/':
        return True
    prefix = ancestor_path.rstrip('/') + '/'
    return descendant_path.startswith(prefix)


def assess_iso(iso, bad_blocks, path_type='auto'):
    # type: (PyCdlib, Iterable[Union[int, str]], str) -> SalvagePlan
    """
    Classify every entry of a parsed ISO against its bad-block table.

    The ISO must already be open via :meth:`pycdlib.PyCdlib.open`.  Entries
    whose metadata could not be parsed are not represented here; in
    particular, if a bad block makes a directory extent unparseable the
    initial open will fail before this routine can run.

    Args:
        iso: An open PyCdlib object.
        bad_blocks: Logical block numbers that are unreadable, accepted as
            ints or as range strings (see :func:`parse_bad_blocks`).
        path_type: Path convention to walk; 'auto' (the default) prefers
            UDF, then Rock Ridge, then Joliet, then plain ISO9660.
    Returns:
        A :class:`SalvagePlan` describing every reachable entry.
    """
    if not iso._initialized:  # pylint: disable=protected-access
        raise pycdlibexception.PyCdlibInvalidInput('This object is not initialized; call open() to create an ISO')

    resolved = _resolve_path_type(iso, path_type)
    if resolved == 'udf_path' and not iso.has_udf():
        raise pycdlibexception.PyCdlibInvalidInput('Can only assess a UDF path on a UDF ISO')
    if resolved == 'rr_path' and not iso.has_rock_ridge():
        raise pycdlibexception.PyCdlibInvalidInput('Can only assess a Rock Ridge path on a Rock Ridge ISO')
    if resolved == 'joliet_path' and not iso.has_joliet():
        raise pycdlibexception.PyCdlibInvalidInput('Can only assess a Joliet path on a Joliet ISO')

    bad = parse_bad_blocks(bad_blocks)
    block_size = iso.logical_block_size
    use_rr = resolved == 'rr_path'

    assessments = []
    for record in _walk_entries(iso, resolved):
        blocks = _entry_blocks(iso, resolved, record, block_size)
        total_bytes = _entry_total_length(resolved, record)
        overlapping = tuple(sorted(frozenset(blocks) & bad))
        missing = _missing_bytes_in_blocks(blocks, frozenset(overlapping),
                                           total_bytes, block_size)
        assessment = _classify(record, blocks, overlapping, total_bytes,
                               missing)
        assessment.path = iso.full_path_from_dirrecord(
            record, rockridge=use_rr)
        assessments.append(assessment)

    untrusted_dirs = [assessment.path for assessment in assessments
                      if assessment.status == DIR_UNTRUSTED]
    for assessment in assessments:
        if assessment.status == DIR_UNTRUSTED:
            continue
        assessment.under_untrusted_dir = any(
            _is_ancestor(dir_path, assessment.path)
            for dir_path in untrusted_dirs)

    return SalvagePlan(resolved, block_size, assessments)


def _safe_output_path(extract_root, relative_name):
    # type: (str, str) -> str
    """Build an output path that stays below the extract root."""
    output_path = os.path.abspath(os.path.join(extract_root, relative_name))
    try:
        lexical_root = os.path.commonpath([extract_root, output_path])
        resolved_root = os.path.commonpath([extract_root,
                                            os.path.realpath(output_path)])
    except ValueError:
        raise pycdlibexception.PyCdlibInvalidInput(
            'ISO entry would be extracted outside the target directory') from None

    if lexical_root != extract_root or resolved_root != extract_root:
        raise pycdlibexception.PyCdlibInvalidInput(
            'ISO entry would be extracted outside the target directory')
    return output_path


def _read_raw_block(iso, extent):
    # type: (PyCdlib, int) -> bytes
    """Read one logical block straight from the backing file object."""
    fp = iso._cdfp  # pylint: disable=protected-access
    fp.seek(extent * iso.logical_block_size)
    return fp.read(iso.logical_block_size)


def _write_partial(iso, assessment, output_path, bad_blocks):
    # type: (PyCdlib, EntryAssessment, str, FrozenSet[int]) -> None
    """
    Write a partial file block by block, zero-filling bad blocks so the
    surviving blocks keep their original file offsets.
    """
    block_size = iso.logical_block_size
    written = 0
    with open(output_path, 'wb') as outfp:
        for block in assessment.data_blocks:
            remaining = assessment.total_bytes - written
            if block in bad_blocks:
                chunk = b'\x00' * min(block_size, remaining)
            else:
                chunk = _read_raw_block(iso, block)
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
            outfp.write(chunk)
            written += len(chunk)


def _extract_one(iso, path_type, assessment, extract_root,
                 include_partial, bad_blocks):
    # type: (PyCdlib, str, EntryAssessment, str, bool, FrozenSet[int]) -> Optional[str]
    """
    Extract a single assessment, returning the written path, or None when the
    entry is skipped (rejected by policy or a symlink that cannot be trusted).
    """
    relative = assessment.path.lstrip('/')

    if assessment.entry_type == 'dir':
        if assessment.status == FILE_COMPLETE and not assessment.under_untrusted_dir:
            os.makedirs(_safe_output_path(extract_root, relative), exist_ok=True)
        return None

    if assessment.under_untrusted_dir:
        return None

    if assessment.entry_type == 'symlink':
        if assessment.status != FILE_COMPLETE or path_type == 'udf_path':
            return None
        output_path = _safe_output_path(extract_root, relative)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        record = iso.get_record(**{path_type: assessment.path})
        assert isinstance(record, dr.DirectoryRecord)
        if record.rock_ridge is None:
            return None
        os.symlink(record.rock_ridge.symlink_path(), output_path)
        return output_path

    if assessment.status == FILE_COMPLETE:
        output_path = _safe_output_path(extract_root, relative)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'wb') as outfp:
            iso.get_file_from_iso_fp(outfp, **{path_type: assessment.path})
        return output_path

    if assessment.status == FILE_PARTIAL and include_partial:
        output_path = _safe_output_path(extract_root, relative + '.partial')
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        _write_partial(iso, assessment, output_path, bad_blocks)
        return output_path

    return None


def extract_recoverable(iso, bad_blocks, extract_root, path_type='auto',
                        include_partial=False):
    # type: (PyCdlib, Iterable[Union[int, str]], str, str, bool) -> Tuple[SalvagePlan, List[Tuple[str, str]]]
    """
    Assess a damaged ISO and ingest the entries that are safe to use.

    Complete entries are extracted under ``extract_root`` using the normal
    read path.  Entries below an untrusted directory are skipped, as are
    partial files unless ``include_partial`` is set; when enabled, partial
    files are written with a ``.partial`` suffix, zero-filling the bad
    blocks so the surviving blocks keep their original file offsets.
    Complete directories are created on demand; complete symlinks are
    recreated as local symlinks.

    Args:
        iso: An open PyCdlib object.
        bad_blocks: Logical block numbers that are unreadable.
        extract_root: Existing local directory to write into.
        path_type: Path convention to walk; see :func:`assess_iso`.
        include_partial: When True, also write partial files with zero-filled
            bad blocks and a ``.partial`` suffix.
    Returns:
        A tuple of the :class:`SalvagePlan` and a list of
        ``(absolute ISO path, local output path)`` pairs written.
    """
    plan = assess_iso(iso, bad_blocks, path_type=path_type)
    bad = parse_bad_blocks(bad_blocks)

    root = os.path.realpath(os.path.abspath(extract_root))
    if not os.path.isdir(root):
        raise pycdlibexception.PyCdlibInvalidInput('Extract root %r is not a directory' % extract_root)

    written = []
    for assessment in plan.assessments:
        output_path = _extract_one(iso, plan.path_type, assessment, root,
                                   include_partial, bad)
        if output_path is not None:
            written.append((assessment.path, output_path))

    return plan, written
