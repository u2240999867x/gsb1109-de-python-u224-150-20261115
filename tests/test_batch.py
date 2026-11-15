#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import unittest

from xmlschema import XMLSchema, XMLSchema11, BatchValidator, BatchRecord, \
    BatchRecordState, BatchError, BatchValidationError, DuplicateKeyError, \
    UnresolvedKeyrefError


SCHEMA_SOURCE = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="batch">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="entry" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="id" type="xs:string"/>
              <xs:element name="ref" type="xs:string" minOccurs="0"/>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
    <xs:key name="entry_key">
      <xs:selector xpath="entry"/>
      <xs:field xpath="id"/>
    </xs:key>
    <xs:keyref name="entry_ref" refer="entry_key">
      <xs:selector xpath="entry"/>
      <xs:field xpath="ref"/>
    </xs:keyref>
  </xs:element>
</xs:schema>
"""


def entry(id_, ref=None):
    if ref is None:
        return '<entry><id>%s</id></entry>' % id_
    return '<entry><id>%s</id><ref>%s</ref></entry>' % (id_, ref)


def record_source(*entries):
    return '<batch>%s</batch>' % ''.join(entries)


class TestBatchValidator(unittest.TestCase):

    schema_class = XMLSchema

    @classmethod
    def setUpClass(cls):
        cls.schema = cls.schema_class(SCHEMA_SOURCE)

    def make_validator(self):
        return BatchValidator(self.schema_class(SCHEMA_SOURCE))

    def test_record_states(self):
        validator = self.make_validator()
        record = validator.add(record_source(entry('a')))

        self.assertIsInstance(record, BatchRecord)
        self.assertEqual(record.index, 0)
        self.assertTrue(record.parsed)
        self.assertTrue(record.locally_valid)
        self.assertFalse(record.committable)
        self.assertIs(record.state, BatchRecordState.LOCALLY_VALID)
        self.assertFalse(record.errors)
        self.assertFalse(validator.committable)

        self.assertTrue(validator.finalize())
        self.assertTrue(record.committable)
        self.assertIs(record.state, BatchRecordState.COMMITTABLE)
        self.assertTrue(validator.committable)
        self.assertTrue(validator.finalized)

    def test_parse_error(self):
        validator = self.make_validator()
        record = validator.add('<batch><entry></batch>')

        self.assertIsNone(record.state)
        self.assertFalse(record.parsed)
        self.assertFalse(record.locally_valid)
        self.assertEqual(len(record.errors), 1)

        self.assertFalse(validator.finalize())
        self.assertFalse(validator.committable)

    def test_local_validation_error(self):
        validator = self.make_validator()
        record = validator.add('<batch><entry><ref>a</ref></entry></batch>')

        self.assertIs(record.state, BatchRecordState.PARSED)
        self.assertTrue(record.parsed)
        self.assertFalse(record.locally_valid)
        self.assertTrue(record.errors)

        self.assertFalse(validator.finalize())
        self.assertIs(record.state, BatchRecordState.PARSED)

    def test_locally_valid_is_not_success(self):
        validator = self.make_validator()
        record1 = validator.add(record_source(entry('a')))
        record2 = validator.add(record_source(entry('b')))

        self.assertTrue(record1.locally_valid)
        self.assertTrue(record2.locally_valid)
        self.assertFalse(record1.committable)
        self.assertFalse(record2.committable)
        self.assertFalse(validator.committable)

        self.assertTrue(validator.finalize())
        self.assertTrue(record1.committable)
        self.assertTrue(record2.committable)

    def test_duplicate_key_across_records(self):
        validator = self.make_validator()
        record1 = validator.add(record_source(entry('a')), name='first.xml')
        record2 = validator.add(record_source(entry('b')))
        record3 = validator.add(record_source(entry('a')), name='third.xml')

        self.assertTrue(all(r.locally_valid for r in validator.records))
        self.assertFalse(validator.finalize())

        self.assertEqual(len(validator.errors), 1)
        error = validator.errors[0]
        self.assertIsInstance(error, DuplicateKeyError)
        self.assertIs(error.first, record1)
        self.assertIs(error.second, record3)
        self.assertIn('first.xml', str(error))
        self.assertIn('third.xml', str(error))
        self.assertIn("('a',)", str(error))

        self.assertEqual(record1.batch_errors, [error])
        self.assertEqual(record3.batch_errors, [error])
        self.assertFalse(record2.batch_errors)

        self.assertFalse(validator.committable)
        self.assertFalse(record1.committable)
        self.assertIs(record1.state, BatchRecordState.LOCALLY_VALID)

    def test_duplicate_key_within_record(self):
        validator = self.make_validator()
        record = validator.add(record_source(entry('a'), entry('a')))

        self.assertTrue(record.parsed)
        self.assertFalse(record.locally_valid)
        self.assertTrue(record.errors)
        self.assertFalse(validator.finalize())

    def test_late_reference(self):
        validator = self.make_validator()
        record1 = validator.add(record_source(entry('a', ref='b')))
        record2 = validator.add(record_source(entry('b')))

        self.assertTrue(record1.locally_valid)
        self.assertTrue(record2.locally_valid)
        self.assertTrue(validator.finalize())
        self.assertTrue(validator.committable)
        self.assertTrue(record1.committable)
        self.assertTrue(record2.committable)

    def test_unresolved_keyref(self):
        validator = self.make_validator()
        record1 = validator.add(record_source(entry('a', ref='missing')))

        self.assertTrue(record1.locally_valid)
        self.assertFalse(validator.finalize())

        self.assertEqual(len(validator.errors), 1)
        error = validator.errors[0]
        self.assertIsInstance(error, UnresolvedKeyrefError)
        self.assertIs(error.record, record1)
        self.assertEqual(error.value, ('missing',))
        self.assertIn('missing', str(error))
        self.assertEqual(record1.batch_errors, [error])
        self.assertFalse(record1.committable)

    def test_late_reference_completed_after_failed_finalize(self):
        validator = self.make_validator()
        record1 = validator.add(record_source(entry('a', ref='b')))

        self.assertFalse(validator.finalize())
        self.assertFalse(record1.committable)

        record2 = validator.add(record_source(entry('b')))
        self.assertIs(record1.state, BatchRecordState.LOCALLY_VALID)
        self.assertFalse(validator.finalized)

        self.assertTrue(validator.finalize())
        self.assertTrue(record1.committable)
        self.assertTrue(record2.committable)
        self.assertFalse(validator.errors)
        self.assertFalse(record1.batch_errors)

    def test_keyref_within_record(self):
        validator = self.make_validator()
        record = validator.add(record_source(entry('a'), entry('b', ref='a')))

        self.assertTrue(record.locally_valid)
        self.assertTrue(validator.finalize())
        self.assertTrue(record.committable)

    def test_commit(self):
        validator = self.make_validator()
        validator.add(record_source(entry('a', ref='b')))

        with self.assertRaises(BatchValidationError) as ctx:
            validator.commit()
        self.assertEqual(len(ctx.exception.errors), 1)
        self.assertIsInstance(ctx.exception.errors[0], UnresolvedKeyrefError)

        validator.add(record_source(entry('b')))
        self.assertIsNone(validator.commit())
        self.assertTrue(validator.committable)

    def test_commit_with_invalid_record(self):
        validator = self.make_validator()
        validator.add(record_source(entry('a')))
        validator.add('<batch><entry></batch>')

        with self.assertRaises(BatchValidationError) as ctx:
            validator.commit()
        self.assertFalse(ctx.exception.errors)
        self.assertEqual(len(ctx.exception.invalid_records), 1)

    def test_add_demotes_committable_records(self):
        validator = self.make_validator()
        record1 = validator.add(record_source(entry('a')))
        self.assertTrue(validator.finalize())
        self.assertTrue(record1.committable)

        validator.add(record_source(entry('b')))
        self.assertIs(record1.state, BatchRecordState.LOCALLY_VALID)
        self.assertFalse(validator.finalized)
        self.assertFalse(validator.committable)

    def test_reset(self):
        validator = self.make_validator()
        validator.add(record_source(entry('a')))
        validator.finalize()
        validator.reset()

        self.assertEqual(len(validator), 0)
        self.assertFalse(validator.finalized)
        self.assertFalse(validator.errors)

    def test_record_name(self):
        validator = self.make_validator()
        record = validator.add(record_source(entry('a')), name='chunk-01.xml')
        self.assertEqual(record.name, 'chunk-01.xml')

        record = validator.add(record_source(entry('b')))
        self.assertEqual(record.name, 'record #1')

    def test_errors_are_xmlschema_exceptions(self):
        from xmlschema import XMLSchemaException
        self.assertTrue(issubclass(BatchError, XMLSchemaException))
        self.assertTrue(issubclass(DuplicateKeyError, BatchError))
        self.assertTrue(issubclass(UnresolvedKeyrefError, BatchError))
        self.assertTrue(issubclass(BatchValidationError, BatchError))

    def test_repr(self):
        validator = self.make_validator()
        record = validator.add(record_source(entry('a')))
        self.assertEqual(repr(record), "BatchRecord(#0, 'record #0', locally_valid)")
        self.assertEqual(repr(validator), 'BatchValidator(records=1, finalized=False)')

    def test_schema_source_argument(self):
        validator = BatchValidator(SCHEMA_SOURCE)
        record = validator.add(record_source(entry('a')))
        self.assertTrue(record.locally_valid)
        self.assertTrue(validator.finalize())


class TestBatchValidator11(TestBatchValidator):

    schema_class = XMLSchema11


if __name__ == '__main__':
    from xmlschema.testing import run_xmlschema_tests
    run_xmlschema_tests('batch validator')
