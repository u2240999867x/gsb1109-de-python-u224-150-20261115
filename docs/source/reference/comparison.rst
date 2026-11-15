Comparing NRRD files
====================

.. autosummary::

    nrrd.compare
    nrrd.compare_headers
    nrrd.NRRDComparison
    nrrd.NRRDHeaderComparison

.. automodule:: nrrd
    :members: compare, compare_headers, NRRDComparison, NRRDHeaderComparison
    :undoc-members:
    :show-inheritance:

Compression, byte order and other storage details are ignored, because they are undone when the data is read. The
comparison separates four aspects:

* The coordinate definition (``space``, ``space directions``, ``space origin``, ``sizes``, ``kinds``, ...).
* The value type. NRRD type aliases such as ``short`` and ``int16`` are the same type, and the byte order is ignored.
* The sample values.
* The custom (non-standard) metadata fields.

Axes may be reordered or reversed between the two files. When ``space directions`` make it possible to match the axes,
the values are compared at matching physical points, so an F-order/C-order rearrangement of the same data is
equivalent. When the headers do not contain enough direction information, the comparison reports whether the arrays
are the same and leaves :attr:`nrrd.NRRDComparison.spatial_equivalence` as :obj:`None`; it does not claim that the
spatial data is consistent.

.. code-block:: python

    import numpy as np
    import nrrd

    data = np.zeros((4, 3), dtype=np.int16)
    header = {'sizes': np.array([3, 4]),
              'space directions': np.array([[1., 0., 0.], [0., 1., 0.]])}
    nrrd.write('a.nrrd', data, header, index_order='F')

    comparison = nrrd.compare('a.nrrd', 'a.nrrd.gz')
    print(comparison.verdict)
    >>> Datasets are equivalent: they contain the same values at the same physical grid points.
