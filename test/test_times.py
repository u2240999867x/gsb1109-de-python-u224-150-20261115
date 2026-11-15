"""Timestamp provenance tests.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import rarfile
from rarfile import (
    TimePrecision, TimezoneBasis, interpret_wallclock, select_by_day,
)

UTC = timezone.utc


def rar3_entry(name: str = "test/files/ctime0.rar") -> rarfile.RarInfo:
    with rarfile.RarFile(name) as rf:
        return rf.getinfo(rf.infolist()[0].filename)


def rar5_entry(name: str = "test/files/ctime5.rar") -> rarfile.RarInfo:
    with rarfile.RarFile(name) as rf:
        return rf.getinfo(rf.infolist()[0].filename)


def test_rar3_dos_is_wallclock() -> None:
    info = rar3_entry("test/files/rar15-comment.rar")
    value = info.times["mtime"]
    assert value.tz_basis is TimezoneBasis.UNKNOWN
    assert value.declared == datetime(2010, 11, 3, 19, 49, 32)
    assert value.declared.tzinfo is None
    assert value.precision is TimePrecision.DOS
    assert value.source == "rar3-dostime"
    assert value.declared_date == date(2010, 11, 3)


def test_rar3_exttime_precision() -> None:
    info = rar3_entry()
    value = info.times["mtime"]
    assert value.tz_basis is TimezoneBasis.UNKNOWN
    assert value.precision is TimePrecision.EXTTIME_3
    assert value.declared.isoformat(" ") == "2011-05-10 21:28:47.899345100"
    # Possible UTC range covers all civil offsets, not the host offset.
    assert value.earliest.isoformat() == "2011-05-10T07:28:47.899345100+00:00"
    assert value.latest.isoformat() == "2011-05-11T09:28:47.899345200+00:00"
    assert not value.is_absolute


def test_rar5_utc_is_absolute() -> None:
    info = rar5_entry()
    value = info.times["mtime"]
    assert value.tz_basis is TimezoneBasis.UTC
    assert value.precision is TimePrecision.NANOSECOND
    assert value.declared.isoformat() == "2020-07-30T20:26:59.677675904+00:00"
    assert value.earliest.isoformat() == "2020-07-30T20:26:59.677675904+00:00"
    assert value.latest.isoformat() == "2020-07-30T20:26:59.677675905+00:00"
    assert value.is_absolute


def test_rar5_filetime_precision() -> None:
    info = rar5_entry("test/files/rar5-times2.rar")
    value = info.times["mtime"]
    assert value.tz_basis is TimezoneBasis.UTC
    assert value.precision is TimePrecision.FILETIME
    assert value.source == "rar5-windowstime"


def test_rar5_unix_second_precision() -> None:
    with rarfile.RarFile("test/files/rar5-times.rar") as rf:
        value = rf.getinfo("stest1.txt").times["mtime"]
    assert value.precision is TimePrecision.SECOND
    assert value.earliest == datetime(2011, 6, 12, 9, 53, 33, tzinfo=UTC)
    assert value.latest == datetime(2011, 6, 12, 9, 53, 34, tzinfo=UTC)


def test_precision_ordering() -> None:
    assert TimePrecision.DOS < TimePrecision.SECOND
    assert TimePrecision.SECOND < TimePrecision.EXTTIME_3
    assert TimePrecision.EXTTIME_1 < TimePrecision.EXTTIME_2
    assert TimePrecision.EXTTIME_2 < TimePrecision.EXTTIME_3
    assert TimePrecision.FILETIME <= TimePrecision.NANOSECOND
    assert TimePrecision.NANOSECOND >= TimePrecision.FILETIME
    assert TimePrecision.NANOSECOND == TimePrecision.NANOSECOND


def test_day_selection_boundary() -> None:
    with rarfile.RarFile("test/files/rar3-old.rar") as rf:
        sel = rf.infolist_by_day(date(2016, 5, 24))
    assert [e.filename for e in sel.certain] == []
    assert [e.filename for e in sel.boundary] == [
        "vols/bigfile.txt", "vols/smallfile.txt",
    ]
    assert [e.filename for e in sel.unknown] == [
        "vols/bigfile.txt", "vols/smallfile.txt",
    ]
    assert sel.day == date(2016, 5, 24)
    assert sel.tz is UTC


def test_day_selection_certain() -> None:
    with rarfile.RarFile("test/files/rar5-times.rar") as rf:
        sel = rf.infolist_by_day(date(2011, 6, 12))
    assert [e.filename for e in sel.certain] == ["stest1.txt"]
    assert sel.boundary == []


def test_day_selection_explicit_offset() -> None:
    with rarfile.RarFile("test/files/rar3-old.rar") as rf:
        sel = rf.infolist_by_day(date(2016, 5, 24),
                                 offset=timedelta(hours=8))
    assert [e.filename for e in sel.certain] == [
        "vols/bigfile.txt", "vols/smallfile.txt",
    ]
    assert sel.boundary == []
    assert sel.unknown == []
    value = sel.values[id(sel.certain[0])]
    assert value.tz_basis is TimezoneBasis.SUPPLIED


def test_day_selection_tz_independent_of_host() -> None:
    with rarfile.RarFile("test/files/rar3-old.rar") as rf:
        infos = rf.infolist()
        utc = select_by_day(infos, date(2016, 5, 24))
        plus8 = select_by_day(infos, date(2016, 5, 24),
                              tz=timezone(timedelta(hours=8)))
    # Day edge is interpreted in the requested tz, but the value without
    # timezone remains in the boundary group either way.
    assert [e.filename for e in utc.boundary] == [e.filename for e in plus8.boundary]
    assert utc.certain == [] and plus8.certain == []


def test_declared_date_not_shifted_by_host() -> None:
    info = rar3_entry("test/files/rar15-comment.rar")
    value = info.times["mtime"]
    original = value.declared
    # Interpreting explicitly does not change the declared value.
    supplied = interpret_wallclock(value, tz=timezone(timedelta(hours=8)))
    assert supplied.declared == original
    assert supplied.declared_date == original.date()
    assert supplied.tz_basis is TimezoneBasis.SUPPLIED
    assert supplied.earliest == datetime(2010, 11, 3, 11, 49, 32, tzinfo=UTC)
    assert supplied.latest == datetime(2010, 11, 3, 11, 49, 34, tzinfo=UTC)


def test_supplied_offset_requires_argument() -> None:
    info = rar3_entry()
    with pytest.raises(ValueError):
        interpret_wallclock(info.times["mtime"])


def test_host_timezone_does_not_change_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switching the host timezone must not move a declaration to another day."""
    with rarfile.RarFile("test/files/rar3-old.rar") as rf:
        infos = rf.infolist()
        first = select_by_day(infos, date(2016, 5, 24))
        old_tz = os.environ.get("TZ")
        try:
            monkeypatch.setenv("TZ", "Pacific/Kiritimati")
            time.tzset()
            second = select_by_day(infos, date(2016, 5, 24))
        finally:
            if old_tz is None:
                monkeypatch.delenv("TZ", raising=False)
            else:
                monkeypatch.setenv("TZ", old_tz)
            time.tzset()

    assert [e.filename for e in first.boundary] == [e.filename for e in second.boundary]
    assert first.certain == second.certain
    # Declared value is independent of host timezone.
    assert infos[0].times["mtime"].declared_date == date(2016, 5, 24)


def test_by_precision() -> None:
    with rarfile.RarFile("test/files/rar3-old.rar") as rf:
        sec = rf.infolist_by_precision(TimePrecision.SECOND)
        ns = rf.infolist_by_precision(TimePrecision.NANOSECOND)
    assert len(sec) == 2
    assert ns == []

    with rarfile.RarFile("test/files/ctime5.rar") as rf:
        ns = rf.infolist_by_precision(TimePrecision.NANOSECOND)
    assert [e.filename for e in ns] == ["timed.txt"]


def test_rar5_field_presence() -> None:
    info = rar5_entry()
    assert set(info.times) == {"mtime", "date_time", "ctime", "atime"}
    assert info.times["date_time"].precision is TimePrecision.SECOND
    assert info.get_time("mtime").precision is TimePrecision.NANOSECOND
    assert info.get_time("arctime") is None
    assert info.get_time("date_time").declared.isoformat() == "2020-07-30T20:26:59+00:00"


def test_rar3_missing_exttime_uses_dos() -> None:
    with rarfile.RarFile("test/files/rar15-comment.rar") as rf:
        info = rf.getinfo("FILE1.TXT")
    assert info.mtime is None
    assert info.get_time("mtime").precision is TimePrecision.DOS
    assert info.get_time("mtime").tz_basis is TimezoneBasis.UNKNOWN
    assert info.get_time("date_time").declared_date == date(2010, 11, 3)
