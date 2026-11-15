#
# This file is part of pyasn1 software.
#
# Copyright (c) 2026, pyasn1 maintenance organization
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import hashlib
import sys
import unittest

from tests.base import BaseTestCase

from pyasn1.codec.ber.semindex import SemanticIndex, SemanticIndexError
from pyasn1.error import PyAsn1Error


def tlv(tag, content):
    size = len(content)
    if size < 128:
        prefix = bytes((size,))
    else:
        octets = []
        while size:
            octets.append(size & 0xFF)
            size >>= 8
        octets.reverse()
        prefix = bytes((0x80 | len(octets),)) + bytes(octets)
    return bytes((tag,)) + prefix + content


def indefinite(tag, content):
    return bytes((tag, 0x80)) + content + b'\x00\x00'


class SemanticIndexTestCase(BaseTestCase):

    def setUp(self):
        BaseTestCase.setUp(self)
        self.index = SemanticIndex()

    def _entity(self, name, substrate):
        return self.index.add(name, substrate).entity_id

    def _assertSameEntity(self, left, right):
        assert left == right, 'expected one entity, got %d and %d' % (
            left, right)

    def _assertDifferentEntity(self, left, right):
        assert left != right, 'expected distinct entities, both %d' % left

    def testDefiniteAndIndefiniteSequenceMatch(self):
        definite = tlv(0x30, tlv(0x02, b'\x01') + tlv(0x04, b'ab'))
        indefinite_len = indefinite(
            0x30, tlv(0x02, b'\x01') + tlv(0x04, b'ab'))

        self._assertSameEntity(
            self._entity('def', definite),
            self._entity('indef', indefinite_len))

    def testLongAndShortLengthFormsMatch(self):
        short = tlv(0x02, b'\x01\x00')
        long_outer = b'\x02\x82\x00\x03\x00\x01\x00'

        self._assertSameEntity(
            self._entity('short', short),
            self._entity('long', long_outer))

    def testConstructedOctetStringMatchesPrimitive(self):
        primitive = tlv(0x04, b'ab')
        constructed = tlv(0x24, tlv(0x04, b'a') + tlv(0x04, b'b'))
        indefinite_len = indefinite(
            0x24, tlv(0x04, b'a') + tlv(0x04, b'b'))

        entity = self._entity('primitive', primitive)
        self._assertSameEntity(
            entity, self._entity('constructed', constructed))
        self._assertSameEntity(
            entity, self._entity('indefinite', indefinite_len))

    def testConstructedCharacterStringMatchesPrimitive(self):
        primitive = tlv(0x0c, b'hi')
        constructed = tlv(0x2c, tlv(0x0c, b'h') + tlv(0x0c, b'i'))

        self._assertSameEntity(
            self._entity('primitive', primitive),
            self._entity('constructed', constructed))

    def testConstructedBitStringMatchesPrimitive(self):
        primitive = tlv(0x03, b'\x06\x6e\x5d\x40')
        constructed = tlv(0x23, tlv(0x03, b'\x06\x6e\x5d\x40'))

        self._assertSameEntity(
            self._entity('primitive', primitive),
            self._entity('constructed', constructed))

    def testSetMemberOrderIsIrrelevant(self):
        first = tlv(0x31, tlv(0x02, b'\x01') + tlv(0x04, b'ab'))
        second = tlv(0x31, tlv(0x04, b'ab') + tlv(0x02, b'\x01'))
        second_indef = indefinite(
            0x31, tlv(0x04, b'ab') + tlv(0x02, b'\x01'))

        entity = self._entity('first', first)
        self._assertSameEntity(entity, self._entity('second', second))
        self._assertSameEntity(
            entity, self._entity('second-indef', second_indef))

    def testSetOfMemberOrderIsIrrelevant(self):
        first = tlv(0x31, tlv(0x02, b'\x01') + tlv(0x02, b'\x02'))
        second = tlv(0x31, tlv(0x02, b'\x02') + tlv(0x02, b'\x01'))

        self._assertSameEntity(
            self._entity('first', first),
            self._entity('second', second))

    def testSequenceMemberOrderIsSignificant(self):
        first = tlv(0x30, tlv(0x02, b'\x01') + tlv(0x04, b'ab'))
        second = tlv(0x30, tlv(0x04, b'ab') + tlv(0x02, b'\x01'))

        self._assertDifferentEntity(
            self._entity('first', first),
            self._entity('second', second))

    def testSequenceOfMemberOrderIsSignificant(self):
        first = tlv(0x30, tlv(0x02, b'\x01') + tlv(0x02, b'\x02'))
        second = tlv(0x30, tlv(0x02, b'\x02') + tlv(0x02, b'\x01'))

        self._assertDifferentEntity(
            self._entity('first', first),
            self._entity('second', second))

    def testSetKeepsMemberMultiplicity(self):
        single = tlv(0x31, tlv(0x02, b'\x01'))
        doubled = tlv(
            0x31, tlv(0x02, b'\x01') + tlv(0x02, b'\x01'))

        self._assertDifferentEntity(
            self._entity('single', single),
            self._entity('doubled', doubled))

    def testDifferentStringTypesAreNotMerged(self):
        ia5 = self._entity('ia5', tlv(0x16, b'hi'))
        utf8 = self._entity('utf8', tlv(0x0c, b'hi'))
        printable = self._entity('printable', tlv(0x13, b'hi'))

        self._assertDifferentEntity(ia5, utf8)
        self._assertDifferentEntity(utf8, printable)
        self._assertDifferentEntity(ia5, printable)

    def testStringFragmentsDoNotMergeAcrossTypes(self):
        utf8 = self._entity('utf8', tlv(0x0c, b'hi'))
        utf8_fragments = self._entity(
            'utf8-frag', tlv(0x2c, tlv(0x0c, b'h') + tlv(0x0c, b'i')))
        ia5 = self._entity('ia5', tlv(0x16, b'hi'))

        self._assertSameEntity(utf8, utf8_fragments)
        self._assertDifferentEntity(utf8_fragments, ia5)

    def testRealBinaryAndCharacterEncodingsMatch(self):
        # 1.5: binary (mantissa 3, base 2, exponent -1) and decimal NR3.
        binary = tlv(0x09, bytes.fromhex('80ff03'))
        decimal = tlv(0x09, b'\x03' + b'1.5')

        self._assertSameEntity(
            self._entity('binary', binary),
            self._entity('decimal', decimal))

    def testRealZeroEncodingsMatch(self):
        empty = tlv(0x09, b'')
        decimal_zero = tlv(0x09, b'\x03' + b'0.0')

        self._assertSameEntity(
            self._entity('empty', empty),
            self._entity('decimal', decimal_zero))

    def testRealInfinitiesAreDistinctFromEachOtherAndFinite(self):
        plus = tlv(0x09, b'\x40')
        minus = tlv(0x09, b'\x41')
        finite = tlv(0x09, bytes.fromhex('80ff03'))

        plus_entity = self._entity('plus', plus)
        self._assertSameEntity(
            plus_entity, self._entity('plus-again', plus))
        self._assertDifferentEntity(
            plus_entity, self._entity('minus', minus))
        self._assertDifferentEntity(
            plus_entity, self._entity('finite', finite))

    def testBooleanEncodingsMatch(self):
        true_ff = tlv(0x01, b'\xff')
        true_01 = tlv(0x01, b'\x01')
        false = tlv(0x01, b'\x00')

        true_entity = self._entity('true-ff', true_ff)
        self._assertSameEntity(
            true_entity, self._entity('true-01', true_01))
        self._assertDifferentEntity(
            true_entity, self._entity('false', false))

    def testExplicitTagWrappersMatchAcrossLengthForms(self):
        definite = tlv(0xa0, tlv(0x02, b'\x07'))
        indefinite_len = indefinite(0xa0, tlv(0x02, b'\x07'))

        self._assertSameEntity(
            self._entity('definite', definite),
            self._entity('indefinite', indefinite_len))

    def testExplicitWrapperDiffersFromBareValue(self):
        wrapped = tlv(0xa0, tlv(0x02, b'\x07'))
        bare = tlv(0x02, b'\x07')

        self._assertDifferentEntity(
            self._entity('wrapped', wrapped),
            self._entity('bare', bare))

    def testExplicitWrapperTagNumberAndClassMatter(self):
        tag_zero = tlv(0xa0, tlv(0x02, b'\x07'))
        tag_one = tlv(0xa1, tlv(0x02, b'\x07'))
        application = tlv(0x60, tlv(0x02, b'\x07'))
        private = tlv(0xe0, tlv(0x02, b'\x07'))

        base = self._entity('context-0', tag_zero)
        self._assertDifferentEntity(
            base, self._entity('context-1', tag_one))
        self._assertDifferentEntity(
            base, self._entity('application-0', application))
        self._assertDifferentEntity(
            base, self._entity('private-0', private))

    def testHighTagNumberWrapper(self):
        # [31] EXPLICIT INTEGER 7, long-form tag 0x1f.
        wrapped = bytes.fromhex('bf1f03') + tlv(0x02, b'\x07')
        wrapped_indef = bytes.fromhex('bf1f80') + tlv(0x02, b'\x07') + b'\x00\x00'

        self._assertSameEntity(
            self._entity('definite', wrapped),
            self._entity('indefinite', wrapped_indef))

    def testUnknownPrimitiveNeverSharesEntity(self):
        first = tlv(0x80, b'\x2a')
        second = tlv(0x80, b'\x2a')

        self._assertDifferentEntity(
            self._entity('first', first),
            self._entity('second', second))

    def testUnknownPrimitiveInsideContainerBlocksEquivalence(self):
        first = tlv(0x30, tlv(0x02, b'\x01') + tlv(0x80, b'\x2a'))
        second = tlv(0x30, tlv(0x02, b'\x01') + tlv(0x80, b'\x2a'))

        self._assertDifferentEntity(
            self._entity('first', first),
            self._entity('second', second))

    def testKnownFieldsAroundUnknownStillGroupWhenUnknownAbsent(self):
        without_unknown = tlv(0x30, tlv(0x02, b'\x01'))
        without_unknown_indef = indefinite(0x30, tlv(0x02, b'\x01'))

        self._assertSameEntity(
            self._entity('definite', without_unknown),
            self._entity('indefinite', without_unknown_indef))

    def testNestedIndefiniteContainers(self):
        member_int = tlv(0x02, b'\x01')
        member_octets = tlv(0x04, b'ab')
        definite = tlv(0x30, tlv(0x31, member_int + member_octets))
        indefinite_len = indefinite(
            0x30, indefinite(0x31, member_octets + member_int))

        self._assertSameEntity(
            self._entity('definite', definite),
            self._entity('indefinite', indefinite_len))

    def testOriginalsAreRetainedVerbatim(self):
        substrate = indefinite(0x30, tlv(0x02, b'\x01'))
        record = self.index.add('original', substrate)

        assert record.substrate == substrate
        assert record.length == len(substrate)
        assert record.fingerprint == hashlib.sha256(
            self.index.fingerprint(substrate)).hexdigest()

        serialized = self.index.to_dict()
        assert serialized['records'][0]['original_sha256'] == hashlib.sha256(
            substrate).hexdigest()

    def testEntityNumbersAreStableAndDense(self):
        first = self._entity('first', tlv(0x02, b'\x01'))
        second = self._entity('second', tlv(0x02, b'\x02'))
        first_again = self._entity('first-again', tlv(0x02, b'\x01'))

        assert first == 1
        assert second == 2
        assert first_again == 1
        assert self.index.entities == {1: ['first', 'first-again'],
                                       2: ['second']}

    def testFingerprintIsStableAcrossInstances(self):
        substrate = tlv(0x30, tlv(0x02, b'\x01') + tlv(0x04, b'ab'))

        first = SemanticIndex().fingerprint(substrate)
        second = SemanticIndex().fingerprint(substrate)

        assert first == second

    def testObjectIdentifierAndNull(self):
        oid = tlv(0x06, bytes.fromhex('2a0304'))
        null = tlv(0x05, b'')

        assert self._entity('oid', oid) == self._entity(
            'oid-again', oid)
        assert self._entity('null', null) == self._entity(
            'null-again', null)
        self._assertDifferentEntity(
            self._entity('oid', oid), self._entity('null', null))

    def testTruncatedSubstrateIsRejected(self):
        try:
            self.index.fingerprint(tlv(0x02, b'\x01')[:-1])
        except PyAsn1Error:
            pass
        else:
            assert 0, 'truncated substrate tolerated'

    def testTrailingOctetsAreRejected(self):
        try:
            self.index.fingerprint(tlv(0x02, b'\x01') + b'\x00')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'trailing octets tolerated'

    def testIndefiniteLengthOnPrimitiveIsRejected(self):
        try:
            self.index.fingerprint(b'\x02\x80\x01\x00\x00')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'indefinite primitive tolerated'

    def testEmptySubstrateIsRejected(self):
        try:
            self.index.fingerprint(b'')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'empty substrate tolerated'

    def testTextSubstrateIsRejected(self):
        try:
            self.index.fingerprint('3000')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'text substrate tolerated'

    def testSemanticIndexErrorIsPyAsn1Error(self):
        assert issubclass(SemanticIndexError, PyAsn1Error)

    def testMalformedScalarContentIsRejected(self):
        # REAL with the binary-encoding flag set but no value octets.
        try:
            self.index.fingerprint(tlv(0x09, b'\x80'))
        except SemanticIndexError:
            pass
        else:
            assert 0, 'malformed REAL content tolerated'

    def testConstructedRealIsRejected(self):
        # X.690 defines no constructed encoding for REAL (tag 0x29).
        try:
            self.index.fingerprint(tlv(0x29, tlv(0x09, b'\x40')))
        except SemanticIndexError:
            pass
        else:
            assert 0, 'constructed REAL tolerated'


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
