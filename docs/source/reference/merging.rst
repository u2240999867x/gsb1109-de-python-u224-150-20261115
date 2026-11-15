Merging NRRD blocks
===================

Spatially tiled blocks that cover one volume can be stitched back together without resampling. Every block must
share the same space directions and place its origin on the common integer voxel grid. Overlapping voxels must
agree, otherwise a conflict is reported.

.. autosummary::

    nrrd.merge_nrrd
    nrrd.merge_nrrd_files
    nrrd.NRRDMergeReport
    nrrd.NRRDBlockPlacement
    nrrd.NRRDGridError
    nrrd.NRRDConflictError

.. automodule:: nrrd
    :members: merge_nrrd, merge_nrrd_files, NRRDMergeReport, NRRDBlockPlacement, NRRDGridError, NRRDConflictError
    :undoc-members:
    :show-inheritance:
