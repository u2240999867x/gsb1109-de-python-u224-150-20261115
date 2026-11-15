"""Support for NRRD headers that reference multiple local slice files.

The NRRD ``data file`` field can describe a volume split across several local
files in two ways:

1. ``data file: LIST`` followed by one filename per line, in slowest-axis order.
2. A printf-style sequence template containing a single integer conversion
   (e.g. ``slice-%03d.raw``), expanded with zero-based slice indices.

This module resolves the referenced files inside an authorized root directory,
validates the slice count, per-file byte counts and inter-slice axis spans, and
assembles the ordered volume. Missing intermediate slices leave a zero-filled
gap: later slices keep their original axis positions instead of shifting
forward.
"""

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from nrrd.errors import NRRDError

LIST_MARKER: str = 'LIST'
"""Value of the ``data file`` field that introduces an explicit filename list."""

SLICE_LIST_FIELD: str = 'data file list'
"""Header key holding the filenames attached to a ``LIST`` marker."""

_TEMPLATE_RE = re.compile(r'%[-+ #0]*\d*(?:\.\d+)?[di]')


@dataclass
class SliceSource:
    """One entry of an ordered multi-slice volume.

    Attributes
    ----------
    index:
        Zero-based position of the slice on the slowest-varying NRRD axis.
    filename:
        Resolved absolute path of the local slice file.
    byte_offset:
        Offset of this slice inside the assembled volume byte buffer.
    byte_count:
        Bytes contributed by this slice. Zero for a missing slice.
    span:
        Number of scalar elements this slice covers on the faster axes.
    exists:
        Whether the referenced file was present on disk.
    """

    index: int
    filename: str
    byte_offset: int
    byte_count: int
    span: int
    exists: bool


@dataclass
class SliceManifest:
    """Ordered description of how slice files map onto a volume.

    Attributes
    ----------
    axis_index:
        Zero-based NRRD axis along which the slices are laid out (the slowest
        axis, i.e. the last entry of ``sizes``).
    axis_size:
        Number of slice positions on the slice axis.
    slice_span:
        Scalar elements expected in every slice.
    slice_byte_count:
        Bytes expected in every present slice file.
    sources:
        Ordered slice sources, one per axis position, including missing ones.
    missing_indices:
        Axis positions whose slice file was missing.
    gap_ranges:
        Inclusive ``(first, last)`` axis-position ranges describing contiguous
        missing slices.
    """

    axis_index: int
    axis_size: int
    slice_span: int
    slice_byte_count: int
    sources: List[SliceSource] = field(default_factory=list)
    missing_indices: List[int] = field(default_factory=list)
    gap_ranges: List[Tuple[int, int]] = field(default_factory=list)


def is_sequence_template(data_filename: str) -> bool:
    """Return whether ``data_filename`` is a printf-style slice template.

    A valid template contains exactly one integer conversion specification
    (``%d`` or ``%i``, optionally with flags, width or precision).
    """

    if not isinstance(data_filename, str) or LIST_MARKER == data_filename:
        return False

    # A valid template contains exactly one percent sign, consumed by the single integer conversion
    matches = list(_TEMPLATE_RE.finditer(data_filename))
    return len(matches) == 1 and data_filename.count('%') == 1


def is_sliced_header(header: dict) -> bool:
    """Return whether ``header`` references multiple local slice files."""

    data_filename = header.get('datafile', header.get('data file', None))
    if not isinstance(data_filename, str):
        return False

    return data_filename == LIST_MARKER or is_sequence_template(data_filename)


def _expand_slice_filenames(header: dict) -> List[str]:
    data_filename = header.get('datafile', header.get('data file', None))
    sizes = np.asarray(header['sizes'])
    axis_size = int(sizes[-1])

    if data_filename == LIST_MARKER:
        filenames = header.get(SLICE_LIST_FIELD)
        if not isinstance(filenames, list) or not filenames:
            raise NRRDError("'data file: LIST' must be followed by at least one slice filename")
        filenames = list(filenames)
    elif is_sequence_template(data_filename):
        filenames = [data_filename % index for index in range(axis_size)]
    else:
        raise NRRDError(f'Invalid multi-slice data file specification: {data_filename}')

    if len(filenames) != axis_size:
        raise NRRDError(
            f'Number of slice files ({len(filenames)}) does not match the slice axis size ({axis_size})')

    return filenames


def _is_within(target: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(root), os.path.realpath(target)]) == os.path.realpath(root)
    except ValueError:
        return False


def _resolve_slice_path(filename: str, relative_root: Optional[str], sandbox_root: Optional[str]) -> str:
    """Resolve a slice path under the authorized-root rules.

    Relative paths are only resolved inside ``relative_root`` (the header directory or an explicit
    :obj:`data_root`). Absolute paths are accepted as-is unless ``sandbox_root`` is set explicitly, in which case
    they must also stay inside it.
    """

    if os.path.isabs(filename):
        resolved = os.path.realpath(filename)
        if sandbox_root is not None and not _is_within(resolved, sandbox_root):
            raise NRRDError(
                f'Slice file escapes the authorized root directory: {filename}')
        return resolved

    if relative_root is None:
        raise NRRDError(
            'An authorized root directory is required to resolve relative slice file paths. '
            'Pass the header filename or an explicit data root')

    resolved = os.path.realpath(os.path.join(relative_root, filename))
    if not _is_within(resolved, relative_root):
        raise NRRDError(
            f'Relative slice file escapes the authorized root directory: {filename}')

    return resolved


def _build_gap_ranges(missing_indices: List[int]) -> List[Tuple[int, int]]:
    gap_ranges: List[Tuple[int, int]] = []
    for index in missing_indices:
        if gap_ranges and index == gap_ranges[-1][1] + 1:
            gap_ranges[-1] = (gap_ranges[-1][0], index)
        else:
            gap_ranges.append((index, index))
    return gap_ranges


def read_sliced_data(header: dict,
                     filename: Optional[str] = None,
                     index_order: str = 'F',
                     data_root: Optional[str] = None) -> Tuple[np.ndarray, SliceManifest]:
    """Assemble ordered volume data from multiple local slice files.

    Parameters
    ----------
    header:
        Parsed NRRD header whose ``data file`` field uses ``LIST`` or a sequence
        template.
    filename:
        Path of the header file. Its directory is the default authorized root
        for relative slice paths.
    index_order:
        ``'F'`` (default) or ``'C'``, matching :meth:`nrrd.read_data`.
    data_root:
        Explicit authorized root directory. Relative slice paths are only
        resolved inside this directory, and absolute paths are additionally
        confined to it. When omitted, the directory of :obj:`filename` is used
        to resolve relative paths, while absolute paths remain unrestricted.

    Returns
    -------
    data:
        Assembled, ordered volume array, shaped and transposed like the output
        of :meth:`nrrd.read_data`. Missing slices are zero-filled.
    manifest:
        :class:`SliceManifest` recording the ordered sources, byte counts and
        gaps.
    """

    if index_order not in ['F', 'C']:
        raise NRRDError('Invalid index order')

    # Imported lazily to avoid a circular import between reader.py and this module.
    from nrrd.reader import _determine_datatype

    encoding = header['encoding']
    if encoding not in ['raw', 'ASCII', 'ascii', 'text', 'txt']:
        raise NRRDError(
            f"Multi-slice 'data file' is only supported with raw or ASCII encoding, got: {encoding}")

    line_skip = header.get('lineskip', header.get('line skip', 0))
    byte_skip = header.get('byteskip', header.get('byte skip', 0))
    if line_skip < 0:
        raise NRRDError('Invalid lineskip, allowed values are greater than or equal to 0')
    if byte_skip < 0:
        raise NRRDError('Invalid byteskip for multi-slice data, allowed values are greater than or equal to 0')

    dtype = _determine_datatype(header)
    sizes = np.asarray(header['sizes'])
    total_data_points = int(sizes.prod(dtype=np.int64))
    axis_index = int(len(sizes)) - 1
    axis_size = int(sizes[-1])
    slice_span = int(sizes[:-1].prod(dtype=np.int64)) if axis_index > 0 else 1
    slice_byte_count = slice_span * dtype.itemsize

    # Relative paths are anchored at the explicit data root, otherwise at the header file's directory.
    relative_root = data_root
    if relative_root is None and filename is not None:
        relative_root = os.path.dirname(os.path.abspath(filename))
    if relative_root is not None:
        relative_root = os.path.realpath(relative_root)

    # An explicit data root also acts as a sandbox for absolute paths; the implicit header directory does not.
    sandbox_root = os.path.realpath(data_root) if data_root is not None else None

    filenames = _expand_slice_filenames(header)

    buffer = bytearray(total_data_points * dtype.itemsize)
    sources: List[SliceSource] = []
    missing_indices: List[int] = []

    for index, slice_filename in enumerate(filenames):
        resolved = _resolve_slice_path(slice_filename, relative_root, sandbox_root)
        byte_offset = index * slice_byte_count
        source = SliceSource(
            index=index,
            filename=resolved,
            byte_offset=byte_offset,
            byte_count=0,
            span=slice_span,
            exists=False,
        )

        if not os.path.isfile(resolved):
            # Keep the axis position and zero-fill the gap; later slices stay at
            # their own positions instead of shifting forward.
            missing_indices.append(index)
            sources.append(source)
            continue

        with open(resolved, 'rb') as slice_fh:
            for _ in range(line_skip):
                slice_fh.readline()
            if byte_skip > 0:
                slice_fh.seek(byte_skip, os.SEEK_CUR)

            if encoding == 'raw':
                chunk = slice_fh.read()
                if len(chunk) != slice_byte_count:
                    raise NRRDError(
                        f'Slice file {slice_filename} contains {len(chunk)} bytes, expected '
                        f'{slice_byte_count} bytes for {slice_span} elements')
                buffer[byte_offset:byte_offset + slice_byte_count] = chunk
            else:
                slice_data = np.fromfile(slice_fh, dtype, sep=' ')
                if slice_data.size != slice_span:
                    raise NRRDError(
                        f'Slice file {slice_filename} contains {slice_data.size} elements, expected '
                        f'{slice_span}')
                buffer[byte_offset:byte_offset + slice_byte_count] = slice_data.tobytes()

        source.exists = True
        source.byte_count = slice_byte_count
        sources.append(source)

    manifest = SliceManifest(
        axis_index=axis_index,
        axis_size=axis_size,
        slice_span=slice_span,
        slice_byte_count=slice_byte_count,
        sources=sources,
        missing_indices=missing_indices,
        gap_ranges=_build_gap_ranges(missing_indices),
    )

    data = np.frombuffer(bytes(buffer), dtype)
    if data.size != total_data_points:
        raise NRRDError(
            f'Size of the data does not equal the product of all the dimensions: '
            f'{total_data_points}-{data.size}={total_data_points - data.size}')

    data = np.reshape(data, tuple(sizes[::-1]))
    if index_order == 'F':
        data = data.T

    return data, manifest
