import io
import os
import subprocess
import sys

import pycdlib


pycdlib_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pycdlib_exe = os.path.join(pycdlib_root, 'tools', 'pycdlib-catalog')


def run_process(cmdline):
    env = os.environ.copy()
    env['PYTHONPATH'] = pycdlib_root
    return subprocess.run(cmdline, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)


def make_three_namespace_iso(iso_path):
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1, rock_ridge='1.09', joliet=3)
    iso.add_directory('/DIR1', rr_name='dir1', joliet_path='/dir1')
    iso.add_fp(io.BytesIO(b'foo data\n'), 9, '/FOO.;1', rr_name='foo',
               joliet_path='/foo')
    iso.add_fp(io.BytesIO(b'bar data here\n'), 14, '/DIR1/BAR.;1',
               rr_name='bar', joliet_path='/dir1/bar')
    iso.add_hard_link(iso_new_path='/FOOLINK.;1', iso_old_path='/FOO.;1',
                      rr_name='foolink')
    iso.add_hard_link(joliet_new_path='/foolink', iso_old_path='/FOO.;1')
    iso.write(str(iso_path))
    iso.close()


def test_pycdlib_catalog_prints_all_three_catalogs(tmpdir):
    iso_path = tmpdir.join('three.iso')
    make_three_namespace_iso(iso_path)

    result = run_process([sys.executable, pycdlib_exe, str(iso_path)])

    assert result.returncode == 0
    output = result.stdout.decode('utf-8')
    assert '# file entities' in output
    assert '# path references' in output
    assert '# shared extent conflicts' in output
    assert 'iso9660\t/FOO.;1\tFOO.;1' in output
    assert 'rockridge\t/foo\tfoo' in output
    assert 'joliet\t/foo\tfoo' in output


def test_pycdlib_catalog_collapses_hard_links_in_entity_catalog(tmpdir):
    iso_path = tmpdir.join('hardlink.iso')
    make_three_namespace_iso(iso_path)

    result = run_process([sys.executable, pycdlib_exe, str(iso_path)])

    assert result.returncode == 0
    entity_lines = [
        line for line in result.stdout.decode('utf-8').splitlines()
        if line.startswith('F') and '\tfile\t' in line
    ]
    foo_rows = [line for line in entity_lines if '\t9\t' in line]
    assert len(foo_rows) == 1
    assert foo_rows[0].endswith('\t6\tiso9660,joliet,rockridge')


def test_pycdlib_catalog_writes_output_directory(tmpdir):
    iso_path = tmpdir.join('three.iso')
    make_three_namespace_iso(iso_path)
    output_dir = tmpdir.mkdir('catalogs')

    result = run_process([sys.executable, pycdlib_exe, '-output-dir',
                          str(output_dir), str(iso_path)])

    assert result.returncode == 0
    assert result.stdout == b''
    names = sorted(os.listdir(str(output_dir)))
    assert names == ['conflicts.tsv', 'entities.tsv', 'references.tsv']

    references = output_dir.join('references.tsv').read_text('utf-8')
    assert 'joliet\t/dir1/bar\tbar' in references
    assert 'rockridge\t/foolink\tfoolink' in references


def test_pycdlib_catalog_fails_on_missing_iso(tmpdir):
    result = run_process([sys.executable, pycdlib_exe,
                          str(tmpdir.join('missing.iso'))])

    assert result.returncode != 0
    assert b'No such file' in result.stderr
