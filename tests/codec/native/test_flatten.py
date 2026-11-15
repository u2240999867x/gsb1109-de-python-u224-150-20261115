#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import sys
import unittest

from tests.base import BaseTestCase

from pyasn1.type import char
from pyasn1.type import namedtype
from pyasn1.type import tag
from pyasn1.type import univ
from pyasn1.codec.native import encoder as nativeEncoder
from pyasn1.codec.native import flatten
from pyasn1.codec.ber import decoder as berDecoder
from pyasn1.codec.ber import encoder as berEncoder
from pyasn1.error import PyAsn1Error


class FlattenTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)

        contextTag0 = tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 0)
        contextTag1 = tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 1)
        contextTag2 = tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 2)

        # Both CHOICEs use context tag [0], but for different types.
        self.firstChoice = univ.Choice(componentType=namedtype.NamedTypes(
            namedtype.NamedType(
                'count',
                univ.Integer().subtype(implicitTag=contextTag0)),
            namedtype.NamedType(
                'text',
                char.UTF8String().subtype(implicitTag=contextTag1))
        ))

        self.secondChoice = univ.Choice(componentType=namedtype.NamedTypes(
            namedtype.NamedType(
                'amount',
                univ.OctetString().subtype(implicitTag=contextTag0)),
            namedtype.NamedType(
                'flag',
                univ.Boolean().subtype(implicitTag=contextTag2))
        ))

        self.entry = univ.Sequence(componentType=namedtype.NamedTypes(
            namedtype.NamedType('first', self.firstChoice),
            namedtype.NamedType('second', self.secondChoice),
            namedtype.OptionalNamedType('note', char.UTF8String()),
            namedtype.NamedType('raw', univ.Any())
        ))

        self.batch = univ.SequenceOf(componentType=self.entry)

    def _makeEntry(self, index):
        entry = self.entry.clone()
        entry.setComponentByName(
            'first',
            self.firstChoice.clone().setComponentByName('count', 100 + index))
        entry.setComponentByName(
            'second',
            self.secondChoice.clone().setComponentByName(
                'amount', b'abc%d' % index))
        entry.setComponentByName('note', 'hi')
        entry.setComponentByName('raw', univ.Any(b'\x04\x03fox'))
        return entry

    def _makeBatch(self):
        batch = self.batch.clone()
        batch.append(self._makeEntry(0))
        batch.append(self._makeEntry(1))
        return batch

    def testChoiceBranchRecorded(self):
        value = self.firstChoice.clone().setComponentByName('count', 5)
        rows = flatten.flatten(value)

        assert len(rows) == 1
        row = rows[0]
        assert row[flatten.PATH] == ('count',)
        assert row[flatten.FIELD] == 'count'
        assert row[flatten.CHOICE_BRANCH] == 'count'
        assert row[flatten.REPETITION] is None
        assert row[flatten.TYPE] == 'Integer'
        assert row[flatten.VALUE] == 5
        assert row[flatten.TAG] == '128:0:0'

    def testSameTagDifferentBranchesKeptApart(self):
        rows = flatten.flatten(self._makeEntry(0))

        ambiguous = [row for row in rows if row[flatten.TAG] == '128:0:0']
        assert len(ambiguous) == 2
        paths = [row[flatten.PATH] for row in ambiguous]
        assert paths == [('first', 'count'), ('second', 'amount')]
        branches = {row[flatten.CHOICE_BRANCH] for row in ambiguous}
        assert branches == {'count', 'amount'}
        types = {row[flatten.TYPE] for row in ambiguous}
        assert types == {'Integer', 'OctetString'}

    def testRepetitionIndexRecorded(self):
        rows = flatten.flatten(self._makeBatch())

        repetitions = [row[flatten.REPETITION]
                       for row in rows if row[flatten.FIELD] == 'count']
        assert repetitions == [0, 1]
        paths = [row[flatten.PATH]
                 for row in rows if row[flatten.FIELD] == 'amount']
        assert paths == [(0, 'second', 'amount'), (1, 'second', 'amount')]

    def testAnyPreservedVerbatim(self):
        entry = self._makeEntry(0)
        row, = [row for row in flatten.flatten(entry)
                if row[flatten.FIELD] == 'raw']

        assert row[flatten.TYPE] == 'Any'
        assert row[flatten.TAG] == ''
        assert row[flatten.VALUE] == b'\x04\x03fox'

    def testAbsentOptionalOmitted(self):
        entry = self.entry.clone()
        entry.setComponentByName(
            'first',
            self.firstChoice.clone().setComponentByName('text', 'x'))
        entry.setComponentByName(
            'second',
            self.secondChoice.clone().setComponentByName('flag', True))
        entry.setComponentByName('raw', univ.Any(b'\x05\x00'))

        fields = [row[flatten.FIELD] for row in flatten.flatten(entry)]
        assert 'note' not in fields

    def testRebuildRoundTrip(self):
        value = self._makeBatch()
        rows = flatten.flatten(value)

        rebuilt = flatten.rebuild(rows, self.batch)

        assert nativeEncoder.encode(rebuilt) == nativeEncoder.encode(value)
        assert berEncoder.encode(rebuilt) == berEncoder.encode(value)

    def testRebuildDoesNotMergeBranches(self):
        value = self._makeBatch()
        rebuilt = flatten.rebuild(flatten.flatten(value), self.batch)

        assert rebuilt[0]['first'].getName() == 'count'
        assert rebuilt[0]['second'].getName() == 'amount'
        assert rebuilt[1]['first'].getComponent() == 101
        assert rebuilt[1]['second'].getComponent() == b'abc1'

    def testKnownFieldTypesAndValuesUnchanged(self):
        rebuilt = flatten.rebuild(
            flatten.flatten(self._makeEntry(0)), self.entry)
        original = self._makeEntry(0)

        for name in ('first', 'second', 'note', 'raw'):
            assert type(rebuilt[name]) is type(original[name])
        assert rebuilt['first'].getComponent() == 100
        assert rebuilt['second'].getComponent() == b'abc0'
        assert rebuilt['raw'].asOctets() == b'\x04\x03fox'

    def testBerSubstrateIdentity(self):
        substrate = berEncoder.encode(self._makeBatch())
        decoded, _ = berDecoder.decode(substrate, asn1Spec=self.batch)

        rebuilt = flatten.rebuild(flatten.flatten(decoded), self.batch)

        assert berEncoder.encode(rebuilt) == substrate

    def testNestedChoice(self):
        inner = univ.Choice(componentType=namedtype.NamedTypes(
            namedtype.NamedType('number', univ.Integer()),
            namedtype.NamedType('text', char.UTF8String())
        ))
        outer = univ.Choice(componentType=namedtype.NamedTypes(
            namedtype.NamedType('inner', inner),
            namedtype.NamedType('direct', univ.Boolean())
        ))
        value = outer.clone().setComponentByName(
            'inner', inner.clone().setComponentByName('number', 3))

        rows = flatten.flatten(value)
        assert len(rows) == 1
        assert rows[0][flatten.PATH] == ('inner', 'number')
        assert rows[0][flatten.CHOICE_BRANCH] == 'number'

        rebuilt = flatten.rebuild(rows, outer)
        assert nativeEncoder.encode(rebuilt) == nativeEncoder.encode(value)
        assert rebuilt.getName(innerFlag=True) == 'number'

    def testScalarRoot(self):
        rows = flatten.flatten(univ.Integer(42))

        assert rows == [{
            flatten.PATH: (),
            flatten.FIELD: 'Integer',
            flatten.TAG: '0:0:2',
            flatten.TYPE: 'Integer',
            flatten.VALUE: 42,
            flatten.CHOICE_BRANCH: None,
            flatten.REPETITION: None
        }]
        rebuilt = flatten.rebuild(rows, univ.Integer())
        assert int(rebuilt) == 42

    def testEmptyContainer(self):
        spec = univ.SequenceOf(componentType=univ.Integer())
        value = spec.clone()

        assert flatten.flatten(value) == []
        assert len(flatten.rebuild(flatten.flatten(value), spec)) == 0

    def testSetRoundTrip(self):
        spec = univ.Set(componentType=namedtype.NamedTypes(
            namedtype.NamedType('number', univ.Integer()),
            namedtype.NamedType('text', char.UTF8String())
        ))
        value = spec.clone()
        value.setComponentByName('number', 7)
        value.setComponentByName('text', 'x')

        rebuilt = flatten.rebuild(flatten.flatten(value), spec)

        assert nativeEncoder.encode(rebuilt) == nativeEncoder.encode(value)
        assert berEncoder.encode(rebuilt) == berEncoder.encode(value)


class FlattenErrorTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)

        self.choice = univ.Choice(componentType=namedtype.NamedTypes(
            namedtype.NamedType('number', univ.Integer()),
            namedtype.NamedType('flag', univ.Boolean())
        ))

    def testNotAnAsn1Item(self):
        try:
            flatten.flatten('not-an-asn1-item')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'non-ASN.1 input tolerated'

    def testUnsetChoiceRejected(self):
        try:
            flatten.flatten(self.choice.clone())
        except PyAsn1Error:
            pass
        else:
            assert 0, 'unset CHOICE tolerated'

    def testInvalidSpecRejected(self):
        try:
            flatten.rebuild([{flatten.PATH: (), flatten.VALUE: 1}],
                            'not-an-asn1-item')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'invalid spec tolerated'

    def testDuplicatePathRejected(self):
        rows = [
            {flatten.PATH: ('number',), flatten.VALUE: 1},
            {flatten.PATH: ('number',), flatten.VALUE: 2}
        ]
        try:
            flatten.rebuild(rows, self.choice)
        except PyAsn1Error:
            pass
        else:
            assert 0, 'duplicate field path tolerated'

    def testMultipleChoiceBranchesRejected(self):
        rows = [
            {flatten.PATH: ('number',), flatten.VALUE: 1},
            {flatten.PATH: ('flag',), flatten.VALUE: True}
        ]
        try:
            flatten.rebuild(rows, self.choice)
        except PyAsn1Error:
            pass
        else:
            assert 0, 'multiple CHOICE branches tolerated'

    def testUnknownFieldRejected(self):
        spec = univ.Sequence(componentType=namedtype.NamedTypes(
            namedtype.NamedType('number', univ.Integer())))
        rows = [{flatten.PATH: ('unknown',), flatten.VALUE: 1}]

        try:
            flatten.rebuild(rows, spec)
        except PyAsn1Error:
            pass
        else:
            assert 0, 'unknown field tolerated'

    def testMissingScalarRowRejected(self):
        try:
            flatten.rebuild([], univ.Integer())
        except PyAsn1Error:
            pass
        else:
            assert 0, 'missing scalar row tolerated'

    def testNonContiguousRepetitionRejected(self):
        spec = univ.SequenceOf(componentType=univ.Integer())
        rows = [
            {flatten.PATH: (0,), flatten.VALUE: 1},
            {flatten.PATH: (2,), flatten.VALUE: 2}
        ]
        try:
            flatten.rebuild(rows, spec)
        except PyAsn1Error:
            pass
        else:
            assert 0, 'non-contiguous repetition indices tolerated'

    def testNonIndexSegmentRejected(self):
        spec = univ.SequenceOf(componentType=univ.Integer())
        rows = [{flatten.PATH: ('x',), flatten.VALUE: 1}]
        try:
            flatten.rebuild(rows, spec)
        except PyAsn1Error:
            pass
        else:
            assert 0, 'non-index repetition segment tolerated'


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
