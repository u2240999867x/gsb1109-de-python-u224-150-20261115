import os
import tempfile
import unittest

import numpy as np

import nrrd
from nrrd.tests.util import *


class TestComparison(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp('nrrdtest')

    def path(self, name):
        return os.path.join(self.temp_dir, name)

    def write(self, name, data, header, index_order='F'):
        filename = self.path(name)
        # bzip2 only accepts compression levels 1 through 9.
        level = 0 if header.get('encoding', 'raw') in ('raw', 'gzip') else 9
        nrrd.write(filename, data, dict(header), index_order=index_order, compression_level=level)
        return filename

    @staticmethod
    def base_header():
        return {
            'type': 'short',
            'dimension': 2,
            'space': 'left-posterior-superior',
            'sizes': np.array([3, 4]),
            'space directions': np.array([[2., 0., 0.], [0., 3., 0.]]),
            'space origin': np.array([1., 2., 0.]),
            'kinds': ['domain', 'domain'],
            'encoding': 'raw',
            'endian': 'little',
        }

    def data(self):
        return np.arange(12, dtype=np.int16).reshape((3, 4), order='F')

    def test_identical(self):
        comparison = nrrd.compare((self.data(), self.base_header()), (self.data(), self.base_header()))
        self.assertTrue(comparison.equivalent)
        self.assertTrue(comparison.values_equal)
        self.assertTrue(comparison.spatial_equivalence)
        self.assertEqual(comparison.claim, 'spatially_equivalent')

    def test_compression_and_endian_are_storage_details(self):
        data = self.data()

        raw = self.write('raw.nrrd', data, self.base_header())
        gz = self.write('gz.nrrd', data, {**self.base_header(), 'encoding': 'gzip'})
        bz2 = self.write('bz2.nrrd', data, {**self.base_header(), 'encoding': 'bzip2'})
        big = self.write('big.nrrd', data.astype('>i2'), {**self.base_header(), 'endian': 'big'})

        for filename in (gz, bz2, big):
            comparison = nrrd.compare(raw, filename)
            self.assertTrue(comparison.equivalent)
            self.assertTrue(comparison.value_type_equal)
            self.assertFalse(comparison.differences)

    def test_type_aliases_compare_equal_but_type_changes_do_not(self):
        alias = nrrd.compare((self.data(), self.base_header()),
                             (self.data(), {**self.base_header(), 'type': 'int16'}))
        self.assertTrue(alias.value_type_equal)

        different = nrrd.compare((self.data(), self.base_header()),
                                 (self.data(), {**self.base_header(), 'type': 'unsigned short'}))
        self.assertFalse(different.value_type_equal)
        self.assertFalse(different.equivalent)
        self.assertIn('value_type', different.differences)

    def test_axis_reordering_is_equivalent(self):
        # Same physical samples, but the axes are described in the opposite order and the array is stored with the
        # matching order. This is the F-order / C-order rearrangement case.
        data = self.data()
        reordered_header = {
            **self.base_header(),
            'sizes': np.array([4, 3]),
            'space directions': np.array([[0., 3., 0.], [2., 0., 0.]]),
        }

        first = self.write('first.nrrd', data, self.base_header())
        second = self.write('second.nrrd', np.ascontiguousarray(data.T), reordered_header)

        comparison = nrrd.compare(first, second)
        self.assertTrue(comparison.coordinate_definition_equal)
        self.assertTrue(comparison.values_equal)
        self.assertTrue(comparison.spatial_equivalence)
        self.assertTrue(comparison.equivalent)
        # The raw arrays are not the same, only the physical grid is.
        self.assertFalse(comparison.arrays_equal)

    def test_reversed_axis_is_equivalent_when_origin_moves(self):
        data = self.data()
        reversed_header = {
            **self.base_header(),
            'space directions': np.array([[-2., 0., 0.], [0., 3., 0.]]),
            'space origin': np.array([1. + 2. * (3 - 1), 2., 0.]),
        }
        first = self.write('first.nrrd', data, self.base_header())
        second = self.write('second.nrrd', np.flip(data, axis=0).copy(), reversed_header)

        comparison = nrrd.compare(first, second)
        self.assertTrue(comparison.equivalent)
        self.assertTrue(comparison.spatial_equivalence)

    def test_reversed_axis_without_moved_origin_is_different(self):
        data = self.data()
        reversed_header = {**self.base_header(), 'space directions': np.array([[-2., 0., 0.], [0., 3., 0.]])}
        first = self.write('first.nrrd', data, self.base_header())
        second = self.write('second.nrrd', np.flip(data, axis=0).copy(), reversed_header)

        comparison = nrrd.compare(first, second)
        self.assertFalse(comparison.coordinate_definition_equal)
        self.assertFalse(comparison.spatial_equivalence)
        self.assertFalse(comparison.equivalent)

    def test_missing_directions_only_claims_array_equality(self):
        no_directions = {'type': 'short', 'dimension': 2, 'sizes': np.array([3, 4]), 'encoding': 'raw',
                         'endian': 'little'}
        first = self.write('first.nrrd', self.data(), no_directions)
        second = self.write('second.nrrd', self.data(), no_directions)

        comparison = nrrd.compare(first, second)
        self.assertTrue(comparison.arrays_equal)
        self.assertTrue(comparison.values_equal)
        self.assertIsNone(comparison.spatial_equivalence)
        self.assertIsNone(comparison.same_grid)
        self.assertFalse(comparison.equivalent)
        self.assertEqual(comparison.claim, 'arrays_equivalent')
        self.assertIn('arrays', comparison.verdict.lower())
        self.assertNotIn('same physical grid', comparison.verdict)

    def test_missing_directions_never_claim_spatial_equivalence(self):
        no_directions = {'type': 'short', 'dimension': 2, 'sizes': np.array([3, 4]), 'encoding': 'raw',
                         'endian': 'little'}
        comparison = nrrd.compare((self.data(), no_directions), (self.data(), no_directions))
        self.assertIsNone(comparison.spatial_equivalence)
        self.assertNotEqual(comparison.claim, 'spatially_equivalent')

    def test_non_spatial_axis_reordering(self):
        header = {'type': 'double', 'dimension': 3, 'space': 'right-anterior-superior',
                  'sizes': np.array([3, 4, 2]),
                  'space directions': np.array([[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]),
                  'kinds': ['domain', 'domain', 'vector'], 'encoding': 'raw', 'endian': 'little'}
        data = np.arange(24, dtype=float).reshape((3, 4, 2), order='F')
        first = self.write('first.nrrd', data, header)

        reordered_header = {**header,
                            'sizes': np.array([3, 2, 4]),
                            'space directions': np.array([[1., 0., 0.], [0., 0., 1.], [0., 1., 0.]]),
                            'kinds': ['domain', 'vector', 'domain']}
        second = self.write('second.nrrd', np.transpose(data, (0, 2, 1)).copy(), reordered_header)

        comparison = nrrd.compare(first, second)
        self.assertTrue(comparison.coordinate_definition_equal)
        self.assertTrue(comparison.equivalent)

    def test_repeated_directions_are_ambiguous(self):
        header = {'type': 'double', 'dimension': 2, 'sizes': np.array([3, 4]),
                  'space directions': np.array([[1., 0., 0.], [1., 0., 0.]]), 'encoding': 'raw', 'endian': 'little'}
        comparison = nrrd.compare((np.zeros((3, 4)), header), (np.zeros((3, 4)), dict(header)))
        self.assertIsNone(comparison.spatial_equivalence)
        self.assertEqual(comparison.claim, 'arrays_equivalent')

    def test_different_space_is_not_spatially_equivalent(self):
        other = {**self.base_header(), 'space': 'right-anterior-superior'}
        comparison = nrrd.compare((self.data(), self.base_header()), (self.data(), other))
        self.assertFalse(comparison.coordinate_definition_equal)
        self.assertFalse(comparison.spatial_equivalence)
        self.assertEqual(comparison.claim, 'different')

    def test_direction_representations_are_interchangeable(self):
        matrix = self.base_header()
        vector_list = {**self.base_header(),
                       'space directions': [np.array([2., 0., 0.]), np.array([0., 3., 0.])]}
        comparison = nrrd.compare((self.data(), matrix), (self.data(), vector_list))
        self.assertTrue(comparison.spatial_equivalence)
        self.assertTrue(comparison.equivalent)

    def test_non_spatial_axis_may_be_none_or_nan_row(self):
        common = {'type': 'short', 'dimension': 3, 'sizes': np.array([3, 4, 2]), 'encoding': 'raw',
                  'endian': 'little'}
        vector_list = {**common, 'space directions': [np.array([1., 0., 0.]), np.array([0., 1., 0.]), None]}
        matrix = {**common, 'space directions': np.array([[1., 0., 0.], [0., 1., 0.], [np.nan] * 3])}
        comparison = nrrd.compare_headers(vector_list, matrix)
        self.assertTrue(comparison.spatial_equivalence)
        self.assertTrue(comparison.equivalent)

    def test_dimension_mismatch_is_a_conflict_even_without_directions(self):
        small = {'type': 'short', 'dimension': 2, 'sizes': np.array([2, 2]), 'encoding': 'raw', 'endian': 'little'}
        large = {'type': 'short', 'dimension': 3, 'sizes': np.array([2, 2, 2]), 'encoding': 'raw',
                 'endian': 'little'}
        comparison = nrrd.compare_headers(small, large)
        self.assertFalse(comparison.spatial_equivalence)
        self.assertEqual(comparison.same_grid, False)

    def test_size_mismatch_is_a_conflict_even_with_directions(self):
        bigger = {**self.base_header(), 'sizes': np.array([3, 5])}
        comparison = nrrd.compare_headers(self.base_header(), bigger)
        self.assertFalse(comparison.coordinate_definition_equal)
        self.assertFalse(comparison.spatial_equivalence)
        self.assertIn('sizes differ', comparison.differences['coordinate_definition'][0])

    def test_value_difference(self):
        changed = self.data()
        changed[0, 0] = 999
        comparison = nrrd.compare((self.data(), self.base_header()), (changed, self.base_header()))
        self.assertTrue(comparison.coordinate_definition_equal)
        self.assertFalse(comparison.values_equal)
        self.assertFalse(comparison.equivalent)
        self.assertIn('values', comparison.differences)

    def test_shape_difference(self):
        comparison = nrrd.compare((self.data(), self.base_header()),
                                  (np.zeros((3, 5), dtype=np.int16),
                                   {**self.base_header(), 'sizes': np.array([3, 5])}))
        self.assertFalse(comparison.coordinate_definition_equal)
        self.assertFalse(comparison.values_equal)

    def test_custom_metadata(self):
        with_metadata = {**self.base_header(), 'patient': 'abc'}
        same = nrrd.compare((self.data(), with_metadata), (self.data(), {**self.base_header(), 'patient': 'abc'}))
        self.assertTrue(same.custom_metadata_equal)

        different = nrrd.compare((self.data(), with_metadata), (self.data(), {**self.base_header(), 'patient': 'xyz'}))
        self.assertFalse(different.custom_metadata_equal)
        self.assertFalse(different.equivalent)

        missing = nrrd.compare((self.data(), with_metadata), (self.data(), self.base_header()))
        self.assertFalse(missing.custom_metadata_equal)

        ignored = nrrd.compare((self.data(), with_metadata), (self.data(), self.base_header()),
                               ignore_fields=['patient'])
        self.assertTrue(ignored.custom_metadata_equal)

    def test_custom_field_map_marks_typed_custom_fields(self):
        header_a = {**self.base_header(), 'acquisition': np.array([1., 2., 3.])}
        header_b = {**self.base_header(), 'acquisition': np.array([1., 2., 4.])}
        comparison = nrrd.compare((self.data(), header_a), (self.data(), header_b),
                                  custom_field_map={'acquisition': 'double vector'})
        self.assertFalse(comparison.custom_metadata_equal)

    def test_header_comparison(self):
        comparison = nrrd.compare_headers(self.base_header(), {**self.base_header(), 'type': 'int16'})
        self.assertTrue(comparison.coordinate_definition_equal)
        self.assertTrue(comparison.value_type_equal)
        self.assertTrue(comparison.spatial_equivalence)
        self.assertTrue(comparison.equivalent)

        no_directions = {'type': 'short', 'dimension': 2, 'sizes': np.array([3, 4])}
        comparison = nrrd.compare_headers(no_directions, dict(no_directions))
        self.assertIsNone(comparison.spatial_equivalence)
        self.assertFalse(comparison.equivalent)

    def test_index_order_of_array_pairs(self):
        # Both pairs are given in C order, where the array axes run slowest-varying to fastest-varying. The two
        # headers describe the same physical grid with the axes listed in opposite orders.
        data = self.data()
        c_header = {**self.base_header(),
                    'sizes': np.array([4, 3]),
                    'space directions': np.array([[0., 3., 0.], [2., 0., 0.]])}
        f_ordered_pair = (data.T, self.base_header())
        c_ordered_pair = (data, c_header)
        comparison = nrrd.compare(f_ordered_pair, c_ordered_pair, index_order='C')
        self.assertTrue(comparison.coordinate_definition_equal)
        self.assertTrue(comparison.values_equal)
        self.assertTrue(comparison.equivalent)

    def test_bool_and_as_dict(self):
        comparison = nrrd.compare((self.data(), self.base_header()), (self.data(), self.base_header()))
        self.assertTrue(bool(comparison))
        result = comparison.as_dict()
        for key in ('coordinate_definition_equal', 'value_type_equal', 'values_equal', 'custom_metadata_equal',
                    'arrays_equal', 'spatial_equivalence', 'equivalent', 'claim', 'verdict'):
            self.assertIn(key, result)

    def test_sample_files_with_different_encodings(self):
        # Fixture files: raw, gzip, bzip2 and detached header all hold the same samples on the same grid.
        comparison = nrrd.compare(RAW_NRRD_FILE_PATH, GZ_NRRD_FILE_PATH)
        self.assertTrue(comparison.equivalent)
        self.assertTrue(comparison.spatial_equivalence)

        comparison = nrrd.compare(RAW_NRRD_FILE_PATH, BZ2_NRRD_FILE_PATH)
        self.assertTrue(comparison.equivalent)

        comparison = nrrd.compare(RAW_NRRD_FILE_PATH, RAW_NHDR_FILE_PATH)
        self.assertTrue(comparison.equivalent)

    def test_invalid_index_order(self):
        with self.assertRaises(nrrd.NRRDError):
            nrrd.compare((self.data(), self.base_header()), (self.data(), self.base_header()), index_order='X')
