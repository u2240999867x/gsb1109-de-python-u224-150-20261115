# -*- coding: utf-8 -*-
import unittest

import vdf


class testcase_scan(unittest.TestCase):
    def test_basic_key_value_spans(self):
        src = '"root"\n{\n\t"a" "1"\n}\n'
        entries = vdf.scan(src)

        self.assertEqual(len(entries), 2)

        section = entries[0]
        self.assertEqual(section.key, 'root')
        self.assertIsNone(section.value)
        self.assertIsNone(section.value_span)
        self.assertEqual(section.key_span, (1, 5))
        self.assertEqual(section.path, ())

        kv = entries[1]
        self.assertEqual(kv.key, 'a')
        self.assertEqual(kv.value, '1')
        self.assertEqual(kv.path, ('root',))
        self.assertEqual(kv.key_span, (11, 12))
        self.assertEqual(kv.value_span, (15, 16))

        bsrc = src.encode('utf-8')
        self.assertEqual(bsrc[kv.key_span[0]:kv.key_span[1]], b'a')
        self.assertEqual(bsrc[kv.value_span[0]:kv.value_span[1]], b'1')

    def test_spans_exclude_quotes_but_cover_raw_content(self):
        src = '"key" "va\\tlue"'
        (kv,) = vdf.scan(src)
        bsrc = src.encode('utf-8')
        self.assertEqual(bsrc[kv.key_span[0]:kv.key_span[1]], b'key')
        self.assertEqual(bsrc[kv.value_span[0]:kv.value_span[1]], b'va\\tlue')
        self.assertEqual(kv.value, 'va\tlue')

    def test_comment_marker_inside_quotes_is_content(self):
        src = '"a" "x // y" // real comment\n"b" "2"'
        entries = vdf.scan(src)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].value, 'x // y')
        self.assertEqual(entries[1].key, 'b')
        self.assertEqual(entries[1].value, '2')

    def test_comment_lines_are_skipped(self):
        src = '// header\n"a" "1"\n// footer'
        (kv,) = vdf.scan(src)
        self.assertEqual(kv.key, 'a')
        self.assertEqual(kv.value, '1')

    def test_escaped_quote_does_not_end_value(self):
        src = r'"a" "x\"y" "b" "2"'
        entries = vdf.scan(src)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].value, 'x"y')
        bsrc = src.encode('utf-8')
        self.assertEqual(bsrc[entries[0].value_span[0]:entries[0].value_span[1]],
                         b'x\\"y')
        self.assertEqual(entries[1].key, 'b')
        self.assertEqual(entries[1].value, '2')

    def test_escaped_backslash_before_quote_ends_value(self):
        src = '"a" "x\\\\" "b" "2"'
        entries = vdf.scan(src)
        self.assertEqual(entries[0].value, 'x\\')
        self.assertEqual(entries[1].key, 'b')

    def test_mixed_newlines_before_multibyte_chars(self):
        src = '"a" "1"\r\n"b" "é"\n"c" "中"\r\n'
        entries = vdf.scan(src)
        bsrc = src.encode('utf-8')

        self.assertEqual([e.key for e in entries], ['a', 'b', 'c'])

        # "a" "1"\r\n is 9 bytes, so "b" starts at byte 9
        self.assertEqual(entries[1].key_span, (10, 11))
        self.assertEqual(entries[1].value_span, (14, 16))  # é is 2 bytes
        self.assertEqual(bsrc[14:16], 'é'.encode('utf-8'))

        self.assertEqual(entries[2].key_span, (19, 20))
        self.assertEqual(entries[2].value_span, (23, 26))  # 中 is 3 bytes
        self.assertEqual(bsrc[23:26], '中'.encode('utf-8'))

        for e in entries:
            self.assertEqual(bsrc[e.key_span[0]:e.key_span[1]],
                             e.key.encode('utf-8'))
            self.assertEqual(bsrc[e.value_span[0]:e.value_span[1]],
                             e.value.encode('utf-8'))

    def test_unterminated_final_value_keeps_previous_entries(self):
        src = '"a" "1"\n"b" "2'
        entries = vdf.scan(src)

        # the unterminated final item is dropped, complete entries survive
        self.assertEqual([(e.key, e.value) for e in entries], [('a', '1')])

    def test_unterminated_final_key_keeps_previous_entries(self):
        src = '"a" "1"\n"b'
        entries = vdf.scan(src)
        self.assertEqual([(e.key, e.value) for e in entries], [('a', '1')])

    def test_multiline_quoted_value(self):
        src = '"a" "1"\n"b" "x\ny"\n"c" "3"'
        entries = vdf.scan(src)
        self.assertEqual([(e.key, e.value) for e in entries],
                         [('a', '1'), ('b', 'x\ny'), ('c', '3')])

    def test_nested_sections_and_paths(self):
        src = '"root"\n{\n\t"sub" { "k" "v" }\n}\n'
        entries = vdf.scan(src)

        self.assertEqual([e.key for e in entries], ['root', 'sub', 'k'])
        self.assertEqual(entries[0].path, ())
        self.assertEqual(entries[1].path, ('root',))
        self.assertEqual(entries[2].path, ('root', 'sub'))
        self.assertEqual(entries[2].value, 'v')

    def test_empty_inline_section(self):
        src = '"a" {}'
        (section,) = vdf.scan(src)
        self.assertEqual(section.key, 'a')
        self.assertIsNone(section.value)
        self.assertIsNone(section.value_span)

    def test_unquoted_tokens(self):
        src = 'a 1'
        (kv,) = vdf.scan(src)
        self.assertEqual(kv.key, 'a')
        self.assertEqual(kv.value, '1')
        self.assertEqual(kv.key_span, (0, 1))
        self.assertEqual(kv.value_span, (2, 3))

    def test_bytes_input(self):
        src = '"a" "é"'.encode('utf-8')
        (kv,) = vdf.scan(src)
        self.assertEqual(kv.value, 'é')
        self.assertEqual(src[kv.value_span[0]:kv.value_span[1]],
                         'é'.encode('utf-8'))

    def test_file_like_input(self):
        from io import StringIO
        (kv,) = vdf.scan(StringIO('"a" "1"'))
        self.assertEqual((kv.key, kv.value), ('a', '1'))

    def test_bom_does_not_shift_spans(self):
        src = '\ufeff"a" "1"'
        (kv,) = vdf.scan(src)
        bsrc = src.encode('utf-8')
        self.assertEqual(bsrc[kv.key_span[0]:kv.key_span[1]], b'a')
        self.assertEqual(bsrc[kv.value_span[0]:kv.value_span[1]], b'1')

    def test_escaped_false_keeps_raw(self):
        src = r'"a" "x\ny"'
        (kv,) = vdf.scan(src, escaped=False)
        self.assertEqual(kv.value, 'x\\ny')

    def test_platform_conditional_is_not_an_entry(self):
        src = '"a" "1" [$WIN32]\n"b" "2"'
        entries = vdf.scan(src)
        self.assertEqual([e.key for e in entries], ['a', 'b'])

    def test_duplicate_keys_each_get_own_span(self):
        src = '"a" "1"\n"a" "2"'
        entries = vdf.scan(src)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].value_span, (5, 6))
        self.assertEqual(entries[1].value_span, (13, 14))

    def test_too_many_closing_brackets_raises(self):
        self.assertRaises(SyntaxError, vdf.scan, '}')

    def test_missing_opening_bracket_raises(self):
        self.assertRaises(SyntaxError, vdf.scan, '"a"\n"b" "1"')

    def test_bad_source_type_raises(self):
        for bad in (5, 5.5, None, [], {}, lambda: 0):
            self.assertRaises(TypeError, vdf.scan, bad)


if __name__ == '__main__':
    unittest.main()
