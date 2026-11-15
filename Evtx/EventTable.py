"""
Build a unified, version-aware event table from EVTX records.

Providers change the meaning of their fields between versions.  A duration
field, for example, may be declared in milliseconds in one provider version
and in microseconds in the next.  Some provider versions even declare the
same field name more than once, so the position of the declaration matters.

This module deliberately does not guess.  Callers supply an explicit mapping
from ``(provider, version)`` to the duration field declaration to use.  Each
normalized row keeps:

* the canonical duration value (milliseconds by default),
* the unit and factor used for the conversion,
* the original field name, index, occurrence, and path,
* every declaration that shared the selected field name.

Versions without a mapping keep their original declarations.  A row that has
already been normalized is never normalized a second time.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union
from xml.etree import ElementTree

CANONICAL_FIELD_PREFIX = "duration_"

CANONICAL_ALIASES = {
    "duration_ms": (
        "duration_ms",
        "duration_msec",
        "duration_milliseconds",
        "DurationMs",
        "DurationMilliseconds",
        "duration",
        "elapsed_ms",
    ),
    "duration_us": (
        "duration_us",
        "duration_usec",
        "duration_microseconds",
        "DurationUs",
        "DurationMicroseconds",
        "elapsed_us",
    ),
    "duration_ns": (
        "duration_ns",
        "duration_nsec",
        "duration_nanoseconds",
        "DurationNs",
        "DurationNanoseconds",
        "elapsed_ns",
    ),
    "duration_s": (
        "duration_s",
        "duration_sec",
        "duration_seconds",
        "DurationSeconds",
        "elapsed_s",
    ),
}

STATUS_MIGRATED = "migrated"
STATUS_ALREADY_MIGRATED = "already_migrated"
STATUS_MISSING = "missing"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN_VERSION = "unknown_version"

PROCESSED_STATUSES = (
    STATUS_MIGRATED,
    STATUS_ALREADY_MIGRATED,
    STATUS_MISSING,
    STATUS_INVALID,
    STATUS_UNKNOWN_VERSION,
)


class EventRow(dict):
    """
    A row of the unified table.

    It behaves like a normal dict, but numeric values for the canonical
    duration column can also be read through common alias spellings (for
    example ``row["duration_us"]`` and ``row["duration_microseconds"]``).
    Serialization only emits the canonical key.
    """

    def __init__(self, *args: Any, canonical_field: Optional[str] = None, **kwargs: Any):
        super(EventRow, self).__init__(*args, **kwargs)
        self.canonical_field = canonical_field

    def __missing__(self, key: Any) -> Any:
        migration = dict.get(self, "migration")
        if isinstance(migration, Mapping):
            aliases = {
                "migration_status": "status",
                "conversion_factor": "factor",
                "original_field": "source_field",
                "original_index": "source_index",
                "original_occurrence": "source_occurrence",
                "original_path": "source_path",
                "original_unit": "source_unit",
            }
            name = aliases.get(key, key)
            if name in migration:
                return migration[name]
        if key in ("fields", "original_fields", "original_declarations", "declarations"):
            if dict.__contains__(self, "declared_fields"):
                return dict.get(self, "declared_fields")
        if self.canonical_field is not None and dict.__contains__(self, self.canonical_field):
            aliases = CANONICAL_ALIASES.get(self.canonical_field, (self.canonical_field,))
            if key in aliases:
                return dict.get(self, self.canonical_field)
            for unit, names in CANONICAL_ALIASES.items():
                if key not in names:
                    continue
                value = dict.get(self, self.canonical_field)
                if value is None:
                    return None
                try:
                    factor = unit_factor(self.canonical_field.split("_", 1)[1], unit.split("_", 1)[1])
                except (InvalidMappingError, ValueError):
                    raise KeyError(key)
                return _convert_number(value, factor)
        raise KeyError(key)

    def __contains__(self, key: Any) -> bool:
        if dict.__contains__(self, key):
            return True
        try:
            self[key]
        except KeyError:
            return False
        return True

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


# Number of microseconds in one unit.
_UNIT_TO_MICROSECONDS = {
    "ns": Fraction(1, 1000),
    "nsec": Fraction(1, 1000),
    "nanosec": Fraction(1, 1000),
    "nanosecond": Fraction(1, 1000),
    "nanoseconds": Fraction(1, 1000),
    "us": Fraction(1, 1),
    "usec": Fraction(1, 1),
    "microsec": Fraction(1, 1),
    "microsecond": Fraction(1, 1),
    "microseconds": Fraction(1, 1),
    "ms": Fraction(1000, 1),
    "msec": Fraction(1000, 1),
    "millisec": Fraction(1000, 1),
    "millisecond": Fraction(1000, 1),
    "milliseconds": Fraction(1000, 1),
    "s": Fraction(1000000, 1),
    "sec": Fraction(1000000, 1),
    "second": Fraction(1000000, 1),
    "seconds": Fraction(1000000, 1),
}


class EventTableError(Exception):
    """Base exception for event table construction."""


class InvalidMappingError(EventTableError):
    """Raised when a provider version mapping is malformed."""


class AmbiguousDurationField(EventTableError):
    """
    Raised when a field name occurs more than once and the mapping does not
    identify the declaration to use.

    Older provider versions can declare the same field name twice.  The
    mapping has to select one with ``occurrence`` or ``index``; silently
    choosing the first one would make the conversion depend on luck.
    """

    def __init__(self, field_name: str, positions: Sequence[Mapping[str, Any]]):
        self.field_name = field_name
        self.positions = list(positions)
        details = ", ".join(
            "index={index}, path={path}, occurrence={occurrence}".format(**position) for position in self.positions
        )
        super(AmbiguousDurationField, self).__init__(
            "field {!r} is declared more than once ({}); "
            "select one with occurrence= or index= in the version mapping".format(field_name, details)
        )


def normalize_unit(unit: Any) -> str:
    """
    Normalize a duration unit name to ``ns``, ``us``, ``ms``, or ``s``.
    """
    if unit is None:
        raise InvalidMappingError("duration unit is required")
    key = str(unit).strip().lower().replace("µ", "u").replace("μ", "u")
    if key not in _UNIT_TO_MICROSECONDS:
        raise InvalidMappingError("unsupported duration unit: {!r}".format(unit))
    if key in ("nanosec", "nanosecond", "nanoseconds"):
        return "ns"
    if key in ("microsec", "microsecond", "microseconds"):
        return "us"
    if key in ("millisec", "millisecond", "milliseconds"):
        return "ms"
    if key in ("sec", "second", "seconds"):
        return "s"
    return key


def unit_factor(source_unit: Any, target_unit: Any) -> Fraction:
    """
    Return the factor that converts values in ``source_unit`` to ``target_unit``.
    """
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    return _UNIT_TO_MICROSECONDS[source] / _UNIT_TO_MICROSECONDS[target]


def canonical_field_name(unit: Any) -> str:
    return "{}{}".format(CANONICAL_FIELD_PREFIX, normalize_unit(unit))


def _public_factor(factor: Fraction) -> Union[int, float]:
    if factor.denominator == 1:
        return factor.numerator
    return float(factor)


def _public_number(value: Any) -> Any:
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return value.numerator
        return float(value)
    return value


def _parse_number(value: Any) -> Union[int, float]:
    """
    Parse a declared duration value.

    An absent value and an empty value raise ``ValueError``; the caller
    keeps those distinct from a declared numeric zero.
    """
    if value is None or isinstance(value, bool):
        raise ValueError("not a numeric duration")
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction):
        return _public_number(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("not a finite duration")
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("not a finite duration")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty duration")
        try:
            return int(text, 10)
        except ValueError:
            pass
        try:
            decimal = Decimal(text)
        except InvalidOperation:
            raise ValueError("not a numeric duration")
        if not decimal.is_finite():
            raise ValueError("not a finite duration")
        if decimal == decimal.to_integral_value():
            return int(decimal)
        return float(decimal)
    raise ValueError("not a numeric duration")


def _convert_number(value: Union[int, float], factor: Fraction) -> Union[int, float]:
    if factor == 1:
        return value
    result = Fraction(value) * factor
    if result.denominator == 1:
        return result.numerator
    return float(result)


@dataclass(frozen=True)
class DeclaredField:
    """
    One field declaration in its original order.

    ``index`` is the position in the event's declared field sequence.
    ``occurrence`` is the position among declarations with the same name
    (zero based), which distinguishes duplicate names.  ``path`` is a stable
    location such as ``EventData/Data[3]``.
    """

    name: Optional[str]
    value: Any
    index: int
    path: str
    occurrence: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "index": self.index,
            "occurrence": self.occurrence,
            "path": self.path,
        }

    def as_source(self, unit: Optional[str] = None) -> Dict[str, Any]:
        source = {
            "name": self.name,
            "index": self.index,
            "occurrence": self.occurrence,
            "path": self.path,
        }
        if unit is not None:
            source["unit"] = normalize_unit(unit)
        return source


@dataclass(frozen=True)
class DeclaredEvent:
    """
    An event before normalization, with all field declarations preserved.
    """

    provider: Optional[str]
    version: Any
    fields: Tuple[DeclaredField, ...]
    event_id: Any = None
    record_id: Any = None
    timestamp: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def field_dicts(self) -> List[Dict[str, Any]]:
        return [declared.as_dict() for declared in self.fields]


@dataclass(frozen=True)
class DurationRule:
    """
    Select and describe a duration field declaration for one provider version.

    Args:
      field: the declared field name, such as ``Duration``.
      source_unit: the unit declared by this provider version.
      occurrence: optional zero-based occurrence among same-name fields.
      index: optional position in the event's declared field sequence.
      path: optional exact declared path, such as ``EventData/Data[1]``.
      canonical_field: optional override for the output column name.
    """

    field: str
    source_unit: str
    occurrence: Optional[int] = None
    index: Optional[int] = None
    path: Optional[str] = None
    canonical_field: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.field:
            raise InvalidMappingError("duration field name is required")
        normalize_unit(self.source_unit)
        if self.occurrence is not None and self.occurrence < 0:
            raise InvalidMappingError("occurrence must be zero or greater")
        if self.index is not None and self.index < 0:
            raise InvalidMappingError("index must be zero or greater")

    def select(self, fields: Sequence[DeclaredField]) -> Tuple[Optional[DeclaredField], List[DeclaredField]]:
        matches = [declared for declared in fields if declared.name == self.field]

        if self.path is not None:
            for declared in fields:
                if declared.path == self.path:
                    if declared.name != self.field:
                        return None, matches
                    if self.occurrence is not None and declared.occurrence != self.occurrence:
                        return None, matches
                    return declared, matches
            return None, matches

        if self.index is not None:
            if self.index >= len(fields):
                return None, matches
            selected = fields[self.index]
            if selected.name != self.field:
                return None, matches
            if self.occurrence is not None and selected.occurrence != self.occurrence:
                return None, matches
            return selected, matches

        if self.occurrence is not None:
            if self.occurrence >= len(matches):
                return None, matches
            return matches[self.occurrence], matches

        if not matches:
            return None, matches
        if len(matches) > 1:
            raise AmbiguousDurationField(self.field, [match.as_source() for match in matches])
        return matches[0], matches


def _rule_from_value(value: Any) -> DurationRule:
    if isinstance(value, DurationRule):
        return value
    if isinstance(value, str):
        raise InvalidMappingError(
            "duration rule {!r} must include source_unit, for example "
            '{{"field": {!r}, "source_unit": "ms"}}'.format(value, value)
        )
    if not isinstance(value, Mapping):
        raise InvalidMappingError("duration rule must be a mapping, got {!r}".format(value))

    if "duration" in value and isinstance(value["duration"], Mapping):
        value = value["duration"]
    elif "duration" in value and isinstance(value["duration"], str) and "field" not in value:
        value = dict(value)
        value["field"] = value["duration"]

    field_name = value.get("field", value.get("name", value.get("source_field", value.get("declared_name"))))
    source_unit = value.get(
        "source_unit",
        value.get("unit", value.get("from_unit", value.get("declared_unit", value.get("scale")))),
    )
    occurrence = value.get("occurrence")
    index = value.get("index", value.get("field_index", value.get("source_index")))
    if index is None and occurrence is None:
        index = value.get("position")
    path = value.get("path", value.get("source_path"))
    canonical = value.get("canonical_field", value.get("canonical_name"))
    if canonical is None:
        target = value.get("target_unit", value.get("to_unit", value.get("output_unit")))
        if target is not None:
            canonical = canonical_field_name(target)

    if field_name is None:
        raise InvalidMappingError("duration rule is missing a field name: {!r}".format(value))
    if source_unit is None:
        raise InvalidMappingError("duration rule is missing a source unit: {!r}".format(value))
    if isinstance(occurrence, str) and occurrence.strip().isdigit():
        occurrence = int(occurrence)
    if isinstance(index, str) and index.strip().isdigit():
        index = int(index)

    return DurationRule(
        field=str(field_name),
        source_unit=str(source_unit),
        occurrence=occurrence,
        index=index,
        path=str(path) if path is not None else None,
        canonical_field=str(canonical) if canonical is not None else None,
    )


class VersionMap(object):
    """
    Explicit ``(provider, version) -> DurationRule`` mapping.

    There is intentionally no wildcard or "nearest version" fallback: an
    unmapped provider version must retain its original declaration instead of
    receiving a guessed conversion.
    """

    def __init__(self, rules: Optional[Any] = None):
        self._rules: Dict[Tuple[str, Any], DurationRule] = {}
        self.canonical_unit: Optional[str] = None
        if rules is not None:
            self.update(rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __contains__(self, item: Tuple[Any, Any]) -> bool:
        provider, version = item
        return self._key(provider, version) in self._rules

    @staticmethod
    def _version_key(version: Any) -> Any:
        if isinstance(version, bool):
            return version
        if isinstance(version, int):
            return version
        if isinstance(version, float) and version.is_integer():
            return int(version)
        if isinstance(version, str):
            text = version.strip()
            if not text:
                return text
            try:
                return int(text, 10)
            except ValueError:
                return text
        return version

    @classmethod
    def _key(cls, provider: Any, version: Any) -> Tuple[str, Any]:
        return (str(provider), cls._version_key(version))

    def add(self, provider: Any, version: Any, rule: Any) -> None:
        versions = version if isinstance(version, (list, tuple, set)) else [version]
        duration_rule = _rule_from_value(rule)
        for item in versions:
            self._rules[self._key(provider, item)] = duration_rule

    def update(self, rules: Any) -> None:
        if isinstance(rules, VersionMap):
            self._rules.update(rules._rules)
            if rules.canonical_unit is not None:
                self.canonical_unit = rules.canonical_unit
            return

        if isinstance(rules, Mapping):
            if "canonical_unit" in rules:
                self.canonical_unit = normalize_unit(rules["canonical_unit"])
            if "providers" in rules:
                providers = rules["providers"]
                if not isinstance(providers, Mapping):
                    raise InvalidMappingError("providers must be a mapping")
                self.update(providers)
                return

            for key, value in rules.items():
                if key == "canonical_unit":
                    continue
                if isinstance(key, tuple):
                    if len(key) != 2:
                        raise InvalidMappingError("mapping keys must be (provider, version) pairs")
                    self.add(key[0], key[1], value)
                elif isinstance(value, Mapping) and not any(
                    name in value for name in ("field", "name", "source_field", "declared_name")
                ):
                    for version, rule in value.items():
                        self.add(key, version, rule)
                else:
                    raise InvalidMappingError(
                        "cannot interpret mapping entry {!r}; use (provider, version) keys "
                        "or nest versions below the provider name".format(key)
                    )
            return

        if isinstance(rules, (list, tuple)):
            for entry in rules:
                if isinstance(entry, Mapping):
                    provider = entry.get("provider")
                    version = entry.get("version", entry.get("versions"))
                    rule = entry.get("rule", entry.get("duration", entry))
                    if provider is None or version is None:
                        raise InvalidMappingError("mapping entry needs provider and version: {!r}".format(entry))
                    self.add(provider, version, rule)
                elif isinstance(entry, (list, tuple)) and len(entry) == 3:
                    self.add(entry[0], entry[1], entry[2])
                else:
                    raise InvalidMappingError("cannot interpret mapping entry: {!r}".format(entry))
            return

        raise InvalidMappingError("cannot interpret version mapping: {!r}".format(rules))

    def lookup(self, provider: Any, version: Any) -> Optional[DurationRule]:
        if provider is None:
            return None
        return self._rules.get(self._key(provider, version))

    def rules(self) -> Dict[Tuple[str, Any], DurationRule]:
        return dict(self._rules)

    @classmethod
    def from_dict(cls, value: Any) -> "VersionMap":
        return cls(value)

    @classmethod
    def from_json_file(cls, path: Union[str, "os.PathLike[str]"]) -> "VersionMap":
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls(payload)


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _child(element: Any, name: str) -> Optional[Any]:
    for candidate in list(element):
        if _local_name(candidate.tag) == name:
            return candidate
    return None


def _element_text(element: Any) -> Optional[str]:
    if element is None or element.text is None:
        return None
    return element.text


def _assign_occurrences(fields: Sequence[DeclaredField]) -> Tuple[DeclaredField, ...]:
    seen: Dict[Optional[str], int] = defaultdict(int)
    result: List[DeclaredField] = []
    for declared in fields:
        occurrence = seen[declared.name]
        seen[declared.name] += 1
        result.append(replace(declared, occurrence=occurrence))
    return tuple(result)


def declared_event_from_xml(xml: Union[str, bytes, Any]) -> DeclaredEvent:
    """
    Parse an event XML document or element into a :class:`DeclaredEvent`.
    """
    if hasattr(xml, "tag"):
        root = xml
    else:
        root = ElementTree.fromstring(xml)

    system = _child(root, "System")
    provider_name = None
    provider_guid = None
    version = None
    event_id = None
    record_id = None
    timestamp = None

    if system is not None:
        provider = _child(system, "Provider")
        if provider is not None:
            provider_name = provider.attrib.get("Name")
            provider_guid = provider.attrib.get("Guid")
        version = _element_text(_child(system, "Version"))
        event_id = _element_text(_child(system, "EventID"))
        record_id = _element_text(_child(system, "EventRecordID"))
        created = _child(system, "TimeCreated")
        if created is not None:
            timestamp = created.attrib.get("SystemTime")

    fields: List[DeclaredField] = []

    event_data = _child(root, "EventData")
    if event_data is not None:
        for position, child in enumerate(list(event_data)):
            tag = _local_name(child.tag)
            if tag not in ("Data", "Binary"):
                continue
            name = child.attrib.get("Name")
            if name is None and tag == "Binary":
                name = "Binary"
            fields.append(
                DeclaredField(
                    name=name,
                    value=child.text,
                    index=len(fields),
                    path="EventData/{}[{}]".format(tag, position),
                )
            )

    user_data = _child(root, "UserData")
    if user_data is not None:

        def walk(element: Any, path: str) -> None:
            children = list(element)
            if not children:
                name = element.attrib.get("Name", _local_name(element.tag))
                fields.append(
                    DeclaredField(
                        name=name,
                        value=element.text,
                        index=len(fields),
                        path=path,
                    )
                )
                return
            for position, child in enumerate(children):
                child_name = _local_name(child.tag)
                walk(child, path + "/{}[{}]".format(child_name, position))

        walk(user_data, "UserData")

    metadata: Dict[str, Any] = {}
    if provider_guid is not None:
        metadata["provider_guid"] = provider_guid
    for name in ("Channel", "Computer"):
        value = _element_text(_child(system, name)) if system is not None else None
        if value is not None:
            metadata[name.lower()] = value

    return DeclaredEvent(
        provider=provider_name,
        version=version,
        event_id=event_id,
        record_id=record_id,
        timestamp=timestamp,
        fields=_assign_occurrences(fields),
        metadata=metadata,
    )


def _field_from_entry(entry: Any, index: int) -> DeclaredField:
    if isinstance(entry, DeclaredField):
        return entry
    if isinstance(entry, Mapping):
        name = entry.get(
            "name",
            entry.get("Name", entry.get("@Name", entry.get("field", entry.get("source_field", entry.get("Field"))))),
        )
        if "value" in entry:
            value = entry["value"]
        elif "Value" in entry:
            value = entry["Value"]
        elif "#text" in entry:
            value = entry["#text"]
        else:
            value = entry.get("@text")
        position = entry.get("index", entry.get("position", entry.get("Index", index)))
        tag = entry.get("tag", entry.get("Tag", "Data"))
        path = entry.get("path", entry.get("Path", "EventData/{}[{}]".format(tag, position)))
        return DeclaredField(name=name, value=value, index=int(position), path=str(path))
    if isinstance(entry, (list, tuple)):
        if len(entry) < 2:
            raise EventTableError("field entries need at least a name and a value: {!r}".format(entry))
        name, value = entry[0], entry[1]
        position = int(entry[2]) if len(entry) > 2 else index
        path = str(entry[3]) if len(entry) > 3 else "EventData/Data[{}]".format(position)
        return DeclaredField(name=name, value=value, index=position, path=path)
    raise EventTableError("cannot interpret field entry: {!r}".format(entry))


def _flatten_fields(raw: Any, path: str, fields: List[DeclaredField]) -> None:
    if isinstance(raw, Mapping):
        if any(name in raw for name in ("@Name", "name", "Name")) and "#text" in raw:
            fields.append(_field_from_entry(raw, len(fields)))
            return
        for name, value in raw.items():
            if str(name).startswith("@"):
                continue
            child_path = "{}/{}".format(path, name) if path else str(name)
            if isinstance(value, (list, tuple)):
                for item in value:
                    _flatten_fields(item, child_path, fields)
            else:
                _flatten_fields(value, child_path, fields)
        return
    if isinstance(raw, (list, tuple)):
        for item in raw:
            _flatten_fields(item, path, fields)
        return
    fields.append(DeclaredField(name=path.rsplit("/", 1)[-1], value=raw, index=len(fields), path=path))


def _fields_from_mapping(raw_fields: Any) -> List[DeclaredField]:
    fields: List[DeclaredField] = []
    if raw_fields is None:
        return fields
    if isinstance(raw_fields, Mapping):
        if any(
            name in raw_fields for name in ("@Name", "name", "Name", "field", "source_field", "value", "Value", "#text")
        ):
            fields.append(_field_from_entry(raw_fields, 0))
            return fields
        for name, value in raw_fields.items():
            if str(name).startswith("@"):
                continue
            if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
                for item in value:
                    if isinstance(item, Mapping) and ("@Name" in item or "#text" in item):
                        fields.append(_field_from_entry(item, len(fields)))
                    else:
                        fields.append(_field_from_entry((name, item), len(fields)))
            elif isinstance(value, Mapping) and ("@Name" in value or "#text" in value):
                fields.append(_field_from_entry(value, len(fields)))
            else:
                _flatten_fields(value, str(name), fields)
        return fields
    if isinstance(raw_fields, (list, tuple)):
        for entry in raw_fields:
            fields.append(_field_from_entry(entry, len(fields)))
        return fields
    raise EventTableError("fields must be a mapping or a sequence")


def declared_event_from_mapping(event: Mapping[str, Any]) -> DeclaredEvent:
    """
    Build a :class:`DeclaredEvent` from a mapping.

    Accepted metadata aliases include ``provider``/``provider_name``,
    ``version``/``provider_version``, ``event_id``/``EventID``,
    ``record_id``/``EventRecordID``, and ``timestamp``/``TimeCreated``.
    Field declarations may be supplied as ``fields`` or ``declared_fields``.
    """
    if not isinstance(event, Mapping):
        raise EventTableError("event must be a mapping, got {!r}".format(event))

    provider = event.get(
        "provider",
        event.get("provider_name", event.get("Provider", event.get("ProviderName", event.get("providerName")))),
    )
    version = event.get(
        "version",
        event.get(
            "provider_version",
            event.get("ProviderVersion", event.get("providerVersion", event.get("Version"))),
        ),
    )
    event_id = event.get("event_id", event.get("EventID", event.get("eventId", event.get("EventId"))))
    record_id = event.get(
        "record_id",
        event.get("EventRecordID", event.get("recordId", event.get("RecordID", event.get("recordNumber")))),
    )
    timestamp = event.get(
        "timestamp",
        event.get("TimeCreated", event.get("time_created", event.get("system_time", event.get("SystemTime")))),
    )

    raw_fields = event.get("fields", event.get("declared_fields", event.get("declarations")))
    if raw_fields is None:
        raw_fields = event.get("event_data", event.get("EventData"))
    fields = _assign_occurrences(_fields_from_mapping(raw_fields))

    metadata = dict(event.get("metadata", {}))
    for name in ("channel", "computer", "provider_guid"):
        if name in event and name not in metadata:
            metadata[name] = event[name]

    return DeclaredEvent(
        provider=provider,
        version=version,
        event_id=event_id,
        record_id=record_id,
        timestamp=timestamp,
        fields=fields,
        metadata=metadata,
    )


def _coerce_declared_event(event: Any) -> DeclaredEvent:
    if isinstance(event, DeclaredEvent):
        return event
    if isinstance(event, Mapping):
        return declared_event_from_mapping(event)
    if isinstance(event, (str, bytes)):
        return declared_event_from_xml(event)
    if hasattr(event, "xml"):
        return declared_event_from_xml(event.xml())
    raise EventTableError("cannot interpret event: {!r}".format(event))


def _processed_marker(event: Mapping[str, Any]) -> Optional[Tuple[str, Any, Dict[str, Any]]]:
    migration = event.get("migration")
    if not isinstance(migration, Mapping):
        return None
    if migration.get("status") not in PROCESSED_STATUSES:
        return None
    canonical = migration.get("canonical_field")
    if canonical is None:
        for key in event:
            if str(key).startswith(CANONICAL_FIELD_PREFIX):
                canonical = key
                break
    if canonical is None:
        return None
    canonical = str(canonical)
    if not dict.__contains__(event, canonical):
        # Unknown versions and missing fields have no canonical value to
        # preserve; passing them back through normalization must not create
        # one (least of all a zero).
        return canonical, None, dict(migration)
    return canonical, dict.__getitem__(event, canonical), dict(migration)


class EventTable(object):
    """
    Normalize versioned duration declarations into one event table.

    Args:
      mapping: a :class:`VersionMap`, or any value accepted by its constructor.
      canonical_unit: the unit of the unified duration column.  Defaults to
        milliseconds, matching the older provider declaration, so newer
        microsecond values scale down by an exact factor of 1000.
    """

    def __init__(self, mapping: Optional[Any] = None, canonical_unit: Optional[Any] = None):
        self.mapping = mapping if isinstance(mapping, VersionMap) else VersionMap(mapping)
        if canonical_unit is None:
            canonical_unit = self.mapping.canonical_unit or "ms"
        self.canonical_unit = normalize_unit(canonical_unit)
        self.default_canonical_field = canonical_field_name(self.canonical_unit)
        self.rows: List[Dict[str, Any]] = []
        self.migrations: List[Dict[str, Any]] = []
        self.conversions: List[Dict[str, Any]] = []

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self.rows)

    @classmethod
    def from_json_file(
        cls, path: Union[str, "os.PathLike[str]"], canonical_unit: Optional[Any] = None
    ) -> "EventTable":
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        unit = canonical_unit if canonical_unit is not None else payload.get("canonical_unit")
        return cls(VersionMap(payload), canonical_unit=unit)

    @classmethod
    def from_evtx(
        cls,
        path: Union[str, "os.PathLike[str]"],
        mapping: Optional[Any] = None,
        canonical_unit: Optional[Any] = None,
    ) -> "EventTable":
        """
        Normalize every record in an EVTX file.
        """
        table = cls(mapping, canonical_unit=canonical_unit)
        table.normalize_many(iter_declared_events(path))
        return table

    def normalize_file(self, path: Union[str, "os.PathLike[str]"]) -> List[Dict[str, Any]]:
        return self.normalize_many(iter_declared_events(path))

    def _base_row(self, event: DeclaredEvent) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "provider": event.provider,
            "provider_version": event.version,
            "event_id": event.event_id,
            "record_id": event.record_id,
            "timestamp": event.timestamp,
            "declared_fields": event.field_dicts(),
        }
        for name, value in event.metadata.items():
            row.setdefault(name, value)
        return row

    @staticmethod
    def _reprocessed_row(
        event: Mapping[str, Any], canonical: str, value: Any, migration: Dict[str, Any]
    ) -> Dict[str, Any]:
        row = EventRow(event, canonical_field=canonical)
        if value is not None or dict.__contains__(event, canonical):
            row[canonical] = value
        migration = dict(migration)
        migration["reprocessed"] = True
        migration["additional_factor"] = 1
        migration["additional_conversion"] = None
        migration["status"] = (
            STATUS_ALREADY_MIGRATED if migration.get("status") == STATUS_MIGRATED else migration.get("status")
        )
        row["migration"] = migration
        return row

    def _finish(self, row: Dict[str, Any], migration: Mapping[str, Any]) -> Dict[str, Any]:
        migration = dict(migration)
        migration["declared_fields"] = row["declared_fields"]
        row["migration"] = migration
        self.rows.append(row)
        self.migrations.append(migration)
        return row

    def normalize(self, event: Any) -> Dict[str, Any]:
        """
        Normalize one event and append the row to the table.

        A row returned by this method can be passed back to ``normalize``;
        the canonical value is preserved and no second scaling is applied.
        """
        if isinstance(event, Mapping):
            marker = _processed_marker(event)
            if marker is not None:
                canonical, value, migration = marker
                row = self._reprocessed_row(event, canonical, value, migration)
                migration = dict(row["migration"])
                migration["declared_fields"] = row.get("declared_fields", [])
                row["migration"] = migration
                self.rows.append(row)
                self.migrations.append(migration)
                return row

        declared = _coerce_declared_event(event)
        base = self._base_row(declared)
        rule = self.mapping.lookup(declared.provider, declared.version)

        if rule is None:
            # No evidence, no correction: keep only the declarations as stored.
            canonical = self.default_canonical_field
            migration = {
                "status": STATUS_UNKNOWN_VERSION,
                "canonical_field": canonical,
                "canonical_unit": self.canonical_unit,
                "source": None,
                "source_field": None,
                "source_index": None,
                "source_occurrence": None,
                "source_path": None,
                "source_unit": None,
                "factor": None,
                "declared_value": None,
                "converted_value": None,
                "converted": False,
                "all_sources": [],
                "declared_fields": base["declared_fields"],
                "reason": "no version mapping; original declarations left unchanged",
            }
            row = EventRow(base, canonical_field=canonical)
            return self._finish(row, migration)

        canonical = rule.canonical_field or canonical_field_name(self.canonical_unit)
        row = EventRow(base, canonical_field=canonical)
        factor = unit_factor(rule.source_unit, self.canonical_unit)
        selected, matches = rule.select(declared.fields)
        all_sources = [match.as_source(unit=rule.source_unit) for match in matches]

        if selected is None:
            # A missing declaration is not the same fact as a declared zero.
            row[canonical] = None
            migration = {
                "status": STATUS_MISSING,
                "canonical_field": canonical,
                "canonical_unit": self.canonical_unit,
                "source": {
                    "name": rule.field,
                    "index": rule.index,
                    "occurrence": rule.occurrence,
                    "path": rule.path,
                    "unit": normalize_unit(rule.source_unit),
                },
                "source_field": rule.field,
                "source_index": rule.index,
                "source_occurrence": rule.occurrence,
                "source_path": rule.path,
                "source_unit": normalize_unit(rule.source_unit),
                "factor": None,
                "declared_value": None,
                "converted_value": None,
                "converted": False,
                "all_sources": all_sources,
                "declared_fields": row["declared_fields"],
                "reason": "field not declared in this event",
            }
            return self._finish(row, migration)

        source = selected.as_source(unit=rule.source_unit)
        migration = {
            "status": STATUS_MIGRATED,
            "canonical_field": canonical,
            "canonical_unit": self.canonical_unit,
            "source": source,
            "source_field": selected.name,
            "source_index": selected.index,
            "source_occurrence": selected.occurrence,
            "source_path": selected.path,
            "source_unit": normalize_unit(rule.source_unit),
            "factor": _public_factor(factor),
            "declared_value": selected.value,
            "converted_value": None,
            "converted": factor != 1,
            "all_sources": all_sources,
            "declared_fields": row["declared_fields"],
        }

        try:
            parsed = _parse_number(selected.value)
        except ValueError:
            row[canonical] = None
            migration["status"] = STATUS_INVALID
            migration["reason"] = "declared value is not numeric; it is not treated as zero"
            row = self._finish(row, migration)
            self.conversions.append(self._conversion_record(declared, migration))
            return row

        converted = _convert_number(parsed, factor)
        row[canonical] = converted
        migration["converted_value"] = converted
        row = self._finish(row, migration)
        self.conversions.append(self._conversion_record(declared, migration))
        return row

    @staticmethod
    def _conversion_record(event: DeclaredEvent, migration: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "provider": event.provider,
            "provider_version": event.version,
            "event_id": event.event_id,
            "record_id": event.record_id,
            "canonical_field": migration.get("canonical_field"),
            "source": migration.get("source"),
            "source_unit": migration.get("source_unit"),
            "canonical_unit": migration.get("canonical_unit"),
            "factor": migration.get("factor"),
            "declared_value": migration.get("declared_value"),
            "converted_value": migration.get("converted_value"),
        }

    def normalize_many(self, events: Iterable[Any]) -> List[Dict[str, Any]]:
        return [self.normalize(event) for event in events]

    add = normalize
    extend = normalize_many

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.rows, indent=indent, default=str)

    def write_jsonl(self, handle: Any) -> None:
        for row in self.rows:
            handle.write(json.dumps(row, default=str))
            handle.write("\n")

    def to_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        columns = [
            "provider",
            "provider_version",
            "event_id",
            "record_id",
            "timestamp",
            self.default_canonical_field,
            "migration_status",
            "source_name",
            "source_index",
            "source_occurrence",
            "source_path",
            "source_unit",
            "factor",
            "declared_value",
            "converted_value",
            "declared_fields",
        ]
        writer.writerow(columns)
        for row in self.rows:
            migration = row.get("migration", {})
            source = migration.get("source") or {}
            writer.writerow(
                [
                    row.get("provider"),
                    row.get("provider_version"),
                    row.get("event_id"),
                    row.get("record_id"),
                    row.get("timestamp"),
                    row.get(self.default_canonical_field),
                    migration.get("status"),
                    source.get("name"),
                    source.get("index"),
                    source.get("occurrence"),
                    source.get("path"),
                    source.get("unit"),
                    migration.get("factor"),
                    migration.get("declared_value"),
                    migration.get("converted_value"),
                    json.dumps(row.get("declared_fields", []), default=str),
                ]
            )
        return output.getvalue()


def normalize_event(event: Any, mapping: Optional[Any] = None, canonical_unit: Optional[Any] = None) -> Dict[str, Any]:
    """
    Normalize a single event without retaining a table.
    """
    return EventTable(mapping, canonical_unit=canonical_unit).normalize(event)


def normalize_events(
    events: Iterable[Any], mapping: Optional[Any] = None, canonical_unit: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """
    Normalize events and return the unified rows.
    """
    return EventTable(mapping, canonical_unit=canonical_unit).normalize_many(events)


def _iter_log_events(log: Any) -> Iterator[DeclaredEvent]:
    for record in log.records():
        yield declared_event_from_xml(record.xml())


def iter_declared_events(source: Any) -> Iterator[DeclaredEvent]:
    """
    Yield declared events from an EVTX path, an ``Evtx`` instance, an
    iterable of records, event mappings, XML documents, or
    :class:`DeclaredEvent` objects.
    """
    if isinstance(source, DeclaredEvent):
        yield source
        return

    if isinstance(source, (str, bytes, os.PathLike)):
        path = os.fspath(source)
        if isinstance(path, bytes):
            path = path.decode("utf-8")
        if path.lower().endswith(".evtx"):
            import Evtx.Evtx as evtx

            with evtx.Evtx(path) as log:
                yield from _iter_log_events(log)
            return
        yield declared_event_from_xml(path)
        return

    if hasattr(source, "records") and callable(source.records):
        yield from _iter_log_events(source)
        return
    if hasattr(source, "get_file_header") and callable(source.get_file_header):
        yield from _iter_log_events(source)
        return

    try:
        iterator = iter(source)
    except TypeError:
        yield _coerce_declared_event(source)
        return

    for event in iterator:
        if isinstance(event, DeclaredEvent):
            yield event
        elif isinstance(event, Mapping):
            yield declared_event_from_mapping(event)
        elif hasattr(event, "xml"):
            yield declared_event_from_xml(event.xml())
        elif isinstance(event, (str, bytes)):
            yield declared_event_from_xml(event)
        else:
            raise EventTableError("cannot interpret event: {!r}".format(event))
