import io
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pycdlib
from pycdlib import diff as diffmod

PYCDLIB_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYCDLIB_DIFF = os.path.join(PYCDLIB_ROOT, 'tools', 'pycdlib-diff')


def _add_tree(iso, directories, files):
    for (path, rr_name, joliet_path, mode) in directories:
        iso.add_directory(path, rr_name=rr_name, joliet_path=joliet_path,
                          file_mode=mode)
    for (path, rr_name, joliet_path, contents, links) in files:
        iso.add_fp(io.BytesIO(contents), len(contents), path,
                   rr_name=rr_name, joliet_path=joliet_path)
        for (link_path, link_rr_name) in links:
            iso.add_hard_link(iso_old_path=path, iso_new_path=link_path,
                              rr_name=link_rr_name)


def _write_old_iso(path):
    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09', joliet=3)
    directories = [('/BIN', 'bin', '/bin', 0o040755),
                   ('/DOC', 'doc', '/doc', 0o040755),
                   ('/LIB', 'lib', '/lib', 0o040755),
                   ('/APPDIR', 'app', '/app', 0o040755)]
    files = [('/BIN/TOOL.;1', 'tool', '/bin/tool', b'tool v1\n', []),
             ('/DOC/README.;1', 'readme.txt', '/doc/readme.txt', b'readme v1\n', []),
             ('/LIB/X.;1', 'libx.so', '/lib/libx.so', b'lib data\n', [('/LIB/Y.;1', 'liby.so')]),
             ('/GONE.;1', 'gone', '/gone', b'gone\n', []),
             ('/APPDIR/CFG.;1', 'cfg', '/app/cfg', b'cfg\n', [])]
    _add_tree(iso, directories, files)
    iso.write(path)
    iso.close()


def _write_new_iso(path):
    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09', joliet=3)
    directories = [('/BIN', 'bin', '/bin', 0o040755),
                   ('/DOCS', 'docs', '/docs', 0o040755),
                   ('/LIB', 'lib', '/lib', 0o040755),
                   ('/APPDIR', 'app', '/app', 0o040750)]
    files = [('/BIN/TOOL.;1', 'tool', '/bin/tool', b'tool v2!\n', []),
             ('/DOCS/README.;1', 'readme.txt', '/docs/readme.txt', b'readme v1\n', []),
             ('/LIB/X.;1', 'libx.so', '/lib/libx.so', b'lib data\n', []),
             ('/LIB/Y.;1', 'liby.so', '/lib/liby.so', b'lib data\n', []),
             ('/APPDIR/CFG.;1', 'cfg', '/app/cfg', b'cfg\n', []),
             ('/NEW.;1', 'newfile', '/newfile', b'brand new\n', [])]
    _add_tree(iso, directories, files)
    iso.write(path)
    iso.close()


@pytest.fixture(scope='module')
def images(tmp_path_factory):
    """Build the old/new image pair once for the whole module."""
    directory = tmp_path_factory.mktemp('diff-images')
    old_path = str(directory.joinpath('old.iso'))
    new_path = str(directory.joinpath('new.iso'))
    _write_old_iso(old_path)
    _write_new_iso(new_path)

    truncated_path = str(directory.joinpath('truncated-old.iso'))
    with open(old_path, 'rb') as infp:
        data = infp.read()
    with open(truncated_path, 'wb') as outfp:
        # 20 sectors is enough for the volume descriptors but cuts the path
        # table and file data, so the image cannot be fully parsed.
        outfp.write(data[:2048 * 20])

    return {'old': old_path, 'new': new_path, 'truncated': truncated_path}


@pytest.fixture(scope='module')
def sharing_images(tmp_path_factory):
    """Build the hard-link split/merge pair once for the whole module."""
    directory = tmp_path_factory.mktemp('diff-sharing')
    no_share_path = str(directory.joinpath('old-no-share.iso'))
    share_path = str(directory.joinpath('new-share.iso'))

    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09')
    iso.add_fp(io.BytesIO(b'payload\n'), 8, '/X.;1', rr_name='x')
    iso.add_fp(io.BytesIO(b'payload\n'), 8, '/Y.;1', rr_name='y')
    iso.write(no_share_path)
    iso.close()

    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge='1.09')
    iso.add_fp(io.BytesIO(b'payload\n'), 8, '/X.;1', rr_name='x')
    iso.add_hard_link(iso_old_path='/X.;1', iso_new_path='/Y.;1', rr_name='y')
    iso.write(share_path)
    iso.close()

    return {'no_share': no_share_path, 'share': share_path}


def test_diff_content_update_and_migration(images):
    result = diffmod.diff_paths(images['old'], images['new'])

    updates = {(change.old.path, change.new.path): change
               for change in result.content_updates}
    assert ('/bin/tool', '/bin/tool') in updates
    assert updates[('/bin/tool', '/bin/tool')].evidence == 'path'
    assert updates[('/bin/tool', '/bin/tool')].old.size == 8
    assert updates[('/bin/tool', '/bin/tool')].new.size == 9

    migrations = {change.old.path: change.new.path
                  for change in result.path_migrations}
    assert migrations['/doc'] == '/docs'
    assert migrations['/doc/readme.txt'] == '/docs/readme.txt'


def test_diff_directory_attributes_only(images):
    result = diffmod.diff_paths(images['old'], images['new'])
    changes = {change.entry.new.path: change.changes
               for change in result.attribute_only_changes}
    assert changes['/app'] == {'rr_mode': (0o040755, 0o040750)}
    assert '/bin' not in changes
    assert '/lib' not in changes


def test_diff_added_and_removed(images):
    result = diffmod.diff_paths(images['old'], images['new'])
    assert [entry.path for entry in result.added] == ['/newfile']
    assert [entry.path for entry in result.removed] == ['/gone']
    assert result.deletion_verified
    assert not result.unverified


def test_diff_shared_block_separated(images):
    result = diffmod.diff_paths(images['old'], images['new'])
    assert len(result.shared_block_changes) == 1
    change = result.shared_block_changes[0]
    assert change.change == 'separated'
    assert change.old_paths == ('/lib/libx.so', '/lib/liby.so')
    assert change.new_paths == ('/lib/libx.so', '/lib/liby.so')


def test_diff_shared_block_joined(sharing_images):
    result = diffmod.diff_paths(sharing_images['no_share'], sharing_images['share'])
    assert [change.change for change in result.shared_block_changes] == ['joined']
    change = result.shared_block_changes[0]
    assert change.old_paths == ('/x', '/y')
    assert change.new_paths == ('/x', '/y')


def test_layout_only_remaster_reports_nothing(images, tmpdir):
    # Rewriting the old ISO through pycdlib without touching the tree must
    # not produce content, path, or attribute changes, even though every
    # extent moves and every timestamp is regenerated.
    old = pycdlib.PyCdlib()
    old.open(images['old'])
    rewritten = str(tmpdir.join('remastered.iso'))
    old.write(rewritten)
    old.close()

    result = diffmod.diff_paths(images['old'], rewritten)
    assert result.content_updates == []
    assert result.path_migrations == []
    assert result.attribute_only_changes == []
    assert result.added == []
    assert result.removed == []


def test_missing_old_image_never_infers_deletion(images):
    result = diffmod.diff_paths(None, images['new'])
    assert result.removed == []
    assert result.added == []
    assert not result.deletion_verified
    assert result.notes


def test_unparseable_old_image_never_infers_deletion(images):
    result = diffmod.diff_paths(images['truncated'], images['new'])
    assert result.removed == []
    assert not result.deletion_verified
    assert any('unavailable' in note for note in result.notes)


def test_explicit_incomplete_old_image_never_infers_deletion(images):
    result = diffmod.diff_paths(images['old'], images['new'], old_complete=False)
    assert result.removed == []
    assert not result.deletion_verified
    assert ('old', '/gone') in [(side, entry.path) for (side, entry) in result.unverified]


def test_iso_namespace_diff_sees_short_names(images):
    result = diffmod.diff_paths(images['old'], images['new'], namespace='iso')
    assert result.new.namespace == 'iso'
    migrated = {change.old.path: change.new.path for change in result.path_migrations}
    assert migrated['/DOC/README.;1'] == '/DOCS/README.;1'


def test_joliet_namespace_diff(images):
    result = diffmod.diff_paths(images['old'], images['new'], namespace='joliet')
    assert result.new.namespace == 'joliet'
    assert any(change.old.path == '/bin/tool' for change in result.content_updates)


def test_namespace_unavailable_is_rejected(images):
    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        diffmod.diff_paths(images['old'], images['new'], namespace='udf')


def test_diff_isos_on_open_objects(images):
    old = pycdlib.PyCdlib()
    old.open(images['old'])
    new = pycdlib.PyCdlib()
    new.open(images['new'])
    try:
        result = diffmod.diff_isos(old, new)
    finally:
        old.close()
        new.close()
    assert len(result.content_updates) == 1


def test_diff_json_serializable(images):
    result = diffmod.diff_paths(images['old'], images['new'])
    decoded = json.loads(json.dumps(result.to_dict()))
    assert decoded['deletion_verified'] is True
    assert decoded['namespace'] == 'rr'
    assert decoded['content_updates'][0]['path_changed'] is False
    assert decoded['shared_block_changes'][0]['change'] == 'separated'


def test_udf_snapshot_hashes_content_and_reads_symlink_targets(tmpdir):
    iso_path = str(tmpdir.join('udf.iso'))
    iso = pycdlib.PyCdlib()
    iso.new(udf='2.60', rock_ridge='1.09')
    iso.add_fp(io.BytesIO(b'payload\n'), 8, '/A.;1', rr_name='a', udf_path='/a')
    iso.add_symlink('/L.;1', rr_symlink_name='l', rr_path='/a',
                    udf_symlink_path='/l', udf_target='/a')
    iso.write(iso_path)
    iso.close()

    opened = pycdlib.PyCdlib()
    opened.open(iso_path)
    try:
        snapshot = diffmod.build_snapshot(opened, 'udf')
    finally:
        opened.close()

    assert snapshot.complete
    assert snapshot.entries['/a'].digest is not None
    assert snapshot.entries['/a'].size == 8
    assert snapshot.entries['/l'].kind == 'symlink'
    assert snapshot.entries['/l'].digest is not None


def run_diff(args):
    env = os.environ.copy()
    env['PYTHONPATH'] = PYCDLIB_ROOT
    return subprocess.run([sys.executable, PYCDLIB_DIFF] + args,
                          env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False, text=True)


def test_cli_text_report_and_exit_codes(images):
    result = run_diff(['--old', images['old'], '--new', images['new'],
                       '--fail-on-diff'])
    assert result.returncode == 1
    assert 'Content updates (1):' in result.stdout
    assert 'Path migrations (2):' in result.stdout
    assert 'Attribute-only changes (1):' in result.stdout
    assert 'Shared-block changes (1):' in result.stdout
    assert result.stderr == ''


def test_cli_json_report(images):
    result = run_diff(['--old', images['old'], '--new', images['new'],
                       '--format', 'json'])
    assert result.returncode == 0
    decoded = json.loads(result.stdout)
    assert decoded['path_migrations'][0]['old_path'] == '/doc'


def test_cli_without_old_image(images):
    result = run_diff(['--new', images['new']])
    assert result.returncode == 0
    assert 'deletions cannot be inferred' in result.stdout


def test_cli_no_differences_exits_zero(images, tmpdir):
    copy_path = str(tmpdir.join('copy.iso'))
    with open(images['new'], 'rb') as infp:
        with open(copy_path, 'wb') as outfp:
            outfp.write(infp.read())
    result = run_diff(['--old', images['new'], '--new', copy_path, '--fail-on-diff'])
    assert result.returncode == 0


def test_cli_output_file(images, tmpdir):
    output_path = str(tmpdir.join('report.json'))
    result = run_diff(['--old', images['old'], '--new', images['new'],
                       '--format', 'json', '--output', output_path])
    assert result.returncode == 0
    assert result.stdout == ''
    with open(output_path, encoding='utf-8') as infp:
        assert json.load(infp)['deletion_verified'] is True


def test_cli_bad_iso_exits_two(images, tmpdir):
    bogus = str(tmpdir.join('bogus.iso'))
    with open(bogus, 'wb') as outfp:
        outfp.write(b'not an iso')
    result = run_diff(['--old', images['old'], '--new', bogus])
    assert result.returncode == 2
    assert 'pycdlib-diff' in result.stderr
