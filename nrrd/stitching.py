from typing import List, Optional, Tuple

import numpy as np

from nrrd.errors import NRRDError
from nrrd.types import NRRDHeader


def _block_geometry(header: NRRDHeader) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Extract (sizes, directions, origin) from a NRRD header.

    Directions are returned as a matrix where row `a` is the direction vector (including spacing) of axis `a`.
    Non-spatial axes (`none` rows) are treated as unit axes. When no geometry fields are present, an identity
    grid anchored at the coordinate origin is assumed.
    """

    sizes = np.atleast_1d(np.asarray(header['sizes'], dtype=int))
    dimension = len(sizes)

    if 'space directions' in header:
        directions = []
        for axis, vector in enumerate(header['space directions']):
            if vector is None or np.all(np.isnan(np.asarray(vector, dtype=float))):
                vector = np.zeros(dimension)
                vector[axis] = 1.0
            directions.append(np.asarray(vector, dtype=float))
        directions = np.array(directions, dtype=float)
    elif 'spacings' in header:
        directions = np.diag(np.asarray(header['spacings'], dtype=float))
    else:
        directions = np.eye(dimension)

    if directions.shape != (dimension, dimension):
        raise NRRDError(f'Stitching requires one direction vector per axis with matching space dimension, '
                        f'got directions with shape {directions.shape} for {dimension} sizes')

    if 'space origin' in header:
        origin = np.asarray(header['space origin'], dtype=float)
    else:
        origin = np.zeros(dimension)

    return sizes, directions, origin


def _canonical_directions(directions: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Return sign-normalized directions and the sign (+1/-1) applied to each axis.

    The sign of each axis is chosen so that the largest-magnitude component of its direction vector is positive.
    """

    signs = np.ones(len(directions))
    canonical = directions.copy()
    for axis, vector in enumerate(directions):
        pivot = np.argmax(np.abs(vector))
        if vector[pivot] < 0:
            signs[axis] = -1.0
            canonical[axis] = -vector
    return canonical, signs


def _flip_block(data: np.ndarray, origin: np.ndarray, directions: np.ndarray, sizes: np.ndarray,
                flips: List[bool]) -> Tuple[np.ndarray, np.ndarray]:
    """Flip block axes so that all direction vectors point the canonical way.

    Flipping axis `a` reverses the data along that axis and moves the origin to the far end of the block:
    ``origin' = origin + (size_a - 1) * direction_a``.
    """

    origin = origin.copy()
    for axis, flip in enumerate(flips):
        if flip:
            data = np.flip(data, axis=axis)
            origin = origin + (sizes[axis] - 1) * directions[axis]
    return data, origin


def stitch(blocks: List[Tuple[np.ndarray, NRRDHeader]], fill_value: Optional[float] = None,
           tolerance: float = 1e-6) -> Tuple[np.ndarray, NRRDHeader, np.ndarray]:
    """Stitch multiple NRRD blocks of the same volume into a single unified grid.

    Blocks are placed on a common voxel grid using their grid directions, spacings and origins. Blocks whose
    axes point in the negative direction are flipped automatically. Blocks are never resampled or interpolated:
    every block origin must land exactly on the reference grid defined by the first block. A block offset by a
    fractional number of voxels (for example half a voxel) raises :class:`NRRDError`.

    Where blocks overlap and agree on the voxel value, the value is kept. Where overlapping voxels disagree,
    the first written value is kept and the voxel is flagged in the returned conflict mask.

    Args:
        blocks: List of ``(data, header)`` tuples as returned by :func:`nrrd.read`.
        fill_value: Value used for voxels not covered by any block. Defaults to ``NaN`` for floating point
            output and ``0`` otherwise.
        tolerance: Absolute tolerance used when comparing direction vectors and checking that origins land on
            whole voxel indices.

    Returns:
        Tuple of ``(data, header, conflicts)`` with the stitched array, a header describing the unified grid
        (updated ``sizes``, ``space directions`` and ``space origin``), and a boolean mask of the same shape as
        ``data`` that is :obj:`True` wherever overlapping blocks disagreed on the voxel value.

    Raises:
        NRRDError: If the blocks do not share the same grid directions and spacings (up to sign), or if a block
            origin is offset by a fractional number of voxels from the reference grid.
    """

    if len(blocks) == 0:
        raise NRRDError('No blocks to stitch')

    dimension = len(np.atleast_1d(blocks[0][1]['sizes']))

    # Normalize every block onto the canonical (sign-normalized) grid of the first block
    reference_directions = None
    reference_origin = None
    normalized = []
    for index, (data, header) in enumerate(blocks):
        sizes, directions, origin = _block_geometry(header)
        data = np.asarray(data)
        if data.ndim != len(sizes) or tuple(data.shape) != tuple(sizes):
            raise NRRDError(f'Block {index} data shape {data.shape} does not match header sizes {list(sizes)}')
        if len(sizes) != dimension:
            raise NRRDError(f'Block {index} has dimension {len(sizes)}, expected {dimension}')

        canonical, _ = _canonical_directions(directions)
        if reference_directions is None:
            reference_directions = canonical
        elif not np.allclose(canonical, reference_directions, atol=tolerance, rtol=0):
            raise NRRDError(
                f'Block {index} has grid directions/spacings that differ from the reference block '
                f'(beyond a sign flip) and cannot be stitched without resampling')

        flips = [not np.allclose(directions[axis], reference_directions[axis], atol=tolerance, rtol=0)
                 for axis in range(dimension)]
        data, origin = _flip_block(data, origin, directions, sizes, flips)
        if reference_origin is None:
            reference_origin = origin
        normalized.append((data, sizes, origin))

    # Locate each block on the reference grid; offsets must be whole voxels
    offsets = []
    for index, (data, sizes, origin) in enumerate(normalized):
        exact, residual, _, _ = np.linalg.lstsq(reference_directions.T, origin - reference_origin, rcond=None)
        if residual.size and np.any(residual > tolerance):
            raise NRRDError(f'Block {index} origin does not lie on the reference grid')
        offset = np.rint(exact).astype(int)
        if not np.allclose(exact, offset, atol=tolerance, rtol=0):
            raise NRRDError(
                f'Block {index} is offset by {list(exact)} voxels from the reference grid; blocks offset by a '
                f'fractional (e.g. half) voxel cannot be stitched directly without interpolation')
        offsets.append(offset)

    lower = np.min([offset for offset in offsets], axis=0)
    upper = np.max([offset + sizes - 1 for (offset, (_, sizes, _)) in zip(offsets, normalized)], axis=0)
    out_sizes = upper - lower + 1

    out_dtype = np.result_type(*[data for data, _, _ in normalized])
    if fill_value is None:
        fill_value = np.nan if np.issubdtype(out_dtype, np.floating) else 0

    out_data = np.full(tuple(out_sizes), fill_value, dtype=out_dtype)
    conflicts = np.zeros(tuple(out_sizes), dtype=bool)
    filled = np.zeros(tuple(out_sizes), dtype=bool)

    for (data, sizes, origin), offset in zip(normalized, offsets):
        placement = tuple(slice(o - l, o - l + n) for o, l, n in zip(offset, lower, sizes))
        block_data = data.astype(out_dtype, copy=False)
        overlap = filled[placement]
        if np.any(overlap):
            existing = out_data[placement]
            if np.issubdtype(out_dtype, np.floating):
                disagree = (existing != block_data) & ~(np.isnan(existing) & np.isnan(block_data))
            else:
                disagree = existing != block_data
            conflicts[placement] |= disagree & overlap
        out_data[placement] = np.where(filled[placement], out_data[placement], block_data)
        filled[placement] = True

    header = dict(blocks[0][1])
    header['dimension'] = dimension
    header['sizes'] = out_sizes
    if 'space directions' in header:
        header['space directions'] = reference_directions
    if 'space origin' in header or 'space directions' in header:
        header['space origin'] = reference_origin + reference_directions.T @ lower

    return out_data, header, conflicts
