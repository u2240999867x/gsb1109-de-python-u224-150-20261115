Measurement frame
=================

.. currentmodule:: nrrd

A vector-valued NRRD dataset stores its components along a ``vector`` axis. Those components are measured in the
basis given by the ``measurement frame`` header field. The rows of this matrix are the measurement-frame basis
vectors expressed in world (``space``) coordinates, so a vector stored as :math:`v_{m}` has world components
:math:`v_{w} = M v_{m}`.

Two distinct operations are involved when working with these components:

1. **Axis rearrangement** locates the component axis of the array and permutes array axes (e.g. via
   :func:`numpy.moveaxis` or transposition when switching between ``'F'`` and ``'C'`` index order). It never
   changes any values.
2. **Vector basis change** multiplies the components by the measurement frame (or its inverse). It combines the
   component values linearly and is only valid when the frame is an invertible matrix.

A degenerate (non-finite, non-square or singular) frame does not describe a basis, so it is rejected with an
:class:`nrrd.NRRDError` instead of being inverted. Scalar datasets, which have no ``vector`` axis, are never treated
as single-component vectors.

.. autosummary::

    nrrd.find_vector_axis
    nrrd.world_components
    nrrd.measurement_components

.. automodule:: nrrd
    :members: find_vector_axis, world_components, measurement_components
    :undoc-members:
    :show-inheritance:
