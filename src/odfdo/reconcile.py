"""Reconciliation of overlapping source tables into facts and sources.

Several delivered tables may list the same business record. Every delivered
row carries a composite key, a version and, optionally, an override
declaration or an explicit deletion marker. The reconciler merges such
deliveries into two tables:

- the *facts* table: one row per surviving business key, marked as
  ``active``, ``deleted`` or ``conflict``;
- the *sources* table: one row per delivered record, kept as an audit
  trail of everything that was received.

Identity is defined solely by the composite key values. Reordering the key
columns in a row never changes identity; two rows with identical payload
values but different keys stay two distinct facts. A table that is simply
not delivered implies nothing: its records are never considered deleted,
only an explicit tombstone deletes a fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import total_ordering
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

__all__ = [
    "Conflict",
    "Fact",
    "Reconciler",
    "ReconciliationResult",
    "SourceRecord",
    "reconcile_tables",
]

ACTIVE = "active"
CONFLICT = "conflict"
DELETED = "deleted"

_TRUE_TOKENS = frozenset({"true", "t", "yes", "y", "1", "delete", "deleted"})
_FALSE_TOKENS = frozenset({"false", "f", "no", "n", "0", "", "active", "alive"})


def _as_bool(value: Any, column: str, table: str) -> bool:
    """Coerce a tombstone or override flag to a boolean."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        msg = f"table {table!r}: cannot interpret {column!r} value {value!r}"
        raise ValueError(msg)
    msg = (
        f"table {table!r}: {column!r} must be a boolean-like value, "
        f"got {type(value).__name__}"
    )
    raise TypeError(msg)


def _version_equal(left: Any, right: Any) -> bool:
    """Return whether two version values identify the same version."""
    try:
        equal = left == right
    except TypeError:
        equal = (
            type(left).__name__,
            repr(left),
        ) == (
            type(right).__name__,
            repr(right),
        )
    return bool(equal)


@total_ordering
class _VersionOrder:
    """Comparable wrapper tolerating versions of unrelated types."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _VersionOrder):
            return NotImplemented
        return _version_equal(self.value, other.value)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _VersionOrder):
            return NotImplemented
        try:
            result = self.value < other.value
        except TypeError:
            left = (type(self.value).__name__, repr(self.value))
            right = (type(other.value).__name__, repr(other.value))
            return left < right
        if isinstance(result, bool):
            return result
        return bool(result)


def _freeze_payload(value: Any) -> Any:
    """Turn a payload value into a hashable snapshot for comparisons."""
    if isinstance(value, dict):
        return tuple(
            (key, _freeze_payload(item)) for key, item in sorted(value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_payload(item) for item in value))
    return value


@dataclass(frozen=True)
class SourceRecord:
    """One record as delivered by one source table.

    Attributes:
        table: identifier of the delivering table.
        sequence: zero-based delivery position across all tables.
        key: composite key values, ordered by the configured key columns.
        version: version declared by the caller for this record.
        data: payload columns copied from the delivered row.
        tombstone: explicit deletion declaration.
        override: explicit declaration that the row overrides same-version
            deliveries.

    """

    table: str
    sequence: int
    key: tuple[Any, ...]
    version: Any
    data: dict[str, Any] = field(hash=False, default_factory=dict)
    tombstone: bool = False
    override: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary view of the source record."""
        return {
            "table": self.table,
            "sequence": self.sequence,
            "key": self.key,
            "version": self.version,
            "tombstone": self.tombstone,
            "override": self.override,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class Conflict:
    """One disagreeing record kept for a same-key conflict."""

    table: str
    sequence: int
    version: Any
    data: dict[str, Any] = field(hash=False, default_factory=dict)
    tombstone: bool = False
    override: bool = False

    @classmethod
    def from_record(cls, record: SourceRecord) -> Conflict:
        """Build a conflict entry from a delivered source record."""
        return cls(
            table=record.table,
            sequence=record.sequence,
            version=record.version,
            data=dict(record.data),
            tombstone=record.tombstone,
            override=record.override,
        )


@dataclass(frozen=True)
class Fact:
    """Reconciled state of one business key.

    Attributes:
        key: composite key values identifying the business record.
        status: ``active``, ``deleted`` or ``conflict``.
        version: winning (highest) version seen for the key.
        data: winning payload; empty for an explicit deletion.
        sources: every delivered record for the key, in delivery order.
        conflicts: disagreeing records at the winning version.
        source_tables: distinct delivering tables, first-seen ordered.

    """

    key: tuple[Any, ...]
    status: str
    version: Any
    data: dict[str, Any] = field(hash=False, default_factory=dict)
    sources: tuple[SourceRecord, ...] = field(default_factory=tuple)
    conflicts: tuple[Conflict, ...] = field(default_factory=tuple)
    source_tables: tuple[str, ...] = field(default_factory=tuple)

    @property
    def deleted(self) -> bool:
        """Whether the fact was explicitly deleted."""
        return self.status == DELETED

    @property
    def is_conflict(self) -> bool:
        """Whether same-version deliveries disagree for the key."""
        return self.status == CONFLICT

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary view of the fact."""
        return {
            "key": self.key,
            "status": self.status,
            "version": self.version,
            "data": dict(self.data),
            "source_tables": self.source_tables,
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True)
class ReconciliationResult:
    """The facts and sources tables produced by a reconciliation."""

    key_columns: tuple[str, ...]
    data_columns: tuple[str, ...]
    delivered_tables: tuple[str, ...]
    facts: tuple[Fact, ...]
    sources: tuple[SourceRecord, ...]

    def fact_for(self, key: tuple[Any, ...]) -> Fact | None:
        """Return the fact for a composite key, or ``None`` if unknown."""
        for fact in self.facts:
            if fact.key == tuple(key):
                return fact
        return None

    @property
    def active_facts(self) -> tuple[Fact, ...]:
        """Return the facts that are currently active."""
        return tuple(fact for fact in self.facts if fact.status == ACTIVE)

    @property
    def deleted_facts(self) -> tuple[Fact, ...]:
        """Return the facts removed by explicit tombstones."""
        return tuple(fact for fact in self.facts if fact.deleted)

    @property
    def conflicting_facts(self) -> tuple[Fact, ...]:
        """Return the facts with unresolved same-key conflicts."""
        return tuple(fact for fact in self.facts if fact.is_conflict)

    @property
    def deleted_keys(self) -> tuple[tuple[Any, ...], ...]:
        """Return the composite keys removed by explicit tombstones."""
        return tuple(fact.key for fact in self.facts if fact.deleted)

    @property
    def conflict_keys(self) -> tuple[tuple[Any, ...], ...]:
        """Return the composite keys with unresolved conflicts."""
        return tuple(fact.key for fact in self.facts if fact.is_conflict)

    def to_fact_rows(self) -> list[dict[str, Any]]:
        """Return the facts table as a list of plain dictionary rows.

        Columns are the key columns, then ``version``, ``status``, the
        payload columns and ``source_tables``. Deleted facts expose no
        payload values.
        """
        rows: list[dict[str, Any]] = []
        for fact in self.facts:
            row: dict[str, Any] = {
                column: fact.key[index] for index, column in enumerate(self.key_columns)
            }
            row["version"] = fact.version
            row["status"] = fact.status
            for column in self.data_columns:
                row[column] = fact.data.get(column) if not fact.deleted else None
            row["source_tables"] = fact.source_tables
            rows.append(row)
        return rows

    def to_source_rows(self) -> list[dict[str, Any]]:
        """Return the sources table as a list of plain dictionary rows."""
        rows: list[dict[str, Any]] = []
        for record in self.sources:
            row = {
                "table": record.table,
                "sequence": record.sequence,
            }
            for index, column in enumerate(self.key_columns):
                row[column] = record.key[index]
            row["version"] = record.version
            row["tombstone"] = record.tombstone
            row["override"] = record.override
            for column in self.data_columns:
                row[column] = record.data.get(column)
            rows.append(row)
        return rows


class Reconciler:
    """Merge overlapping table deliveries into facts and sources.

    Identity is defined by the composite key column values, so reordering
    the key columns in a row never changes the resulting key.
    """

    def __init__(
        self,
        key_columns: Sequence[str],
        *,
        data_columns: Sequence[str] | None = None,
        version_column: str = "version",
        deleted_column: str | None = None,
        override_column: str | None = None,
    ) -> None:
        """Initialize the reconciler.

        Args:
            key_columns: columns forming the composite business key.
            data_columns: payload columns to keep. When omitted, every row
                column outside the key and control columns is kept.
            version_column: column carrying the record version.
            deleted_column: column carrying the explicit deletion marker.
                When omitted, no row can be a tombstone.
            override_column: column carrying the override declaration.
                When omitted, no row claims an override.

        """
        if not key_columns:
            msg = "at least one key column is required"
            raise ValueError(msg)
        self.key_columns = tuple(key_columns)
        self._explicit_data_columns = (
            tuple(data_columns) if data_columns is not None else None
        )
        self.version_column = version_column
        self.deleted_column = deleted_column
        self.override_column = override_column
        self._records: list[SourceRecord] = []
        self._tables: list[str] = []
        self._data_columns: list[str] = []
        self._sequence = 0

    @property
    def data_columns(self) -> tuple[str, ...]:
        """Payload columns retained on each record, first-seen ordered."""
        if self._explicit_data_columns is not None:
            return self._explicit_data_columns
        return tuple(self._data_columns)

    @property
    def delivered_tables(self) -> tuple[str, ...]:
        """Tables actually registered with the reconciler."""
        return tuple(self._tables)

    def register_table(self, table: str, rows: Iterable[Mapping[str, Any]]) -> None:
        """Register one delivered table and its rows.

        Not registering a table means the table was not delivered; the
        absence of a delivery never deletes any record.
        """
        table_id = str(table)
        if table_id in self._tables:
            msg = f"table {table_id!r} already delivered"
            raise ValueError(msg)
        self._tables.append(table_id)
        for row in rows:
            if not isinstance(row, dict):
                msg = (
                    f"table {table_id!r}: rows must be mappings, "
                    f"got {type(row).__name__}"
                )
                raise TypeError(msg)
            self._records.append(self._build_record(table_id, row))

    def _build_record(self, table: str, row: Mapping[str, Any]) -> SourceRecord:
        key = self._extract_key(table, row)
        version = row.get(self.version_column)
        tombstone = (
            _as_bool(row[self.deleted_column], self.deleted_column, table)
            if self.deleted_column is not None and self.deleted_column in row
            else False
        )
        override = (
            _as_bool(row[self.override_column], self.override_column, table)
            if self.override_column is not None and self.override_column in row
            else False
        )
        data = self._extract_data(table, row)
        record = SourceRecord(
            table=table,
            sequence=self._sequence,
            key=key,
            version=version,
            data=data,
            tombstone=tombstone,
            override=override,
        )
        self._sequence += 1
        return record

    def _extract_key(self, table: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
        key_values: list[Any] = []
        for column in self.key_columns:
            if column not in row:
                msg = f"table {table!r}: missing key column {column!r}"
                raise ValueError(msg)
            key_values.append(row[column])
        return tuple(key_values)

    def _extract_data(self, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
        special = set(self.key_columns) | {self.version_column}
        if self.deleted_column is not None:
            special.add(self.deleted_column)
        if self.override_column is not None:
            special.add(self.override_column)
        if self._explicit_data_columns is not None:
            return {
                column: row[column]
                for column in self._explicit_data_columns
                if column in row
            }
        data: dict[str, Any] = {}
        for column, value in row.items():
            if column in special:
                continue
            if column not in self._data_columns:
                self._data_columns.append(column)
            data[column] = value
        return data

    def reconcile(self) -> ReconciliationResult:
        """Build the facts and sources tables from all deliveries."""
        grouped: dict[tuple[Any, ...], list[SourceRecord]] = {}
        key_order: list[tuple[Any, ...]] = []
        for record in self._records:
            if record.key not in grouped:
                grouped[record.key] = []
                key_order.append(record.key)
            grouped[record.key].append(record)
        facts = tuple(self._build_fact(key, grouped[key]) for key in key_order)
        return ReconciliationResult(
            key_columns=self.key_columns,
            data_columns=self.data_columns,
            delivered_tables=tuple(self._tables),
            facts=facts,
            sources=tuple(self._records),
        )

    def _build_fact(self, key: tuple[Any, ...], records: list[SourceRecord]) -> Fact:
        latest_version = max(
            (record.version for record in records),
            key=lambda value: _VersionOrder(value),
        )
        latest = [
            record
            for record in records
            if _version_equal(record.version, latest_version)
        ]
        source_tables = self._source_tables(records)
        if all(record.tombstone for record in latest):
            return Fact(
                key=key,
                status=DELETED,
                version=latest_version,
                data={},
                sources=tuple(records),
                conflicts=(),
                source_tables=source_tables,
            )
        if not self._disagree(latest):
            winner = self._pick_winner(latest)
            return Fact(
                key=key,
                status=ACTIVE,
                version=latest_version,
                data=dict(winner.data),
                sources=tuple(records),
                conflicts=(),
                source_tables=source_tables,
            )
        overrides = [record for record in latest if record.override]
        if overrides and not self._disagree(overrides):
            winner = overrides[-1]
            if winner.tombstone:
                return Fact(
                    key=key,
                    status=DELETED,
                    version=latest_version,
                    data={},
                    sources=tuple(records),
                    conflicts=(),
                    source_tables=source_tables,
                )
            return Fact(
                key=key,
                status=ACTIVE,
                version=latest_version,
                data=dict(winner.data),
                sources=tuple(records),
                conflicts=(),
                source_tables=source_tables,
            )
        candidates = overrides or latest
        winner = self._pick_winner(
            [record for record in candidates if not record.tombstone] or candidates
        )
        return Fact(
            key=key,
            status=CONFLICT,
            version=latest_version,
            data=dict(winner.data),
            sources=tuple(records),
            conflicts=tuple(Conflict.from_record(record) for record in latest),
            source_tables=source_tables,
        )

    @staticmethod
    def _source_tables(
        records: list[SourceRecord],
    ) -> tuple[str, ...]:
        tables: list[str] = []
        for record in records:
            if record.table not in tables:
                tables.append(record.table)
        return tuple(tables)

    @staticmethod
    def _disagree(records: list[SourceRecord]) -> bool:
        """Return whether same-version records claim different outcomes."""
        tombstones = [record for record in records if record.tombstone]
        live = [record for record in records if not record.tombstone]
        if tombstones and live:
            return True
        if tombstones:
            return False
        payloads = {_freeze_payload(record.data) for record in live}
        return len(payloads) > 1

    @staticmethod
    def _pick_winner(records: list[SourceRecord]) -> SourceRecord:
        overrides = [record for record in records if record.override]
        if overrides:
            return overrides[-1]
        return records[-1]


def reconcile_tables(
    tables: Mapping[str, Iterable[Mapping[str, Any]]]
    | Iterable[tuple[str, Iterable[Mapping[str, Any]]]],
    key_columns: Sequence[str],
    *,
    data_columns: Sequence[str] | None = None,
    version_column: str = "version",
    deleted_column: str | None = None,
    override_column: str | None = None,
) -> ReconciliationResult:
    """Reconcile several delivered tables in one call.

    Args:
        tables: either a mapping of table identifier to rows, or an
            iterable of ``(table identifier, rows)`` pairs. Tables absent
            from this argument are simply undelivered and imply no
            deletion.
        key_columns: columns forming the composite business key.
        data_columns: payload columns to keep.
        version_column: column carrying the record version.
        deleted_column: column carrying the explicit deletion marker.
        override_column: column carrying the override declaration.

    """
    reconciler = Reconciler(
        key_columns,
        data_columns=data_columns,
        version_column=version_column,
        deleted_column=deleted_column,
        override_column=override_column,
    )
    iterator: Iterator[tuple[str, Iterable[Mapping[str, Any]]]]
    if isinstance(tables, dict):
        iterator = iter(tables.items())
    else:
        iterator = iter(tables)
    for table, rows in iterator:
        reconciler.register_table(table, rows)
    return reconciler.reconcile()
