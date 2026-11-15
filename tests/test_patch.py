import unittest
import vdf
from vdf import VDFDict
from vdf.patch import DailyVDF, UpdateError, apply_manifest, apply_patch, META_KEY


class DailyVDFManifestCase(unittest.TestCase):
    def test_manifest_replaces_state(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'a': '1', 'b': {'c': '2'}})
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 2},
                     'x': '9'})
        self.assertEqual(dict(daily.data.items()), {'x': '9'})

    def test_manifest_aliases(self):
        for kind in ('manifest', 'full', 'snapshot'):
            daily = DailyVDF()
            daily.apply({'a': '1'})
            daily.apply({META_KEY: {'kind': kind}, 'b': '2'})
            self.assertEqual(dict(daily.data.items()), {'b': '2'})

    def test_apply_manifest_result_type(self):
        result = apply_manifest({'a': {'b': '1'}})
        self.assertIsInstance(result, VDFDict)
        self.assertIsInstance(result['a'], VDFDict)
        self.assertEqual(result['a']['b'], '1')

    def test_apply_manifest_invalid(self):
        with self.assertRaises(TypeError):
            apply_manifest([1, 2])


class DailyVDFPatchScopeCase(unittest.TestCase):
    def test_missing_keys_outside_scope_keep_values(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'a': '1', 'b': '2', 'c': {'d': '3', 'e': '4'}})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2},
                     'a': '10'})
        self.assertEqual(daily.data['a'], '10')
        self.assertEqual(daily.data['b'], '2')
        self.assertEqual(daily.data['c']['d'], '3')
        self.assertEqual(daily.data['c']['e'], '4')

    def test_replace_scope_swaps_subtree(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'depots': {'old': '1', 'keep': 'x'},
                     'other': 'y'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2,
                                'replace': ['depots']},
                     'depots': {'new': '2'}})
        self.assertNotIn('old', daily.data['depots'])
        self.assertNotIn('keep', daily.data['depots'])
        self.assertEqual(daily.data['depots']['new'], '2')
        self.assertEqual(daily.data['other'], 'y')

    def test_replace_scope_nested_path(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'a': {'b': {'old': '1'}, 'c': 'keep'}})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2,
                                'replace': [('a', 'b')]},
                     'a': {'b': {'new': '2'}}})
        self.assertEqual(dict(daily.data['a']['b'].items()), {'new': '2'})
        self.assertEqual(daily.data['a']['c'], 'keep')

    def test_replace_scope_missing_from_patch_is_untouched(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'a': {'b': '1'}})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2,
                                'replace': ['a']},
                     'c': '3'})
        self.assertEqual(daily.data['a']['b'], '1')
        self.assertEqual(daily.data['c'], '3')

    def test_deep_merge_adds_new_keys(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'a': {'b': '1'}})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2},
                     'a': {'c': '2'}, 'd': '3'})
        self.assertEqual(daily.data['a']['b'], '1')
        self.assertEqual(daily.data['a']['c'], '2')
        self.assertEqual(daily.data['d'], '3')


class DailyVDFDeleteCase(unittest.TestCase):
    def test_delete_item(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'a': '1', 'b': '2'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2,
                                'delete': ['a']}})
        self.assertNotIn('a', daily.data)
        self.assertEqual(daily.data['b'], '2')

    def test_delete_missing_is_ignored(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1}, 'a': '1'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2,
                                'delete': ['nope', 'a.b.c']}})
        self.assertEqual(daily.data['a'], '1')

    def test_delete_only_specified_duplicate(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'k': 'first'})
        daily.apply({META_KEY: {'kind': 'patch'}, 'k': 'second'})
        self.assertEqual(daily.data.get_all_for('k'), ['first', 'second'])

        daily.apply({META_KEY: {'kind': 'patch', 'delete': [('k',)]}})
        self.assertEqual(daily.data.get_all_for('k'), ['second'])

    def test_delete_duplicate_by_index(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'k': 'first'})
        daily.apply({META_KEY: {'kind': 'patch'}, 'k': 'second'})
        daily.apply({META_KEY: {'kind': 'patch'}, 'k': 'third'})
        self.assertEqual(daily.data.get_all_for('k'), ['first', 'second', 'third'])

        daily.apply({META_KEY: {'kind': 'patch', 'delete': [((1, 'k'),)]}})
        self.assertEqual(daily.data.get_all_for('k'), ['first', 'third'])

    def test_delete_nested_duplicate_by_index(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'sec': {'k': 'a'}})
        daily.apply({META_KEY: {'kind': 'patch'}, 'sec': {'k': 'b'}})
        self.assertEqual(daily.data['sec'].get_all_for('k'), ['a', 'b'])

        daily.apply({META_KEY: {'kind': 'patch', 'delete': [('sec', (0, 'k'))]}})
        self.assertEqual(daily.data['sec'].get_all_for('k'), ['b'])


class DailyVDFVersionCase(unittest.TestCase):
    def test_late_older_patch_does_not_overwrite(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'patch', 'version': 5}, 'a': 'new'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 3}, 'a': 'old'})
        self.assertEqual(daily.data['a'], 'new')
        self.assertEqual(daily.data.get_all_for('a'), ['new'])

    def test_newer_patch_overwrites(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'patch', 'version': 3}, 'a': 'old'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 5}, 'a': 'new'})
        self.assertEqual(daily.data['a'], 'new')
        self.assertEqual(daily.data.get_all_for('a'), ['new'])

    def test_equal_version_applies(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'patch', 'version': 3}, 'a': '1'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 3}, 'b': '2'})
        self.assertEqual(daily.data['b'], '2')

    def test_versions_tracked_per_source(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'patch', 'source': 'A', 'version': 5},
                     'a': '1'})
        # source B is not affected by A's newer version
        daily.apply({META_KEY: {'kind': 'patch', 'source': 'B', 'version': 2},
                     'b': '2'})
        self.assertEqual(daily.data['b'], '2')
        # but an older patch from A is ignored
        daily.apply({META_KEY: {'kind': 'patch', 'source': 'A', 'version': 4},
                     'c': '3'})
        self.assertNotIn('c', daily.data)

    def test_string_versions(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'patch', 'version': '1.10'}, 'a': 'new'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': '1.9'}, 'a': 'old'})
        self.assertEqual(daily.data['a'], 'new')

    def test_late_older_manifest_ignored(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 5}, 'a': 'new'})
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 2}, 'b': 'old'})
        self.assertEqual(dict(daily.data.items()), {'a': 'new'})

    def test_conflict_without_comparable_versions_keeps_both(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest'}, 'a': '1'})
        daily.apply({META_KEY: {'kind': 'patch'}, 'a': '2'})
        self.assertEqual(sorted(daily.data.get_all_for('a')), ['1', '2'])

    def test_versioned_patch_wins_conflict(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1}, 'a': '1'})
        daily.apply({META_KEY: {'kind': 'patch', 'version': 2}, 'a': '2'})
        self.assertEqual(daily.data.get_all_for('a'), ['2'])

    def test_unversioned_after_versioned_keeps_both(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1}, 'a': '1'})
        daily.apply({META_KEY: {'kind': 'patch'}, 'a': '2'})
        self.assertEqual(sorted(daily.data.get_all_for('a')), ['1', '2'])
        # version bookkeeping is preserved
        self.assertEqual(daily.versions[''], 1)

    def test_identical_values_are_not_duplicated(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest'}, 'a': '1'})
        daily.apply({META_KEY: {'kind': 'patch'}, 'a': '1'})
        self.assertEqual(daily.data.get_all_for('a'), ['1'])


class DailyVDFMiscCase(unittest.TestCase):
    def test_apply_all(self):
        daily = DailyVDF()
        daily.apply_all([
            {META_KEY: {'kind': 'manifest', 'version': 1}, 'a': '1'},
            {META_KEY: {'kind': 'patch', 'version': 2}, 'b': '2'},
        ])
        self.assertEqual(dict(daily.data.items()), {'a': '1', 'b': '2'})

    def test_update_without_meta_is_a_patch(self):
        daily = DailyVDF()
        daily.apply({'a': '1'})
        daily.apply({'b': '2'})
        self.assertEqual(dict(daily.data.items()), {'a': '1', 'b': '2'})

    def test_invalid_update_type(self):
        daily = DailyVDF()
        with self.assertRaises(TypeError):
            daily.apply([1, 2])

    def test_invalid_kind(self):
        daily = DailyVDF()
        with self.assertRaises(UpdateError):
            daily.apply({META_KEY: {'kind': 'bogus'}})

    def test_invalid_meta(self):
        daily = DailyVDF()
        with self.assertRaises(UpdateError):
            daily.apply({META_KEY: 'nope'})

    def test_apply_patch_invalid(self):
        with self.assertRaises(TypeError):
            apply_patch([], {})
        with self.assertRaises(TypeError):
            apply_patch({}, [])

    def test_apply_patch_plain_dict_base(self):
        base = {'a': '1', 'b': {'c': '2'}}
        apply_patch(base, {'b': {'d': '3'}, 'e': '4'}, prefer_incoming=True)
        self.assertEqual(base, {'a': '1', 'b': {'c': '2', 'd': '3'}, 'e': '4'})

    def test_apply_patch_plain_dict_keeps_existing_on_conflict(self):
        base = {'a': '1'}
        apply_patch(base, {'a': '2'}, prefer_incoming=False)
        self.assertEqual(base['a'], '1')

    def test_state_usable_with_vdf_dumps(self):
        daily = DailyVDF()
        daily.apply({META_KEY: {'kind': 'manifest', 'version': 1},
                     'a': {'b': '1'}})
        text = vdf.dumps(daily.data)
        self.assertEqual(vdf.loads(text)['a']['b'], '1')

    def test_patch_exposed_on_package(self):
        self.assertIs(vdf.patch.DailyVDF, DailyVDF)


if __name__ == '__main__':
    unittest.main()
