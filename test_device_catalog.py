#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import unittest

from device_catalog import DeviceCatalog, parse_oid, format_oid


class ParseOidTestCase(unittest.TestCase):
    def test_long_arc_not_truncated(self):
        big = 123456789012345678901234567890123456789  # > 2**128
        arcs = parse_oid('2.999.%d' % big)
        assert arcs == (2, 999, big)
        assert format_oid(arcs) == '2.999.%d' % big

    def test_invalid_first_arc(self):
        try:
            parse_oid('3.1')
        except ValueError:
            pass
        else:
            assert 0, 'first arc 3 tolerated'

    def test_invalid_second_arc(self):
        try:
            parse_oid('1.40')
        except ValueError:
            pass
        else:
            assert 0, 'second arc 40 under first arc 1 tolerated'

    def test_malformed_oid(self):
        for bad in ('', '1', '1..2', '1.2.x', '1.2.-3', 12):
            try:
                parse_oid(bad)
            except ValueError:
                pass
            else:
                assert 0, 'malformed OID %r tolerated' % (bad,)


class InheritanceTestCase(unittest.TestCase):
    def setUp(self):
        self.catalog = DeviceCatalog()
        self.catalog.add_rule('1.3.6.1.4.1', unit='count', displayName='root')
        self.catalog.add_rule('1.3.6.1.4.1.99999', unit='celsius')
        self.catalog.add_rule('1.3.6.1.4.1.88888', unit='volt')

    def test_deepest_rule_wins_per_attribute(self):
        resolved = self.catalog.resolve('1.3.6.1.4.1.99999.1.7')
        # unit overridden by the deeper rule ...
        assert resolved['unit'] == {'value': 'celsius', 'source': '1.3.6.1.4.1.99999'}
        # ... displayName still inherited from the parent rule
        assert resolved['displayName'] == {'value': 'root', 'source': '1.3.6.1.4.1'}

    def test_prefix_is_arc_wise_not_string_wise(self):
        catalog = DeviceCatalog()
        catalog.add_rule('1.2.3', unit='ampere')
        # 1.2.3 must not cover 1.2.30 (string prefix would match)
        assert catalog.resolve('1.2.30') == {}
        assert catalog.resolve('1.2.30.1') == {}
        assert catalog.resolve('1.2.3.1')['unit']['value'] == 'ampere'

    def test_long_arc_rule_and_item(self):
        big = '123456789012345678901234567890123456789'
        catalog = DeviceCatalog()
        catalog.add_rule('2.999.' + big, unit='hertz')
        resolved = catalog.resolve('2.999.' + big + '.7')
        assert resolved['unit'] == {'value': 'hertz', 'source': '2.999.' + big}
        # a truncated look-alike must not match
        assert catalog.resolve('2.999.12345678901234567890.7') == {}

    def test_remove_restores_parent_rule(self):
        item = '1.3.6.1.4.1.99999.1.7'
        assert self.catalog.resolve(item)['unit']['value'] == 'celsius'
        assert self.catalog.remove_rule('1.3.6.1.4.1.99999') is True
        resolved = self.catalog.resolve(item)
        # override gone: parent rule supplies unit again
        assert resolved['unit'] == {'value': 'count', 'source': '1.3.6.1.4.1'}
        assert resolved['displayName'] == {'value': 'root', 'source': '1.3.6.1.4.1'}

    def test_remove_does_not_touch_sibling_subtree(self):
        self.catalog.remove_rule('1.3.6.1.4.1.99999')
        sibling = self.catalog.resolve('1.3.6.1.4.1.88888.4')
        assert sibling['unit'] == {'value': 'volt', 'source': '1.3.6.1.4.1.88888'}

    def test_remove_missing_rule(self):
        assert self.catalog.remove_rule('1.3.6.1.4.1.55555') is False

    def test_remove_keeps_deeper_rules(self):
        self.catalog.add_rule('1.3.6.1.4.1.99999.1', displayName='leaf')
        self.catalog.remove_rule('1.3.6.1.4.1.99999')
        resolved = self.catalog.resolve('1.3.6.1.4.1.99999.1.2')
        assert resolved['displayName'] == {'value': 'leaf', 'source': '1.3.6.1.4.1.99999.1'}
        assert resolved['unit'] == {'value': 'count', 'source': '1.3.6.1.4.1'}

    def test_no_matching_rule(self):
        assert self.catalog.resolve('2.1.2.3') == {}

    def test_resolve_batch(self):
        results = self.catalog.resolve_batch(['1.3.6.1.4.1.99999.1', '2.1.2.3'])
        assert [r['oid'] for r in results] == ['1.3.6.1.4.1.99999.1', '2.1.2.3']
        assert results[0]['attributes']['unit']['value'] == 'celsius'
        assert results[1]['attributes'] == {}


if __name__ == '__main__':
    unittest.main()
