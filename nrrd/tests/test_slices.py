import os
import unittest
from tempfile import TemporaryDirectory

import numpy as np

import nrrd


def _write_list_header(path, sizes, filenames, dtype_name='uint8', encoding='raw'):
    with open(path, 'w') as fh:
        fh.write('NRRD0005\n')
        fh.write(f'type: {dtype_name}\n')
        fh.write(f'dimension: {len(sizes)}\n')
        fh.write(f'sizes: {" ".join(str(size) for size in sizes)}\n')
        fh.write(f'encoding: {encoding}\n')
        fh.write('data file: LIST\n')
        for filename in filenames:
            fh.write(f'{filename}\n')
        fh.write('\n')


def _write_template_header(path, sizes, template, dtype_name='uint8'):
    with open(path, 'w') as fh:
        fh.write('NRRD0005\n')
        fh.write(f'type: {dtype_name}\n')
        fh.write(f'dimension: {len(sizes)}\n')
        fh.write(f'sizes: {" ".join(str(size) for size in sizes)}\n')
        fh.write('encoding: raw\n')
        fh.write(f'data file: {template}\n')
        fh.write('\n')


class TestMultiSliceReading(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = self.temp_dir.name

        # sizes (2, 3, 4): each slowest-axis slice holds 6 uint8 values
        self.sizes = np.array([2, 3, 4])
        self.volume = np.arange(1, 25, dtype=np.uint8).reshape((4, 3, 2))
        self.slice_span = 6
        self.slice_byte_count = 6

        for index in range(4):
            self.volume[index].tofile(os.path.join(self.root, f'list_{index}.raw'))
            self.volume[index].tofile(os.path.join(self.root, f'slice-{index:02d}.raw'))

        self.list_header = os.path.join(self.root, 'vol_list.nhdr')
        _write_list_header(self.list_header, self.sizes, [f'list_{index}.raw' for index in range(4)])

        self.template_header = os.path.join(self.root, 'vol_template.nhdr')
        _write_template_header(self.template_header, self.sizes, 'slice-%02d.raw')

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_read_header_collects_list_filenames(self):
        header = nrrd.read_header(self.list_header)
        self.assertEqual(header['data file'], 'LIST')
        self.assertEqual(header['data file list'], [f'list_{index}.raw' for index in range(4)])

    def test_sequence_template_detection(self):
        self.assertTrue(nrrd.is_sequence_template('slice-%03d.raw'))
        self.assertTrue(nrrd.is_sequence_template('slice-%d.raw'))
        self.assertFalse(nrrd.is_sequence_template('plain.raw'))
        self.assertFalse(nrrd.is_sequence_template('a-%d-b-%d.raw'))
        self.assertFalse(nrrd.is_sequence_template('100%-slice.raw'))
        self.assertFalse(nrrd.is_sequence_template('LIST'))

    def test_read_list_slices_ordered(self):
        data, header, manifest = nrrd.read_sliced(self.list_header)

        np.testing.assert_array_equal(data, self.volume.T)
        self.assertEqual(manifest.axis_index, 2)
        self.assertEqual(manifest.axis_size, 4)
        self.assertEqual(manifest.slice_span, self.slice_span)
        self.assertEqual(manifest.slice_byte_count, self.slice_byte_count)
        self.assertEqual(len(manifest.sources), 4)
        self.assertEqual([source.index for source in manifest.sources], [0, 1, 2, 3])
        self.assertEqual([source.byte_offset for source in manifest.sources], [0, 6, 12, 18])
        self.assertTrue(all(source.exists for source in manifest.sources))
        self.assertTrue(all(source.byte_count == 6 for source in manifest.sources))
        self.assertTrue(all(os.path.isabs(source.filename) for source in manifest.sources))
        self.assertEqual(manifest.missing_indices, [])
        self.assertEqual(manifest.gap_ranges, [])
        self.assertEqual(header['data file list'], [f'list_{index}.raw' for index in range(4)])

    def test_read_template_slices_ordered(self):
        data, _, manifest = nrrd.read_sliced(self.template_header)

        np.testing.assert_array_equal(data, self.volume.T)
        self.assertEqual(
            [os.path.basename(source.filename) for source in manifest.sources],
            [f'slice-{index:02d}.raw' for index in range(4)])

    def test_nrrd_read_accepts_sliced_header(self):
        data, header = nrrd.read(self.list_header)
        np.testing.assert_array_equal(data, self.volume.T)
        self.assertEqual(header['data file'], 'LIST')

    def test_read_data_c_order(self):
        header = nrrd.read_header(self.list_header)
        data = nrrd.read_data(header, filename=self.list_header, index_order='C')
        self.assertEqual(data.shape, (4, 3, 2))
        np.testing.assert_array_equal(data, self.volume)

    def test_missing_intermediate_slices_keep_gap_without_shifting(self):
        os.remove(os.path.join(self.root, 'list_1.raw'))
        os.remove(os.path.join(self.root, 'list_2.raw'))

        data, _, manifest = nrrd.read_sliced(self.list_header)

        expected = self.volume.T.copy()
        expected[:, :, 1] = 0
        expected[:, :, 2] = 0
        np.testing.assert_array_equal(data, expected)

        self.assertEqual(manifest.missing_indices, [1, 2])
        self.assertEqual(manifest.gap_ranges, [(1, 2)])
        self.assertFalse(manifest.sources[1].exists)
        self.assertEqual(manifest.sources[1].byte_count, 0)
        # The later slice keeps its original offset instead of shifting forward
        self.assertTrue(manifest.sources[3].exists)
        self.assertEqual(manifest.sources[3].byte_offset, 18)

    def test_slice_byte_count_mismatch_raises(self):
        short_dir = os.path.join(self.root, 'short')
        os.makedirs(short_dir)
        np.arange(5, dtype=np.uint8).tofile(os.path.join(short_dir, 'short_0.raw'))
        self.volume[1].tofile(os.path.join(short_dir, 'short_1.raw'))
        bad_header = os.path.join(short_dir, 'vol.nhdr')
        _write_list_header(bad_header, [2, 3, 2], ['short_0.raw', 'short_1.raw'])

        with self.assertRaisesRegex(nrrd.NRRDError, 'expected 6 bytes'):
            nrrd.read_sliced(bad_header)

    def test_slice_count_mismatch_raises(self):
        bad_header = os.path.join(self.root, 'vol_count.nhdr')
        _write_list_header(bad_header, self.sizes, ['list_0.raw', 'list_3.raw'])

        with self.assertRaisesRegex(nrrd.NRRDError, 'does not match the slice axis size'):
            nrrd.read_sliced(bad_header)

    def test_relative_path_escaping_authorized_root_rejected(self):
        outside = os.path.join(os.path.dirname(self.root), 'outside.raw')
        outside_values = np.arange(6, dtype=np.uint8).reshape((3, 2))
        outside_values.tofile(outside)
        try:
            escape_header = os.path.join(self.root, 'vol_escape.nhdr')
            _write_list_header(escape_header, [2, 3, 1], ['../outside.raw'])

            with self.assertRaisesRegex(nrrd.NRRDError, 'escapes the authorized root'):
                nrrd.read_sliced(escape_header)

            abs_header = os.path.join(self.root, 'vol_abs.nhdr')
            _write_list_header(abs_header, [2, 3, 1], [outside])

            with self.assertRaisesRegex(nrrd.NRRDError, 'escapes the authorized root'):
                nrrd.read_sliced(abs_header, data_root=self.root)

            # Without an explicit sandbox, absolute paths are accepted even when outside the header directory
            data, _, manifest = nrrd.read_sliced(abs_header)
            np.testing.assert_array_equal(data.squeeze(), outside_values.T)
            self.assertEqual(manifest.sources[0].filename, os.path.realpath(outside))
        finally:
            os.remove(outside)

    def test_relative_path_without_authorized_root_rejected(self):
        header = {'type': 'uint8', 'dimension': 3, 'sizes': np.array([2, 3, 1]), 'encoding': 'raw',
                  'data file': 'LIST', 'data file list': ['list_0.raw']}

        with self.assertRaisesRegex(nrrd.NRRDError, 'authorized root'):
            nrrd.read_sliced_data(header)

    def test_explicit_data_root_resolves_relative_slices(self):
        header_path = os.path.join(os.path.dirname(self.root), 'vol_root.nhdr')
        _write_list_header(header_path, [2, 3, 1], ['list_0.raw'])
        try:
            data, _, manifest = nrrd.read_sliced(header_path, data_root=self.root)
            np.testing.assert_array_equal(data.squeeze(), self.volume[0].T)
            self.assertTrue(all(self.root in source.filename for source in manifest.sources))
        finally:
            os.remove(header_path)

    def test_ascii_slices(self):
        ascii_dir = os.path.join(self.root, 'ascii')
        os.makedirs(ascii_dir)
        for index in range(2):
            values = np.arange(index * 6 + 1, index * 6 + 7, dtype=np.uint8)
            with open(os.path.join(ascii_dir, f'a_{index}.txt'), 'w') as fh:
                fh.write(' '.join(str(value) for value in values))

        ascii_header = os.path.join(ascii_dir, 'vol.nhdr')
        _write_list_header(ascii_header, [2, 3, 2], ['a_0.txt', 'a_1.txt'], encoding='ASCII')

        data, _, manifest = nrrd.read_sliced(ascii_header)
        expected = np.arange(1, 13, dtype=np.uint8).reshape((2, 3, 2)).T
        np.testing.assert_array_equal(data, expected)
        self.assertTrue(all(source.exists for source in manifest.sources))

    def test_compressed_encoding_rejected(self):
        header = {'type': 'uint8', 'dimension': 3, 'sizes': np.array([2, 3, 1]), 'encoding': 'gzip',
                  'data file': 'slice-%d.raw'}

        with self.assertRaisesRegex(nrrd.NRRDError, 'only supported with raw or ASCII'):
            nrrd.read_sliced_data(header, data_root=self.root)

    def test_read_sliced_rejects_single_file_header(self):
        single_path = os.path.join(self.root, 'single.raw')
        self.volume.tofile(single_path)
        single_header = os.path.join(self.root, 'single.nhdr')
        with open(single_header, 'w') as fh:
            fh.write('NRRD0005\ntype: uint8\ndimension: 3\nsizes: 2 3 4\nencoding: raw\n'
                     'data file: single.raw\n\n')

        with self.assertRaisesRegex(nrrd.NRRDError, 'does not reference multiple slice files'):
            nrrd.read_sliced(single_header)


if __name__ == '__main__':
    unittest.main()
