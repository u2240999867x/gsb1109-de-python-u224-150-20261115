import unittest

import numpy as np

import nrrd
from nrrd.errors import NRRDError


def _header(sizes, origin=(0.0, 0.0), directions=None):
    if directions is None:
        directions = np.eye(len(sizes))
    return {'dimension': len(sizes),
            'sizes': np.array(sizes),
            'space directions': np.array(directions, dtype=float),
            'space origin': np.array(origin, dtype=float),
            'type': 'float',
            'encoding': 'raw'}


class TestStitching(unittest.TestCase):
    def test_stitch_adjacent_blocks(self):
        block_a = (np.array([[1, 2], [3, 4]]), _header([2, 2]))
        block_b = (np.array([[5, 6], [7, 8]]), _header([2, 2], origin=(2, 0)))

        data, header, conflicts = nrrd.stitch([block_a, block_b])

        np.testing.assert_array_equal(data, [[1, 2], [3, 4], [5, 6], [7, 8]])
        np.testing.assert_array_equal(header['sizes'], [4, 2])
        np.testing.assert_array_equal(header['space origin'], [0, 0])
        self.assertFalse(np.any(conflicts))

    def test_stitch_overlap_agreeing_values(self):
        block_a = (np.ones((3, 2)), _header([3, 2]))
        block_b = (np.ones((2, 2)), _header([2, 2], origin=(2, 0)))

        data, _, conflicts = nrrd.stitch([block_a, block_b])

        np.testing.assert_array_equal(data, np.ones((4, 2)))
        self.assertFalse(np.any(conflicts))

    def test_stitch_overlap_conflicting_values(self):
        block_a = (np.ones((3, 2)), _header([3, 2]))
        block_b = (np.array([[1, 5], [9, 9]]), _header([2, 2], origin=(2, 0)))

        data, _, conflicts = nrrd.stitch([block_a, block_b])

        # First written value wins, conflicting voxels are flagged
        self.assertEqual(data[2, 1], 1)
        np.testing.assert_array_equal(conflicts, [[False, False], [False, False],
                                                  [False, True], [False, False]])

    def test_stitch_negative_axis_direction(self):
        block_a = (np.array([[1, 2], [3, 4]]),
                   _header([2, 2], origin=(1, 0), directions=[[-1, 0], [0, 1]]))
        block_b = (np.array([[5, 6], [7, 8]]), _header([2, 2], origin=(2, 0)))

        data, header, conflicts = nrrd.stitch([block_a, block_b])

        np.testing.assert_array_equal(data, [[3, 4], [1, 2], [5, 6], [7, 8]])
        np.testing.assert_array_equal(header['space origin'], [0, 0])
        np.testing.assert_array_equal(header['space directions'], [[1, 0], [0, 1]])
        self.assertFalse(np.any(conflicts))

    def test_stitch_all_axes_negative(self):
        block_a = (np.array([[1, 2], [3, 4]]),
                   _header([2, 2], origin=(1, 1), directions=[[-1, 0], [0, -1]]))
        block_b = (np.array([[5, 6], [7, 8]]), _header([2, 2], origin=(2, 0)))

        data, _, _ = nrrd.stitch([block_a, block_b])

        np.testing.assert_array_equal(data, [[4, 3], [2, 1], [5, 6], [7, 8]])

    def test_stitch_scaled_spacing(self):
        block_a = (np.array([[1], [2]]), _header([2, 1], directions=[[2, 0], [0, 2]]))
        block_b = (np.array([[3], [4]]), _header([2, 1], origin=(4, 0), directions=[[2, 0], [0, 2]]))

        data, header, _ = nrrd.stitch([block_a, block_b])

        np.testing.assert_array_equal(data, [[1], [2], [3], [4]])
        np.testing.assert_array_equal(header['space directions'], [[2, 0], [0, 2]])

    def test_stitch_half_voxel_offset_rejected(self):
        block_a = (np.ones((2, 2)), _header([2, 2]))
        block_b = (np.ones((2, 2)), _header([2, 2], origin=(0.5, 0)))

        with self.assertRaisesRegex(NRRDError, r'fractional \(e.g. half\) voxel'):
            nrrd.stitch([block_a, block_b])

    def test_stitch_mismatched_spacing_rejected(self):
        block_a = (np.ones((2, 2)), _header([2, 2]))
        block_b = (np.ones((2, 2)), _header([2, 2], origin=(2, 0), directions=[[2, 0], [0, 2]]))

        with self.assertRaisesRegex(NRRDError, 'cannot be stitched without resampling'):
            nrrd.stitch([block_a, block_b])

    def test_stitch_gap_filled_with_nan(self):
        block_a = (np.ones((2, 2)), _header([2, 2]))
        block_b = (np.ones((2, 2)) * 2, _header([2, 2], origin=(4, 0)))

        data, _, _ = nrrd.stitch([block_a, block_b])

        self.assertEqual(data.shape, (6, 2))
        np.testing.assert_array_equal(data[2:4], np.nan)

    def test_stitch_3d_blocks(self):
        block_a = (np.ones((2, 2, 2)), _header([2, 2, 2], origin=(0, 0, 0),
                                               directions=np.eye(3)))
        block_b = (np.ones((2, 2, 2)) * 2, _header([2, 2, 2], origin=(0, 0, 2),
                                                   directions=np.eye(3)))

        data, header, conflicts = nrrd.stitch([block_a, block_b])

        self.assertEqual(data.shape, (2, 2, 4))
        np.testing.assert_array_equal(data[:, :, :2], 1)
        np.testing.assert_array_equal(data[:, :, 2:], 2)
        np.testing.assert_array_equal(header['sizes'], [2, 2, 4])
        self.assertFalse(np.any(conflicts))

    def test_stitch_no_blocks(self):
        with self.assertRaises(NRRDError):
            nrrd.stitch([])


if __name__ == '__main__':
    unittest.main()
