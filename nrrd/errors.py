class NRRDError(Exception):
    """Exceptions for NRRD class."""
    pass


class NRRDGridError(NRRDError):
    """Raised when NRRD blocks cannot be placed on a common voxel grid.

    This includes mismatched directions or spacings, non-spatial axes without a geometric placement and origins
    that are offset by a non-integer number of voxels (e.g. half a voxel), which cannot be stitched without
    resampling and therefore without interpolation.
    """
    pass


class NRRDConflictError(NRRDError):
    """Raised when overlapping voxels from different blocks hold different values.

    The :attr:`report` attribute contains the :class:`~nrrd.merger.NRRDMergeReport`, including the conflict mask
    and details of the first conflicting voxels.
    """

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report
