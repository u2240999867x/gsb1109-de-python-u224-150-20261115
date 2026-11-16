#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import sys
import unittest

from tests.base import BaseTestCase

from pyasn1 import error
from pyasn1.codec.ber import decoder
from pyasn1.codec.ber import encoder
from pyasn1.codec.ber import validator
from pyasn1.type import constraint
from pyasn1.type import namedtype
from pyasn1.type import tag
from pyasn1.type import univ

Digit = constraint.ValueRangeConstraint(0, 9)
ShortText = constraint.ValueSizeConstraint(0, 2)


class Choice(univ.Choice):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('number', univ.Integer().subtype(
            subtypeSpec=Digit)),
        namedtype.NamedType('text', univ.OctetString().subtype(
            subtypeSpec=ShortText))
    )


class Numbers(univ.SequenceOf):
    componentType = univ.Integer().subtype(subtypeSpec=Digit)
    subtypeSpec = constraint.ValueSizeConstraint(2, 3)


class Record(univ.Sequence):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('id', univ.Integer().subtype(
            subtypeSpec=Digit)),
        namedtype.NamedType('pick', Choice()),
        namedtype.NamedType('nums', Numbers())
    )


def encodeLength(length):
    if length < 128:
        return bytes([length])

    octets = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(octets)]) + octets


def tlv(identifier, payload, indefinite=False):
    if indefinite:
        return bytes([identifier, 0x80]) + payload + b'\x00\x00'

    return bytes([identifier]) + encodeLength(len(payload)) + payload


def integer(value):
    payload = value.to_bytes((value.bit_length() + 8) // 8, 'big', signed=True)
    return tlv(0x02, payload)


def record(idValue, choiceTag, choiceValue, numbers):
    body = integer(idValue)
    body += tlv(choiceTag, choiceValue)
    body += tlv(0x30, b''.join(integer(number) for number in numbers))
    return tlv(0x30, body)


class BatchDecodeTestCase(BaseTestCase):

    def testAcceptedObjectsSurviveAFailingNeighbour(self):
        batch = record(1, 0x02, b'\x01', (1, 2))
        batch += tlv(0x30, integer(1))
        batch += record(2, 0x02, b'\x02', (3, 4))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.objects) == 2, 'complete objects were lost'
        assert [item.value['id'] for item in report.objects] == [1, 2]
        assert len(report.failures) == 1
        assert report.failures[0].kind == validator.DECODE
        assert report.failures[0].range == (16, 21)
        assert report.failures[0].raw == tlv(0x30, integer(1))

    def testAcceptedObjectsCarryOriginalSchema(self):
        report = validator.validate(
            record(1, 0x02, b'\x01', (1, 2)), asn1Spec=Record())

        assert report.ok
        value = report.objects[0].value
        assert type(value) is Record, 'relaxed schema class leaked'
        assert type(value['pick']) is Choice
        assert type(value['nums']) is Numbers
        assert value.isSameTypeWith(Record())
        assert value.componentType.namedTypes[0].asn1Object.subtypeSpec
        assert value.readOnly['componentType'] is Record.componentType

    def testRangesPointAtOriginalSubstrate(self):
        first = record(1, 0x02, b'\x01', (1, 2))
        batch = first + record(2, 0x02, b'\x02', (3, 4))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.objects) == 2
        assert report.objects[0].range == (0, len(first))
        assert report.objects[0].raw == first
        assert report.objects[1].range == (len(first), len(batch))

    def testMalformedFramingDoesNotSwallowFollowingObjects(self):
        batch = b'\x30\x09\x02\x01' + record(7, 0x02, b'\x07', (7, 8))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.failures) == 1, 'only the broken item should fail'
        assert report.failures[0].kind == validator.DECODE
        assert report.failures[0].range[0] == 0
        assert report.failures[0].range[1] < len(batch)

        assert len(report.objects) == 1
        assert report.objects[0].value['id'] == 7

    def testIndefiniteLengthObjects(self):
        body = integer(1) + tlv(0x04, b'abc') + tlv(
            0x30, integer(2) + integer(3))

        batch = tlv(0x30, body, indefinite=True)
        batch += record(1, 0x02, b'\x09', (1, 2))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.objects) == 1
        assert report.failures[0].path == '$.pick.text'
        assert report.failures[0].kind == validator.VALUE

    def testEmptySubstrate(self):
        report = validator.validate(b'', asn1Spec=Record())

        assert not report.objects
        assert not report.failures
        assert report.ok

    def testSchemaLessBatch(self):
        batch = integer(1) + tlv(0x04, b'ab')

        report = validator.validate(batch)

        assert len(report.objects) == 2
        assert isinstance(report.objects[0].value, univ.Integer)
        assert isinstance(report.objects[1].value, univ.OctetString)
        assert not report.failures

    def testBinaryStreamSubstrate(self):
        import io

        batch = record(1, 0x02, b'\x01', (1, 2))

        report = validator.validate(io.BytesIO(batch), asn1Spec=Record())

        assert len(report.objects) == 1
        assert report.objects[0].value['id'] == 1

    def testUnsupportedSubstrate(self):
        try:
            validator.validate(object(), asn1Spec=Record())

        except error.PyAsn1Error:
            pass

        else:
            assert False, 'unsupported substrate tolerated'


class ChoicePathTestCase(BaseTestCase):

    def testFailingChoiceBranchUsesItsOwnName(self):
        # The wire carries the OctetString alternative (`text`); the failure
        # must be reported as $.pick.text, never as the other branch `number`.
        batch = record(1, 0x04, b'abc', (1, 2))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.failures) == 1
        failure = report.failures[0]
        assert failure.path == '$.pick.text'
        assert 'number' not in failure.path
        assert failure.kind == validator.VALUE
        assert failure.constraintName == 'ValueSizeConstraint'
        assert failure.range == (5, 10)
        assert failure.raw == tlv(0x04, b'abc')

    def testFailingChoiceIntegerBranch(self):
        batch = record(1, 0x02, b'\x63', (1, 2))

        report = validator.validate(batch, asn1Spec=Record())

        failure = report.failures[0]
        assert failure.path == '$.pick.number'
        assert 'text' not in failure.path
        assert failure.constraintName == 'ValueRangeConstraint'

    def testNestedChoiceUsesInnerName(self):
        class Inner(univ.Choice):
            componentType = namedtype.NamedTypes(
                namedtype.NamedType('small', univ.Integer().subtype(
                    subtypeSpec=Digit)),
                namedtype.NamedType('blob', univ.OctetString().subtype(
                    subtypeSpec=ShortText))
            )

        class Outer(univ.Choice):
            componentType = namedtype.NamedTypes(
                namedtype.NamedType('inner', Inner())
            )

        batch = tlv(0x04, b'abc')

        report = validator.validate(batch, asn1Spec=Outer())

        assert len(report.failures) == 1
        assert report.failures[0].path == '$.inner.blob'

    def testExplicitlyTaggedChoice(self):
        choose = Choice().subtype(
            explicitTag=tag.Tag(
                tag.tagClassContext, tag.tagFormatConstructed, 3))

        bad = tlv(0xA3, integer(99))
        good = tlv(0xA3, integer(3))

        report = validator.validate(bad + good, asn1Spec=choose)

        assert [(failure.path, failure.kind) for failure in
                report.failures] == [('$.number', validator.VALUE)]
        assert report.failures[0].range == (2, 5)
        assert len(report.objects) == 1


class SubstrateTestCase(BaseTestCase):

    def testByteArrayAndMemoryView(self):
        batch = record(1, 0x02, b'\x01', (1, 2))

        for substrate in (bytearray(batch), memoryview(batch),
                          univ.OctetString(batch)):
            report = validator.validate(substrate, asn1Spec=Record())
            assert len(report.objects) == 1

    def testTextStreamIsRejected(self):
        import io

        try:
            validator.validate(io.StringIO(''), asn1Spec=Record())

        except error.PyAsn1Error:
            pass

        else:
            assert False, 'text-mode substrate tolerated'

    def testTrailingBrokenItemIsReportedOnce(self):
        batch = record(1, 0x02, b'\x01', (1, 2)) + b'\x30\x05'

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.objects) == 1
        assert len(report.failures) == 1
        assert report.failures[0].kind == validator.DECODE
        assert report.failures[0].range == (0x10, 0x12)

    def testRendering(self):
        batch = record(1, 0x02, b'\x63', (1, 2))

        report = validator.validate(batch, asn1Spec=Record())
        failure = report.failures[0]

        assert 'ValidationReport object' in repr(report)
        assert 'Diagnostic object' in repr(failure)
        assert failure.message.startswith('$.pick.number: ')
        assert 'ValueRangeConstraint' in failure.message

        accepted = validator.validate(
            record(1, 0x02, b'\x01', (1, 2)),
            asn1Spec=Record()).objects[0]

        assert 'ValidationItem object' in repr(accepted)


class RecoveryTestCase(BaseTestCase):

    def testLongMalformedPrefixStillFindsNextObject(self):
        batch = (b'\x30\xff' + b'\x00' * 5000 +
                 record(1, 0x02, b'\x01', (1, 2)))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.objects) == 1
        assert len(report.failures) == 1
        assert report.failures[0].kind == validator.DECODE
        assert report.failures[0].range[1] <= len(batch)

    def testManyFalseStartsStayBounded(self):
        # Every 0x30 looks like a Record but decodes against the CHOICE
        # schema only by accident; all of them are reported, none is lost.
        noise = record(1, 0x02, b'\x01', (1, 2)) * 50

        class One(univ.Choice):
            componentType = namedtype.NamedTypes(
                namedtype.NamedType('number', univ.Integer()))

        batch = noise + integer(3)

        report = validator.validate(batch, asn1Spec=One())

        assert [int(item.value.getComponent())
                for item in report.objects] == [3]
        assert len(report.failures) == len(noise) // len(
            record(1, 0x02, b'\x01', (1, 2)))

    def testUnsatisfiableEndIsOneFailure(self):
        report = validator.validate(b'\x30\x80', asn1Spec=Record())

        assert not report.objects
        assert len(report.failures) == 1
        assert report.failures[0].kind == validator.DECODE

    def testSchemaLessRecoveryScansFragments(self):
        # Without a schema a damaged header is followed by fragments; they
        # are reported individually rather than swallowed.
        batch = b'\x30\x80' + integer(1) + tlv(0x04, b'ab')

        report = validator.validate(batch)

        assert report.failures
        assert report.objects

    def testLongFormTagAndLength(self):
        # [128] IMPLICIT OCTET STRING needs a long-form tag and a long-form
        # length; both must survive the framing walk untouched.
        longTag = bytes([0x9F, 0x81, 0x00])
        payload = b'\x01'
        batch = longTag + bytes([0x81, 0x01]) + payload
        spec = univ.OctetString().subtype(
            implicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 128))

        report = validator.validate(batch, asn1Spec=spec)

        assert len(report.objects) == 1
        assert report.objects[0].value.asOctets() == payload
        assert report.objects[0].range == (0, len(batch))

    def testNestingDepthIsReportedWithoutSplitting(self):
        def encodeLength(length):
            if length < 128:
                return bytes([length])

            octets = length.to_bytes((length.bit_length() + 7) // 8, 'big')
            return bytes([0x80 | len(octets)]) + octets

        batch = b'\x05\x00'
        for _ in range(120):
            batch = b'\x30' + encodeLength(len(batch)) + batch

        report = validator.validate(batch)

        assert not report.objects
        assert len(report.failures) == 1
        assert 'nesting depth' in report.failures[0].reason


class DiagnosticClassificationTestCase(BaseTestCase):

    def testSizeAndValueFailuresAreSeparate(self):
        batch = record(1, 0x02, b'\x01', (1,))
        batch += record(99, 0x02, b'\x01', (1, 99))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.sizeFailures) == 1, 'size failure not isolated'
        assert report.sizeFailures[0].path == '$.nums'
        assert report.sizeFailures[0].itemIndex == 0
        assert report.sizeFailures[0].constraintName == 'ValueSizeConstraint'

        assert len(report.valueFailures) == 2
        assert [failure.path for failure in report.valueFailures] == [
            '$.id', '$.nums[1]']
        assert [failure.itemIndex for failure in
                report.valueFailures] == [1, 1]

        assert not report.decodeFailures
        assert not report.ok

    def testPerItemGroupingKeepsKindsApart(self):
        batch = record(1, 0x02, b'\x01', (1,))

        report = validator.validate(batch, asn1Spec=Record())

        sizes, values = report.itemFailures(0)
        assert [failure.path for failure in sizes] == ['$.nums']
        assert values == ()
        assert report.itemFailures(99) == ((), ())

    def testNestedSizeAndValueAreClassifiedByOwner(self):
        class Inner(univ.SequenceOf):
            componentType = univ.OctetString().subtype(
                subtypeSpec=ShortText)
            subtypeSpec = constraint.ValueSizeConstraint(2, 3)

        class Outer(univ.Sequence):
            componentType = namedtype.NamedTypes(
                namedtype.NamedType('x', Inner()))

        batch = tlv(0x30, tlv(0x30, tlv(0x04, b'abc')))

        report = validator.validate(batch, asn1Spec=Outer())

        assert [(failure.path, failure.kind)
                for failure in report.failures] == [
            ('$.x', validator.SIZE), ('$.x[0]', validator.VALUE)]

    def testConstraintObjectIsTheInnermostOne(self):
        batch = record(1, 0x02, b'\x01', (1, 99))

        report = validator.validate(batch, asn1Spec=Record())

        failure = report.failures[0]
        assert isinstance(failure.constraint, constraint.ValueRangeConstraint)
        assert failure.constraint.start == 0
        assert failure.constraint.stop == 9
        assert 'ValueRangeConstraint' in failure.constraintText

    def testDiagnosticAsDict(self):
        batch = record(1, 0x02, b'\x63', (1, 2))

        failure = validator.validate(batch, asn1Spec=Record()).failures[0]
        record_ = failure.asDict()

        assert record_['path'] == '$.pick.number'
        assert record_['kind'] == validator.VALUE
        assert record_['constraint'] == 'ValueRangeConstraint'
        assert record_['range'] == failure.range
        assert record_['raw'] == failure.raw
        assert failure.fieldPath == failure.path
        assert failure.originalRange == failure.range
        assert failure.original == failure.raw

    def testSequenceSizeConstraintIsASizeFailure(self):
        class Couple(univ.Sequence):
            componentType = namedtype.NamedTypes(
                namedtype.NamedType('a', univ.Integer()),
                namedtype.OptionalNamedType('b', univ.Integer())
            )
            subtypeSpec = constraint.WithComponentsConstraint(
                ('b', constraint.ComponentPresentConstraint()))

        batch = tlv(0x30, integer(1))

        report = validator.validate(batch, asn1Spec=Couple())

        assert len(report.failures) == 1
        assert report.failures[0].kind == validator.VALUE
        assert report.failures[0].path == '$'


class ReportMappingTestCase(BaseTestCase):

    def testAcceptedValuesProperty(self):
        batch = record(1, 0x02, b'\x01', (1, 2))
        batch += record(2, 0x02, b'\x02', (3, 4))

        report = validator.validate(batch, asn1Spec=Record())

        assert [int(value['id']) for value in report.values] == [1, 2]
        assert report.accepted == report.objects
        assert report.diagnostics == report.failures
        assert len(report) == 2
        assert list(report) == list(report.objects)


class RobustnessTestCase(BaseTestCase):

    def testDeeplyNestedValueDoesNotLoseTheItem(self):
        class Node(univ.Sequence):
            pass

        Node.componentType = namedtype.NamedTypes(
            namedtype.OptionalNamedType('next', Node()),
            namedtype.NamedType('v', univ.Integer().subtype(
                subtypeSpec=Digit))
        )

        payload = b''
        for _ in range(3):
            payload = tlv(0x30, payload + integer(1))

        # `next` precedes `v` in the schema, so each wrapper carries the
        # nested node first and its own value last.  The leaf node holds the
        # single violating `v`.
        bad = tlv(0x30, integer(99))
        for _ in range(3):
            bad = tlv(0x30, bad + integer(1))

        batch = payload + bad

        report = validator.validate(batch, asn1Spec=Node())

        assert len(report.objects) == 1
        assert report.failures[0].path == '$.next.next.next.v'
        assert report.failures[0].constraintName == 'ValueRangeConstraint'

    def testMissingComponentIsADecodeFailure(self):
        batch = tlv(0x30, integer(1))

        report = validator.validate(batch, asn1Spec=Record())

        assert len(report.failures) == 1
        assert report.failures[0].kind == validator.DECODE
        assert report.failures[0].range == (0, 5)

    def testNoFailureLeaksASubsequentObjectOnDecodeError(self):
        broken = b'\x30\x0c\x02\x01' + record(5, 0x02, b'\x05', (5, 6))

        report = validator.validate(broken, asn1Spec=Record())

        assert any(failure.kind == validator.DECODE
                   for failure in report.failures)
        assert [int(item.value['id']) for item in report.objects] == [5]


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
