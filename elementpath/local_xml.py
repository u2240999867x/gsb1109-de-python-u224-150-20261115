"""
Batch-scoped catalog and resolver for local XML documents.

The catalog is intended for processing rules that allow only ``fn:doc()`` and
``fn:collection()`` access to a locked set of XML documents.  All resources are
resolved against a local base directory and are read only when they are both
inside that directory and part of the locked set for the batch.

A catalog keeps its parsed node trees in a cache keyed by batch and canonical
URI.  Consequently two catalogs for different batches never share a parsed
document even when they refer to the same URI, while repeated accesses inside
one batch return the same document node instance.
"""
from __future__ import annotations

import io
import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname
from xml.etree import ElementTree

import elementpath.aliases as ta

from elementpath.exceptions import ElementPathValueError, XMLResourceForbidden
from elementpath.etree import defuse_xml
from elementpath.tree_builders import get_node_tree
from elementpath.xpath_nodes import DocumentNode

__all__ = ['LocalXMLCatalog', 'LocalDocumentCatalog', 'LocalXMLResolver',
           'LocalDocumentResolver', 'LocalURICatalog', 'LocalUriCatalog',
           'LocalDirectoryCatalog', 'BatchXMLCatalog', 'LockedDocumentCatalog',
           'DocumentCatalog', 'DocumentResolver', 'LocalXMLCollection',
           'LocalDocumentCollection', 'CatalogEntry', 'CatalogReport',
           'LocalXMLResourceError', 'MissingDocumentError', 'MissingCollectionError',
           'DocumentAccessError', 'OutOfScopeError', 'InvalidDocumentURIError',
           'DocumentParseError', 'load_catalog']

PathLike = Union[str, os.PathLike[str]]
LockedSpec = Union[PathLike, Mapping[str, Any], Sequence[Any]]
CacheType = dict[tuple[str, str], DocumentNode]


class _DocumentCache(dict[tuple[str, str], DocumentNode]):
    """A cache dictionary that also carries the lock used to fill it."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.RLock()


_fallback_cache_lock = threading.RLock()


class LocalXMLResourceError(ElementPathValueError):
    """Base class for errors raised by a :class:`LocalXMLCatalog`."""

    kind = 'resource'
    reason = 'resource'

    def __init__(self, message: str, code: str = 'FODC0002',
                 uri: Optional[str] = None) -> None:
        super().__init__(message, code)
        self.uri = uri


class MissingDocumentError(LocalXMLResourceError):
    """A locked document is missing from the local URI directory."""

    kind = 'missing'
    reason = 'missing'

    def __init__(self, message: str, uri: Optional[str] = None,
                 uris: Optional[Iterable[str]] = None) -> None:
        super().__init__(message, 'FODC0002', uri)
        self.uris = tuple(uris) if uris is not None else (() if uri is None else (uri,))


class MissingCollectionError(LocalXMLResourceError):
    """A collection is not available in the local URI directory."""

    kind = 'missing'
    reason = 'missing'

    def __init__(self, message: str, uri: Optional[str] = None) -> None:
        super().__init__(message, 'FODC0002', uri)


class DocumentAccessError(LocalXMLResourceError):
    """A URI is outside the local directory or is not locked for the batch."""

    kind = 'out_of_scope'

    def __init__(self, message: str, uri: Optional[str] = None,
                 code: str = 'FODC0005', reason: str = 'not_locked') -> None:
        super().__init__(message, code, uri)
        self.reason = reason


class InvalidDocumentURIError(LocalXMLResourceError):
    """The URI cannot be used as a document URI."""

    kind = 'invalid'
    reason = 'invalid'

    def __init__(self, message: str, uri: Optional[str] = None) -> None:
        super().__init__(message, 'FODC0005', uri)


class DocumentParseError(LocalXMLResourceError):
    """A locked document is not a well-formed XML document."""

    kind = 'parse'
    reason = 'parse'

    def __init__(self, message: str, uri: Optional[str] = None) -> None:
        super().__init__(message, 'FODC0002', uri)


# A more explicit name for an out-of-scope access.
OutOfScopeError = DocumentAccessError


@dataclass(frozen=True)
class CatalogEntry:
    """A locked document entry.

    :param uri: the URI exposed by the catalog and returned by ``fn:document-uri``.
    :param path: the local file path of the document.
    :param name: the name used in the locked set, useful for reports.
    """
    uri: str
    path: Path
    name: str

    @property
    def exists(self) -> bool:
        return self.path.is_file()


@dataclass(frozen=True)
class CatalogReport:
    """Result of :meth:`LocalXMLCatalog.report`.

    ``missing`` lists locked documents that are not present on disk,
    ``out_of_bounds`` lists URIs that would resolve outside the local URI
    directory, and ``not_locked`` lists local URIs that are present but were
    not locked for the batch. ``out_of_scope`` is the union of the latter two.
    """
    missing: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    out_of_bounds: tuple[str, ...] = ()
    not_locked: tuple[str, ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.missing or self.out_of_scope)

    def __getitem__(self, key: str) -> tuple[str, ...]:
        if key == 'missing':
            return self.missing
        elif key in ('out_of_scope', 'rejected'):
            return self.out_of_scope
        elif key in ('out_of_bounds', 'outside'):
            return self.out_of_bounds
        elif key in ('not_locked', 'unlocked'):
            return self.not_locked
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, Any]:
        return {
            'missing': list(self.missing),
            'out_of_scope': list(self.out_of_scope),
            'out_of_bounds': list(self.out_of_bounds),
            'not_locked': list(self.not_locked),
            'reasons': dict(self.reasons),
        }

    @property
    def missing_files(self) -> tuple[str, ...]:
        return self.missing

    @property
    def has_errors(self) -> bool:
        return self.__bool__()


def _is_within(path: Path, base_dir: Path) -> bool:
    try:
        path.relative_to(base_dir)
    except ValueError:
        return False
    else:
        return True


def _is_local_scheme(scheme: str) -> bool:
    return scheme in ('', 'file') or \
        len(scheme) == 1 and scheme.isalpha()


def _as_posix(value: str) -> str:
    return value.replace('\\', '/') if os.sep == '\\' else value


class LocalXMLCatalog:
    """
    A local, batch-scoped catalog for XML documents and collections.

    :param base_dir: the local directory used as the URI base for the catalog.
    :param locked: the documents locked for the batch.  It can be a single path \
    or URI, an iterable of paths/URIs, or a mapping from public URI to local path. \
    A mapping value of ``None`` means that the key is a path/URI in *base_dir*. \
    If omitted, all ``*.xml`` files below *base_dir* are locked.
    :param batch: an optional batch identifier.  It is part of the cache identity, \
    so catalogs with different batch identifiers never reuse parsed documents.
    :param collections: an optional mapping from collection names to iterables of \
    locked documents or to directories inside *base_dir*.
    :param default_collection: an optional name or iterable of documents used by \
    ``fn:collection()`` without arguments.  If omitted, the default collection is \
    the sequence of all locked documents in catalog order.
    :param defuse_xml: if `True` (the default) XML entities are rejected before parsing.
    :param namespaces: an optional namespace mapping used when building node trees.
    :param strict: if `True` out-of-scope entries raise at construction time.  For \
    default they are recorded and reported by :meth:`report`.
    :param cache: an optional cache shared with other catalogs.  Keys always include \
    the batch identifier and the canonical URI.
    """

    def __init__(self, base_dir: PathLike,
                 locked: Optional[LockedSpec] = None,
                 *,
                 batch: Optional[str] = None,
                 collections: Optional[Mapping[str, LockedSpec]] = None,
                 default_collection: Optional[Union[str, LockedSpec]] = None,
                 defuse_xml: bool = True,
                 namespaces: Optional[ta.NamespacesType] = None,
                 strict: bool = False,
                 cache: Optional[CacheType] = None) -> None:

        self.base_dir = Path(base_dir).expanduser().resolve(strict=False)
        if self.base_dir.exists() and not self.base_dir.is_dir():
            raise ValueError(f"{base_dir!r} is not a directory")

        self.base_uri = self.base_dir.as_uri().rstrip('/') + '/'
        self.batch = batch if batch is not None else self.base_dir.name
        self.defuse_xml = defuse_xml
        self.namespaces = dict(namespaces) if namespaces else {}
        self.strict = strict

        self._entries: dict[str, CatalogEntry] = {}
        self._entry_order: list[str] = []
        self._collections: dict[str, tuple[str, ...]] = {}
        if cache is None:
            self._cache: CacheType = _DocumentCache()
        else:
            self._cache = cache
        self._out_of_scope: list[str] = []
        self._reasons: dict[str, str] = {}
        self._lock = getattr(self._cache, 'lock', None) or _fallback_cache_lock

        if locked is None:
            locked = sorted(
                str(p.relative_to(self.base_dir))
                for p in self.base_dir.rglob('*.xml') if p.is_file()
            )

        for spec in self._iter_specs(locked):
            self._add_spec(spec)

        if collections:
            for name, members in collections.items():
                self._collections[name] = self._collect_uris(members)

        if default_collection is None:
            if 'default' in self._collections:
                self._default_collection = self._collections['default']
            else:
                self._default_collection = tuple(self._entry_order)
        elif isinstance(default_collection, str) and default_collection in self._collections:
            self._default_collection = self._collections[default_collection]
        else:
            self._default_collection = self._collect_uris(default_collection)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}(base_dir={str(self.base_dir)!r}, ' \
               f'batch={self.batch!r}, locked={len(self._entries)})'

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[str]:
        yield from self._entry_order

    def __contains__(self, uri: object) -> bool:
        if isinstance(uri, os.PathLike):
            uri = os.fspath(uri)
        if not isinstance(uri, str):
            return False
        try:
            self._resolve_entry(uri, record=False)
        except LocalXMLResourceError:
            return False
        else:
            return True

    def __getitem__(self, uri: str) -> DocumentNode:
        return self.get_document(uri)

    ###
    # Mapping-like accessors
    def keys(self) -> tuple[str, ...]:
        return tuple(self._entry_order)

    def values(self) -> tuple[DocumentNode, ...]:
        return tuple(self.get_document(uri) for uri in self._entry_order)

    def items(self) -> tuple[tuple[str, DocumentNode], ...]:
        return tuple((uri, self.get_document(uri)) for uri in self._entry_order)

    @property
    def entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(self._entries[uri] for uri in self._entry_order)

    @property
    def collections(self) -> dict[str, tuple[str, ...]]:
        return dict(self._collections)

    @property
    def default_collection(self) -> tuple[str, ...]:
        return self._default_collection

    ###
    # Batch and cache identity
    @property
    def cache_key(self) -> tuple[str, str]:
        return self.batch, ''

    def new_batch(self, batch: str, locked: Optional[LockedSpec] = None) -> 'LocalXMLCatalog':
        """Returns a catalog for another batch with an independent cache namespace."""
        return self.__class__(
            self.base_dir,
            self._entry_order if locked is None else locked,
            batch=batch,
            collections=self._collections,
            default_collection=self._default_collection,
            defuse_xml=self.defuse_xml,
            namespaces=self.namespaces,
            strict=self.strict,
            cache=self._cache,
        )

    def set_batch(self, batch: str) -> None:
        """Switches the cache namespace of this catalog."""
        self.batch = batch

    def clear_cache(self, batch: Optional[str] = None) -> None:
        """Clears cached documents for *batch*, or for the current batch."""
        batch = self.batch if batch is None else batch
        with self._lock:
            for key in [k for k in self._cache if k[0] == batch]:
                del self._cache[key]

    ###
    # Entries and URI resolution
    @staticmethod
    def _iter_specs(locked: Optional[LockedSpec]) -> Iterator[Any]:
        if locked is None:
            yield from ()
        elif isinstance(locked, (str, os.PathLike, Mapping, CatalogEntry)):
            yield locked
        else:
            yield from locked

    def _iter_locked_pairs(self, spec: Any) -> Iterator[tuple[Optional[str], Any]]:
        if isinstance(spec, CatalogEntry):
            yield spec.uri, spec.path
        elif isinstance(spec, Mapping):
            if 'uri' in spec or 'path' in spec:
                yield spec.get('uri'), spec.get('path')
            elif 'documents' in spec or 'locked' in spec:
                for item in spec.get('documents', spec.get('locked', ())):
                    yield from self._iter_locked_pairs(item)
            else:
                for key, value in spec.items():
                    if value is None or isinstance(value, bool):
                        yield None, key
                    else:
                        yield str(key), value
        elif isinstance(spec, (str, os.PathLike)):
            yield None, spec
        else:
            raise TypeError(f"invalid locked document specification {spec!r}")

    def _add_spec(self, spec: Any) -> None:
        try:
            pairs = list(self._iter_locked_pairs(spec))
        except TypeError:
            raise
        except LocalXMLResourceError as err:
            self._record_out_of_scope(err.uri or repr(spec), err.reason)
            if self.strict:
                raise
            return

        for declared, target in pairs:
            self._add_entry(declared, target)

    def _add_entry(self, declared: Optional[str], target: Any) -> None:
        if target is None:
            target = declared
        if target is None:
            raise ValueError(f"missing path in locked document specification {declared!r}")

        raw_target = os.fspath(target)
        try:
            path = self._path_from_uri(raw_target)
            if path is None:
                raise DocumentAccessError(
                    f"document URI {raw_target!r} is not a local URI",
                    raw_target, reason='invalid'
                )

            path = path.expanduser().resolve(strict=False)
            if not _is_within(path, self.base_dir):
                raise DocumentAccessError(
                    f"document {raw_target!r} is outside the local URI directory",
                    raw_target, reason='outside'
                )

            if declared is None:
                name = raw_target
                uri = path.as_uri()
            else:
                name = declared
                uri = self._normalise_entry_uri(declared, path)
        except LocalXMLResourceError as err:
            self._record_out_of_scope(err.uri or raw_target, err.reason)
            if self.strict:
                raise
            return

        entry = CatalogEntry(uri, path, name)
        if uri not in self._entries:
            self._entry_order.append(uri)
        self._entries[uri] = entry

    def _normalise_entry_uri(self, uri: str, path: Path) -> str:
        parts = urlsplit(uri.strip())
        if parts.fragment:
            raise InvalidDocumentURIError(f"document URI {uri!r} has a fragment", uri)
        if parts.query:
            raise InvalidDocumentURIError(f"document URI {uri!r} has a query", uri)

        if not parts.scheme:
            if uri.startswith('/'):
                return path.as_uri()
            return urljoin(self.base_uri, _as_posix(uri.strip()))
        elif parts.scheme == 'file':
            return path.as_uri()
        else:
            return uri.strip()

    def _path_from_uri(self, uri: str) -> Optional[Path]:
        parts = urlsplit(uri.strip())
        if parts.fragment:
            raise InvalidDocumentURIError(f"document URI {uri!r} has a fragment", uri)
        if parts.query:
            raise InvalidDocumentURIError(f"document URI {uri!r} has a query", uri)

        if parts.scheme == 'file':
            if parts.netloc not in ('', 'localhost'):
                return None
            return Path(url2pathname(unquote(parts.path)))
        elif not _is_local_scheme(parts.scheme):
            return None

        path = unquote(parts.path)
        if not path:
            path = unquote(uri.strip())
        if not path:
            return self.base_dir

        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.base_dir / candidate
        return candidate

    def _canonical_uri(self, uri: str) -> Optional[str]:
        parts = urlsplit(uri.strip())
        if parts.fragment:
            raise InvalidDocumentURIError(f"document URI {uri!r} has a fragment", uri)
        if parts.query:
            raise InvalidDocumentURIError(f"document URI {uri!r} has a query", uri)

        if not parts.scheme:
            if uri.startswith('/'):
                path = Path(uri).expanduser().resolve(strict=False)
                return path.as_uri()
            return urljoin(self.base_uri, _as_posix(uri.strip()))
        elif parts.scheme == 'file':
            path = self._path_from_uri(uri)
            if path is None:
                return None
            canonical = path.expanduser().resolve(strict=False).as_uri()
            # Prefer the URI declared for the locked entry when it resolves to
            # the same file, also for alternative URI spellings.
            for entry in self._entries.values():
                if entry.path == path.expanduser().resolve(strict=False):
                    return entry.uri
            return canonical
        else:
            return uri.strip()

    def _resolve_entry(self, uri: str | os.PathLike[str], *,
                       record: bool = True) -> CatalogEntry:
        if isinstance(uri, os.PathLike):
            uri = os.fspath(uri)
        if not isinstance(uri, str):
            raise InvalidDocumentURIError(f"invalid document URI {uri!r}")

        uri = uri.strip()
        if not uri:
            raise InvalidDocumentURIError("empty document URI", uri)

        try:
            canonical_uri = self._canonical_uri(uri)
        except InvalidDocumentURIError:
            if record:
                self._record_out_of_scope(uri, 'invalid')
            raise

        if canonical_uri is not None and canonical_uri in self._entries:
            return self._entries[canonical_uri]
        elif uri in self._entries:
            return self._entries[uri]

        path = self._path_from_uri(uri)
        if path is not None:
            path = path.expanduser().resolve(strict=False)
            if not _is_within(path, self.base_dir):
                if record:
                    self._record_out_of_scope(uri, 'outside')
                raise DocumentAccessError(
                    f"document {uri!r} is outside the local URI directory",
                    uri, reason='outside'
                )

        if record:
            self._record_out_of_scope(uri, 'not_locked')
        raise DocumentAccessError(
            f"document {uri!r} is not locked for batch {self.batch!r}",
            uri, reason='not_locked'
        )

    def _record_out_of_scope(self, uri: str, reason: str = 'not_locked') -> None:
        if uri not in self._out_of_scope:
            self._out_of_scope.append(uri)
        self._reasons.setdefault(uri, reason)

    def _collect_uris(self, members: Any) -> tuple[str, ...]:
        uris: list[str] = []
        for spec in self._iter_specs(members):
            try:
                pairs = list(self._iter_locked_pairs(spec))
            except LocalXMLResourceError as err:
                self._record_out_of_scope(err.uri or repr(spec), err.reason)
                if self.strict:
                    raise
                continue

            for declared, target in pairs:
                if target is None:
                    target = declared
                if target is None:
                    continue

                raw_target = os.fspath(target)
                try:
                    path = self._path_from_uri(raw_target)
                except LocalXMLResourceError as err:
                    self._record_out_of_scope(err.uri or raw_target, err.reason)
                    if self.strict:
                        raise
                    continue

                if path is not None:
                    path = path.expanduser().resolve(strict=False)
                    if not _is_within(path, self.base_dir):
                        self._record_out_of_scope(
                            declared or raw_target, 'outside'
                        )
                        if self.strict:
                            raise DocumentAccessError(
                                f"collection member {raw_target!r} is outside "
                                f"the local URI directory", raw_target, reason='outside'
                            )
                        continue
                    elif path.is_dir():
                        for entry in self.entries:
                            if _is_within(entry.path, path) and entry.uri not in uris:
                                uris.append(entry.uri)
                        continue

                try:
                    entry = self._resolve_entry(declared or raw_target)
                except LocalXMLResourceError:
                    if self.strict:
                        raise
                    continue
                else:
                    if entry.uri not in uris:
                        uris.append(entry.uri)

        return tuple(uris)

    ###
    # Document and collection access
    def get_path(self, uri: str | os.PathLike[str]) -> Path:
        """Returns the local path associated with a locked document URI."""
        return self._resolve_entry(uri).path

    def get_document_uri(self, uri: str | os.PathLike[str]) -> str:
        """Returns the canonical URI exposed for a locked document."""
        return self._resolve_entry(uri).uri

    def resolve(self, uri: str | os.PathLike[str]) -> CatalogEntry:
        """Returns the catalog entry for *uri* or raises an out-of-scope error."""
        return self._resolve_entry(uri)

    resolve_uri = get_document_uri

    def parse(self, uri: str) -> DocumentNode:
        """Alias of :meth:`get_document`."""
        return self.get_document(uri)

    def parse_all(self, ignore_missing: bool = False) -> dict[str, DocumentNode]:
        """Alias of :meth:`load_all`."""
        return self.load_all(ignore_missing)

    def iter_uris(self) -> Iterator[str]:
        """Iterates over locked document URIs in catalog order."""
        yield from self._entry_order

    def get_collection_uris(self, uri: Optional[str | os.PathLike[str]] = None) -> list[str]:
        """Alias of :meth:`uri_collection`."""
        return self.uri_collection(uri)

    def is_locked(self, uri: str | os.PathLike[str]) -> bool:
        return uri in self

    def exists(self, uri: str | os.PathLike[str]) -> bool:
        try:
            return self._resolve_entry(uri, record=False).path.is_file()
        except LocalXMLResourceError:
            return False

    def get_document(self, uri: str | os.PathLike[str]) -> DocumentNode:
        """Returns the document node for *uri*, parsing it at most once per batch."""
        entry = self._resolve_entry(uri)

        if entry.path.is_dir():
            raise InvalidDocumentURIError(
                f"document URI {entry.uri!r} is a directory", entry.uri
            )
        elif not entry.path.is_file():
            raise MissingDocumentError(
                f"locked document {entry.uri!r} is missing from {str(self.base_dir)!r}",
                entry.uri
            )

        cache_key = (self.batch, entry.uri)
        with self._lock:
            try:
                return self._cache[cache_key]
            except KeyError:
                pass

            document = self._parse_document(entry.path, entry.uri)
            self._cache[cache_key] = document
            return document

    def _parse_document(self, path: Path, uri: str) -> DocumentNode:
        try:
            if self.defuse_xml:
                data = path.read_bytes()
                data = defuse_xml(data)
                document = ElementTree.parse(io.BytesIO(data))
            else:
                document = ElementTree.parse(str(path))
        except XMLResourceForbidden as err:
            raise DocumentParseError(
                f"document {uri!r} contains a forbidden XML resource: {err.message}", uri
            ) from None
        except ElementTree.ParseError as err:
            raise DocumentParseError(f"document {uri!r} is not well-formed: {err}", uri) from None
        except FileNotFoundError:
            raise MissingDocumentError(f"locked document {uri!r} is missing", uri) from None
        except OSError as err:
            raise DocumentParseError(f"cannot read document {uri!r}: {err}", uri) from None

        node = get_node_tree(document, self.namespaces, uri=uri)
        assert isinstance(node, DocumentNode)
        return node

    # Convenience aliases
    load = get_document
    get = get_document
    document_uri = get_document_uri
    path = get_path
    open = get_path

    def load_all(self, ignore_missing: bool = False) -> dict[str, DocumentNode]:
        """Parses and returns all locked documents."""
        documents: dict[str, DocumentNode] = {}
        for entry in self.entries:
            try:
                documents[entry.uri] = self.get_document(entry.uri)
            except MissingDocumentError:
                if not ignore_missing:
                    raise
        return documents

    def iter_documents(self) -> Iterator[tuple[str, DocumentNode]]:
        """Iterates over already parsed documents, without parsing locked ones."""
        with self._lock:
            items = tuple(self._cache.items())
        for (batch, uri), doc in items:
            if batch == self.batch:
                yield uri, doc

    @property
    def documents(self) -> dict[str, DocumentNode]:
        """A snapshot of the documents parsed so far."""
        return dict(self.iter_documents())

    @property
    def loaded_documents(self) -> dict[str, DocumentNode]:
        return self.documents

    @property
    def missing(self) -> tuple[str, ...]:
        """Locked URIs whose files are not present on disk."""
        return tuple(e.uri for e in self.entries if not e.path.is_file())

    @property
    def out_of_scope(self) -> tuple[str, ...]:
        """URIs rejected as not locked or outside the base directory."""
        return tuple(self._out_of_scope)

    @property
    def out_of_bounds(self) -> tuple[str, ...]:
        """URIs rejected because they resolve outside the local URI directory."""
        return tuple(x for x in self._out_of_scope if self._reasons.get(x) == 'outside')

    @property
    def not_locked(self) -> tuple[str, ...]:
        """Local URIs rejected because they are not part of the locked batch."""
        return tuple(x for x in self._out_of_scope if self._reasons.get(x) == 'not_locked')

    def report(self) -> CatalogReport:
        """Reports missing locked documents and rejected out-of-scope accesses."""
        return CatalogReport(
            self.missing, self.out_of_scope, self.out_of_bounds,
            self.not_locked, dict(self._reasons)
        )

    def get_default_collection(self) -> list[DocumentNode]:
        return [self.get_document(uri) for uri in self._default_collection]

    def get_collection(self, uri: Optional[str | os.PathLike[str]] = None) -> list[DocumentNode]:
        """
        Returns the documents of a named collection, a local directory collection,
        or the default collection when *uri* is `None`.
        """
        if uri is None:
            return self.get_default_collection()

        uri = uri.strip()
        if uri in self._collections:
            return [self.get_document(x) for x in self._collections[uri]]

        try:
            path = self._path_from_uri(uri)
        except LocalXMLResourceError:
            self._record_out_of_scope(uri, 'invalid')
            raise

        if path is None:
            self._record_out_of_scope(uri, 'not_locked')
            raise DocumentAccessError(
                f"collection {uri!r} is not a local collection",
                uri, 'FODC0004', reason='not_locked'
            )

        path = path.expanduser().resolve(strict=False)
        if not _is_within(path, self.base_dir):
            self._record_out_of_scope(uri, 'outside')
            raise DocumentAccessError(
                f"collection {uri!r} is outside the local URI directory",
                uri, 'FODC0004', reason='outside'
            )

        if path.is_dir():
            uris = [e.uri for e in self.entries if _is_within(e.path, path)]
            if not uris:
                raise MissingCollectionError(
                    f"collection {uri!r} has no locked XML documents", uri
                )
            return [self.get_document(x) for x in uris]
        elif path.is_file():
            return [self.get_document(self._resolve_entry(uri).uri)]

        raise MissingCollectionError(f"collection {uri!r} not found", uri)

    collection = get_collection

    def uri_collection(self, uri: Optional[str | os.PathLike[str]] = None) -> list[str]:
        """Returns the URIs of a collection without parsing its documents."""
        if uri is None:
            return list(self._default_collection)

        uri = uri.strip()
        if uri in self._collections:
            return list(self._collections[uri])

        try:
            path = self._path_from_uri(uri)
        except LocalXMLResourceError:
            self._record_out_of_scope(uri, 'invalid')
            raise

        if path is None:
            self._record_out_of_scope(uri, 'not_locked')
            raise DocumentAccessError(
                f"collection {uri!r} is not a local collection",
                uri, 'FODC0004', reason='not_locked'
            )

        path = path.expanduser().resolve(strict=False)
        if not _is_within(path, self.base_dir):
            self._record_out_of_scope(uri, 'outside')
            raise DocumentAccessError(
                f"collection {uri!r} is outside the local URI directory",
                uri, 'FODC0004', reason='outside'
            )

        if path.is_dir():
            uris = [e.uri for e in self.entries if _is_within(e.path, path)]
            if not uris:
                raise MissingCollectionError(
                    f"collection {uri!r} has no locked XML documents", uri
                )
            return uris
        elif path.is_file():
            return [self._resolve_entry(uri).uri]

        raise MissingCollectionError(f"collection {uri!r} not found", uri)

    uris = uri_collection

    ###
    # Manifest and directory constructors
    @classmethod
    def from_manifest(cls, manifest: PathLike, *,
                      base_dir: Optional[PathLike] = None,
                      batch: Optional[str] = None,
                      **kwargs: Any) -> 'LocalXMLCatalog':
        """Builds a catalog from a JSON, XML or line-oriented manifest file."""
        manifest_path = Path(manifest).expanduser().resolve(strict=False)
        if base_dir is None:
            base_dir = manifest_path.parent

        text = manifest_path.read_text(encoding='utf-8')
        locked: Any
        collections: dict[str, Any] = {}
        default_collection: Any = None

        if text.lstrip().startswith('<'):
            root = ElementTree.fromstring(text)
            root_name = root.tag.rsplit('}', 1)[-1]
            if root_name in ('document', 'doc', 'resource'):
                locked = [root.get('path') or root.get('file') or
                          root.get('uri') or root.get('href')]
            else:
                locked = []
                for elem in root.iter():
                    if callable(elem.tag):
                        continue
                    name = elem.tag.rsplit('}', 1)[-1]
                    if name in ('document', 'doc', 'resource'):
                        target = elem.get('path') or elem.get('file') or \
                            elem.get('uri') or elem.get('href')
                        if target is not None:
                            locked.append(target)
                    elif name == 'collection':
                        collection_name = elem.get('name')
                        if collection_name:
                            collections[collection_name] = [
                                e.get('path') or e.get('file') or
                                e.get('uri') or e.get('href')
                                for e in elem.iter()
                                if not callable(e.tag) and
                                e.tag.rsplit('}', 1)[-1] in ('document', 'doc', 'resource')
                            ]
        else:
            data: Any
            if text.lstrip().startswith(('[', '{')):
                data = json.loads(text)
            else:
                data = None

            if data is None:
                locked = [line.split('#', 1)[0].strip() for line in text.splitlines()
                          if line.split('#', 1)[0].strip()]
            elif isinstance(data, list):
                locked = data
            elif isinstance(data, Mapping):
                if 'base_dir' in data and base_dir == manifest_path.parent:
                    base_dir = manifest_path.parent / data['base_dir']
                batch = data.get('batch', batch)
                locked = data.get('documents', data.get('locked', []))
                collections = data.get('collections', {})
                default_collection = data.get('default_collection')
            else:
                raise ValueError(f"invalid catalog manifest {str(manifest_path)!r}")

        return cls(base_dir, locked, batch=batch,
                   collections=collections or None,
                   default_collection=default_collection, **kwargs)

    @classmethod
    def from_directory(cls, base_dir: PathLike, *,
                       pattern: str = '*.xml', batch: Optional[str] = None,
                       **kwargs: Any) -> 'LocalXMLCatalog':
        """Locks all files matching *pattern* below *base_dir*."""
        root = Path(base_dir).expanduser().resolve(strict=False)
        locked = sorted(str(p.relative_to(root)) for p in root.rglob(pattern) if p.is_file())
        return cls(root, locked, batch=batch, **kwargs)


# Compatibility aliases for the public name of the local resolver.
LocalDocumentCatalog = LocalXMLCatalog
LocalXMLResolver = LocalXMLCatalog
LocalDocumentResolver = LocalXMLCatalog
LocalURICatalog = LocalXMLCatalog
LocalUriCatalog = LocalXMLCatalog
LocalDirectoryCatalog = LocalXMLCatalog
BatchXMLCatalog = LocalXMLCatalog
LockedDocumentCatalog = LocalXMLCatalog
DocumentCatalog = LocalXMLCatalog
DocumentResolver = LocalXMLCatalog
LocalXMLCollection = LocalXMLCatalog
LocalDocumentCollection = LocalXMLCatalog


def load_catalog(base_dir: PathLike,
                 locked: Optional[LockedSpec] = None,
                 **kwargs: Any) -> LocalXMLCatalog:
    """Creates a :class:`LocalXMLCatalog` for a local URI directory."""
    return LocalXMLCatalog(base_dir, locked, **kwargs)


def create_catalog(base_dir: PathLike,
                   locked: Optional[LockedSpec] = None,
                   **kwargs: Any) -> LocalXMLCatalog:
    """Alias of :func:`load_catalog`."""
    return LocalXMLCatalog(base_dir, locked, **kwargs)


def resolve_document(catalog: LocalXMLCatalog, uri: str) -> DocumentNode:
    """Returns a document node from *catalog*, resolving *uri* locally."""
    return catalog.get_document(uri)


def resolve_collection(catalog: LocalXMLCatalog, uri: Optional[str] = None) \
        -> list[DocumentNode]:
    """Returns a collection of document nodes from *catalog*."""
    return catalog.get_collection(uri)
