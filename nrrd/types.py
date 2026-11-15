from typing import Any, Dict, NamedTuple, Sequence, Tuple, Union

from typing_extensions import Literal

NRRDFieldType = Literal['int', 'double', 'string', 'int list', 'double list', 'string list', 'quoted string list',
                        'int vector', 'double vector', 'int matrix', 'double matrix']

IndexOrder = Literal['F', 'C']

NRRDFieldMap = Dict[str, NRRDFieldType]
NRRDHeader = Dict[str, Any]

RegionRange = Tuple[int, int]
"""Inclusive, zero-based ``(start, stop)`` index range along one axis.

``stop`` is inclusive and may be ``-1`` to select an empty axis (e.g. ``(0, -1)``).
"""

RegionAxis = Union[RegionRange, slice, range, int, Sequence[int]]
"""Specification of the indices to select along one axis.

Supported forms are an inclusive ``(start, stop)`` range, a :class:`slice` (with ``step == 1``), a single
non-negative integer, or a sequence of non-negative integer indices. Slices and sequences must be contiguous
ranges; non-contiguous selections are rejected rather than silently materialized.
"""

Region = Sequence[RegionAxis]
"""One :data:`RegionAxis` per axis, expressed in the caller's ``index_order``."""


class RegionInfo(NamedTuple):
    """Description of the original data set and the region that was read from it.

    All sequences are ordered according to :attr:`index_order`, matching the region given to :func:`nrrd.read_region`
    and the returned data array.
    """

    sizes: Tuple[int, ...]
    """Sizes of the full (uncropped) data set along each axis."""

    starts: Tuple[int, ...]
    """First selected index along each axis."""

    stops: Tuple[int, ...]
    """Last selected index along each axis, inclusive. Equal to ``start - 1`` for an empty axis."""

    index_order: IndexOrder = 'F'
    """Index order in which the ranges and data are expressed."""

    full_decode: bool = False
    """Whether the encoding forced the whole data set to be decoded to produce the region."""

    @property
    def shape(self) -> Tuple[int, ...]:
        """Shape of the selected region."""

        return tuple(stop - start + 1 for start, stop in zip(self.starts, self.stops))

    def ranges(self) -> Tuple[RegionRange, ...]:
        """Inclusive ``(start, stop)`` ranges of the region along each axis."""

        return tuple(zip(self.starts, self.stops))

    def to_original(self, index: Sequence[int]) -> Tuple[int, ...]:
        """Map an index local to the region back to the original data set."""

        return tuple(start + local for start, local in zip(self.starts, index))
