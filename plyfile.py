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
from collections.abc import Mapping as _Mapping

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
                 comments=[], obj_info=[]):
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
        """
        self.byte_order = byte_order
        self.text = text

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
    def read(stream, mmap='c', known_list_len={}, columnar=False):
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
        columnar : bool, optional
            If true, return a `PlyColumnarData` instead of a
            `PlyData` instance.

        Raises
        ------
        PlyParseError
            If the file cannot be parsed for any reason.
        ValueError
            If `stream` is open in text mode but the PLY header
            indicates binary encoding.
        """
        (must_close, stream) = _open_stream(stream, 'read')
        try:
            data = PlyData._parse_header(stream)
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
        finally:
            if must_close:
                stream.close()

        if columnar:
            return data.to_columnar()
        return data

    @classmethod
    def read_columnar(cls, stream, mmap='c', known_list_len={}):
        """
        Read PLY data and return it in column-oriented form.

        Parameters
        ----------
        stream : str or readable open file
        mmap : {'c', 'r', 'r+'} or bool, optional
            Same as for `read`.
        known_list_len : dict, optional
            Same as for `read`.

        Returns
        -------
        PlyColumnarData
        """
        return cls.read(stream, mmap=mmap,
                        known_list_len=known_list_len, columnar=True)

    def write_columnar(self, stream, missing='error'):
        """
        Write this data to a PLY file through the column-oriented form.

        Parameters
        ----------
        stream : str or writeable open file
        missing : {'keep', 'empty', 'error'}, optional
            How to treat missing list values. `PlyData` values that
            originated from PLY files never contain missing values, so
            the strict default does not affect normal round trips.
        """
        self.to_columnar().write(stream, missing=missing)

    def write(self, stream):
        """
        Write PLY data to a writeable file-like object or filename.

        Parameters
        ----------
        stream : str or writeable open file

        Raises
        ------
        ValueError
            If `stream` is open in text mode and the file to be written
            is binary-format.
        """
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
                elt._write(stream, self.text, self.byte_order)
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

    def to_columnar(self, elements=None):
        """
        Export the data as ordered element columns.

        Each element becomes a `PlyElementColumns` mapping. Scalar
        properties are one-dimensional arrays; list properties are
        `PlyListColumn` instances with cumulative offsets, flattened
        values, and (if requested) a validity mask.

        Offsets are stored per list property. They are cumulative
        across all element rows, beginning at 0 and ending with the
        total number of values, so row ``k`` occupies
        ``values[offsets[k]:offsets[k + 1]]``. The offsets array has
        one more entry than there are rows. Empty lists therefore
        appear as two equal consecutive offsets and are never dropped;
        they remain distinct from a missing value (``validity`` false).

        Parameters
        ----------
        elements : iterable of str, optional
            Element names to export, in the given order. Defaults to
            all elements in their existing order.

        Returns
        -------
        PlyColumnarData
            Ordered mapping from element name to
            `PlyElementColumns`. It also carries the file-level
            `text`, `byte_order`, `comments`, and `obj_info`
            metadata needed by `PlyData.from_columnar`.
        """
        if elements is None:
            selected = self.elements
        elif isinstance(elements, str):
            selected = (self[elements],)
        else:
            selected = tuple(self[name] for name in elements)

        result = PlyColumnarData(
            text=self.text, byte_order=self.byte_order,
            comments=self.comments, obj_info=self.obj_info)
        for elt in selected:
            result[elt.name] = elt.to_columnar()
        return result

    def columnar(self, elements=None):
        """
        Alias for `to_columnar`.
        """
        return self.to_columnar(elements)

    @property
    def columns(self):
        """
        Column-oriented view of all elements.

        This is equivalent to `to_columnar()`.
        """
        return self.to_columnar()

    def to_columns(self, elements=None):
        """
        Alias for `to_columnar`.
        """
        return self.to_columnar(elements)

    def as_columnar(self, elements=None):
        """
        Alias for `to_columnar`.
        """
        return self.to_columnar(elements)

    @classmethod
    def from_columnar(cls, elements, text=None, byte_order=None,
                      comments=None, obj_info=None, missing='keep'):
        """
        Build a `PlyData` instance from column-oriented data.

        Parameters
        ----------
        elements : mapping
            Mapping from element name to a column mapping accepted by
            `PlyElement.from_columnar`. A `PlyColumnarData` instance
            is also accepted, in which case its metadata is used
            unless overridden.
        text : bool, optional
            Whether the PLY data will be text. Defaults to the value
            carried by `elements`, or False.
        byte_order : {'<', '>', '='}, optional
            Byte order for binary data. Defaults to the value carried
            by `elements`, or ``'='``.
        comments : iterable of str, optional
            File-level comments. Defaults to the value carried by
            `elements`, or an empty list.
        obj_info : iterable of str, optional
            File-level obj_info lines. Defaults to the value carried
            by `elements`, or an empty list.
        missing : {'keep', 'empty', 'error'}, optional
            How to treat missing list values. See
            `PlyElement.from_columnar`.

        Returns
        -------
        PlyData

        Raises
        ------
        PlyColumnarError
            If the column data is malformed or inconsistent.
        """
        if not isinstance(elements, _Mapping):
            raise TypeError("elements must be a mapping")

        if isinstance(elements, PlyColumnarData):
            if text is None:
                text = elements.text
            if byte_order is None:
                byte_order = elements.byte_order
            if comments is None:
                comments = elements.comments
            if obj_info is None:
                obj_info = elements.obj_info

        if text is None:
            text = False
        if byte_order is None:
            byte_order = '='
        if comments is None:
            comments = []
        if obj_info is None:
            obj_info = []

        result = []
        for (name, columns) in elements.items():
            comments_for_element = []
            if isinstance(columns, PlyElementColumns):
                comments_for_element = columns.comments
            result.append(PlyElement.from_columnar(
                columns, name=name, comments=comments_for_element,
                missing=missing))

        return cls(result, text=text, byte_order=byte_order,
                   comments=comments, obj_info=obj_info)

    @classmethod
    def from_columns(cls, elements, **kwargs):
        """
        Alias for `from_columnar`.
        """
        return cls.from_columnar(elements, **kwargs)

    def __str__(self):
        return self.header

    def __repr__(self):
        return ('PlyData(%r, text=%r, byte_order=%r, '
                'comments=%r, obj_info=%r)' %
                (self.elements, self.text, self.byte_order,
                 self.comments, self.obj_info))


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

    def to_columnar(self):
        """
        Export this element as a mapping of column names to arrays.

        Scalar properties become one-dimensional `numpy` arrays with
        their original data type. List properties become
        `PlyListColumn` instances containing cumulative offsets and a
        flat array of values. There is one offset entry per element
        row (plus a final sentinel), so empty lists are represented by
        two equal consecutive offsets and are not dropped.

        Returns
        -------
        PlyElementColumns
            Mapping from property name to column data. The returned
            mapping also has `name` and `count` attributes.
        """
        columns = PlyElementColumns(self.name, self.count,
                                    self.comments)
        for prop in self.properties:
            values = self._data[prop.name]
            if isinstance(prop, PlyListProperty):
                offsets = _np.empty(self.count + 1, dtype='i8')
                offsets[0] = 0
                validity = _np.ones(self.count, dtype='bool')
                parts = []
                total = 0
                for k in range(self.count):
                    value = values[k]
                    if value is None:
                        validity[k] = False
                    else:
                        part = _np.asarray(value,
                                           dtype=prop.val_dtype).ravel()
                        parts.append(part)
                        total += part.size
                    offsets[k + 1] = total
                if parts:
                    flat = _np.concatenate(parts)
                else:
                    flat = _np.empty(0, dtype=prop.val_dtype)
                columns[prop.name] = PlyListColumn(
                    offsets, flat, validity,
                    len_dtype=prop.len_dtype, val_dtype=prop.val_dtype,
                    name=prop.name)
            else:
                columns[prop.name] = _np.array(values, copy=True)
        return columns

    def columnar(self):
        """
        Alias for `to_columnar`.
        """
        return self.to_columnar()

    @property
    def columns(self):
        """
        Column-oriented view of this element.

        This is equivalent to `to_columnar()`.
        """
        return self.to_columnar()

    def to_columns(self):
        """
        Alias for `to_columnar`.
        """
        return self.to_columnar()

    def as_columnar(self):
        """
        Alias for `to_columnar`.
        """
        return self.to_columnar()

    def list_columns(self):
        """
        Column data for list properties only.

        Returns
        -------
        dict
            Mapping from list property name to `PlyListColumn`, in
            property order.
        """
        return self.to_columnar().list_columns()

    def scalar_columns(self):
        """
        Column data for scalar properties only.

        Returns
        -------
        dict
            Mapping from scalar property name to array, in property
            order.
        """
        return self.to_columnar().scalar_columns()

    @classmethod
    def from_columnar(cls, columns, name=None, count=None,
                      len_types={}, val_types={}, comments=[],
                      properties=None, missing='keep'):
        """
        Reconstruct an element from column-oriented data.

        Parameters
        ----------
        columns : mapping or PlyElementColumns
            Mapping from property name to either a scalar array or a
            list column. A list column may be a `PlyListColumn`, a
            mapping with keys ``"offsets"`` and ``"values"`` (and
            optionally ``"validity"``), or a two-item sequence
            ``(offsets, values)``.
        name : str, optional
            Element name. If omitted, the name of a
            `PlyElementColumns` instance is used.
        count : int, optional
            Number of rows. Inferred from the columns if omitted.
        len_types : dict, optional
            Overrides of list-length types, by property name.
        val_types : dict, optional
            Overrides of list-value types, by property name.
        comments : list of str, optional
            Element comments.
        properties : iterable of PlyProperty, optional
            Explicit property metadata and ordering. By default,
            property metadata and order are taken from `columns`.
        missing : {'keep', 'empty', 'error'}, optional
            How to represent a missing list value (validity false).
            ``'keep'`` stores `None` in the object field, ``'empty'``
            turns it into an empty list, and ``'error'`` raises
            `PlyColumnarError`. Since PLY itself cannot represent
            missing list values, the default keeps the distinction and
            rejects such data only when writing a PLY file.

        Returns
        -------
        PlyElement

        Raises
        ------
        PlyColumnarError
            If the column data is malformed or inconsistent.
        """
        if isinstance(columns, str):
            # Also accept from_columnar(name, columns, ...).
            if not isinstance(name, _Mapping):
                raise TypeError("expected column mapping after name")
            (columns, name) = (name, columns)

        if isinstance(columns, PlyElementColumns):
            if name is None:
                name = columns.name
            if count is None:
                count = columns.count
        if name is None:
            raise TypeError("element name is required")
        if not isinstance(columns, _Mapping):
            raise TypeError("columns must be a mapping")

        missing = _check_missing_mode(missing)
        explicit_properties = properties is not None

        if properties is not None:
            properties = tuple(properties)
            prop_names = [p.name for p in properties]
            for prop in properties:
                if prop.name not in columns:
                    raise PlyColumnarError(
                        "missing column for property %r" % prop.name,
                        element=name, prop=prop.name)
            for prop_name in columns:
                if prop_name not in prop_names:
                    raise PlyColumnarError(
                        "column %r has no matching property" % prop_name,
                        element=name, prop=prop_name)
        else:
            properties = tuple(
                _infer_property(
                    prop_name, column,
                    len_type=len_types.get(prop_name),
                    val_type=val_types.get(prop_name))
                for (prop_name, column) in columns.items())

        resolved = []
        inferred_counts = []
        for prop in properties:
            column = columns[prop.name]
            if isinstance(prop, PlyListProperty):
                list_col = _coerce_list_column(
                    column, count=count, element=name, prop=prop.name,
                    len_dtype=len_types.get(prop.name),
                    val_dtype=val_types.get(prop.name))
                if count is None:
                    count = list_col.count
                inferred_counts.append(list_col.count)
                if list_col.count != count:
                    raise PlyColumnarError(
                        "column has %d rows, expected %d" %
                        (list_col.count, count),
                        element=name, prop=prop.name)
                resolved.append((prop, list_col))
            else:
                array = _np.asarray(column)
                if array.ndim != 1:
                    raise PlyColumnarError(
                        "scalar column must be one-dimensional",
                        element=name, prop=prop.name)
                if count is None:
                    count = len(array)
                inferred_counts.append(len(array))
                if len(array) != count:
                    raise PlyColumnarError(
                        "column has %d rows, expected %d" %
                        (len(array), count),
                        element=name, prop=prop.name)
                resolved.append((prop, array))

        if count is None:
            count = 0
        if inferred_counts and any(n != count for n in inferred_counts):
            raise PlyColumnarError(
                "columns have inconsistent row counts",
                element=name)

        dtype = _np.dtype([(prop.name, prop.dtype()) for prop in properties])
        data = _np.empty(count, dtype=dtype)

        for (prop, column) in resolved:
            if isinstance(prop, PlyListProperty):
                len_dtype = len_types.get(prop.name)
                val_dtype = val_types.get(prop.name)
                if len_dtype is None and not explicit_properties:
                    len_dtype = column.len_dtype
                if val_dtype is None and not explicit_properties:
                    val_dtype = column.val_dtype
                if len_dtype is None:
                    len_dtype = prop.len_dtype
                if val_dtype is None:
                    val_dtype = prop.val_dtype
                prop.len_dtype = len_dtype
                prop.val_dtype = val_dtype
                max_len = int(column.lengths.max()) if count else 0
                _check_list_length(max_len, prop.len_dtype,
                                   element=name, prop=prop.name)
                field = data[prop.name]
                for k in range(count):
                    if not column.validity[k]:
                        if missing == 'error':
                            raise PlyColumnarError(
                                "missing list value", element=name,
                                prop=prop.name, row=k)
                        elif missing == 'empty':
                            field[k] = _np.empty(0, dtype=prop.val_dtype)
                        else:
                            field[k] = None
                    else:
                        start = int(column.offsets[k])
                        end = int(column.offsets[k + 1])
                        field[k] = _np.array(
                            column.values[start:end],
                            dtype=prop.val_dtype, copy=True)
            else:
                data[prop.name] = column.astype(prop.dtype(), copy=False)

        elt = cls(name, properties, count, comments)
        elt.data = data
        return elt

    @classmethod
    def from_columns(cls, columns, **kwargs):
        """
        Alias for `from_columnar`.
        """
        return cls.from_columnar(columns, **kwargs)

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
            if isinstance(data, _Mapping):
                return PlyElement.from_columnar(
                    data, name=name, comments=comments,
                    len_types=len_types, val_types=val_types)
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

    def _write(self, stream, text, byte_order):
        """
        Write the data to a PLY file.

        Parameters
        ----------
        stream : writeable open file
        text : bool
        byte_order : {'<', '>', '='}
        """
        if text:
            self._write_txt(stream)
        else:
            if self._have_list:
                # There are list properties, so serialization is
                # slightly complicated.
                self._write_bin(stream, byte_order)
            else:
                # no list properties, so serialization is
                # straightforward.
                stream.write(self.data.astype(self.dtype(byte_order),
                                              copy=False).data)

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

    def _write_txt(self, stream):
        """
        Save a PLY element to an ASCII-format PLY file.  The element may
        contain list properties.

        Parameters
        ----------
        stream : writeable open file
        """
        for (k, rec) in enumerate(self.data):
            fields = []
            for prop in self.properties:
                value = rec[prop.name]
                if isinstance(prop, PlyListProperty) and value is None:
                    raise PlyColumnarError(
                        "missing list value cannot be written to PLY",
                        element=self.name, prop=prop.name, row=k)
                fields.extend(prop._to_fields(value))

            _np.savetxt(stream, [fields], '%.18g', newline='\n')

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

    def _write_bin(self, stream, byte_order):
        """
        Save a PLY element to a binary PLY file.  The element may
        contain list properties.

        Parameters
        ----------
        stream : writeable open file
        byte_order : {'<', '>', '='}
        """
        for (k, rec) in enumerate(self.data):
            for prop in self.properties:
                value = rec[prop.name]
                if isinstance(prop, PlyListProperty) and value is None:
                    raise PlyColumnarError(
                        "missing list value cannot be written to PLY",
                        element=self.name, prop=prop.name, row=k)
                prop._write_bin(value, stream, byte_order)

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
        return _np.dtype(self.dtype()).type(next(fields))

    def _to_fields(self, data):
        """
        Parameters
        ----------
        data
            Property data to encode.

        Yields
        ------
        encoded_data
            Data with type consistent with `self.val_dtype`.
        """
        yield _np.dtype(self.dtype()).type(data)

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

    def _write_bin(self, data, stream, byte_order):
        """
        Write data to a binary stream.

        Parameters
        ----------
        data
            Property data to encode.
        stream : writeable open binary file
        byte_order : {'<', '>', '='}
        """
        _write_array(stream, _np.dtype(self.dtype(byte_order)).type(data))

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

        n = int(_np.dtype(len_t).type(next(fields)))

        if n == 0:
            return _np.empty(0, dtype=val_t)

        data = _np.loadtxt(list(_islice(fields, n)), val_t, ndmin=1)
        if len(data) < n:
            raise StopIteration

        return data

    def _to_fields(self, data):
        """
        Return generator over the (numerical) PLY representation of the
        list data (length followed by actual data).

        Parameters
        ----------
        data : numpy.ndarray
            Property data to encode.

        Yields
        ------
        Length followed by each list element.
        """
        (len_t, val_t) = self.list_dtype()

        data = _np.asarray(data, dtype=val_t).ravel()

        yield _np.dtype(len_t).type(data.size)
        for x in data:
            yield x

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

    def _write_bin(self, data, stream, byte_order):
        """
        Write data to a binary stream.

        Parameters
        ----------
        data : numpy.ndarray
            Data to encode.
        stream : writeable open binary file
        byte_order : {'<', '>', '='}
        """
        (len_t, val_t) = self.list_dtype(byte_order)

        data = _np.asarray(data, dtype=val_t).ravel()

        _write_array(stream, _np.array(data.size, dtype=len_t))
        _write_array(stream, data)

    def __str__(self):
        len_str = _data_type_reverse[self.len_dtype]
        val_str = _data_type_reverse[self.val_dtype]
        return 'property list %s %s %s' % (len_str, val_str, self.name)

    def __repr__(self):
        return ('PlyListProperty(%r, %r, %r)' %
                (self.name,
                 _lookup_type(self.len_dtype),
                 _lookup_type(self.val_dtype)))


class PlyListColumn(object):
    """
    Column-oriented representation of one PLY list property.

    Attributes
    ----------
    offsets : numpy.ndarray
        Cumulative offsets, with one entry per element row and a final
        sentinel such that ``len(offsets) == row_count + 1``. Row
        ``k`` occupies ``values[offsets[k]:offsets[k + 1]]``. An empty
        list has equal consecutive offsets.
    values : numpy.ndarray
        Flattened list values for all rows.
    validity : numpy.ndarray
        Boolean mask with one entry per row. ``False`` marks a missing
        list value (as opposed to an empty list, which is valid and
        has zero length).
    len_dtype : str
        `numpy` dtype used to encode the list length in the PLY file.
    val_dtype : str
        `numpy` dtype of the list values. If not given, it is inferred
        from `values`, defaulting to ``'i4'`` for an empty array.
    name : str or None
        Property name.
    """

    def __init__(self, offsets, values, validity=None, len_dtype='u1',
                 val_dtype=None, name=None):
        offsets = _np.asarray(offsets)
        if offsets.ndim != 1:
            raise PlyColumnarError("offsets must be one-dimensional",
                                   element=None, prop=name)
        if offsets.size == 0:
            raise PlyColumnarError("offsets must not be empty",
                                   element=None, prop=name)
        if offsets.dtype.kind not in 'iu':
            raise PlyColumnarError("offsets must have an integer type",
                                   element=None, prop=name)

        values = _np.asarray(values)
        if values.ndim != 1:
            raise PlyColumnarError("values must be one-dimensional",
                                   element=None, prop=name)

        if offsets[0] != 0:
            raise PlyColumnarError("offsets must start at 0",
                                   element=None, prop=name)
        if (offsets < 0).any():
            raise PlyColumnarError("offsets must be non-negative",
                                   element=None, prop=name)
        if (offsets[1:] < offsets[:-1]).any():
            row = int(_np.flatnonzero(offsets[1:] < offsets[:-1])[0])
            raise PlyColumnarError("offsets must be non-decreasing",
                                   element=None, prop=name, row=row)
        if offsets[-1] != len(values):
            raise PlyColumnarError(
                "final offset %d does not match values length %d" %
                (int(offsets[-1]), len(values)),
                element=None, prop=name)

        self.name = name
        self.offsets = _np.array(offsets, dtype='i8', copy=True)
        self.values = values
        self.len_dtype = _data_types[_normalize_type(len_dtype)]
        if val_dtype is None:
            if len(values):
                val_dtype = _ply_type_for_array(values, prop=name)
            else:
                val_dtype = 'i4'
        self.val_dtype = _data_types[_normalize_type(val_dtype)]

        if validity is None:
            validity = _np.ones(self.count, dtype='bool')
        else:
            validity = _np.asarray(validity)
            if validity.ndim != 1:
                raise PlyColumnarError(
                    "validity must be a one-dimensional array",
                    element=None, prop=name)
            if len(validity) != self.count:
                raise PlyColumnarError(
                    "validity has %d rows, expected %d" %
                    (len(validity), self.count),
                    element=None, prop=name)
            validity = validity.astype('bool', copy=True)
        self.validity = validity

        missing_rows = _np.flatnonzero(~self.validity)
        if missing_rows.size:
            lengths = self.lengths
            bad = missing_rows[lengths[missing_rows] != 0]
            if bad.size:
                raise PlyColumnarError(
                    "missing values must have zero length",
                    element=None, prop=name, row=int(bad[0]))

        self._check_lengths()

    @property
    def count(self):
        """
        Number of element rows, including empty and missing rows.
        """
        return len(self.offsets) - 1

    @property
    def lengths(self):
        """
        Number of values in each row.
        """
        return _np.diff(self.offsets)

    @property
    def starts(self):
        """
        Start offset of each row (one entry per element row).
        """
        return self.offsets[:-1]

    @property
    def ends(self):
        """
        End offset of each row (one entry per element row).
        """
        return self.offsets[1:]

    @property
    def row_offsets(self):
        """
        Alias for `starts`.
        """
        return self.starts

    @property
    def value_offsets(self):
        """
        Alias for `offsets`.
        """
        return self.offsets

    @property
    def flat_values(self):
        """
        Alias for `values`.
        """
        return self.values

    @property
    def null_mask(self):
        """
        Boolean mask that is true for missing list values.
        """
        return ~self.validity

    @property
    def is_valid(self):
        """
        Alias for `validity`.
        """
        return self.validity

    @property
    def len_type(self):
        """
        PLY name of the list-length type (e.g. ``'uchar'``).
        """
        return _data_type_reverse[self.len_dtype]

    @property
    def value_type(self):
        """
        PLY name of the list-value type (e.g. ``'int'``).
        """
        return _data_type_reverse[self.val_dtype]

    def __len__(self):
        return self.count

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in ('offsets', 'values', 'validity',
                       'len_dtype', 'val_dtype', 'name'):
                return getattr(self, key)
            raise KeyError(key)
        if isinstance(key, slice):
            return list(self)[key]
        k = int(key)
        if k < 0:
            k += self.count
        if not 0 <= k < self.count:
            raise IndexError("list column index out of range")
        if not self.validity[k]:
            return None
        return self.values[self.offsets[k]:self.offsets[k + 1]]

    def __iter__(self):
        for k in range(self.count):
            yield self[k]

    def keys(self):
        """
        Names of the component arrays.
        """
        return ('offsets', 'values', 'validity')

    def get(self, key, default=None):
        """
        Look up a component array by name.
        """
        try:
            return self[key]
        except (KeyError, TypeError):
            return default

    def items(self):
        """
        Iterate over ``(name, array)`` component pairs.
        """
        for key in self.keys():
            yield (key, self[key])

    def to_dict(self):
        """
        Return offsets, values, and validity as a plain mapping.
        """
        result = {key: self[key] for key in self.keys()}
        result['len_dtype'] = self.len_dtype
        result['val_dtype'] = self.val_dtype
        return result

    def copy(self):
        """
        Return a deep copy of this column.
        """
        return PlyListColumn(self.offsets.copy(), self.values.copy(),
                             self.validity.copy(), self.len_dtype,
                             self.val_dtype, self.name)

    def _check_lengths(self):
        if self.count == 0:
            return
        max_len = int(self.lengths.max())
        _check_list_length(max_len, self.len_dtype,
                           element=None, prop=self.name)

    def __repr__(self):
        return ('PlyListColumn(offsets=%r, values=%r, validity=%r, '
                'len_dtype=%r, val_dtype=%r, name=%r)' %
                (self.offsets, self.values, self.validity,
                 self.len_type, self.value_type, self.name))

    def __eq__(self, other):
        if not isinstance(other, PlyListColumn):
            return NotImplemented
        return (_np.array_equal(self.offsets, other.offsets) and
                _np.array_equal(self.values, other.values) and
                _np.array_equal(self.validity, other.validity) and
                self.len_dtype == other.len_dtype and
                self.val_dtype == other.val_dtype)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


class PlyElementColumns(dict):
    """
    Ordered mapping of property name to column data for one element.

    Insertion order is the property order. Scalar columns are
    one-dimensional arrays; list columns are `PlyListColumn`
    instances.

    Attributes
    ----------
    name : str or None
    count : int
    comments : list of str
    """

    def __init__(self, name=None, count=None, comments=[]):
        dict.__init__(self)
        if isinstance(name, _Mapping):
            # Also accept PlyElementColumns(mapping, ...).
            mapping = name
            name = mapping.name if isinstance(
                mapping, PlyElementColumns) else None
            if isinstance(mapping, PlyElementColumns):
                count = mapping.count
                comments = mapping.comments
            self.update(mapping)
        self.name = name
        self.count = count
        self.comments = list(comments)

    def add(self, name, column):
        """
        Add or replace a property column.
        """
        self[name] = column
        return self

    def list_columns(self):
        """
        Mapping of list property names to `PlyListColumn` instances.
        """
        return {name: column for (name, column) in self.items()
                if isinstance(column, PlyListColumn)}

    def scalar_columns(self):
        """
        Mapping of scalar property names to arrays.
        """
        return {name: column for (name, column) in self.items()
                if not isinstance(column, PlyListColumn)}

    def to_element(self, len_types={}, val_types={}, missing='keep',
                   name=None, count=None, properties=None):
        """
        Reconstruct a `PlyElement` from these columns.
        """
        return PlyElement.from_columnar(
            self, name=self.name if name is None else name,
            count=self.count if count is None else count,
            comments=self.comments, len_types=len_types,
            val_types=val_types, missing=missing,
            properties=properties)

    def element(self, **kwargs):
        """
        Alias for `to_element`.
        """
        return self.to_element(**kwargs)

    def __repr__(self):
        return ('PlyElementColumns(%r, name=%r, count=%r, '
                'comments=%r)' % (dict(self), self.name, self.count,
                                  self.comments))


class PlyColumnarData(dict):
    """
    Ordered mapping of element name to `PlyElementColumns`.

    Attributes
    ----------
    text : bool
    byte_order : {'<', '>', '='}
    comments : list of str
    obj_info : list of str
    """

    def __init__(self, elements=None, text=False, byte_order='=',
                 comments=[], obj_info=[]):
        dict.__init__(self)
        if elements is not None:
            if isinstance(elements, PlyColumnarData):
                text = elements.text
                byte_order = elements.byte_order
                comments = elements.comments
                obj_info = elements.obj_info
            self.update(elements)
        self.text = text
        self.byte_order = byte_order
        self.comments = list(comments)
        self.obj_info = list(obj_info)

    def to_plydata(self, **kwargs):
        """
        Reconstruct a `PlyData` instance from this columnar data.

        Keyword arguments override the stored metadata.
        """
        params = {'text': self.text, 'byte_order': self.byte_order,
                  'comments': self.comments,
                  'obj_info': self.obj_info}
        params.update(kwargs)
        return PlyData.from_columnar(self, **params)

    def write(self, stream, missing='error'):
        """
        Write this columnar data to a PLY file.

        Parameters
        ----------
        stream : str or writeable open file
        missing : {'keep', 'empty', 'error'}, optional
            How to treat missing list values. See
            `PlyElement.from_columnar`.
        """
        self.to_plydata(missing=missing).write(stream)

    def plydata(self, **kwargs):
        """
        Alias for `to_plydata`.
        """
        return self.to_plydata(**kwargs)

    @property
    def elements(self):
        """
        Element names in order.
        """
        return tuple(self.keys())

    def __repr__(self):
        return ('PlyColumnarData(%r, text=%r, byte_order=%r, '
                'comments=%r, obj_info=%r)' %
                (dict(self), self.text, self.byte_order,
                 self.comments, self.obj_info))


class PlyParseError(Exception):
    """
    Base class for PLY parsing errors.
    """

    pass


class PlyColumnarError(ValueError):
    """
    Raised when column-oriented data is malformed or inconsistent.

    Attributes
    ----------
    message : str
    element : str or None
    prop : str or None
    row : int or None
    """

    def __init__(self, message, element=None, prop=None, row=None):
        self.message = message
        self.element = element
        self.prop = prop
        self.row = row

        s = ''
        if self.element is not None:
            s += 'element %r: ' % self.element
        if self.prop is not None:
            s += 'property %r: ' % self.prop
        if self.row is not None:
            s += 'row %d: ' % self.row
        s += self.message

        ValueError.__init__(self, s)

    def __repr__(self):
        return ('%s(%r, element=%r, prop=%r, row=%r)' %
                (self.__class__.__name__, self.message, self.element,
                 self.prop, self.row))


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


def read_ply_columnar(stream, mmap='c', known_list_len={}):
    """
    Read a PLY file and return column-oriented data.

    This is a module-level shortcut for `PlyData.read_columnar`.

    Parameters
    ----------
    stream : str or readable open file
    mmap : {'c', 'r', 'r+'} or bool, optional
    known_list_len : dict, optional

    Returns
    -------
    PlyColumnarData
    """
    return PlyData.read_columnar(stream, mmap=mmap,
                                 known_list_len=known_list_len)


def write_ply_columnar(stream, elements, missing='error'):
    """
    Write column-oriented data to a PLY file.

    This is a module-level shortcut for `PlyColumnarData.write`.

    Parameters
    ----------
    stream : str or writeable open file
    elements : mapping or PlyColumnarData
    missing : {'keep', 'empty', 'error'}, optional
    """
    if not isinstance(elements, PlyColumnarData):
        elements = PlyColumnarData(elements)
    elements.write(stream, missing=missing)


def to_columnar(data, elements=None):
    """
    Convert a `PlyData` instance to `PlyColumnarData`.

    Parameters
    ----------
    data : PlyData
    elements : iterable of str, optional
        Element names to export, in order.

    Returns
    -------
    PlyColumnarData
    """
    if not isinstance(data, PlyData):
        raise TypeError("expected a PlyData instance")
    return data.to_columnar(elements)


def from_columnar(elements, **kwargs):
    """
    Convert column-oriented data to a `PlyData` instance.

    Parameters
    ----------
    elements : mapping or PlyColumnarData
    **kwargs
        Passed to `PlyData.from_columnar`.

    Returns
    -------
    PlyData
    """
    if isinstance(elements, PlyColumnarData):
        return elements.to_plydata(**kwargs)
    return PlyData.from_columnar(elements, **kwargs)


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


def _check_missing_mode(missing):
    if missing not in ('keep', 'empty', 'error'):
        raise ValueError("missing must be 'keep', 'empty', or 'error'")
    return missing


def _normalize_type(type_str):
    """
    Normalize a PLY or `numpy` type description to a PLY type name.
    """
    if isinstance(type_str, _np.dtype):
        type_str = type_str.str
    type_str = str(type_str)
    if type_str and type_str[0] in '<>=|':
        type_str = type_str[1:]
    return _lookup_type(type_str)


def _check_list_length(length, len_dtype, element=None, prop=None):
    """
    Ensure a list length is representable by the PLY length type.
    """
    if length < 0:
        raise PlyColumnarError("list length must be non-negative",
                               element=element, prop=prop)
    try:
        info = _np.iinfo(len_dtype)
    except ValueError:
        raise PlyColumnarError(
            "list length type %r is not an integer type" %
            _lookup_type(len_dtype), element=element, prop=prop)
    if length > info.max:
        raise PlyColumnarError(
            "list length %d exceeds maximum %d for length type %r" %
            (length, int(info.max), _lookup_type(len_dtype)),
            element=element, prop=prop)


def _infer_property(name, column, len_type=None, val_type=None):
    """
    Infer `PlyProperty` metadata from one column.

    Explicit `len_type` and `val_type` overrides take precedence over
    metadata carried by the column data.
    """
    if len_type is not None or val_type is not None:
        coerced = _coerce_list_column(column, element=None, prop=name,
                                      len_dtype=len_type,
                                      val_dtype=val_type)
        return PlyListProperty(name, coerced.len_dtype,
                               coerced.val_dtype)

    if isinstance(column, PlyListColumn):
        return PlyListProperty(name, column.len_dtype, column.val_dtype)

    if isinstance(column, _Mapping) or (
            hasattr(column, 'offsets') and hasattr(column, 'values')):
        coerced = _coerce_list_column(column, element=None, prop=name)
        return PlyListProperty(name, coerced.len_dtype,
                               coerced.val_dtype)

    if isinstance(column, tuple) and 2 <= len(column) <= 5:
        coerced = _coerce_list_column(column, element=None, prop=name)
        return PlyListProperty(name, coerced.len_dtype,
                               coerced.val_dtype)

    if _is_nested_sequence(column):
        coerced = _list_column_from_nested(column)
        return PlyListProperty(name, coerced.len_dtype,
                               coerced.val_dtype)

    array = _np.asarray(column)
    if array.ndim == 2:
        coerced = _coerce_list_column(array, element=None, prop=name)
        return PlyListProperty(name, coerced.len_dtype,
                               coerced.val_dtype)
    if array.ndim != 1:
        raise PlyColumnarError(
            "scalar column must be one-dimensional", prop=name)
    if array.dtype.kind == 'O':
        return PlyListProperty(name, 'u1', _infer_object_val_dtype(array))
    return PlyProperty(name, _ply_type_for_array(array, prop=name))


def _is_nested_sequence(column):
    """
    Determine whether a column is a sequence of per-row sequences.
    """
    if isinstance(column, _np.ndarray):
        return column.ndim == 2
    if isinstance(column, (str, bytes)):
        return False
    if not isinstance(column, (list, tuple)) or not column:
        return False
    return all(value is None or _is_sequence(value) for value in column)


def _is_sequence(value):
    if isinstance(value, (str, bytes)):
        return False
    if isinstance(value, _np.ndarray):
        return value.ndim <= 1
    return isinstance(value, (list, tuple))


def _list_column_from_nested(values, len_dtype='u1'):
    """
    Build a `PlyListColumn` from a sequence of per-row sequences.
    """
    values = list(values)
    validity = _np.ones(len(values), dtype='bool')
    offsets = _np.empty(len(values) + 1, dtype='i8')
    offsets[0] = 0
    parts = []
    total = 0
    for k, value in enumerate(values):
        if value is None:
            validity[k] = False
            part = _np.empty(0, dtype='i4')
        else:
            part = _np.asarray(value).ravel()
            if part.dtype.kind not in 'iuf':
                raise PlyColumnarError(
                    "unsupported list value dtype %r" % part.dtype)
        parts.append(part)
        total += part.size
        offsets[k + 1] = total
    if parts:
        flat = _np.concatenate(parts)
    else:
        flat = _np.empty(0, dtype='i4')
    if flat.size:
        val_type = _ply_type_for_array(flat)
    else:
        val_type = 'i4'
    return PlyListColumn(offsets, flat, validity, len_dtype=len_dtype,
                         val_dtype=val_type)


def _ply_type_for_array(array, prop=None):
    """
    Map a numeric `numpy` array to the closest PLY value type.
    """
    array = _np.asarray(array)
    kind = array.dtype.kind
    itemsize = array.dtype.itemsize

    if kind == 'b':
        return 'u1'
    if kind == 'f':
        return 'f4' if itemsize <= 4 else 'f8'
    if kind in 'iu':
        if kind == 'i':
            minimum = -(2 ** 31)
            maximum = 2 ** 31 - 1
            if itemsize <= 4:
                return _data_type_reverse['i%d' % itemsize]
            if array.size:
                (low, high) = (int(array.min()), int(array.max()))
                if low < minimum or high > maximum:
                    raise PlyColumnarError(
                        "integer values exceed the range of a PLY "
                        "32-bit integer", prop=prop)
            return 'i4'
        maximum = 2 ** 32 - 1
        if itemsize <= 4:
            return _data_type_reverse['u%d' % itemsize]
        if array.size and int(array.max()) > maximum:
            raise PlyColumnarError(
                "unsigned integer values exceed the range of a PLY "
                "32-bit unsigned integer", prop=prop)
        return 'u4'
    raise PlyColumnarError("unsupported column dtype %r" % array.dtype,
                           prop=prop)


def _infer_object_val_dtype(values):
    for value in values:
        if value is None:
            continue
        array = _np.asarray(value)
        if array.size == 0:
            continue
        return _ply_type_for_array(array)
    return 'i4'


def _list_column_from_object_array(values):
    """
    Turn an object array of sequences into a `PlyListColumn`.

    `None` marks a missing list, whereas an empty sequence marks a
    valid empty list.
    """
    values = _np.asarray(values)
    validity = _np.ones(len(values), dtype='bool')
    offsets = _np.empty(len(values) + 1, dtype='i8')
    offsets[0] = 0
    val_dtype = _infer_object_val_dtype(values)
    parts = []
    total = 0
    for k in range(len(values)):
        value = values[k]
        if value is None:
            validity[k] = False
        else:
            part = _np.asarray(value).ravel()
            if part.dtype.kind in 'iuf':
                part = part.astype(val_dtype, copy=False)
            else:
                part = part.astype(val_dtype)
            parts.append(part)
            total += part.size
        offsets[k + 1] = total
    if parts:
        flat = _np.concatenate(parts)
    else:
        flat = _np.empty(0, dtype=val_dtype)
    return PlyListColumn(offsets, flat, validity,
                         val_dtype=val_dtype)


def _coerce_list_column(column, count=None, element=None, prop=None,
                        len_dtype=None, val_dtype=None):
    """
    Normalize user-supplied list column data to `PlyListColumn`.

    `len_dtype` and `val_dtype`, when given, override any type
    metadata carried by the column data.
    """
    if isinstance(column, PlyListColumn):
        offsets = column.offsets
        values = column.values
        validity = column.validity
        source_len_dtype = column.len_dtype
        source_val_dtype = column.val_dtype
        if len_dtype is not None or val_dtype is not None:
            return _apply_list_column_overrides(
                column, count=count, element=element, prop=prop,
                len_dtype=len_dtype, val_dtype=val_dtype)
    elif hasattr(column, 'offsets') and hasattr(column, 'values'):
        offsets = column.offsets
        values = column.values
        validity = getattr(column, 'validity', None)
        source_len_dtype = getattr(column, 'len_dtype', None)
        source_val_dtype = getattr(column, 'val_dtype', None)
    elif isinstance(column, _Mapping):
        offsets = _first_present(
            column, ('offsets', 'value_offsets', 'list_offsets',
                     'row_offsets'))
        if offsets is None:
            raise PlyColumnarError(
                "list column is missing 'offsets'",
                element=element, prop=prop)
        values = _first_present(
            column, ('values', 'flat_values', 'list_values'))
        if values is None:
            values = _np.empty(0, dtype='i4')
        validity = column.get('validity')
        if validity is None:
            nulls = column.get('null_mask')
            if nulls is not None:
                validity = ~_np.asarray(nulls, dtype='bool')
        source_len_dtype = column.get('len_dtype')
        source_val_dtype = column.get('val_dtype')
    elif isinstance(column, tuple):
        # A tuple is always the explicit (offsets, values[, ...])
        # form, never a sequence of rows.
        if len(column) < 2 or len(column) > 5:
            raise PlyColumnarError(
                "list column must be (offsets, values[, validity[, "
                "len_dtype[, val_dtype]]])",
                element=element, prop=prop)
        offsets = column[0]
        values = column[1]
        validity = column[2] if len(column) > 2 else None
        source_len_dtype = column[3] if len(column) > 3 else None
        source_val_dtype = column[4] if len(column) > 4 else None
    elif _is_nested_sequence(column):
        if (isinstance(column, _np.ndarray) and column.ndim == 2 and
                column.dtype.kind != 'O'):
            row_length = column.shape[1]
            offsets = _np.arange(0, column.size + 1, row_length,
                                 dtype='i8')
            values = column.ravel()
            validity = None
            source_len_dtype = None
            source_val_dtype = None
            if values.dtype.kind in 'iuf':
                source_val_dtype = _ply_type_for_array(values,
                                                       prop=prop)
        else:
            rows = list(column)
            if isinstance(column, _np.ndarray):
                rows = [row.tolist() for row in rows]
            result = _list_column_from_nested(
                rows, len_dtype=len_dtype if len_dtype else 'u1')
            return _apply_list_column_overrides(
                result, count=count, element=element, prop=prop,
                len_dtype=None, val_dtype=val_dtype)
    else:
        array = _np.asarray(column)
        if array.ndim != 1:
            raise PlyColumnarError(
                "list column must be one-dimensional",
                element=element, prop=prop)
        if array.dtype.kind != 'O':
            raise PlyColumnarError(
                "list column requires offsets and values",
                element=element, prop=prop)
        result = _list_column_from_object_array(array)
        return _apply_list_column_overrides(
            result, count=count, element=element, prop=prop,
            len_dtype=len_dtype, val_dtype=val_dtype)

    offsets = _np.asarray(offsets)
    if offsets.size == 0 and count in (None, 0):
        offsets = _np.zeros(1, dtype='i8')
        count = 0
    if offsets.ndim != 1 or offsets.dtype.kind not in 'iu':
        raise PlyColumnarError(
            "offsets must be a one-dimensional integer array",
            element=element, prop=prop)
    if offsets.size == 0:
        raise PlyColumnarError("offsets must not be empty",
                               element=element, prop=prop)

    values = _np.asarray(values)
    if values.ndim != 1:
        raise PlyColumnarError(
            "values must be one-dimensional", element=element, prop=prop)
    if values.dtype.kind == 'O':
        raise PlyColumnarError(
            "values must be a numeric array", element=element, prop=prop)

    if count is not None and len(offsets) == count:
        # Accept start offsets without a final sentinel.
        offsets = _np.append(offsets, len(values))
    elif (count is None and len(offsets) == len(values) and
          offsets[-1] != len(values)):
        # One offset per value is the Arrow-style "starts" form.
        offsets = _np.append(offsets, len(values))

    _check_offsets(offsets, len(values), element=element, prop=prop)
    if count is None:
        count = len(offsets) - 1
    elif len(offsets) != count + 1:
        raise PlyColumnarError(
            "offsets have %d entries, expected %d" %
            (len(offsets), count + 1), element=element, prop=prop)

    if len_dtype is None:
        len_dtype = source_len_dtype if source_len_dtype else 'u1'
    if val_dtype is None:
        val_dtype = (source_val_dtype if source_val_dtype else
                     _ply_type_for_array(values, prop=prop))
    result = PlyListColumn(offsets, values, validity,
                           len_dtype=len_dtype, val_dtype=val_dtype,
                           name=prop)
    if count is not None and result.count != count:
        raise PlyColumnarError(
            "column has %d rows, expected %d" %
            (result.count, count), element=element, prop=prop)
    return result


def _apply_list_column_overrides(column, count=None, element=None,
                                 prop=None, len_dtype=None,
                                 val_dtype=None):
    """
    Apply explicit type overrides to an already-built list column.
    """
    result = PlyListColumn(
        column.offsets, column.values, column.validity,
        len_dtype=len_dtype if len_dtype is not None else column.len_dtype,
        val_dtype=val_dtype if val_dtype is not None else column.val_dtype,
        name=prop)
    if count is not None and result.count != count:
        raise PlyColumnarError(
            "column has %d rows, expected %d" %
            (result.count, count), element=element, prop=prop)
    return result


def _first_present(mapping, names):
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _check_offsets(offsets, value_count, element=None, prop=None):
    """
    Validate a complete cumulative offsets array.
    """
    if offsets[0] != 0:
        raise PlyColumnarError("offsets must start at 0",
                               element=element, prop=prop)
    if (offsets < 0).any():
        raise PlyColumnarError("offsets must be non-negative",
                               element=element, prop=prop)
    if (offsets[1:] < offsets[:-1]).any():
        row = int(_np.flatnonzero(offsets[1:] < offsets[:-1])[0])
        raise PlyColumnarError("offsets must be non-decreasing",
                               element=element, prop=prop, row=row)
    if offsets[-1] != value_count:
        raise PlyColumnarError(
            "final offset %d does not match values length %d" %
            (int(offsets[-1]), value_count),
            element=element, prop=prop)


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
