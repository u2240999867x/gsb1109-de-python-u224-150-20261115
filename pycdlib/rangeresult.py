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

"""Results of a logical-range read over one or more on-disc extents."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import List


class RangeReadSegment:
    """
    One contiguous piece of a logical-range read, together with the
    on-disc location it was copied from.

    A logical range of a file can span several extents -- a multi-extent
    ISO9660 file (a file larger than a single directory record can
    describe) or a UDF file with several allocation descriptors.  Each
    segment describes the contribution of one such extent, in ascending
    file-logical order.

    Attributes:
        logical_offset: The byte offset of this segment's first byte
            within the original file (zero-based, file-relative).
        data: The bytes read for this segment.  Concatenating the
            ``data`` of every segment in order rebuilds the available
            portion of the requested range.
        disc_offset: The absolute byte offset of this segment's first
            byte inside the backing disc image (or the source file
            object for an ISO that has not been written out yet).
        disc_length: The number of bytes this segment was expected to
            occupy on the disc.  ``len(data)`` is smaller only when the
            backing extent returned a short read.
    """
    __slots__ = ('logical_offset', 'data', 'disc_offset', 'disc_length')

    def __init__(self, logical_offset, data, disc_offset, disc_length):
        # type: (int, bytes, int, int) -> None
        self.logical_offset = logical_offset
        self.data = data
        self.disc_offset = disc_offset
        self.disc_length = disc_length

    def __len__(self):
        # type: () -> int
        return len(self.data)

    def __repr__(self):
        # type: () -> str
        return ('RangeReadSegment(logical_offset=%d, len=%d, disc_offset=%d, disc_length=%d)'
                % (self.logical_offset, len(self.data),
                   self.disc_offset, self.disc_length))

    def __eq__(self, other):
        if not isinstance(other, RangeReadSegment):
            return NotImplemented
        return (self.logical_offset == other.logical_offset and
                self.data == other.data and
                self.disc_offset == other.disc_offset and
                self.disc_length == other.disc_length)


class RangeReadResult:
    """
    The outcome of a logical-range read.

    Besides the reassembled :attr:`data` bytes, the result carries the
    per-segment :attr:`segments` list describing where every byte came
    from on the disc, plus the bookkeeping a collector needs when a
    trailing extent is missing or returns a short read.

    A range read never fakes EOF: when a later extent cannot be read,
    the already-complete leading extents are still delivered here, and
    :attr:`complete` is ``False`` rather than silently truncating the
    file's logical length.

    Attributes:
        data: The reassembled bytes that were actually read, in file
            order.  This is the concatenation of every segment's
            ``data``.
        segments: The :class:`RangeReadSegment` objects backing
            :attr:`data`, in ascending file-logical order.
        file_length: The logical length of the original file, including
            any bytes that could not be read.
        logical_start: The zero-based file offset the range was asked
            to begin at.
        requested_length: The number of bytes the range was asked for,
            clamped to the original file length.
        available_length: The number of bytes actually delivered in
            :attr:`data` (``len(data)``).
        complete: ``True`` when the full requested range was read
            (``available_length == requested_length``), ``False`` when a
            trailing extent was missing or short.
        short_read: ``True`` when at least one extent returned fewer
            bytes than it was expected to hold.
    """
    __slots__ = ('data', 'segments', 'file_length', 'logical_start',
                 'requested_length', 'available_length', 'complete',
                 'short_read')

    def __init__(self, segments, file_length, logical_start,
                 requested_length, available_length, complete, short_read):
        # type: (List[RangeReadSegment], int, int, int, int, bool, bool) -> None
        self.segments = segments
        self.data = b''.join(seg.data for seg in segments)
        self.file_length = file_length
        self.logical_start = logical_start
        self.requested_length = requested_length
        self.available_length = available_length
        self.complete = complete
        self.short_read = short_read

    def __len__(self):
        # type: () -> int
        return len(self.data)

    def __repr__(self):
        # type: () -> str
        return ('RangeReadResult(start=%d, requested=%d, available=%d, complete=%s, short_read=%s, segments=%d)'
                % (self.logical_start, self.requested_length,
                   self.available_length, self.complete,
                   self.short_read, len(self.segments)))
