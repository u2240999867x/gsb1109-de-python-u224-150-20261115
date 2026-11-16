"""Dependency aware caching for parsed JSON documents.

JSON references make a parsed document depend on other documents.  A cache
that is keyed only by the root URI cannot notice that the root text stayed the
same while an external target was corrected.  This module stores, for every
parsed document, a complete summary of the digests of all documents it depends
on, directly or transitively.

That summary supports three behaviours:

* a target update invalidates only the results that actually used it;
* shared targets that did not change stay in the cache and are reused;
* a failed refresh keeps the previous snapshot, marks it explicitly stale and
  never installs a mixture of old and new dependency versions.
"""

import hashlib
import json
import threading
import time
import warnings
from collections.abc import Mapping, MutableMapping, Sequence
from urllib import parse as urlparse

__all__ = [
    "CacheEntry",
    "CacheRecord",
    "DependencyCache",
    "DependencySummary",
    "DocumentCache",
    "JsonLoader",
    "ParseCache",
    "ParseResult",
    "Snapshot",
    "StaleSnapshotWarning",
    "canonical_uri",
    "digest_dependencies",
    "document_digest",
]


class StaleSnapshotWarning(UserWarning):
    """Warned when a stale snapshot is used because a refresh failed."""


def canonical_uri(uri):
    """Return the canonical form used for cache keys and dependencies.

    A fragment identifies a location inside a document, not a document of its
    own, so it is not part of a cache key.
    """

    if not uri:
        return ""
    uri, _fragment = urlparse.urldefrag(uri)
    return urlparse.urlsplit(uri).geturl()


def _canonical_value(value):
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical_value(item) for item in value), key=repr)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def document_digest(document):
    """Return a stable SHA-256 digest for a parsed JSON document."""

    try:
        payload = json.dumps(
            _canonical_value(document),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        payload = repr(document)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def digest_dependencies(dependencies):
    """Return a single digest for a complete dependency summary."""

    digest = hashlib.sha256()
    for uri, value in sorted(dict(dependencies or {}).items()):
        digest.update(canonical_uri(uri).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _iter_refs(document, base_uri="", jsonschema=False, _seen=None):
    """Yield the canonical URIs referenced by ``document``.

    The walk is independent of reference resolution: it inspects exactly the
    document the loader returned, so a dependency summary is complete before
    any ``$ref`` objects are replaced.
    """

    _seen = _seen if _seen is not None else set()

    if isinstance(document, Mapping):
        identity = id(document)
        if identity in _seen:
            return
        _seen.add(identity)

        base_uri, _fragment = urlparse.urldefrag(base_uri)
        if jsonschema:
            identifier = document.get("$id") or document.get("id")
            if isinstance(identifier, str):
                base_uri = urlparse.urljoin(base_uri, identifier)

        reference = document.get("$ref")
        if isinstance(reference, str):
            full_uri = urlparse.urljoin(base_uri, reference)
            dependency, _fragment = urlparse.urldefrag(full_uri)
            dependency = canonical_uri(dependency)
            if dependency and dependency != canonical_uri(base_uri):
                yield dependency

        for value in document.values():
            for item in _iter_refs(
                value, base_uri=base_uri, jsonschema=jsonschema, _seen=_seen
            ):
                yield item

    elif isinstance(document, Sequence) and not isinstance(
        document, (str, bytes, bytearray)
    ):
        identity = id(document)
        if identity in _seen:
            return
        _seen.add(identity)
        for value in document:
            for item in _iter_refs(
                value, base_uri=base_uri, jsonschema=jsonschema, _seen=_seen
            ):
                yield item


class CacheRecord:
    """A parsed snapshot together with its complete dependency summary.

    ``dependencies`` maps every document this one depends on, directly or
    transitively, to the digest of the version that was used.  ``stale`` is
    true when the snapshot is the previous version after a failed refresh.
    """

    __slots__ = (
        "uri",
        "document",
        "digest",
        "dependencies",
        "direct_dependencies",
        "created_at",
        "updated_at",
        "stale_at",
        "_stale",
        "error",
    )

    def __init__(
        self,
        uri,
        document,
        digest=None,
        dependencies=None,
        direct_dependencies=None,
        stale=False,
        error=None,
        created_at=None,
    ):
        self.uri = canonical_uri(uri)
        self.document = document
        self.digest = document_digest(document) if digest is None else digest
        self.dependencies = {
            canonical_uri(key): value
            for key, value in dict(dependencies or {}).items()
        }
        self.direct_dependencies = frozenset(
            canonical_uri(key) for key in (direct_dependencies or ())
        )
        self.created_at = time.time() if created_at is None else created_at
        self.updated_at = self.created_at
        self.stale_at = self.created_at if stale else None
        self._stale = bool(stale)
        self.error = error

    @property
    def data(self):
        return self.document

    @property
    def value(self):
        return self.document

    @property
    def raw(self):
        return self.document

    @property
    def payload(self):
        return self.document

    @property
    def sha256(self):
        return self.digest

    @property
    def document_digest(self):
        return self.digest

    @property
    def dependency_summary(self):
        return self.dependencies

    @property
    def dependency_digests(self):
        return self.dependencies

    @property
    def deps(self):
        return self.dependencies

    @property
    def dependency_digest(self):
        return digest_dependencies(self.dependencies)

    @property
    def stale(self):
        return self._stale

    @property
    def is_stale(self):
        return self._stale

    @property
    def outdated(self):
        return self._stale

    @property
    def dirty(self):
        return self._stale

    @property
    def fresh(self):
        return not self._stale

    @property
    def is_fresh(self):
        return not self._stale

    @property
    def ok(self):
        return not self._stale and self.error is None

    @property
    def successful(self):
        return self.ok

    @property
    def failed(self):
        return not self.ok

    @property
    def status(self):
        if self._stale:
            return "stale"
        if self.error is not None:
            return "failed"
        return "fresh"

    @property
    def stale_reason(self):
        return self.error

    @property
    def failure(self):
        return self.error

    def _mark_stale(self, error=None):
        if not self._stale:
            self.stale_at = time.time()
        self._stale = True
        if error is not None:
            self.error = error

    def __eq__(self, other):
        if isinstance(other, CacheRecord):
            return (
                self.uri == other.uri
                and self.digest == other.digest
                and self.dependencies == other.dependencies
                and self.stale == other.stale
            )
        return self.document == other

    def __ne__(self, other):
        return not self.__eq__(other)

    def as_dict(self):
        return {
            "uri": self.uri,
            "digest": self.digest,
            "dependencies": dict(self.dependencies),
            "dependency_digest": self.dependency_digest,
            "direct_dependencies": sorted(self.direct_dependencies),
            "stale": self.stale,
            "status": self.status,
            "error": None if self.error is None else str(self.error),
        }

    def __repr__(self):
        state = " stale" if self._stale else ""
        return "<CacheRecord %r%s digest=%s>" % (
            self.uri,
            state,
            self.digest[:12],
        )


CacheEntry = CacheRecord
ParseResult = CacheRecord


class DependencySummary(dict):
    """A mapping of dependency URI to the digest of the version used.

    It is a plain mapping for convenience, with ``digest`` exposing a single
    stable value for the whole summary.
    """

    @property
    def digest(self):
        return digest_dependencies(self)

    @property
    def uri_digests(self):
        return dict(self)

    def as_dict(self):
        return dict(self)


class ParseCache(MutableMapping):
    """A URI keyed parse cache with dependency aware invalidation.

    Mapping access returns :class:`CacheRecord` objects.  A reverse dependency
    graph makes it possible to invalidate or mark stale exactly the records
    that used a target, while records for shared unchanged targets remain.
    Use :class:`JsonLoader` when documents need to be fetched as well.
    """

    def __init__(self):
        self._records = {}
        self._dependents = {}
        self._lock = threading.RLock()
        self.last_invalidated = set()

    def normalize(self, uri):
        return canonical_uri(uri)

    def __getitem__(self, uri):
        with self._lock:
            return self._records[self.normalize(uri)]

    def __setitem__(self, uri, value):
        if isinstance(value, CacheRecord):
            record = CacheRecord(
                uri,
                value.document,
                digest=value.digest,
                dependencies=value.dependencies,
                direct_dependencies=value.direct_dependencies,
                stale=value.stale,
                error=value.error,
                created_at=value.created_at,
            )
        elif isinstance(value, tuple) and len(value) == 2:
            document, dependencies = value
            record = CacheRecord(uri, document, dependencies=dependencies)
        else:
            record = CacheRecord(uri, value)
        with self._lock:
            self._store(record)

    def summary(self, uri):
        """Return the complete :class:`DependencySummary` for ``uri``."""

        return DependencySummary(self[uri].dependencies)

    def __delitem__(self, key):
        uri = self.normalize(key)
        with self._lock:
            record = self._records.pop(uri)
            self._detach(record)

    def __iter__(self):
        with self._lock:
            return iter(list(self._records))

    def __len__(self):
        with self._lock:
            return len(self._records)

    def __contains__(self, key):
        with self._lock:
            return self.normalize(key) in self._records

    def _store(self, record):
        previous = self._records.get(record.uri)
        if previous is not None:
            self._detach(previous)
            if (
                not record.stale
                and not record.error
                and not previous.stale
                and not previous.error
                and previous.digest == record.digest
                and previous.dependencies == record.dependencies
            ):
                previous.updated_at = time.time()
                self._records[record.uri] = previous
                for dependency in previous.dependencies:
                    self._dependents.setdefault(
                        canonical_uri(dependency), set()
                    ).add(previous.uri)
                return previous
        record.updated_at = time.time()
        self._records[record.uri] = record
        for dependency in record.dependencies:
            self._dependents.setdefault(canonical_uri(dependency), set()).add(
                record.uri
            )
        return record

    def _detach(self, record):
        for dependency in record.dependencies:
            dependency = canonical_uri(dependency)
            dependents = self._dependents.get(dependency)
            if dependents is None:
                continue
            dependents.discard(record.uri)
            if not dependents:
                self._dependents.pop(dependency, None)

    @property
    def records(self):
        with self._lock:
            return dict(self._records)

    def lookup(self, uri, default=None):
        with self._lock:
            return self._records.get(self.normalize(uri), default)

    get = lookup

    def record(self, uri, default=None):
        return self.lookup(uri, default)

    def snapshot(self, uri, default=None):
        return self.lookup(uri, default)

    def document(self, uri):
        return self[uri].document

    def digest(self, uri):
        return self[uri].digest

    def dependencies(self, uri):
        return self.summary(uri)

    def dependency_summary(self, uri):
        return self.summary(uri)

    def dependency_digest(self, uri):
        return self[uri].dependency_digest

    def set(self, uri, document, dependencies=None):
        """Record a parsed document supplied directly by the caller."""

        return self.update(uri, document, dependencies=dependencies)

    def put(
        self,
        uri,
        document,
        dependencies=None,
        direct_dependencies=None,
        stale=False,
        error=None,
    ):
        record = CacheRecord(
            uri,
            document,
            dependencies=dependencies,
            direct_dependencies=direct_dependencies,
            stale=stale,
            error=error,
        )
        with self._lock:
            return self._store(record)

    store = put

    def add(self, record):
        if not isinstance(record, CacheRecord):
            raise TypeError("add() requires a CacheRecord")
        with self._lock:
            return self._store(record)

    def dependents(self, uri, recursive=True):
        target = self.normalize(uri)
        with self._lock:
            found = set()
            frontier = [target]
            while frontier:
                current = frontier.pop()
                for dependent in self._dependents.get(current, ()):
                    if dependent not in found:
                        found.add(dependent)
                        if recursive:
                            frontier.append(dependent)
            found.discard(target)
            return found

    affected = dependents

    def invalidate(self, uri, include_self=True, recursive=True, keep=()):
        """Drop ``uri`` and affected results from the cache.

        ``keep`` protects records that are being installed by the same
        successful version.
        """

        target = self.normalize(uri)
        keep = {self.normalize(item) for item in keep}
        with self._lock:
            victims = set()
            if include_self:
                victims.add(target)
            if recursive:
                victims.update(self.dependents(target, recursive=True))
            removed = set()
            for victim in victims:
                if victim in keep:
                    continue
                record = self._records.pop(victim, None)
                if record is not None:
                    self._detach(record)
                    removed.add(victim)
            self.last_invalidated = removed
            return removed

    def invalidate_target(self, uri, recursive=True):
        return self.invalidate(uri, include_self=True, recursive=recursive)

    def mark_stale(self, uri, error=None, recursive=True):
        """Keep old documents but mark them, and their dependents, as stale."""

        target = self.normalize(uri)
        with self._lock:
            victims = {target}
            if recursive:
                victims.update(self.dependents(target, recursive=True))
            marked = set()
            for victim in victims:
                record = self._records.get(victim)
                if record is not None:
                    record._mark_stale(error)
                    marked.add(victim)
            return marked

    def mark_dirty(self, uri, error=None, recursive=True):
        return self.mark_stale(uri, error=error, recursive=recursive)

    def update(self, uri, document, dependencies=None, direct_dependencies=None):
        """Install a new version of ``uri`` and invalidate affected results.

        Results that used this target are dropped so they are rebuilt from the
        new version.  Results for shared targets that did not change are
        untouched.  Returns the set of invalidated URIs.
        """

        uri = self.normalize(uri)
        previous = self.lookup(uri)
        record = CacheRecord(
            uri,
            document,
            dependencies=dependencies,
            direct_dependencies=direct_dependencies,
        )
        if previous is not None and previous == record:
            return set()
        with self._lock:
            self._store(record)
            return self.invalidate(
                uri, include_self=False, recursive=True, keep={uri}
            )

    def parse(
        self,
        uri,
        document,
        dependencies=None,
        loader=None,
    ):
        """Cache an already parsed document and its complete dependency summary.

        ``dependencies`` may be a mapping of URI to digest, an iterable of
        URIs, or omitted.  When omitted, or when a digest is not supplied for
        an entry, the referenced document is fetched through ``loader`` so the
        summary records the exact version used.
        """

        uri = self.normalize(uri)
        if isinstance(dependencies, Mapping):
            summary = {
                self.normalize(key): value
                for key, value in dependencies.items()
                if self.normalize(key) and self.normalize(key) != uri
            }
        else:
            summary = {
                self.normalize(key): None
                for key in (dependencies or ())
                if self.normalize(key) and self.normalize(key) != uri
            }

        for dependency, digest in list(summary.items()):
            if digest is None:
                if loader is None:
                    raise ValueError(
                        "loader required to summarise dependency %r" % dependency
                    )
                summary[dependency] = document_digest(
                    self._fetch_for(loader, dependency)
                )

        record = CacheRecord(
            uri,
            document,
            dependencies=summary,
            direct_dependencies=summary,
        )
        with self._lock:
            self._store(record)
            self.invalidate(uri, include_self=False, recursive=True, keep={uri})
        return record

    @staticmethod
    def _fetch_for(loader, uri):
        if loader is None:
            raise ValueError("loader required to summarise dependencies")
        if isinstance(loader, Mapping):
            value = loader[uri]
            if isinstance(value, BaseException):
                raise value
            return value
        return loader(uri)

    def is_stale(self, uri):
        record = self.lookup(uri)
        return bool(record is not None and record.stale)

    def is_fresh(self, uri):
        record = self.lookup(uri)
        return bool(record is not None and not record.stale)

    def clear(self):
        with self._lock:
            self._records.clear()
            self._dependents.clear()
            self.last_invalidated = set()

    def __repr__(self):
        with self._lock:
            return "ParseCache(%d records)" % len(self._records)


# Common spellings for the same dependency aware cache.
DependencyCache = ParseCache
DocumentCache = ParseCache
Snapshot = CacheRecord


class _RefreshFailure(Exception):
    def __init__(self, uri, error):
        super(_RefreshFailure, self).__init__(str(error))
        self.uri = canonical_uri(uri)
        self.error = error


class JsonLoader:
    """A caching JSON loader with complete dependency summaries.

    ``loader`` is any callable accepting a URI and returning parsed JSON.  A
    mapping of URI to document is also accepted.  Every load records the
    document digest and the transitive digests of the documents it refers to.

    With ``validate=False`` (the default) a cached record is reused until its
    target is invalidated, updated or refreshed, so shared targets are not
    fetched again for every result.  ``refresh()`` and ``refresh=True`` force
    a revalidation of the whole tree.  If a refresh fails, the previous
    snapshot is returned marked stale instead of being presented as a fresh,
    successful version.
    """

    def __init__(
        self,
        loader=None,
        base_uri="",
        cache=None,
        validate=False,
        jsonschema=False,
        **kwargs
    ):
        if loader is None:
            loader = _default_loader
        elif isinstance(loader, Mapping):
            documents = loader

            def loader(uri):
                value = documents[uri]
                if isinstance(value, BaseException):
                    raise value
                return value

        self.loader = loader
        self.base_uri = base_uri
        self.cache = cache if cache is not None else ParseCache()
        self.validate_sources = validate
        self.jsonschema = jsonschema
        self.kwargs = kwargs
        self._lock = threading.RLock()
        self.last_invalidated = set()
        self.last_failure = None

    @property
    def records(self):
        return self.cache.records

    def __call__(self, uri, **kwargs):
        return self.load(uri, **kwargs)

    def _fetch(self, uri):
        try:
            if self.kwargs and self.loader is _default_loader:
                return self.loader(uri, **self.kwargs)
            return self.loader(uri)
        except Exception as error:
            raise _RefreshFailure(uri, error) from error

    def _resolve_uri(self, uri):
        uri = canonical_uri(uri)
        if not uri:
            uri = canonical_uri(self.base_uri)
        return uri

    def _direct_dependencies(self, uri, document):
        return frozenset(
            dependency
            for dependency in _iter_refs(
                document, base_uri=uri, jsonschema=self.jsonschema
            )
            if dependency and dependency != uri
        )

    def _collect(self, uri, validate, force=False):
        """Fetch a consistent version of ``uri`` and all its dependencies.

        Nothing is installed in the cache here.  The complete version is
        committed atomically in :meth:`_commit`, so a failure part way through
        cannot leave a mixture of old and new documents behind.

        A cached record already carries a complete dependency summary from the
        version it was built with.  Those edges are preserved so the root
        summary stays complete without refetching every target on each reuse.
        A record that is marked stale is never reused silently as fresh.
        """

        documents = {}
        direct = {}
        pending = [uri]

        while pending:
            current = canonical_uri(pending.pop())
            if not current or current in documents:
                continue

            cached = self.cache.lookup(current)
            if (
                cached is not None
                and not force
                and not validate
                and not cached.stale
            ):
                documents[current] = cached.document
                edges = set(cached.direct_dependencies)
                if not edges:
                    edges.update(cached.dependencies)
                direct[current] = frozenset(edges)
                pending.extend(direct[current])
                continue

            document = self._fetch(current)
            documents[current] = document
            direct[current] = self._direct_dependencies(current, document)
            pending.extend(direct[current])

        return documents, direct

    def _summary(self, uri, documents, direct):
        """Return the complete dependency summary for ``uri``.

        The summary includes the digest of every document reachable through
        references, including documents reached through a cycle.  The record's
        own URI is never included.
        """

        seen = set()
        pending = list(direct.get(uri, ()))
        while pending:
            current = canonical_uri(pending.pop())
            if not current or current == uri or current in seen:
                continue
            seen.add(current)
            pending.extend(direct.get(current, ()))

        summary = {}
        for dependency in seen:
            digest = None
            if dependency in documents:
                digest = document_digest(documents[dependency])
            else:
                cached = self.cache.lookup(dependency)
                if cached is not None:
                    digest = cached.digest
            if digest is not None:
                summary[dependency] = digest
        return summary

    def _build_records(self, documents, direct):
        records = {}
        changed = set()
        digests = {
            uri: document_digest(document) for uri, document in documents.items()
        }
        for uri, document in documents.items():
            dependencies = self._summary(uri, documents, direct)
            cached = self.cache.lookup(uri)
            if (
                cached is not None
                and not cached.stale
                and cached.digest == digests[uri]
                and cached.dependencies == dependencies
            ):
                records[uri] = cached
                continue
            records[uri] = CacheRecord(
                uri,
                document,
                digest=digests[uri],
                dependencies=dependencies,
                direct_dependencies=direct.get(uri, ()),
            )
            changed.add(uri)
        return records, changed

    def _commit(self, records, changed):
        if not changed:
            self.last_invalidated = set()
            return

        keep = set(records)
        invalidated = set()
        with self.cache._lock:
            for uri in changed:
                self.cache._store(records[uri])
            # Results built from any changed target are affected by the new
            # version.  Unchanged shared targets and records installed by this
            # atomic refresh are left in place.
            for uri in changed:
                invalidated.update(
                    self.cache.invalidate(uri, include_self=False, keep=keep)
                )
        self.last_invalidated = invalidated

    def snapshot(self, uri, refresh=False, allow_stale=True, validate=None):
        """Return a :class:`CacheRecord` for ``uri``.

        A refresh failure keeps the previous snapshot, marks it stale, and
        returns it only with ``stale`` set.  If there is no previous snapshot,
        the underlying error is raised.
        """

        if validate is None:
            validate = self.validate_sources
        uri = self._resolve_uri(uri)
        self.last_failure = None

        with self._lock:
            try:
                documents, direct = self._collect(
                    uri, validate, force=refresh
                )
                records, changed = self._build_records(documents, direct)
                record = records.get(uri)
                if record is None:
                    raise KeyError(uri)
                self._commit(records, changed)
                return record
            except Exception as error:
                original = getattr(error, "error", error)
                failure_uri = getattr(error, "uri", uri)
                self.cache.mark_stale(failure_uri, error=original, recursive=True)
                if failure_uri != uri:
                    self.cache.mark_stale(uri, error=original, recursive=True)
                self.last_failure = {
                    "uri": failure_uri,
                    "error": original,
                }
                previous = self.cache.lookup(uri)
                if previous is None or not allow_stale:
                    raise original
                warnings.warn(
                    "Using stale snapshot for %r after refresh failed: %s"
                    % (uri, original),
                    StaleSnapshotWarning,
                    stacklevel=2,
                )
                return previous

    def load(self, uri, refresh=False, allow_stale=True, validate=None):
        return self.snapshot(
            uri, refresh=refresh, allow_stale=allow_stale, validate=validate
        ).document

    get = load
    load_document = load

    def record(self, uri, refresh=False, allow_stale=True, validate=None):
        return self.snapshot(
            uri, refresh=refresh, allow_stale=allow_stale, validate=validate
        )

    def refresh(self, uri, allow_stale=True, validate=True):
        return self.snapshot(
            uri, refresh=True, allow_stale=allow_stale, validate=validate
        )

    def reload(self, uri, allow_stale=True):
        return self.refresh(uri, allow_stale=allow_stale)

    def validate(self, uri, allow_stale=True):
        return self.snapshot(uri, refresh=False, allow_stale=allow_stale, validate=True)

    def invalidate(self, uri, include_self=True, recursive=True):
        return self.cache.invalidate(
            uri, include_self=include_self, recursive=recursive
        )

    def mark_stale(self, uri, error=None, recursive=True):
        return self.cache.mark_stale(uri, error=error, recursive=recursive)

    def dependencies(self, uri, refresh=False):
        return self.snapshot(uri, refresh=refresh).dependencies

    dependency_summary = dependencies

    def targets(self, uri, refresh=False):
        """Return the documents ``uri`` depends on, directly or transitively."""

        return set(self.dependencies(uri, refresh=refresh))

    def record_document(self, uri, document, dependencies=None, loader=None):
        """Record an already parsed root document.

        ``dependencies`` may be a complete mapping of URI to digest, or an
        iterable of URIs to fetch through ``loader``.  When it is omitted, the
        root document's references are discovered and their documents fetched.
        A failure leaves the previous version in place, marked stale.
        """

        uri = self._resolve_uri(uri)
        if not uri:
            return None
        self.last_failure = None
        source = self.loader if loader is None else loader

        with self._lock:
            try:
                direct = self._direct_dependencies(uri, document)
                documents = {uri: document}
                direct_map = {uri: direct}
                summary = {}

                if isinstance(dependencies, Mapping):
                    summary = {}
                    for key, value in dependencies.items():
                        key = canonical_uri(key)
                        if not key or key == uri:
                            continue
                        if value is None:
                            cached = self.cache.lookup(key)
                            if cached is not None:
                                value = cached.digest
                            else:
                                value = document_digest(
                                    self._fetch_from(source, key)
                                )
                        summary[key] = value
                    direct = frozenset(summary) | direct
                else:
                    pending = list(
                        direct if dependencies is None else dependencies
                    )
                    while pending:
                        dependency = canonical_uri(pending.pop())
                        if not dependency or dependency == uri:
                            continue
                        if dependency in documents:
                            pending.extend(direct_map.get(dependency, ()))
                            continue
                        if dependency in direct_map:
                            pending.extend(direct_map[dependency])
                            continue
                        document_for = self._fetch_from(source, dependency)
                        documents[dependency] = document_for
                        direct_map[dependency] = self._direct_dependencies(
                            dependency, document_for
                        )
                        pending.extend(direct_map[dependency])
                    summary = self._summary(uri, documents, direct_map)

                record = CacheRecord(
                    uri,
                    document,
                    dependencies=summary,
                    direct_dependencies=direct,
                )
                self.cache._store(record)
                self.cache.invalidate(
                    uri, include_self=False, recursive=True, keep={uri}
                )
                return record
            except Exception as error:
                original = getattr(error, "error", error)
                self.last_failure = {
                    "uri": getattr(error, "uri", uri),
                    "error": original,
                }
                self.cache.mark_stale(uri, error=original, recursive=True)
                previous = self.cache.lookup(uri)
                if previous is None:
                    raise original
                return previous

    def parse(self, uri, document, dependencies=None, loader=None):
        return self.record_document(
            uri, document, dependencies=dependencies, loader=loader
        )

    @staticmethod
    def _fetch_from(loader, uri):
        if isinstance(loader, Mapping):
            value = loader[uri]
            if isinstance(value, BaseException):
                raise value
            return value
        return loader(uri)

    def dependency_digest(self, uri, refresh=False):
        return self.snapshot(uri, refresh=refresh).dependency_digest

    def digest(self, uri, refresh=False):
        return self.snapshot(uri, refresh=refresh).digest

    def is_stale(self, uri):
        return self.cache.is_stale(self._resolve_uri(uri))

    def is_fresh(self, uri):
        return self.cache.is_fresh(self._resolve_uri(uri))

    def clear(self):
        self.cache.clear()


def _default_loader(uri, **kwargs):
    # Imported lazily so this module can be imported by jsonref itself without
    # a circular import at module load time.
    from . import jsonloader

    return jsonloader(uri, **kwargs)
