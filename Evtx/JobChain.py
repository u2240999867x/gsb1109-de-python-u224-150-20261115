"""Reconstruct collection job chains from Windows event records.

The events that describe a collection job are commonly emitted by several
providers.  Each event carries an activity identifier and, for a handoff, the
identifier of the activity it relates to.  This module turns those events into
a directed graph of activities so that branches, merges, gaps, and cycles can
be inspected together.
"""

from __future__ import absolute_import

import os
import re
import hashlib
import json
from collections import OrderedDict

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

try:
    import xml.etree.ElementTree as ET
except ImportError:  # pragma: no cover
    ET = None


START = "start"
TRANSFER = "transfer"
END = "end"
UNKNOWN = "unknown"

ROLES = (START, TRANSFER, END, UNKNOWN)

_START_WORDS = {
    "begin",
    "began",
    "beginning",
    "collect",
    "collected",
    "collection",
    "collector",
    "create",
    "created",
    "creation",
    "initiate",
    "initiated",
    "ingest",
    "ingested",
    "input",
    "launch",
    "launched",
    "open",
    "opened",
    "source",
    "start",
    "started",
    "starting",
}
_TRANSFER_WORDS = {
    "dispatch",
    "dispatched",
    "dispatcher",
    "forward",
    "forwarded",
    "forwarder",
    "handed",
    "handoff",
    "handover",
    "move",
    "moved",
    "relay",
    "relayed",
    "route",
    "routed",
    "router",
    "send",
    "sent",
    "transport",
    "transfer",
    "transferred",
    "transmit",
    "transmitted",
}
_END_WORDS = {
    "archive",
    "archived",
    "archiver",
    "close",
    "closed",
    "complete",
    "completed",
    "completion",
    "deliver",
    "delivered",
    "destination",
    "done",
    "end",
    "ended",
    "finish",
    "finished",
    "finalize",
    "finalized",
    "output",
    "sink",
    "stop",
    "stopped",
    "store",
    "stored",
    "terminate",
    "terminated",
}
_ROLE_WORDS = OrderedDict(((START, _START_WORDS), (TRANSFER, _TRANSFER_WORDS), (END, _END_WORDS)))
_ROLE_TERMS = (
    (START, ("开始", "启动", "发起", "采集", "创建")),
    (TRANSFER, ("转交", "转发", "移交", "传递", "发送")),
    (END, ("结束", "完成", "终止", "归档", "入库")),
)
_ROLE_EXACT = {
    "0": UNKNOWN,
    "1": START,
    "2": TRANSFER,
    "3": END,
    "begin": START,
    "start": START,
    "handoff": TRANSFER,
    "transfer": TRANSFER,
    "end": END,
    "finish": END,
}

_ACTIVITY_KEYS = {
    "activity",
    "activityguid",
    "activityid",
    "collectionid",
    "collectionjobid",
    "jobguid",
    "jobid",
}
_RELATED_KEYS = {
    "correlation",
    "correlationguid",
    "correlationid",
    "originalactivityid",
    "parentactivityid",
    "parentcorrelationid",
    "parentguid",
    "parentid",
    "parentjobid",
    "previousactivityid",
    "relatedactivityguid",
    "relatedactivityid",
    "relatedguid",
    "relatedid",
    "sourceactivityid",
    "upstreamactivityid",
}
_ROLE_KEYS = (
    "role",
    "eventtype",
    "eventkind",
    "eventstage",
    "stage",
    "phase",
    "operation",
    "action",
    "kind",
    "type",
    "status",
    "state",
    "jobstate",
    "jobstatus",
    "activitytype",
    "eventname",
    "operationname",
    "taskname",
    "category",
    "message",
    "description",
)
_SYSTEM_TAGS = {
    "Binary",
    "Channel",
    "Computer",
    "Correlation",
    "Data",
    "Event",
    "EventData",
    "EventID",
    "EventRecordID",
    "Execution",
    "Keywords",
    "Level",
    "Opcode",
    "Provider",
    "Security",
    "System",
    "Task",
    "TimeCreated",
    "UserData",
    "Version",
}
_NULL_IDS = {
    "",
    "-",
    "00000000000000000000000000000000",
    "00000000-0000-0000-0000-000000000000",
    "none",
    "null",
}


def _tokenize(value):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    return [token.lower() for token in re.split(r"[^A-Za-z0-9]+", text) if token]


def normalize_role(value):
    """Return one of ``start``, ``transfer``, ``end``, or ``unknown``."""

    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return UNKNOWN
    text = str(value).strip().lower()
    if not text:
        return UNKNOWN
    if text in _ROLE_EXACT:
        return _ROLE_EXACT[text]

    term_role = UNKNOWN
    term_position = -1
    for role, terms in _ROLE_TERMS:
        for term in terms:
            position = text.rfind(term)
            if position > term_position:
                term_position = position
                term_role = role
    if term_role != UNKNOWN:
        return term_role

    tokens = _tokenize(text)
    resolved = UNKNOWN
    for token in tokens:
        for role, words in _ROLE_WORDS.items():
            if token in words:
                resolved = role
                break
    if resolved != UNKNOWN:
        return resolved

    compact = re.sub(r"[^a-z0-9]", "", text)
    for word in sorted(_TRANSFER_WORDS, key=len, reverse=True):
        if len(word) >= 4 and word in compact:
            return TRANSFER
    for word in sorted(_END_WORDS, key=len, reverse=True):
        if len(word) >= 4 and word in compact:
            return END
    for word in sorted(_START_WORDS, key=len, reverse=True):
        if len(word) >= 4 and word in compact:
            return START
    return UNKNOWN


def canonical_id(value):
    """Normalize an activity/correlation identifier for comparisons.

    GUIDs are case-insensitive and are often written with or without braces.
    Empty values and the all-zero GUID are treated as absent.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in _NULL_IDS:
        return None
    return lowered


def _clean_id(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if canonical_id(text) is None:
        return None
    return text


def _id_values(value):
    """Return the non-empty identifiers in a scalar or collection."""

    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        candidates = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        candidates = [text]
        if "," in text or ";" in text:
            candidates = [part.strip() for part in re.split(r"[,;]", text) if part.strip()]
    else:
        text = str(value).strip()
        if not text:
            return []
        candidates = [text]

    result = []
    seen = set()
    stack = list(reversed(candidates))
    while stack:
        candidate = stack.pop()
        if isinstance(candidate, (list, tuple, set, frozenset)):
            stack.extend(reversed(sorted(candidate, key=str)))
            continue
        cleaned = _clean_id(candidate)
        if cleaned is None:
            continue
        key = canonical_id(cleaned)
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _normalize_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _local_name(tag):
    if tag is None:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _element_text(element):
    return "".join(element.itertext()).strip()


def _attr(attributes, *names):
    for name in names:
        if name in attributes:
            return attributes[name]
        lowered = name.lower()
        for key, value in attributes.items():
            if key.lower() == lowered:
                return value
    return None


def _lookup(data, aliases):
    if not data:
        return None
    for key, value in data.items():
        if _normalize_key(key) in aliases and value is not None:
            if isinstance(value, (list, tuple)):
                result = _id_values(value)
                if result:
                    return result
                continue
            text = str(value).strip()
            if text:
                return value
    return None


def _first(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def parse_role_map(mapping):
    """Normalize a provider/event-id to role mapping."""

    result = {}
    if not mapping:
        return result
    for key, value in mapping.items():
        role = normalize_role(value)
        if role == UNKNOWN:
            continue
        if isinstance(key, (tuple, list)) and len(key) == 2:
            result[(str(key[0]).strip().lower(), str(key[1]).strip())] = role
        else:
            result[str(key).strip()] = role
    return result


def role_from_provider(provider):
    """Infer a job role from a provider name."""

    return normalize_role(provider)


def _provider_role(provider, provider_roles):
    if not provider_roles:
        return UNKNOWN
    if provider in provider_roles:
        return provider_roles[provider]
    lowered = provider.strip().lower()
    for key, role in provider_roles.items():
        if isinstance(key, tuple):
            continue
        if str(key).strip().lower() == lowered:
            return role
    return UNKNOWN


def _event_role(event, provider_roles=None, event_id_roles=None, default_role=UNKNOWN, explicit=None):
    explicit_role = normalize_role(_first(explicit, getattr(event, "explicit_role", None)))
    if explicit_role != UNKNOWN:
        return explicit_role

    provider = event.provider or ""
    event_id = "" if event.event_id is None else str(event.event_id).strip()

    if provider_roles:
        role = _provider_role(provider, provider_roles)
        if role != UNKNOWN:
            return role
        for key, mapped in provider_roles.items():
            if isinstance(key, tuple) and len(key) == 2:
                if str(key[0]).strip().lower() == provider.strip().lower() and str(key[1]).strip() == event_id:
                    return mapped

    if event_id_roles:
        for key in (event_id, event.event_id):
            if key is None:
                continue
            if key in event_id_roles:
                return event_id_roles[key]
            text_key = str(key).strip()
            if text_key in event_id_roles:
                return event_id_roles[text_key]

    role = normalize_role(_lookup(event.data, set(_ROLE_KEYS)))
    if role != UNKNOWN:
        return role

    role = role_from_provider(provider)
    if role != UNKNOWN:
        return role

    return normalize_role(default_role)


class JobEvent(object):
    """A single start, transfer, or end observation."""

    def __init__(
        self,
        activity_id,
        related_activity_id=None,
        kind=None,
        provider="",
        event_id=None,
        record_id=None,
        timestamp=None,
        channel="",
        computer="",
        data=None,
        uid=None,
        source=None,
        correlation_id=None,
        role=None,
        event_type=None,
        related_activity_ids=None,
        explicit_role=None,
    ):
        self.activity_id = _clean_id(activity_id)
        self.related_activity_ids = _id_values(
            related_activity_ids
            if related_activity_ids is not None
            else _first_multi(related_activity_id, correlation_id)
        )
        if self.related_activity_ids:
            self.related_activity_id = self.related_activity_ids[0]
        else:
            self.related_activity_id = None
        self.explicit_role = normalize_role(_first(kind, role, event_type, explicit_role))
        self.provider = "" if provider is None else str(provider)
        self.event_id = event_id
        self.record_id = record_id
        self.timestamp = timestamp
        self.channel = "" if channel is None else str(channel)
        self.computer = "" if computer is None else str(computer)
        self.data = dict(data or {})
        self.uid = None if uid is None else str(uid)
        self.source = source

    @property
    def activity_key(self):
        return canonical_id(self.activity_id)

    @property
    def related_key(self):
        return canonical_id(self.related_activity_id)

    @property
    def related_keys(self):
        return [canonical_id(value) for value in self.related_activity_ids if canonical_id(value) is not None]

    @property
    def correlation_id(self):
        return self.related_activity_id

    @property
    def correlation_ids(self):
        return list(self.related_activity_ids)

    @property
    def role(self):
        return self.kind

    @property
    def _role_value(self):
        if self.explicit_role != UNKNOWN:
            return self.explicit_role
        return self.kind

    @property
    def event_uid(self):
        if self.uid is not None:
            return "uid:" + self.uid
        if self.record_id is not None:
            return "record:{}|{}|{}".format(self.channel, self.computer, self.record_id)
        if self.timestamp is not None:
            return "event:{}|{}|{}|{}|{}|{}".format(
                self.provider,
                self.event_id,
                self.timestamp,
                self.activity_key,
                ",".join(self.related_keys),
                self.kind,
            )
        payload = json.dumps(self.data, sort_keys=True, default=str)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        return "content:{}|{}|{}|{}|{}|{}|{}".format(
            self.provider,
            self.event_id,
            self.activity_key,
            ",".join(self.related_keys),
            self.kind,
            self.channel,
            digest,
        )

    def to_dict(self):
        return {
            "uid": self.event_uid,
            "activity_id": self.activity_id,
            "related_activity_id": self.related_activity_id,
            "related_activity_ids": list(self.related_activity_ids),
            "kind": self.kind,
            "provider": self.provider,
            "event_id": self.event_id,
            "record_id": self.record_id,
            "timestamp": None if self.timestamp is None else str(self.timestamp),
            "channel": self.channel,
            "computer": self.computer,
        }

    def __repr__(self):
        return "JobEvent(activity_id={!r}, related_activity_id={!r}, kind={!r}, provider={!r})".format(
            self.activity_id, self.related_activity_id, self.kind, self.provider
        )

    @classmethod
    def from_dict(cls, payload, **kwargs):
        return event_from_dict(payload, **kwargs)

    @classmethod
    def from_xml(cls, payload, **kwargs):
        return event_from_xml(payload, **kwargs)

    @classmethod
    def from_record(cls, record, **kwargs):
        return event_from_record(record, **kwargs)


def _first_multi(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            if len(value):
                return value
            continue
        if str(value).strip():
            return value
    return None


class ActivityNode(object):
    """An activity and the job lifecycle events observed for it."""

    def __init__(self, key, activity_id):
        self.key = key
        self.activity_id = activity_id
        self.events = []
        self.parents = set()
        self.children = set()

    def add_event(self, event):
        self.events.append(event)

    @property
    def start_events(self):
        return [event for event in self.events if event.kind == START]

    @property
    def transfer_events(self):
        return [event for event in self.events if event.kind == TRANSFER]

    @property
    def end_events(self):
        return [event for event in self.events if event.kind == END]

    @property
    def unknown_events(self):
        return [event for event in self.events if event.kind == UNKNOWN]

    @property
    def has_start(self):
        return bool(self.start_events)

    @property
    def has_end(self):
        return bool(self.end_events)

    @property
    def missing_start(self):
        return not self.has_start

    @property
    def missing_end(self):
        return not self.has_end

    @property
    def status(self):
        if self.missing_start and self.missing_end:
            return "missing_start_and_end"
        if self.missing_start:
            return "missing_start"
        if self.missing_end:
            return "missing_end"
        return "complete"

    @property
    def event_count(self):
        return len({event.event_uid for event in self.events})

    def to_dict(self, display=None):
        display = display or {parent: parent for parent in self.parents}
        display.update({child: child for child in self.children})
        display.setdefault(self.key, self.activity_id)
        return {
            "activity_id": self.activity_id,
            "key": self.key,
            "status": self.status,
            "has_start": self.has_start,
            "has_end": self.has_end,
            "missing_start": self.missing_start,
            "missing_end": self.missing_end,
            "start_count": len(self.start_events),
            "transfer_count": len(self.transfer_events),
            "end_count": len(self.end_events),
            "unknown_count": len(self.unknown_events),
            "event_count": self.event_count,
            "parents": sorted(display[parent] for parent in self.parents),
            "children": sorted(display[child] for child in self.children),
        }

    def __repr__(self):
        return "ActivityNode(activity_id={!r}, status={!r})".format(self.activity_id, self.status)


class JobEdge(object):
    def __init__(self, source, target):
        self.source = source
        self.target = target

    def __repr__(self):
        return "JobEdge(source={!r}, target={!r})".format(self.source, self.target)


def _strongly_connected(nodes, edges):
    """Return strongly connected components using an iterative Kosaraju pass."""

    graph = {node: set() for node in nodes}
    reverse = {node: set() for node in nodes}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        reverse.setdefault(target, set()).add(source)

    visited = set()
    order = []
    for root in sorted(nodes):
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            node, finished = stack.pop()
            if finished:
                order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for child in sorted(graph.get(node, ()), reverse=True):
                if child not in visited:
                    stack.append((child, False))

    assigned = set()
    components = []
    for root in reversed(order):
        if root in assigned:
            continue
        component = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if node in assigned:
                continue
            assigned.add(node)
            component.add(node)
            for parent in sorted(reverse.get(node, ())):
                if parent not in assigned:
                    stack.append(parent)
        components.append(component)
    return components


def _find_cycles(nodes, edges):
    cycles = []
    edge_set = set(edges)
    for component in _strongly_connected(nodes, edges):
        if len(component) > 1:
            cycles.append(sorted(component))
        else:
            node = next(iter(component))
            if (node, node) in edge_set:
                cycles.append([node])
    cycles.sort()
    return cycles


def _topological_order(nodes, edges):
    indegree = {node: 0 for node in nodes}
    for source, target in edges:
        if source in indegree and target in indegree:
            indegree[target] += 1
    ready = [node for node in nodes if indegree[node] == 0]
    ready.sort()
    ordered = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for child in sorted(child for src, child in edges if src == node):
            if child not in indegree:
                continue
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()
    return ordered


class JobChain(object):
    """A connected component of the activity graph."""

    def __init__(self, nodes, edges, events, index=0):
        self.nodes = OrderedDict((key, nodes[key]) for key in sorted(nodes))
        self.edges = []
        for source, target in sorted(edges):
            if source in self.nodes and target in self.nodes:
                self.edges.append(JobEdge(source, target))
        self.index = index
        self._events = []
        seen = set()
        for event in events:
            if event.event_uid in seen:
                continue
            seen.add(event.event_uid)
            self._events.append(event)

    @property
    def chain_id(self):
        return "chain-{}".format(self.index + 1)

    @property
    def id(self):
        return self.chain_id

    @property
    def events(self):
        return list(self._events)

    @property
    def event_count(self):
        return len({event.event_uid for event in self._events})

    @property
    def workload(self):
        return self.event_count

    @property
    def roots(self):
        return [node.activity_id for node in self.nodes.values() if not node.parents]

    @property
    def leaves(self):
        return [node.activity_id for node in self.nodes.values() if not node.children]

    @property
    def start_events(self):
        return [event for event in self._events if event.kind == START]

    @property
    def end_events(self):
        return [event for event in self._events if event.kind == END]

    @property
    def missing_start_nodes(self):
        return [
            node.activity_id
            for node in self.nodes.values()
            if node.missing_start and not node.parents
        ]

    @property
    def missing_end_nodes(self):
        return [
            node.activity_id
            for node in self.nodes.values()
            if node.missing_end and not node.children
        ]

    @property
    def activities_missing_start(self):
        return [node.activity_id for node in self.nodes.values() if node.missing_start]

    @property
    def activities_missing_end(self):
        return [node.activity_id for node in self.nodes.values() if node.missing_end]

    @property
    def missing_start(self):
        if self.missing_start_nodes:
            return True
        return not self.start_events

    @property
    def missing_end(self):
        if self.missing_end_nodes:
            return True
        return not self.end_events

    @property
    def complete(self):
        return not self.missing_start and not self.missing_end

    @property
    def has_merged_activity(self):
        return any(len(node.parents) > 1 for node in self.nodes.values())

    @property
    def merged_activities(self):
        return [node.activity_id for node in self.nodes.values() if len(node.parents) > 1]

    @property
    def branched_activities(self):
        return [node.activity_id for node in self.nodes.values() if len(node.children) > 1]

    @property
    def has_start(self):
        return not self.missing_start

    @property
    def has_end(self):
        return not self.missing_end

    @property
    def start_count(self):
        return len(self.start_events)

    @property
    def end_count(self):
        return len(self.end_events)

    @property
    def transfer_count(self):
        return len([event for event in self._events if event.kind == TRANSFER])

    @property
    def cycles(self):
        return _find_cycles(list(self.nodes), [(edge.source, edge.target) for edge in self.edges])

    @property
    def cyclic(self):
        return bool(self.cycles)

    @property
    def is_cyclic(self):
        return self.cyclic

    @property
    def topological_order(self):
        return _topological_order(list(self.nodes), [(edge.source, edge.target) for edge in self.edges])

    def node(self, activity_id):
        key = canonical_id(activity_id)
        if key is None:
            return None
        return self.nodes.get(key)

    def contains(self, activity_id):
        return self.node(activity_id) is not None

    def display(self, key):
        node = self.nodes.get(key)
        return node.activity_id if node is not None else key

    def paths(self, max_paths=1000):
        """Enumerate root-to-leaf paths, cutting cycles and repeated nodes."""

        paths = []
        roots = [node.key for node in self.nodes.values() if not node.parents]
        if not roots:
            roots = [node.key for node in self.nodes.values()]

        def walk(node_key, path):
            if len(paths) >= max_paths:
                return
            path = path + [node_key]
            node = self.nodes[node_key]
            children = sorted(node.children)
            if not children or node_key in path[:-1]:
                paths.append(path)
                return
            for child in children:
                if child in path:
                    paths.append(path + [child])
                    continue
                walk(child, path)

        for root in roots:
            walk(root, [])
        return [[self.display(key) for key in path] for path in paths]

    def to_dict(self):
        display = {node.key: node.activity_id for node in self.nodes.values()}
        return {
            "id": self.chain_id,
            "nodes": [node.to_dict(display) for node in self.nodes.values()],
            "edges": [
                {"source": self.display(edge.source), "target": self.display(edge.target)} for edge in self.edges
            ],
            "roots": self.roots,
            "leaves": self.leaves,
            "missing_start": self.missing_start,
            "missing_end": self.missing_end,
            "missing_start_nodes": self.missing_start_nodes,
            "missing_end_nodes": self.missing_end_nodes,
            "complete": self.complete,
            "cyclic": self.cyclic,
            "cycles": [[self.display(key) for key in cycle] for cycle in self.cycles],
            "branched_activities": self.branched_activities,
            "merged_activities": self.merged_activities,
            "workload": self.workload,
            "event_count": self.event_count,
            "events": [event.to_dict() for event in self._events],
        }

    def __repr__(self):
        return "JobChain(id={!r}, nodes={!r}, workload={!r})".format(
            self.chain_id, len(self.nodes), self.workload
        )


class JobChainReport(object):
    """The result of rebuilding all job chains from a collection of events."""

    def __init__(self, chains, unassigned_events=None):
        self.chains = list(chains)
        self.unassigned_events = list(unassigned_events or [])

    @property
    def workload(self):
        seen = {event.event_uid for event in self.unassigned_events}
        for chain in self.chains:
            seen.update(event.event_uid for event in chain.events)
        return len(seen)

    @property
    def event_count(self):
        return self.workload

    @property
    def total_workload(self):
        return self.workload

    @property
    def missing_start_chains(self):
        return [chain for chain in self.chains if chain.missing_start]

    @property
    def missing_end_chains(self):
        return [chain for chain in self.chains if chain.missing_end]

    @property
    def missing_start(self):
        return bool(self.missing_start_chains)

    @property
    def missing_end(self):
        return bool(self.missing_end_chains)

    @property
    def duplicate_chains(self):
        return [chain for chain in self.chains if chain.has_merged_activity]

    @property
    def merged_activities(self):
        return [activity for chain in self.chains for activity in chain.merged_activities]

    @property
    def branched_activities(self):
        return [activity for chain in self.chains for activity in chain.branched_activities]

    @property
    def cyclic_chains(self):
        return [chain for chain in self.chains if chain.cyclic]

    @property
    def cycles(self):
        return [cycle for chain in self.chains for cycle in chain.cycles]

    @property
    def complete(self):
        return not self.missing_start_chains and not self.missing_end_chains

    def chain_for(self, activity_id):
        key = canonical_id(activity_id)
        if key is None:
            return None
        for chain in self.chains:
            if key in chain.nodes:
                return chain
        return None

    def to_dict(self):
        return {
            "chain_count": len(self.chains),
            "workload": self.workload,
            "event_count": self.event_count,
            "missing_start": bool(self.missing_start_chains),
            "missing_end": bool(self.missing_end_chains),
            "cyclic": bool(self.cyclic_chains),
            "branched_activities": self.branched_activities,
            "merged_activities": self.merged_activities,
            "chains": [chain.to_dict() for chain in self.chains],
            "unassigned_events": [event.to_dict() for event in self.unassigned_events],
        }

    def __iter__(self):
        return iter(self.chains)

    def __len__(self):
        return len(self.chains)

    def __getitem__(self, index):
        return self.chains[index]

    def __repr__(self):
        return "JobChainReport(chains={!r}, workload={!r})".format(len(self.chains), self.workload)


def event_from_xml(xml, provider_roles=None, event_id_roles=None, default_role=UNKNOWN, source=None):
    """Build a :class:`JobEvent` from a record's rendered XML."""

    if ET is None:  # pragma: no cover
        raise RuntimeError("xml.etree.ElementTree is unavailable")
    if hasattr(xml, "iter") and not isinstance(xml, (str, bytes)):
        root = xml
    else:
        if isinstance(xml, bytes):
            xml = xml.decode("utf-8")
        root = ET.fromstring(xml)

    provider = ""
    event_id = None
    record_id = None
    timestamp = None
    channel = ""
    computer = ""
    correlation_activity = None
    correlation_related = None
    data = {}

    for element in root.iter():
        tag = _local_name(element.tag)
        attributes = {_local_name(key): value for key, value in element.attrib.items()}
        if tag == "Provider":
            provider = _first(_attr(attributes, "Name"), (element.text or "").strip()) or ""
            continue
        if tag == "EventID":
            event_id = _first((element.text or "").strip(), _attr(attributes, "Value"))
            continue
        if tag == "EventRecordID":
            record_id = _first((element.text or "").strip(), _attr(attributes, "Value"))
            continue
        if tag == "TimeCreated":
            timestamp = _first(_attr(attributes, "SystemTime"), (element.text or "").strip())
            continue
        if tag == "Channel":
            channel = _first((element.text or "").strip(), _attr(attributes, "Value")) or ""
            continue
        if tag == "Computer":
            computer = _first((element.text or "").strip(), _attr(attributes, "Value")) or ""
            continue
        if tag == "Correlation":
            correlation_activity = _first(_attr(attributes, "ActivityID"), _attr(attributes, "ActivityId"))
            correlation_related = _first(
                _attr(attributes, "RelatedActivityID"),
                _attr(attributes, "RelatedActivityId"),
                _attr(attributes, "CorrelationID"),
                _attr(attributes, "CorrelationId"),
            )
            continue
        if tag == "Data":
            name = _first(_attr(attributes, "Name"))
            if name:
                text = _element_text(element)
                if name in data:
                    existing = data[name]
                    if not isinstance(existing, list):
                        existing = [existing]
                    existing.append(text)
                    data[name] = existing
                else:
                    data[name] = text
            continue
        if tag in _SYSTEM_TAGS:
            continue
        if not list(element):
            text = _element_text(element)
            if text:
                data.setdefault(tag, text)
        for name, value in attributes.items():
            data.setdefault(name, value)

    activity_id = _first(correlation_activity, _lookup(data, _ACTIVITY_KEYS))
    data_related = _lookup(data, _RELATED_KEYS)
    related_values = []
    for candidate in (correlation_related, data_related):
        for value in _id_values(candidate):
            if canonical_id(value) not in [canonical_id(existing) for existing in related_values]:
                related_values.append(value)
    event = JobEvent(
        activity_id=activity_id,
        related_activity_ids=related_values,
        kind=None,
        provider=provider,
        event_id=event_id,
        record_id=record_id,
        timestamp=timestamp,
        channel=channel,
        computer=computer,
        data=data,
        source=source,
    )
    event.kind = _event_role(event, provider_roles, event_id_roles, default_role)
    event.explicit_role = event.kind
    return event


def event_from_record(record, provider_roles=None, event_id_roles=None, default_role=UNKNOWN):
    """Build a :class:`JobEvent` from an ``Evtx.Evtx.Record``."""

    event = event_from_xml(
        record.xml(),
        provider_roles=provider_roles,
        event_id_roles=event_id_roles,
        default_role=default_role,
        source=record,
    )
    if event.record_id is None:
        try:
            event.record_id = record.record_num()
        except Exception:
            pass
    if event.timestamp is None:
        try:
            event.timestamp = record.timestamp()
        except Exception:
            pass
    return event


def _flatten_event_dict(system, wrapper=None):
    flattened = {}
    if wrapper:
        flattened.update({key: value for key, value in wrapper.items() if key not in ("Event", "System")})

    document = dict(wrapper or {})
    system = dict(system)
    body = system
    if "System" in system and isinstance(system["System"], Mapping):
        data = system.get("EventData")
        system = dict(system["System"])
        if data is not None:
            body = dict(body)
            body["EventData"] = data
        else:
            body = {}

    provider = system.get("Provider")
    if isinstance(provider, Mapping):
        flattened.setdefault("provider", provider.get("Name"))
    elif provider is not None:
        flattened.setdefault("provider", provider)
    for name in ("EventID", "EventRecordID", "Channel", "Computer"):
        candidate = system.get(name)
        if isinstance(candidate, Mapping):
            candidate = candidate.get("Value") or candidate.get("#text")
        if candidate is not None:
            flattened.setdefault(name, candidate)
    time_created = system.get("TimeCreated")
    if isinstance(time_created, Mapping):
        flattened.setdefault("timestamp", time_created.get("SystemTime"))
    elif time_created is not None:
        flattened.setdefault("timestamp", time_created)
    correlation = system.get("Correlation")
    if isinstance(correlation, Mapping):
        flattened.setdefault("activity_id", correlation.get("ActivityID") or correlation.get("ActivityId"))
        flattened.setdefault(
            "related_activity_id",
            correlation.get("RelatedActivityID") or correlation.get("RelatedActivityId"),
        )
    elif correlation is not None:
        flattened.setdefault("activity_id", correlation)

    data = body.get("EventData") if body else None
    if data is None and document:
        data = document.get("EventData")
    if data is not None:
        flattened.setdefault("EventData", data)
    return flattened


def event_from_dict(event, provider_roles=None, event_id_roles=None, default_role=UNKNOWN):
    """Build a :class:`JobEvent` from a dictionary."""

    if "Event" in event and isinstance(event["Event"], Mapping):
        event = _flatten_event_dict(event["Event"], event)
    elif "System" in event and isinstance(event["System"], Mapping):
        event = _flatten_event_dict(event)

    raw_data = event.get("data") or event.get("EventData") or {}
    if isinstance(raw_data, Mapping) and "Data" in raw_data:
        raw_data = raw_data["Data"]
    if isinstance(raw_data, Mapping):
        data = dict(raw_data)
    elif isinstance(raw_data, list):
        data = {}
        for entry in raw_data:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("@Name") or entry.get("Name")
            if name is None:
                continue
            value = entry.get("#text")
            if value is None:
                value = entry.get("Value")
            if value is None:
                value = {key: item for key, item in entry.items() if key not in ("@Name", "Name")}
            if name in data:
                existing = data[name]
                if not isinstance(existing, list):
                    existing = [existing]
                existing.append(value)
                data[name] = existing
            else:
                data[name] = value
    else:
        data = {}

    def value(*names):
        for name in names:
            if name in event and event[name] is not None:
                return event[name]
            for key, candidate in event.items():
                if _normalize_key(key) == _normalize_key(name) and candidate is not None:
                    return candidate
        return None

    explicit = value("kind", "role", "event_type", "eventType", "type")
    activity_id = value("activity_id", "activityId", "ActivityID", "activity", "job_id", "jobId")
    related_id = value(
        "related_activity_id",
        "relatedActivityId",
        "RelatedActivityID",
        "related_activity_ids",
        "relatedActivityIds",
        "correlation_id",
        "correlationId",
        "CorrelationID",
        "correlation_ids",
        "correlationIds",
        "related",
    )
    if activity_id is None:
        activity_id = _lookup(data, _ACTIVITY_KEYS)
    if related_id is None:
        related_id = _lookup(data, _RELATED_KEYS)
    related_ids = _id_values(related_id)
    for key, candidate in data.items():
        if _normalize_key(key) in _RELATED_KEYS:
            for item in _id_values(candidate):
                if canonical_id(item) not in [canonical_id(existing) for existing in related_ids]:
                    related_ids.append(item)

    event_obj = JobEvent(
        activity_id=activity_id,
        related_activity_ids=related_ids,
        kind=explicit,
        provider=value("provider", "Provider") or "",
        event_id=value("event_id", "eventId", "EventID", "eid"),
        record_id=value("record_id", "recordId", "EventRecordID", "record_num"),
        timestamp=value("timestamp", "time", "TimeCreated", "system_time"),
        channel=value("channel", "Channel") or "",
        computer=value("computer", "Computer") or "",
        data=data,
        uid=value("uid", "event_uid", "eventUid"),
        source=event,
    )
    event_obj.kind = _event_role(event_obj, provider_roles, event_id_roles, default_role)
    event_obj.explicit_role = event_obj.kind
    return event_obj


def _event_from_item(item, provider_roles=None, event_id_roles=None, default_role=UNKNOWN):
    if isinstance(item, JobEvent):
        item.kind = _event_role(item, provider_roles, event_id_roles, default_role)
        return item
    if isinstance(item, Mapping):
        return event_from_dict(item, provider_roles, event_id_roles, default_role)
    if hasattr(item, "xml") and callable(item.xml):
        return event_from_record(item, provider_roles, event_id_roles, default_role)
    if isinstance(item, (str, bytes)) or (ET is not None and hasattr(item, "iter")):
        return event_from_xml(item, provider_roles, event_id_roles, default_role)
    raise TypeError("unsupported event type: {}".format(type(item).__name__))


class JobChainBuilder(object):
    """Collect job events and rebuild their activity graph."""

    def __init__(
        self,
        provider_roles=None,
        event_id_roles=None,
        default_role=UNKNOWN,
        edge_direction="related_to_activity",
    ):
        self.provider_roles = parse_role_map(provider_roles)
        self.event_id_roles = parse_role_map(event_id_roles)
        self.default_role = default_role
        self.edge_direction = edge_direction
        self._events = []
        self._seen = set()

    def add(self, item):
        event = _event_from_item(item, self.provider_roles, self.event_id_roles, self.default_role)
        if event.event_uid not in self._seen:
            self._seen.add(event.event_uid)
            self._events.append(event)
        return self

    def add_event(self, item):
        return self.add(item)

    def add_record(self, item):
        return self.add(item)

    def add_events(self, items):
        for item in items:
            self.add(item)
        return self

    extend = add_events

    def add_file(self, path):
        from Evtx.Evtx import Evtx

        with Evtx(path) as log:
            for record in log.records():
                self.add(record)
        return self

    def build(self):
        return build_job_chains(
            self._events,
            provider_roles=self.provider_roles,
            event_id_roles=self.event_id_roles,
            default_role=self.default_role,
            edge_direction=self.edge_direction,
        )

    @classmethod
    def from_events(cls, events, **kwargs):
        builder = cls(**kwargs)
        builder.add_events(events)
        return builder.build()


def _ensure_node(nodes, key, activity_id):
    if key not in nodes:
        nodes[key] = ActivityNode(key, activity_id)
    elif not nodes[key].activity_id and activity_id:
        nodes[key].activity_id = activity_id
    return nodes[key]


def build_job_chains(
    events,
    provider_roles=None,
    event_id_roles=None,
    default_role=UNKNOWN,
    edge_direction="related_to_activity",
):
    """Build job chains from events, records, XML strings, or dictionaries."""

    provider_roles = parse_role_map(provider_roles)
    event_id_roles = parse_role_map(event_id_roles)

    if isinstance(events, JobChainReport):
        return events
    if isinstance(events, JobChainBuilder):
        return events.build()
    if isinstance(events, Mapping) or isinstance(events, (str, bytes)) or (ET is not None and hasattr(events, "iter")):
        events = [events]

    normalized = []
    seen = set()
    for item in events:
        event = _event_from_item(item, provider_roles, event_id_roles, default_role)
        if event.event_uid in seen:
            continue
        seen.add(event.event_uid)
        normalized.append(event)

    nodes = OrderedDict()
    edges = OrderedDict()
    unassigned = []
    node_order = []

    def ensure(key, activity_id):
        if key not in nodes:
            _ensure_node(nodes, key, activity_id)
            node_order.append(key)
        return nodes[key]

    for event in normalized:
        activity_key = event.activity_key
        related_keys = event.related_keys
        synthesized_activity = False
        if activity_key is None and not related_keys:
            unassigned.append(event)
            continue
        if activity_key is None:
            activity_key = related_keys[0]
            synthesized_activity = True
            if event.activity_id is None:
                event.activity_id = event.related_activity_id
        if activity_key is None:
            unassigned.append(event)
            continue

        node = ensure(activity_key, event.activity_id)
        node.add_event(event)

        if synthesized_activity:
            continue

        for related_key, related_id in zip(related_keys, event.related_activity_ids):
            if related_key == activity_key:
                edges.setdefault((activity_key, related_key), JobEdge(activity_key, related_key))
                continue
            ensure(related_key, related_id)
            if edge_direction == "activity_to_related":
                source_key, target_key = activity_key, related_key
            else:
                source_key, target_key = related_key, activity_key
            edges.setdefault((source_key, target_key), JobEdge(source_key, target_key))
            nodes[source_key].children.add(target_key)
            nodes[target_key].parents.add(source_key)

    if not nodes:
        return JobChainReport([], unassigned)

    graph = {key: set() for key in nodes}
    reverse = {key: set() for key in nodes}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        reverse.setdefault(target, set()).add(source)

    components = []
    visited = set()
    for root in node_order:
        if root in visited:
            continue
        component = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            for child in graph.get(node, ()):
                if child not in visited:
                    stack.append(child)
            for parent in reverse.get(node, ()):
                if parent not in visited:
                    stack.append(parent)
        components.append(component)

    chains = []
    for index, component in enumerate(components):
        component_nodes = {key: nodes[key] for key in component}
        component_edges = [
            (source, target) for source, target in edges if source in component and target in component
        ]
        component_events = [
            event
            for event in normalized
            if event.activity_key in component
            or (event.activity_key is None and any(key in component for key in event.related_keys))
        ]
        chains.append(JobChain(component_nodes, component_edges, component_events, index=index))
    return JobChainReport(chains, unassigned)


build_chains = build_job_chains
build_job_chain = build_job_chains
analyze = build_job_chains


def build_from_file(path, **kwargs):
    return job_chains_from_file(path, **kwargs)


def build_from_json(payload, **kwargs):
    """Build job chains from a decoded JSON document or list."""

    document = payload
    if isinstance(document, str):
        document = json.loads(document)
    if isinstance(document, Mapping):
        if "chains" in document and "events" not in document:
            return document
        document = document.get("events", [document])
    return build_job_chains(document, **kwargs)


def build_from_xml(payloads, **kwargs):
    """Build job chains from one XML document or a list of them."""

    if isinstance(payloads, (str, bytes)) or (ET is not None and hasattr(payloads, "iter")):
        payloads = [payloads]
    return build_job_chains(payloads, **kwargs)


def job_chains_from_file(path, **kwargs):
    """Build job chains directly from an EVTX file."""

    builder = JobChainBuilder(
        provider_roles=kwargs.get("provider_roles"),
        event_id_roles=kwargs.get("event_id_roles"),
        default_role=kwargs.get("default_role", UNKNOWN),
        edge_direction=kwargs.get("edge_direction", "related_to_activity"),
    )
    return builder.add_file(path).build()


def is_evtx_file(path):
    if not isinstance(path, str) or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(8) == b"ElfFile\x00"
    except OSError:
        return False
