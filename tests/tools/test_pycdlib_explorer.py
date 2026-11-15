import io
import os
import struct
import subprocess
import sys

import pycdlib
import pytest


pycdlib_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pycdlib_exe = os.path.join(pycdlib_root, 'tools', 'pycdlib-explorer')


def run_explorer(iso_path, commands):
    env = os.environ.copy()
    env['PYTHONPATH'] = pycdlib_root
    stdin = ('\n'.join(commands) + '\n').encode('utf-8')
    return subprocess.run([sys.executable, pycdlib_exe, str(iso_path)],
                          env=env, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)


def make_multi_section_iso(iso_path, pad_sectors=0):
    iso = pycdlib.PyCdlib()
    iso.new()

    bios_data = b'bios boot\n'.ljust(2048, b'\x00')
    iso.add_fp(io.BytesIO(bios_data), len(bios_data), '/ISOLINUX.BIN;1')
    iso.add_eltorito('/ISOLINUX.BIN;1', '/BOOT.CAT;1', boot_load_size=4)

    efi_data = b'efi boot\n'.ljust(2048, b'\x00')
    iso.add_fp(io.BytesIO(efi_data), len(efi_data), '/EFIBOOT.IMG;1')
    iso.add_eltorito('/EFIBOOT.IMG;1', efi=True)

    iso.write(str(iso_path))
    iso.close()

    if pad_sectors:
        with open(str(iso_path), 'ab') as fp:
            fp.write(b'\x00' * (pad_sectors * 2048))


def catalog_extent(iso_path):
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    extent = iso.eltorito_boot_catalog.extent_location()
    iso.close()
    return extent


def patch_catalog_entry(iso_path, record_index, field_offset, value, width):
    extent = catalog_extent(iso_path)
    with open(str(iso_path), 'r+b') as fp:
        fp.seek(extent * 2048 + record_index * 32 + field_offset)
        fp.write(struct.pack('<I' if width == 4 else '<H', value))


def test_list_eltorito_entries_reports_default_and_sections(tmpdir):
    iso_path = tmpdir.join('multi.iso')
    make_multi_section_iso(iso_path)

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    reports = iso.list_eltorito_entries()
    iso.close()

    assert len(reports) == 2

    default_entry, section_entry = reports

    assert default_entry.label == 'Initial/Default Entry'
    assert default_entry.platform == 'x86'
    assert default_entry.platform_id == 0
    assert default_entry.media == 'no emulation'
    assert default_entry.bootable is True
    assert default_entry.virtual_sector_size == 512
    assert default_entry.sector_count == 4
    assert default_entry.load_size == 2048
    assert default_entry.referenced_files == ['/ISOLINUX.BIN;1']
    assert default_entry.in_iso is True
    assert default_entry.ok is True
    assert default_entry.errors == []

    assert section_entry.label == 'Section 1 Entry 1'
    assert section_entry.platform == 'UEFI'
    assert section_entry.platform_id == 0xef
    assert section_entry.media == 'no emulation'
    assert section_entry.referenced_files == ['/EFIBOOT.IMG;1']
    assert section_entry.ok is True


def test_out_of_bounds_entry_is_marked_broken_but_others_survive(tmpdir):
    iso_path = tmpdir.join('corrupt.iso')
    # Pad the ISO so the Section Entry's start RBA remains inside the image
    # while an inflated sector count pushes its referenced range past the end.
    make_multi_section_iso(iso_path, pad_sectors=4)

    # The catalog lays out Validation Entry (record 0), Initial Entry
    # (record 1), Section Header (record 2), and Section Entry (record 3).
    # Inflate the Section Entry's sector count so the range it references runs
    # past the end of the ISO even though the start RBA is valid.
    patch_catalog_entry(iso_path, 3, 6, 40, 2)

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    reports = iso.list_eltorito_entries()
    iso.close()

    assert len(reports) == 2
    assert reports[0].ok is True
    assert reports[0].errors == []

    broken = reports[1]
    assert broken.ok is False
    assert broken.in_iso is False
    assert any('past the end of the ISO' in error for error in broken.errors)


def test_entry_exceeding_backing_file_is_a_coverage_error(tmpdir):
    iso_path = tmpdir.join('coverage.iso')
    # The padding keeps the inflated byte range inside the ISO, so the only
    # failure left is that the backing file does not cover the whole range.
    make_multi_section_iso(iso_path, pad_sectors=8)

    # Bump the Initial Entry's sector count from 4 to 8 (4096 bytes) while the
    # backing boot file is only 2048 bytes.
    patch_catalog_entry(iso_path, 1, 6, 8, 2)

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    reports = iso.list_eltorito_entries()
    iso.close()

    default_entry = reports[0]
    assert default_entry.in_iso is True
    assert default_entry.ok is False
    assert len(default_entry.errors) == 1
    assert 'only covers 2048 bytes' in default_entry.errors[0]
    assert reports[1].ok is True


def test_explorer_boot_entries_lists_every_entry(tmpdir):
    iso_path = tmpdir.join('multi.iso')
    make_multi_section_iso(iso_path)

    result = run_explorer(iso_path, ['boot_entries', 'exit'])

    assert result.returncode == 0
    output = result.stdout.decode('utf-8')
    assert 'Initial/Default Entry' in output
    assert 'Section 1 Entry 1' in output
    assert 'Platform: x86' in output
    assert 'Platform: UEFI' in output
    assert 'Media: no emulation' in output
    assert '/ISOLINUX.BIN;1' in output
    assert '/EFIBOOT.IMG;1' in output
    assert '512 bytes' in output
    assert 'Status: OK' in output


def test_explorer_boot_entries_keeps_good_entries_when_one_is_broken(tmpdir):
    iso_path = tmpdir.join('corrupt.iso')
    make_multi_section_iso(iso_path, pad_sectors=4)
    patch_catalog_entry(iso_path, 3, 6, 40, 2)

    result = run_explorer(iso_path, ['boot_entries', 'exit'])

    assert result.returncode == 0
    output = result.stdout.decode('utf-8')
    assert 'Initial/Default Entry' in output
    assert 'Section 1 Entry 1' in output
    assert 'Status: OK' in output
    assert 'Status: BROKEN' in output


def test_explorer_boot_entries_without_eltorito(tmpdir):
    iso_path = tmpdir.join('plain.iso')
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.write(str(iso_path))
    iso.close()

    result = run_explorer(iso_path, ['boot_entries', 'exit'])

    assert result.returncode == 0
    assert 'does not have an El Torito Boot Record' in result.stdout.decode('utf-8')


def test_list_eltorito_entries_without_eltorito_raises(tmpdir):
    iso_path = tmpdir.join('plain.iso')
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.write(str(iso_path))

    with pytest.raises(pycdlib.pycdlibexception.PyCdlibInvalidInput):
        iso.list_eltorito_entries()

    iso.close()


def test_list_eltorito_entries_three_entries_order_and_platforms(tmpdir):
    iso_path = tmpdir.join('three.iso')
    iso = pycdlib.PyCdlib()
    iso.new()

    for name in ('/BOOT1.;1', '/BOOT2.;1', '/BOOT3.;1'):
        data = b'boot\n'.ljust(2048, b'\x00')
        iso.add_fp(io.BytesIO(data), len(data), name)

    iso.add_eltorito('/BOOT1.;1', '/BOOT.CAT;1', boot_load_size=4)
    iso.add_eltorito('/BOOT2.;1', efi=True)
    iso.add_eltorito('/BOOT3.;1', efi=True)
    iso.write(str(iso_path))
    iso.close()

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    reports = iso.list_eltorito_entries()
    iso.close()

    assert [report.label for report in reports] == [
        'Initial/Default Entry', 'Section 1 Entry 1', 'Section 2 Entry 1']
    assert [report.platform_id for report in reports] == [0, 0xef, 0xef]
    assert [report.platform for report in reports] == ['x86', 'UEFI', 'UEFI']
    assert [report.referenced_files for report in reports] == [
        ['/BOOT1.;1'], ['/BOOT2.;1'], ['/BOOT3.;1']]
    assert all(report.ok for report in reports)


def test_list_eltorito_entries_hidden_boot_file(tmpdir):
    iso_path = tmpdir.join('hidden.iso')
    make_multi_section_iso(iso_path)

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    iso.rm_hard_link(iso_path='/EFIBOOT.IMG;1')
    iso.write(str(tmpdir.join('hidden-rewritten.iso')))
    iso.close()

    iso = pycdlib.PyCdlib()
    iso.open(str(tmpdir.join('hidden-rewritten.iso')))
    reports = iso.list_eltorito_entries()
    iso.close()

    section_entry = reports[1]
    assert section_entry.referenced_files == []
    assert section_entry.in_iso is True
    assert section_entry.ok is True


def test_list_eltorito_entries_floppy_media_type(tmpdir):
    iso_path = tmpdir.join('floppy.iso')
    iso = pycdlib.PyCdlib()
    iso.new()

    data = b'floppy\n'.ljust(2048, b'\x00')
    iso.add_fp(io.BytesIO(data), len(data), '/FLOPPY.IMG;1')
    iso.add_eltorito('/FLOPPY.IMG;1', '/BOOT.CAT;1',
                     media_name='floppy', boot_load_size=2880)
    iso.write(str(iso_path))
    iso.close()

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    report = iso.list_eltorito_entries()[0]
    iso.close()

    assert report.media_type == pycdlib.eltorito.EltoritoEntry.MEDIA_144FLOPPY
    assert report.media == '1.44MB floppy emulation'
    assert report.virtual_sector_size == 512
    assert report.ok is True


def test_list_eltorito_entries_non_bootable_entry(tmpdir):
    iso_path = tmpdir.join('nonboot.iso')
    iso = pycdlib.PyCdlib()
    iso.new()

    data = b'boot\n'.ljust(2048, b'\x00')
    iso.add_fp(io.BytesIO(data), len(data), '/BOOT.;1')
    iso.add_eltorito('/BOOT.;1', '/BOOT.CAT;1', bootable=False)
    iso.write(str(iso_path))
    iso.close()

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    report = iso.list_eltorito_entries()[0]
    iso.close()

    assert report.bootable is False
    assert report.ok is True
