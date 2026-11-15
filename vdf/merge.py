"""
Tools for merging key/value lists from two sources into one.

The merge is driven by per-level key rules: every level of the nested
mapping gets its own key normalizer, so a parent level can be matched
case-insensitively while its children are matched case-sensitively.

The default rules model Steam library lists: library paths compare
case-insensitively (like Windows paths), while app identifiers compare
case-sensitively. Once parent keys are merged, their children are still
matched under their own level's rule.

Duplicate keys that appear within a single source are preserved in the
merged result; deduplication only applies across the two sources.
"""

from collections import OrderedDict

try:
    from collections.abc import Mapping
except ImportError:
    from collections import Mapping

from vdf.vdict import VDFDict

__all__ = ['merge_sources', 'MergeReport', 'DEFAULT_KEY_RULES']

_casefold = getattr(str, 'casefold', str.lower)

#: Default key rules: level 0 (library paths) is case-insensitive,
#: every level below it (app identifiers) is case-sensitive.
DEFAULT_KEY_RULES = (_casefold, None)

_MISSING = object()


class MergeReport(object):
    """
    Result of :func:`merge_sources`.

    ``merged``
        A ``VDFDict`` with the merged key/value pairs. Duplicate keys
        originating from a single source are preserved.
    ``spellings``
        ``dict`` mapping a key path tuple to the original spellings seen
        for that key, in order of first appearance (source A first).
    ``equivalents``
        List of ``(key path, [spellings])`` for keys found in both sources
        and unified under the level's key rule.
    ``conflicts``
        List of ``(key path, a value, b value)`` for keys whose values
        could not be reconciled. The merged result keeps source A's value.
    """

    def __init__(self):
        self.merged = VDFDict()
        self.spellings = OrderedDict()
        self.equivalents = []
        self.conflicts = []

    def __repr__(self):
        return ("%s(merged=%r, spellings=%r, equivalents=%r, conflicts=%r)"
                % (self.__class__.__name__, self.merged, self.spellings,
                   self.equivalents, self.conflicts))


def merge_sources(source_a, source_b, key_rules=None):
    """
    Merge two nested mappings of key/value pairs into one.

    ``source_a`` and ``source_b`` are mappings (or lists of key/value
    pairs). ``VDFDict`` instances keep their duplicate keys.

    ``key_rules`` is a sequence of key normalizers, one per nesting level.
    ``None`` means exact (case-sensitive) matching. When the data is deeper
    than the sequence, the last rule is reused. Defaults to
    ``DEFAULT_KEY_RULES``: case-insensitive library paths on level 0,
    case-sensitive app identifiers below.

    Returns a :class:`MergeReport` with the merged mapping, the original
    spellings, the cross-source equivalent keys, and the conflicts.
    """
    if key_rules is None:
        key_rules = DEFAULT_KEY_RULES
    if not key_rules:
        raise ValueError("Expected at least one key rule")

    report = MergeReport()
    report.merged = _merge_level(_pairs_of(source_a), _pairs_of(source_b),
                                 key_rules, 0, (), report)
    return report


def _pairs_of(data):
    if isinstance(data, Mapping):
        if hasattr(data, 'iteritems'):
            return list(data.iteritems())
        return list(data.items())
    return list(data)


def _merge_level(a_pairs, b_pairs, key_rules, level, path, report):
    normalize = key_rules[min(level, len(key_rules) - 1)]
    groups = OrderedDict()

    for side, pairs in (('a', a_pairs), ('b', b_pairs)):
        for key, value in pairs:
            norm = normalize(key) if normalize is not None else key
            group = groups.setdefault(norm, {'spellings': [], 'a': [], 'b': []})
            if key not in group['spellings']:
                group['spellings'].append(key)
            group[side].append(value)

    merged = VDFDict()

    for group in groups.values():
        spelling = group['spellings'][0]
        key_path = path + (spelling,)
        report.spellings[key_path] = list(group['spellings'])

        if group['a'] and group['b']:
            report.equivalents.append((key_path, list(group['spellings'])))

        a_vals = group['a']
        b_vals = group['b']

        for idx in range(max(len(a_vals), len(b_vals))):
            a_val = a_vals[idx] if idx < len(a_vals) else _MISSING
            b_val = b_vals[idx] if idx < len(b_vals) else _MISSING
            value = _reconcile(a_val, b_val, key_rules, level, key_path, report)
            if value is not _MISSING:
                merged[spelling] = value

    return merged


def _reconcile(a_val, b_val, key_rules, level, key_path, report):
    if b_val is _MISSING:
        return a_val
    if a_val is _MISSING:
        return b_val

    a_is_map = isinstance(a_val, Mapping)
    b_is_map = isinstance(b_val, Mapping)

    if a_is_map and b_is_map:
        return _merge_level(_pairs_of(a_val), _pairs_of(b_val),
                            key_rules, level + 1, key_path, report)

    if a_is_map != b_is_map or a_val != b_val:
        report.conflicts.append((key_path, a_val, b_val))

    return a_val
