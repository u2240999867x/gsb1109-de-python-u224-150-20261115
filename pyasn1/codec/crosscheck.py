#
# This file is part of pyasn1 software.
#
# Copyright (c) 2026, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
"""CER/DER cross-validation export.

CER splits long octet and bit strings into 1000-unit constructed segments
while DER always emits one primitive payload, so the two serialisations of
the same value cannot be compared byte-for-byte or by segment count. This
module exports an ASN.1 value through both codecs and compares what survives
a decode round-trip: the logical content for every type and, for REAL, the
significant digits rather than the chosen base or exponent representation.

The source CER substrate is additionally walked before re-encoding, so that a
malformed segment -- an overlong chunk, a primitive/constructed mismatch or a
tag mismatch inside a constructed string -- fails validation instead of being
silently hidden by the canonical DER re-encoding.
"""
from pyasn1 import error
from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.codec.cer import decoder as cer_decoder
from pyasn1.codec.cer import encoder as cer_encoder
from pyasn1.codec.der import decoder as der_decoder
from pyasn1.codec.der import encoder as der_encoder
from pyasn1.type import base
from pyasn1.type import tag as tag_module
from pyasn1.type import univ

__all__ = ['CrossValidationError', 'CrossValidationReport',
           'export_and_validate', 'validate_cer_substrate', 'encode']

#: X.690 9.2: CER string segments hold at most this many octets, and every
#: segment except the last one must use exactly this size.
CER_MAX_CHUNK_SIZE = cer_encoder.SingleItemEncoder.fixedChunkSize

#: A constructed BIT STRING segment adds its one-octet unused-bits prefix to
#: its 1000 data octets, so its TLV content may be one octet larger.
CER_MAX_BIT_STRING_SEGMENT_SIZE = CER_MAX_CHUNK_SIZE + 1

#: UNIVERSAL tag numbers of the types whose encoder may emit a constructed,
#: segmented form: BIT STRING plus OCTET STRING, every character string and
#: the two useful time types, all of which ride on OctetStringEncoder.
_TAG_CLASS_UNIVERSAL = 0x00
_TAG_NUMBER_BIT_STRING = 0x03
_TAG_NUMBER_OCTET_STRING = 0x04
_SEGMENTED_OCTET_STRING_TAGS = frozenset((
    0x04,  # OCTET STRING
    0x07,  # ObjectDescriptor
    0x0C,  # UTF8String
    0x12,  # NumericString
    0x13,  # PrintableString
    0x14,  # TeletexString / T61String
    0x15,  # VideotexString
    0x16,  # IA5String
    0x17,  # UTCTime
    0x18,  # GeneralizedTime
    0x19,  # GraphicString
    0x1A,  # VisibleString / ISO646String
    0x1B,  # GeneralString
    0x1C,  # UniversalString
    0x1E,  # BMPString
))

#: The constructed bit in a BER/CER/DER identifier octet.
_TAG_CONSTRUCTED_BIT = 0x20
_TAG_CLASS_MASK = 0xC0
_TAG_NUMBER_MASK = 0x1F
_INDEFINITE_LENGTH = 0x80
_END_OF_CONTENTS = b'\x00\x00'


class CrossValidationError(error.PyAsn1Error):
    """A CER/DER cross-validation check failed."""


def _read_tlv_header(substrate, offset):
    """Parse one TLV header.

    Returns (firstTagByte, tagNumber, length, contentStart). Multi-octet
    (high tag number) identifiers are consumed fully so the length octet is
    located correctly, bounded by the decoder's own tag-octet limit.
    """
    try:
        firstTagByte = substrate[offset]

    except IndexError:
        raise CrossValidationError(
            'Truncated CER substrate at offset %d' % offset)

    position = offset + 1
    tagNumber = firstTagByte & _TAG_NUMBER_MASK

    if tagNumber == _TAG_NUMBER_MASK:
        # Long form: tag number continues across the following octets.
        tagNumber = 0
        tagOctets = 0

        while True:
            try:
                tagOctet = substrate[position]

            except IndexError:
                raise CrossValidationError(
                    'Truncated multi-octet tag at offset %d' % position)

            position += 1
            tagOctets += 1

            if tagOctets > ber_decoder.MAX_TAG_OCTETS:
                raise CrossValidationError(
                    'Tag ID octet count exceeds limit (%d)'
                    % ber_decoder.MAX_TAG_OCTETS)

            tagNumber = (tagNumber << 7) | (tagOctet & 0x7F)

            if not tagOctet & 0x80:
                break

    try:
        firstLengthOctet = substrate[position]

    except IndexError:
        raise CrossValidationError(
            'Missing length octet at offset %d' % position)

    position += 1

    if firstLengthOctet < _INDEFINITE_LENGTH:
        return firstTagByte, tagNumber, firstLengthOctet, position

    if firstLengthOctet == _INDEFINITE_LENGTH:
        return firstTagByte, tagNumber, -1, position

    lengthOctets = firstLengthOctet & 0x7F

    if lengthOctets > ber_decoder.MAX_LENGTH_OCTETS:
        raise CrossValidationError(
            'Length field spans %d octets, limit is %d'
            % (lengthOctets, ber_decoder.MAX_LENGTH_OCTETS))

    if position + lengthOctets > len(substrate):
        raise CrossValidationError(
            'Truncated length field at offset %d' % position)

    length = 0
    for octet in substrate[position:position + lengthOctets]:
        length = (length << 8) | octet

    position += lengthOctets

    return firstTagByte, tagNumber, length, position


def _walk_tlvs(substrate, offset, end):
    """Recursively walk every TLV between ``offset`` and ``end``.

    Containers (SEQUENCE/SET/EXPLICIT tags and their indefinite-length CER
    form) are descended into. A constructed UNIVERSAL string TLV is a
    segmented CER string, so X.690's segment rules are enforced there: every
    segment is primitive, carries the OCTET STRING (or BIT STRING) tag,
    respects the 1000-octet data limit, and every non-final segment uses the
    exact full size. A primitive string longer than the limit is rejected,
    because CER requires such values to be segmented. Returns the offset just
    past the walked region.
    """
    while offset < end:
        if substrate[offset:offset + 2] == _END_OF_CONTENTS:
            return offset + 2

        tagByte, tagNumber, length, contentStart = _read_tlv_header(
            substrate, offset)
        isConstructed = bool(tagByte & _TAG_CONSTRUCTED_BIT)
        tagClass = tagByte & _TAG_CLASS_MASK

        if length < 0:
            if not isConstructed:
                raise CrossValidationError(
                    'Primitive TLV 0x%02x uses indefinite length' % tagByte)
            contentEnd = end
        else:
            contentEnd = contentStart + length
            if contentEnd > end:
                raise CrossValidationError(
                    'TLV at offset %d overruns its container' % offset)

        if not isConstructed:
            if tagClass == _TAG_CLASS_UNIVERSAL:
                if tagNumber == _TAG_NUMBER_BIT_STRING:
                    primitiveLimit = CER_MAX_BIT_STRING_SEGMENT_SIZE
                elif tagNumber in _SEGMENTED_OCTET_STRING_TAGS:
                    primitiveLimit = CER_MAX_CHUNK_SIZE
                else:
                    primitiveLimit = None

                if primitiveLimit is not None and length > primitiveLimit:
                    raise CrossValidationError(
                        'Primitive CER string 0x%02x holds %d octets, must '
                        'use the constructed form past %d'
                        % (tagByte, length, primitiveLimit))

            offset = contentEnd
            continue

        isBitString = (
            tagClass == _TAG_CLASS_UNIVERSAL
            and tagNumber == _TAG_NUMBER_BIT_STRING)
        isSegmentedString = isBitString or (
            tagClass == _TAG_CLASS_UNIVERSAL
            and tagNumber in _SEGMENTED_OCTET_STRING_TAGS)

        if isSegmentedString:
            expectedSegmentTag = (
                _TAG_NUMBER_BIT_STRING if isBitString
                else _TAG_NUMBER_OCTET_STRING)
            maxSegmentSize = (
                CER_MAX_BIT_STRING_SEGMENT_SIZE if isBitString
                else CER_MAX_CHUNK_SIZE)
            segmentCount = 0
            cursor = contentStart

            while cursor < contentEnd:
                if length < 0 and substrate[cursor:cursor + 2] == _END_OF_CONTENTS:
                    cursor += 2
                    break

                segmentFirstByte, segmentTagNumber, segmentLength, \
                    segmentStart = _read_tlv_header(substrate, cursor)

                if segmentFirstByte & _TAG_CONSTRUCTED_BIT or \
                        (segmentFirstByte & _TAG_CLASS_MASK) != \
                        _TAG_CLASS_UNIVERSAL or \
                        segmentTagNumber != expectedSegmentTag:
                    raise CrossValidationError(
                        'CER string segment at offset %d must be a primitive '
                        'UNIVERSAL tag 0x%02x, got 0x%02x'
                        % (cursor, expectedSegmentTag, segmentFirstByte))

                if segmentLength < 0:
                    raise CrossValidationError(
                        'String segment 0x%02x uses indefinite length'
                        % segmentFirstByte)

                segmentEnd = segmentStart + segmentLength
                if segmentEnd > contentEnd:
                    raise CrossValidationError(
                        'String segment at offset %d overruns its container'
                        % cursor)

                if segmentLength > maxSegmentSize:
                    raise CrossValidationError(
                        'CER string segment holds %d octets, limit is %d'
                        % (segmentLength, maxSegmentSize))

                if length >= 0:
                    isFinalSegment = segmentEnd == contentEnd
                else:
                    # An indefinite-length container still carries its two
                    # end-of-contents octets after the final segment.
                    isFinalSegment = (
                        substrate[segmentEnd:segmentEnd + 2]
                        == _END_OF_CONTENTS)

                if not isFinalSegment and segmentLength != maxSegmentSize:
                    raise CrossValidationError(
                        'Non-final CER segment holds %d octets, must hold '
                        'exactly %d' % (segmentLength, maxSegmentSize))

                cursor = segmentEnd
                segmentCount += 1

            if segmentCount == 0:
                raise CrossValidationError(
                    'Constructed CER string 0x%02x contains no segments'
                    % tagByte)

            if length < 0 and substrate[cursor - 2:cursor] != _END_OF_CONTENTS:
                raise CrossValidationError(
                    'Indefinite-length TLV 0x%02x missing end-of-contents'
                    % tagByte)

            offset = cursor
            continue

        # A regular nested container: validate its contents recursively.
        offset = _walk_tlvs(substrate, contentStart, contentEnd)

    return offset


def _validate_cer_segmentation(substrate):
    """Walk the whole substrate so bad source segments cannot be hidden."""
    end = _walk_tlvs(substrate, 0, len(substrate))

    if end != len(substrate):
        raise CrossValidationError(
            'Unprocessed octets remain after CER substrate walk')


def _real_signature(value):
    """Reduce a REAL to its sign and significant mantissa digits.

    The exponent and base carry magnitude, not precision: trailing zeroes of
    a base-10 mantissa are insignificant, so 3.14159 and 3.141590 keep the
    same value with the same number of significant digits.
    """
    if value.isInf:
        return ('real-inf', value.isPlusInf)

    mantissa, base, exponent = value

    if not mantissa:
        return ('real-zero',)

    sign = -1 if mantissa < 0 else 1
    mantissa = abs(mantissa)

    if base == 10:
        while mantissa and mantissa % 10 == 0:
            mantissa //= 10
            exponent += 1

    return ('real', sign, mantissa, base, exponent, len(str(mantissa)))


def _stable_sort_key(signature):
    # Signatures mix tuples, bytes and TagSet objects that do not share a
    # total ordering; their repr is a stable, fully comparable stand-in.
    return repr(signature)


def _tag_key(tagSet):
    # The primitive/constructed format is an encoding choice (CER turns a long
    # string's tag into the constructed form), not part of the logical type, so
    # only tag class and number participate in the comparison.
    return tuple(
        (singleTag.tagClass, singleTag.tagId)
        for singleTag in tagSet.superTags)


def _primitive_tag_set(tagSet):
    # A segmented CER string arrives carrying the constructed format on its
    # tag. DER must use the primitive form, so rebuild the TagSet keeping each
    # tag's class and number but clearing the constructed bit.
    primitiveTags = [
        tag_module.Tag(singleTag.tagClass, tag_module.tagFormatSimple,
                       singleTag.tagId)
        for singleTag in tagSet.superTags
    ]

    return tag_module.TagSet(tagSet.baseTag, *primitiveTags)


def _normalized_for_der(value):
    """Return a DER-encodable copy with string tags back in primitive form.

    The decoded value keeps the constructed tag format of a segmented CER
    string, which DER must not use. Constructed types are deep-copied with
    their values first so the reported CER value is never mutated, and absent
    OPTIONAL/DEFAULT components are left untouched.
    """
    if isinstance(value, (univ.OctetString, univ.BitString)):
        if any(singleTag.tagFormat
               for singleTag in value.tagSet.superTags):
            return value.clone(tagSet=_primitive_tag_set(value.tagSet))

        return value

    if isinstance(value, base.ConstructedAsn1Type):
        normalized = value.clone(cloneValueFlag=True)

        for idx in range(len(normalized)):
            component = normalized.getComponentByPosition(
                idx, instantiate=False)

            if component is univ.noValue:
                continue

            replacement = _normalized_for_der(component)

            if replacement is not component:
                normalized[idx] = replacement

        return normalized

    return value


def _logical_signature(value):
    """Build a structure-sensitive comparison key independent of segmentation."""
    if isinstance(value, univ.Real):
        return _real_signature(value)

    if isinstance(value, univ.Choice):
        # CHOICE adds no wire element: the decoded object is the chosen
        # alternative itself, so collapse the wrapper before comparing.
        return _logical_signature(value.getComponent())

    if isinstance(value, univ.BitString):
        return ('bitstring', _tag_key(value.tagSet), len(value),
                value.asOctets())

    if isinstance(value, base.ConstructedAsn1Type):
        # Sequence/Set iterate as field-name dicts, so components are taken
        # by position; SequenceOf/SetOf support the same access protocol. The
        # class name is intentionally omitted: SET and SET OF share one tag
        # and are indistinguishable without a schema, and the canonical
        # encoders reorder SET components, so their signatures are compared as
        # order-independent multisets keyed by the container's tag set.
        componentSignatures = []

        for idx in range(len(value)):
            component = value.getComponentByPosition(idx, instantiate=False)

            if component is univ.noValue:
                # Absent OPTIONAL/DEFAULT components carry no encoded content.
                continue

            componentSignatures.append(_logical_signature(component))

        if isinstance(value, (univ.Set, univ.SetOf)):
            componentSignatures = sorted(
                componentSignatures, key=_stable_sort_key)

        return ('constructed', _tag_key(value.tagSet), componentSignatures)

    if isinstance(value, univ.OctetString):
        return ('octet-string', _tag_key(value.tagSet), value.asOctets())

    return (value.__class__.__name__, _tag_key(value.tagSet), value)


class CrossValidationReport(object):
    """Outcome of :func:`export_and_validate`."""

    def __init__(self, cerSubstrate, derSubstrate, cerValue, derValue):
        self.cerSubstrate = cerSubstrate
        self.derSubstrate = derSubstrate
        self.cerValue = cerValue
        self.derValue = derValue

    def __repr__(self):
        return ('CrossValidationReport(cer=%d octets, der=%d octets)'
                % (len(self.cerSubstrate), len(self.derSubstrate)))


def _decode_strict(codecName, decoder, substrate, asn1Spec):
    value, remainder = decoder.decode(substrate, asn1Spec=asn1Spec)

    if remainder:
        raise CrossValidationError(
            '%s decode left %d unprocessed octets'
            % (codecName, len(remainder)))

    return value


def _cross_check(cerSubstrate, derSubstrate, asn1Spec, sourceValue=None):
    _validate_cer_segmentation(cerSubstrate)

    cerValue = _decode_strict(
        'CER', cer_decoder, cerSubstrate, asn1Spec)
    derValue = _decode_strict(
        'DER', der_decoder, derSubstrate, asn1Spec)

    cerSignature = _logical_signature(cerValue)
    derSignature = _logical_signature(derValue)

    if cerSignature != derSignature:
        raise CrossValidationError(
            'CER and DER round-trips diverge: %r != %r'
            % (cerSignature, derSignature))

    # When the original ASN.1 object is available, compare against its own
    # logical content so an export cannot silently alter it. A bare Python
    # value on the schema-driven path is covered by the CER/DER agreement and
    # by both encoders enforcing the schema.
    if isinstance(sourceValue, base.Asn1Item):
        sourceSignature = _logical_signature(sourceValue)

        if cerSignature != sourceSignature:
            raise CrossValidationError(
                'Export changed the logical content: %r != %r'
                % (sourceSignature, cerSignature))

    return CrossValidationReport(
        cerSubstrate, derSubstrate, cerValue, derValue)


def validate_cer_substrate(cerSubstrate, asn1Spec=None):
    """Validate an archived CER substrate and cross-check it against DER.

    Unlike :func:`export_and_validate`, the CER octets are supplied rather
    than produced by the encoder, so a malformed segment in the archived
    original is caught by the segmentation walk before any canonical
    re-encoding could normalise it away. The recovered value is re-exported
    through DER and both decodings must agree on logical content.

    Parameters
    ----------
    cerSubstrate: :py:class:`bytes`
        The archived CER octet stream to validate.
    asn1Spec:
        Optional schema guiding the decodings.

    Returns
    -------
    : :class:`CrossValidationReport`
        The validated CER substrate, the freshly produced DER substrate and
        the values recovered from each.

    Raises
    ------
    CrossValidationError
        If the CER segmentation violates X.690, a decode fails, or the CER and
        DER round-trips do not carry the same logical content.
    """
    _validate_cer_segmentation(cerSubstrate)

    cerValue = _decode_strict(
        'CER', cer_decoder, cerSubstrate, asn1Spec)

    # The decoded object is a fully typed value (its tagSet carries any
    # implicit tagging), so it is re-encoded without re-passing asn1Spec;
    # doing so would make the schema-driven encoder materialise absent
    # OPTIONAL components as schema objects.
    derValue = _normalized_for_der(cerValue)
    derSubstrate = der_encoder.encode(derValue)

    return _cross_check(cerSubstrate, derSubstrate, asn1Spec)


def export_and_validate(value, asn1Spec=None):
    """Export one value through CER and DER and cross-check the results.

    Parameters
    ----------
    value: :py:class:`~pyasn1.type.base.PyAsn1Item` derivative
        The ASN.1 value to export.
    asn1Spec:
        Optional schema guiding both encodings.

    Returns
    -------
    : :class:`CrossValidationReport`
        Both substrates plus the values recovered from them.

    Raises
    ------
    CrossValidationError
        If the source CER segmentation violates X.690, either decode rejects
        its substrate, the round-tripped values differ, or a REAL loses or
        gains significant digits between the two codecs.
    """
    cerSubstrate = cer_encoder.encode(value, asn1Spec=asn1Spec)
    derSubstrate = der_encoder.encode(value, asn1Spec=asn1Spec)

    sourceValue = value if isinstance(value, base.Asn1Item) else None

    return _cross_check(
        cerSubstrate, derSubstrate, asn1Spec, sourceValue=sourceValue)


#: Functional alias matching the codec-level ``encode`` entry points.
encode = export_and_validate
