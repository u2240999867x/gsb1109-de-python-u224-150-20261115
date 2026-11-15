"""Support for NRRD data stored in multiple detached slice files.

The NRRD ``data file`` field can name a single detached file, or it can request
that the data be split over one file per slice.  The two multi-file forms
supported here are:

* ``data file: LIST`` followed by one filename per line in the header.
* a printf-style sequence template such as ``data file: slice%03d.raw``.

Files are assembled in NRRD axis order.  A missing file leaves a gap at its
position along the slice axis; later files are never shifted towards the
beginning of the volume.
"""

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import numpy.typing as npt

from nrrd.errors import NRRDError

_TEMPLATE_SPEC_RE = re.compile(r'%(?:[-+ #0]*)(?:\d+)?(?:\.\d+)?([diu])')
_TEMPLATE_ANY_RE = re.compile(r'%(?:[-+ #0]*)(?:\d+)?(?:\.\d+)?[diuxX]|%%')


class SegmentInfo(dict):
    """Information about one position along the slice axis.

    The object is a regular :class:`dict`, so both ``segment['source']`` and
    ``segment.source`` work.  This makes it convenient to inspect from tests
    and to serialize into a header or report.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any):
        self[name] = value


class MultiFileVolume(np.ndarray):
    """Volume assembled from multiple slice files.

    The returned object is a normal :class:`numpy.ndarray` (a subclass) with
    extra attributes describing the provenance of each slice:

    ``segments``
        One :class:`SegmentInfo` per position along the slice axis.
    ``sources``
        The filename for each position, or ``None`` for a gap.
    ``gaps``
        Slice positions that had no usable file.
    ``axis``
        NRRD axis index used as the slice axis (0 is fastest-varying).
    ``axis_span``
        Physical span between the first and last present slice.
    ``axis_spacing``
        Physical distance between adjacent slice positions.
    """

    def __new__(cls, input_array: npt.NDArray, segments: Optional[Sequence[SegmentInfo]] = None,
                axis: int = -1, axis_spacing: float = 1.0, numpy_axis: Optional[int] = None,
                report: Optional[Dict[str, Any]] = None):
        obj = np.asarray(input_array).view(cls)
        obj.segments = list(segments) if segments is not None else []
        obj.sources = [segment.get('source') for segment in obj.segments]
        obj.paths = [segment.get('path') for segment in obj.segments]
        obj.gaps = [segment['index'] for segment in obj.segments
                    if segment.get('status') in ('missing', 'gap')]
        obj.axis = axis
        obj.numpy_axis = numpy_axis
        obj.axis_spacing = axis_spacing
        obj.axis_span = None
        present = [segment for segment in obj.segments if segment.get('status') == 'loaded']
        if present:
            obj.axis_span = axis_spacing * (present[-1]['index'] - present[0]['index'])
        obj.declared_axis_span = axis_spacing * (max(len(obj.segments) - 1, 0))
        obj.count = len(obj.segments)
        obj.loaded_count = len(present)
        obj.missing_count = obj.count - obj.loaded_count
        obj.mask = np.ones(obj.shape, dtype=bool)
        if numpy_axis is not None:
            for segment in obj.segments:
                if segment.get('status') in ('missing', 'gap'):
                    indexer = [slice(None)] * obj.ndim
                    indexer[numpy_axis] = segment['index']
                    obj.mask[tuple(indexer)] = False
        obj.report = report if report is not None else {}
        obj.summary = obj.report
        return obj

    def __array_finalize__(self, obj):
        if obj is None:
            return
        for name in ('segments', 'sources', 'paths', 'gaps', 'axis', 'numpy_axis',
                     'axis_spacing', 'axis_span', 'declared_axis_span', 'count',
                     'loaded_count', 'missing_count', 'mask', 'report', 'summary'):
            setattr(self, name, getattr(obj, name, None))


def _header_value(header: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in header:
            return header[name]
    return None


def _get_field(header: Dict[str, Any], *names: str) -> Any:
    return _header_value(header, *names)


def _as_sizes(header: Dict[str, Any]) -> np.ndarray:
    return np.asarray(header['sizes'], dtype=np.int64)


def _has_sequence_placeholder(template: str) -> bool:
    for match in _TEMPLATE_ANY_RE.finditer(template):
        if match.group(0) != '%%' and _TEMPLATE_SPEC_RE.fullmatch(match.group(0)):
            return True
    return False


def _format_sequence_name(template: str, index: int) -> str:
    """Expand every numeric placeholder in a sequence template.

    Using the placeholder itself for formatting preserves flags such as zero
    padding and width (``%03d``).  Multiple placeholders are all expanded to
    the same index, which is the conventional NRRD slice spelling.
    """

    pieces: List[str] = []
    position = 0
    replaced = 0
    for match in _TEMPLATE_ANY_RE.finditer(template):
        pieces.append(template[position:match.start()])
        text = match.group(0)
        if text == '%%':
            pieces.append('%')
        elif _TEMPLATE_SPEC_RE.fullmatch(text):
            try:
                pieces.append(text % index)
            except (TypeError, ValueError) as exc:
                raise NRRDError(f'Invalid data file sequence template {template!r}: {exc}') from exc
            replaced += 1
        else:
            pieces.append(text)
        position = match.end()
    pieces.append(template[position:])
    if replaced == 0:
        raise NRRDError(f'Invalid data file sequence template {template!r}: no numeric placeholder')
    return ''.join(pieces)


def _template_regex_basename(template: str) -> Optional[re.Pattern]:
    """Build a regex that matches a sequence template's basename.

    Only numeric conversions are considered sequence placeholders.  Literal
    percent signs are kept literal.  If there are multiple placeholders they
    must all expand to the same index, which is how NRRD slice templates are
    conventionally written.
    """

    pieces: List[str] = []
    position = 0
    numeric_groups = 0
    for match in _TEMPLATE_ANY_RE.finditer(template):
        pieces.append(re.escape(template[position:match.start()]))
        text = match.group(0)
        if text == '%%':
            pieces.append('%')
        elif _TEMPLATE_SPEC_RE.fullmatch(text):
            if numeric_groups == 0:
                pieces.append(r'(\d+)')
            else:
                pieces.append(r'\1')
            numeric_groups += 1
        else:
            return None
        position = match.end()
    pieces.append(re.escape(template[position:]))

    if numeric_groups == 0:
        return None
    return re.compile('^' + ''.join(pieces) + '$')
def _template_indices_on_disk(template: str) -> Tuple[Optional[str], List[int]]:
    """Return the scan directory and numeric indices matching a template.

    ``template`` is an absolute path or a path relative to the caller's
    authorized root.  The template regex itself is built from the basename,
    but the literal directory from the template is preserved.
    """

    directory, basename = os.path.split(template)
    regex = _template_regex_basename(basename)
    if regex is None:
        return None, []

    if not os.path.isdir(directory):
        return directory, []
    try:
        entries = os.listdir(directory)
    except OSError:
        return directory, []

    indices: List[int] = []
    for entry in entries:
        if os.path.isdir(os.path.join(directory, entry)):
            continue
        match = regex.match(entry)
        if not match:
            continue
        try:
            index = int(match.group(1))
        except (TypeError, ValueError):
            continue
        indices.append(index)
    return directory, indices


def _normalize_index_positions(raw_indices: Sequence[int], expected: int) -> List[int]:
    """Map filename indices to positions in the declared slice axis.

    NRRD sequence templates are sometimes one-based.  Once the first index is
    known, positions are normalized to ``0..expected-1``.  Gaps are preserved,
    which is the important property for missing middle slices.
    """

    if not raw_indices:
        return [], []

    unique = sorted(set(raw_indices))
    if len(unique) != len(raw_indices):
        raise NRRDError('Duplicate data file index in multi-file data reference')

    # Anchor positions at the lowest referenced index.  A LIST can start at
    # any index (e.g. slice_005.raw); the declared axis size fixes the count,
    # while the numeric names preserve holes for missing middle files.
    first = unique[0]
    positions = [index - first for index in raw_indices]
    if min(positions) < 0 or max(positions) >= expected:
        raise NRRDError(
            f'Data file index span {min(unique)}..{max(unique)} does not fit within '
            f'the declared slice axis of size {expected}')
    return positions


def _list_indices_from_names(names: Sequence[str]) -> Optional[List[int]]:
    indices: List[int] = []
    for name in names:
        stem = os.path.splitext(os.path.basename(name))[0]
        matches = re.findall(r'\d+', stem)
        if not matches:
            return None
        indices.append(int(matches[-1]))
    return indices


def _resolve_authorized(path: str, root: str, field_name: str = 'data file') -> str:
    """Resolve a filename and enforce that it remains inside ``root``."""

    root = os.path.abspath(root)
    root_real = os.path.realpath(root)

    if os.path.isabs(path):
        candidate = os.path.abspath(path)
    else:
        candidate = os.path.join(root, path)
    candidate = os.path.abspath(candidate)
    candidate_real = os.path.realpath(candidate)

    def within(value: str, base: str) -> bool:
        try:
            return os.path.commonpath([value, base]) == base
        except ValueError:
            return False

    # Check both the lexical path and the symlink-resolved path.  A symlink
    # inside the root must not be used to escape the authorized root.
    if not within(candidate, root) or not within(candidate_real, root_real):
        raise NRRDError(
            f'Data file path escapes authorized root {root!r}: {path!r}')

    return candidate


def _slice_axis_for_header(header: Dict[str, Any], slice_axis: Optional[Any],
                           reference_count: Optional[int]) -> int:
    dimension = int(header['dimension'])

    explicit = slice_axis if slice_axis is not None else _get_field(
        header, 'data file axis', 'datafile axis', 'slice axis', 'data axis')
    if explicit is not None:
        if isinstance(explicit, str):
            names = ['x', 'y', 'z', 'w', 'v', 'u']
            try:
                axis = names.index(explicit.lower())
            except ValueError:
                try:
                    axis = int(explicit)
                except ValueError:
                    raise NRRDError(f'Invalid slice axis: {explicit!r}')
        else:
            axis = int(explicit)
        if axis < 0:
            axis += dimension
        if axis < 0 or axis >= dimension:
            raise NRRDError(f'Slice axis {explicit!r} is outside the data dimension {dimension}')
        return axis

    # A declared count that matches the last axis is the conventional slice
    # stack.  A partial on-disk run is not a reliable axis signal; in that
    # case the last axis remains the default.
    sizes = _as_sizes(header)
    if reference_count is not None and int(sizes[dimension - 1]) == reference_count:
        return dimension - 1

    return dimension - 1


def _axis_spacing(header: Dict[str, Any], axis: int) -> float:
    directions = _get_field(header, 'space directions')
    if directions is not None:
        try:
            row = directions[axis]
            if row is not None:
                values = np.asarray(row, dtype=float)
                if values.ndim == 1 and values.size > 0 and np.all(np.isfinite(values)):
                    spacing = float(np.linalg.norm(values))
                    if spacing > 0:
                        return spacing
        except (IndexError, TypeError, ValueError):
            pass

    for field_name in ('spacings', 'thicknesses'):
        value = _get_field(header, field_name)
        if value is not None:
            try:
                spacing = float(np.asarray(value, dtype=float)[axis])
                if np.isfinite(spacing) and spacing > 0:
                    return spacing
            except (IndexError, TypeError, ValueError):
                pass

    return 1.0


def _gap_fill_value(dtype: np.dtype, gap_value: Optional[Any]) -> Any:
    if gap_value is None:
        if dtype.kind in ('f', 'c'):
            return np.nan
        return 0
    return gap_value


def _data_file_list(header: Dict[str, Any]) -> Optional[List[str]]:
    value = _get_field(header, 'data file list', 'datafile list',
                       'data file names', 'datafile names', 'data files')
    if value is None:
        # Also accept a plain list stored directly in the data file field.
        direct = _get_field(header, 'data file', 'datafile')
        if isinstance(direct, (list, tuple, np.ndarray)):
            value = list(direct)
        else:
            return None
    if isinstance(value, str):
        # A LIST may be supplied as newline- or whitespace-separated names.
        return [name for name in re.split(r'[\r\n]+|\s+', value) if name]
    return list(value)


def _single_data_file(header: Dict[str, Any]) -> Optional[str]:
    # A header dictionary can legally hold a single filename in 'data file'.
    value = _get_field(header, 'datafile', 'data file')
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        return None
    return str(value)


def is_multifile_reference(header: Dict[str, Any]) -> bool:
    """Return whether ``header`` describes multiple detached data files."""

    if _data_file_list(header) is not None:
        return True
    reference = _single_data_file(header)
    if reference is None:
        return False
    if reference.strip().upper() == 'LIST':
        return True
    return _has_sequence_placeholder(reference)


def _resolve_data_files(header: Dict[str, Any], root: str, slice_axis: Optional[Any] = None,
                        expected: Optional[int] = None) -> Tuple[List[Dict[str, Any]], int, int]:
    """Expand a multi-file header into per-position file plans.

    Each plan has ``index``, ``source`` (the filename as referenced by the
    header, or :obj:`None` for a gap), and ``path`` (the authorized absolute
    path, or :obj:`None` for a gap).  Positions are never compacted: a missing
    file leaves a hole at its declared index.
    """

    sizes = _as_sizes(header)
    dimension = int(header['dimension'])
    reference = _single_data_file(header)
    names = _data_file_list(header)

    if names is not None:
        raw_names: Optional[List[str]] = names
    elif reference is not None and reference.strip().upper() == 'LIST':
        raw_names = []
    else:
        raw_names = None

    if raw_names is not None:
        explicit_count: Optional[int]
        if expected is not None:
            explicit_count = expected
        else:
            index_values = _list_indices_from_names(raw_names)
            if index_values:
                explicit_count = max(index_values) - min(index_values) + 1
            else:
                explicit_count = len(raw_names)

        axis = _slice_axis_for_header(header, slice_axis,
                                      explicit_count if explicit_count is not None else len(raw_names))
        count = int(sizes[axis]) if expected is None else expected
        if count <= 0:
            raise NRRDError('Slice axis size must be positive')

        if len(raw_names) == 0:
            return ([{'index': index, 'source': None, 'path': None} for index in range(count)],
                    axis, 0)

        index_values = _list_indices_from_names(raw_names)
        if index_values is not None:
            positions = _normalize_index_positions(index_values, count)
            start = index_values[0]
        else:
            positions = list(range(len(raw_names)))
            start = 0

        plans: List[Dict[str, Any]] = [
            {'index': index, 'source': None, 'path': None} for index in range(count)
        ]
        for name, position in zip(raw_names, positions):
            if plans[position]['source'] is not None:
                raise NRRDError(f'Duplicate data file position {position}')
            plans[position]['source'] = name
            plans[position]['path'] = _resolve_authorized(name, root)
        return plans, axis, start

    if reference is None:
        raise NRRDError('Header does not contain a data file reference')
    if not _has_sequence_placeholder(reference):
        return ([{'index': 0, 'source': reference,
                  'path': _resolve_authorized(reference, root)}], dimension - 1, 0)

    # Resolve the template directory once, then work with an absolute
    # template so the same path rules apply to every expanded name.
    absolute_template = _resolve_template_for_scan(reference, root)
    _, on_disk = _template_indices_on_disk(absolute_template)

    candidate_count = (max(on_disk) - min(on_disk) + 1) if on_disk else None
    axis = _slice_axis_for_header(header, slice_axis, candidate_count)
    count = int(sizes[axis]) if expected is None else expected
    if count <= 0:
        raise NRRDError('Slice axis size must be positive')

    explicit_start = _get_field(header, 'data file start', 'datafile start',
                                'data file index', 'datafile index', 'slice start')
    if explicit_start is not None:
        try:
            start = int(explicit_start)
        except (TypeError, ValueError):
            raise NRRDError(f'Invalid data file start index: {explicit_start!r}')
    elif on_disk:
        # Choose the start that explains the most files on disk.  This
        # handles zero-based, one-based and arbitrary starts without shifting
        # a missing middle slice.
        candidates = {0, 1, min(on_disk), max(on_disk) - count + 1}
        start = min(
            (candidate for candidate in candidates if candidate >= 0),
            key=lambda candidate: (
                -sum(1 for value in range(candidate, candidate + count)
                     if os.path.isfile(_format_sequence_name(absolute_template, value))),
                candidate),
        )
    else:
        start = 0

    plans = []
    for index in range(count):
        value = start + index
        source = _format_sequence_name(reference, value)
        path = _resolve_authorized(_format_sequence_name(absolute_template, value), root)
        plans.append({'index': index, 'source': source, 'path': path})
    return plans, axis, start


def data_file_references(header: Dict[str, Any], root: str, slice_axis: Optional[Any] = None,
                         expected: Optional[int] = None) -> Tuple[List[Optional[str]], int, int]:
    """Expand a multi-file header into concrete authorized paths.

    Returns ``(sources, slice_axis, start_index)``.  ``sources[i]`` is the
    absolute path for slice position ``i`` or :obj:`None` for a gap.  The
    returned paths are checked against ``root``.
    """

    plans, axis, start = _resolve_data_files(header, root, slice_axis, expected)
    return [plan['path'] for plan in plans], axis, start


def _resolve_template_for_scan(template: str, root: str) -> str:
    """Return a template with its relative directory part anchored at root."""

    directory, basename = os.path.split(template)
    if directory:
        resolved_directory = _resolve_authorized(directory, root, 'data file template directory')
    else:
        resolved_directory = os.path.abspath(root)
    return os.path.join(resolved_directory, basename)


def read_multifile_data(header: Dict[str, Any], filename: str, index_order: str = 'F',
                        data_root: Optional[str] = None, slice_axis: Optional[Any] = None,
                        gap_value: Optional[Any] = None,
                        return_segments: bool = False) -> Any:
    """Read a multi-file NRRD data set.

    The implementation deliberately mirrors :func:`nrrd.read_data` for each
    individual piece, so the same encoding, endianness, line skip and byte
    skip rules apply to every slice file.  Missing files become gaps; they are
    not skipped when placing later slices.
    """

    from nrrd.reader import _determine_datatype, _read_single_data

    sizes = _as_sizes(header)
    dimension = int(header['dimension'])
    if dimension != len(sizes):
        raise NRRDError(f'Number of elements in sizes does not match dimension. Dimension: {dimension}, '
                        f'len(sizes): {len(sizes)}')
    if dimension < 1 or np.any(sizes <= 0):
        raise NRRDError('Dimension and sizes must be positive')

    dtype = _determine_datatype(header)
    root = data_root
    if root is None:
        root = os.path.dirname(os.path.abspath(filename))
    else:
        root = os.path.abspath(root)

    plans, axis, start = _resolve_data_files(header, root, slice_axis)
    count = int(sizes[axis])

    if len(plans) != count:
        raise NRRDError(
            f'Number of data files ({len(plans)}) does not match the declared slice axis size ({count})')

    spacing = _axis_spacing(header, axis)
    reduced_sizes = np.delete(sizes, axis)
    elements_per_slice = int(np.prod(reduced_sizes, dtype=np.int64))

    if index_order == 'F':
        output_shape = tuple(int(x) for x in sizes)
        numpy_axis = axis
    elif index_order == 'C':
        output_shape = tuple(int(x) for x in sizes[::-1])
        numpy_axis = dimension - 1 - axis
    else:
        raise NRRDError('Invalid index order')

    fill = _gap_fill_value(dtype, gap_value)
    data = np.empty(output_shape, dtype=dtype)
    data[...] = fill

    segments: List[SegmentInfo] = []
    found = 0
    missing = 0
    for position, plan in enumerate(plans):
        source = plan['path']
        expected_bytes = elements_per_slice * dtype.itemsize
        segment = SegmentInfo(
            index=position,
            source=plan['source'],
            source_name=plan['source'],
            path=source,
            resolved_path=source,
            file=os.path.basename(plan['source']) if plan['source'] is not None else None,
            status='missing',
            present=False,
            loaded=False,
            gap=False,
            axis_index=position,
            axis_position=spacing * position,
            axis_start=spacing * position,
            axis_end=spacing * (position + 1),
            axis_span=None,
            span_from_previous=None,
            span_before=None,
            expected_bytes=expected_bytes,
            actual_bytes=None,
            file_bytes=None,
            byte_count=None,
            data_bytes=None,
            bytes=None,
            size_bytes=None,
            shape=None,
            dtype=None,
        )

        if source is None:
            segment['status'] = 'gap'
            segment['gap'] = True
            missing += 1
            segments.append(segment)
            continue
        if not os.path.isfile(source):
            segment['status'] = 'missing'
            segment['gap'] = True
            missing += 1
            segments.append(segment)
            continue

        file_bytes = os.path.getsize(source)
        segment['file_bytes'] = file_bytes
        segment['byte_count'] = file_bytes
        segment['size_bytes'] = file_bytes
        segment['exists'] = True

        slice_header = dict(header)
        for key in ('data file', 'datafile', 'data file list', 'datafile list',
                    'data file names', 'datafile names', 'data files'):
            slice_header.pop(key, None)
        slice_header['dimension'] = dimension - 1
        slice_header['sizes'] = reduced_sizes

        try:
            with open(source, 'rb') as fh:
                slice_data = _read_single_data(slice_header, fh, source, index_order)
        except NRRDError as exc:
            raise NRRDError(
                f'Data file {source!r} for slice {position} failed validation: {exc}') from exc
        except OSError as exc:
            raise NRRDError(f'Unable to read data file {source!r} for slice {position}: {exc}') from exc

        actual_bytes = int(slice_data.nbytes)
        segment['actual_bytes'] = actual_bytes
        segment['data_bytes'] = actual_bytes
        segment['bytes'] = actual_bytes
        segment['byte_count'] = file_bytes
        segment['shape'] = tuple(int(x) for x in slice_data.shape)
        segment['dtype'] = str(slice_data.dtype)

        if actual_bytes != expected_bytes:
            raise NRRDError(
                f'Data file {source!r} for slice {position} has {actual_bytes} data bytes, '
                f'expected {expected_bytes}')

        indexer: List[Any] = [slice(None)] * dimension
        indexer[numpy_axis] = position
        data[tuple(indexer)] = slice_data

        segment['status'] = 'loaded'
        segment['present'] = True
        segment['loaded'] = True
        found += 1
        segments.append(segment)

    # Compute the physical span from each retained slice to the next retained
    # slice.  With a gap this is intentionally larger than one slice spacing.
    previous: Optional[SegmentInfo] = None
    for segment in segments:
        if segment['status'] != 'loaded':
            continue
        if previous is not None:
            span = spacing * (segment['index'] - previous['index'])
            segment['span_from_previous'] = span
            segment['axis_span'] = span
        previous = segment

    if found == 0:
        raise NRRDError('No data files were found for the multi-file data reference')

    loaded_segments = [segment for segment in segments if segment['status'] == 'loaded']
    present_span = (spacing * (loaded_segments[-1]['index'] - loaded_segments[0]['index'])
                    if loaded_segments else 0.0)
    total_bytes = sum(segment['actual_bytes'] or 0 for segment in segments)
    expected_total_bytes = count * elements_per_slice * dtype.itemsize
    report = {
        'count': count,
        'loaded': found,
        'missing': missing,
        'complete': missing == 0,
        'axis': axis,
        'axis_index': axis,
        'start': start,
        'axis_spacing': spacing,
        'axis_span': present_span,
        'declared_axis_span': spacing * (count - 1),
        'expected_bytes_per_slice': elements_per_slice * dtype.itemsize,
        'total_bytes': total_bytes,
        'expected_total_bytes': expected_total_bytes,
        'segments': [dict(segment) for segment in segments],
    }

    # Store the provenance in a few convenient places.  The public array is
    # an ndarray subclass, while these header entries make the information
    # available to callers that only keep the header.
    header['data file axis'] = axis
    header['data file start'] = start
    header['data file count'] = found
    header['data file loaded count'] = found
    header['data file expected count'] = count
    header['data file missing count'] = missing
    header['data file span'] = spacing * (count - 1)
    header['data file segments'] = [dict(segment) for segment in segments]
    header['data file paths'] = [segment['path'] for segment in segments]
    header['data file total bytes'] = total_bytes
    header['data file expected total bytes'] = expected_total_bytes
    header['data file report'] = report
    header['slice sources'] = [segment['source'] for segment in segments]

    volume = MultiFileVolume(data, segments=segments, axis=axis, axis_spacing=spacing,
                             numpy_axis=numpy_axis, report=report)

    if return_segments:
        return volume, segments
    return volume
