"""End-to-end demo: convert batches of three-component vector fields between their per-file measurement frame and
world coordinates.

Run with: python3 measurement_frame_demo.py
"""

import tempfile
import os

import numpy as np

import nrrd


def write_vector_field(path, frame, raw_components):
    # Header fields are in Fortran order: the first (fastest-varying) axis is the vector axis.
    header = {
        'space': 'right-anterior-superior',
        'kinds': ['vector', 'domain', 'domain'],
        'sizes': np.array([3, 2, 2]),
        'space directions': np.array([[np.nan, np.nan, np.nan],
                                      [1.0, 0.0, 0.0],
                                      [0.0, 1.0, 0.0]]),
        'measurement frame': frame,
        'encoding': 'raw',
    }
    nrrd.write(path, raw_components, header, index_order='F')


def main():
    raw = np.array([
        [[1.0, 2.0], [3.0, 4.0]],
        [[5.0, 6.0], [7.0, 8.0]],
        [[9.0, 10.0], [11.0, 12.0]],
    ])

    # Each file in the batch carries its own independent measurement frame.
    frames = {
        'field_a.nrrd': np.array([[2.0, 0.0, 0.0],
                                  [0.0, 1.0, 1.0],
                                  [0.0, 0.0, 3.0]]),
        'field_b.nrrd': np.array([[0.0, 1.0, 0.0],
                                  [-1.0, 0.0, 0.0],
                                  [0.0, 0.0, 1.0]]),
    }

    with tempfile.TemporaryDirectory() as directory:
        for name, frame in frames.items():
            write_vector_field(os.path.join(directory, name), frame, raw)

        print('=== Per-file measurement frames ===')
        for name, frame in frames.items():
            data, header = nrrd.read(os.path.join(directory, name), index_order='F')

            world = nrrd.world_components(data, header, index_order='F')
            recovered = nrrd.measurement_components(world, header, index_order='F')

            print(f'\n{name}')
            print('measurement frame M =\n', frame)
            print('raw (measurement-frame) components =\n', data)
            print('world components M @ v =\n', world)
            print('round-trip back to raw components:', np.allclose(recovered, data))

        print('\n=== Degenerate direction matrix is rejected ===')
        singular_frame = np.array([[1.0, 2.0, 3.0],
                                   [2.0, 4.0, 6.0],
                                   [7.0, 8.0, 9.0]])
        singular_header = {
            'kinds': ['vector', 'domain', 'domain'],
            'sizes': np.array([3, 2, 2]),
            'measurement frame': singular_frame,
        }
        try:
            nrrd.world_components(raw, singular_header, index_order='F')
        except nrrd.NRRDError as error:
            print(f'NRRDError as expected: {error}')

        print('\n=== Scalar batch is not a one-component vector ===')
        scalar_header = {
            'kinds': ['domain', 'domain', 'domain'],
            'sizes': np.array([1, 4, 5]),
            'measurement frame': np.eye(3),
        }
        try:
            nrrd.world_components(np.zeros((1, 4, 5)), scalar_header, index_order='F')
        except nrrd.NRRDError as error:
            print(f'NRRDError as expected: {error}')


if __name__ == '__main__':
    main()
