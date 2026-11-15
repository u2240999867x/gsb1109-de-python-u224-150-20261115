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
import os as _os
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
                 comments=[], obj_info=[], reference_rules=None,
                 references=None):
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

        if references is not None:
            if reference_rules is None:
                reference_rules = references
            else:
                reference_rules = _merge_reference_maps(reference_rules,
                                                        references)

        (comments, declared) = _split_references(comments)
        if reference_rules is not None:
            declared = _merge_reference_maps(declared, reference_rules)

        self.comments = comments
        self.obj_info = obj_info
        self.elements = elements
        self.reference_rules = declared

    @property
    def references(self):
        """
        Mapping from element names to their reference rules.

        Each value is a mapping from a property name to the name of the
        element whose indices the property contains.  These rules are
        used by `merge_ply` to shift indices when sources are
        concatenated.
        """
        rules = dict((name, elt.references) for name, elt in
                     ((e.name, e) for e in self.elements))
        for (name, element_rules) in self._reference_rules.items():
            if name in rules:
                rules[name] = _merge_element_references(
                    rules[name], element_rules)
            else:
                rules[name] = dict(element_rules)
        return rules

    @references.setter
    def references(self, rules):
        self.reference_rules = rules

    def _get_reference_rules(self):
        return dict((name, dict(rules)) for name, rules in
                    self._reference_rules.items())

    def _set_reference_rules(self, rules):
        self._reference_rules = _normalize_reference_map(rules)

    reference_rules = property(_get_reference_rules, _set_reference_rules)

    @property
    def missing(self):
        """
        Mapping from element names to their `PlyElement.missing`
        mappings.
        """
        return dict((elt.name, elt.missing) for elt in self.elements
                    if elt.missing)

    @property
    def missing_mask(self):
        """
        Alias for `missing`.
        """
        return self.missing

    @property
    def masks(self):
        """
        Alias for `missing`.
        """
        return self.missing

    @property
    def mask(self):
        """
        Alias for `missing`.
        """
        return self.missing

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

    def merge(self, *others, **kwargs):
        """
        Merge this instance with one or more other PLY sources.

        This is a convenience wrapper around `merge_ply`.  The
        receiver is the first source, so both of the following are
        equivalent::

            a.merge(b, mode='union')
            merge_ply(a, b, mode='union')

        Parameters
        ----------
        *others : PLY sources
            Additional `PlyData` instances, filenames, path-like
            objects, or readable streams.
        **kwargs
            See `merge_ply` for the supported keyword arguments.

        Returns
        -------
        PlyData
        """
        return merge_ply(self, *others, **kwargs)

    concat = merge
    concatenate = merge
    merge_batches = merge

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

        for (element, prop, target) in _reference_header_lines(
                self._reference_rules):
            lines.append('comment plyfile.reference %s %s %s' %
                         (element, prop, target))

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

    def __init__(self, name, properties, count, comments=[],
                 references=None):
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

        (comments, declared) = _split_references(comments, self.name)
        (comments, declared_missing) = _split_missing(comments,
                                                       self.name)
        if references is None:
            references = declared
        elif declared:
            references = _merge_element_references(
                declared,
                _normalize_element_references(references, self.name))

        self.comments = comments
        self.references = references
        self._declared_missing = declared_missing
        self._missing = {}

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
        if self._declared_missing:
            self._missing = _decode_missing(self._declared_missing, data)
            self._declared_missing = {}
        else:
            self._missing = {}

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
        names = set(prop.name for prop in self._properties)
        self._missing = dict((name, mask) for (name, mask)
                             in self._missing.items() if name in names)

    properties = property(_get_properties, _set_properties)

    def _get_comments(self):
        return list(self._comments)

    def _set_comments(self, comments):
        _check_comments(comments)
        self._comments = list(comments)

    comments = property(_get_comments, _set_comments)

    def _get_references(self):
        return dict(self._references)

    def _set_references(self, references):
        self._references = _normalize_element_references(
            references, self.name)

    references = property(_get_references, _set_references)

    @property
    def missing(self):
        """
        Mapping from property names to boolean arrays marking values
        that were absent from a merge.
        """
        return _MaskDict(self, self._missing)

    @property
    def masks(self):
        """
        Alias for `missing`.
        """
        return self.missing

    @property
    def missing_mask(self):
        """
        Mapping from property names to boolean missing-value masks.

        Fully present properties yield all-False masks.  The mapping is
        callable as a shorthand: ``element.missing_mask('red')``.
        """
        return self.missing

    @property
    def mask(self):
        """
        The `missing` mapping.  It can be indexed or called with a
        property name.
        """
        return self.missing

    @property
    def missing_masks(self):
        """
        Alias for `missing`.
        """
        return self.missing

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
                 comments=[], references=None, reference_rules=None,
                 reference=None):
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

        if reference is not None:
            if references is None:
                references = reference
            elif references != reference:
                raise PlyMergeConflict(
                    "conflicting reference declarations",
                    element=name)
        if references is None:
            references = reference_rules
        elif reference_rules is not None:
            references = _merge_element_references(
                _normalize_element_references(references, name),
                _normalize_element_references(reference_rules, name))

        elt = PlyElement(name, properties, count, comments,
                         references=references)
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
        self._restore_missing()

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

    def _restore_missing(self):
        if self._declared_missing:
            self._missing = _decode_missing(
                self._declared_missing, self.data)
            self._declared_missing = {}

    @property
    def header(self):
        lines = ['element %s %d' % (self.name, self.count)]

        # Some information is lost here, since all comments are placed
        # between the 'element' line and the first property definition.
        for c in self.comments:
            lines.append('comment ' + c)

        for (prop, target) in self._references.items():
            lines.append('comment plyfile.reference %s %s %s' %
                         (self.name, prop, target))

        for (prop, mask) in self._encoded_missing().items():
            lines.append('comment plyfile.missing %s.%s:%s' %
                         (self.name, prop, mask))

        lines.extend(list(map(str, self.properties)))

        return '\n'.join(lines)

    def _encoded_missing(self):
        result = {}
        for (prop_name, mask) in self._missing.items():
            encoded = _encode_mask(mask)
            if encoded != 'none':
                result[prop_name] = encoded
        return result

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


_REFERENCE_COMMENT = 'plyfile.reference'
_MISSING_COMMENT = 'plyfile.missing'

_MERGE_MODES = {
    'union': 'union',
    'outer': 'union',
    'append': 'union',
    'concat': 'union',
    'concatenate': 'union',
    'merge': 'union',
    'intersection': 'intersection',
    'inner': 'intersection',
    'common': 'intersection',
    'strict': 'strict',
    'exact': 'strict',
    'same': 'strict',
    'require_same': 'strict',
    'complete': 'complete',
    'complete_cases': 'complete',
    'drop_missing': 'complete',
    'drop': 'complete',
}

_FACE_ELEMENT_NAMES = {
    'face', 'faces', 'polygon', 'polygons', 'triangle', 'triangles',
}

_VERTEX_REFERENCE_NAMES = {
    'vertex_indices', 'vertex_index', 'vertex_ids', 'vertex_id',
    'vertices',
}


class _MaskDict(dict):
    """
    Mapping of property names to missing-value masks.

    Looking up a property that is fully present returns an all-False
    mask, so callers do not need to distinguish "no missing values"
    from "property not tracked".
    """

    def __init__(self, element, masks):
        dict.__init__(self, masks)
        self._element = element

    def __missing__(self, name):
        if name in self._element:
            return _np.zeros(self._element.count, dtype=bool)
        raise KeyError(name)

    def __call__(self, name):
        return self[name]


def _normalize_target(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)) and value:
        return _normalize_target(value[0])
    if isinstance(value, dict):
        for key in ('target', 'element', 'to', 'ref', 'reference'):
            if key in value:
                return _normalize_target(value[key])
    raise ValueError("invalid reference target %r" % (value,))


def _split_rule_string(text):
    for separator in ('->', '=>', '=', ':'):
        if separator in text:
            (left, right) = text.split(separator, 1)
            break
    else:
        fields = text.split()
        if len(fields) == 3 and '.' not in fields[0]:
            return (fields[0], fields[1], _normalize_target(fields[2]))
        if len(fields) == 2:
            (left, right) = fields
        else:
            raise ValueError("invalid reference rule %r" % text)

    left = left.strip()
    left = left.strip('()[]')
    if not left:
        raise ValueError("invalid reference rule %r" % text)

    if '.' in left:
        (element, prop) = left.split('.', 1)
    elif len(left.split()) == 2:
        (element, prop) = left.split()
    else:
        raise ValueError("invalid reference rule %r" % text)

    return (element.strip(), prop.strip(), _normalize_target(right.strip()))


def _normalize_element_references(rules, element_name):
    """
    Normalize a single element's reference rules to a mapping from
    property name (or ``'*'``) to target element name.
    """
    if rules is None:
        return {}

    normalized = {}

    def add(prop, target):
        for name in ([prop] if isinstance(prop, str) else prop):
            target_name = _normalize_target(target)
            _check_name(name)
            _check_name(target_name)
            if name in normalized and normalized[name] != target_name:
                raise PlyMergeConflict(
                    "conflicting reference target for property %r" % name,
                    element=element_name, property=name)
            normalized[name] = target_name

    if isinstance(rules, str):
        add('*', rules)
        return normalized

    if isinstance(rules, dict):
        if any(key in rules for key in
               ('target', 'element', 'to', 'ref', 'reference')):
            prop = rules.get('property', rules.get('prop', '*'))
            add(prop, rules)
            return normalized

        for (prop, target) in rules.items():
            add(prop, target)
        return normalized

    if isinstance(rules, (list, tuple)):
        for rule in rules:
            if isinstance(rule, str):
                add(rule, 'vertex')
            elif isinstance(rule, dict):
                prop = rule.get('property', rule.get('prop'))
                if prop is None:
                    raise ValueError("reference rule missing property")
                add(prop, rule)
            elif isinstance(rule, (list, tuple)):
                if len(rule) == 1:
                    add(rule[0], 'vertex')
                elif len(rule) == 2:
                    add(rule[0], rule[1])
                else:
                    raise ValueError("invalid reference rule %r" % (rule,))
            else:
                raise ValueError("invalid reference rule %r" % (rule,))
        return normalized

    raise TypeError("reference rules must be a mapping or sequence")


def _merge_element_references(first, second):
    result = dict(first)
    for (prop, target) in second.items():
        if prop in result and result[prop] != target:
            raise PlyMergeConflict(
                "conflicting reference target %r != %r" %
                (result[prop], target), property=prop)
        result[prop] = target
    return result


def _normalize_reference_map(rules):
    """
    Normalize data-level reference declarations.

    Accepted forms include a mapping from element names to element
    rule mappings, a sequence of ``(element, property, target)``
    triples, and strings like ``'face.vertex_indices->vertex'``.
    """
    if rules is None:
        return {}

    result = {}

    def add(element, prop, target):
        _check_name(element)
        element_rules = result.setdefault(element, {})
        normalized = _normalize_element_references(
            {prop: target}, element)
        result[element] = _merge_element_references(
            element_rules, normalized)

    if isinstance(rules, str):
        add(*_split_rule_string(rules))
        return result

    if isinstance(rules, dict):
        if all(key in rules for key in ('element', 'property')):
            element = rules['element']
            prop = rules.get('property', rules.get('prop'))
            add(element, prop, rules)
            return result

        for (key, value) in rules.items():
            if isinstance(key, (tuple, list)):
                if len(key) != 2:
                    raise ValueError("invalid reference key %r" % (key,))
                add(key[0], key[1], value)
            elif isinstance(key, str) and (
                    '->' in key or ',' in key or ':' in key or
                    '=' in key):
                (element, prop, target) = _split_rule_string(key)
                if value not in (True, None):
                    target = value
                add(element, prop, target)
            elif isinstance(key, str) and '.' in key:
                (element, prop) = key.split('.', 1)
                add(element, prop, value)
            else:
                element = key
                if isinstance(value, str):
                    add(element, '*', value)
                elif isinstance(value, dict):
                    if any(k in value for k in
                           ('target', 'element', 'to', 'ref',
                            'reference')):
                        prop = value.get('property', value.get('prop'))
                        if prop is None:
                            raise ValueError(
                                "reference rule missing property")
                        add(element, prop, value)
                    else:
                        result[element] = _merge_element_references(
                            result.get(element, {}),
                            _normalize_element_references(
                                value, element))
                elif isinstance(value, (list, tuple)):
                    if (len(value) == 2 and
                            all(isinstance(x, str) for x in value) and
                            not isinstance(value[0], (list, tuple,
                                                      dict))):
                        add(element, value[0], value[1])
                    else:
                        result[element] = _merge_element_references(
                            result.get(element, {}),
                            _normalize_element_references(
                                value, element))
                else:
                    raise ValueError(
                        "invalid reference rules for %r" % element)
        return result

    if isinstance(rules, (list, tuple)):
        if (len(rules) == 3 and
                all(isinstance(item, str) for item in rules)):
            rules = (rules,)
        for rule in rules:
            if isinstance(rule, str):
                add(*_split_rule_string(rule))
            elif isinstance(rule, dict):
                element = rule.get('element')
                prop = rule.get('property', rule.get('prop'))
                if element is None or prop is None:
                    raise ValueError("invalid reference rule %r" % (rule,))
                add(element, prop, rule)
            elif isinstance(rule, (list, tuple)):
                if len(rule) != 3:
                    raise ValueError("invalid reference rule %r" % (rule,))
                add(rule[0], rule[1], rule[2])
            else:
                raise ValueError("invalid reference rule %r" % (rule,))
        return result

    raise TypeError("reference rules must be a mapping or sequence")


def _parse_reference_comment(comment):
    if not comment.startswith(_REFERENCE_COMMENT + ' '):
        return None
    fields = comment.split()
    if len(fields) != 4:
        return None
    return (fields[1], fields[2], fields[3])


def _split_references(comments, element_name=None):
    if isinstance(comments, str):
        comments = [comments]

    result = []
    rules = {}

    for comment in comments:
        parsed = _parse_reference_comment(comment)
        if parsed is None:
            result.append(comment)
            continue

        (element, prop, target) = parsed
        if element_name is None:
            rules.setdefault(element, {})[prop] = target
        elif element == element_name:
            if prop in rules and rules[prop] != target:
                raise PlyMergeConflict(
                    "conflicting reference target for property %r" % prop,
                    element=element_name, property=prop)
            rules[prop] = target
        else:
            result.append(comment)

    return (result, rules)


def _split_missing(comments, element_name=None):
    result = []
    masks = {}

    for comment in comments:
        if not comment.startswith(_MISSING_COMMENT + ' '):
            result.append(comment)
            continue

        raw_fields = comment[len(_MISSING_COMMENT) + 1:].split()
        fields = []
        for field in raw_fields:
            if element_name is not None and '.' in field:
                (owner, prop) = field.split('.', 1)
                if owner != element_name:
                    continue
                field = prop
            fields.append(field)
        if not fields:
            result.append(comment)
            continue

        for spec in fields:
            if ':' not in spec:
                raise PlyParseError(
                    "malformed missing-value declaration %r" % spec)
            (prop, encoded) = spec.split(':', 1)
            masks[prop] = encoded

    return (result, masks)


def _decode_missing(declarations, data):
    masks = {}
    for (prop, encoded) in declarations.items():
        if prop not in data.dtype.fields:
            raise PlyParseError(
                "missing-value mask for unknown property %r" % prop)
        try:
            masks[prop] = _decode_mask(encoded, len(data))
        except (ValueError, TypeError):
            raise PlyParseError(
                "malformed missing-value mask for property %r" % prop)
    return masks


def _encode_mask(mask):
    mask = _np.asarray(mask, dtype=bool)
    indices = _np.flatnonzero(mask)
    if indices.size == 0:
        return 'none'
    if indices.size == mask.size:
        return 'all'
    if len(indices) > 4:
        consecutive = _np.flatnonzero(_np.diff(indices) != 1)
        if consecutive.size == 0:
            return 'range:%d-%d' % (int(indices[0]), int(indices[-1]) + 1)
    return 'index:' + ','.join(str(int(k)) for k in indices)


def _decode_mask(encoded, count):
    if encoded == 'none':
        return _np.zeros(count, dtype=bool)
    mask = _np.zeros(count, dtype=bool)
    if encoded == 'all':
        mask[:] = True
        return mask
    if encoded.startswith('range:'):
        (start, stop) = encoded[len('range:'):].split('-', 1)
        mask[int(start):int(stop)] = True
        return mask
    if encoded.startswith('index:'):
        for item in encoded[len('index:'):].split(','):
            if item:
                mask[int(item)] = True
        return mask
    raise ValueError("unknown mask encoding %r" % encoded)


def _reference_header_lines(rules):
    lines = []
    for element in sorted(rules):
        for prop in sorted(rules[element]):
            lines.append((element, prop, rules[element][prop]))
    return lines


def _property_signature(prop):
    if isinstance(prop, PlyListProperty):
        return ('list', prop.len_dtype, prop.val_dtype)
    return ('scalar', None, prop.val_dtype)


def _property_type_name(prop):
    if isinstance(prop, PlyListProperty):
        return 'list %s %s' % (_data_type_reverse[prop.len_dtype],
                               _data_type_reverse[prop.val_dtype])
    return _data_type_reverse[prop.val_dtype]


def _copy_property(prop):
    if isinstance(prop, PlyListProperty):
        return PlyListProperty(prop.name, prop.len_dtype, prop.val_dtype)
    return PlyProperty(prop.name, prop.val_dtype)


def _is_face_element(name):
    return name.lower() in _FACE_ELEMENT_NAMES


def _auto_reference_rules(data):
    rules = {}
    element_names = set(elt.name for elt in data.elements)
    if 'vertex' not in element_names:
        return rules

    for elt in data.elements:
        face = _is_face_element(elt.name)
        for prop in elt.properties:
            name = prop.name.lower()
            matches = name in _VERTEX_REFERENCE_NAMES
            if face and 'vertex' in name:
                matches = True
            if matches:
                rules.setdefault(elt.name, {})[prop.name] = 'vertex'

    return rules


def _explicit_reference_rules(data):
    rules = _normalize_reference_map(data.reference_rules)
    for elt in data.elements:
        element_rules = _normalize_element_references(
            elt.references, elt.name)
        if elt.name in rules:
            rules[elt.name] = _merge_element_references(
                rules[elt.name], element_rules)
        elif element_rules:
            rules[elt.name] = element_rules
    return rules


def _collect_reference_rules(data, extra=None, base=None):
    rules = _explicit_reference_rules(data)
    if extra:
        for element in extra:
            if element in rules:
                rules[element] = _merge_element_references(
                    rules[element], extra[element])
            else:
                rules[element] = dict(extra[element])

    if base:
        for (element, element_rules) in base.items():
            if element in rules:
                rules[element] = _merge_element_references(
                    rules[element], element_rules)
            else:
                rules[element] = dict(element_rules)

    for (element, element_rules) in _auto_reference_rules(data).items():
        current = rules.setdefault(element, {})
        for (prop, target) in element_rules.items():
            current.setdefault(prop, target)

    return rules


def _expand_element_references(properties, rules):
    expanded = {}
    for (prop, target) in rules.items():
        if prop == '*':
            for p in properties:
                if isinstance(p, PlyListProperty):
                    expanded.setdefault(p.name, target)
        else:
            expanded[prop] = target
    return expanded


def _shift_reference_values(values, prop, offset):
    if offset == 0:
        return values

    if isinstance(prop, PlyListProperty):
        result = _np.empty(len(values), dtype=object)
        for (k, value) in enumerate(values):
            array = _np.asarray(value)
            if array.size == 0:
                result[k] = _np.array([], dtype=prop.val_dtype)
            else:
                result[k] = (array.astype(_np.int64) + offset).astype(
                    prop.val_dtype, copy=False)
        return result

    array = _np.asarray(values)
    return (array.astype(_np.int64) + offset).astype(
        prop.val_dtype, copy=False)


def _empty_list_values(count, dtype):
    result = _np.empty(count, dtype=object)
    for k in range(count):
        result[k] = _np.array([], dtype=dtype)
    return result


def _as_ply_data(source):
    if isinstance(source, PlyData):
        return source
    if isinstance(source, (str, bytes, _os.PathLike)):
        return PlyData.read(source)
    if hasattr(source, 'read'):
        return PlyData.read(source)
    raise TypeError("expected PlyData, filename, or readable stream")


def _flatten_sources(sources):
    flattened = []
    for source in sources:
        if (isinstance(source, (list, tuple)) and
                not isinstance(source, PlyData)):
            flattened.extend(_flatten_sources(source))
        else:
            flattened.append(source)
    return tuple(_as_ply_data(source) for source in flattened)


def _normalize_merge_mode(mode):
    if mode is None:
        return 'union'
    if not isinstance(mode, str):
        raise ValueError("merge mode must be a string")
    key = mode.strip().lower()
    if key not in _MERGE_MODES:
        raise ValueError("unknown merge mode %r; expected one of %s" %
                         (mode, ', '.join(sorted(set(_MERGE_MODES.values())))))
    return _MERGE_MODES[key]


def merge_ply(*sources, **kwargs):
    """
    Merge multiple PLY sources into a new `PlyData` instance.

    All sources must be concatenated along their elements; rows are
    never aligned by index.  The merge mode decides how schemas that
    differ between sources are handled:

    ``'union'`` (also ``'outer'``, ``'append'``)
        Keep every element and every property.  A property missing
        from a source is synthesized in the output and the affected
        rows are flagged in the element's `missing` masks.  Missing
        scalar values are zero; missing list values are empty arrays.
    ``'intersection'`` (also ``'inner'``, ``'common'``)
        Keep only elements and properties present in every source.
    ``'strict'`` (also ``'exact'``)
        Require every source to have exactly the same elements and
        properties, with compatible types.
    ``'complete'`` (also ``'drop_missing'``)
        Merge like ``'union'`` but drop rows for which any property is
        missing.  References are remapped after rows are dropped.

    Types are never widened.  If the same element and property name
    occur with different types, `PlyMergeConflict` is raised.

    Parameters
    ----------
    *sources : PlyData, str, path-like, or readable stream
        Two or more PLY sources.  A single list or tuple of sources is
        also accepted.
    mode : str, optional (default='union')
        Merge mode as described above.
    reference_rules : mapping or sequence, optional
        Declarations that the given properties contain indices into
        another element.  Accepted forms include
        ``{'face': {'vertex_indices': 'vertex'}}`` and
        ``[('face', 'vertex_indices', 'vertex')]``.  Element-level
        declarations via `PlyElement.references` are also honored.
        The standard ``face.vertex_indices`` reference to ``vertex``
        is recognized automatically.
    mask : bool, optional (default=True)
        Whether to record missing-value masks on the result.
    mask_suffix : str, optional
        If given, add a ``uint8`` property named ``property + suffix``
        for every property with missing values (1 means missing).
    preserve_types : bool, optional (default=True)
        Preserve each property's original PLY type.  This argument is
        accepted for clarity; types are always preserved.
    text, byte_order, comments, obj_info : optional
        Override the corresponding output metadata.  By default they
        are taken from the first source.

    Returns
    -------
    PlyData

    Raises
    ------
    PlyMergeConflict
        If property types or reference declarations conflict.
    PlyMergeError
        For other merge errors.
    """
    merge_mode = kwargs.pop('merge_mode', None)
    for alias in ('how', 'join', 'strategy'):
        value = kwargs.pop(alias, None)
        if value is None:
            continue
        if merge_mode is not None and value != merge_mode:
            raise ValueError("conflicting merge mode arguments")
        merge_mode = value
    mode = kwargs.pop('mode', None)
    if (mode is None and sources and
            isinstance(sources[-1], str) and
            sources[-1].strip().lower() in _MERGE_MODES and
            len(sources) > 2):
        mode = sources[-1]
        sources = sources[:-1]
    if mode is None:
        mode = merge_mode
    elif merge_mode is not None and \
            _normalize_merge_mode(merge_mode) != _normalize_merge_mode(mode):
        raise ValueError("conflicting values for 'mode' and 'merge_mode'")
    mode = _normalize_merge_mode(mode)
    reference_rules = kwargs.pop('reference_rules', None)
    for alias in ('reference', 'refs', 'ref_rules', 'reference_declarations',
                  'references'):
        value = kwargs.pop(alias, None)
        if value is None:
            continue
        if reference_rules is None:
            reference_rules = value
        else:
            reference_rules = _merge_reference_maps(reference_rules, value)
    declared_references = kwargs.pop('declared_references', None)
    mask = kwargs.pop('mask', kwargs.pop('masks', True))
    mask_suffix = kwargs.pop('mask_suffix',
                             kwargs.pop('missing_suffix', None))
    preserve_types = kwargs.pop('preserve_types', True)
    for alias in ('preserve_type', 'preserve_dtype', 'preserve_dtypes',
                  'keep_types', 'keep_dtype'):
        preserve_types = kwargs.pop(alias, preserve_types)
    text = kwargs.pop('text', None)
    byte_order = kwargs.pop('byte_order', None)
    comments = kwargs.pop('comments', None)
    obj_info = kwargs.pop('obj_info', None)
    extra_sources = kwargs.pop('sources', None)

    if not preserve_types:
        raise ValueError("merge_ply always preserves property types")

    if kwargs:
        raise TypeError("unexpected keyword argument(s): %s" %
                        ', '.join(sorted(kwargs)))

    if extra_sources is not None:
        if sources:
            raise TypeError("pass sources either positionally or via "
                            "'sources', not both")
        sources = tuple(extra_sources)

    sources = _flatten_sources(sources)
    if not sources:
        raise PlyMergeError("at least one source is required")

    if declared_references is None:
        extra_rules = reference_rules
    elif reference_rules is None:
        extra_rules = declared_references
    else:
        extra_rules = _merge_reference_maps(reference_rules,
                                            declared_references)
    extra_rules = _normalize_reference_map(extra_rules)

    first = sources[0]

    if text is None:
        text = first.text
    if byte_order is None:
        byte_order = first.byte_order
    if comments is None:
        comments = first.comments
    if obj_info is None:
        obj_info = first.obj_info

    element_order = []
    element_sources = {}
    for (source_index, source) in enumerate(sources):
        for elt in source.elements:
            if elt.name not in element_sources:
                element_order.append(elt.name)
                element_sources[elt.name] = []
            element_sources[elt.name].append((source_index, elt))

    source_names = [set(elt.name for elt in source.elements)
                    for source in sources]

    if mode in ('intersection', 'strict'):
        required = set.intersection(*source_names) if source_names else set()
        if mode == 'strict' and any(names != required
                                    for names in source_names):
            raise PlyMergeConflict(
                "strict merge requires identical element sets")
        element_order = [name for name in element_order
                         if name in required]

    if not element_order:
        return PlyData([], text=text, byte_order=byte_order,
                       comments=comments, obj_info=obj_info)

    explicit_rules = []
    for source in sources:
        explicit_rules.append(_merge_reference_maps(
            _explicit_reference_rules(source), extra_rules))

    merged_rules = {}
    for rules in explicit_rules:
        merged_rules = _merge_reference_maps(merged_rules, rules)
    merged_rules = _normalize_reference_map(merged_rules)

    source_rules = []
    all_rules = merged_rules
    for source in sources:
        rules = _collect_reference_rules(source, extra_rules,
                                         base=merged_rules)
        source_rules.append(rules)
        all_rules = _merge_reference_maps(all_rules, rules)

    merged_rules = all_rules

    known_elements = set(element_order)
    all_elements = set()
    for names in source_names:
        all_elements.update(names)

    effective_rules = {}
    for (element, rules) in merged_rules.items():
        if element not in all_elements:
            raise PlyMergeConflict(
                "reference rule for unknown element %r" % element,
                element=element)
        if element in known_elements:
            effective_rules[element] = dict(rules)

    for (element, rules) in effective_rules.items():
        for (prop, target) in rules.items():
            if prop != '*' and prop not in _element_property_names(
                    element_sources.get(element, [])):
                raise PlyMergeConflict(
                    "reference rule for unknown property %r" % prop,
                    element=element, property=prop)
            if target not in element_sources:
                raise PlyMergeConflict(
                    "reference target element %r is missing" % target,
                    element=element, property=prop)
    merged_rules = effective_rules

    offsets = _reference_offsets(element_order, element_sources)

    output_elements = []
    references_by_name = {}
    for name in element_order:
        entries = element_sources[name]
        output = _merge_element(
            name, entries, source_rules, merged_rules, mode, mask,
            mask_suffix, len(sources), offsets)
        if output is not None:
            output_elements.append(output)
            references_by_name[name] = dict(output.references)

    if mode == 'complete':
        _complete_elements(output_elements, references_by_name)

    return PlyData(output_elements, text=text, byte_order=byte_order,
                   comments=comments, obj_info=obj_info)




def _merge_reference_maps(first, second):
    result = _normalize_reference_map(first)
    other = _normalize_reference_map(second)
    for element in other:
        if element in result:
            result[element] = _merge_element_references(
                result[element], other[element])
        else:
            result[element] = dict(other[element])
    return result


def _element_property_names(entries):
    names = set()
    for (source_index, elt) in entries:
        names.update(prop.name for prop in elt.properties)
    return names


def _reference_offsets(element_order, element_sources):
    """
    For each referenced element, compute the per-source row offset in
    the merged output.  A source that lacks the target element gets no
    offset entry, which makes references from that source an error.
    """
    offsets = {}
    position = 0
    for name in element_order:
        entries = element_sources[name]
        source_offsets = {}
        for (source_index, elt) in entries:
            source_offsets[source_index] = position
            position += elt.count
        offsets[name] = source_offsets
    return offsets


def _element_property_order(entries, mode):
    order = []
    seen = set()
    for (source_index, elt) in entries:
        for prop in elt.properties:
            if prop.name not in seen:
                seen.add(prop.name)
                order.append(prop.name)

    if mode in ('intersection', 'strict'):
        if mode == 'intersection':
            common = None
            for (source_index, elt) in entries:
                names = set(prop.name for prop in elt.properties)
                common = names if common is None else common & names
        else:
            reference = set(prop.name for prop in entries[0][1].properties)
            common = reference
            for (source_index, elt) in entries[1:]:
                if set(prop.name for prop in elt.properties) != reference:
                    raise PlyMergeConflict(
                        "strict merge requires identical property sets",
                        element=entries[0][1].name)
        if common is not None:
            order = [name for name in order if name in common]

    return order


def _element_property_lookup(entries, order, element_name):
    lookup = {}
    for name in order:
        found = None
        for (source_index, elt) in entries:
            if name not in elt:
                continue
            prop = elt.ply_property(name)
            if found is None:
                found = prop
            elif _property_signature(prop) != _property_signature(found):
                raise PlyMergeConflict(
                    "incompatible types for property %r: %s vs %s" %
                    (name, _property_type_name(found),
                     _property_type_name(prop)),
                    element=element_name, property=name)
        if found is None:
            raise PlyMergeError("property %r not found" % name,
                                element=element_name, property=name)
        lookup[name] = found
    return lookup


def _merge_element(name, entries, source_rules, merged_rules, mode, mask,
                   mask_suffix, source_count, offsets):
    if mode == 'intersection':
        present_sources = set(index for (index, elt) in entries)
        if len(present_sources) != source_count:
            return None

    order = _element_property_order(entries, mode)
    if not order:
        had_properties = any(elt.properties for (index, elt) in entries)
        if had_properties and mode in ('intersection', 'strict'):
            return None
        first_elt = entries[0][1]
        properties = []
    else:
        first_elt = entries[0][1]
        lookup = _element_property_lookup(entries, order, name)
        properties = [_copy_property(lookup[prop_name])
                      for prop_name in order]

    fields = []
    for prop in properties:
        if isinstance(prop, PlyListProperty):
            fields.append((prop.name, object))
        else:
            fields.append((prop.name, prop.dtype()))

    present_sources = set(index for (index, elt) in entries)
    if mode == 'strict' and len(present_sources) != source_count:
        raise PlyMergeConflict(
            "strict merge requires every source to contain element %r" %
            name, element=name)

    count = sum(elt.count for (index, elt) in entries)
    data = _np.zeros(count, dtype=_np.dtype(fields))

    masks = dict((prop.name, _np.zeros(count, dtype=bool))
                 for prop in properties)

    element_rules = merged_rules.get(name, {})
    references = _expand_element_references(properties, element_rules)
    references = dict((prop, target) for (prop, target) in
                      references.items() if prop in set(order))

    position = 0
    for (source_index, elt) in entries:
        rows = elt.count
        (start, stop) = (position, position + rows)
        position = stop

        source_refs = source_rules[source_index].get(name, {})
        source_refs = _expand_element_references(elt.properties,
                                                 source_refs)

        for prop in properties:
            out_field = data[prop.name]
            if prop.name not in elt:
                masks[prop.name][start:stop] = True
                if isinstance(prop, PlyListProperty):
                    out_field[start:stop] = _empty_list_values(
                        rows, prop.val_dtype)
                else:
                    out_field[start:stop] = _np.zeros(
                        rows, dtype=prop.dtype())
                continue

            source_data = elt.data[prop.name]
            if prop.name in source_refs:
                target = source_refs[prop.name]
                if target not in offsets:
                    raise PlyMergeConflict(
                        "reference target element %r is missing" % target,
                        element=name, property=prop.name)
                if source_index not in offsets[target]:
                    raise PlyMergeConflict(
                        "source has no element %r to reference" % target,
                        element=name, property=prop.name)
                offset = offsets[target][source_index]
                source_data = _shift_reference_values(
                    source_data, elt.ply_property(prop.name), offset)

            if isinstance(prop, PlyListProperty):
                values = _np.empty(rows, dtype=object)
                for (k, value) in enumerate(source_data):
                    values[k] = _np.asarray(value, dtype=prop.val_dtype)
                out_field[start:stop] = values
            else:
                out_field[start:stop] = _np.asarray(
                    source_data, dtype=prop.dtype())

    missing = dict((prop_name, masks[prop_name])
                   for prop_name in masks if masks[prop_name].any())

    comments = first_elt.comments
    result = PlyElement(name, properties, count, comments,
                        references=references)
    result.data = data
    keep_masks = mask or mode == 'complete'
    result._missing = _MaskDict(result, missing) if keep_masks else \
        _MaskDict(result, {})

    if mask_suffix:
        _add_mask_properties(result, mask_suffix, missing)

    return result


def _complete_elements(elements, references_by_name):
    """
    Drop rows with missing values and cascade to rows that reference
    dropped rows, then remap references accordingly.
    """
    keeps = dict((elt.name, _np.ones(elt.count, dtype=bool))
                 for elt in elements)
    for elt in elements:
        for values in elt._missing.values():
            keeps[elt.name] &= ~values

    changed = True
    while changed:
        changed = False
        for elt in elements:
            for (prop_name, target) in references_by_name[elt.name].items():
                if prop_name not in elt:
                    continue
                row_keep = keeps[elt.name]
                if target not in keeps:
                    raise PlyMergeConflict(
                        "reference target element %r is missing" % target,
                        element=elt.name, property=prop_name)
                prop = elt.ply_property(prop_name)
                for (row, value) in enumerate(elt.data[prop_name]):
                    if not row_keep[row]:
                        continue
                    indices = _reference_indices(value, prop)
                    for index in indices:
                        if index < 0 or index >= len(keeps[target]):
                            raise PlyMergeConflict(
                                "reference index %d is out of range"
                                % index, element=elt.name,
                                property=prop_name)
                        if not keeps[target][index]:
                            row_keep[row] = False
                            changed = True
                            break

    remaps = {}
    for elt in elements:
        keep = keeps[elt.name]
        remap = _np.full(elt.count, -1, dtype=_np.int64)
        remap[keep] = _np.arange(int(keep.sum()), dtype=_np.int64)
        remaps[elt.name] = remap

    for elt in elements:
        keep = keeps[elt.name]
        masks = dict((prop_name, values[keep]) for (prop_name, values)
                     in elt._missing.items())
        data = elt.data[keep]

        for (prop_name, target) in references_by_name[elt.name].items():
            if prop_name not in elt:
                continue
            if target not in remaps:
                raise PlyMergeConflict(
                    "reference target element %r is missing" % target,
                    element=elt.name, property=prop_name)
            prop = elt.ply_property(prop_name)
            data[prop_name] = _remap_reference_values(
                data[prop_name], prop, remaps[target])

        elt._data = data
        elt._count = len(data)
        elt._missing = _MaskDict(
            elt, dict((prop_name, values) for (prop_name, values)
                      in masks.items() if values.any()))


def _remap_reference_values(values, prop, remap):
    if isinstance(prop, PlyListProperty):
        result = _np.empty(len(values), dtype=object)
        for (k, value) in enumerate(values):
            array = _np.asarray(value, dtype=_np.int64)
            if array.size == 0:
                result[k] = _np.array([], dtype=prop.val_dtype)
                continue
            mapped = remap[array]
            if (mapped < 0).any():
                raise PlyMergeConflict(
                    "reference to a dropped row", property=prop.name)
            result[k] = mapped.astype(prop.val_dtype)
        return result

    array = _np.asarray(values, dtype=_np.int64)
    mapped = remap[array]
    if (mapped < 0).any():
        raise PlyMergeConflict(
            "reference to a dropped row", property=prop.name)
    return mapped.astype(prop.val_dtype)


def _reference_indices(value, prop):
    array = _np.asarray(value)
    if isinstance(prop, PlyListProperty):
        return array.astype(_np.int64).ravel()
    return array.astype(_np.int64).reshape(-1)




def _add_mask_properties(element, suffix, missing):
    if not missing:
        return

    fields = list(element.data.dtype.descr)
    for prop_name in missing:
        mask_name = prop_name + suffix
        if mask_name in element:
            raise PlyMergeConflict(
                "mask property %r already exists" % mask_name,
                element=element.name, property=mask_name)
        fields.append((mask_name, 'u1'))

    data = _np.zeros(element.count, dtype=_np.dtype(fields))
    for prop in element.properties:
        data[prop.name] = element.data[prop.name]
    for (prop_name, values) in missing.items():
        data[prop_name + suffix] = values.astype('u1')

    properties = list(element.properties)
    for prop_name in missing:
        properties.append(PlyProperty(prop_name + suffix, 'u1'))

    element._properties = tuple(properties)
    element._index()
    element._data = data
    element._count = len(data)
    element._missing = _MaskDict(
        element, dict((prop_name, values) for (prop_name, values)
                      in element._missing.items()))


class PlyParseError(Exception):
    """
    Base class for PLY parsing errors.
    """

    pass


class PlyMergeError(ValueError):
    """
    Base class for PLY merge errors.
    """

    pass


class PlyMergeConflict(PlyMergeError):
    """
    Raised when two sources cannot be merged because their metadata is
    incompatible.

    Attributes
    ----------
    element : str or None
    property : str or None
    message : str
    """

    def __init__(self, message, element=None, property=None):
        self.message = message
        self.element = element
        self.property = property

        s = ''
        if self.element is not None:
            s += 'element %r: ' % self.element
        if self.property is not None:
            s += 'property %r: ' % self.property
        s += message

        PlyMergeError.__init__(self, s)

    def __repr__(self):
        return ('%s(%r, element=%r, property=%r)' %
                (self.__class__.__name__, self.message,
                 self.element, self.property))


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


merge = merge_ply
concatenate = merge_ply
merge_batches = merge_ply
combine_ply = merge_ply
merge_ply_files = merge_ply

MergeError = PlyMergeError
MergeConflictError = PlyMergeConflict
