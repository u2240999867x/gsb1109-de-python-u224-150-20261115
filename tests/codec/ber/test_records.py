#
# This file is part of pyasn1 software.
#
# Copyright (c) 2026, Simon Pichugin <simon.pichugin@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import io
import sys
import unittest

from tests.base import BaseTestCase

from pyasn1 import error
from pyasn1.codec.ber import decoder as decoderModule
from pyasn1.codec.ber import encoder
from pyasn1.codec.ber import records
from pyasn1.type import tag
from pyasn1.type import univ


def _encodeLength(length):
    if length < 128:
        return bytes([length])

    octets = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(octets)]) + octets


def _wrap(identifier, body, indefinite=False):
    if indefinite:
        return identifier + b'\x80' + body + b'\x00\x00'

    return identifier + _encodeLength(len(body)) + body


class RecordDecoderTestCase(BaseTestCase):

    def setUp(self):
        BaseTestCase.setUp(self)

        self.integers = [univ.Integer(0), univ.Integer(127), univ.Integer(-1)]
        self.payloads = [encoder.encode(value) for value in self.integers]
        self.substrate = b''.join(self.payloads)

    def testSingleRecord(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.payloads[0])

        record = recordDecoder.read()

        assert record.asn1Object == self.integers[0]
        assert record.start == 0
        assert record.end == len(self.payloads[0])
        assert recordDecoder.position == record.end

    def testMultipleRecords(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        values = [record.asn1Object for record in recordDecoder]
        ranges = [(record.start, record.end) for record in recordDecoder.records()]

        assert values == self.integers
        assert ranges == []

        recordDecoder.feed(b'')
        assert recordDecoder.read() is None

    def testConsumedRanges(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        expectedStart = 0
        for value, payload in zip(self.integers, self.payloads):
            record = recordDecoder.read()

            assert record.asn1Object == value
            assert record.start == expectedStart
            assert record.end == expectedStart + len(payload)
            assert record.consumed == len(payload)
            assert record.byteRange == (record.start, record.end)

            expectedStart = record.end

        assert expectedStart == len(self.substrate)

    def testSchemaDrivenRecord(self):
        spec = univ.OctetString().subtype(
            implicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 100000))
        value = univ.OctetString(b'x' * 200).subtype(
            implicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 100000))

        payload = encoder.encode(value)

        recordDecoder = records.RecordDecoder(asn1Spec=spec)
        recordDecoder.feed(payload)

        record = recordDecoder.read()

        assert record.asn1Object == value
        assert record.start == 0
        assert record.end == len(payload)


class RecordSplitTestCase(BaseTestCase):

    def setUp(self):
        BaseTestCase.setUp(self)

        self.value = univ.OctetString(b'x' * 200).subtype(
            implicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 100000))
        self.spec = univ.OctetString().subtype(
            implicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 100000))
        self.payload = encoder.encode(self.value)

    def testEveryTwoWaySplit(self):
        for cut in range(1, len(self.payload)):
            with self.subTest(cut=cut):
                recordDecoder = records.RecordDecoder(asn1Spec=self.spec)

                recordDecoder.feed(self.payload[:cut])
                assert recordDecoder.read() is None

                recordDecoder.feed(self.payload[cut:])

                record = recordDecoder.read()

                assert record is not None
                assert record.asn1Object == self.value
                assert record.end == len(self.payload)

    def testByteAtATime(self):
        recordDecoder = records.RecordDecoder(asn1Spec=self.spec)

        for index, octet in enumerate(self.payload):
            recordDecoder.feed(bytes([octet]))

            if index + 1 < len(self.payload):
                assert recordDecoder.read() is None, (
                    'Record delivered before its final octet at %d' % index)

        record = recordDecoder.read()

        assert record is not None
        assert record.asn1Object == self.value
        assert record.end == len(self.payload)
        assert recordDecoder.read() is None

    def testSplitTagAndLength(self):
        # Context tag 0x1F85 0x14 (tag id 100000), then an eight-octet length.
        payload = b'\x9f\x86\x8d\x20\x88' + (200).to_bytes(8, 'big') + b'x' * 200

        recordDecoder = records.RecordDecoder(asn1Spec=self.spec)

        recordDecoder.feed(payload[:2])
        assert recordDecoder.read() is None

        recordDecoder.feed(payload[2:6])
        assert recordDecoder.read() is None

        recordDecoder.feed(payload[6:])

        record = recordDecoder.read()

        assert record is not None
        assert record.asn1Object == self.value
        assert record.end == len(payload)


class RecordResumeTestCase(BaseTestCase):

    def setUp(self):
        BaseTestCase.setUp(self)

        self.payloads = [
            encoder.encode(univ.Integer(index)) for index in range(3)
        ]
        self.substrate = b''.join(self.payloads)

    def testResumeAfterAcknowledgement(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.payloads[0])

        first = recordDecoder.read()
        assert recordDecoder.acknowledge() == first.end

        # A reconnect resumes at the acknowledged position and resends the
        # rest of the channel.
        recordDecoder.feed(self.substrate[recordDecoder.acknowledged:])

        remaining = [record.asn1Object for record in recordDecoder]

        assert remaining == [univ.Integer(1), univ.Integer(2)]
        assert recordDecoder.position == len(self.substrate)

    def testResendingLastUnacknowledgedBlock(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate[:4])

        first = recordDecoder.read()
        assert first.end == 3

        # The second record is incomplete at this point; resend the whole
        # unacknowledged tail, including the already delivered record.
        recordDecoder.feed(
            self.substrate[recordDecoder.acknowledged:],
            offset=recordDecoder.acknowledged
        )

        recordsRead = list(recordDecoder)

        assert [record.asn1Object for record in recordsRead] == [
            univ.Integer(1), univ.Integer(2)
        ]

        recordDecoder.acknowledge(len(self.substrate))
        assert list(recordDecoder) == []

    def testFullRetransmitAfterAcknowledgement(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        first = recordDecoder.read()
        recordDecoder.acknowledge(first.end)

        recordDecoder.feed(self.substrate, offset=0)

        remaining = [record.asn1Object for record in recordDecoder]

        assert remaining == [univ.Integer(1), univ.Integer(2)]

    def testRepeatedResendDoesNotDuplicate(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        assert [record.asn1Object for record in recordDecoder] == [
            univ.Integer(0), univ.Integer(1), univ.Integer(2)
        ]

        assert recordDecoder.acknowledge(len(self.substrate)) == len(self.substrate)

        for _ in range(3):
            recordDecoder.feed(self.substrate, offset=0)
            assert list(recordDecoder) == []

        assert recordDecoder.acknowledged == len(self.substrate)

    def testConflictingRetransmissionRejected(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.payloads[0])

        try:
            recordDecoder.feed(b'\x02\x01\x7f', offset=0)
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Conflicting retransmission tolerated'

        # The original record is still deliverable.
        record = recordDecoder.read()
        assert record.asn1Object == univ.Integer(0)

    def testAcknowledgeBeyondDeliveredRejected(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        try:
            recordDecoder.acknowledge(1)
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Acknowledgement beyond delivered records tolerated'

        record = recordDecoder.read()
        assert record.end == 3
        assert recordDecoder.acknowledge() == 3

    def testAcknowledgeCannotCrossRecordBoundary(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        recordDecoder.read()
        recordDecoder.read()

        try:
            recordDecoder.acknowledge(4)
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Mid-record acknowledgement tolerated'

    def testAcknowledgementIsMonotonic(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        recordDecoder.read()
        recordDecoder.acknowledge()

        try:
            recordDecoder.acknowledge(0)
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Backwards acknowledgement tolerated'


class RecordIndefiniteLengthTestCase(BaseTestCase):

    def setUp(self):
        BaseTestCase.setUp(self)

        # SEQUENCE { SEQUENCE { INTEGER 1 } INTEGER 2 }, every container
        # encoded with indefinite length.
        self.substrate = (
            b'\x30\x80'
            b'\x30\x80\x02\x01\x01\x00\x00'
            b'\x02\x01\x02'
            b'\x00\x00'
        )

    def testOuterNotConfirmedBeforeNestedEoo(self):
        recordDecoder = records.RecordDecoder()

        for index, octet in enumerate(self.substrate[:-1]):
            recordDecoder.feed(bytes([octet]))

            assert list(recordDecoder) == [], (
                'Nested indefinite container confirmed at octet %d' % (index + 1))

        assert recordDecoder.position == 0

        recordDecoder.feed(self.substrate[-1:])

        record = recordDecoder.read()

        assert record is not None
        assert record.end == len(self.substrate)
        assert recordDecoder.acknowledge() == len(self.substrate)

    def testNestedDefiniteInsideIndefinite(self):
        substrate = (
            b'\x30\x80'
            b'\x30\x06\x02\x01\x01\x02\x01\x02'
            b'\x00\x00'
        )

        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(substrate)

        record = recordDecoder.read()

        assert record.end == len(substrate)
        assert recordDecoder.acknowledge(len(substrate)) == len(substrate)

    def testIndefiniteRecordFollowedByNextRecord(self):
        substrate = self.substrate + encoder.encode(univ.Integer(3))

        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(substrate)

        first = recordDecoder.read()
        assert first.end == len(self.substrate)

        second = recordDecoder.read()
        assert second.asn1Object == univ.Integer(3)
        assert second.start == len(self.substrate)

    def testPrimitiveIndefiniteRejected(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\x02\x80\x00\x00')
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Primitive indefinite length accepted'

    def testMalformedEooRejected(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\x30\x80\x00\x01\x00')
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Malformed end-of-octets octets accepted'

    def testElementOverrunsEnclosingLength(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\x30\x03\x04\x05abc')
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Element overrunning enclosing length accepted'

    def testIndefiniteChildOverrunsDefiniteParent(self):
        # SEQUENCE (definite, 4 octets) containing an indefinite SEQUENCE
        # whose EOO lies beyond the parent's extent.
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\x30\x04\x30\x80\x00\x00\x00\x00')
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Indefinite child overran definite parent'

    def testIndefiniteChildClosedAtParentEnd(self):
        # SEQUENCE (definite) containing exactly an indefinite SEQUENCE,
        # its contents and EOO: the child ends where the parent ends.
        child = b'\x30\x80\x02\x01\x01\x00\x00'
        substrate = b'\x30' + bytes([len(child)]) + child

        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(substrate)

        record = recordDecoder.read()

        assert record is not None
        assert record.end == len(substrate)


class RecordLimitTestCase(BaseTestCase):

    def testNestingUnderLimitFramed(self):
        inner = b'\x05\x00'
        for _ in range(50):
            inner = _wrap(b'\x30', inner)

        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(inner)

        record = recordDecoder.read()

        assert record is not None
        assert record.end == len(inner)

    def testNestingOverLimitRejected(self):
        inner = b'\x05\x00'
        for _ in range(150):
            inner = _wrap(b'\x30', inner)

        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(inner)
        except error.PyAsn1Error as exc:
            assert 'nesting depth' in str(exc).lower(), (
                'Unexpected error message %s' % exc)
        else:
            assert False, 'Deeply nested record accepted'

    def testExactNestingLimitFramed(self):
        inner = b'\x05\x00'
        for _ in range(decoderModule.MAX_NESTING_DEPTH):
            inner = _wrap(b'\x30', inner)

        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(inner)

        record = recordDecoder.read()

        assert record is not None
        assert record.end == len(inner)

    def testOneOverNestingLimitRejected(self):
        inner = b'\x05\x00'
        for _ in range(decoderModule.MAX_NESTING_DEPTH + 1):
            inner = _wrap(b'\x30', inner)

        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(inner)
        except error.PyAsn1Error as exc:
            assert str(decoderModule.MAX_NESTING_DEPTH) in str(exc), exc
        else:
            assert False, 'Record one level over the nesting limit accepted'

    def testOversizedTagRejected(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\xbf' + b'\x81' * 30)
        except error.PyAsn1Error as exc:
            assert 'tag' in str(exc).lower(), exc
        else:
            assert False, 'Oversized tag accepted'

    def testOversizedLengthRejected(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\x04\x89' + b'\x01' * 9)
        except error.PyAsn1Error as exc:
            assert 'length' in str(exc).lower(), exc
        else:
            assert False, 'Oversized length field accepted'


class RecordUtilityTestCase(BaseTestCase):

    def testDecodeRecordsFromBytes(self):
        substrate = b''.join(
            encoder.encode(univ.Integer(index)) for index in range(4))

        recordsRead = records.decodeRecords(substrate)

        assert [record.asn1Object for record in recordsRead] == [
            univ.Integer(0), univ.Integer(1), univ.Integer(2), univ.Integer(3)
        ]
        assert [record.start for record in recordsRead] == [0, 3, 6, 9]
        assert [record.end for record in recordsRead] == [3, 6, 9, 12]

    def testDecodeRecordsFromChunkIterable(self):
        substrate = b''.join(
            encoder.encode(univ.Integer(index)) for index in range(2))

        chunks = [substrate[:2], substrate[2:5], substrate[5:]]

        recordsRead = records.decodeRecords(chunks)

        assert [record.asn1Object for record in recordsRead] == [
            univ.Integer(0), univ.Integer(1)
        ]

    def testDecodeRecordsFromOctetString(self):
        substrate = encoder.encode(univ.Integer(5))

        recordsRead = records.decodeRecords(univ.OctetString(substrate))

        assert len(recordsRead) == 1
        assert recordsRead[0].asn1Object == univ.Integer(5)
        assert recordsRead[0].byteRange == (0, len(substrate))

    def testConstructorSubstrate(self):
        substrate = encoder.encode(univ.Integer(6))

        recordDecoder = records.RecordDecoder(substrate)

        assert len(recordDecoder) == 1
        assert recordDecoder.hasPendingRecords
        assert recordDecoder.received == len(substrate)
        assert recordDecoder.pending == len(substrate)
        assert recordDecoder.unacknowledged == len(substrate)
        assert recordDecoder.read().asn1Object == univ.Integer(6)
        assert not recordDecoder.hasPendingRecords

    def testRecordAliases(self):
        substrate = encoder.encode(univ.Integer(7))

        record = records.decodeRecords(substrate)[0]

        assert record.object is record.asn1Object
        assert record.value is record.asn1Object
        assert record.offset == record.start
        assert record.length == record.consumed == len(substrate)
        assert record.nextOffset == record.end
        assert record.range == record.byteRange == (0, len(substrate))


class RecordFeedValidationTestCase(BaseTestCase):

    def setUp(self):
        BaseTestCase.setUp(self)

        self.substrate = encoder.encode(univ.Integer(0)) + \
            encoder.encode(univ.Integer(1))

    def testNonBytesSubstrateRejected(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed('not bytes')
        except TypeError:
            pass
        else:
            assert False, 'Non-bytes substrate accepted'

    def testNonIntegerOffsetRejected(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\x02\x01\x00', offset='0')
        except TypeError:
            pass
        else:
            assert False, 'Non-integer offset accepted'

    def testNegativeOffsetRejected(self):
        recordDecoder = records.RecordDecoder()

        try:
            recordDecoder.feed(b'\x02\x01\x00', offset=-1)
        except ValueError:
            pass
        else:
            assert False, 'Negative offset accepted'

    def testGapInInputRejected(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        try:
            recordDecoder.feed(b'\x02\x01\x02', offset=len(self.substrate) + 1)
        except error.PyAsn1Error:
            pass
        else:
            assert False, 'Gap in input accepted'

    def testDuplicatePrefixBeforeAcknowledgement(self):
        recordDecoder = records.RecordDecoder()

        recordDecoder.feed(self.substrate[:2])
        assert recordDecoder.read() is None

        # The second delivery repeats a strict prefix of the unacknowledged
        # input; it is verified and skipped, not appended.
        recordDecoder.feed(self.substrate[:2], offset=0)

        assert recordDecoder.received == 2

        recordDecoder.feed(self.substrate[2:])

        assert [record.asn1Object for record in recordDecoder] == [
            univ.Integer(0), univ.Integer(1)
        ]

    def testCompleteDuplicateBeforeAcknowledgement(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        first = recordDecoder.read()

        recordDecoder.feed(self.substrate[:first.end], offset=0)

        assert recordDecoder.received == len(self.substrate)
        assert [record.asn1Object for record in recordDecoder] == [univ.Integer(1)]

    def testAcknowledgeNonIntegerRejected(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        try:
            recordDecoder.acknowledge('0')
        except TypeError:
            pass
        else:
            assert False, 'Non-integer acknowledgement accepted'

    def testConfirmIsAcknowledge(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        first = recordDecoder.read()

        assert recordDecoder.confirm(first.end) == first.end
        assert recordDecoder.consumed == first.end
        assert recordDecoder.consumedRange == (0, first.end)
        assert recordDecoder.ack(first.end) == first.end

    def testAcknowledgeRecord(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        first = recordDecoder.read()

        assert recordDecoder.acknowledgeRecord(first) == first.end

    def testReplayAfterReconnect(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        first = recordDecoder.read()
        recordDecoder.acknowledgeRecord(first)

        # The peer replays from the acknowledged boundary, repeating the
        # final unacknowledged block as well.
        recordDecoder.replay(self.substrate[recordDecoder.acknowledged:])

        assert [record.asn1Object for record in recordDecoder] == [univ.Integer(1)]

        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)
        recordDecoder.read()
        recordDecoder.acknowledge()
        recordDecoder.resend(self.substrate, offset=0)
        assert [record.asn1Object for record in recordDecoder] == [univ.Integer(1)]

    def testNewDecoderAtAcknowledgedOffset(self):
        recordDecoder = records.RecordDecoder()
        recordDecoder.feed(self.substrate)

        first = recordDecoder.read()
        assert first.asn1Object == univ.Integer(0)

        acknowledged = recordDecoder.acknowledge()

        # A reconnect may start a fresh decoder at the confirmed position.
        reconnected = records.RecordDecoder(offset=acknowledged)
        reconnected.feed(self.substrate[acknowledged:])

        recordsRead = list(reconnected)

        assert [record.asn1Object for record in recordsRead] == [univ.Integer(1)]
        assert recordsRead[0].start == acknowledged
        assert recordsRead[0].end == len(self.substrate)
        assert reconnected.acknowledged == acknowledged


class RecordStreamInterfaceTestCase(BaseTestCase):

    def testFileLikeSubstrate(self):
        substrate = encoder.encode(univ.Integer(1)) + encoder.encode(univ.Integer(2))

        recordsRead = records.decodeRecords(io.BytesIO(substrate))

        assert [record.asn1Object for record in recordsRead] == [
            univ.Integer(1), univ.Integer(2)
        ]


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
