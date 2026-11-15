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

    def normalize_colors(self, elements=None, properties=None,
                         source_units=None, target='uchar',
                         ambiguous='error', clamp=False):
        """
        Return a copy with color properties normalized to one unit.

        Only scalar color properties are touched.  Every other element,
        property, comment, `obj_info` line, data type, field order and
        value is carried over unchanged.  Custom element and property
        names are preserved exactly.

        Parameters
        ----------
        elements : iterable of str, optional
            Element names to consider.  Defaults to all elements.
        properties : mapping or iterable of (str, str), optional
            Explicit ``(element, property)`` pairs to normalize.  By
            default, properties named ``red``, ``green``, ``blue``,
            ``alpha``, ``diffuse_*``, ``ambient_*``, ``specular_*`` or
            ``emissive_*`` are considered.  Single-letter ``r``, ``g``,
            ``b``, ``a`` are considered only when ``r``, ``g`` and ``b``
            all occur in the same element.
        source_units : str or mapping, optional
            Unit of floating-point color values.  ``'unit'`` means the
            range [0, 1]; ``'byte'`` means the range [0, 255].  This can
            be a single string, a mapping from ``(element, property)``
            pairs to units, or a nested mapping keyed by element and
            property name.  Integer properties are always interpreted as
            byte-range values.
        target : {'uchar', 'float'}, optional
            ``'uchar'`` (default) normalizes to 8-bit values in
            [0, 255].  ``'float'`` normalizes to 32-bit values in
            [0, 1].
        ambiguous : {'error', 'unit', 'byte'}, optional
            How to treat floating-point colors whose observed range is
            compatible with both units (for example, only 0.0 and 1.0).
            The default is to raise `AmbiguousColorUnitError`; pass
            ``'unit'`` or ``'byte'`` to choose for the ambiguous group.
        clamp : bool, optional
            If True, clamp out-of-range values instead of raising
            `PlyTransformError`.  The number of clamped values is
            recorded in the conversion report.

        Returns
        -------
        ColorNormalizationResult
            The normalized `PlyData` and a tuple of `ColorConversion`
            records describing every color property considered.

        Notes
        -----
        Float colors are interpreted per element: fractional values in
        (0, 1) mean unit-range, values above 1 mean byte-range, and an
        element that mixes both is rejected.  Channels that contain only
        0 and 1 are ambiguous.  All decisions, including an explicit
        ``source_units`` override, are recorded per property in
        ``conversions``.

        Raises
        ------
        AmbiguousColorUnitError
            If a floating-point unit cannot be determined safely.
        PlyTransformError
            If a color value has the wrong type or cannot be converted.
        """
        target_dtype, target_unit = _color_target(target)
        if ambiguous not in ('error', 'unit', 'byte'):
            raise ValueError("ambiguous must be 'error', 'unit' or "
                             "'byte'")
        selected = _select_color_properties(self, elements, properties)
        conversions = []
        new_elements = []

        for elt in self.elements:
            elt_props = [(prop, name) for (prop, name) in selected
                         if name == elt.name]
            if not elt_props:
                new_elements.append(_clone_element(elt))
                continue

            elt_units = _infer_element_units(
                elt, elt_props, source_units, ambiguous, clamp)

            new_props = []
            converted = {}
            for prop in elt.properties:
                pair = next(((p, n) for (p, n) in elt_props
                             if p.name == prop.name), None)
                if pair is None:
                    new_props.append(_clone_property(prop))
                    continue

                if isinstance(prop, PlyListProperty):
                    raise PlyTransformError(
                        "color property %r of element %r is a list; "
                        "only scalar colors can be normalized" %
                        (prop.name, elt.name))

                unit, unit_note = elt_units[prop.name]
                new_values, conversion = _convert_color(
                    elt.data[prop.name], prop.val_dtype, target_dtype,
                    target_unit, unit, unit_note, clamp, elt.name,
                    prop.name)
                converted[prop.name] = new_values
                conversions.append(conversion)

                val_str = _data_type_reverse[
                    _np.dtype(target_dtype).str[1:]]
                new_props.append(PlyProperty(prop.name, val_str))

            if not converted:
                new_elements.append(_clone_element(elt))
                continue

            new_data = _copy_with_fields(elt.data, converted)
            new_elt = PlyElement(elt.name, new_props, len(new_data),
                                 elt.comments)
            new_elt.data = new_data
            new_elements.append(new_elt)

        new_ply = PlyData(new_elements, text=self.text,
                          byte_order=self.byte_order,
                          comments=self.comments, obj_info=self.obj_info)
        return ColorNormalizationResult(new_ply, tuple(conversions))

    def audit_vertex_references(self, vertex_element='vertex'):
        """
        Report properties that appear to reference rows of an element.

        A list property of integer type is reported when all of its
        values are valid row indices, even if its name does not mention
        vertices; this is how unknown vendor elements are detected.
        Scalar integer properties are reported only when their names
        look like vertex references, since arbitrary small integers are
        too common to infer from data alone.

        Parameters
        ----------
        vertex_element : str, optional
            Name of the referenced element (default ``'vertex'``).

        Returns
        -------
        tuple of VertexReference
        """
        return _audit_vertex_references(self, vertex_element)

    def reindex_vertices(self, order, reference_rules=(),
                         vertex_element='vertex', strict=True):
        """
        Reorder rows of a vertex element and remap index references.

        ``order`` uses NumPy fancy-indexing semantics: row ``k`` of the
        result is row ``order[k]`` of the input.  Every property that may
        reference a vertex row must have an explicit
        `VertexReferenceRule`; otherwise this method refuses to claim
        that the file can be reordered safely.  In particular, a vendor
        element whose integer list happens to contain valid vertex
        indices is not silently rewritten.

        Only the vertex data and properties named by rules are changed.
        All other data types, field orders and values, as well as all
        comments and `obj_info` lines, are preserved.

        Parameters
        ----------
        order : sequence of int
            A permutation of ``range(len(vertices))``.
        reference_rules : iterable of VertexReferenceRule or mapping, optional
            Rules for properties that reference vertices.  A mapping of
            ``(element, property)`` pairs (or nested element mappings) to
            a target element name is also accepted.  Use
            ``VertexReferenceRule(..., ignore=True)`` to state that a
            candidate property is not a reference.
        vertex_element : str, optional
            Name of the element whose rows are reordered.
        strict : bool, optional
            If True (default), raise `UnsafeVertexReorderError` when any
            candidate reference has no rule.  If False, return a result
            with ``safe=False`` and leave those properties untouched.

        Returns
        -------
        VertexReorderResult

        Raises
        ------
        UnsafeVertexReorderError
            If a candidate reference has no explicit rule and `strict`
            is True.
        PlyTransformError
            If a rule is invalid or an index is out of range.
        ValueError
            If `order` is not a permutation.
        """
        target = self[vertex_element]
        n = len(target)
        order_arr = _validate_order(order, n)
        inverse = _inverse_order(order_arr)

        rules = _coerce_reference_rules(reference_rules)
        rule_map = {}
        for rule in rules:
            if rule.target != vertex_element:
                raise PlyTransformError(
                    "reference rule %r targets %r, but this operation "
                    "only reindexes %r" %
                    (rule, rule.target, vertex_element))
            key = (rule.element, rule.property)
            if key in rule_map:
                raise PlyTransformError("duplicate reference rule for "
                                        "%r.%r" % key)
            if rule.element not in self:
                raise PlyTransformError(
                    "reference rule names unknown element %r" %
                    rule.element)
            elt = self[rule.element]
            if rule.property not in elt:
                raise PlyTransformError(
                    "reference rule names unknown property %r on "
                    "element %r" % (rule.property, rule.element))
            if not rule.ignore:
                prop = elt.ply_property(rule.property)
                _check_rule_kind(rule, prop)
            rule_map[key] = rule

        # Conventional "face.vertex_indices" links are the only
        # references migrated without a caller-provided rule.  Any
        # vendor element, non-standard property name or unfamiliar
        # integer list needs an explicit rule.
        standard = []
        for rule in STANDARD_VERTEX_REFERENCE_RULES:
            if rule.target != vertex_element or rule.element not in self:
                continue
            elt = self[rule.element]
            if rule.property not in elt:
                continue
            prop = elt.ply_property(rule.property)
            if not isinstance(prop, PlyListProperty):
                continue
            if (rule.element, rule.property) not in rule_map:
                standard.append(rule)
        standard_keys = set((r.element, r.property) for r in standard)

        candidates = _audit_vertex_references(self, vertex_element)
        unresolved = tuple(
            c for c in candidates
            if (c.element, c.property) not in rule_map and
            (c.element, c.property) not in standard_keys)
        if unresolved and strict:
            raise UnsafeVertexReorderError(unresolved, vertex_element)

        applied = tuple(standard) + tuple(
            rule for rule in rules if not rule.ignore)

        new_elements = []
        for elt in self.elements:
            elt_rules = [r for r in applied if r.element == elt.name]
            if elt.name == vertex_element:
                new_data = elt.data[order_arr]
                if elt_rules:
                    new_data = _copy_structured(new_data)
                    for rule in elt_rules:
                        prop = elt.ply_property(rule.property)
                        new_data[rule.property] = _remap_reference(
                            elt.data[rule.property], prop, inverse, n,
                            elt.name, rule.property)
                new_elements.append(_element_with_data(elt, new_data))
                continue

            if not elt_rules:
                new_elements.append(_clone_element(elt))
                continue

            new_data = _copy_structured(elt.data)
            for rule in elt_rules:
                prop = elt.ply_property(rule.property)
                new_data[rule.property] = _remap_reference(
                    elt.data[rule.property], prop, inverse, n,
                    elt.name, rule.property)
            new_elements.append(_element_with_data(elt, new_data))

        new_ply = PlyData(new_elements, text=self.text,
                          byte_order=self.byte_order,
                          comments=self.comments, obj_info=self.obj_info)
        ignored = tuple(r for r in rules if r.ignore)
        return VertexReorderResult(
            new_ply, safe=not unresolved, applied_rules=applied,
            unresolved_references=unresolved,
            ignored_references=ignored,
            vertex_element=vertex_element)

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


class AmbiguousColorUnitError(ValueError):
    """
    Raised when floating-point colors cannot be assigned a unit safely.

    Attributes
    ----------
    element : str
    property : str
    values : tuple of float
        The observed minimum and maximum values.
    """

    def __init__(self, element, property, values):
        self.element = element
        self.property = property
        self.values = tuple(values)

        ValueError.__init__(
            self,
            "color property %r of element %r has values in [%g, %g], "
            "which is compatible with both unit and byte ranges; pass "
            "source_units explicitly" %
            (property, element, self.values[0], self.values[1]))


class PlyTransformError(ValueError):
    """
    Raised when a transformation cannot be applied safely.
    """

    pass


class UnsafeVertexReorderError(PlyTransformError):
    """
    Raised when vertex reordering has unclassified references.

    Attributes
    ----------
    unresolved : tuple of VertexReference
        Candidate references with no explicit rule.
    vertex_element : str
    """

    def __init__(self, unresolved, vertex_element):
        self.unresolved = tuple(unresolved)
        self.vertex_element = vertex_element

        details = ', '.join('%s.%s' % (r.element, r.property)
                            for r in self.unresolved)
        PlyTransformError.__init__(
            self,
            "cannot claim a safe reorder of %r: no explicit reference "
            "rule for %s" % (vertex_element, details))


class ColorConversion(object):
    """
    Provenance for one color property normalized by
    `PlyData.normalize_colors`.

    Attributes
    ----------
    element : str
    property : str
    source_dtype : str
        PLY type name of the input property (for example ``'float'``).
    target_dtype : str
        PLY type name of the output property (for example ``'uchar'``).
    source_unit : {'byte', 'unit'}
        Interpretation of the input values.
    target_unit : {'byte', 'unit'}
        Interpretation of the output values.
    scale : float
        Multiplier applied to input values.
    offset : float
        Value added after scaling.
    formula : str
        Human-readable conversion formula.
    source : str
        Why the source unit was chosen.
    clamped : int
        Number of values clamped to the target range.

    When the property is already in the target dtype and unit, `scale`
    is 1, `offset` is 0 and no values are modified.
    """

    def __init__(self, element, property, source_dtype, target_dtype,
                 source_unit, target_unit, scale, offset, formula,
                 source, clamped):
        self.element = element
        self.property = property
        self.source_dtype = source_dtype
        self.target_dtype = target_dtype
        self.source_unit = source_unit
        self.target_unit = target_unit
        self.scale = scale
        self.offset = offset
        self.formula = formula
        self.source = source
        self.clamped = clamped

    def as_dict(self):
        """
        Return the conversion provenance as a plain dictionary.
        """
        return {
            'element': self.element,
            'property': self.property,
            'source_dtype': self.source_dtype,
            'target_dtype': self.target_dtype,
            'source_unit': self.source_unit,
            'target_unit': self.target_unit,
            'scale': self.scale,
            'offset': self.offset,
            'formula': self.formula,
            'source': self.source,
            'clamped': self.clamped,
        }

    def __repr__(self):
        return ('ColorConversion(%r, %r, source_dtype=%r, '
                'target_dtype=%r, source_unit=%r, target_unit=%r, '
                'formula=%r, source=%r)' %
                (self.element, self.property, self.source_dtype,
                 self.target_dtype, self.source_unit, self.target_unit,
                 self.formula, self.source))


class ColorNormalizationResult(object):
    """
    Result of `PlyData.normalize_colors`.

    Attributes
    ----------
    plydata : PlyData
        The normalized copy.
    conversions : tuple of ColorConversion
        Per-property conversion provenance, in element/property order.
    """

    def __init__(self, plydata, conversions):
        self.plydata = plydata
        self.conversions = tuple(conversions)

    @property
    def provenance(self):
        """
        Conversion provenance as a tuple of plain dictionaries.
        """
        return tuple(c.as_dict() for c in self.conversions)

    def __repr__(self):
        return ('ColorNormalizationResult(%r, conversions=%r)' %
                (self.plydata, self.conversions))


class VertexReference(object):
    """
    A property that may reference rows of a vertex-like element.

    Attributes
    ----------
    element : str
    property : str
    reason : str
        Why the property was flagged (``'name'`` or ``'values'``).
    """

    def __init__(self, element, property, reason):
        self.element = element
        self.property = property
        self.reason = reason

    def __repr__(self):
        return ('VertexReference(%r, %r, reason=%r)' %
                (self.element, self.property, self.reason))

    def __eq__(self, other):
        return (isinstance(other, VertexReference) and
                (self.element, self.property, self.reason) ==
                (other.element, other.property, other.reason))

    def __hash__(self):
        return hash((self.element, self.property, self.reason))


class VertexReferenceRule(object):
    """
    Explicit rule for a property that references a vertex element.

    Parameters
    ----------
    element : str
    property : str
    target : str, optional
        Referenced element name (default ``'vertex'``).
    kind : {'list', 'scalar'}, optional
        Shape of the reference.  The default is inferred from the
        property.
    ignore : bool, optional
        If True, state that the property is not a vertex reference and
        must be left unchanged.
    """

    def __init__(self, element, property, target='vertex',
                 kind=None, ignore=False):
        self.element = element
        self.property = property
        self.target = target
        self.kind = kind
        self.ignore = ignore

    def __repr__(self):
        return ('VertexReferenceRule(%r, %r, target=%r, kind=%r, '
                'ignore=%r)' %
                (self.element, self.property, self.target, self.kind,
                 self.ignore))

    def __eq__(self, other):
        return (isinstance(other, VertexReferenceRule) and
                (self.element, self.property, self.target, self.kind,
                 self.ignore) ==
                (other.element, other.property, other.target, other.kind,
                 other.ignore))

    def __hash__(self):
        return hash((self.element, self.property, self.target, self.kind,
                     self.ignore))


class VertexReorderResult(object):
    """
    Result of `PlyData.reindex_vertices`.

    Attributes
    ----------
    plydata : PlyData
        The transformed copy.
    safe : bool
        True only if every candidate reference had an explicit rule.
    applied_rules : tuple of VertexReferenceRule
    unresolved_references : tuple of VertexReference
        Candidate references left untouched because `strict=False`.
    ignored_references : tuple of VertexReferenceRule
    vertex_element : str
    """

    def __init__(self, plydata, safe, applied_rules,
                 unresolved_references, ignored_references,
                 vertex_element):
        self.plydata = plydata
        self.safe = safe
        self.applied_rules = tuple(applied_rules)
        self.unresolved_references = tuple(unresolved_references)
        self.ignored_references = tuple(ignored_references)
        self.vertex_element = vertex_element

    def __repr__(self):
        return ('VertexReorderResult(safe=%r, applied_rules=%r, '
                'unresolved_references=%r, ignored_references=%r)' %
                (self.safe, self.applied_rules,
                 self.unresolved_references, self.ignored_references))


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


_color_axis_names = ('red', 'green', 'blue', 'alpha')
_color_prefixes = ('diffuse_', 'ambient_', 'specular_', 'emissive_')
_color_targets = {
    'uchar': ('u1', 'byte'),
    'float': ('f4', 'unit'),
}

STANDARD_VERTEX_REFERENCE_RULES = (
    VertexReferenceRule('face', 'vertex_indices', target='vertex',
                        kind='list'),
    VertexReferenceRule('face', 'vertex_index', target='vertex',
                        kind='list'),
)


def _color_target(target):
    if target not in _color_targets:
        raise ValueError("target must be 'uchar' or 'float'")
    return _color_targets[target]


def _is_float_type(val_dtype):
    return val_dtype in ('f4', 'f8')


def _color_family(name):
    """
    Classify a property name as a color scalar.

    Returns None for non-color names.
    """
    lowered = name.lower()
    if lowered in _color_axis_names:
        return lowered
    for prefix in _color_prefixes:
        if not lowered.startswith(prefix):
            continue
        suffix = lowered[len(prefix):]
        if suffix in _color_axis_names or suffix in ('r', 'g', 'b', 'a'):
            return prefix + suffix
    return None


def _select_color_properties(ply, elements, properties):
    """
    Resolve the set of (PlyProperty, element_name) pairs to normalize.
    """
    if elements is None:
        names = [elt.name for elt in ply.elements]
    elif isinstance(elements, str):
        names = [elements]
    else:
        names = []
        for name in elements:
            if name not in ply:
                raise ValueError("unknown element %r" % name)
            names.append(name)
    if elements is not None:
        for name in names:
            if name not in ply:
                raise ValueError("unknown element %r" % name)

    if properties is not None:
        pairs = list(properties.items() if hasattr(properties, 'items')
                     else properties)
        selected = []
        for element, name in pairs:
            if element not in ply:
                raise ValueError("unknown element %r" % element)
            if element not in names:
                continue
            if name not in ply[element]:
                raise ValueError("unknown property %r on element %r" %
                                 (name, element))
            selected.append((element, name))
    else:
        selected = []
        for name in names:
            elt = ply[name]
            candidates = []
            for prop in elt.properties:
                if _color_family(prop.name) is not None:
                    candidates.append(prop.name)
            if all(short in elt for short in ('r', 'g', 'b')):
                candidates.extend(short for short in ('r', 'g', 'b', 'a')
                                  if short in elt)
            for prop_name in candidates:
                if prop_name not in elt:
                    continue
                selected.append((name, prop_name))

    result = []
    seen = set()
    for element, name in selected:
        if (element, name) in seen:
            continue
        seen.add((element, name))
        result.append((ply[element].ply_property(name), element))
    return result


def _lookup_setting(setting, element, property):
    if setting is None:
        return None
    if isinstance(setting, str):
        return setting
    if (element, property) in setting:
        return setting[(element, property)]
    try:
        nested = setting[element]
    except (KeyError, TypeError):
        return None
    if isinstance(nested, str):
        return nested
    try:
        return nested[property]
    except (KeyError, TypeError, IndexError):
        return None


def _conversion_formula(source_unit, target_unit):
    if source_unit == 'byte' and target_unit == 'byte':
        return 'identity (byte-range to byte-range)'
    if source_unit == 'unit' and target_unit == 'byte':
        return 'value * 255, rounded to nearest integer'
    if source_unit == 'byte' and target_unit == 'unit':
        return 'value / 255'
    return 'identity (unit-range to unit-range)'


def _infer_element_units(elt, elt_props, source_units, ambiguous, clamp):
    """
    Decide the source unit for every selected color property of one
    element.

    Floating-point channels in the same element are treated as one
    unit system.  If any channel has a fractional value in (0, 1), the
    group is unit-range; if any channel exceeds 1, it is byte-range.
    Mixing both patterns is rejected rather than guessed.  Values that
    are only 0 and 1 are genuinely ambiguous.

    Returns a mapping from property name to (unit, explanation).
    """
    explicit = {}
    for prop, _ in elt_props:
        explicit[prop.name] = _lookup_setting(source_units, elt.name,
                                              prop.name)

    float_props = [prop for prop, _ in elt_props
                   if not isinstance(prop, PlyListProperty) and
                   _is_float_type(prop.val_dtype)]
    has_fraction = False
    has_above_one = False
    has_values = False
    for prop in float_props:
        if explicit.get(prop.name) is not None:
            continue
        values = _np.asarray(elt.data[prop.name], dtype='f8')
        if values.size == 0:
            continue
        has_values = True
        if _np.isnan(values).any():
            raise PlyTransformError(
                "color property %r of element %r contains NaN" %
                (prop.name, elt.name))
        if (values < 0).any():
            if not clamp:
                raise PlyTransformError(
                    "color property %r of element %r has negative "
                    "values; only unit and byte ranges are supported "
                    "(pass clamp=True to clamp them)" %
                    (prop.name, elt.name))
        if (values > 1.0).any():
            has_above_one = True
        if ((values > 0.0) & (values < 1.0)).any():
            has_fraction = True

    if has_above_one and has_fraction:
        raise PlyTransformError(
            "element %r mixes fractional color values with values above "
            "1; pass source_units explicitly" % elt.name)
    if has_above_one:
        inferred = 'byte'
        note = 'observed values above 1 in this element'
    elif has_fraction:
        inferred = 'unit'
        note = 'observed fractional values in [0, 1]'
    elif not has_values:
        inferred = 'unit'
        note = 'no values to inspect; assumed unit range'
    else:
        inferred = ambiguous
        note = ('all floating-point colors in this element are 0 and/or '
                '1, compatible with both units')

    result = {}
    for prop, _ in elt_props:
        if isinstance(prop, PlyListProperty):
            raise PlyTransformError(
                "color property %r of element %r is a list; only scalar "
                "colors can be normalized" % (prop.name, elt.name))
        if not _is_float_type(prop.val_dtype):
            result[prop.name] = (
                'byte', 'integer properties are byte-range')
            continue
        if explicit.get(prop.name) is not None:
            unit = explicit[prop.name]
            if unit not in ('byte', 'unit'):
                raise ValueError("source_units for %r.%r must be 'byte' "
                                 "or 'unit'" % (elt.name, prop.name))
            result[prop.name] = (unit, 'explicit source_units override')
            continue
        if inferred == 'error':
            values = _np.asarray(elt.data[prop.name], dtype='f8')
            raise AmbiguousColorUnitError(
                elt.name, prop.name,
                (float(values.min()) if values.size else 0.0,
                 float(values.max()) if values.size else 0.0))
        result[prop.name] = (inferred, note)
    return result


def _convert_color(values, source_dtype, target_dtype, target_unit,
                   source_unit, source_note, clamp, element, property):
    """
    Convert one scalar color array and return (array, ColorConversion).
    """
    target_byte = target_unit == 'byte'
    working = _np.asarray(values, dtype='f8')

    if source_unit == 'unit' and target_unit == 'byte':
        converted = working * 255.0
    elif source_unit == 'byte' and target_unit == 'unit':
        converted = working / 255.0
    else:
        converted = working.copy()

    low, high = ((0.0, 255.0) if target_byte else (0.0, 1.0))
    outside = (converted < low) | (converted > high)
    clamped = int(_np.count_nonzero(outside))
    if clamped:
        if not clamp:
            raise PlyTransformError(
                "color property %r of element %r would need clamping to "
                "[%g, %g]; pass clamp=True to allow it" %
                (property, element, low, high))
        converted = _np.clip(converted, low, high)

    if target_byte:
        result = _np.rint(converted).astype(_np.dtype(target_dtype))
    else:
        result = converted.astype(_np.dtype(target_dtype))

    if source_unit == 'unit' and target_unit == 'byte':
        scale, offset = 255.0, 0.0
    elif source_unit == 'byte' and target_unit == 'unit':
        scale, offset = 1.0 / 255.0, 0.0
    else:
        scale, offset = 1.0, 0.0

    conversion = ColorConversion(
        element, property, _data_type_reverse[source_dtype],
        _data_type_reverse[target_dtype], source_unit, target_unit,
        scale, offset, _conversion_formula(source_unit, target_unit),
        source_note, clamped)
    return result, conversion


def _copy_with_fields(data, replacements):
    """
    Copy a structured array, replacing selected fields by name.
    """
    fields = []
    for name in data.dtype.names:
        if name in replacements:
            fields.append((name, replacements[name].dtype))
        else:
            fields.append((name, data.dtype.fields[name][0]))
    result = _np.empty(len(data), dtype=fields)
    for name in data.dtype.names:
        if name in replacements:
            result[name] = replacements[name]
        else:
            result[name] = data[name]
    return result


def _copy_structured(data):
    result = _np.empty(len(data), dtype=data.dtype)
    result[:] = data
    return result


def _clone_property(prop):
    if isinstance(prop, PlyListProperty):
        return PlyListProperty(prop.name, prop.len_dtype, prop.val_dtype)
    return PlyProperty(prop.name, prop.val_dtype)


def _clone_properties(properties):
    return tuple(_clone_property(prop) for prop in properties)


def _clone_element(elt):
    """
    Copy an element and its array without touching values or dtypes.
    """
    new_elt = PlyElement(elt.name, _clone_properties(elt.properties),
                         elt.count, elt.comments)
    new_elt.data = _copy_structured(elt.data)
    return new_elt


def _element_with_data(elt, data):
    new_elt = PlyElement(elt.name, _clone_properties(elt.properties),
                         len(data), elt.comments)
    new_elt.data = data
    return new_elt


def _validate_order(order, n):
    order_arr = _np.asarray(order)
    if order_arr.ndim != 1:
        raise ValueError("order must be one-dimensional")
    if len(order_arr) != n:
        raise ValueError("order must contain exactly %d entries" % n)
    if not _np.issubdtype(order_arr.dtype, _np.integer):
        raise ValueError("order must contain integers")
    if not _np.array_equal(_np.sort(order_arr), _np.arange(n)):
        raise ValueError("order must be a permutation of range(%d)" % n)
    return order_arr.astype(_np.intp)


def _inverse_order(order):
    inverse = _np.empty(len(order), dtype=_np.intp)
    inverse[order] = _np.arange(len(order))
    return inverse


def _reference_name_matches(name):
    lowered = name.lower()
    patterns = ('vertex_ind', 'vertex_index', 'vertex_id',
                'vertex_ids', 'vertex_reference', 'vertex_ref',
                'vertex_index_', 'vertices', 'vertexindices',
                'source_vertex', 'target_vertex')
    if lowered in ('vertex_indices', 'vertex_index', 'vertex_ids'):
        return True
    return any(p in lowered for p in patterns)


def _audit_vertex_references(ply, vertex_element):
    if vertex_element not in ply:
        raise ValueError("unknown element %r" % vertex_element)
    n = len(ply[vertex_element])
    result = []
    for elt in ply.elements:
        for prop in elt.properties:
            if elt.name == vertex_element:
                # A vertex element can also refer to its own rows (for
                # example, a merged mesh remembering source vertices).
                # Only name-like list properties are flagged here;
                # arbitrary per-vertex integer lists are far too common.
                if (isinstance(prop, PlyListProperty) and
                        _is_integer_type(prop.val_dtype) and
                        _reference_name_matches(prop.name)):
                    result.append(VertexReference(elt.name, prop.name,
                                                  'name'))
                continue
            if isinstance(prop, PlyListProperty):
                if not _is_integer_type(prop.val_dtype):
                    continue
                if _reference_name_matches(prop.name):
                    result.append(VertexReference(elt.name, prop.name,
                                                  'name'))
                    continue
                if n and _list_values_are_indices(elt.data[prop.name], n):
                    result.append(VertexReference(elt.name, prop.name,
                                                  'values'))
            else:
                if not _is_integer_type(prop.val_dtype):
                    continue
                if _reference_name_matches(prop.name):
                    result.append(VertexReference(elt.name, prop.name,
                                                  'name'))
    return tuple(result)


def _is_integer_type(val_dtype):
    return val_dtype in ('i1', 'i2', 'i4', 'u1', 'u2', 'u4')


def _list_values_are_indices(values, n):
    for row in values:
        arr = _np.asarray(row)
        if arr.size == 0:
            continue
        if arr.dtype.kind not in 'iu':
            return False
        if arr.min() < 0 or arr.max() >= n:
            return False
    return True


def _coerce_reference_rules(rules):
    if rules is None:
        return ()
    if isinstance(rules, dict):
        result = []
        for key, value in rules.items():
            if (isinstance(key, tuple) and len(key) == 2 and
                    isinstance(value, str)):
                result.append(VertexReferenceRule(key[0], key[1],
                                                  target=value))
                continue
            if isinstance(key, str) and isinstance(value, dict):
                for prop, target in value.items():
                    result.append(VertexReferenceRule(key, prop,
                                                      target=target))
                continue
            raise ValueError("reference_rules mapping must map "
                             "(element, property) to a target element")
        return tuple(result)

    result = []
    for rule in rules:
        if isinstance(rule, VertexReferenceRule):
            result.append(rule)
        elif (isinstance(rule, tuple) and len(rule) in (2, 3)):
            result.append(VertexReferenceRule(*rule))
        else:
            raise TypeError("reference_rules must contain "
                            "VertexReferenceRule instances")
    return tuple(result)


def _check_rule_kind(rule, prop):
    if rule.kind is None:
        return
    if rule.kind not in ('list', 'scalar'):
        raise PlyTransformError("reference rule kind must be 'list', "
                                "'scalar' or None")
    actual = 'list' if isinstance(prop, PlyListProperty) else 'scalar'
    if rule.kind != actual:
        raise PlyTransformError(
            "reference rule for %r.%r says %r but the property is %r" %
            (rule.element, rule.property, rule.kind, actual))


def _remap_reference(values, prop, inverse, n, element, property):
    if isinstance(prop, PlyListProperty):
        values = _np.asarray(values)
        if values.ndim > 1:
            if values.size and (values.min() < 0 or values.max() >= n):
                raise PlyTransformError(
                    "reference %r.%r contains index outside range(%d)" %
                    (element, property, n))
            return inverse[values]

        result = _np.empty(len(values), dtype=object)
        for k, row in enumerate(values):
            arr = _np.asarray(row)
            if arr.size == 0:
                result[k] = arr
                continue
            if arr.min() < 0 or arr.max() >= n:
                raise PlyTransformError(
                    "reference %r.%r row %d contains index outside "
                    "range(%d)" % (element, property, k, n))
            result[k] = inverse[arr]
        return result

    arr = _np.asarray(values)
    if arr.size and (arr.min() < 0 or arr.max() >= n):
        raise PlyTransformError(
            "reference %r.%r contains index outside range(%d)" %
            (element, property, n))
    return inverse[arr]


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


def _main(argv=None):
    """
    Command-line color normalization with a provenance report.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog='python -m plyfile',
        description='Normalize PLY color units and record provenance.')
    parser.add_argument('input')
    parser.add_argument('output')
    parser.add_argument('--target', choices=('uchar', 'float'),
                        default='uchar')
    parser.add_argument('--source-unit', choices=('byte', 'unit'))
    parser.add_argument('--ambiguous', choices=('error', 'unit', 'byte'),
                        default='error')
    parser.add_argument('--clamp', action='store_true')
    parser.add_argument('--properties', nargs='+', metavar='ELEMENT.PROPERTY')
    parser.add_argument('--report',
                        help='write JSON provenance to this path '
                             '(default: <output>.colors.json)')
    args = parser.parse_args(argv)

    properties = None
    if args.properties:
        properties = [tuple(item.split('.', 1))
                      for item in args.properties]

    ply = PlyData.read(args.input)
    result = ply.normalize_colors(
        properties=properties, source_units=args.source_unit,
        target=args.target, ambiguous=args.ambiguous, clamp=args.clamp)
    result.plydata.write(args.output)

    report = {
        'input': args.input,
        'output': args.output,
        'target': args.target,
        'conversions': list(result.provenance),
    }
    report_path = args.report or (args.output + '.colors.json')
    with open(report_path, 'w') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
