#
# This file is part of pyasn1 software.
#
# Copyright (c) 2026, Simon Pichugin <simon.pichugin@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import sys
import unittest

from tests.base import BaseTestCase

from pyasn1.codec import crosscheck
from pyasn1.codec.cer import encoder as cer_encoder
from pyasn1.error import PyAsn1Error
from pyasn1.type import char
from pyasn1.type import namedtype
from pyasn1.type import univ


class ExportAndValidateTestCase(BaseTestCase):
    def testMultibyteCharacterAcrossChunkBoundary(self):
        # Each '中' is three UTF-8 octets, so boundaries cut through glyphs.
        value = char.UTF8String('中' * 400)

        report = crosscheck.export_and_validate(value)

        assert report.cerValue == value
        assert report.derValue == value
        # CER is segmented, DER is not: comparison must ignore segment count.
        assert report.cerSubstrate[0] == 0x2c
        assert report.derSubstrate[0] == 0x0c

    def testMultibyteCharacterExactlyOnBoundary(self):
        value = char.UTF8String('A' * 999 + '中')

        report = crosscheck.export_and_validate(value)

        assert report.cerValue == value
        assert report.derValue == value

    def testWideCharacterStrings(self):
        for value in (char.BMPString('A' * 600),
                      char.UniversalString('中' * 300)):
            report = crosscheck.export_and_validate(value)

            assert report.cerValue == value
            assert report.derValue == value

    def testPlainOctetString(self):
        value = univ.OctetString('Q' * 1001)

        report = crosscheck.export_and_validate(value)

        assert report.cerValue == value
        assert report.derValue == value
        assert report.cerSubstrate[2] == 0x04

    def testConstructedBitString(self):
        value = univ.BitString((1, 0) * 5000)

        report = crosscheck.export_and_validate(value)

        assert report.cerValue == value
        assert report.derValue == value
        assert report.cerSubstrate[0] == 0x23

    def testNestedSetOfDifferentTags(self):
        setOf = univ.SetOf(componentType=univ.Any())
        setOf.append(univ.Integer(3))
        setOf.append(univ.OctetString('zz'))
        setOf.append(univ.Integer(1))

        sequence = univ.Sequence()
        sequence.setComponentByPosition(0, setOf)
        sequence.setComponentByPosition(1, char.UTF8String('中' * 400))

        report = crosscheck.export_and_validate(sequence)

        assert report.cerValue == report.derValue

    def testChoiceWithMultibyteString(self):
        choice = univ.Choice(componentType=namedtype.NamedTypes(
            namedtype.NamedType('text', char.UTF8String()),
            namedtype.NamedType('number', univ.Integer())))
        choice.setComponentByName('text', '中' * 400)

        report = crosscheck.export_and_validate(choice)

        assert report.cerValue == choice
        assert report.derValue == choice

    def testSchemaDrivenPath(self):
        spec = univ.Sequence(componentType=namedtype.NamedTypes(
            namedtype.NamedType('name', char.UTF8String()),
            namedtype.NamedType('count', univ.Integer())))

        report = crosscheck.export_and_validate(
            {'name': '中' * 400, 'count': 7}, asn1Spec=spec)

        assert report.cerValue == report.derValue

    def testRealSignificantDigits(self):
        value = univ.Real((314159, 10, -5))

        report = crosscheck.export_and_validate(value)

        assert report.cerValue == value
        assert report.derValue == value
        # Trailing-zero mantissa forms carry the same significant digits.
        assert crosscheck._real_signature(univ.Real((3141590, 10, -6))) == \
            crosscheck._real_signature(univ.Real((314159, 10, -5)))

    def testRealZeroAndInfinity(self):
        assert crosscheck.export_and_validate(
            univ.Real((0, 10, 0))).cerValue == 0.0
        assert crosscheck.export_and_validate(
            univ.Real(float('inf'))).cerValue.isPlusInf

    def testEncodeAlias(self):
        value = char.UTF8String('中' * 400)

        assert crosscheck.encode(value).cerValue == value

    def testExportDoesNotMaterializeDefault(self):
        value = univ.Sequence(componentType=namedtype.NamedTypes(
            namedtype.NamedType('id', univ.Integer()),
            namedtype.DefaultedNamedType('house', univ.Integer(0))))
        value['id'] = 123

        assert value.getComponentByName(
            'house', instantiate=False) is univ.noValue

        crosscheck.export_and_validate(value)

        assert value.getComponentByName(
            'house', instantiate=False) is univ.noValue

    def testRealPrecisionDifferenceIsDetected(self):
        threeDigits = univ.Real((314, 10, -2))
        sixDigits = univ.Real((314159, 10, -5))

        assert crosscheck._real_signature(threeDigits) != \
            crosscheck._real_signature(sixDigits)

    def testConcreteTaggedFragmentsDecodeAfterAssembly(self):
        # The lenient decoder accepts fragments tagged with the concrete
        # character string type as well as OCTET STRING, assembling the octets
        # before applying the UTF-8 codec once.
        from pyasn1.codec.cer import decoder as cer_decoder

        fragments = b'\x0c\x01A' * 64
        value, rest = cer_decoder.decode(b'\x2c\x81\xc0' + fragments)

        assert value == char.UTF8String('A' * 64)
        assert rest == b''


class ValidateCerSubstrateTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)
        self.goodSubstrate = cer_encoder.encode(
            char.UTF8String('A' * 2001))

    def testArchivedSubstrateRoundTripsToDer(self):
        report = crosscheck.validate_cer_substrate(self.goodSubstrate)

        assert report.cerValue == char.UTF8String('A' * 2001)
        # The DER re-export uses the primitive string tag.
        assert report.derSubstrate[0] == 0x0c

    def testShortPrimitiveSubstrateAccepted(self):
        substrate = cer_encoder.encode(char.UTF8String('hello'))

        report = crosscheck.validate_cer_substrate(substrate)

        assert report.cerValue == 'hello'

    def testOverlongSegmentRejected(self):
        tampered = bytearray(self.goodSubstrate)
        # First segment is 04 82 03 e8: turn length 1000 (0x03e8) into 1001.
        assert tampered[3] == 0x82 and tampered[4] == 0x03
        assert tampered[5] == 0xe8
        tampered[5] = 0xe9

        try:
            crosscheck.validate_cer_substrate(bytes(tampered))
        except PyAsn1Error:
            pass
        else:
            assert 0, 'overlong CER segment tolerated'

    def testWrongSegmentTagRejected(self):
        tampered = bytearray(self.goodSubstrate)
        # The canonical encoder tags segments as OCTET STRING (0x04). The
        # lenient decoder also accepts the concrete UTF8String tag (0x0c), so
        # the structural walk is what stops this non-canonical segment from
        # being normalised away.
        assert tampered[2] == 0x04
        tampered[2] = 0x0c

        try:
            crosscheck.validate_cer_substrate(bytes(tampered))
        except PyAsn1Error:
            pass
        else:
            assert 0, 'mismatched segment tag tolerated'

    def testUnsegmentedLongPrimitiveRejected(self):
        substrate = b'\x0c\x82\x07\xd1' + b'A' * 2001

        try:
            crosscheck.validate_cer_substrate(substrate)
        except PyAsn1Error:
            pass
        else:
            assert 0, 'unsegmented long primitive string tolerated'

    def testShortNonFinalSegmentRejected(self):
        # Constructed UTF8String (0x2c) whose first segment is only two
        # octets while a second segment follows: a non-final segment must
        # hold the full 1000 octets.
        substrate = (b'\x2c\x80'
                     b'\x04\x02\xaa\xaa'
                     b'\x04\x02\xbb\xbb'
                     b'\x00\x00')

        try:
            crosscheck.validate_cer_substrate(substrate)
        except PyAsn1Error:
            pass
        else:
            assert 0, 'undersized non-final segment tolerated'

    def testMultiOctetTaggedString(self):
        from pyasn1.type import tag

        implicitTag = tag.Tag(
            tag.tagClassContext, tag.tagFormatSimple, 31)
        value = char.UTF8String('中' * 400).subtype(implicitTag=implicitTag)
        spec = char.UTF8String().subtype(implicitTag=implicitTag)

        report = crosscheck.export_and_validate(value, asn1Spec=spec)

        assert report.cerValue == report.derValue
        # A tag number of 31 forces the multi-octet identifier form; the
        # constructed CER framing sets the constructed bit as well.
        assert report.cerSubstrate[0] == 0xbf
        assert report.cerSubstrate[1] == 0x1f
        assert crosscheck.validate_cer_substrate(
            report.cerSubstrate, asn1Spec=spec).cerValue == report.derValue

    def testTruncatedMultiOctetTagRejected(self):
        try:
            crosscheck.validate_cer_substrate(b'\x9f')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'truncated multi-octet tag tolerated'

    def testTruncatedSubstrateRejected(self):
        try:
            crosscheck.validate_cer_substrate(b'\x24\x82\x03')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'truncated substrate tolerated'

    def testErrorIsPyAsn1Error(self):
        assert issubclass(crosscheck.CrossValidationError, PyAsn1Error)


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
