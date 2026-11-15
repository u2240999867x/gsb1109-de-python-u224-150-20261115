import os
import unittest
from tempfile import TemporaryDirectory

import numpy as np

import nrrd
from nrrd.errors import NRRDError

# 90 degree rotation about the world z axis: columns are the measurement frame basis vectors in world coordinates
ROTATION_90_Z = np.array([[0., -1., 0.],
                          [1., 0., 0.],
                          [0., 0., 1.]])


class TestToWorldComponents(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(42)
        # 3-vector field on a 2x3x4 grid, component axis first (Fortran order)
        self.data = rng.standard_normal((3, 2, 3, 4))
        self.header = {'dimension': 4,
                       'sizes': np.array([3, 2, 3, 4]),
                       'kinds': ['3-vector', 'domain', 'domain', 'domain'],
                       'space': 'right-anterior-superior',
                       'space directions': np.array([[np.nan, np.nan, np.nan],
                                                     [1.5, 0., 0.],
                                                     [0., 1.5, 0.],
                                                     [0., 0., 1.]]),
                       'space origin': np.array([0., 0., 0.]),
                       'measurement frame': ROTATION_90_Z.copy()}

    def test_world_components(self):
        components = nrrd.to_world_components(self.data, self.header)

        np.testing.assert_allclose(components.world, np.einsum('ij,j...->i...', ROTATION_90_Z, self.data))
        np.testing.assert_array_equal(components.original, self.data)
        self.assertEqual(components.world.shape, self.data.shape)

    def test_world_components_known_vector(self):
        data = np.zeros((3, 2, 2, 2))
        data[0] = 1.  # unit vector along the first measurement frame axis everywhere

        components = nrrd.to_world_components(data, self.header)

        # First measurement frame basis vector expressed in world coordinates
        np.testing.assert_allclose(components.world[0], 0.)
        np.testing.assert_allclose(components.world[1], 1.)
        np.testing.assert_allclose(components.world[2], 0.)

    def test_identity_measurement_frame(self):
        self.header['measurement frame'] = np.eye(3)

        components = nrrd.to_world_components(self.data, self.header)

        np.testing.assert_allclose(components.world, self.data)

    def test_component_axis_not_first(self):
        # Component axis last: sizes ordered (x, y, z, component) in the header
        data = np.moveaxis(self.data, 0, -1)
        header = dict(self.header)
        header['sizes'] = np.array([2, 3, 4, 3])
        header['kinds'] = ['domain', 'domain', 'domain', '3-vector']
        header['space directions'] = np.array([[1.5, 0., 0.],
                                               [0., 1.5, 0.],
                                               [0., 0., 1.],
                                               [np.nan, np.nan, np.nan]])

        components = nrrd.to_world_components(data, header)

        expected = np.einsum('ij,...j->...i', ROTATION_90_Z, data)
        np.testing.assert_allclose(components.world, expected)
        self.assertEqual(components.world.shape, data.shape)

    def test_index_order_is_axis_reordering_not_basis_transform(self):
        # The same field read with index_order='C' is the transposed array. Reordering axes must not change the
        # components: world components of the C-order array equal the transpose of the F-order world components.
        components_f = nrrd.to_world_components(self.data, self.header, index_order='F')
        components_c = nrrd.to_world_components(self.data.T, self.header, index_order='C')

        np.testing.assert_allclose(components_c.world, components_f.world.T)
        np.testing.assert_array_equal(components_c.original, self.data.T)

    def test_space_directions_vector_list(self):
        # 'double vector list' representation: non-spatial axes are None instead of a NaN row
        header = dict(self.header)
        header['space directions'] = [None, np.array([1.5, 0., 0.]), np.array([0., 1.5, 0.]),
                                      np.array([0., 0., 1.])]

        components = nrrd.to_world_components(self.data, header)

        np.testing.assert_allclose(components.world, np.einsum('ij,j...->i...', ROTATION_90_Z, self.data))

    def test_missing_space_directions(self):
        # Space directions are not needed for vector components at all
        header = dict(self.header)
        del header['space directions']

        components = nrrd.to_world_components(self.data, header)

        np.testing.assert_allclose(components.world, np.einsum('ij,j...->i...', ROTATION_90_Z, self.data))

    def test_scalar_batch_is_not_a_vector(self):
        # A batch of scalar volumes (kind 'scalar') must not be mistaken for a single-component vector
        header = dict(self.header)
        header['kinds'] = ['scalar', 'domain', 'domain', 'domain']

        with self.assertRaisesRegex(NRRDError, 'No vector component axis'):
            nrrd.to_world_components(self.data, header)

    def test_missing_kinds(self):
        header = dict(self.header)
        del header['kinds']

        with self.assertRaisesRegex(NRRDError, "missing 'kinds'"):
            nrrd.to_world_components(self.data, header)

    def test_missing_measurement_frame(self):
        header = dict(self.header)
        del header['measurement frame']

        with self.assertRaisesRegex(NRRDError, "missing 'measurement frame'"):
            nrrd.to_world_components(self.data, header)

    def test_multiple_vector_axes(self):
        header = dict(self.header)
        header['kinds'] = ['3-vector', 'domain', 'domain', 'vector']

        with self.assertRaisesRegex(NRRDError, 'Multiple vector component axes'):
            nrrd.to_world_components(self.data, header)

    def test_kind_size_mismatch(self):
        header = dict(self.header)
        header['kinds'] = ['4-vector', 'domain', 'domain', 'domain']

        with self.assertRaisesRegex(NRRDError, 'expects 4 components'):
            nrrd.to_world_components(self.data, header)

    def test_measurement_frame_shape_mismatch(self):
        header = dict(self.header)
        header['measurement frame'] = np.eye(2)

        with self.assertRaisesRegex(NRRDError, 'does not match the number of components'):
            nrrd.to_world_components(self.data, header)

    def test_measurement_frame_with_nan(self):
        header = dict(self.header)
        header['measurement frame'] = np.full((3, 3), np.nan)

        with self.assertRaisesRegex(NRRDError, 'NaN'):
            nrrd.to_world_components(self.data, header)

    def test_component_axis_with_space_direction(self):
        # The component axis is not spatial: its space directions row must be none (NaN)
        header = dict(self.header)
        header['space directions'] = np.array([[1., 0., 0.],
                                               [1.5, 0., 0.],
                                               [0., 1.5, 0.],
                                               [0., 0., 1.]])

        with self.assertRaisesRegex(NRRDError, 'must be none'):
            nrrd.to_world_components(self.data, header)

    def test_space_directions_row_count_mismatch(self):
        # Fewer spatial rows than measurement frame dimensions: degenerate matrix must not be padded
        header = dict(self.header)
        header['space directions'] = np.array([[np.nan, np.nan, np.nan],
                                               [1.5, 0., 0.],
                                               [0., 1.5, 0.],
                                               [np.nan, np.nan, np.nan]])

        with self.assertRaisesRegex(NRRDError, 'spatial axes'):
            nrrd.to_world_components(self.data, header)

    def test_invalid_index_order(self):
        with self.assertRaisesRegex(NRRDError, 'Invalid index order'):
            nrrd.to_world_components(self.data, self.header, index_order='A')

    def test_write_read_roundtrip(self):
        with TemporaryDirectory() as temp_dir:
            filename = os.path.join(temp_dir, 'vector_field.nrrd')
            nrrd.write(filename, self.data, self.header, index_order='F')

            data_f, header_f = nrrd.read(filename, index_order='F')
            data_c, header_c = nrrd.read(filename, index_order='C')

        components_f = nrrd.to_world_components(data_f, header_f, index_order='F')
        components_c = nrrd.to_world_components(data_c, header_c, index_order='C')

        np.testing.assert_allclose(components_f.original, self.data)
        np.testing.assert_allclose(components_f.world,
                                   np.einsum('ij,j...->i...', ROTATION_90_Z, self.data), rtol=1e-12)
        np.testing.assert_allclose(components_c.world, components_f.world.T)


if __name__ == '__main__':
    unittest.main()
