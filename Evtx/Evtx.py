#!/usr/bin/python
#    This file is part of python-evtx.
#
#   Copyright 2012, 2013 Willi Ballenthin <william.ballenthin@mandiant.com>
#                    while at Mandiant <http://www.mandiant.com>
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
#   Version v.0.3.0
from __future__ import absolute_import

import re
import sys
import mmap
import struct
import logging
import binascii
from dataclasses import dataclass, field
from functools import wraps
from typing import List, Optional

import Evtx.Views as e_views

from .Nodes import RootNode, TemplateNode, NameStringNode
from .BinaryParser import Block, OverrunBufferException, ParseException

logger = logging.getLogger(__name__)


# Fixed geometry of the EVTX format.  Chunks are always laid out at
# `file header size + chunk_index * CHUNK_SIZE`, so even when a chunk is
# unreadable, the following chunks are located by arithmetic on the original
# file rather than by scanning for signatures.
FILE_HEADER_SIZE = 0x1000
CHUNK_SIZE = 0x10000
CHUNK_HEADER_SIZE = 0x200
RECORD_HEADER_SIZE = 0x18
RECORD_MAGIC = 0x2A2A


class RecordStatus(object):
    """
    Structural status of a record header read at a layout-derived boundary.

    The status is decided solely from the bytes at an offset reached through
    the file's own structure (the chunk data origin, or the previous record's
    declared size).  The record magic appearing inside a payload is never
    treated as evidence of a record boundary.
    """

    VALID = "valid"
    OUT_OF_BOUNDS = "out_of_bounds"
    INVALID_MAGIC = "invalid_magic"
    INVALID_SIZE = "invalid_size"
    SIZE_MISMATCH = "size_mismatch"


class ChunkStatus(object):
    """
    Structural status of a 64KiB chunk located at its layout-derived offset.
    """

    VALID = "valid"
    EMPTY = "empty"
    INVALID_MAGIC = "invalid_magic"
    HEADER_CHECKSUM_FAILED = "header_checksum_failed"
    DATA_CHECKSUM_FAILED = "data_checksum_failed"
    INACCESSIBLE = "inaccessible"


@dataclass
class RecordResult(object):
    """
    The outcome of probing one record boundary.

    A `VALID` result carries a usable `Record`; every other status carries no
    record and the caller must stop walking this chunk, because the next
    boundary can no longer be derived from a trusted record size.

    Attributes:
      status: One of `RecordStatus`.
      offset: Absolute file offset of the probed record header.
      size: Declared record size, or None if not readable / not sane.
      record_num: Declared record number, or None if not readable.
      record: A `Record` when `status == RecordStatus.VALID`, else None.
      error: Human readable explanation of the failure, else None.
    """

    status: str
    offset: int
    size: Optional[int] = None
    record_num: Optional[int] = None
    record: Optional["Record"] = None
    error: Optional[str] = None
    render_error: Optional[str] = None

    @property
    def is_valid(self):
        return self.status == RecordStatus.VALID

    @property
    def renders(self):
        """
        Whether the recovered record's binary XML payload renders.

        This is independent of `is_valid`, which only covers the record
        header and size boundary.  It is populated by
        `FileRecoveryResult.verify_renderable_records`.
        """
        return self.is_valid and self.render_error is None


@dataclass
class ChunkRecoveryResult(object):
    """
    The trustworthy records recovered from one chunk, and the raw range that
    could not be recovered.

    The skipped range is expressed against the original file and covers from
    the first unparseable boundary up to the end of the chunk's data area.
    Records after that point inside the same chunk are not claimed, since no
    trusted boundary exists for them.  Later chunks are still processed
    independently through their fixed offsets.

    Attributes:
      chunk_index: Zero based position of the chunk in the file.
      chunk_offset: Absolute file offset of the chunk start.
      status: One of `ChunkStatus`, describing the chunk header/checksums.
      header_checksum_ok: Whether the stored header CRC matched.
      data_checksum_ok: Whether the stored data CRC matched.
      records: `RecordResult` entries for every boundary probed in order.
      skipped_range: `(start, end)` absolute byte range not recovered,
        inclusive-start/exclusive-end, or None if everything was recovered.
      error: Human readable explanation when the whole chunk was inaccessible.
    """

    chunk_index: int
    chunk_offset: int
    status: str
    header_checksum_ok: Optional[bool] = None
    data_checksum_ok: Optional[bool] = None
    records: List[RecordResult] = field(default_factory=list)
    skipped_range: Optional[tuple] = None
    error: Optional[str] = None

    @property
    def valid_records(self):
        return [r.record for r in self.records if r.is_valid]

    @property
    def skipped_length(self):
        if self.skipped_range is None:
            return 0
        return self.skipped_range[1] - self.skipped_range[0]


@dataclass
class FileRecoveryResult(object):
    """
    Aggregate outcome of an offline, best-effort recovery pass.

    Attributes:
      chunk_results: One `ChunkRecoveryResult` per chunk examined, in order.
      skipped_ranges: Every non-empty `(start, end)` raw range that was not
        recovered, in file order and expressed against the original file.
    """

    chunk_results: List[ChunkRecoveryResult] = field(default_factory=list)

    @property
    def records(self):
        for chunk_result in self.chunk_results:
            for record_result in chunk_result.records:
                if record_result.is_valid:
                    yield record_result.record

    @property
    def record_results(self):
        for chunk_result in self.chunk_results:
            for record_result in chunk_result.records:
                yield record_result

    def verify_renderable_records(self):
        """
        Attempt to render every structurally valid recovered record.

        Structural validity (header magic, size boundary, trailing size copy)
        is necessary but not sufficient: the binary XML payload can still be
        undecodable.  This records a separate `render_error` on each record
        result so the caller can distinguish trustworthy, fully readable
        records from records with an intact header but corrupt content.

        Returns:
          FileRecoveryResult: self, for convenient chaining.
        """
        for record_result in self.record_results:
            if not record_result.is_valid:
                continue
            try:
                record_result.record.xml()
            except Exception as exc:  # content-level failure, not a boundary failure
                record_result.render_error = "{}: {}".format(type(exc).__name__, exc)
        return self

    @property
    def skipped_ranges(self):
        return [cr.skipped_range for cr in self.chunk_results if cr.skipped_range is not None]

    @property
    def total_skipped_bytes(self):
        return sum(end - start for start, end in self.skipped_ranges)


class InvalidRecordException(ParseException):
    def __init__(self):
        super(InvalidRecordException, self).__init__("Invalid record structure")


class Evtx(object):
    """
    A convenience class that makes it easy to open an
      EVTX file and start iterating the important structures.
    Note, this class must be used in a context statement
       (see the `with` keyword).
    Note, this class will mmap the target file, so ensure
      your platform supports this operation.
    """

    def __init__(self, filename):
        """
        @type filename:  str
        @param filename: A string that contains the path
          to the EVTX file to open.
        """
        self._filename = filename
        self._buf = None
        self._f = None
        self._fh = None

    def __enter__(self):
        self._f = open(self._filename, "rb")
        self._buf = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        self._fh = FileHeader(self._buf, 0x0)
        return self

    def __exit__(self, type, value, traceback):
        self._buf.close()
        self._f.close()
        self._fh = None

    def ensure_contexted(func):
        """
        This decorator ensure that an instance of the
          Evtx class is used within a context statement.  That is,
          that the `with` statement is used, or `__enter__()`
          and `__exit__()` are called explicitly.
        """

        @wraps(func)
        def wrapped(self, *args, **kwargs):
            if self._buf is None:
                raise TypeError("An Evtx object must be used with" " a context (see the `with` statement).")
            else:
                return func(self, *args, **kwargs)

        return wrapped

    @ensure_contexted
    def chunks(self):
        """
        Get each of the ChunkHeaders from within this EVTX file.

        @rtype generator of ChunkHeader
        @return A generator of ChunkHeaders from this EVTX file.
        """
        for chunk in self._fh.chunks():
            yield chunk

    @ensure_contexted
    def records(self):
        """
        Get each of the Records from within this EVTX file.

        @rtype generator of Record
        @return A generator of Records from this EVTX file.
        """
        for chunk in self.chunks():
            for record in chunk.records():
                yield record

    @ensure_contexted
    def get_record(self, record_num):
        """
        Get a Record by record number.

        @type record_num:  int
        @param record_num: The record number of the the record to fetch.
        @rtype Record or None
        @return The record request by record number, or None if
          the record is not found.
        """
        return self._fh.get_record(record_num)

    @ensure_contexted
    def get_file_header(self):
        return self._fh

    @ensure_contexted
    def recover(self):
        """
        Offline, best-effort recovery across the whole file.

        Every chunk is reached at its fixed geometric offset
        (`0x1000 + index * 0x10000`), so an unreadable chunk never prevents
        the following chunks from being examined.  Each chunk is recovered
        independently; a failure is captured in that chunk's result rather
        than aborting the pass.

        Returns:
          FileRecoveryResult: the per-chunk results in order, the recovered
            records, and every skipped raw range against the original file.
        """
        result = FileRecoveryResult()
        file_length = len(self._buf)
        chunk_index = 0
        chunk_offset = FILE_HEADER_SIZE
        while chunk_offset < file_length:
            if chunk_offset + CHUNK_SIZE > file_length:
                # A truncated final chunk: report the bytes that exist rather
                # than silently dropping them.  The skipped range only covers
                # a data area that is actually present; a chunk truncated
                # inside its header has no in-file data range to skip.
                data_start = min(chunk_offset + CHUNK_HEADER_SIZE, file_length)
                skipped_range = (data_start, file_length) if file_length > data_start else None
                result.chunk_results.append(
                    ChunkRecoveryResult(
                        chunk_index=chunk_index,
                        chunk_offset=chunk_offset,
                        status=ChunkStatus.INACCESSIBLE,
                        skipped_range=skipped_range,
                        error="chunk {} is truncated: {} of {} bytes present".format(
                            chunk_index, file_length - chunk_offset, CHUNK_SIZE
                        ),
                    )
                )
                break

            chunk = ChunkHeader(self._buf, chunk_offset)
            try:
                chunk_result = chunk.recover()
            except (ParseException, OverrunBufferException) as exc:
                chunk_result = ChunkRecoveryResult(
                    chunk_index=chunk_index,
                    chunk_offset=chunk_offset,
                    status=ChunkStatus.INACCESSIBLE,
                    skipped_range=(chunk_offset + CHUNK_HEADER_SIZE, chunk_offset + CHUNK_SIZE),
                    error="could not parse chunk at {}: {}".format(hex(chunk_offset), exc),
                )
            result.chunk_results.append(chunk_result)

            chunk_index += 1
            chunk_offset += CHUNK_SIZE

        return result


class FileHeader(Block):
    def __init__(self, buf, offset):
        logger.debug("FILE HEADER at {}.".format(hex(offset)))
        super(FileHeader, self).__init__(buf, offset)
        self.declare_field("string", "magic", 0x0, length=8)
        self.declare_field("qword", "oldest_chunk")
        self.declare_field("qword", "current_chunk_number")
        self.declare_field("qword", "next_record_number")
        self.declare_field("dword", "header_size")
        self.declare_field("word", "minor_version")
        self.declare_field("word", "major_version")
        self.declare_field("word", "header_chunk_size")
        self.declare_field("word", "chunk_count")
        self.declare_field("binary", "unused1", length=0x4C)
        self.declare_field("dword", "flags")
        self.declare_field("dword", "checksum")

    def __repr__(self):
        return "FileHeader(buf={!r}, offset={!r})".format(self._buf, self._offset)

    def __str__(self):
        return "FileHeader(offset={})".format(hex(self._offset))

    def check_magic(self):
        """
        @return A boolean that indicates if the first eight bytes of
          the FileHeader match the expected magic value.
        """
        try:
            return self.magic() == "ElfFile\x00"
        except UnicodeDecodeError:
            return False

    def calculate_checksum(self):
        """
        @return A integer in the range of an unsigned int that
          is the calculated CRC32 checksum off the first 0x78 bytes.
          This is consistent with the checksum stored by the FileHeader.
        """
        return binascii.crc32(self.unpack_binary(0, 0x78)) & 0xFFFFFFFF

    def verify(self):
        """
        @return A boolean that indicates that the FileHeader
          successfully passes a set of heuristic checks that
          all EVTX FileHeaders should pass.
        """
        return (
            self.check_magic()
            and self.major_version() == 0x3
            and self.minor_version() == 0x1
            and self.header_chunk_size() == 0x1000
            and self.checksum() == self.calculate_checksum()
        )

    def is_dirty(self):
        """
        @return A boolean that indicates that the log has been
          opened and was changed, though not all changes might be
          reflected in the file header.
        """
        return self.flags() & 0x1 == 0x1

    def is_full(self):
        """
        @return A boolean that indicates that the log
          has reached its maximum configured size and the retention
          policy in effect does not allow to reclaim a suitable amount
          of space from the oldest records and an event message could
          not be written to the log file.
        """
        return self.flags() & 0x2 == 0x2

    def first_chunk(self):
        """
        @return A ChunkHeader instance that is the first chunk
          in the log file, which is always found directly after
          the FileHeader.
        """
        ofs = self._offset + self.header_chunk_size()
        return ChunkHeader(self._buf, ofs)

    def current_chunk(self):
        """
        @return A ChunkHeader instance that is the current chunk
          indicated by the FileHeader.
        """
        ofs = self._offset + self.header_chunk_size()
        ofs += self.current_chunk_number() * 0x10000
        return ChunkHeader(self._buf, ofs)

    def chunks(self, include_inactive=False):
        """
        @return A generator that yields the chunks of the log file
          starting with the first chunk, which is always found directly
          after the FileHeader.

        If `include_inactive` is set to true, enumerate chunks beyond those
        declared in the file header (and may therefore be corrupt).
        """
        if include_inactive:
            chunk_count = sys.maxsize
        else:
            chunk_count = self.chunk_count()

        i = 0
        ofs = self._offset + self.header_chunk_size()
        while ofs + 0x10000 <= len(self._buf) and i < chunk_count:
            yield ChunkHeader(self._buf, ofs)
            ofs += 0x10000
            i += 1

    def get_record(self, record_num):
        """
        Get a Record by record number.

        @type record_num:  int
        @param record_num: The record number of the the record to fetch.
        @rtype Record or None
        @return The record request by record number, or None if the
          record is not found.
        """
        for chunk in self.chunks():
            first_record = chunk.log_first_record_number()
            last_record = chunk.log_last_record_number()
            if not (first_record <= record_num <= last_record):
                continue
            for record in chunk.records():
                if record.record_num() == record_num:
                    return record
        return None


class Template(object):
    def __init__(self, template_node):
        self._template_node = template_node
        self._xml = None
    def _load_xml(self):
        """
        TODO(wb): One day, nodes should generate format strings
          instead of the XML format made-up abomination.
        """
        if self._xml is not None:
            return
        matcher = r"\[(?:Normal|Conditional) Substitution\(index=(\d+), type=\d+\)\]"
        self._xml = re.sub(
            matcher, "{\\1:}", self._template_node.template_format().replace("{", "{{").replace("}", "}}")
        )

    def make_substitutions(self, substitutions):
        """

        @type substitutions: list of VariantTypeNode
        """
        self._load_xml()
        return self._xml.format(*[n.xml() for n in substitutions])

    def node(self):
        return self._template_node


def probe_record_header(buf, offset, chunk, chunk_end):
    """
    Validate the record header found at a single layout-derived offset.

    This never searches for the record magic.  `offset` must be reached
    through the file's own structure: the chunk data origin (chunk start +
    0x200) for the first record, or the previous record's offset plus its
    declared size for subsequent records.  A `0x2a2a` dword appearing inside a
    payload is therefore never mistaken for a boundary.

    The checks, each independently reported:
      1. the full 0x18-byte header must be readable and lie inside the chunk;
      2. the leading dword must equal the record magic `0x2a2a`;
      3. the declared size must fit a header and not cross the chunk end;
      4. the size dword must be repeated in the trailing copy at `size - 4`.

    Args:
      buf: The memory mapped file contents.
      offset: Absolute file offset of the candidate record header.
      chunk: The owning `ChunkHeader`, attached to recovered records.
      chunk_end: Absolute offset one past the end of this chunk.

    Returns:
      RecordResult: `VALID` carries a `Record`; otherwise it carries the
        specific failing check and the caller must stop walking this chunk.
    """
    chunk_start = chunk.offset()
    if offset + RECORD_HEADER_SIZE > chunk_end or offset < chunk_start:
        return RecordResult(
            RecordStatus.OUT_OF_BOUNDS,
            offset,
            error="record header at {} is not fully inside chunk [{}, {})".format(
                hex(offset), hex(chunk_start), hex(chunk_end)
            ),
        )

    try:
        magic, size, record_num = struct.unpack_from("<IIQ", buf, offset)
    except struct.error as exc:
        return RecordResult(RecordStatus.OUT_OF_BOUNDS, offset, error=str(exc))

    if magic != RECORD_MAGIC:
        return RecordResult(
            RecordStatus.INVALID_MAGIC,
            offset,
            size=size,
            record_num=record_num,
            error="expected record magic 0x{:08x} at {}, found 0x{:08x}".format(
                RECORD_MAGIC, hex(offset), magic
            ),
        )

    if size < RECORD_HEADER_SIZE + 4 or offset + size > chunk_end:
        return RecordResult(
            RecordStatus.INVALID_SIZE,
            offset,
            size=size,
            record_num=record_num,
            error="declared size {} at {} does not fit inside the chunk".format(size, hex(offset)),
        )

    try:
        trailing_size = struct.unpack_from("<I", buf, offset + size - 4)[0]
    except struct.error as exc:
        return RecordResult(
            RecordStatus.INVALID_SIZE, offset, size=size, record_num=record_num, error=str(exc)
        )

    if trailing_size != size:
        return RecordResult(
            RecordStatus.SIZE_MISMATCH,
            offset,
            size=size,
            record_num=record_num,
            error="leading size {} does not match trailing size copy {}".format(size, trailing_size),
        )

    record = Record(buf, offset, chunk)
    return RecordResult(RecordStatus.VALID, offset, size=size, record_num=record_num, record=record)


class ChunkHeader(Block):
    def __init__(self, buf, offset):
        logger.debug("CHUNK HEADER at {}.".format(hex(offset)))
        super(ChunkHeader, self).__init__(buf, offset)
        self._strings = None
        self._templates = None

        self.declare_field("string", "magic", 0x0, length=8)
        self.declare_field("qword", "file_first_record_number")
        self.declare_field("qword", "file_last_record_number")
        self.declare_field("qword", "log_first_record_number")
        self.declare_field("qword", "log_last_record_number")
        self.declare_field("dword", "header_size")
        self.declare_field("dword", "last_record_offset")
        self.declare_field("dword", "next_record_offset")
        self.declare_field("dword", "data_checksum")
        self.declare_field("binary", "unused", length=0x44)
        self.declare_field("dword", "header_checksum")

    def __repr__(self):
        return "ChunkHeader(buf={!r}, offset={!r})".format(self._buf, self._offset)

    def __str__(self):
        return "ChunkHeader(offset={})".format(hex(self._offset))

    def check_magic(self):
        """
        @return A boolean that indicates if the first eight bytes of
          the ChunkHeader match the expected magic value.
        """
        try:
            return self.magic() == "ElfChnk\x00"
        except UnicodeDecodeError:
            return False

    def is_empty(self):
        """
        Whether this chunk slot was never allocated and is zero filled.

        Empty chunks appear past the active chunk count.  They are distinct
        from damaged chunks: there is nothing to recover and no bytes were
        lost, so they must not be reported as skipped ranges.
        """
        try:
            return self.unpack_binary(0x0, 8) == b"\x00" * 8
        except OverrunBufferException:
            return False

    def calculate_header_checksum(self):
        """
        @return A integer in the range of an unsigned int that
          is the calculated CRC32 checksum of the ChunkHeader fields.
        """
        data = self.unpack_binary(0x0, 0x78)
        data += self.unpack_binary(0x80, 0x180)
        return binascii.crc32(data) & 0xFFFFFFFF

    def calculate_data_checksum(self):
        """
        @return A integer in the range of an unsigned int that
          is the calculated CRC32 checksum of the Chunk data.
        """
        data = self.unpack_binary(0x200, self.next_record_offset() - 0x200)
        return binascii.crc32(data) & 0xFFFFFFFF

    def verify(self):
        """
        @return A boolean that indicates that the FileHeader
          successfully passes a set of heuristic checks that
          all EVTX ChunkHeaders should pass.
        """
        return (
            self.check_magic()
            and self.calculate_header_checksum() == self.header_checksum()
            and self.calculate_data_checksum() == self.data_checksum()
        )

    def _load_strings(self):
        if self._strings is None:
            self._strings = {}
        for i in range(64):
            ofs = self.unpack_dword(0x80 + (i * 4))
            while ofs > 0:
                string_node = self.add_string(ofs)
                ofs = string_node.next_offset()

    def strings(self):
        """
        @return A dict(offset --> NameStringNode)
        """
        if not self._strings:
            self._load_strings()
        return self._strings

    def add_string(self, offset, parent=None):
        """
        @param offset An integer offset that is relative to the start of
          this chunk.
        @param parent (Optional) The parent of the newly created
           NameStringNode instance. (Default: this chunk).
        @return None
        """
        if self._strings is None:
            self._load_strings()
        string_node = NameStringNode(self._buf, self._offset + offset, self, parent or self)
        self._strings[offset] = string_node
        return string_node

    def _load_templates(self):
        """
        @return None
        """
        if self._templates is None:
            self._templates = {}
        for i in range(32):
            ofs = self.unpack_dword(0x180 + (i * 4))
            while ofs > 0:
                # unclear why these are found before the offset
                # this is a direct port from A.S.'s code
                token = self.unpack_byte(ofs - 10)
                pointer = self.unpack_dword(ofs - 4)
                if token != 0x0C or pointer != ofs:
                    logger.warning("Unexpected token encountered")
                    ofs = 0
                    continue
                template = self.add_template(ofs)
                ofs = template.next_offset()

    def add_template(self, offset, parent=None):
        """
        @param offset An integer which contains the chunk-relative offset
           to a template to load into this Chunk.
        @param parent (Optional) The parent of the newly created
           TemplateNode instance. (Default: this chunk).
        @return Newly added TemplateNode instance.
        """
        if self._templates is None:
            self._load_templates()

        node = TemplateNode(self._buf, self._offset + offset, self, parent or self)
        self._templates[offset] = node
        return node

    def templates(self):
        """
        @return A dict(offset --> Template) of all encountered
          templates in this Chunk.
        """
        if not self._templates:
            self._load_templates()
        return self._templates

    def first_record(self):
        return Record(self._buf, self._offset + 0x200, self)

    def records(self):
        try:
            record = self.first_record()
        except InvalidRecordException:
            return
        while record._offset < self._offset + self.next_record_offset() and record.length() > 0:
            yield record
            try:
                record = Record(self._buf, record._offset + record.length(), self)
            except InvalidRecordException:
                return

    def header_checksum_valid(self):
        """
        Whether the stored chunk header CRC matches a fresh calculation.

        Returns None if the calculation cannot be performed (for example the
        chunk is truncated before the end of its header).
        """
        try:
            return self.calculate_header_checksum() == self.header_checksum()
        except OverrunBufferException:
            return None

    def data_checksum_valid(self):
        """
        Whether the stored CRC over the chunk's record data matches.

        The data CRC covers the region from 0x200 up to
        `next_record_offset`.  Returns None if it cannot be calculated.
        """
        try:
            if self.next_record_offset() < CHUNK_HEADER_SIZE:
                return None
            return self.calculate_data_checksum() == self.data_checksum()
        except OverrunBufferException:
            return None

    def recover(self):
        """
        Offline, best-effort recovery of the records in this chunk.

        Record boundaries are taken only from the chunk data origin and each
        prior record's declared size; the bytes are never scanned for the
        record magic.  As soon as a header probe fails, the walk stops and the
        remainder of the chunk's data area is reported as one skipped raw
        range against the original file.  A later chunk is unaffected, because
        its location is the fixed chunk geometry rather than anything found
        inside this chunk.

        Returns:
          ChunkRecoveryResult: the valid records in order, the per-boundary
            probe statuses, the header/data checksum outcomes, and the skipped
            raw byte range.
        """
        result = ChunkRecoveryResult(
            chunk_index=(self._offset - FILE_HEADER_SIZE) // CHUNK_SIZE,
            chunk_offset=self._offset,
            status=ChunkStatus.VALID,
        )

        if self.is_empty():
            # A never-allocated chunk is zero filled.  It holds no records and
            # no data was lost, so it contributes no skipped range.
            result.status = ChunkStatus.EMPTY
            return result

        if not self.check_magic():
            result.status = ChunkStatus.INVALID_MAGIC
            result.error = "chunk at {} does not begin with the ElfChnk magic".format(hex(self._offset))
            result.skipped_range = (self._offset + CHUNK_HEADER_SIZE, self._offset + CHUNK_SIZE)
            return result

        result.header_checksum_ok = self.header_checksum_valid()
        if result.header_checksum_ok is False:
            result.status = ChunkStatus.HEADER_CHECKSUM_FAILED

        result.data_checksum_ok = self.data_checksum_valid()
        if result.data_checksum_ok is False and result.status == ChunkStatus.VALID:
            result.status = ChunkStatus.DATA_CHECKSUM_FAILED

        # Trust the declared end of the record area only while it is sane;
        # otherwise fall back to probing through the whole 64KiB data area.
        try:
            next_record_offset = self.next_record_offset()
        except OverrunBufferException:
            next_record_offset = None
        if next_record_offset is not None and CHUNK_HEADER_SIZE <= next_record_offset <= CHUNK_SIZE:
            walk_end = self._offset + next_record_offset
        else:
            walk_end = self._offset + CHUNK_SIZE

        data_start = self._offset + CHUNK_HEADER_SIZE
        data_end = self._offset + CHUNK_SIZE
        boundary = data_start
        while boundary + RECORD_HEADER_SIZE <= walk_end:
            probe = probe_record_header(self._buf, boundary, self, data_end)
            if not probe.is_valid:
                result.records.append(probe)
                result.skipped_range = (boundary, data_end)
                return result

            result.records.append(probe)
            boundary = probe.offset + probe.size

        return result


class Record(Block):
    def __init__(self, buf, offset, chunk):
        logger.debug("Record at {}.".format(hex(offset)))
        super(Record, self).__init__(buf, offset)
        self._chunk = chunk

        self.declare_field("dword", "magic", 0x0)  # 0x00002a2a
        self.declare_field("dword", "size")
        self.declare_field("qword", "record_num")
        self.declare_field("filetime", "timestamp")

        if self.size() > 0x10000:
            raise InvalidRecordException()

        self.declare_field("dword", "size2", self.size() - 4)

    def __repr__(self):
        return "Record(buf={!r}, offset={!r})".format(self._buf, self._offset)

    def __str__(self):
        return "Record(offset={})".format(hex(self._offset))

    def root(self):
        return RootNode(self._buf, self._offset + 0x18, self._chunk, self)

    def length(self):
        return self.size()

    def verify(self):
        return self.size() == self.size2()

    def data(self):
        """
        Return the raw data block which makes up this record as a bytestring.

        @rtype str
        @return A string that is a copy of the buffer that makes
          up this record.
        """
        return self._buf[self.offset() : self.offset() + self.size()]

    def xml(self):
        """
        render the record into XML.
        does not include the xml declaration header.

        Returns:
          str: the rendered xml document.
        """
        return e_views.evtx_record_xml_view(self)

    def lxml(self):
        """
        render the record into a lxml document.
        this is useful for querying data from the record using xpath, etc.

        note: lxml must be installed.

        Returns:
          lxml.etree.ElementTree: the rendered and parsed xml document.

        Raises:
          ImportError: if lxml is not installed.
        """
        import lxml.etree

        return lxml.etree.fromstring((e_views.XML_HEADER + self.xml()).encode("utf-8"))
