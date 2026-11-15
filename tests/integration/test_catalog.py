# -*- coding: utf-8 -*-

import io
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pycdlib
import pycdlib.pycdlibexception
from pycdlib import catalog


def make_three_namespace_iso():
    """Build an in-memory ISO carrying ISO9660, Joliet, and Rock Ridge names."""
    buf = io.BytesIO()
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1, rock_ridge='1.09', joliet=3)
    iso.add_directory('/DIR1', rr_name='dir1', joliet_path='/dir1')
    iso.add_fp(io.BytesIO(b'foo data\n'), 9, '/FOO.;1', rr_name='foo',
               joliet_path='/foo')
    iso.add_fp(io.BytesIO(b'bar data here\n'), 14, '/DIR1/BAR.;1',
               rr_name='bar', joliet_path='/dir1/bar')
    iso.add_fp(io.BytesIO(b''), 0, '/EMPTY.;1', rr_name='empty',
               joliet_path='/empty')
    iso.add_hard_link(iso_new_path='/FOOLINK.;1', iso_old_path='/FOO.;1',
                      rr_name='foolink')
    iso.add_hard_link(joliet_new_path='/foolink', iso_old_path='/FOO.;1')
    iso.write_fp(buf)
    iso.close()

    opened = pycdlib.PyCdlib()
    opened.open_fp(io.BytesIO(buf.getvalue()))
    return opened


def references_for(cat, namespace, path):
    return [ref for ref in cat.references
            if ref.namespace == namespace and ref.path == path]


def test_catalog_keeps_each_namespace_original_name():
    iso = make_three_namespace_iso()

    cat = catalog.build_catalog(iso)

    iso_ref = references_for(cat, catalog.ISO_NAMESPACE, '/FOO.;1')
    rr_ref = references_for(cat, catalog.ROCK_RIDGE_NAMESPACE, '/foo')
    joliet_ref = references_for(cat, catalog.JOLIET_NAMESPACE, '/foo')

    assert len(iso_ref) == 1
    assert len(rr_ref) == 1
    assert len(joliet_ref) == 1
    assert iso_ref[0].original_name == 'FOO.;1'
    assert rr_ref[0].original_name == 'foo'
    assert joliet_ref[0].original_name == 'foo'

    iso_dir = references_for(cat, catalog.ISO_NAMESPACE, '/DIR1')[0]
    rr_dir = references_for(cat, catalog.ROCK_RIDGE_NAMESPACE, '/dir1')[0]
    assert iso_dir.original_name == 'DIR1'
    assert rr_dir.original_name == 'dir1'

    iso.close()


def test_catalog_does_not_match_on_display_name():
    buf = io.BytesIO()
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1, rock_ridge='1.09', joliet=3)
    iso.add_fp(io.BytesIO(b'alpha\n'), 6, '/ALPHA.;1', rr_name='alpha',
               joliet_path='/alpha-joliet')
    iso.write_fp(buf)
    iso.close()

    opened = pycdlib.PyCdlib()
    opened.open_fp(io.BytesIO(buf.getvalue()))
    cat = catalog.build_catalog(opened)

    joliet_ref = references_for(cat, catalog.JOLIET_NAMESPACE, '/alpha-joliet')[0]
    iso_ref = references_for(cat, catalog.ISO_NAMESPACE, '/ALPHA.;1')[0]
    rr_ref = references_for(cat, catalog.ROCK_RIDGE_NAMESPACE, '/alpha')[0]

    # Different display names still point at one entity, because the parsed
    # records share one Inode rather than because the names were matched.
    assert joliet_ref.entity_id == iso_ref.entity_id == rr_ref.entity_id
    file_entities = [e for e in cat.entities if e.kind == catalog.KIND_FILE]
    assert len(file_entities) == 1
    assert len(file_entities[0].references) == 3

    opened.close()


def test_catalog_hard_links_collapse_to_one_entity():
    iso = make_three_namespace_iso()
    cat = catalog.build_catalog(iso)

    foo_iso = references_for(cat, catalog.ISO_NAMESPACE, '/FOO.;1')[0]
    foolink_iso = references_for(cat, catalog.ISO_NAMESPACE, '/FOOLINK.;1')[0]
    foo_joliet = references_for(cat, catalog.JOLIET_NAMESPACE, '/foo')[0]
    foolink_joliet = references_for(cat, catalog.JOLIET_NAMESPACE, '/foolink')[0]
    foo_rr = references_for(cat, catalog.ROCK_RIDGE_NAMESPACE, '/foo')[0]
    foolink_rr = references_for(cat, catalog.ROCK_RIDGE_NAMESPACE, '/foolink')[0]

    entity_ids = {ref.entity_id for ref in (foo_iso, foolink_iso, foo_joliet,
                                            foolink_joliet, foo_rr, foolink_rr)}
    assert entity_ids == {foo_iso.entity_id}

    entity = next(e for e in cat.entities if e.entity_id == foo_iso.entity_id)
    assert entity.kind == catalog.KIND_FILE
    assert entity.data_length == 9
    assert len(entity.references) == 6

    iso.close()


def test_catalog_counts_shared_data_area_once():
    iso = make_three_namespace_iso()
    cat = catalog.build_catalog(iso)

    foo_entities = [e for e in cat.entities if e.data_length == 9]
    assert len(foo_entities) == 1

    # Every reference resolves to a known entity.
    entity_ids = {e.entity_id for e in cat.entities}
    assert all(ref.entity_id in entity_ids for ref in cat.references)

    iso.close()


def test_catalog_zero_length_files_are_distinct_entities():
    iso = make_three_namespace_iso()
    cat = catalog.build_catalog(iso)

    empty_refs = [ref for ref in cat.references if ref.path.endswith('empty')]
    assert len(empty_refs) == 2
    assert len({ref.entity_id for ref in empty_refs}) == 2

    for ref in empty_refs:
        assert ref.extent_location == 0
        assert ref.data_length == 0

    iso.close()


def test_catalog_iso_and_rockridge_share_directory_entity():
    iso = make_three_namespace_iso()
    cat = catalog.build_catalog(iso)

    iso_root = references_for(cat, catalog.ISO_NAMESPACE, '/')[0]
    rr_root = references_for(cat, catalog.ROCK_RIDGE_NAMESPACE, '/')[0]
    assert iso_root.entity_id == rr_root.entity_id
    assert iso_root.entry_type == catalog.KIND_DIRECTORY

    iso_dir = references_for(cat, catalog.ISO_NAMESPACE, '/DIR1')[0]
    rr_dir = references_for(cat, catalog.ROCK_RIDGE_NAMESPACE, '/dir1')[0]
    assert iso_dir.entity_id == rr_dir.entity_id

    iso.close()


def test_catalog_joliet_directories_are_separate_entities():
    iso = make_three_namespace_iso()
    cat = catalog.build_catalog(iso)

    iso_dir = references_for(cat, catalog.ISO_NAMESPACE, '/DIR1')[0]
    joliet_dir = references_for(cat, catalog.JOLIET_NAMESPACE, '/dir1')[0]
    assert iso_dir.entity_id != joliet_dir.entity_id

    directory_entities = [e for e in cat.entities
                          if e.kind == catalog.KIND_DIRECTORY]
    assert len(directory_entities) == 4

    iso.close()


def test_catalog_classifies_symlinks_as_single_entity():
    buf = io.BytesIO()
    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09', joliet=3)
    iso.add_symlink('/LINK', 'link', 'target', joliet_path='/link')
    iso.write_fp(buf)
    iso.close()

    opened = pycdlib.PyCdlib()
    opened.open_fp(io.BytesIO(buf.getvalue()))
    cat = catalog.build_catalog(opened)

    symlink_entities = [e for e in cat.entities
                        if e.kind == catalog.KIND_SYMLINK]
    assert len(symlink_entities) == 1
    assert symlink_entities[0].entity_id.startswith('S')

    symlink_refs = [ref for ref in cat.references
                    if ref.entry_type == catalog.KIND_SYMLINK]
    assert {ref.namespace for ref in symlink_refs} == {
        catalog.ISO_NAMESPACE, catalog.ROCK_RIDGE_NAMESPACE}
    assert len({ref.entity_id for ref in symlink_refs}) == 1

    opened.close()


def test_catalog_reports_shared_extent_conflict_records():
    buf = io.BytesIO()
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1, rock_ridge='1.09', joliet=3)
    iso.add_directory('/D', rr_name='d', joliet_path='/d')
    iso.add_fp(io.BytesIO(b'victim\n'), 7, '/VICTIM.;1', rr_name='victim',
               joliet_path='/victim')
    iso.add_fp(io.BytesIO(b'other\n'), 6, '/OTHER.;1', rr_name='other',
               joliet_path='/other')
    dir_extent = iso.get_record(iso_path='/D').extent_location()
    iso.write_fp(buf)
    iso.close()

    image = bytearray(buf.getvalue())
    idx = image.find(b'VICTIM')
    rec_start = idx - 33
    struct.pack_into('<L', image, rec_start + 2, dir_extent)
    struct.pack_into('>L', image, rec_start + 6, dir_extent)

    opened = pycdlib.PyCdlib()
    opened.open_fp(io.BytesIO(bytes(image)))
    cat = catalog.build_catalog(opened)

    assert len(cat.conflicts) == 1
    conflict = cat.conflicts[0]
    assert conflict.extent_location == dir_extent

    conflict_paths = {(ref.namespace, ref.path) for ref in conflict.records}
    assert (catalog.ISO_NAMESPACE, '/D') in conflict_paths
    assert (catalog.ISO_NAMESPACE, '/VICTIM.;1') in conflict_paths
    assert (catalog.ROCK_RIDGE_NAMESPACE, '/d') in conflict_paths
    assert (catalog.ROCK_RIDGE_NAMESPACE, '/victim') in conflict_paths

    conflict_entities = {ref.entity_id for ref in conflict.records}
    assert len(conflict_entities) == 2

    opened.close()


def test_catalog_plain_iso_has_single_namespace():
    buf = io.BytesIO()
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(b'x'), 1, '/X.;1')
    iso.write_fp(buf)
    iso.close()

    opened = pycdlib.PyCdlib()
    opened.open_fp(io.BytesIO(buf.getvalue()))
    cat = catalog.build_catalog(opened)

    assert {ref.namespace for ref in cat.references} == {
        catalog.ISO_NAMESPACE}
    assert cat.conflicts == []

    opened.close()


def test_catalog_uninitialized_iso_raises():
    iso = pycdlib.PyCdlib()
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        catalog.build_catalog(iso)


def test_catalog_formatted_output_has_headers():
    iso = make_three_namespace_iso()
    cat = catalog.build_catalog(iso)

    entity_text = catalog.format_entity_catalog(cat)
    reference_text = catalog.format_reference_catalog(cat)
    conflict_text = catalog.format_conflict_catalog(cat)

    assert entity_text.splitlines()[0].startswith('entity_id\t')
    assert reference_text.splitlines()[0].startswith('namespace\t')
    assert conflict_text.splitlines()[0].startswith('extent_location\t')
    assert '/FOO.;1' in reference_text
    assert 'FOO.;1' in entity_text or 'F000001' in entity_text

    iso.close()
