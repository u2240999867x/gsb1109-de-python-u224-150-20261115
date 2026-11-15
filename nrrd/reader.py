import bz2
import io
import itertools
import os
import re
import shlex
import warnings
import zlib
from collections import OrderedDict
from typing import IO, Any, AnyStr, Iterable, Optional, Sequence, Tuple, Union

import nrrd
from nrrd.parsers import *
from nrrd.types import IndexOrder, NRRDFieldMap, NRRDFieldType, NRRDHeader, Region, RegionAxis, RegionInfo

# Older versions of Python had issues when uncompressed data was larger than 4GB (2^32). This should be fixed in latest
# version of Python 2.7 and all versions of Python 3. The fix for this issue is to read the data in smaller chunks.
# Chunk size is set to be large at 1GB to improve performance. If issues arise decompressing larger files, try to reduce
# this value
_READ_CHUNKSIZE: int = 2 ** 32

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


def _validate_data_header(header: NRRDHeader, index_order: IndexOrder) -> np.dtype:
    """Validate the header fields that are required to read data and return the NumPy dtype."""

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

    return _determine_datatype(header)


def _close_data_source(fh: IO, data_filename: Optional[str]):
    """Close the file handle only when this module opened the detached data file."""

    if data_filename is not None:
        fh.close()


def _prepare_data_source(header: NRRDHeader, fh: Optional[IO], filename: Optional[str], dtype: np.dtype) \
        -> Tuple[IO, Optional[str], int, int]:
    """Open the data source (if detached) and consume line/byte skips.

    Returns the file handle to read from, the detached data filename (or :obj:`None` when :obj:`fh` is used), the
    total number of data points, and the effective byte skip. Non-negative byte skips have already been consumed; the
    only remaining case is the negative skip that applies after decompression.
    """

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
        _close_data_source(fh, data_filename)

        raise NRRDError('Invalid lineskip, allowed values are greater than or equal to 0')

    # Skip the requested number of bytes or seek backward, and then parse the data using NumPy
    if byte_skip < -1:
        # Must close the file because if the file was opened above from detached filename, there is no "with" block to
        # close it for us
        _close_data_source(fh, data_filename)

        raise NRRDError('Invalid byteskip, allowed values are greater than or equal to -1')
    elif byte_skip >= 0:
        fh.seek(byte_skip, os.SEEK_CUR)
    elif byte_skip == -1 and header['encoding'] not in ['gzip', 'gz', 'bzip2', 'bz2']:
        fh.seek(-dtype.itemsize * total_data_points, os.SEEK_END)
    else:
        # The only case left should be: byte_skip == -1 and the encoding is compressed
        byte_skip = -dtype.itemsize * total_data_points

    return fh, data_filename, total_data_points, byte_skip


def read_data(header: NRRDHeader, fh: Optional[IO] = None, filename: Optional[str] = None,
              index_order: IndexOrder = 'F', region: Optional[Region] = None) -> npt.NDArray:
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
    region : sequence, optional
        Index region to read, with one entry per axis expressed in :obj:`index_order`. Each entry may be an inclusive
        ``(start, stop)`` pair, a :class:`slice` with unit step, a single integer, or a sequence of contiguous
        integers. :obj:`None` selects the full extent of an axis. When :obj:`None` (the default) the whole data set is
        read and the behavior is identical to previous releases.

        For the ``raw`` encoding only the bytes of the selected region are read, following the actual storage layout
        (the first NRRD axis varies fastest). ``gzip``/``bzip2`` and ``ASCII`` data are decoded in full before the
        region is extracted, and a :class:`UserWarning` reports that cost.

    Returns
    -------
    data : :class:`numpy.ndarray`
        Data read from NRRD file. When :obj:`region` is given only the selected values are returned, in
        :obj:`index_order`.

    See Also
    --------
    :meth:`read`, :meth:`read_header`, :meth:`read_region`
    """

    if region is None:
        return _read_data_full(header, fh, filename, index_order)

    data, _, _ = _read_data_region(header, fh, filename, region, index_order)
    return data


def _read_data_full(header: NRRDHeader, fh: Optional[IO], filename: Optional[str],
                    index_order: IndexOrder) -> npt.NDArray:
    """Read the complete data set (used when no region is requested)."""

    # Determine the data type from the header
    dtype = _validate_data_header(header, index_order)
    fh, data_filename, total_data_points, byte_skip = _prepare_data_source(header, fh, filename, dtype)

    # If a compression encoding is used, then byte skip AFTER decompressing
    if header['encoding'] == 'raw':
        if isinstance(fh, io.BytesIO):
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
        data = _read_compressed_data(header, fh, data_filename, dtype, byte_skip)

    # Close the file if we opened it
    _close_data_source(fh, data_filename)

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

    return data


def _read_compressed_data(header: NRRDHeader, fh: IO, data_filename: Optional[str], dtype: np.dtype,
                          byte_skip: int) -> npt.NDArray:
    """Decompress an entire gzip or bzip2 stream and return the raw element buffer."""

    # Construct the decompression object based on encoding
    if header['encoding'] in ['gzip', 'gz']:
        decompobj = zlib.decompressobj(zlib.MAX_WBITS | 16)
    elif header['encoding'] in ['bzip2', 'bz2']:
        decompobj = bz2.BZ2Decompressor()
    else:
        # Must close the file because if the file was opened above from detached filename, there is no "with" block to
        # close it for us
        _close_data_source(fh, data_filename)

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
        _close_data_source(fh, data_filename)
        raise NRRDError('Data ended before reaching the end of the compressed stream')

    # Byte skip is applied AFTER the decompression. Skip first x bytes of the decompressed data and parse it using
    # NumPy
    return np.frombuffer(decompressed_data[byte_skip:], dtype)


def _is_integral(value: Any) -> bool:
    return isinstance(value, (int, np.integer)) and not isinstance(value, bool)


def _normalize_axis_range(axis: RegionAxis, size: int, axis_index: int) -> Tuple[int, int]:
    """Normalize one axis selection to an inclusive ``(start, stop)`` pair.

    An empty selection is normalized to ``(start, start - 1)``.
    """

    if axis is None:
        return 0, size - 1

    if isinstance(axis, slice):
        if axis.step not in (None, 1):
            raise NRRDError(f'Region axis {axis_index} has a step of {axis.step}. Only contiguous selections with a '
                            'step of 1 are supported')
        start, stop, _ = axis.indices(size)
        return start, stop - 1

    if _is_integral(axis):
        index = int(axis)
        if index < 0:
            index += size
        if not 0 <= index < size:
            raise NRRDError(f'Region index {axis} is out of bounds for axis {axis_index} with size {size}')
        return index, index

    if isinstance(axis, np.ndarray):
        if axis.ndim != 1:
            raise NRRDError(f'Region axis {axis_index} must be one-dimensional')
        values = axis.tolist()
    elif isinstance(axis, range):
        if axis.step not in (1, -1):
            raise NRRDError(f'Region axis {axis_index} has a step of {axis.step}. Only contiguous selections with a '
                            'step of 1 are supported')
        values = list(axis)
    elif isinstance(axis, (tuple, list)):
        values = list(axis)
    else:
        raise NRRDError(f'Invalid region specification for axis {axis_index}: {axis!r}')

    # A 2-tuple is an inclusive ``(start, stop)`` range. ``stop == start - 1`` represents an empty selection,
    # including the conventional ``(0, -1)`` and ``(size, size - 1)`` forms. A list (or any other sequence) is a
    # sequence of explicit indices.
    if isinstance(axis, tuple) and len(values) == 2 and all(value is None or _is_integral(value) for value in values):
        start = 0 if values[0] is None else int(values[0])
        stop = size - 1 if values[1] is None else int(values[1])
        if start < 0 or start > size:
            raise NRRDError(f'Region start {values[0]} is out of bounds for axis {axis_index} with size {size}')
        if stop < -1 or stop >= size:
            raise NRRDError(f'Region stop {values[1]} is out of bounds for axis {axis_index} with size {size}')
        if stop < start - 1:
            raise NRRDError(f'Region range ({values[0]}, {values[1]}) is not a valid contiguous range for axis '
                            f'{axis_index} with size {size}')
        return start, stop

    if len(values) == 0:
        return 0, -1

    if not all(_is_integral(value) for value in values):
        raise NRRDError(f'Region axis {axis_index} must contain integers')
    indices = [int(value) + size if int(value) < 0 else int(value) for value in values]
    if any(index < 0 or index >= size for index in indices):
        raise NRRDError(f'Region indices {values} are out of bounds for axis {axis_index} with size {size}')
    if any(b != a + 1 for a, b in zip(indices, indices[1:])) or indices != sorted(indices):
        raise NRRDError(f'Region indices for axis {axis_index} are not contiguous: {values}')
    return indices[0], indices[-1]


def _normalize_region(region: Region, sizes: Sequence[int], index_order: IndexOrder) -> Tuple[Tuple[int, int], ...]:
    """Normalize a caller-ordered region into inclusive ranges in NRRD (fastest-first) axis order."""

    if isinstance(region, (str, bytes)) or not hasattr(region, '__len__'):
        raise NRRDError('Region must be a sequence with one entry per axis')

    if len(region) != len(sizes):
        raise NRRDError(f'Region has {len(region)} axes but the data has {len(sizes)} dimensions')

    # The caller expresses the region in its own index order. Axes in the NRRD header are fastest-varying first.
    caller_sizes = sizes if index_order == 'F' else sizes[::-1]
    caller_ranges = tuple(_normalize_axis_range(axis, int(size), index)
                          for index, (axis, size) in enumerate(zip(region, caller_sizes)))
    if index_order == 'F':
        return caller_ranges
    return caller_ranges[::-1]


def _read_raw_region(fh: IO, dtype: np.dtype, sizes: np.ndarray, ranges: Tuple[Tuple[int, int], ...]) \
        -> npt.NDArray:
    """Read a region of raw data directly, one read per contiguous run in storage layout."""

    sizes = np.asarray(sizes, dtype=np.int64)
    starts = np.array([start for start, _ in ranges], dtype=np.int64)
    lengths = np.array([stop - start + 1 for start, stop in ranges], dtype=np.int64)

    # Element stride of each axis in the on-disk layout. Axis 0 varies fastest, as required by NRRD.
    strides = np.ones(len(sizes), dtype=np.int64)
    if len(sizes) > 1:
        strides[1:] = np.cumprod(sizes[:-1]).astype(np.int64)

    itemsize = dtype.itemsize
    data_start = fh.tell()
    buffer = bytearray()

    for offset, count in _iter_region_blocks(sizes, starts, lengths, strides):
        start_byte = offset * itemsize
        nbytes = count * itemsize
        fh.seek(data_start + start_byte)
        chunk = fh.read(nbytes)

        if len(chunk) != nbytes:
            raise NRRDError(f'Data ended before reaching the end of the selected region: expected {nbytes} bytes, '
                            f'read {len(chunk)}')
        buffer += chunk

    return np.frombuffer(buffer, dtype).reshape(tuple(int(length) for length in lengths), order='F')


def _iter_region_blocks(sizes: np.ndarray, starts: np.ndarray, lengths: np.ndarray, strides: np.ndarray):

    selected_points = int(np.prod(lengths, dtype=np.int64))
    if selected_points == 0:
        return

    # Axes faster than the first partially selected axis are fully covered, so each outer index selects one
    # contiguous run of values. Coalescing those axes keeps the number of reads proportional to the number of
    # selected rows/slices rather than to the number of selected values.
    first_partial = 0
    while first_partial < len(sizes) and lengths[first_partial] == sizes[first_partial]:
        first_partial += 1

    if first_partial == len(sizes):
        yield 0, selected_points
        return

    block_axis = first_partial
    block_length = int(lengths[block_axis] * strides[block_axis])
    base_offset = int(starts[block_axis] * strides[block_axis])

    # Iterate the slower axes from slowest to fastest so reads are issued in ascending storage order.
    higher_axes = list(range(block_axis + 1, len(sizes)))
    if higher_axes:
        outer_ranges = [range(int(starts[axis]), int(starts[axis] + lengths[axis]))
                        for axis in reversed(higher_axes)]
        combinations = itertools.product(*outer_ranges)
    else:
        combinations = [()]

    for combination in combinations:
        offset = base_offset
        for axis, index in zip(higher_axes, reversed(combination)):
            offset += index * int(strides[axis])
        yield offset, block_length


def _crop_header(header: NRRDHeader, ranges: Tuple[Tuple[int, int], ...], data: npt.NDArray) -> NRRDHeader:
    """Build a copy of the header describing the selected region."""

    cropped = OrderedDict(header)
    cropped_sizes = [stop - start + 1 for start, stop in ranges]
    cropped['sizes'] = np.array(cropped_sizes, dtype=np.int64)

    # Per-axis fields such as spacings, kinds, labels and space directions keep one entry per axis. Their values are
    # properties of the axes and do not change when a sub-range is selected.

    # Shift the space origin by the number of voxels removed along each (spatial) axis.
    if 'space origin' in cropped and 'space directions' in cropped and len(ranges) == header['dimension']:
        directions = cropped['space directions']
        origin = np.asarray(cropped['space origin'], dtype=float).copy()
        try:
            for axis, (start, _) in enumerate(ranges):
                vector = directions[axis]
                if vector is None:
                    continue
                vector = np.asarray(vector, dtype=float)
                if vector.shape and np.all(np.isfinite(vector)):
                    origin += start * vector
            cropped['space origin'] = origin
        except (TypeError, IndexError, ValueError):
            # Leave the origin untouched for headers whose space directions cannot be interpreted.
            pass

    # These fields describe how to locate the original (uncropped) data in the file; they do not apply to the crop.
    for field in ['datafile', 'data file', 'byteskip', 'byte skip', 'lineskip', 'line skip',
                  'oldmin', 'old min', 'oldmax', 'old max']:
        cropped.pop(field, None)

    # The min/max now describe the selected values, not the full data set.
    if ('min' in cropped or 'max' in cropped) and data.dtype.kind in 'iuf':
        if data.size > 0:
            if 'min' in cropped:
                cropped['min'] = float(data.min())
            if 'max' in cropped:
                cropped['max'] = float(data.max())
        else:
            cropped.pop('min', None)
            cropped.pop('max', None)

    return cropped


def _read_data_region(header: NRRDHeader, fh: Optional[IO], filename: Optional[str], region: Region,
                      index_order: IndexOrder) -> Tuple[npt.NDArray, NRRDHeader, RegionInfo]:
    """Read a bounded region, returning the data, the cropped header, and the original range."""

    dtype = _validate_data_header(header, index_order)
    sizes = np.asarray(header['sizes'], dtype=np.int64)
    ranges = _normalize_region(region, sizes, index_order)

    if fh is None and header.get('datafile', header.get('data file', None)) is None:
        raise NRRDError('File handle must be specified to read data that is attached to the header')

    caller_ranges = ranges if index_order == 'F' else ranges[::-1]
    caller_sizes = sizes if index_order == 'F' else sizes[::-1]
    region_info = RegionInfo(sizes=tuple(int(size) for size in caller_sizes),
                             starts=tuple(int(start) for start, _ in caller_ranges),
                             stops=tuple(int(stop) for _, stop in caller_ranges),
                             index_order=index_order,
                             full_decode=header['encoding'] != 'raw')

    if header['encoding'] == 'raw':
        fh, opened_filename, _, _ = _prepare_data_source(header, fh, filename, dtype)
        try:
            if not hasattr(fh, 'seek') or not hasattr(fh, 'tell'):
                raise NRRDError('Bounded reads of raw data require a seekable file handle')
            selected = _read_raw_region(fh, dtype, sizes, ranges)
        finally:
            _close_data_source(fh, opened_filename)

        data = selected if index_order == 'F' else selected.T
        return data, _crop_header(header, ranges, data), region_info

    # Compressed and ASCII encodings cannot be decoded partially: the whole stream has to be consumed before the
    # selected region can be extracted. Report that cost rather than implying a bounded read.
    selected_points = int(np.prod([stop - start + 1 for start, stop in ranges], dtype=np.int64))
    if selected_points != int(sizes.prod(dtype=np.int64)):
        encoding = header['encoding']
        cost = int(sizes.prod(dtype=np.int64)) * dtype.itemsize
        warnings.warn(f"Encoding '{encoding}' cannot be read partially. The file must be decompressed/parsed in full "
                      f"({int(sizes.prod(dtype=np.int64))} values, {cost} bytes after decoding) to return the selected "
                      f"{selected_points} values.", UserWarning, stacklevel=3)

    full_data = _read_data_full(header, fh, filename, index_order)
    slices = tuple(slice(start, stop + 1) for start, stop in caller_ranges)
    data = full_data[slices]
    return data, _crop_header(header, ranges, data), region_info


def read_region(filename: str, region: Region, custom_field_map: Optional[NRRDFieldMap] = None,
                index_order: IndexOrder = 'F') -> Tuple[npt.NDArray, NRRDHeader, RegionInfo]:
    """Read one index region from a NRRD file and return it with its cropped header and original range

    For the uncompressed ``raw`` encoding, only the bytes of the selected region are read. Reads follow the actual
    storage layout: each contiguous run of selected values becomes one read, and runs are coalesced when all faster
    axes are fully covered. For ``gzip``/``bzip2`` and ``ASCII`` encodings the whole stream must be decoded first; a
    :class:`UserWarning` is issued and :attr:`RegionInfo.full_decode` is set to :obj:`True`.

    Parameters
    ----------
    filename : :class:`str`
        Filename of the NRRD file
    region : sequence
        Index region with one entry per axis, expressed in :obj:`index_order`. Each entry may be an inclusive
        ``(start, stop)`` pair, a :class:`slice` with unit step, a single integer, or a contiguous sequence of
        integers. A value of :obj:`None` selects the full extent of that axis.
    custom_field_map : :class:`dict` (:class:`str`, :class:`str`), optional
        Dictionary used for parsing custom field types.
    index_order : {'C', 'F'}, optional
        Index order in which :obj:`region` is expressed and in which the returned data is ordered.

    Returns
    -------
    data : :class:`numpy.ndarray`
        The selected values, with shape matching the region.
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Copy of the header cropped to the region. ``sizes`` and other per-axis fields describe the returned data, and
        ``space origin`` is shifted accordingly.
    region_info : :class:`nrrd.types.RegionInfo`
        The full data sizes and the inclusive range of the selected region, in :obj:`index_order`, so that local
        indices can be mapped back to the original data.

    See Also
    --------
    :meth:`read`, :meth:`read_data`
    """

    with open(filename, 'rb') as fh:
        header = read_header(fh, custom_field_map)
        return _read_data_region(header, fh, filename, region, index_order)


def read(filename: str, custom_field_map: Optional[NRRDFieldMap] = None, index_order: IndexOrder = 'F',
         region: Optional[Region] = None) \
        -> Union[Tuple[npt.NDArray, NRRDHeader], Tuple[npt.NDArray, NRRDHeader, RegionInfo]]:
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
    region : sequence, optional
        Index region to read, with one entry per axis expressed in :obj:`index_order`. See :meth:`read_region` for the
        accepted forms and the cost of each encoding. When given, the returned header is cropped to the region and a
        third :class:`nrrd.types.RegionInfo` value describing the original range is returned as well.

    Returns
    -------
    data : :class:`numpy.ndarray`
        Data read from NRRD file
    header : :class:`dict` (:class:`str`, :obj:`Object`)
        Dictionary containing the header fields and their corresponding parsed value. When :obj:`region` is given,
        the header is cropped to the selected region.
    region_info : :class:`nrrd.types.RegionInfo`
        Only returned when :obj:`region` is given. Contains the original full sizes and the inclusive selected range,
        in :obj:`index_order`.

    See Also
    --------
    :meth:`write`, :meth:`read_header`, :meth:`read_data`, :meth:`read_region`
    """

    with open(filename, 'rb') as fh:
        header = read_header(fh, custom_field_map)

        if region is None:
            data = read_data(header, fh, filename, index_order)
            return data, header

        data, cropped_header, region_info = _read_data_region(header, fh, filename, region, index_order)
        return data, cropped_header, region_info
