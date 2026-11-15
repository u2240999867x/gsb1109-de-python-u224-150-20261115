"""
Check whether two VDF patches against the same base document commute.

A patch is a list of operations. Each operation addresses nodes by
*occurrence identity*: a path element is a (occurrence_index, key) tuple,
matching how VDFDict stores duplicate keys internally.

Operations:
    ('set',     path, value)    set value at path
    ('del',     path)           delete node at path (later duplicates re-index)
    ('replace', path, subtree)  replace the whole subtree at path
    ('reorder', path, order)    rebuild node at path with keys in `order`
                                (order = list of (occurrence_index, key))

Two patches commute iff applying them in both orders succeeds and yields
equal trees. VDFDict.__eq__ compares item lists in order, so equality
means "same business tree AND same duplicate order".
"""
import copy
import traceback

import vdf
from vdf.vdict import VDFDict

BASE = '''
"root"
{
    "item"  "alpha"
    "item"  "beta"
    "config"
    {
        "mode"  "1"
        "level" "3"
    }
}
'''


def load_base():
    # Duplicates only survive with VDFDict + merge_duplicate_keys=False.
    # With the default dict mapper they are silently overwritten (last wins).
    return vdf.loads(BASE, mapper=VDFDict, merge_duplicate_keys=False)


def clone(node):
    # copy.deepcopy does NOT work on VDFDict: reconstruction replays
    # (index, key) tuple keys through __setitem__, which rejects tuple
    # keys that do not exist yet. Rebuild occurrence by occurrence instead.
    if isinstance(node, VDFDict):
        new = VDFDict()
        for key, value in node.items():
            new[key] = clone(value)
        return new
    return copy.deepcopy(node)


def navigate(tree, path):
    node = tree
    for occ, key in path:
        node = node[(occ, key)]
    return node


def apply_op(tree, op):
    kind = op[0]
    if kind == 'set':
        _, path, value = op
        navigate(tree, path[:-1])[path[-1]] = value
    elif kind == 'del':
        _, path = op
        parent = navigate(tree, path[:-1])
        del parent[path[-1]]
    elif kind == 'replace':
        _, path, subtree = op
        navigate(tree, path[:-1])[path[-1]] = clone(subtree)
    elif kind == 'reorder':
        _, path, order = op
        node = navigate(tree, path)
        pairs = [(key, node[key]) for key in order]
        rebuilt = VDFDict()
        for (_, key_name), value in pairs:
            rebuilt[key_name] = value  # str key appends, preserving order
        navigate(tree, path[:-1])[path[-1]] = rebuilt
    else:
        raise ValueError('unknown op %r' % (kind,))


def apply_patch(tree, patch):
    for op in patch:
        apply_op(tree, op)
    return tree


def run_scenario(name, patch_a, patch_b):
    print('=== %s ===' % name)
    results = {}
    for label, seq in (('A->B', [patch_a, patch_b]), ('B->A', [patch_b, patch_a])):
        tree = load_base()
        try:
            for patch in seq:
                apply_patch(tree, patch)
            results[label] = tree
            print('  %s OK:     %s' % (label, list(tree['root'].items())))
        except Exception as exc:
            results[label] = exc
            print('  %s FAILED: %s: %s' % (label, type(exc).__name__, exc))

    a, b = results['A->B'], results['B->A']
    if isinstance(a, Exception) or isinstance(b, Exception):
        print('  => CONFLICT: combination invalid in at least one order\n')
    elif a == b:
        print('  => COMMUTES: identical tree and duplicate order\n')
    else:
        print('  => CONFLICT: both orders valid but results differ\n')


ROOT = (0, 'root')

# S1: disjoint leaf edits -- no shared path, no reorder
run_scenario(
    'S1 disjoint edits (item[0] vs config/mode)',
    [('set', [ROOT, (0, 'item')], 'A')],
    [('set', [ROOT, (0, 'config'), (0, 'mode')], '2')],
)

# S2: occurrence identity -- delete re-indexes duplicates, set targets by index
run_scenario(
    'S2 occurrence identity (set item[0] vs del item[0])',
    [('set', [ROOT, (0, 'item')], 'A')],
    [('del', [ROOT, (0, 'item')])],
)

# S3: ancestor replacement -- one patch replaces the subtree the other edits inside
run_scenario(
    'S3 ancestor replacement (replace config vs set config/mode)',
    [('replace', [ROOT, (0, 'config')], VDFDict([('mode', '9')]))],
    [('set', [ROOT, (0, 'config'), (0, 'mode')], '2')],
)

# S4: key reordering -- reorder swaps duplicate order, set addresses by index
run_scenario(
    'S4 key reordering (swap item duplicates vs set item[0])',
    [('reorder', [ROOT], [(0, 'config'), (1, 'item'), (0, 'item')])],
    [('set', [ROOT, (0, 'item')], 'A')],
)

# S5: individually valid, combination invalid -- delete ancestor vs edit inside
run_scenario(
    'S5 delete ancestor vs edit inside (del config vs set config/mode)',
    [('del', [ROOT, (0, 'config')])],
    [('set', [ROOT, (0, 'config'), (0, 'mode')], '2')],
)
