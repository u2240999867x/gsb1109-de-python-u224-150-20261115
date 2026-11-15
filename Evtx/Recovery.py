#!/usr/bin/python
#    This file is part of python-evtx.
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
"""
Salvage of event records from truncated EVTX files.

A truncated EVTX keeps the header it was last written with, including the
record counts it declares.  Those declarations may be stale, may run past the
bytes that reached disk, and may disagree with one another, because the file
header and the chunk headers are updated by separate writes.  A declaration is
therefore reported as evidence, never used as a record list.

The bytes actually present are walked instead.  Each candidate record is
checked against its own self-describing size fields, and each candidate chunk
against its magic.  From that walk the report derives:

  * the verified records, each marked renderable or not,
  * the end of the last complete record and of the last complete chunk,
  * the byte ranges present but not accounted for, and the declared range
    that lies beyond the end of the file,
  * what each header claimed, kept separate so a silent declaration does not
    hide records that are really there.

An incomplete record at the cut is reported as incomplete and contributes no
record and no size; it is never padded or extrapolated to a whole record.
"""
from __future__ import absolute_import

import binascii
import datetime
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import Evtx.Evtx as evtx

from .Views import XML_HEADER

FILE_MAGIC = b"ElfFile\x00"
CHUNK_MAGIC = b"ElfChnk\x00"
FILE_HEADER_SIZE = 0x1000
CHUNK_SIZE = 0x10000
CHUNK_HEADER_SIZE = 0x200
RECORD_MAGIC = 0x00002A2A
RECORD_HEADER_SIZE = 0x18
RECORD_TRAILER_SIZE = 0x4
# Every BinXML fragment begins with this root-node token/version/flags tuple.
BINXML_SIGNATURE = b"\x0f\x01\x01\x00"


def _crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF


@dataclass
class FileHeaderFacts:
    """What the file header declared, and whether it is self-consistent."""

    present: bool
    magic_ok: bool
    checksum_ok: bool
    dirty: bool
    full: bool
    declared_chunk_count: int
    declared_next_record_number: int
    oldest_chunk: int
    current_chunk: int


@dataclass
class Truncation:
    """An incomplete structure at the end of the physically available bytes."""

    offset: int
    declared_size: int
    available: int
    kind: str
    detail: str

    @property
    def missing(self):
        return max(0, self.declared_size - self.available)

    @property
    def declared_end(self):
        return self.offset + self.declared_size if self.declared_size else self.offset


@dataclass
class RecordFacts:
    """A record whose framing was verified against the bytes present."""

    offset: int
    size: int
    record_num: int
    chunk_number: int
    chunk_relative_offset: int
    timestamp: datetime.datetime
    renderable: bool
    render_error: str
    untrusted_fields: List[str]
    _buf: bytes = field(repr=False, default=b"")
    _record: object = field(repr=False, default=None)

    @property
    def end(self):
        return self.offset + self.size

    def xml(self):
        return self._record.xml()


@dataclass
class ChunkFacts:
    """What one chunk's header declared versus what its bytes contain."""

    number: int
    offset: int
    available_size: int
    truncated: bool
    magic_ok: bool
    header_checksum_ok: Optional[bool]
    data_checksum_ok: Optional[bool]
    data_checksum_coverable: bool
    declared_file_first: Optional[int]
    declared_file_last: Optional[int]
    declared_log_first: Optional[int]
    declared_log_last: Optional[int]
    declared_next_record_offset: Optional[int]
    record_count: int
    first_record_num: Optional[int]
    last_record_num: Optional[int]
    truncation: Optional[Truncation]
    declared: bool = True

    @property
    def declared_record_count(self):
        if self.declared_log_first is None or self.declared_log_last is None:
            return 0
        return max(0, self.declared_log_last - self.declared_log_first + 1)


@dataclass
class Coverage:
    """A byte range present in the file that no verified structure claims."""

    start: int
    end: int
    reason: str
    inside_chunk: Optional[int]

    @property
    def length(self):
        return self.end - self.start


class RecoveryReport:
    """The evidence recovered from one possibly truncated EVTX file."""

    def __init__(self, path, buf):
        self.path = path
        self._buf = buf
        self.file_size = len(buf)
        self.file_header = None  # type: Optional[FileHeaderFacts]
        self.chunks = []  # type: List[ChunkFacts]
        self.records = []  # type: List[RecordFacts]
        self.truncations = []  # type: List[Truncation]
        self.declared_region_end = 0

    # -- record views -----------------------------------------------------

    @property
    def verified_records(self):
        return [r for r in self.records if not r.untrusted_fields]

    @property
    def untrusted_records(self):
        return [r for r in self.records if r.untrusted_fields]

    @property
    def renderable_records(self):
        return [r for r in self.records if r.renderable]

    @property
    def unrenderable_records(self):
        return [r for r in self.records if not r.renderable]

    @property
    def terminal_truncation(self):
        """The incomplete structure the cut went through, if any."""
        return self.truncations[-1] if self.truncations else None

    @property
    def last_complete_record(self):
        if not self.records:
            return None
        return max(self.records, key=lambda r: r.end)

    @property
    def last_complete_chunk(self):
        complete = [c for c in self.chunks if not c.truncated and c.magic_ok]
        if not complete:
            return None
        return max(complete, key=lambda c: c.offset)

    # -- declaration views ------------------------------------------------

    def declarations(self) -> List[Tuple[str, Optional[int], Optional[int], str]]:
        """(source, first, last, note) for each place a count is declared.

        A header that declares zero is reported as empty rather than omitted,
        because "declared nothing" and "declared something" are different
        evidence and must not look alike in the output.
        """
        out = []
        fh = self.file_header
        if fh is not None and fh.present:
            note = "dirty" if fh.dirty else "clean"
            if not fh.declared_next_record_number:
                note += "; declares no records"
            out.append(
                (
                    "file header next_record_number",
                    None,
                    fh.declared_next_record_number - 1 if fh.declared_next_record_number else None,
                    note,
                )
            )
        declared = [c for c in self.chunks if c.declared_log_first is not None]
        if declared:
            populated = [c for c in declared if c.declared_log_last]
            empty = len(declared) - len(populated)
            if populated:
                firsts = [c.declared_log_first for c in populated]
                lasts = [c.declared_log_last for c in populated]
                note = "{} chunk headers".format(len(populated))
                if empty:
                    note += "; {} declare an empty span".format(empty)
                out.append(("chunk headers log first/last", min(firsts), max(lasts), note))
            else:
                out.append(("chunk headers log first/last", None, None, "{} declare an empty span".format(empty)))
        return out

    def declaration_conflicts(self):
        """Where the headers disagree with each other or with the bytes found."""
        out = []
        fh = self.file_header
        if fh is None or not fh.present:
            return out
        declared_by_file_header = fh.declared_next_record_number - 1 if fh.declared_next_record_number else 0
        declared_by_chunks = [
            c.declared_log_last
            for c in self.chunks
            if c.declared_log_last is not None and c.declared_log_first is not None
        ]
        if declared_by_chunks and declared_by_file_header:
            highest = max(declared_by_chunks)
            if declared_by_file_header != highest:
                out.append(
                    "file header declares last record {} but chunk headers declare {} "
                    "(difference {}{})".format(
                        declared_by_file_header,
                        highest,
                        abs(highest - declared_by_file_header),
                        "; header is dirty" if fh.dirty else "",
                    )
                )
        if fh.declared_chunk_count == 0 and fh.current_chunk:
            out.append(
                "file header declares 0 chunks but names chunk {} as current".format(fh.current_chunk)
            )
        if self.records:
            found_first = min(r.record_num for r in self.records)
            found_last = max(r.record_num for r in self.records)
            if found_first < fh.oldest_chunk and fh.oldest_chunk:
                out.append(
                    "verified record {} predates the declared oldest chunk {}".format(
                        found_first, fh.oldest_chunk
                    )
                )
            declared_chunks = fh.declared_chunk_count
            if declared_chunks and len(self.chunks) > declared_chunks:
                out.append(
                    "{} chunk headers present but the file header declares {}".format(len(self.chunks), declared_chunks)
                )
            for source, first, last, _ in self.declarations():
                if last is not None and found_last > last:
                    out.append(
                        "verified record {} is past what {} declares ({})".format(found_last, source, last)
                    )
        return out

    # -- byte coverage ----------------------------------------------------

    def undeclared_chunks(self):
        """Chunks with a valid header that the file header never declared.

        A chunk that was written but never recorded in the file header is
        still real: its header checksums prove the chunk is intact.  It is
        reported rather than folded into the declared chunk count.
        """
        return [c for c in self.chunks if not c.declared and c.magic_ok]

    def coverage(self) -> List[Coverage]:
        """Byte ranges present in the file that hold no verified structure.

        The file header, each verified chunk header, and each verified record
        are accounted for.  Everything else is reported: chunk slack,
        unreadable fragments, and the bytes past the last record.
        """
        covered = []  # type: List[Tuple[int, int]]
        if self.file_header is not None and self.file_header.magic_ok:
            covered.append((0, min(FILE_HEADER_SIZE, self.file_size)))
        for chunk in self.chunks:
            if chunk.magic_ok:
                header_end = min(chunk.offset + CHUNK_HEADER_SIZE, self.file_size)
                covered.append((chunk.offset, header_end))
        for record in self.records:
            covered.append((record.offset, record.end))
        covered.sort()

        gaps = []
        position = 0
        for start, end in covered:
            if start > position:
                gaps.append((position, min(start, self.file_size)))
            position = max(position, end)
        if position < self.file_size:
            gaps.append((position, self.file_size))

        out = []
        cutters = [t for t in self.truncations if t.available and t.declared_size]
        for start, end in gaps:
            if end <= start:
                continue
            inside = None
            for chunk in self.chunks:
                if chunk.offset <= start < chunk.offset + chunk.available_size:
                    inside = chunk.number
                    break
            cut = None
            for truncation in cutters:
                if start <= truncation.offset < end:
                    cut = truncation
                    break
            if cut is not None:
                reason = "partial bytes of the cut {} at {}; no record is counted from these".format(
                    cut.kind, hex(cut.offset)
                )
            elif inside is not None:
                reason = "chunk {} slack (bytes past the last verified record)".format(inside)
            elif start >= FILE_HEADER_SIZE:
                reason = "past the last verified structure"
            else:
                reason = "file header missing or invalid"
            out.append(Coverage(start, end, reason, inside))
        return out

    def missing_beyond_eof(self) -> List[Coverage]:
        """Ranges the data declares but the file on disk never received.

        The end of what is missing is what the *most specific* declaration
        says the data should reach; the start is the end of the file.  The
        file header and the cut structure describe the same missing tail, so
        they are merged here instead of being reported twice.
        """
        candidates = []
        if self.declared_region_end > self.file_size:
            candidates.append(self.declared_region_end)
        truncation = self.terminal_truncation
        if truncation is not None and truncation.declared_size:
            candidates.append(truncation.declared_end)
        if not candidates:
            return []
        declared_end = max(candidates)
        if declared_end <= self.file_size:
            return []
        reason = "declared by the file header"
        if truncation is not None and truncation.declared_size:
            reason = "declared by the cut {} at {}".format(truncation.kind, hex(truncation.offset))
        return [Coverage(self.file_size, declared_end, reason, None)]


# -- header readers -------------------------------------------------------


def _read_file_header(buf):
    size = len(buf)
    if size < 0x80:
        return FileHeaderFacts(False, False, False, False, False, 0, 0, 0, 0)

    magic_ok = buf[0:8] == FILE_MAGIC
    oldest_chunk, current_chunk, next_record_number = struct.unpack_from("<QQQ", buf, 0x8)
    _, _, _, header_chunk_size, chunk_count = struct.unpack_from("<IHHHH", buf, 0x20)
    flags, checksum = struct.unpack_from("<II", buf, 0x78)
    return FileHeaderFacts(
        present=True,
        magic_ok=magic_ok,
        checksum_ok=_crc32(buf[0:0x78]) == checksum,
        dirty=bool(flags & 0x1),
        full=bool(flags & 0x2),
        declared_chunk_count=chunk_count,
        declared_next_record_number=next_record_number,
        oldest_chunk=oldest_chunk,
        current_chunk=current_chunk,
    )


def _declared_chunk_capacity(file_header):
    """Chunk slots the declarations can refer to, independent of file size.

    `chunk_count` is the number of chunks the header claims to hold and is
    authoritative.  `current_chunk` is only used when no count is declared,
    since a stale count of zero must not be inflated by a stale index.
    """
    if file_header.declared_chunk_count:
        return file_header.declared_chunk_count
    if file_header.declared_chunk_count == 0 and file_header.current_chunk and not file_header.magic_ok:
        return file_header.current_chunk + 1
    return 0


# -- record scanning ------------------------------------------------------


def _scan_record(buf, offset, limit):
    """Check the record framing at offset within [offset, limit).

    Returns (size, truncated_declared_size):
      (size, 0) the whole record is present and its trailing size agrees,
      (0, declared) the framing is plausible but the bytes are cut,
      (0, 0) the bytes do not look like a record at all.
    """
    if offset + RECORD_HEADER_SIZE > limit:
        return 0, 0
    magic, size = struct.unpack_from("<II", buf, offset)
    if magic != RECORD_MAGIC:
        return 0, 0
    if size < RECORD_HEADER_SIZE + RECORD_TRAILER_SIZE or size > CHUNK_SIZE or size % 8 != 0:
        return 0, 0
    if offset + size > limit:
        return 0, size
    if struct.unpack_from("<I", buf, offset + size - RECORD_TRAILER_SIZE)[0] != size:
        return 0, 0
    return size, 0


def _record_untrusted(view, buf):
    """Fields of a framed record that its own bytes do not support.

    Framing (magic, size, size2) is already established by the caller.  This
    adds the two things a readable record must also have: a BinXML body and a
    non-zero timestamp.
    """
    problems = []
    if not view.verify():
        problems.append("size2 != size")
    if buf[view.offset() + RECORD_HEADER_SIZE : view.offset() + RECORD_HEADER_SIZE + 4] != BINXML_SIGNATURE:
        problems.append("BinXML root signature missing")
    if view.timestamp() == datetime.datetime.min:
        problems.append("timestamp is zero or unreadable")
    return problems


def _record_facts(buf, chunk_view, offset, size, chunk_number, chunk_offset):
    view = evtx.Record(buf, offset, chunk_view)
    untrusted = _record_untrusted(view, buf)
    try:
        view.xml()
        renderable, render_error = True, ""
    except Exception as ex:  # a corrupt BinXML body is evidence, not a crash
        renderable, render_error = False, "{}: {}".format(type(ex).__name__, ex)
    return RecordFacts(
        offset=offset,
        size=size,
        record_num=view.record_num(),
        chunk_number=chunk_number,
        chunk_relative_offset=offset - chunk_offset,
        timestamp=view.timestamp(),
        renderable=renderable,
        render_error=render_error,
        untrusted_fields=untrusted,
        _buf=buf,
        _record=view,
    )


def _walk_chunk(buf, index, offset, available):
    """Read one chunk slot: header fields, records, and where it stops."""
    facts = ChunkFacts(
        number=index,
        offset=offset,
        available_size=available,
        truncated=False,
        magic_ok=False,
        header_checksum_ok=None,
        data_checksum_ok=None,
        data_checksum_coverable=False,
        declared_file_first=None,
        declared_file_last=None,
        declared_log_first=None,
        declared_log_last=None,
        declared_next_record_offset=None,
        record_count=0,
        first_record_num=None,
        last_record_num=None,
        truncation=None,
    )
    records = []

    if available < 8 or buf[offset : offset + 8] != CHUNK_MAGIC:
        return facts, records
    facts.magic_ok = True

    if available < CHUNK_HEADER_SIZE:
        facts.truncated = True
        facts.truncation = Truncation(offset, CHUNK_HEADER_SIZE, available, "chunk header", "chunk header cut")
        if available >= 0x38:
            _read_chunk_fields(facts, buf, offset, available)
        return facts, records

    _read_chunk_fields(facts, buf, offset, CHUNK_HEADER_SIZE)
    facts.header_checksum_ok = (
        _crc32(buf[offset : offset + 0x78] + buf[offset + 0x80 : offset + CHUNK_HEADER_SIZE])
        == struct.unpack_from("<I", buf, offset + 0x7C)[0]
    )

    limit = offset + min(available, CHUNK_SIZE)
    data_end = offset + facts.declared_next_record_offset
    declared_end = facts.declared_next_record_offset
    if declared_end is not None and CHUNK_HEADER_SIZE <= declared_end <= limit - offset:
        facts.data_checksum_coverable = True
        facts.data_checksum_ok = (
            _crc32(buf[offset + CHUNK_HEADER_SIZE : data_end])
            == struct.unpack_from("<I", buf, offset + 0x34)[0]
        )

    chunk_view = evtx.ChunkHeader(buf, offset)

    position = offset + CHUNK_HEADER_SIZE
    while position < limit:
        size, declared = _scan_record(buf, position, limit)
        if declared:
            facts.truncated = True
            facts.truncation = Truncation(position, declared, limit - position, "record", "record cut after framing")
            break
        if not size:
            if limit - position >= 8 and struct.unpack_from("<I", buf, position)[0] == RECORD_MAGIC:
                facts.truncation = Truncation(position, 0, limit - position, "record", "record magic with invalid size")
            elif limit < offset + CHUNK_SIZE:
                facts.truncated = True
                facts.truncation = Truncation(
                    position, CHUNK_SIZE - (position - offset), limit - position, "chunk", "chunk cut before its end"
                )
            break
        record = _record_facts(buf, chunk_view, position, size, index, offset)
        records.append(record)
        facts.record_count += 1
        if facts.first_record_num is None:
            facts.first_record_num = record.record_num
        facts.last_record_num = record.record_num
        position += size

    if limit < offset + CHUNK_SIZE and facts.truncation is None:
        # The chunk itself is short but the cut happened to fall on a clean
        # record boundary; the missing bytes are the rest of the chunk.
        facts.truncated = True
        facts.truncation = Truncation(
            position, CHUNK_SIZE - (position - offset), limit - position, "chunk", "chunk bytes cut"
        )
    return facts, records


def _read_chunk_fields(facts, buf, offset, length):
    if length >= 0x28:
        (
            facts.declared_file_first,
            facts.declared_file_last,
            facts.declared_log_first,
            facts.declared_log_last,
        ) = struct.unpack_from("<QQQQ", buf, offset + 0x8)
    if length >= 0x34:
        facts.declared_next_record_offset = struct.unpack_from("<I", buf, offset + 0x30)[0]


def renderable_xml(report):
    """XML document holding every renderable verified record, in file order.

    A record whose BinXML cannot be decoded is left out rather than given a
    placeholder, so the document contains only events that were really read.
    """
    parts = [XML_HEADER, "<Events>\n"]
    for record in report.records:
        if record.renderable:
            parts.append(record.xml())
            parts.append("\n")
    parts.append("</Events>\n")
    return "".join(parts)


def analyze(path):
    """Walk a possibly truncated EVTX file and return a RecoveryReport."""
    with open(path, "rb") as f:
        buf = f.read()
    report = RecoveryReport(path, buf)
    report.file_header = _read_file_header(buf)
    report.declared_region_end = FILE_HEADER_SIZE + _declared_chunk_capacity(report.file_header) * CHUNK_SIZE

    capacity = _declared_chunk_capacity(report.file_header)
    index = 0
    while FILE_HEADER_SIZE + index * CHUNK_SIZE < len(buf):
        offset = FILE_HEADER_SIZE + index * CHUNK_SIZE
        available = min(CHUNK_SIZE, len(buf) - offset)
        facts, records = _walk_chunk(buf, index, offset, available)
        # A slot with valid chunk magic holds evidence whether or not the file
        # header counted it; only slots that never became a chunk are skipped.
        if facts.magic_ok:
            facts.declared = index < capacity
            report.chunks.append(facts)
            report.records.extend(records)
        index += 1

    # Only a structure that runs into the end of the file is a truncation.
    # A damaged record in the middle of a chunk stops that chunk's walk but
    # says nothing about where the file was cut.
    for chunk in report.chunks:
        if chunk.truncation is not None and chunk.truncation.declared_end >= report.file_size:
            report.truncations.append(chunk.truncation)
    return report
