"""Resumable, generation-aware collection of EVTX records.

A checkpoint that only stores the largest ``EventRecordID`` cannot safely
resume an EVTX feed.  Windows replaces a log at the same path (rotation) or
clears it in place (a new file generation), and records recovered into an old
file can carry IDs below the previous maximum.  A maximum-only checkpoint
therefore either skips new rows or asks the sink to store the same event twice.

This module checkpoints three related things for every file generation:

* a generation identifier derived from the file's system identity, so appends
  and circular chunk reuse stay in one generation while a replacement or
  in-place rebuild starts a new one;
* the physical locator of the first and last record in the verified prefix; and
* a ledger of every delivered record's exact bytes and original locator.

A chunk is trusted only when its header and data checksums verify and every
record exactly fills the used region.  The verified frontier never crosses the
first chunk that fails verification.  Delivery is decided by record content
plus a per-generation replay budget, so an unchanged re-read emits nothing, a
new record with a lower record ID is still emitted, a record that shifted
because older history was inserted is not emitted again, and a different
record written over the same physical location is a new event.  States for
older generations are retained, so an archived file that later receives
backfilled history can still contribute the events that are new to it.
"""

from __future__ import annotations

import binascii
import datetime
import enum
import hashlib
import json
import mmap
import os
import struct
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .BinaryParser import ParseException
from .Evtx import ChunkHeader, FileHeader, InvalidRecordException, Record

CHECKPOINT_VERSION = 1
CHUNK_SIZE = 0x10000
FIRST_CHUNK_OFFSET = 0x1000
RECORD_DATA_OFFSET = 0x200
MIN_RECORD_SIZE = 0x1C
GENERATION_PREFIX = "evtx-generation-v1:"
DEFAULT_HISTORY_LIMIT = 16


class CollectorError(Exception):
    """Raised when a file cannot be collected safely."""


class DeliveryError(CollectorError):
    """Raised when the record sink fails while a record is being delivered."""


class ChangeKind(enum.Enum):
    """How the file changed since the last scan of its generation."""

    NEW = "new"
    APPENDED = "appended"
    UNCHANGED = "unchanged"
    TRUNCATED = "truncated"
    BLOCKED = "blocked"
    ROTATED = "rotated"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RecordLocator:
    """The physical location and identity of one EVTX record."""

    chunk_offset: int
    record_offset: int
    record_size: int
    record_number: int
    sha256: str

    @classmethod
    def from_record(cls, record: Record) -> "RecordLocator":
        return cls(
            chunk_offset=record._chunk.offset(),
            record_offset=record.offset(),
            record_size=record.size(),
            record_number=record.record_num(),
            sha256=hashlib.sha256(record.data()).hexdigest(),
        )

    def end_offset(self) -> int:
        return self.record_offset + self.record_size

    def to_dict(self) -> dict:
        return {
            "chunk_offset": self.chunk_offset,
            "record_offset": self.record_offset,
            "record_size": self.record_size,
            "record_number": self.record_number,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RecordLocator":
        return cls(
            chunk_offset=int(data["chunk_offset"]),
            record_offset=int(data["record_offset"]),
            record_size=int(data["record_size"]),
            record_number=int(data["record_number"]),
            sha256=str(data["sha256"]),
        )


@dataclass
class ChunkBarrier:
    """The first chunk or record that prevented the scan from advancing."""

    offset: int
    reason: str
    chunk_offset: Optional[int] = None
    chunk_index: Optional[int] = None
    record_offset: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "reason": self.reason,
            "chunk_offset": self.chunk_offset,
            "chunk_index": self.chunk_index,
            "record_offset": self.record_offset,
        }


@dataclass
class CollectedEvent:
    """A record delivered by the collector.

    Raw ``data`` is retained because a damaged binary XML template does not
    invalidate the physical record.  ``xml`` is ``None`` when rendering failed
    and ``xml_error`` carries the parser message.
    """

    record_number: int
    timestamp: Optional[str]
    data: bytes
    locator: RecordLocator
    xml: Optional[str] = None
    xml_error: Optional[str] = None
    historical: bool = False

    def to_dict(self, include_data: bool = False, include_xml: bool = True) -> dict:
        out = {
            "record_number": self.record_number,
            "timestamp": self.timestamp,
            "historical": self.historical,
            "locator": self.locator.to_dict(),
        }
        if include_xml:
            out["xml"] = self.xml
            if self.xml_error is not None:
                out["xml_error"] = self.xml_error
        if include_data:
            out["data"] = self.data.hex()
        return out


@dataclass
class DeliveredRecord:
    """The identity and original physical location of a delivered record."""

    sha256: str
    record_number: int
    chunk_offset: int
    record_offset: int
    record_size: int

    @classmethod
    def from_locator(cls, locator: RecordLocator) -> "DeliveredRecord":
        return cls(
            sha256=locator.sha256,
            record_number=locator.record_number,
            chunk_offset=locator.chunk_offset,
            record_offset=locator.record_offset,
            record_size=locator.record_size,
        )

    def to_list(self) -> List[int]:
        return [self.record_number, self.chunk_offset, self.record_offset, self.record_size]

    def to_dict(self) -> dict:
        out = {"sha256": self.sha256}
        out.update(dict(zip(("record_number", "chunk_offset", "record_offset", "record_size"), self.to_list())))
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "DeliveredRecord":
        return cls(
            sha256=str(data["sha256"]),
            record_number=int(data["record_number"]),
            chunk_offset=int(data["chunk_offset"]),
            record_offset=int(data["record_offset"]),
            record_size=int(data["record_size"]),
        )


@dataclass
class GenerationState:
    """Checkpoint state for one logical file generation.

    ``delivered`` is keyed by record content (SHA-256) rather than by chunk or
    offset, because inserting history before a record or recycling a chunk can
    move it while leaving the event identical.  Each entry keeps a list of
    original physical locators so the checkpoint still says where every
    delivered record came from.  The list also makes duplicate identical
    records distinct: each physical occurrence consumes one delivered entry,
    and an extra occurrence is emitted as a new event.
    """

    generation: str
    file_size: int = 0
    chunks_verified: int = 0
    verified_through: int = FIRST_CHUNK_OFFSET
    first_record: Optional[RecordLocator] = None
    last_record: Optional[RecordLocator] = None
    delivered: Dict[str, List[DeliveredRecord]] = field(default_factory=dict)
    chunk_digests: Dict[int, str] = field(default_factory=dict)
    delivered_count: int = 0
    max_record_number: Optional[int] = None
    updated_at: Optional[str] = None

    def replay_budget(self) -> Dict[str, int]:
        """Return how many deliveries of each content are already accounted for."""

        return {digest: len(records) for digest, records in self.delivered.items() if records}

    def take_replay(self, locator: RecordLocator, budget: Dict[str, int]) -> bool:
        """Consume one delivered occurrence of this content, if available."""

        remaining = budget.get(locator.sha256, 0)
        if remaining <= 0:
            return False
        budget[locator.sha256] = remaining - 1
        return True

    def mark_delivered(self, locator: RecordLocator) -> None:
        """Remember an exact delivered record and its original locator."""

        self.delivered.setdefault(locator.sha256, []).append(DeliveredRecord.from_locator(locator))
        self.delivered_count += 1
        if self.max_record_number is None or locator.record_number > self.max_record_number:
            self.max_record_number = locator.record_number

    def remember_first(self, locator: RecordLocator) -> None:
        if self.first_record is None:
            self.first_record = locator

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "file_size": self.file_size,
            "chunks_verified": self.chunks_verified,
            "verified_through": self.verified_through,
            "first_record": self.first_record.to_dict() if self.first_record is not None else None,
            "last_record": self.last_record.to_dict() if self.last_record is not None else None,
            "delivered": {
                digest: [[record.sha256] + record.to_list() for record in records]
                for digest, records in sorted(self.delivered.items())
            },
            "chunk_digests": {str(offset): digest for offset, digest in sorted(self.chunk_digests.items())},
            "delivered_count": self.delivered_count,
            "max_record_number": self.max_record_number,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationState":
        delivered = {}
        for digest, items in data.get("delivered", {}).items():
            records = []
            for sha, number, chunk, offset, size in items:
                records.append(
                    DeliveredRecord(
                        sha256=str(sha),
                        record_number=int(number),
                        chunk_offset=int(chunk),
                        record_offset=int(offset),
                        record_size=int(size),
                    )
                )
            delivered[str(digest)] = records
        return cls(
            generation=str(data["generation"]),
            file_size=int(data.get("file_size", 0)),
            chunks_verified=int(data.get("chunks_verified", 0)),
            verified_through=int(data.get("verified_through", FIRST_CHUNK_OFFSET)),
            first_record=RecordLocator.from_dict(data["first_record"]) if data.get("first_record") else None,
            last_record=RecordLocator.from_dict(data["last_record"]) if data.get("last_record") else None,
            delivered=delivered,
            chunk_digests={int(offset): str(digest) for offset, digest in data.get("chunk_digests", {}).items()},
            delivered_count=int(data.get("delivered_count", 0)),
            max_record_number=(
                int(data["max_record_number"]) if data.get("max_record_number") is not None else None
            ),
            updated_at=data.get("updated_at"),
        )


@dataclass
class CollectorCheckpoint:
    """A versioned collection checkpoint, keyed by physical file generation."""

    source_path: str = ""
    current_generation: Optional[str] = None
    generations: Dict[str, GenerationState] = field(default_factory=dict)
    aliases: Dict[str, str] = field(default_factory=dict)
    history_limit: int = DEFAULT_HISTORY_LIMIT
    version: int = CHECKPOINT_VERSION
    updated_at: Optional[str] = None

    @classmethod
    def load(cls, path: str) -> "CollectorCheckpoint":
        """Load a checkpoint; a missing file yields empty state."""

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            raise CollectorError("unable to read checkpoint {!r}: {}".format(path, e))

        version = int(data.get("version", 0))
        if version != CHECKPOINT_VERSION:
            raise CollectorError(
                "unsupported checkpoint version {} in {!r} (expected {})".format(version, path, CHECKPOINT_VERSION)
            )

        generations = {}
        for generation, state in data.get("generations", {}).items():
            generations[str(generation)] = GenerationState.from_dict(state)
        return cls(
            source_path=str(data.get("source_path") or ""),
            current_generation=data.get("current_generation"),
            generations=generations,
            aliases={str(alias): str(target) for alias, target in data.get("aliases", {}).items()},
            history_limit=int(data.get("history_limit", DEFAULT_HISTORY_LIMIT)),
            updated_at=data.get("updated_at"),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source_path": self.source_path,
            "current_generation": self.current_generation,
            "history_limit": self.history_limit,
            "generations": {generation: state.to_dict() for generation, state in self.generations.items()},
            "aliases": dict(sorted(self.aliases.items())),
            "updated_at": self.updated_at,
        }

    def bind(self, source_path: str) -> None:
        """Bind this checkpoint to an EVTX file.

        Reusing one checkpoint for a different path is refused rather than
        silently mixing delivery ledgers.  Call :meth:`reset` first when a
        different file should start with fresh state.
        """

        source = os.path.abspath(source_path)
        if self.source_path and os.path.abspath(self.source_path) != source:
            raise CollectorError(
                "checkpoint belongs to {!r}, not {!r}; call reset() to rebind".format(self.source_path, source)
            )
        self.source_path = source

    def save(self, path: str) -> None:
        """Atomically persist this checkpoint to ``path``."""

        if not path:
            raise CollectorError("no checkpoint path configured")
        target = os.path.abspath(path)
        self.updated_at = _now()
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".evtx-checkpoint-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def reset(self) -> None:
        """Forget all delivery state and start a fresh history."""

        self.current_generation = None
        self.generations = {}
        self.aliases = {}
        self.updated_at = None

    def state_for(self, generation: str) -> GenerationState:
        generation = self.aliases.get(generation, generation)
        state = self.generations.get(generation)
        if state is None:
            state = GenerationState(generation=generation)
            self.generations[generation] = state
        return state

    def add_alias(self, alias: str, generation: str) -> None:
        """Remember that one computed generation resolved to another."""

        if alias != generation:
            self.aliases[alias] = generation

    def remember_generation(self, state: GenerationState) -> None:
        """Persist a generation and retire the least recently updated state."""

        state.updated_at = _now()
        self.generations[state.generation] = state
        self.current_generation = state.generation
        self.updated_at = state.updated_at
        if len(self.generations) <= self.history_limit:
            return
        candidates = [g for g in self.generations if g != state.generation]
        candidates.sort(key=lambda g: self.generations[g].updated_at or "")
        retired = set(candidates[: len(self.generations) - self.history_limit])
        for generation in retired:
            del self.generations[generation]
        if retired:
            self.aliases = {alias: target for alias, target in self.aliases.items() if target not in retired}


@dataclass
class CollectionResult:
    """The outcome of one collection pass."""

    change: ChangeKind
    generation: str
    emitted: List[CollectedEvent]
    skipped: int
    checkpoint: CollectorCheckpoint
    barrier: Optional[ChunkBarrier]
    file_size: int
    verified_through: int
    header_verified: bool
    already_known_generation: bool = False

    @property
    def historical_emitted(self) -> int:
        return sum(1 for event in self.emitted if event.historical)

    def to_dict(self, include_events: bool = True, include_data: bool = False) -> dict:
        out = {
            "change": self.change.value,
            "generation": self.generation,
            "emitted": len(self.emitted),
            "historical_emitted": self.historical_emitted,
            "skipped": self.skipped,
            "file_size": self.file_size,
            "verified_through": self.verified_through,
            "header_verified": self.header_verified,
            "already_known_generation": self.already_known_generation,
            "barrier": self.barrier.to_dict() if self.barrier is not None else None,
        }
        if include_events:
            out["events"] = [event.to_dict(include_data=include_data) for event in self.emitted]
        return out


def load_checkpoint(path: str) -> CollectorCheckpoint:
    """Load a checkpoint from ``path`` (a missing file yields empty state)."""

    return CollectorCheckpoint.load(path)


def save_checkpoint(checkpoint: CollectorCheckpoint, path: str) -> None:
    """Atomically save ``checkpoint`` to ``path``."""

    checkpoint.save(path)


def _high_water(state: GenerationState) -> Optional[int]:
    """Return the largest record number previously delivered from this generation.

    A record below this value is still delivered when it is new to the file;
    the collector marks it ``historical`` so a sink can distinguish backfill
    from the live tail.  The value is per generation because a rotated log
    restarts its record numbering, and this value is never used to skip.
    """

    return state.max_record_number


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_timestamp(record: Record) -> Optional[str]:
    try:
        timestamp = record.timestamp()
    except (ParseException, ValueError, OSError):
        return None
    if timestamp == datetime.datetime.min:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
    return timestamp.isoformat()


def _record_xml(record: Record) -> Tuple[Optional[str], Optional[str]]:
    try:
        return record.xml(), None
    except Exception as e:  # a damaged template must not invalidate a verified record
        return None, "{}: {}".format(type(e).__name__, e)


def _looks_like_record(buf, chunk: ChunkHeader, offset: int, next_offset: int) -> Optional[Record]:
    """Parse one record and validate that it fits strictly in the chunk."""

    if offset + MIN_RECORD_SIZE > next_offset:
        return None
    try:
        record = Record(buf, chunk.offset() + offset, chunk)
        if record.magic() != 0x2A2A:
            return None
        if record.size() < MIN_RECORD_SIZE:
            return None
        if offset + record.size() > next_offset:
            return None
        if not record.verify():
            return None
        return record
    except (ParseException, InvalidRecordException, ValueError, OSError):
        return None


def _verify_chunk(buf, chunk: ChunkHeader) -> Tuple[Optional[List[Record]], Optional[str], Optional[int]]:
    """Validate a whole chunk and return its records.

    Returns ``(records, None, None)`` for a verified chunk and
    ``(None, reason, offending_record_offset)`` otherwise.  The chunk is only
    trusted when magic, header checksum and data checksum verify and the
    records exactly fill the used region.
    """

    try:
        if not chunk.check_magic():
            return None, "chunk magic is invalid", None
    except (ParseException, UnicodeDecodeError):
        return None, "chunk magic cannot be decoded", None

    try:
        next_offset = chunk.next_record_offset()
    except ParseException as e:
        return None, "chunk next record offset cannot be read: {}".format(e), None
    if next_offset < RECORD_DATA_OFFSET or next_offset > CHUNK_SIZE:
        return None, "chunk next record offset is out of range: {:#x}".format(next_offset), None

    try:
        if chunk.calculate_header_checksum() != chunk.header_checksum():
            return None, "chunk header checksum mismatch", None
    except (ParseException, binascii.Error):
        return None, "chunk header checksum cannot be calculated", None

    try:
        if chunk.calculate_data_checksum() != chunk.data_checksum():
            return None, "chunk data checksum mismatch", None
    except (ParseException, binascii.Error):
        return None, "chunk data checksum cannot be calculated", None

    records: List[Record] = []
    offset = RECORD_DATA_OFFSET
    while offset < next_offset:
        record = _looks_like_record(buf, chunk, offset, next_offset)
        if record is None:
            return None, "invalid record at chunk offset {:#x}".format(offset), chunk.offset() + offset
        records.append(record)
        offset += record.size()

    if offset != next_offset:
        return None, "records do not exactly fill the used chunk", chunk.offset() + offset

    if records:
        try:
            if chunk.last_record_offset() != records[-1].offset() - chunk.offset():
                return None, "chunk last record offset does not match its last record", records[-1].offset()
        except ParseException:
            return None, "chunk last record offset cannot be read", None

    return records, None, None


def _first_record_of_chunk(buf, chunk: ChunkHeader) -> Optional[Record]:
    """Best-effort first record lookup used only to identify a generation."""

    try:
        if not chunk.check_magic():
            return None
        next_offset = chunk.next_record_offset()
        if next_offset <= RECORD_DATA_OFFSET:
            return None
        return _looks_like_record(buf, chunk, RECORD_DATA_OFFSET, min(next_offset, CHUNK_SIZE))
    except (ParseException, UnicodeDecodeError):
        return None


def _logical_chunks(chunks: List[ChunkHeader], oldest_chunk: int) -> Iterable[ChunkHeader]:
    """Yield chunks in log order, beginning at the declared oldest chunk."""

    if not chunks:
        return []
    count = len(chunks)
    start = oldest_chunk % count if count else 0
    return (chunks[(start + i) % count] for i in range(count))


def _first_present_record(buf, chunks: List[ChunkHeader], oldest_chunk: int) -> Optional[Record]:
    for chunk in _logical_chunks(chunks, oldest_chunk):
        record = _first_record_of_chunk(buf, chunk)
        if record is not None:
            return record
    return None


def _file_identity(path: str) -> bytes:
    """Return the filesystem identity of a log file.

    Device, inode and (where available) birth time are stable while a log is
    appended to, circularly overwritten, or cleared in place.  Replacing the
    file at the same path gives a different identity.
    """

    try:
        stat = os.stat(path)
    except OSError as e:
        raise CollectorError("unable to stat EVTX file: {}".format(e))

    birth = getattr(stat, "st_birthtime_ns", None)
    if birth is None and os.name == "nt":
        birth = getattr(stat, "st_ctime_ns", 0)
    return struct.pack("<QQQ", stat.st_dev, stat.st_ino, birth or 0)


def _generation_base(path: str, fh: FileHeader) -> str:
    digest = hashlib.sha256()
    digest.update(GENERATION_PREFIX.encode("ascii"))
    try:
        digest.update(fh.unpack_binary(0, 8))
        digest.update(struct.pack("<HH", fh.major_version(), fh.minor_version()))
        digest.update(struct.pack("<H", fh.header_chunk_size()))
    except ParseException as e:
        raise CollectorError("unable to read the EVTX file header: {}".format(e))
    digest.update(_file_identity(path))
    return digest.hexdigest()


def _generation_reset(base: str, seed: Optional[RecordLocator]) -> str:
    """Derive a generation for an in-place clear or rebuild of the same file."""

    digest = hashlib.sha256()
    digest.update(base.encode("ascii"))
    digest.update(b"\0reset\0")
    if seed is None:
        digest.update(b"empty")
    else:
        digest.update(seed.sha256.encode("ascii"))
        digest.update(struct.pack("<QI", seed.record_offset, seed.record_size))
    return digest.hexdigest()


def _state_matches(buf, state: GenerationState) -> bool:
    """Return whether any delivered chunk or record of ``state`` survives."""

    for chunk_offset, old_digest in state.chunk_digests.items():
        if chunk_offset + CHUNK_SIZE <= len(buf):
            current = hashlib.sha256(bytes(buf[chunk_offset : chunk_offset + CHUNK_SIZE])).hexdigest()
            if current == old_digest:
                return True
    for records in state.delivered.values():
        for record in records:
            if record.record_offset + record.record_size > len(buf):
                continue
            data = bytes(buf[record.record_offset : record.record_offset + record.record_size])
            if hashlib.sha256(data).hexdigest() == record.sha256:
                return True
    return False


def _generation_identity(
    path: str,
    buf,
    checkpoint: CollectorCheckpoint,
    fh: FileHeader,
    chunks: List[ChunkHeader],
) -> Tuple[str, Optional[RecordLocator], bool]:
    """Return ``(generation, first_record, rotated)`` for the current file.

    ``rotated`` reports that the scan switched away from the checkpoint's
    current generation, either because the file was replaced or because an
    older generation was supplied again for backfill.
    """

    base = _generation_base(path, fh)
    aliased_base = checkpoint.aliases.get(base, base)
    seed = _first_present_record(buf, chunks, fh.oldest_chunk())
    seed_locator = RecordLocator.from_record(seed) if seed is not None else None

    prior = checkpoint.generations.get(aliased_base)
    if prior is not None:
        if prior.delivered_count == 0 or _state_matches(buf, prior):
            return aliased_base, seed_locator or prior.first_record, aliased_base != checkpoint.current_generation
        reset = _generation_reset(aliased_base, seed_locator)
        checkpoint.add_alias(base, reset)
        prior_reset = checkpoint.generations.get(reset)
        return reset, seed_locator or (prior_reset.first_record if prior_reset else None), True

    if aliased_base != base:
        prior = checkpoint.generations.get(base)

    # A copy of a known generation has a different inode but identical
    # verified bytes.  One surviving delivered record is enough to recognize
    # it; an empty file cannot be matched and correctly starts fresh.
    for candidate in checkpoint.generations.values():
        if candidate.delivered_count > 0 and _state_matches(buf, candidate):
            checkpoint.add_alias(base, candidate.generation)
            return candidate.generation, seed_locator or candidate.first_record, (
                candidate.generation != checkpoint.current_generation
            )

    return base, seed_locator, bool(checkpoint.generations)


def _classify_change(
    prior: Optional[GenerationState],
    buf,
    file_size: int,
    verified_chunks: List[Tuple[int, int, str]],
    emitted: int,
    barrier: Optional[ChunkBarrier],
) -> ChangeKind:
    if prior is None or (prior.delivered_count == 0 and prior.chunks_verified == 0):
        return ChangeKind.NEW
    if file_size < prior.file_size:
        return ChangeKind.TRUNCATED
    if prior.last_record is not None:
        end = prior.last_record.end_offset()
        if end > len(buf):
            return ChangeKind.TRUNCATED
        digest = hashlib.sha256(bytes(buf[prior.last_record.record_offset : end])).hexdigest()
        if digest != prior.last_record.sha256:
            return ChangeKind.TRUNCATED
    if barrier is not None:
        # A declared chunk missing from the file is truncation.  A chunk that
        # is present but fails verification is unverified data: the checkpoint
        # must stop, but the file was not necessarily truncated.
        if barrier.reason.startswith("declared chunk"):
            return ChangeKind.TRUNCATED
        return ChangeKind.BLOCKED
    if file_size == prior.file_size and emitted == 0 and len(verified_chunks) == prior.chunks_verified:
        return ChangeKind.UNCHANGED
    return ChangeKind.APPENDED


def _make_event(record: Record, historical: bool) -> CollectedEvent:
    xml, xml_error = _record_xml(record)
    return CollectedEvent(
        record_number=record.record_num(),
        timestamp=_record_timestamp(record),
        data=record.data(),
        locator=RecordLocator.from_record(record),
        xml=xml,
        xml_error=xml_error,
        historical=historical,
    )


class EvtxCollector:
    """Collect records from one EVTX file with a persistent checkpoint."""

    def __init__(
        self,
        path: str,
        checkpoint_path: Optional[str] = None,
        include_inactive: bool = False,
    ):
        self.path = os.path.abspath(path)
        self.checkpoint_path = os.path.abspath(checkpoint_path) if checkpoint_path else None
        self.include_inactive = include_inactive

    def collect(
        self,
        checkpoint: Optional[CollectorCheckpoint] = None,
        on_record: Optional[Callable[[CollectedEvent], None]] = None,
        save: bool = True,
    ) -> CollectionResult:
        """Scan the file and deliver only previously unseen physical records.

        ``on_record`` runs for each record before its delivery is committed to
        the checkpoint.  If it raises, that record is not marked delivered and
        the checkpoint still records the records committed before it.  The
        checkpoint is written atomically before the failure is re-raised.
        """

        if checkpoint is None:
            checkpoint = (
                CollectorCheckpoint.load(self.checkpoint_path) if self.checkpoint_path else CollectorCheckpoint()
            )
        checkpoint.bind(self.path)

        with open(self.path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < FIRST_CHUNK_OFFSET:
                raise CollectorError("file is too small to contain an EVTX file header")
            buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                result = self._collect_mapped(buf, size, checkpoint, on_record)
            finally:
                buf.close()

        if save and self.checkpoint_path:
            checkpoint.save(self.checkpoint_path)
        return result

    def _collect_mapped(
        self,
        buf,
        file_size: int,
        checkpoint: CollectorCheckpoint,
        on_record: Optional[Callable[[CollectedEvent], None]],
    ) -> CollectionResult:
        try:
            fh = FileHeader(buf, 0)
        except ParseException as e:
            raise CollectorError("unable to parse the EVTX file header: {}".format(e))

        if not fh.check_magic():
            raise CollectorError("file does not begin with the EVTX ElfFile magic")

        if not fh.verify():
            return CollectionResult(
                change=ChangeKind.UNCHANGED,
                generation="",
                emitted=[],
                skipped=0,
                checkpoint=checkpoint,
                barrier=ChunkBarrier(0, "file header failed verification"),
                file_size=file_size,
                verified_through=FIRST_CHUNK_OFFSET,
                header_verified=False,
            )

        try:
            chunks = list(fh.chunks(include_inactive=self.include_inactive))
        except (ParseException, ValueError) as e:
            raise CollectorError("unable to enumerate EVTX chunks: {}".format(e))

        generation, seed, rotated = _generation_identity(self.path, buf, checkpoint, fh, chunks)
        prior = checkpoint.generations.get(generation)
        prior_snapshot = GenerationState.from_dict(prior.to_dict()) if prior is not None else None
        was_known = prior is not None
        state = checkpoint.state_for(generation)
        if seed is not None:
            state.remember_first(seed)
        high_water = _high_water(prior) if prior is not None else None
        replay_budget = state.replay_budget()

        verified_chunks: List[Tuple[int, int, str]] = []
        last_verified_record: Optional[Record] = None
        barrier: Optional[ChunkBarrier] = None
        emitted: List[CollectedEvent] = []
        skipped = 0

        declared_count = fh.chunk_count()
        available_count = len(chunks)

        for index, chunk in enumerate(chunks):
            records, reason, offending_record = _verify_chunk(buf, chunk)
            if records is None:
                barrier = ChunkBarrier(
                    offset=offending_record if offending_record is not None else chunk.offset(),
                    reason=reason or "chunk failed verification",
                    chunk_index=index,
                    chunk_offset=chunk.offset(),
                    record_offset=offending_record,
                )
                break

            chunk_digest = hashlib.sha256(bytes(buf[chunk.offset() : chunk.offset() + CHUNK_SIZE])).hexdigest()
            verified_chunks.append((chunk.offset(), CHUNK_SIZE, chunk_digest))
            if records:
                last_verified_record = records[-1]

            for record in records:
                locator = RecordLocator.from_record(record)
                if state.take_replay(locator, replay_budget):
                    skipped += 1
                    continue

                historical = high_water is not None and record.record_num() <= high_water
                event = _make_event(record, historical)
                if on_record is not None:
                    try:
                        on_record(event)
                    except BaseException as e:
                        state.file_size = file_size
                        state.verified_through = chunk.offset()
                        state.chunks_verified = len(verified_chunks) - 1
                        checkpoint.remember_generation(state)
                        if self.checkpoint_path:
                            checkpoint.save(self.checkpoint_path)
                        if isinstance(e, Exception):
                            raise DeliveryError(
                                "record {} at {:#x} was not committed: {}".format(
                                    record.record_num(), record.offset(), e
                                )
                            ) from e
                        raise

                emitted.append(event)
                state.mark_delivered(locator)
                if state.first_record is None:
                    state.first_record = locator
                state.last_record = locator

        if barrier is None and available_count < declared_count:
            barrier = ChunkBarrier(
                offset=FIRST_CHUNK_OFFSET + available_count * CHUNK_SIZE,
                reason="declared chunk {} is missing from the file".format(available_count),
                chunk_index=available_count,
                chunk_offset=FIRST_CHUNK_OFFSET + available_count * CHUNK_SIZE,
            )

        verified_through = FIRST_CHUNK_OFFSET + len(verified_chunks) * CHUNK_SIZE

        change = _classify_change(prior_snapshot, buf, file_size, verified_chunks, len(emitted), barrier)
        if rotated:
            change = ChangeKind.ROTATED

        state.file_size = file_size
        state.verified_through = verified_through
        state.chunks_verified = len(verified_chunks)
        state.chunk_digests = {offset: digest for offset, _length, digest in verified_chunks}
        if last_verified_record is not None:
            state.last_record = RecordLocator.from_record(last_verified_record)
        checkpoint.remember_generation(state)

        if self.checkpoint_path:
            checkpoint.save(self.checkpoint_path)

        return CollectionResult(
            change=change,
            generation=generation,
            emitted=emitted,
            skipped=skipped,
            checkpoint=checkpoint,
            barrier=barrier,
            file_size=file_size,
            verified_through=verified_through,
            header_verified=True,
            already_known_generation=was_known,
        )


def collect_evtx(
    path: str,
    checkpoint: Optional[CollectorCheckpoint] = None,
    checkpoint_path: Optional[str] = None,
    on_record: Optional[Callable[[CollectedEvent], None]] = None,
    include_inactive: bool = False,
    save: bool = True,
) -> CollectionResult:
    """Convenience wrapper around :class:`EvtxCollector`.

    ``checkpoint`` may be an object or a path; a path is loaded and saved
    atomically around the scan.
    """

    if isinstance(checkpoint, (str, os.PathLike)):
        checkpoint_path = os.fspath(checkpoint)
        checkpoint = None
    if checkpoint is None and checkpoint_path is not None:
        checkpoint = CollectorCheckpoint.load(checkpoint_path)
    collector = EvtxCollector(path, checkpoint_path=checkpoint_path, include_inactive=include_inactive)
    return collector.collect(checkpoint=checkpoint, on_record=on_record, save=save)
