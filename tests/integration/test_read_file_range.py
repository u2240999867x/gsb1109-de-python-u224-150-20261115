# -*- coding: utf-8 -*-

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pycdlib
import pycdlib.pycdlibexception

from test_common import *


# A multi-extent ISO9660 file is one whose size exceeds what a single
# directory record can describe (0xfffff800 bytes).  Sparse files keep these
# tests cheap: the apparent size is multi-gigabyte while the on-disk cost is
# only the handful of bytes we explicitly write.
MULTI_EXTENT_BOUNDARY = 0xfffff800


def _open_multi_extent_iso(tmpdir, udf=False, size=MULTI_EXTENT_BOUNDARY + 4096):
    """Build an ISO holding one sparse multi-extent file and return handles."""
    big = os.path.join(str(tmpdir), 'big')
    with open(big, 'wb') as outfp:
        outfp.truncate(size)
        outfp.seek(0)
        outfp.write(b'\xaa' * 16)
        outfp.seek(MULTI_EXTENT_BOUNDARY - 8)
        outfp.write(b'\xbb' * 16)
        outfp.seek(size - 16)
        outfp.write(b'\xcc' * 16)

    iso = pycdlib.PyCdlib()
    if udf:
        iso.new(interchange_level=3, udf='2.60')
        iso.add_file(big, '/BIG.;1', udf_path='/big')
    else:
        iso.new(interchange_level=3)
        iso.add_file(big, '/BIG.;1')
    return iso, big, size


def test_read_file_range_single_extent_mastering():
    payload = b'0123456789ABCDEF'
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(payload), len(payload), '/F.;1')

    result = iso.read_file_range(offset=2, length=10, iso_path='/F.;1')
    assert(result.data == payload[2:12])
    assert(result.complete is True)
    assert(result.short_read is False)
    assert(result.file_length == len(payload))
    assert(result.requested_length == 10)
    assert(result.available_length == 10)
    assert(len(result.segments) == 1)

    seg = result.segments[0]
    assert(seg.logical_offset == 2)
    assert(seg.data == payload[2:12])
    assert(seg.disc_length == 10)
    iso.close()


def test_read_file_range_whole_file_default_length():
    payload = b'0123456789ABCDEF'
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(payload), len(payload), '/F.;1')

    result = iso.read_file_range(iso_path='/F.;1')
    assert(result.data == payload)
    assert(result.requested_length == len(payload))
    assert(result.available_length == len(payload))
    assert(result.complete is True)
    iso.close()


def test_read_file_range_tail_clamped_to_file_length():
    payload = b'0123456789ABCDEF'
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(payload), len(payload), '/F.;1')

    # Asking well past EOF is clamped to the original file length, not padded.
    result = iso.read_file_range(offset=len(payload) - 4, length=1000,
                                 iso_path='/F.;1')
    assert(result.data == payload[-4:])
    assert(result.requested_length == 4)
    assert(result.available_length == 4)
    assert(result.complete is True)

    # An offset at EOF yields an empty (but complete) read.
    at_eof = iso.read_file_range(offset=len(payload), iso_path='/F.;1')
    assert(at_eof.data == b'')
    assert(at_eof.requested_length == 0)
    assert(at_eof.complete is True)
    assert(at_eof.segments == [])

    # An offset past EOF behaves the same as an offset at EOF.
    past_eof = iso.read_file_range(offset=len(payload) + 50, iso_path='/F.;1')
    assert(past_eof.data == b'')
    assert(past_eof.complete is True)
    iso.close()


def test_read_file_range_empty_file():
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(b''), 0, '/EMPTY.;1')

    result = iso.read_file_range(iso_path='/EMPTY.;1')
    assert(result.data == b'')
    assert(result.file_length == 0)
    assert(result.requested_length == 0)
    assert(result.complete is True)
    assert(result.short_read is False)
    assert(result.segments == [])
    iso.close()


def test_read_file_range_crosses_multi_extent_boundary(tmpdir):
    iso, _, size = _open_multi_extent_iso(tmpdir)
    boundary = MULTI_EXTENT_BOUNDARY

    # 8 bytes on each side of the boundary: the range must be assembled from
    # two extents, with the bytes still in file order.
    result = iso.read_file_range(offset=boundary - 8, length=32,
                                 iso_path='/BIG.;1')
    assert(len(result.segments) == 2)
    assert(result.data == b'\xbb' * 16 + b'\x00' * 16)
    assert(result.complete is True)
    assert(result.short_read is False)
    assert(result.requested_length == 32)

    first, second = result.segments
    assert(first.logical_offset == boundary - 8)
    assert(len(first.data) == 8)
    assert(second.logical_offset == boundary)
    assert(len(second.data) == 24)
    # The two extents are contiguous on disc.
    assert(first.disc_offset + len(first.data) == second.disc_offset)
    # Segment provenance length is what was requested from that extent.
    assert(first.disc_length == 8)
    assert(second.disc_length == 24)
    iso.close()


def test_read_file_range_within_second_extent(tmpdir):
    iso, _, size = _open_multi_extent_iso(tmpdir)
    boundary = MULTI_EXTENT_BOUNDARY

    # A range entirely inside the trailing extent returns one segment.
    result = iso.read_file_range(offset=size - 16, length=16,
                                 iso_path='/BIG.;1')
    assert(len(result.segments) == 1)
    assert(result.data == b'\xcc' * 16)
    assert(result.complete is True)
    iso.close()


def test_read_file_range_full_multi_extent(tmpdir):
    iso, _, size = _open_multi_extent_iso(tmpdir)

    result = iso.read_file_range(iso_path='/BIG.;1')
    assert(result.file_length == size)
    assert(result.requested_length == size)
    assert(result.available_length == size)
    assert(result.complete is True)
    assert(len(result.segments) == 2)
    assert(result.segments[0].logical_offset == 0)
    assert(result.segments[1].logical_offset == MULTI_EXTENT_BOUNDARY)
    iso.close()


def test_read_file_range_provenance_matches_byte_extents(tmpdir):
    # The per-segment on-disc source ranges must agree with the established
    # get_file_byte_extents() layout, on both the ISO9660 and UDF views.
    iso, _, size = _open_multi_extent_iso(tmpdir, udf=True)

    iso_result = iso.read_file_range(iso_path='/BIG.;1')
    assert([(seg.disc_offset, seg.disc_length) for seg in iso_result.segments] ==
           iso.get_file_byte_extents(iso_path='/BIG.;1'))

    udf_result = iso.read_file_range(udf_path='/big')
    assert([(seg.disc_offset, seg.disc_length) for seg in udf_result.segments] ==
           iso.get_file_byte_extents(udf_path='/big'))
    iso.close()


def _physical_span(result):
    """The (start, end) physical bytes covered by a result's segments."""
    starts = [seg.disc_offset for seg in result.segments]
    ends = [seg.disc_offset + len(seg.data) for seg in result.segments]
    return min(starts), max(ends)


def test_read_file_range_udf_and_iso_share_provenance(tmpdir):
    iso, _, size = _open_multi_extent_iso(tmpdir, udf=True)
    boundary = MULTI_EXTENT_BOUNDARY

    iso_result = iso.read_file_range(offset=boundary - 16, length=32,
                                     iso_path='/BIG.;1')
    udf_result = iso.read_file_range(offset=boundary - 16, length=32,
                                     udf_path='/big')

    # Both filesystem views resolve to the same physical bytes.  Their segment
    # granularity may differ (the ISO9660 view splits at the multi-extent chunk
    # boundary while a UDF allocation descriptor can span it), so compare the
    # reassembled data and the covered physical byte interval rather than the
    # number of segments.
    assert(iso_result.data == udf_result.data)
    assert(_physical_span(iso_result) == _physical_span(udf_result))
    for result in (iso_result, udf_result):
        # Segments are contiguous and in file order within each view.
        offsets = [seg.disc_offset for seg in result.segments]
        ends = [seg.disc_offset + len(seg.data) for seg in result.segments]
        assert(ends[:-1] == offsets[1:])
    iso.close()


def test_read_file_range_provenance_matches_disc_bytes():
    payload = b'kernel-payload-bytes'
    iso = pycdlib.PyCdlib()
    iso.new(udf='2.60')
    iso.add_fp(io.BytesIO(payload), len(payload), '/FOO.;1', udf_path='/foo')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()

    raw = out.getvalue()
    reopened = pycdlib.PyCdlib()
    out.seek(0)
    reopened.open_fp(out)

    result = reopened.read_file_range(offset=3, length=9, iso_path='/FOO.;1')
    assert(result.data == payload[3:12])
    for seg in result.segments:
        assert(raw[seg.disc_offset:seg.disc_offset + len(seg.data)] == seg.data)

    udf_result = reopened.read_file_range(udf_path='/foo')
    assert(udf_result.data == payload)
    assert(udf_result.segments[0].disc_offset ==
           result.segments[0].disc_offset - 3)
    reopened.close()


class _ShortReadFP(io.BytesIO):
    """A backing descriptor that never returns more than `cap` bytes per read."""
    def __init__(self, data, cap):
        super().__init__(data)
        self._cap = cap

    def read(self, n=-1):
        if n is not None and n > 0:
            n = min(n, self._cap)
        return super().read(n)


def test_read_file_range_short_read_not_faked_as_eof():
    payload = bytes(range(20))
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(_ShortReadFP(payload, 4), len(payload), '/F.;1')

    result = iso.read_file_range(iso_path='/F.;1')
    # The original file length is preserved even though the extent is short.
    assert(result.file_length == len(payload))
    assert(result.requested_length == len(payload))
    assert(result.available_length == 4)
    assert(result.data == payload[:4])
    assert(result.short_read is True)
    assert(result.complete is False)

    # A range fully inside the bytes that ARE available is independently
    # delivered as a complete read.
    leading = iso.read_file_range(offset=0, length=4, iso_path='/F.;1')
    assert(leading.data == payload[:4])
    assert(leading.complete is True)
    assert(leading.short_read is False)
    iso.close()


class _HoleRangeISO(pycdlib.PyCdlib):
    """Test double that injects a missing trailing extent into the layout."""
    def set_extents(self, extents, file_length):
        self._hole_extents = extents
        self._hole_file_length = file_length

    def _collect_file_extents(self, record, is_udf):
        return self._hole_extents, self._hole_file_length


def test_read_file_range_missing_trailing_extent_delivers_prefix():
    iso = _HoleRangeISO()
    iso.new()
    iso.add_fp(io.BytesIO(b'ABCDEFGHIJ'), 10, '/G.;1')
    # Assign real extent locations before fabricating the hole.
    iso._reshuffle_extents()
    rec = iso._find_iso_record(b'/G.;1')
    base_extents, _ = pycdlib.PyCdlib._collect_file_extents(iso, rec, False)
    leading_ino = base_extents[0][2]
    leading_disc = base_extents[0][4]

    # A 15-byte logical file whose first 10 bytes are present and whose
    # trailing 5-byte extent is missing.
    fabricated = [
        (0, 10, leading_ino, 0, leading_disc),
        (10, 5, None, 0, None),
    ]
    iso.set_extents(fabricated, 15)

    whole = iso.read_file_range(iso_path='/G.;1')
    assert(whole.file_length == 15)
    assert(whole.requested_length == 15)
    assert(whole.available_length == 10)
    assert(whole.data == b'ABCDEFGHIJ')
    assert(whole.complete is False)
    assert(whole.short_read is False)

    # The complete leading range can still be delivered on its own.
    prefix = iso.read_file_range(offset=2, length=4, iso_path='/G.;1')
    assert(prefix.data == b'CDEF')
    assert(prefix.complete is True)

    # A range that reaches into the hole stops at the boundary rather than
    # pretending the file ended early.
    crossing = iso.read_file_range(offset=8, length=6, iso_path='/G.;1')
    assert(crossing.data == b'IJ')
    assert(crossing.available_length == 2)
    assert(crossing.complete is False)
    iso.close()


class _TruncatedImage(io.BytesIO):
    """An image whose reads past a cutoff always come up short."""
    def __init__(self, data, cutoff):
        super().__init__(data)
        self._cutoff = cutoff

    def read(self, n=-1):
        pos = self.tell()
        available = max(0, self._cutoff - pos)
        if n is None or n < 0:
            n = available
        return super().read(min(n, available))


def test_read_file_range_truncated_disc_image(tmpdir):
    payload = b'0123456789ABCDEF'
    built = pycdlib.PyCdlib()
    built.new()
    built.add_fp(io.BytesIO(payload), len(payload), '/F.;1')
    out = io.BytesIO()
    built.write_fp(out)
    built.close()
    raw = out.getvalue()

    locate = pycdlib.PyCdlib()
    probe = io.BytesIO(raw)
    locate.open_fp(probe)
    data_offset = locate.get_file_byte_extents(iso_path='/F.;1')[0][0]
    locate.close()

    # Cut the image four bytes into the file's data extent.
    truncated = pycdlib.PyCdlib()
    truncated.open_fp(_TruncatedImage(raw, data_offset + 4))

    result = truncated.read_file_range(iso_path='/F.;1')
    assert(result.file_length == len(payload))
    assert(result.data == payload[:4])
    assert(result.short_read is True)
    assert(result.complete is False)

    leading = truncated.read_file_range(offset=0, length=4, iso_path='/F.;1')
    assert(leading.data == payload[:4])
    assert(leading.complete is True)
    assert(leading.short_read is False)
    truncated.close()


def test_read_file_range_not_initialized():
    iso = pycdlib.PyCdlib()
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(iso_path='/F.;1')
    assert(str(excinfo.value) == 'This object is not initialized; call either open() or new() to create an ISO')


def test_read_file_range_unknown_keyword():
    iso = pycdlib.PyCdlib()
    iso.new()
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(bogus_path='/F.;1')
    assert(str(excinfo.value) == "Invalid keyword, must be one of 'iso_path', 'rr_path', 'joliet_path', or 'udf_path'")
    iso.close()


def test_read_file_range_no_path():
    iso = pycdlib.PyCdlib()
    iso.new()
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range()
    assert(str(excinfo.value) == "Must specify one, and only one of 'iso_path', 'rr_path', 'joliet_path', or 'udf_path'")
    iso.close()


def test_read_file_range_multiple_paths():
    iso = pycdlib.PyCdlib()
    iso.new(joliet=3)
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(iso_path='/F.;1', joliet_path='/f')
    assert(str(excinfo.value) == "Must specify one, and only one of 'iso_path', 'rr_path', 'joliet_path', or 'udf_path'")
    iso.close()


def test_read_file_range_negative_and_float_arguments():
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(b'abcd'), 4, '/F.;1')

    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(-1, iso_path='/F.;1')
    assert(str(excinfo.value) == 'Invalid offset value (must be positive)')

    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(0, -1, iso_path='/F.;1')
    assert(str(excinfo.value) == 'Invalid length value (must be positive)')

    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(1.0, iso_path='/F.;1')
    assert(str(excinfo.value) == 'an integer is required')
    iso.close()


def test_read_file_range_directory_rejected():
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_directory('/DIR1')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(iso_path='/DIR1')
    assert(str(excinfo.value) == 'Path to read must be a file')
    iso.close()


def test_read_file_range_udf_path_on_non_udf():
    iso = pycdlib.PyCdlib()
    iso.new()
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(udf_path='/foo')
    assert(str(excinfo.value) == 'Can only specify a UDF path for a UDF ISO')
    iso.close()


def test_read_file_range_rr_path_on_non_rock_ridge():
    iso = pycdlib.PyCdlib()
    iso.new()
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.read_file_range(rr_path='/foo')
    assert(str(excinfo.value) == 'Cannot fetch a rr_path from a non-Rock Ridge ISO')
    iso.close()
