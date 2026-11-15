import json
import sys
import unittest
from collections import OrderedDict
from io import BytesIO, StringIO

import vdf
from vdf import json_interchange as ji

u = str if sys.version_info >= (3,) else unicode

UINT64_MAX = (1 << 64) - 1
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1


def sample_object():
    pairs = [
        ('plain_str', 'hello'),
        ('utf8', b'\xd0\xb0\xd0\xb1\xd0\xb2\xd0\xb3'.decode('utf-8')),
        ('plain_int', -500),
        ('a_float', 1.0),
        ('ptr', vdf.POINTER(1234)),
        ('color', vdf.COLOR(0x11223344)),
        ('u64', vdf.UINT_64(UINT64_MAX)),
        ('i64_min', vdf.INT_64(INT64_MIN)),
        ('i64_max', vdf.INT_64(INT64_MAX)),
    ]
    root = OrderedDict(pairs)
    root['nest'] = OrderedDict(pairs)
    root['nest']['deeper'] = OrderedDict([
        ('u64', vdf.UINT_64(42)),
        ('ptr', vdf.POINTER(-7)),
    ])
    return root


class TypedEncodeTests(unittest.TestCase):
    def test_envelope_shape(self):
        env = ji.encode_typed(sample_object())
        self.assertEqual(env[ji.ENVELOPE_KEY], ji.ENVELOPE_VERSION)
        self.assertIn(ji.DATA_KEY, env)
        self.assertIn(ji.SIDECAR_KEY, env)
        self.assertIsInstance(env[ji.SIDECAR_KEY], dict)

    def test_big_ints_are_strings_not_numbers(self):
        env = ji.encode_typed({'u': vdf.UINT_64(UINT64_MAX),
                               'i': vdf.INT_64(INT64_MIN)})
        self.assertEqual(env['data']['u'], u(str(UINT64_MAX)))
        self.assertEqual(env['data']['i'], u(str(INT64_MIN)))
        # serialised JSON must quote them, so no float coercion can happen
        text = json.dumps(env)
        self.assertIn(u('"%d"' % UINT64_MAX), text)
        self.assertIn(u('"%d"' % INT64_MIN), text)

    def test_sidecar_records_only_text_vdf_unrepresentable_types(self):
        env = ji.encode_typed(sample_object())
        sidecar = env[ji.SIDECAR_KEY]
        self.assertEqual(sidecar['/u64'], ji.TAG_UINT64)
        self.assertEqual(sidecar['/i64_min'], ji.TAG_INT64)
        self.assertEqual(sidecar['/i64_max'], ji.TAG_INT64)
        self.assertEqual(sidecar['/ptr'], ji.TAG_POINTER)
        self.assertEqual(sidecar['/color'], ji.TAG_COLOR)
        self.assertEqual(sidecar['/nest/u64'], ji.TAG_UINT64)
        self.assertEqual(sidecar['/nest/deeper/ptr'], ji.TAG_POINTER)
        # plain JSON scalars are not tracked in the sidecar
        for key in ('/plain_str', '/plain_int', '/a_float', '/utf8'):
            self.assertNotIn(key, sidecar)

    def test_plain_scalars_pass_through(self):
        env = ji.encode_typed({'s': 'x', 'n': 7, 'f': 2.5})
        self.assertEqual(env['data'], {'s': 'x', 'n': 7, 'f': 2.5})

    def test_json_pointer_escaping(self):
        obj = OrderedDict([
            ('a/b', vdf.UINT_64(1)),
            ('m~n', vdf.INT_64(2)),
        ])
        env = ji.encode_typed(obj)
        self.assertEqual(env[ji.SIDECAR_KEY]['/a~1b'], ji.TAG_UINT64)
        self.assertEqual(env[ji.SIDECAR_KEY]['/m~0n'], ji.TAG_INT64)
        # round trip keeps the raw keys
        back = ji.decode_typed(env, mapper=OrderedDict)
        self.assertEqual(list(back.keys()), ['a/b', 'm~n'])
        self.assertIsInstance(back['a/b'], vdf.UINT_64)
        self.assertIsInstance(back['m~n'], vdf.INT_64)

    def test_bool_rejected(self):
        with self.assertRaises(TypeError):
            ji.encode_typed({'flag': True})

    def test_unsupported_type_rejected(self):
        with self.assertRaises(TypeError):
            ji.encode_typed({'x': object()})


class DuplicateKeyTests(unittest.TestCase):
    def test_duplicate_keys_become_ordered_pairs(self):
        obj = vdf.VDFDict([('k', vdf.UINT_64(1)),
                           ('k', vdf.UINT_64(2)),
                           ('other', 'x')])
        env = ji.encode_typed(obj)
        # the whole mapping is an ordered array, not a collapsed object
        self.assertIsInstance(env[ji.DATA_KEY], list)
        self.assertEqual([pair[0] for pair in env[ji.DATA_KEY]],
                         ['k', 'k', 'other'])
        # sidecar still addresses each typed value precisely
        self.assertEqual(env[ji.SIDECAR_KEY]['/0/1'], ji.TAG_UINT64)
        self.assertEqual(env[ji.SIDECAR_KEY]['/1/1'], ji.TAG_UINT64)

    def test_duplicate_keys_round_trip(self):
        obj = vdf.VDFDict([('k', vdf.UINT_64(1)),
                           ('k', vdf.UINT_64(2)),
                           ('other', vdf.POINTER(9))])
        back = ji.decode_typed(ji.encode_typed(obj), mapper=vdf.VDFDict)
        self.assertTrue(back.has_duplicates())
        self.assertEqual(back.get_all_for('k'),
                         [vdf.UINT_64(1), vdf.UINT_64(2)])
        self.assertIsInstance(back['other'], vdf.POINTER)
        self.assertEqual(int(back['other']), 9)

    def test_nested_duplicate_mapping(self):
        inner = vdf.VDFDict([('dup', vdf.INT_64(-3)), ('dup', vdf.INT_64(-4))])
        outer = OrderedDict([('inner', inner), ('plain', 5)])
        env = ji.encode_typed(outer)
        # outer stays an object, only the duplicate inner level becomes a list
        self.assertIsInstance(env[ji.DATA_KEY], dict)
        self.assertIsInstance(env[ji.DATA_KEY]['inner'], list)
        self.assertEqual(env[ji.SIDECAR_KEY]['/inner/0/1'], ji.TAG_INT64)
        self.assertEqual(env[ji.SIDECAR_KEY]['/inner/1/1'], ji.TAG_INT64)

        # preserving duplicates needs a duplicate-capable mapper, just like
        # vdf.binary_loads itself
        back = ji.decode_typed(env, mapper=vdf.VDFDict)
        rebuilt_inner = back['inner']
        self.assertEqual(rebuilt_inner.get_all_for('dup'),
                         [vdf.INT_64(-3), vdf.INT_64(-4)])
        self.assertEqual(back['plain'], 5)

    def test_unique_key_vdfdict_stays_object(self):
        obj = vdf.VDFDict([('a', vdf.UINT_64(1)), ('b', 'x')])
        env = ji.encode_typed(obj)
        self.assertIsInstance(env[ji.DATA_KEY], dict)
        back = ji.decode_typed(env, mapper=vdf.VDFDict)
        self.assertEqual(back.get_all_for('a'), [vdf.UINT_64(1)])

    def test_malformed_pair_rejected(self):
        env = {
            ji.DATA_KEY: [['only-one']],
            ji.SIDECAR_KEY: {},
        }
        with self.assertRaises(ValueError):
            ji.decode_typed(env)

    def test_binary_round_trip_with_duplicates(self):
        # a real binary VDF blob carrying duplicate keys
        blob = (b'\x00a\x00'
                b'\x01k\x001\x00'
                b'\x01k\x002\x00'
                b'\x08\x08')
        obj = vdf.binary_loads(blob, mapper=vdf.VDFDict,
                               merge_duplicate_keys=False)
        self.assertEqual(obj['a'].get_all_for('k'), ['1', '2'])
        envelope = ji.binary_to_typed(blob, mapper=vdf.VDFDict,
                                      merge_duplicate_keys=False)
        rebuilt = ji.typed_to_binary(envelope, mapper=vdf.VDFDict)
        self.assertEqual(rebuilt, blob)


class TypedDecodeTests(unittest.TestCase):
    def test_reconstructs_exact_subclasses_and_values(self):
        env = ji.encode_typed(sample_object())
        back = ji.decode_typed(env, mapper=OrderedDict)

        self.assertIsInstance(back['u64'], vdf.UINT_64)
        self.assertEqual(int(back['u64']), UINT64_MAX)
        self.assertIsInstance(back['i64_min'], vdf.INT_64)
        self.assertEqual(int(back['i64_min']), INT64_MIN)
        self.assertIsInstance(back['i64_max'], vdf.INT_64)
        self.assertEqual(int(back['i64_max']), INT64_MAX)
        self.assertIsInstance(back['ptr'], vdf.POINTER)
        self.assertEqual(int(back['ptr']), 1234)
        self.assertIsInstance(back['color'], vdf.COLOR)
        self.assertEqual(int(back['color']), 0x11223344)
        self.assertIsInstance(back['nest']['deeper']['u64'], vdf.UINT_64)
        self.assertEqual(int(back['nest']['deeper']['u64']), 42)
        # plain values keep their native python types
        self.assertEqual(back['plain_int'], -500)
        self.assertNotIsInstance(back['plain_int'], vdf.BASE_INT)
        self.assertEqual(back['a_float'], 1.0)
        self.assertEqual(back['plain_str'], 'hello')

    def test_no_float_in_parsing_path(self):
        # a value beyond the exact integer range of a double must survive.
        # prove that the float path would have been lossy while the typed
        # path stays exact (parsed via int(), never float()).
        env = ji.encode_typed({'u': vdf.UINT_64(UINT64_MAX)})
        back = ji.decode_typed(env)
        self.assertEqual(int(back['u']), UINT64_MAX)
        self.assertNotEqual(int(float(UINT64_MAX)), UINT64_MAX)

    def test_decimal_text_required(self):
        env = {
            ji.DATA_KEY: {'u': '12.5'},
            ji.SIDECAR_KEY: {'/u': ji.TAG_UINT64},
        }
        with self.assertRaises(ValueError):
            ji.decode_typed(env)

    def test_typed_node_must_be_string(self):
        env = {
            ji.DATA_KEY: {'u': 1234},
            ji.SIDECAR_KEY: {'/u': ji.TAG_UINT64},
        }
        with self.assertRaises(ValueError):
            ji.decode_typed(env)

    def test_unknown_tag(self):
        env = {
            ji.DATA_KEY: {'u': '1'},
            ji.SIDECAR_KEY: {'/u': 'bogus'},
        }
        with self.assertRaises(ValueError):
            ji.decode_typed(env)

    def test_range_validation(self):
        cases = [
            (ji.TAG_UINT64, '-1'),
            (ji.TAG_UINT64, str(UINT64_MAX + 1)),
            (ji.TAG_INT64, str(INT64_MIN - 1)),
            (ji.TAG_INT64, str(INT64_MAX + 1)),
            (ji.TAG_POINTER, str(INT32_MAX + 1)),
            (ji.TAG_COLOR, str(INT32_MIN - 1)),
        ]
        for tag, raw in cases:
            env = {ji.DATA_KEY: {'v': raw},
                   ji.SIDECAR_KEY: {'/v': tag}}
            with self.assertRaises(ValueError):
                ji.decode_typed(env)

    def test_boundary_values_accepted(self):
        env = {
            ji.DATA_KEY: {
                'u_max': str(UINT64_MAX),
                'u_zero': '0',
                'i_min': str(INT64_MIN),
                'i_max': str(INT64_MAX),
                'p_min': str(INT32_MIN),
                'c_max': str(INT32_MAX),
            },
            ji.SIDECAR_KEY: {
                '/u_max': ji.TAG_UINT64,
                '/u_zero': ji.TAG_UINT64,
                '/i_min': ji.TAG_INT64,
                '/i_max': ji.TAG_INT64,
                '/p_min': ji.TAG_POINTER,
                '/c_max': ji.TAG_COLOR,
            },
        }
        back = ji.decode_typed(env)
        self.assertEqual(int(back['u_max']), UINT64_MAX)
        self.assertEqual(int(back['u_zero']), 0)
        self.assertEqual(int(back['i_min']), INT64_MIN)
        self.assertEqual(int(back['i_max']), INT64_MAX)
        self.assertEqual(int(back['p_min']), INT32_MIN)
        self.assertEqual(int(back['c_max']), INT32_MAX)

    def test_unresolved_sidecar_entry(self):
        env = {
            ji.DATA_KEY: {'present': '1'},
            ji.SIDECAR_KEY: {'/missing': ji.TAG_UINT64},
        }
        with self.assertRaises(ValueError):
            ji.decode_typed(env)

    def test_missing_data_key(self):
        with self.assertRaises(ValueError):
            ji.decode_typed({ji.SIDECAR_KEY: {}})

    def test_sidecar_optional(self):
        back = ji.decode_typed({ji.DATA_KEY: {'s': 'x', 'n': 3}})
        self.assertEqual(back, {'s': 'x', 'n': 3})


class FullRoundTripTests(unittest.TestCase):
    def test_object_typed_round_trip(self):
        obj = sample_object()
        back = ji.decode_typed(ji.encode_typed(obj), mapper=OrderedDict)
        self.assertEqual(back, obj)
        # structural and key ordering preserved
        self.assertEqual(list(back.keys()), list(obj.keys()))
        self.assertEqual(list(back['nest'].keys()), list(obj['nest'].keys()))

    def test_binary_bytes_round_trip_identical(self):
        obj = sample_object()
        original = vdf.binary_dumps(obj)

        envelope = ji.binary_to_typed(original, mapper=OrderedDict)
        rebuilt = ji.typed_to_binary(envelope, mapper=OrderedDict)

        self.assertEqual(rebuilt, original)
        # and the rebuilt object equals the source object
        self.assertEqual(vdf.binary_loads(rebuilt, mapper=OrderedDict), obj)

    def test_alt_format_round_trip(self):
        obj = sample_object()
        original = vdf.binary_dumps(obj, alt_format=True)
        envelope = ji.binary_to_typed(original, mapper=OrderedDict,
                                      alt_format=True)
        rebuilt = ji.typed_to_binary(envelope, mapper=OrderedDict,
                                     alt_format=True)
        self.assertEqual(rebuilt, original)

    def test_vbkv_round_trip(self):
        # vbkv_dumps packs the crc as a signed int; this payload's checksum is
        # in range (the unsigned-crc packing bug in the library is unrelated)
        obj = OrderedDict([
            ('u', vdf.UINT_64(42)),
            ('p', vdf.POINTER(7)),
        ])
        original = vdf.vbkv_dumps(obj)
        envelope = ji.encode_typed(vdf.vbkv_loads(original, mapper=OrderedDict))
        rebuilt = vdf.vbkv_dumps(ji.decode_typed(envelope, mapper=OrderedDict))
        self.assertEqual(rebuilt, original)
        self.assertEqual(vdf.vbkv_loads(rebuilt, mapper=OrderedDict), obj)

    def test_json_text_survives_standard_json_module(self):
        obj = sample_object()
        text = ji.typed_json_dumps(obj, indent=2)
        back = ji.typed_json_loads(text, mapper=OrderedDict)
        self.assertEqual(back, obj)
        # the JSON text is valid for any consumer
        parsed = json.loads(text)
        self.assertIsInstance(parsed['data']['u64'], str)

    def test_dump_load_stream(self):
        obj = sample_object()
        out = StringIO()
        ji.typed_json_dump(obj, out)
        out.seek(0)
        back = ji.typed_json_load(out, mapper=OrderedDict)
        self.assertEqual(back, obj)

    def test_package_level_api(self):
        obj = sample_object()
        text = vdf.typed_json_dumps(obj)
        back = vdf.typed_json_loads(text, mapper=OrderedDict)
        self.assertEqual(back, obj)
        rebuilt = vdf.typed_to_binary(vdf.binary_to_typed(
            vdf.binary_dumps(obj), mapper=OrderedDict), mapper=OrderedDict)
        self.assertEqual(rebuilt, vdf.binary_dumps(obj))


if __name__ == '__main__':
    unittest.main()
