#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
JSON-safe representations for XSD atomic values.

Some XSD atomic values cannot be stored in a JSON document without losing
information. The most common cases are:

  * ``xs:decimal`` values: JSON numbers are usually parsed as IEEE 754
    doubles, so trailing zeroes and the original precision are lost.
  * ``xs:integer`` values: JSON numbers cannot represent integers with an
    arbitrary number of digits in many JSON implementations (JavaScript
    safely represents integers up to ``2 ** 53 - 1`` only).
  * date/time values: the timezone may be absent and must not be replaced
    by the local timezone of the machine that performs the conversion.

This module defines a small, self-describing JSON representation for these
values. A typed value is a JSON object with a reserved ``$type`` key::

    {"$type": "decimal", "value": "100.5", "lexical": "+00100.5000"}
    {"$type": "integer", "value": "123456789012345678901234567890",
     "lexical": "+00123456789012345678901234567890"}
    {"$type": "date", "value": "2001-04-15", "lexical": "2001-04-15",
     "timezone": null}
    {"$type": "dateTime", "value": "2019-01-01T13:40:00+05:30",
     "lexical": "2019-01-01T13:40:00+05:30", "timezone": "+05:30"}

The ``value`` field is a canonical representation, suitable for comparing
and associating values. For decimals the canonical value has no trailing
zeroes, so ``1.0`` and ``1.00`` have the same ``value`` even though their
``lexical`` fields are different. The ``lexical`` field always preserves the
original XSD lexical value (after the whitespace normalization required by
the XSD datatype) and can be used for auditing and for a lossless
re-encoding to XML.

For date/time values the ``timezone`` field is ``null`` when the lexical
value has no timezone. A consumer must not apply the local timezone in that
case; ``null`` means "floating" (timezone unknown), not "local time".
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, Optional

from elementpath.datatypes import AbstractDateTime, Duration

from xmlschema.exceptions import XMLSchemaValueError, XMLSchemaTypeError

__all__ = [
    'JSON_SAFE_INTEGER', 'JSON_TYPE', 'JSON_VALUE', 'JSON_LEXICAL', 'JSON_TIMEZONE',
    'JsonDecimal', 'JsonInteger', 'JsonValueError',
    'is_json_value', 'is_big_integer', 'to_json_value', 'from_json_value',
    'json_value_key', 'json_values_match', 'group_json_values', 'get_timezone_info',
    'encode_json_values', 'decode_json_values', 'dumps', 'loads',
]

JSON_TYPE = '$type'
JSON_VALUE = 'value'
JSON_LEXICAL = 'lexical'
JSON_TIMEZONE = 'timezone'

JSON_SAFE_INTEGER = 2 ** 53 - 1
"""The largest integer that can be represented exactly by a JSON number
in all common JSON implementations (the JavaScript safe integer limit)."""

DECIMAL_PATTERN = re.compile(r'^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$')
INTEGER_PATTERN = re.compile(r'^[+-]?[0-9]+$')

DATE_TYPES = frozenset((
    'date', 'dateTime', 'dateTimeStamp', 'time',
    'gDay', 'gMonth', 'gMonthDay', 'gYear', 'gYearMonth',
))
JSON_VALUE_TYPES = frozenset(('decimal', 'integer', 'duration')) | DATE_TYPES

# Attribute used for storing the original lexical value on date/time objects.
# Elementpath's date/time classes have a __dict__, so no subclass is needed.
LEXICAL_ATTR = 'lexical'
XSD_TYPE_ATTR = 'xsd_type'


class JsonValueError(XMLSchemaValueError):
    """Raised when a typed JSON value is malformed or inconsistent."""


class JsonDecimal(Decimal):
    """
    A ``Decimal`` that keeps the original XSD lexical value.

    Value comparison and hashing are inherited from ``Decimal``, so two
    instances that represent the same numeric value compare equal and have
    the same hash. The original precision is not erased: it is available
    through the *lexical* attribute.
    """
    __slots__ = ('lexical', 'xsd_type')

    def __new__(cls, value: Any, lexical: Optional[str] = None,
                xsd_type: str = 'decimal') -> 'JsonDecimal':
        obj = super().__new__(cls, value)
        obj.lexical = str(value) if lexical is None else lexical
        obj.xsd_type = xsd_type
        return obj

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.lexical!r})"


class JsonInteger(int):
    """
    An ``int`` that keeps the original XSD lexical value.

    Python integers have an arbitrary precision, so the numeric value is
    exact. The *lexical* attribute preserves optional signs and leading
    zeroes for auditing purposes.
    """
    # Note: subclasses of int cannot define non-empty __slots__.
    def __new__(cls, value: Any, lexical: Optional[str] = None,
                xsd_type: str = 'integer') -> 'JsonInteger':
        obj = super().__new__(cls, value)
        obj.lexical = str(value) if lexical is None else lexical
        obj.xsd_type = xsd_type
        return obj

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.lexical!r})"


def _type_name(xsd_type: Any) -> Optional[str]:
    """Returns the local name of an XSD type, if available."""
    if xsd_type is None:
        return None
    elif isinstance(xsd_type, str):
        return xsd_type.split(':')[-1]
    else:
        return getattr(xsd_type, 'local_name', None)


def _date_type_name(value: AbstractDateTime, xsd_type: Any = None) -> str:
    """Returns the XSD local name of a date/time value."""
    name = _type_name(xsd_type)
    if name in DATE_TYPES:
        return name
    elif (name := getattr(value, XSD_TYPE_ATTR, None)) in DATE_TYPES:
        return name
    else:
        return getattr(type(value), 'name', 'dateTime')


def _canonical_decimal(value: Decimal) -> str:
    """Returns a canonical decimal representation without trailing zeroes."""
    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return '0'

    # Strip trailing zeroes without using Decimal.normalize(), that applies
    # the current context precision and can round large values.
    digits_list = list(digits)
    while len(digits_list) > 1 and digits_list[-1] == 0:
        digits_list.pop()
        exponent += 1

    coefficient = ''.join(map(str, digits_list))

    if exponent >= 0:
        result = coefficient + '0' * exponent
    else:
        point = len(coefficient) + exponent
        if point > 0:
            result = f'{coefficient[:point]}.{coefficient[point:]}'
        else:
            result = '0.' + '0' * -point + coefficient

    return f'-{result}' if sign else result


def _canonical_integer(value: int) -> str:
    return str(int(value))


def _timezone_text(value: AbstractDateTime) -> Optional[str]:
    tzinfo = value.tzinfo
    return None if tzinfo is None else str(tzinfo)


def get_timezone_info(obj: dict[str, Any], tzinfo: Any = None) -> Optional[Any]:
    """
    Returns the timezone to use for a typed date/time value.

    The *timezone* field of a typed value is authoritative: ``null`` means
    that the value is a floating date/time without a timezone. In that case
    the provided *tzinfo* is ignored, so a decoding process can't silently
    apply the local timezone of the running machine.

    :param obj: a typed date/time JSON value.
    :param tzinfo: a fallback timezone, used only if the typed value has no
        ``timezone`` field at all.
    """
    if not is_json_value(obj):
        raise JsonValueError("a JSON object with a '$type' field is required")
    elif obj[JSON_TYPE] not in DATE_TYPES:
        raise JsonValueError(f"{obj[JSON_TYPE]!r} is not a date/time type")
    elif JSON_TIMEZONE in obj:
        return None if obj[JSON_TIMEZONE] is None else obj[JSON_TIMEZONE]
    else:
        return tzinfo


def _decimal_lexical(value: Decimal) -> str:
    return getattr(value, LEXICAL_ATTR, None) or str(value)


def _integer_lexical(value: int) -> str:
    return getattr(value, LEXICAL_ATTR, None) or str(value)


def _date_lexical(value: AbstractDateTime) -> str:
    return getattr(value, LEXICAL_ATTR, None) or str(value)


def is_big_integer(value: int, max_safe: int = JSON_SAFE_INTEGER) -> bool:
    """Returns `True` if the integer is outside the safe JSON integer range."""
    return abs(value) > max_safe


def to_json_value(value: Any, xsd_type: Any = None) -> dict[str, Any]:
    """
    Converts a decoded XSD atomic value to its JSON-safe typed representation.

    :param value: a `Decimal`, `int` or an XSD date/time value. A typed
        JSON value is returned unchanged.
    :param xsd_type: optional XSD type used for naming date/time values.
    :return: a JSON object with the ``$type``, ``value`` and ``lexical`` keys.
    :raises: :exc:`XMLSchemaTypeError` if the value is not a supported type.
    """
    if is_json_value(value):
        return value  # type: ignore[return-value]

    if isinstance(value, bool):
        raise XMLSchemaTypeError("boolean values don't need a typed JSON representation")
    elif isinstance(value, Decimal):
        return {
            JSON_TYPE: 'decimal',
            JSON_VALUE: _canonical_decimal(value),
            JSON_LEXICAL: _decimal_lexical(value),
        }
    elif isinstance(value, int):
        return {
            JSON_TYPE: 'integer',
            JSON_VALUE: _canonical_integer(value),
            JSON_LEXICAL: _integer_lexical(value),
        }
    elif isinstance(value, AbstractDateTime):
        return {
            JSON_TYPE: _date_type_name(value, xsd_type),
            JSON_VALUE: str(value),
            JSON_LEXICAL: _date_lexical(value),
            JSON_TIMEZONE: _timezone_text(value),
        }
    elif isinstance(value, Duration):
        return {
            JSON_TYPE: 'duration',
            JSON_VALUE: str(value),
            JSON_LEXICAL: getattr(value, LEXICAL_ATTR, None) or str(value),
        }

    raise XMLSchemaTypeError(
        f"{type(value)!r} is not a supported XSD atomic type for a typed JSON value"
    )


def is_json_value(obj: Any) -> bool:
    """Returns `True` if the object is a typed JSON value."""
    return isinstance(obj, dict) and JSON_TYPE in obj


def _get_required_field(obj: dict[str, Any], name: str) -> Any:
    try:
        return obj[name]
    except KeyError:
        raise JsonValueError(f"missing {name!r} field in a typed JSON value") from None


def from_json_value(obj: dict[str, Any]) -> str:
    """
    Converts a typed JSON value back to its original XSD lexical value.

    The returned string can be passed to the encoder of the XSD type without
    losing the original lexical form.

    :param obj: a typed JSON value.
    :return: the original lexical value.
    :raises: :exc:`JsonValueError` if the typed value is malformed.
    """
    if not is_json_value(obj):
        raise JsonValueError("a JSON object with a '$type' field is required")

    type_name = obj[JSON_TYPE]
    if not isinstance(type_name, str):
        raise JsonValueError("the '$type' field must be a string")
    elif type_name not in JSON_VALUE_TYPES:
        raise JsonValueError(f"unknown typed JSON value {type_name!r}")

    lexical = obj.get(JSON_LEXICAL)
    if lexical is not None:
        if not isinstance(lexical, str):
            raise JsonValueError("the 'lexical' field must be a string")
        return lexical

    value = _get_required_field(obj, JSON_VALUE)
    if not isinstance(value, str):
        raise JsonValueError("the 'value' field must be a string")

    if type_name == 'decimal':
        if DECIMAL_PATTERN.match(value) is None:
            raise JsonValueError(f"invalid decimal value {value!r}")
    elif type_name == 'integer':
        if INTEGER_PATTERN.match(value) is None:
            raise JsonValueError(f"invalid integer value {value!r}")
    elif type_name == 'duration':
        pass
    elif type_name in DATE_TYPES:
        timezone = obj.get(JSON_TIMEZONE)
        if timezone is not None:
            if not isinstance(timezone, str):
                raise JsonValueError("the 'timezone' field must be a string or null")
            if not value.endswith(timezone) and not (
                    timezone == 'Z' and value.endswith('Z')):
                value = f'{value}{timezone}'
    else:  # pragma: no cover
        raise JsonValueError(f"unknown typed JSON value {type_name!r}")

    return value


def json_value_key(obj: dict[str, Any]) -> str:
    """
    Returns an association key for a typed JSON value.

    Values that represent the same XSD value share the same key even if
    their lexical forms differ. This is mostly useful for decimals: the
    lexical form of ``1.0`` and ``1.00`` is preserved in the data, but both
    values are associated with the same key ``decimal:1``.
    """
    if not is_json_value(obj):
        raise JsonValueError("a JSON object with a '$type' field is required")

    type_name = obj[JSON_TYPE]
    if type_name == 'decimal':
        value = obj.get(JSON_VALUE, obj.get(JSON_LEXICAL))
        if not isinstance(value, str) or DECIMAL_PATTERN.match(value) is None:
            raise JsonValueError(f"invalid decimal value {value!r}")
        return f'decimal:{_canonical_decimal(Decimal(value))}'
    elif type_name == 'integer':
        value = obj.get(JSON_VALUE, obj.get(JSON_LEXICAL))
        if not isinstance(value, str) or INTEGER_PATTERN.match(value) is None:
            raise JsonValueError(f"invalid integer value {value!r}")
        return f'integer:{int(value)}'
    elif type_name in DATE_TYPES:
        value = obj.get(JSON_VALUE)
        if not isinstance(value, str):
            raise JsonValueError("the 'value' field must be a string")
        return f'{type_name}:{value}'
    elif type_name == 'duration':
        value = obj.get(JSON_VALUE, obj.get(JSON_LEXICAL))
        if not isinstance(value, str):
            raise JsonValueError("the 'value' field must be a string")
        return f'duration:{value}'
    else:
        raise JsonValueError(f"unknown typed JSON value {type_name!r}")


def json_values_match(obj1: dict[str, Any], obj2: dict[str, Any]) -> bool:
    """Returns `True` if two typed JSON values represent the same XSD value."""
    return json_value_key(obj1) == json_value_key(obj2)


def group_json_values(values: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Groups typed JSON values by their association key.

    Values that represent the same XSD value are grouped together, but each
    original typed value (and therefore its lexical form) is preserved in the
    list. For example ``1.0`` and ``1.00`` are both grouped under
    ``decimal:1`` even though they keep different ``lexical`` fields.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for obj in values:
        groups.setdefault(json_value_key(obj), []).append(obj)
    return groups


def attach_lexical(value: Any, lexical: str, xsd_type: Any = None) -> Any:
    """
    Attaches the original lexical value to a decoded atomic value.

    Decimal and integer values are replaced by their typed subclasses.
    Date/time and duration objects are marked with a ``lexical`` attribute.
    Other values are returned unchanged.
    """
    if isinstance(value, bool) or value is None:
        return value
    elif isinstance(value, Decimal):
        return JsonDecimal(value, lexical, _type_name(xsd_type) or 'decimal')
    elif isinstance(value, int):
        return JsonInteger(value, lexical, _type_name(xsd_type) or 'integer')
    elif isinstance(value, (AbstractDateTime, Duration)):
        setattr(value, LEXICAL_ATTR, lexical)
        setattr(value, XSD_TYPE_ATTR, _date_type_name(value, xsd_type)
                if isinstance(value, AbstractDateTime) else 'duration')
        return value
    else:
        return value


def encode_json_values(obj: Any, integers: str = 'auto',
                       max_safe_integer: int = JSON_SAFE_INTEGER) -> Any:
    """
    Recursively replaces typed XSD atomic values with JSON-safe objects.

    :param obj: a decoded data structure.
    :param integers: how to encode integer values. Can be ``'auto'`` (default) \
    for encoding only integers outside the safe JSON range, or ``'all'`` for \
    encoding all integers as typed values.
    :param max_safe_integer: the largest absolute integer value that is \
    considered safe for a JSON number. Used when *integers* is ``'auto'``.
    """
    if integers not in ('auto', 'all'):
        raise XMLSchemaValueError("the 'integers' argument must be 'auto' or 'all'")

    if isinstance(obj, dict):
        return {k: encode_json_values(v, integers, max_safe_integer)
                for k, v in obj.items()}
    elif isinstance(obj, list):
        return [encode_json_values(v, integers, max_safe_integer) for v in obj]
    elif isinstance(obj, tuple):
        return tuple(encode_json_values(v, integers, max_safe_integer) for v in obj)
    elif isinstance(obj, bool) or obj is None:
        return obj
    elif isinstance(obj, Decimal):
        return to_json_value(obj)
    elif isinstance(obj, int):
        if integers == 'all' or integers == 'auto' and \
                is_big_integer(obj, max_safe_integer):
            return to_json_value(obj)
        else:
            return int(obj)
    elif isinstance(obj, (AbstractDateTime, Duration)):
        return to_json_value(obj)
    elif isinstance(obj, Iterator):
        return (encode_json_values(v, integers, max_safe_integer) for v in obj)
    else:
        return obj


def decode_json_values(obj: Any, strict: bool = False) -> Any:
    """
    Recursively replaces typed JSON values with their XSD lexical values.

    :param obj: a decoded JSON object.
    :param strict: if `True` malformed typed values raise a \
    :exc:`JsonValueError`, otherwise they are left unchanged.
    """
    if isinstance(obj, dict):
        if is_json_value(obj):
            if strict:
                return from_json_value(obj)
            try:
                return from_json_value(obj)
            except JsonValueError:
                return {k: decode_json_values(v, strict=False) for k, v in obj.items()}
        return {k: decode_json_values(v, strict) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decode_json_values(v, strict) for v in obj]
    elif isinstance(obj, tuple):
        return tuple(decode_json_values(v, strict) for v in obj)
    else:
        return obj


def dumps(obj: Any, *, integers: str = 'auto',
          max_safe_integer: int = JSON_SAFE_INTEGER, **kwargs: Any) -> str:
    """Serializes a decoded data structure to a JSON string with typed values."""
    return json.dumps(
        encode_json_values(obj, integers, max_safe_integer), **kwargs
    )


def loads(source: str | bytes, *, strict: bool = False, **kwargs: Any) -> Any:
    """Deserializes a JSON string with typed values to XSD lexical values."""
    return decode_json_values(json.loads(source, **kwargs), strict)
