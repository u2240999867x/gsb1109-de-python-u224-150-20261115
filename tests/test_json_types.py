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
"""Tests for the JSON-safe representations of XSD atomic values."""
import json
import unittest
from decimal import Decimal

from elementpath import datatypes

import xmlschema
from xmlschema.json_types import JsonDecimal, JsonInteger, JsonValueError, \
    JSON_SAFE_INTEGER, to_json_value, from_json_value, json_value_key, \
    json_values_match, group_json_values, get_timezone_info, encode_json_values, \
    decode_json_values


class TestJsonTypes(unittest.TestCase):

    def test_decimal_lexical_value(self):
        value = JsonDecimal(Decimal('100.50'), '+00100.5000')
        self.assertEqual(value, Decimal('100.5'))
        self.assertEqual(value.lexical, '+00100.5000')
        self.assertEqual(value.as_tuple().exponent, -2)

        obj = to_json_value(value)
        self.assertEqual(obj['$type'], 'decimal')
        self.assertEqual(obj['lexical'], '+00100.5000')
        self.assertEqual(obj['value'], '100.5')
        self.assertEqual(from_json_value(obj), '+00100.5000')

    def test_same_decimal_value_can_be_associated(self):
        obj1 = to_json_value(JsonDecimal(Decimal('1.0'), '1.0'))
        obj2 = to_json_value(JsonDecimal(Decimal('1.00'), '1.00'))
        obj3 = to_json_value(JsonDecimal(Decimal('1.000'), '+001.000'))

        self.assertEqual(json_value_key(obj1), 'decimal:1')
        self.assertEqual(json_value_key(obj2), 'decimal:1')
        self.assertEqual(json_value_key(obj3), 'decimal:1')
        self.assertTrue(json_values_match(obj1, obj2))
        self.assertTrue(json_values_match(obj2, obj3))

        # The original precision is preserved in each typed value
        self.assertEqual(obj1['lexical'], '1.0')
        self.assertEqual(obj2['lexical'], '1.00')
        self.assertEqual(obj3['lexical'], '+001.000')

        groups = group_json_values([obj1, obj2, obj3])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups['decimal:1']), 3)

    def test_different_decimals_are_not_associated(self):
        obj1 = to_json_value(Decimal('1.0'))
        obj2 = to_json_value(Decimal('1.01'))
        self.assertNotEqual(json_value_key(obj1), json_value_key(obj2))
        self.assertFalse(json_values_match(obj1, obj2))

    def test_negative_zero_decimal(self):
        obj = to_json_value(Decimal('-0.00'))
        self.assertEqual(obj['value'], '0')
        self.assertEqual(obj['lexical'], '-0.00')

    def test_large_decimal_is_not_rounded(self):
        value = Decimal('123456789012345678901234567890.000')
        obj = to_json_value(value)
        self.assertEqual(obj['value'], '123456789012345678901234567890')

    def test_canonical_decimal_forms(self):
        self.assertEqual(to_json_value(Decimal('1E+3'))['value'], '1000')
        self.assertEqual(to_json_value(Decimal('1.2300E+5'))['value'], '123000')
        self.assertEqual(to_json_value(Decimal('1E-10'))['value'], '0.0000000001')

    def test_big_integer(self):
        big = 10 ** 40 + 1
        obj = to_json_value(JsonInteger(big, '+000' + str(big)))
        self.assertEqual(obj['$type'], 'integer')
        self.assertEqual(obj['value'], str(big))
        self.assertEqual(obj['lexical'], '+000' + str(big))
        self.assertEqual(from_json_value(obj), '+000' + str(big))

        self.assertGreater(abs(big), JSON_SAFE_INTEGER)

    def test_integer_encoding_modes(self):
        data = {'small': 10, 'big': 2 ** 60}

        encoded = encode_json_values(data)
        self.assertEqual(encoded['small'], 10)
        self.assertEqual(encoded['big']['$type'], 'integer')

        encoded = encode_json_values(data, integers='all')
        self.assertEqual(encoded['small']['$type'], 'integer')
        self.assertEqual(encoded['big']['$type'], 'integer')

        with self.assertRaises(xmlschema.exceptions.XMLSchemaValueError):
            encode_json_values(data, integers='unknown')

    def test_date_time_without_timezone_is_floating(self):
        value = datatypes.DateTime10.fromstring('2019-01-01T13:40:00')
        obj = to_json_value(value)

        self.assertEqual(obj['$type'], 'dateTime')
        self.assertEqual(obj['value'], '2019-01-01T13:40:00')
        self.assertIsNone(obj['timezone'])

        # A floating value must not fall back to the local timezone.
        self.assertIsNone(get_timezone_info(obj, 'Europe/Rome'))
        self.assertEqual(from_json_value(obj), '2019-01-01T13:40:00')

    def test_date_time_with_timezone(self):
        value = datatypes.DateTime10.fromstring('2019-01-01T13:40:00+05:30')
        obj = to_json_value(value)

        self.assertEqual(obj['$type'], 'dateTime')
        self.assertEqual(obj['timezone'], '+05:30')
        self.assertEqual(get_timezone_info(obj, 'Europe/Rome'), '+05:30')
        self.assertEqual(from_json_value(obj), '2019-01-01T13:40:00+05:30')

    def test_date_types(self):
        date = datatypes.Date10.fromstring('2001-04-15')
        obj = to_json_value(date)
        self.assertEqual(obj['$type'], 'date')
        self.assertEqual(obj['lexical'], '2001-04-15')
        self.assertIsNone(obj['timezone'])

        time = datatypes.Time.fromstring('12:00:00Z')
        obj = to_json_value(time)
        self.assertEqual(obj['$type'], 'time')
        self.assertEqual(obj['timezone'], 'Z')

    def test_duration_value(self):
        duration = datatypes.Duration.fromstring('P5Y3M')
        obj = to_json_value(duration)
        self.assertEqual(obj['$type'], 'duration')
        self.assertEqual(obj['value'], 'P5Y3M')
        self.assertEqual(from_json_value(obj), 'P5Y3M')

    def test_invalid_typed_values(self):
        with self.assertRaises(JsonValueError):
            from_json_value({'value': '1.0'})

        with self.assertRaises(JsonValueError):
            from_json_value({'$type': 'unknown', 'value': 'x'})

        with self.assertRaises(JsonValueError):
            from_json_value({'$type': 'decimal', 'value': 'NaN'})

        with self.assertRaises(JsonValueError):
            from_json_value({'$type': 'integer', 'value': '1.5'})

        with self.assertRaises(JsonValueError):
            json_value_key({'$type': 'decimal', 'value': 'NaN'})

    def test_decode_json_values_strict(self):
        data = {'a': {'$type': 'decimal', 'value': '1.5', 'lexical': '1.50'}}
        self.assertEqual(decode_json_values(data), {'a': '1.50'})
        self.assertEqual(decode_json_values(data, strict=True), {'a': '1.50'})

        malformed = {'a': {'$type': 'unknown', 'value': 'x'}}
        self.assertEqual(decode_json_values(malformed),
                         {'a': {'$type': 'unknown', 'value': 'x'}})
        with self.assertRaises(JsonValueError):
            decode_json_values(malformed, strict=True)

    def test_dumps_and_loads(self):
        data = {'amount': Decimal('1.50'), 'count': 2 ** 60, 'plain': 1}
        text = xmlschema.json_dumps(data)
        decoded = json.loads(text)
        self.assertEqual(decoded['plain'], 1)
        self.assertEqual(decoded['amount']['lexical'], '1.50')
        self.assertEqual(decoded['count']['$type'], 'integer')

        round_tripped = xmlschema.json_loads(text)
        self.assertEqual(round_tripped, {
            'amount': '1.50',
            'count': str(2 ** 60),
            'plain': 1,
        })


class TestJsonDocumentApi(unittest.TestCase):

    xsd = """<?xml version="1.0" encoding="UTF-8"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="root">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="decimal" type="xs:decimal"/>
            <xs:element name="integer" type="xs:integer"/>
            <xs:element name="dateTime" type="xs:dateTime"/>
            <xs:element name="date" type="xs:date"/>
          </xs:sequence>
        </xs:complexType>
      </xs:element>
    </xs:schema>"""

    xml = '<root>' \
          '<decimal>+00100.5000</decimal>' \
          '<integer>+00123456789012345678901234567890</integer>' \
          '<dateTime>2019-01-01T13:40:00.1000</dateTime>' \
          '<date>2001-04-15+05:30</date>' \
          '</root>'

    def setUp(self):
        self.schema = xmlschema.XMLSchema10(self.xsd)

    def test_to_json_default_is_unchanged(self):
        data = json.loads(xmlschema.to_json(self.xml, schema=self.schema))
        self.assertEqual(data['decimal'], 100.5)
        self.assertEqual(data['integer'], 123456789012345678901234567890)
        self.assertEqual(data['dateTime'], '2019-01-01T13:40:00.1000')
        self.assertEqual(data['date'], '2001-04-15+05:30')

    def test_to_json_with_json_types(self):
        data = json.loads(xmlschema.to_json(self.xml, schema=self.schema, json_types=True))

        self.assertEqual(data['decimal'], {
            '$type': 'decimal', 'value': '100.5', 'lexical': '+00100.5000'
        })
        self.assertEqual(data['integer']['$type'], 'integer')
        self.assertEqual(data['integer']['lexical'],
                         '+00123456789012345678901234567890')
        self.assertEqual(data['dateTime']['lexical'], '2019-01-01T13:40:00.1000')
        self.assertIsNone(data['dateTime']['timezone'])
        self.assertEqual(data['date']['timezone'], '+05:30')

    def test_from_json_with_json_types(self):
        json_data = xmlschema.to_json(self.xml, schema=self.schema, json_types=True)
        root = xmlschema.from_json(json_data, schema=self.schema, json_types=True)

        self.assertEqual(root.findtext('decimal'), '+00100.5000')
        self.assertEqual(root.findtext('integer'),
                         '+00123456789012345678901234567890')
        self.assertEqual(root.findtext('dateTime'), '2019-01-01T13:40:00.1000')
        self.assertEqual(root.findtext('date'), '2001-04-15+05:30')

    def test_round_trip_keeps_lexical_values(self):
        json_data = xmlschema.to_json(self.xml, schema=self.schema, json_types=True)
        root = xmlschema.from_json(json_data, schema=self.schema, json_types=True)
        json_data2 = xmlschema.to_json(
            xmlschema.etree_tostring(root), schema=self.schema, json_types=True
        )
        self.assertEqual(json.loads(json_data), json.loads(json_data2))

    def test_from_json_typed_value_without_lexical(self):
        json_data = json.dumps({
            'decimal': {'$type': 'decimal', 'value': '100.5'},
            'integer': {'$type': 'integer', 'value': '123456789012345678901234567890'},
            'dateTime': {'$type': 'dateTime', 'value': '2019-01-01T13:40:00',
                         'timezone': None},
            'date': {'$type': 'date', 'value': '2001-04-15', 'timezone': '+05:30'},
        })
        root = xmlschema.from_json(json_data, schema=self.schema, json_types=True)
        self.assertEqual(root.findtext('decimal'), '100.5')
        self.assertEqual(root.findtext('dateTime'), '2019-01-01T13:40:00')
        self.assertEqual(root.findtext('date'), '2001-04-15+05:30')

    def test_from_json_malformed_typed_value(self):
        json_data = json.dumps({
            'decimal': {'$type': 'decimal', 'value': 'not-a-number'},
            'integer': '1',
            'dateTime': '2019-01-01T13:40:00',
            'date': '2001-04-15',
        })
        with self.assertRaises(JsonValueError):
            xmlschema.from_json(json_data, schema=self.schema, json_types=True)


if __name__ == '__main__':
    unittest.main()
