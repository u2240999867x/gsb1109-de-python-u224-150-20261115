"""Audit ISO files against a table of missing logical blocks."""

import os
import re

from pycdlib import pycdlibexception
from pycdlib import udf as udfmod
from pycdlib import utils

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any  # noqa: F401


FILE_COMPLETE = 'complete'
FILE_PARTIAL = 'partial'
FILE_MISSING = 'missing'
FILE_UNTRUSTED = 'untrusted'

DIRECTORY_TRUSTED = 'trusted'
DIRECTORY_UNTRUSTED = 'untrusted'

_TOKEN_RE = re.compile(r'^(0[xX][0-9a-fA-F]+|\d+)(?:\s*(?:-|\.\.|:)\s*(0[xX][0-9a-fA-F]+|\d+))?$')


def _parse_number(token):
    # type: (str) -> int
    """Parse a decimal or 0x-prefixed block number."""
    if token.lower().startswith('0x'):
        return int(token, 16)
    return int(token, 10)


def _parse_token(token):
    # type: (str) -> tuple
    """Parse a single table token into an inclusive (first, last) pair."""
    match = _TOKEN_RE.match(token)
    if match is None:
        raise pycdlibexception.PyCdlibInvalidInput("Invalid missing block table entry '%s'" % (token,))

    first = _parse_number(match.group(1))
    if match.group(2) is None:
        last = first
    else:
        last = _parse_number(match.group(2))

    if first < 0 or last < 0:
        raise pycdlibexception.PyCdlibInvalidInput('Missing block numbers cannot be negative')
    if last < first:
        raise pycdlibexception.PyCdlibInvalidInput("Missing block range '%s' ends before it starts" % (token,))
    return first, last


def _normalize_ranges(entries):
    # type: (Any) -> tuple
    """
    Turn user-supplied missing block entries into sorted, disjoint inclusive
    ranges.  Entries may be block numbers, (first, last) inclusive ranges,
    range() objects, or strings such as '12-20'.
    """
    ranges = []

    def add(first, last):
        # type: (int, int) -> None
        if first < 0 or last < 0:
            raise pycdlibexception.PyCdlibInvalidInput('Missing block numbers cannot be negative')
        if last < first:
            raise pycdlibexception.PyCdlibInvalidInput('Missing block range ends before it starts')
        ranges.append((first, last))

    def walk(value):
        # type: (Any) -> None
        if value is None:
            return
        if isinstance(value, bool):
            raise pycdlibexception.PyCdlibInvalidInput('Missing block numbers must be integers')
        if isinstance(value, int):
            add(value, value)
            return
        if isinstance(value, range):
            if value.step != 1:
                raise pycdlibexception.PyCdlibInvalidInput('Missing block range must have a step of 1')
            if len(value) > 0:
                add(value.start, value.stop - 1)
            return
        if isinstance(value, str):
            token = value.strip()
            if not token:
                return
            for part in re.split(r'[,;\s]+', token):
                if part:
                    first, last = _parse_token(part)
                    add(first, last)
            return
        if isinstance(value, tuple):
            if len(value) == 2 and all(isinstance(item, int) and not isinstance(item, bool) for item in value):
                add(value[0], value[1])
                return
            for item in value:
                walk(item)
            return
        if isinstance(value, list):
            # A list is always a sequence of entries; a two-element inclusive
            # range has to be spelled as a tuple (or a 'first-last' string) so
            # that [0, 27] cannot silently mean "blocks 0 through 27".
            for item in value:
                walk(item)
            return
        try:
            iterator = iter(value)
        except TypeError:
            raise pycdlibexception.PyCdlibInvalidInput('Missing block table must contain integers or ranges') from None
        for item in iterator:
            walk(item)

    walk(entries)

    ranges.sort()
    merged = []  # type: list
    for first, last in ranges:
        if merged and first <= merged[-1][1] + 1:
            if last > merged[-1][1]:
                merged[-1] = (merged[-1][0], last)
        else:
            merged.append((first, last))
    return tuple(merged)


class BlockTable:
    """
    A sorted set of missing logical blocks, stored as inclusive ranges.

    The table is independent of the ISO's logical block size; pass the same
    block size to the audit API that the table was recorded in.  A table of
    512-byte sectors can therefore be used against a 2048-byte ISO by passing
    ``block_size=512``.
    """

    __slots__ = ('ranges',)

    def __init__(self, entries=None):
        # type: (Any) -> None
        """
        Create a missing block table.

        Args:
            entries: An iterable of block numbers, inclusive (first, last)
                pairs, range() objects, or strings.  A string may contain
                comma- or whitespace-separated entries and '#' or '//'
                comments.  None or an empty iterable means no missing blocks.
        Returns:
            Nothing.
        """
        self.ranges = _normalize_ranges(entries)

    @classmethod
    def from_text(cls, text):
        # type: (str) -> BlockTable
        """Build a table from a textual list of blocks and ranges."""
        entries = []
        for line in text.splitlines():
            line = line.split('#', 1)[0].split('//', 1)[0]
            for token in re.split(r'[,;\s]+', line.strip()):
                if token:
                    entries.append(token)
        return cls(entries)

    def __bool__(self):
        # type: () -> bool
        return bool(self.ranges)

    def __len__(self):
        # type: () -> int
        return sum(last - first + 1 for first, last in self.ranges)

    def __iter__(self):
        # type: () -> Any
        for first, last in self.ranges:
            for block in range(first, last + 1):
                yield block

    def __repr__(self):
        # type: () -> str
        return 'BlockTable(%r)' % (self.ranges,)

    def contains(self, block):
        # type: (int) -> bool
        """Return True if the given logical block is missing."""
        for first, last in self.ranges:
            if block < first:
                return False
            if block <= last:
                return True
        return False

    def missing_blocks(self, byte_offset, length, block_size):
        # type: (int, int, int) -> tuple
        """
        Return the missing block numbers touched by a byte range.

        Args:
            byte_offset: The first byte of the range.
            length: The length of the range in bytes.
            block_size: The size of a block in the table.
        Returns:
            A tuple of missing block numbers, in ascending order.
        """
        if length <= 0:
            return ()
        first = byte_offset // block_size
        last = (byte_offset + length - 1) // block_size
        blocks = []  # type: list
        for low, high in self.ranges:
            if high < first:
                continue
            if low > last:
                break
            start = max(low, first)
            end = min(high, last)
            blocks.extend(range(start, end + 1))
        return tuple(blocks)

    def missing_bytes(self, byte_offset, length, block_size):
        # type: (int, int, int) -> int
        """
        Return how many bytes in a range fall inside missing blocks.

        Unlike :meth:`missing_blocks`, this is O(number of ranges) and does
        not build a potentially huge list of block numbers.
        """
        if length <= 0:
            return 0
        return sum(end - start for start, end in
                   self.missing_intervals(byte_offset, length, block_size))

    def missing_intervals(self, byte_offset, length, block_size):
        # type: (int, int, int) -> tuple
        """
        Return the missing byte intervals inside a range.

        Args:
            byte_offset: The first byte of the range.
            length: The length of the range in bytes.
            block_size: The size of a block in the table.
        Returns:
            A tuple of (start_byte, end_byte) pairs, end exclusive, in
            ascending order and with adjacent intervals merged.
        """
        if length <= 0:
            return ()
        first = byte_offset // block_size
        last = (byte_offset + length - 1) // block_size
        range_end = byte_offset + length
        intervals = []  # type: list
        for low, high in self.ranges:
            if high < first:
                continue
            if low > last:
                break
            start = max(byte_offset, max(low, first) * block_size)
            end = min(range_end, (min(high, last) + 1) * block_size)
            if end > start:
                if intervals and start <= intervals[-1][1]:
                    if end > intervals[-1][1]:
                        intervals[-1] = (intervals[-1][0], end)
                else:
                    intervals.append((start, end))
        return tuple(intervals)


def parse_block_table(text):
    # type: (str) -> BlockTable
    """
    Parse a textual missing block table.

    Each line may contain comments starting with '#' or '//'.  Entries are
    separated by commas, semicolons, or whitespace, and may be a single block
    ('42' or '0x2a') or an inclusive range ('42-50' or '42..50').

    Args:
        text: The table contents.
    Returns:
        A BlockTable.
    """
    if isinstance(text, bytes):
        text = text.decode('utf-8')
    if not isinstance(text, str):
        raise pycdlibexception.PyCdlibInvalidInput('Missing block table must be text')
    return BlockTable.from_text(text)


def load_block_table(source):
    # type: (Any) -> BlockTable
    """
    Load a missing block table from a file object, path, or text.

    Args:
        source: A file object, a path to a table file, or the table text.
    Returns:
        A BlockTable.
    """
    if hasattr(source, 'read'):
        return parse_block_table(source.read())
    if isinstance(source, (str, bytes, os.PathLike)):
        path = os.fspath(source)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as infp:
                return parse_block_table(infp.read())
        if isinstance(path, bytes):
            path = path.decode('utf-8')
        return parse_block_table(path)
    return BlockTable(source)


def _coerce_table(missing_blocks):
    # type: (Any) -> BlockTable
    if isinstance(missing_blocks, BlockTable):
        return missing_blocks
    if missing_blocks is None:
        return BlockTable()
    if isinstance(missing_blocks, (str, bytes, os.PathLike)) or hasattr(missing_blocks, 'read'):
        return load_block_table(missing_blocks)
    return BlockTable(missing_blocks)


class FileHealth:
    """The result of auditing one file path against a missing block table."""

    __slots__ = ('path', 'view', 'status', 'data_status', 'size',
                 'readable_bytes', 'missing_bytes', 'missing_blocks',
                 'extents', 'reason', 'missing_ranges')

    def __init__(self, path, view, status, data_status, size, readable_bytes,
                 missing_bytes, missing_blocks, extents, reason='',
                 missing_ranges=()):
        # type: (str, str, str, str, int, int, int, tuple, tuple, str, tuple) -> None
        self.path = path
        self.view = view
        self.status = status
        self.data_status = data_status
        self.size = size
        self.readable_bytes = readable_bytes
        self.missing_bytes = missing_bytes
        self.missing_blocks = missing_blocks
        self.extents = extents
        self.reason = reason
        self.missing_ranges = missing_ranges

    @property
    def is_complete(self):
        # type: () -> bool
        """Whether every byte of the file is readable."""
        return self.status == FILE_COMPLETE

    @property
    def is_partial(self):
        # type: () -> bool
        """Whether only part of the file is readable."""
        return self.status == FILE_PARTIAL

    @property
    def is_missing(self):
        # type: () -> bool
        """Whether none of the file's data is readable."""
        return self.status == FILE_MISSING

    @property
    def is_untrusted(self):
        # type: () -> bool
        """Whether the file's directory chain could not be trusted."""
        return self.status == FILE_UNTRUSTED

    @property
    def readable(self):
        # type: () -> bool
        """Whether at least some data is available (a zero-byte file counts)."""
        return self.status == FILE_COMPLETE or self.readable_bytes > 0

    def to_dict(self):
        # type: () -> dict
        """Serialize this result to JSON-friendly values."""
        return {
            'path': self.path,
            'view': self.view,
            'status': self.status,
            'data_status': self.data_status,
            'size': self.size,
            'readable_bytes': self.readable_bytes,
            'missing_bytes': self.missing_bytes,
            'missing_blocks': list(self.missing_blocks),
            'missing_ranges': [list(byte_range) for byte_range in self.missing_ranges],
            'extents': [list(extent) for extent in self.extents],
            'reason': self.reason,
        }

    def __repr__(self):
        # type: () -> str
        return 'FileHealth(path=%r, view=%r, status=%r, size=%d, readable_bytes=%d)' % (
            self.path, self.view, self.status, self.size, self.readable_bytes)


class DirectoryHealth:
    """The result of auditing one directory against a missing block table."""

    __slots__ = ('path', 'view', 'trusted', 'size', 'missing_blocks',
                 'extent', 'reason')

    def __init__(self, path, view, trusted, size, missing_blocks, extent, reason=''):
        # type: (str, str, bool, int, tuple, int, str) -> None
        self.path = path
        self.view = view
        self.trusted = trusted
        self.size = size
        self.missing_blocks = missing_blocks
        self.extent = extent
        self.reason = reason

    @property
    def status(self):
        # type: () -> str
        """Either 'trusted' or 'untrusted'."""
        return DIRECTORY_TRUSTED if self.trusted else DIRECTORY_UNTRUSTED

    @property
    def is_trusted(self):
        # type: () -> bool
        """Whether the directory's own records and all its ancestors are readable."""
        return self.trusted

    def to_dict(self):
        # type: () -> dict
        """Serialize this result to JSON-friendly values."""
        return {
            'path': self.path,
            'view': self.view,
            'status': self.status,
            'trusted': self.trusted,
            'size': self.size,
            'missing_blocks': list(self.missing_blocks),
            'extent': self.extent,
            'reason': self.reason,
        }

    def __repr__(self):
        # type: () -> str
        return 'DirectoryHealth(path=%r, view=%r, trusted=%r)' % (
            self.path, self.view, self.trusted)


class HealthReport:
    """A collection of file and directory health results for one ISO."""

    __slots__ = ('files', 'directories', 'missing_blocks', 'block_size',
                 'logical_block_size')

    def __init__(self, files, directories, missing_blocks, block_size,
                 logical_block_size):
        # type: (list, list, BlockTable, int, int) -> None
        self.files = files
        self.directories = directories
        self.missing_blocks = missing_blocks
        self.block_size = block_size
        self.logical_block_size = logical_block_size

    def _files_with_status(self, status):
        # type: (str) -> list
        return [entry for entry in self.files if entry.status == status]

    @property
    def complete_files(self):
        # type: () -> list
        """All files whose data is completely readable."""
        return self._files_with_status(FILE_COMPLETE)

    @property
    def partial_files(self):
        # type: () -> list
        """All files with at least one readable and one missing byte."""
        return self._files_with_status(FILE_PARTIAL)

    @property
    def missing_files(self):
        # type: () -> list
        """All non-empty files with no readable data."""
        return self._files_with_status(FILE_MISSING)

    @property
    def untrusted_files(self):
        # type: () -> list
        """All files whose directory chain (or metadata) is untrusted."""
        return self._files_with_status(FILE_UNTRUSTED)

    @property
    def untrusted_directories(self):
        # type: () -> list
        """All directories whose own extent or ancestor is untrusted."""
        return [entry for entry in self.directories if not entry.trusted]

    @property
    def usable_files(self):
        # type: () -> list
        """
        Files that can be ingested as-is: every byte is present and the
        directory chain above them is trusted.
        """
        return self.complete_files

    @property
    def salvageable_files(self):
        # type: () -> list
        """
        Files with some readable data but at least one missing byte.  They
        can be ingested with the missing ranges recorded.
        """
        return self.partial_files

    @property
    def unusable_files(self):
        # type: () -> list
        """Files that have no readable data, or that cannot be trusted."""
        return self.missing_files + self.untrusted_files

    def for_view(self, view):
        # type: (str) -> tuple
        """Return the (files, directories) results for a single view."""
        return ([entry for entry in self.files if entry.view == view],
                [entry for entry in self.directories if entry.view == view])

    def file(self, path, view=None):
        # type: (str, Any) -> FileHealth
        """Find a file result by path, optionally restricted to one view."""
        for entry in self.files:
            if entry.path == path and (view is None or entry.view == view):
                return entry
        raise KeyError("No health result for path '%s'" % (path,))

    def summary(self):
        # type: () -> dict
        """Return counts grouped by file and directory status."""
        return {
            'files': {
                FILE_COMPLETE: len(self.complete_files),
                FILE_PARTIAL: len(self.partial_files),
                FILE_MISSING: len(self.missing_files),
                FILE_UNTRUSTED: len(self.untrusted_files),
            },
            'directories': {
                DIRECTORY_TRUSTED: len(self.directories) - len(self.untrusted_directories),
                DIRECTORY_UNTRUSTED: len(self.untrusted_directories),
            },
        }

    def to_dict(self):
        # type: () -> dict
        """Serialize the whole report to JSON-friendly values."""
        return {
            'block_size': self.block_size,
            'logical_block_size': self.logical_block_size,
            'missing_blocks': [list(block_range) for block_range in self.missing_blocks.ranges],
            'summary': self.summary(),
            'files': [entry.to_dict() for entry in self.files],
            'directories': [entry.to_dict() for entry in self.directories],
        }

    def __repr__(self):
        # type: () -> str
        return 'HealthReport(files=%d, directories=%d, block_size=%d)' % (
            len(self.files), len(self.directories), self.block_size)


def _status_from_missing(size, missing_bytes):
    # type: (int, int) -> str
    if size <= 0 or missing_bytes <= 0:
        return FILE_COMPLETE
    if missing_bytes >= size:
        return FILE_MISSING
    return FILE_PARTIAL


def _extent_missing(table, extents, block_size):
    # type: (BlockTable, tuple, int) -> tuple
    missing_blocks = []  # type: list
    missing_bytes = 0
    for byte_offset, length in extents:
        missing_blocks.extend(table.missing_blocks(byte_offset, length, block_size))
        missing_bytes += table.missing_bytes(byte_offset, length, block_size)
    return tuple(sorted(set(missing_blocks))), missing_bytes


def _missing_ranges(missing_blocks, extents, block_size):
    # type: (tuple, tuple, int) -> tuple
    """
    Convert file-relative missing byte runs to (offset, length) pairs.

    The returned offsets are relative to the start of the file, so a caller
    can skip exactly the damaged ranges while reading.
    """
    if not missing_blocks or not extents:
        return ()
    missing = BlockTable(missing_blocks)
    ranges = []
    file_offset = 0
    for byte_offset, length in extents:
        intervals = missing.missing_intervals(byte_offset, length, block_size)
        for start, end in intervals:
            ranges.append((file_offset + start - byte_offset, end - start))
        file_offset += length
    return tuple(ranges)


def _bytes_in_extents(extents):
    # type: (tuple) -> int
    return sum(length for _offset, length in extents)


def _iso_file_extents(record, logical_block_size):
    # type: (Any, int) -> tuple
    """Get the byte extents for a record and its multi-extent continuations."""
    extents = []
    current = record
    while current is not None:
        if current.inode is None or current.data_length == 0:
            break
        byte_offset = (current.inode.extent_location() + current.data_extent_offset) * logical_block_size
        extents.append((byte_offset, current.data_length))
        current = current.data_continuation
    return tuple(extents)


def _iso_file_size(record):
    # type: (Any) -> int
    """
    The declared file size.  A data_continuation record only contributes
    when it has its own Inode; duplicate directory records that were split
    across blocks (for example an El Torito boot catalog entry) can share the
    first record's name but carry no data of their own, and must not inflate
    the size.
    """
    size = 0
    current = record
    while current is not None:
        if current.inode is None and current is not record:
            break
        size += current.data_length
        current = current.data_continuation
    return size


def _udf_data_extents(record, part_start, logical_block_size):
    # type: (Any, int, int) -> tuple
    """Get the byte extents for a UDF File Entry."""
    extents = []
    for desc in record.alloc_descs:
        if isinstance(desc, udfmod.UDFInlineAD):
            byte_offset = desc.log_block_num * logical_block_size + desc.offset
        else:
            byte_offset = (part_start + desc.log_block_num) * logical_block_size + getattr(desc, 'offset', 0)
        extents.append((byte_offset, desc.extent_length))
    return tuple(extents)


def _trim_extents(extents, size):
    # type: (tuple, int) -> tuple
    trimmed = []
    remaining = size
    for byte_offset, length in extents:
        if remaining <= 0:
            break
        use_length = min(length, remaining)
        trimmed.append((byte_offset, use_length))
        remaining -= use_length
    return tuple(trimmed)


def _udf_entry_block(record):
    # type: (Any) -> int
    """The logical block holding a UDF File Entry itself."""
    return record.extent_location()


def _udf_file_health(record, path, table, block_size, logical_block_size,
                     part_start, parent_trusted):
    # type: (Any, str, BlockTable, int, int, int, bool) -> FileHealth
    size = record.get_data_length()
    data_extents = _udf_data_extents(record, part_start, logical_block_size)
    data_extents = _trim_extents(data_extents, size)
    extents_total = _bytes_in_extents(data_extents)

    entry_blocks = table.missing_blocks(_udf_entry_block(record) * logical_block_size,
                                        logical_block_size, block_size)
    missing_blocks, missing_bytes = _extent_missing(table, data_extents, block_size)
    missing_blocks = tuple(sorted(set(entry_blocks + missing_blocks)))
    missing_ranges = _missing_ranges(missing_blocks, data_extents, block_size)

    data_status = _status_from_missing(size, min(missing_bytes, size))
    readable_bytes = max(0, size - min(missing_bytes, size))

    status = data_status
    reason = ''
    if entry_blocks:
        status = FILE_UNTRUSTED
        reason = 'udf_file_entry_block_missing'
    elif not parent_trusted:
        status = FILE_UNTRUSTED
        reason = 'parent_directory_untrusted'
    elif size > 0 and not data_extents:
        status = FILE_UNTRUSTED
        reason = 'no_data_extent'
    elif extents_total < size:
        status = FILE_UNTRUSTED
        reason = 'extent_length_mismatch'
    elif size == 0:
        reason = 'empty_file'

    return FileHealth(path, 'udf', status, data_status, size, readable_bytes,
                      min(missing_bytes, size), missing_blocks, data_extents,
                      reason, missing_ranges)


def _iso_file_health(record, path, view, table, block_size,
                     logical_block_size, parent_trusted):
    # type: (Any, str, str, BlockTable, int, int, bool) -> FileHealth
    if record.is_symlink():
        return FileHealth(path, view, FILE_COMPLETE if parent_trusted else FILE_UNTRUSTED,
                          FILE_COMPLETE, 0, 0, 0, (), (),
                          'symlink' if parent_trusted else 'parent_directory_untrusted')

    size = _iso_file_size(record)
    extents = _iso_file_extents(record, logical_block_size)
    extents_total = _bytes_in_extents(extents)
    missing_blocks, missing_bytes = _extent_missing(table, extents, block_size)
    missing_ranges = _missing_ranges(missing_blocks, extents, block_size)
    missing_bytes = min(missing_bytes, size)
    data_status = _status_from_missing(size, missing_bytes)
    readable_bytes = max(0, size - missing_bytes)

    status = data_status
    reason = ''
    if not parent_trusted:
        status = FILE_UNTRUSTED
        reason = 'parent_directory_untrusted'
    elif size > 0 and record.inode is None:
        status = FILE_UNTRUSTED
        reason = 'no_data_extent'
    elif extents_total < size:
        status = FILE_UNTRUSTED
        reason = 'extent_length_mismatch'
    elif size == 0:
        reason = 'empty_file'

    return FileHealth(path, view, status, data_status, size, readable_bytes,
                      missing_bytes, missing_blocks, extents, reason,
                      missing_ranges)


def _iso_directory_health(record, path, view, table, block_size,
                          logical_block_size, parent_trusted):
    # type: (Any, str, str, BlockTable, int, int, bool) -> DirectoryHealth
    size = record.get_data_length()
    extent = record.extent_location()
    missing_blocks = table.missing_blocks(extent * logical_block_size, size, block_size)
    trusted = parent_trusted and not missing_blocks
    if missing_blocks:
        reason = 'directory_extent_missing'
    elif not parent_trusted:
        reason = 'parent_directory_untrusted'
    else:
        reason = ''
    return DirectoryHealth(path, view, trusted, size, missing_blocks, extent, reason)


def _udf_directory_health(record, path, table, block_size,
                          logical_block_size, part_start, parent_trusted):
    # type: (Any, str, BlockTable, int, int, int, bool) -> DirectoryHealth
    size = record.get_data_length()
    extents = _trim_extents(_udf_data_extents(record, part_start, logical_block_size), size)
    entry_missing = table.missing_blocks(_udf_entry_block(record) * logical_block_size,
                                         logical_block_size, block_size)
    extent_missing, _unused = _extent_missing(table, extents, block_size)
    missing_blocks = tuple(sorted(set(entry_missing + extent_missing)))
    trusted = parent_trusted and not missing_blocks
    if entry_missing:
        reason = 'udf_file_entry_block_missing'
    elif extent_missing:
        reason = 'directory_extent_missing'
    elif not parent_trusted:
        reason = 'parent_directory_untrusted'
    else:
        reason = ''
    return DirectoryHealth(path, 'udf', trusted, size, missing_blocks,
                           _udf_entry_block(record), reason)


def _iso_path(iso, record):
    # type: (Any, Any) -> str
    return iso.full_path_from_dirrecord(record)


def _audit_iso_view(iso, root, view, table, block_size, logical_block_size,
                    report, rockridge=False):
    # type: (Any, Any, str, BlockTable, int, int, HealthReport, bool) -> None
    stack = [(root, _iso_path(iso, root), True)]
    while stack:
        record, path, parent_trusted = stack.pop()
        directory = _iso_directory_health(record, path, view, table, block_size,
                                          logical_block_size, parent_trusted)
        report.directories.append(directory)

        if rockridge:
            children = [child for child in _rr_children(record)
                        if child is not None and not child.is_dot() and not child.is_dotdot()]
        else:
            children = _iso_children(record)

        for child in children:
            if rockridge:
                child_path = _rr_child_path(path, child)
            else:
                child_path = _iso_path(iso, child)
            if _is_unresolved_child_link(child):
                # The relocated directory this placeholder points at could
                # not be parsed (typically because its extent is damaged), so
                # everything below it is unknown.
                report.directories.append(DirectoryHealth(
                    child_path, view, False, 0, (), child.extent_location(),
                    'child_link_target_missing'))
            elif child.is_dir():
                stack.append((child, child_path, directory.trusted))
            else:
                report.files.append(_iso_file_health(child, child_path, view,
                                                     table, block_size,
                                                     logical_block_size,
                                                     directory.trusted))


def _is_unresolved_child_link(child):
    # type: (Any) -> bool
    return child.rock_ridge is not None and \
        child.rock_ridge.child_link_record_exists() and \
        child.rock_ridge.cl_to_moved_dr is None


def _iso_children(record):
    # type: (Any) -> list
    """
    Children of an ISO9660 directory, with multi-extent continuation chunks
    collapsed onto their first record.  Rock Ridge CL placeholders are kept
    only when their relocation target could not be parsed, so the caller can
    report the missing subtree instead of silently dropping it.
    """
    children = []
    seen = set()
    for child in record.children:
        if child.is_dot() or child.is_dotdot():
            continue
        if child.rock_ridge is not None and child.rock_ridge.child_link_record_exists():
            if child.rock_ridge.cl_to_moved_dr is None:
                children.append(child)
            continue
        ident = child.file_identifier()
        if ident in seen:
            # A later chunk of a multi-extent file; the base record already
            # carries the whole data_continuation chain.
            continue
        seen.add(ident)
        children.append(child)
    return children


def _rr_children(record):
    # type: (Any) -> list
    """Children of a record with Rock Ridge relocation links followed."""
    # Imported here because pycdlib.pycdlib imports this module.
    from pycdlib import pycdlib as pycdlibmod  # pylint: disable=cyclic-import,import-outside-toplevel
    return list(pycdlibmod._yield_children(record, True))  # pylint: disable=protected-access


def _rr_child_path(parent_path, child):
    # type: (str, Any) -> str
    if child.rock_ridge is not None:
        name = child.rock_ridge.name().decode('utf-8')
    else:
        name = child.file_identifier().decode('utf-8')
    if parent_path == '/':
        return '/' + name
    return parent_path + '/' + name


def _udf_child_path(parent_path, file_ident):
    # type: (str, Any) -> str
    name = file_ident.fi
    if name:
        try:
            decoded = name.decode(file_ident.encoding or 'latin-1')
        except (LookupError, UnicodeDecodeError):
            decoded = name.decode('latin-1', 'replace')
    else:
        decoded = ''
    if parent_path == '/':
        return '/' + decoded
    return parent_path + '/' + decoded


def _audit_udf_view(iso, table, block_size, logical_block_size, report):
    # type: (Any, BlockTable, int, int, HealthReport) -> None
    part_start = iso.udf_main_descs.partitions[0].part_start_location
    stack = [(iso.udf_root, '/', True)]
    while stack:
        record, path, parent_trusted = stack.pop()
        directory = _udf_directory_health(record, path, table, block_size,
                                          logical_block_size, part_start,
                                          parent_trusted)
        report.directories.append(directory)

        for file_ident in record.fi_descs.values():
            if file_ident.is_parent():
                continue
            child_path = _udf_child_path(path, file_ident)
            child = file_ident.file_entry
            if child is None:
                # pycdlib represents an all-zero (unreadable) File Entry as
                # None; report the block that holds it if the table names it.
                entry_block = part_start + file_ident.icb.log_block_num
                entry_missing = table.missing_blocks(
                    entry_block * logical_block_size, logical_block_size,
                    block_size)
                reason = ('udf_file_entry_block_missing' if entry_missing
                          else 'empty_udf_file_entry')
                if file_ident.is_dir():
                    report.directories.append(DirectoryHealth(
                        child_path, 'udf', False, 0, entry_missing,
                        entry_block, reason))
                else:
                    report.files.append(FileHealth(
                        child_path, 'udf', FILE_UNTRUSTED, FILE_UNTRUSTED,
                        0, 0, 0, entry_missing, (), reason))
                continue

            if child.is_dir():
                stack.append((child, child_path, directory.trusted))
            elif child.is_symlink():
                report.files.append(FileHealth(
                    child_path, 'udf',
                    FILE_COMPLETE if directory.trusted else FILE_UNTRUSTED,
                    FILE_COMPLETE, 0, 0, 0, (), (),
                    'symlink' if directory.trusted else
                    'parent_directory_untrusted'))
            else:
                report.files.append(_udf_file_health(
                    child, child_path, table, block_size, logical_block_size,
                    part_start, directory.trusted))


def audit_iso(iso, missing_blocks, block_size=None, views=None):
    # type: (Any, Any, Any, Any) -> HealthReport
    """
    Audit every file and directory in an ISO against a missing block table.

    Args:
        iso: An initialized PyCdlib object.
        missing_blocks: A BlockTable, a path to a table file, a file object,
            table text, or an iterable of block numbers / inclusive ranges.
        block_size: The size of a block in the table.  When None, the ISO's
            logical block size is used.
        views: The views to audit, in order.  Any of 'iso', 'rr', 'joliet',
            and 'udf'.  When None, a single view is chosen the same way
            pycdlib-extract-files chooses its path type: UDF if present,
            then Rock Ridge, then Joliet, then ISO9660.  Auditing the same
            file through several views produces one entry per view, so pass
            an explicit list when that is wanted.
    Returns:
        A HealthReport.
    """
    if not iso._initialized:  # pylint: disable=protected-access
        raise pycdlibexception.PyCdlibInvalidInput('This object is not initialized; call either open() or new() to create an ISO')

    if iso._needs_reshuffle:  # pylint: disable=protected-access
        iso._reshuffle_extents()  # pylint: disable=protected-access

    logical_block_size = iso.logical_block_size
    if block_size is None:
        block_size = logical_block_size
    if not isinstance(block_size, int) or isinstance(block_size, bool) or block_size <= 0:
        raise pycdlibexception.PyCdlibInvalidInput('block_size must be a positive integer')

    if views is None:
        if iso.udf_root is not None:
            views = ['udf']
        elif iso.rock_ridge:
            views = ['rr']
        elif iso.joliet_vd is not None:
            views = ['joliet']
        else:
            views = ['iso']
    else:
        views = [views] if isinstance(views, str) else list(views)
        for view in views:
            if view not in ('iso', 'rr', 'joliet', 'udf'):
                raise pycdlibexception.PyCdlibInvalidInput("Invalid view '%s'; must be one of 'iso', 'rr', 'joliet', or 'udf'" % (view,))

    table = _coerce_table(missing_blocks)
    report = HealthReport([], [], table, block_size, logical_block_size)

    for view in views:
        if view == 'iso':
            _audit_iso_view(iso, iso.pvd.root_directory_record(), 'iso',
                            table, block_size, logical_block_size, report)
        elif view == 'rr':
            if not iso.rock_ridge:
                raise pycdlibexception.PyCdlibInvalidInput('Cannot audit a rr view on a non-Rock Ridge ISO')
            _audit_iso_view(iso, iso.pvd.root_directory_record(), 'rr',
                            table, block_size, logical_block_size, report,
                            rockridge=True)
        elif view == 'joliet':
            if iso.joliet_vd is None:
                raise pycdlibexception.PyCdlibInvalidInput('Cannot audit a joliet view on a non-Joliet ISO')
            _audit_iso_view(iso, iso.joliet_vd.root_directory_record(),
                            'joliet', table, block_size, logical_block_size,
                            report)
        else:
            if iso.udf_root is None:
                raise pycdlibexception.PyCdlibInvalidInput('Cannot audit a udf view on a non-UDF ISO')
            _audit_udf_view(iso, table, block_size, logical_block_size, report)

    return report


def _normalize_lookup_path(path):
    # type: (str) -> str
    return utils.normpath(path).decode('utf-8')


def _ancestor_directories(iso, record):
    # type: (Any, Any) -> list
    """
    Directory records (and their paths) from the root down to a record's
    parent.  Sockets are cut when the underlying parent chain is malformed,
    so a corrupt ISO cannot make this loop.
    """
    ancestors = []
    seen = set()
    current = record.parent
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        ancestors.append(current)
        current = current.parent
    ancestors.reverse()
    return [(ancestor, _iso_path(iso, ancestor)) for ancestor in ancestors]


def _iso_ancestors_trusted(iso, record, view, table, block_size,
                           logical_block_size):
    # type: (Any, Any, str, BlockTable, int, int) -> tuple
    trusted = True
    missing = []  # type: list
    for ancestor, path in _ancestor_directories(iso, record):
        health = _iso_directory_health(ancestor, path, view, table, block_size,
                                       logical_block_size, trusted)
        missing.extend(health.missing_blocks)
        if not health.trusted:
            trusted = False
    return trusted, tuple(sorted(set(missing)))


def _rr_ancestors_trusted(iso, rr_path, table, block_size, logical_block_size):
    # type: (Any, str, BlockTable, int, int) -> tuple
    """
    Check the directory trust of every prefix of a Rock Ridge path.

    Rock Ridge relocation means the physical parent chain of a record can
    differ from the logical path that names it, so the logical prefixes are
    re-resolved instead of walking ``record.parent``.
    """
    from pycdlib import utils  # pylint: disable=import-outside-toplevel
    normalized = utils.normpath(rr_path).decode('utf-8')
    components = [component for component in normalized.split('/') if component]
    trusted = True
    missing = []  # type: list
    prefix = '/'
    if components:
        records = [iso.get_record(rr_path=prefix)]
        for component in components[:-1]:
            prefix = prefix + component if prefix == '/' else prefix + '/' + component
            records.append(iso.get_record(rr_path=prefix))
    else:
        records = [iso.get_record(rr_path='/')]
    for record in records:
        health = _iso_directory_health(record, _iso_path(iso, record), 'rr',
                                       table, block_size, logical_block_size,
                                       trusted)
        missing.extend(health.missing_blocks)
        if not health.trusted:
            trusted = False
    return trusted, tuple(sorted(set(missing)))


def _udf_ancestors_trusted(iso, record, table, block_size, logical_block_size):
    # type: (Any, Any, BlockTable, int, int) -> tuple
    part_start = iso.udf_main_descs.partitions[0].part_start_location
    trusted = True
    missing = []  # type: list
    for ancestor, path in _ancestor_directories(iso, record):
        health = _udf_directory_health(ancestor, path, table, block_size,
                                       logical_block_size, part_start, trusted)
        missing.extend(health.missing_blocks)
        if not health.trusted:
            trusted = False
    return trusted, tuple(sorted(set(missing)))


def _damaged_directories(iso, view, table, block_size, logical_block_size):
    # type: (Any, str, BlockTable, int, int) -> list
    """
    Report the directories whose own extent is missing, for one view.

    Used when a path lookup fails: if the table names one of these
    directories, the failure is explained by the damage rather than by the
    caller passing a nonexistent path.
    """
    try:
        full = audit_iso(iso, table, block_size, views=view)
    except pycdlibexception.PyCdlibInvalidInput:
        return []
    damaged_reasons = ('directory_extent_missing', 'udf_file_entry_block_missing',
                       'child_link_target_missing')
    damaged = []
    for entry in full.directories:
        if entry.reason in damaged_reasons:
            damaged.append(entry)
        elif not entry.trusted and entry.missing_blocks:
            damaged.append(entry)
    return damaged


def file_health(iso, missing_blocks, block_size=None, **kwargs):
    # type: (Any, Any, Any, Any) -> FileHealth
    """
    Audit one file path against a missing block table.

    Exactly one of iso_path, rr_path, joliet_path, or udf_path must be given.
    """
    if not iso._initialized:  # pylint: disable=protected-access
        raise pycdlibexception.PyCdlibInvalidInput('This object is not initialized; call either open() or new() to create an ISO')

    if iso._needs_reshuffle:  # pylint: disable=protected-access
        iso._reshuffle_extents()  # pylint: disable=protected-access

    logical_block_size = iso.logical_block_size
    if block_size is None:
        block_size = logical_block_size
    if not isinstance(block_size, int) or isinstance(block_size, bool) or block_size <= 0:
        raise pycdlibexception.PyCdlibInvalidInput('block_size must be a positive integer')

    num_paths = 0
    lookup = {}
    for key, value in kwargs.items():
        if key in ('iso_path', 'rr_path', 'joliet_path', 'udf_path'):
            if value is not None:
                num_paths += 1
                lookup[key] = value
        else:
            raise pycdlibexception.PyCdlibInvalidInput("Invalid keyword, must be one of 'iso_path', 'rr_path', 'joliet_path', or 'udf_path'")
    if num_paths != 1:
        raise pycdlibexception.PyCdlibInvalidInput("Must specify one, and only one of 'iso_path', 'rr_path', 'joliet_path', or 'udf_path'")

    table = _coerce_table(missing_blocks)

    view = None
    try:
        if 'udf_path' in lookup:
            view = 'udf'
            if iso.udf_root is None:
                raise pycdlibexception.PyCdlibInvalidInput('Cannot fetch a udf_path from a non-UDF ISO')
            record = iso.get_record(udf_path=lookup['udf_path'])
            path = _normalize_lookup_path(lookup['udf_path'])
        elif 'joliet_path' in lookup:
            view = 'joliet'
            if iso.joliet_vd is None:
                raise pycdlibexception.PyCdlibInvalidInput('Cannot fetch a joliet_path from a non-Joliet ISO')
            record = iso.get_record(joliet_path=lookup['joliet_path'])
            path = _iso_path(iso, record)
        elif 'rr_path' in lookup:
            view = 'rr'
            if not iso.rock_ridge:
                raise pycdlibexception.PyCdlibInvalidInput('Cannot fetch a rr_path from a non-Rock Ridge ISO')
            record = iso.get_record(rr_path=lookup['rr_path'])
            path = _normalize_lookup_path(lookup['rr_path'])
        else:
            view = 'iso'
            record = iso.get_record(iso_path=lookup['iso_path'])
            path = _iso_path(iso, record)
    except pycdlibexception.PyCdlibInvalidInput as err:
        if str(err) != 'Could not find path' or view is None:
            raise
        # The path itself may have vanished because a directory extent on the
        # way to it is missing.  Report that as untrusted instead of failing
        # the whole lookup, but only when the table actually damages a
        # directory; a genuinely wrong path stays an error.
        damaged = _damaged_directories(iso, view, table, block_size,
                                       logical_block_size)
        if not damaged:
            raise
        return FileHealth(path=_normalize_lookup_path(list(lookup.values())[0]),
                          view=view, status=FILE_UNTRUSTED,
                          data_status=FILE_UNTRUSTED, size=0,
                          readable_bytes=0, missing_bytes=0,
                          missing_blocks=damaged[0].missing_blocks,
                          extents=(), reason='directory_extent_missing')

    if record.is_dir():
        raise pycdlibexception.PyCdlibInvalidInput('Cannot get file health for a directory')

    if view == 'udf':
        trusted, _missing = _udf_ancestors_trusted(iso, record, table,
                                                   block_size,
                                                   logical_block_size)
        part_start = iso.udf_main_descs.partitions[0].part_start_location
        result = _udf_file_health(record, path, table, block_size,
                                  logical_block_size, part_start, trusted)
    elif view == 'rr':
        trusted, _missing = _rr_ancestors_trusted(iso, lookup['rr_path'],
                                                  table, block_size,
                                                  logical_block_size)
        result = _iso_file_health(record, path, view, table, block_size,
                                  logical_block_size, trusted)
    else:
        trusted, _missing = _iso_ancestors_trusted(iso, record, view, table,
                                                   block_size, logical_block_size)
        result = _iso_file_health(record, path, view, table, block_size,
                                  logical_block_size, trusted)
    return result
