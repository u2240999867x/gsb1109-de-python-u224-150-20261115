"""Convert instrument NRRD data into a lossless, unified measurement table.

Instrument files do not agree on how they spell a missing sample.  One scanner may reserve ``0`` for a
missing value, another may use ``-1``, another may use the largest value of an unsigned integer type.
The same acquisition may also apply a per-channel scale and offset, and some channels may not be
calibrated at all.  Simply replacing every sentinel by ``NaN`` loses the distinction between a missing
sample, a reserved code and a genuine zero.  This module keeps those concepts separate:

* :attr:`MeasurementTable.values` is the canonical floating-point measurement table.  Invalid samples
  are ``NaN`` there.
* :attr:`MeasurementTable.raw_codes` is an exact copy of the source data.  Integer source files retain
  their integer dtype and their original sentinel codes.
* :attr:`MeasurementTable.valid` is a per-sample validity mask.  The more detailed
  :attr:`MeasurementTable.status` array distinguishes input ``NaN``, missing codes, reserved codes,
  out-of-range values and samples that could not be calibrated.
* :attr:`MeasurementTable.missing`, :attr:`MeasurementTable.reserved` and
  :attr:`MeasurementTable.nan` are mutually exclusive masks.  A raw zero is a real measurement
  unless the source declaration explicitly assigns a special meaning to it.
* :attr:`MeasurementTable.source_index` records the originating instrument after several tables are
  concatenated, so a unified row can always be traced back to its raw declaration.

The conversion is deliberately conservative.  When a target dtype (or an explicit target resolution)
cannot distinguish two source codes, :class:`PrecisionError` is raised instead of silently rounding
the measurement table.  The failure is reported with the target dtype, the number of affected samples
and at least one example code, so the caller can choose a wider target or a coarser source range.
"""

from dataclasses import dataclass, field
from enum import IntEnum
import math
import re
import shlex
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import numpy.typing as npt

from nrrd.errors import NRRDError

__all__ = [
    'MeasurementError', 'DeclarationError', 'CalibrationError', 'PrecisionError', 'MeasurementStatus',
    'MEASUREMENT_STATUS_LABELS', 'AxisCalibration', 'SourceDeclaration', 'MeasurementTable',
    'to_measurement_table', 'convert_to_measurement_table', 'read_measurement_table',
    'measurement_table_from_nrrd', 'declaration_from_header', 'measurement_table_from_header',
    'harmonize_measurement_tables', 'concatenate_measurement_tables'
]


class MeasurementError(NRRDError):
    """Base exception for measurement-table conversion errors."""


class DeclarationError(MeasurementError, ValueError):
    """The source declaration is malformed or internally inconsistent."""


class CalibrationError(MeasurementError, ValueError):
    """A calibration cannot be applied to the requested data axes."""


class PrecisionError(MeasurementError, ValueError):
    """The requested target precision cannot represent the source losslessly.

    The exception is intentionally raised instead of returning a rounded table.  Consumers can inspect
    :attr:`failing_count`, :attr:`target_dtype`, :attr:`target_precision` and :attr:`example` to decide
    whether a wider dtype or a coarser target resolution is acceptable.
    """

    def __init__(self, message: str, *, target_dtype: Any = None, target_precision: Optional[float] = None,
                 failing_count: Optional[int] = None, example: Any = None) -> None:
        super().__init__(message)
        self.target_dtype = target_dtype
        self.target_precision = target_precision
        self.failing_count = failing_count
        self.example = example


class MeasurementStatus(IntEnum):
    """Per-sample status of a :class:`MeasurementTable`.

    The numeric values are stable and suitable for compact storage.  Use :meth:`label` when a readable
    value is needed.
    """

    VALID = 0
    NAN = 1
    NONFINITE = 2
    MISSING = 3
    RESERVED = 4
    OUT_OF_RANGE = 5
    UNCALIBRATED = 6

    def label(self) -> str:
        """Return the lower-case name used in :attr:`MeasurementTable.status`."""

        return self.name.lower()


_STATUS_DTYPE = np.dtype('<U16')
_STATUS_NAMES = np.array([status.label() for status in MeasurementStatus], dtype=_STATUS_DTYPE)

#: Readable status labels indexed by :class:`MeasurementStatus` value.
MEASUREMENT_STATUS_LABELS = tuple(status.label() for status in MeasurementStatus)


def _status_names(codes: npt.NDArray) -> npt.NDArray:
    return _STATUS_NAMES[codes.astype(np.intp, copy=False)]


def _normalize_labels(labels: Optional[Sequence[Any]], ndim: int) -> Optional[Tuple[str, ...]]:
    if labels is None:
        return None
    if isinstance(labels, str):
        labels = _as_sequence(labels)
    values = tuple(str(label) for label in labels)
    if len(values) != ndim:
        raise DeclarationError(f'Expected {ndim} axis labels, got {len(values)}')
    return values


def _format_example(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def _values_equal(left: Any, right: Any) -> bool:
    """Equality that is safe for calibration fields that may contain arrays or None entries."""

    if left is None or right is None:
        return left is right
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        return np.array_equal(np.asarray(left, dtype=object), np.asarray(right, dtype=object))
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_values_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return (set(left) == set(right)
                and all(_values_equal(left[key], right[key]) for key in left))
    try:
        return bool(left == right)
    except (TypeError, ValueError):
        return False


def _as_sequence(value: Any) -> List[Any]:
    """Return a flat Python list for a header/scalar/list-ish value."""

    if value is None:
        return []
    if isinstance(value, np.ndarray):
        return list(value.ravel())
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if text == '' or text.lower() == 'none':
            return []
        # Header fields such as "range: (0, 4095)" and "scale: 0.1 0.2" are both common.  shlex
        # handles quoted values while the explicit delimiter normalisation handles tuples.
        text = text.strip('()[]{}')
        if '"' in text or "'" in text:
            parts = shlex.split(text)
        else:
            parts = [part for part in re.split(r'[\s,;]+', text) if part != '']
        return parts
    return [value]


def _to_float(value: Any, field_name: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise DeclarationError(f'{field_name} must contain numbers, got a boolean {value!r}')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise DeclarationError(f'{field_name} must contain numbers, got {value!r}') from None
    if math.isnan(result):
        raise DeclarationError(f'{field_name} must not contain NaN')
    return result


def _to_int(value: Any, field_name: str) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise DeclarationError(f'{field_name} must contain integers, got a boolean {value!r}')
    # Parse integers without going through float: codes such as uint64 18446744073709551615 are
    # valid sentinels but are not exactly representable as a double.
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        numeric = float(value)
        if not math.isfinite(numeric) or numeric != math.trunc(numeric):
            raise DeclarationError(f'{field_name} must contain integers, got {value!r}')
        return int(numeric)
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        raise DeclarationError(f'{field_name} must contain integers, got {value!r}') from None


def _normalize_codes(value: Optional[Union[int, str, Iterable[Any]]], field_name: str) -> Tuple[int, ...]:
    values = _as_sequence(value)
    return tuple(_to_int(item, field_name) for item in values)


def _normalize_range(value: Any, field_name: str, *, integer: bool) -> Optional[Tuple[Any, Any]]:
    if value is None:
        return None
    parts = _as_sequence(value)
    if len(parts) != 2:
        raise DeclarationError(f'{field_name} must contain exactly two values, got {value!r}')
    if parts[0] is None or parts[1] is None:
        raise DeclarationError(f'{field_name} cannot use open bounds; specify both ends or omit it')
    if integer:
        bounds = tuple(_to_int(item, field_name) for item in parts)
    else:
        bounds = tuple(_to_float(item, field_name) for item in parts)
    if bounds[0] > bounds[1]:
        raise DeclarationError(f'{field_name} lower bound {bounds[0]!r} exceeds upper bound {bounds[1]!r}')
    return bounds  # type: ignore[return-value]


def _is_unset(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return True
    return False


@dataclass(frozen=True, eq=False)
class AxisCalibration:
    """Calibration for one data axis.

    ``scale`` and ``offset`` may be a scalar or a sequence with one entry per slice along ``axis``.  A
    ``None`` entry means that the corresponding slice is explicitly *not calibrated*.  It is not
    replaced with a scalar value and it is not broadcast to the other slices; those samples receive
    :attr:`MeasurementStatus.UNCALIBRATED` and remain ``NaN`` in the canonical table.  This is useful
    for instruments that expose a calibration only for some channels of a multi-channel axis.
    """

    axis: Union[int, str, None] = None
    scale: Optional[Union[float, Sequence[Optional[float]], npt.NDArray]] = None
    offset: Optional[Union[float, Sequence[Optional[float]], npt.NDArray]] = None
    applicable: bool = True

    def __post_init__(self) -> None:
        if self.scale is None and self.offset is None:
            raise DeclarationError('AxisCalibration requires at least one of scale or offset')

    def to_dict(self) -> Dict[str, Any]:
        """Return the calibration fields as a plain dictionary."""

        return {
            'axis': self.axis,
            'scale': self.scale,
            'offset': self.offset,
            'applicable': self.applicable,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AxisCalibration):
            return NotImplemented
        return (self.axis == other.axis and self.applicable == other.applicable
                and _values_equal(self.scale, other.scale) and _values_equal(self.offset, other.offset))

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return NotImplemented if result is NotImplemented else not result

    @classmethod
    def from_value(cls, value: Any, *, axis: Union[int, str, None] = None) -> 'AxisCalibration':
        """Build a calibration from an :class:`AxisCalibration`, tuple or mapping."""

        if isinstance(value, cls):
            return cls(axis=value.axis if value.axis is not None else axis, scale=value.scale,
                       offset=value.offset, applicable=value.applicable)
        if isinstance(value, Mapping):
            data = dict(value)
            if 'axis' not in data and axis is not None:
                data['axis'] = axis
            return cls(**data)
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return cls(axis=axis, scale=value[0], offset=value[1])
        return cls(axis=axis, scale=value)


@dataclass(frozen=True, eq=False)
class SourceDeclaration:
    """Declare how one instrument's NRRD samples should be interpreted.

    Parameters
    ----------
    name : str, optional
        Source/instrument label retained in the measurement table.
    missing_codes : sequence of int, optional
        Raw integer codes that mean *missing*.  They are not interchangeable with ``NaN`` or with
        reserved codes.
    reserved_codes : sequence of int, optional
        Raw codes reserved by the instrument.  They are invalid measurements but are kept distinct from
        missing codes.
    valid_codes : sequence of int, optional
        If given, only these raw codes are valid.  Codes outside this set are marked out of range.
    code_range : (int, int), optional
        Inclusive raw-code range.  Codes outside it are invalid; missing and reserved codes take
        precedence for classification.
    physical_range : (float, float), optional
        Inclusive physical range after scaling and offset.  Values outside it are invalid and ``NaN``
        in the canonical table.
    scale, offset : scalar, sequence or ndarray, optional
        Global calibration.  A scalar applies to every sample.  A one-dimensional sequence is a
        per-channel vector along :attr:`channel_axis`; if :attr:`channel_axis` is not given, its length
        must equal the last data dimension.  A NumPy array whose shape exactly matches the data is an
        element-wise calibration.  ``None`` entries in a channel or element-wise calibration mean "not
        calibrated" and receive ``UNCALIBRATED`` status.
    channel_axis : int or str, optional
        Axis along which a vector ``scale``/``offset`` is interpreted.
    axis_calibrations : mapping, optional
        Mapping from axis index/label to :class:`AxisCalibration`, a ``(scale, offset)`` pair or a
        mapping of those fields.  Axis-specific calibration is never broadcast to an axis that was not
        declared.  Unless :attr:`allow_uncalibrated_axes` is true, every data axis must be covered when
        this mapping is used.
    allow_uncalibrated_axes : bool
        If true (the default), axes not covered by :attr:`axis_calibrations` keep identity
        calibration: they are neither calibrated with another axis' value nor marked invalid.  Set to
        false to require an explicit calibration for every data axis.
    target_dtype : dtype, optional
        Floating dtype of :attr:`MeasurementTable.values`.  Defaults to ``float64``.
    target_precision : float, optional
        Optional absolute physical resolution.  Two distinct valid source codes must remain separated
        by at least this amount in the target table; otherwise :class:`PrecisionError` is raised.
    units : str or sequence, optional
        Unit metadata retained on the table.
    labels : sequence of str, optional
        Axis labels, used to resolve string axis keys in :attr:`axis_calibrations`.
    strict_precision : bool
        If true (the default), reject any conversion that cannot be recovered from the target dtype.
        If false, the target dtype is allowed to round the values.
    metadata : mapping, optional
        Opaque metadata copied to the resulting table.
    """

    name: Optional[str] = None
    missing_codes: Sequence[int] = field(default_factory=tuple)
    reserved_codes: Sequence[int] = field(default_factory=tuple)
    valid_codes: Optional[Sequence[int]] = None
    code_range: Optional[Tuple[int, int]] = None
    physical_range: Optional[Tuple[float, float]] = None
    scale: Optional[Union[float, Sequence[Optional[float]], npt.NDArray]] = None
    offset: Optional[Union[float, Sequence[Optional[float]], npt.NDArray]] = None
    channel_axis: Optional[Union[int, str]] = None
    axis_calibrations: Mapping[Union[int, str], Any] = field(default_factory=dict)
    allow_uncalibrated_axes: bool = True
    target_dtype: Any = np.float64
    target_precision: Optional[float] = None
    units: Optional[Union[str, Sequence[str]]] = None
    labels: Optional[Sequence[str]] = None
    strict_precision: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = _normalize_codes(self.missing_codes, 'missing_codes')
        reserved = _normalize_codes(self.reserved_codes, 'reserved_codes')
        overlap = sorted(set(missing).intersection(reserved))
        if overlap:
            raise DeclarationError(f'Codes cannot be both missing and reserved: {overlap}')

        valid = None if self.valid_codes is None else _normalize_codes(self.valid_codes, 'valid_codes')
        code_range = _normalize_range(self.code_range, 'code_range', integer=True)
        physical_range = _normalize_range(self.physical_range, 'physical_range', integer=False)
        target_dtype = _validate_target_dtype(self.target_dtype)
        target_precision = self.target_precision
        if target_precision is not None:
            target_precision = _to_float(target_precision, 'target_precision')
            if target_precision <= 0:
                raise DeclarationError('target_precision must be greater than zero')

        if self.channel_axis is not None and not isinstance(self.channel_axis, (int, np.integer, str)):
            raise DeclarationError('channel_axis must be an integer axis index or an axis label')
        for boolean_name in ('allow_uncalibrated_axes', 'strict_precision'):
            if not isinstance(getattr(self, boolean_name), (bool, np.bool_)):
                raise DeclarationError(f'{boolean_name} must be a boolean')

        object.__setattr__(self, 'missing_codes', missing)
        object.__setattr__(self, 'reserved_codes', reserved)
        object.__setattr__(self, 'valid_codes', valid)
        object.__setattr__(self, 'code_range', code_range)
        object.__setattr__(self, 'physical_range', physical_range)
        object.__setattr__(self, 'target_dtype', target_dtype)
        object.__setattr__(self, 'target_precision', target_precision)
        if self.labels is not None:
            labels = _as_sequence(self.labels) if isinstance(self.labels, str) else list(self.labels)
            object.__setattr__(self, 'labels', tuple(str(label) for label in labels))
        object.__setattr__(self, 'axis_calibrations', dict(self.axis_calibrations))
        object.__setattr__(self, 'metadata', dict(self.metadata))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> 'SourceDeclaration':
        """Build a declaration from a mapping, accepting common field aliases."""

        if isinstance(values, cls):
            return values
        if not isinstance(values, Mapping):
            raise DeclarationError(f'Expected a SourceDeclaration or mapping, got {type(values).__name__}')

        aliases = {
            'missing': 'missing_codes', 'missing_code': 'missing_codes', 'missing_codes': 'missing_codes',
            'missing_values': 'missing_codes', 'missing_value': 'missing_codes', 'nodata': 'missing_codes',
            'no_data': 'missing_codes',
            'reserved': 'reserved_codes', 'reserved_code': 'reserved_codes', 'reserved_codes': 'reserved_codes',
            'reserved_values': 'reserved_codes', 'reserved_value': 'reserved_codes',
            'valid': 'valid_codes', 'valid_code': 'valid_codes', 'valid_codes': 'valid_codes',
            'valid_values': 'valid_codes',
            'range': 'physical_range', 'measurement_range': 'physical_range',
            'physical_range': 'physical_range', 'physical_minmax': 'physical_range',
            'raw_range': 'code_range', 'code_range': 'code_range', 'digital_range': 'code_range',
            'scale_factor': 'scale', 'scaling': 'scale', 'slope': 'scale', 'gain': 'scale',
            'intercept': 'offset', 'bias': 'offset',
            'precision': 'target_precision', 'resolution': 'target_precision',
            'dtype': 'target_dtype', 'target_type': 'target_dtype',
            'axis_calibration': 'axis_calibrations', 'calibrations': 'axis_calibrations',
        }
        normalized: Dict[str, Any] = {}
        for key, value in values.items():
            normalized_key = str(key).strip().lower().replace(' ', '_').replace('-', '_')
            logical = aliases.get(normalized_key, normalized_key)
            if logical in cls.__dataclass_fields__:
                normalized[logical] = value
        return cls(**normalized)

    def merged(self, override: Optional['SourceDeclaration']) -> 'SourceDeclaration':
        """Return a declaration using ``override`` for explicitly supplied fields."""

        if override is None:
            return self
        if not isinstance(override, SourceDeclaration):
            override = SourceDeclaration.from_mapping(override)

        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for name in self.__dataclass_fields__:
            value = getattr(override, name)
            if not _is_unset(value):
                data[name] = value
        return SourceDeclaration(**data)

    @classmethod
    def from_header(cls, header: Mapping[str, Any], **kwargs: Any) -> 'SourceDeclaration':
        """Alias for :func:`declaration_from_header`."""

        return declaration_from_header(header, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        """Return the declaration fields as a plain dictionary."""

        result: Dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name == 'target_dtype':
                value = np.dtype(value).name
            elif name == 'axis_calibrations':
                value = {
                    key: (calibration.to_dict() if isinstance(calibration, AxisCalibration) else calibration)
                    for key, calibration in value.items()
                }
            result[name] = value
        return result

    as_dict = to_dict

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SourceDeclaration):
            return NotImplemented
        return (self.name == other.name
                and self.missing_codes == other.missing_codes
                and self.reserved_codes == other.reserved_codes
                and self.valid_codes == other.valid_codes
                and self.code_range == other.code_range
                and self.physical_range == other.physical_range
                and _values_equal(self.scale, other.scale)
                and _values_equal(self.offset, other.offset)
                and self.channel_axis == other.channel_axis
                and _values_equal(self.axis_calibrations, other.axis_calibrations)
                and self.allow_uncalibrated_axes == other.allow_uncalibrated_axes
                and self.target_dtype == other.target_dtype
                and self.target_precision == other.target_precision
                and _values_equal(self.units, other.units)
                and _values_equal(self.labels, other.labels)
                and self.strict_precision == other.strict_precision
                and _values_equal(self.metadata, other.metadata))

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return NotImplemented if result is NotImplemented else not result

    __hash__ = None


def _validate_target_dtype(dtype: Any) -> np.dtype:
    try:
        result = np.dtype(dtype)
    except TypeError:
        raise DeclarationError(f'Invalid target dtype: {dtype!r}') from None
    if result.kind != 'f':
        raise DeclarationError(f'target_dtype must be a floating-point dtype, got {result.name!r}')
    return result


@dataclass(frozen=True, eq=False)
class MeasurementTable:
    """Canonical measurement table with raw codes and validity preserved.

    Attributes
    ----------
    values : ndarray
        Floating measurement values in physical units.  Invalid samples are ``NaN``.  A genuine raw
        zero is a valid ``0.0`` value, never confused with a missing sample.
    raw_codes : ndarray
        Exact copy of the input data, in its original dtype and array order.
    valid : ndarray of bool
        True only for samples that have a real, calibrated measurement within the declared range and
        whose conversion survives the target precision check.
    status : ndarray of str
        One readable :class:`MeasurementStatus` label per sample.
    status_code : ndarray of int8
        Compact numeric status codes.
    missing, reserved, nan, nonfinite, out_of_range, uncalibrated : ndarray of bool
        Independent masks.  ``missing`` and ``reserved`` are never inferred from a raw zero; they come
        only from explicit source declarations.
    source_index : ndarray of int, optional
        For a harmonized table, the index of each sample's input in :attr:`sources`.
    """

    values: npt.NDArray
    raw_codes: npt.NDArray
    valid: npt.NDArray
    status: npt.NDArray
    status_code: npt.NDArray
    missing: npt.NDArray
    reserved: npt.NDArray
    nan: npt.NDArray
    nonfinite: npt.NDArray
    out_of_range: npt.NDArray
    uncalibrated: npt.NDArray
    declaration: SourceDeclaration
    source: Optional[str] = None
    source_index: Optional[npt.NDArray] = None
    units: Optional[Union[str, Sequence[str]]] = None
    labels: Optional[Sequence[str]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        arrays = {
            'values': self.values, 'raw_codes': self.raw_codes, 'valid': self.valid,
            'status': self.status, 'status_code': self.status_code, 'missing': self.missing,
            'reserved': self.reserved, 'nan': self.nan, 'nonfinite': self.nonfinite,
            'out_of_range': self.out_of_range, 'uncalibrated': self.uncalibrated,
        }
        if self.source_index is not None:
            arrays['source_index'] = self.source_index
            if np.dtype(self.source_index.dtype).kind not in 'iu':
                raise MeasurementError('MeasurementTable source_index must have an integer dtype')
        shape = np.shape(self.values)
        for name, array in arrays.items():
            if np.shape(array) != shape:
                raise MeasurementError(f'MeasurementTable field {name!r} has shape {np.shape(array)!r}, '
                                       f'expected {shape!r}')
        if np.dtype(self.values.dtype).kind != 'f':
            raise MeasurementError('MeasurementTable values must use a floating-point dtype')
        for name in ('valid', 'missing', 'reserved', 'nan', 'nonfinite', 'out_of_range', 'uncalibrated'):
            if np.dtype(getattr(self, name).dtype) != np.dtype(bool):
                raise MeasurementError(f'MeasurementTable field {name!r} must have boolean dtype')
        if np.dtype(self.status_code.dtype) != np.dtype(np.int8):
            raise MeasurementError('MeasurementTable status_code must use int8 dtype')
        for name in ('metadata',):
            object.__setattr__(self, name, dict(getattr(self, name)))
        if self.labels is not None:
            labels = _as_sequence(self.labels) if isinstance(self.labels, str) else list(self.labels)
            object.__setattr__(self, 'labels', tuple(str(label) for label in labels))

    @property
    def shape(self) -> Tuple[int, ...]:
        """Shape of the measurement table."""

        return self.values.shape

    @property
    def ndim(self) -> int:
        """Number of dimensions in the measurement table."""

        return self.values.ndim

    @property
    def invalid(self) -> npt.NDArray:
        """Boolean mask that is the inverse of :attr:`valid`."""

        return ~self.valid

    # Readable aliases for the canonical field names used by callers that model a measurement table
    # as (data, raw, validity).  They are views, not copies.
    @property
    def data(self) -> npt.NDArray:
        """Alias for :attr:`values`."""

        return self.values

    @property
    def raw(self) -> npt.NDArray:
        """Alias for :attr:`raw_codes`."""

        return self.raw_codes

    @property
    def original_codes(self) -> npt.NDArray:
        """Alias for :attr:`raw_codes`."""

        return self.raw_codes

    @property
    def codes(self) -> npt.NDArray:
        """Alias for :attr:`raw_codes`."""

        return self.raw_codes

    @property
    def validity(self) -> npt.NDArray:
        """Alias for :attr:`valid`."""

        return self.valid

    @property
    def sources(self) -> Tuple[str, ...]:
        """Source labels in concatenation order.

        A single-source table has one label; a harmonized table keeps one label per input and
        :attr:`source_index` identifies the source of each sample.
        """

        labels = self.metadata.get('source_labels')
        if labels is None:
            return (self.source,) if self.source is not None else ()
        return tuple(str(label) for label in labels)

    @property
    def missing_mask(self) -> npt.NDArray:
        """Alias for :attr:`missing`."""

        return self.missing

    @property
    def reserved_mask(self) -> npt.NDArray:
        """Alias for :attr:`reserved`."""

        return self.reserved

    def __len__(self) -> int:
        """Return the length of the first axis, matching :class:`numpy.ndarray`."""

        return len(self.values)

    def __repr__(self) -> str:
        return (f'MeasurementTable(shape={self.shape!r}, dtype={self.values.dtype.name!r}, '
                f'valid={int(self.valid.sum())}, invalid={int((~self.valid).sum())})')

    def to_records(self, include_invalid: bool = True) -> List[Dict[str, Any]]:
        """Return one flat record per sample.

        Records contain ``source``, ``index``, ``raw_code``, ``value``, ``valid``, ``status``,
        ``missing``, ``reserved``, ``nan``, ``nonfinite``, ``out_of_range``, ``uncalibrated`` and
        ``units``.  ``value`` is ``None`` for invalid samples so that callers do not accidentally use
        ``NaN`` as a measured value.  Set ``include_invalid`` to false to keep only real measurements.
        """

        flat_indices = np.ndindex(self.shape)
        raw_flat = self.raw_codes.ravel()
        source_index_flat = None if self.source_index is None else self.source_index.ravel()
        sources = self.sources
        values_flat = self.values.ravel()
        valid_flat = self.valid.ravel()
        status_flat = self.status.ravel()
        missing_flat = self.missing.ravel()
        reserved_flat = self.reserved.ravel()
        nan_flat = self.nan.ravel()
        nonfinite_flat = self.nonfinite.ravel()
        out_of_range_flat = self.out_of_range.ravel()
        uncalibrated_flat = self.uncalibrated.ravel()

        records: List[Dict[str, Any]] = []
        for flat_index, index in enumerate(flat_indices):
            is_valid = bool(valid_flat[flat_index])
            if not include_invalid and not is_valid:
                continue
            source = self.source
            if source_index_flat is not None and sources:
                source = sources[int(source_index_flat[flat_index])]
            records.append({
                'source': source,
                'index': tuple(int(i) for i in index),
                'raw_code': _format_example(raw_flat[flat_index]),
                'value': float(values_flat[flat_index]) if is_valid else None,
                'valid': is_valid,
                'status': str(status_flat[flat_index]),
                'missing': bool(missing_flat[flat_index]),
                'reserved': bool(reserved_flat[flat_index]),
                'nan': bool(nan_flat[flat_index]),
                'nonfinite': bool(nonfinite_flat[flat_index]),
                'out_of_range': bool(out_of_range_flat[flat_index]),
                'uncalibrated': bool(uncalibrated_flat[flat_index]),
                'units': self.units,
            })
        return records

    def as_dict(self, copy: bool = False) -> Dict[str, Any]:
        """Return the table as a dictionary of arrays and metadata.

        The returned arrays are views unless ``copy`` is true.  This is the natural interchange form
        for a unified table: canonical values, original raw codes and each validity mask stay separate
        keys so a downstream writer cannot accidentally use ``raw`` as physical values.
        """

        def _value(array: npt.NDArray) -> npt.NDArray:
            return array.copy() if copy else array

        return {
            'values': _value(self.values),
            'raw_codes': _value(self.raw_codes),
            'valid': _value(self.valid),
            'status': _value(self.status),
            'status_code': _value(self.status_code),
            'missing': _value(self.missing),
            'reserved': _value(self.reserved),
            'nan': _value(self.nan),
            'nonfinite': _value(self.nonfinite),
            'out_of_range': _value(self.out_of_range),
            'uncalibrated': _value(self.uncalibrated),
            'declaration': self.declaration,
            'source': self.source,
            'source_index': None if self.source_index is None else _value(self.source_index),
            'sources': self.sources,
            'units': self.units,
            'labels': self.labels,
            'metadata': dict(self.metadata),
        }

    def to_dict(self, copy: bool = False) -> Dict[str, Any]:
        """Alias for :meth:`as_dict`."""

        return self.as_dict(copy=copy)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MeasurementTable):
            return NotImplemented
        fields = ('values', 'raw_codes', 'valid', 'status', 'status_code', 'missing', 'reserved',
                  'nan', 'nonfinite', 'out_of_range', 'uncalibrated', 'source_index')
        for name in fields:
            left, right = getattr(self, name), getattr(other, name)
            if left is None or right is None:
                if left is not right:
                    return False
                continue
            left_array = np.asarray(left)
            right_array = np.asarray(right)
            try:
                equal = np.array_equal(left_array, right_array, equal_nan=True)
            except TypeError:
                # Object arrays (for example exact uint64 raw codes) do not support equal_nan.
                equal = np.array_equal(left_array, right_array)
            if not equal:
                return False
        return (self.declaration == other.declaration and self.source == other.source
                and _values_equal(self.units, other.units) and _values_equal(self.labels, other.labels)
                and _values_equal(self.metadata, other.metadata))

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return NotImplemented if result is NotImplemented else not result

    __hash__ = None

    def check_consistency(self) -> None:
        """Raise :class:`MeasurementError` if status and masks disagree.

        Every sample must have exactly one status category, and the boolean masks must select exactly
        the samples with that status.  This is primarily useful when a table was built manually or
        deserialized from separate arrays.
        """

        category_masks = [self.valid, self.missing, self.reserved, self.nan, self.nonfinite,
                          self.out_of_range, self.uncalibrated]
        counts = np.sum(np.stack([mask.astype(np.int8) for mask in category_masks]), axis=0)
        if np.any(counts != 1):
            raise MeasurementError('MeasurementTable status masks must select exactly one category per sample')

        expected = np.full(self.shape, MeasurementStatus.VALID.value, dtype=np.int8)
        for status, mask in ((MeasurementStatus.MISSING, self.missing),
                             (MeasurementStatus.RESERVED, self.reserved),
                             (MeasurementStatus.NAN, self.nan),
                             (MeasurementStatus.NONFINITE, self.nonfinite),
                             (MeasurementStatus.OUT_OF_RANGE, self.out_of_range),
                             (MeasurementStatus.UNCALIBRATED, self.uncalibrated)):
            expected[mask] = status.value
        if not np.array_equal(self.status_code, expected):
            raise MeasurementError('MeasurementTable status_code does not match its boolean masks')
        if not np.array_equal(self.status, _status_names(expected)):
            raise MeasurementError('MeasurementTable status labels do not match its status_code')
        if np.any(self.valid & ~np.isfinite(self.values)):
            raise MeasurementError('Valid measurement values must be finite')
        if np.any(~self.valid & ~np.isnan(self.values)):
            raise MeasurementError('Invalid measurement values must be NaN')

    @classmethod
    def concatenate(cls, tables: Sequence['MeasurementTable'], axis: int = 0,
                    source_labels: Optional[Sequence[str]] = None) -> 'MeasurementTable':
        """Concatenate compatible tables along an existing axis.

        All tables must have the same number of dimensions, the same canonical dtype, compatible
        shapes on every axis except the concatenation axis, and matching units and labels.  Each table
        may have been produced with a different source declaration (sentinel codes, ranges and
        scaling); those declarations are retained in ``metadata['source_declarations']``.
        """

        if not tables:
            raise MeasurementError('At least one measurement table is required')
        first = tables[0]
        if not isinstance(first, MeasurementTable):
            raise MeasurementError('concatenate expects MeasurementTable instances')
        if source_labels is not None and len(source_labels) != len(tables):
            raise DeclarationError('source_labels must have one entry per table')

        axis = int(axis)
        if axis < 0:
            axis += first.ndim
        if axis < 0 or axis >= first.ndim:
            raise MeasurementError(f'Concatenation axis {axis} is outside the table dimension {first.ndim}')

        for table in tables[1:]:
            if table.ndim != first.ndim:
                raise MeasurementError('All measurement tables must have the same number of dimensions')
            if table.values.dtype != first.values.dtype:
                raise MeasurementError('All measurement tables must use the same target dtype')
            for dim, (left, right) in enumerate(zip(first.shape, table.shape)):
                if dim != axis and left != right:
                    raise MeasurementError('Measurement tables have incompatible shapes: '
                                           f'{first.shape!r} versus {table.shape!r}')
            if first.units is not None and table.units is not None and table.units != first.units:
                raise MeasurementError(f'Measurement tables use different units: {first.units!r} versus '
                                       f'{table.units!r}')
            if first.labels is not None and table.labels is not None and table.labels != first.labels:
                raise CalibrationError(f'Measurement tables use different axis labels: {first.labels!r} versus '
                                       f'{table.labels!r}')

        def _cat(name: str) -> npt.NDArray:
            return np.concatenate([getattr(table, name) for table in tables], axis=axis)

        raw_dtypes = [table.raw_codes.dtype for table in tables]
        raw_arrays = [table.raw_codes for table in tables]
        if len(set(raw_dtypes)) == 1:
            raw_codes = _cat('raw_codes')
        else:
            raw_codes = _concat_raw_codes(raw_arrays, axis)
        values = _cat('values')
        status_code = _cat('status_code')
        valid = _cat('valid')

        source_indices: List[npt.NDArray] = []
        default_labels: List[str] = []
        offset = 0
        for index, table in enumerate(tables):
            if source_labels is not None:
                # The caller supplied one label per input table, so every sample in this table maps
                # to that label regardless of any nested provenance it may already carry.
                source_indices.append(np.full(table.shape, offset, dtype=np.int32))
                default_labels.append(str(source_labels[index]))
                offset += 1
                continue

            table_labels = table.sources
            if table.source_index is not None and table_labels:
                source_indices.append(table.source_index.astype(np.int32, copy=False) + offset)
                default_labels.extend(table_labels)
                offset += len(table_labels)
            else:
                source_indices.append(np.full(table.shape, offset, dtype=np.int32))
                default_labels.append(table.source if table.source is not None else f'source-{index}')
                offset += 1
        source_index = np.concatenate(source_indices, axis=axis)

        metadata = dict(first.metadata)
        metadata['concatenated_sources'] = [table.source for table in tables]
        # Each source may legitimately use a different declaration (different sentinel codes, ranges
        # or calibration).  The unified table keeps the canonical values plus every raw declaration so
        # the source semantics remain auditable instead of being flattened into one declaration.
        metadata['source_declarations'] = [table.declaration for table in tables]
        metadata['source_metadata'] = [dict(table.metadata) for table in tables]
        metadata['source_labels'] = tuple(default_labels)

        return cls(
            values=values, raw_codes=raw_codes, valid=valid, status=_status_names(status_code),
            status_code=status_code, missing=_cat('missing'), reserved=_cat('reserved'), nan=_cat('nan'),
            nonfinite=_cat('nonfinite'), out_of_range=_cat('out_of_range'),
            uncalibrated=_cat('uncalibrated'), declaration=first.declaration,
            source=first.source, source_index=source_index, units=first.units, labels=first.labels,
            metadata=metadata,
        )


def _concat_raw_codes(arrays: Sequence[npt.NDArray], axis: int) -> npt.NDArray:
    """Concatenate raw codes from sources with different dtypes without losing exact integers."""

    dtypes = [array.dtype for array in arrays]
    if len(set(dtypes)) == 1:
        return np.concatenate(arrays, axis=axis)

    if all(dtype.kind in 'iu' for dtype in dtypes):
        common = np.result_type(*dtypes)
        if common.kind in 'iu':
            promoted = [array.astype(common, copy=False) for array in arrays]
            if all(np.array_equal(original, merged) for original, merged in zip(arrays, promoted)):
                return np.concatenate(promoted, axis=axis)

    # No single numeric dtype can represent every raw code exactly (for example uint64 and int64).
    # Object arrays keep each code as an exact Python integer instead of a lossy float.
    return np.concatenate([array.astype(object) for array in arrays], axis=axis)


def _normalize_declaration(declaration: Optional[Union[SourceDeclaration, Mapping[str, Any]]]
                           ) -> SourceDeclaration:
    if declaration is None:
        return SourceDeclaration()
    if isinstance(declaration, SourceDeclaration):
        return declaration
    return SourceDeclaration.from_mapping(declaration)


def _ensure_axis(axis: Union[int, str, None], ndim: int,
                 labels: Optional[Sequence[str]]) -> int:
    if isinstance(axis, str):
        if labels is not None:
            matches = [i for i, label in enumerate(labels) if label == axis]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise CalibrationError(f'Axis label {axis!r} is not unique: {list(labels)!r}')
        try:
            axis = int(axis)
        except ValueError:
            raise CalibrationError(f'Unknown axis label: {axis!r}') from None

    if axis is None:
        raise CalibrationError('An axis calibration requires an axis index or label')

    axis = int(axis)
    if axis < 0:
        axis += ndim
    if axis < 0 or axis >= ndim:
        raise CalibrationError(f'Axis {axis} is outside the data dimension {ndim}')

    return axis


def _as_flat_array(value: Any, field_name: str) -> Optional[npt.NDArray]:
    """Return an object array for a sequence, or None when ``value`` is a scalar."""

    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return None
        return value
    if isinstance(value, (list, tuple)):
        return np.asarray(value, dtype=object)
    if isinstance(value, str):
        # A header string such as "0.1 0.2" is a vector; a bare "0.1" is a scalar.
        parts = _as_sequence(value)
        if len(parts) == 1 and not any(delimiter in value for delimiter in ' ,;()[]{}'):
            return None
        return np.asarray(parts, dtype=object)
    return None


def _axis_length_calibration(value: Any, length: int, field_name: str
                             ) -> Tuple[npt.NDArray, npt.NDArray]:
    """Return per-index values and a calibrated mask for one axis.

    A scalar applies to the whole axis.  A sequence must have exactly one entry per index along that
    axis; a ``None`` entry means that slice is explicitly uncalibrated and is not filled with a
    neighbouring value.
    """

    array = _as_flat_array(value, field_name)
    if array is None:
        scalar = _to_float(value, field_name)
        return np.full(length, scalar, dtype=np.longdouble), np.ones(length, dtype=bool)

    items = list(array.ravel())
    if len(items) != length:
        raise CalibrationError(f'{field_name} has {len(items)} entries, expected {length} along its axis')
    values = np.ones(length, dtype=np.longdouble)
    calibrated = np.ones(length, dtype=bool)
    for index, item in enumerate(items):
        if item is None or (isinstance(item, str) and item.strip().lower() == 'none'):
            calibrated[index] = False
            continue
        if isinstance(item, np.ndarray) or isinstance(item, (list, tuple)):
            raise DeclarationError(f'{field_name} must be one-dimensional')
        values[index] = _to_float(item, field_name)
    return values, calibrated


def _reshape_axis(values: npt.NDArray, axis: int, ndim: int) -> npt.NDArray:
    shape = [1] * ndim
    shape[axis] = values.shape[0]
    return values.reshape(shape)


def _code_in_range(code: int, dtype: np.dtype) -> bool:
    """Return whether an integer code is representable by an integer dtype."""

    if dtype.kind not in 'iu':
        return True
    info = np.iinfo(dtype)
    return int(info.min) <= int(code) <= int(info.max)


def _code_mask(data: npt.NDArray, codes: Iterable[int]) -> npt.NDArray:
    """Compare integer-like codes without silently wrapping them into the data dtype."""

    mask = np.zeros(data.shape, dtype=bool)
    if data.dtype.kind not in 'iu':
        # Comparing a large Python integer to a floating array can overflow during numpy's scalar
        # conversion.  Elementwise Python comparison is exact here and only used for this uncommon
        # floating-source case.
        for code in codes:
            mask |= np.fromiter((value == code for value in data.ravel()),
                                dtype=bool, count=data.size).reshape(data.shape)
        return mask

    for code in codes:
        if _code_in_range(code, data.dtype):
            mask |= (data == code)
    return mask


def _range_mask(data: npt.NDArray, bounds: Tuple[Any, Any]) -> npt.NDArray:
    low, high = bounds
    if data.dtype.kind not in 'iu':
        return np.fromiter((low <= value <= high for value in data.ravel()),
                           dtype=bool, count=data.size).reshape(data.shape)

    # For integer arrays, compare with Python integers.  numpy handles out-of-range bounds correctly
    # (an unsigned array is never below -1 and never above 256 for uint8), so no cast is needed.
    return (data >= low) & (data <= high)


def _global_calibration_map(value: Any, field_name: str, shape: Tuple[int, ...],
                            channel_axis: Optional[Union[int, str]],
                            labels: Optional[Sequence[str]]) -> Tuple[npt.NDArray, npt.NDArray]:
    """Build a full-shape map and calibrated mask for a global scalar, vector or array."""

    if isinstance(value, np.ndarray):
        if value.ndim == len(shape) and value.shape == shape:
            if value.dtype == object:
                calibrated = np.fromiter(
                    (item is not None and str(item).strip().lower() != 'none' for item in value.ravel()),
                    dtype=bool, count=value.size).reshape(shape)
                fill = 1.0 if field_name == 'scale' else 0.0
                values = np.full(shape, fill, dtype=np.longdouble)
                if np.any(calibrated):
                    try:
                        values[calibrated] = np.asarray(value[calibrated].tolist(), dtype=np.longdouble)
                    except (TypeError, ValueError):
                        raise DeclarationError(f'{field_name} must contain only numbers or None') from None
                return values, calibrated
            return value.astype(np.longdouble, copy=False), np.ones(shape, dtype=bool)
        if value.ndim > 1:
            raise CalibrationError(f'{field_name} array shape {value.shape!r} does not match data shape '
                                   f'{shape!r}')
        # A one-dimensional ndarray is accepted as a channel vector.  It is not confused with a
        # broadcastable array because its length must match the declared channel axis exactly.
        # A one-dimensional array is a per-channel calibration vector.  This must also work when the
        # declared channel axis is not the last axis and its length differs from the last axis.

    array = _as_flat_array(value, field_name)
    if array is None:
        scalar = _to_float(value, field_name)
        return np.full(shape, scalar, dtype=np.longdouble), np.ones(shape, dtype=bool)

    items = list(array.ravel())
    axis = channel_axis
    if axis is None:
        if not shape or len(items) != shape[-1]:
            raise CalibrationError(f'{field_name} is a vector but channel_axis was not specified')
        axis = len(shape) - 1
    axis_index = _ensure_axis(axis, len(shape), labels)
    if len(items) != shape[axis_index]:
        raise CalibrationError(f'{field_name} has {len(items)} entries, expected {shape[axis_index]} '
                               f'along channel axis {axis_index}')
    values, calibrated = _axis_length_calibration(items, shape[axis_index], field_name)
    return (np.broadcast_to(_reshape_axis(values, axis_index, len(shape)), shape),
            np.broadcast_to(_reshape_axis(calibrated, axis_index, len(shape)), shape))


def _build_calibration_maps(data: npt.NDArray, declaration: SourceDeclaration,
                            labels: Optional[Sequence[str]]) -> Tuple[npt.NDArray, npt.NDArray, npt.NDArray]:
    """Return (scale, offset, calibrated) maps with the same shape as ``data``.

    A global calibration is only used when it is explicitly declared.  Axis-specific calibration is
    applied in ascending axis order.  An axis without an explicit calibration keeps identity scaling;
    a value declared for one axis is never reused as a calibration for another axis.  Set the
    declaration's ``allow_uncalibrated_axes`` to false to require full axis coverage instead.
    """

    shape = data.shape
    ndim = data.ndim
    scale = np.ones(shape, dtype=np.longdouble)
    offset = np.zeros(shape, dtype=np.longdouble)
    calibrated = np.ones(shape, dtype=bool)

    has_global = declaration.scale is not None or declaration.offset is not None
    has_axis = bool(declaration.axis_calibrations)
    if has_global and has_axis:
        raise CalibrationError('Global scale/offset cannot be mixed with axis_calibrations; declare one '
                               'calibration source so an axis value is never broadcast by accident')

    if has_global:
        if declaration.scale is not None:
            scale, scale_calibrated = _global_calibration_map(
                declaration.scale, 'scale', shape, declaration.channel_axis, labels)
            calibrated &= scale_calibrated
        if declaration.offset is not None:
            offset, offset_calibrated = _global_calibration_map(
                declaration.offset, 'offset', shape, declaration.channel_axis, labels)
            calibrated &= offset_calibrated

    if has_axis:
        covered = set()
        for key, raw_calibration in sorted(declaration.axis_calibrations.items(),
                                           key=lambda item: str(item[0])):
            calibration = AxisCalibration.from_value(raw_calibration, axis=key)
            if calibration.axis is not None and calibration.axis != key:
                declared_axis = _ensure_axis(calibration.axis, ndim, labels)
                key_axis = _ensure_axis(key, ndim, labels)
                if declared_axis != key_axis:
                    raise CalibrationError(
                        f'Axis calibration keyed by {key!r} declares a different axis '
                        f'{calibration.axis!r}')
            axis_key = calibration.axis if calibration.axis is not None else key
            axis = _ensure_axis(axis_key, ndim, labels)
            if axis in covered:
                raise CalibrationError(f'Axis {axis} has more than one calibration')
            covered.add(axis)

            if not calibration.applicable:
                # A calibration explicitly marked as inapplicable does not become an identity
                # calibration and it is not reused for another axis.  Samples along this axis have
                # no valid physical interpretation and are marked uncalibrated.
                calibrated &= np.broadcast_to(
                    _reshape_axis(np.zeros(shape[axis], dtype=bool), axis, ndim), shape)
                continue

            # Each axis contributes an independent affine component:
            #     physical = raw * product(axis scales) + sum(axis offsets)
            # Composing in-place in ascending axis order therefore does not let one axis' scale
            # change another axis' offset, and the result is independent of declaration order.
            if calibration.scale is not None:
                values, mask = _axis_length_calibration(calibration.scale, shape[axis], 'scale')
                scale *= np.broadcast_to(_reshape_axis(values, axis, ndim), shape)
                calibrated &= np.broadcast_to(_reshape_axis(mask, axis, ndim), shape)
            if calibration.offset is not None:
                values, mask = _axis_length_calibration(calibration.offset, shape[axis], 'offset')
                offset += np.broadcast_to(_reshape_axis(values, axis, ndim), shape)
                calibrated &= np.broadcast_to(_reshape_axis(mask, axis, ndim), shape)

        if not declaration.allow_uncalibrated_axes:
            missing_axes = [axis for axis in range(ndim) if axis not in covered]
            if missing_axes:
                raise CalibrationError(
                    'Axis-specific calibration was given but these axes are not covered: '
                    f'{missing_axes!r}.  A per-axis calibration is never broadcast to other axes; '
                    'declare every axis or set allow_uncalibrated_axes=True to leave the other axes '
                    'unscaled.')
        # With allow_uncalibrated_axes, an uncovered axis deliberately keeps identity calibration
        # instead of borrowing a value from a declared axis.  UNCALIBRATED is used only for a slice
        # with an explicit None entry, not for a whole axis that was intentionally left alone.

    if np.any(~np.isfinite(scale)) or np.any(~np.isfinite(offset)):
        raise DeclarationError('Calibration must contain only finite values')
    if np.any(scale == 0):
        raise DeclarationError('Calibration scale must not be zero')

    return scale, offset, calibrated


def _precision_failure(raw_codes: npt.NDArray, values: npt.NDArray, mask: npt.NDArray,
                       scale: npt.NDArray, offset: npt.NDArray, target_dtype: np.dtype,
                       target_precision: Optional[float]) -> Tuple[int, Any, str]:
    """Return (failing_count, example_code, detail) for a lossless-precision check."""

    cast = values[mask].astype(target_dtype)
    if not np.all(np.isfinite(cast)):
        bad = ~np.isfinite(cast)
        example = raw_codes[mask][bad][0]
        return int(np.count_nonzero(bad)), _format_example(example), 'target dtype overflowed to a non-finite value'

    cast_high = cast.astype(np.longdouble)
    source_values = raw_codes[mask]
    original_high = values[mask]
    if raw_codes.dtype == object or np.issubdtype(raw_codes.dtype, np.integer):
        recovered = (cast_high - offset[mask]) / scale[mask]
        reconstructed = np.rint(recovered)
        # Convert each element to a Python int before comparing.  Converting the whole arrays to
        # object dtype leaves them as numpy scalars, whose comparison can silently coerce back to
        # float and hide the loss of codes above 2**53 (e.g. 9007199254740993 -> 9007199254740992.0).
        bad = np.fromiter((int(round_value) != int(raw_value)
                           for round_value, raw_value in zip(reconstructed, source_values)),
                          dtype=bool, count=reconstructed.size)
        detail = 'raw code cannot be recovered from the target value'
    else:
        # A floating source value is either representable at the target precision or it is not.
        # Comparing the round-trip cast is deliberately exact: a tolerance would silently accept a
        # value that has already lost low-order bits at the requested target dtype.
        bad = cast_high.astype(np.float64) != original_high.astype(np.float64)
        detail = 'source value cannot be represented at the target precision'

    if target_precision is not None:
        # A target resolution cannot distinguish codes whose physical values are closer than the
        # resolution.  This is a second, explicit precision budget in addition to dtype precision.
        valid_values = np.sort(np.unique(original_high))
        if valid_values.size > 1:
            separations = np.diff(valid_values)
            # ``target_precision`` is normally written as a decimal.  A separation that is equal to
            # it in decimal can differ by a few ulps after binary conversion; compare with a small
            # relative tolerance so an exact fit is not rejected as "too fine".
            tolerance = max(abs(target_precision) * 1e-12, float(np.finfo(np.longdouble).eps))
            close_indices = np.flatnonzero(separations < target_precision - tolerance)
            if close_indices.size:
                close = int(close_indices[0])
                pair = np.array([valid_values[close], valid_values[close + 1]])
                bad = bad | np.isin(original_high, pair)
                detail = (f'minimum physical separation {float(np.min(separations)):.17g} is below '
                          f'target_precision {target_precision:.17g}')

    failing = int(np.count_nonzero(bad))
    if failing:
        example_index = int(np.flatnonzero(bad)[0])
        return failing, _format_example(source_values[example_index]), detail
    return 0, None, ''


def to_measurement_table(data: npt.NDArray, declaration: Optional[Union[SourceDeclaration, Mapping[str, Any]]] = None,
                         *, labels: Optional[Sequence[str]] = None,
                         target_dtype: Any = None, target_precision: Optional[float] = None,
                         strict_precision: Optional[bool] = None, copy: bool = True,
                         metadata: Optional[Mapping[str, Any]] = None) -> MeasurementTable:
    """Convert one instrument array to a canonical measurement table.

    The raw array is classified first, then calibrated, range-checked and finally checked against the
    target precision.  Classification and calibration are independent: a raw zero remains a valid
    measurement unless the declaration explicitly lists ``0`` as missing or reserved.

    Parameters
    ----------
    data : ndarray
        Numeric source array, usually read from an NRRD file.
    declaration : SourceDeclaration or mapping
        Instrument declaration.  If omitted, identity scaling and no special codes are assumed.
    labels : sequence of str, optional
        Axis labels used to resolve string keys in ``axis_calibrations``.
    target_dtype : dtype, optional
        Override ``declaration.target_dtype``.
    target_precision : float, optional
        Override ``declaration.target_precision``.  This is an absolute physical resolution.
    strict_precision : bool, optional
        Override ``declaration.strict_precision``.  When true, a value that cannot be recovered from
        the target dtype raises :class:`PrecisionError` instead of being rounded.
    copy : bool
        Keep an independent copy of the raw source array.  The canonical table should normally own its
        raw codes; setting this to false is only useful for large read-only arrays.
    metadata : mapping, optional
        Extra metadata merged into ``declaration.metadata``.

    Returns
    -------
    table : MeasurementTable
        Canonical floating values, exact raw codes, per-sample validity and detailed status masks.
    """

    if not isinstance(data, np.ndarray):
        data = np.asarray(data)
    if data.dtype.kind == 'O':
        # Object arrays are produced when concatenating raw codes from integer dtypes that cannot share
        # one exact numpy type (for example uint64 and int64).  Accept them only if every element is a
        # real Python/numpy number, and keep the object array as raw codes while evaluating values.
        if not all(isinstance(item, (int, float, np.integer, np.floating)) for item in data.ravel()):
            raise DeclarationError('Measurement data must contain only real numbers')
        if any(isinstance(item, (bool, np.bool_)) for item in data.ravel()):
            raise DeclarationError('Measurement data must contain only real numbers')
    elif not np.issubdtype(data.dtype, np.number):
        raise DeclarationError(f'Measurement data must be numeric, got {data.dtype!r}')
    if data.dtype.kind == 'c':
        raise DeclarationError('Complex measurement data is not supported')

    declaration = _normalize_declaration(declaration)
    labels = _normalize_labels(labels if labels is not None else declaration.labels, data.ndim)
    if target_dtype is None:
        target_dtype = declaration.target_dtype
    target_dtype = _validate_target_dtype(target_dtype)
    if target_precision is None:
        target_precision = declaration.target_precision
    if strict_precision is None:
        strict_precision = declaration.strict_precision

    combined_metadata: Dict[str, Any] = dict(declaration.metadata)
    if metadata:
        combined_metadata.update(metadata)

    scale_map, offset_map, calibrated = _build_calibration_maps(data, declaration, labels)

    shape = data.shape
    status = np.full(shape, MeasurementStatus.VALID, dtype=np.int8)

    if np.issubdtype(data.dtype, np.floating):
        nan_mask = np.isnan(data)
        nonfinite_mask = np.isinf(data)
    else:
        nan_mask = np.zeros(shape, dtype=bool)
        nonfinite_mask = np.zeros(shape, dtype=bool)

    missing_mask = np.zeros(shape, dtype=bool)
    reserved_mask = np.zeros(shape, dtype=bool)
    out_of_range_mask = np.zeros(shape, dtype=bool)
    uncalibrated_mask = ~calibrated

    if declaration.missing_codes:
        missing_mask = _code_mask(data, declaration.missing_codes)
    if declaration.reserved_codes:
        reserved_mask = _code_mask(data, declaration.reserved_codes)

    code_valid = np.ones(shape, dtype=bool)
    if declaration.valid_codes is not None:
        code_valid &= _code_mask(data, declaration.valid_codes)
    if declaration.code_range is not None:
        code_valid &= _range_mask(data, declaration.code_range)
    out_of_range_mask = ~code_valid

    # Precedence: NaN/infinity describe the source value itself; explicit sentinel declarations are
    # next; then numeric range; then a source physically outside the calibrated range.  A sample gets
    # exactly one status so counts never double-count an invalid value.
    status[nan_mask] = MeasurementStatus.NAN
    status[nonfinite_mask & ~nan_mask] = MeasurementStatus.NONFINITE
    status[missing_mask & ~nan_mask & ~nonfinite_mask] = MeasurementStatus.MISSING
    status[reserved_mask & ~nan_mask & ~nonfinite_mask & ~missing_mask] = MeasurementStatus.RESERVED
    status[out_of_range_mask & ~nan_mask & ~nonfinite_mask & ~missing_mask & ~reserved_mask] = \
        MeasurementStatus.OUT_OF_RANGE
    status[uncalibrated_mask & (status == MeasurementStatus.VALID)] = MeasurementStatus.UNCALIBRATED

    # Integer sources need enough internal precision to recover every code; floating sources should be
    # evaluated at their natural (double) precision so a float32 source promoted to float64 does not
    # pretend to carry extra bits from the calibration product.
    if data.dtype.kind == 'O' or np.issubdtype(data.dtype, np.integer):
        values_high = data.astype(np.longdouble) * scale_map + offset_map
    else:
        values_high = data.astype(np.float64) * scale_map.astype(np.float64) + offset_map.astype(np.float64)
    with np.errstate(over='ignore', invalid='ignore'):
        values_high = np.asarray(values_high)

    if declaration.physical_range is not None:
        low, high = declaration.physical_range
        outside = (values_high < low) | (values_high > high)
        newly_out = outside & (status == MeasurementStatus.VALID)
        status[newly_out] = MeasurementStatus.OUT_OF_RANGE
        out_of_range_mask = out_of_range_mask | newly_out

    candidate_valid = status == MeasurementStatus.VALID
    if strict_precision and np.any(candidate_valid):
        failing, example, detail = _precision_failure(data, values_high, candidate_valid, scale_map,
                                                       offset_map, target_dtype, target_precision)
        if failing:
            raise PrecisionError(
                f'Cannot convert {failing} of {int(np.count_nonzero(candidate_valid))} valid samples '
                f'losslessly to {target_dtype.name}: {detail}; first raw code {example!r}. '
                'Use a wider target dtype or a larger target_precision.',
                target_dtype=target_dtype, target_precision=target_precision, failing_count=failing,
                example=example)

    valid = status == MeasurementStatus.VALID
    values = np.full(shape, np.nan, dtype=target_dtype)
    if np.any(valid):
        with np.errstate(over='ignore', invalid='ignore'):
            values[valid] = values_high[valid].astype(target_dtype)

    # Make the public masks mutually exclusive category flags.  A sample is exactly one of: valid,
    # NaN, infinite, missing, reserved, out of range, or uncalibrated.  Keeping the masks exclusive
    # prevents a missing sentinel that is also outside the declared code range from being counted as
    # two different kinds of invalid data.
    raw_codes = data.copy() if copy else data
    return MeasurementTable(
        values=values,
        raw_codes=raw_codes,
        valid=valid,
        status=_status_names(status),
        status_code=status,
        missing=status == MeasurementStatus.MISSING,
        reserved=status == MeasurementStatus.RESERVED,
        nan=status == MeasurementStatus.NAN,
        nonfinite=status == MeasurementStatus.NONFINITE,
        out_of_range=status == MeasurementStatus.OUT_OF_RANGE,
        uncalibrated=status == MeasurementStatus.UNCALIBRATED,
        declaration=declaration,
        source=declaration.name,
        units=declaration.units,
        labels=labels,
        metadata=combined_metadata,
    )


def convert_to_measurement_table(data: npt.NDArray,
                                 declaration: Optional[Union[SourceDeclaration, Mapping[str, Any]]] = None,
                                 **kwargs: Any) -> MeasurementTable:
    """Alias for :func:`to_measurement_table`."""

    return to_measurement_table(data, declaration, **kwargs)


_HEADER_ALIASES = {
    'missing_codes': ('missing code', 'missing codes', 'missing value', 'missing values', 'nodata', 'no data',
                      'absent value', 'absent values', 'missing', 'missing sample', 'missing samples',
                      'null value', 'null values', 'sentinel', 'sentinels'),
    'reserved_codes': ('reserved code', 'reserved codes', 'reserved value', 'reserved values', 'reserved',
                       'reserved raw', 'reserved raw code', 'reserved raw codes'),
    'valid_codes': ('valid code', 'valid codes', 'valid value', 'valid values', 'valid raw codes'),
    'code_range': ('code range', 'raw range', 'digital range', 'integer range', 'adc range', 'digital min max'),
    'physical_range': ('physical range', 'measurement range', 'valid range', 'range', 'scale range',
                       'physical min max', 'engineering range'),
    'scale': ('scale', 'scaling', 'scale factor', 'slope', 'gain'),
    'offset': ('offset', 'intercept', 'bias'),
    'target_precision': ('target precision', 'precision', 'resolution'),
    'target_dtype': ('target dtype', 'target type', 'output dtype'),
    'units': ('units', 'unit', 'sample units'),
    'labels': ('labels', 'label'),
    'channel_axis': ('channel axis', 'channel'),
}


def _header_key(key: Any) -> str:
    text = str(key).strip().lower()
    text = text.replace('(s)', '').replace('（s）', '')
    text = re.sub(r'[\s_\-]+', ' ', text)
    return text


def _header_lookup(header: Mapping[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in header.items():
        normalized[_header_key(key)] = value
    result: Dict[str, Any] = {}
    for target, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            for candidate in (_header_key(alias), _header_key(alias) + 's', _header_key(alias) + 'es'):
                if candidate in normalized:
                    result[target] = normalized[candidate]
                    break
            if target in result:
                break
    return result


def declaration_from_header(header: Mapping[str, Any], *, name: Optional[str] = None,
                            target_dtype: Any = None, target_precision: Optional[float] = None,
                            allow_uncalibrated_axes: bool = True) -> SourceDeclaration:
    """Build a :class:`SourceDeclaration` from NRRD header fields.

    Custom NRRD fields are read as strings by :func:`nrrd.read_header`, so both ``missing codes:
    65535 65534`` and ``missing codes: (65535,65534)`` work.  Recognised aliases include
    ``missing code(s)``, ``reserved code(s)``, ``valid code(s)``, ``code range``,
    ``physical range``/``measurement range``/``range``, ``scale``/``scaling``/``slope``,
    ``offset``/``intercept``, ``target precision``/``resolution``, ``units`` and ``labels``.

    Standard NRRD ``min``/``max`` fields are deliberately *not* treated as a validity range; they
    describe observed data extrema and would silently discard valid samples.  Use an explicit
    ``physical range`` field or pass :class:`SourceDeclaration` directly.
    """

    fields = _header_lookup(header)
    values: Dict[str, Any] = {'name': name, 'allow_uncalibrated_axes': allow_uncalibrated_axes}
    for key in ('missing_codes', 'reserved_codes', 'valid_codes', 'code_range', 'physical_range', 'scale',
                'offset', 'units', 'labels', 'channel_axis', 'target_dtype'):
        if key in fields:
            values[key] = fields[key]
    if target_dtype is not None:
        values['target_dtype'] = target_dtype
    if target_precision is not None:
        values['target_precision'] = target_precision
    elif 'target_precision' in fields:
        values['target_precision'] = fields['target_precision']

    # Per-axis declarations use fields such as "axis 0 scale" / "axis 1 offset".  They are optional,
    # but are parsed only when present so ordinary headers remain unaffected.
    axis_calibrations: Dict[Union[int, str], Dict[str, Any]] = {}
    for key, value in header.items():
        match = re.match(r'^\s*axis[\s_-]+([\w-]+)[\s_-]+(scale|offset)\s*$', str(key), flags=re.IGNORECASE)
        if not match:
            continue
        axis_text, field_name = match.group(1), match.group(2).lower()
        try:
            axis: Union[int, str] = int(axis_text)
        except ValueError:
            axis = axis_text
        axis_calibrations.setdefault(axis, {})[field_name] = value
    if axis_calibrations:
        values['axis_calibrations'] = axis_calibrations

    return SourceDeclaration(**values)


def _read_nrrd(filename: Union[str, Any], custom_field_map: Optional[Mapping[str, Any]],
               index_order: str) -> Tuple[npt.NDArray, Mapping[str, Any]]:
    import nrrd

    if hasattr(filename, 'read'):
        # A file object has no stable path; read the header and data from the same stream.
        header = nrrd.read_header(filename, custom_field_map=custom_field_map)
        data = nrrd.read_data(header, filename, index_order=index_order)
        return data, header

    return nrrd.read(filename, custom_field_map=custom_field_map, index_order=index_order)


def read_measurement_table(filename: Union[str, Any],
                           declaration: Optional[Union[SourceDeclaration, Mapping[str, Any]]] = None,
                           *, custom_field_map: Optional[Mapping[str, Any]] = None,
                           index_order: str = 'F', merge_header: bool = True,
                           target_dtype: Any = None, target_precision: Optional[float] = None,
                           strict_precision: Optional[bool] = None) -> MeasurementTable:
    """Read an NRRD file and convert it to a canonical measurement table.

    If ``declaration`` is omitted, the declaration is parsed from custom NRRD header fields (see
    :func:`declaration_from_header`).  If it is supplied, header fields fill only fields that the
    explicit declaration leaves unset when ``merge_header`` is true; explicit values always win.
    """

    data, header = _read_nrrd(filename, custom_field_map, index_order)
    source_name = filename if isinstance(filename, str) else getattr(filename, 'name', repr(filename))
    header_labels = header.get('labels') if isinstance(header, Mapping) else None
    labels = header_labels
    if labels is not None and index_order == 'C' and len(labels) == data.ndim:
        labels = list(labels)[::-1]

    header_declaration = declaration_from_header(header, name=str(source_name))
    if header_labels is not None and index_order == 'C' and len(header_labels) == data.ndim:
        # ``labels`` now follows the C-order data array, while header-declared axis names/indices
        # refer to the original NRRD order.  Translate the header declaration so an axis calibration
        # remains attached to the same physical axis rather than to whichever array position happens
        # to share its name.  An explicit declaration, by contrast, is interpreted in data order.
        header_positions = {str(label): index for index, label in enumerate(header_labels)}
        translated = {}
        for key, calibration in header_declaration.axis_calibrations.items():
            if isinstance(key, str) and key in header_positions:
                translated[data.ndim - 1 - header_positions[key]] = calibration
            elif isinstance(key, (int, np.integer)):
                translated[data.ndim - 1 - int(key)] = calibration
            else:
                translated[key] = calibration
        header_declaration = SourceDeclaration(
            **{**header_declaration.__dict__, 'axis_calibrations': translated})

    if declaration is None:
        effective = header_declaration
    else:
        explicit = _normalize_declaration(declaration)
        effective = header_declaration.merged(explicit) if merge_header else explicit
        if effective.name is None:
            effective = SourceDeclaration(**{**effective.__dict__, 'name': str(source_name)})

    metadata = dict(effective.metadata)
    metadata.setdefault('header', dict(header))
    metadata.setdefault('filename', str(source_name))
    return to_measurement_table(data, effective, labels=labels, target_dtype=target_dtype,
                                target_precision=target_precision, strict_precision=strict_precision,
                                metadata=metadata)


def measurement_table_from_nrrd(filename: Union[str, Any],
                                declaration: Optional[Union[SourceDeclaration, Mapping[str, Any]]] = None,
                                **kwargs: Any) -> MeasurementTable:
    """Alias for :func:`read_measurement_table`."""

    return read_measurement_table(filename, declaration, **kwargs)


def measurement_table_from_header(data: npt.NDArray, header: Mapping[str, Any],
                                  declaration: Optional[Union[SourceDeclaration, Mapping[str, Any]]] = None,
                                  *, labels: Optional[Sequence[str]] = None,
                                  target_dtype: Any = None, target_precision: Optional[float] = None,
                                  strict_precision: Optional[bool] = None) -> MeasurementTable:
    """Convert an in-memory NRRD data array using a header mapping.

    This is useful when the header and data were read separately, or when the caller already has raw
    samples from another reader.  Header-declared values are merged under the same precedence rules as
    :func:`read_measurement_table`: an explicitly supplied declaration wins, and the header fills the
    remaining fields.
    """

    # ``data`` is expected to be in the same axis order as the header's labels.  If the data has been
    # reordered, pass ``labels`` explicitly in that array's order.
    header_declaration = declaration_from_header(header)
    if declaration is None:
        effective = header_declaration
    else:
        explicit = _normalize_declaration(declaration)
        effective = header_declaration.merged(explicit)

    if labels is None and isinstance(header, Mapping):
        labels = header.get('labels')
    return to_measurement_table(data, effective, labels=labels, target_dtype=target_dtype,
                                target_precision=target_precision, strict_precision=strict_precision,
                                metadata={'header': dict(header)})


def harmonize_measurement_tables(sources: Sequence[Any],
                                 declarations: Optional[Sequence[Optional[Union[SourceDeclaration,
                                                                                Mapping[str, Any]]]]] = None,
                                 *, axis: int = 0, **read_kwargs: Any) -> MeasurementTable:
    """Read and concatenate multiple instrument NRRDs into one measurement table.

    ``sources`` may contain filenames or existing :class:`MeasurementTable` objects.  Every source is
    converted independently using its own declaration, then the canonical tables are concatenated along
    ``axis``.  Raw codes and validity masks stay aligned with every canonical value.
    """

    if declarations is not None and len(declarations) != len(sources):
        raise DeclarationError('declarations must have one entry per source')

    tables: List[MeasurementTable] = []
    for index, source in enumerate(sources):
        source_declaration = None if declarations is None else declarations[index]
        if isinstance(source, MeasurementTable):
            if source_declaration is not None:
                raise DeclarationError('A declaration cannot be applied to an existing MeasurementTable')
            tables.append(source)
        else:
            tables.append(read_measurement_table(source, source_declaration, **read_kwargs))
    return concatenate_measurement_tables(tables, axis=axis)


def concatenate_measurement_tables(tables: Sequence[MeasurementTable], axis: int = 0) -> MeasurementTable:
    """Concatenate compatible measurement tables; see :meth:`MeasurementTable.concatenate`."""

    return MeasurementTable.concatenate(tables, axis=axis)
