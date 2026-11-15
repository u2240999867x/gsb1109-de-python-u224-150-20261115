"""Compare NRRD files and headers.

The functions in this module compare NRRD datasets without relying on how the data was stored. Storage details such
as the ``encoding`` (``raw``, ``gzip``, ``bzip2``, ...), ``endian``, ``byteskip`` and ``lineskip`` do not influence
the comparison; they are undone by the reader before any comparison takes place.

Four independent aspects are compared:

* :attr:`NRRDComparison.coordinate_definition_equal` -- the coordinate system (``space``, ``space directions``,
  ``space origin``, ``sizes``, ``kinds``, ...). Axes may be ordered differently in the two files as long as they
  describe the same physical grid.
* :attr:`NRRDComparison.value_type_equal` -- the NRRD ``type`` (``short`` and ``int16`` are the same type, while
  ``int16`` and ``uint16`` are not). Byte order is deliberately ignored.
* :attr:`NRRDComparison.values_equal` -- the sample values, compared at matching physical points when the headers
  provide enough direction information to map the axes, and positionally otherwise.
* :attr:`NRRDComparison.custom_metadata_equal` -- non-standard header fields (the fields written with ``:=``).

A file reordered from Fortran to C index order (or vice versa) can therefore still be equivalent, as long as the
space directions show that the same physical points are stored. When the headers do not contain enough direction
information to establish that, :attr:`NRRDComparison.spatial_equivalence` is :obj:`None`, and only array equality
(:attr:`NRRDComparison.arrays_equal`) is reported. No spatial claim is made in that case.
"""

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
import numpy.typing as npt

from nrrd.errors import NRRDError
from nrrd.reader import _TYPEMAP_NRRD2NUMPY, read
from nrrd.types import NRRDFieldMap, NRRDHeader

__all__ = ['NRRDComparison', 'NRRDHeaderComparison', 'compare', 'compare_headers']

# Standard fields from the NRRD specification. Fields that are not in this set are considered custom metadata.
_STANDARD_FIELDS = frozenset([
    'dimension',
    'type',
    'encoding',
    'endian',
    'sizes',
    'datafile',
    'data file',
    'lineskip',
    'line skip',
    'byteskip',
    'byte skip',
    'content',
    'kinds',
    'labels',
    'units',
    'min',
    'max',
    'spacings',
    'thicknesses',
    'centerings',
    'centers',
    'oldmin',
    'old min',
    'oldmax',
    'old max',
    'axismins',
    'axis mins',
    'axismaxs',
    'axis maxs',
    'sample units',
    'space',
    'space dimension',
    'space units',
    'space directions',
    'space origin',
    'measurement frame',
])

_FIELD_ALIASES = {
    'data file': 'datafile',
    'line skip': 'lineskip',
    'byte skip': 'byteskip',
    'old min': 'oldmin',
    'old max': 'oldmax',
    'axis mins': 'axismins',
    'axis maxs': 'axismaxs',
    'centers': 'centerings',
}


def _canonical_field(field: str) -> str:
    return _FIELD_ALIASES.get(field, field)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple, np.ndarray))


def _is_numeric_scalar(value: Any) -> bool:
    return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool)


def _close(a: Any, b: Any, rtol: float, atol: float, equal_nan: bool = True) -> bool:
    """Compare two numbers with a relative and absolute tolerance."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return bool(np.isclose(a, b, rtol=rtol, atol=atol, equal_nan=equal_nan))
    except TypeError:
        return a == b


def _values_equal(a: Any, b: Any, rtol: float, atol: float, equal_nan: bool = True) -> bool:
    """Compare header values, which may be scalars, strings, arrays or ragged lists."""
    if a is None or b is None:
        return a is None and b is None

    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b

    a_seq = _is_sequence(a)
    b_seq = _is_sequence(b)
    if a_seq and b_seq:
        # ``space directions`` may be represented as a list containing ``None`` entries, so compare elementwise when
        # a plain numeric array cannot be created.
        try:
            arr_a = np.asarray(a)
            arr_b = np.asarray(b)
            if arr_a.shape == arr_b.shape and arr_a.dtype.kind in 'biufc' and arr_b.dtype.kind in 'biufc':
                if arr_a.dtype.kind in 'fc' or arr_b.dtype.kind in 'fc':
                    return bool(np.allclose(arr_a, arr_b, rtol=rtol, atol=atol, equal_nan=equal_nan))
                return bool(np.array_equal(arr_a, arr_b))
        except (TypeError, ValueError):
            pass
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y, rtol, atol, equal_nan) for x, y in zip(a, b))
    if a_seq != b_seq:
        return False

    if _is_numeric_scalar(a) and _is_numeric_scalar(b):
        return _close(a, b, rtol, atol, equal_nan)
    return a == b


def _default_origin(header: NRRDHeader, dimension: int) -> npt.NDArray:
    origin = header.get('space origin')
    if origin is None:
        return np.zeros(dimension, dtype=float)
    return np.asarray(origin, dtype=float)


def _direction_matrix(header: NRRDHeader, dimension: int) -> Tuple[Optional[npt.NDArray], Optional[str]]:
    """Return the ``space directions`` as a (dimension, space dimension) matrix of floats.

    Non-spatial axes are represented by a row of NaNs. Returns ``(None, reason)`` when no usable direction
    information is available.
    """
    if 'space directions' not in header:
        return None, 'space directions are missing'

    value = header['space directions']
    if isinstance(value, np.ndarray) and value.dtype.kind in 'fc' and value.ndim == 2:
        matrix = value.astype(float)
    elif _is_sequence(value):
        rows = []
        for row in value:
            if row is None:
                rows.append(None)
            else:
                try:
                    rows.append(np.asarray(row, dtype=float).ravel())
                except (TypeError, ValueError):
                    return None, 'space directions could not be parsed'
        width = next((len(row) for row in rows if row is not None), None)
        if width is None:
            return None, 'space directions contain no spatial axes'
        matrix = np.full((len(rows), width), np.nan, dtype=float)
        for index, row in enumerate(rows):
            if row is None:
                continue
            if row.size != width:
                return None, 'space directions rows have inconsistent lengths'
            matrix[index] = row
    else:
        return None, 'space directions could not be parsed'

    if matrix.ndim != 2 or matrix.shape[0] != dimension:
        return None, 'space directions do not match the data dimension'
    if not np.any(np.isfinite(matrix)):
        return None, 'space directions contain no spatial axes'
    return matrix, None


def _kinds(header: NRRDHeader, dimension: int) -> List[str]:
    kinds = header.get('kinds')
    if kinds is None:
        return ['domain'] * dimension
    return [str(kind) for kind in kinds]


def _centerings(header: NRRDHeader, dimension: int) -> Optional[List[str]]:
    centerings = header.get('centerings', header.get('centers'))
    if centerings is None:
        return None
    return [str(centering) for centering in centerings]


def _sizes(header: NRRDHeader, dimension: int) -> npt.NDArray:
    sizes = header.get('sizes')
    if sizes is None:
        raise NRRDError('Header is missing required field: sizes')
    sizes = np.asarray(sizes, dtype=np.int64).ravel()
    if sizes.size != dimension:
        raise NRRDError(f'Number of elements in sizes does not match dimension. Dimension: {dimension}, '
                        f'len(sizes): {sizes.size}')
    return sizes


@dataclass
class _AxisMapping:
    """Mapping from axes of the first dataset to axes of the second dataset."""

    permutation: npt.NDArray  # permutation[i] is the axis in the second dataset matching axis i of the first
    flips: npt.NDArray  # True when the axis direction is reversed

    def align(self, data: npt.NDArray) -> npt.NDArray:
        aligned = np.transpose(data, axes=self.permutation)
        for axis, flip in enumerate(self.flips):
            if flip:
                aligned = np.flip(aligned, axis=axis)
        return aligned


def _match_axes(direction_a: npt.NDArray, direction_b: npt.NDArray, rtol: float, atol: float) \
        -> Tuple[Optional[_AxisMapping], Optional[str], bool]:
    """Find a permutation and flips that map the axes of the first grid onto the second.

    Returns ``(mapping, reason, ambiguous)``. A reason with ``ambiguous`` :obj:`True` means the directions repeat so
    that the axes cannot be told apart, rather than the grids conflicting.
    """
    if direction_a.shape[1] != direction_b.shape[1]:
        return None, 'the two headers have different space dimensions', False

    domain_a = [i for i in range(direction_a.shape[0]) if np.any(np.isfinite(direction_a[i]))]
    domain_b = [j for j in range(direction_b.shape[0]) if np.any(np.isfinite(direction_b[j]))]

    if len(domain_a) != len(domain_b):
        return None, 'the two headers have a different number of spatial axes', False

    candidates: Dict[int, List[Tuple[int, bool]]] = {}
    for i in domain_a:
        if not np.all(np.isfinite(direction_a[i])):
            return None, 'a spatial axis has an incomplete direction vector', False
        matches = []
        for j in domain_b:
            if not np.all(np.isfinite(direction_b[j])):
                continue
            same_length = np.isclose(np.linalg.norm(direction_a[i]), np.linalg.norm(direction_b[j]),
                                     rtol=rtol, atol=atol)
            if not same_length:
                continue
            if np.allclose(direction_a[i], direction_b[j], rtol=rtol, atol=atol):
                matches.append((j, False))
            elif np.allclose(direction_a[i], -direction_b[j], rtol=rtol, atol=atol):
                matches.append((j, True))
        if not matches:
            return None, f'no matching axis found for spatial axis {i}', False
        candidates[i] = matches

    # A repeated direction (for example two identical axes) makes the association ambiguous rather than wrong.
    if any(len(matches) > 1 for matches in candidates.values()):
        return None, 'the space directions contain repeated directions, so the axes cannot be told apart', True

    assignment: Dict[int, Tuple[int, bool]] = {}
    used = set()

    order = sorted(domain_a, key=lambda axis: len(candidates[axis]))

    def assign(index: int) -> bool:
        if index == len(order):
            return True
        axis = order[index]
        for other_axis, flip in candidates[axis]:
            if other_axis in used:
                continue
            used.add(other_axis)
            assignment[axis] = (other_axis, flip)
            if assign(index + 1):
                return True
            used.discard(other_axis)
            del assignment[axis]
        return False

    if not assign(0):
        return None, 'the spatial axes of the two headers cannot be matched one-to-one', False

    # Non-spatial axes do not have a direction that could identify them. Map the remaining axes in their original
    # order, which preserves the relative order of e.g. vector component axes.
    remaining_a = [i for i in range(direction_a.shape[0]) if i not in assignment]
    remaining_b = [j for j in range(direction_b.shape[0]) if j not in used]
    for i, j in zip(remaining_a, remaining_b):
        assignment[i] = (j, False)

    permutation = np.array([assignment[i][0] for i in range(direction_a.shape[0])], dtype=int)
    flips = np.array([assignment[i][1] for i in range(direction_a.shape[0])], dtype=bool)
    return _AxisMapping(permutation, flips), None, False


def _aligned_origin_equal(header_a: NRRDHeader, header_b: NRRDHeader, mapping: _AxisMapping,
                          direction_b: npt.NDArray, sizes_a: npt.NDArray, rtol: float, atol: float) -> bool:
    """Check that the physical sample coordinates of the two grids are the same.

    Reversing an axis changes which end of that axis is index zero, so the origin has to move by ``(n - 1) * d`` for
    every reversed axis.
    """
    dimension = direction_b.shape[0]
    origin_a = _default_origin(header_a, dimension)
    origin_b = _default_origin(header_b, dimension)

    expected_origin_b = origin_a.copy()
    for axis in range(dimension):
        if mapping.flips[axis]:
            expected_origin_b = expected_origin_b - (sizes_a[axis] - 1) * direction_b[mapping.permutation[axis]]

    return bool(np.allclose(origin_b, expected_origin_b, rtol=rtol, atol=atol))


def _axis_aware_coordinate_comparison(header_a: NRRDHeader, header_b: NRRDHeader, sizes_a: npt.NDArray,
                                      sizes_b: npt.NDArray, mapping: _AxisMapping,
                                      direction_b: npt.NDArray, differences: List[str], notes: List[str],
                                      rtol: float, atol: float) -> None:
    for axis in range(sizes_a.size):
        other_axis = mapping.permutation[axis]
        if sizes_a[axis] != sizes_b[other_axis]:
            differences.append(f'sizes differ: axis {axis} has {sizes_a[axis]} samples in the first dataset and '
                               f'{sizes_b[other_axis]} samples in the second dataset')

    kinds_a = _kinds(header_a, sizes_a.size)
    kinds_b = _kinds(header_b, sizes_b.size)
    for axis in range(sizes_a.size):
        other_axis = mapping.permutation[axis]
        if kinds_a[axis] != kinds_b[other_axis]:
            differences.append(f'kinds differ: axis {axis} is {kinds_a[axis]!r} in the first dataset and '
                               f'{kinds_b[other_axis]!r} in the second dataset')

    centerings_a = _centerings(header_a, sizes_a.size)
    centerings_b = _centerings(header_b, sizes_b.size)
    if (centerings_a is None) != (centerings_b is None):
        differences.append('centerings are specified in only one of the headers')
    elif centerings_a is not None and centerings_b is not None:
        for axis in range(sizes_a.size):
            other_axis = mapping.permutation[axis]
            if centerings_a[axis] != centerings_b[other_axis]:
                differences.append(f'centerings differ: axis {axis} is {centerings_a[axis]!r} in the first dataset '
                                   f'and {centerings_b[other_axis]!r} in the second dataset')

    if not _aligned_origin_equal(header_a, header_b, mapping, direction_b, sizes_a, rtol, atol):
        differences.append('space origin does not describe the same physical sample positions after aligning the axes')

    space_units_a = header_a.get('space units')
    space_units_b = header_b.get('space units')
    if space_units_a is not None and space_units_b is not None:
        units_a = [space_units_a] if isinstance(space_units_a, str) else list(space_units_a)
        units_b = [space_units_b] if isinstance(space_units_b, str) else list(space_units_b)
        if len(units_a) == 1 and len(units_b) == 1:
            if units_a[0] != units_b[0]:
                differences.append(f'space units differ: {units_a[0]!r} vs {units_b[0]!r}')
        elif len(units_a) == sizes_a.size and len(units_b) == sizes_b.size:
            for axis in range(sizes_a.size):
                other_axis = mapping.permutation[axis]
                if units_a[axis] != units_b[other_axis]:
                    differences.append(f'space units differ for axis {axis}: {units_a[axis]!r} vs '
                                       f'{units_b[other_axis]!r}')
        elif units_a != units_b:
            differences.append(f'space units differ: {units_a!r} vs {units_b!r}')
    elif (space_units_a is None) != (space_units_b is None):
        differences.append('space units are specified in only one of the headers')

    measurement_frame_a = header_a.get('measurement frame')
    measurement_frame_b = header_b.get('measurement frame')
    if (measurement_frame_a is None) != (measurement_frame_b is None):
        differences.append('measurement frame is specified in only one of the headers')
    elif measurement_frame_a is not None and measurement_frame_b is not None:
        if not _values_equal(measurement_frame_a, measurement_frame_b, rtol, atol):
            differences.append('measurement frame differs')

    if np.any(mapping.permutation != np.arange(sizes_a.size)):
        notes.append(f'axes are ordered differently between the two datasets; axis mapping is '
                     f'{mapping.permutation.tolist()}')
    if np.any(mapping.flips):
        notes.append(f'axes {np.flatnonzero(mapping.flips).tolist()} are reversed between the two datasets')


def _literal_coordinate_comparison(header_a: NRRDHeader, header_b: NRRDHeader, dimension: int,
                                   sizes_a: npt.NDArray, sizes_b: npt.NDArray, differences: List[str],
                                   rtol: float, atol: float) -> None:
    if dimension != header_b.get('dimension', dimension):
        differences.append(f'dimension differs: {dimension} vs {header_b.get("dimension")}')
    if not np.array_equal(sizes_a, sizes_b):
        differences.append(f'sizes differ: {sizes_a.tolist()} vs {sizes_b.tolist()}')

    for field_name in ('space', 'space dimension'):
        value_a = header_a.get(field_name)
        value_b = header_b.get(field_name)
        if (value_a is None) != (value_b is None) or not _values_equal(value_a, value_b, rtol, atol):
            differences.append(f'{field_name} differs: {value_a!r} vs {value_b!r}')

    direction_a = header_a.get('space directions')
    direction_b = header_b.get('space directions')
    if (direction_a is None) != (direction_b is None) or not _values_equal(direction_a, direction_b, rtol, atol):
        differences.append('space directions differ or are present in only one of the headers')

    origin_a = header_a.get('space origin')
    origin_b = header_b.get('space origin')
    default_origin = np.zeros(dimension, dtype=float)
    origin_a_value = default_origin if origin_a is None else origin_a
    origin_b_value = default_origin if origin_b is None else origin_b
    if not _values_equal(origin_a_value, origin_b_value, rtol, atol):
        differences.append(f'space origin differs: {origin_a!r} vs {origin_b!r}')

    for field_name in ('kinds', 'centerings', 'centers'):
        value_a = header_a.get(field_name)
        value_b = header_b.get(field_name)
        if (value_a is None) != (value_b is None) or not _values_equal(value_a, value_b, rtol, atol):
            differences.append(f'{field_name} differs: {value_a!r} vs {value_b!r}')

    for field_name in ('spacings', 'thicknesses', 'space units'):
        value_a = header_a.get(field_name)
        value_b = header_b.get(field_name)
        if (value_a is None) != (value_b is None) or not _values_equal(value_a, value_b, rtol, atol):
            differences.append(f'{field_name} differs: {value_a!r} vs {value_b!r}')

    measurement_frame_a = header_a.get('measurement frame')
    measurement_frame_b = header_b.get('measurement frame')
    if (measurement_frame_a is None) != (measurement_frame_b is None) \
            or not _values_equal(measurement_frame_a, measurement_frame_b, rtol, atol):
        differences.append('measurement frame differs or is present in only one of the headers')


def _type_signature(header: NRRDHeader, data: Optional[npt.NDArray]) -> Tuple[Any, Optional[str]]:
    """Return a byte-order independent signature for the NRRD value type."""
    nrrd_type = header.get('type')
    if nrrd_type is None:
        if data is None:
            return ('unknown', None), None
        dtype = data.dtype
        kind, itemsize = dtype.kind, dtype.itemsize
    elif nrrd_type in _TYPEMAP_NRRD2NUMPY:
        dtype = np.dtype(_TYPEMAP_NRRD2NUMPY[nrrd_type])
        kind, itemsize = dtype.kind, dtype.itemsize
        # The 'block' type does not specify an item size in the header; use the array for that.
        if kind == 'V' and data is not None:
            itemsize = data.dtype.itemsize
    else:
        return ('nrrd', str(nrrd_type)), str(nrrd_type)

    # Endianness is intentionally not part of the signature: the same physical type stored as little or big endian
    # must compare equal.
    return (kind, itemsize), None


def _data_type_signature(data: npt.NDArray) -> Tuple[str, int]:
    return data.dtype.kind, data.dtype.itemsize


def _arrays_close(a: npt.NDArray, b: npt.NDArray, rtol: float, atol: float, equal_nan: bool) -> bool:
    if a.shape != b.shape:
        return False
    if a.dtype.kind == 'V' or b.dtype.kind == 'V':
        return bool(np.array_equal(a, b))
    if a.dtype.kind in 'fc' or b.dtype.kind in 'fc':
        return bool(np.allclose(a, b, rtol=rtol, atol=atol, equal_nan=equal_nan))
    return bool(np.array_equal(a, b))


def _custom_field_names(header: NRRDHeader, custom_field_map: Optional[NRRDFieldMap],
                        ignored_fields: Iterable[str]) -> List[str]:
    names = set()
    for field_name in header:
        if _canonical_field(field_name) not in _STANDARD_FIELDS:
            names.add(field_name)
    if custom_field_map:
        names.update(field_name for field_name in custom_field_map if field_name in header)
    names.difference_update(ignored_fields)
    return sorted(names)


def _custom_metadata_comparison(header_a: NRRDHeader, header_b: NRRDHeader,
                                custom_field_map: Optional[NRRDFieldMap], ignored_fields: Iterable[str],
                                rtol: float, atol: float) -> List[str]:
    differences = []
    fields_a = _custom_field_names(header_a, custom_field_map, ignored_fields)
    fields_b = _custom_field_names(header_b, custom_field_map, ignored_fields)
    for field_name in sorted(set(fields_a) | set(fields_b)):
        if field_name not in header_a:
            differences.append(f'custom field {field_name!r} is missing from the first header')
        elif field_name not in header_b:
            differences.append(f'custom field {field_name!r} is missing from the second header')
        elif not _values_equal(header_a[field_name], header_b[field_name], rtol, atol):
            differences.append(f'custom field {field_name!r} differs: {header_a[field_name]!r} vs '
                               f'{header_b[field_name]!r}')
    return differences


@dataclass
class NRRDHeaderComparison:
    """Result of comparing the headers of two NRRD datasets."""

    coordinate_definition_equal: bool
    value_type_equal: bool
    custom_metadata_equal: bool
    spatial_equivalence: Optional[bool]
    differences: Dict[str, List[str]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    direction_basis_available: bool = False

    @property
    def same_grid(self) -> Optional[bool]:
        """Whether the headers describe the same physical grid, or :obj:`None` when that cannot be determined."""
        return self.spatial_equivalence

    @property
    def equivalent(self) -> bool:
        """Whether the headers are equivalent *and* describe the same physical grid.

        Requires a positive spatial verdict, so this is :obj:`False` when the headers lack the direction information
        needed to establish that the grids match. Use :attr:`coordinate_definition_equal` to see whether the
        coordinate fields themselves match.
        """
        return bool(self.coordinate_definition_equal and self.value_type_equal and self.custom_metadata_equal
                    and self.spatial_equivalence is True)

    def __bool__(self) -> bool:
        return self.equivalent

    def as_dict(self) -> Dict[str, Any]:
        return OrderedDict([
            ('coordinate_definition_equal', self.coordinate_definition_equal),
            ('value_type_equal', self.value_type_equal),
            ('custom_metadata_equal', self.custom_metadata_equal),
            ('spatial_equivalence', self.spatial_equivalence),
            ('equivalent', self.equivalent),
            ('differences', self.differences),
            ('notes', self.notes),
        ])


@dataclass
class NRRDComparison:
    """Result of comparing two NRRD datasets.

    Attributes
    ----------
    coordinate_definition_equal : :class:`bool`
        Whether the two headers define the same coordinate system and sampling grid. Axes may be ordered differently
        and may be reversed, as long as they cover the same physical sample points.
    value_type_equal : :class:`bool`
        Whether the sample value types are equal. Type aliases (``short``/``int16``) and byte order are ignored.
    values_equal : :class:`bool`
        Whether the sample values are equal. When the axes can be matched from the space directions, the values are
        compared at matching physical points; otherwise the arrays are compared positionally.
    custom_metadata_equal : :class:`bool`
        Whether the custom (non-standard) header fields are equal.
    arrays_equal : :class:`bool`
        Whether the data arrays are equal without any axis reordering. This is the plain array comparison and does
        not make a statement about the physical grid.
    spatial_equivalence : :class:`bool` or :obj:`None`
        Whether the datasets represent the same values on the same physical grid. :obj:`None` when the headers do not
        contain enough direction information to decide; in that case no spatial claim is made.
    equivalent : :class:`bool`
        Whether the datasets are equivalent: matching coordinate definition, value type, values and custom metadata,
        with a positive :attr:`spatial_equivalence` verdict. This is :obj:`False` when the headers do not contain the
        direction information needed to claim that the physical grids are the same; use :attr:`claim` to find out
        whether only the arrays match.
    """

    coordinate_definition_equal: bool
    value_type_equal: bool
    values_equal: bool
    custom_metadata_equal: bool
    arrays_equal: bool
    spatial_equivalence: Optional[bool]
    equivalent: bool
    differences: Dict[str, List[str]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def same_grid(self) -> Optional[bool]:
        """Alias of :attr:`spatial_equivalence`."""
        return self.spatial_equivalence

    @property
    def claim(self) -> str:
        """The strongest claim supported by the comparison.

        One of ``'spatially_equivalent'``, ``'arrays_equivalent'`` or ``'different'``. ``'arrays_equivalent'`` means
        the arrays and metadata match but the headers lack the direction information needed to claim that the
        physical grids are the same; in that case only array equality is asserted.
        """
        if self.spatial_equivalence:
            return 'spatially_equivalent'
        if (self.coordinate_definition_equal and self.value_type_equal and self.values_equal
                and self.custom_metadata_equal and self.spatial_equivalence is None):
            return 'arrays_equivalent'
        return 'different'

    @property
    def coordinate_definition_conflict(self) -> bool:
        """Whether the headers positively contradict each other about the grid."""
        return self.spatial_equivalence is False

    @property
    def verdict(self) -> str:
        """A one-sentence description of the result that does not overstate the available evidence."""
        if self.claim == 'spatially_equivalent':
            return 'Datasets are equivalent: they contain the same values at the same physical grid points.'
        if self.claim == 'arrays_equivalent':
            return ('Data arrays are equivalent, but spatial equivalence cannot be established because the headers '
                    'do not provide sufficient direction information.')
        failed = [name for name in ('coordinate_definition_equal', 'value_type_equal', 'values_equal',
                                    'custom_metadata_equal') if not getattr(self, name)]
        message = f'Datasets differ ({", ".join(failed)}).'
        if self.spatial_equivalence is None:
            message += ' Spatial equivalence cannot be determined from the available direction information.'
        return message

    def __bool__(self) -> bool:
        return self.equivalent

    def __getitem__(self, item: str) -> Any:
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def as_dict(self) -> Dict[str, Any]:
        return OrderedDict([
            ('coordinate_definition_equal', self.coordinate_definition_equal),
            ('value_type_equal', self.value_type_equal),
            ('values_equal', self.values_equal),
            ('custom_metadata_equal', self.custom_metadata_equal),
            ('arrays_equal', self.arrays_equal),
            ('spatial_equivalence', self.spatial_equivalence),
            ('equivalent', self.equivalent),
            ('claim', self.claim),
            ('verdict', self.verdict),
            ('differences', self.differences),
            ('notes', self.notes),
        ])


def _load(source: Union[str, os.PathLike, Tuple[npt.NDArray, NRRDHeader]], custom_field_map: Optional[NRRDFieldMap],
          index_order: str) -> Tuple[npt.NDArray, NRRDHeader]:
    if isinstance(source, (str, os.PathLike)):
        data, header = read(os.fspath(source), custom_field_map=custom_field_map, index_order='F')
        return np.asarray(data), header

    if isinstance(source, (tuple, list)) and len(source) == 2 and isinstance(source[1], dict):
        data, header = source
        data = np.asarray(data)
        dimension = header.get('dimension', data.ndim)
        sizes = _sizes(header, dimension)
        if index_order == 'F':
            if tuple(data.shape) != tuple(sizes):
                raise NRRDError(f'Data shape {data.shape} does not match the header sizes {tuple(sizes)}')
        elif index_order == 'C':
            if tuple(data.shape[::-1]) != tuple(sizes):
                raise NRRDError(f'Data shape {data.shape} does not match the header sizes {tuple(sizes)} in C order')
            data = data.T
        else:
            raise NRRDError('Invalid index order')
        return data, header

    raise TypeError('Each dataset must be a filename or a (data, header) tuple')


def _compare_header_pair(header_a: NRRDHeader, header_b: NRRDHeader, data_a: Optional[npt.NDArray],
                         data_b: Optional[npt.NDArray], custom_field_map: Optional[NRRDFieldMap],
                         ignored_fields: Iterable[str], rtol: float, atol: float, grid_rtol: float,
                         grid_atol: float, equal_nan: bool) \
        -> Tuple[NRRDHeaderComparison, Optional[_AxisMapping], Optional[npt.NDArray], Optional[npt.NDArray]]:
    differences: Dict[str, List[str]] = OrderedDict([
        ('coordinate_definition', []),
        ('value_type', []),
        ('custom_metadata', []),
    ])
    notes: List[str] = []

    dimension_a = int(header_a.get('dimension', data_a.ndim if data_a is not None else 0))
    dimension_b = int(header_b.get('dimension', data_b.ndim if data_b is not None else 0))
    sizes_a = _sizes(header_a, dimension_a)
    sizes_b = _sizes(header_b, dimension_b)

    direction_a, reason_a = _direction_matrix(header_a, dimension_a)
    direction_b, reason_b = _direction_matrix(header_b, dimension_b)

    spatial_equivalence: Optional[bool]
    direction_basis_available = False
    mapping: Optional[_AxisMapping] = None

    if direction_a is not None and direction_b is not None:
        space_a = header_a.get('space')
        space_b = header_b.get('space')
        if space_a is not None and space_b is not None and space_a != space_b:
            spatial_equivalence = False
            differences['coordinate_definition'].append(f'space differs: {space_a!r} vs {space_b!r}')
        elif (space_a is None) != (space_b is None):
            spatial_equivalence = None
            differences['coordinate_definition'].append('space is specified in only one of the headers')
            notes.append('Spatial equivalence cannot be established because only one header names its space.')
        else:
            direction_basis_available = True
            if space_a is None and space_b is None:
                notes.append('Neither header names a space; the space directions are compared as an unlabelled frame.')

            if dimension_a != dimension_b:
                spatial_equivalence = False
                differences['coordinate_definition'].append(
                    f'dimension differs: {dimension_a} vs {dimension_b}')
            else:
                mapping, reason, ambiguous = _match_axes(direction_a, direction_b, grid_rtol, grid_atol)
                if mapping is None:
                    if ambiguous:
                        # Repeated directions: the grids do not conflict, but the axes cannot be matched reliably.
                        spatial_equivalence = None
                        direction_basis_available = False
                        notes.append(f'Spatial equivalence cannot be established: {reason}. Only the data arrays are '
                                     'compared.')
                    else:
                        spatial_equivalence = False
                        differences['coordinate_definition'].append(reason)
                else:
                    _axis_aware_coordinate_comparison(header_a, header_b, sizes_a, sizes_b, mapping, direction_b,
                                                      differences['coordinate_definition'], notes, grid_rtol,
                                                      grid_atol)
                    spatial_equivalence = True

                    space_dimension_a = header_a.get('space dimension', direction_a.shape[1])
                    space_dimension_b = header_b.get('space dimension', direction_b.shape[1])
                    if space_dimension_a != space_dimension_b:
                        spatial_equivalence = False
                        differences['coordinate_definition'].append(
                            f'space dimension differs: {space_dimension_a} vs {space_dimension_b}')

                    # Sizes, kinds, centerings, units or the origin may still contradict the grid even though the
                    # axis directions line up.
                    if differences['coordinate_definition']:
                        spatial_equivalence = False
    else:
        spatial_equivalence = None
        _literal_coordinate_comparison(header_a, header_b, dimension_a, sizes_a, sizes_b,
                                       differences['coordinate_definition'], grid_rtol, grid_atol)
        if direction_a is None and direction_b is None:
            reason = reason_a or reason_b or 'space directions are missing'
        else:
            reason = 'space directions are present in only one of the headers'
        # Different dimensions or sample counts are conclusive even without direction information: the datasets
        # cannot cover the same grid.
        if dimension_a != dimension_b or not np.array_equal(sizes_a, sizes_b):
            spatial_equivalence = False
        else:
            notes.append(f'Spatial equivalence cannot be established: {reason}. Only the data arrays are compared.')

    # Compare the value type, ignoring byte order and NRRD type aliases.
    signature_a, type_name_a = _type_signature(header_a, data_a)
    signature_b, type_name_b = _type_signature(header_b, data_b)
    value_type_equal = signature_a == signature_b
    if not value_type_equal:
        differences['value_type'].append(f'value type differs: {type_name_a or header_a.get("type")!r} vs '
                                         f'{type_name_b or header_b.get("type")!r}')

    if data_a is not None and data_b is not None:
        data_signature_a = _data_type_signature(data_a)
        data_signature_b = _data_type_signature(data_b)
        if data_signature_a != data_signature_b:
            value_type_equal = False
            differences['value_type'].append(f'array dtype differs: {data_a.dtype} vs {data_b.dtype}')

    differences['custom_metadata'].extend(
        _custom_metadata_comparison(header_a, header_b, custom_field_map, ignored_fields, rtol, atol))

    comparison = NRRDHeaderComparison(
        coordinate_definition_equal=not differences['coordinate_definition'],
        value_type_equal=value_type_equal,
        custom_metadata_equal=not differences['custom_metadata'],
        spatial_equivalence=spatial_equivalence,
        differences=OrderedDict((key, value) for key, value in differences.items() if value),
        notes=notes,
        direction_basis_available=direction_basis_available,
    )
    return comparison, mapping, direction_a, direction_b


def compare_headers(header_a: NRRDHeader, header_b: NRRDHeader, custom_field_map: Optional[NRRDFieldMap] = None,
                    ignore_fields: Iterable[str] = (), rtol: float = 1e-9, atol: float = 1e-12,
                    grid_rtol: float = 1e-6, grid_atol: float = 1e-8) -> NRRDHeaderComparison:
    """Compare two NRRD headers without comparing their data.

    The coordinate definition, value type and custom metadata are compared separately. The ``encoding``, ``endian``,
    ``datafile``, ``lineskip`` and ``byteskip`` fields only describe storage and are ignored.

    Parameters
    ----------
    header_a, header_b : :class:`dict` (:class:`str`, :obj:`Object`)
        Parsed headers, as returned by :func:`nrrd.read_header`.
    custom_field_map : :class:`dict` (:class:`str`, :class:`str`), optional
        Custom field types, used to decide which fields are custom metadata.
    ignore_fields : iterable of :class:`str`, optional
        Additional header field names to ignore when comparing custom metadata.
    rtol, atol : :class:`float`, optional
        Relative and absolute tolerance for numeric header values.
    grid_rtol, grid_atol : :class:`float`, optional
        Relative and absolute tolerance for space directions and origins.

    Returns
    -------
    comparison : :class:`NRRDHeaderComparison`
        Detailed comparison result.
    """

    comparison, _, _, _ = _compare_header_pair(header_a, header_b, None, None, custom_field_map, ignore_fields,
                                               rtol, atol, grid_rtol, grid_atol, True)
    return comparison


def compare(a: Union[str, os.PathLike, Tuple[npt.NDArray, NRRDHeader]],
            b: Union[str, os.PathLike, Tuple[npt.NDArray, NRRDHeader]],
            custom_field_map: Optional[NRRDFieldMap] = None, ignore_fields: Iterable[str] = (),
            rtol: float = 1e-5, atol: float = 1e-8, equal_nan: bool = True, index_order: str = 'F') \
        -> NRRDComparison:
    """Compare two NRRD files or ``(data, header)`` pairs.

    Storage differences such as compression (``raw``, ``gzip``, ``bzip2``) and byte order do not affect the result.
    If the two headers describe the same physical grid but order or reverse the axes, the values are compared at the
    matching physical points, so an F-order/C-order rearrangement of the same data is considered equivalent.

    When the headers do not carry enough direction information (``space directions``), the function does not claim
    that the spatial data is consistent. In that case :attr:`NRRDComparison.spatial_equivalence` is :obj:`None` and
    :attr:`NRRDComparison.verdict` only reports whether the arrays are the same.

    Parameters
    ----------
    a, b : :class:`str`, path-like or (:class:`numpy.ndarray`, :class:`dict`)
        Dataset to compare, given either as a filename or as a ``(data, header)`` pair. For a pair, the array is
        interpreted in the order given by :obj:`index_order`; it is internally normalized to header order.
    custom_field_map : :class:`dict` (:class:`str`, :class:`str`), optional
        Custom field types used when reading files and when deciding which fields are custom metadata.
    ignore_fields : iterable of :class:`str`, optional
        Additional header field names to ignore when comparing custom metadata.
    rtol, atol : :class:`float`, optional
        Relative and absolute tolerance for sample values.
    equal_nan : :class:`bool`, optional
        Whether NaNs in the same position compare equal. Default is :obj:`True`.
    index_order : {'F', 'C'}, optional
        Index order of the arrays when :obj:`a` or :obj:`b` is given as a ``(data, header)`` pair. Files are always
        read in header order internally.

    Returns
    -------
    comparison : :class:`NRRDComparison`
        Detailed comparison result. The object is truthy when :attr:`NRRDComparison.equivalent` is :obj:`True`.
    """

    if index_order not in ['F', 'C']:
        raise NRRDError('Invalid index order')

    data_a, header_a = _load(a, custom_field_map, index_order)
    data_b, header_b = _load(b, custom_field_map, index_order)

    header_comparison, mapping, direction_a, direction_b = _compare_header_pair(
        header_a, header_b, data_a, data_b, custom_field_map, ignore_fields, 1e-9, 1e-12, 1e-6, 1e-8, equal_nan)

    differences = OrderedDict(header_comparison.differences)
    for category in ('coordinate_definition', 'value_type', 'values', 'custom_metadata'):
        differences.setdefault(category, [])

    # Plain array comparison, without any axis reordering. This is what can be asserted when no direction evidence
    # is available.
    arrays_equal = _arrays_close(data_a, data_b, rtol, atol, equal_nan)

    values_equal = arrays_equal
    spatial_equivalence = header_comparison.spatial_equivalence
    notes = list(header_comparison.notes)

    if mapping is not None:
        aligned_b = mapping.align(data_b)
        values_equal = _arrays_close(data_a, aligned_b, rtol, atol, equal_nan)
        if not values_equal:
            if data_a.shape != aligned_b.shape:
                differences['values'].append(f'array shapes differ after aligning the axes: {data_a.shape} vs '
                                             f'{aligned_b.shape}')
            else:
                differences['values'].append('sample values differ at matching physical grid points')
        spatial_equivalence = bool(header_comparison.coordinate_definition_equal and values_equal)
    elif spatial_equivalence:
        # Directions are present and match, but a concrete axis mapping could not be built. Positional equality is
        # then the only available evidence.
        spatial_equivalence = bool(arrays_equal)
    else:
        if arrays_equal and 'Spatial equivalence cannot be established' not in ' '.join(notes):
            notes.append('Spatial equivalence cannot be established; only array equality is reported.')
        if not arrays_equal:
            if data_a.shape != data_b.shape:
                differences['values'].append(f'array shapes differ: {data_a.shape} vs {data_b.shape}')
            else:
                unequal = int(np.count_nonzero(~np.isclose(data_a, data_b, rtol=rtol, atol=atol,
                                                           equal_nan=equal_nan)
                                               if data_a.dtype.kind in 'fc' or data_b.dtype.kind in 'fc'
                                               else data_a != data_b))
                differences['values'].append(f'data arrays differ in {unequal} of {data_a.size} positions')

    equivalent = bool(header_comparison.coordinate_definition_equal and header_comparison.value_type_equal
                      and values_equal and header_comparison.custom_metadata_equal
                      and spatial_equivalence is True)

    differences = OrderedDict((name, values) for name, values in differences.items() if values)

    return NRRDComparison(
        coordinate_definition_equal=header_comparison.coordinate_definition_equal,
        value_type_equal=header_comparison.value_type_equal,
        values_equal=values_equal,
        custom_metadata_equal=header_comparison.custom_metadata_equal,
        arrays_equal=arrays_equal,
        spatial_equivalence=spatial_equivalence,
        equivalent=equivalent,
        differences=differences,
        notes=notes,
    )
