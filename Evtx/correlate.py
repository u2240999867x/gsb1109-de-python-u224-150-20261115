"""
Correlation of event records across overlapping EVTX backups.

Three problems make naive de-duplication unsafe:

  * backup windows overlap, so the same physical event shows up in more than
    one EVTX file;
  * the binary EventRecordID is only unique per machine/channel and is reused
    on different machines (and after log rotation);
  * genuine, distinct events can have byte-identical event bodies.

The strategy here is deliberately conservative:

  * observations are only ever grouped within the same (machine, channel);
  * copies of one logical event are confirmed on concrete evidence - either a
    byte-exact record, or a stable RecordID plus matching timestamp and System
    metadata seen in another file generation;
  * field equality alone is never enough to confirm a copy. Such observations
    are returned as candidate links and keep their own rows;
  * every confirmed copy keeps all of its original locations.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

import lxml.etree as ET

from .BinaryParser import ParseException
from .Evtx import Evtx

logger = logging.getLogger(__name__)

EVENT_NS = "http://schemas.microsoft.com/win/2004/08/events/event"
NS = {"e": EVENT_NS}

STATUS_DUPLICATE = "duplicate"
STATUS_UNIQUE = "unique"

EVIDENCE_BYTE_EXACT = "byte-exact record across file generations"
EVIDENCE_EVENT_METADATA = "matching RecordID, timestamp and System metadata across file generations"

REASON_DISTINCT_RECORD_ID = "identical body but distinct RecordIDs; separate real events cannot be merged"
REASON_REUSED_RECORD_ID = "RecordID reused with conflicting timestamp or System metadata"


@dataclass(frozen=True)
class EventLocation:
    """A physical occurrence of an event inside a concrete EVTX file."""

    source: str
    generation: str
    machine: str
    channel: str
    record_num: int
    file_record_num: Optional[int]
    chunk_index: int
    offset: int

    def label(self) -> str:
        return "{}#{}@0x{:x}".format(self.source, self.generation, self.offset)


@dataclass
class ObservedEvent:
    """One parsed record together with the fingerprints used for correlation."""

    location: EventLocation
    timestamp: str
    event_id: Optional[str]
    body_hash: str
    raw_hash: str
    system_fingerprint: str


@dataclass
class LogicalEvent:
    """A single real event and every location at which a copy was confirmed."""

    machine: str
    channel: str
    record_num: int
    timestamp: str
    observations: list[ObservedEvent] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return STATUS_DUPLICATE if len(self.observations) > 1 else STATUS_UNIQUE

    @property
    def locations(self) -> list[EventLocation]:
        return [obs.location for obs in self.observations]

    @property
    def body_hash(self) -> str:
        return self.observations[0].body_hash


@dataclass
class CandidateLink:
    """Observations that look related but cannot be confirmed as copies."""

    machine: str
    channel: str
    body_hash: str
    reason: str
    locations: list[EventLocation]


@dataclass
class CorrelationResult:
    logical_events: list[LogicalEvent]
    candidate_links: list[CandidateLink]
    parse_errors: list[str]

    def duplicates(self) -> list[LogicalEvent]:
        return [event for event in self.logical_events if event.status == STATUS_DUPLICATE]

    def unique_events(self) -> list[LogicalEvent]:
        return [event for event in self.logical_events if event.status == STATUS_UNIQUE]

    def to_dict(self) -> dict:
        return {
            "logical_events": [
                {
                    "machine": event.machine,
                    "channel": event.channel,
                    "record_id": event.record_num,
                    "timestamp": event.timestamp,
                    "status": event.status,
                    "evidence": event.evidence,
                    "body_hash": event.body_hash,
                    "locations": [
                        {
                            "source": loc.source,
                            "generation": loc.generation,
                            "record_num": loc.record_num,
                            "file_record_num": loc.file_record_num,
                            "chunk_index": loc.chunk_index,
                            "offset": loc.offset,
                        }
                        for loc in event.locations
                    ],
                }
                for event in self.logical_events
            ],
            "candidate_links": [
                {
                    "machine": link.machine,
                    "channel": link.channel,
                    "body_hash": link.body_hash,
                    "reason": link.reason,
                    "locations": [
                        {
                            "source": loc.source,
                            "generation": loc.generation,
                            "record_num": loc.record_num,
                            "file_record_num": loc.file_record_num,
                            "chunk_index": loc.chunk_index,
                            "offset": loc.offset,
                        }
                        for loc in link.locations
                    ],
                }
                for link in self.candidate_links
            ],
            "parse_errors": self.parse_errors,
        }


def _find_text(node: ET._Element, path: str) -> Optional[str]:
    found = node.find(path, NS)
    return found.text if found is not None else None


def _canonical_body(root: ET._Element) -> bytes:
    parts = []
    for tag in ("EventData", "UserData"):
        for element in root.iter("{{{}}}{}".format(EVENT_NS, tag)):
            parts.append(ET.tostring(element, method="c14n"))
    return b"|".join(parts)


def _system_fingerprint(root: ET._Element) -> bytes:
    system = root.find("e:System", NS)
    clone = ET.fromstring(ET.tostring(system))
    for record_id in clone.findall("e:EventRecordID", NS):
        clone.remove(record_id)
    return ET.tostring(clone, method="c14n")


def extract_event(record, chunk, chunk_index: int, source: str, generation: str) -> ObservedEvent:
    """
    Parse one EVTX record into an ObservedEvent.

    Args:
      record: an Evtx.Evtx.Record.
      chunk: the ChunkHeader that owns the record, for file ordinal metadata.
      chunk_index: zero based index of the chunk within the source file.
      source: path of the EVTX file.
      generation: caller supplied label identifying the file generation/backup.
    """
    root = record.lxml()
    machine = _find_text(root, "e:System/e:Computer") or ""
    channel = _find_text(root, "e:System/e:Channel") or ""
    event_id = _find_text(root, "e:System/e:EventID")
    record_num = record.record_num()

    file_record_num = None
    if chunk.file_first_record_number() <= record_num <= chunk.file_last_record_number():
        file_record_num = record_num - chunk.file_first_record_number() + 1

    location = EventLocation(
        source=source,
        generation=generation,
        machine=machine,
        channel=channel,
        record_num=record_num,
        file_record_num=file_record_num,
        chunk_index=chunk_index,
        offset=record.offset(),
    )
    return ObservedEvent(
        location=location,
        timestamp=record.timestamp().isoformat(),
        event_id=event_id,
        body_hash=hashlib.sha256(_canonical_body(root)).hexdigest(),
        raw_hash=hashlib.sha256(record.data()).hexdigest(),
        system_fingerprint=hashlib.sha256(_system_fingerprint(root)).hexdigest(),
    )


def scan_sources(sources: Iterable[tuple[str, str]]) -> tuple[list[ObservedEvent], list[str]]:
    """
    Read every record from the given (path, generation) pairs.

    Scanning the same (path, generation, record offset) twice yields one
    observation, and records that cannot be parsed are skipped and reported
    instead of aborting the whole scan.
    """
    observations: list[ObservedEvent] = []
    parse_errors: list[str] = []
    seen: set[tuple[str, str, int]] = set()
    for source, generation in sources:
        with Evtx(source) as log:
            for chunk_index, chunk in enumerate(log.chunks()):
                try:
                    records = list(chunk.records())
                except ParseException as exc:
                    parse_errors.append("{} chunk {}: {}".format(source, chunk_index, exc))
                    continue
                for record in records:
                    try:
                        identity = (source, generation, record.offset())
                        if identity in seen:
                            continue
                        seen.add(identity)
                        observations.append(extract_event(record, chunk, chunk_index, source, generation))
                    except ParseException as exc:
                        parse_errors.append(
                            "{}#{}@0x{:x}: {}".format(source, generation, record.offset(), exc)
                        )
    return observations, parse_errors


def _confirmed_copies(cluster: list[ObservedEvent]) -> Optional[list[str]]:
    """
    Return the evidence strings if every member is confirmed to be the same
    logical event, otherwise None.

    Confirmation requires the same RecordID, timestamp, System metadata and
    body, observed in at least two distinct file generations.
    """
    first = cluster[0]
    if any(obs.timestamp != first.timestamp for obs in cluster[1:]):
        return None
    if any(obs.system_fingerprint != first.system_fingerprint for obs in cluster[1:]):
        return None
    if any(obs.body_hash != first.body_hash for obs in cluster[1:]):
        return None
    if len({obs.location.generation for obs in cluster}) < 2:
        return None

    evidence = []
    if len({obs.raw_hash for obs in cluster}) == 1:
        evidence.append(EVIDENCE_BYTE_EXACT)
    evidence.append(EVIDENCE_EVENT_METADATA)
    return evidence


def correlate(observations: list[ObservedEvent], parse_errors: Optional[list[str]] = None) -> CorrelationResult:
    """
    Group observations into logical events and unconfirmed candidate links.

    Grouping never crosses a (machine, channel) boundary, so RecordIDs reused
    on different machines can never collapse together. Two observations with
    the same body but different RecordIDs always remain separate rows.
    """
    body_groups: dict[tuple[str, str, str], list[ObservedEvent]] = defaultdict(list)
    for obs in observations:
        key = (obs.location.machine, obs.location.channel, obs.body_hash)
        body_groups[key].append(obs)

    logical_events: list[LogicalEvent] = []
    candidate_links: list[CandidateLink] = []

    for (machine, channel, body_hash), members in sorted(body_groups.items()):
        id_clusters: dict[int, list[ObservedEvent]] = defaultdict(list)
        for obs in members:
            id_clusters[obs.location.record_num].append(obs)

        group_events: list[LogicalEvent] = []

        for record_num, cluster in sorted(id_clusters.items()):
            evidence = _confirmed_copies(cluster)
            if evidence is None:
                for obs in cluster:
                    group_events.append(
                        LogicalEvent(
                            machine=machine,
                            channel=channel,
                            record_num=record_num,
                            timestamp=obs.timestamp,
                            observations=[obs],
                        )
                    )
                if len(cluster) > 1:
                    candidate_links.append(
                        CandidateLink(
                            machine=machine,
                            channel=channel,
                            body_hash=body_hash,
                            reason=REASON_REUSED_RECORD_ID,
                            locations=[obs.location for obs in cluster],
                        )
                    )
                continue

            group_events.append(
                LogicalEvent(
                    machine=machine,
                    channel=channel,
                    record_num=record_num,
                    timestamp=cluster[0].timestamp,
                    observations=cluster,
                    evidence=evidence,
                )
            )

        if len(id_clusters) > 1:
            candidate_links.append(
                CandidateLink(
                    machine=machine,
                    channel=channel,
                    body_hash=body_hash,
                    reason=REASON_DISTINCT_RECORD_ID,
                    locations=[loc for event in group_events for loc in event.locations],
                )
            )

        logical_events.extend(group_events)

    logical_events.sort(key=lambda event: (event.machine, event.channel, event.record_num, event.timestamp))
    return CorrelationResult(
        logical_events=logical_events,
        candidate_links=candidate_links,
        parse_errors=list(parse_errors or []),
    )


def correlate_sources(sources: Iterable[tuple[str, str]]) -> CorrelationResult:
    """Convenience wrapper: scan (path, generation) pairs and correlate them."""
    observations, parse_errors = scan_sources(sources)
    return correlate(observations, parse_errors)
