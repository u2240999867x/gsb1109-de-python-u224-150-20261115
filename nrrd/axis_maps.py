"""Reorientation of NRRD voxel data onto an explicit target axis convention.

Two NRRD volumes describing the same physical object may store their axes in a different order and orientation (for
example one station uses (x, y, z) while another uses (z, -y, x)). The :meth:`reorient` function rearranges the voxel
array together with the spatial header fields so that the spatial axes follow an explicitly requested target
convention.

The transformation is purely a permutation of axes and, when an existing axis points opposite to the requested target
direction, a reversal along that axis. It never changes the physical meaning of a voxel: the world coordinate of the
voxel originally stored at any given index is identical before and after reorientation. Each new spatial axis points
exactly along its explicit target direction while keeping the original spacing; if that requires changing the sign of
a negative space direction, the matching array reversal and 'space origin' shift are applied together, so the sign is
never flipped on its own.

Non-spatial axes, i.e. rows of 'none'/NaN in 'space directions' such as a color or list axis, do not take part in the
spatial transformation. Their data and header entries keep their original position.
"""

from typing import List, Optional, Sequence, Tuple, Union

import numpy as np
import numpy.typing as npt

from nrrd.errors import NRRDError
from nrrd.types import IndexOrder, NRRDHeader

# Cosine of the angle above which two unit vectors are considered collinear.
_COLLINEAR_EPS = 1e-10

# Per-axis header fields that contain one entry per (header-order) axis. These must be permuted together with the
# array axes. 'space directions' is handled separately because its entries are spatial vectors or 'none'.
_PER_AXIS_FIELDS = (
    'sizes', 'spacings', 'thicknesses', 'kinds', 'labels', 'units', 'centerings',
    'axis mins', 'axismins', 'axis maxs', 'axismaxs',
)

# Named anatomical directions for the spaces defined by the NRRD specification. Each entry maps a direction name (and
# a single-letter shorthand) to the direction vector in that space.
_NAMED_DIRECTIONS = {
    'right-anterior-superior': {
        'r': (1.0, 0.0, 0.0), 'l': (-1.0, 0.0, 0.0),
        'a': (0.0, 1.0, 0.0), 'p': (0.0, -1.0, 0.0),
        's': (0.0, 0.0, 1.0), 'i': (0.0, 0.0, -1.0),
    },
    'left-anterior-superior': {
        'l': (1.0, 0.0, 0.0), 'r': (-1.0, 0.0, 0.0),
        'a': (0.0, 1.0, 0.0), 'p': (0.0, -1.0, 0.0),
        's': (0.0, 0.0, 1.0), 'i': (0.0, 0.0, -1.0),
    },
    'left-posterior-superior': {
        'l': (1.0, 0.0, 0.0), 'r': (-1.0, 0.0, 0.0),
        'p': (0.0, 1.0, 0.0), 'a': (0.0, -1.0, 0.0),
        's': (0.0, 0.0, 1.0), 'i': (0.0, 0.0, -1.0),
    },
    'right-posterior-superior': {
        'r': (1.0, 0.0, 0.0), 'l': (-1.0, 0.0, 0.0),
        'p': (0.0, 1.0, 0.0), 'a': (0.0, -1.0, 0.0),
        's': (0.0, 0.0, 1.0), 'i': (0.0, 0.0, -1.0),
    },
    # Two-dimensional spaces
    'right-anterior': {
        'r': (1.0, 0.0), 'l': (-1.0, 0.0),
        'a': (0.0, 1.0), 'p': (0.0, -1.0),
    },
    'left-anterior': {
        'l': (1.0, 0.0), 'r': (-1.0, 0.0),
        'a': (0.0, 1.0), 'p': (0.0, -1.0),
    },
    'left-posterior': {
        'l': (1.0, 0.0), 'r': (-1.0, 0.0),
        'p': (0.0, 1.0), 'a': (0.0, -1.0),
    },
    'right-posterior': {
        'r': (1.0, 0.0), 'l': (-1.0, 0.0),
        'p': (0.0, 1.0), 'a': (0.0, -1.0),
    },
}


def _normalize_directions(space_directions: Union[npt.NDArray, Sequence[Optional[npt.NDArray]]]
                          ) -> List[Optional[np.ndarray]]:
    """Return 'space directions' as a list of optional float vectors.

    A matrix represents non-spatial axes as a row of NaN, whereas the vector list representation uses :obj:`None`.
    """

    if isinstance(space_directions, np.ndarray):
        rows = [space_directions[i] for i in range(space_directions.shape[0])]
    else:
        rows = list(space_directions)

    directions: List[Optional[np.ndarray]] = []
    for row in rows:
        if row is None:
            directions.append(None)
        else:
            vector = np.asarray(row, dtype=float)
            if np.all(np.isnan(vector)):
                directions.append(None)
            else:
                directions.append(vector)

    return directions


def _array_axis_indices(ndim: int, index_order: IndexOrder) -> List[int]:
    """Return the array axis corresponding to each header-order (Fortran) axis."""

    if index_order == 'F':
        return list(range(ndim))
    return list(range(ndim - 1, -1, -1))


_SHORTHAND_NAMES = {
    'right': 'r', 'left': 'l', 'anterior': 'a', 'posterior': 'p',
    'superior': 's', 'inferior': 'i',
}

# Compact three-/two-letter space codes as they may appear in the NRRD 'space' field.
_SPACE_CODES = {
    'ras': 'right-anterior-superior',
    'las': 'left-anterior-superior',
    'lps': 'left-posterior-superior',
    'rps': 'right-posterior-superior',
    'ra': 'right-anterior',
    'la': 'left-anterior',
    'lp': 'left-posterior',
    'rp': 'right-posterior',
}


def _resolve_header_space(space: Optional[str], space_dim: int) -> Optional[str]:
    """Normalize the NRRD 'space' field into one of the keys of :data:`_NAMED_DIRECTIONS`."""

    if space is None:
        return None

    normalized = space.strip().lower()
    normalized = _SPACE_CODES.get(normalized, normalized)

    if normalized in _NAMED_DIRECTIONS and len(next(iter(_NAMED_DIRECTIONS[normalized].values()))) == space_dim:
        return normalized
    return None


def _lookup_named_direction(token: str, space_dim: int, space_name: Optional[str] = None) -> np.ndarray:
    token = token.strip().lower()

    # Fall back to the right-anterior-(superior) convention when no header space is declared, so that single-letter
    # shorthands always resolve deterministically.
    if space_name is None:
        space_name = 'right-anterior-superior' if space_dim == 3 else 'right-anterior'

    directions = _NAMED_DIRECTIONS[space_name]

    # Compact single-letter tokens are used directly as dictionary keys.
    if token in directions:
        return np.asarray(directions[token], dtype=float)

    # Full direction names ('right', 'anterior', ...) are expanded to their shorthand first.
    shorthand = _SHORTHAND_NAMES.get(token)
    if shorthand is not None and shorthand in directions:
        return np.asarray(directions[shorthand], dtype=float)

    raise NRRDError(f'Unknown named direction: {token!r}')


def _resolve_target_axes(target_axes: Union[str, Sequence[Union[str, Sequence[float]]]], space_dim: int,
                         space_name: Optional[str] = None) -> List[np.ndarray]:
    """Resolve the target axis specification into unit direction vectors."""

    if isinstance(target_axes, str):
        tokens = target_axes.split('-') if '-' in target_axes else list(target_axes)
    else:
        tokens = list(target_axes)

    vectors: List[np.ndarray] = []
    for token in tokens:
        if isinstance(token, str):
            vector = _lookup_named_direction(token, space_dim, space_name)
        else:
            vector = np.asarray(token, dtype=float)

        if vector.shape != (space_dim,):
            raise NRRDError(f'Target direction {vector!r} does not have {space_dim} components')
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise NRRDError('Target directions must be non-zero')
        vectors.append(vector / norm)

    if len(vectors) != space_dim:
        raise NRRDError(f'Expected {space_dim} target axes but got {len(vectors)}')

    # Target axes must describe distinct spatial directions.
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            if abs(float(np.dot(vectors[i], vectors[j]))) > 1.0 - _COLLINEAR_EPS:
                raise NRRDError('Target axes contain two collinear directions')

    return vectors


def _match_spatial_axes(source_units: List[np.ndarray], target_units: List[np.ndarray]
                        ) -> List[Tuple[int, float]]:
    """Match every source spatial axis to a target axis.

    Returns one ``(target_index, sign)`` pair per source axis, where ``sign`` is +1 if the source axis already points
    along the target direction and -1 if it points opposite.
    """

    matches: List[Optional[Tuple[int, float]]] = [None] * len(source_units)
    used_targets = set()

    for source_index, source_unit in enumerate(source_units):
        for target_index, target_unit in enumerate(target_units):
            cosine = float(np.dot(source_unit, target_unit))
            if abs(cosine) > 1.0 - _COLLINEAR_EPS:
                if target_index in used_targets:
                    raise NRRDError('Target axes do not describe the same spatial axes as the input')
                used_targets.add(target_index)
                matches[source_index] = (target_index, 1.0 if cosine >= 0.0 else -1.0)
                break
        else:
            raise NRRDError('Target axes do not describe the same spatial axes as the input')

    return [match for match in matches if match is not None]


def _permute_per_axis(value, header_permutation: Sequence[int]):
    """Apply a header-axis permutation to a per-axis header field."""

    if isinstance(value, np.ndarray):
        return value[np.asarray(header_permutation)]
    return [value[index] for index in header_permutation]


def reorient(data: npt.NDArray, header: NRRDHeader, target_axes: Union[str, Sequence[Union[str, Sequence[float]]]],
             index_order: IndexOrder = 'F') -> Tuple[npt.NDArray, NRRDHeader]:
    """Reorient voxel data and its spatial header fields onto explicit target axes.

    The spatial axes of :obj:`data` and :obj:`header` are rearranged so that they follow :obj:`target_axes`. Axes are
    matched by collinearity with the existing 'space directions' vectors, so only the order and orientation change:
    spacings, shears and the physical position of every voxel are preserved. Each output spatial axis points exactly
    along its target direction while keeping its original spacing. If a source axis points opposite to the requested
    target direction, the array is reversed along that axis and 'space origin' is shifted by the matching amount at
    the same time; a negative space direction is therefore never turned positive on its own, and voxel world
    coordinates remain unchanged.

    Non-spatial axes (rows of 'none'/NaN in 'space directions') are not part of the spatial convention and remain in
    their original array position with their header entries untouched.

    Parameters
    ----------
    data : :class:`numpy.ndarray`
        Voxel data whose axes follow :obj:`index_order`.
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        NRRD header containing at least 'space directions' and, when the data has spatial axes, 'space origin'.
    target_axes : :class:`str` or sequence
        Explicit target axis convention. Either a hyphen-separated string of direction names ('right-anterior-
        superior'), a compact string of direction shorthands ('RAS'), or a sequence whose entries are direction names
        or explicit direction vectors. A leading minus is expressed through the opposite direction name or a negated
        vector, e.g. ``('right', 'posterior', (0, 0, -1))``.
    index_order : {'C', 'F'}, optional
        Index order of :obj:`data`, matching the :obj:`index_order` used with :meth:`read`/:meth:`write`. Header
        fields are always in Fortran order. Defaults to ``'F'``.

    Returns
    -------
    data : :class:`numpy.ndarray`
        Reoriented voxel data (may be a view of the input array).
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        New header with permuted per-axis fields, recomputed 'space directions' and 'space origin'. The input header
        is not modified.

    See Also
    --------
    :meth:`read`, :meth:`write`
    """

    if index_order not in ('F', 'C'):
        raise NRRDError('Invalid index order')

    if 'space directions' not in header:
        raise NRRDError("Header is missing required field: 'space directions'")

    directions = _normalize_directions(header['space directions'])
    if len(directions) != data.ndim:
        raise NRRDError("Number of rows in 'space directions' does not match the number of data axes")

    spatial_header_axes = [axis for axis, direction in enumerate(directions) if direction is not None]
    spatial_vectors = [directions[axis] for axis in spatial_header_axes]
    space_dim = len(spatial_vectors)

    if space_dim == 0:
        raise NRRDError('Cannot reorient data without any spatial axes')

    if 'space origin' not in header:
        raise NRRDError("Header is missing required field: 'space origin'")
    origin = np.asarray(header['space origin'], dtype=float)
    if origin.shape != (space_dim,):
        raise NRRDError("'space origin' does not have one component per spatial dimension")

    target_units = _resolve_target_axes(target_axes, space_dim,
                                        _resolve_header_space(header.get('space'), space_dim))
    source_units = [vector / np.linalg.norm(vector) for vector in spatial_vectors]
    source_spacings = [float(np.linalg.norm(vector)) for vector in spatial_vectors]
    matches = _match_spatial_axes(source_units, target_units)

    # For each target position, record the source spatial axis that fills it and whether the axis must be reversed.
    target_sources: List[Tuple[int, bool]] = [(0, False)] * space_dim
    for source_spatial_index, (target_index, sign) in enumerate(matches):
        target_sources[target_index] = (source_spatial_index, sign < 0.0)

    sizes = header['sizes']
    array_axes = _array_axis_indices(data.ndim, index_order)

    # Non-spatial axes are not part of the spatial convention, so they keep their exact header/array position. The
    # spatial slots (the positions originally occupied by spatial axes) are filled with the spatial axes in target
    # order.
    spatial_header_positions = list(spatial_header_axes)
    ordered_header_axes: List[Optional[int]] = [
        axis if directions[axis] is None else None for axis in range(data.ndim)
    ]
    for target_index, (source_spatial_index, _flipped) in enumerate(target_sources):
        ordered_header_axes[spatial_header_positions[target_index]] = \
            spatial_header_axes[source_spatial_index]
    ordered_header_axes = [axis for axis in ordered_header_axes if axis is not None]

    # New header position p holds old header axis ordered_header_axes[p]. Translate that into old/new array axes,
    # taking the C-order reversal between header positions and array axes into account.
    array_permutation = [0] * data.ndim
    for header_position, old_header_axis in enumerate(ordered_header_axes):
        old_array_axis = array_axes[old_header_axis]
        new_array_axis = header_position if index_order == 'F' else data.ndim - 1 - header_position
        array_permutation[new_array_axis] = old_array_axis

    reoriented = np.transpose(data, array_permutation)

    # Flipped axes are identified by their new header position, then translated to the corresponding array axis.
    flipped_array_axes = []
    for target_index, (_, flipped) in enumerate(target_sources):
        if flipped:
            header_position = spatial_header_positions[target_index]
            flipped_array_axes.append(header_position if index_order == 'F'
                                      else data.ndim - 1 - header_position)
    if flipped_array_axes:
        reoriented = np.flip(reoriented, axis=flipped_array_axes)

    # Recompute the origin from the voxel that was originally stored at index zero on every spatial axis.
    new_origin = origin.copy()
    for source_spatial_index, (target_index, sign) in enumerate(matches):
        if sign < 0.0:
            header_axis = spatial_header_axes[source_spatial_index]
            new_origin = new_origin + spatial_vectors[source_spatial_index] * (sizes[header_axis] - 1)

    # Rebuild 'space directions' in the new header-axis order. Non-spatial rows stay 'none'/None in place and each
    # spatial slot points exactly along its target direction while keeping the original spacing. Any sign change
    # relative to the source direction is compensated by the array reversal and origin shift above, never applied to
    # the direction alone.
    is_matrix = isinstance(header['space directions'], np.ndarray)
    new_directions: List[Optional[np.ndarray]] = [None] * data.ndim
    for target_index, (source_spatial_index, _flipped) in enumerate(target_sources):
        header_position = spatial_header_positions[target_index]
        new_directions[header_position] = target_units[target_index] * source_spacings[source_spatial_index]

    if is_matrix:
        new_space_directions = np.full((data.ndim, space_dim), np.nan)
        for axis, direction in enumerate(new_directions):
            if direction is not None:
                new_space_directions[axis] = direction
    else:
        new_space_directions = new_directions
    new_header = header.copy()
    new_header['space directions'] = new_space_directions
    new_header['space origin'] = new_origin

    for field in _PER_AXIS_FIELDS:
        if field in new_header:
            new_header[field] = _permute_per_axis(new_header[field], ordered_header_axes)

    return reoriented, new_header
