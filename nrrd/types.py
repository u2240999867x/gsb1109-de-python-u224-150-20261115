from typing import Any, Dict, Optional, Tuple

from typing_extensions import Literal

NRRDFieldType = Literal['int', 'double', 'string', 'int list', 'double list', 'string list', 'quoted string list',
                        'int vector', 'double vector', 'int matrix', 'double matrix']

IndexOrder = Literal['F', 'C']

NRRDFieldMap = Dict[str, NRRDFieldType]
NRRDHeader = Dict[str, Any]

# Inclusive lower and exclusive upper index of a single axis. None leaves the corresponding side unbounded.
NRRDBounds = Tuple[Optional[int], Optional[int]]

# One (min, max) pair per axis. Axes follow the index order of the returned array (see index_order).
NRRDSelection = Tuple[NRRDBounds, ...]
