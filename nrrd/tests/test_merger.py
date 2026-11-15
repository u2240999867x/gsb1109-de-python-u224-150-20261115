import os
import tempfile
import unittest

import numpy as np

import nrrd
from nrrd.errors import NRRDConflictError, NRRDGridError


def _block(data, origin, directions=None, extra=None):
    header = {
        'space origin': np.asarray(origin, dtype=float),
        'space directions': np.eye(data.ndim, dtype=float) if directions is None else np.asarray(directions, float),
    }
    if extra:
        header.update(extra)
    return data, header


class TestMergeNRRD(unittest.TestCase):
    def test_two_adjacent_blocks_positive_axes(self):
        left = np.arange(6, dtype=np.int32).reshape(2, 3)
        right = (np.arange(6, dtype=np.int32) + 100).reshape(2, 3)

        block0 = _block(left, origin=[0.0, 0.0])
        block1 = _block(right, origin=[2.0, 0.0])

        merged, header, report = nrrd.merge_nrrd([block0, block1])

        expected = np.concatenate([left, right], axis=0)
        np.testing.assert_array_equal(merged, expected)
        np.testing.assert_array_equal(header['space origin'], [0.0, 0.0])
        np.testing.assert_array_equal(header['sizes'], [4, 3])
        self.assertEqual(header['dimension'], 2)
        self.assertTrue(report.covered_mask.all())
        self.assertEqual(report.conflict_count, 0)
        self.assertEqual([placement.offset for placement in report.placements], [(0, 0), (2, 0)])

    def test_blocks_order_does_not_change_result(self):
        block0 = _block(np.arange(6, dtype=np.int32).reshape(2, 3), origin=[0.0, 0.0])
        block1 = _block((np.arange(6, dtype=np.int32) + 100).reshape(2, 3), origin=[2.0, 0.0])

        merged_a, _, _ = nrrd.merge_nrrd([block0, block1])
        merged_b, header_b, report_b = nrrd.merge_nrrd([block1, block0])

        np.testing.assert_array_equal(merged_a, merged_b)
        np.testing.assert_array_equal(header_b['space origin'], [0.0, 0.0])
        self.assertEqual(report_b.placements[0].offset, (2, 0))
        self.assertEqual(report_b.placements[1].offset, (0, 0))

    def test_negative_axis_direction(self):
        # First axis points in the negative world direction with spacing 1.5.
        directions = np.array([[-1.5, 0.0], [0.0, 1.0]])
        block0 = _block(np.full((2, 2), 1, dtype=np.uint8), origin=[3.0, 0.0], directions=directions)
        block1 = _block(np.full((2, 2), 2, dtype=np.uint8), origin=[0.0, 0.0], directions=directions)

        merged, header, report = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (4, 2))
        # Block 0 occupies world x in (0, 3] i.e. output indices 0..1, block 1 covers x in [-3, 0] i.e. indices 2..3.
        np.testing.assert_array_equal(merged[:2], 1)
        np.testing.assert_array_equal(merged[2:], 2)
        np.testing.assert_allclose(header['space origin'], [3.0, 0.0])
        self.assertEqual(report.placements[0].offset, (0, 0))
        self.assertEqual(report.placements[1].offset, (2, 0))

    def test_overlap_with_equal_values_is_accepted(self):
        block0 = _block(np.ones((3, 3), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.ones((3, 3), dtype=np.int32), origin=[1.0, 1.0])

        merged, _, report = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (4, 4))
        self.assertEqual(report.conflict_count, 0)
        # Two 3x3 blocks offset by (1, 1) cover 16 - 4 = 14 voxels; the two corners stay unfilled.
        self.assertEqual(int(merged.sum()), 14)
        self.assertEqual(int(report.covered_mask.sum()), 14)

    def test_overlap_conflict_raises_and_keeps_report(self):
        block0 = _block(np.ones((3, 3), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.full((3, 3), 2, dtype=np.int32), origin=[1.0, 1.0])

        with self.assertRaises(NRRDConflictError) as context:
            nrrd.merge_nrrd([block0, block1])

        report = context.exception.report
        self.assertEqual(report.conflict_count, 4)
        self.assertTrue(report.conflict_mask[1:3, 1:3].all())
        self.assertEqual(report.conflicts[0]['first_block'], 0)
        self.assertEqual(report.conflicts[0]['conflicting_block'], 1)
        self.assertEqual(report.conflicts[0]['first_value'], 1)
        self.assertEqual(report.conflicts[0]['conflicting_value'], 2)

    def test_overlap_conflict_keep_first(self):
        block0 = _block(np.ones((3, 3), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.full((3, 3), 2, dtype=np.int32), origin=[1.0, 1.0])

        merged, _, report = nrrd.merge_nrrd([block0, block1], on_conflict='keep_first')

        self.assertEqual(report.conflict_count, 4)
        np.testing.assert_array_equal(merged[:3, :3], 1)
        np.testing.assert_array_equal(merged[3, 1:], 2)
        np.testing.assert_array_equal(merged[1:, 3], 2)
        # The corner voxel (3, 0) is covered by neither block and keeps the integer fill value.
        self.assertEqual(merged[3, 0], 0)

    def test_half_voxel_origin_shift_is_rejected(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.ones((2, 2), dtype=np.int32), origin=[1.5, 0.0])

        with self.assertRaises(NRRDGridError) as context:
            nrrd.merge_nrrd([block0, block1])

        self.assertIn('interpolation', str(context.exception))
        self.assertIn('1.5', str(context.exception))

    def test_different_spacing_is_rejected(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        directions = np.diag([2.0, 1.0])
        block1 = _block(np.ones((2, 2), dtype=np.int32), origin=[4.0, 0.0], directions=directions)

        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([block0, block1])

    def test_different_orientation_is_rejected(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        directions = np.array([[0.0, 1.0], [1.0, 0.0]])
        block1 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0], directions=directions)

        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([block0, block1])

    def test_missing_origin_is_rejected(self):
        data = np.ones((2, 2), dtype=np.int32)
        block0 = _block(data, origin=[0.0, 0.0])
        block1 = (data, {'space directions': np.eye(2)})

        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([block0, block1])

    def test_dtype_mismatch_is_rejected(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.ones((2, 2), dtype=np.float64), origin=[2.0, 0.0])

        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([block0, block1])

    def test_custom_fill_value_for_gap(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.full((2, 2), 9, dtype=np.int32), origin=[4.0, 0.0])

        merged, _, report = nrrd.merge_nrrd([block0, block1], fill_value=-1)

        self.assertEqual(merged.shape, (6, 2))
        np.testing.assert_array_equal(merged[2:4], -1)
        np.testing.assert_array_equal(~report.covered_mask[2:4], True)

    def test_default_float_fill_is_nan(self):
        block0 = _block(np.ones((2, 2), dtype=np.float64), origin=[0.0, 0.0])
        block1 = _block(np.full((2, 2), 9.0, dtype=np.float64), origin=[4.0, 0.0])

        merged, _, _ = nrrd.merge_nrrd([block0, block1])

        self.assertTrue(np.isnan(merged[2:4]).all())

    def test_four_dimensional_non_spatial_axis(self):
        # Axes 0 and 1 are spatial, axis 2 is a non-spatial (e.g. RGB) axis shared by both blocks.
        directions = np.array([[1.0, 0.0], [0.0, 1.0], [np.nan, np.nan]])
        block0 = _block(np.ones((2, 2, 3), dtype=np.int32), origin=[0.0, 0.0], directions=directions)
        block1 = _block(np.full((2, 2, 3), 5, dtype=np.int32), origin=[2.0, 0.0], directions=directions)

        merged, header, report = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (4, 2, 3))
        np.testing.assert_array_equal(merged[:2], 1)
        np.testing.assert_array_equal(merged[2:], 5)
        self.assertEqual(report.spatial_axes, (0, 1))
        np.testing.assert_array_equal(header['space origin'], [0.0, 0.0])

    def test_non_spatial_axis_size_mismatch_is_rejected(self):
        directions0 = np.array([[1.0, 0.0], [0.0, 1.0], [np.nan, np.nan]])
        block0 = _block(np.ones((2, 2, 3), dtype=np.int32), origin=[0.0, 0.0], directions=directions0)
        block1 = _block(np.ones((2, 2, 4), dtype=np.int32), origin=[2.0, 0.0], directions=directions0)

        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([block0, block1])

    def test_interleaved_non_spatial_axis_keeps_axis_positions(self):
        # Axis 1 is non-spatial (e.g. a list/vector axis) sandwiched between spatial axes 0 and 2.
        directions = np.array([[1.0, 0.0], [np.nan, np.nan], [0.0, 1.0]])
        block0 = _block(np.ones((2, 3, 2), dtype=np.int32), origin=[0.0, 0.0], directions=directions)
        block1 = _block(np.full((2, 3, 2), 6, dtype=np.int32), origin=[2.0, 0.0], directions=directions)

        merged, _, report = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (4, 3, 2))
        np.testing.assert_array_equal(merged[:2], 1)
        np.testing.assert_array_equal(merged[2:], 6)
        self.assertEqual(report.spatial_axes, (0, 2))
        self.assertEqual(report.placements[1].slices, (slice(2, 4), slice(0, 3), slice(0, 2)))

    def test_vector_list_directions(self):
        block0_data = np.ones((2, 2), dtype=np.int32)
        block1_data = np.full((2, 2), 7, dtype=np.int32)
        directions = [np.array([1.0, 0.0]), np.array([0.0, 2.0])]
        block0 = (block0_data, {'space directions': directions, 'space origin': np.array([0.0, 0.0])})
        block1 = (block1_data, {'space directions': directions, 'space origin': np.array([2.0, 0.0])})

        merged, header, _ = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (4, 2))
        np.testing.assert_array_equal(merged[2:], 7)
        np.testing.assert_allclose(header['space origin'], [0.0, 0.0])

    def test_nan_values_are_not_conflicts(self):
        data0 = np.array([[1.0, np.nan], [2.0, 3.0]])
        data1 = np.array([[np.nan, np.nan], [2.0, 3.0]])
        block0 = _block(data0, origin=[0.0, 0.0])
        block1 = _block(data1, origin=[1.0, 0.0])

        merged, _, report = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(report.conflict_count, 0)
        # A real sample takes precedence over an overlapping NaN, and purely unsampled voxels stay NaN.
        self.assertEqual(merged[1, 0], 2.0)
        self.assertTrue(np.isnan(merged[0, 1]))
        self.assertEqual(merged[2, 0], 2.0)

    def test_anisotropic_spacing_origin_math(self):
        directions = np.diag([0.5, 2.0])
        block0 = _block(np.full((2, 3), 1, dtype=np.int32), origin=[0.0, 0.0], directions=directions)
        block1 = _block(np.full((2, 3), 2, dtype=np.int32), origin=[1.0, 0.0], directions=directions)

        merged, header, report = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (4, 3))
        np.testing.assert_array_equal(merged[:2], 1)
        np.testing.assert_array_equal(merged[2:], 2)
        np.testing.assert_allclose(header['space origin'], [0.0, 0.0])
        self.assertEqual(report.placements[1].offset, (2, 0))

    def test_round_trip_through_nrrd_write(self):
        block0 = _block(np.arange(6, dtype=np.int32).reshape(2, 3), origin=[1.0, -2.0])
        block1 = _block((np.arange(6, dtype=np.int32) + 100).reshape(2, 3), origin=[3.0, -2.0])

        merged, header, _ = nrrd.merge_nrrd([block0, block1])

        with tempfile.TemporaryDirectory() as temp_dir:
            filename = os.path.join(temp_dir, 'merged.nrrd')
            nrrd.write(filename, merged, {**header, 'encoding': 'raw'})
            read_data, read_header = nrrd.read(filename)

        np.testing.assert_array_equal(read_data, merged)
        np.testing.assert_allclose(read_header['space origin'], [1.0, -2.0])
        np.testing.assert_array_equal(read_header['sizes'], [4, 3])

    def test_merge_nrrd_files(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.full((2, 2), 4, dtype=np.int32), origin=[2.0, 0.0])

        with tempfile.TemporaryDirectory() as temp_dir:
            filename0 = os.path.join(temp_dir, 'block0.nrrd')
            filename1 = os.path.join(temp_dir, 'block1.nrrd')
            nrrd.write(filename0, block0[0], {**block0[1], 'encoding': 'raw'})
            nrrd.write(filename1, block1[0], {**block1[1], 'encoding': 'raw'})

            # Files are passed in reverse order, but geometry determines placement, not input order.
            merged, _, report = nrrd.merge_nrrd_files([filename1, filename0])

        self.assertEqual(merged.shape, (4, 2))
        np.testing.assert_array_equal(merged[:2], 1)
        np.testing.assert_array_equal(merged[2:], 4)
        self.assertEqual(report.conflict_count, 0)

    def test_empty_input_is_rejected(self):
        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([])

    def test_three_dimensional_merge_keeps_directions(self):
        directions = np.diag([1.0, 1.0, 1.0])
        block0 = _block(np.ones((2, 2, 2), dtype=np.int32), origin=[0.0, 0.0, 0.0], directions=directions)
        block1 = _block(np.full((2, 2, 2), 7, dtype=np.int32), origin=[0.0, 0.0, 2.0], directions=directions)

        merged, header, report = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (2, 2, 4))
        np.testing.assert_array_equal(merged[:, :, :2], 1)
        np.testing.assert_array_equal(merged[:, :, 2:], 7)
        np.testing.assert_allclose(header['space directions'], directions)
        self.assertEqual(report.placements[1].offset, (0, 0, 2))

    def test_conflict_details_are_capped_but_count_is_exact(self):
        block0 = _block(np.ones((4, 4), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.full((4, 4), 2, dtype=np.int32), origin=[1.0, 1.0])

        with self.assertRaises(NRRDConflictError) as context:
            nrrd.merge_nrrd([block0, block1])

        report = context.exception.report
        self.assertEqual(report.conflict_count, 9)
        self.assertEqual(int(report.conflict_mask.sum()), 9)
        self.assertLessEqual(len(report.conflicts), 10)

    def test_alignment_tolerance_accepts_near_integer_offset(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.ones((2, 2), dtype=np.int32), origin=[2.0 + 5e-7, 0.0])

        merged, _, report = nrrd.merge_nrrd([block0, block1], alignment_tolerance=1e-6)

        self.assertEqual(merged.shape, (4, 2))
        self.assertEqual(report.conflict_count, 0)

    def test_alignment_tolerance_rejects_offset_beyond_tolerance(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.ones((2, 2), dtype=np.int32), origin=[2.001, 0.0])

        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([block0, block1], alignment_tolerance=1e-6)

    def test_merge_nrrd_files_propagates_conflict(self):
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0])
        block1 = _block(np.full((2, 2), 4, dtype=np.int32), origin=[1.0, 0.0])

        with tempfile.TemporaryDirectory() as temp_dir:
            filename0 = os.path.join(temp_dir, 'block0.nrrd')
            filename1 = os.path.join(temp_dir, 'block1.nrrd')
            nrrd.write(filename0, block0[0], {**block0[1], 'encoding': 'raw'})
            nrrd.write(filename1, block1[0], {**block1[1], 'encoding': 'raw'})

            with self.assertRaises(NRRDConflictError):
                nrrd.merge_nrrd_files([filename0, filename1])

    def test_spacings_header_agreeing_with_directions_is_accepted(self):
        directions = np.diag([2.0, 3.0])
        extra = {'spacings': np.array([2.0, 3.0])}
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0], directions=directions, extra=extra)
        block1 = _block(np.full((2, 2), 5, dtype=np.int32), origin=[4.0, 0.0], directions=directions, extra=extra)

        merged, _, _ = nrrd.merge_nrrd([block0, block1])

        self.assertEqual(merged.shape, (4, 2))

    def test_spacings_header_disagreeing_with_directions_is_rejected(self):
        directions = np.diag([2.0, 3.0])
        block0 = _block(np.ones((2, 2), dtype=np.int32), origin=[0.0, 0.0], directions=directions,
                        extra={'spacings': np.array([2.0, 3.0])})
        block1 = _block(np.ones((2, 2), dtype=np.int32), origin=[4.0, 0.0], directions=directions,
                        extra={'spacings': np.array([1.5, 3.0])})

        with self.assertRaises(NRRDGridError):
            nrrd.merge_nrrd([block0, block1])


if __name__ == '__main__':
    unittest.main()
