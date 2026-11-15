#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
"""Device catalog with OID-prefix attribute inheritance.

Rules attach partial attributes (e.g. ``unit``, ``displayName``) to an OID
prefix. A sampling item inherits each attribute from the *deepest* rule
whose prefix matches it arc-by-arc, so a subtree may override a single
attribute while inheriting the rest. Removing a rule makes its subtree
fall back to the parent rule again, without touching sibling subtrees.

OIDs are matched on integer arcs (via tuples of arbitrary-precision Python
ints), never on strings: rule ``1.2.3`` does not match item ``1.2.30``,
and arc numbers larger than 64/128 bits are kept exactly.

CLI:

    python device_catalog.py RULES_JSON ITEMS_JSON [--remove OID]... [--json]

RULES_JSON:  [{"oid": "1.3.6.1.4.1", "unit": "C", "displayName": "..."}]
ITEMS_JSON:  ["1.3.6.1.4.1.1", ...] or [{"oid": "..."}, ...]
"""

import argparse
import json
import re
import sys

from pyasn1.type import univ

_OID_RE = re.compile(r'\d+(\.\d+)+$')


def parse_oid(text):
    """Parse a dotted OID string into a tuple of integer arcs.

    Arcs are arbitrary-precision ints: long arc numbers are never
    truncated. Raises :class:`ValueError` on malformed input.
    """
    if not isinstance(text, str):
        raise ValueError('OID must be a dotted string, got %r' % (text,))
    text = text.strip()
    if not _OID_RE.match(text):
        raise ValueError('malformed OID %r' % (text,))
    try:
        arcs = tuple(univ.ObjectIdentifier(text))
    except Exception as exc:
        raise ValueError('malformed OID %r: %s' % (text, exc))
    if len(arcs) < 2:
        raise ValueError('OID %r needs at least two arcs' % (text,))
    if arcs[0] not in (0, 1, 2):
        raise ValueError('OID %r: first arc must be 0, 1 or 2' % (text,))
    if arcs[0] < 2 and arcs[1] > 39:
        raise ValueError('OID %r: second arc must be 0..39 when first arc < 2' % (text,))
    return arcs


def format_oid(arcs):
    return '.'.join(str(arc) for arc in arcs)


class _Node(object):
    __slots__ = ('children', 'attrs')

    def __init__(self):
        self.children = {}
        self.attrs = {}


class DeviceCatalog(object):
    """OID-prefix rule trie with per-attribute inheritance."""

    def __init__(self):
        self._root = _Node()

    def add_rule(self, prefix, **attrs):
        """Attach attributes to an OID prefix, merging per attribute."""
        node = self._root
        for arc in parse_oid(prefix) if isinstance(prefix, str) else tuple(prefix):
            node = node.children.setdefault(arc, _Node())
        node.attrs.update(attrs)
        return self

    def remove_rule(self, prefix):
        """Delete the rule at *prefix*; descendants then inherit from the
        nearest surviving ancestor. Sibling subtrees are untouched.
        Returns True if a rule existed there."""
        arcs = parse_oid(prefix) if isinstance(prefix, str) else tuple(prefix)
        chain = [self._root]
        for arc in arcs:
            nxt = chain[-1].children.get(arc)
            if nxt is None:
                return False
            chain.append(nxt)
        existed = bool(chain[-1].attrs)
        chain[-1].attrs.clear()
        # prune empty nodes from the leaf up, keeping nodes that still
        # carry attributes or children
        for depth in range(len(chain) - 1, 0, -1):
            child = chain[depth]
            if child.children or child.attrs:
                break
            del chain[depth - 1].children[arcs[depth - 1]]
        return existed

    def resolve(self, oid):
        """Resolve effective attributes for one OID.

        Returns ``{attr: {'value': v, 'source': prefix-or-None}}`` where
        *source* is the dotted prefix of the rule that supplied the value,
        or None if no rule defines that attribute.
        """
        arcs = parse_oid(oid) if isinstance(oid, str) else tuple(oid)
        effective = {}
        node = self._root
        matched = []
        for arc in arcs:
            node = node.children.get(arc)
            if node is None:
                break
            matched.append(arc)
            if node.attrs:
                source = format_oid(matched)
                for name, value in node.attrs.items():
                    effective[name] = {'value': value, 'source': source}
        return effective

    def resolve_batch(self, oids):
        """Resolve many OIDs; returns a list of result dicts."""
        results = []
        for oid in oids:
            text = oid if isinstance(oid, str) else format_oid(oid)
            results.append({'oid': text, 'attributes': self.resolve(oid)})
        return results

    def rules(self):
        """Yield ``(prefix, attrs)`` for every rule, shallowest first."""
        stack = [((), self._root)]
        while stack:
            prefix, node = stack.pop(0)
            if node.attrs:
                yield format_oid(prefix), dict(node.attrs)
            for arc in sorted(node.children):
                stack.append((prefix + (arc,), node.children[arc]))


def _load_rules(path):
    with open(path) as handle:
        entries = json.load(handle)
    rules = []
    for entry in entries:
        entry = dict(entry)
        oid = entry.pop('oid')
        rules.append((oid, entry))
    return rules


def _load_items(path):
    with open(path) as handle:
        entries = json.load(handle)
    items = []
    for entry in entries:
        items.append(entry['oid'] if isinstance(entry, dict) else entry)
    return items


def _render_table(results):
    lines = []
    for result in results:
        lines.append(result['oid'])
        attrs = result['attributes']
        if not attrs:
            lines.append('    (no matching rule)')
        for name in sorted(attrs):
            resolved = attrs[name]
            source = resolved['source'] or '(no source)'
            lines.append('    %-12s = %-24s <- rule %s' % (
                name, resolved['value'], source))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Resolve device-catalog attributes by OID-prefix inheritance')
    parser.add_argument('rules', help='JSON file with rule entries')
    parser.add_argument('items', help='JSON file with sampling item OIDs')
    parser.add_argument('--remove', metavar='OID', action='append', default=[],
                        help='delete the rule at OID before resolving (repeatable)')
    parser.add_argument('--json', action='store_true',
                        help='emit JSON instead of a table')
    args = parser.parse_args(argv)

    catalog = DeviceCatalog()
    for prefix, attrs in _load_rules(args.rules):
        catalog.add_rule(prefix, **attrs)

    for prefix in args.remove:
        if not catalog.remove_rule(prefix):
            print('warning: no rule at %s to remove' % prefix, file=sys.stderr)

    results = catalog.resolve_batch(_load_items(args.items))

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(_render_table(results))
    return 0


if __name__ == '__main__':
    sys.exit(main())
