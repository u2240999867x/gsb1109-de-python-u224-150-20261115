import io
import os
import sys
import struct
import time

import pytest

prefix = '.'
for i in range(0, 3):
    if os.path.isdir(os.path.join(prefix, 'pycdlib')):
        sys.path.insert(0, prefix)
        break
    else:
        prefix = '../' + prefix

import pycdlib.utils
import pycdlib.pycdlibexception

def test_swab_32bit():
    assert(pycdlib.utils.swab_32bit(0x89) == 0x89000000)

def test_swab_32bit_bad_input():
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInternalError) as excinfo:
        pycdlib.utils.swab_32bit(-1)
    assert(str(excinfo.value) == 'Invalid integer passed to swab; must be unsigned 32-bits!')

def test_swab_16bit():
    assert(pycdlib.utils.swab_16bit(0x55aa) == 0xaa55)

def test_swab_16bit_bad_input():
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInternalError) as excinfo:
        pycdlib.utils.swab_16bit(-1)
    assert(str(excinfo.value) == 'Invalid integer passed to swab; must be unsigned 16-bits!')

def test_ceiling_div():
    assert(pycdlib.utils.ceiling_div(0, 2048) == 0)

def test_ceiling_div2():
    assert(pycdlib.utils.ceiling_div(2048, 2048) == 1)

def test_ceiling_div3():
    assert(pycdlib.utils.ceiling_div(2049, 2048) == 2)

def test_ceiling_div_nan():
    with pytest.raises(ZeroDivisionError) as exc_info:
        pycdlib.utils.ceiling_div(2048, 0)

def test_copy_data():
    infp = io.BytesIO()
    outfp = io.BytesIO()

    infp.write(b'\x00'*1)
    infp.seek(0)

    pycdlib.utils.copy_data(1, 8192, infp, outfp)

    assert(outfp.getvalue() == b'\x00')

def test_copy_data_short():
    infp = io.BytesIO()
    outfp = io.BytesIO()

    infp.write(b'\x00'*10)
    infp.seek(0)

    pycdlib.utils.copy_data(100, 8192, infp, outfp)

    assert(outfp.getvalue() == b'\x00'*10)

def test_encode_space_pad_too_short():
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as exc_info:
        pycdlib.utils.encode_space_pad(b'hello', 4, 'ascii')
    assert(str(exc_info.value) == 'Input string too long!')

def test_encode_space_pad_no_pad():
    assert(pycdlib.utils.encode_space_pad(b'hello', 5, 'ascii') == b'hello')

def test_encode_space_pad_one():
    assert(pycdlib.utils.encode_space_pad(b'hello', 6, 'ascii') == b'hello ')

def test_encode_space_pad_extra():
    assert(pycdlib.utils.encode_space_pad(b'hello', 11, 'utf-16_be') == b'\x00h\x00e\x00l\x00l\x00o\x00')

def test_normpath_double_slashes_beginning():
    assert(pycdlib.utils.normpath('//') == b'/')

def test_normpath_double_slashes_middle():
    assert(pycdlib.utils.normpath('/foo//bar') == b'/foo/bar')

def test_normpath_with_dotdot():
    assert(pycdlib.utils.normpath('/foo/bar/../baz') == b'/foo/baz')

def test_normpath_with_dotdot_after_slash():
    assert(pycdlib.utils.normpath('/../foo') == b'/foo')

def test_normpath_empty():
    assert(pycdlib.utils.normpath('') == b'.')

def save_and_set_tz(newtz):
    if 'TZ' in os.environ:
        oldtz = os.environ['TZ']
    else:
        oldtz = None

    os.environ['TZ'] = newtz
    time.tzset()

    return oldtz

def restore_tz(oldtz):
    if oldtz is not None:
        os.environ['TZ'] = oldtz
    else:
        del os.environ['TZ']
    time.tzset()

def test_gmtoffset_from_tm():
    oldtz = save_and_set_tz('America/New_York')
    now = 1546914300
    # America/New_York is GMT-5 hours, which is 5*60 = 300 minutes behind.  Since
    # the gmtoffset in ISO 9660 is in 15 minute intervals, this is 300/15 = 20
    # behind.
    assert(pycdlib.utils.gmtoffset_from_tm(now, time.localtime(now)) == -20)
    restore_tz(oldtz)

def test_gmtoffset_from_tm_day_rollover():
    # Setup the timezone to Tokyo
    oldtz = save_and_set_tz('Asia/Tokyo')

    # Asia/Tokyo is GMT+9 hours, which is 9*60 = 540 minutes ahead.  Since the
    # gmtoffset in ISO 9660 is in 15 minute intervals, this is 540/15 = 36
    # ahead.

    # This tm is carefully chosen so that the day of the week is the next day
    # in the Tokyo region.
    now = 1550417871
    assert(pycdlib.utils.gmtoffset_from_tm(now, time.localtime(now)) == 36)
    restore_tz(oldtz)

def test_gmtoffset_from_tm_2023_rollover():
    oldtz = save_and_set_tz('Australia/Sydney')

    # Australia/Sydney is GMT+11 hours, which is 11*60 = 660 minutes ahead.
    # Since the gmtoffset in ISO 9660 is in 15 minute intervals, this is
    # 660/15 = 44 ahead.

    now = 1672530300
    assert(pycdlib.utils.gmtoffset_from_tm(now, time.localtime(now)) == 44)

    restore_tz(oldtz)

def test_zero_pad():
    fp = io.BytesIO()
    pycdlib.utils.zero_pad(fp, 5, 10)
    assert(fp.getvalue() == b'\x00'*5)

def test_zero_pad_no_pad():
    fp = io.BytesIO()
    pycdlib.utils.zero_pad(fp, 5, 5)
    assert(fp.getvalue() == b'')

def test_zero_pad_negative_pad():
    fp = io.BytesIO()
    pycdlib.utils.zero_pad(fp, 5, 4)
    assert(fp.getvalue() == b'\x00'*3)

def test_starts_with_slash():
    assert(pycdlib.utils.starts_with_slash(b'/'))

def test_starts_with_slash_no_slash():
    assert(not pycdlib.utils.starts_with_slash(b'foo/bar'))

def test_starts_with_slash_double_slash():
    assert(pycdlib.utils.starts_with_slash(b'//foo/bar'))

def test_split_path():
    assert(pycdlib.utils.split_path(b'/foo/bar') == [b'foo', b'bar'])

def test_split_path_no_leading_slash():
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as exc_info:
        pycdlib.utils.split_path(b'foo/bar')
    assert(str(exc_info.value) == 'Must be a path starting with /')

def test_split_path_single():
    assert(pycdlib.utils.split_path(b'/foo') == [b'foo'])

def test_split_path_slash_only():
    assert(pycdlib.utils.split_path(b'/') == [b''])

def test_split_path_trailing_slash():
    assert(pycdlib.utils.split_path(b'/foo/') == [b'foo', b''])

def test_file_object_supports_binary_bytesio():
    fp = io.BytesIO()
    assert(pycdlib.utils.file_object_supports_binary(fp))

def test_truncate_basename_isolevel4():
    assert(pycdlib.utils.truncate_basename('foo', 4, False) == 'foo')

def test_truncate_basename_isolevel3():
    assert(pycdlib.utils.truncate_basename('foo', 3, False) == 'FOO')

def test_mangle_file_for_iso9660_isolevel4_no_ext():
    assert(pycdlib.utils.mangle_file_for_iso9660('foo', 4) == ('foo', ''))

def test_mangle_file_for_iso9660_isolevel4_with_ext():
    assert(pycdlib.utils.mangle_file_for_iso9660('foo.txt', 4) == ('foo', 'txt'))

def test_mangle_file_for_iso9660_isolevel3_with_empty_ext():
    assert(pycdlib.utils.mangle_file_for_iso9660('foo.', 3) == ('FOO_', ';1'))

def test_file_object_supports_binary_real_file(tmpdir):
    testout = tmpdir.join('foo')
    with open(str(testout), 'wb') as outfp:
        assert(pycdlib.utils.file_object_supports_binary(outfp))

def test_file_object_does_not_support_binary_real_file(tmpdir):
    testout = tmpdir.join('foo')
    with open(str(testout), 'w') as outfp:
        assert(not pycdlib.utils.file_object_supports_binary(outfp))


def test_mangle_file_for_joliet_short_name_unchanged():
    assert(pycdlib.utils.mangle_file_for_joliet('foo.txt') == 'foo.txt')


def test_mangle_file_for_joliet_truncates_to_64():
    name = 'a' * 70
    result = pycdlib.utils.mangle_file_for_joliet(name)
    assert(result == 'a' * 64)


def test_mangle_dir_for_joliet_truncates_to_64():
    name = 'd' * 65
    result = pycdlib.utils.mangle_dir_for_joliet(name)
    assert(result == 'd' * 64)


def test_mangle_for_joliet_keeps_unicode():
    name = 'café'
    assert(pycdlib.utils.mangle_file_for_joliet(name) == name)


def test_name_mapper_emits_both_names():
    mapper = pycdlib.utils.NameMapper(3)
    record = mapper.map_name('report.txt', False)
    assert(record['joliet_name'] == 'report.txt')
    assert(record['iso_name'] == 'REPORT.TXT;1')
    assert(record['iso_basename'] == 'REPORT')
    assert(record['iso_ext'] == 'TXT;1')
    assert(not record['iso_aliased'])
    assert(not record['joliet_aliased'])
    assert(record['iso_conflicts'] == [])
    assert(record['joliet_conflicts'] == [])


def test_name_mapper_directory_has_no_version():
    mapper = pycdlib.utils.NameMapper(3)
    record = mapper.map_name('mydir', True)
    assert(record['joliet_name'] == 'mydir')
    assert(record['iso_name'] == 'MYDIR')
    assert(record['iso_ext'] == '')


def test_name_mapper_case_merge_iso_uses_alias():
    mapper = pycdlib.utils.NameMapper(3)
    first = mapper.map_name('foo', False)
    second = mapper.map_name('FOO', False)
    assert(first['iso_name'] == 'FOO.;1')
    assert(second['iso_name'] == 'FOO_0.;1')
    assert(second['iso_aliased'])
    assert(second['iso_conflicts'] == ['foo'])


def test_name_mapper_repeat_is_stable_and_idempotent():
    mapper = pycdlib.utils.NameMapper(3)
    first = mapper.map_name('stable.txt', False)
    second = mapper.map_name('stable.txt', False)
    assert(first is second)
    assert(first['iso_name'] == second['iso_name'])
    # A repeat must not consume an additional slot.
    assert(len(mapper._iso_occupied) == 1)
    assert(len(mapper._joliet_occupied) == 1)


def test_name_mapper_appending_batch_keeps_old_mapping():
    mapper = pycdlib.utils.NameMapper(3)
    original = dict(mapper.map_name('keepme.txt', False))
    for index in range(100):
        mapper.map_name('keepme%d.txt' % (index,), False)
    assert(dict(mapper.map_name('keepme.txt', False)) == original)


def test_name_mapper_truncated_long_names_get_distinct_aliases():
    mapper = pycdlib.utils.NameMapper(3)
    first = mapper.map_name('a' * 40, True)
    second = mapper.map_name('a' * 40 + 'b', True)
    assert(first['iso_name'] == 'A' * 31)
    assert(second['iso_name'] == 'A' * 29 + '_0')
    assert(second['iso_conflicts'] == ['a' * 40])
    assert(first['iso_name'] != second['iso_name'])


def test_name_mapper_file_and_directory_cannot_share_slot_file_first():
    mapper = pycdlib.utils.NameMapper(3)
    file_rec = mapper.map_name('thing', False)
    dir_rec = mapper.map_name('thing', True)
    assert(file_rec['iso_name'] == 'THING.;1')
    assert(dir_rec['iso_name'] == 'THING_0')
    assert(file_rec['joliet_name'] == 'thing')
    assert(dir_rec['joliet_name'] == 'thing_0')
    assert(dir_rec['iso_conflicts'] == ['thing'])


def test_name_mapper_file_and_directory_cannot_share_slot_dir_first():
    mapper = pycdlib.utils.NameMapper(3)
    dir_rec = mapper.map_name('thing', True)
    file_rec = mapper.map_name('thing', False)
    assert(dir_rec['iso_name'] == 'THING')
    assert(file_rec['iso_name'] == 'THING_0.;1')
    assert(dir_rec['joliet_name'] == 'thing')
    assert(file_rec['joliet_name'] == 'thing_0')


def test_name_mapper_level_one_preserves_extension_on_alias():
    mapper = pycdlib.utils.NameMapper(1)
    first = mapper.map_name('test.log', False)
    second = mapper.map_name('TEST.LOG', False)
    assert(first['iso_name'] == 'TEST.LOG;1')
    assert(second['iso_name'] == 'TEST_0.LOG;1')
    assert(second['iso_basename'] == 'TEST_0')
    assert(second['iso_ext'] == 'LOG;1')
    assert(len(second['iso_basename']) <= 8)


def test_name_mapper_joliet_alias_stays_within_64_chars():
    mapper = pycdlib.utils.NameMapper(3)
    long_name = 'L' * 64
    for index in range(1200):
        record = mapper.map_name(long_name + str(index), index % 3 == 0)
        assert(len(record['joliet_name']) <= 64)


def test_name_mapper_iso_aliases_stay_within_level_limits():
    for level, base_limit, dir_limit in ((1, 8, 8), (3, 30, 31)):
        mapper = pycdlib.utils.NameMapper(level)
        for index in range(500):
            record = mapper.map_name('L' * 64 + str(index), index % 2 == 0)
            if record['is_dir']:
                assert(len(record['iso_basename']) <= dir_limit)
            else:
                assert(len(record['iso_basename']) <= base_limit)


def test_name_mapper_all_iso_and_joliet_names_unique():
    mapper = pycdlib.utils.NameMapper(3)
    for index in range(300):
        mapper.map_name('sameprefix%d' % (index,), index % 2 == 0)
    iso_keys = list(mapper._iso_occupied.keys())
    joliet_keys = list(mapper._joliet_occupied.keys())
    assert(len(iso_keys) == len(set(iso_keys)))
    assert(len(joliet_keys) == len(set(joliet_keys)))


def test_name_mapper_unicode_truncation_collision():
    mapper = pycdlib.utils.NameMapper(3)
    name = '日本語' + 'x' * 70
    first = mapper.map_name(name, False)
    second = mapper.map_name(name + 'y', False)
    assert(len(first['joliet_name']) == 64)
    assert(len(second['joliet_name']) <= 64)
    assert(first['joliet_name'] != second['joliet_name'])
    assert(second['joliet_aliased'])
    assert(second['joliet_conflicts'] == [name])


def test_name_mapper_seed_blocks_existing_disc_names():
    mapper = pycdlib.utils.NameMapper(3)
    mapper.seed(iso_identifier='FOO.;1', joliet_identifier='foo')
    record = mapper.map_name('foo', False)
    assert(record['iso_name'] == 'FOO_0.;1')
    assert(record['joliet_name'] == 'foo_0')
    assert(record['iso_conflicts'] == ['<already on disc>'])
    assert(record['joliet_conflicts'] == ['<already on disc>'])


def test_name_mapper_lookup_accessors_are_type_aware():
    mapper = pycdlib.utils.NameMapper(3)
    mapper.map_name('dual', False)
    mapper.map_name('dual', True)
    assert(mapper.iso_name_for('dual', False) == 'DUAL.;1')
    assert(mapper.iso_name_for('dual', True) == 'DUAL_0')
    assert(mapper.joliet_name_for('dual', False) == 'dual')
    assert(mapper.joliet_name_for('dual', True) == 'dual_0')
    assert(mapper.lookup('dual', False)['is_dir'] is False)
    assert(mapper.lookup('dual', True)['is_dir'] is True)
    assert(mapper.lookup('missing', False) is None)


def test_name_mapper_level_four_does_not_truncate_iso():
    mapper = pycdlib.utils.NameMapper(4)
    name = 'a' * 100
    record = mapper.map_name(name, False)
    assert(record['iso_name'] == name)
    # Joliet still enforces its own 64-character limit.
    assert(len(record['joliet_name']) == 64)


def test_name_mapper_level_four_iso_names_stay_distinct():
    # At level 4 the ISO mangler does not change case, so names that differ
    # only by case keep distinct, untruncated ISO identifiers.
    mapper = pycdlib.utils.NameMapper(4)
    lower = mapper.map_name('long' * 30, False)
    upper = mapper.map_name('LONG' * 30, False)
    assert(lower['iso_name'] != upper['iso_name'])
    assert(not lower['iso_aliased'])
    assert(not upper['iso_aliased'])


def test_name_mapper_level_four_joliet_truncation_still_aliases():
    # ISO level 4 does not relax the separate Joliet 64-character limit.
    mapper = pycdlib.utils.NameMapper(4)
    first = mapper.map_name('j' * 70, False)
    second = mapper.map_name('j' * 70 + 'k', False)
    assert(len(first['iso_name']) > 64)
    assert(len(second['joliet_name']) <= 64)
    assert(first['joliet_name'] != second['joliet_name'])
    assert(second['joliet_aliased'])


def test_iso_collision_key_normalizes_file_and_dir():
    assert(pycdlib.utils._iso_collision_key('FOO.;1') == 'FOO')
    assert(pycdlib.utils._iso_collision_key('FOO.TXT;1') == 'FOO.TXT')
    assert(pycdlib.utils._iso_collision_key('FOO') == 'FOO')


def test_name_mapper_records_preserve_order_and_correspondence():
    mapper = pycdlib.utils.NameMapper(3)
    mapper.map_name('first.txt', False)
    mapper.map_name('FIRST.TXT', False)
    mapper.map_name('adir', True)
    records = mapper.records()
    assert([record['original'] for record in records] ==
           ['first.txt', 'FIRST.TXT', 'adir'])
    # The alias record keeps the original name alongside its ISO identifier.
    assert(records[1]['original'] == 'FIRST.TXT')
    assert(records[1]['iso_name'] == 'FIRST_0.TXT;1')
    assert(records[1]['iso_conflicts'] == ['first.txt'])
    # The returned list is a copy and cannot mutate the mapper.
    records.clear()
    assert(len(mapper.records()) == 3)
