from __future__ import print_function

from io import BytesIO

import numpy
import pytest

from plyfile import (PlyData, PlyElement, PlyBBoxClipError)


def make_ply(vertices, faces, text=True, vertex_dtype=None,
             face_dtype=None):
    """
    Build a PlyData mesh from plain coordinate and index sequences.
    """
    if vertex_dtype is None:
        vertex_dtype = [('x', 'f8'), ('y', 'f8'), ('z', 'f8'),
                        ('label', 'i4'), ('weight', 'f8')]
    if face_dtype is None:
        face_dtype = [('vertex_indices', 'O'), ('tag', 'i4')]

    vertex_array = numpy.array(vertices, dtype=vertex_dtype)
    face_array = numpy.array(faces, dtype=face_dtype)
    return PlyData(
        [PlyElement.describe(vertex_array, 'vertex'),
         PlyElement.describe(face_array, 'face')],
        text=text)


# A triangle straddling the unit-ish box and an edge-only crossing
# face whose vertices are all outside the box.
VERTICES = [
    (-1.0, -1.0, 0.0, 0, 1.0),
    (1.0, -1.0, 0.0, 1, 2.0),
    (1.0, 1.0, 0.0, 2, 3.0),
    (-1.0, 1.0, 0.0, 3, 4.0),
    (0.0, 0.0, -2.0, 4, 5.0),
    (0.0, 0.0, 2.0, 5, 6.0),
]
FACES = [
    ([0, 1, 2], 10),
    ([0, 2, 3], 11),
    ([4, 5, 2], 12),
]
BOX = numpy.array([[-0.5, 0.5], [-0.5, 0.5], [-0.5, 0.5]])


@pytest.fixture(scope='function')
def mesh_ply():
    return make_ply(VERTICES, FACES)


def triangle_area(a, b, c):
    return 0.5 * numpy.linalg.norm(numpy.cross(b - a, c - a))


def output_positions(ply):
    vertex = ply['vertex'].data
    return numpy.stack([vertex['x'], vertex['y'], vertex['z']],
                       axis=1)


def input_positions(ply):
    vertex = ply['vertex'].data
    return numpy.stack([vertex['x'], vertex['y'], vertex['z']],
                       axis=1)


def test_clip_keeps_geometry_inside_box(mesh_ply):
    result = mesh_ply.clip(BOX)
    positions = output_positions(result.ply)
    assert numpy.all(positions >= -0.5 - 1e-12)
    assert numpy.all(positions <= 0.5 + 1e-12)
    assert len(numpy.unique(positions, axis=0)) == len(positions)


def test_clip_indices_are_self_consistent(mesh_ply):
    result = mesh_ply.clip(BOX)
    face_data = result.ply['face'].data
    n_vertices = len(result.ply['vertex'].data)
    assert len(face_data) > 0
    for row in face_data:
        indices = numpy.asarray(row['vertex_indices'])
        assert len(indices) == 3
        assert len(set(indices.tolist())) == 3
        assert indices.min() >= 0
        assert indices.max() < n_vertices


def test_clip_retains_all_outside_crossing_face(mesh_ply):
    result = mesh_ply.clip(BOX)
    # Face 2 has all three vertices outside the box but its edge
    # between vertices 4 and 5 passes through it.
    assert 2 in result.face_map
    rows = numpy.flatnonzero(result.face_map == 2)
    for row in rows:
        assert result.ply['face'].data['tag'][row] == 12


def test_clip_face_map_points_to_existing_faces(mesh_ply):
    result = mesh_ply.clip(BOX)
    assert set(numpy.unique(result.face_map)).issubset(
        set(range(len(FACES))))


def test_clip_provenance_reconstructs_positions(mesh_ply):
    result = mesh_ply.clip(BOX)
    original = input_positions(mesh_ply)
    clipped = output_positions(result.ply)
    n_edge = 0
    n_higher_order = 0
    for k in range(len(clipped)):
        support, weights = result.trace_vertex(k)
        assert numpy.isclose(weights.sum(), 1.0)
        assert (weights >= -1e-12).all()
        reconstructed = (weights[:, None] *
                         original[support]).sum(axis=0)
        assert numpy.allclose(clipped[k], reconstructed, atol=1e-12)
        if len(support) == 2:
            n_edge += 1
            assert 0.0 < weights[1] < 1.0
        elif len(support) > 2:
            n_higher_order += 1
        else:
            assert weights[0] == 1.0
    assert n_edge > 0
    assert n_higher_order > 0


def test_clip_provenance_arrays_are_consistent(mesh_ply):
    result = mesh_ply.clip(BOX)
    n_vertices = len(result.ply['vertex'].data)
    assert len(result.provenance_indptr) == n_vertices + 1
    assert result.provenance_indptr[0] == 0
    assert result.provenance_indptr[-1] == \
        len(result.provenance_vertices) == \
        len(result.provenance_weights)
    assert (numpy.diff(result.provenance_indptr) >= 1).all()


def test_clip_discrete_labels_not_interpolated(mesh_ply):
    result = mesh_ply.clip(BOX)
    vertex_data = result.ply['vertex'].data
    for k in range(len(vertex_data)):
        support, weights = result.trace_vertex(k)
        labels = set(mesh_ply['vertex'].data['label'][support].tolist())
        assert vertex_data['label'][k] in labels
        expected_weight = (
            weights *
            mesh_ply['vertex'].data['weight'][support]).sum()
        assert numpy.isclose(vertex_data['weight'][k],
                             expected_weight, atol=1e-6)


@pytest.mark.parametrize('rule', ['first', 'last', 'majority'])
def test_clip_discrete_rules(mesh_ply, rule):
    result = mesh_ply.clip(BOX, discrete_rule=rule)
    vertex_data = result.ply['vertex'].data
    for k in range(len(vertex_data)):
        support, _ = result.trace_vertex(k)
        labels = set(mesh_ply['vertex'].data['label'][support].tolist())
        assert vertex_data['label'][k] in labels


def test_clip_first_and_last_rules_differ(mesh_ply):
    first = mesh_ply.clip(BOX, discrete_rule='first')
    last = mesh_ply.clip(BOX, discrete_rule='last')
    first_labels = first.ply['vertex'].data['label']
    last_labels = last.ply['vertex'].data['label']
    assert (first_labels != last_labels).any()


def test_clip_full_box_is_identity(mesh_ply):
    big_box = numpy.array([[-2.0, 2.0], [-2.0, 2.0], [-3.0, 3.0]])
    result = mesh_ply.clip(big_box)
    assert len(result.ply['vertex'].data) == len(VERTICES)
    assert len(result.ply['face'].data) == len(FACES)
    assert sorted(result.provenance_vertices.tolist()) == \
        list(range(len(VERTICES)))
    for k in range(len(VERTICES)):
        support, weights = result.trace_vertex(k)
        assert len(support) == 1 and weights[0] == 1.0


def test_clip_disjoint_box_is_empty_and_consistent(mesh_ply):
    far_box = numpy.array([[10.0, 11.0], [10.0, 11.0],
                           [10.0, 11.0]])
    result = mesh_ply.clip(far_box)
    assert len(result.ply['vertex'].data) == 0
    assert len(result.ply['face'].data) == 0
    assert result.provenance_indptr.tolist() == [0]
    assert result.provenance_vertices.shape == (0,)
    assert result.provenance_weights.shape == (0,)
    assert result.face_map.shape == (0,)
    buffer = BytesIO()
    result.ply.write(buffer)
    buffer.seek(0)
    reread = PlyData.read(buffer)
    assert len(reread['vertex'].data) == 0
    assert len(reread['face'].data) == 0


def test_clip_box_inside_large_face():
    vertices = [
        (-5.0, -5.0, 0.0, 0, 0.0),
        (5.0, -5.0, 0.0, 1, 1.0),
        (5.0, 5.0, 0.0, 2, 2.0),
        (-5.0, 5.0, 0.0, 3, 3.0),
    ]
    ply = make_ply(vertices, [([0, 1, 2, 3], 0)])
    result = ply.clip(numpy.array([[-1.0, 1.0], [-1.0, 1.0],
                                   [-1.0, 1.0]]))
    clipped = output_positions(result.ply)
    assert len(result.ply['face'].data) > 0
    # The clipped polygon is the full 2x2 square, area 4.
    area = 0.0
    for row in result.ply['face'].data:
        indices = numpy.asarray(row['vertex_indices'])
        area += triangle_area(*clipped[indices])
    assert numpy.isclose(area, 4.0)


def test_clip_triangulates_polygon_face():
    vertices = [
        (-2.0, 0.0, 0.0, 0, 0.0),
        (2.0, 0.0, 0.0, 1, 1.0),
        (2.0, 2.0, 0.0, 2, 2.0),
        (-2.0, 2.0, 0.0, 3, 3.0),
    ]
    ply = make_ply(vertices, [([0, 1, 2, 3], 0)])
    result = ply.clip(numpy.array([[-1.0, 1.0], [-1.0, 1.0],
                                   [-1.0, 1.0]]))
    for row in result.ply['face'].data:
        indices = numpy.asarray(row['vertex_indices'])
        assert len(indices) == 3
        assert len(set(indices.tolist())) == 3


def test_clip_boundary_through_vertices(mesh_ply):
    boundary_box = numpy.array([[0.0, 1.0], [0.0, 1.0],
                                [-1.0, 1.0]])
    result = mesh_ply.clip(boundary_box)
    positions = output_positions(result.ply)
    assert len(numpy.unique(positions, axis=0)) == len(positions)
    for row in result.ply['face'].data:
        indices = numpy.asarray(row['vertex_indices'])
        assert len(set(indices.tolist())) == 3


def test_clip_roundtrip(mesh_ply):
    result = mesh_ply.clip(BOX)
    for text, byte_order in ((True, '='), (False, '<'), (False, '>')):
        ply = PlyData(list(result.ply.elements), text=text,
                      byte_order=byte_order)
        buffer = BytesIO()
        ply.write(buffer)
        buffer.seek(0)
        reread = PlyData.read(buffer)
        assert len(reread['vertex'].data) == \
            len(result.ply['vertex'].data)
        assert len(reread['face'].data) == \
            len(result.ply['face'].data)


def test_clip_preserves_other_elements_and_comments(mesh_ply):
    extra = PlyElement.describe(
        numpy.array([(7,)], dtype=[('q', 'i4')]), 'extra')
    ply = PlyData(list(mesh_ply.elements) + [extra],
                  text=True, comments=['keep me'])
    result = ply.clip(BOX)
    assert [element.name for element in result.ply.elements] == \
        ['vertex', 'face', 'extra']
    assert result.ply.comments == ['keep me']


def test_clip_named_elements_and_index_property():
    vertex_dtype = [('x', 'f4'), ('y', 'f4'), ('z', 'f4')]
    face_dtype = [('corners', 'i4', (3,))]
    vertices = numpy.array(
        [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
         (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)], dtype=vertex_dtype)
    faces = numpy.array([([0, 1, 2],), ([0, 2, 3],)],
                        dtype=face_dtype)
    ply = PlyData(
        [PlyElement.describe(vertices, 'points'),
         PlyElement.describe(faces, 'cells')])
    result = ply.clip(
        BOX, vertex_element='points', face_element='cells',
        index_property='corners')
    assert len(result.ply['cells'].data) > 0


def test_clip_invalid_bbox_shape(mesh_ply):
    with pytest.raises(PlyBBoxClipError):
        mesh_ply.clip(numpy.zeros((2, 2)))


def test_clip_invalid_bbox_order(mesh_ply):
    with pytest.raises(PlyBBoxClipError):
        mesh_ply.clip(numpy.array([[1.0, 0.0], [0.0, 1.0],
                                   [0.0, 1.0]]))


def test_clip_invalid_rule(mesh_ply):
    with pytest.raises(PlyBBoxClipError):
        mesh_ply.clip(BOX, discrete_rule='nope')


def test_clip_missing_elements():
    ply = PlyData([PlyElement.describe(
        numpy.zeros(1, dtype=[('x', 'f8'), ('y', 'f8'),
                              ('z', 'f8')]), 'vertex')])
    with pytest.raises(PlyBBoxClipError):
        ply.clip(BOX)
    with pytest.raises(PlyBBoxClipError):
        ply.clip(BOX, face_element='missing')
    with pytest.raises(PlyBBoxClipError):
        ply.clip(BOX, vertex_element='missing')


def test_clip_missing_position_property():
    vertices = numpy.array(
        [(0.0, 0.0)], dtype=[('x', 'f8'), ('y', 'f8')])
    faces = numpy.array([([0, 0, 0],)],
                        dtype=[('vertex_indices', 'O')])
    ply = PlyData(
        [PlyElement.describe(vertices, 'vertex'),
         PlyElement.describe(faces, 'face')])
    with pytest.raises(PlyBBoxClipError):
        ply.clip(BOX)


def test_clip_index_property_must_be_list():
    vertices = numpy.array(
        [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0)],
        dtype=[('x', 'f8'), ('y', 'f8'), ('z', 'f8')])
    faces = numpy.array([(0,)], dtype=[('tag', 'i4')])
    ply = PlyData(
        [PlyElement.describe(vertices, 'vertex'),
         PlyElement.describe(faces, 'face')])
    with pytest.raises(PlyBBoxClipError):
        ply.clip(BOX, index_property='tag')


def test_clip_unknown_discrete_property(mesh_ply):
    with pytest.raises(PlyBBoxClipError):
        mesh_ply.clip(BOX, discrete_properties=['nope'])


def test_clip_position_property_cannot_be_discrete(mesh_ply):
    with pytest.raises(PlyBBoxClipError):
        mesh_ply.clip(BOX, discrete_properties=['x'])


def test_clip_out_of_range_face_index():
    vertices = numpy.array(
        [(-1.0, -1.0, 0.0, 0, 0.0), (1.0, -1.0, 0.0, 1, 1.0),
         (1.0, 1.0, 0.0, 2, 2.0)],
        dtype=[('x', 'f8'), ('y', 'f8'), ('z', 'f8'),
               ('label', 'i4'), ('weight', 'f8')])
    faces = numpy.array([([0, 1, 99], 0)],
                        dtype=[('vertex_indices', 'O'),
                               ('tag', 'i4')])
    ply = PlyData(
        [PlyElement.describe(vertices, 'vertex'),
         PlyElement.describe(faces, 'face')])
    with pytest.raises(PlyBBoxClipError):
        ply.clip(BOX)


def test_clip_example_tetrahedron():
    ply = PlyData.read('examples/tet.ply')
    result = ply.clip(
        numpy.array([[0.0, 2.0], [0.0, 2.0], [0.0, 2.0]]))
    vertex_data = result.ply['vertex'].data
    face_data = result.ply['face'].data
    indices = numpy.concatenate(
        [numpy.asarray(row) for row in
         face_data['vertex_indices'].tolist()])
    assert indices.min() >= 0
    assert indices.max() < len(vertex_data)
    original = numpy.stack(
        [ply['vertex']['x'], ply['vertex']['y'],
         ply['vertex']['z']], axis=1)
    clipped = numpy.stack(
        [vertex_data['x'], vertex_data['y'],
         vertex_data['z']], axis=1)
    for k in range(len(clipped)):
        support, weights = result.trace_vertex(k)
        reconstructed = (weights[:, None] *
                         original[support]).sum(axis=0)
        assert numpy.allclose(clipped[k], reconstructed, atol=1e-12)
