# -*- coding: utf-8 -*-

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

from pycdlib import health
from pycdlib import pycdlibexception


class FakeInode:
    def __init__(self, extent, length):
        self._extent = extent
        self.data_length = length

    def extent_location(self):
        return self._extent


class FakeRecord:
    def __init__(self, extent=None, length=0, next_record=None, inode_obj=None):
        self.inode = inode_obj
        self.data_length = length
        self.data_extent_offset = 0
        self.data_continuation = next_record


def test_health_iso_file_extents_multi_extent_chain():
    third = FakeRecord(300, 100, inode_obj=FakeInode(300, 100))
    second = FakeRecord(200, 200, third, FakeInode(200, 200))
    first = FakeRecord(100, 300, second, FakeInode(100, 300))
    assert health._iso_file_extents(first, 2048) == (
        (100 * 2048, 300), (200 * 2048, 200), (300 * 2048, 100))
    assert health._iso_file_size(first) == 600


def test_health_iso_file_size_ignores_records_without_data():
    # A duplicate record that was split across directory blocks has no Inode
    # of its own and must not contribute to the file size.
    extra = FakeRecord(0, 2048)
    first = FakeRecord(50, 2048, extra, FakeInode(50, 2048))
    assert health._iso_file_size(first) == 2048
    assert health._iso_file_extents(first, 2048) == ((50 * 2048, 2048),)


def test_health_iso_file_extents_data_extent_offset():
    record = FakeRecord(10, 100, inode_obj=FakeInode(10, 500))
    record.data_extent_offset = 1
    assert health._iso_file_extents(record, 2048) == ((11 * 2048, 100),)


def test_health_udf_data_extents():
    class AD:
        def __init__(self, block, length):
            self.log_block_num = block
            self.extent_length = length
            self.offset = 0

    class Record:
        def __init__(self, descs):
            self.alloc_descs = descs

    record = Record([AD(5, 100), AD(9, 50)])
    extents = health._udf_data_extents(record, 2, 2048)
    assert extents == ((7 * 2048, 100), (11 * 2048, 50))
    assert health._trim_extents(extents, 120) == ((7 * 2048, 100), (11 * 2048, 20))


def test_health_status_from_missing():
    assert health._status_from_missing(0, 0) == health.FILE_COMPLETE
    assert health._status_from_missing(100, 0) == health.FILE_COMPLETE
    assert health._status_from_missing(100, 1) == health.FILE_PARTIAL
    assert health._status_from_missing(100, 100) == health.FILE_MISSING


def test_health_ranges_overlap_and_merge():
    table = health.BlockTable([(1, 4), (3, 8), (10, 10)])
    assert table.ranges == ((1, 8), (10, 10))
    assert table.missing_blocks(0, 100, 1) == tuple(range(1, 9)) + (10,)
    assert table.missing_bytes(0, 100, 1) == 9


def test_health_ranges_accept_generators():
    table = health.BlockTable(block for block in [1, 2, 3])
    assert table.ranges == ((1, 3),)
    assert len(table) == 3


def test_health_block_table_empty_string():
    assert health.BlockTable('').ranges == ()
    assert health.BlockTable(['', '  ']).ranges == ()


def test_health_parse_hex_and_colon_ranges():
    assert health.parse_block_table('0x10:0x12').ranges == ((16, 18),)


def test_health_bad_hex():
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        health.parse_block_table('0xzz')


def test_health_missing_bytes_clips_to_range():
    table = health.BlockTable([2])
    # Range starts halfway through block 2 and is only half a block long.
    assert table.missing_bytes(2 * 2048 + 1024, 500, 2048) == 500
    # Range ends before block 2.
    assert table.missing_bytes(0, 2 * 2048, 2048) == 0


def test_health_missing_intervals_merge_across_adjacent_blocks():
    table = health.BlockTable([2, 3])
    assert table.missing_intervals(0, 5 * 2048, 2048) == ((2 * 2048, 4 * 2048),)


def test_health_coerce_table_variants(tmpdir):
    path = str(tmpdir.join('t.txt'))
    with open(path, 'w', encoding='utf-8') as outfp:
        outfp.write('1-2')
    assert health._coerce_table('1-2').ranges == ((1, 2),)
    assert health._coerce_table(path).ranges == ((1, 2),)
    assert health._coerce_table(health.BlockTable([3])).ranges == ((3, 3),)
    assert health._coerce_table(None).ranges == ()


def test_health_missing_ranges():
    extents = ((0, 4096),)
    assert health._missing_ranges((), extents, 2048) == ()
    assert health._missing_ranges((1,), extents, 2048) == ((2048, 2048),)
    assert health._missing_ranges((0, 1), extents, 2048) == ((0, 4096),)
