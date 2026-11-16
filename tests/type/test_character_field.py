#
# This file is part of pyasn1 software.
#
# Copyright (c) 2026, Simon Pichugin <simon.pichugin@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import sys
import unittest

from tests.base import BaseTestCase

from pyasn1 import error
from pyasn1.type import char
from pyasn1.type import constraint
from pyasn1.type import univ


class DecodeCharacterFieldTestCase(BaseTestCase):

    def testDecodableFieldRetainsTypeOctetsAndText(self):
        field = char.decodeCharacterField(char.TeletexString, b'caf\xe9')

        assert field.asn1Type is char.TeletexString
        assert field.kind == 'TeletexString'
        assert field.getOctets() == b'caf\xe9'
        assert field.getText() == 'café'
        assert field.repertoire == 'latin-1'
        assert not field.isQuarantined

    def testUtf8FieldRetainsOriginalOctetsNotReencodedText(self):
        octets = 'héllo'.encode('utf-8')

        field = char.decodeCharacterField(char.UTF8String, octets)

        assert field.getOctets() == b'h\xc3\xa9llo'
        assert field.getText() == 'héllo'

    def testQuarantineKeepsOctetsInsteadOfReplacementCharacters(self):
        field = char.decodeCharacterField(char.UTF8String, b'\xff\xfe')

        assert field.isQuarantined
        assert field.getOctets() == b'\xff\xfe'
        assert field.text is None
        assert isinstance(field.quarantine, error.PyAsn1UnicodeDecodeError)
        assert '�' not in repr(field)

    def testQuarantinedTextAccessRaises(self):
        field = char.decodeCharacterField(char.UTF8String, b'\xff\xfe')

        try:
            field.getText()
        except char.QuarantinedCharacterFieldError:
            pass
        else:
            assert 0, 'quarantined text access tolerated'

    def testQuarantineAppliesToMultiByteTypes(self):
        for asn1Type, octets in ((char.BMPString, b'\x00'),
                                 (char.UniversalString, b'\x00\x00\x00')):
            field = char.decodeCharacterField(asn1Type, octets)

            assert field.isQuarantined, 'truncated %s not quarantined' % asn1Type.__name__
            assert field.getOctets() == octets

    def testTextInitializerEncodesWithDeclaredCodec(self):
        field = char.decodeCharacterField(char.UTF8String, 'héllo')

        assert field.getOctets() == 'héllo'.encode('utf-8')
        assert field.getText() == 'héllo'

    def testTextOutsideDeclaredCodecIsRejected(self):
        try:
            char.decodeCharacterField(char.PrintableString, 'héllo')
        except char.CharacterRepertoireError:
            pass
        else:
            assert 0, 'non-ASCII text accepted for PrintableString'

    def testNonCharacterTypeIsRejected(self):
        try:
            char.decodeCharacterField(bytes, b'abc')
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'non-character type accepted'

    def testNonBytesPayloadIsRejected(self):
        try:
            char.decodeCharacterField(char.UTF8String, 123)
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'non-bytes payload accepted'

    def testRebuildPreservesOriginType(self):
        field = char.decodeCharacterField(char.TeletexString, b'caf\xe9')

        value = field.asAsn1()

        assert isinstance(value, char.TeletexString)
        assert value.asOctets() == b'caf\xe9'

    def testRebuildOfQuarantinedValueSurfacesQuarantine(self):
        field = char.decodeCharacterField(char.UTF8String, b'\xff')

        try:
            field.asAsn1()
        except char.QuarantinedCharacterFieldError:
            pass
        else:
            assert 0, 'quarantined rebuild tolerated'

    def testOutOfRepertoireValueIsQuarantined(self):
        field = char.decodeCharacterField(char.PrintableString, b'a\tb')

        assert field.isQuarantined
        assert field.getOctets() == b'a\tb'
        assert isinstance(field.quarantine, char.CharacterRepertoireError)

    def testSchemaInstanceKeepsConstraints(self):
        spec = char.UTF8String(subtypeSpec=constraint.ValueSizeConstraint(1, 3))

        field = char.decodeCharacterField(spec, b'abc')

        assert field.getText() == 'abc'
        assert field.asAsn1().readOnly['subtypeSpec'] == spec.subtypeSpec

        try:
            char.decodeCharacterField(spec, b'abcd')
        except error.PyAsn1Error:
            pass
        else:
            assert 0, 'size constraint violated by oversized payload'

    def testCleanFieldCarriesOriginalTypeOctetsAndText(self):
        field = char.decodeCharacterField(char.TeletexString, b'caf\xe9')

        record = field.asRecord()

        assert record['type'] == 'TeletexString'
        assert record['asn1Type'] is char.TeletexString
        assert record['octets'] == b'caf\xe9'
        assert record['octetsHex'] == b'caf\xe9'.hex()
        assert record['text'] == 'café'
        assert record['quarantined'] is False
        assert record['error'] is None
        assert record['errorMessage'] is None

    def testQuarantinedRecordKeepsBytesAndOmitsSubstitutedText(self):
        field = char.decodeCharacterField(char.UTF8String, b'\xff\xfe')

        record = field.asRecord()

        assert record['octets'] == b'\xff\xfe'
        assert record['octetsHex'] == 'fffe'
        assert record['text'] is None
        assert record['quarantined'] is True
        assert isinstance(record['error'], error.PyAsn1UnicodeDecodeError)
        assert record['errorMessage']

    def testRecordCarriesSourceLabel(self):
        field = char.decodeCharacterField(char.UTF8String, b'abc', source='new')

        assert field.asRecord()['source'] == 'new'

    def testDirectAttributesExposeThreeFacets(self):
        field = char.decodeCharacterField(char.TeletexString, b'caf\xe9')

        assert field.originalType is char.TeletexString
        assert field.originalOctets == b'caf\xe9'
        assert field.standardText == 'café'

    def testQuarantinedFieldHasNoStandardTextAttribute(self):
        field = char.decodeCharacterField(char.UTF8String, b'\xff')

        assert field.originalType is char.UTF8String
        assert field.originalOctets == b'\xff'
        assert field.standardText is None


class CrossSourceMigrationTestCase(BaseTestCase):
    """The end-to-end shape the two-source field migration is meant to have."""

    def testRestrictedAndUtf8RowsMergeAndSplitBack(self):
        old = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                        source='old')
        new = char.decodeCharacterField(char.UTF8String, b'caf\xc3\xa9',
                                        source='new')

        merged = char.mergeCharacterFields(old, new)

        assert merged.text == 'café'
        assert merged.getBySource('old')[0].getOctets() == b'caf\xe9'
        assert merged.getBySource('new')[0].getOctets() == b'caf\xc3\xa9'

        migrated = merged.migrateTo(char.TeletexString)

        assert migrated.kind == 'TeletexString'
        assert migrated.originalOctets == b'caf\xe9'
        assert migrated.standardText == 'café'

    def testUtf8OnlyTextIsRejectedWhenMigratedBack(self):
        new = char.decodeCharacterField(char.UTF8String, '日本'.encode('utf-8'),
                                        source='new')

        merged = char.mergeCharacterFields([new])

        try:
            merged.migrateTo(char.TeletexString)
        except char.CharacterRepertoireError:
            pass
        else:
            assert 0, 'out-of-range merged text migrated back to TeletexString'

    def testUndecodableUtf8RowStaysQuarantinedAndOutOfMerge(self):
        good = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                         source='old')
        bad = char.decodeCharacterField(char.UTF8String, b'caf\xe9',
                                        source='new')

        assert good.getText() == 'café'
        assert bad.isQuarantined
        assert bad.getOctets() == b'caf\xe9'
        assert bad.text is None

        try:
            char.mergeCharacterFields(good, bad)
        except char.QuarantinedCharacterFieldError:
            pass
        else:
            assert 0, 'quarantined row silently merged'


class CharacterFieldConstructionTestCase(BaseTestCase):

    def testQuarantinedFieldRejectsText(self):
        try:
            char.CharacterField(char.UTF8String, b'\xff', 'x',
                                quarantine=error.PyAsn1Error('bad'))
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'quarantined field accepted standard text'

    def testFieldWithoutTextOrReasonIsInvalid(self):
        try:
            char.CharacterField(char.UTF8String, b'\xff', None)
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'textless field without a reason accepted'

    def testNonBytesOctetsRejected(self):
        try:
            char.CharacterField(char.UTF8String, 'abc', 'abc')
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'text accepted as wire octets'

    def testMismatchedSchemaRejected(self):
        try:
            char.CharacterField(char.UTF8String, b'abc', 'abc',
                                asn1Spec=char.TeletexString())
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'schema of a different type accepted'


class CharacterFieldPicklingTestCase(BaseTestCase):

    def testCleanFieldRoundTrips(self):
        import pickle

        field = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                          source='old')

        restored = pickle.loads(pickle.dumps(field))

        assert restored == field
        assert restored.getText() == 'café'
        assert restored.getOctets() == b'caf\xe9'

    def testQuarantinedFieldRoundTripsWithOctets(self):
        import pickle

        field = char.decodeCharacterField(char.UTF8String, b'caf\xe9',
                                          source='new')

        restored = pickle.loads(pickle.dumps(field))

        assert restored.isQuarantined
        assert restored.getOctets() == b'caf\xe9'
        assert restored.text is None
        assert isinstance(restored.quarantine, error.PyAsn1UnicodeDecodeError)

    def testUnicodeErrorsRoundTrip(self):
        import pickle

        errors = []

        try:
            b'\xff'.decode('utf-8')
        except UnicodeDecodeError as exc:
            errors.append(error.PyAsn1UnicodeDecodeError('decode', exc))

        try:
            'café'.encode('ascii')
        except UnicodeEncodeError as exc:
            errors.append(error.PyAsn1UnicodeEncodeError('encode', exc))

        for original in errors:
            restored = pickle.loads(pickle.dumps(original))

            assert type(restored) is type(original)
            assert restored.args == original.args
            assert restored._args == original._args

    def testUnicodeErrorMessageIsReadable(self):
        try:
            b'\xff'.decode('utf-8')
        except UnicodeDecodeError as exc:
            original = error.PyAsn1UnicodeDecodeError('decode failed', exc)

        assert 'decode failed' in str(original)
        assert 'utf-8' in str(original)


class CharacterRepertoireTestCase(BaseTestCase):

    def testKnownRepertoires(self):
        assert char.getCharacterRepertoire(char.NumericString) == 'numeric'
        assert char.getCharacterRepertoire(char.PrintableString()) == 'printable'
        assert char.getCharacterRepertoire(char.IA5String) == 'ia5'
        assert char.getCharacterRepertoire(char.VisibleString) == 'visible'
        assert char.getCharacterRepertoire(char.TeletexString) == 'latin-1'
        assert char.getCharacterRepertoire(char.BMPString) == 'bmp'
        assert char.getCharacterRepertoire(char.UTF8String) == 'unicode'

    def testRepertoireIsInheritedByAliases(self):
        assert char.getCharacterRepertoire(char.T61String) == 'latin-1'
        assert char.getCharacterRepertoire(char.ISO646String) == 'visible'

    def testRepertoireIsInheritedBySubclass(self):
        class MyPrintable(char.PrintableString):
            pass

        assert char.getCharacterRepertoire(MyPrintable) == 'printable'
        assert char.getCharacterRepertoire(MyPrintable()) == 'printable'

    def testUnrelatedTypeRejected(self):
        try:
            char.getCharacterRepertoire(univ.Integer)
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'non-character type given a repertoire'


class MergeCharacterFieldsTestCase(BaseTestCase):

    def testMergeByTextAcrossDifferentTypes(self):
        restricted = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                               source='old')
        utf8 = char.decodeCharacterField(char.UTF8String, b'caf\xc3\xa9',
                                         source='new')

        merged = char.mergeCharacterFields([restricted, utf8])

        assert merged.text == 'café'
        assert merged.sources.get(char.TeletexString) is restricted
        assert merged.sources.get(char.UTF8String) is utf8

    def testMergeAcceptsPositionalFields(self):
        old = char.decodeCharacterField(char.TeletexString, b'abc',
                                        source='old')
        new = char.decodeCharacterField(char.UTF8String, b'abc', source='new')

        merged = char.mergeCharacterFields(old, new)

        assert merged.getSource(char.PrintableString) is None
        assert merged.getBySource('old') == (old,)
        assert merged.getBySource('new') == (new,)

    def testMergeAcceptsGenerator(self):
        old = char.decodeCharacterField(char.TeletexString, b'abc',
                                        source='old')
        new = char.decodeCharacterField(char.UTF8String, b'abc', source='new')

        merged = char.mergeCharacterFields(field for field in (old, new))

        assert merged.sourceLabels == ('old', 'new')

    def testMergeRejectsScalar(self):
        try:
            char.mergeCharacterFields('abc')
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'scalar accepted as merge input'

    def testMergeKeepsBothSourceOctets(self):
        restricted = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                               source='old')
        utf8 = char.decodeCharacterField(char.UTF8String, b'caf\xc3\xa9',
                                         source='new')

        merged = char.mergeCharacterFields([restricted, utf8])

        assert merged.getSource(char.TeletexString).getOctets() == b'caf\xe9'
        assert merged.getSource(char.UTF8String).getOctets() == b'caf\xc3\xa9'

    def testMergedRecordCarriesEverySource(self):
        old = char.decodeCharacterField(char.TeletexString, b'abc',
                                        source='old')
        new = char.decodeCharacterField(char.UTF8String, b'abc', source='new')

        record = char.mergeCharacterFields(old, new).asRecord()

        assert record['text'] == 'abc'
        assert record['sourceLabels'] == ('old', 'new')
        assert [source['type'] for source in record['sources']] == [
            'TeletexString', 'UTF8String']

    def testMergeIndexesRepeatedSourceType(self):
        first = char.decodeCharacterField(char.UTF8String, b'abc')
        second = char.decodeCharacterField(char.UTF8String, b'abc')

        merged = char.mergeCharacterFields([first, second])

        assert len(merged.sources) == 2
        assert merged.getSource(char.UTF8String) is first
        assert merged.getSources(char.UTF8String) == (first, second)

    def testMergeRejectsDifferentText(self):
        try:
            char.mergeCharacterFields([
                char.decodeCharacterField(char.UTF8String, b'abc'),
                char.decodeCharacterField(char.UTF8String, b'abd')
            ])
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'merge of differing text tolerated'

    def testMergeRejectsQuarantinedSource(self):
        try:
            char.mergeCharacterFields([
                char.decodeCharacterField(char.UTF8String, b'abc'),
                char.decodeCharacterField(char.UTF8String, b'\xff')
            ])
        except char.QuarantinedCharacterFieldError:
            pass
        else:
            assert 0, 'merge of quarantined value tolerated'

    def testMergeRejectsEmptyInput(self):
        try:
            char.mergeCharacterFields([])
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'empty merge tolerated'

    def testMergedFieldForAbsentSourceTypeIsAbsent(self):
        merged = char.mergeCharacterFields([
            char.decodeCharacterField(char.UTF8String, b'abc')
        ])

        assert merged.getField(char.PrintableString) is None
        assert merged.getSource(char.PrintableString) is None

    def testMergeTracksBothSourceLabels(self):
        old = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                        source='old')
        new = char.decodeCharacterField(char.UTF8String, b'caf\xc3\xa9',
                                        source='new')

        merged = char.mergeCharacterFields([old, new])

        assert merged.sourceLabels == ('old', 'new')
        assert merged.sources.sourceLabels == ('old', 'new')
        assert merged.sources.getBySource('old') == (old,)
        assert merged.sources.getBySource('new') == (new,)
        assert merged.sources.getBySource('unknown') == ()


class CharacterFieldSetTestCase(BaseTestCase):

    def testCleanAndQuarantinedPartitions(self):
        clean = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                          source='old')
        bad = char.decodeCharacterField(char.UTF8String, b'\xff', source='new')

        fields = char.CharacterFieldSet([clean, bad])

        assert len(fields) == 2
        assert fields.clean == (clean,)
        assert fields.quarantined == (bad,)
        assert fields.getBySource('old') == (clean,)
        assert fields.getBySource('new') == (bad,)
        assert fields.sourceLabels == ('old', 'new')

    def testRecordsKeepTypeOctetsAndTextForEveryField(self):
        fields = char.CharacterFieldSet([
            char.decodeCharacterField(char.TeletexString, b'caf\xe9'),
            char.decodeCharacterField(char.UTF8String, b'\xff')
        ])

        records = fields.asRecords()

        assert records[0]['type'] == 'TeletexString'
        assert records[0]['octets'] == b'caf\xe9'
        assert records[0]['text'] == 'café'
        assert records[1]['type'] == 'UTF8String'
        assert records[1]['octets'] == b'\xff'
        assert records[1]['text'] is None
        assert records[1]['quarantined'] is True

    def testSetRejectsNonField(self):
        try:
            char.CharacterFieldSet(['abc'])
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'non-field added to set'

    def testJsonRecordsAreSerialisable(self):
        import json

        fields = char.CharacterFieldSet([
            char.decodeCharacterField(char.TeletexString, b'caf\xe9'),
            char.decodeCharacterField(char.UTF8String, b'\xff')
        ])

        encoded = json.dumps(fields.asJsonRecords())
        decoded = json.loads(encoded)

        assert decoded[0]['type'] == 'TeletexString'
        assert decoded[0]['octetsHex'] == '636166e9'
        assert decoded[0]['text'] == 'café'
        assert decoded[0]['quarantined'] is False
        assert decoded[1]['text'] is None
        assert decoded[1]['quarantined'] is True
        assert decoded[1]['errorMessage']


class MigrateCharacterFieldTestCase(BaseTestCase):

    def testMigrationReencodesTextUnderTargetType(self):
        field = char.decodeCharacterField(char.UTF8String, b'caf\xc3\xa9')

        migrated = char.migrateCharacterField(field, char.TeletexString)

        assert migrated.kind == 'TeletexString'
        assert migrated.getOctets() == b'caf\xe9'
        assert migrated.getText() == 'café'

    def testMigrationToUtf8FromRestrictedType(self):
        field = char.decodeCharacterField(char.TeletexString, b'caf\xe9')

        migrated = field.migrateTo(char.UTF8String)

        assert migrated.getOctets() == 'café'.encode('utf-8')

    def testMigrationWithinSameTypeKeepsOriginalOctets(self):
        field = char.decodeCharacterField(char.TeletexString, b'caf\xe9')

        assert field.migrateTo(char.TeletexString) is field

    def testMigrationAboveCharacterRangeIsRejected(self):
        field = char.decodeCharacterField(char.UTF8String, '日本'.encode('utf-8'))

        try:
            char.migrateCharacterField(field, char.TeletexString)
        except char.CharacterRepertoireError:
            pass
        else:
            assert 0, 'out-of-range text migrated into TeletexString'

    def testMigrationAboveBmpRejected(self):
        field = char.decodeCharacterField(char.UTF8String, '😀'.encode('utf-8'))

        try:
            field.migrateTo(char.BMPString)
        except char.CharacterRepertoireError:
            pass
        else:
            assert 0, 'astral character migrated into BMPString'

    def testMigrationAbovePrintableRejected(self):
        field = char.decodeCharacterField(char.UTF8String, b'caf\xc3\xa9')

        try:
            field.migrateTo(char.PrintableString)
        except char.CharacterRepertoireError:
            pass
        else:
            assert 0, 'accented text migrated into PrintableString'

    def testMergedFieldMigrationRejectsOverflow(self):
        field = char.decodeCharacterField(char.UTF8String, '日本'.encode('utf-8'))
        merged = char.mergeCharacterFields([field])

        try:
            merged.migrateTo(char.TeletexString)
        except char.CharacterRepertoireError:
            pass
        else:
            assert 0, 'merged out-of-range text migrated'

    def testMergedFieldMigrationKeepsText(self):
        old = char.decodeCharacterField(char.UTF8String, b'caf\xc3\xa9',
                                        source='new')
        new = char.decodeCharacterField(char.TeletexString, b'caf\xe9',
                                        source='old')

        migrated = char.mergeCharacterFields(old, new).migrateTo(
            char.TeletexString)

        assert migrated.kind == 'TeletexString'
        assert migrated.getOctets() == b'caf\xe9'
        assert migrated.getText() == 'café'

    def testMigrationOfQuarantinedValueIsRejected(self):
        field = char.decodeCharacterField(char.UTF8String, b'\xff')

        try:
            field.migrateTo(char.TeletexString)
        except char.CharacterRepertoireError:
            pass
        else:
            assert 0, 'quarantined value migrated'

    def testMigrationDoesNotMutateOriginal(self):
        field = char.decodeCharacterField(char.UTF8String, b'abc')

        field.migrateTo(char.PrintableString)

        assert field.kind == 'UTF8String'
        assert field.getOctets() == b'abc'

    def testMigrationRejectsNonField(self):
        try:
            char.migrateCharacterField(b'abc', char.UTF8String)
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'non-field migrated'


class CharacterFieldIndexTestCase(BaseTestCase):

    def testLookupByInstanceAndClass(self):
        field = char.decodeCharacterField(char.PrintableString, b'abc')
        index = char.CharacterFieldIndex([field])

        assert index.get(char.PrintableString) is field
        assert index.get(char.PrintableString()) is field

    def testIterationAndLength(self):
        index = char.CharacterFieldIndex([
            char.decodeCharacterField(char.PrintableString, b'abc'),
            char.decodeCharacterField(char.UTF8String, b'abc')
        ])

        assert len(index) == 2
        assert len(list(index)) == 2

    def testAddRejectsNonField(self):
        index = char.CharacterFieldIndex()

        try:
            index.add('abc')
        except char.CharacterFieldError:
            pass
        else:
            assert 0, 'non-field added to index'


class CharacterFieldEqualityTestCase(BaseTestCase):

    def testSameTypeOctetsTextAndSourceAreEqual(self):
        first = char.decodeCharacterField(char.UTF8String, b'abc', source='new')
        second = char.decodeCharacterField(char.UTF8String, b'abc', source='new')

        assert first == second
        assert not first != second
        assert hash(first) == hash(second)

    def testDifferentSourceIsNotEqual(self):
        old = char.decodeCharacterField(char.UTF8String, b'abc', source='old')
        new = char.decodeCharacterField(char.UTF8String, b'abc', source='new')

        assert old != new

    def testMergedFieldsCompareByText(self):
        first = char.mergeCharacterFields([
            char.decodeCharacterField(char.UTF8String, b'abc')])
        second = char.mergeCharacterFields([
            char.decodeCharacterField(char.PrintableString, b'abc')])

        assert first == second
        assert hash(first) == hash(second)


class StrictRepertoireTestCase(BaseTestCase):

    def testAsciiTextAcceptedForIa5(self):
        field = char.decodeCharacterField(char.IA5String, b'abc@example.net')

        assert not field.isQuarantined

    def testNonAsciiQuarantinedForIa5(self):
        field = char.decodeCharacterField(char.IA5String, b'caf\xe9')

        assert field.isQuarantined

    def testSpaceAcceptedByVisibleAndPrintable(self):
        visible = char.decodeCharacterField(char.VisibleString, b'a b')
        printable = char.decodeCharacterField(char.PrintableString, b'a b')

        assert not visible.isQuarantined
        assert not printable.isQuarantined

    def testControlCharacterQuarantinedForVisible(self):
        field = char.decodeCharacterField(char.VisibleString, b'a\tb')

        assert field.isQuarantined

    def testNumericStringRejectsLetters(self):
        field = char.decodeCharacterField(char.NumericString, b'12a')

        assert field.isQuarantined

    def testNumericStringAcceptsDigitsAndSpace(self):
        field = char.decodeCharacterField(char.NumericString, b'12 34')

        assert not field.isQuarantined

    def testBmpAcceptsBasicPlane(self):
        field = char.decodeCharacterField(char.BMPString, '日本'.encode('utf-16-be'))

        assert not field.isQuarantined
        assert field.getText() == '日本'


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
