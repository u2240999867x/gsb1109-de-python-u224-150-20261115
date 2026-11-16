#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import hashlib
import os
from collections import OrderedDict
from threading import Lock, Event
from collections.abc import Callable, Iterable
from functools import lru_cache, wraps
from typing import Any, Generic, Optional, overload, TypeVar, TYPE_CHECKING, Union, cast
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from xmlschema.aliases import SchemaType, SourceArgType
from xmlschema.exceptions import XMLSchemaAttributeError, XMLSchemaTypeError, XMLSchemaValueError
from xmlschema.utils.etree import etree_tostring, is_etree_document, is_etree_element
from xmlschema.utils.urls import is_remote_url, normalize_url

if TYPE_CHECKING:
    from xmlschema.resources import XMLResource  # noqa
    from xmlschema.validators.xsdbase import XsdComponent  # noqa


T = TypeVar('T', bound=Union[SchemaType, 'XsdComponent'])
RT = TypeVar('RT', covariant=True)


_cached_functions: dict[Callable[..., Any], tuple[int | None, bool]] = {}


class SchemaCache:

    __slots__ = ('_enabled', '_functions', '_caches', '_lock')

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._functions = _cached_functions.copy()
        self._caches: dict[Callable[..., Any], Any] = {}
        self._lock = Lock()
        self._create_caches()

    def __call__(self, func: Callable[..., RT], *args: Any, **kwargs: Any) -> RT:
        try:
            return cast(RT, self._caches[func](*args, **kwargs))
        except KeyError:
            with self._lock:
                return cast(RT, self._caches[func](*args, **kwargs))

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if value is not self._enabled:
            self._enabled = value
            with self._lock:
                self._create_caches()

    def _create_caches(self) -> None:
        if self._enabled:
            for func, (maxsize, typed) in self._functions.items():
                self._caches[func] = lru_cache(maxsize, typed)(func)
        else:
            # Caching is not enables: fallback to registered functions
            for func in self._functions:
                self._caches[func] = func

    def register(self, func: Callable[..., RT],
                 maxsize: int | None = None,
                 typed: bool = True) -> None:
        if not callable(func):
            raise XMLSchemaTypeError(f"{func!r} is not callable")

        with self._lock:
            if func in self._functions:
                raise XMLSchemaValueError(f"{func!r} is already cached by {self!r}")
            self._functions[func] = maxsize, typed
            self._caches[func] = lru_cache(maxsize, typed)(func) if self._enabled else func

    def clear(self) -> None:
        with self._lock:
            if self._enabled:
                for cache in self._caches.values():
                    cache.cache_clear()


def schema_lru_cache(maxsize: int | None = None, typed: bool = False) -> Callable[..., RT]:
    """
    Cache an XSD validator method using an LRU cache stored in XSD global maps cache.
    """
    def wrapper_cached(func: Callable[..., RT]) -> Callable[..., RT]:
        _cached_functions[func] = maxsize, typed

        @wraps(func)
        def wrapped_func(validator: T, *args: Any, **kwargs: Any) -> RT:
            return validator.maps.cache(func, validator, *args, **kwargs)
        return wrapped_func

    return cast(Callable[..., RT], wrapper_cached)


def schema_cache(func: Callable[..., RT]) -> Callable[..., RT]:
    """
    Cache an XSD validator method using an unlimited cache stored in XSD global maps cache.
    """
    _cached_functions[func] = None, False

    @wraps(func)
    def wrapped_func(validator: T, *args: Any, **kwargs: Any) -> RT:
        return validator.maps.cache(func, validator, *args, **kwargs)
    return wrapped_func


# noinspection PyPep8Naming
class schema_cached_property(Generic[T, RT]):
    """
    A property that caches the value on the :class:`SchemaCache` of the global maps.
    """
    __slots__ = ('func', '_name', '__dict__')

    def __init__(self, func: Callable[[T], RT]) -> None:
        _cached_functions[func] = None, False
        self.func = func
        self.__doc__ = func.__doc__
        if hasattr(func, '__module__'):
            self.__module__ = func.__module__

    def __set_name__(self, owner: type[T], name: str) -> None:
        if not hasattr(owner, 'built'):
            raise XMLSchemaTypeError("{!r} is not an XSD validator".format(owner))
        if name == 'built':
            raise XMLSchemaAttributeError("can't apply to 'built' property")
        self._name = name

    @overload
    def __get__(self, validator: None, owner: type[T]) -> Callable[[T], RT]:
        ...

    @overload
    def __get__(self, validator: T, owner: type[T]) -> RT:
        ...

    def __get__(self, validator: T | None, owner: type[T]) -> Callable[[T], RT] | RT:
        if validator is None:
            return self.func
        elif not validator.maps.built:
            return self.func(validator)
        return validator.maps.cache(self.func, validator)


def _content_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def _local_path(url: str) -> Optional[str]:
    """Returns the local filesystem path for a URL, `None` for remote URLs."""
    if is_remote_url(url):
        return None
    elif url.startswith('file:'):
        return url2pathname(unquote(urlsplit(url).path)) or None
    return url


def _hash_file(path: str) -> Optional[str]:
    """Returns a content hash for a local file, `None` if it's not readable."""
    try:
        with open(path, 'rb') as fp:
            return _content_hash(fp.read())
    except OSError:
        return None


def _source_token(source: Any, base_url: Optional[str]) -> Optional[tuple]:
    """
    Returns a hashable token identifying a schema source, `None` if the source
    cannot be safely identified and so it's not cacheable.
    """
    from xmlschema.resources import XMLResource

    if isinstance(source, (list, tuple)):
        tokens = tuple(_source_token(s, base_url) for s in source)
        if any(token is None for token in tokens):
            return None
        return 'sources', tokens

    if isinstance(source, XMLResource):
        if source.url is not None:
            return 'url', source.url
        elif getattr(source, 'text', None) is not None:
            return 'text', _content_hash(source.text.encode('utf-8'))
        return 'tree', _content_hash(etree_tostring(source.root, encoding='utf-8'))

    if isinstance(source, str):
        if source.lstrip().startswith('<'):
            return 'text', _content_hash(source.encode('utf-8'))
        try:
            return 'url', normalize_url(source, base_url)
        except (TypeError, ValueError):
            return None

    if isinstance(source, bytes):
        return 'text', _content_hash(source)

    if isinstance(source, os.PathLike):
        try:
            return 'url', normalize_url(os.fspath(source), base_url)
        except (TypeError, ValueError):
            return None

    if is_etree_document(source):
        return 'tree', _content_hash(etree_tostring(source.getroot(), encoding='utf-8'))
    if is_etree_element(source):
        return 'tree', _content_hash(etree_tostring(source, encoding='utf-8'))

    if hasattr(source, 'read'):
        try:
            position = source.tell()
        except (AttributeError, OSError):
            return None
        data = source.read()
        try:
            source.seek(position)
        except OSError:
            return None
        if isinstance(data, str):
            data = data.encode('utf-8')
        return 'stream', _content_hash(data)

    return None


def _freeze_option(value: Any) -> Any:
    """Converts a settings option value to a hashable and comparable form."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        items = [(_freeze_option(k), _freeze_option(v)) for k, v in value.items()]
        return tuple(sorted(items, key=repr))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_option(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_option(v) for v in value), key=repr))
    if isinstance(value, type):
        return 'class', value.__module__, value.__qualname__
    if hasattr(value, '__dict__'):
        cls = value.__class__
        return cls.__module__, cls.__qualname__, _freeze_option(dict(vars(value)))
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _make_build_key(cls: type[SchemaType], source: Any, kwargs: dict[str, Any]) \
        -> Optional[tuple]:
    """
    Returns the cache key for a schema build, identified by the schema class, the
    root schema source and the parsing settings. Returns `None` if the build is
    not cacheable (unidentifiable source or a context-dependent initialization).
    """
    if kwargs.get('global_maps') is not None or kwargs.get('parent') is not None:
        return None  # The build depends on an external context
    if kwargs.get('build') is False or kwargs.get('partial'):
        return None  # Incomplete schema objects are not cacheable

    source_token = _source_token(source, kwargs.get('base_url'))
    if source_token is None:
        return None
    settings_token = _freeze_option(dict(kwargs))
    return cls.__module__, cls.__qualname__, source_token, settings_token


def _schema_dependencies(schema: SchemaType) -> set[str]:
    """Returns the URLs of the resource closure of a built schema."""
    dependencies = set()
    seen = set()
    stack = [schema]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))

        url = getattr(getattr(item, 'source', None), 'url', None)
        if url:
            dependencies.add(url)

        includes = getattr(item, 'includes', None) or {}
        imports = getattr(item, 'imports', None) or {}
        stack.extend(includes.values())
        stack.extend(s for s in imports.values() if s is not None)
    return dependencies


class _BuildState:
    """Synchronization state for an in-progress schema build."""
    __slots__ = ('event', 'schema', 'error')

    def __init__(self) -> None:
        self.event = Event()
        self.schema: Optional[SchemaType] = None
        self.error: Optional[BaseException] = None


class _BuildCacheEntry:
    """A cached schema instance with its dependency closure."""
    __slots__ = ('schema', 'dependencies', 'fingerprints')

    def __init__(self, schema: SchemaType, dependencies: Iterable[str]) -> None:
        self.schema = schema
        self.dependencies = frozenset(dependencies)
        self.fingerprints: dict[str, Optional[str]] = {}
        for url in self.dependencies:
            path = _local_path(url)
            if path is not None:
                self.fingerprints[url] = _hash_file(path)

    def is_fresh(self) -> bool:
        """Checks that no local dependency has been updated since the build."""
        for url, fingerprint in self.fingerprints.items():
            path = _local_path(url)
            if path is None or _hash_file(path) != fingerprint:
                return False
        return True


class SchemaBuildCache:
    """
    A thread-safe cache for built schema instances, for reusing the build cost
    of schemas that are repeatedly validated against (e.g. same-named schemas
    provided by different vendors and validated in sequence by a backend).

    Cached entries are identified by the schema class, the root schema source
    and the parsing settings. Each entry tracks the closure of the resources
    the schema has been built from (the root schema and its imports/includes):
    when a local dependency is updated only the entries whose closure contains
    that dependency are invalidated, other cached schemas are kept.

    Failed builds are not cached and don't pollute the cache. Concurrent builds
    for the same key are coordinated so that a partially built schema object is
    never visible to other threads.

    :param maxsize: maximum number of cached schemas, evicting the least \
    recently used entries when exceeded. If `None` the cache is unbounded.
    """

    def __init__(self, maxsize: Optional[int] = 128) -> None:
        if maxsize is not None and maxsize <= 0:
            raise XMLSchemaValueError("maxsize must be a positive integer or None")
        self._maxsize = maxsize
        self._entries: OrderedDict[tuple, _BuildCacheEntry] = OrderedDict()
        self._dep_index: dict[str, set[tuple]] = {}
        self._building: dict[tuple, _BuildState] = {}
        self._lock = Lock()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def maxsize(self) -> Optional[int]:
        return self._maxsize

    def get_or_build(self, cls: type[SchemaType],
                     source: Union[SourceArgType, list[SourceArgType]],
                     builder: Optional[Callable[[], SchemaType]] = None,
                     **kwargs: Any) -> SchemaType:
        """
        Returns the cached schema instance for the given arguments, building and
        caching a new one on a cache miss or when a local dependency of a cached
        entry has been updated.

        :param cls: the schema class to use for the build.
        :param source: the schema source argument.
        :param builder: an optional custom callable for building the schema \
        instance. If not provided the schema is built with `cls(source, **kwargs)`.
        :param kwargs: parsing settings for the schema initialization.
        """
        key = _make_build_key(cls, source, kwargs)
        if key is None:
            # Not cacheable: build directly without touching the cache
            return builder() if builder is not None else cls(source, **kwargs)

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                state = None
                owner = False
            else:
                state = self._building.get(key)
                if state is None:
                    state = _BuildState()
                    self._building[key] = state
                    owner = True
                else:
                    owner = False

        if entry is not None:
            if entry.is_fresh():
                with self._lock:
                    if self._entries.get(key) is entry:
                        self._entries.move_to_end(key)
                return entry.schema

            # A local dependency has been updated: evict only this entry
            # (the closure related to the dependency) and rebuild.
            with self._lock:
                if self._entries.get(key) is entry:
                    self._evict(key)
            return self.get_or_build(cls, source, builder, **kwargs)

        if not owner:
            # Another thread is building the same schema: wait for it to
            # complete, so a partially built schema object is never visible.
            assert state is not None
            state.event.wait()
            if state.error is not None:
                raise state.error
            return cast(SchemaType, state.schema)

        try:
            schema = builder() if builder is not None else cls(source, **kwargs)
        except BaseException as exc:
            # A failed build is not cached and doesn't pollute the cache
            with self._lock:
                self._building.pop(key, None)
                state.error = exc
                state.event.set()
            raise

        entry = _BuildCacheEntry(schema, _schema_dependencies(schema))
        with self._lock:
            self._building.pop(key, None)
            self._store(key, entry)
            state.schema = schema
            state.event.set()
        return schema

    def invalidate(self, cls: type[SchemaType],
                   source: Union[SourceArgType, list[SourceArgType]],
                   **kwargs: Any) -> bool:
        """
        Invalidates the cached entry identified by the given build arguments.
        Returns `True` if an entry has been evicted.
        """
        key = _make_build_key(cls, source, kwargs)
        if key is None:
            return False
        with self._lock:
            if key not in self._entries:
                return False
            self._evict(key)
            return True

    def invalidate_dependency(self, url: str) -> int:
        """
        Invalidates all the cached entries whose dependency closure contains
        the given resource URL or path. Returns the number of evicted entries.
        """
        candidates = {url}
        try:
            candidates.add(normalize_url(url))
        except (TypeError, ValueError):
            pass

        with self._lock:
            keys: set[tuple] = set()
            for candidate in candidates:
                keys.update(self._dep_index.get(candidate, ()))
            for key in keys:
                self._evict(key)
            return len(keys)

    def clear(self) -> None:
        """Clears the cache. In-progress builds of other threads are not affected."""
        with self._lock:
            self._entries.clear()
            self._dep_index.clear()

    def _store(self, key: tuple, entry: _BuildCacheEntry) -> None:
        if key in self._entries:
            self._evict(key)
        self._entries[key] = entry
        for url in entry.dependencies:
            self._dep_index.setdefault(url, set()).add(key)
        if self._maxsize is not None:
            while len(self._entries) > self._maxsize:
                self._evict(next(iter(self._entries)))

    def _evict(self, key: tuple) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        for url in entry.dependencies:
            keys = self._dep_index.get(url)
            if keys is not None:
                keys.discard(key)
                if not keys:
                    del self._dep_index[url]
