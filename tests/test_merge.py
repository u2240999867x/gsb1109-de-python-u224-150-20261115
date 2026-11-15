import unittest
from vdf import VDFDict, merge_sources, MergeReport


class MergeSourcesCase(unittest.TestCase):
    def test_returns_report(self):
        report = merge_sources({}, {})
        self.assertIsInstance(report, MergeReport)
        self.assertIsInstance(report.merged, VDFDict)
        self.assertEqual(list(report.merged.items()), [])
        self.assertEqual(report.spellings, {})
        self.assertEqual(report.equivalents, [])
        self.assertEqual(report.conflicts, [])

    def test_path_keys_case_insensitive(self):
        a = {'C:\\Steam': {'440': '100'}}
        b = {'c:\\steam': {'440': '100'}}
        report = merge_sources(a, b)

        self.assertEqual(list(report.merged.keys()), ['C:\\Steam'])
        self.assertEqual(report.spellings[('C:\\Steam',)],
                         ['C:\\Steam', 'c:\\steam'])
        self.assertEqual(report.equivalents,
                         [(('C:\\Steam',), ['C:\\Steam', 'c:\\steam']),
                          (('C:\\Steam', '440'), ['440'])])
        self.assertEqual(report.conflicts, [])

    def test_app_keys_case_sensitive(self):
        a = {'C:\\Steam': {'ABC': '1'}}
        b = {'c:\\steam': {'abc': '2'}}
        report = merge_sources(a, b)

        apps = report.merged['C:\\Steam']
        self.assertEqual(list(apps.items()), [('ABC', '1'), ('abc', '2')])
        self.assertEqual(report.spellings[('C:\\Steam', 'ABC')], ['ABC'])
        self.assertEqual(report.spellings[('C:\\Steam', 'abc')], ['abc'])
        self.assertEqual(report.conflicts, [])

    def test_cross_source_dedup_same_value(self):
        a = {'C:\\Steam': {'440': '100'}}
        b = {'C:\\Steam': {'440': '100'}}
        report = merge_sources(a, b)

        apps = report.merged['C:\\Steam']
        self.assertEqual(list(apps.items()), [('440', '100')])
        self.assertEqual(report.equivalents,
                         [(('C:\\Steam',), ['C:\\Steam']),
                          (('C:\\Steam', '440'), ['440'])])

    def test_same_source_duplicates_preserved(self):
        a = VDFDict([('C:\\Steam', VDFDict([('440', '100'), ('440', '100')]))])
        b = {'c:\\steam': {'440': '100'}}
        report = merge_sources(a, b)

        apps = report.merged['C:\\Steam']
        self.assertEqual(list(apps.items()), [('440', '100'), ('440', '100')])

    def test_extra_occurrence_from_b_kept(self):
        a = {'C:\\Steam': {'440': '100'}}
        b = VDFDict([('C:\\Steam', VDFDict([('440', '100'), ('440', '100')]))])
        report = merge_sources(a, b)

        apps = report.merged['C:\\Steam']
        self.assertEqual(list(apps.items()), [('440', '100'), ('440', '100')])

    def test_same_source_duplicates_not_conflicts(self):
        a = VDFDict([('C:\\Steam', VDFDict([('440', '100'), ('440', '200')]))])
        report = merge_sources(a, {})

        apps = report.merged['C:\\Steam']
        self.assertEqual(list(apps.items()), [('440', '100'), ('440', '200')])
        self.assertEqual(report.conflicts, [])

    def test_conflict_first_source_wins(self):
        a = {'C:\\Steam': {'440': '100'}}
        b = {'C:\\Steam': {'440': '200'}}
        report = merge_sources(a, b)

        self.assertEqual(report.merged['C:\\Steam']['440'], '100')
        self.assertEqual(report.conflicts,
                         [(('C:\\Steam', '440'), '100', '200')])

    def test_conflict_mapping_vs_value(self):
        a = {'C:\\Steam': {'440': {'depots': '1'}}}
        b = {'C:\\Steam': {'440': '100'}}
        report = merge_sources(a, b)

        self.assertEqual(list(report.merged['C:\\Steam']['440'].items()),
                         [('depots', '1')])
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0][0], ('C:\\Steam', '440'))
        self.assertEqual(report.conflicts[0][2], '100')

    def test_source_only_keys_kept(self):
        a = {'A:\\one': {'1': 'x'}}
        b = {'B:\\two': {'2': 'y'}}
        report = merge_sources(a, b)

        self.assertEqual(list(report.merged.keys()), ['A:\\one', 'B:\\two'])
        self.assertEqual(report.equivalents, [])

    def test_nested_levels_reuse_last_rule(self):
        a = {'C:\\Steam': {'440': {'Depot': '1'}}}
        b = {'c:\\steam': {'440': {'depot': '2'}}}
        report = merge_sources(a, b)

        depots = report.merged['C:\\Steam']['440']
        self.assertEqual(list(depots.items()), [('Depot', '1'), ('depot', '2')])

    def test_custom_key_rules(self):
        a = {'Lib': {'App': '1'}}
        b = {'lib': {'app': '2'}}
        lower = str.lower if not hasattr(str, 'casefold') else str.casefold
        report = merge_sources(a, b, key_rules=(lower, lower))

        self.assertEqual(list(report.merged.keys()), ['Lib'])
        self.assertEqual(list(report.merged['Lib'].items()), [('App', '1')])

    def test_empty_key_rules_raises(self):
        with self.assertRaises(ValueError):
            merge_sources({}, {}, key_rules=())

    def test_list_of_pairs_sources(self):
        a = [('C:\\Steam', {'440': '100'})]
        b = [('c:\\steam', {'440': '100'})]
        report = merge_sources(a, b)

        self.assertEqual(list(report.merged.keys()), ['C:\\Steam'])
        self.assertEqual(report.spellings[('C:\\Steam',)],
                         ['C:\\Steam', 'c:\\steam'])
