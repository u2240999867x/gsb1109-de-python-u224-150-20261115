import json
import unittest

try:
    from unittest import mock
except ImportError:
    import mock

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

from vdf import channel


class testcase_condition_syntax(unittest.TestCase):
    def test_symbol(self):
        self.assertIs(channel.evaluate_condition('[$WINDOWS]', {'WINDOWS': True}), True)
        self.assertIs(channel.evaluate_condition('[$WINDOWS]', {'WINDOWS': False}), False)

    def test_brackets_optional_and_dollar_optional(self):
        self.assertIs(channel.evaluate_condition('$WINDOWS', {'WINDOWS': True}), True)
        self.assertIs(channel.evaluate_condition('WINDOWS', {'WINDOWS': True}), True)

    def test_symbol_case_insensitive(self):
        self.assertIs(channel.evaluate_condition('[$windows]', {'WINDOWS': True}), True)
        self.assertIs(channel.evaluate_condition('[$WINDOWS]', {'windows': True}), True)

    def test_not(self):
        self.assertIs(channel.evaluate_condition('[!$LINUX]', {'LINUX': False}), True)
        self.assertIs(channel.evaluate_condition('[!$LINUX]', {'LINUX': True}), False)

    def test_and_or_parens(self):
        env = {'A': True, 'B': False, 'C': True}
        self.assertIs(channel.evaluate_condition('[$A && $C]', env), True)
        self.assertIs(channel.evaluate_condition('[$A && $B]', env), False)
        self.assertIs(channel.evaluate_condition('[$A || $B]', env), True)
        self.assertIs(channel.evaluate_condition('[($A || $B) && !$C]', env), False)
        self.assertIs(channel.evaluate_condition('[$B || ($A && $C)]', env), True)

    def test_and_binds_tighter_than_or(self):
        env = {'A': False, 'B': True, 'C': False}
        self.assertIs(channel.evaluate_condition('[$A || $B && $C]', env), False)

    def test_unknown_symbol_is_none_not_false(self):
        self.assertIs(channel.evaluate_condition('[$STEAMDECK]', {}), None)
        self.assertIs(channel.evaluate_condition('[!$STEAMDECK]', {}), None)

    def test_three_valued_logic(self):
        self.assertIs(channel.evaluate_condition('[$KNOWN && $UNKNOWN]', {'KNOWN': True}), None)
        self.assertIs(channel.evaluate_condition('[$KNOWN && $UNKNOWN]', {'KNOWN': False}), False)
        self.assertIs(channel.evaluate_condition('[$KNOWN || $UNKNOWN]', {'KNOWN': True}), True)
        self.assertIs(channel.evaluate_condition('[$KNOWN || $UNKNOWN]', {'KNOWN': False}), None)

    def test_syntax_errors(self):
        for bad in ('', '[]', '[$A $B]', '[$A&$B]', '[!]', '[($A]', '[$A]', '[$A ||]'):
            if bad == '[$A]':
                continue  # valid
            self.assertRaises(channel.ConditionSyntaxError,
                              channel.evaluate_condition, bad, {})

    def test_env_string_values(self):
        self.assertIs(channel.evaluate_condition('[$A]', {'A': 'true'}), True)
        self.assertIs(channel.evaluate_condition('[$A]', {'A': '0'}), False)
        self.assertRaises(ValueError, channel.evaluate_condition, '[$A]', {'A': 'maybe'})


class testcase_parse_channel(unittest.TestCase):
    CONFIG = '\n'.join([
        '// channel config',
        '"channel"',
        '{',
        '    "name"        "demo"',
        '    "installdir"  "common/path"',
        '    "installdir"  "win/path"      [$WINDOWS] // windows only',
        '    "installdir"  "lin/path"      [$LINUX]',
        '    "mac"         [$OSX]',
        '    {',
        '        "installdir"  "mac/path"',
        '    }',
        '}',
    ])

    def test_entries_preserve_duplicates_conditions_and_lines(self):
        entries = channel.parse_channel(self.CONFIG)

        installdirs = [e for e in entries if e.key == 'installdir']
        self.assertEqual(len(installdirs), 4)
        self.assertEqual([e.value for e in installdirs],
                         ['common/path', 'win/path', 'lin/path', 'mac/path'])

        self.assertEqual(installdirs[0].conditions, ())
        self.assertEqual(installdirs[1].conditions, ('[$WINDOWS]',))
        self.assertEqual(installdirs[3].conditions, ('[$OSX]',))  # inherited from section

        self.assertEqual(installdirs[0].lineno, 5)
        self.assertEqual(installdirs[1].lineno, 6)
        self.assertEqual(installdirs[3].lineno, 10)

        self.assertEqual(installdirs[0].full_path, ('channel', 'installdir'))

    def test_name_entry(self):
        entries = channel.parse_channel(self.CONFIG)
        name = [e for e in entries if e.key == 'name']
        self.assertEqual(len(name), 1)
        self.assertEqual(name[0].value, 'demo')

    def test_parse_from_file_object(self):
        entries = channel.parse_channel(StringIO(self.CONFIG))
        self.assertTrue(any(e.key == 'name' for e in entries))

    def test_type_assert(self):
        for bad in (5, None, [], object()):
            self.assertRaises(TypeError, channel.parse_channel, bad)

    def test_escaped_values(self):
        entries = channel.parse_channel('"a"\n{\n"k" "va\\"lue"\n}\n')
        self.assertEqual(entries[0].value, 'va"lue')

    def test_bom_and_comments(self):
        entries = channel.parse_channel('﻿// c\n"a"\n{\n"k" "v" // tail\n}\n')
        self.assertEqual(entries[0].value, 'v')

    def test_unbalanced_brackets(self):
        self.assertRaises(SyntaxError, channel.parse_channel, '"a" { "k" "v"')
        self.assertRaises(SyntaxError, channel.parse_channel, '"a" { "k" "v" } }')

    def test_unterminated_quote(self):
        self.assertRaises(SyntaxError, channel.parse_channel, '"a" { "k" "v }')


class testcase_resolve(unittest.TestCase):
    CONFIG = '\n'.join([
        '"channel"',
        '{',
        '    "name"        "demo"',
        '    "installdir"  "common/path"',
        '    "installdir"  "win/path"      [$WINDOWS]',
        '    "installdir"  "lin/path"      [$LINUX]',
        '    "installdir"  "mac/path"      [$OSX]',
        '    "tooldir"     "deck/tool"     [$STEAMDECK]',
        '}',
    ])

    def test_resolve_windows(self):
        manifest = channel.build_manifest(
            self.CONFIG, env={'WINDOWS': True, 'LINUX': False, 'OSX': False})

        entry = manifest['resolved']['channel.installdir']
        self.assertEqual(entry['value'], 'win/path')
        self.assertEqual(entry['sources'], [{'line': 5, 'condition': '[$WINDOWS]'}])

        self.assertEqual(manifest['resolved']['channel.name']['value'], 'demo')
        self.assertEqual(manifest['resolved']['channel.name']['sources'],
                         [{'line': 3, 'condition': None}])

        # unknown condition: kept as candidate, not treated as true
        self.assertNotIn('channel.tooldir', manifest['resolved'])
        deck = [c for c in manifest['candidates'] if c['path'] == 'channel.tooldir']
        self.assertEqual(len(deck), 1)
        self.assertEqual(deck[0]['value'], 'deck/tool')
        self.assertEqual(deck[0]['condition'], '[$STEAMDECK]')

        # false conditions are excluded and auditable
        excluded_paths = [e['value'] for e in manifest['excluded']]
        self.assertIn('lin/path', excluded_paths)
        self.assertIn('mac/path', excluded_paths)

        self.assertEqual(manifest['conflicts'], {})

    def test_empty_env_resolves_unconditional_only(self):
        manifest = channel.build_manifest(self.CONFIG, env={})

        self.assertEqual(manifest['resolved']['channel.installdir']['value'], 'common/path')
        self.assertNotIn('channel.tooldir', manifest['resolved'])

        # every conditional entry is preserved as a candidate
        self.assertEqual(len(manifest['candidates']), 4)
        self.assertEqual({c['value'] for c in manifest['candidates']},
                         {'win/path', 'lin/path', 'mac/path', 'deck/tool'})

    def test_conflict_is_not_last_wins(self):
        config = '\n'.join([
            '"channel"',
            '{',
            '    "installdir"  "first/path"  [$WINDOWS]',
            '    "installdir"  "second/path" [$WINDOWS && $64BIT]',
            '}',
        ])
        manifest = channel.build_manifest(config, env={'WINDOWS': True, '64BIT': True})

        # both branches are effective at the same tier: conflict, no silent last-wins
        self.assertNotIn('channel.installdir', manifest['resolved'])
        choices = manifest['conflicts']['channel.installdir']['choices']
        self.assertEqual([c['value'] for c in choices], ['first/path', 'second/path'])
        self.assertEqual([c['line'] for c in choices], [3, 4])

    def test_unconditional_duplicates_conflict(self):
        config = '"a"\n{\n"k" "v1"\n"k" "v2"\n}\n'
        manifest = channel.build_manifest(config, env={})
        self.assertNotIn('a.k', manifest['resolved'])
        self.assertEqual([c['value'] for c in manifest['conflicts']['a.k']['choices']],
                         ['v1', 'v2'])

    def test_same_value_duplicates_resolve_with_all_sources(self):
        config = '"a"\n{\n"k" "v" [$X]\n"k" "v" [$X]\n}\n'
        manifest = channel.build_manifest(config, env={'X': True})
        self.assertEqual(manifest['resolved']['a.k']['value'], 'v')
        self.assertEqual(len(manifest['resolved']['a.k']['sources']), 2)

    def test_conditional_overrides_unconditional(self):
        config = '"a"\n{\n"k" "base"\n"k" "override" [$X]\n}\n'
        manifest = channel.build_manifest(config, env={'X': True})
        self.assertEqual(manifest['resolved']['a.k']['value'], 'override')
        self.assertEqual(manifest['conflicts'], {})

    def test_section_condition_chain(self):
        config = '\n'.join([
            '"win" [$WINDOWS]',
            '{',
            '    "k" "v" [$64BIT]',
            '}',
        ])
        self.assertNotIn('win.k', channel.build_manifest(config, env={'WINDOWS': False})['resolved'])
        self.assertNotIn('win.k', channel.build_manifest(config, env={'WINDOWS': True})['resolved'])
        resolved = channel.build_manifest(config, env={'WINDOWS': True, '64BIT': True})['resolved']
        self.assertEqual(resolved['win.k']['value'], 'v')
        self.assertEqual(resolved['win.k']['sources'][0]['condition'],
                         '[$WINDOWS] && [$64BIT]')

    def test_strict_raises_on_conflict(self):
        config = '"a"\n{\n"k" "v1" [$X]\n"k" "v2" [$X]\n}\n'
        self.assertRaises(channel.ConflictError,
                          channel.build_manifest, config, env={'X': True}, strict=True)

        manifest = channel.build_manifest(config, env={'X': True}, strict=False)
        self.assertIn('a.k', manifest['conflicts'])

    def test_manifest_is_json_serializable(self):
        manifest = channel.build_manifest(self.CONFIG, env={'WINDOWS': True})
        json.dumps(manifest)


class testcase_local_environment(unittest.TestCase):
    def test_defines_platform_symbols(self):
        env = channel.local_environment()
        for symbol in ('WINDOWS', 'LINUX', 'OSX', 'POSIX', '64BIT'):
            self.assertIn(symbol, env)
            self.assertIsInstance(env[symbol], bool)

        import sys
        if sys.platform.startswith('win'):
            self.assertTrue(env['WINDOWS'])
        elif sys.platform == 'darwin':
            self.assertTrue(env['OSX'])
        elif sys.platform.startswith('linux'):
            self.assertTrue(env['LINUX'])


class testcase_cli(unittest.TestCase):
    CONFIG = '"channel"\n{\n"installdir" "win/path" [$WINDOWS]\n}\n'

    def _run_main(self, argv, stdin_text):
        out = StringIO()
        with mock.patch('sys.stdin', StringIO(stdin_text)), \
             mock.patch('sys.stdout', out):
            code = channel.main(argv)
        return code, out.getvalue()

    def test_main_resolves_with_env(self):
        code, output = self._run_main(['--env', 'WINDOWS'], self.CONFIG)
        self.assertEqual(code, 0)
        manifest = json.loads(output)
        self.assertEqual(manifest['resolved']['channel.installdir']['value'], 'win/path')

    def test_main_unknown_condition_kept_as_candidate(self):
        code, output = self._run_main([], self.CONFIG)
        self.assertEqual(code, 0)
        manifest = json.loads(output)
        self.assertNotIn('channel.installdir', manifest['resolved'])
        self.assertEqual(len(manifest['candidates']), 1)

    def test_main_conflict_exit_codes(self):
        config = '"a"\n{\n"k" "v1" [$X]\n"k" "v2" [$X]\n}\n'

        code, output = self._run_main(['--env', 'X=1'], config)
        self.assertEqual(code, 1)
        self.assertIn('a.k', json.loads(output)['conflicts'])

        code, output = self._run_main(['--env', 'X=1', '--strict'], config)
        self.assertEqual(code, 2)
        self.assertEqual(output, '')  # no manifest emitted in strict mode

    def test_main_invalid_env_value(self):
        with mock.patch('sys.stdin', StringIO(self.CONFIG)):
            self.assertRaises(SystemExit, channel.main, ['--env', 'X=maybe'])
