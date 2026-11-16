import functools
import json
import threading
import warnings
from collections.abc import Mapping, MutableMapping, Sequence
from enum import Enum
from urllib import parse as urlparse
from urllib.parse import unquote
from urllib.request import urlopen

from . import proxytypes  # noqa: F401
from .proxytypes import LazyProxy

try:
    # If requests >=1.0 is available, we will use it
    import requests

    if not callable(requests.Response.json):
        requests = None
except ImportError:
    requests = None

__version__ = "1.1.0"


class LoadState(str, Enum):
    """
    State of an offline document load.

    ``UNVISITED`` means the reference has not been dereferenced yet,
    ``LOADED`` means the document was loaded successfully, and ``FAILED``
    means an attempt was made and failed. A failed document is not cached, so
    a later access retries the loader (for example after the missing file is
    supplied).
    """

    UNVISITED = "unvisited"
    LOADED = "loaded"
    FAILED = "failed"

    # Common alternative spellings, kept as aliases for convenience.
    NOT_LOADED = "unvisited"
    NOT_VISITED = "unvisited"
    UNLOADED = "unvisited"
    PENDING = "unvisited"
    SUCCESS = "loaded"
    OK = "loaded"
    FAILURE = "failed"
    ERROR = "failed"

    def __str__(self):
        return self.value

    def __call__(self):
        return self


LoadStatus = LoadState
RefState = LoadState
DocumentState = LoadState
UNVISITED = LoadState.UNVISITED
LOADED = LoadState.LOADED
FAILED = LoadState.FAILED


class ReferencePaths(tuple):
    """Immutable collection of reference paths that share a document URI."""

    def __call__(self):
        return self


class _StateSet(set):
    """Set of URIs in one load state, also callable to test a single URI."""

    def __init__(self, tracker, state):
        self.tracker = tracker
        self.state = state
        super().__init__(tracker.uris_in_state(state))

    def __call__(self, uri):
        return self.tracker.state_for(uri) is self.state


class _StateView:
    """Callable view over all known URIs and their load states."""

    def __init__(self, tracker):
        self.tracker = tracker

    def __call__(self, uri):
        return self.tracker.state_for(uri)

    def __getitem__(self, uri):
        return self.tracker.state_for(uri)

    def items(self):
        return self.tracker.states().items()

    def keys(self):
        return self.tracker.states().keys()

    def values(self):
        return self.tracker.states().values()

    def __iter__(self):
        return iter(self.tracker.states())

    def __len__(self):
        return len(self.tracker.states())

    def __repr__(self):
        return repr(self.tracker.states())


def _normalize_uri(uri):
    return urlparse.urlsplit(uri).geturl()


class LoadTracker:
    """
    Thread-safe bookkeeping for documents loaded by :class:`JsonRef`.

    A tracker is shared by every reference created from a single
    :func:`replace_refs` call. State is kept per document URI, so two fields
    pointing at the same document observe the same state. Only successful
    loads are cached by :class:`JsonRef`; a failure leaves no document behind
    and the next access (or :meth:`JsonRef.retry`) tries the loader again.
    """

    def __init__(self):
        self._states = {}
        self._errors = {}
        self._paths = {}
        self.lock = threading.RLock()

    def state_for(self, uri):
        with self.lock:
            return self._states.get(_normalize_uri(uri), LoadState.UNVISITED)

    def error_for(self, uri):
        with self.lock:
            return self._errors.get(_normalize_uri(uri))

    def paths_for(self, uri):
        with self.lock:
            return ReferencePaths(self._paths.get(_normalize_uri(uri), ()))

    def add_path(self, uri, path):
        with self.lock:
            uri = _normalize_uri(uri)
            paths = self._paths.setdefault(uri, [])
            if path not in paths:
                paths.append(path)

    def mark_loaded(self, uri):
        with self.lock:
            uri = _normalize_uri(uri)
            self._states[uri] = LoadState.LOADED
            self._errors.pop(uri, None)

    def mark_failed(self, uri, error=None):
        with self.lock:
            uri = _normalize_uri(uri)
            self._states[uri] = LoadState.FAILED
            if error is not None:
                self._errors[uri] = error

    def reset(self, uri):
        with self.lock:
            uri = _normalize_uri(uri)
            self._states[uri] = LoadState.UNVISITED
            self._errors.pop(uri, None)

    def uris_in_state(self, state):
        with self.lock:
            uri_set = set(self._paths) | set(self._states)
            return {
                uri
                for uri in uri_set
                if self._states.get(uri, LoadState.UNVISITED) is state
            }

    def states(self):
        with self.lock:
            return {
                uri: self._states.get(uri, LoadState.UNVISITED)
                for uri in set(self._paths) | set(self._states)
            }

    def state_map(self):
        return _StateView(self)

    def unvisited_uris(self, uri=None):
        result = _StateSet(self, LoadState.UNVISITED)
        return result(uri) if uri is not None else result

    def loaded_uris(self, uri=None):
        result = _StateSet(self, LoadState.LOADED)
        return result(uri) if uri is not None else result

    def failed_uris(self, uri=None):
        result = _StateSet(self, LoadState.FAILED)
        return result(uri) if uri is not None else result

    def failures(self):
        with self.lock:
            return dict(self._errors)

    def all_paths(self):
        with self.lock:
            return {
                uri: ReferencePaths(paths) for uri, paths in self._paths.items()
            }

    # Common aliases, kept intentionally small and predictable.
    def state(self, uri):
        return self.state_for(uri)

    def error(self, uri):
        return self.error_for(uri)

    def paths(self, uri):
        return self.paths_for(uri)

    def affected_paths(self, uri):
        return self.paths_for(uri)


def _loader_tracker(loader):
    tracker = getattr(loader, "tracker", None)
    return tracker if isinstance(tracker, LoadTracker) else None


class JsonRefError(Exception):
    def __init__(self, message, reference, uri="", base_uri="", path=(), cause=None):
        self.message = message
        self.reference = reference
        self.uri = uri
        self.base_uri = base_uri
        self.path = path
        self.cause = self.__cause__ = cause

    def __repr__(self):
        return "<%s: %r>" % (self.__class__.__name__, self.message)

    def __str__(self):
        return str(self.message)


class JsonRef(LazyProxy):
    """
    A lazy loading proxy to the dereferenced data pointed to by a JSON
    Reference object.

    """

    __notproxied__ = (
        "__reference__",
        "tracker",
        "document_uri",
        "load_state",
        "load_status",
        "document_state",
        "state",
        "status",
        "load_error",
        "error",
        "affected_paths",
        "paths",
        "is_loaded",
        "is_failed",
        "is_unvisited",
        "loaded",
        "failed",
        "unvisited",
        "has_failed",
        "retry",
        "retry_failed",
        "reload",
        "_state_override",
        "_error_override",
    )

    @classmethod
    def replace_refs(
        cls, obj, base_uri="", loader=None, jsonschema=False, load_on_repr=True
    ):
        """
        .. deprecated:: 0.4
            Use :func:`replace_refs` instead.

        Returns a deep copy of `obj` with all contained JSON reference objects
        replaced with :class:`JsonRef` instances.

        :param obj: If this is a JSON reference object, a :class:`JsonRef`
            instance will be created. If `obj` is not a JSON reference object,
            a deep copy of it will be created with all contained JSON
            reference objects replaced by :class:`JsonRef` instances
        :param base_uri: URI to resolve relative references against
        :param loader: Callable that takes a URI and returns the parsed JSON
            (defaults to global ``jsonloader``)
        :param jsonschema: Flag to turn on `JSON Schema mode
            <http://json-schema.org/latest/json-schema-core.html#anchor25>`_.
            'id' keyword changes the `base_uri` for references contained within
            the object
        :param load_on_repr: If set to ``False``, :func:`repr` call on a
            :class:`JsonRef` object will not cause the reference to be loaded
            if it hasn't already. (defaults to ``True``)

        """
        return replace_refs(
            obj,
            base_uri=base_uri,
            loader=loader,
            jsonschema=jsonschema,
            load_on_repr=load_on_repr,
        )

    def __init__(
        self,
        refobj,
        base_uri="",
        loader=None,
        jsonschema=False,
        load_on_repr=True,
        merge_props=False,
        _path=(),
        _store=None,
        _tracker=None,
    ):
        if not isinstance(refobj.get("$ref"), str):
            raise ValueError("Not a valid json reference object: %s" % refobj)
        self.__reference__ = refobj
        self.base_uri = base_uri
        self.loader = loader or jsonloader
        self.jsonschema = jsonschema
        self.load_on_repr = load_on_repr
        self.merge_props = merge_props
        self.path = _path
        self.store = _store  # Use the same object to be shared with children
        if self.store is None:
            self.store = URIDict()
        self.tracker = _tracker  # Shared state for all refs in a document
        if self.tracker is None:
            self.tracker = _loader_tracker(self.loader)
        if self.tracker is None:
            self.tracker = LoadTracker()
        self.tracker.add_path(self.document_uri, self.path)

    @property
    def _ref_kwargs(self):
        return dict(
            base_uri=self.base_uri,
            loader=self.loader,
            jsonschema=self.jsonschema,
            load_on_repr=self.load_on_repr,
            merge_props=self.merge_props,
            path=self.path,
            store=self.store,
            tracker=self.tracker,
        )

    @property
    def full_uri(self):
        return urlparse.urljoin(self.base_uri, self.__reference__["$ref"])

    @property
    def document_uri(self):
        """The URI of the document this reference points at (without fragment)."""
        return urlparse.urldefrag(self.full_uri)[0]

    def callback(self):
        uri, fragment = urlparse.urldefrag(self.full_uri)
        self.tracker.add_path(uri, self.path)
        remote = _normalize_uri(uri) != _normalize_uri(self.base_uri)

        with self.tracker.lock:
            # If we already looked this up, return a reference to the same
            # object. A failed remote document is treated as a cache miss so a
            # later retry can pick up a document that arrives after the fact.
            document_failed = self.tracker.state_for(uri) is LoadState.FAILED
            loaded = uri in self.store and (not remote or not document_failed)
            if loaded:
                base_doc = self.store[uri]
            else:
                existing = set(self.store.store)
                try:
                    base_doc = self.loader(uri)
                    base_doc = _replace_refs(
                        base_doc,
                        **{**self._ref_kwargs, "base_uri": uri, "recursing": False},
                    )
                except Exception as e:
                    # Discard documents added by this load attempt so a
                    # failure cannot publish a half-parsed document, while
                    # documents loaded earlier remain untouched.
                    for key in set(self.store.store) - existing:
                        del self.store.store[key]
                    error = self._as_error(e)
                    self.tracker.mark_failed(uri, error)
                    raise error from e
            # The document itself is available; a later pointer/self-reference
            # problem is specific to this reference and must not mark the
            # whole document as failed.
            self.tracker.mark_loaded(uri)
            try:
                result = self.resolve_pointer(base_doc, fragment)
                if result is self:
                    raise self._error("Reference refers directly to itself.")
            except Exception as e:
                # A bad pointer/self reference is an error of this reference,
                # not a failure to load the document. Keep the document state
                # intact so sibling references are unaffected.
                error = self._as_error(e)
                self._state_override = LoadState.FAILED
                self._error_override = error
                raise error from e
        try:
            if hasattr(result, "__subject__"):
                result = result.__subject__
            if (
                self.merge_props
                and isinstance(result, Mapping)
                and len(self.__reference__) > 1
            ):
                result = {
                    **result,
                    **{k: v for k, v in self.__reference__.items() if k != "$ref"},
                }
        except Exception as e:
            error = self._as_error(e)
            self._state_override = LoadState.FAILED
            self._error_override = error
            raise error from e
        self._state_override = None
        self._error_override = None
        return result

    @property
    def load_state(self):
        # A per-reference pointer/self-reference failure takes precedence over
        # the shared document state: the document itself may be perfectly
        # fine for other references.
        if object.__getattribute__(self, "__dict__").get("_error_override"):
            return LoadState.FAILED
        return self.tracker.state_for(self.document_uri)

    @property
    def state(self):
        return self.load_state

    @property
    def load_status(self):
        return self.load_state

    @property
    def document_state(self):
        return self.load_state

    @property
    def status(self):
        return self.load_state

    @property
    def load_error(self):
        override = object.__getattribute__(self, "__dict__").get("_error_override")
        if override is not None:
            return override
        return self.tracker.error_for(self.document_uri)

    @property
    def error(self):
        return self.load_error

    @property
    def affected_paths(self):
        """Paths of all references in this document that share this URI."""
        return self.tracker.paths_for(self.document_uri)

    @property
    def paths(self):
        return self.affected_paths

    @property
    def is_loaded(self):
        return self.load_state is LoadState.LOADED

    @property
    def loaded(self):
        return self.is_loaded

    @property
    def is_failed(self):
        return self.load_state is LoadState.FAILED

    @property
    def failed(self):
        return self.is_failed

    @property
    def has_failed(self):
        return self.is_failed

    @property
    def is_unvisited(self):
        return self.load_state is LoadState.UNVISITED

    @property
    def unvisited(self):
        return self.is_unvisited

    def retry(self):
        """
        Retry loading this reference after a failure.

        Returns the newly resolved subject. Raises :class:`JsonRefError` if
        the retry also fails. Successful results are cached as usual.
        """
        return self._resolve(force=True)

    def retry_failed(self):
        """Alias for :meth:`retry`, only meaningful after a failure."""
        return self._resolve(force=self.load_state is LoadState.FAILED)

    def reload(self):
        """Alias for :meth:`retry`, re-resolving this reference."""
        return self.retry()

    def _resolve(self, force=False):
        if force:
            attributes = object.__getattribute__(self, "__dict__")
            if attributes.get("_error_override") is not None:
                # A per-reference pointer/self-reference error did not corrupt
                # the shared document; keep its cache and state intact.
                attributes.pop("_error_override", None)
                attributes.pop("_state_override", None)
            else:
                uri = self.document_uri
                self.tracker.reset(uri)
                # Fragment-only references reuse the root document already in
                # the store; only remote documents should be discarded and
                # reloaded.
                if uri and _normalize_uri(uri) != _normalize_uri(self.base_uri):
                    if uri in self.store:
                        del self.store[uri]
            attributes.pop("cache", None)
        return self.__subject__

    def resolve_pointer(self, document, pointer):
        """
        Resolve a json pointer ``pointer`` within the referenced ``document``.

        :argument document: the referent document
        :argument str pointer: a json pointer URI fragment to resolve within it

        """
        parts = unquote(pointer.lstrip("/")).split("/") if pointer else []

        for part in parts:
            part = part.replace("~1", "/").replace("~0", "~")

            if isinstance(document, Sequence):
                # Try to turn an array index to an int
                try:
                    part = int(part)
                except ValueError:
                    pass
            # If a reference points inside itself, it must mean inside reference object, not the referent data
            if document is self:
                document = self.__reference__
            try:
                document = document[part]
            except (TypeError, LookupError) as e:
                raise self._error(
                    "Unresolvable JSON pointer: %r" % pointer, cause=e
                ) from e
        return document

    def _as_error(self, exc):
        if isinstance(exc, JsonRefError):
            return exc
        return self._error("%s: %s" % (exc.__class__.__name__, str(exc)), cause=exc)

    def _error(self, message, cause=None):
        message = "Error while resolving `{}`: {}".format(self.full_uri, message)
        return JsonRefError(
            message,
            self.__reference__,
            uri=self.full_uri,
            base_uri=self.base_uri,
            path=self.path,
            cause=cause,
        )

    def __repr__(self):
        if hasattr(self, "cache") or self.load_on_repr:
            return repr(self.__subject__)
        return "JsonRef(%r)" % self.__reference__


class JsonLoader:
    """
    Callable loader that keeps per-URI load state and a successful-load cache.

    ``JsonLoader`` is a drop-in replacement for :func:`jsonloader`: calling
    it with a URI returns the parsed document. In addition it records whether
    each document is unvisited, loaded, or failed, retries failed documents on
    the next call (so a file that arrives later is picked up), and exposes
    the reference paths affected by each document.

    :param loader: The underlying callable to use (defaults to
        :func:`jsonloader`)
    """

    def __init__(self, loader=None, **kwargs):
        self.loader = loader or jsonloader
        self.kwargs = kwargs
        self.tracker = LoadTracker()
        self._cache = {}
        self.lock = threading.RLock()

    def __call__(self, uri):
        uri = _normalize_uri(uri)
        with self.lock:
            if uri in self._cache:
                self.tracker.mark_loaded(uri)
                return self._cache[uri]
            try:
                result = self.loader(uri, **self.kwargs)
            except Exception as e:
                self.tracker.mark_failed(uri, e)
                raise
            self._cache[uri] = result
            self.tracker.mark_loaded(uri)
            return result

    def state(self, uri=None):
        if uri is None:
            return self.tracker.state_map()
        return self.tracker.state_for(uri)

    def status(self, uri=None):
        return self.state(uri)

    def error(self, uri=None):
        if uri is None:
            return self.tracker.failures()
        return self.tracker.error_for(uri)

    def affected_paths(self, uri=None):
        if uri is None:
            return self.tracker.all_paths()
        return self.tracker.paths_for(uri)

    def add_path(self, uri, path):
        self.tracker.add_path(uri, path)

    def retry(self, uri):
        """Forget a failed document and load it again."""
        uri = _normalize_uri(uri)
        with self.lock:
            self._cache.pop(uri, None)
            self.tracker.reset(uri)
            return self(uri)

    def clear(self):
        """Forget all cached documents and reset state."""
        with self.lock:
            self._cache.clear()
            self.tracker = LoadTracker()

    def paths(self, uri=None):
        return self.affected_paths(uri)

    def load_state(self, uri=None):
        return self.state(uri)

    @property
    def states(self):
        return self.tracker.state_map()

    @property
    def failures(self):
        return self.tracker.failures()

    @property
    def all_paths(self):
        return self.tracker.all_paths()

    @property
    def unvisited_uris(self):
        return self.tracker.unvisited_uris()

    @property
    def loaded_uris(self):
        return self.tracker.loaded_uris()

    @property
    def failed_uris(self):
        return self.tracker.failed_uris()

    def is_loaded(self, uri):
        return self.tracker.state_for(uri) is LoadState.LOADED

    def is_failed(self, uri):
        return self.tracker.state_for(uri) is LoadState.FAILED

    def is_unvisited(self, uri):
        return self.tracker.state_for(uri) is LoadState.UNVISITED


class URIDict(MutableMapping):
    """
    Dictionary which uses normalized URIs as keys.
    """

    def normalize(self, uri):
        return urlparse.urlsplit(uri).geturl()

    def __init__(self, *args, **kwargs):
        self.store = dict()
        self.store.update(*args, **kwargs)

    def __getitem__(self, uri):
        return self.store[self.normalize(uri)]

    def __setitem__(self, uri, value):
        self.store[self.normalize(uri)] = value

    def __delitem__(self, uri):
        del self.store[self.normalize(uri)]

    def __iter__(self):
        return iter(self.store)

    def __len__(self):
        return len(self.store)

    def __repr__(self):
        return repr(self.store)


def jsonloader(uri, **kwargs):
    """
    Provides a callable which takes a URI, and returns the loaded JSON referred
    to by that URI. Uses :mod:`requests` if available for HTTP URIs, and falls
    back to :mod:`urllib`.
    """
    scheme = urlparse.urlsplit(uri).scheme

    if scheme in ["http", "https"] and requests:
        # Prefer requests, it has better encoding detection
        resp = requests.get(uri)
        # If the http server doesn't respond normally then raise exception
        # e.g. 404, 500 error
        resp.raise_for_status()
        try:
            result = resp.json(**kwargs)
        except TypeError:
            warnings.warn("requests >=1.2 required for custom kwargs to json.loads")
            result = resp.json()
    else:
        # Otherwise, pass off to urllib and assume utf-8
        with urlopen(uri) as content:
            result = json.loads(content.read().decode("utf-8"), **kwargs)

    return result


def _walk_refs(obj, func, replace=False, _processed=None):
    # Keep track of already processed items to prevent recursion
    _processed = _processed or {}
    if type(obj) is JsonRef:
        oid = id(obj)
        if oid in _processed:
            return _processed[oid]
        r = func(obj)
        obj = r if replace else obj
        _processed[oid] = obj
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            r = _walk_refs(v, func, replace=replace, _processed=_processed)
            if replace:
                obj[k] = r
    elif isinstance(obj, Sequence) and not isinstance(obj, str):
        for i, v in enumerate(obj):
            r = _walk_refs(v, func, replace=replace, _processed=_processed)
            if replace:
                obj[i] = r
    return obj


def replace_refs(
    obj,
    base_uri="",
    loader=jsonloader,
    jsonschema=False,
    load_on_repr=True,
    merge_props=False,
    proxies=True,
    lazy_load=True,
):
    """
    Returns a deep copy of `obj` with all contained JSON reference objects
    replaced with :class:`JsonRef` instances.

    :param obj: If this is a JSON reference object, a :class:`JsonRef`
        instance will be created. If `obj` is not a JSON reference object,
        a deep copy of it will be created with all contained JSON
        reference objects replaced by :class:`JsonRef` instances
    :param base_uri: URI to resolve relative references against
    :param loader: Callable that takes a URI and returns the parsed JSON
        (defaults to global ``jsonloader``). A :class:`JsonLoader` also keeps
        per-document load state that can be queried by URI.
    :param jsonschema: Flag to turn on `JSON Schema mode
        <http://json-schema.org/latest/json-schema-core.html#anchor25>`_.
        'id' or '$id' keyword changes the `base_uri` for references contained
        within the object
    :param load_on_repr: If set to ``False``, :func:`repr` call on a
        :class:`JsonRef` object will not cause the reference to be loaded
        if it hasn't already. (defaults to ``True``)
    :param merge_props: When ``True``, JSON reference objects that
        have extra keys other than '$ref' in them will be merged into the
        document resolved by the reference (if it is a dictionary.) NOTE: This
        is not part of the JSON Reference spec, and may not behave the same as
        other libraries.
    :param proxies: If `True`, references will be replaced with transparent
        proxy objects. Otherwise, they will be replaced directly with the
        referred data. (defaults to ``True``)
    :param lazy_load: When proxy objects are used, and this is `True`, the
        references will not be resolved until that section of the JSON
        document is accessed. (defaults to ``True``)

    """
    result = _replace_refs(
        obj,
        base_uri=base_uri,
        loader=loader,
        jsonschema=jsonschema,
        load_on_repr=load_on_repr,
        merge_props=merge_props,
        store=URIDict(),
        path=(),
        recursing=False,
        tracker=_loader_tracker(loader) or LoadTracker(),
    )
    if not proxies:
        _walk_refs(result, lambda r: r.__subject__, replace=True)
    elif not lazy_load:
        _walk_refs(result, lambda r: r.__subject__)
    return result


def _replace_refs(
    obj,
    *,
    base_uri,
    loader,
    jsonschema,
    load_on_repr,
    merge_props,
    store,
    path,
    recursing,
    tracker,
):
    base_uri, frag = urlparse.urldefrag(base_uri)
    store_uri = None  # If this does not get set, we won't store the result
    if not frag and not recursing:
        store_uri = base_uri
    if jsonschema and isinstance(obj, Mapping):
        # id changed to $id in later jsonschema versions
        id_ = obj.get("$id") or obj.get("id")
        if isinstance(id_, str):
            base_uri = urlparse.urljoin(base_uri, id_)
            store_uri = base_uri

    # First recursively iterate through our object, replacing children with JsonRefs
    if isinstance(obj, Mapping):
        obj = {
            k: _replace_refs(
                v,
                base_uri=base_uri,
                loader=loader,
                jsonschema=jsonschema,
                load_on_repr=load_on_repr,
                merge_props=merge_props,
                store=store,
                path=path + (k,),
                recursing=True,
                tracker=tracker,
            )
            for k, v in obj.items()
        }
    elif isinstance(obj, Sequence) and not isinstance(obj, str):
        obj = [
            _replace_refs(
                v,
                base_uri=base_uri,
                loader=loader,
                jsonschema=jsonschema,
                load_on_repr=load_on_repr,
                merge_props=merge_props,
                store=store,
                path=path + (i,),
                recursing=True,
                tracker=tracker,
            )
            for i, v in enumerate(obj)
        ]

    # If this object itself was a reference, replace it with a JsonRef
    if isinstance(obj, Mapping) and isinstance(obj.get("$ref"), str):
        obj = JsonRef(
            obj,
            base_uri=base_uri,
            loader=loader,
            jsonschema=jsonschema,
            load_on_repr=load_on_repr,
            merge_props=merge_props,
            _path=path,
            _store=store,
            _tracker=tracker,
        )

    # Store the document with all references replaced in our cache
    if store_uri is not None:
        store[store_uri] = obj

    return obj


def load(
    fp,
    base_uri="",
    loader=None,
    jsonschema=False,
    load_on_repr=True,
    merge_props=False,
    proxies=True,
    lazy_load=True,
    **kwargs,
):
    """
    Drop in replacement for :func:`json.load`, where JSON references are
    proxied to their referent data.

    :param fp: File-like object containing JSON document
    :param **kwargs: This function takes any of the keyword arguments from
        :func:`replace_refs`. Any other keyword arguments will be passed to
        :func:`json.load`

    """

    if loader is None:
        loader = functools.partial(jsonloader, **kwargs)

    return replace_refs(
        json.load(fp, **kwargs),
        base_uri=base_uri,
        loader=loader,
        jsonschema=jsonschema,
        load_on_repr=load_on_repr,
        merge_props=merge_props,
        proxies=proxies,
        lazy_load=lazy_load,
    )


def loads(
    s,
    base_uri="",
    loader=None,
    jsonschema=False,
    load_on_repr=True,
    merge_props=False,
    proxies=True,
    lazy_load=True,
    **kwargs,
):
    """
    Drop in replacement for :func:`json.loads`, where JSON references are
    proxied to their referent data.

    :param s: String containing JSON document
    :param **kwargs: This function takes any of the keyword arguments from
        :func:`replace_refs`. Any other keyword arguments will be passed to
        :func:`json.loads`

    """

    if loader is None:
        loader = functools.partial(jsonloader, **kwargs)

    return replace_refs(
        json.loads(s, **kwargs),
        base_uri=base_uri,
        loader=loader,
        jsonschema=jsonschema,
        load_on_repr=load_on_repr,
        merge_props=merge_props,
        proxies=proxies,
        lazy_load=lazy_load,
    )


def load_uri(
    uri,
    base_uri=None,
    loader=None,
    jsonschema=False,
    load_on_repr=True,
    merge_props=False,
    proxies=True,
    lazy_load=True,
):
    """
    Load JSON data from ``uri`` with JSON references proxied to their referent
    data.

    :param uri: URI to fetch the JSON from
    :param **kwargs: This function takes any of the keyword arguments from
        :func:`replace_refs`

    """

    if loader is None:
        loader = jsonloader
    if base_uri is None:
        base_uri = uri

    return replace_refs(
        loader(uri),
        base_uri=base_uri,
        loader=loader,
        jsonschema=jsonschema,
        load_on_repr=load_on_repr,
        merge_props=merge_props,
        proxies=proxies,
        lazy_load=lazy_load,
    )


def dump(obj, fp, **kwargs):
    """
    Serialize `obj`, which may contain :class:`JsonRef` objects, as a JSON
    formatted stream to file-like `fp`. `JsonRef` objects will be dumped as the
    original reference object they were created from.

    :param obj: Object to serialize
    :param fp: File-like to output JSON string
    :param kwargs: Keyword arguments are the same as to :func:`json.dump`

    """
    # Strangely, json.dumps does not use the custom serialization from our
    # encoder on python 2.7+. Instead, just write json.dumps output to a file.
    fp.write(dumps(obj, **kwargs))


def dumps(obj, **kwargs):
    """
    Serialize `obj`, which may contain :class:`JsonRef` objects, to a JSON
    formatted string. `JsonRef` objects will be dumped as the original
    reference object they were created from.

    :param obj: Object to serialize
    :param kwargs: Keyword arguments are the same as to :func:`json.dumps`

    """
    kwargs["cls"] = _ref_encoder_factory(kwargs.get("cls", json.JSONEncoder))
    return json.dumps(obj, **kwargs)


def _ref_encoder_factory(cls):
    class JSONRefEncoder(cls):
        def default(self, o):
            if hasattr(o, "__reference__"):
                return o.__reference__
            return super(JSONRefEncoder, cls).default(o)

        # Python 2.6 doesn't work with the default method
        def _iterencode(self, o, *args, **kwargs):
            if hasattr(o, "__reference__"):
                o = o.__reference__
            return super(JSONRefEncoder, self)._iterencode(o, *args, **kwargs)

        # Pypy doesn't work with either of the other methods
        def _encode(self, o, *args, **kwargs):
            if hasattr(o, "__reference__"):
                o = o.__reference__
            return super(JSONRefEncoder, self)._encode(o, *args, **kwargs)

    return JSONRefEncoder
