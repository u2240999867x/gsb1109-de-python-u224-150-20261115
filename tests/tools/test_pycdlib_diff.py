import os
import subprocess
import sys
from io import BytesIO

import pycdlib


pycdlib_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pycdlib_exe = os.path.join(pycdlib_root, 'tools', 'pycdlib-diff')


def run_process(cmdline):
    env = os.environ.copy()
    env['PYTHONPATH'] = pycdlib_root
    return subprocess.run(cmdline, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)


def make_iso(path, dirs=(), files=(), hard_links=(), bump_date_dir=None):
    iso = pycdlib.PyCdlib()
    iso.new()
    for dirname in dirs:
        iso.add_directory(dirname)
    for isopath, contents in files:
        iso.add_fp(BytesIO(contents), len(contents), isopath)
    for old_path, new_path in hard_links:
        iso.add_hard_link(iso_old_path=old_path, iso_new_path=new_path)
    if bump_date_dir is not None:
        rec = iso.get_record(iso_path=bump_date_dir)
        rec.date.years_since_1900 += 1
    iso.write(str(path))
    iso.close()
    return path


def run_diff(old_iso, new_iso, *extra_args):
    return run_process([sys.executable, pycdlib_exe, *extra_args,
                        str(old_iso), str(new_iso)])


def test_pycdlib_diff_identical_isos(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'), dirs=('/DIR1',),
                       files=(('/A.;1', b'a\n'), ('/DIR1/B.;1', b'b\n')))
    new_iso = make_iso(tmpdir.join('new.iso'), dirs=('/DIR1',),
                       files=(('/A.;1', b'a\n'), ('/DIR1/B.;1', b'b\n')))

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 0
    assert b'No differences' in result.stdout


def test_pycdlib_diff_content_update(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'), files=(('/A.;1', b'old\n'),))
    new_iso = make_iso(tmpdir.join('new.iso'), files=(('/A.;1', b'new\n'),))

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Content updates:' in result.stdout
    assert b'/A.;1' in result.stdout
    assert b'Deleted:' not in result.stdout
    assert b'Added:' not in result.stdout


def test_pycdlib_diff_path_migration(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'), dirs=('/DIR1',),
                       files=(('/DIR1/C.;1', b'c\n'),))
    new_iso = make_iso(tmpdir.join('new.iso'), dirs=('/DIR1',),
                       files=(('/C.;1', b'c\n'),))

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Path migrations:' in result.stdout
    assert b'/DIR1/C.;1 -> /C.;1' in result.stdout
    assert b'Deleted:' not in result.stdout
    assert b'Added:' not in result.stdout


def test_pycdlib_diff_directory_attribute_change(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'), dirs=('/DIR1',),
                       files=(('/DIR1/A.;1', b'a\n'),))
    new_iso = make_iso(tmpdir.join('new.iso'), dirs=('/DIR1',),
                       files=(('/DIR1/A.;1', b'a\n'),),
                       bump_date_dir='/DIR1')

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Directory attribute changes:' in result.stdout
    assert b'/DIR1' in result.stdout
    assert b'Content updates:' not in result.stdout


def test_pycdlib_diff_shared_block_split(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'),
                       files=(('/A.;1', b'shared\n'),),
                       hard_links=(('/A.;1', '/H.;1'),))
    new_iso = make_iso(tmpdir.join('new.iso'),
                       files=(('/A.;1', b'shared\n'), ('/H.;1', b'shared\n')))

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Shared block changes:' in result.stdout
    assert b'/A.;1 + /H.;1' in result.stdout
    assert b'/A.;1 | /H.;1' in result.stdout
    assert b'Content updates:' not in result.stdout


def test_pycdlib_diff_shared_block_merge(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'),
                       files=(('/A.;1', b'shared\n'), ('/H.;1', b'shared\n')))
    new_iso = make_iso(tmpdir.join('new.iso'),
                       files=(('/A.;1', b'shared\n'),),
                       hard_links=(('/A.;1', '/H.;1'),))

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Shared block changes:' in result.stdout
    assert b'/A.;1 | /H.;1' in result.stdout
    assert b'/A.;1 + /H.;1' in result.stdout


def test_pycdlib_diff_shared_block_unchanged(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'),
                       files=(('/A.;1', b'shared\n'),),
                       hard_links=(('/A.;1', '/H.;1'),))
    new_iso = make_iso(tmpdir.join('new.iso'),
                       files=(('/A.;1', b'shared\n'),),
                       hard_links=(('/A.;1', '/H.;1'),))

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 0
    assert b'No differences' in result.stdout


def test_pycdlib_diff_added_and_deleted(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'),
                       files=(('/GONE.;1', b'gone\n'),))
    new_iso = make_iso(tmpdir.join('new.iso'),
                       files=(('/FRESH.;1', b'fresh\n'),))

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Added:' in result.stdout
    assert b'/FRESH.;1' in result.stdout
    assert b'Deleted:' in result.stdout
    assert b'/GONE.;1' in result.stdout


def test_pycdlib_diff_partial_old_suppresses_deletions(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'),
                       files=(('/GONE.;1', b'gone\n'), ('/A.;1', b'a\n')))
    new_iso = make_iso(tmpdir.join('new.iso'), files=(('/A.;1', b'a\n'),))

    result = run_diff(old_iso, new_iso, '--old-is-partial')

    assert result.returncode == 0
    assert b'Deleted:' not in result.stdout
    assert b'/GONE.;1' not in result.stdout
    assert b'incomplete' in result.stderr


def test_pycdlib_diff_truncated_old_suppresses_deletions(tmpdir):
    old_iso = make_iso(tmpdir.join('old.iso'),
                       files=(('/A.;1', b'a\n'), ('/BIG.;1', b'B' * 8192),
                              ('/GONE.;1', b'gone\n')))
    # Truncate the tail of the old image so it is no longer complete.
    truncated = tmpdir.join('old-truncated.iso')
    with open(str(old_iso), 'rb') as infp:
        data = infp.read()
    with open(str(truncated), 'wb') as outfp:
        outfp.write(data[:-2048])
    new_iso = make_iso(tmpdir.join('new.iso'),
                       files=(('/A.;1', b'a\n'), ('/BIG.;1', b'B' * 8192)))

    result = run_diff(truncated, new_iso)

    assert b'Deleted:' not in result.stdout
    assert b'/GONE.;1' not in result.stdout
    assert b'incomplete' in result.stderr


def test_pycdlib_diff_rock_ridge(tmpdir):
    def make_rr(path, contents, rr_name):
        iso = pycdlib.PyCdlib()
        iso.new(rock_ridge='1.09')
        iso.add_fp(BytesIO(contents), len(contents), '/FILE.;1',
                   rr_name=rr_name)
        iso.write(str(path))
        iso.close()
        return path

    old_iso = make_rr(tmpdir.join('old.iso'), b'old\n', 'a-file')
    new_iso = make_rr(tmpdir.join('new.iso'), b'new\n', 'a-file')

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Content updates:' in result.stdout
    assert b'/a-file' in result.stdout


def test_pycdlib_diff_symlink_target_update(tmpdir):
    def make_rr(path, target):
        iso = pycdlib.PyCdlib()
        iso.new(rock_ridge='1.09')
        iso.add_fp(BytesIO(b'data\n'), 5, '/F.;1', rr_name='f')
        iso.add_symlink('/L.;1', rr_symlink_name='link', rr_path=target)
        iso.write(str(path))
        iso.close()
        return path

    old_iso = make_rr(tmpdir.join('old.iso'), 'f')
    new_iso = make_rr(tmpdir.join('new.iso'), 'other')

    result = run_diff(old_iso, new_iso)

    assert result.returncode == 1
    assert b'Content updates:' in result.stdout
    assert b'/link' in result.stdout
