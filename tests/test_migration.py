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
from pyasn1 import migration


class MigrateTestCase(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)
        self.migration = migration.OfflineMigration(
            renames={'user_name': 'username'},
            passthrough=('profile',),
            state_merges={
                'status': {
                    'source': 'state',
                    'values': {
                        'active': 'enabled',
                        'standby': 'enabled',
                        'banned': 'disabled',
                    },
                },
            },
            defaults={'login_count': 0},
        )

    def testRenamesAndDefaults(self):
        result = self.migration.migrate(
            {'user_name': 'alice', 'state': 'active'})

        assert result.record['username'] == 'alice'
        assert result.record['status'] == 'enabled'
        assert result.record['login_count'] == 0
        assert 'user_name' not in result.record
        assert 'state' not in result.record
        assert result.lost == (
            migration.LostValue('status', 'active', 'enabled'),)

    def testDefaultsDoNotOverrideMigratedValues(self):
        migration_with_default = migration.OfflineMigration(
            renames={'logins': 'login_count'},
            defaults={'login_count': 0},
        )

        result = migration_with_default.migrate({'logins': 7})

        assert result.record['login_count'] == 7

    def testAbsentOptionalFieldsStayAbsent(self):
        result = self.migration.migrate({'state': 'banned'})

        assert 'username' not in result.record
        assert 'profile' not in result.record

    def testIrreversibleMergeReportsLostValue(self):
        result = self.migration.migrate({'state': 'standby'})

        assert result.record['status'] == 'enabled'
        assert result.lost == (
            migration.LostValue('status', 'standby', 'enabled'),)

    def testReversibleMergeReportsNoLoss(self):
        result = self.migration.migrate({'state': 'banned'})

        assert result.record['status'] == 'disabled'
        assert result.lost == ()

    def testUnmappedStateValueRaises(self):
        try:
            self.migration.migrate({'state': 'pending'})

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'unmapped old state value tolerated'

    def testUnknownExtensionsStoredSeparately(self):
        result = self.migration.migrate(
            {'state': 'active', 'x_legacy_flag': True, 'x_blob': [1, 2]})

        assert 'x_legacy_flag' not in result.record
        assert 'x_blob' not in result.record
        assert result.extensions == {'x_legacy_flag': True, 'x_blob': [1, 2]}
        assert result.record['_extensions'] == result.extensions

    def testUntouchedComplexFieldIsDeepCopied(self):
        profile = {'settings': {'theme': 'dark'}, 'tags': ['a', 'b']}

        result = self.migration.migrate(
            {'state': 'active', 'profile': profile})

        assert result.record['profile'] == profile
        assert result.record['profile'] is not profile
        assert result.record['profile']['settings'] is not profile['settings']

    def testRoundTripRestoresUntouchedComplexFields(self):
        old_record = {
            'user_name': 'alice',
            'state': 'standby',
            'profile': {'settings': {'theme': 'dark'}, 'tags': ['a']},
            'x_legacy_flag': True,
        }

        result = self.migration.migrate(old_record)
        restored = self.migration.reverse(result.record, lost=result.lost)

        assert restored == old_record
        assert restored['profile'] == {'settings': {'theme': 'dark'},
                                       'tags': ['a']}
        assert restored['profile'] != {}

    def testRoundTripWithoutExtensions(self):
        old_record = {'user_name': 'alice', 'state': 'banned',
                      'profile': {'tags': []}}

        result = self.migration.migrate(old_record)
        restored = self.migration.reverse(result.record)

        assert restored == old_record

    def testReverseIrreversibleMergeWithoutHintRaises(self):
        result = self.migration.migrate({'state': 'standby'})

        try:
            self.migration.reverse(result.record)

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'irreversible merge reversed without lost value'

    def testReverseRejectsBogusLostValue(self):
        result = self.migration.migrate({'state': 'standby'})

        try:
            self.migration.reverse(
                result.record, lost={'status': 'nonexistent'})

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'bogus lost value tolerated'

    def testReverseDropsDefaultedOptionalFields(self):
        result = self.migration.migrate({'state': 'banned'})

        assert result.record['login_count'] == 0

        restored = self.migration.reverse(result.record)

        assert 'login_count' not in restored

    def testReverseRejectsNonDefaultNewOnlyField(self):
        result = self.migration.migrate({'state': 'banned'})
        result.record['login_count'] = 5

        try:
            self.migration.reverse(result.record)

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'non-default new-only field silently dropped'

    def testReverseRejectsUnknownFields(self):
        result = self.migration.migrate({'state': 'banned'})
        result.record['surprise'] = 1

        try:
            self.migration.reverse(result.record)

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'unknown new field tolerated on reverse'

    def testLegacyExtensionBucketIsMerged(self):
        result = self.migration.migrate(
            {'state': 'active', '_extensions': {'x_one': 1}, 'x_two': 2})

        assert result.extensions == {'x_one': 1, 'x_two': 2}
        assert result.record['_extensions'] == {'x_one': 1, 'x_two': 2}

    def testExtensionKeyCollisionRejected(self):
        try:
            migration.OfflineMigration(
                passthrough=('data',), extension_key='data')

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'extension key colliding with a field tolerated'

    def testOverlappingTargetFieldsRejected(self):
        try:
            migration.OfflineMigration(
                renames={'a': 'shared'}, passthrough=('shared',))

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'overlapping target fields tolerated'

    def testMalformedStateMergeRejected(self):
        try:
            migration.OfflineMigration(state_merges={'status': {}})

        except error.PyAsn1Error:
            pass

        else:
            assert 0, 'state merge without source tolerated'


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite)
