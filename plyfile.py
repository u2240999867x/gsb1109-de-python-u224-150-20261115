#   Copyright 2014-2026 Darsh Ranjan and plyfile authors.
#
#   This file is part of python-plyfile.
#
#   python-plyfile is free software: you can redistribute it and/or
#   modify it under the terms of the GNU General Public License as
#   published by the Free Software Foundation, either version 3 of the
#   License, or (at your option) any later version.
#
#   python-plyfile is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with python-plyfile.  If not, see
#       <http://www.gnu.org/licenses/>.
"""
NumPy-based PLY format input and output for Python.
"""

import io as _io
from itertools import islice as _islice

import numpy as _np
from sys import byteorder as _byteorder


class PlyData(object):
    """
    PLY file header and data.

    A `PlyData` instance is created in one of two ways: by the static
    method `PlyData.read` (to read a PLY file), or directly from
    `__init__` given a sequence of elements (which can then be written
    to a PLY file).

    Attributes
    ----------
    elements : list of PlyElement
    comments : list of str
    obj_info : list of str
    text : bool
    byte_order : {'<', '>', '='}
    header : str
    """

    def __init__(self, elements=[], text=False, byte_order='=',
                 comments=[], obj_info=[], nan_policy='preserve'):
        """
        Parameters
        ----------
        elements : iterable of PlyElement
        text : bool, optional
            Whether the resulting PLY file will be text (True) or
            binary (False).
        byte_order : {'<', '>', '='}, optional
            `'<'` for little-endian, `'>'` for big-endian, or `'='`
            for native.  This is only relevant if `text` is False.
        comments : iterable of str, optional
            Comment lines between "ply" and "format" lines.
        obj_info : iterable of str, optional
            like comments, but will be placed in the header with
            "obj_info ..." instead of "comment ...".
        nan_policy : {'preserve', 'error'}, optional
            How to treat NaN values when writing.  `'preserve'` writes
            them as NaNs when the property type is a floating-point
            type.  `'error'` rejects them.  NaNs are never converted to
            an integer (or to zero); an attempt to do so is an error.
        """
        self.byte_order = byte_order
        self.text = text
        self.nan_policy = nan_policy

        self.comments = comments
        self.obj_info = obj_info
        self.elements = elements

    def _get_elements(self):
        return self._elements

    def _set_elements(self, elements):
        self._elements = tuple(elements)
        self._index()

    elements = property(_get_elements, _set_elements)

    def _get_byte_order(self):
        if not self.text and self._byte_order == '=':
            return _native_byte_order
        return self._byte_order

    def _set_byte_order(self, byte_order):
        if byte_order not in ['<', '>', '=']:
            raise ValueError("byte order must be '<', '>', or '='")

        self._byte_order = byte_order

    byte_order = property(_get_byte_order, _set_byte_order)

    def _index(self):
        self._element_lookup = dict((elt.name, elt) for elt in
                                    self._elements)
        if len(self._element_lookup) != len(self._elements):
            raise ValueError("two elements with same name")

    def _get_comments(self):
        return list(self._comments)

    def _set_comments(self, comments):
        _check_comments(comments)
        self._comments = list(comments)

    comments = property(_get_comments, _set_comments)

    def _get_obj_info(self):
        return list(self._obj_info)

    def _set_obj_info(self, obj_info):
        _check_comments(obj_info)
        self._obj_info = list(obj_info)

    obj_info = property(_get_obj_info, _set_obj_info)

    def _get_nan_policy(self):
        return self._nan_policy

    def _set_nan_policy(self, nan_policy):
        self._nan_policy = _normalize_nan_policy(nan_policy)

    nan_policy = property(_get_nan_policy, _set_nan_policy)

    @staticmethod
    def _parse_header(stream):
        parser = _PlyHeaderParser(_PlyHeaderLines(stream))
        return PlyData(
            [PlyElement(*e) for e in parser.elements],
            parser.format == 'ascii',
            _byte_order_map[parser.format],
            parser.comments,
            parser.obj_info
        )

    @staticmethod
    def read(stream, mmap='c', known_list_len={}, nan_policy='preserve'):
        """
        Read PLY data from a readable file-like object or filename.

        Parameters
        ----------
        stream : str or readable open file
        mmap : {'c', 'r', 'r+'} or bool, optional (default='c')
            Configures memory-mapping. Any falsy value disables
            memory mapping, and any non-string truthy value is
            equivalent to 'c', for copy-on-write mapping.
        known_list_len : dict, optional
            Mapping from element names to mappings from list property
            names to their fixed lengths.  This optional argument is
            necessary to enable memory mapping of elements that contain
            list properties. (Note that elements with variable-length
            list properties cannot be memory-mapped.)
        nan_policy : {'preserve', 'error'}, optional
            Policy for NaN values.  `'preserve'` reads NaNs as NaNs;
            `'error'` raises `PlyElementParseError` as soon as a NaN is
            found in the data.  The policy is also attached to the
            returned `PlyData` instance for later writes.

        Raises
        ------
        PlyParseError
            If the file cannot be parsed for any reason.
        ValueError
            If `stream` is open in text mode but the PLY header
            indicates binary encoding.
        """
        nan_policy = _normalize_nan_policy(nan_policy)
        (must_close, stream) = _open_stream(stream, 'read')
        try:
            data = PlyData._parse_header(stream)
            data.nan_policy = nan_policy
            if isinstance(stream.read(0), str):
                if data.text:
                    data_stream = stream
                else:
                    raise ValueError("can't read binary-format PLY "
                                     "from text stream")
            else:
                if data.text:
                    data_stream = _io.TextIOWrapper(stream, 'ascii')
                else:
                    data_stream = stream
            for elt in data:
                elt._read(data_stream, data.text, data.byte_order, mmap,
                          known_list_len=known_list_len.get(elt.name, {}))
                if nan_policy == 'error':
                    _check_element_nan(elt)
        finally:
            if must_close:
                stream.close()

        return data

    def write(self, stream, nan_policy=None):
        """
        Write PLY data to a writeable file-like object or filename.

        Parameters
        ----------
        stream : str or writeable open file
        nan_policy : {'preserve', 'error'}, optional
            Overrides the instance's `nan_policy` for this write.

        Raises
        ------
        ValueError
            If `stream` is open in text mode and the file to be written
            is binary-format.
        PlyElementWriteError
            If a value cannot be represented by its property type
            without loss.
        """
        if nan_policy is None:
            nan_policy = self.nan_policy
        else:
            nan_policy = _normalize_nan_policy(nan_policy)

        (must_close, stream) = _open_stream(stream, 'write')
        try:
            try:
                stream.write(b'')
                binary_stream = True
            except TypeError:
                binary_stream = False
            if binary_stream:
                stream.write(self.header.encode('ascii'))
                stream.write(b'\n')
            else:
                if not self.text:
                    raise ValueError("can't write binary-format PLY to "
                                     "text stream")
                stream.write(self.header)
                stream.write('\n')
            for elt in self:
                elt._write(stream, self.text, self.byte_order,
                           nan_policy)
        finally:
            if must_close:
                stream.close()

    @property
    def header(self):
        """
        PLY-formatted metadata for the instance.
        """
        lines = ['ply']

        if self.text:
            lines.append('format ascii 1.0')
        else:
            lines.append('format ' +
                         _byte_order_reverse[self.byte_order] +
                         ' 1.0')

        # Some information is lost here, since all comments are placed
        # between the 'format' line and the first element.
        for c in self.comments:
            lines.append('comment ' + c)

        for c in self.obj_info:
            lines.append('obj_info ' + c)

        lines.extend(elt.header for elt in self.elements)
        lines.append('end_header')
        return '\n'.join(lines)

    def __iter__(self):
        """
        Iterate over the elements.
        """
        return iter(self.elements)

    def __len__(self):
        """
        Return the number of elements.
        """
        return len(self.elements)

    def __contains__(self, name):
        """
        Check if an element with the given name exists.
        """
        return name in self._element_lookup

    def __getitem__(self, name):
        """
        Retrieve an element by name.

        Parameters
        ----------
        name : str

        Returns
        -------
        PlyElement

        Raises
        ------
        KeyError
            If the element can't be found.
        """
        return self._element_lookup[name]

    def __str__(self):
        return self.header

    def __repr__(self):
        return ('PlyData(%r, text=%r, byte_order=%r, '
                'comments=%r, obj_info=%r, nan_policy=%r)' %
                (self.elements, self.text, self.byte_order,
                 self.comments, self.obj_info, self.nan_policy))


class PlyElement(object):
    """
    PLY file element.

    Creating a `PlyElement` instance is generally done in one of two
    ways: as a byproduct of `PlyData.read` (when reading a PLY file) and
    by `PlyElement.describe` (before writing a PLY file).

    Attributes
    ----------
    name : str
    count : int
    data : numpy.ndarray
    properties : list of PlyProperty
    comments : list of str
    header : str
        PLY header block for this element.
    """

    def __init__(self, name, properties, count, comments=[]):
        """
        This is not part of the public interface.  The preferred methods
        of obtaining `PlyElement` instances are `PlyData.read` (to read
        from a file) and `PlyElement.describe` (to construct from a
        `numpy` array).

        Parameters
        ----------
        name : str
        properties : list of PlyProperty
        count : str
        comments : list of str
        """
        _check_name(name)
        self._name = str(name)
        self._count = count

        self._properties = tuple(properties)
        self._index()

        self.comments = comments

        self._have_list = any(isinstance(p, PlyListProperty)
                              for p in self.properties)

    @property
    def count(self):
        return self._count

    def _get_data(self):
        return self._data

    def _set_data(self, data):
        self._data = data
        self._count = len(data)
        self._check_sanity()

    data = property(_get_data, _set_data)

    def _check_sanity(self):
        for prop in self.properties:
            if prop.name not in self._data.dtype.fields:
                raise ValueError("dangling property %r" % prop.name)

    def _get_properties(self):
        return self._properties

    def _set_properties(self, properties):
        self._properties = tuple(properties)
        self._check_sanity()
        self._index()

    properties = property(_get_properties, _set_properties)

    def _get_comments(self):
        return list(self._comments)

    def _set_comments(self, comments):
        _check_comments(comments)
        self._comments = list(comments)

    comments = property(_get_comments, _set_comments)

    def _index(self):
        self._property_lookup = dict((prop.name, prop)
                                     for prop in self._properties)
        if len(self._property_lookup) != len(self._properties):
            raise ValueError("two properties with same name")

    def ply_property(self, name):
        """
        Look up property by name.

        Parameters
        ----------
        name : str

        Returns
        -------
        PlyProperty

        Raises
        ------
        KeyError
            If the property can't be found.
        """
        return self._property_lookup[name]

    @property
    def name(self):
        return self._name

    def dtype(self, byte_order='='):
        """
        Return the `numpy.dtype` description of the in-memory
        representation of the data.  (If there are no list properties,
        and the PLY format is binary, then this also accurately
        describes the on-disk representation of the element.)

        Parameters
        ----------
        byte_order : {'<', '>', '='}

        Returns
        -------
        numpy.dtype
        """
        return _np.dtype([(prop.name, prop.dtype(byte_order))
                          for prop in self.properties])

    @staticmethod
    def describe(data, name, len_types={}, val_types={},
                 comments=[]):
        """
        Construct a `PlyElement` instance from an array's metadata.

        Parameters
        ----------
        data : numpy.ndarray
            Structured `numpy` array.
        len_types : dict, optional
            Mapping from list property names to type strings
            (`numpy`-style like `'u1'`, `'f4'`, etc., or PLY-style like
            `'int8'`, `'float32'`, etc.), which will be used to encode
            the length of the list in binary-format PLY files.  Defaults
            to `'u1'` (8-bit integer) for all list properties.
        val_types : dict, optional
            Mapping from list property names to type strings as for
            `len_types`, but is used to encode the list elements in
            binary-format PLY files.  Defaults to `'i4'` (32-bit
            integer) for all list properties.
        comments : list of str
            Comments between the "element" line and first property
            definition in the header.

        Returns
        -------
        PlyElement

        Raises
        ------
        TypeError, ValueError
        """
        if not isinstance(data, _np.ndarray):
            raise TypeError("only numpy arrays are supported")

        if len(data.shape) != 1:
            raise ValueError("only one-dimensional arrays are "
                             "supported")

        count = len(data)

        properties = []
        descr = data.dtype.descr

        for t in descr:
            if not isinstance(t[1], str):
                raise ValueError("nested records not supported")

            if not t[0]:
                raise ValueError("field with empty name")

            if len(t) != 2 or t[1][1] == 'O':
                # non-scalar field, which corresponds to a list
                # property in PLY.

                if t[1][1] == 'O':
                    if len(t) != 2:
                        raise ValueError("non-scalar object fields not "
                                         "supported")

                len_str = _data_type_reverse[len_types.get(t[0], 'u1')]
                if t[1][1] == 'O':
                    val_type = val_types.get(t[0], 'i4')
                    val_str = _lookup_type(val_type)
                else:
                    val_str = _lookup_type(t[1][1:])

                prop = PlyListProperty(t[0], len_str, val_str)
            else:
                val_str = _lookup_type(t[1][1:])
                prop = PlyProperty(t[0], val_str)

            properties.append(prop)

        elt = PlyElement(name, properties, count, comments)
        elt.data = data

        return elt

    def _read(self, stream, text, byte_order, mmap,
              known_list_len={}):
        """
        Read the actual data from a PLY file.

        Parameters
        ----------
        stream : readable open file
        text : bool
        byte_order : {'<', '>', '='}
        mmap : {'c', 'r', 'r+'} or bool
        known_list_len : dict
        """
        if text:
            self._read_txt(stream)
        else:
            list_prop_names = set(p.name for p in self.properties
                                  if isinstance(p, PlyListProperty))
            can_mmap_lists = list_prop_names <= set(known_list_len)
            mmap_mode = mmap if isinstance(mmap, str) else 'c'
            if mmap and _can_mmap(stream, mmap_mode) and can_mmap_lists:
                # Loading the data is straightforward.  We will memory
                # map the file in the requested mode.
                self._read_mmap(stream, byte_order, mmap_mode,
                                known_list_len)
            else:
                # A simple load is impossible.
                self._read_bin(stream, byte_order)

        self._check_sanity()

    def _write(self, stream, text, byte_order, nan_policy='preserve'):
        """
        Write the data to a PLY file.

        Parameters
        ----------
        stream : writeable open file
        text : bool
        byte_order : {'<', '>', '='}
        nan_policy : {'preserve', 'error'}
        """
        if text:
            self._write_txt(stream, nan_policy)
        else:
            if self._have_list:
                # There are list properties, so serialization is
                # slightly complicated.
                self._write_bin(stream, byte_order, nan_policy)
            else:
                # no list properties, so serialization is
                # straightforward.
                stream.write(self._checked_data(byte_order,
                                               nan_policy).data)

    def _read_mmap(self, stream, byte_order, mmap_mode, known_list_len):
        """
        Memory-map an input file as `self.data`.

        Parameters
        ----------
        stream : readable open file
        byte_order : {'<', '>', '='}
        mmap_mode: str
        known_list_len : dict
        """
        list_len_props = {}
        # update the dtype to include the list length and list dtype
        new_dtype = []
        for p in self.properties:
            if isinstance(p, PlyListProperty):
                len_dtype, val_dtype = p.list_dtype(byte_order)
                # create new dtype for the list length
                new_dtype.append((p.name + "\nlen", len_dtype))
                # a new dtype with size for the list values themselves
                new_dtype.append((p.name, val_dtype,
                                  (known_list_len[p.name],)))
                list_len_props[p.name] = p.name + "\nlen"
            else:
                new_dtype.append((p.name, p.dtype(byte_order)))
        dtype = _np.dtype(new_dtype)
        num_bytes = self.count * dtype.itemsize
        offset = stream.tell()
        stream.seek(0, 2)
        max_bytes = stream.tell() - offset
        if max_bytes < num_bytes:
            raise PlyElementParseError("early end-of-file", self,
                                       max_bytes // dtype.itemsize)
        self._data = _np.memmap(stream, dtype, mmap_mode, offset, self.count)
        # Fix stream position
        stream.seek(offset + self.count * dtype.itemsize)
        # remove any extra properties added
        for prop in list_len_props:
            field = list_len_props[prop]
            len_check = self._data[field] == known_list_len[prop]
            if not len_check.all():
                row = _np.flatnonzero(len_check ^ True)[0]
                raise PlyElementParseError(
                    "unexpected list length",
                    self, row, self.ply_property(prop))
        props = [p.name for p in self.properties]
        self._data = self._data[props]

    def _read_txt(self, stream):
        """
        Load a PLY element from an ASCII-format PLY file.  The element
        may contain list properties.

        Parameters
        ----------
        stream : readable open file
        """
        self._data = _np.empty(self.count, dtype=self.dtype())

        k = 0
        for line in _islice(iter(stream.readline, ''), self.count):
            fields = iter(line.strip().split())
            for prop in self.properties:
                try:
                    self._data[prop.name][k] = prop._from_fields(fields)
                except StopIteration:
                    raise PlyElementParseError("early end-of-line",
                                               self, k, prop)
                except ValueError:
                    raise PlyElementParseError("malformed input",
                                               self, k, prop)
            try:
                next(fields)
            except StopIteration:
                pass
            else:
                raise PlyElementParseError("expected end-of-line",
                                           self, k)
            k += 1

        if k < self.count:
            del self._data
            raise PlyElementParseError("early end-of-file", self, k)

    def _write_txt(self, stream, nan_policy='preserve'):
        """
        Save a PLY element to an ASCII-format PLY file.  The element may
        contain list properties.

        Parameters
        ----------
        stream : writeable open file
        nan_policy : {'preserve', 'error'}
        """
        scalar_fields = {}
        for prop in self.properties:
            if not isinstance(prop, PlyListProperty):
                scalar_fields[prop.name] = _checked_array_cast(
                    self.data[prop.name], prop.dtype(), self, None, prop,
                    nan_policy)

        for k, rec in enumerate(self.data):
            fields = []
            for prop in self.properties:
                if isinstance(prop, PlyListProperty):
                    fields.extend(prop._to_fields(rec[prop.name], self, k,
                                                  nan_policy))
                else:
                    fields.append(_format_value(scalar_fields[prop.name][k]))

            _np.savetxt(stream, [fields], '%s', newline='\n')

    def _checked_data(self, byte_order, nan_policy):
        """
        Return the element's data cast to its on-disk representation,
        checking that every value is represented exactly.

        Parameters
        ----------
        byte_order : {'<', '>', '='}
        nan_policy : {'preserve', 'error'}

        Returns
        -------
        numpy.ndarray
        """
        dtype = self.dtype(byte_order)
        data = _np.empty(self.count, dtype=dtype)
        for prop in self.properties:
            data[prop.name] = _checked_array_cast(
                self.data[prop.name], prop.dtype(byte_order), self,
                None, prop, nan_policy)
        return data

    def _read_bin(self, stream, byte_order):
        """
        Load a PLY element from a binary PLY file.  The element may
        contain list properties.

        Parameters
        ----------
        stream : readable open file
        byte_order : {'<', '>', '='}
        """
        self._data = _np.empty(self.count, dtype=self.dtype(byte_order))

        for k in range(self.count):
            for prop in self.properties:
                try:
                    self._data[prop.name][k] = \
                        prop._read_bin(stream, byte_order)
                except StopIteration:
                    raise PlyElementParseError("early end-of-file",
                                               self, k, prop)

    def _write_bin(self, stream, byte_order, nan_policy='preserve'):
        """
        Save a PLY element to a binary PLY file.  The element may
        contain list properties.

        Parameters
        ----------
        stream : writeable open file
        byte_order : {'<', '>', '='}
        nan_policy : {'preserve', 'error'}
        """
        for k, rec in enumerate(self.data):
            for prop in self.properties:
                prop._write_bin(rec[prop.name], stream, byte_order,
                                self, k, nan_policy)

    @property
    def header(self):
        lines = ['element %s %d' % (self.name, self.count)]

        # Some information is lost here, since all comments are placed
        # between the 'element' line and the first property definition.
        for c in self.comments:
            lines.append('comment ' + c)

        lines.extend(list(map(str, self.properties)))

        return '\n'.join(lines)

    def __len__(self):
        """
        Return the number of rows in the element.
        """
        return self.count

    def __contains__(self, name):
        """
        Determine if a property with the given name exists.
        """
        return name in self._property_lookup

    def __getitem__(self, key):
        """
        Proxy to `self.data.__getitem__` for convenience.
        """
        return self.data[key]

    def __setitem__(self, key, value):
        """
        Proxy to `self.data.__setitem__` for convenience.
        """
        self.data[key] = value

    def __str__(self):
        return self.header

    def __repr__(self):
        return ('PlyElement(%r, %r, count=%d, comments=%r)' %
                (self.name, self.properties, self.count,
                 self.comments))


class PlyProperty(object):
    """
    PLY property description.

    This class is pure metadata. The data itself is contained in
    `PlyElement` instances.

    Attributes
    ----------
    name : str
    val_dtype : str
        `numpy.dtype` description for the property's data.
    """

    def __init__(self, name, val_dtype):
        """
        Parameters
        ----------
        name : str
        val_dtype : str
        """
        _check_name(name)
        self._name = str(name)
        self.val_dtype = val_dtype

    def _get_val_dtype(self):
        return self._val_dtype

    def _set_val_dtype(self, val_dtype):
        self._val_dtype = _data_types[_lookup_type(val_dtype)]

    val_dtype = property(_get_val_dtype, _set_val_dtype)

    @property
    def name(self):
        return self._name

    def dtype(self, byte_order='='):
        """
        Return the `numpy.dtype` description for this property.

        Parameters
        ----------
        byte_order : {'<', '>', '='}, default='='

        Returns
        -------
        tuple of str
        """
        return byte_order + self.val_dtype

    def _from_fields(self, fields):
        """
        Parse data from generator.

        Parameters
        ----------
        fields : iterator of str

        Returns
        -------
        data
            Parsed data of the correct type.

        Raises
        ------
        StopIteration
            if the property's data could not be read.
        """
        return _parse_scalar(next(fields), self.dtype())

    def _to_fields(self, data, element=None, row=None,
                   nan_policy='preserve'):
        """
        Parameters
        ----------
        data
            Property data to encode.
        element : PlyElement, optional
        row : int, optional
        nan_policy : {'preserve', 'error'}, optional

        Yields
        ------
        encoded_data
            Data with type consistent with `self.val_dtype`.
        """
        yield _format_scalar(data, self.dtype(), element, row, self,
                             nan_policy)

    def _read_bin(self, stream, byte_order):
        """
        Read data from a binary stream.

        Parameters
        ----------
        stream : readable open binary file
        byte_order : {'<'. '>', '='}

        Raises
        ------
        StopIteration
            If the property data could not be read.
        """
        try:
            return _read_array(stream, self.dtype(byte_order), 1)[0]
        except IndexError:
            raise StopIteration

    def _write_bin(self, data, stream, byte_order, element=None,
                   row=None, nan_policy='preserve'):
        """
        Write data to a binary stream.

        Parameters
        ----------
        data
            Property data to encode.
        stream : writeable open binary file
        byte_order : {'<', '>', '='}
        element : PlyElement, optional
        row : int, optional
        nan_policy : {'preserve', 'error'}, optional
        """
        _write_array(stream, _checked_scalar_cast(
            data, self.dtype(byte_order), element, row, self, nan_policy))

    def __str__(self):
        val_str = _data_type_reverse[self.val_dtype]
        return 'property %s %s' % (val_str, self.name)

    def __repr__(self):
        return 'PlyProperty(%r, %r)' % (self.name,
                                        _lookup_type(self.val_dtype))


class PlyListProperty(PlyProperty):
    """
    PLY list property description.

    Attributes
    ----------
    name
    val_dtype
    len_dtype : str
        `numpy.dtype` description for the property's length field.
    """

    def __init__(self, name, len_dtype, val_dtype):
        """
        Parameters
        ----------
        name : str
        len_dtype : str
        val_dtype : str
        """
        PlyProperty.__init__(self, name, val_dtype)

        self.len_dtype = len_dtype

    def _get_len_dtype(self):
        return self._len_dtype

    def _set_len_dtype(self, len_dtype):
        self._len_dtype = _data_types[_lookup_type(len_dtype)]

    len_dtype = property(_get_len_dtype, _set_len_dtype)

    def dtype(self, byte_order='='):
        """
        `numpy.dtype` name for the property's field in the element.

        List properties always have a numpy dtype of "object".

        Parameters
        ----------
        byte_order : {'<', '>', '='}

        Returns
        -------
        dtype : str
            Always `'|O'`.
        """
        return '|O'

    def list_dtype(self, byte_order='='):
        """
        Return the pair `(len_dtype, val_dtype)` (both numpy-friendly
        strings).

        Parameters
        ----------
        byte_order : {'<', '>', '='}

        Returns
        -------
        len_dtype : str
        val_dtype : str
        """
        return (byte_order + self.len_dtype,
                byte_order + self.val_dtype)

    def _from_fields(self, fields):
        """
        Parse data from generator.

        Parameters
        ----------
        fields : iterator of str

        Returns
        -------
        data : numpy.ndarray
            Parsed list data for the property.

        Raises
        ------
        StopIteration
            if the property's data could not be read.
        """
        (len_t, val_t) = self.list_dtype()

        n = int(_parse_scalar(next(fields), len_t))
        if n < 0:
            raise ValueError("negative list length")

        tokens = list(_islice(fields, n))
        if n == 0:
            return _np.empty(0, dtype=val_t)

        data = _np.loadtxt(tokens, val_t, ndmin=1)
        if len(data) < n:
            raise StopIteration

        return data

    def _to_fields(self, data, element=None, row=None,
                   nan_policy='preserve'):
        """
        Return generator over the (numerical) PLY representation of the
        list data (length followed by actual data).

        Parameters
        ----------
        data : numpy.ndarray
            Property data to encode.
        element : PlyElement, optional
        row : int, optional
        nan_policy : {'preserve', 'error'}, optional

        Yields
        ------
        Length followed by each list element.
        """
        (len_t, val_t) = self.list_dtype()

        values = _checked_array_cast(data, val_t, element, row, self,
                                     nan_policy).ravel()

        yield _format_scalar(values.size, len_t, element, row, self,
                             nan_policy)
        for x in values:
            yield _format_value(x)

    def _read_bin(self, stream, byte_order):
        """
        Read data from a binary stream.

        Parameters
        ----------
        stream : readable open binary file
        byte_order : {'<', '>', '='}

        Returns
        -------
        data : numpy.ndarray

        Raises
        ------
        StopIteration
            If data could not be read.
        """
        (len_t, val_t) = self.list_dtype(byte_order)

        try:
            n = _read_array(stream, _np.dtype(len_t), 1)[0]
        except IndexError:
            raise StopIteration

        data = _read_array(stream, _np.dtype(val_t), n)
        if len(data) < n:
            raise StopIteration

        return data

    def _write_bin(self, data, stream, byte_order, element=None,
                   row=None, nan_policy='preserve'):
        """
        Write data to a binary stream.

        Parameters
        ----------
        data : numpy.ndarray
            Data to encode.
        stream : writeable open binary file
        byte_order : {'<', '>', '='}
        element : PlyElement, optional
        row : int, optional
        nan_policy : {'preserve', 'error'}, optional
        """
        (len_t, val_t) = self.list_dtype(byte_order)

        values = _checked_array_cast(data, val_t, element, row, self,
                                     nan_policy).ravel()
        length = _checked_scalar_cast(values.size, len_t, element, row,
                                      self, nan_policy)

        _write_array(stream, _np.asarray([length], dtype=len_t))
        _write_array(stream, values)

    def __str__(self):
        len_str = _data_type_reverse[self.len_dtype]
        val_str = _data_type_reverse[self.val_dtype]
        return 'property list %s %s %s' % (len_str, val_str, self.name)

    def __repr__(self):
        return ('PlyListProperty(%r, %r, %r)' %
                (self.name,
                 _lookup_type(self.len_dtype),
                 _lookup_type(self.val_dtype)))


class PlyParseError(Exception):
    """
    Base class for PLY parsing errors.
    """

    pass


class PlyElementParseError(PlyParseError):
    """
    Raised when a PLY element cannot be parsed.

    Attributes
    ----------
    message : str
    element : PlyElement
    row : int
    prop : PlyProperty
    """

    def __init__(self, message, element=None, row=None, prop=None):
        self.message = message
        self.element = element
        self.row = row
        self.prop = prop

        s = ''
        if self.element:
            s += 'element %r: ' % self.element.name
        if self.row is not None:
            s += 'row %d: ' % self.row
        if self.prop:
            s += 'property %r: ' % self.prop.name
        s += self.message

        Exception.__init__(self, s)

    def __repr__(self):
        return ('%s(%r, element=%r, row=%r, prop=%r)' %
                (self.__class__.__name__,
                 self.message, self.element, self.row, self.prop))


class PlyElementWriteError(PlyElementParseError, ValueError):
    """
    Raised when a PLY element cannot be written without losing data.

    Attributes
    ----------
    message : str
    element : PlyElement
    row : int
    prop : PlyProperty
    value
        The value that could not be represented, if available.
    """

    def __init__(self, message, element=None, row=None, prop=None,
                 value=None):
        self.value = value
        PlyElementParseError.__init__(self, message, element, row, prop)

    @property
    def record(self):
        """
        Alias for `row`.
        """
        return self.row

    def __repr__(self):
        return ('%s(%r, element=%r, row=%r, prop=%r, value=%r)' %
                (self.__class__.__name__,
                 self.message, self.element, self.row, self.prop,
                 self.value))


class PlyHeaderParseError(PlyParseError):
    """
    Raised when a PLY header cannot be parsed.

    Attributes
    ----------
    line : str
        Which header line the error occurred on.
    """

    def __init__(self, message, line=None):
        self.message = message
        self.line = line

        s = ''
        if self.line:
            s += 'line %r: ' % self.line
        s += self.message

        Exception.__init__(self, s)

    def __repr__(self):
        return ('%s(%r, line=%r)' %
                (self.__class__.__name__,
                 self.message, self.line))


class _PlyHeaderParser(object):
    """
    Parser for PLY format header.

    Attributes
    ----------
    format : str
        "ascii", "binary_little_endian", or "binary_big_endian"
    elements : list of (name, comments, count, properties)
    comments : list of str
    obj_info : list of str
    lines : int
    """

    def __init__(self, lines):
        """
        Parameters
        ----------
        lines : iterable of str
            Header lines, starting *after* the "ply" line.

        Raises
        ------
        PlyHeaderParseError
        """
        self.format = None
        self.elements = []
        self.comments = []
        self.obj_info = []
        self.lines = 1
        self._allowed = ['format', 'comment', 'obj_info']
        for line in lines:
            self.consume(line)
        if self._allowed:
            self._error("early end-of-file")

    def consume(self, raw_line):
        """
        Parse and internalize one line of input.
        """
        self.lines += 1
        if not raw_line:
            self._error("early end-of-file")

        line = raw_line.strip()
        if line == '':
            # We silently skip empty header lines. This isn't strictly
            # allowed in the spec, but this logic slightly improves
            # interoperability with other tools.
            return self._allowed
        try:
            keyword = line.split(None, 1)[0]
        except IndexError:
            self._error()

        if keyword not in self._allowed:
            self._error("expected one of {%s}" %
                        ", ".join(self._allowed))

        # This dynamic method lookup pattern is somewhat questionable,
        # but it's probably not worth replacing it with something more
        # principled but also more complex.
        getattr(self, 'parse_' + keyword)(line[len(keyword)+1:])
        return self._allowed

    def _error(self, message="parse error"):
        raise PlyHeaderParseError(message, self.lines)

    # The parse_* methods below are used to parse all the different
    # types of PLY header lines. (See `consume` above for the call site,
    # which uses dynamic lookup.) Each method accepts a single argument,
    # which is the remainder of the header line after the first word,
    # and the method does two things:
    # - internalize the semantic content of the string into the
    #   instance's attributes, and
    # - set self._allowed to a list of the line types that can come
    #   next.

    def parse_format(self, data):
        fields = data.strip().split()
        if len(fields) != 2:
            self._error("expected \"format {format} 1.0\"")

        self.format = fields[0]
        if self.format not in _byte_order_map:
            self._error("don't understand format %r" % self.format)

        if fields[1] != '1.0':
            self._error("expected version '1.0'")

        self._allowed = ['element', 'comment', 'obj_info', 'end_header']

    def parse_comment(self, data):
        if not self.elements:
            self.comments.append(data)
        else:
            self.elements[-1][3].append(data)

    def parse_obj_info(self, data):
        self.obj_info.append(data)

    def parse_element(self, data):
        fields = data.strip().split()
        if len(fields) != 2:
            self._error("expected \"element {name} {count}\"")

        name = fields[0]
        try:
            count = int(fields[1])
        except ValueError:
            self._error("expected integer count")

        self.elements.append((name, [], count, []))
        self._allowed = ['element', 'comment', 'property', 'end_header']

    def parse_property(self, data):
        properties = self.elements[-1][1]
        fields = data.strip().split()
        if len(fields) < 2:
            self._error("bad 'property' line")

        if fields[0] == 'list':
            if len(fields) != 4:
                self._error("expected \"property list "
                            "{len_type} {val_type} {name}\"")

            try:
                properties.append(
                    PlyListProperty(fields[3], fields[1], fields[2])
                )
            except ValueError as e:
                self._error(str(e))

        else:
            if len(fields) != 2:
                self._error("expected \"property {type} {name}\"")

            try:
                properties.append(
                    PlyProperty(fields[1], fields[0])
                )
            except ValueError as e:
                self._error(str(e))

    def parse_end_header(self, data):
        if data:
            self._error("unexpected data after 'end_header'")
        self._allowed = []


class _PlyHeaderLines(object):
    """
    Generator over lines in the PLY header.

    LF, CR, and CRLF line endings are supported.
    """

    def __init__(self, stream):
        """
        Parameters
        ----------
        stream : text or binary stream.

        Raises
        ------
        PlyHeaderParseError
        """
        s = self._decode(stream.read(4))
        self.chars = []
        if s[:3] != 'ply':
            raise PlyHeaderParseError("expected 'ply'", 1)
        self.nl = s[3:]
        if s[3:] == '\r':
            c = self._decode(stream.read(1))
            if c == '\n':
                self.nl += c
            else:
                self.chars.append(c)
        elif s[3:] != '\n':
            raise PlyHeaderParseError(
                "unexpected characters after 'ply'", 1)
        self.stream = stream
        self.len_nl = len(self.nl)
        self.done = False
        self.lines = 1

    @staticmethod
    def _decode(s):
        """
        Convert input `str` or `bytes` instance to `str`, decoding
        as ASCII if necessary.
        """
        if isinstance(s, str):
            return s
        try:
            return s.decode('ascii')
        except UnicodeDecodeError:
            raise PlyHeaderParseError(
                "header contains non-ASCII characters")

    def __iter__(self):
        """
        Yields
        ------
        line : str
            Decoded line with newline removed.
        """
        while not self.done:
            self.lines += 1
            while ''.join(self.chars[-self.len_nl:]) != self.nl:
                char = self._decode(self.stream.read(1))
                if not char:
                    raise PlyHeaderParseError("early end-of-file",
                                              self.lines)
                self.chars.append(char)
            line = ''.join(self.chars[:-self.len_nl])
            self.chars = []
            if line == 'end_header':
                self.done = True
            yield line


def _open_stream(stream, read_or_write):
    """
    Normalizing function: given a filename or open stream,
    return an open stream.

    Parameters
    ----------
    stream : str or open file-like object
    read_or_write : str
        `"read"` or `"write"`, the method to be used on the stream.

    Returns
    -------
    must_close : bool
        Whether `.close` needs to be called on the file object
        by the caller (i.e., it wasn't already open).
    file : file-like object

    Raises
    ------
    TypeError
        If `stream` is neither a string nor has the
        `read_or_write`-indicated method.
    """
    if hasattr(stream, read_or_write):
        return (False, stream)
    try:
        return (True, open(stream, read_or_write[0] + 'b'))
    except TypeError:
        raise TypeError("expected open file or filename")


def _check_name(name):
    """
    Check that a string can be safely be used as the name of an element
    or property in a PLY file.

    Parameters
    ----------
    name : str

    Raises
    ------
    ValueError
        If the check failed.
    """
    for char in name:
        if not 0 <= ord(char) < 128:
            raise ValueError("non-ASCII character in name %r" % name)
        if char.isspace():
            raise ValueError("space character(s) in name %r" % name)


def _check_comments(comments):
    """
    Check that the given comments can be safely used in a PLY header.

    Parameters
    ----------
    comments : list of str

    Raises
    ------
    ValueError
        If the check fails.
    """
    for comment in comments:
        for char in comment:
            if not 0 <= ord(char) < 128:
                raise ValueError("non-ASCII character in comment")
            if char == '\n':
                raise ValueError("embedded newline in comment")


def _check_element_nan(elt):
    """
    Raise `PlyElementParseError` if `elt` contains a NaN value.
    """
    for prop in elt.properties:
        values = elt.data[prop.name]
        if isinstance(prop, PlyListProperty):
            for row, value in enumerate(values):
                if _np.isnan(_np.asarray(value, dtype='f8')).any():
                    raise PlyElementParseError(
                        "NaN is not allowed by the NaN policy",
                        elt, row, prop)
        elif _np.issubdtype(values.dtype, _np.floating):
            nan_mask = _np.isnan(values)
            if nan_mask.any():
                row = int(_np.flatnonzero(nan_mask)[0])
                raise PlyElementParseError(
                    "NaN is not allowed by the NaN policy",
                    elt, row, prop)


def _normalize_nan_policy(nan_policy):
    """
    Validate and normalize a NaN policy name.

    Parameters
    ----------
    nan_policy : str

    Returns
    -------
    str
    """
    aliases = {'raise': 'error', 'propagate': 'preserve'}
    if not isinstance(nan_policy, str):
        raise ValueError("nan policy must be 'preserve' or 'error'")
    nan_policy = aliases.get(nan_policy, nan_policy)
    if nan_policy not in ('preserve', 'error'):
        raise ValueError("nan policy must be 'preserve' or 'error'")
    return nan_policy


def _write_error(message, element, row, prop, value=None):
    return PlyElementWriteError(message, element, row, prop, value)


def _describe_value(value):
    try:
        return repr(value.item())
    except AttributeError:
        return repr(value)


def _checked_object_cast(flat, target, element, row, prop,
                         nan_policy):
    """
    Checked cast for object arrays, whose elements may be Python ints
    too large for any numpy integer type (or arbitrary other objects).
    """
    info = _np.iinfo(target) if target.kind in 'iu' else None
    out = []

    def fail(message, index, value):
        bad_row = index if row is None else row
        raise _write_error(message, element, bad_row, prop, value)

    for index, value in enumerate(flat):
        if isinstance(value, (bool, _np.bool_)):
            out.append(int(value))
            continue
        if isinstance(value, (int, _np.integer)):
            if target.kind == 'f':
                try:
                    converted = float(value)
                except OverflowError:
                    fail("value %s is out of range for float type %s" %
                         (_describe_value(value), target), index, value)
                out.append(converted)
                continue
            if value < info.min or value > info.max:
                fail("value %s is out of range for integer type %s" %
                     (_describe_value(value), target), index, value)
            out.append(int(value))
        elif isinstance(value, (float, _np.floating)):
            if _np.isnan(value):
                if nan_policy == 'error':
                    fail("NaN is not allowed by the NaN policy", index,
                         value)
                out.append(value)
                continue
            if target.kind != 'f' and not _np.isfinite(value):
                fail("value %s cannot be written to integer type %s" %
                     (_describe_value(value), target), index, value)
            if target.kind != 'f' and value != _np.trunc(value):
                fail("value %s is not an integer and cannot be written "
                     "to integer type %s" % (_describe_value(value),
                                             target), index, value)
            if target.kind != 'f' and not info.min <= value <= info.max:
                fail("value %s is out of range for integer type %s" %
                     (_describe_value(value), target), index, value)
            out.append(value)
        else:
            fail("cannot represent %s as %s" %
                 (_describe_value(value), target), index, value)
    with _np.errstate(over='ignore', invalid='ignore'):
        result = _np.asarray(out, dtype=target)
    if target.kind == 'f':
        source = _np.asarray(out, dtype='f8')
        overflow = _np.isfinite(source) & ~_np.isfinite(result)
        if overflow.any():
            bad = int(_np.flatnonzero(overflow)[0])
            fail("value %s is out of range for float type %s" %
                 (_describe_value(flat[bad]), target), bad, flat[bad])
    return result


def _checked_scalar_cast(value, dtype, element, row, prop,
                         nan_policy):
    """
    Cast one value to `dtype`, rejecting anything that would change it.

    Parameters
    ----------
    value
        Value to cast.
    dtype : numpy dtype description
    element : PlyElement
    row : int
    prop : PlyProperty
    nan_policy : {'preserve', 'error'}

    Returns
    -------
    numpy scalar
    """
    try:
        if isinstance(value, (bool, int, float, _np.generic)):
            arr = _np.asarray([value], dtype=object)
        else:
            arr = _np.asarray([value])
    except Exception:
        raise _write_error("cannot represent %s as %s" %
                           (_describe_value(value), _np.dtype(dtype)),
                           element, row, prop, value)
    return _checked_array_cast(arr, dtype, element, row, prop,
                               nan_policy)[0]


def _checked_array_cast(values, dtype, element, row, prop,
                        nan_policy):
    """
    Cast an array to `dtype`, rejecting values that would be changed.

    For floating-point target types, NaNs are written as NaNs or
    rejected according to `nan_policy`.  NaN is never converted to an
    integer.  Integer target types must represent every value exactly;
    otherwise `PlyElementWriteError` identifies the record and property.

    Parameters
    ----------
    values : array-like
    dtype : numpy dtype description
    element : PlyElement
    row : int or None
        Record index, or None if `values` contains one entry per record.
    prop : PlyProperty
    nan_policy : {'preserve', 'error'}

    Returns
    -------
    numpy.ndarray
    """
    target = _np.dtype(dtype)
    arr = _np.asarray(values) if not isinstance(values, (list, tuple)) \
        else _np.asarray(values, dtype=object)
    flat = arr.reshape(-1)

    def fail(message, bad_index=0, bad_value=None):
        bad_row = bad_index if row is None else row
        if bad_value is None and flat.size:
            bad_value = flat[min(bad_index, flat.size - 1)]
        raise _write_error(message, element, bad_row, prop, bad_value)

    if arr.dtype.kind == 'O':
        return _checked_object_cast(flat, target, element, row, prop,
                                    nan_policy)

    if arr.dtype.kind not in 'iubf':
        fail("cannot represent %s as %s" %
             (_describe_value(flat[0]) if flat.size else 'value',
              target))

    if target.kind == 'f':
        if arr.dtype.kind == 'f' and nan_policy == 'error':
            nan_mask = _np.isnan(arr)
            if nan_mask.any():
                bad = int(_np.flatnonzero(nan_mask)[0])
                fail("NaN is not allowed by the NaN policy", bad,
                     flat[bad])
        with _np.errstate(over='ignore', invalid='ignore'):
            out = arr.astype(target)
        if arr.dtype.kind == 'f':
            overflow = _np.isfinite(flat) & ~_np.isfinite(out)
            if overflow.any():
                bad = int(_np.flatnonzero(overflow)[0])
                fail("value %s is out of range for float type %s" %
                     (_describe_value(flat[bad]), target), bad,
                     flat[bad])
            underflow = (flat != 0) & (out == 0)
            if underflow.any():
                bad = int(_np.flatnonzero(underflow)[0])
                fail("value %s underflows float type %s" %
                     (_describe_value(flat[bad]), target), bad,
                     flat[bad])
        return out

    if target.kind != 'i' and target.kind != 'u':
        fail("unsupported target type %s" % target)

    if arr.dtype.kind == 'f':
        nan_mask = _np.isnan(flat)
        if nan_mask.any():
            bad = int(_np.flatnonzero(nan_mask)[0])
            fail("NaN cannot be written to integer type %s" % target,
                 bad, flat[bad])
        inf_mask = _np.isinf(flat)
        if inf_mask.any():
            bad = int(_np.flatnonzero(inf_mask)[0])
            fail("value %s cannot be written to integer type %s" %
                 (_describe_value(flat[bad]), target), bad, flat[bad])
        nonintegral = flat != _np.trunc(flat)
        if nonintegral.any():
            bad = int(_np.flatnonzero(nonintegral)[0])
            fail("value %s is not an integer and cannot be written "
                 "to integer type %s" %
                 (_describe_value(flat[bad]), target), bad, flat[bad])

    info = _np.iinfo(target)
    too_large = flat > info.max
    if arr.dtype.kind == 'u' or arr.dtype.kind == 'b':
        too_small = _np.zeros(flat.shape, dtype=bool)
    else:
        too_small = flat < info.min
    if too_large.any() or too_small.any():
        bad = int(_np.flatnonzero(too_large | too_small)[0])
        fail("value %s is out of range for integer type %s" %
             (_describe_value(flat[bad]), target), bad, flat[bad])

    with _np.errstate(over='ignore', invalid='ignore'):
        return arr.astype(target)


def _format_value(value):
    """
    Render a numpy scalar for ASCII output.

    Floating-point values use the shortest decimal string that reads
    back to the same value of that dtype.  NaNs and infinities are
    rendered as `nan`, `inf`, and `-inf`.
    """
    text = str(value)
    if text.endswith('.0'):
        text = text[:-2]
    return text


def _format_scalar(value, dtype, element, row, prop, nan_policy):
    """
    Convert one value to an ASCII-format PLY field.

    The result is the shortest decimal representation that reads back
    to the same floating-point value.  Integer values are emitted
    exactly, and values that do not fit are rejected.
    """
    cast = _checked_scalar_cast(value, dtype, element, row, prop,
                                nan_policy)
    return _format_value(cast)


def _parse_scalar(text, dtype):
    """
    Parse one ASCII-format PLY field.
    """
    try:
        return _np.dtype(dtype).type(text)
    except OverflowError as error:
        raise ValueError(str(error))


def _read_array(stream, dtype, n):
    """
    Read `n` elements of type `dtype` from an open stream.

    Parameters
    ----------
    stream : readable open binary file
    dtype : dtype description
    n : int

    Returns
    -------
    numpy.ndarray

    Raises
    ------
    StopIteration
        If `n` elements could not be read.
    """
    try:
        size = int(_np.dtype(dtype).itemsize) * int(n)
        return _np.frombuffer(stream.read(size), dtype)
    except Exception:
        raise StopIteration


def _write_array(stream, array):
    """
    Write `numpy` array to a binary file.

    Parameters
    ----------
    stream : writeable open binary file
    array : numpy.ndarray
    """
    stream.write(array.tobytes())


def _can_mmap(stream, mmap_mode='c'):
    """
    Determine if a readable stream can be memory-mapped, using some good
    heuristics.

    Parameters
    ----------
    stream : open binary file
    mmap_mode : {'c', 'r', 'r+'}

    Returns
    -------
    bool
    """
    try:
        pos = stream.tell()
        try:
            _np.memmap(stream, 'u1', mmap_mode)
            stream.seek(pos)
            return True
        except Exception:
            stream.seek(pos)
            return False
    except Exception:
        return False


def _lookup_type(type_str):
    if type_str not in _data_type_reverse:
        try:
            type_str = _data_types[type_str]
        except KeyError:
            raise ValueError("field type %r not in %r" %
                             (type_str, _types_list))

    return _data_type_reverse[type_str]


# Many-many relation
_data_type_relation = [
    ('int8', 'i1'),
    ('char', 'i1'),
    ('uint8', 'u1'),
    ('uchar', 'b1'),
    ('uchar', 'u1'),
    ('int16', 'i2'),
    ('short', 'i2'),
    ('uint16', 'u2'),
    ('ushort', 'u2'),
    ('int32', 'i4'),
    ('int', 'i4'),
    ('uint32', 'u4'),
    ('uint', 'u4'),
    ('float32', 'f4'),
    ('float', 'f4'),
    ('float64', 'f8'),
    ('double', 'f8')
]

_data_types = dict(_data_type_relation)
_data_type_reverse = dict((b, a) for (a, b) in _data_type_relation)

_types_list = []
_types_set = set()
for (_a, _b) in _data_type_relation:
    if _a not in _types_set:
        _types_list.append(_a)
        _types_set.add(_a)
    if _b not in _types_set:
        _types_list.append(_b)
        _types_set.add(_b)


_byte_order_map = {
    'ascii': '=',
    'binary_little_endian': '<',
    'binary_big_endian': '>'
}

_byte_order_reverse = {
    '<': 'binary_little_endian',
    '>': 'binary_big_endian'
}

_native_byte_order = {'little': '<', 'big': '>'}[_byteorder]
