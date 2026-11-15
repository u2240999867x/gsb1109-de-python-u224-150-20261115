from typing import NamedTuple, Optional

import numpy as np
import numpy.typing as npt

from nrrd.errors import NRRDError
from nrrd.types import IndexOrder, NRRDHeader

VECTOR_KINDS = frozenset(['vector', '2-vector', '3-vector', '4-vector'])
"""NRRD axis kinds that identify a vector component axis

Only axes explicitly marked with one of these kinds in the 'kinds' header field are treated as vector component
axes. In particular, axes of kind 'scalar' (e.g. a batch of scalar volumes stacked along an extra axis) are never
interpreted as single-component vectors.
"""

_VECTOR_KIND_SIZES = {'2-vector': 2, '3-vector': 3, '4-vector': 4}


class WorldComponents(NamedTuple):
    """Vector field components in world coordinates and in the original measurement frame

    Attributes:
        world: Components expressed in world (space) coordinates, computed as
            ``measurement frame @ original`` for every sample. The array layout (shape and axis order) is
            identical to the input data, only the components along the vector axis are transformed.
        original: Components as stored in the file, i.e. expressed in the measurement frame. This is the input
            array itself, unchanged.
    """

    world: npt.NDArray
    original: npt.NDArray


def _optional_matrix(value) -> np.ndarray:
    """Normalize a per-axis matrix field to a 2D float array with NaN rows for non-spatial axes.

    Handles both representations of fields such as 'space directions': a matrix where non-spatial axes are rows
    of NaN ('double matrix') and a list of optional vectors where non-spatial axes are :obj:`None`
    ('double vector list').
    """

    if isinstance(value, np.ndarray):
        return value.astype(float)

    vectors = [None if vector is None else np.asarray(vector, dtype=float) for vector in value]
    width = next((vector.size for vector in vectors if vector is not None), 0)

    matrix = np.full((len(vectors), width), np.nan)
    for index, vector in enumerate(vectors):
        if vector is not None:
            matrix[index] = vector

    return matrix


def _validate_space_directions(header: NRRDHeader, component_axis: int, space_dimension: int) -> None:
    """Check 'space directions' for consistency without ever inverting the (degenerate) matrix.

    The 'space directions' matrix is degenerate by design: non-spatial axes (such as the vector component axis
    itself) are represented by rows of NaN, so the full matrix cannot be inverted or padded with made-up values.
    Instead, the spatial rows are selected directly and only consistency checks are performed.
    """

    if 'space directions' not in header:
        return

    directions = _optional_matrix(header['space directions'])

    if directions.shape[0] != header.get('dimension', directions.shape[0]):
        raise NRRDError(f'Number of space directions rows ({directions.shape[0]}) does not match dimension '
                        f'({header["dimension"]})')

    non_spatial = np.isnan(directions).all(axis=1)
    partially_nan = np.isnan(directions).any(axis=1) & ~non_spatial
    if partially_nan.any():
        raise NRRDError('Space directions contain a partially NaN row')

    if not non_spatial[component_axis]:
        raise NRRDError('Space directions row of the vector component axis must be none (NaN), the component '
                        'axis is not a spatial axis')

    spatial = directions[~non_spatial]
    if spatial.shape[0] != space_dimension:
        raise NRRDError(f'Number of spatial axes ({spatial.shape[0]}) does not match the measurement frame '
                        f'dimension ({space_dimension})')


def to_world_components(data: npt.NDArray, header: NRRDHeader, index_order: IndexOrder = 'F') -> WorldComponents:
    """Transform vector field components from the measurement frame into world (space) coordinates

    Per the NRRD specification, the components of a vector field are expressed in the measurement frame. The
    world-coordinate components are obtained by multiplying the components with the 'measurement frame' matrix,
    whose columns are the measurement frame basis vectors expressed in world coordinates:

    ``v_world = measurement_frame @ v_measurement``

    This is a vector basis transformation: it only mixes the components along the vector component axis (the axis
    whose kind is one of :data:`VECTOR_KINDS`). It is independent of the axis ordering of the array — passing the
    same field with a different :obj:`index_order` (i.e. a transposed array) yields the same world components,
    just transposed accordingly. Axis reordering never changes the components themselves.

    Notes:
        * The vector component axis is identified solely through the 'kinds' header field. Data without a vector
          kind (e.g. a batch of scalar volumes) is rejected rather than guessed to be a single-component vector.
        * The 'space directions' matrix is degenerate when non-spatial axes are present (rows of NaN). It is
          never inverted or padded with substitute values; only its spatial rows are used, and only for
          consistency checks. Vector components do not depend on 'space directions' or 'space origin' at all.

    Parameters
    ----------
    data : :class:`numpy.ndarray`
        Vector field data with exactly one vector component axis
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Parsed NRRD header containing 'kinds' and 'measurement frame' fields
    index_order : {'C', 'F'}, optional
        Index order of :obj:`data`, see :meth:`nrrd.read`. Determines which array axis corresponds to which
        header axis.

    Returns
    -------
    components : :class:`WorldComponents`
        Named tuple with the world-coordinate components and the original measurement-frame components
    """

    if index_order not in ['F', 'C']:
        raise NRRDError('Invalid index order')

    data = np.asarray(data)

    dimension = header.get('dimension', data.ndim)
    if data.ndim != dimension:
        raise NRRDError(f'Dimension of data ({data.ndim}) does not match header dimension ({dimension})')

    kinds: Optional[list] = header.get('kinds')
    if kinds is None:
        raise NRRDError("Header is missing 'kinds' field, cannot identify the vector component axis. Data "
                        'without an explicit vector kind is not treated as vector data')

    if len(kinds) != dimension:
        raise NRRDError(f'Number of kinds ({len(kinds)}) does not match dimension ({dimension})')

    vector_axes = [axis for axis, kind in enumerate(kinds) if kind in VECTOR_KINDS]

    if len(vector_axes) == 0:
        raise NRRDError('No vector component axis found in kinds. Scalar batches are not single-component '
                        'vectors')
    elif len(vector_axes) > 1:
        raise NRRDError(f'Multiple vector component axes found in kinds: {vector_axes}')

    component_axis_header = vector_axes[0]
    kind = kinds[component_axis_header]

    # Map the header axis to the array axis. This is pure axis reordering: in Fortran order the array axes match
    # the header axes directly, in C order they are reversed. The components themselves are untouched by this.
    component_axis = component_axis_header if index_order == 'F' else dimension - 1 - component_axis_header
    num_components = data.shape[component_axis]

    if 'sizes' in header and header['sizes'][component_axis_header] != num_components:
        raise NRRDError(f'Component axis size ({num_components}) does not match sizes field '
                        f'({header["sizes"][component_axis_header]})')

    expected_size = _VECTOR_KIND_SIZES.get(kind)
    if expected_size is not None and expected_size != num_components:
        raise NRRDError(f"Kind '{kind}' expects {expected_size} components, got {num_components}")

    if 'measurement frame' not in header:
        raise NRRDError("Header is missing 'measurement frame' field, cannot transform components to world "
                        'coordinates')

    measurement_frame = np.asarray(header['measurement frame'], dtype=float)

    if measurement_frame.shape != (num_components, num_components):
        raise NRRDError(f'Measurement frame shape {measurement_frame.shape} does not match the number of '
                        f'components ({num_components})')

    if np.isnan(measurement_frame).any():
        raise NRRDError('Measurement frame contains NaN values')

    _validate_space_directions(header, component_axis_header, num_components)

    # Vector basis transformation: apply the measurement frame along the component axis only. The array layout
    # is preserved by moving the component axis to the front and back again.
    moved = np.moveaxis(data, component_axis, 0)
    world = np.tensordot(measurement_frame, moved, axes=([1], [0]))
    world = np.moveaxis(world, 0, component_axis)

    return WorldComponents(world=world, original=data)
