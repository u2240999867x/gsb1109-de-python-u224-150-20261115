"""
Applying *daily* VDF updates: full manifests and partial patches.

A daily update is a mapping (typically parsed from a VDF document) that is
either a *full manifest* -- a complete snapshot replacing the current state --
or a *partial patch* -- a delta merged into the current state.

An update may carry a metadata section under :data:`META_KEY`::

    {
        '__update__': {
            'kind': 'patch',            # 'manifest'/'full' or 'patch'/'delta'
            'source': 'steamdb',        # origin of the update
            'version': 12,              # version of this update
            'replace': ['depots', 'config.launch'],   # replacement scope
            'delete': ['depots.temp', ('branches', (1, 'beta'))],  # deletions
        },
        # ... payload keys ...
    }

Semantics:

* updates are applied per ``source`` and matched by the key under which
  entries appear (their identity);
* a patch only touches its payload, its replacement scope and its deletion
  items -- anything else that is missing from the patch keeps its current
  value;
* deletion items name single entries; for duplicate keys only the specified
  occurrence is removed (a plain key removes the first occurrence, a
  ``(index, key)`` tuple removes that specific duplicate);
* a newer version that arrived first is never overwritten by a late,
  older patch -- updates older than the newest version already applied for
  their source are ignored;
* when neither side carries a comparable version, conflicting entries are
  kept on both sides (as duplicate keys in a :class:`VDFDict`).
"""

try:
    from collections.abc import Mapping
except ImportError:
    from collections import Mapping

from vdf.vdict import VDFDict

__all__ = [
    'META_KEY',
    'KIND_MANIFEST',
    'KIND_PATCH',
    'UpdateError',
    'DailyVDF',
    'apply_manifest',
    'apply_patch',
]

#: key under which an update carries its metadata
META_KEY = '__update__'

#: update kinds
KIND_MANIFEST = 'manifest'
KIND_PATCH = 'patch'

_MANIFEST_ALIASES = frozenset(['manifest', 'full', 'snapshot'])
_PATCH_ALIASES = frozenset(['patch', 'delta', 'partial'])

_MISSING = object()


class UpdateError(Exception):
    """Raised when a daily update document is malformed"""
    pass


def _parse_version(version):
    """
    Normalize ``version`` to a comparable tuple, or ``None`` when the
    version is missing or not numeric.
    """
    if version is None or isinstance(version, bool):
        return None
    if isinstance(version, int):
        return (version,)
    if isinstance(version, str):
        parts = version.strip().split('.')
        if parts and all(part.isdigit() for part in parts):
            return tuple(int(part) for part in parts)
    return None


def _compare_versions(a, b):
    """
    Compare two versions. Returns -1, 0 or 1, or ``None`` when the two
    versions are not comparable (either side missing or not numeric).
    """
    pa, pb = _parse_version(a), _parse_version(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


def _normalize_path(path):
    """
    Normalize a key path. Accepts a dotted string (``'a.b.c'``) or a
    tuple/list of path components. The last component may itself be a
    ``(index, key)`` tuple to address a specific duplicate of a
    :class:`VDFDict` key.
    """
    if isinstance(path, str):
        return tuple(part for part in path.split('.') if part != '')
    if isinstance(path, (list, tuple)):
        return tuple(path)
    raise UpdateError("Invalid path: %r" % (path,))


def _mapper_for(mapping):
    return mapping.__class__ if isinstance(mapping, Mapping) else VDFDict


def _convert(value, mapper):
    """
    Deep-copy ``value`` rebuilding every nested mapping as ``mapper``.
    Duplicate keys of a :class:`VDFDict` are preserved.
    """
    if isinstance(value, Mapping):
        result = mapper()
        for key, item in value.items():
            result[key] = _convert(item, mapper)
        return result
    return value


def _lookup(mapping, parts):
    node = mapping
    for part in parts:
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set_replacing(mapping, key, value):
    """
    Set ``key`` to ``value`` replacing any existing entries for that key,
    including duplicates.
    """
    if isinstance(mapping, VDFDict) and isinstance(key, str):
        if key in mapping:
            mapping.remove_all_for(key)
        mapping[key] = value
    else:
        try:
            del mapping[key]
        except (KeyError, TypeError):
            pass
        mapping[key] = value


def _delete_path(data, path):
    """
    Delete the single entry named by ``path``. Missing targets are ignored.
    Only the specified occurrence of a duplicated key is removed.
    """
    parts = _normalize_path(path)
    if not parts:
        return

    node = _lookup(data, parts[:-1]) if len(parts) > 1 else data
    if node is _MISSING or not isinstance(node, Mapping):
        return

    try:
        del node[parts[-1]]
    except (KeyError, TypeError):
        pass


def _merge(base, payload, prefer_incoming, replace_paths, _prefix=()):
    """
    Deep-merge ``payload`` into ``base``. Subtrees listed in
    ``replace_paths`` are skipped (already swapped wholesale). On conflicting
    leaf values the incoming value wins when ``prefer_incoming`` is true,
    otherwise both sides are kept (duplicate keys in a :class:`VDFDict`).
    """
    mapper = _mapper_for(base)

    for key, value in payload.items():
        path = _prefix + (key,)

        if path in replace_paths:
            continue

        if key not in base:
            base[key] = _convert(value, mapper)
            continue

        existing = base[key]

        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            _merge(existing, value, prefer_incoming, replace_paths, path)
        elif existing != value:
            if prefer_incoming:
                _set_replacing(base, key, _convert(value, mapper))
            elif isinstance(base, VDFDict):
                # conflict without comparable versions: keep both sides
                base[key] = _convert(value, mapper)
            # plain mappings cannot hold both sides, keep the existing value


def apply_manifest(payload, mapper=VDFDict):
    """
    Build a fresh state from a full manifest ``payload``. The manifest is a
    complete listing, so the result contains exactly its contents.

    ``mapper`` specifies the mapping type of the result, ``VDFDict`` by
    default.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("Expected payload to be a Mapping, got %s" % type(payload))
    return _convert(payload, mapper)


def apply_patch(base, payload, replace=(), delete=(), prefer_incoming=True):
    """
    Merge a partial patch into ``base`` (modified in place) and return it.

    ``replace`` is the replacement scope: an iterable of key paths whose
    subtrees are replaced wholesale by the patch's content.

    ``delete`` is an iterable of key paths naming single entries to remove.
    For duplicate keys only the specified occurrence is deleted; use a
    ``(index, key)`` tuple as the last path component to pick a specific
    duplicate.

    Keys outside the replacement scope that are missing from the patch keep
    their current values.

    ``prefer_incoming`` controls conflict resolution: when true the patch
    wins conflicting entries, otherwise both sides are kept (requires
    ``base`` to be a :class:`VDFDict`).
    """
    if not isinstance(base, Mapping):
        raise TypeError("Expected base to be a Mapping, got %s" % type(base))
    if not isinstance(payload, Mapping):
        raise TypeError("Expected payload to be a Mapping, got %s" % type(payload))

    replace_paths = set()
    for path in replace:
        parts = _normalize_path(path)
        if parts:
            replace_paths.add(parts)

    # explicit deletions apply to the state the patch was written against
    for path in delete:
        _delete_path(base, path)

    # swap the replacement scope wholesale
    for parts in replace_paths:
        value = _lookup(payload, parts)
        if value is _MISSING:
            continue
        parent = _lookup(base, parts[:-1]) if len(parts) > 1 else base
        if not isinstance(parent, Mapping):
            continue
        _set_replacing(parent, parts[-1], _convert(value, _mapper_for(parent)))

    _merge(base, payload, prefer_incoming, replace_paths)

    return base


class DailyVDF(object):
    """
    Accumulates daily VDF updates into a single state.

    Updates are applied in arrival order via :meth:`apply`. Bookkeeping of
    the newest applied version is kept per source, so a newer version that
    arrived first is never overwritten by a late, older patch from the same
    source. Updates whose version is not comparable to the last applied one
    are merged with conflicts kept on both sides.
    """

    def __init__(self, mapper=VDFDict):
        self.mapper = mapper
        self.data = mapper()
        self.versions = {}

    def apply(self, update):
        """
        Apply a single daily update (manifest or patch) and return the
        current state.
        """
        if not isinstance(update, Mapping):
            raise TypeError("Expected update to be a Mapping, got %s" % type(update))

        meta = update.get(META_KEY) or {}
        if not isinstance(meta, Mapping):
            raise UpdateError("Expected %s to be a Mapping, got %s" % (META_KEY, type(meta)))

        source = meta.get('source', '')
        version = meta.get('version')
        kind = str(meta.get('kind', KIND_PATCH)).lower()

        cmp = _compare_versions(version, self.versions.get(source))
        if cmp is not None and cmp < 0:
            # late, older update: a newer version is already in place
            return self.data

        payload = _convert(update, self.mapper)
        if META_KEY in payload:
            del payload[META_KEY]

        if kind in _MANIFEST_ALIASES:
            self.data = apply_manifest(payload, mapper=self.mapper)
        elif kind in _PATCH_ALIASES:
            apply_patch(self.data, payload,
                        replace=meta.get('replace', ()),
                        delete=meta.get('delete', ()),
                        prefer_incoming=cmp is not None)
        else:
            raise UpdateError("Unknown update kind: %r" % meta.get('kind'))

        if _parse_version(version) is not None:
            self.versions[source] = version

        return self.data

    def apply_all(self, updates):
        """
        Apply a sequence of daily updates in arrival order and return the
        current state.
        """
        for update in updates:
            self.apply(update)
        return self.data
