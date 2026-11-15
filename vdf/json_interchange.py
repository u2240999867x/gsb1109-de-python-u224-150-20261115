"""
Typed JSON interchange representation for binary VDF.

A directory service that only speaks JSON cannot round-trip the typed values of
binary VDF through plain ``json``:

* JSON numbers are parsed as IEEE-754 doubles by most consumers, so the 64-bit
  VDF integers (``UINT_64`` / ``INT_64``) lose precision above 2**53.
* Text VDF only knows strings and nested blocks. It cannot express the
  ``uint64``, ``int64``, ``pointer`` and ``color`` binary types, so the type
  information has to travel out of band.

This module emits a self describing envelope that keeps every big integer as an
exact decimal *string* (never a JSON number, never a float) and records the
types text VDF cannot express in a ``sidecar`` table keyed by RFC 6901 JSON
Pointer::

    {
        "vdf_typed_json": "1.0",
        "data": {
            "name": "value",
            "big": "18446744073709551615",
            "nested": {"ptr": "1234"}
        },
        "sidecar": {
            "/big": "uint64",
            "/nested/ptr": "pointer"
        }
    }

Decoding parses the decimal text back with ``int()`` (never ``float()``), range
checks it and rebuilds the original ``vdf`` integer subclass, so both value and
type survive the JSON hop unchanged.

A mapping whose keys are all unique is encoded as a JSON object. A mapping that
contains duplicate keys (only ``vdf.VDFDict`` can) has no JSON object
equivalent, so it is encoded as an ordered array of ``[key, value]`` pairs and
decoded back through the chosen mapper, preserving every duplicate and its
order. Sidecar pointers address the value of pair ``i`` as ``<path>/<i>/1``.
"""
import re
import json

try:
    from collections.abc import Mapping
except ImportError:
    from collections import Mapping

from vdf import UINT_64, INT_64, POINTER, COLOR, string_type, int_type

ENVELOPE_VERSION = "1.0"
ENVELOPE_KEY = "vdf_typed_json"
DATA_KEY = "data"
SIDECAR_KEY = "sidecar"

TAG_UINT64 = "uint64"
TAG_INT64 = "int64"
TAG_POINTER = "pointer"
TAG_COLOR = "color"

# class -> sidecar tag
_TYPE_TAGS = {
    UINT_64: TAG_UINT64,
    INT_64: TAG_INT64,
    POINTER: TAG_POINTER,
    COLOR: TAG_COLOR,
}
# sidecar tag -> class
_TAG_TYPES = {tag: cls for cls, tag in _TYPE_TAGS.items()}

# inclusive value ranges, matching how binary VDF packs each type
_UINT64_MAX = (1 << 64) - 1
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1
_TAG_RANGES = {
    TAG_UINT64: (0, _UINT64_MAX),
    TAG_INT64: (_INT64_MIN, _INT64_MAX),
    # pointer and color share the signed 32 bit pack used by binary VDF
    TAG_POINTER: (_INT32_MIN, _INT32_MAX),
    TAG_COLOR: (_INT32_MIN, _INT32_MAX),
}

_DECIMAL_RE = re.compile(r'^-?[0-9]+$')


def _escape_pointer_token(token):
    # RFC 6901: '~' must be escaped before '/'
    return token.replace("~", "~0").replace("/", "~1")


def _join_pointer(parent, key):
    return parent + "/" + _escape_pointer_token(key)


def _has_duplicate_keys(mapping):
    seen = set()
    for key in mapping.keys():
        if key in seen:
            return True
        seen.add(key)
    return False


def encode_typed(obj):
    """
    Convert a binary VDF Python object into the typed JSON envelope.

    The returned structure contains only JSON native types. The four binary
    integer types are rendered as decimal strings and listed in ``sidecar``;
    plain ``int``/``float``/``str`` values stay as ordinary JSON scalars.

    Mappings with unique keys become JSON objects. A mapping that contains
    duplicate keys (only ``vdf.VDFDict`` can) has no JSON object equivalent, so
    it is encoded as an ordered array of ``[key, value]`` pairs instead.
    """
    sidecar = {}
    data = _encode_value(obj, "", sidecar)
    return {
        ENVELOPE_KEY: ENVELOPE_VERSION,
        DATA_KEY: data,
        SIDECAR_KEY: sidecar,
    }


def _check_key(key):
    if not isinstance(key, string_type):
        raise TypeError("dict keys must be of type str, got %s" % type(key))


def _encode_value(value, pointer, sidecar):
    if isinstance(value, Mapping):
        if _has_duplicate_keys(value):
            # ordered list of [key, value] pairs; the value of pair i lives at
            # "<pointer>/<i>/1" so sidecar pointers stay RFC 6901 addressable
            result = []
            for index, (key, child) in enumerate(value.items()):
                _check_key(key)
                child_pointer = "%s/%d/1" % (pointer, index)
                result.append([key, _encode_value(child, child_pointer, sidecar)])
            return result

        result = {}
        for key, child in value.items():
            _check_key(key)
            result[key] = _encode_value(child, _join_pointer(pointer, key), sidecar)
        return result

    if isinstance(value, string_type):
        return value

    # bool is a subclass of int, but binary VDF has no boolean type
    if isinstance(value, bool):
        raise TypeError("boolean values are not representable in binary VDF")

    for cls, tag in _TYPE_TAGS.items():
        if isinstance(value, cls):
            sidecar[pointer] = tag
            # str() of the int subclass yields the exact decimal representation
            return str(int(value))

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return value

    raise TypeError("Unsupported type: %s" % type(value))


def decode_typed(envelope, mapper=dict):
    """
    Rebuild a binary VDF Python object from a typed JSON envelope.

    Every sidecar entry is resolved against ``data`` and used to reconstruct the
    matching ``vdf`` integer subclass. Decimal text is parsed via ``int()`` and
    range validated, so no value ever passes through a float.
    """
    if not isinstance(envelope, Mapping):
        raise TypeError("Expected envelope to be a dict, got %s" % type(envelope))
    if DATA_KEY not in envelope:
        raise ValueError("Envelope is missing the %r key" % DATA_KEY)

    sidecar = envelope.get(SIDECAR_KEY) or {}
    if not isinstance(sidecar, Mapping):
        raise ValueError("Envelope %r must be an object" % SIDECAR_KEY)

    unresolved = set(sidecar)
    result = _decode_value(envelope[DATA_KEY], "", sidecar, unresolved, mapper)

    if unresolved:
        raise ValueError("Sidecar entries do not resolve to any value: %s"
                         % sorted(unresolved))

    return result


def _decode_value(node, pointer, sidecar, unresolved, mapper):
    if isinstance(node, Mapping):
        result = mapper()
        for key, child in node.items():
            result[key] = _decode_value(
                child, _join_pointer(pointer, key), sidecar, unresolved, mapper)
        return result

    if isinstance(node, list):
        # ordered [key, value] pairs produced for a duplicate-key mapping;
        # assigning through the mapper re-creates duplicates (e.g. VDFDict)
        result = mapper()
        for index, pair in enumerate(node):
            if not (isinstance(pair, list) and len(pair) == 2
                    and isinstance(pair[0], string_type)):
                raise ValueError(
                    "Expected [key, value] pair at %s/%d, got %r"
                    % (pointer, index, pair))
            key, child = pair
            result[key] = _decode_value(
                child, "%s/%d/1" % (pointer, index), sidecar, unresolved, mapper)
        return result

    tag = sidecar.get(pointer)
    if tag is not None:
        unresolved.discard(pointer)
        return _rebuild_typed(tag, node, pointer)

    if isinstance(node, bool):
        raise TypeError("boolean values are not representable in binary VDF")
    if isinstance(node, (int_type, float, string_type)):
        return node

    raise TypeError("Unsupported JSON type at %s: %s" % (pointer, type(node)))


def _rebuild_typed(tag, node, pointer):
    if tag not in _TAG_TYPES:
        raise ValueError("Unknown sidecar type %r at %s" % (tag, pointer))
    if not isinstance(node, string_type):
        raise ValueError("Typed value at %s must be a decimal string, got %s"
                         % (pointer, type(node).__name__))
    if not _DECIMAL_RE.match(node):
        raise ValueError("Typed value at %s is not an integer decimal: %r"
                         % (pointer, node))

    value = int(node, 10)
    low, high = _TAG_RANGES[tag]
    if not low <= value <= high:
        raise ValueError("Value %d at %s is out of range for %s (%d..%d)"
                         % (value, pointer, tag, low, high))

    return _TAG_TYPES[tag](value)


def typed_json_dumps(obj, **kwargs):
    """Serialize a binary VDF object to a typed JSON ``str``."""
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(encode_typed(obj), **kwargs)


def typed_json_dump(obj, fp, **kwargs):
    """Serialize a binary VDF object to a typed JSON stream."""
    kwargs.setdefault("ensure_ascii", False)
    json.dump(encode_typed(obj), fp, **kwargs)


def typed_json_loads(s, mapper=dict, **kwargs):
    """Deserialize a typed JSON ``str`` back to a binary VDF object."""
    return decode_typed(json.loads(s, **kwargs), mapper=mapper)


def typed_json_load(fp, mapper=dict, **kwargs):
    """Deserialize a typed JSON stream back to a binary VDF object."""
    return decode_typed(json.load(fp, **kwargs), mapper=mapper)


def binary_to_typed(data, mapper=dict, **binary_load_kwargs):
    """
    Parse binary VDF ``bytes`` and return its typed JSON envelope.

    This is the entry point for handing binary VDF to a JSON only service.
    """
    import vdf
    return encode_typed(vdf.binary_loads(data, mapper=mapper, **binary_load_kwargs))


def typed_to_binary(envelope, mapper=dict, alt_format=False):
    """Rebuild a typed JSON envelope back to binary VDF ``bytes``."""
    import vdf
    return vdf.binary_dumps(decode_typed(envelope, mapper=mapper),
                            alt_format=alt_format)
