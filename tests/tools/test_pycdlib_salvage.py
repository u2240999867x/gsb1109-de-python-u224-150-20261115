import io
import os
import subprocess
import sys

import pycdlib


pycdlib_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pycdlib_exe = os.path.join(pycdlib_root, 'tools', 'pycdlib-salvage')


def run_process(cmdline, stdin_text=None):
    env = os.environ.copy()
    env['PYTHONPATH'] = pycdlib_root
    return subprocess.run(cmdline, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False,
                          input=stdin_text, text=True)


def make_damaged_iso(tmpdir):
    iso_path = tmpdir.join('damaged.iso')
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(b'A' * 2048 + b'B' * 2048 + b'C' * 100), 4196,
               '/BIG.;1')
    iso.add_fp(io.BytesIO(b'x'), 1, '/ONE.;1')
    iso.add_fp(io.BytesIO(b''), 0, '/EMPTY.;1')
    iso.add_directory('/DIR')
    iso.add_fp(io.BytesIO(b'y' * 2048), 2048, '/DIR/INNER.;1')
    iso.write(str(iso_path))
    iso.close()

    probe = pycdlib.PyCdlib()
    probe.open(str(iso_path))
    big_extent = probe.get_record(iso_path='/BIG.;1').extent_location()
    dir_extent = probe.get_record(iso_path='/DIR').extent_location()
    probe.close()
    return iso_path, big_extent, dir_extent


def test_salvage_report_classifies_entries(tmpdir):
    iso_path, big_extent, dir_extent = make_damaged_iso(tmpdir)
    bad_blocks = tmpdir.join('bad.txt')
    bad_blocks.write('# bad block table\n%d\n%d\n' %
                     (big_extent + 1, dir_extent))

    result = run_process([sys.executable, pycdlib_exe, '-bad-blocks',
                          str(bad_blocks), str(iso_path)])

    assert result.returncode == 0
    assert 'partial   /BIG.;1' in result.stdout
    assert 'untrusted /DIR' in result.stdout
    assert 'complete  /EMPTY.;1' in result.stdout
    assert '[under untrusted directory]' in result.stdout
    assert '1 partial, 1 untrusted' in result.stdout


def test_salvage_reads_bad_blocks_from_stdin(tmpdir):
    iso_path, big_extent, dir_extent = make_damaged_iso(tmpdir)
    result = run_process([sys.executable, pycdlib_exe, '-bad-blocks', '-',
                          str(iso_path)],
                         stdin_text='%d\n%d\n' % (big_extent + 1, dir_extent))
    assert result.returncode == 0
    assert 'untrusted /DIR' in result.stdout


def test_salvage_extract_ingests_only_safe_entries(tmpdir):
    iso_path, big_extent, dir_extent = make_damaged_iso(tmpdir)
    bad_blocks = tmpdir.join('bad.txt')
    bad_blocks.write('%d\n%d\n' % (big_extent + 1, dir_extent))
    extract_to = tmpdir.mkdir('extract')

    result = run_process([sys.executable, pycdlib_exe, '-bad-blocks',
                          str(bad_blocks), '-extract-to', str(extract_to),
                          '-include-partial', str(iso_path)])

    assert result.returncode == 0
    names = os.listdir(str(extract_to))
    assert 'BIG.;1.partial' in names
    assert 'ONE.;1' in names
    assert 'EMPTY.;1' in names
    assert 'DIR' not in names

    partial = extract_to.join('BIG.;1.partial').read_binary()
    assert partial[:2048] == b'A' * 2048
    assert partial[2048:4096] == b'\x00' * 2048
    assert partial[4096:] == b'C' * 100


def test_salvage_without_partial_flag_skips_damaged_file(tmpdir):
    iso_path, big_extent, dir_extent = make_damaged_iso(tmpdir)
    bad_blocks = tmpdir.join('bad.txt')
    bad_blocks.write('%d\n' % (big_extent + 1,))
    extract_to = tmpdir.mkdir('extract')

    result = run_process([sys.executable, pycdlib_exe, '-bad-blocks',
                          str(bad_blocks), '-extract-to', str(extract_to),
                          str(iso_path)])

    assert result.returncode == 0
    names = os.listdir(str(extract_to))
    assert 'BIG.;1' not in names
    assert 'BIG.;1.partial' not in names
    assert 'ONE.;1' in names


def test_salvage_rejects_invalid_bad_block_file(tmpdir):
    iso_path, _, _ = make_damaged_iso(tmpdir)
    bad_blocks = tmpdir.join('bad.txt')
    bad_blocks.write('not-a-block\n')

    result = run_process([sys.executable, pycdlib_exe, '-bad-blocks',
                          str(bad_blocks), str(iso_path)])

    assert result.returncode == 2
    assert 'Invalid bad block specification' in result.stderr


def test_salvage_rejects_missing_iso(tmpdir):
    bad_blocks = tmpdir.join('bad.txt')
    bad_blocks.write('1\n')
    result = run_process([sys.executable, pycdlib_exe, '-bad-blocks',
                          str(bad_blocks), str(tmpdir.join('missing.iso'))])
    assert result.returncode == 3
