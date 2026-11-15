import unittest

import vdf
from io import BytesIO
from collections import OrderedDict

u = str


def one(obj, alt_format=False):
    return vdf.binary_dumps(obj, alt_format=alt_format)


class BinaryVDFStream(unittest.TestCase):
    def test_first_object_variant_and_length(self):
        data = one({'a': 'x'})
        buf = BytesIO(data + b'\xff')

        record = vdf.binary_load_object(buf)

        self.assertEqual(record.data, {'a': 'x'})
        self.assertFalse(record.alt_format)
        self.assertEqual(record.variant, 'base')
        self.assertEqual(record.end_marker, vdf.BIN_END)
        self.assertEqual(record.consumed, len(data))
        self.assertEqual(record.offset, 0)
        self.assertEqual(buf.tell(), len(data))
        self.assertEqual(buf.read(), b'\xff')

    def test_alt_variant_detected(self):
        data = one({'a': 'x'}, alt_format=True)

        record = vdf.binary_load_object(BytesIO(data))

        self.assertTrue(record.alt_format)
        self.assertEqual(record.variant, 'alt')
        self.assertEqual(record.end_marker, vdf.BIN_END_ALT)
        self.assertEqual(record.consumed, len(data))

    def test_concatenated_mixed_variants(self):
        objects = [
            (one({'a': 1}), 'base'),
            (one({'b': {'c': 'd'}}, alt_format=True), 'alt'),
            (one({'e': 'f'}), 'base'),
            (one({'g': 1.5}, alt_format=True), 'alt'),
        ]
        data = b''.join(item for item, _ in objects)
        buf = BytesIO(data)

        records = list(vdf.binary_load_stream(buf))

        self.assertEqual([r.data for r in records], [
            {'a': 1}, {'b': {'c': 'd'}}, {'e': 'f'}, {'g': 1.5}])
        self.assertEqual([r.variant for r in records], [variant for _, variant in objects])
        self.assertEqual(sum(r.consumed for r in records), len(data))
        offset = 0
        expected_offsets = []
        for item, _ in objects:
            expected_offsets.append(offset)
            offset += len(item)
        self.assertEqual([r.offset for r in records], expected_offsets)
        self.assertEqual(buf.read(), b'')

    def test_nested_end_is_not_top_level_end(self):
        data = one({'outer': {'inner': {'value': 'x'}}})
        buf = BytesIO(data + one({'next': 1}))

        first = vdf.binary_load_object(buf)

        self.assertEqual(first.data, {'outer': {'inner': {'value': 'x'}}})
        self.assertEqual(first.consumed, len(data))
        self.assertEqual(vdf.binary_load_object(buf).data, {'next': 1})

    def test_nested_end_marker_never_bubbles_as_variant(self):
        # inner sections close with 0x08, outer object with 0x0B
        data = one({'a': {'b': {'c': 1}}}, alt_format=True)
        record = vdf.binary_load_object(BytesIO(data))

        self.assertEqual(record.data, {'a': {'b': {'c': 1}}})
        self.assertTrue(record.alt_format)
        self.assertEqual(record.consumed, len(data))

    def test_explicit_alt_format_rejects_mismatch(self):
        data = one({'a': 1}, alt_format=True)

        with self.assertRaises(SyntaxError):
            vdf.binary_load_object(BytesIO(data), alt_format=False)

        with self.assertRaises(SyntaxError):
            vdf.binary_load_object(BytesIO(one({'a': 1})), alt_format=True)

    def test_clean_end_of_stream(self):
        buf = BytesIO(b'')

        self.assertIsNone(vdf.binary_load_object(buf))
        self.assertEqual(buf.tell(), 0)
        self.assertEqual(list(vdf.binary_load_stream(BytesIO(b''))), [])
        self.assertEqual(list(vdf.binary_load_stream(b'')), [])

    def test_truncated_object_left_unconsumed(self):
        complete = one({'a': 'x'})
        tail = one({'b': 'y'})[:-1]
        buf = BytesIO(complete + tail)

        self.assertEqual(vdf.binary_load_object(buf).data, {'a': 'x'})
        start = buf.tell()
        with self.assertRaises(vdf.TruncatedBinaryVDF) as raised:
            vdf.binary_load_object(buf)
        self.assertEqual(buf.tell(), start)
        self.assertEqual(raised.exception.offset, start)
        self.assertEqual(buf.read(), tail)

    def test_stream_reexposes_only_truncated_object_after_reconnect(self):
        first = one({'a': 'x'})
        second = one({'nested': {'b': 'y'}})
        prefix = first + second[:-1]
        remainder = second[-1:]
        buf = BytesIO(prefix)

        records = list(vdf.binary_load_stream(buf))
        self.assertEqual([r.data for r in records], [{'a': 'x'}])
        held = buf.read()
        self.assertEqual(held, second[:-1])

        buf.seek(0)
        resumed = list(vdf.binary_load_stream(BytesIO(held + remainder)))
        self.assertEqual([r.data for r in resumed], [{'nested': {'b': 'y'}}])
        self.assertEqual([r.consumed for r in resumed], [len(second)])

    def test_truncation_inside_string(self):
        data = one({'key': 'value'})[:-1]
        buf = BytesIO(data)

        with self.assertRaises(vdf.TruncatedBinaryVDF) as raised:
            vdf.binary_load_object(buf)

        self.assertEqual(raised.exception.offset, 0)
        self.assertEqual(buf.tell(), 0)
        self.assertEqual(buf.read(), data)

    def test_truncation_inside_numeric_value(self):
        data = vdf.BIN_INT32 + b'k\x00' + b'\x01\x02'
        buf = BytesIO(data)

        with self.assertRaises(vdf.TruncatedBinaryVDF):
            vdf.binary_load_object(buf)

        self.assertEqual(buf.read(), data)

    def test_bytes_source_and_offset(self):
        first = one({'a': 1})
        second = one({'b': 2}, alt_format=True)
        data = first + second

        record = vdf.binary_load_object(data, offset=len(first))

        self.assertEqual(record.data, {'b': 2})
        self.assertEqual(record.offset, len(first))
        self.assertEqual(record.consumed, len(second))

    def test_bytes_source_reports_unconsumed_tail(self):
        data = one({'a': 1}) + one({'b': 2})[:-1]

        with self.assertRaises(vdf.TruncatedBinaryVDF) as raised:
            vdf.binary_load_object(data, offset=len(one({'a': 1})))

        self.assertEqual(raised.exception.tail, one({'b': 2})[:-1])
        self.assertEqual(raised.exception.offset, len(one({'a': 1})))

    def test_stream_returns_records_with_mapper(self):
        data = one({'a': 1}) + one({'b': 2}, alt_format=True)

        records = list(vdf.binary_load_stream(data, mapper=OrderedDict))

        self.assertEqual(records[0].data, OrderedDict([('a', 1)]))
        self.assertEqual(records[1].data, OrderedDict([('b', 2)]))

    def test_duplicate_merge_options(self):
        test = b'\x00a\x00\x01a\x001\x00\x01b\x002\x00\x08\x00a\x00\x01a\x003\x00\x01c\x004\x00\x08\x08'

        record = vdf.binary_load_object(BytesIO(test), merge_duplicate_keys=True)
        self.assertEqual(record.data, {'a': {'a': '3', 'b': '2', 'c': '4'}})

        record = vdf.binary_load_object(BytesIO(test), merge_duplicate_keys=False)
        self.assertEqual(record.data, {'a': {'a': '3', 'c': '4'}})

    def test_nested_marker_does_not_define_variant(self):
        # a nested section may close with either marker; the object's variant
        # is determined by its own top-level terminator
        data = vdf.BIN_NONE + b'k\x00' + vdf.BIN_END_ALT + vdf.BIN_END
        record = vdf.binary_load_object(BytesIO(data))

        self.assertEqual(record.data, {'k': {}})
        self.assertFalse(record.alt_format)
        self.assertEqual(record.consumed, len(data))

    def test_unknown_type(self):
        with self.assertRaises(SyntaxError):
            vdf.binary_load_object(BytesIO(b'\x33a\x00\x08'))

    def test_type_checks(self):
        with self.assertRaises(TypeError):
            vdf.binary_load_object(1234)
        with self.assertRaises(TypeError):
            vdf.binary_load_object(b'', mapper=list)
        with self.assertRaises(TypeError):
            list(vdf.binary_load_stream(1234))
        with self.assertRaises(ValueError):
            list(vdf.binary_load_stream(b'', on_truncated='ignore'))

    def test_on_truncated_raise(self):
        data = one({'a': 1}) + one({'b': 2})[:-1]
        buf = BytesIO(data)

        records = []
        with self.assertRaises(vdf.TruncatedBinaryVDF):
            for record in vdf.binary_load_stream(buf, on_truncated='raise'):
                records.append(record)
        self.assertEqual([r.data for r in records], [{'a': 1}])
        self.assertEqual(buf.read(), one({'b': 2})[:-1])

    def test_stream_class_iteration_and_resume(self):
        first = one({'a': 1})
        second = one({'b': 2})
        stream = vdf.BinaryVDFStream(first + second[:-1])

        self.assertEqual(next(stream).data, {'a': 1})
        with self.assertRaises(vdf.TruncatedBinaryVDF):
            stream.read()
        self.assertEqual(stream.position, len(first))
        self.assertEqual(stream.remaining, len(second) - 1)

        stream.feed(second[-1:])

        self.assertEqual(next(stream).data, {'b': 2})
        self.assertIsNone(stream.read())
        self.assertEqual(stream.position, len(first) + len(second))

    def test_short_reading_source(self):
        class Chunked(object):
            def __init__(self, data, step):
                self.data = data
                self.pos = 0
                self.step = step

            def read(self, size=-1):
                if size is None or size < 0:
                    size = len(self.data) - self.pos
                size = min(size, self.step, max(0, len(self.data) - self.pos))
                chunk = self.data[self.pos:self.pos + size]
                self.pos += len(chunk)
                return chunk

            def tell(self):
                return self.pos

            def seek(self, offset, whence=0):
                if whence == 0:
                    self.pos = offset
                elif whence == 1:
                    self.pos += offset
                else:
                    self.pos = len(self.data) + offset
                return self.pos

        first = one({'a': 1})
        second = one({'b': {'c': 'd'}}, alt_format=True)
        blob = first + second

        for step in (1, 3, 7, 64):
            records = list(vdf.binary_load_stream(Chunked(blob, step)))
            self.assertEqual([r.data for r in records],
                             [{'a': 1}, {'b': {'c': 'd'}}])
            self.assertEqual([r.consumed for r in records], [len(first), len(second)])

            source = Chunked(blob[:-1], step)
            records = list(vdf.binary_load_stream(source))
            self.assertEqual([r.data for r in records], [{'a': 1}])
            self.assertEqual(source.tell(), len(first))

    def test_mixed_nested_markers(self):
        for object_alt in (False, True):
            for nested_alt in (False, True):
                inner = (vdf.BIN_NONE + b'i\x00' + vdf.BIN_STRING + b'k\x00v\x00' +
                         (vdf.BIN_END_ALT if nested_alt else vdf.BIN_END))
                outer = (vdf.BIN_NONE + b'o\x00' + inner +
                         (vdf.BIN_END_ALT if object_alt else vdf.BIN_END) * 2)
                record = vdf.binary_load_object(BytesIO(outer))
                self.assertEqual(record.data, {'o': {'i': {'k': 'v'}}})
                self.assertEqual(record.alt_format, object_alt)
                self.assertEqual(record.consumed, len(outer))

    def test_empty_object(self):
        record = vdf.binary_load_object(BytesIO(vdf.BIN_END))
        self.assertEqual(record.data, {})
        self.assertEqual(record.variant, 'base')
        self.assertEqual(record.consumed, 1)

        record = vdf.binary_load_object(BytesIO(vdf.BIN_END_ALT))
        self.assertEqual(record.data, {})
        self.assertEqual(record.variant, 'alt')
        self.assertEqual(record.consumed, 1)

        with self.assertRaises(SyntaxError):
            vdf.binary_load_object(BytesIO(vdf.BIN_END), alt_format=True)

    def test_record_indexing_and_unpacking(self):
        record = vdf.binary_load_object(BytesIO(one({'a': 1}) + one({'b': 2})))

        data, alt_format, consumed = record
        self.assertEqual(data, {'a': 1})
        self.assertFalse(alt_format)
        self.assertEqual(record['consumed'], record.consumed)
        self.assertEqual(record['variant'], 'base')
        self.assertEqual(record.get('missing', 0), 0)
        self.assertEqual(record.length, consumed)


if __name__ == '__main__':
    unittest.main()
