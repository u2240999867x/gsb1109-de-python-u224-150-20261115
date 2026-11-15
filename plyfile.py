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
    def read(stream, mmap='c', known_list_len={}):
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

        return data

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

    def __str__(self):
        return self.header

    def __repr__(self):
        return ('PlyData(%r, text=%r, byte_order=%r, '
                'comments=%r, obj_info=%r)' %
                (self.elements, self.text, self.byte_order,
                 self.comments, self.obj_info))

    def clip(self, bbox, vertex_element='vertex', face_element='face',
             index_property=None, discrete_properties=None,
             discrete_rule='first'):
        """
        Clip the mesh against an axis-aligned bounding box.

        The portion of the mesh intersecting the closed box is kept.
        Original edges crossing the box are split at their
        intersection points.  Every output vertex is a convex
        combination of original input vertices, recorded in the
        returned `ClipResult`: an untouched vertex has a single
        support, an edge intersection has the two edge endpoints as
        supports with weights giving the interpolation position, and
        a box-edge or box-corner intersection may combine several
        vertices of the originating face.  Floating-point vertex
        properties are linearly interpolated at intersections;
        discrete (integer, boolean, or object) vertex properties are
        not interpolated and are selected with `discrete_rule`.
        Faces with all vertices outside the box but whose edges still
        pass through it are retained.

        Parameters
        ----------
        bbox : array-like, shape (3, 2)
            The lower and upper bounds on the x, y, and z axes, in
            that order.  Bounds are inclusive.
        vertex_element : str, optional
            Name of the element holding the vertices (default
            `'vertex'`).  It must contain `'x'`, `'y'`, and `'z'`
            properties.
        face_element : str, optional
            Name of the element holding the faces (default `'face'`).
        index_property : str, optional
            Name of the face list property holding vertex indices.
            If omitted, the face element's only list property is
            used.
        discrete_properties : iterable of str, optional
            Vertex property names that must not be interpolated.  If
            omitted, every integer, boolean, or object vertex
            property (other than `'x'`, `'y'`, `'z'`) is treated as
            discrete; floating-point properties are interpolated.
        discrete_rule : {'first', 'last', 'majority'}, optional
            How to assign a discrete property at an intersection of an
            edge whose endpoints carry different labels.  `'first'`
            takes the start vertex's label, `'last'` the end vertex's
            label, and `'majority'` takes the more frequent label
            among the edge endpoints (for two endpoints this means the
            start vertex's label when they differ).

        Returns
        -------
        ClipResult

        Raises
        ------
        PlyBBoxClipError
            If the required elements or properties are missing, an
            index is out of range, or an argument is invalid.
        """
        clipper = _PlyMeshClipper(
            self, bbox, vertex_element, face_element,
            index_property, discrete_properties, discrete_rule)
        return clipper.run()


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
        for rec in self.data:
            fields = []
            for prop in self.properties:
                fields.extend(prop._to_fields(rec[prop.name]))

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
        for rec in self.data:
            for prop in self.properties:
                prop._write_bin(rec[prop.name], stream, byte_order)

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


class PlyBBoxClipError(ValueError):
    """
    Raised when a PLY mesh cannot be clipped against a bounding box,
    for example because the required elements or properties are
    missing or because an argument is invalid.
    """

    pass


class ClipResult(object):
    """
    Result of clipping a vertex/face mesh against an axis-aligned
    bounding box.

    Instances are returned by `PlyData.clip`; this class is not
    intended to be instantiated directly.

    Every output vertex is a convex combination of the original input
    vertices.  The combination is stored in compressed-sparse-row
    style: for output vertex ``k``, its supporting original vertices
    are ``provenance_vertices[provenance_indptr[k]:
    provenance_indptr[k+1]]`` with weights
    ``provenance_weights[...]`` over the same slice.

    Attributes
    ----------
    ply : PlyData
        The clipped mesh.  Every vertex of its vertex element lies in
        the box; it contains the surviving original vertices and the
        intersection vertices created on original edges or on box
        edges/corners.  Its face element holds only faces with
        non-empty geometry inside the box (polygons fan-triangulated),
        renumbered so that every index points at a valid vertex of the
        new vertex element.
    provenance_indptr : numpy.ndarray
        Length ``len(vertex element) + 1``; offsets into
        `provenance_vertices` and `provenance_weights` for each output
        vertex.
    provenance_vertices : numpy.ndarray
        Concatenated input indices of the original vertices
        supporting each output vertex.
    provenance_weights : numpy.ndarray
        Convex weights corresponding to `provenance_vertices`; each
        output vertex's weights sum to 1.
    face_map : numpy.ndarray
        For each output face, the index of the face it derives from in
        the input face element.  Clipping one input face can produce
        several output faces.
    """

    def __init__(self, ply, provenance_indptr, provenance_vertices,
                 provenance_weights, face_map):
        self.ply = ply
        self.provenance_indptr = provenance_indptr
        self.provenance_vertices = provenance_vertices
        self.provenance_weights = provenance_weights
        self.face_map = face_map

    def __repr__(self):
        return ('ClipResult(vertices=%d, faces=%d)' %
                (len(self.provenance_indptr) - 1,
                 len(self.face_map)))

    def trace_vertex(self, index):
        """
        Return the original vertices and convex weights an output
        vertex is derived from.

        An untouched original vertex yields one support vertex with
        weight 1.  An intersection on an original mesh edge yields
        exactly two support vertices: the edge endpoints, with weights
        ``1 - t`` and ``t``, where ``t`` is the interpolation position
        along the edge.  A vertex on a box edge or box corner (where
        clipping planes meet inside a face) combines the vertices of
        the originating face and may therefore have three or more
        support vertices.

        Parameters
        ----------
        index : int
            Index of a vertex in the output vertex element.

        Returns
        -------
        vertices : numpy.ndarray
            Input indices of the supporting original vertices.
        weights : numpy.ndarray
            Corresponding convex weights, summing to 1.
        """
        index = int(index)
        start = int(self.provenance_indptr[index])
        stop = int(self.provenance_indptr[index + 1])
        return (self.provenance_vertices[start:stop].copy(),
                self.provenance_weights[start:stop].copy())


class _PlyMeshClipper(object):
    """
    Engine that clips a vertex/face mesh to an axis-aligned bounding
    box.  Not part of the public interface; use `PlyData.clip`.

    Output vertices are registered lazily and compacted at the end so
    that output indices are contiguous and every index referenced by
    an output face is valid.
    """

    _position_properties = ('x', 'y', 'z')
    _rules = ('first', 'last', 'majority')

    def __init__(self, plydata, bbox, vertex_element, face_element,
                 index_property, discrete_properties, discrete_rule):
        self._plydata = plydata
        self._bbox = bbox
        self._vertex_element_name = vertex_element
        self._face_element_name = face_element
        self._index_property_name = index_property
        self._discrete_property_names = discrete_properties
        self._discrete_rule = discrete_rule

    def run(self):
        """
        Perform the clipping.

        Returns
        -------
        ClipResult
        """
        self._prepare()

        for face_index in range(self._face_count):
            self._clip_face(face_index)

        return self._build_result()

    def _prepare(self):
        """
        Validate all inputs and initialize the clipping state.
        """
        bbox = _np.asarray(self._bbox, dtype=_np.float64)
        if bbox.shape != (3, 2):
            raise PlyBBoxClipError(
                "bbox must have shape (3, 2), giving (min, max) "
                "for x, y, z")
        self._lower = bbox[:, 0]
        self._upper = bbox[:, 1]
        if _np.any(self._lower > self._upper):
            raise PlyBBoxClipError(
                "bbox minimum must not exceed maximum on any axis")

        if self._discrete_rule not in self._rules:
            raise PlyBBoxClipError(
                "discrete_rule must be one of %r" % (self._rules,))

        for name in (self._vertex_element_name,
                     self._face_element_name):
            if name not in self._plydata:
                raise PlyBBoxClipError(
                    "element %r not found" % name)

        self._vertex_el = self._plydata[self._vertex_element_name]
        self._face_el = self._plydata[self._face_element_name]
        self._vertex_data = self._vertex_el.data
        self._face_data = self._face_el.data
        self._vertex_count = len(self._vertex_data)
        self._face_count = len(self._face_data)

        for prop_name in self._position_properties:
            if prop_name not in self._vertex_el:
                raise PlyBBoxClipError(
                    "vertex element %r has no %r property" %
                    (self._vertex_element_name, prop_name))

        positions = []
        for prop_name in self._position_properties:
            positions.append(
                _np.asarray(self._vertex_data[prop_name],
                            dtype=_np.float64))
        self._all_positions = _np.stack(positions, axis=1)

        self._index_prop = self._resolve_index_property()
        self._index_dtype = self._index_prop.val_dtype

        self._classify_vertex_properties()

        # Output vertex registry.  Each registered vertex records its
        # structured data, exact position, and its convex weights on
        # the original input vertices.
        self._records = []
        self._positions = []
        self._support = []
        self._original_id = _np.full(self._vertex_count, -1,
                                     dtype=_np.int64)
        self._edge_cache = {}

        # Output face data.
        self._out_rings = []
        self._face_map = []

    def _resolve_index_property(self):
        """
        Return the face element's list property holding vertex
        indices.
        """
        list_props = [p for p in self._face_el.properties
                      if isinstance(p, PlyListProperty)]
        if self._index_property_name is not None:
            if self._index_property_name not in self._face_el:
                raise PlyBBoxClipError(
                    "face element %r has no property %r" %
                    (self._face_element_name,
                     self._index_property_name))
            prop = self._face_el.ply_property(
                self._index_property_name)
            if not isinstance(prop, PlyListProperty):
                raise PlyBBoxClipError(
                    "index property %r must be a list property" %
                    self._index_property_name)
            return prop

        if len(list_props) == 1:
            return list_props[0]

        if not list_props:
            raise PlyBBoxClipError(
                "face element %r has no list property holding "
                "vertex indices" % self._face_element_name)

        raise PlyBBoxClipError(
            "face element %r has several list properties %r; "
            "index_property must be specified" %
            (self._face_element_name,
             [p.name for p in list_props]))

    def _classify_vertex_properties(self):
        """
        Decide which vertex properties are interpolated and which are
        discrete labels that must be selected rather than blended.
        """
        names = list(self._vertex_data.dtype.names)
        if self._discrete_property_names is None:
            discrete = set()
            for name in names:
                if name in self._position_properties:
                    continue
                kind = self._vertex_data.dtype.fields[name][0].kind
                if kind in ('i', 'u', 'b', 'O'):
                    discrete.add(name)
        else:
            discrete = set(self._discrete_property_names)
            for name in discrete:
                if name not in self._vertex_el:
                    raise PlyBBoxClipError(
                        "vertex element %r has no property %r" %
                        (self._vertex_element_name, name))
            for name in self._position_properties:
                if name in discrete:
                    raise PlyBBoxClipError(
                        "position property %r cannot be treated "
                        "as discrete" % name)

        # Object properties cannot be arithmetically interpolated, so
        # they are always handled as discrete labels.
        for name in names:
            kind = self._vertex_data.dtype.fields[name][0].kind
            if kind == 'O':
                discrete.add(name)
        self._discrete = discrete

    def _face_indices(self, face_index):
        """
        Return the vertex-index array of one input face.
        """
        raw = self._face_data[self._index_prop.name][face_index]
        indices = _np.asarray(raw).ravel()
        if indices.size < 3:
            raise PlyBBoxClipError(
                "face %d has fewer than 3 vertices" % face_index)
        if indices.min() < 0 or indices.max() >= self._vertex_count:
            raise PlyBBoxClipError(
                "face %d references a vertex outside the vertex "
                "element" % face_index)
        return [int(i) for i in indices]

    def _clip_face(self, face_index):
        """
        Clip one input face and register its output geometry.

        No separate intersection pre-filter is used: a face that
        contains the whole box while having every vertex and edge
        outside it must also be retained, and Sutherland-Hodgman
        clipping naturally yields an empty polygon for a genuinely
        disjoint face.
        """
        ring = self._face_indices(face_index)
        tokens = [self._add_original(i) for i in ring]

        # Sutherland-Hodgman clipping against the six half-spaces.
        for axis in range(3):
            tokens = self._clip_plane(
                tokens, axis, self._lower[axis], True)
            tokens = self._clip_plane(
                tokens, axis, self._upper[axis], False)

        if len(tokens) < 3:
            return

        tokens = self._dedupe_ring(tokens)
        for k in range(1, len(tokens) - 1):
            triangle = (tokens[0], tokens[k], tokens[k + 1])
            if len(set(triangle)) < 3:
                # Skip degenerate triangles created when several
                # clipping planes meet at a box edge or corner.
                continue
            self._out_rings.append(triangle)
            self._face_map.append(face_index)

    @staticmethod
    def _dedupe_ring(tokens):
        """
        Remove consecutive duplicate vertices (including across the
        ring closure) from a polygon given as registry indices.
        """
        deduped = []
        for token in tokens:
            if not deduped or deduped[-1] != token:
                deduped.append(token)
        if len(deduped) > 1 and deduped[0] == deduped[-1]:
            deduped.pop()
        return deduped

    def _clip_plane(self, tokens, axis, bound, lower):
        """
        Clip a convex polygon (given as registry indices) against one
        clipping plane.  When `lower` is True the retained half-space
        is coordinate >= bound, otherwise coordinate <= bound.
        """
        if not tokens:
            return tokens

        output = []
        previous = tokens[-1]
        previous_inside = self._token_inside(previous, axis,
                                             bound, lower)
        for current in tokens:
            current_inside = self._token_inside(current, axis,
                                                bound, lower)
            if current_inside:
                if not previous_inside:
                    output.append(self._add_intersection(
                        previous, current, axis, bound))
                output.append(current)
            elif previous_inside:
                output.append(self._add_intersection(
                    previous, current, axis, bound))
            previous = current
            previous_inside = current_inside
        return output

    def _token_inside(self, token, axis, bound, lower):
        coordinate = self._positions[token][axis]
        if lower:
            return coordinate >= bound
        return coordinate <= bound

    def _add_original(self, vertex_index):
        """
        Register an input vertex in the output registry.
        """
        existing = self._original_id[vertex_index]
        if existing >= 0:
            return existing

        token = len(self._records)
        self._original_id[vertex_index] = token
        self._records.append(
            self._vertex_data[vertex_index])
        self._positions.append(
            self._all_positions[vertex_index].copy())
        self._support.append({vertex_index: 1.0})
        return token

    def _add_intersection(self, first, second, axis, bound):
        """
        Register (or reuse) the intersection of the segment between
        two registered vertices with one clipping plane.  The convex
        weights of both parents over the original input vertices are
        merged using the local interpolation parameter.
        """
        key = (first, second) if first < second else (second, first)
        cached = self._edge_cache.get(key)
        if cached is not None:
            return cached

        start = self._positions[first]
        end = self._positions[second]
        parameter = ((bound - start[axis]) /
                     (end[axis] - start[axis]))

        if parameter <= 0.0:
            return first
        if parameter >= 1.0:
            return second

        position = start + parameter * (end - start)
        position[axis] = bound
        record = self._interpolate_record(first, second,
                                          parameter)
        support = {}
        for (vertex, weight) in self._support[first].items():
            support[vertex] = (1.0 - parameter) * weight
        for (vertex, weight) in self._support[second].items():
            support[vertex] = support.get(vertex, 0.0) + \
                parameter * weight

        token = len(self._records)
        self._edge_cache[key] = token
        self._records.append(record)
        self._positions.append(position)
        self._support.append(support)
        return token

    def _interpolate_record(self, first, second, parameter):
        """
        Build the vertex record of an intersection vertex.

        Floating-point properties are linearly interpolated; discrete
        properties are selected according to the configured rule.
        """
        first_record = self._records[first]
        second_record = self._records[second]
        record = _np.empty((), dtype=self._vertex_data.dtype)
        for name in self._vertex_data.dtype.names:
            first_value = first_record[name]
            second_value = second_record[name]
            if name in self._discrete:
                value = self._select_discrete(first_value,
                                              second_value)
            else:
                value = (first_value +
                         parameter * (second_value -
                                      first_value))
            record[name] = value
        return record

    def _select_discrete(self, first_value, second_value):
        if self._discrete_rule == 'first':
            return first_value
        if self._discrete_rule == 'last':
            return second_value
        if first_value == second_value:
            return first_value
        # 'majority' with two sources and no agreement: fall back to
        # the first source deterministically.
        return first_value

    def _build_result(self):
        """
        Compact the registry, renumber indices, and assemble the
        returned `ClipResult`.  Only vertices referenced by an output
        face are kept; provenance is stored separately, so the output
        vertex element contains no point outside the box.
        """
        used = sorted({token for ring in self._out_rings
                       for token in ring})
        remap = {old: new for (new, old) in enumerate(used)}

        vertex_dtype = self._vertex_data.dtype
        if used:
            new_vertices = _np.array(
                [self._records[i] for i in used],
                dtype=vertex_dtype)
        else:
            new_vertices = _np.empty(0, dtype=vertex_dtype)

        indptr = [0]
        provenance_vertices = []
        provenance_weights = []
        for token in used:
            support = self._support[token]
            for vertex in sorted(support):
                provenance_vertices.append(vertex)
                provenance_weights.append(support[vertex])
            indptr.append(len(provenance_vertices))
        provenance_indptr = _np.array(indptr, dtype=_np.int64)
        provenance_vertices = _np.array(provenance_vertices,
                                        dtype=_np.int64)
        provenance_weights = _np.array(provenance_weights,
                                       dtype=_np.float64)

        face_records = []
        face_map = []
        face_dtype = self._face_data.dtype
        index_name = self._index_prop.name
        field_names = list(face_dtype.names)
        for (ring, source_face) in zip(self._out_rings,
                                       self._face_map):
            indices = _np.array(
                [remap[token] for token in ring],
                dtype=self._index_dtype)
            source_record = self._face_data[source_face]
            values = tuple(
                indices if name == index_name
                else source_record[name]
                for name in field_names)
            face_records.append(values)
            face_map.append(source_face)

        if face_records:
            new_faces = _np.array(face_records, dtype=face_dtype)
        else:
            new_faces = _np.empty(0, dtype=face_dtype)

        new_vertex_el = self._describe_element(
            self._vertex_el, new_vertices)
        new_face_el = self._describe_element(
            self._face_el, new_faces)

        elements = []
        for element in self._plydata.elements:
            if element.name == self._vertex_el.name:
                elements.append(new_vertex_el)
            elif element.name == self._face_el.name:
                elements.append(new_face_el)
            else:
                elements.append(element)

        new_ply = PlyData(
            elements, self._plydata.text,
            self._plydata._byte_order,
            self._plydata.comments, self._plydata.obj_info)

        return ClipResult(
            new_ply, provenance_indptr, provenance_vertices,
            provenance_weights,
            _np.array(face_map, dtype=_np.int64))

    def _describe_element(self, old_element, new_data):
        """
        Rebuild an element around a replacement data array while
        preserving property type choices and comments.
        """
        len_types = {}
        val_types = {}
        for prop in old_element.properties:
            if isinstance(prop, PlyListProperty):
                len_types[prop.name] = prop.len_dtype
                val_types[prop.name] = prop.val_dtype
        return PlyElement.describe(
            new_data, old_element.name,
            len_types=len_types, val_types=val_types,
            comments=old_element.comments)


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
