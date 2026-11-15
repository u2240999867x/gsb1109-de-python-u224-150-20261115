# -*- coding: utf-8

import io
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pycdlib
from pycdlib import salvage
from pycdlib import pycdlibexception


def make_iso(entries):
    """
    Build an in-memory ISO.  Each entry is a tuple of (path, content) for a
    file or (path, None) for a directory, using plain ISO9660 paths.
    """
    iso = pycdlib.PyCdlib()
    iso.new()
    for path, content in entries:
        if content is None:
            iso.add_directory(path)
        else:
            iso.add_fp(io.BytesIO(content), len(content), path)
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    out.seek(0)
    return out


def open_iso(fp):
    iso = pycdlib.PyCdlib()
    iso.open_fp(fp)
    return iso


def test_parse_bad_blocks_accepts_ints_ranges_and_strings():
    assert salvage.parse_bad_blocks([3, '5', '7-9']) == frozenset([3, 5, 7, 8, 9])
    assert salvage.parse_bad_blocks([' 10 ', '12-12', 10]) == frozenset([10, 12])
    assert salvage.parse_bad_blocks([]) == frozenset()


def test_parse_bad_blocks_rejects_bad_input():
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        salvage.parse_bad_blocks(['nope'])
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        salvage.parse_bad_blocks(['9-3'])
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        salvage.parse_bad_blocks([-1])


def test_clean_iso_everything_complete():
    iso = open_iso(make_iso([('/A.;1', b'abc'), ('/D', None), ('/D/B.;1', b'def')]))
    plan = salvage.assess_iso(iso, [])
    assert plan.summary() == {'complete': 4, 'partial': 0, 'untrusted': 0}
    assert all(assessment.status == salvage.FILE_COMPLETE for assessment in plan.assessments)
    assert all(assessment.extractable for assessment in plan.assessments)
    iso.close()


def test_empty_file_is_complete_without_data_blocks():
    iso = open_iso(make_iso([('/EMPTY.;1', b''), ('/FULL.;1', b'x' * 2048)]))
    full_extent = iso.get_record(iso_path='/FULL.;1').extent_location()
    empty_record = iso.get_record(iso_path='/EMPTY.;1')

    # Zero-length files may carry a random extent; flagging that block must
    # not turn the empty file into a partial/missing entry.
    plan = salvage.assess_iso(iso, [full_extent, empty_record.extent_location()])
    by_path = plan.by_path()
    empty = by_path['/EMPTY.;1']
    assert empty.entry_type == 'empty'
    assert empty.status == salvage.FILE_COMPLETE
    assert empty.data_blocks == tuple()
    assert empty.missing_bytes == 0
    assert empty.extractable
    iso.close()


def test_partial_file_tracks_bad_blocks_and_missing_bytes():
    content = b'A' * 2048 + b'B' * 2048 + b'C' * 100
    iso = open_iso(make_iso([('/BIG.;1', content)]))
    start = iso.get_record(iso_path='/BIG.;1').extent_location()

    middle_bad = salvage.assess_iso(iso, [start + 1]).by_path()['/BIG.;1']
    assert middle_bad.status == salvage.FILE_PARTIAL
    assert middle_bad.bad_blocks == (start + 1,)
    assert middle_bad.missing_bytes == 2048
    assert middle_bad.readable_bytes == 2148
    assert not middle_bad.extractable

    tail_bad = salvage.assess_iso(iso, [start + 2]).by_path()['/BIG.;1']
    assert tail_bad.status == salvage.FILE_PARTIAL
    # The final block is only partially used, so only the 100 tail bytes count.
    assert tail_bad.missing_bytes == 100
    assert tail_bad.readable_bytes == 4096
    iso.close()


def test_shared_bad_block_affects_every_hard_link():
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(b'Z' * 2048), 2048, '/SHARED.;1')
    iso.add_hard_link(iso_old_path='/SHARED.;1', iso_new_path='/LINK.;1')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()

    iso = open_iso(out)
    shared_extent = iso.get_record(iso_path='/SHARED.;1').extent_location()
    assert iso.get_record(iso_path='/LINK.;1').extent_location() == shared_extent

    by_path = salvage.assess_iso(iso, [shared_extent]).by_path()
    assert by_path['/SHARED.;1'].status == salvage.FILE_PARTIAL
    assert by_path['/LINK.;1'].status == salvage.FILE_PARTIAL
    assert by_path['/SHARED.;1'].data_blocks == by_path['/LINK.;1'].data_blocks
    iso.close()


def test_joliet_bridge_shares_bad_block_across_namespaces():
    iso = pycdlib.PyCdlib()
    iso.new(joliet=3)
    iso.add_fp(io.BytesIO(b'J' * 2048), 2048, iso_path='/FILE.;1',
               joliet_path='/file_jt')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()

    iso = open_iso(out)
    extent = iso.get_record(iso_path='/FILE.;1').extent_location()
    iso_view = salvage.assess_iso(iso, [extent], path_type='iso_path').by_path()
    joliet_view = salvage.assess_iso(iso, [extent], path_type='joliet_path').by_path()
    assert iso_view['/FILE.;1'].status == salvage.FILE_PARTIAL
    assert joliet_view['/file_jt'].status == salvage.FILE_PARTIAL
    iso.close()


def test_bad_directory_block_marks_directory_untrusted():
    iso = open_iso(make_iso([
        ('/D', None),
        ('/D/INNER.;1', b'q' * 2048),
        ('/SAFE.;1', b's' * 2048),
    ]))
    dir_extent = iso.get_record(iso_path='/D').extent_location()

    plan = salvage.assess_iso(iso, [dir_extent])
    by_path = plan.by_path()
    assert by_path['/D'].status == salvage.DIR_UNTRUSTED
    assert by_path['/D'].bad_blocks == (dir_extent,)

    inner = by_path['/D/INNER.;1']
    assert inner.under_untrusted_dir is True
    assert not inner.extractable

    safe = by_path['/SAFE.;1']
    assert safe.status == salvage.FILE_COMPLETE
    assert safe.extractable
    iso.close()


def test_untrusted_directory_propagates_through_nested_descendants():
    iso = open_iso(make_iso([
        ('/BAD', None),
        ('/BAD/SUB', None),
        ('/BAD/SUB/DEEP.;1', b'd' * 10),
    ]))
    bad_extent = iso.get_record(iso_path='/BAD').extent_location()
    by_path = salvage.assess_iso(iso, [bad_extent]).by_path()
    assert by_path['/BAD'].status == salvage.DIR_UNTRUSTED
    assert by_path['/BAD/SUB'].under_untrusted_dir is True
    assert by_path['/BAD/SUB/DEEP.;1'].under_untrusted_dir is True
    iso.close()


def test_udf_classification_uses_partition_relative_blocks():
    iso = pycdlib.PyCdlib()
    iso.new(udf='2.60')
    iso.add_fp(io.BytesIO(b'U' * 2048 + b'V' * 500), 2548, udf_path='/udf_file')
    iso.add_fp(io.BytesIO(b''), 0, udf_path='/udf_empty')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()

    iso = open_iso(out)
    part_start = iso.udf_main_descs.partitions[0].part_start_location
    relative = iso.get_record(udf_path='/udf_file').alloc_descs[0].log_block_num

    plan = salvage.assess_iso(iso, [part_start + relative])
    assert plan.path_type == 'udf_path'
    by_path = plan.by_path()
    assert by_path['/udf_file'].status == salvage.FILE_PARTIAL
    assert by_path['/udf_file'].bad_blocks == (part_start + relative,)
    assert by_path['/udf_empty'].status == salvage.FILE_COMPLETE
    iso.close()


def test_dr_entry_blocks_walks_multiextent_chain():
    pvd = pycdlib.headervd.pvd_factory(b'', b'', 0, 0, 2048, b'', b'', b'',
                                       b'', b'', b'', b'', 0.0, b'', False)
    root = pycdlib.dr.DirectoryRecord()
    root.new_root(pvd, 1, 2048, time.time())

    chunks = []
    for index in range(3):
        chunk = pycdlib.dr.DirectoryRecord()
        chunk.new_file(pvd, 2048, b'BIG.TAR;1', root, 1, '', b'', False, 0,
                       time.time())
        chunk.orig_extent_loc = 40 + index
        chunks.append(chunk)
    root.add_child(chunks[0], 2048)
    for chunk in chunks[1:]:
        root.add_child(chunk, 2048, True)

    blocks = salvage._dr_entry_blocks(chunks[0], 2048)
    assert blocks == tuple(range(40, 43))
    assert salvage._dr_total_length(chunks[0]) == 3 * 2048


def test_extract_recoverable_skips_untrusted_and_partial_by_default(tmpdir):
    iso = open_iso(make_iso([
        ('/GOOD.;1', b'keep me'),
        ('/BROKEN.;1', b'X' * 2048),
        ('/D', None),
        ('/D/CHILD.;1', b'c' * 2048),
        ('/EMPTY.;1', b''),
    ]))
    broken_extent = iso.get_record(iso_path='/BROKEN.;1').extent_location()
    dir_extent = iso.get_record(iso_path='/D').extent_location()

    extract_root = str(tmpdir.mkdir('out'))
    plan, written = salvage.extract_recoverable(
        iso, [broken_extent, dir_extent], extract_root)

    names = sorted(os.path.basename(path) for _, path in written)
    assert names == ['EMPTY.;1', 'GOOD.;1']
    assert not os.path.exists(os.path.join(extract_root, 'BROKEN.;1'))
    assert not os.path.exists(os.path.join(extract_root, 'D'))
    assert open(os.path.join(extract_root, 'GOOD.;1'), 'rb').read() == b'keep me'
    assert os.path.getsize(os.path.join(extract_root, 'EMPTY.;1')) == 0
    iso.close()


def test_extract_recoverable_zero_fills_partial_with_offsets(tmpdir):
    content = b'A' * 2048 + b'B' * 2048 + b'C' * 2048
    iso = open_iso(make_iso([('/P.;1', content)]))
    start = iso.get_record(iso_path='/P.;1').extent_location()

    extract_root = str(tmpdir.mkdir('out'))
    salvage.extract_recoverable(iso, [start + 1], extract_root,
                                include_partial=True)
    recovered = open(os.path.join(extract_root, 'P.;1.partial'), 'rb').read()
    assert recovered[:2048] == b'A' * 2048
    assert recovered[2048:4096] == b'\x00' * 2048
    assert recovered[4096:] == b'C' * 2048
    iso.close()


def test_extract_recoverable_recreates_complete_symlink(tmpdir):
    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09')
    iso.add_fp(io.BytesIO(b'R' * 16), 16, iso_path='/REAL.;1', rr_name='real')
    iso.add_symlink('/SYM.;1', 'link', 'real')
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()

    iso = open_iso(out)
    extract_root = str(tmpdir.mkdir('out'))
    salvage.extract_recoverable(iso, [], extract_root)
    link_path = os.path.join(extract_root, 'link')
    assert os.path.islink(link_path)
    assert os.readlink(link_path) == 'real'
    assert open(os.path.join(extract_root, 'real'), 'rb').read() == b'R' * 16
    iso.close()


def test_extract_recoverable_rejects_missing_root(tmpdir):
    iso = open_iso(make_iso([('/A.;1', b'a')]))
    missing = os.path.join(str(tmpdir), 'does-not-exist')
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        salvage.extract_recoverable(iso, [], missing)
    iso.close()


def test_assess_requires_open_iso():
    iso = pycdlib.PyCdlib()
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        salvage.assess_iso(iso, [])


def test_assess_rejects_path_type_the_iso_lacks():
    iso = open_iso(make_iso([('/A.;1', b'a')]))
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        salvage.assess_iso(iso, [], path_type='udf_path')
    iso.close()


def test_plan_helpers_filter_and_lookup():
    iso = open_iso(make_iso([('/A.;1', b'a' * 2048), ('/D', None)]))
    extent = iso.get_record(iso_path='/A.;1').extent_location()
    plan = salvage.assess_iso(iso, [extent])

    assert plan.entries_with_status(salvage.FILE_PARTIAL)[0].path == '/A.;1'
    assert plan.entries_with_status(salvage.DIR_UNTRUSTED) == []
    assert '/A.;1' in plan.by_path()
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput):
        plan.entries_with_status('bogus')
    iso.close()
