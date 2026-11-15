import io
import unittest

import numpy as np

import nrrd


class TestMeasurementFrame(unittest.TestCase):
    def setUp(self):
        # Measurement frame: rows are the measurement basis vectors expressed in world coordinates.
        # Deliberately non-orthogonal so that a plain axis permutation cannot reproduce the result.
        self.frame = np.array([[2.0, 0.0, 0.0],
                               [0.0, 1.0, 1.0],
                               [0.0, 0.0, 3.0]])

        self.header = {
            'space': 'right-anterior-superior',
            'kinds': ['vector', 'domain', 'domain'],
            'sizes': np.array([3, 2, 2]),
            'space directions': np.array([[np.nan, np.nan, np.nan],
                                          [1.0, 0.0, 0.0],
                                          [0.0, 1.0, 0.0]]),
            'measurement frame': self.frame,
        }

        # Raw components as stored on disk: components along measurement-frame basis vectors.
        self.raw = np.array([
            [[1.0, 2.0], [3.0, 4.0]],
            [[5.0, 6.0], [7.0, 8.0]],
            [[9.0, 10.0], [11.0, 12.0]],
        ])

    def test_find_vector_axis(self):
        self.assertEqual(nrrd.find_vector_axis(self.header), 0)

    def test_find_vector_axis_fallback_to_space_directions(self):
        header = dict(self.header)
        del header['kinds']
        self.assertEqual(nrrd.find_vector_axis(header), 0)

    def test_find_vector_axis_fallback_list_form(self):
        header = {
            'kinds': ['domain', 'domain', 'vector'],
            'sizes': np.array([2, 2, 3]),
            'space directions': [np.array([1.0, 0.0, 0.0]),
                                 np.array([0.0, 1.0, 0.0]),
                                 None],
            'measurement frame': self.frame,
        }
        self.assertEqual(nrrd.find_vector_axis(header), 2)

    def test_multiple_vector_axes_rejected(self):
        header = dict(self.header)
        header['kinds'] = ['vector', 'vector', 'domain']
        with self.assertRaisesRegex(nrrd.NRRDError, 'single vector axis'):
            nrrd.find_vector_axis(header)

    def test_world_components_apply_basis_change(self):
        world = nrrd.world_components(self.raw, self.header, index_order='F')

        # Hand-checked basis change: v_world = M @ v_measurement for every spatial location.
        expected = np.tensordot(self.frame, self.raw, axes=([1], [0]))
        np.testing.assert_allclose(world, expected)

        # Spot-check individual components to make sure values are linearly combined, not merely reordered.
        # world[1] = raw[1] + raw[2], which no axis rearrangement can produce.
        np.testing.assert_allclose(world[1], self.raw[1] + self.raw[2])

    def test_world_components_does_not_mutate_input(self):
        snapshot = self.raw.copy()
        nrrd.world_components(self.raw, self.header, index_order='F')
        np.testing.assert_array_equal(self.raw, snapshot)

    def test_identity_frame_preserves_components(self):
        header = dict(self.header)
        header['measurement frame'] = np.eye(3)
        world = nrrd.world_components(self.raw, header, index_order='F')
        np.testing.assert_allclose(world, self.raw)
        self.assertEqual(world.dtype, np.float64)

    def test_round_trip_recovers_raw_components(self):
        world = nrrd.world_components(self.raw, self.header, index_order='F')
        recovered = nrrd.measurement_components(world, self.header, index_order='F')
        np.testing.assert_allclose(recovered, self.raw, atol=1e-12)

    def test_axis_rearrangement_differs_from_basis_change_in_c_order(self):
        # C-order array is the transpose of the F-order array: component axis moves from first to last.
        raw_c = self.raw.T
        self.assertEqual(raw_c.shape, (2, 2, 3))

        world_f = nrrd.world_components(self.raw, self.header, index_order='F')
        world_c = nrrd.world_components(raw_c, self.header, index_order='C')

        # Same logical field after undoing the index-order transposition.
        np.testing.assert_allclose(world_c.T, world_f)

        # Rearrangement alone (moveaxis) only permutes positions and preserves every value, unlike the basis change,
        # which creates new values through linear combinations of components.
        rearranged = np.moveaxis(raw_c, -1, 0)
        np.testing.assert_array_equal(np.sort(rearranged.ravel()), np.sort(raw_c.ravel()))
        with self.assertRaises(AssertionError):
            np.testing.assert_allclose(world_c, raw_c)

    def test_singular_frame_rejected_without_inversion(self):
        header = dict(self.header)
        header['measurement frame'] = np.array([[1.0, 2.0, 3.0],
                                                [2.0, 4.0, 6.0],
                                                [7.0, 8.0, 9.0]])
        for function in (nrrd.world_components, nrrd.measurement_components):
            with self.assertRaisesRegex(nrrd.NRRDError, 'singular'):
                function(self.raw, header, index_order='F')

    def test_non_finite_frame_rejected(self):
        header = dict(self.header)
        header['measurement frame'] = self.frame.copy()
        header['measurement frame'][0, 0] = np.nan
        with self.assertRaisesRegex(nrrd.NRRDError, 'NaN or infinite'):
            nrrd.world_components(self.raw, header, index_order='F')

    def test_non_square_frame_rejected(self):
        header = dict(self.header)
        header['measurement frame'] = np.zeros((3, 2))
        with self.assertRaisesRegex(nrrd.NRRDError, 'square'):
            nrrd.world_components(self.raw, header, index_order='F')

    def test_frame_component_count_mismatch_rejected(self):
        header = dict(self.header)
        header['measurement frame'] = np.eye(2)
        with self.assertRaisesRegex(nrrd.NRRDError, 'component'):
            nrrd.world_components(self.raw, header, index_order='F')

    def test_missing_measurement_frame_rejected(self):
        header = dict(self.header)
        del header['measurement frame']
        with self.assertRaisesRegex(nrrd.NRRDError, 'measurement frame'):
            nrrd.world_components(self.raw, header, index_order='F')

    def test_scalar_batch_not_treated_as_single_component_vector(self):
        # A scalar batch with a size-one axis: neither the size-one axis nor anything else implies a vector.
        scalar_header = {
            'kinds': ['domain', 'domain', 'domain'],
            'sizes': np.array([1, 4, 5]),
            'space directions': np.array([[1.0, 0.0, 0.0],
                                          [0.0, 1.0, 0.0],
                                          [0.0, 0.0, 1.0]]),
            'measurement frame': np.eye(3),
        }
        scalar_data = np.zeros((1, 4, 5))

        with self.assertRaisesRegex(nrrd.NRRDError, "kind 'vector'"):
            nrrd.find_vector_axis(scalar_header)
        with self.assertRaises(nrrd.NRRDError):
            nrrd.world_components(scalar_data, scalar_header, index_order='F')

    def test_one_dimensional_scalar_not_vector(self):
        scalar_header = {
            'kinds': ['domain'],
            'sizes': np.array([1]),
            'measurement frame': np.eye(1),
        }
        with self.assertRaises(nrrd.NRRDError):
            nrrd.world_components(np.ones((1,)), scalar_header, index_order='F')

    def test_end_to_end_write_read_and_transform(self):
        header = dict(self.header)
        header['encoding'] = 'raw'
        buffer = io.BytesIO()
        nrrd.write(buffer, self.raw, header, index_order='F')
        buffer.seek(0)
        read_header = nrrd.read_header(buffer)
        read_data = nrrd.read_data(read_header, buffer, index_order='F')

        world = nrrd.world_components(read_data, read_header, index_order='F')
        expected = np.tensordot(self.frame, self.raw, axes=([1], [0]))
        np.testing.assert_allclose(world, expected)

    def test_end_to_end_c_order(self):
        header = dict(self.header)
        header['encoding'] = 'raw'
        buffer = io.BytesIO()
        nrrd.write(buffer, self.raw, header, index_order='F')
        buffer.seek(0)
        read_header = nrrd.read_header(buffer)
        read_data = nrrd.read_data(read_header, buffer, index_order='C')

        self.assertEqual(read_data.shape, (2, 2, 3))
        world_c = nrrd.world_components(read_data, read_header, index_order='C')
        world_f = nrrd.world_components(self.raw, self.header, index_order='F')
        np.testing.assert_allclose(world_c.T, world_f)


if __name__ == '__main__':
    unittest.main()
