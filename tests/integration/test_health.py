# -*- coding: utf-8 -*-

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pycdlib
from pycdlib import health


def _iso_data(**kwargs):
    """Build an ISO in memory with a fixed set of files and directories."""
    iso = pycdlib.PyCdlib()
    iso.new(**kwargs)

    use_rr = bool(kwargs.get('rock_ridge'))
    use_joliet = kwargs.get('joliet') is not None
    use_udf = bool(kwargs.get('udf'))

    def names(stem, iso_path, joliet_path=None, udf_path=None):
        extra = {}
        if use_rr:
            extra['rr_name'] = stem
        if use_joliet:
            extra['joliet_path'] = joliet_path or '/' + stem
        if use_udf:
            extra['udf_path'] = udf_path or '/' + stem
        return extra

    iso.add_directory('/DIR1', **names('dir1', '/DIR1'))
    iso.add_directory('/DIR2', **names('dir2', '/DIR2'))
    iso.add_fp(io.BytesIO(b'a' * 5000), 5000, '/DIR1/BIG.;1',
               **names('big', '/DIR1/BIG.;1', '/dir1/big', '/dir1/big'))
    iso.add_fp(io.BytesIO(b'b' * 3000), 3000, '/DIR1/MID.;1',
               **names('mid', '/DIR1/MID.;1', '/dir1/mid', '/dir1/mid'))
    iso.add_fp(io.BytesIO(b'c' * 100), 100, '/DIR2/SMALL.;1',
               **names('small', '/DIR2/SMALL.;1', '/dir2/small', '/dir2/small'))
    iso.add_fp(io.BytesIO(b''), 0, '/EMPTY.;1',
               **names('empty', '/EMPTY.;1', '/empty', '/empty'))
    iso.add_fp(io.BytesIO(b'd' * 100), 100, '/SHARED.;1',
               **names('shared', '/SHARED.;1', '/shared', '/shared'))
    extra = {}
    if use_rr:
        extra['rr_name'] = 'shared2'
    if use_udf:
        iso.add_hard_link(iso_old_path='/SHARED.;1', udf_new_path='/shared2')
    else:
        iso.add_hard_link(iso_old_path='/SHARED.;1',
                          iso_new_path='/SHARED2.;1', **extra)

    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    return out.getvalue()


def _block_of_extent(iso, **kwargs):
    return iso.get_file_byte_extents(**kwargs)[0][0] // iso.logical_block_size


def _zero(data, *blocks):
    damaged = bytearray(data)
    for block in blocks:
        damaged[block * 2048:(block + 1) * 2048] = b'\x00' * 2048
    return bytes(damaged)


def _open(tmpdir, data, name='test.iso'):
    path = str(tmpdir.join(name))
    with open(path, 'wb') as outfp:
        outfp.write(data)
    iso = pycdlib.PyCdlib()
    iso.open(path)
    return iso, path


################################ BLOCK TABLE #################################

def test_health_block_table_parse_text():
    table = health.parse_block_table(
        '# damaged sectors\n'
        '3, 5-7\n'
        '0x10..0x11 // tail\n'
        '\n'
        '8\n')
    assert table.ranges == ((3, 3), (5, 8), (16, 17))
    assert list(table) == [3, 5, 6, 7, 8, 16, 17]
    assert len(table) == 7
    assert table.contains(7)
    assert not table.contains(9)


def test_health_block_table_accepts_iterables_and_ranges():
    assert health.BlockTable(None).ranges == ()
    assert health.BlockTable([]).ranges == ()
    assert health.BlockTable([1, 2, 3]).ranges == ((1, 3),)
    assert health.BlockTable([(4, 6), range(7, 9), '10-11']).ranges == ((4, 8), (10, 11))
    assert health.BlockTable('12 14').ranges == ((12, 12), (14, 14))


def test_health_block_table_invalid_entries():
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        health.parse_block_table('abc')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        health.parse_block_table('9-4')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        health.BlockTable([-1])
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        health.BlockTable([True])
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        health.BlockTable([range(0, 10, 2)])


def test_health_block_table_intervals_and_byte_sizes():
    table = health.BlockTable([5, 6])
    assert table.missing_bytes(4 * 2048 + 1024, 3 * 2048, 2048) == 2 * 2048
    assert table.missing_intervals(4 * 2048 + 1024, 3 * 2048, 2048) == ((5 * 2048, 7 * 2048),)
    # A 512-byte table is interpreted against the same byte offsets.
    sectors = health.BlockTable([5 * 4 + 1, 5 * 4 + 2])
    assert sectors.missing_intervals(5 * 2048, 2048, 512) == ((5 * 2048 + 512, 5 * 2048 + 1536),)
    assert table.missing_bytes(0, 0, 2048) == 0


def test_health_block_table_from_path_and_file_object(tmpdir):
    path = str(tmpdir.join('missing.txt'))
    with open(path, 'w', encoding='utf-8') as outfp:
        outfp.write('2\n4-5\n')
    assert health.load_block_table(path).ranges == ((2, 2), (4, 5))
    with open(path, 'r', encoding='utf-8') as infp:
        assert health.load_block_table(infp).ranges == ((2, 2), (4, 5))


################################ BASE STATUSES ###############################

def test_health_all_complete(tmpdir):
    iso, _path = _open(tmpdir, _iso_data())
    report = iso.audit_missing_blocks([])
    assert report.summary() == {
        'files': {'complete': 6, 'partial': 0, 'missing': 0, 'untrusted': 0},
        'directories': {'trusted': 3, 'untrusted': 0},
    }
    # Only the ISO9660 view is present, and it does not include CL records.
    assert {entry.path for entry in report.files} == {
        '/DIR1/BIG.;1', '/DIR1/MID.;1', '/DIR2/SMALL.;1', '/EMPTY.;1',
        '/SHARED.;1', '/SHARED2.;1'}
    iso.close()


def test_health_empty_file_is_complete_not_missing(tmpdir):
    data = _iso_data()
    # Even a table naming block 0, where an empty file's (unused) extent
    # pointer happens to point, must not make the empty file missing.
    iso, _path = _open(tmpdir, data)
    first_block = _block_of_extent(iso, iso_path='/DIR1/BIG.;1')
    report = iso.audit_missing_blocks([0, first_block])
    assert report.file('/EMPTY.;1').status == health.FILE_COMPLETE
    assert report.file('/EMPTY.;1').missing_bytes == 0
    assert report.file('/EMPTY.;1').data_status == health.FILE_COMPLETE
    assert report.file('/DIR1/BIG.;1').status == health.FILE_PARTIAL
    iso.close()


def test_health_partial_and_missing(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    first = _block_of_extent(iso, iso_path='/DIR1/BIG.;1')
    # BIG is 3 blocks; losing one leaves it partial.
    report = iso.audit_missing_blocks([first + 1])
    big = report.file('/DIR1/BIG.;1')
    assert big.status == health.FILE_PARTIAL
    assert big.data_status == health.FILE_PARTIAL
    assert big.missing_bytes == 2048
    assert big.readable_bytes == big.size - 2048
    assert big.missing_ranges == ((2048, 2048),)

    # Losing all of BIG's blocks makes it missing, not partial.
    report = iso.audit_missing_blocks(range(first, first + 3))
    big = report.file('/DIR1/BIG.;1')
    assert big.status == health.FILE_MISSING
    assert big.readable_bytes == 0
    assert big.missing_bytes == big.size
    iso.close()


def test_health_shared_extent_affects_every_link(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    shared = _block_of_extent(iso, iso_path='/SHARED.;1')
    report = iso.audit_missing_blocks([shared])
    one = report.file('/SHARED.;1')
    two = report.file('/SHARED2.;1')
    assert one.status == health.FILE_MISSING
    assert two.status == health.FILE_MISSING
    assert one.missing_blocks == two.missing_blocks == (shared,)
    iso.close()


def test_health_unrelated_file_unaffected(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    small = _block_of_extent(iso, iso_path='/DIR2/SMALL.;1')
    report = iso.audit_missing_blocks([small])
    assert report.file('/DIR2/SMALL.;1').status == health.FILE_MISSING
    assert report.file('/DIR1/BIG.;1').status == health.FILE_COMPLETE
    assert report.file('/DIR1/MID.;1').status == health.FILE_COMPLETE
    iso.close()


################################ DIRECTORIES #################################

def test_health_damaged_directory_is_untrusted_and_propagates(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    dir1 = iso.get_record(iso_path='/DIR1')
    dir1_block = dir1.extent_location()
    report = iso.audit_missing_blocks([dir1_block])

    directory = [entry for entry in report.directories
                 if entry.view == 'iso' and entry.path == '/DIR1'][0]
    assert directory.trusted is False
    assert directory.status == health.DIRECTORY_UNTRUSTED
    assert directory.reason == 'directory_extent_missing'

    # Every file below the damaged directory is untrusted, even though its
    # data blocks are intact.
    for path in ('/DIR1/BIG.;1', '/DIR1/MID.;1'):
        entry = report.file(path)
        assert entry.status == health.FILE_UNTRUSTED
        assert entry.reason == 'parent_directory_untrusted'
        assert entry.data_status == health.FILE_COMPLETE

    # A single-path lookup reaches the same conclusion without walking.
    single = iso.file_health([dir1_block], iso_path='/DIR1/MID.;1')
    assert single.status == health.FILE_UNTRUSTED
    assert single.reason == 'parent_directory_untrusted'

    # Siblings under another directory, and the root itself, stay usable.
    assert report.file('/DIR2/SMALL.;1').status == health.FILE_COMPLETE
    assert report.file('/EMPTY.;1').status == health.FILE_COMPLETE
    assert [entry.path for entry in report.untrusted_directories] == ['/DIR1']
    iso.close()


def test_health_damaged_root_directory(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    root_block = iso.pvd.root_directory_record().extent_location()
    report = iso.audit_missing_blocks([root_block])
    assert report.directories[0].path == '/'
    assert report.directories[0].trusted is False
    # The subdirectories are structurally intact, but their parent is not
    # trustworthy, so every directory in the tree is untrusted.
    assert report.summary()['directories'] == {'trusted': 0, 'untrusted': 3}
    iso.close()


def test_health_directory_with_multiple_blocks(tmpdir):
    # A directory with many records occupies several blocks; losing the
    # second one still makes the whole directory untrusted.
    iso = pycdlib.PyCdlib()
    iso.new()
    for index in range(80):
        iso.add_fp(io.BytesIO(b'x'), 1, '/FILE%03d.;1' % index)
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    data = out.getvalue()

    iso, _path = _open(tmpdir, data)
    root = iso.pvd.root_directory_record()
    assert root.get_data_length() > iso.logical_block_size
    report = iso.audit_missing_blocks([root.extent_location() + 1])
    assert report.directories[0].trusted is False
    assert report.directories[0].missing_blocks == (root.extent_location() + 1,)
    iso.close()


################################ OTHER VIEWS #################################

def test_health_joliet_view(tmpdir):
    data = _iso_data(joliet=3)
    iso, _path = _open(tmpdir, data)
    big = _block_of_extent(iso, joliet_path='/dir1/big')
    report = iso.audit_missing_blocks([big], views=['iso', 'joliet'])
    assert report.file('/dir1/big', 'joliet').status == health.FILE_PARTIAL
    assert report.file('/DIR1/BIG.;1', 'iso').status == health.FILE_PARTIAL
    assert report.file('/empty', 'joliet').status == health.FILE_COMPLETE
    iso.close()


def test_health_rock_ridge_view_and_relocation(tmpdir):
    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09')
    path = ''
    for index in range(1, 9):
        path += '/DIR%d' % index
        iso.add_directory(path, rr_name='dir%d' % index)
        iso.add_fp(io.BytesIO(b'y' * 10), 10, path + '/FILE%d.;1' % index,
                   rr_name='file%d' % index)
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    data = out.getvalue()

    iso, _path = _open(tmpdir, data)
    report = iso.audit_missing_blocks([], views='rr')
    rr_files = {entry.path for entry in report.files}
    assert '/dir1/dir2/dir3/dir4/dir5/dir6/dir7/dir8/file8' in rr_files
    # The physical RR_MOVED placeholder is not presented as a logical path.
    assert not any('/rr_moved/' in name and 'dir8' in name for name in rr_files)

    # Damage the relocated directory's extent.  The RR view cannot resolve
    # the logical /dir1/.../dir8 path any more, so it is untrusted.
    dir8 = iso.get_record(iso_path='/DIR1/DIR2/DIR3/DIR4/DIR5/DIR6/DIR7/DIR8')
    assert dir8 is not None
    target_block = dir8.extent_location()
    iso.close()

    iso, _path = _open(tmpdir, _zero(data, target_block), 'damaged.iso')
    report = iso.audit_missing_blocks([target_block], views='rr')
    damaged = [entry for entry in report.directories
               if entry.path.endswith('/dir8')]
    # Both the physical /rr_moved/dir8 path and the logical
    # /dir1/.../dir8 path are reported as untrusted.
    assert len(damaged) == 2
    assert all(entry.trusted is False for entry in damaged)
    iso.close()


def test_health_udf_data_and_directory_blocks(tmpdir):
    data = _iso_data(udf='2.60')
    iso, _path = _open(tmpdir, data)
    big = _block_of_extent(iso, udf_path='/dir1/big')
    report = iso.audit_missing_blocks([big], views='udf')
    assert report.file('/dir1/big', 'udf').status == health.FILE_PARTIAL
    assert report.file('/empty', 'udf').status == health.FILE_COMPLETE

    dir1_entry = iso.get_record(udf_path='/dir1')
    part_start = iso.udf_main_descs.partitions[0].part_start_location
    dir1_data_block = part_start + dir1_entry.alloc_descs[0].log_block_num
    report = iso.audit_missing_blocks([dir1_data_block], views='udf')
    assert [entry.path for entry in report.untrusted_directories] == ['/dir1']
    for path in ('/dir1/big', '/dir1/mid'):
        assert report.file(path, 'udf').status == health.FILE_UNTRUSTED
    assert report.file('/dir2/small', 'udf').status == health.FILE_COMPLETE
    iso.close()


def test_health_udf_file_entry_block_is_untrusted(tmpdir):
    data = _iso_data(udf='2.60')
    iso, _path = _open(tmpdir, data)
    entry = iso.get_record(udf_path='/dir1/big')
    entry_block = entry.extent_location()
    report = iso.audit_missing_blocks([entry_block], views='udf')
    result = report.file('/dir1/big', 'udf')
    assert result.status == health.FILE_UNTRUSTED
    assert result.reason == 'udf_file_entry_block_missing'
    iso.close()


def test_health_default_view_is_single_best_view(tmpdir):
    data = _iso_data(rock_ridge='1.09', joliet=3, udf='2.60')
    iso, _path = _open(tmpdir, data)
    report = iso.audit_missing_blocks([])
    # Like pycdlib-extract-files, the default picks UDF when it is present.
    assert {entry.view for entry in report.files} == {'udf'}
    assert {entry.view for entry in report.directories} == {'udf'}
    iso.close()


def test_health_all_views(tmpdir):
    data = _iso_data(rock_ridge='1.09', joliet=3, udf='2.60')
    iso, _path = _open(tmpdir, data)
    big = _block_of_extent(iso, iso_path='/DIR1/BIG.;1')
    report = iso.audit_missing_blocks([big], views=['iso', 'rr', 'joliet', 'udf'])
    assert {entry.view for entry in report.files} == {'iso', 'rr', 'joliet', 'udf'}
    for view, path in (('iso', '/DIR1/BIG.;1'), ('rr', '/dir1/big'),
                       ('joliet', '/dir1/big'), ('udf', '/dir1/big')):
        assert report.file(path, view).status == health.FILE_PARTIAL
    # Different views still report the same empty file as complete.
    for view, path in (('iso', '/EMPTY.;1'), ('rr', '/empty'),
                       ('joliet', '/empty'), ('udf', '/empty')):
        assert report.file(path, view).status == health.FILE_COMPLETE
    iso.close()


################################ VALIDATION ##################################

def test_health_views_validation(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.audit_missing_blocks([], views='bogus')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.audit_missing_blocks([], views='joliet')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.audit_missing_blocks([], views='udf')
    iso.close()


def test_health_audit_not_initialized():
    iso = pycdlib.PyCdlib()
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput) as excinfo:
        iso.audit_missing_blocks([])
    assert str(excinfo.value) == 'This object is not initialized; call either open() or new() to create an ISO'
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([], iso_path='/FOO.;1')


def test_health_invalid_block_size(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    for bad in (0, -1, 'x', True, 1.5):
        with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
            iso.audit_missing_blocks([], block_size=bad)
        with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
            iso.file_health([], block_size=bad, iso_path='/DIR2/SMALL.;1')
    iso.close()


def test_health_file_health_path_validation(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([], iso_path='/DIR2/SMALL.;1', joliet_path='/dir2/small')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([])
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([], bogus_path='/DIR2/SMALL.;1')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([], iso_path='/DIR2')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([], rr_path='/dir2/small')
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([], joliet_path='/dir2/small')
    iso.close()


def test_health_file_health_matches_audit(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    big = _block_of_extent(iso, iso_path='/DIR1/BIG.;1')
    table = health.BlockTable([big + 1])
    report = iso.audit_missing_blocks(table)
    single = iso.file_health(table, iso_path='/DIR1/BIG.;1')
    assert single.status == report.file('/DIR1/BIG.;1').status
    assert single.missing_blocks == report.file('/DIR1/BIG.;1').missing_blocks
    assert single.missing_ranges == report.file('/DIR1/BIG.;1').missing_ranges
    iso.close()


def test_health_file_health_shared_extent_sibling(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    shared = _block_of_extent(iso, iso_path='/SHARED.;1')
    result = iso.file_health([shared], iso_path='/SHARED2.;1')
    assert result.status == health.FILE_MISSING
    assert result.missing_blocks == (shared,)
    iso.close()


def test_health_symlink_is_complete(tmpdir):
    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09')
    iso.add_symlink(symlink_path='/LINK.;1', rr_symlink_name='link',
                    rr_path='/target')
    iso.add_fp(io.BytesIO(b'x'), 1, '/TARGET.;1', rr_name='target')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()

    iso, _path = _open(tmpdir, out.getvalue())
    report = iso.audit_missing_blocks([], views='rr')
    link = report.file('/link', 'rr')
    assert link.status == health.FILE_COMPLETE
    assert link.reason == 'symlink'
    iso.close()


def test_health_report_helpers(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    big = _block_of_extent(iso, iso_path='/DIR1/BIG.;1')
    report = iso.audit_missing_blocks([big + 1])
    assert {entry.path for entry in report.usable_files} >= {'/EMPTY.;1'}
    assert [entry.path for entry in report.salvageable_files] == ['/DIR1/BIG.;1']
    assert report.unusable_files == []
    files, directories = report.for_view('iso')
    assert len(files) == len(report.files)
    assert len(directories) == len(report.directories)
    summary = report.to_dict()
    assert summary['summary'] == report.summary()
    assert summary['files'][0]['path']
    assert summary['block_size'] == 2048
    assert summary['logical_block_size'] == 2048
    with pytest.raises(KeyError):
        report.file('/NOPE.;1')
    iso.close()


def test_health_block_size_512(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    block = _block_of_extent(iso, iso_path='/DIR1/MID.;1')
    # MID is 3000 bytes and starts at a 2048-byte block boundary, so it spans
    # 512-byte sectors 0 through 5.  Losing its second sector leaves part of
    # it readable.
    report = iso.audit_missing_blocks([block * 4 + 1], block_size=512)
    mid = report.file('/DIR1/MID.;1')
    assert mid.status == health.FILE_PARTIAL
    assert mid.missing_bytes == 512
    assert mid.missing_ranges == ((512, 512),)

    # A 512-byte sector table can also classify a whole small file as missing.
    small_block = _block_of_extent(iso, iso_path='/DIR2/SMALL.;1')
    report = iso.audit_missing_blocks([small_block * 4], block_size=512)
    small = report.file('/DIR2/SMALL.;1')
    assert small.status == health.FILE_MISSING
    assert small.missing_bytes == 100
    assert small.missing_ranges == ((0, 100),)
    iso.close()


def test_health_module_functions_match_methods(tmpdir):
    data = _iso_data()
    iso, _path = _open(tmpdir, data)
    big = _block_of_extent(iso, iso_path='/DIR1/BIG.;1')
    report = health.audit_iso(iso, [big])
    assert health.audit_iso(iso, [big]).file('/DIR1/BIG.;1').status == report.file('/DIR1/BIG.;1').status
    result = health.file_health(iso, [big], iso_path='/DIR1/BIG.;1')
    assert result.status == health.FILE_PARTIAL
    iso.close()


def test_health_facade_udf(tmpdir):
    data = _iso_data(udf='2.60')
    iso, _path = _open(tmpdir, data)
    big = _block_of_extent(iso, udf_path='/dir1/big')
    facade = iso.get_udf_facade()
    report = facade.audit_missing_blocks([big])
    assert {entry.view for entry in report.files} == {'udf'}
    assert facade.file_health([big], '/dir1/big').status == health.FILE_PARTIAL
    iso.close()


def test_health_unwritten_new_iso(tmpdir):
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(b'foo'), 3, '/FOO.;1')
    # Extents are assigned lazily; the audit has to force a reshuffle.
    report = iso.audit_missing_blocks([])
    assert report.file('/FOO.;1').status == health.FILE_COMPLETE
    iso.close()


def test_health_file_health_through_damaged_directory(tmpdir):
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_directory('/DIR1')
    iso.add_fp(io.BytesIO(b'A' * 100), 100, '/DIR1/FOO.;1')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    data = out.getvalue()

    iso, path = _open(tmpdir, data)
    dir1_block = iso.get_record(iso_path='/DIR1').extent_location()
    iso.close()

    iso, _path = _open(tmpdir, _zero(data, dir1_block), 'damaged.iso')
    # The path can no longer be resolved because the directory extent is
    # gone; the result must be untrusted, not an exception.
    result = iso.file_health([dir1_block], iso_path='/DIR1/FOO.;1')
    assert result.status == health.FILE_UNTRUSTED
    assert result.reason == 'directory_extent_missing'
    assert result.missing_blocks == (dir1_block,)

    # With an empty table the directory should still resolve; the failure is
    # only excused when the table explains it.
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.file_health([], iso_path='/DIR1/FOO.;1')
    iso.close()


def test_health_zeroed_udf_file_entry_block_is_untrusted(tmpdir):
    iso = pycdlib.PyCdlib()
    iso.new(udf='2.60')
    iso.add_fp(io.BytesIO(b'F' * 100), 100, '/FOO.;1', udf_path='/foo')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    data = out.getvalue()

    iso, _path = _open(tmpdir, data)
    entry_block = iso.get_record(udf_path='/foo').extent_location()
    iso.close()

    iso, _path = _open(tmpdir, _zero(data, entry_block), 'zeroed-entry.iso')
    report = iso.audit_missing_blocks([entry_block], views='udf')
    result = report.file('/foo', 'udf')
    assert result.status == health.FILE_UNTRUSTED
    assert result.reason == 'udf_file_entry_block_missing'
    assert result.missing_blocks == (entry_block,)
    iso.close()
