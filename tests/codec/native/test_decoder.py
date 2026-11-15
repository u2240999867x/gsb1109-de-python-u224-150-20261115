#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import sys
import unittest

from tests.base import BaseTestCase

from pyasn1.type import namedtype
from pyasn1.type import univ
from pyasn1.codec.native import PRESENCE_ATTRIBUTE
from pyasn1.codec.native import decoder
from pyasn1.error import PyAsn1Error


class BadAsn1SpecTestCase(BaseTestCase):
    def testBadSpec(self):
        try:
            decoder.decode('', asn1Spec='not an Asn1Item')
        except PyAsn1Error:
            pass
        else:
            assert 0, 'Invalid asn1Spec accepted'


class IntegerDecoderTestCase(BaseTestCase):
    def testPosInt(self):
        assert decoder.decode(12, asn1Spec=univ.Integer()) == univ.Integer(12)

    def testNegInt(self):
        assert decoder.decode(-12, asn1Spec=univ.Integer()) == univ.Integer(-12)


class BooleanDecoderTestCase(BaseTestCase):
    def testTrue(self):
        assert decoder.decode(True, asn1Spec=univ.Boolean()) == univ.Boolean(True)

    def testTrueNeg(self):
        assert decoder.decode(False, asn1Spec=univ.Boolean()) == univ.Boolean(False)


class BitStringDecoderTestCase(BaseTestCase):
    def testSimple(self):
        assert decoder.decode('11111111', asn1Spec=univ.BitString()) == univ.BitString(hexValue='ff')


class OctetStringDecoderTestCase(BaseTestCase):
    def testSimple(self):
        assert decoder.decode('Quick brown fox', asn1Spec=univ.OctetString()) == univ.OctetString('Quick brown fox')


class NullDecoderTestCase(BaseTestCase):
    def testNull(self):
        assert decoder.decode(None, asn1Spec=univ.Null()) == univ.Null('')


class ObjectIdentifierDecoderTestCase(BaseTestCase):
    def testOne(self):
        assert decoder.decode('1.3.6.11', asn1Spec=univ.ObjectIdentifier()) == univ.ObjectIdentifier('1.3.6.11')


class RealDecoderTestCase(BaseTestCase):
    def testSimple(self):
        assert decoder.decode(1.33, asn1Spec=univ.Real()) == univ.Real(1.33)


class SequenceDecoderTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)

        self.s = univ.Sequence(
            componentType=namedtype.NamedTypes(
                namedtype.NamedType('place-holder', univ.Null()),
                namedtype.NamedType('first-name', univ.OctetString()),
                namedtype.NamedType('age', univ.Integer(33))
            )
        )

    def testSimple(self):
        s = self.s.clone()
        s[0] = univ.Null('')
        s[1] = univ.OctetString('xx')
        s[2] = univ.Integer(33)
        assert decoder.decode({'place-holder': None, 'first-name': 'xx', 'age': 33}, asn1Spec=self.s) == s


class SequenceDecoderWithLateBoundComponentTypeTestCase(BaseTestCase):
    def testLateBoundSpec(self):

        class LateSequence(univ.Sequence):
            pass

        s = LateSequence()

        LateSequence.componentType = namedtype.NamedTypes(
            namedtype.NamedType('name', univ.OctetString())
        )

        asn1Value = decoder.decode({'name': 'abc'}, asn1Spec=s)

        assert asn1Value['name'] == b'abc'


class SequenceDecoderPresenceTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)

        self.s = univ.Sequence(
            componentType=namedtype.NamedTypes(
                namedtype.NamedType('place-holder', univ.Null()),
                namedtype.OptionalNamedType('first-name', univ.OctetString()),
                namedtype.DefaultedNamedType('age', univ.Integer(33)),
                namedtype.OptionalNamedType(
                    'tags', univ.SequenceOf(componentType=univ.OctetString()))
            )
        )

    def testAbsentComponentsNotPresent(self):
        asn1Value = decoder.decode({'place-holder': None}, asn1Spec=self.s)

        assert getattr(asn1Value, PRESENCE_ATTRIBUTE) == frozenset(['place-holder'])

    def testExplicitDefaultPresent(self):
        asn1Value = decoder.decode(
            {'place-holder': None, 'age': 33}, asn1Spec=self.s)

        assert getattr(asn1Value, PRESENCE_ATTRIBUTE) == frozenset(['place-holder', 'age'])
        assert asn1Value.getComponentByName('age', instantiate=False) == univ.Integer(33)

    def testExplicitEmptyCollectionPresent(self):
        asn1Value = decoder.decode(
            {'place-holder': None, 'tags': []}, asn1Spec=self.s)

        assert getattr(asn1Value, PRESENCE_ATTRIBUTE) == frozenset(['place-holder', 'tags'])

        tags = asn1Value.getComponentByName('tags', instantiate=False)

        assert tags is not univ.noValue
        assert tags.isValue
        assert len(tags) == 0

    def testNestedPresence(self):
        s = univ.Sequence(componentType=namedtype.NamedTypes(
            namedtype.NamedType('id', univ.Integer()),
            namedtype.OptionalNamedType('sub', self.s)
        ))

        asn1Value = decoder.decode({'id': 1, 'sub': {'place-holder': None}}, asn1Spec=s)

        sub = asn1Value.getComponentByName('sub', instantiate=False)

        assert getattr(sub, PRESENCE_ATTRIBUTE) == frozenset(['place-holder'])


class ChoiceDecoderTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)

        self.s = univ.Choice(
            componentType=namedtype.NamedTypes(
                namedtype.NamedType('place-holder', univ.Null()),
                namedtype.NamedType('first-name', univ.OctetString()),
                namedtype.NamedType('age', univ.Integer(33))
            )
        )

    def testSimple(self):
        s = self.s.clone()
        s[1] = univ.OctetString('xx')
        assert decoder.decode({'first-name': 'xx'}, asn1Spec=self.s) == s


class AnyDecoderTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)

        self.s = univ.Any()

    def testSimple(self):
        assert decoder.decode('fox', asn1Spec=univ.Any()) == univ.Any('fox')


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
