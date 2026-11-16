import functools
import json
import warnings
from collections import namedtuple
from collections.abc import Mapping, MutableMapping, Sequence
from urllib import parse as urlparse
from urllib.parse import unquote
from urllib.request import urlopen

from . import proxytypes  # noqa: F401
from .proxytypes import LazyProxy
from .references import (
    Document,
    DocumentKey,  # noqa: F401 - re-exported for graph inspection
    DocumentStore,
    Reference,
    ReferenceGraph,
)

try:
    # If requests >=1.0 is available, we will use it
    import requests

    if not callable(requests.Response.json):
        requests = None
except ImportError:
    requests = None

__version__ = "1.1.0"


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
        "base_uri",
        "source_uri",
        "path",
        "call_path",
        "graph",
        "reference_info",
        "store",
        "full_uri",
        "target",
        "target_document",
        "target_uri",
        "reference",
        "chain",
        "all_chains",
        "loader",
        "jsonschema",
        "load_on_repr",
        "merge_props",
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
        _graph=None,
        _reference=None,
        _call_path=(),
        _parent_reference=None,
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
        self.call_path = _call_path
        self.store = _store  # Use the same object to be shared with children
        if self.store is None:
            self.store = DocumentStore()
        self.graph = _graph
        self.reference_info = _reference
        self.parent_reference = _parent_reference

    @property
    def _ref_kwargs(self):
        return dict(
            base_uri=self.base_uri,
            retrieval_uri=self.base_uri,
            loader=self.loader,
            jsonschema=self.jsonschema,
            load_on_repr=self.load_on_repr,
            merge_props=self.merge_props,
            path=self.path,
            store=self.store,
            graph=self.graph,
        )

    @property
    def full_uri(self):
        return urlparse.urljoin(self.base_uri, self.__reference__["$ref"])

    @property
    def source_uri(self):
        return self.base_uri

    @property
    def target(self):
        """The :class:`jsonref.references.Document` this reference points at."""
        if self.reference_info is not None:
            return self.reference_info.target
        return None

    @property
    def reference(self):
        """The :class:`jsonref.references.Reference` occurrence, if tracked."""
        return self.reference_info

    @property
    def target_uri(self):
        return self.full_uri

    @property
    def chain(self):
        """References followed from the root document to this occurrence."""
        if self.reference_info is not None:
            return self.reference_info.chain
        return ()

    def all_chains(self):
        """Every root-to-reference path for this occurrence."""
        if self.reference_info is not None:
            return self.reference_info.all_chains()
        return ()

    def callback(self):
        full_uri = self.full_uri
        uri, fragment = urlparse.urldefrag(full_uri)

        # Look up the target by retrieval URI.  The store maps a URI to the
        # canonical (retrieval_uri, base_uri) entry, so a document declaring an
        # $id can be shared without losing the base its contents resolve
        # against.
        key = None
        ref = self.__reference__["$ref"]
        source_document = self.reference_info.source if self.reference_info else None
        own_base = source_document.base_uri if source_document is not None else None
        if ref == "" or ref.startswith("#") or (uri and uri == own_base):
            # A reference to the current base (including fragment-only
            # references) resolves within the document that declares it.
            # Prefer its own resource even if another document declares the
            # same base URI.
            base_doc = self._local_document()
            key = source_document.key if source_document is not None else None
        elif self.store is not None and uri in self.store:
            base_doc = self.store[uri]
            key = self.store.canonical_key(uri)
            if self.graph is not None and self.reference_info is not None:
                target_document = self._document_for_uri(uri)
                if target_document is not None:
                    self.graph.resolve_reference(
                        self.reference_info, target_document
                    )
                    key = target_document.key
        else:
            # Remote ref
            try:
                base_doc = self.loader(uri)
            except Exception as e:
                raise self._error(
                    "%s: %s" % (e.__class__.__name__, str(e)), cause=e
                ) from e
            parent_doc = self.target_document
            base_doc = _replace_refs(
                base_doc,
                **{
                    **self._ref_kwargs,
                    "base_uri": uri,
                    "retrieval_uri": uri,
                    "recursing": False,
                    "document": parent_doc,
                    "parent_reference": self.reference_info,
                }
            )
            if self.graph is not None:
                key = self.store.canonical_key(uri)
                target_document = self.graph.documents.get(key)
                if target_document is None:
                    target_document = self.graph.find_document(uri)
                    if target_document is not None:
                        key = target_document.key
                if target_document is not None:
                    if self.reference_info is not None:
                        self.graph.resolve_reference(
                            self.reference_info, target_document
                        )
        result = self.resolve_pointer(base_doc, fragment)
        if result is self:
            raise self._error("Reference refers directly to itself.")
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
        if self.reference_info is not None:
            if self.graph is not None:
                target_document = self.graph.find_document(key)
                if target_document is not None:
                    self.graph.resolve_reference(
                        self.reference_info, target_document, result
                    )
        return result

    @property
    def target_document(self):
        if self.reference_info is not None:
            return self.reference_info.source
        return None

    def _local_document(self):
        """The document object containing this reference, if known."""
        reference = self.reference_info
        if reference is not None and reference.source is not None:
            document = reference.source
            if document.object is not None:
                return document.object
        try:
            return self.store[self.base_uri]
        except KeyError:
            return None

    def _document_for_uri(self, uri):
        if self.graph is None:
            return None
        document = self.graph.find_document(uri)
        if document is not None:
            return document
        try:
            key = self.store.canonical_key(uri)
        except KeyError:
            return None
        return self.graph.documents.get(key)

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


class URIDict(MutableMapping):
    """
    Dictionary which uses normalized URIs as keys.

    .. deprecated:: 1.2
        Use :class:`jsonref.references.DocumentStore` instead.  ``URIDict``
        remains available for backwards compatibility and does not track base
        URIs.
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
    return_graph=False,
    with_graph=False,
    resolve_graph=None,
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
        (defaults to global ``jsonloader``, a :class:`JsonLoader` instance)
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
    :param return_graph: When ``True``, return a ``(result, graph)`` tuple.
        ``graph`` is a :class:`jsonref.ReferenceGraph` describing every
        document resource and reference occurrence that was resolved.  The
        graph is complete when the requested result has no unresolved lazy
        references.
    :param with_graph: Alias for ``return_graph``.
    :param resolve_graph: When ``True``, resolve every reference before
        returning so the graph is complete, regardless of ``lazy_load``.

    """
    if with_graph:
        return_graph = True
    if resolve_graph is None:
        resolve_graph = return_graph

    base_uri, _ = urlparse.urldefrag(base_uri)
    graph = ReferenceGraph()
    graph.root_uri = base_uri
    store = DocumentStore()
    result = _replace_refs(
        obj,
        base_uri=base_uri,
        retrieval_uri=base_uri,
        loader=loader,
        jsonschema=jsonschema,
        load_on_repr=load_on_repr,
        merge_props=merge_props,
        store=store,
        path=(),
        recursing=False,
        graph=graph,
        document=None,
        parent_reference=None,
    )
    graph.result = result
    if not proxies:
        _walk_refs(result, lambda r: r.__subject__, replace=True)
    elif not lazy_load or resolve_graph:
        _walk_refs(result, lambda r: r.__subject__)
    if return_graph:
        return result, graph
    return result


def replace_refs_with_graph(obj, **kwargs):
    """Like :func:`replace_refs`, but always return ``(result, graph)``."""
    kwargs["return_graph"] = True
    return replace_refs(obj, **kwargs)


class Resolved(namedtuple("Resolved", "result graph")):
    """
    A resolved object together with its reference provenance graph.

    The tuple unpacks as ``result, graph = resolved``.  ``object``/``value``
    are aliases for ``result``, and ``documents``/``references`` proxy the
    corresponding graph attributes.
    """

    __slots__ = ()

    @property
    def object(self):
        return self.result

    @property
    def value(self):
        return self.result

    @property
    def documents(self):
        return self.graph.documents

    @property
    def references(self):
        return self.graph.references

    def to_dict(self):
        return {"result": self.result, "graph": self.graph.to_dict()}


def resolve(obj, **kwargs):
    """
    Resolve ``obj`` and return a :class:`Resolved` pair.

    This is equivalent to :func:`replace_refs` with ``return_graph=True``.  By
    default every reference is resolved so the graph is complete; pass
    ``resolve_graph=False`` to preserve lazy loading.
    """
    kwargs.pop("return_graph", None)
    kwargs.pop("with_graph", None)
    result, graph = replace_refs(obj, return_graph=True, **kwargs)
    return Resolved(result, graph)


def _replace_refs(
    obj,
    *,
    base_uri,
    retrieval_uri,
    loader,
    jsonschema,
    load_on_repr,
    merge_props,
    store,
    path,
    recursing,
    graph,
    document,
    parent_reference,
):
    base_uri, _fragment = urlparse.urldefrag(base_uri)
    if retrieval_uri is None:
        retrieval_uri = base_uri
    retrieval_uri, _ = urlparse.urldefrag(retrieval_uri)

    declared_id = None
    if jsonschema and isinstance(obj, Mapping):
        # id changed to $id in later jsonschema versions
        declared_id = obj.get("$id") or obj.get("id")
        if isinstance(declared_id, str):
            base_uri = urlparse.urljoin(base_uri, declared_id)
        else:
            declared_id = None

    if declared_id is not None and document is not None:
        if base_uri == document.base_uri:
            declared_id = None

    start_new_document = graph is not None and (not recursing or declared_id is not None)
    if start_new_document:
        parent_document = document
        if recursing:
            # A nested $id declares a resource within the current document.
            # It does not add another reference hop; it inherits the path of
            # the document that contains it.
            document = graph.document(
                retrieval_uri,
                base_uri,
                declared_id=declared_id,
                container=parent_document,
            )
        else:
            document = graph.document(
                retrieval_uri,
                base_uri,
                declared_id=declared_id,
                container=parent_document,
                retrieved_from=parent_document,
                via=parent_reference,
            )

    # First recursively iterate through our object, replacing children with JsonRefs
    if isinstance(obj, Mapping):
        obj = {
            k: _replace_refs(
                v,
                base_uri=base_uri,
                retrieval_uri=retrieval_uri,
                loader=loader,
                jsonschema=jsonschema,
                load_on_repr=load_on_repr,
                merge_props=merge_props,
                store=store,
                path=path + (k,),
                recursing=True,
                graph=graph,
                document=document,
                parent_reference=parent_reference,
            )
            for k, v in obj.items()
        }
    elif isinstance(obj, Sequence) and not isinstance(obj, str):
        obj = [
            _replace_refs(
                v,
                base_uri=base_uri,
                retrieval_uri=retrieval_uri,
                loader=loader,
                jsonschema=jsonschema,
                load_on_repr=load_on_repr,
                merge_props=merge_props,
                store=store,
                path=path + (i,),
                recursing=True,
                graph=graph,
                document=document,
                parent_reference=parent_reference,
            )
            for i, v in enumerate(obj)
        ]

    # If this object itself was a reference, replace it with a JsonRef
    if isinstance(obj, Mapping) and isinstance(obj.get("$ref"), str):
        full_uri = urlparse.urljoin(base_uri, obj["$ref"])
        reference_info = None
        if graph is not None:
            reference_info = graph.add_reference(
                obj,
                source=document,
                path=path,
                full_uri=full_uri,
                parent=parent_reference,
            )
        obj = JsonRef(
            obj,
            base_uri=base_uri,
            loader=loader,
            jsonschema=jsonschema,
            load_on_repr=load_on_repr,
            merge_props=merge_props,
            _path=path,
            _store=store,
            _graph=graph,
            _reference=reference_info,
            _parent_reference=parent_reference,
        )
        if reference_info is not None:
            target_document = graph.find_document(full_uri)
            if target_document is not None:
                graph.resolve_reference(reference_info, target_document)

    # Store the document with all references replaced in our cache.  The key
    # always contains the effective base URI, so a document whose id changes
    # the base cannot overwrite the entry for its retrieval URI.
    if start_new_document:
        document.object = obj
        primary = not recursing
        aliases = [base_uri]
        if primary:
            aliases.append(retrieval_uri)
        store.register(retrieval_uri, base_uri, obj, aliases=aliases, primary=primary)

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
    return_graph=False,
    with_graph=False,
    resolve_graph=None,
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
        return_graph=return_graph or with_graph,
        resolve_graph=resolve_graph,
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
    return_graph=False,
    with_graph=False,
    resolve_graph=None,
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
        return_graph=return_graph or with_graph,
        resolve_graph=resolve_graph,
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
    return_graph=False,
    with_graph=False,
    resolve_graph=None,
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
        return_graph=return_graph or with_graph,
        resolve_graph=resolve_graph,
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
