import os
import tempfile
import unittest

import numpy as np

import nrrd


class MultiFileTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp('nrrdmultifile')

    def write_slices(self, shape=(4, 5), dtype='<u2', count=6, missing=(), offset=100,
                     directory=None):
        directory = directory or self.directory
        for index in range(count):
            if index in missing:
                continue
            data = np.full(shape, offset + index, dtype=dtype)
            data.tofile(os.path.join(directory, f'slice_{index:02d}.raw'))

    def write_header(self, lines, name='series.nhdr'):
        path = os.path.join(self.directory, name)
        with open(path, 'w') as fh:
            fh.write('\n'.join(lines + ['']))
        return path

    def list_header(self, names, shape=(4, 5, 6), extra=()):
        lines = ['NRRD0005', 'type: uint16', f'dimension: {len(shape)}',
                 'sizes: ' + ' '.join(str(x) for x in shape), 'encoding: raw',
                 'endian: little', 'data file: LIST']
        lines.extend(extra)
        lines.extend(names)
        return self.write_header(lines)

    def template_header(self, template, shape=(4, 5, 6), extra=()):
        lines = ['NRRD0005', 'type: uint16', f'dimension: {len(shape)}',
                 'sizes: ' + ' '.join(str(x) for x in shape), 'encoding: raw',
                 'endian: little', f'data file: {template}']
        lines.extend(extra)
        return self.write_header(lines)

    def test_list_preserves_order_and_gap(self):
        self.write_slices(missing=(3,))
        names = [f'slice_{index:02d}.raw' for index in range(6) if index != 3]
        data, header = nrrd.read(self.list_header(names))

        self.assertEqual((4, 5, 6), data.shape)
        np.testing.assert_array_equal([100, 101, 102, 0, 104, 105], data[0, 0, :])
        self.assertEqual([3], data.gaps)
        self.assertEqual(names[:3] + [None] + names[3:], data.sources)
        self.assertEqual(6, header['data file expected count'])
        self.assertEqual(5, header['data file count'])
        self.assertEqual(1, header['data file missing count'])

    def test_list_entries_are_not_compacted(self):
        self.write_slices(missing=(1,))
        names = [f'slice_{index:02d}.raw' for index in range(6) if index != 1]
        data, _ = nrrd.read(self.list_header(names))

        np.testing.assert_array_equal([100, 0, 102, 103, 104, 105], data[0, 0, :])

    def test_list_uses_trailing_index_not_line_order(self):
        self.write_slices(missing=(3,))
        names = [f'slice_{index:02d}.raw' for index in range(6) if index != 3][::-1]
        data, _ = nrrd.read(self.list_header(names))

        np.testing.assert_array_equal([100, 101, 102, 0, 104, 105], data[0, 0, :])

    def test_sequence_template_with_middle_gap(self):
        self.write_slices(missing=(3,))
        data, header = nrrd.read(self.template_header('slice_%02d.raw'))

        np.testing.assert_array_equal([100, 101, 102, 0, 104, 105], data[0, 0, :])
        self.assertEqual([3], data.gaps)
        self.assertEqual('slice_05.raw', data.sources[5])

    def test_one_based_sequence_template(self):
        os.makedirs(os.path.join(self.directory, 'sub'))
        self.write_slices(missing=(4,), count=6, offset=200,
                          directory=os.path.join(self.directory, 'sub'))
        for source_index in range(6):
            if source_index == 4:
                continue
            target_index = source_index + 1
            os.replace(os.path.join(self.directory, 'sub', f'slice_{source_index:02d}.raw'),
                       os.path.join(self.directory, 'sub', f'img.{target_index}.raw'))

        data, _ = nrrd.read(self.template_header('sub/img.%d.raw'))

        np.testing.assert_array_equal([200, 201, 202, 203, 0, 205], data[0, 0, :])
        self.assertEqual([4], data.gaps)
        self.assertEqual('sub/img.4.raw', data.sources[3])

    def test_arbitrary_series_start_and_gap(self):
        for index in (5, 6, 8, 9):
            np.full((4, 5), index, dtype='<u2').tofile(
                os.path.join(self.directory, f'slice_{index:03d}.raw'))

        data, _ = nrrd.read(self.template_header('slice_%03d.raw', shape=(4, 5, 5)))

        np.testing.assert_array_equal([5, 6, 0, 8, 9], data[0, 0, :])
        self.assertEqual([2], data.gaps)
        self.assertEqual(5, data.segments[0]['index'] + 5)

        names = [f'slice_{index:03d}.raw' for index in (5, 6, 8, 9)]
        data, _ = nrrd.read(self.list_header(names, shape=(4, 5, 5)))
        np.testing.assert_array_equal([5, 6, 0, 8, 9], data[0, 0, :])
        self.assertEqual([2], data.gaps)

    def test_list_names_without_numbers_are_positional(self):
        for name, value in (('a.raw', 1), ('b.raw', 2)):
            np.full((2,), value, dtype='<u2').tofile(os.path.join(self.directory, name))

        path = self.write_header([
            'NRRD0005', 'type: uint16', 'dimension: 2', 'sizes: 2 2',
            'encoding: raw', 'endian: little', 'data file: LIST', 'a.raw', 'b.raw'])
        data, _ = nrrd.read(path)

        np.testing.assert_array_equal([1, 2], data[0, :])
        self.assertEqual(['a.raw', 'b.raw'], data.sources)

    def test_byte_count_and_slice_shape_are_reported(self):
        self.write_slices(missing=(3,))
        data, header = nrrd.read(self.template_header('slice_%02d.raw'))

        segments = header['data file segments']
        self.assertEqual(6, len(segments))
        for segment in segments:
            self.assertEqual(40, segment['expected_bytes'])
        self.assertEqual(40, segments[0]['actual_bytes'])
        self.assertEqual(40, segments[0]['file_bytes'])
        self.assertEqual((4, 5), segments[0]['shape'])
        self.assertIsNone(segments[3]['actual_bytes'])
        self.assertEqual('missing', segments[3]['status'])

    def test_axis_span_accounts_for_gap(self):
        self.write_slices(missing=(3,))
        extra = ['space directions: (2,0,0) (0,3,0) (0,0,4)']
        data, header = nrrd.read(self.template_header('slice_%02d.raw', extra=extra))

        self.assertEqual(4.0, data.axis_spacing)
        self.assertEqual(20.0, data.axis_span)
        self.assertEqual(20.0, header['data file span'])
        self.assertEqual(8.0, data.segments[4]['span_from_previous'])
        self.assertEqual(4.0, data.segments[5]['span_from_previous'])

    def test_truncated_slice_is_an_error(self):
        self.write_slices(missing=(3,))
        with open(os.path.join(self.directory, 'slice_02.raw'), 'wb') as fh:
            fh.write(b'\x01\x02')

        with self.assertRaisesRegex(nrrd.NRRDError, 'slice 2'):
            nrrd.read(self.template_header('slice_%02d.raw'))

    def test_missing_first_and_last_slice_keeps_positions(self):
        self.write_slices(missing=(0, 5))
        data, _ = nrrd.read(self.template_header('slice_%02d.raw'))

        np.testing.assert_array_equal([0, 101, 102, 103, 104, 0], data[0, 0, :])
        self.assertEqual([0, 5], data.gaps)
        self.assertEqual(1.0, data.segments[4]['span_from_previous'])

    def test_relative_path_escaping_root_is_rejected(self):
        self.write_slices()
        path = self.template_header('../outside_%02d.raw')

        with self.assertRaisesRegex(nrrd.NRRDError, 'escapes authorized root'):
            nrrd.read(path)

    def test_absolute_path_outside_root_is_rejected(self):
        self.write_slices()
        path = self.template_header(os.path.join(tempfile.gettempdir(), 'outside_%02d.raw'))

        with self.assertRaisesRegex(nrrd.NRRDError, 'escapes authorized root'):
            nrrd.read(path)

    def test_symlink_outside_root_is_rejected(self):
        self.write_slices()
        outside_directory = tempfile.mkdtemp('nrrdoutside')
        outside = os.path.join(outside_directory, 'outside.raw')
        with open(outside, 'wb') as fh:
            fh.write(b'\x00' * 40)
        os.makedirs(os.path.join(self.directory, 'linked'))
        for index in range(6):
            os.symlink(outside, os.path.join(self.directory, 'linked', f'slice_{index:02d}.raw'))

        with self.assertRaisesRegex(nrrd.NRRDError, 'escapes authorized root'):
            nrrd.read(self.template_header('linked/slice_%02d.raw'))

    def test_explicit_data_root_allows_only_inside(self):
        self.write_slices()
        os.makedirs(os.path.join(self.directory, 'sub'))
        path = self.template_header('slice_%02d.raw')

        data, _ = nrrd.read(path, data_root=self.directory)
        self.assertEqual((4, 5, 6), data.shape)

        with self.assertRaisesRegex(nrrd.NRRDError, 'No data files were found'):
            nrrd.read(path, data_root=os.path.join(self.directory, 'sub'))

        # A parent root is authorized, but a relative data reference is
        # resolved below that root and therefore does not find this header's
        # neighboring slices.
        with self.assertRaisesRegex(nrrd.NRRDError, 'No data files were found'):
            nrrd.read(path, data_root=os.path.dirname(self.directory))

    def test_gap_value_for_floating_point_data(self):
        dtype = '<f4'
        for index in range(4):
            if index == 2:
                continue
            np.full((2, 2), float(index), dtype=dtype).tofile(
                os.path.join(self.directory, f'f_{index}.raw'))

        path = self.write_header([
            'NRRD0005', 'type: float', 'dimension: 3', 'sizes: 2 2 4',
            'encoding: raw', 'endian: little', 'data file: f_%d.raw'])
        data, _ = nrrd.read(path)

        self.assertTrue(np.isnan(data[0, 0, 2]))

    def test_c_order_read(self):
        self.write_slices(missing=(3,))
        data, _ = nrrd.read(self.template_header('slice_%02d.raw'), index_order='C')

        self.assertEqual((6, 5, 4), data.shape)
        np.testing.assert_array_equal([100, 101, 102, 0, 104, 105], data[:, 0, 0])


if __name__ == '__main__':
    unittest.main()
