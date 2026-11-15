import bz2
import io
import os
import re
import shlex
import warnings
import zlib
from collections import OrderedDict
from typing import IO, Any, AnyStr, Iterable, Tuple

import nrrd
from nrrd.parsers import *
from nrrd.types import IndexOrder, NRRDFieldMap, NRRDFieldType, NRRDHeader, NRRDSelection

# Older versions of Python had issues when uncompressed data was larger than 4GB (2^32). This should be fixed in latest
# version of Python 2.7 and all versions of Python 3. The fix for this issue is to read the data in smaller chunks.
# Chunk size is set to be large at 1GB to improve performance. If issues arise decompressing larger files, try to reduce
# this value
_READ_CHUNKSIZE: int = 2 ** 32

# Header field recording the original Fortran-order (lower, upper) bounds of a bounded read. It is private to pynrrd:
# leading underscore makes the writer omit it, so cropped headers still round-trip through the NRRD format cleanly.
SELECTION_HEADER_FIELD: str = '_selection'

_NRRD_REQUIRED_FIELDS = ['dimension', 'type', 'encoding', 'sizes']

ALLOW_DUPLICATE_FIELD: bool = False
"""Allow duplicate header fields when reading NRRD files

When there are duplicated fields in a NRRD file header, pynrrd throws an error by default. Setting this field as
:obj:`True` will instead show a warning.

Example:
    Reading a NRRD file with duplicated header field 'space' with field set to :obj:`False`.

    >>> filedata, fileheader = nrrd.read('filename_duplicatedheader.nrrd')
    nrrd.errors.NRRDError: Duplicate header field: 'space'

    Set the field as :obj:`True` to receive a warning instead.

    >>> nrrd.reader.ALLOW_DUPLICATE_FIELD = True
    >>> filedata, fileheader = nrrd.read('filename_duplicatedheader.nrrd')
    UserWarning: Duplicate header field: 'space' warnings.warn(dup_message)

Note:
    Duplicated fields are prohibited by the NRRD file specification.
"""

_TYPEMAP_NRRD2NUMPY = {
    'signed char': 'i1',
    'int8': 'i1',
    'int8_t': 'i1',
    'uchar': 'u1',
    'unsigned char': 'u1',
    'uint8': 'u1',
    'uint8_t': 'u1',
    'short': 'i2',
    'short int': 'i2',
    'signed short': 'i2',
    'signed short int': 'i2',
    'int16': 'i2',
    'int16_t': 'i2',
    'ushort': 'u2',
    'unsigned short': 'u2',
    'unsigned short int': 'u2',
    'uint16': 'u2',
    'uint16_t': 'u2',
    'int': 'i4',
    'signed int': 'i4',
    'int32': 'i4',
    'int32_t': 'i4',
    'uint': 'u4',
    'unsigned int': 'u4',
    'uint32': 'u4',
    'uint32_t': 'u4',
    'longlong': 'i8',
    'long long': 'i8',
    'long long int': 'i8',
    'signed long long': 'i8',
    'signed long long int': 'i8',
    'int64': 'i8',
    'int64_t': 'i8',
    'ulonglong': 'u8',
    'unsigned long long': 'u8',
    'unsigned long long int': 'u8',
    'uint64': 'u8',
    'uint64_t': 'u8',
    'float': 'f4',
    'double': 'f8',
    'block': 'V'
}


def _get_field_type(field: str, custom_field_map: Optional[NRRDFieldMap]) -> NRRDFieldType:
    if field in ['dimension', 'lineskip', 'line skip', 'byteskip', 'byte skip', 'space dimension']:
        return 'int'
    elif field in ['min', 'max', 'oldmin', 'old min', 'oldmax', 'old max']:
        return 'double'
    elif field in ['endian', 'encoding', 'content', 'sample units', 'datafile', 'data file', 'space', 'type']:
        return 'string'
    elif field in ['sizes']:
        return 'int list'
    elif field in ['spacings', 'thicknesses', 'axismins', 'axis mins', 'axismaxs', 'axis maxs']:
        return 'double list'
    elif field in ['kinds', 'centerings']:
        return 'string list'
    elif field in ['labels', 'units', 'space units']:
        return 'quoted string list'
    # No int vector fields yet
    # elif field in []:
    #     return 'int vector'
    elif field in ['space origin']:
        return 'double vector'
    elif field in ['measurement frame']:
        return 'double matrix'
    elif field in ['space directions']:
        return nrrd.SPACE_DIRECTIONS_TYPE
    else:
        if custom_field_map and field in custom_field_map:
            return custom_field_map[field]

        # Default the type to string if unknown type
        return 'string'


def _parse_field_value(value: str, field_type: NRRDFieldType) -> Any:
    if field_type == 'int':
        return int(value)
    elif field_type == 'double':
        return float(value)
    elif field_type == 'string':
        return str(value)
    elif field_type == 'int list':
        return parse_number_list(value, dtype=int)
    elif field_type == 'double list':
        return parse_number_list(value, dtype=float)
    elif field_type == 'string list':
        return [str(x) for x in value.split()]
    elif field_type == 'quoted string list':
        return shlex.split(value)
    elif field_type == 'int vector':
        return parse_vector(value, dtype=int)
    elif field_type == 'double vector':
        return parse_vector(value, dtype=float)
    elif field_type == 'int matrix':
        return parse_matrix(value, dtype=int)
    elif field_type == 'double matrix':
        # For matrices of double type, parse as an optional matrix to allow for rows of the matrix to be none
        # This is only valid for double matrices because the matrix is represented with NaN in the entire row
        # for none rows. NaN is only valid for floating point numbers
        return parse_optional_matrix(value)
    elif field_type == 'int vector list':
        return parse_optional_vector_list(value, dtype=int)
    elif field_type == 'double vector list':
        return parse_optional_vector_list(value, dtype=float)
    else:
        raise NRRDError(f'Invalid field type given: {field_type}')


def _determine_datatype(header: NRRDHeader) -> np.dtype:
    """Determine the numpy dtype of the data."""

    # Convert the NRRD type string identifier into a NumPy string identifier using a map
    np_typestring = _TYPEMAP_NRRD2NUMPY[header['type']]

    # This is only added if the datatype has more than one byte and is not using ASCII encoding
    # Note: Endian is not required for ASCII encoding
    if np.dtype(np_typestring).itemsize > 1 and header['encoding'] not in ['ASCII', 'ascii', 'text', 'txt']:
        if 'endian' not in header:
            raise NRRDError('Header is missing required field: endian')
        elif header['endian'] == 'big':
            np_typestring = '>' + np_typestring
        elif header['endian'] == 'little':
            np_typestring = '<' + np_typestring
        else:
            raise NRRDError(f'Invalid endian value in header: {header["endian"]}')

    return np.dtype(np_typestring)


def _normalize_selection(selection: Optional[NRRDSelection], sizes: npt.NDArray,
                         index_order: IndexOrder) -> Optional[npt.NDArray]:
    """Validate a selection and convert it to Fortran (NRRD) axis order.

    The selection is a tuple of ``(lower, upper)`` pairs, one per axis, with inclusive lower and exclusive upper
    bounds. Either bound may be :obj:`None` to leave that side open. The selection follows :obj:`index_order`, i.e.
    for ``'F'`` the axes match the NRRD ``sizes`` field directly, whereas for ``'C'`` they are reversed.

    Returns an ``(dimension, 2)`` array of integer bounds in Fortran order, or :obj:`None` when no selection is
    given or the selection covers the entire array.
    """

    if selection is None:
        return None

    if not isinstance(selection, (tuple, list)) or len(selection) != len(sizes):
        raise NRRDError(f'Selection must contain one (lower, upper) pair per dimension '
                        f'(expected {len(sizes)}, got {len(selection) if isinstance(selection, (tuple, list)) else 0})')

    # Validate against sizes expressed in the same axis order as the selection
    ordered_sizes = sizes[::-1] if index_order == 'C' else sizes
    bounds = np.empty((len(sizes), 2), dtype=np.int64)

    for axis, axis_bounds in enumerate(selection):
        if not isinstance(axis_bounds, (tuple, list)) or len(axis_bounds) != 2:
            raise NRRDError(f'Selection bounds for axis {axis} must be a (lower, upper) pair')

        lower, upper = axis_bounds
        lower = 0 if lower is None else int(lower)
        upper = int(ordered_sizes[axis]) if upper is None else int(upper)

        if lower < 0 or upper < 0:
            raise NRRDError(f'Selection bounds for axis {axis} must be non-negative')
        if lower > upper:
            raise NRRDError(f'Selection lower bound exceeds upper bound for axis {axis}: {lower} > {upper}')
        if upper > ordered_sizes[axis]:
            raise NRRDError(f'Selection upper bound exceeds size of axis {axis}: {upper} > {ordered_sizes[axis]}')

        bounds[axis] = (lower, upper)

    # The selection follows the index order of the returned array. NRRD storage (and thus the extraction logic below)
    # is always Fortran order, so reverse the bounds when C order was requested.
    if index_order == 'C':
        bounds = bounds[::-1]

    if np.all(bounds[:, 0] == 0) and np.array_equal(bounds[:, 1], sizes):
        return None

    return bounds


def _read_raw_selection(fh: IO, bounds_f: npt.NDArray, sizes: npt.NDArray, dtype: np.dtype) -> npt.NDArray:
    """Read a selection from an uncompressed (raw) stream without reading the surrounding data.

    The stream position must point at the first data byte (any line/byte skip is already applied by the caller).
    Bounds are given in Fortran (NRRD) axis order. Data is gathered by walking the on-disk Fortran layout directly:
    each selected inner-axis run is reached with a seek over the skipped bytes, so only byte ranges intersecting the
    selection are read, including when the selection spans rows or higher-dimension slabs.
    """

    dimension = len(sizes)
    lower = bounds_f[:, 0]
    upper = bounds_f[:, 1]
    cropped_sizes = upper - lower

    # Fortran-order element strides of the stored array; axis 0 varies fastest on disk
    strides = np.empty(dimension, dtype=np.int64)
    strides[0] = 1
    if dimension > 1:
        strides[1:] = np.cumprod(sizes[:-1])

    inner_bytes = int(cropped_sizes[0]) * dtype.itemsize

    # Seek to the first selected element
    start_offset = int(np.dot(lower, strides)) * dtype.itemsize
    fh.seek(start_offset, os.SEEK_CUR)

    if dimension == 1:
        chunk = fh.read(inner_bytes)
        if len(chunk) != inner_bytes:
            raise NRRDError('Data ended before reaching the end of the selected region')
        return np.frombuffer(chunk, dtype).copy()

    # Each run covers cropped_sizes[0] contiguous elements. Runs are enumerated in Fortran order, which matches the
    # on-disk order, so the file pointer only ever moves forward.
    run_count = int(np.prod(cropped_sizes[1:]))
    outer_cropped = cropped_sizes[1:]
    outer_strides = strides[1:]
    outer_lower = lower[1:]

    chunks = []
    position = start_offset

    for run_index in range(run_count):
        # Decode the Fortran-order linear run index into cropped outer-axis coordinates
        remainder = run_index
        base = 0
        for axis in range(dimension - 1):
            coordinate = remainder % outer_cropped[axis]
            remainder //= outer_cropped[axis]
            base += (coordinate + outer_lower[axis]) * outer_strides[axis]

        target = int(base + lower[0]) * dtype.itemsize
        if target != position:
            # Jump over the unselected rows/slabs between this run and the previous one
            fh.seek(target - position, os.SEEK_CUR)

        chunk = fh.read(inner_bytes)
        if len(chunk) != inner_bytes:
            raise NRRDError('Data ended before reaching the end of the selected region')
        chunks.append(chunk)
        position = target + inner_bytes

    # Copy so the returned array is writable (np.frombuffer returns a read-only view)
    return np.frombuffer(b''.join(chunks), dtype).copy()


def _crop_header(header: NRRDHeader, bounds_f: npt.NDArray) -> None:
    """Trim a parsed header in place so that it describes the selected region.

    Only the fields that actually change when an index range is restricted are modified: ``sizes`` becomes the
    selected extents and, when spatial geometry is available, the ``space origin`` is shifted by the lower bound
    along each spatial axis. Per-axis descriptors (spacings, kinds, labels, space directions, ...) describe the axes
    themselves and are left untouched because cropping restricts an axis rather than removing it.
    """

    lower = bounds_f[:, 0]
    header['sizes'] = np.array(bounds_f[:, 1] - lower, dtype=header['sizes'].dtype)

    # Record the original range of every NRRD (Fortran-order) axis relative to the full array so that callers can map
    # the cropped data back to its source. Stored under a private, underscore-prefixed key that the writer skips, hence
    # it never appears in a serialized NRRD header.
    header[SELECTION_HEADER_FIELD] = tuple((int(lo), int(hi)) for lo, hi in bounds_f)

    if 'space origin' not in header or 'space directions' not in header:
        return

    directions = header['space directions']
    if not isinstance(directions, np.ndarray) or directions.ndim != 2:
        return

    origin = np.array(header['space origin'], dtype=float)
    for axis in range(min(len(lower), directions.shape[0])):
        direction = directions[axis]
        # Rows of NaN represent non-spatial axes and must not contribute to the origin shift
        if np.ndim(direction) == 1 and not np.any(np.isnan(direction)):
            origin = origin + lower[axis] * direction

    header['space origin'] = origin


def _validate_magic_line(line: str) -> int:
    """For NRRD files, the first four characters are always "NRRD", and
    remaining characters give information about the file format version

    >>> _validate_magic_line('NRRD0005')
    8
    >>> _validate_magic_line('NRRD0006')
    Traceback (most recent call last):
        ...
    NrrdError: NRRD file version too new for this library.
    >>> _validate_magic_line('NRRD')
    Traceback (most recent call last):
        ...
    NrrdError: Invalid NRRD magic line: NRRD
    """

    if not line.startswith('NRRD'):
        raise NRRDError('Invalid NRRD magic line. Is this an NRRD file?')

    try:
        version = int(line[4:])
        if version > 5:
            raise NRRDError(f'Unsupported NRRD file version (version: {version}). This library only supports v5 '
                            'and below.')
    except ValueError:
        raise NRRDError(f'Invalid NRRD magic line: {line}')

    return len(line)


def read_header(file: Union[str, Iterable[AnyStr]], custom_field_map: Optional[NRRDFieldMap] = None) -> NRRDHeader:
    """Read contents of header and parse values from :obj:`file`

    :obj:`file` can be a filename indicating where the NRRD header is located or a string iterator object. If a
    filename is specified, then the file will be opened and closed after the header is read from it. If not specifying
    a filename, the :obj:`file` parameter can be any sort of iterator that returns a string each time :meth:`next` is
    called. The two common objects that meet these requirements are file objects and a list of strings. When
    :obj:`file` is a file object, it must be opened with the binary flag ('b') on platforms where that makes a
    difference, such as Windows.

    See :ref:`background/how-to-use:reading nrrd files` for more information on reading NRRD files.

    Parameters
    ----------
    file : :class:`str` or string iterator
        Filename, file object or string iterator object to read NRRD header from
    custom_field_map : :class:`dict` (:class:`str`, :class:`str`), optional
        Dictionary used for parsing custom field types where the key is the custom field name and the value is a
        string identifying datatype for the custom field.

    Returns
    -------
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Dictionary containing the header fields and their corresponding parsed value

    See Also
    --------
    :meth:`read`, :meth:`read_data`
    """

    # If the file is a filename rather than the file handle, then open the file and call this function again with the
    # file handle. Since read function uses a filename, it is easy to think read_header is the same syntax.
    if isinstance(file, str) and file.count('\n') == 0:
        with open(file, 'rb') as fh:
            return read_header(fh, custom_field_map)

    # Collect number of bytes in the file header (for seeking below)
    header_size = 0

    # Get iterator for the file and extract the first line, the magic line
    it = iter(file)
    magic_line = next(it)

    # Depending on what type file is, decoding may or may not be necessary. Decode if necessary, otherwise skip.
    need_decode = False
    if hasattr(magic_line, 'decode'):
        need_decode = True
        magic_line = magic_line.decode('ascii', 'ignore')

    # Validate the magic line and increment header size by size of the line
    header_size += _validate_magic_line(magic_line)

    # Create empty header
    # This is an OrderedDict rather than an ordinary dict because an OrderedDict will keep it's order that key/values
    # are added for when looping back through it. The added benefit of this is that saving the header will save the
    # fields in the same order.
    header = OrderedDict()

    # Loop through each line
    for line in it:
        header_size += len(line)
        if need_decode:
            line = line.decode('ascii', 'ignore')

        # Trailing whitespace ignored per the NRRD spec
        line = line.rstrip()

        # Skip comments starting with # (no leading whitespace is allowed)
        # Or, stop reading the header once a blank line is encountered. This separates header from data.
        if line.startswith('#'):
            continue
        elif line == '':
            break

        # Read the field and value from the line, split using regex to search for := or : delimiter
        field, value = re.split(r':=?', line, maxsplit=1)

        # Remove whitespace before and after the field and value
        field, value = field.strip(), value.strip()

        # Check if the field has been added already
        if field in header.keys():
            if not ALLOW_DUPLICATE_FIELD:
                raise NRRDError(f'Duplicate header field: {field}')
            else:
                warnings.warn(f'Duplicate header field: {field}')

        # Get the datatype of the field based on its field name and custom field map
        field_type = _get_field_type(field, custom_field_map)

        # Parse the field value using the datatype retrieved
        # Place it in the header dictionary
        header[field] = _parse_field_value(value, field_type)

    # Reading the file line by line is buffered and so the header is not in the correct position for reading data if
    # the file contains the data in it as well. The solution is to set the file pointer to just behind the header.
    if hasattr(file, 'seek'):
        file.seek(header_size)

    return header


def read_data(header: NRRDHeader, fh: Optional[IO] = None, filename: Optional[str] = None,
              index_order: IndexOrder = 'F',
              selection: Optional[NRRDSelection] = None) -> npt.NDArray:
    """Read data from file into :class:`numpy.ndarray`

    The two parameters :obj:`fh` and :obj:`filename` are optional depending on the parameters but it never hurts to
    specify both. The file handle (:obj:`fh`) is necessary if the header is attached with the NRRD data. However, if
    the NRRD data is detached from the header, then the :obj:`filename` parameter is required to obtain the absolute
    path to the data file.

    See :ref:`background/how-to-use:reading nrrd files` for more information on reading NRRD files.

    Parameters
    ----------
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Parsed fields/values obtained from :meth:`read_header` function
    fh : file-object, optional
        File object pointing to first byte of data. Only necessary if data is attached to header.
    filename : :class:`str`, optional
        Filename of the header file. Only necessary if data is detached from the header. This is used to get the
        absolute data path.
    index_order : {'C', 'F'}, optional
        Specifies the index order of the resulting data array. Either 'C' (C-order) where the dimensions are ordered
        from slowest-varying to fastest-varying (e.g. (z, y, x)), or 'F' (Fortran-order) where the dimensions are
        ordered from fastest-varying to slowest-varying (e.g. (x, y, z)).
    selection : tuple of tuple of int, optional
        Bounded read restricting the data to an index region. Contains one ``(lower, upper)`` pair per dimension with
        an inclusive lower bound and exclusive upper bound; either bound may be :obj:`None` to leave that side open.
        The pairs follow :obj:`index_order`, so for ``'F'`` they match the NRRD header axes while for ``'C'`` they are
        given in reverse. The returned header is trimmed to describe the selected region (updated ``sizes`` and, when
        present, a shifted ``space origin``). For ``raw`` encoding only the intersecting byte ranges are read by
        walking the on-disk Fortran layout, including across rows and slabs. ASCII and compressed encodings cannot be
        partially decoded, so the complete payload is still read and decompressed before slicing; the selection only
        saves array memory in those cases.

    Returns
    -------
    data : :class:`numpy.ndarray`
        Data read from NRRD file

    See Also
    --------
    :meth:`read`, :meth:`read_header`
    """

    if index_order not in ['F', 'C']:
        raise NRRDError('Invalid index order')

    # Check that the required fields are in the header
    for field in _NRRD_REQUIRED_FIELDS:
        if field not in header:
            raise NRRDError(f'Header is missing required field: {field}')

    if header['dimension'] != len(header['sizes']):
        raise NRRDError(f'Number of elements in sizes does not match dimension. Dimension: {header["dimension"]}, '
                        f'len(sizes): {len(header["sizes"])}')

    if header['dimension'] < 1 or np.any(header['sizes'] <= 0):
        raise NRRDError('Dimension and sizes must be positive')

    # Normalize the selection into Fortran (NRRD) axis order. Returns None when nothing restricts the read.
    bounds_f = _normalize_selection(selection, np.asarray(header['sizes']), index_order)

    # Determine the data type from the header
    dtype = _determine_datatype(header)

    # Determine the byte skip, line skip and the data file
    # These all can be written with or without the space according to the NRRD spec, so we check them both
    line_skip = header.get('lineskip', header.get('line skip', 0))
    byte_skip = header.get('byteskip', header.get('byte skip', 0))
    data_filename = header.get('datafile', header.get('data file', None))

    # If the data file is separate from the header file, then open the data file to read from that instead
    if data_filename is not None:
        # If the pathname is relative, then append the current directory from the filename
        if not os.path.isabs(data_filename):
            if filename is None:
                raise NRRDError('Filename parameter must be specified when a relative data file path is given')

            data_filename = os.path.join(os.path.dirname(filename), data_filename)

        # Override the fh parameter with the data filename
        # Note that this is opened without a "with" block, thus it must be closed manually in all circumstances
        fh = open(data_filename, 'rb')

    # Get the total number of data points by multiplying the size of each dimension together
    total_data_points = header['sizes'].prod(dtype=np.int64)

    # Skip the number of lines requested when line_skip >= 0
    # Irrespective of the NRRD file having attached/detached header
    # Lines are skipped before getting to the beginning of the data
    if line_skip >= 0:
        for _ in range(line_skip):
            fh.readline()
    else:
        # Must close the file because if the file was opened above from detached filename, there is no "with" block to
        # close it for us
        if data_filename is not None:
            fh.close()

        raise NRRDError('Invalid lineskip, allowed values are greater than or equal to 0')

    # Skip the requested number of bytes or seek backward, and then parse the data using NumPy
    if byte_skip < -1:
        # Must close the file because if the file was opened above from detached filename, there is no "with" block to
        # close it for us
        if data_filename is not None:
            fh.close()

        raise NRRDError('Invalid byteskip, allowed values are greater than or equal to -1')
    elif byte_skip >= 0:
        fh.seek(byte_skip, os.SEEK_CUR)
    elif byte_skip == -1 and header['encoding'] not in ['gzip', 'gz', 'bzip2', 'bz2']:
        fh.seek(-dtype.itemsize * total_data_points, os.SEEK_END)
    else:
        # The only case left should be: byte_skip == -1 and header['encoding'] == 'gzip'
        byte_skip = -dtype.itemsize * total_data_points

    # If a compression encoding is used, then byte skip AFTER decompressing
    if header['encoding'] == 'raw':
        if bounds_f is not None:
            # Bounded raw read: seek along the on-disk Fortran layout and only touch intersecting byte ranges
            data = _read_raw_selection(fh, bounds_f, np.asarray(header['sizes']), dtype)
        elif isinstance(fh, io.BytesIO):
            raw_data = bytearray(fh.read(total_data_points * dtype.itemsize))
            data = np.frombuffer(raw_data, dtype)
        else:
            data = np.fromfile(fh, dtype)
    elif header['encoding'] in ['ASCII', 'ascii', 'text', 'txt']:
        if isinstance(fh, io.BytesIO):
            data = np.fromstring(fh.read(), dtype, sep=' ')
        else:
            data = np.fromfile(fh, dtype, sep=' ')
    else:
        # Handle compressed data now
        # Construct the decompression object based on encoding
        if header['encoding'] in ['gzip', 'gz']:
            decompobj = zlib.decompressobj(zlib.MAX_WBITS | 16)
        elif header['encoding'] in ['bzip2', 'bz2']:
            decompobj = bz2.BZ2Decompressor()
        else:
            # Must close the file because if the file was opened above from detached filename, there is no "with" block
            # to close it for us
            if data_filename is not None:
                fh.close()

            raise NRRDError(f'Unsupported encoding: {header["encoding"]}')

        # Loop through the file and read a chunk at a time (see _READ_CHUNKSIZE why it is read in chunks)
        decompressed_data = bytearray()

        # Read all the remaining data from the file
        # Obtain the length of the compressed data since we will be using it repeatedly, more efficient
        compressed_data = fh.read()
        compressed_data_len = len(compressed_data)
        start_index = 0

        # Loop through data and decompress it chunk by chunk
        while start_index < compressed_data_len:
            # Calculate the end index = start index plus chunk size
            # Set to the string length to read the remaining chunk at the end
            end_index = min(start_index + _READ_CHUNKSIZE, compressed_data_len)

            # Decompress and append data
            decompressed_data += decompobj.decompress(compressed_data[start_index:end_index])

            # Update start index
            start_index = end_index

        # Delete the compressed data since we do not need it anymore
        # This could potentially be using a lot of memory
        del compressed_data

        if not decompobj.eof:
            if data_filename is not None:
                fh.close()
            raise NRRDError('Data ended before reaching the end of the compressed stream')

        # Byte skip is applied AFTER the decompression. Skip first x bytes of the decompressed data and parse it using
        # NumPy
        data = np.frombuffer(decompressed_data[byte_skip:], dtype)

    # Close the file if we opened it
    if data_filename is not None:
        fh.close()

    if bounds_f is not None and header['encoding'] == 'raw':
        # The raw bounded reader only gathered the selected elements
        cropped_sizes_f = bounds_f[:, 1] - bounds_f[:, 0]
        selected_points = int(np.prod(cropped_sizes_f))

        if selected_points != data.size:
            raise NRRDError(f'Size of the selected data does not equal the product of the selected dimensions: '
                            f'{selected_points}-{data.size}={selected_points - data.size}')

        data = np.reshape(data, tuple(cropped_sizes_f[::-1]))

        if index_order == 'F':
            data = data.T
    else:
        if total_data_points != data.size:
            raise NRRDError(f'Size of the data does not equal the product of all the dimensions: '
                            f'{total_data_points}-{data.size}={total_data_points - data.size}')

        # In the NRRD header, the fields are specified in Fortran order, i.e, the first index is the one that changes
        # fastest and last index changes slowest. This needs to be taken into consideration since numpy uses C-order
        # indexing.

        # The array shape from NRRD (x,y,z) needs to be reversed as numpy expects (z,y,x).
        data = np.reshape(data, tuple(header['sizes'][::-1]))

        # Transpose data to enable Fortran indexing if requested.
        if index_order == 'F':
            data = data.T

        if bounds_f is not None:
            # ASCII and compressed payloads cannot be partially decoded, so the full payload was read above. Slice the
            # decoded array using the selection in the requested index order.
            ordered_bounds = bounds_f if index_order == 'F' else bounds_f[::-1]
            slicer = tuple(slice(int(lower), int(upper)) for lower, upper in ordered_bounds)
            data = data[slicer]

    if bounds_f is not None:
        _crop_header(header, bounds_f)

    return data


def read(filename: str, custom_field_map: Optional[NRRDFieldMap] = None, index_order: IndexOrder = 'F',
         selection: Optional[NRRDSelection] = None) -> Tuple[npt.NDArray, NRRDHeader]:
    """Read a NRRD file and return the header and data

    See :ref:`background/how-to-use:reading nrrd files` for more information on reading NRRD files.

    .. note::
            Users should be aware that the `index_order` argument needs to be consistent between `nrrd.read` and
            `nrrd.write`. I.e., reading an array with `index_order='F'` will result in a transposed version of the
            original data and hence the writer needs to be aware of this.

    Parameters
    ----------
    filename : :class:`str`
        Filename of the NRRD file
    custom_field_map : :class:`dict` (:class:`str`, :class:`str`), optional
        Dictionary used for parsing custom field types where the key is the custom field name and the value is a
        string identifying datatype for the custom field.
    index_order : {'C', 'F'}, optional
        Specifies the index order of the resulting data array. Either 'C' (C-order) where the dimensions are ordered
        from slowest-varying to fastest-varying (e.g. (z, y, x)), or 'F' (Fortran-order) where the dimensions are
        ordered from fastest-varying to slowest-varying (e.g. (x, y, z)).
    selection : tuple of tuple of int, optional
        Bounded read restricting the data to an index region. Contains one ``(lower, upper)`` pair per dimension with
        an inclusive lower bound and exclusive upper bound; either bound may be :obj:`None` to leave that side open.
        The pairs follow :obj:`index_order`. On return, the header describes the cropped region and the private
        ``_selection`` field records the original Fortran-order bounds relative to the full array. See
        :meth:`read_data` for the per-encoding cost of a bounded read.

    Returns
    -------
    data : :class:`numpy.ndarray`
        Data read from NRRD file
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Dictionary containing the header fields and their corresponding parsed value

    See Also
    --------
    :meth:`write`, :meth:`read_header`, :meth:`read_data`
    """

    with open(filename, 'rb') as fh:
        header = read_header(fh, custom_field_map)
        data = read_data(header, fh, filename, index_order, selection)

    return data, header
