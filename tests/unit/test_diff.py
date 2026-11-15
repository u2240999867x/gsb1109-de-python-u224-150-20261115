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

from pycdlib import dates
from pycdlib import diff as diffmod
from pycdlib import pycdlibexception
from pycdlib import udf as udfmod


def _snapshot(entries, namespace='rr', block_size=2048):
    snap = diffmod.Snapshot(None, namespace, block_size)
    for spec in entries:
        entry = diffmod.Entry(spec['path'], namespace, spec['kind'])
        entry.digest = spec.get('digest')
        entry.attributes = spec.get('attributes', {})
        entry.extents = spec.get('extents', ())
        entry.size = spec.get('size', 0)
        snap.entries[entry.path] = entry
    return snap


def _dir(path):
    return {'path': path, 'kind': 'dir'}


def _file(path, digest, extents=(), attributes=None, size=None):
    # Empty files never carry identity evidence, so make test files look
    # non-empty unless a test says otherwise.
    if size is None:
        size = len(digest) if digest else 0
    return {'path': path, 'kind': 'file', 'digest': digest,
            'extents': extents, 'attributes': attributes or {}, 'size': size}


def test_entry_kinds_are_preserved_in_snapshot_dict():
    entry = diffmod.Entry('/FILE.;1', 'iso', 'file')
    entry.size = 4
    entry.digest = 'abc'
    entry.attributes = {'iso_flags': 0}
    entry.extents = ((10, 4),)
    assert entry.to_dict() == {'path': '/FILE.;1', 'namespace': 'iso',
                               'kind': 'file', 'size': 4, 'digest': 'abc',
                               'attributes': {'iso_flags': 0},
                               'extents': [[10, 4]], 'readable': True}


def test_diff_without_old_image_never_reports_deletions():
    # No old snapshot at all: the new image's entries must not be guessed to
    # be additions, and nothing may be reported as removed.
    new = _snapshot([_dir('/'), _file('/A.;1', 'aaa'), _file('/B.;1', 'bbb')])
    result = diffmod.diff_snapshots(None, new)
    assert result.removed == []
    assert result.added == []
    assert not result.deletion_verified
    assert result.notes
    assert 'no deletion is inferred' in result.notes[0].lower()


def test_diff_with_incomplete_old_image_reports_unverified_not_removed():
    old = _snapshot([_dir('/'), _file('/A.;1', 'aaa'), _file('/B.;1', 'bbb')])
    old.complete = False
    new = _snapshot([_dir('/'), _file('/A.;1', 'aaa')])
    result = diffmod.diff_snapshots(old, new)
    assert result.removed == []
    assert not result.deletion_verified
    assert ('old', '/B.;1') in [(side, entry.path) for (side, entry) in result.unverified]


def test_diff_with_complete_images_reports_removals():
    old = _snapshot([_dir('/'), _file('/A.;1', 'aaa'), _file('/B.;1', 'bbb')])
    new = _snapshot([_dir('/'), _file('/A.;1', 'aaa'), _file('/C.;1', 'ccc')])
    result = diffmod.diff_snapshots(old, new)
    assert [entry.path for entry in result.removed] == ['/B.;1']
    assert [entry.path for entry in result.added] == ['/C.;1']
    assert result.deletion_verified


def test_same_path_different_digest_is_content_update():
    old = _snapshot([_dir('/'), _file('/A.;1', 'aaa', size=4)])
    new = _snapshot([_dir('/'), _file('/A.;1', 'bbb', size=5)])
    result = diffmod.diff_snapshots(old, new)
    assert len(result.content_updates) == 1
    update = result.content_updates[0]
    assert update.old.path == update.new.path == '/A.;1'
    assert update.evidence == 'path'
    assert not result.path_migrations
    assert result.to_dict()['content_updates'][0]['path_changed'] is False


def test_different_path_same_digest_is_path_migration():
    old = _snapshot([_dir('/'), _file('/doc/readme.txt', 'same')])
    new = _snapshot([_dir('/'), _file('/docs/readme.txt', 'same')])
    result = diffmod.diff_snapshots(old, new)
    assert len(result.path_migrations) == 1
    assert result.path_migrations[0].old.path == '/doc/readme.txt'
    assert result.path_migrations[0].new.path == '/docs/readme.txt'
    assert not result.content_updates
    assert result.path_migrations[0].to_dict()['size'] == len('same')


def test_same_path_same_digest_attribute_change_only():
    old = _snapshot([_dir('/'), _file('/A.;1', 'aaa', attributes={'rr_mode': 0o100644})])
    new = _snapshot([_dir('/'), _file('/A.;1', 'aaa', attributes={'rr_mode': 0o100600})])
    result = diffmod.diff_snapshots(old, new)
    assert len(result.attribute_only_changes) == 1
    change = result.attribute_only_changes[0]
    assert change.entry.new.path == '/A.;1'
    assert change.changes == {'rr_mode': (0o100644, 0o100600)}
    assert not result.content_updates
    assert result.to_dict()['attribute_only_changes'][0]['changes'] == {
        'rr_mode': {'old': 0o100644, 'new': 0o100600}}


def test_timestamps_are_ignored_by_default():
    old = _snapshot([_dir('/'), _file('/A.;1', 'aaa', attributes={'rr_mtime': '2020-01-01'})])
    new = _snapshot([_dir('/'), _file('/A.;1', 'aaa', attributes={'rr_mtime': '2026-01-01'})])
    result = diffmod.diff_snapshots(old, new)
    assert result.attribute_only_changes == []

    result = diffmod.diff_snapshots(old, new, include_timestamps=True)
    assert len(result.attribute_only_changes) == 1


def test_directory_mode_change_is_attribute_only_even_with_children():
    old = _snapshot([_dir('/'), _dir('/APP'), _file('/APP/CFG.;1', 'cfg')])
    new = _snapshot([_dir('/'), _dir('/APP'), _file('/APP/CFG.;1', 'cfg')])
    old.entries['/APP'].attributes = {'rr_mode': 0o040755}
    new.entries['/APP'].attributes = {'rr_mode': 0o040750}
    result = diffmod.diff_snapshots(old, new)
    assert [change.entry.new.path for change in result.attribute_only_changes] == ['/APP']
    # The unchanged child must not be dragged into the report just because
    # its parent's mode changed.
    assert not result.content_updates
    assert not result.path_migrations


def test_renamed_directory_with_identical_subtree_is_migration():
    old = _snapshot([_dir('/'), _dir('/DOC'), _file('/DOC/README.;1', 'readme')])
    new = _snapshot([_dir('/'), _dir('/DOCS'), _file('/DOCS/README.;1', 'readme')])
    result = diffmod.diff_snapshots(old, new)
    migrated = {change.old.path: change.new.path for change in result.path_migrations}
    assert migrated['/DOC'] == '/DOCS'
    assert migrated['/DOC/README.;1'] == '/DOCS/README.;1'
    assert not result.added
    assert not result.removed


def test_ambiguous_duplicate_digest_is_not_guessed():
    # Two old files are byte-identical and both disappeared from their old
    # paths; the new image has one file with that digest.  There is no
    # directory evidence that picks which one moved, so the pair must be
    # reported ambiguous, not invented.
    old = _snapshot([_dir('/'), _dir('/D1'), _dir('/D2'),
                     _file('/D1/A.;1', 'dup'), _file('/D2/A.;1', 'dup')])
    new = _snapshot([_dir('/'), _file('/DUP.;1', 'dup')])
    result = diffmod.diff_snapshots(old, new)
    assert result.path_migrations == []
    assert any(path == '/DUP.;1' for (_side, path, _digest) in result.ambiguous)


def test_directory_evidence_pairs_moved_and_changed_file():
    # The file both moved into a renamed directory and changed contents.  The
    # directory match is the identity evidence, and the digest difference is
    # what makes it a content update rather than a migration.
    old = _snapshot([_dir('/'), _dir('/BIN'), _file('/BIN/TOOL.;1', 'v1')])
    new = _snapshot([_dir('/'), _dir('/TOOLS'), _file('/TOOLS/TOOL.;1', 'v2')])
    old.entries['/BIN'].attributes = {'rr_mode': 0o040755}
    new.entries['/TOOLS'].attributes = {'rr_mode': 0o040755}
    result = diffmod.diff_snapshots(old, new)
    assert len(result.content_updates) == 1
    assert result.content_updates[0].old.path == '/BIN/TOOL.;1'
    assert result.content_updates[0].new.path == '/TOOLS/TOOL.;1'
    assert result.content_updates[0].evidence == 'directory+digest-differs'


def test_shared_blocks_separated():
    old = _snapshot([_dir('/'),
                     _file('/X.;1', 'p', extents=((2048, 8),)),
                     _file('/Y.;1', 'p', extents=((2048, 8),))])
    new = _snapshot([_dir('/'),
                     _file('/X.;1', 'p', extents=((2048, 8),)),
                     _file('/Y.;1', 'p', extents=((4096, 8),))])
    result = diffmod.diff_snapshots(old, new)
    assert len(result.shared_block_changes) == 1
    change = result.shared_block_changes[0]
    assert change.change == 'separated'
    assert change.old_paths == ('/X.;1', '/Y.;1')
    assert change.to_dict()['old_extents'] == [[[2048, 8]], [[2048, 8]]]


def test_shared_blocks_joined():
    old = _snapshot([_dir('/'),
                     _file('/X.;1', 'p', extents=((2048, 8),)),
                     _file('/Y.;1', 'p', extents=((4096, 8),))])
    new = _snapshot([_dir('/'),
                     _file('/X.;1', 'p', extents=((2048, 8),)),
                     _file('/Y.;1', 'p', extents=((2048, 8),))])
    result = diffmod.diff_snapshots(old, new)
    assert len(result.shared_block_changes) == 1
    assert result.shared_block_changes[0].change == 'joined'
    assert result.shared_block_changes[0].old_paths == ('/X.;1', '/Y.;1')


def test_shared_partial_block_in_udf_counts_as_sharing():
    # Two UDF files that start at different offsets inside the same logical
    # block share that block even though their byte ranges do not overlap.
    old = _snapshot([_dir('/'),
                     _file('/a', 'x', extents=((8192 + 10, 5),)),
                     _file('/b', 'y', extents=((8192 + 500, 5),))])
    old.logical_block_size = 512
    result = diffmod.diff_snapshots(old, old)
    assert result.shared_block_changes == []
    assert old.sharing_edges() == {('/a', '/b')}


def test_adjacent_blocks_do_not_count_as_sharing():
    snap = _snapshot([_dir('/'),
                      _file('/a', 'x', extents=((0, 2048),)),
                      _file('/b', 'y', extents=((2048, 2048),))])
    assert snap.sharing_edges() == set()


def test_snapshot_namespace_mismatch_is_rejected():
    old = _snapshot([_dir('/')], namespace='rr')
    new = _snapshot([_dir('/')], namespace='iso')
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput) as excinfo:
        diffmod.diff_snapshots(old, new)
    assert 'same namespace' in str(excinfo.value)


def test_build_snapshot_rejects_unknown_namespace():
    class FakeIso:
        def has_rock_ridge(self):
            return False
    with pytest.raises(pycdlibexception.PyCdlibInvalidInput) as excinfo:
        diffmod.build_snapshot(FakeIso(), 'bogus')
    assert "Unknown namespace 'bogus'" == str(excinfo.value)


def test_format_text_reports_each_section():
    old = _snapshot([_dir('/'), _file('/A.;1', 'aaa', extents=((2048, 4),))])
    new = _snapshot([_dir('/'), _file('/A.;1', 'bbb', extents=((4096, 4),))])
    result = diffmod.diff_snapshots(old, new)
    text = diffmod.format_text(result)
    assert 'Content updates (1):' in text
    assert 'Path migrations (0):' in text
    assert 'Attribute-only changes (0):' in text
    assert 'Shared-block changes (0):' in text
    assert 'Added (0):' in text
    assert 'Removed (0):' in text


def test_format_text_without_old_image_says_so():
    new = _snapshot([_dir('/'), _file('/A.;1', 'aaa')])
    result = diffmod.diff_snapshots(None, new)
    text = diffmod.format_text(result)
    assert 'deletions cannot be inferred' in text
    assert 'Removed (0):' not in text


def test_snapshot_to_dict_keeps_completeness_and_notes():
    snap = _snapshot([_dir('/'), _file('/A.;1', 'aaa')])
    snap.path = 'old.iso'
    snap.complete = False
    snap.notes.append('a note')
    data = snap.to_dict()
    assert data['path'] == 'old.iso'
    assert data['complete'] is False
    assert data['notes'] == ['a note']
    assert data['entries'][0]['path'] == '/'


def test_iso_diff_has_differences_and_dict_shape():
    old = _snapshot([_dir('/'), _file('/A.;1', 'aaa')])
    new = _snapshot([_dir('/'), _file('/A.;1', 'bbb')])
    result = diffmod.diff_snapshots(old, new)
    assert result.has_differences()
    data = result.to_dict()
    assert data['deletion_verified'] is True
    assert set(data) >= {'old', 'new', 'namespace', 'content_updates',
                         'path_migrations', 'attribute_only_changes',
                         'shared_block_changes', 'added', 'removed',
                         'unverified', 'ambiguous', 'notes'}


def test_snapshot_extentless_entries_do_not_break_sharing():
    old = _snapshot([_dir('/'), _file('/A.;1', 'a', extents=())])
    new = _snapshot([_dir('/'), _file('/A.;1', 'a', extents=())])
    result = diffmod.diff_snapshots(old, new)
    assert result.shared_block_changes == []


def test_entry_readability_flag_survives_to_dict():
    entry = diffmod.Entry('/A.;1', 'iso', 'file')
    entry.readable = False
    entry.read_error = 'boom'
    assert entry.to_dict()['readable'] is False


def test_symlink_entry_dict_has_no_extents():
    entry = diffmod.Entry('/link', 'rr', 'symlink')
    entry.digest = 'hash'
    assert entry.to_dict()['extents'] == []


def test_moved_empty_file_uses_directory_evidence():
    # Empty files carry no usable digest, but a unique name under a matched
    # parent is still explicit directory evidence.
    old = _snapshot([_dir('/'), _dir('/DOC'), _file('/DOC/EMPTY.;1', 'e3b0c442', size=0)])
    new = _snapshot([_dir('/'), _dir('/DOCS'), _file('/DOCS/EMPTY.;1', 'e3b0c442', size=0)])
    result = diffmod.diff_snapshots(old, new)
    assert [(change.old.path, change.new.path) for change in result.path_migrations] == [
        ('/DOC', '/DOCS'), ('/DOC/EMPTY.;1', '/DOCS/EMPTY.;1')]
    assert result.path_migrations[1].evidence == 'directory-empty-file'


def test_unique_empty_files_with_same_name_are_not_content_updates():
    old = _snapshot([_dir('/'), _dir('/D'), _file('/D/E.;1', 'h', size=0)])
    new = _snapshot([_dir('/'), _dir('/D2'), _file('/D2/E.;1', 'h', size=0)])
    result = diffmod.diff_snapshots(old, new)
    assert result.content_updates == []


def test_date_string_renders_iso_offsets_in_quarter_hours():
    value = dates.DirectoryRecordDate.from_fields(126, 11, 15, 12, 0, 0, 32)
    assert diffmod._date_string(value) == '2026-11-15T12:00:00+08:00'
    negative = dates.DirectoryRecordDate.from_fields(126, 11, 15, 12, 0, 0, -12)
    assert diffmod._date_string(negative) == '2026-11-15T12:00:00-03:00'


def test_date_string_renders_udf_offsets_in_minutes():
    stamp = udfmod.UDFTimestamp()
    stamp.new(0)
    # Timestamp offsets are minutes; local time here is not UTC, so the
    # rendered string must end with a conventional +HH:MM offset.
    rendered = diffmod._date_string(stamp)
    assert rendered.endswith(('%+03d:%02d' % (stamp.tz // 60, abs(stamp.tz) % 60)))


def test_date_string_handles_none():
    assert diffmod._date_string(None) is None
