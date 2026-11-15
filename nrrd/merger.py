"""Stitching spatially tiled NRRD blocks back into a single coverage volume.

The blocks of one volume are delivered as separate NRRD files that share a common voxel grid but cover different
regions of space. Each block declares its placement through the ``space directions`` and ``space origin`` header
fields. This module places the blocks on that common grid and copies the voxels without any resampling or
interpolation.

The geometry rules are enforced strictly:

* All blocks must use the same directions (orientation *and* spacing), including negative axis directions.
* Origins must differ by an integer number of voxels. An offset of half a voxel means the blocks do not share a
  grid and cannot be stitched without interpolation, so :class:`~nrrd.errors.NRRDGridError` is raised.
* Where blocks overlap, the voxel values must agree. Disagreements are recorded as conflicts on a conflict mask
  and, by default, raise :class:`~nrrd.errors.NRRDConflictError`. Nothing is interpolated or averaged away.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np
import numpy.typing as npt
from typing_extensions import Literal

from nrrd.errors import NRRDConflictError, NRRDGridError
from nrrd.reader import read
from nrrd.types import NRRDHeader

#: Tolerance, in voxels, when checking that block origins line up on a common grid.
DEFAULT_ALIGNMENT_TOLERANCE: float = 1e-6

#: Relative/absolute tolerance when comparing direction matrices and spacings.
DEFAULT_GEOMETRY_TOLERANCE: float = 1e-6

#: Maximum number of individual conflict details kept on the report.
_MAX_REPORTED_CONFLICTS = 10

NRRDBlock = Tuple[npt.NDArray, NRRDHeader]


@dataclass
class NRRDBlockPlacement:
    """Where one block was placed inside the merged volume."""

    index: int
    offset: Tuple[int, ...]
    slices: Tuple[slice, ...]


@dataclass
class NRRDMergeReport:
    """Result of a :func:`merge_nrrd` call.

    Attributes:
        shape: Shape of the merged volume in NRRD (Fortran-ordered) axis order.
        origin: ``space origin`` of the merged volume, i.e. the world position of voxel ``(0, 0, ...)``.
        spatial_axes: Array axes that carry a spatial direction.
        directions: Direction matrix of the spatial axes, one direction vector per row.
        placements: Placement details for every input block, in input order.
        covered_mask: Boolean mask of voxels covered by at least one block.
        conflict_mask: Boolean mask of overlapping voxels whose block values disagree.
        conflict_count: Number of ``True`` voxels in :attr:`conflict_mask`.
        conflicts: Details for the first conflicting voxels, each containing the voxel index in the merged
            volume and the values supplied by the two disagreeing blocks.
    """

    shape: Tuple[int, ...]
    origin: npt.NDArray
    spatial_axes: Tuple[int, ...]
    directions: npt.NDArray
    placements: List[NRRDBlockPlacement] = field(default_factory=list)
    covered_mask: Optional[npt.NDArray] = None
    conflict_mask: Optional[npt.NDArray] = None
    conflict_count: int = 0
    conflicts: List[dict] = field(default_factory=list)


@dataclass
class _Geometry:
    index: int
    ndim: int
    shape: Tuple[int, ...]
    spatial_axes: Tuple[int, ...]
    directions: npt.NDArray
    origin: npt.NDArray


def _normalize_directions(header: NRRDHeader, ndim: int) -> Tuple[npt.NDArray, Tuple[int, ...]]:
    """Return the full (ndim x spatial_dim) direction matrix and the spatial axis indices."""
    raw = header.get('space directions')

    if raw is None:
        # NRRD defaults to unit spacing along every axis.
        return np.eye(ndim, dtype=float), tuple(range(ndim))

    if isinstance(raw, np.ndarray):
        full = np.array(raw, dtype=float)
        if full.ndim != 2 or full.shape[0] != ndim:
            raise NRRDGridError(
                f"Block 'space directions' must be a matrix with one row per axis (expected {ndim} rows, "
                f'got shape {full.shape})')
        spatial_axes = tuple(int(axis) for axis in np.where(~np.isnan(full).all(axis=1))[0])
        return full, spatial_axes

    # 'double vector list' representation: one optional vector per axis.
    vectors = [None if vec is None else np.asarray(vec, dtype=float) for vec in raw]
    if len(vectors) != ndim:
        raise NRRDGridError(
            f"Block 'space directions' must contain one entry per axis (expected {ndim}, got {len(vectors)})")

    spatial_vectors = [vec for vec in vectors if vec is not None]
    if not spatial_vectors:
        raise NRRDGridError("Block 'space directions' contains no spatial axis vectors")

    spatial_dim = len(spatial_vectors[0])
    if any(len(vec) != spatial_dim for vec in spatial_vectors):
        raise NRRDGridError("All spatial direction vectors must have the same length")

    full = np.full((ndim, spatial_dim), np.nan, dtype=float)
    spatial_axes = []
    for axis, vec in enumerate(vectors):
        if vec is not None:
            full[axis] = vec
            spatial_axes.append(axis)

    return full, tuple(spatial_axes)


def _extract_geometry(index: int, data: npt.NDArray, header: NRRDHeader) -> _Geometry:
    ndim = data.ndim

    if 'dimension' in header and int(header['dimension']) != ndim:
        raise NRRDGridError(
            f"Block {index} header 'dimension' ({header['dimension']}) does not match data ndim ({ndim})")

    if 'sizes' in header and [int(size) for size in header['sizes']] != list(data.shape):
        raise NRRDGridError(f"Block {index} header 'sizes' do not match the data shape")

    full_directions, spatial_axes = _normalize_directions(header, ndim)

    if not spatial_axes:
        raise NRRDGridError(f'Block {index} has no spatial axes and cannot be placed in world space')

    if 'space origin' not in header:
        raise NRRDGridError(
            f"Block {index} is missing the 'space origin' field, so its spatial placement is unknown")

    origin = np.asarray(header['space origin'], dtype=float)
    directions = full_directions[list(spatial_axes), :]
    spatial_dim = directions.shape[1]

    if origin.shape != (spatial_dim,):
        raise NRRDGridError(
            f'Block {index} origin has {origin.shape[0]} components but the spatial frame has {spatial_dim} '
            'dimensions')

    if directions.shape[0] != spatial_dim or np.linalg.matrix_rank(directions) != spatial_dim:
        raise NRRDGridError(
            f'Block {index} spatial directions must form a full-rank square matrix, got shape {directions.shape}')

    # The optional 'spacings' field must agree with the norms of the direction vectors.
    if 'spacings' in header:
        spacings = np.asarray(header['spacings'], dtype=float)
        for row, axis in enumerate(spatial_axes):
            spacing = float(spacings[axis])
            if np.isfinite(spacing) and not np.isclose(
                    spacing, np.linalg.norm(directions[row]), rtol=DEFAULT_GEOMETRY_TOLERANCE,
                    atol=DEFAULT_GEOMETRY_TOLERANCE):
                raise NRRDGridError(
                    f"Block {index} 'spacings' value {spacing} on axis {axis} disagrees with its space "
                    f'direction norm {np.linalg.norm(directions[row])}')

    return _Geometry(index, ndim, tuple(data.shape), spatial_axes, directions, origin)


def _check_common_grid(geometries: Sequence[_Geometry]) -> None:
    reference = geometries[0]

    for geometry in geometries[1:]:
        if geometry.ndim != reference.ndim:
            raise NRRDGridError(
                f'Block {geometry.index} has {geometry.ndim} dimensions, expected {reference.ndim}')

        if geometry.spatial_axes != reference.spatial_axes:
            raise NRRDGridError(
                f'Block {geometry.index} spatial axes {geometry.spatial_axes} do not match block '
                f'{reference.index} spatial axes {reference.spatial_axes}')

        if geometry.directions.shape != reference.directions.shape or not np.allclose(
                geometry.directions, reference.directions, rtol=DEFAULT_GEOMETRY_TOLERANCE,
                atol=DEFAULT_GEOMETRY_TOLERANCE):
            raise NRRDGridError(
                f'Block {geometry.index} space directions\n{geometry.directions}\ndo not match block '
                f'{reference.index} space directions\n{reference.directions}\n'
                'Blocks with different orientation or spacing cannot be stitched without interpolation')

    # Non-spatial axes have no geometric placement; their sizes must be identical across blocks.
    non_spatial_axes = [axis for axis in range(reference.ndim) if axis not in reference.spatial_axes]
    for axis in non_spatial_axes:
        sizes = {geometry.shape[axis] for geometry in geometries}
        if len(sizes) != 1:
            raise NRRDGridError(
                f'Non-spatial axis {axis} has mismatched sizes between blocks: {sorted(sizes)}')


def _voxel_offset(geometry: _Geometry, reference: _Geometry,
                  alignment_tolerance: float) -> npt.NDArray:
    """Return the integer voxel offset of a block relative to the reference block."""
    origin_delta = geometry.origin - reference.origin
    # World positions satisfy p = origin + index @ D, so the index offset is origin_delta @ inv(D).
    fractional_offset = origin_delta @ np.linalg.inv(reference.directions)
    integer_offset = np.rint(fractional_offset)

    if not np.allclose(fractional_offset, integer_offset, atol=alignment_tolerance, rtol=0.0):
        raise NRRDGridError(
            f'Block {geometry.index} origin is offset by {fractional_offset.tolist()} voxels relative to block '
            f'{reference.index}. A non-integer (e.g. half-voxel) shift means the blocks do not share a voxel '
            'grid and cannot be stitched without interpolation')

    return integer_offset.astype(np.int64)


def _values_differ(current: npt.NDArray, incoming: npt.NDArray, mask: npt.NDArray) -> npt.NDArray:
    """Voxels in ``mask`` whose existing and incoming samples disagree.

    ``mask`` only covers sampled, already-occupied voxels, so NaN handling belongs to the caller.
    """
    if not mask.any():
        return mask

    if np.issubdtype(current.dtype, np.floating):
        return mask & ~np.isclose(current, incoming, rtol=DEFAULT_GEOMETRY_TOLERANCE,
                                  atol=DEFAULT_GEOMETRY_TOLERANCE)

    return mask & (current != incoming)


def merge_nrrd(blocks: Sequence[NRRDBlock], fill_value: Any = None,
               on_conflict: Literal['raise', 'keep_first'] = 'raise',
               alignment_tolerance: float = DEFAULT_ALIGNMENT_TOLERANCE) -> Tuple[npt.NDArray, NRRDHeader,
                                                                                   NRRDMergeReport]:
    """Merge spatially tiled NRRD blocks into one coverage volume.

    Args:
        blocks: ``(data, header)`` pairs as returned by :func:`nrrd.read`, in the default Fortran axis order.
            Every block must provide ``space directions`` and ``space origin``.
        fill_value: Value used for voxels not covered by any block. Defaults to ``NaN`` for floating point data
            and zero otherwise.
        on_conflict: ``'raise'`` (default) raises :class:`~nrrd.errors.NRRDConflictError` when overlapping voxels
            disagree; ``'keep_first'`` keeps the value of the first block covering the voxel.
        alignment_tolerance: Maximum deviation, in voxels, allowed when checking that origins line up on the
            common integer grid.

    Returns:
        A tuple of the merged data array, an NRRD header describing it (ready for :func:`nrrd.write`) and a
        :class:`NRRDMergeReport` with placement and conflict information.

    Raises:
        NRRDGridError: If the blocks do not share a common voxel grid, including half-voxel origin shifts.
        NRRDConflictError: If overlapping voxels disagree and ``on_conflict='raise'``.
    """
    if not blocks:
        raise NRRDGridError('At least one NRRD block is required')

    datas = [block[0] for block in blocks]
    headers = [block[1] for block in blocks]

    dtype_strs = {data.dtype.str for data in datas}
    if len(dtype_strs) != 1:
        raise NRRDGridError(
            f'All blocks must share the same numpy dtype; found {sorted(dtype_strs)}. Cast the arrays explicitly '
            'instead of relying on implicit conversion')

    geometries = [_extract_geometry(index, data, header) for index, (data, header) in enumerate(blocks)]
    _check_common_grid(geometries)

    reference = geometries[0]
    spatial_axes = reference.spatial_axes

    # Offset of every block along each spatial axis, relative to the reference block's voxel indices.
    spatial_offsets = [
        _voxel_offset(geometry, reference, alignment_tolerance) if geometry.index != 0
        else np.zeros(len(spatial_axes), dtype=np.int64)
        for geometry in geometries
    ]

    # Determine the common coverage extents and shift every block so output indices start at zero.
    spatial_starts = np.zeros(len(spatial_axes), dtype=np.int64)
    spatial_ends = np.zeros(len(spatial_axes), dtype=np.int64)
    for geometry, offset in zip(geometries, spatial_offsets):
        for row, axis in enumerate(spatial_axes):
            spatial_starts[row] = min(spatial_starts[row], offset[row])
            spatial_ends[row] = max(spatial_ends[row], offset[row] + geometry.shape[axis])

    output_shape = list(reference.shape)
    for row, axis in enumerate(spatial_axes):
        output_shape[axis] = int(spatial_ends[row] - spatial_starts[row])
    output_shape = tuple(output_shape)

    dtype = datas[0].dtype
    if fill_value is None:
        fill_value = np.nan if np.issubdtype(dtype, np.floating) else 0
    output = np.full(output_shape, fill_value, dtype=dtype)

    occupancy = np.zeros(output_shape, dtype=bool)
    ownership = np.full(output_shape, -1, dtype=np.int64)
    conflict_mask = np.zeros(output_shape, dtype=bool)
    placements: List[NRRDBlockPlacement] = []
    conflicts: List[dict] = []

    for geometry, offset_spatial, data in zip(geometries, spatial_offsets, datas):
        offset = [0] * geometry.ndim
        # Index the slices by axis number so interleaved non-spatial axes keep their position.
        slices: List[Optional[slice]] = [None] * geometry.ndim
        for row, axis in enumerate(spatial_axes):
            start = int(offset_spatial[row] - spatial_starts[row])
            offset[axis] = start
            slices[axis] = slice(start, start + geometry.shape[axis])
        for axis in range(geometry.ndim):
            if axis not in spatial_axes:
                slices[axis] = slice(0, geometry.shape[axis])

        block_slices = tuple(slices)
        placements.append(NRRDBlockPlacement(geometry.index, tuple(offset), block_slices))

        overlap = occupancy[block_slices]
        # NaN voxels carry no sample: they neither cover a voxel nor participate in conflict checks.
        if np.issubdtype(data.dtype, np.floating):
            sampled = ~np.isnan(data)
        else:
            sampled = np.ones(data.shape, dtype=bool)

        differing = _values_differ(output[block_slices], data, overlap & sampled)

        if differing.any():
            conflict_mask[block_slices] |= differing
            local_positions = np.argwhere(differing)
            for local_position in local_positions[:_MAX_REPORTED_CONFLICTS - len(conflicts)]:
                voxel = tuple(int(coord) for coord in local_position + np.asarray(offset))
                owner_index = int(ownership[block_slices][tuple(local_position)])
                conflicts.append({
                    'voxel': voxel,
                    'first_block': owner_index,
                    'first_value': output[block_slices][tuple(local_position)],
                    'conflicting_block': geometry.index,
                    'conflicting_value': data[tuple(local_position)],
                })
                if len(conflicts) >= _MAX_REPORTED_CONFLICTS:
                    break

        # Only write into voxels without an existing sample. Equal overlaps keep the same value, conflicts keep
        # the first block's value and unsampled (NaN) voxels never overwrite anything.
        newly_covered = sampled & ~overlap
        output[block_slices] = np.where(newly_covered, data, output[block_slices])
        occupancy[block_slices] |= sampled
        ownership[block_slices] = np.where(newly_covered, geometry.index, ownership[block_slices])

    # World position of output voxel zero: shift the reference origin by the global minimum index.
    global_origin = reference.origin + spatial_starts @ reference.directions

    report = NRRDMergeReport(
        shape=output_shape,
        origin=global_origin,
        spatial_axes=spatial_axes,
        directions=reference.directions,
        placements=placements,
        covered_mask=occupancy,
        conflict_mask=conflict_mask,
        conflict_count=int(conflict_mask.sum()),
        conflicts=conflicts,
    )

    merged_header: NRRDHeader = dict(headers[0])
    merged_header['dimension'] = len(output_shape)
    merged_header['sizes'] = list(output_shape)
    merged_header['space origin'] = global_origin
    merged_header.pop('datafile', None)
    merged_header.pop('data file', None)

    if report.conflict_count > 0 and on_conflict == 'raise':
        first = conflicts[0]
        raise NRRDConflictError(
            f"{report.conflict_count} conflicting voxel(s) found while merging {len(blocks)} blocks; first "
            f"conflict at voxel {first['voxel']} between block {first['first_block']} and block "
            f"{first['conflicting_block']}", report)

    return output, merged_header, report


def merge_nrrd_files(filenames: Sequence[str], fill_value: Any = None,
                     on_conflict: Literal['raise', 'keep_first'] = 'raise',
                     alignment_tolerance: float = DEFAULT_ALIGNMENT_TOLERANCE
                     ) -> Tuple[npt.NDArray, NRRDHeader, NRRDMergeReport]:
    """Read NRRD files from disk and merge them via :func:`merge_nrrd`.

    Args:
        filenames: Paths to the NRRD block files, in any order.
        fill_value: See :func:`merge_nrrd`.
        on_conflict: See :func:`merge_nrrd`.
        alignment_tolerance: See :func:`merge_nrrd`.

    Returns:
        The same ``(data, header, report)`` tuple as :func:`merge_nrrd`.
    """
    blocks = [read(filename) for filename in filenames]
    return merge_nrrd(blocks, fill_value=fill_value, on_conflict=on_conflict,
                      alignment_tolerance=alignment_tolerance)
