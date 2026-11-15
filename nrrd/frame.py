"""Measurement frame transformations for vector (multi-component) NRRD data.

The NRRD ``measurement frame`` field defines the basis in which the components of a vector-valued dataset are
expressed. The rows of the matrix are the measurement-frame basis vectors given in world (``space``) coordinates.
This module converts the raw, on-disk components between the measurement frame and the world frame.

Two conceptually different operations are involved and they are kept separate:

1. **Axis rearrangement** (:func:`numpy.moveaxis` / transposition): locating the component (``vector``) axis of the
   NumPy array and moving it into a known position. This only permutes array axes and never changes any values. The
   position of the component axis depends on the ``index_order`` used when reading the file ('F' or 'C').
2. **Vector basis change** (matrix multiplication): expressing the same physical vector in another basis, i.e.
   ``v_world = M @ v_measurement`` where ``M`` is the measurement frame matrix. This combines component values
   linearly and is only valid when ``M`` is invertible.

A degenerate (singular or non-finite) measurement frame cannot describe a basis, so it is rejected explicitly.
The inverse frame is never used to fabricate values for such data. Scalar datasets (no ``vector`` axis) are never
treated as single-component vectors.
"""

from typing import Tuple

import numpy as np
import numpy.typing as npt

from nrrd.errors import NRRDError
from nrrd.types import IndexOrder, NRRDHeader


def find_vector_axis(header: NRRDHeader) -> int:
    """Return the index of the vector (component) axis in NRRD header (Fortran) order.

    Header fields such as ``kinds`` and ``sizes`` are ordered from fastest-varying to slowest-varying axis, i.e.
    Fortran order. The returned index refers to that ordering, not necessarily to the axes of an array read with
    ``index_order='C'``.

    Parameters
    ----------
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Parsed NRRD header obtained from :meth:`nrrd.read_header` or :meth:`nrrd.read`.

    Returns
    -------
    axis : :class:`int`
        Index of the vector axis in Fortran/header order.

    Raises
    ------
    NRRDError
        If the header does not describe a vector-valued dataset, or if it describes more than one vector axis.
    """

    kinds = header.get('kinds')

    if kinds is not None:
        vector_axes = [axis for axis, kind in enumerate(kinds) if kind == 'vector']

        if len(vector_axes) > 1:
            raise NRRDError(f'Expected a single vector axis, but found {len(vector_axes)} in kinds: {list(kinds)}')

        if len(vector_axes) == 1:
            return vector_axes[0]

        # An explicit kinds field without a 'vector' kind means this is not a vector field. Do not guess from the
        # array shape, otherwise a scalar batch could be mistaken for a single-component vector.
        raise NRRDError(f"No axis with kind 'vector' found in kinds: {list(kinds)}")

    # Without a kinds field, fall back to the single non-spatial axis marked in 'space directions'.
    space_directions = header.get('space directions')

    if isinstance(space_directions, np.ndarray):
        non_spatial = ~np.isfinite(space_directions).all(axis=1)
        non_spatial_axes = np.flatnonzero(non_spatial).tolist()
    elif space_directions is not None:
        non_spatial_axes = [axis for axis, direction in enumerate(space_directions) if direction is None]
    else:
        non_spatial_axes = []

    if len(non_spatial_axes) == 1:
        return non_spatial_axes[0]

    raise NRRDError('Unable to determine the vector axis: provide a kinds field containing a single "vector" axis')


def _array_vector_axis(header: NRRDHeader, data: npt.NDArray, index_order: IndexOrder) -> Tuple[int, int]:
    """Return the vector axis index in both header/Fortran order and the given array order."""

    header_axis = find_vector_axis(header)

    if header_axis >= data.ndim:
        raise NRRDError(f'Vector axis index {header_axis} is out of bounds for data with {data.ndim} dimension(s)')

    if index_order == 'F':
        array_axis = header_axis
    elif index_order == 'C':
        array_axis = data.ndim - 1 - header_axis
    else:
        raise NRRDError('Invalid index order')

    return header_axis, array_axis


def _validate_measurement_frame(frame: npt.NDArray, component_count: int) -> npt.NDArray:
    """Validate that the measurement frame is a finite, square, invertible basis matrix.

    A degenerate matrix is rejected instead of being inverted, because a singular frame has no inverse and any
    world/measurement components derived from a pseudo-inverse would be fabricated values.
    """

    if not isinstance(frame, np.ndarray):
        frame = np.asarray(frame, dtype=float)

    frame = frame.astype(float, copy=False)

    if frame.ndim != 2 or frame.shape[0] != frame.shape[1]:
        raise NRRDError(f'Measurement frame must be a square 2D matrix, but got shape {frame.shape}')

    if frame.shape[0] != component_count:
        raise NRRDError(f'Measurement frame has shape {frame.shape}, but the vector axis has '
                        f'{component_count} component(s)')

    if not np.isfinite(frame).all():
        raise NRRDError('Measurement frame contains NaN or infinite values, so it does not define a valid basis')

    if np.linalg.matrix_rank(frame) < frame.shape[0]:
        raise NRRDError('Measurement frame is singular (rank deficient) and cannot be inverted; '
                        'world components cannot be determined from a degenerate direction matrix')

    return frame


def _apply_basis_matrix(data: npt.NDArray, matrix: npt.NDArray, array_axis: int) -> npt.NDArray:
    """Apply a basis change ``matrix`` along the component axis ``array_axis``.

    The array axis is moved to the last position purely by rearrangement, the matrix is applied as a linear
    combination of components, and the axis is moved back. No data values are altered by the rearrangement.
    """

    # Axis rearrangement only: no arithmetic is performed on the component values.
    rearranged = np.moveaxis(data, array_axis, -1)

    # Vector basis change: result[..., j] = sum_i rearranged[..., i] * matrix[j, i]
    transformed = rearranged @ matrix.T

    return np.moveaxis(transformed, -1, array_axis)


def world_components(data: npt.NDArray, header: NRRDHeader,
                     index_order: IndexOrder = 'F') -> npt.NDArray:
    """Express vector components in the world (space) frame.

    The data returned by :meth:`nrrd.read` holds components measured in the per-file measurement frame. Given the
    measurement frame ``M`` (rows are the measurement basis vectors in world coordinates), the world components are
    computed as ``v_world = M @ v_measurement``.

    The input array is not modified; a new array is returned. The original, on-disk components remain available as
    the array returned by :meth:`nrrd.read`.

    Parameters
    ----------
    data : :class:`numpy.ndarray`
        Vector-valued data read from an NRRD file, containing a ``vector`` axis.
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Parsed NRRD header containing a valid ``measurement frame`` field.
    index_order : {'C', 'F'}, optional
        Index order of :obj:`data`, matching the value passed to :meth:`nrrd.read`. Defaults to ``'F'``.

    Returns
    -------
    world_data : :class:`numpy.ndarray`
        Copy of :obj:`data` with the component axis expressed in world coordinates. The result is floating point.

    Raises
    ------
    NRRDError
        If the header has no measurement frame, the data is not vector-valued (e.g. a scalar batch), the frame shape
        does not match the number of components, or the frame is non-finite or singular.
    """

    if 'measurement frame' not in header:
        raise NRRDError("Header has no 'measurement frame' field; world components are not defined for this data")

    header_axis, array_axis = _array_vector_axis(header, data, index_order)

    component_count = int(np.asarray(header['sizes'])[header_axis])
    if data.shape[array_axis] != component_count:
        raise NRRDError(f'Vector axis size {data.shape[array_axis]} does not match sizes entry {component_count}')

    frame = _validate_measurement_frame(header['measurement frame'], component_count)

    return _apply_basis_matrix(np.array(data, dtype=float, copy=True), frame, array_axis)


def measurement_components(world_data: npt.NDArray, header: NRRDHeader,
                           index_order: IndexOrder = 'F') -> npt.NDArray:
    """Express world-frame vector components back in the measurement frame.

    This is the inverse of :func:`world_components`: ``v_measurement = M^{-1} @ v_world``. The frame is validated as
    invertible before its inverse is used, so a degenerate frame raises an error rather than producing fabricated
    components.

    Parameters
    ----------
    world_data : :class:`numpy.ndarray`
        Vector-valued data whose components are expressed in world coordinates, e.g. the output of
        :func:`world_components`.
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Parsed NRRD header containing a valid ``measurement frame`` field.
    index_order : {'C', 'F'}, optional
        Index order of :obj:`world_data`. Defaults to ``'F'``.

    Returns
    -------
    measurement_data : :class:`numpy.ndarray`
        Components expressed in the measurement frame, with the same shape and axis ordering as :obj:`world_data`.

    Raises
    ------
    NRRDError
        If the header has no measurement frame, the data is not vector-valued, or the frame is not a valid invertible
        basis matrix.
    """

    if 'measurement frame' not in header:
        raise NRRDError("Header has no 'measurement frame' field; measurement components are not defined for this data")

    header_axis, array_axis = _array_vector_axis(header, world_data, index_order)

    component_count = int(np.asarray(header['sizes'])[header_axis])
    if world_data.shape[array_axis] != component_count:
        raise NRRDError(f'Vector axis size {world_data.shape[array_axis]} does not match sizes entry '
                        f'{component_count}')

    frame = _validate_measurement_frame(header['measurement frame'], component_count)

    inverse_frame = np.linalg.inv(frame)

    return _apply_basis_matrix(np.array(world_data, dtype=float, copy=True), inverse_frame, array_axis)
