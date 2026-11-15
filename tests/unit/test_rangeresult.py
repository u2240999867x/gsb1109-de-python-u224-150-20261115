import os
import sys

import pytest

prefix = '.'
for i in range(0, 3):
    if os.path.isdir(os.path.join(prefix, 'pycdlib')):
        sys.path.insert(0, prefix)
        break
    else:
        prefix = '../' + prefix

from pycdlib import rangeresult


def test_range_read_segment_basic():
    seg = rangeresult.RangeReadSegment(7, b'abc', 1024, 3)
    assert(seg.logical_offset == 7)
    assert(seg.data == b'abc')
    assert(seg.disc_offset == 1024)
    assert(seg.disc_length == 3)
    assert(len(seg) == 3)


def test_range_read_segment_repr():
    seg = rangeresult.RangeReadSegment(0, b'ab', 10, 2)
    text = repr(seg)
    assert('logical_offset=0' in text)
    assert('disc_offset=10' in text)
    assert('disc_length=2' in text)


def test_range_read_segment_equality():
    a = rangeresult.RangeReadSegment(0, b'ab', 10, 2)
    b = rangeresult.RangeReadSegment(0, b'ab', 10, 2)
    c = rangeresult.RangeReadSegment(0, b'cd', 10, 2)
    assert(a == b)
    assert(a != c)
    assert(a.__eq__('not a segment') is NotImplemented)


def test_range_read_result_concatenates_segments():
    segments = [
        rangeresult.RangeReadSegment(0, b'ab', 10, 2),
        rangeresult.RangeReadSegment(2, b'cd', 30, 2),
    ]
    result = rangeresult.RangeReadResult(segments, 5, 0, 5, 4, True, False)
    assert(result.data == b'abcd')
    assert(len(result) == 4)
    assert(result.available_length == 4)
    assert(result.requested_length == 5)
    assert(result.file_length == 5)
    assert(result.complete is True)
    assert(result.short_read is False)
    assert(result.segments is segments)


def test_range_read_result_empty():
    result = rangeresult.RangeReadResult([], 10, 10, 0, 0, True, False)
    assert(result.data == b'')
    assert(len(result) == 0)
    assert(result.segments == [])
    assert(result.complete is True)


def test_range_read_result_repr_flags():
    result = rangeresult.RangeReadResult([], 10, 0, 10, 4, False, True)
    text = repr(result)
    assert('complete=False' in text)
    assert('short_read=True' in text)
    assert('requested=10' in text)
    assert('available=4' in text)
