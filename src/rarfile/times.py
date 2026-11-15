"""Timestamp provenance for archive entries.

RAR archives mix timestamps with different levels of precision and with
different timezone semantics:

* RAR3 stores DOS timestamps as local wall-clock values without a timezone.
  The optional extended time record adds fractional seconds, but still does
  not record a timezone.
* RAR5 stores Unix timestamps and Windows FILETIME values in UTC.  The
  optional extended record may add nanoseconds.

This module keeps the value exactly as declared by the archive, the basis
for interpreting its timezone, and the range of instants that are provable
from that declaration.  It never falls back to the host timezone.

For a value without a timezone the declared value is a wall-clock reading
``W``.  With civil UTC offsets limited to ``-12:00 .. +14:00`` the possible
instants are::

    [W - 14 hours, W + 12 hours + resolution)

The range is deliberately conservative: a calendar day selection can put
the value into ``certain`` only when that whole range is inside the day.
Values that overlap a day boundary are reported separately instead of being
silently assigned to one day.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta, timezone, tzinfo
from enum import Enum
from typing import TYPE_CHECKING

from .utils import nsdatetime, to_nsdatetime

if TYPE_CHECKING:
    from .info import RarInfo

__all__ = (
    "TimezoneBasis", "TimePrecision", "TimeValue", "DaySelection",
    "select_by_day", "select_by_precision", "interpret_wallclock",
    "MIN_UTC_OFFSET", "MAX_UTC_OFFSET",
)


#: Smallest civil UTC offset (UTC-12:00).
MIN_UTC_OFFSET = timedelta(hours=-12)

#: Largest civil UTC offset (UTC+14:00).
MAX_UTC_OFFSET = timedelta(hours=14)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NSEC = 1000000000


class TimezoneBasis(Enum):
    """How the timezone of a declared timestamp is established.

    .. versionadded:: 5.0
    """

    #: The archive declares an absolute timestamp in UTC.
    UTC = "utc"

    #: The archive declares a wall-clock reading without a timezone.
    #: The host timezone must not be used to interpret it.
    UNKNOWN = "unknown"

    #: The archive does not declare a timezone, but the caller supplied an
    #: explicit UTC offset for interpreting the wall-clock reading.
    SUPPLIED = "supplied"


class TimePrecision(Enum):
    """Precision provable from the archive source.

    Ordering is from coarse to fine: ``DOS < ... < NANOSECOND``.
    Comparing two precisions answers whether one is at least as precise as
    the other.

    .. versionadded:: 5.0
    """

    #: 32-bit DOS timestamp, two-second resolution.
    DOS = ("dos", 2 * _NSEC)

    #: Whole-second value, such as a Unix timestamp.
    SECOND = ("second", _NSEC)

    #: RAR3 extended time with one fractional byte (~6.55 ms).
    EXTTIME_1 = ("rar3-exttime-1", 6553600)

    #: RAR3 extended time with two fractional bytes (25.6 us).
    EXTTIME_2 = ("rar3-exttime-2", 25600)

    #: RAR3 extended time with three fractional bytes (100 ns).
    EXTTIME_3 = ("rar3-exttime-3", 100)

    #: Windows FILETIME, 100ns resolution.
    FILETIME = ("windows-filetimestamp", 100)

    #: Unix timestamp with an additional nanosecond field.
    NANOSECOND = ("unix-nanosecond", 1)

    def __init__(self, source: str, resolution_ns: int) -> None:
        self.source = source
        self.resolution_ns = resolution_ns

    @property
    def resolution(self) -> timedelta:
        """Nominal resolution as :class:`~datetime.timedelta`.

        Rounds up to full microseconds; use :attr:`resolution_ns` when
        exactness matters.
        """
        return timedelta(microseconds=(self.resolution_ns + 999) // 1000)

    def _rank(self) -> int:
        return -self.resolution_ns

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, TimePrecision):
            return NotImplemented
        return self._rank() < other._rank()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, TimePrecision):
            return NotImplemented
        return self._rank() <= other._rank()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, TimePrecision):
            return NotImplemented
        return self._rank() > other._rank()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, TimePrecision):
            return NotImplemented
        return self._rank() >= other._rank()


def _datetime_to_ns(dt: datetime) -> int:
    """Convert datetime to nanoseconds without consulting the host timezone.
    """
    if isinstance(dt, nsdatetime):
        sub_ns = dt.nanosecond
    else:
        sub_ns = dt.microsecond * 1000
    base = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    if dt.tzinfo is not None:
        offset = dt.utcoffset() or timedelta(0)
        base -= offset
    delta = base - _EPOCH.replace(tzinfo=None)
    return (delta.days * 86400 + delta.seconds) * _NSEC + sub_ns


def _ns_to_datetime(nsec: int) -> datetime:
    """Convert UTC nanoseconds to datetime, keeping sub-microsecond part.
    """
    secs, rem = divmod(nsec, _NSEC)
    base = _EPOCH + timedelta(seconds=secs)
    return to_nsdatetime(base, rem)


@dataclass(frozen=True, kw_only=True)
class TimeValue:
    """A declared timestamp together with its provenance.

    Attributes:

        field
            Archive entry field the value belongs to, such as ``"mtime"``.

        declared
            Value exactly as declared by the archive.  RAR3 values are naive
            wall-clock readings; RAR5 values are timezone-aware UTC.

        tz_basis
            Basis for interpreting :attr:`declared`.

        precision
            Precision provable from the source record.

        source
            Short name of the on-disk record that supplied the value.

        earliest
            Earliest instant consistent with the declaration, as aware UTC.

        latest
            Exclusive end of the possible instant range, as aware UTC.

        flags
            Raw source flags, kept for auditing.

    .. versionadded:: 5.0
    """

    field: str
    declared: datetime
    tz_basis: TimezoneBasis
    precision: TimePrecision
    source: str
    earliest: datetime
    latest: datetime
    flags: int = 0

    def __post_init__(self) -> None:
        if self.tz_basis is TimezoneBasis.UTC:
            if self.declared.tzinfo is not None:
                self._check_utc(self.declared)
        elif self.declared.tzinfo is not None:
            raise ValueError("time must be naive unless based on UTC")
        if self.earliest.tzinfo is None or self.latest.tzinfo is None:
            raise ValueError("time range must use aware datetimes")
        if self.latest < self.earliest:
            raise ValueError("time range is reversed")

    @staticmethod
    def _check_utc(dt: datetime) -> None:
        if dt.utcoffset() != timedelta(0):
            raise ValueError("absolute time must be in UTC")

    @classmethod
    def from_utc(cls, *, field_name: str, declared: datetime,
                 precision: TimePrecision, source: str,
                 flags: int = 0) -> TimeValue:
        """Create a value from an absolute timestamp declared in UTC.
        """
        if declared.tzinfo is None:
            raise ValueError("absolute time requires tzinfo")
        cls._check_utc(declared)
        start_ns = _datetime_to_ns(declared)
        end_ns = start_ns + precision.resolution_ns
        return cls(
            field=field_name,
            declared=declared,
            tz_basis=TimezoneBasis.UTC,
            precision=precision,
            source=source,
            earliest=_ns_to_datetime(start_ns),
            latest=_ns_to_datetime(end_ns),
            flags=flags,
        )

    @classmethod
    def from_wallclock(cls, *, field_name: str, declared: datetime,
                       precision: TimePrecision, source: str,
                       offset: timedelta | None = None,
                       flags: int = 0) -> TimeValue:
        """Create a value from a wall-clock reading without a timezone.

        With *offset* the caller supplies an explicit interpretation; the
        value is then absolute and :attr:`tz_basis` is
        :attr:`TimezoneBasis.SUPPLIED`.  Without it the possible range covers
        all civil UTC offsets from ``-12:00`` to ``+14:00``.
        """
        if declared.tzinfo is not None:
            raise ValueError("wall-clock time must be naive")
        wall_ns = _datetime_to_ns(declared)
        if offset is not None:
            start_ns = wall_ns - int(offset.total_seconds() * _NSEC)
            end_ns = start_ns + precision.resolution_ns
            basis = TimezoneBasis.SUPPLIED
        else:
            min_ns = int(MIN_UTC_OFFSET.total_seconds() * _NSEC)
            max_ns = int(MAX_UTC_OFFSET.total_seconds() * _NSEC)
            # UTC = wall - offset
            start_ns = wall_ns - max_ns
            end_ns = wall_ns - min_ns + precision.resolution_ns
            basis = TimezoneBasis.UNKNOWN
        return cls(
            field=field_name,
            declared=declared,
            tz_basis=basis,
            precision=precision,
            source=source,
            earliest=_ns_to_datetime(start_ns),
            latest=_ns_to_datetime(end_ns),
            flags=flags,
        )

    @property
    def resolution_ns(self) -> int:
        """Resolution of the declared value in nanoseconds."""
        return self.precision.resolution_ns

    @property
    def time_range(self) -> tuple[datetime, datetime]:
        """Possible instant range as half-open ``(earliest, latest)`` UTC."""
        return self.earliest, self.latest

    @property
    def is_absolute(self) -> bool:
        """Whether the declaration has a known timezone."""
        return self.tz_basis in (TimezoneBasis.UTC, TimezoneBasis.SUPPLIED)

    @property
    def declared_date(self) -> date:
        """Calendar date of the declared value, without any conversion.

        For wall-clock values this is the only provable date and it is
        independent of the host timezone.
        """
        return self.declared.date()

    @property
    def declared_timezone(self) -> tzinfo | None:
        """Timezone attached to the declared value, if any."""
        return self.declared.tzinfo

    def at_least(self, precision: TimePrecision) -> bool:
        """Whether declared value is provable at least at *precision*."""
        return self.precision >= precision

    def for_offset(self, offset: timedelta) -> TimeValue:
        """Apply an explicit UTC offset to a value without a timezone.

        Values that already have a known timezone are returned unchanged.
        """
        if self.tz_basis is not TimezoneBasis.UNKNOWN:
            return self
        return TimeValue.from_wallclock(
            field_name=self.field,
            declared=self.declared,
            precision=self.precision,
            source="%s+supplied" % self.source,
            offset=offset,
            flags=self.flags,
        )

    def covers_day(self, day_start: datetime, day_end: datetime) -> bool:
        """Whether the whole possible range is inside the given interval."""
        return self.earliest >= day_start and self.latest <= day_end

    def overlaps_day(self, day_start: datetime, day_end: datetime) -> bool:
        """Whether the possible range intersects the given interval."""
        return self.earliest < day_end and self.latest > day_start


@dataclass(kw_only=True)
class DaySelection:
    """Result of selecting entries by calendar day.

    Attributes:

        day
            Requested calendar day.

        tz
            Timezone in which the day was interpreted.

        field
            Timestamp field used for selection.

        certain
            Entries whose whole possible range is inside the day.

        boundary
            Entries whose possible range intersects the day but is not fully
            contained.  For values without a timezone this is the normal
            result: the archive does not prove a single UTC day.

        values
            Timestamp values used for the selection, keyed by ``id(entry)``.
            These reflect any explicit offset that was applied.

    .. versionadded:: 5.0
    """

    day: date
    tz: tzinfo
    field: str
    certain: list[RarInfo] = dc_field(default_factory=list)
    boundary: list[RarInfo] = dc_field(default_factory=list)
    values: dict[int, TimeValue] = dc_field(default_factory=dict, repr=False)

    @property
    def matching(self) -> list[RarInfo]:
        """Entries certainly on the requested day."""
        return self.certain

    @property
    def uncertain(self) -> list[RarInfo]:
        """Alias for :attr:`boundary`."""
        return self.boundary

    @property
    def unknown(self) -> list[RarInfo]:
        """Boundary entries whose timezone the archive does not declare."""
        return [e for e in self.boundary
                if self.values[id(e)].tz_basis is TimezoneBasis.UNKNOWN]


def _day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    next_day = day + timedelta(days=1)
    end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _entry_time(entry: RarInfo, field_name: str) -> TimeValue | None:
    return entry.times.get(field_name)


def interpret_wallclock(value: TimeValue, *,
                        offset: timedelta | None = None,
                        tz: tzinfo | None = None) -> TimeValue:
    """Re-interpret a wall-clock value with an explicit timezone.

    The original :attr:`TimeValue.declared` value is kept unchanged.  The
    result has :attr:`TimezoneBasis.SUPPLIED` and a narrow range based on
    the supplied offset.  This never reads the host timezone.

    .. versionadded:: 5.0
    """
    if value.tz_basis is not TimezoneBasis.UNKNOWN:
        return value
    if offset is None:
        if tz is None:
            raise ValueError("offset or tz is required")
        probe = value.declared.replace(tzinfo=tz)
        offset = probe.utcoffset()
        if offset is None:
            raise ValueError("timezone has no UTC offset for this value")
    return TimeValue.from_wallclock(
        field_name=value.field,
        declared=value.declared,
        precision=value.precision,
        source="%s+supplied" % value.source,
        offset=offset,
        flags=value.flags,
    )


def select_by_day(entries: Iterable[RarInfo], day: date, *,
                  tz: tzinfo = timezone.utc,
                  offset: timedelta | None = None,
                  field_name: str = "mtime") -> DaySelection:
    """Split entries into certain and boundary matches for one day.

    The day interval is computed in *tz*, which defaults to UTC.  The host
    timezone is never consulted.  Entries without the requested timestamp
    are ignored.

    Values with no timezone have a wide possible instant range.  If that
    range crosses an edge of the day interval, the entry is put into
    :attr:`DaySelection.boundary` rather than being assigned to the day.

    *offset* is an explicit timezone basis for wall-clock values, for
    example when the archive source is known to use a fixed offset.  It is
    never inferred from the host.  Absolute values are unaffected.

    .. versionadded:: 5.0
    """
    day_start, day_end = _day_bounds(day, tz)
    result = DaySelection(day=day, tz=tz, field=field_name)
    for entry in entries:
        value = _entry_time(entry, field_name)
        if value is None:
            continue
        if offset is not None:
            value = value.for_offset(offset)
        if value.covers_day(day_start, day_end):
            result.certain.append(entry)
            result.values[id(entry)] = value
        elif value.overlaps_day(day_start, day_end):
            result.boundary.append(entry)
            result.values[id(entry)] = value
    return result


def select_by_precision(entries: Iterable[RarInfo],
                        min_precision: TimePrecision, *,
                        field_name: str = "mtime") -> list[RarInfo]:
    """Select entries whose timestamp is provable at least at *min_precision*.

    Entries without the requested timestamp are ignored.

    .. versionadded:: 5.0
    """
    result = []
    for entry in entries:
        value = _entry_time(entry, field_name)
        if value is not None and value.at_least(min_precision):
            result.append(entry)
    return result
