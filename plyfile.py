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
from collections.abc import Mapping as _Mapping
from itertools import islice as _islice

import numpy as _np
from sys import byteorder as _byteorder


class PlyTransformError(ValueError):
    """
    Base class for errors raised while transforming spatial data.
    """

    pass


class DegenerateTransformError(PlyTransformError):
    """
    Raised when an affine transform is singular.

    Degenerate transforms are reported separately from ordinary
    validation errors because positions can still be mapped, while
    normals, directions, and face orientation are no longer
    well-defined.
    """

    pass


class SpatialPropertyError(PlyTransformError):
    """
    Raised when a declared spatial property is invalid.
    """

    pass


class AffineTransform(object):
    """
    Three-dimensional affine transform.

    The transform consists of a linear map ``L`` and a translation
    ``t``, and acts on column vectors as ``p -> L p + t``.  It supports
    translation, non-uniform scaling, rotation, shear, and mirroring.

    Parameters
    ----------
    matrix : array_like, optional
        A 3x3 linear map, a 4x4 homogeneous matrix, or a 3x4 matrix
        whose last column is the translation.  Defaults to the identity.
    translation : array_like, optional
        Translation vector.  If given, it replaces the translation
        encoded in `matrix`.
    linear : array_like, optional
        Alternative spelling for a 3x3 `matrix`.  It is an error to
        pass both `matrix` and `linear`.
    """

    def __init__(self, matrix=None, translation=None, linear=None):
        if matrix is not None and linear is not None:
            raise ValueError("pass either matrix or linear, not both")

        if linear is not None:
            matrix = linear

        if matrix is None:
            linear_part = _np.eye(3)
        else:
            array = _np.asarray(matrix, dtype=float)
            if array.shape == (3, 3):
                linear_part = array
            elif array.shape == (4, 4):
                if not _np.allclose(array[3], [0.0, 0.0, 0.0, 1.0]):
                    raise ValueError("last row of a 4x4 transform must be "
                                     "[0, 0, 0, 1]")
                linear_part = array[:3, :3]
                if translation is None:
                    translation = array[:3, 3]
            elif array.shape == (3, 4):
                linear_part = array[:, :3]
                if translation is None:
                    translation = array[:, 3]
            else:
                raise ValueError("transform must have shape (3, 3), "
                                 "(3, 4), or (4, 4)")

        if translation is None:
            translation_part = _np.zeros(3)
        else:
            translation_part = _np.asarray(translation, dtype=float)

        if translation_part.shape != (3,):
            raise ValueError("translation must be a three-element vector")

        linear_part = _np.array(linear_part, dtype=float, copy=True)
        translation_part = _np.array(translation_part, dtype=float,
                                     copy=True)

        if not (_np.isfinite(linear_part).all() and
                _np.isfinite(translation_part).all()):
            raise ValueError("transform components must be finite")

        self._linear = linear_part
        self._translation = translation_part

    @property
    def linear(self):
        """
        The 3x3 linear part of the transform.
        """
        return self._linear.copy()

    @property
    def translation(self):
        """
        The translation vector of the transform.
        """
        return self._translation.copy()

    @property
    def matrix(self):
        """
        The 4x4 homogeneous matrix representation of the transform.
        """
        matrix = _np.eye(4)
        matrix[:3, :3] = self._linear
        matrix[:3, 3] = self._translation
        return matrix

    @property
    def determinant(self):
        """
        Determinant of the linear part.
        """
        return float(_np.linalg.det(self._linear))

    @property
    def is_mirror(self):
        """
        Whether the linear part reverses orientation.

        This uses ``slogdet`` rather than a plain determinant so the
        orientation remains detectable even when the determinant
        underflows for a very small scale.
        """
        sign, _ = _np.linalg.slogdet(self._linear)
        return bool(sign < 0)

    @property
    def is_degenerate(self):
        """
        Whether the linear part is singular.

        This tests the smallest singular value relative to the largest,
        so a small but well-conditioned uniform scale is not mistaken
        for a degenerate transform.
        """
        singular_values = _np.linalg.svd(self._linear,
                                         compute_uv=False)
        largest = singular_values[0]
        if largest == 0:
            return True
        return bool(singular_values[-1] <=
                    _np.finfo(float).eps * 3 * largest)

    @property
    def normal_matrix(self):
        """
        Matrix that maps normals, namely ``inv(L).T``.
        """
        if self.is_degenerate:
            raise DegenerateTransformError(
                "a degenerate transform has no inverse normal matrix")
        return _np.linalg.inv(self._linear).T

    @classmethod
    def from_translation(cls, translation):
        """
        Construct a pure translation.
        """
        return cls(translation=translation)

    @classmethod
    def from_scale(cls, scale):
        """
        Construct a non-uniform scale from a scalar or three values.
        """
        scale = _np.asarray(scale, dtype=float)
        if scale.shape == ():
            scale = _np.repeat(scale, 3)
        if scale.shape != (3,):
            raise ValueError("scale must be a scalar or three-element "
                             "vector")
        return cls(_np.diag(scale))

    @classmethod
    def from_mirror(cls, axis='x'):
        """
        Construct a mirror about the plane normal to `axis`.
        """
        if isinstance(axis, str):
            try:
                index = 'xyz'.index(axis.lower())
            except ValueError:
                raise ValueError("axis must be 'x', 'y', or 'z'")
        else:
            index = int(axis)
            if index not in (0, 1, 2):
                raise ValueError("axis must be 0, 1, 2, 'x', 'y', or 'z'")
        scale = _np.ones(3)
        scale[index] = -1
        return cls(_np.diag(scale))

    def compose(self, other):
        """
        Return the composition ``self @ other``.

        `other` is applied first, then `self`.
        """
        if not isinstance(other, AffineTransform):
            other = AffineTransform(other)
        return AffineTransform(
            _np.dot(self._linear, other._linear),
            _np.dot(self._linear, other._translation) + self._translation)

    def __matmul__(self, other):
        return self.compose(other)

    def transform_points(self, points):
        """
        Apply the full affine map to an array of points.
        """
        points = _np.asarray(points, dtype=float)
        if points.shape[-1:] != (3,):
            raise ValueError("points must have shape (..., 3)")
        return _np.dot(points, self._linear.T) + self._translation

    def transform_directions(self, directions):
        """
        Apply only the linear part to direction vectors.
        """
        directions = _np.asarray(directions, dtype=float)
        if directions.shape[-1:] != (3,):
            raise ValueError("directions must have shape (..., 3)")
        return _np.dot(directions, self._linear.T)

    def transform_normals(self, normals):
        """
        Apply the inverse-transpose map and renormalize normals.

        A zero normal remains zero rather than producing NaN.
        """
        normals = _np.asarray(normals, dtype=float)
        if normals.shape[-1:] != (3,):
            raise ValueError("normals must have shape (..., 3)")
        result = _np.dot(normals, self.normal_matrix.T)
        lengths = _np.linalg.norm(result, axis=-1, keepdims=True)
        return _np.divide(result, lengths, out=_np.zeros_like(result),
                          where=lengths != 0)

    def __repr__(self):
        return ('AffineTransform(%r, translation=%r)' %
                (self._linear, self._translation))


def _spatial_vector_group(value, role):
    """
    Normalize one declared three-component property group.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        if len(value) == 3 and all(isinstance(item, str) for item in value):
            return tuple(value)
        if len(value) == 1 and isinstance(value[0], str):
            return (value[0],)
    raise SpatialPropertyError(
        "%s must name three properties or one vector property" % role)


def _spatial_vector_groups(value, role):
    """
    Normalize a declaration that may contain several vector groups.
    """
    if isinstance(value, str):
        return ((value,),)
    if isinstance(value, (list, tuple)):
        if all(isinstance(item, str) for item in value):
            return (_spatial_vector_group(value, role),)
        groups = []
        for item in value:
            groups.append(_spatial_vector_group(item, role))
        return tuple(groups)
    raise SpatialPropertyError(
        "%s properties must be a property name, a group of three names, "
        "or a sequence of groups" % role)


def _spatial_is_role_mapping(spatial):
    """
    Whether a spatial mapping names roles rather than element names.
    """
    if not isinstance(spatial, _Mapping):
        return False
    aliases = {'position', 'positions', 'direction', 'directions',
               'normal', 'normals', 'face', 'faces'}
    return bool(spatial) and all(key in aliases for key in spatial)


def _merge_spatial(spatial=None, position=None, directions=None,
                   normals=None, faces=None):
    """
    Combine a spatial mapping with individual role arguments.

    Returns None when nothing was declared.
    """
    declaration = {}
    for key, value in (('position', position),
                       ('directions', directions),
                       ('normals', normals),
                       ('faces', faces)):
        if value is not None:
            declaration[key] = value

    if spatial is None:
        return declaration or None
    if declaration:
        raise SpatialPropertyError(
            "pass spatial or individual role arguments, not both")
    return spatial


def _resolve_transform(transform=None, matrix=None, translation=None,
                       linear=None):
    """
    Normalize a transform argument plus keyword spellings.
    """
    if transform is None:
        if matrix is None and linear is None and translation is None:
            raise ValueError("a transform is required")
        return AffineTransform(matrix=matrix, translation=translation,
                               linear=linear)

    if matrix is not None or translation is not None or linear is not None:
        raise ValueError("pass either a transform or matrix/translation "
                         "keywords, not both")

    if isinstance(transform, AffineTransform):
        return transform
    if isinstance(transform, _Mapping):
        unknown = set(transform) - {'matrix', 'linear', 'translation'}
        if unknown:
            raise ValueError("unknown transform keys: %s" %
                             ', '.join(sorted(unknown)))
        if 'matrix' in transform and 'linear' in transform:
            raise ValueError("pass either matrix or linear, not both")
        return AffineTransform(
            matrix=transform.get('matrix', transform.get('linear')),
            translation=transform.get('translation'))
    return AffineTransform(transform)


def _infer_spatial(data):
    """
    Infer only canonical, unambiguous structural roles from field names.

    Conventional names count as an explicit annotation for vertices:
    ``x/y/z`` or a ``position`` vector for positions, and
    ``vertex_indices`` for face winding.

    Direction and normal attributes are deliberately *not* inferred:
    the requirement is that they be explicitly annotated.  Arbitrary
    three-field groups such as ``red/green/blue`` are also never
    inferred; those require an explicit `spatial` declaration.
    """
    fields = data.dtype.fields
    if not fields:
        return None

    def has_scalar_triplet(names):
        for name in names:
            if name not in fields:
                return False
            if fields[name][0].kind not in 'iuf':
                return False
            if _np.asarray(data[name]).ndim != 1:
                return False
        return True

    def has_vector(name):
        if name not in fields:
            return False
        values = _np.asarray(data[name])
        if values.dtype.kind == 'O':
            for row in values:
                row = _np.asarray(row)
                if row.shape != (3,) or row.dtype.kind not in 'iuf':
                    return False
            return True
        if values.dtype.kind not in 'iuf':
            return False
        return values.ndim >= 2 and values.shape[-1] == 3

    result = {}

    for names in (('x', 'y', 'z'), ('X', 'Y', 'Z')):
        if has_scalar_triplet(names):
            result['position'] = names
            break
    else:
        for name in ('position', 'pos'):
            if has_vector(name):
                result['position'] = (name,)
                break

    faces = []
    for name in ('vertex_indices', 'vertex_index', 'vertex_ids'):
        if name not in fields:
            continue
        values = _np.asarray(data[name])
        if values.dtype.kind == 'O':
            for row in values:
                row = _np.asarray(row)
                if row.ndim != 1 or row.dtype.kind not in 'iu':
                    break
            else:
                faces.append(name)
        elif values.dtype.kind in 'iu' and values.ndim >= 2:
            faces.append(name)
    if faces:
        result['faces'] = tuple(faces)

    return result or None


def _normalize_spatial(spatial):
    """
    Validate and normalize a spatial-role declaration.

    The canonical result has the optional keys ``position``,
    ``directions``, ``normals``, and ``faces``.
    """
    if spatial is None:
        return None
    if not isinstance(spatial, _Mapping):
        raise SpatialPropertyError("spatial declaration must be a mapping")

    aliases = {
        'position': 'position', 'positions': 'position',
        'direction': 'directions', 'directions': 'directions',
        'normal': 'normals', 'normals': 'normals',
        'face': 'faces', 'faces': 'faces'
    }

    result = {}
    for key, value in spatial.items():
        if key not in aliases:
            raise SpatialPropertyError("unknown spatial role %r" % key)
        canonical = aliases[key]
        if canonical in result:
            raise SpatialPropertyError("duplicate spatial role %r" %
                                       canonical)
        if canonical == 'position':
            result[canonical] = (None if value is None else
                                 _spatial_vector_group(value, 'position'))
        elif canonical in ('directions', 'normals'):
            result[canonical] = (() if value is None else
                                 _spatial_vector_groups(value, canonical))
        else:
            if value is None:
                result[canonical] = ()
            elif isinstance(value, str):
                result[canonical] = (value,)
            elif isinstance(value, (list, tuple)):
                if not all(isinstance(item, str) for item in value):
                    raise SpatialPropertyError(
                        "faces must be property names")
                result[canonical] = tuple(value)
            else:
                raise SpatialPropertyError(
                    "faces must be property names")
    return result


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

    def transform(self, transform=None, *, copy=True,
                  allow_degenerate=False, spatial=None,
                  matrix=None, translation=None, linear=None):
        """
        Apply an affine transform to every element with declared
        spatial properties.

        Elements without a `spatial` declaration are reused unchanged
        (when `copy` is true) or left unchanged (in place), so that
        transform logic never guesses at which three numeric fields
        form a vector.

        Parameters
        ----------
        transform : AffineTransform, array_like, or dict
            The transform.  As a convenience, `matrix`, `translation`,
            and `linear` may be passed as keyword arguments instead.
        copy : bool, optional
            If true (the default), return a new `PlyData` and leave this
            instance unchanged.  If false, transform declared elements
            in place and return this instance.
        allow_degenerate : bool, optional
            Whether to permit a singular transform on elements that
            declare positions only.  See `PlyElement.transform`.
        spatial : dict, optional
            Mapping from element names to per-element spatial
            declarations, for elements that were not declared at
            construction time.  Elements already carrying a declaration
            keep it unless overridden here.

        Returns
        -------
        PlyData

        Raises
        ------
        DegenerateTransformError
            If the transform is singular and a declared element has
            directions, normals, or faces, or if a positions-only
            element is transformed without `allow_degenerate=True`.
        """
        transform = _resolve_transform(transform, matrix, translation,
                                       linear)

        overrides = {}
        if spatial is not None:
            if not isinstance(spatial, _Mapping):
                raise SpatialPropertyError(
                    "spatial mapping must map element names to declarations")
            if _spatial_is_role_mapping(spatial):
                raise SpatialPropertyError(
                    "PlyData.transform needs element names in its spatial "
                    "mapping")
            overrides = spatial

        known = set(element.name for element in self.elements)
        unknown = set(overrides) - known
        if unknown:
            raise SpatialPropertyError(
                "spatial mapping names unknown elements: %s" %
                ', '.join(sorted(unknown)))

        targets = []
        for element in self.elements:
            if element.name in overrides:
                declaration = _normalize_spatial(overrides[element.name])
                if declaration is None:
                    continue
                targets.append((element, declaration))
            else:
                declaration = element._declared_spatial()
                if declaration is not None:
                    targets.append((element, declaration))

        if transform.is_degenerate:
            for element, declaration in targets:
                element._validate_spatial_data(declaration)
                element._check_degenerate_declaration(declaration,
                                                      allow_degenerate)

        # Compute every element before committing anything, so that an
        # in-place transform cannot leave a partially transformed file.
        transformed = [
            element.transform(transform, copy=True,
                              spatial=declaration,
                              allow_degenerate=allow_degenerate)
            for element, declaration in targets]

        if copy:
            replacements = dict(
                (id(element), new_element)
                for (element, _), new_element in zip(targets, transformed))
            return PlyData([replacements.get(id(element), element)
                            for element in self.elements],
                           self.text, self.byte_order,
                           self.comments, self.obj_info)

        for (element, _), new_element in zip(targets, transformed):
            element.data = new_element.data
        return self

    def apply_transform(self, transform=None, *, copy=True,
                        allow_degenerate=False, spatial=None, matrix=None,
                        translation=None, linear=None):
        """
        Alias for `transform`.
        """
        return self.transform(transform, copy=copy,
                              allow_degenerate=allow_degenerate,
                              spatial=spatial,
                              matrix=matrix, translation=translation,
                              linear=linear)


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

    def __init__(self, name, properties, count, comments=[], spatial=None):
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
        spatial : dict, optional
            Spatial roles of properties.  See `PlyElement.describe`.
        """
        _check_name(name)
        self._name = str(name)
        self._count = count

        self._properties = tuple(properties)
        self._index()

        self.comments = comments
        self._set_spatial(spatial)

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
        if self._spatial is not None:
            self._validate_spatial_data(self._spatial)

    def _get_properties(self):
        return self._properties

    def _set_properties(self, properties):
        self._properties = tuple(properties)
        self._check_sanity()
        self._index()

    properties = property(_get_properties, _set_properties)

    def _get_spatial(self):
        if self._spatial is None:
            return None
        result = {}
        for key, value in self._spatial.items():
            if key == 'position':
                result[key] = None if value is None else tuple(value)
            elif key in ('directions', 'normals'):
                result[key] = tuple(tuple(group) for group in value)
            else:
                result[key] = tuple(value)
        return result

    def _set_spatial(self, spatial):
        normalized = _normalize_spatial(spatial)
        if hasattr(self, '_data'):
            self._validate_spatial_data(normalized)
        self._spatial = normalized

    def _declared_spatial(self):
        """
        Spatial roles for transforms: the explicit declaration if one
        was made, otherwise the conservative canonical-name inference.
        """
        if self._spatial is not None:
            return self._spatial
        return _infer_spatial(self._data)

    spatial = property(_get_spatial, _set_spatial,
                       doc="Explicit spatial roles of this element's "
                           "properties.")

    def _validate_spatial_data(self, spatial):
        if spatial is None or getattr(self, '_data', None) is None:
            return
        data = self._data
        fields = data.dtype.fields
        if fields is None:
            raise SpatialPropertyError("spatial properties require a "
                                       "structured data array")

        def check_group(group, role):
            for name in group:
                if name not in fields:
                    raise SpatialPropertyError(
                        "%s property %r is not present in element %r" %
                        (role, name, self.name))

            if len(group) == 1:
                values = _np.asarray(data[group[0]])
                if values.dtype.kind == 'O':
                    for row in values:
                        row = _np.asarray(row)
                        if row.shape != (3,) or row.dtype.kind not in 'iuf':
                            raise SpatialPropertyError(
                                "%s property %r must contain three-element "
                                "numeric values" % (role, group[0]))
                elif values.ndim < 2 or values.shape[-1] != 3:
                    raise SpatialPropertyError(
                        "%s property %r must contain three-element values" %
                        (role, group[0]))
                elif values.dtype.kind not in 'iuf':
                    raise SpatialPropertyError(
                        "%s property %r must be numeric" % (role, group[0]))
            else:
                for name in group:
                    if fields[name][0].kind not in 'iuf':
                        raise SpatialPropertyError(
                            "%s property %r must be numeric" % (role, name))
                    if _np.asarray(data[name]).ndim != 1:
                        raise SpatialPropertyError(
                            "%s property %r must be a scalar field when "
                            "declared with three names" % (role, name))

        if spatial.get('position') is not None:
            check_group(spatial['position'], 'position')
        for role in ('directions', 'normals'):
            for group in spatial.get(role, ()):
                check_group(group, role)

        for name in spatial.get('faces', ()):
            if name not in fields:
                raise SpatialPropertyError(
                    "face property %r is not present in element %r" %
                    (name, self.name))
            values = _np.asarray(data[name])
            if values.dtype.kind == 'O':
                for row in values:
                    row = _np.asarray(row)
                    if row.ndim != 1 or row.dtype.kind not in 'iu':
                        raise SpatialPropertyError(
                            "face property %r must contain integer indices" %
                            name)
            elif values.dtype.kind not in 'iu' or values.ndim < 2:
                raise SpatialPropertyError(
                    "face property %r must contain integer indices" % name)

    def transform(self, transform=None, *, copy=True,
                  allow_degenerate=False, spatial=None, position=None,
                  directions=None, normals=None, faces=None,
                  matrix=None, translation=None, linear=None):
        """
        Apply an affine transform to this element's declared spatial
        properties.

        Positions are transformed by the full affine map.  Direction
        properties use the linear part.  Normal properties use the
        inverse-transpose and are renormalized.  Face properties are
        reordered to preserve their winding when the transform mirrors.

        Properties not declared in `spatial` are left untouched.  In
        particular, three numeric fields that merely look like a vector
        are never transformed automatically.

        Parameters
        ----------
        transform : AffineTransform, array_like, or dict
            The transform.  As a convenience, `matrix`, `translation`,
            and `linear` may be passed as keyword arguments instead.
        copy : bool, optional
            If true (the default), return a new element and leave this
            one unchanged.  If false, modify this element in place.
        allow_degenerate : bool, optional
            Whether to permit a singular transform on an element that
            declares positions only.  By default degenerate transforms
            are rejected even when the result is technically
            computable, so they cannot silently collapse geometry.
        spatial : dict, optional
            Spatial roles to use for this call instead of the element's
            stored declaration.  The individual `position`,
            `directions`, `normals`, and `faces` keywords may be used
            instead of a single mapping.

        Returns
        -------
        PlyElement

        Raises
        ------
        DegenerateTransformError
            If the transform is singular and `allow_degenerate` is
            false, or if directions, normals, or faces are declared.
        SpatialPropertyError
            If this element has no spatial declaration.
        """
        transform = _resolve_transform(transform, matrix, translation,
                                       linear)

        override = _merge_spatial(spatial, position, directions, normals,
                                  faces)
        if override is None:
            declaration = self._declared_spatial()
        else:
            declaration = _normalize_spatial(override)

        if declaration is None:
            raise SpatialPropertyError(
                "element %r has no declared spatial properties" % self.name)
        self._validate_spatial_data(declaration)

        if transform.is_degenerate:
            self._check_degenerate_declaration(declaration,
                                               allow_degenerate)

        source = self._data
        data = _np.array(source, dtype=source.dtype, copy=True)
        if source.dtype.names:
            for name in source.dtype.names:
                if source[name].dtype.kind != 'O':
                    continue
                copied = _np.empty(len(source), dtype=object)
                for index, row in enumerate(source[name]):
                    copied[index] = _np.array(row, copy=True)
                data[name] = copied
        changed = []

        position_group = declaration.get('position')
        if position_group is not None:
            points = _vector_values(data, position_group)
            points = transform.transform_points(points)
            _set_vector_values(data, position_group, points)
            changed.append(position_group)

        for group in declaration.get('directions', ()):
            values = _vector_values(data, group)
            values = transform.transform_directions(values)
            _set_vector_values(data, group, values)
            changed.append(group)

        for group in declaration.get('normals', ()):
            values = _vector_values(data, group)
            values = transform.transform_normals(values)
            _set_vector_values(data, group, values)
            changed.append(group)

        reversed_face = transform.is_mirror
        for name in declaration.get('faces', ()):
            if reversed_face:
                _reverse_face_winding(data[name])
            changed.append((name,))

        if not changed:
            raise SpatialPropertyError(
                "element %r declares no spatial properties to transform" %
                self.name)

        if copy:
            element = PlyElement(self.name, self.properties, self.count,
                                 self.comments, self.spatial)
            element.data = data
            return element

        self.data = data
        return self

    def _check_degenerate_declaration(self, declaration, allow_degenerate):
        """
        Validate a degenerate transform against a declaration.
        """
        uses_vectors = bool(declaration.get('directions') or
                            declaration.get('normals') or
                            declaration.get('faces'))
        if uses_vectors:
            raise DegenerateTransformError(
                "cannot transform directions, normals, or face orientation "
                "of element %r with a degenerate transform" % self.name)
        if not allow_degenerate:
            raise DegenerateTransformError(
                "degenerate transform would collapse element %r; pass "
                "allow_degenerate=True to map positions only" % self.name)

    def apply_transform(self, transform=None, *, copy=True,
                        allow_degenerate=False, spatial=None, position=None,
                        directions=None, normals=None, faces=None,
                        matrix=None, translation=None, linear=None):
        """
        Alias for `transform`.
        """
        return self.transform(
            transform, copy=copy, allow_degenerate=allow_degenerate,
            spatial=spatial, position=position, directions=directions,
            normals=normals, faces=faces, matrix=matrix,
            translation=translation, linear=linear)

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
                 comments=[], spatial=None):
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
        spatial : dict, optional
            Explicit declaration of spatial property roles, so that
            `transform` can act on them.  Recognized keys are
            ``'position'``, ``'directions'``, ``'normals'``, and
            ``'faces'``.  A position is three property names (or one
            vector property name); directions and normals may each hold
            several such groups; faces lists face-index property names.

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

        elt = PlyElement(name, properties, count, comments, spatial=spatial)
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


def _reverse_face_winding(faces):
    """
    Reverse the vertex order of each face in a face-index array.

    Faces may be stored as either an object array of index arrays or a
    fixed-width integer array.
    """
    if faces.dtype.kind == 'O':
        for index in range(len(faces)):
            faces[index] = faces[index][::-1]
        return

    if faces.dtype.kind not in 'iu':
        raise SpatialPropertyError(
            "face properties must contain integer indices")

    if faces.ndim > 1:
        faces[...] = faces[..., ::-1]


def _vector_values(data, group):
    """
    Read a declared vector group as an ``(n, 3)`` array.
    """
    if len(group) == 3:
        return _np.column_stack([data[name] for name in group])
    values = data[group[0]]
    if values.dtype.kind == 'O':
        if len(values) == 0:
            return _np.zeros((0, 3), dtype=float)
        return _np.asarray([_np.asarray(row, dtype=float)
                            for row in values], dtype=float)
    return _np.asarray(values, dtype=float)


def _set_vector_values(data, group, values):
    """
    Write a transformed ``(n, 3)`` array back to a vector group.
    """
    if len(group) == 3:
        for index, name in enumerate(group):
            data[name] = values[:, index]
        return

    name = group[0]
    if data[name].dtype.kind == 'O':
        for index, row in enumerate(values):
            target_dtype = data[name][index].dtype
            if target_dtype.kind in 'iu':
                data[name][index] = _np.rint(row).astype(target_dtype)
            else:
                data[name][index] = row.astype(target_dtype)
    else:
        data[name] = values


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
