import io
import unittest
from typing import ClassVar

import numpy as np
from typing_extensions import Literal

import nrrd
from nrrd.errors import NRRDError

NAN = np.nan


def world_coordinate(header, indices):
    """World coordinate of a voxel given in header-order (Fortran) indices."""

    directions = header['space directions']
    coordinate = np.array(header['space origin'], dtype=float).copy()
    for axis, index in enumerate(indices):
        vector = directions[axis]
        if vector is not None and not np.all(np.isnan(vector)):
            coordinate = coordinate + vector * index
    return coordinate


class Abstract:
    class TestReorientFunctions(unittest.TestCase):
        index_order: ClassVar[Literal['F', 'C']]

        def _array(self, header_shape, values=None):
            # Header-order shape is (fastest, ..., slowest). Build an array consistent with index_order.
            if values is None:
                values = np.arange(int(np.prod(header_shape)), dtype=float)
            if self.index_order == 'F':
                return np.asarray(values.reshape(header_shape), order='F')
            return np.asarray(values.reshape(header_shape[::-1]), order='C')

        def test_identity_target_leaves_data_unchanged(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([10., 20., 30.]),
            }

            result, result_header = nrrd.reorient(data, header, 'right-anterior-superior', self.index_order)

            np.testing.assert_array_equal(result, data)
            np.testing.assert_allclose(result_header['space directions'], header['space directions'])
            np.testing.assert_allclose(result_header['space origin'], header['space origin'])

        def test_axis_permutation(self):
            # Header axes are (x, y, z) but the target requests (z, y, x).
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([1., 2., 3.]),
                'kinds': ['domain', 'domain', 'domain'],
                'labels': ['X', 'Y', 'Z'],
            }

            result, result_header = nrrd.reorient(data, header, 'superior-anterior-right', self.index_order)

            expected_shape = tuple(result_header['sizes']) if self.index_order == 'F' \
                else tuple(np.asarray(result_header['sizes'])[::-1])
            self.assertEqual(result.shape, expected_shape)
            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[0., 0., 3.], [0., 2., 0.], [1., 0., 0.]]))
            self.assertEqual(result_header['kinds'], ['domain', 'domain', 'domain'])
            self.assertEqual(result_header['labels'], ['Z', 'Y', 'X'])

            # Every voxel keeps its world coordinate and its value.
            for indices in [(0, 0, 0), (1, 2, 3), (0, 1, 2), (1, 0, 3)]:
                new_indices = (indices[2], indices[1], indices[0])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, new_indices))
                np.testing.assert_array_equal(
                    result[new_indices[::-1] if self.index_order == 'C' else new_indices],
                    data[indices[::-1] if self.index_order == 'C' else indices])

        def test_negative_direction_flip_is_paired_with_reversal_and_origin_shift(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., -3.]]),
                'space origin': np.array([10., 20., 30.]),
            }

            result, result_header = nrrd.reorient(data, header, 'RAS', self.index_order)

            # The sign change is allowed only because the explicit target points the opposite way, and it must be
            # paired with the array reversal and an origin shift so voxel world coordinates are unchanged.
            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]))
            np.testing.assert_allclose(result_header['space origin'], np.array([10., 20., 21.]))

            for indices in [(0, 0, 0), (1, 2, 3), (0, 1, 2), (1, 0, 3)]:
                new_indices = (indices[0], indices[1], 3 - indices[2])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, new_indices))
                np.testing.assert_array_equal(
                    result[new_indices[::-1] if self.index_order == 'C' else new_indices],
                    data[indices[::-1] if self.index_order == 'C' else indices])

        def test_negative_direction_is_preserved_when_target_agrees(self):
            # Same negative source direction, but the target explicitly requests the negative axis, so no sign change,
            # no reversal and no origin shift may occur.
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., -3.]]),
                'space origin': np.array([10., 20., 30.]),
            }

            result, result_header = nrrd.reorient(data, header, 'RAI', self.index_order)

            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., -3.]]))
            np.testing.assert_allclose(result_header['space origin'], np.array([10., 20., 30.]))
            np.testing.assert_array_equal(result, data)

            for indices in [(0, 0, 0), (1, 2, 3), (0, 1, 2)]:
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, indices))

        def test_permutation_and_flip_combined(self):
            # Source axes: (x, -y, z). Target (z, y, x): z moves first, -y reverses, x moves last.
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., -2., 0.], [0., 0., 3.]]),
                'space origin': np.array([1., 2., 3.]),
            }

            result, result_header = nrrd.reorient(data, header, 'superior-anterior-right', self.index_order)

            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[0., 0., 3.], [0., 2., 0.], [1., 0., 0.]]))
            np.testing.assert_allclose(result_header['space origin'], np.array([1., -2., 3.]))

            for indices in [(0, 0, 0), (1, 2, 3), (0, 1, 2), (1, 0, 3)]:
                new_indices = (indices[2], 2 - indices[1], indices[0])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, new_indices))
                np.testing.assert_array_equal(
                    result[new_indices[::-1] if self.index_order == 'C' else new_indices],
                    data[indices[::-1] if self.index_order == 'C' else indices])

        def test_non_spatial_axis_is_left_in_place(self):
            # Header axes: (color, x, y, z). The color axis must never move or be reversed.
            data = self._array((3, 2, 3, 4))
            header = {
                'sizes': np.array([3, 2, 3, 4]),
                'space directions': np.array([
                    [NAN, NAN, NAN], [1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([0., 0., 0.]),
                'kinds': ['vector', 'domain', 'domain', 'domain'],
            }

            result, result_header = nrrd.reorient(data, header, ('inferior', 'anterior', 'left'), self.index_order)

            self.assertEqual(result.shape[0] if self.index_order == 'F' else result.shape[-1], 3)
            self.assertEqual(result_header['kinds'][0], 'vector')
            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[NAN, NAN, NAN], [0., 0., -3.], [0., 2., 0.], [-1., 0., 0.]]),
                equal_nan=True)

            for indices in [(0, 0, 0, 0), (1, 1, 2, 3), (2, 0, 1, 2)]:
                new_indices = (indices[0], 3 - indices[3], indices[2], 1 - indices[1])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, new_indices))
                array_index = new_indices[::-1] if self.index_order == 'C' else new_indices
                old_index = indices[::-1] if self.index_order == 'C' else indices
                np.testing.assert_array_equal(result[array_index], data[old_index])

        def test_non_spatial_axis_between_spatial_axes_stays_in_place(self):
            # Header axes: (x, color, y, z). Reordering spatial axes to (z, y, x) must leave the color axis in its
            # original slot (index 1) rather than moving it to the front.
            data = self._array((2, 3, 4, 5))
            header = {
                'sizes': np.array([2, 3, 4, 5]),
                'space directions': np.array([
                    [1., 0., 0.], [NAN, NAN, NAN], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([0., 0., 0.]),
                'kinds': ['domain', 'vector', 'domain', 'domain'],
            }

            result, result_header = nrrd.reorient(data, header, 'superior-anterior-right', self.index_order)

            self.assertEqual(result_header['kinds'][1], 'vector')
            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[0., 0., 3.], [NAN, NAN, NAN], [0., 2., 0.], [1., 0., 0.]]),
                equal_nan=True)

            for indices in [(0, 0, 0, 0), (1, 2, 3, 4), (0, 1, 2, 3)]:
                new_indices = (indices[3], indices[1], indices[2], indices[0])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, new_indices))
                array_index = new_indices[::-1] if self.index_order == 'C' else new_indices
                old_index = indices[::-1] if self.index_order == 'C' else indices
                np.testing.assert_array_equal(result[array_index], data[old_index])

        def test_vector_list_directions_representation_preserved(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': [np.array([1., 0., 0.]), np.array([0., 2., 0.]),
                                     np.array([0., 0., 3.])],
                'space origin': np.array([0., 0., 0.]),
            }

            _, result_header = nrrd.reorient(data, header, 'superior-anterior-right', self.index_order)

            self.assertIsInstance(result_header['space directions'], list)
            np.testing.assert_allclose(result_header['space directions'][0], np.array([0., 0., 3.]))
            np.testing.assert_allclose(result_header['space directions'][2], np.array([1., 0., 0.]))

        def test_explicit_vector_target_axes(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([0., 0., 0.]),
            }

            result, result_header = nrrd.reorient(
                data, header, [(0., 0., 1.), (0., 1., 0.), (-1., 0., 0.)], self.index_order)

            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[0., 0., 3.], [0., 2., 0.], [-1., 0., 0.]]))
            indices = (1, 2, 3)
            new_indices = (indices[2], indices[1], 1 - indices[0])
            np.testing.assert_allclose(world_coordinate(header, indices),
                                       world_coordinate(result_header, new_indices))

        def test_input_header_not_modified(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., -2., 0.], [0., 0., 3.]]),
                'space origin': np.array([1., 2., 3.]),
                'kinds': ['domain', 'domain', 'domain'],
            }
            original_directions = header['space directions'].copy()
            original_origin = header['space origin'].copy()
            original_kinds = list(header['kinds'])

            nrrd.reorient(data, header, 'RAS', self.index_order)

            np.testing.assert_array_equal(header['space directions'], original_directions)
            np.testing.assert_array_equal(header['space origin'], original_origin)
            self.assertEqual(header['kinds'], original_kinds)

        def test_round_trip_through_write_and_read(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., -2., 0.], [0., 0., 3.]]),
                'space origin': np.array([1., 2., 3.]),
                'encoding': 'raw',
            }

            result, result_header = nrrd.reorient(data, header, 'superior-anterior-right', self.index_order)
            written_header = dict(result_header)
            parsed = nrrd.writer._handle_header(result, written_header, self.index_order)
            self.assertEqual(list(parsed['sizes']), list(result_header['sizes']))

        def test_invalid_targets_raise(self):
            data = self._array((2, 3, 4))
            base_header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([0., 0., 0.]),
            }

            with self.assertRaises(NRRDError):
                # Wrong number of target axes.
                nrrd.reorient(data, base_header, 'RA', self.index_order)
            with self.assertRaises(NRRDError):
                # Collinear target axes.
                nrrd.reorient(data, base_header, ('right', 'left', 'superior'), self.index_order)
            with self.assertRaises(NRRDError):
                # Target axis not collinear with any source axis.
                nrrd.reorient(data, base_header,
                              [(1., 0., 0.), (0., 1., 0.), (1., 1., 0.)], self.index_order)
            with self.assertRaises(NRRDError):
                # Unknown direction name.
                nrrd.reorient(data, base_header, 'RQX', self.index_order)

        def test_missing_spatial_fields_raise(self):
            data = self._array((2, 3, 4))

            with self.assertRaises(NRRDError):
                nrrd.reorient(data, {'sizes': np.array([2, 3, 4])}, 'RAS', self.index_order)

            header_no_origin = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
            }
            with self.assertRaises(NRRDError):
                nrrd.reorient(data, header_no_origin, 'RAS', self.index_order)

        def test_invalid_index_order_raises(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([0., 0., 0.]),
            }
            with self.assertRaises(NRRDError):
                nrrd.reorient(data, header, 'RAS', 'X')

        def test_named_directions_follow_header_space(self):
            # The header declares the left-posterior-superior convention, so 'right' points along -x in world
            # coordinates and requires reversing the leftward source axis.
            data = self._array((2, 3, 4))
            header = {
                'space': 'left-posterior-superior',
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([5., 6., 7.]),
            }

            result, result_header = nrrd.reorient(data, header, 'right-anterior-superior', self.index_order)

            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[-1., 0., 0.], [0., -2., 0.], [0., 0., 3.]]))
            np.testing.assert_allclose(result_header['space origin'], np.array([6., 10., 7.]))

            for indices in [(0, 0, 0), (1, 2, 3), (0, 1, 2)]:
                new_indices = (1 - indices[0], 2 - indices[1], indices[2])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, new_indices))
                array_index = new_indices[::-1] if self.index_order == 'C' else new_indices
                old_index = indices[::-1] if self.index_order == 'C' else indices
                np.testing.assert_array_equal(result[array_index], data[old_index])

        def test_sheared_directions_survive_permutation(self):
            # Off-axis (sheared) directions are matched using explicit target vectors, preserving every component.
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0.5, 2., 0.], [0., 0., 3.]]),
                'space origin': np.array([1., 2., 3.]),
            }
            units = [vector / np.linalg.norm(vector) for vector in header['space directions']]

            result, result_header = nrrd.reorient(
                data, header, [units[2], units[0], units[1]], self.index_order)

            np.testing.assert_allclose(
                result_header['space directions'],
                np.array([[0., 0., 3.], [1., 0., 0.], [0.5, 2., 0.]]))
            for indices in [(0, 0, 0), (1, 2, 3), (0, 1, 2)]:
                new_indices = (indices[2], indices[0], indices[1])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(result_header, new_indices))

        def test_write_and_read_round_trip_preserves_world_coordinates(self):
            data = self._array((2, 3, 4))
            header = {
                'sizes': np.array([2, 3, 4]),
                'space directions': np.array([[1., 0., 0.], [0., -2., 0.], [0., 0., 3.]]),
                'space origin': np.array([1., 2., 3.]),
                'encoding': 'raw',
            }

            result, result_header = nrrd.reorient(data, header, 'superior-anterior-right', self.index_order)

            buffer = io.BytesIO()
            nrrd.write(buffer, result, result_header, index_order=self.index_order)
            buffer.seek(0)
            parsed_header = nrrd.read_header(buffer)
            parsed_data = nrrd.read_data(parsed_header, buffer, index_order=self.index_order)

            np.testing.assert_allclose(parsed_header['space directions'], result_header['space directions'])
            np.testing.assert_allclose(parsed_header['space origin'], result_header['space origin'])
            np.testing.assert_array_equal(parsed_data, result)

            for indices in [(0, 0, 0), (1, 2, 3)]:
                new_indices = (indices[2], 2 - indices[1], indices[0])
                np.testing.assert_allclose(world_coordinate(header, indices),
                                           world_coordinate(parsed_header, new_indices))


class TestVoxelBucketConvergence(unittest.TestCase):
    def test_two_stations_converge_on_same_voxel_bucket(self):
        # Ground-truth volume in the canonical (x, y, z) convention.
        ground_truth = np.arange(4 * 5 * 6, dtype=float).reshape(4, 5, 6)
        origin = np.array([10., 20., 30.])
        directions = np.array([[1., 0., 0.], [0., 2., 0.], [0., 0., 3.]])
        sizes = np.array([4, 5, 6])

        # Station A stores axes as (z, x, y): a pure permutation with the same origin.
        data_a = np.transpose(ground_truth, (2, 0, 1))
        header_a = {
            'sizes': np.array([6, 4, 5]),
            'space directions': np.array([directions[2], directions[0], directions[1]]),
            'space origin': origin.copy(),
        }

        # Station B stores axes as (-x, y, z): x is reversed, so the origin shifts to the other corner.
        data_b = np.flip(ground_truth, axis=0)
        header_b = {
            'sizes': sizes.copy(),
            'space directions': np.array([-directions[0], directions[1], directions[2]]),
            'space origin': origin + directions[0] * (sizes[0] - 1),
        }

        result_a, header_a_out = nrrd.reorient(data_a, header_a, 'RAS', 'F')
        result_b, header_b_out = nrrd.reorient(data_b, header_b, 'RAS', 'F')

        # Both stations must land in the same voxel bucket, reproducing the ground truth exactly.
        np.testing.assert_array_equal(result_a, ground_truth)
        np.testing.assert_array_equal(result_b, ground_truth)
        np.testing.assert_allclose(header_a_out['space directions'], directions)
        np.testing.assert_allclose(header_b_out['space directions'], directions)
        np.testing.assert_allclose(header_a_out['space origin'], origin)
        np.testing.assert_allclose(header_b_out['space origin'], origin)


class TestReorientFunctionsFortran(Abstract.TestReorientFunctions):
    index_order = 'F'


class TestReorientFunctionsC(Abstract.TestReorientFunctions):
    index_order = 'C'


if __name__ == '__main__':
    unittest.main()
