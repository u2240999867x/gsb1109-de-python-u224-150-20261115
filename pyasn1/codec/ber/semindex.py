#
# This file is part of pyasn1 software.
#
# Copyright (c) 2026, pyasn1 maintenance organization
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
"""Semantic de-duplication index over BER substrates.

The same ASN.1 object may be transmitted in more than one valid BER
encoding: definite versus indefinite length, short versus long length
forms, primitive versus constructed (fragmented) strings, or the members
of a SET serialized in a different order.  This module groups *semantically
equal* substrates under one stable entity number while keeping every
transmitted original byte string untouched.

Equivalence rules implemented here (ITU-T X.690 BER semantics):

* SET and SET OF are unordered: member order in the substrate does not
  affect the fingerprint, but member multiplicity does.
* SEQUENCE and SEQUENCE OF are ordered: two encodings whose members appear
  in a different order get different fingerprints.
* Restricted character strings are keyed together with their ASN.1 type.
  Fragments of one string are reassembled before keying, but fragments of
  different string types are never merged.
* OCTET STRING, BIT STRING and the character string types compare by their
  decoded content, so their primitive and constructed encodings match.
* REAL values are reduced to an exact rational number so that binary and
  character (decimal) encodings of the same value match.
* A field whose type cannot be interpreted (unknown context/application/
  private primitive tag) makes equivalence unprovable: every occurrence is
  given a unique identity, so two substrates carrying such a field are never
  assigned the same entity number.
* An unknown constructed tag wrapping one value is treated as an explicit
  tag (wrapper class and tag number stay part of the key).  An unknown
  constructed tag wrapping several values is treated as an opaque, ordered
  structure rather than an unordered SET, since its type is not known.
"""
import hashlib
import math
import os
import sys
from collections import namedtuple

from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.error import PyAsn1Error
from pyasn1.type import char
from pyasn1.type import univ
from pyasn1.type import useful

__all__ = ['SemanticIndex', 'Record', 'SemanticIndexError']

# BER tag class bits (X.690 8.1.2)
_TAG_CLASS_MASK = 0xC0
_TAG_CONSTRUCTED_BIT = 0x20
_TAG_NUMBER_MASK = 0x1F
_HIGH_TAG_NUMBER = 0x1F

_TAG_CLASS_UNIVERSAL = 0x00

_INDEFINITE_LENGTH = 0x80
_LONG_LENGTH_MASK = 0x7F
_END_OF_CONTENTS = b'\x00\x00'

# Canonical key type markers.  They never appear as length octets of an
# inner field because each field is length-prefixed.
_M_BOOLEAN = b'B'
_M_INTEGER = b'I'
_M_ENUMERATED = b'E'
_M_BIT_STRING = b'X'
_M_OCTET_STRING = b'O'
_M_NULL = b'N'
_M_OID = b'R'
_M_REL_OID = b'D'
_M_REAL = b'L'
_M_TEXT = b'T'
_M_SEQUENCE = b'Q'
_M_SET = b'S'
_M_WRAPPER = b'W'
_M_UNKNOWN_CONSTRUCTED = b'C'
_M_UNKNOWN_PRIMITIVE = b'P'

_REAL_PLUS_INF = b'+'
_REAL_MINUS_INF = b'-'
_REAL_RATIONAL = b'Q'

# Fragmentable scalar types: OCTET STRING, BIT STRING, the restricted
# character strings and the useful text/descriptor types may appear in a
# constructed (segmented) form in BER.  REAL has no constructed form in
# X.690 and is deliberately excluded.
_CONSTRUCTED_SCALAR_TAGS = frozenset((
    univ.BitString.tagSet[0].tagId,
    univ.OctetString.tagSet[0].tagId,
    useful.ObjectDescriptor.tagSet[0].tagId,
    char.NumericString.tagSet[0].tagId,
    char.PrintableString.tagSet[0].tagId,
    char.TeletexString.tagSet[0].tagId,
    char.VideotexString.tagSet[0].tagId,
    char.IA5String.tagSet[0].tagId,
    char.GraphicString.tagSet[0].tagId,
    char.VisibleString.tagSet[0].tagId,
    char.GeneralString.tagSet[0].tagId,
    char.UniversalString.tagSet[0].tagId,
    char.BMPString.tagSet[0].tagId,
    char.UTF8String.tagSet[0].tagId,
    useful.UTCTime.tagSet[0].tagId,
    useful.GeneralizedTime.tagSet[0].tagId,
))

# Binary REAL exponents are length-bounded by the substrate, but reject
# absurd values before they reach pow() (denial-of-service guardrail).
_MAX_REAL_EXPONENT = 10000

_Tlv = namedtuple(
    '_Tlv', ('tag_class', 'constructed', 'tag_id', 'content', 'raw'))
Record = namedtuple(
    'Record', ('name', 'entity_id', 'fingerprint', 'substrate', 'length'))


class SemanticIndexError(PyAsn1Error):
    """A substrate cannot be parsed into a semantic fingerprint."""


def _length_prefix(payload):
    size = len(payload)
    if size < 128:
        return bytes((size,)) + payload

    octets = []
    while size:
        octets.append(size & 0xFF)
        size >>= 8

    octets.reverse()
    return bytes((0x80 | len(octets),)) + bytes(octets) + payload


def _field(marker, payload=b''):
    return marker + _length_prefix(payload)


def _parse_tlv(data, offset, end, depth):
    if depth > ber_decoder.MAX_NESTING_DEPTH:
        raise SemanticIndexError(
            'ASN.1 structure nesting depth exceeds limit (%d)'
            % ber_decoder.MAX_NESTING_DEPTH)

    if offset >= end:
        raise SemanticIndexError('Truncated BER substrate: missing tag')

    first_octet = data[offset]
    start = offset
    offset += 1
    tag_class = first_octet & _TAG_CLASS_MASK
    constructed = bool(first_octet & _TAG_CONSTRUCTED_BIT)
    tag_id = first_octet & _TAG_NUMBER_MASK

    if tag_id == _HIGH_TAG_NUMBER:
        tag_id = 0
        tag_octets = 0
        while True:
            if offset >= end:
                raise SemanticIndexError(
                    'Truncated BER substrate: incomplete high tag number')
            octet = data[offset]
            offset += 1
            tag_octets += 1
            if tag_octets > ber_decoder.MAX_TAG_OCTETS:
                raise SemanticIndexError(
                    'Tag exceeds %d octets' % ber_decoder.MAX_TAG_OCTETS)
            tag_id = (tag_id << 7) | (octet & 0x7F)
            if not octet & 0x80:
                break

    if offset >= end:
        raise SemanticIndexError('Truncated BER substrate: missing length')

    first_length = data[offset]
    offset += 1

    if first_length == _INDEFINITE_LENGTH:
        if not constructed:
            raise SemanticIndexError(
                'Indefinite length used with primitive encoding')
        indefinite = True
        content_end = None
    elif first_length & 0x80:
        count = first_length & _LONG_LENGTH_MASK
        if count > ber_decoder.MAX_LENGTH_OCTETS:
            raise SemanticIndexError(
                'Length field exceeds %d octets'
                % ber_decoder.MAX_LENGTH_OCTETS)
        if offset + count > end:
            raise SemanticIndexError(
                'Truncated BER substrate: incomplete length field')
        length = 0
        for _ in range(count):
            length = (length << 8) | data[offset]
            offset += 1
        if length > sys.maxsize:
            raise SemanticIndexError(
                'Length %d exceeds maximum readable size %d'
                % (length, sys.maxsize))
        content_end = offset + length
        if content_end > end:
            raise SemanticIndexError(
                'Truncated BER substrate: declared content exceeds input')
        indefinite = False
    else:
        length = first_length
        content_end = offset + length
        if content_end > end:
            raise SemanticIndexError(
                'Truncated BER substrate: declared content exceeds input')
        indefinite = False

    if indefinite:
        # Content runs until the end-of-contents octets; remember where the
        # EOC sits so nested parsing stays inside this container.
        probe = offset
        while True:
            if probe + 2 > end:
                raise SemanticIndexError(
                    'Truncated BER substrate: missing end-of-contents')
            if data[probe:probe + 2] == _END_OF_CONTENTS:
                content_end = probe
                next_offset = probe + 2
                break
            _, next_offset = _scan_tlv(data, probe, end, depth)
            probe = next_offset
        raw = bytes(data[start:next_offset])
        content = bytes(data[offset:content_end])
        return _Tlv(tag_class, constructed, tag_id, content, raw), next_offset

    raw = bytes(data[start:content_end])
    content = bytes(data[offset:content_end])
    return _Tlv(tag_class, constructed, tag_id, content, raw), content_end


def _scan_tlv(data, offset, end, depth):
    tlv, next_offset = _parse_tlv(data, offset, end, depth)
    return tlv, next_offset


def _iter_children(tlv, depth):
    data = tlv.content
    end = len(data)
    offset = 0
    while offset < end:
        child, offset = _parse_tlv(data, offset, end, depth + 1)
        yield child
    if offset != end:
        raise SemanticIndexError('Malformed BER container content')


def _decode_whole(tlv):
    """Decode one complete TLV through pyasn1 and return the value object."""
    try:
        value, tail = ber_decoder.decode(tlv.raw)
    except PyAsn1Error:
        raise
    except Exception as exc:
        raise SemanticIndexError('Unparseable BER TLV: %s' % exc)

    if tail:
        raise SemanticIndexError('Trailing octets after BER TLV')

    return value


def _signed_bytes(number):
    if number == 0:
        return b'\x00'
    return number.to_bytes((number.bit_length() + 8) // 8, 'big', signed=True)


def _unsigned_bytes(number):
    if number == 0:
        return b'\x00'
    return number.to_bytes((number.bit_length() + 7) // 8, 'big')


def _normalize_real(value):
    if value.isInf:
        if value.isPlusInf:
            return _REAL_PLUS_INF
        return _REAL_MINUS_INF

    mantissa, base, exponent = tuple(value)

    if abs(exponent) > _MAX_REAL_EXPONENT:
        raise SemanticIndexError(
            'REAL exponent %d exceeds supported range' % exponent)

    if exponent >= 0:
        numerator = mantissa * base ** exponent
        denominator = 1
    else:
        numerator = mantissa
        denominator = base ** (-exponent)

    if denominator != 1:
        # gcd(0, d) == d, so zero values collapse to the canonical 0/1.
        common = math.gcd(abs(numerator), denominator)
        numerator //= common
        denominator //= common

    return (_REAL_RATIONAL
            + _length_prefix(_signed_bytes(numerator))
            + _length_prefix(_unsigned_bytes(denominator)))


def _scalar_fingerprint(tlv):
    value = _decode_whole(tlv)
    tag_id = tlv.tag_id

    if tag_id == univ.Boolean.tagSet[0].tagId:
        return _field(_M_BOOLEAN, b'\x01' if int(value) else b'\x00')

    if tag_id == univ.Integer.tagSet[0].tagId:
        number = int(value)
        return _field(_M_INTEGER, number.to_bytes(
            (number.bit_length() + 8) // 8, 'big', signed=True))

    if tag_id == univ.Enumerated.tagSet[0].tagId:
        number = int(value)
        return _field(_M_ENUMERATED, number.to_bytes(
            (number.bit_length() + 8) // 8, 'big', signed=True))

    if tag_id == univ.BitString.tagSet[0].tagId:
        # asBinary() carries the exact bit count, including trailing bits,
        # so differing padding never collides.
        return _field(_M_BIT_STRING, value.asBinary().encode('ascii'))

    if tag_id == univ.Null.tagSet[0].tagId:
        return _field(_M_NULL)

    if tag_id == univ.ObjectIdentifier.tagSet[0].tagId:
        arcs = b''.join(_length_prefix(_unsigned_bytes(arc))
                        for arc in tuple(value))
        return _field(_M_OID, arcs)

    if tag_id == univ.RelativeOID.tagSet[0].tagId:
        arcs = b''.join(_length_prefix(_unsigned_bytes(arc))
                        for arc in tuple(value))
        return _field(_M_REL_OID, arcs)

    if tag_id == univ.Real.tagSet[0].tagId:
        return _field(_M_REAL, _normalize_real(value))

    if tag_id == univ.OctetString.tagSet[0].tagId:
        return _field(_M_OCTET_STRING, value.asOctets())

    # Every restricted character string and useful text type keeps its tag
    # number in the key, so different string types never merge.
    return _field(_M_TEXT, _unsigned_bytes(tag_id) + value.asOctets())


def _fingerprint(tlv, depth=0):
    if tlv.tag_class != _TAG_CLASS_UNIVERSAL:
        if not tlv.constructed:
            # Opaque primitive of an unknown type: its meaning cannot be
            # recovered, so equivalence is unprovable.  A random nonce gives
            # every occurrence a globally unique fingerprint.
            return _field(
                _M_UNKNOWN_PRIMITIVE,
                bytes((tlv.tag_class >> 6,))
                + _length_prefix(tlv.content)
                + _length_prefix(os.urandom(16)))

        children = [_fingerprint(child, depth + 1)
                    for child in _iter_children(tlv, depth)]
        wrapper_head = bytes((tlv.tag_class >> 6,)) + _length_prefix(
            _unsigned_bytes(tlv.tag_id))
        if len(children) == 1:
            # Explicit tag wrapping exactly one value.
            return _field(_M_WRAPPER, wrapper_head + children[0])

        payload = wrapper_head + b''.join(
            _length_prefix(child) for child in children)
        return _field(_M_UNKNOWN_CONSTRUCTED, payload)

    if tlv.constructed and tlv.tag_id not in (
            univ.Sequence.tagSet[0].tagId, univ.Set.tagSet[0].tagId):
        if tlv.tag_id in _CONSTRUCTED_SCALAR_TAGS:
            # Constructed (fragmented) scalar: pyasn1 reassembles the
            # fragments, then the value is keyed like its primitive form.
            return _scalar_fingerprint(tlv)

        raise SemanticIndexError(
            'Unsupported constructed universal tag %d' % tlv.tag_id)

    if tlv.constructed:
        children = [_fingerprint(child, depth + 1)
                    for child in _iter_children(tlv, depth)]
        if tlv.tag_id == univ.Set.tagSet[0].tagId:
            # Unordered collection: sort canonical member encodings so the
            # substrate member order becomes irrelevant; duplicates survive
            # because every member is counted separately.
            members = sorted(children)
            return _field(_M_SET, b''.join(_length_prefix(member)
                                           for member in members))

        return _field(_M_SEQUENCE,
                      b''.join(_length_prefix(child) for child in children))

    return _scalar_fingerprint(tlv)


class SemanticIndex(object):
    """Group BER substrates by semantic equivalence.

    Each :meth:`add` call stores the transmitted substrate verbatim and
    assigns it an entity number.  Two substrates share an entity number if
    and only if their canonical fingerprints are identical.
    """

    def __init__(self):
        self._records = []
        self._entity_by_fingerprint = {}
        self._names_by_entity = {}

    def __len__(self):
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    @property
    def records(self):
        """All retained records, in insertion order."""
        return list(self._records)

    @property
    def entities(self):
        """Mapping of entity number to the record names carrying it."""
        return dict((entity_id, list(names))
                    for entity_id, names in self._names_by_entity.items())

    def fingerprint(self, substrate):
        """Return the canonical binary fingerprint of one substrate."""
        if isinstance(substrate, str):
            raise SemanticIndexError(
                'Substrate must be bytes, not a character string')

        try:
            data = bytes(substrate)
        except (TypeError, ValueError) as exc:
            raise SemanticIndexError('Unusable substrate: %s' % exc)

        if not data:
            raise SemanticIndexError('Empty BER substrate')

        try:
            tlv, tail = _parse_tlv(data, 0, len(data), 0)
        except SemanticIndexError:
            raise
        except PyAsn1Error as exc:
            raise SemanticIndexError(str(exc))

        if tail != len(data):
            raise SemanticIndexError(
                'Trailing octets after outermost BER TLV')

        try:
            return _fingerprint(tlv)
        except SemanticIndexError:
            raise
        except PyAsn1Error as exc:
            raise SemanticIndexError(str(exc))

    def add(self, name, substrate):
        """Retain one transmission and assign (or reuse) an entity number."""
        canonical = self.fingerprint(substrate)
        digest = hashlib.sha256(canonical).hexdigest()

        entity_id = self._entity_by_fingerprint.get(canonical)
        if entity_id is None:
            entity_id = len(self._entity_by_fingerprint) + 1
            self._entity_by_fingerprint[canonical] = entity_id
            self._names_by_entity[entity_id] = []

        self._names_by_entity[entity_id].append(name)

        record = Record(name, entity_id, digest, bytes(substrate),
                        len(substrate))
        self._records.append(record)
        return record

    def to_dict(self):
        """Serialise the index, including hashes of every original."""
        return {
            'entities': self.entities,
            'records': [
                {
                    'name': record.name,
                    'entity_id': record.entity_id,
                    'fingerprint': record.fingerprint,
                    'original_sha256': hashlib.sha256(
                        record.substrate).hexdigest(),
                    'length': record.length,
                }
                for record in self._records
            ],
        }
