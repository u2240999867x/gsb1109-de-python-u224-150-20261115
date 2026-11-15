import functools
import json
import warnings
from collections.abc import Mapping, MutableMapping, Sequence
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

    __notproxied__ = ("__reference__",)

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
        )

    @property
    def full_uri(self):
        return urlparse.urljoin(self.base_uri, self.__reference__["$ref"])

    def callback(self):
        if getattr(self, "_resolving", False):
            # We are already in the middle of resolving this reference, which
            # means the references form a cycle with no concrete data in it
            # (e.g. {"a": {"$ref": "#/b"}, "b": {"$ref": "#/a"}})
            raise self._error("Circular reference detected.")
        self._resolving = True
        try:
            return self._callback()
        finally:
            self._resolving = False

    def _callback(self):
        uri, fragment = urlparse.urldefrag(self.full_uri)

        # If we already looked this up, return a reference to the same object
        if uri not in self.store:
            # Remote ref
            try:
                base_doc = self.loader(uri)
            except Exception as e:
                raise self._error(
                    "%s: %s" % (e.__class__.__name__, str(e)), cause=e
                ) from e
            base_doc = _replace_refs(
                base_doc, **{**self._ref_kwargs, "base_uri": uri, "recursing": False}
            )
        else:
            base_doc = self.store[uri]
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
        return result

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
    # Keep track of already processed items to prevent infinite recursion
    _processed = _processed or {}
    if type(obj) is JsonRef:
        oid = id(obj)
        if oid in _processed:
            return _processed[oid]
        r = func(obj)
        obj = r if replace else obj
        _processed[oid] = obj
    if isinstance(obj, Mapping):
        # Once references have been replaced with their referent data, cycles
        # in the data are direct container cycles. Keep track of containers we
        # have already descended into, so the structure stays a finite graph
        # of nodes and edges rather than being copied to infinite depth.
        if type(obj) is not JsonRef:
            oid = id(obj)
            if oid in _processed:
                return obj
            _processed[oid] = obj
        for k, v in obj.items():
            r = _walk_refs(v, func, replace=replace, _processed=_processed)
            if replace:
                obj[k] = r
    elif isinstance(obj, Sequence) and not isinstance(obj, str):
        if type(obj) is not JsonRef:
            oid = id(obj)
            if oid in _processed:
                return obj
            _processed[oid] = obj
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
        referred data. Documents with recursive references will produce
        recursive python data structures, preserving the cycles rather than
        copying data to infinite depth. (defaults to ``True``)
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
    original reference object they were created from. Cyclic data structures
    will have their cycles dumped as JSON reference objects.

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

    If `obj` contains cyclic data structures (e.g. created by using
    :func:`replace_refs` with ``proxies=False`` on a document with recursive
    references), the cycles will be dumped as JSON reference objects pointing
    at the first occurrence of the repeated node, keeping the output finite.

    :param obj: Object to serialize
    :param kwargs: Keyword arguments are the same as to :func:`json.dumps`

    """
    kwargs["cls"] = _ref_encoder_factory(kwargs.get("cls", json.JSONEncoder))
    return json.dumps(_cycles_to_refs(obj), **kwargs)


def _cycles_to_refs(obj, _path=(), _ancestors=None):
    """
    Returns a version of `obj` where any container which is its own ancestor
    (a cycle) is replaced by a JSON reference object pointing at the path of
    its first occurrence. Acyclic structures are returned untouched, and
    :class:`JsonRef` objects are left for the encoder to handle.

    """
    if _ancestors is None:
        _ancestors = {}
    if hasattr(obj, "__reference__"):
        # A JsonRef proxy. The encoder will dump the original reference
        # object, so there is nothing to descend into here.
        return obj
    if isinstance(obj, Mapping):
        oid = id(obj)
        if oid in _ancestors:
            return {"$ref": "#" + _format_pointer(_ancestors[oid])}
        _ancestors[oid] = _path
        try:
            changed = False
            items = []
            for k, v in obj.items():
                new_v = _cycles_to_refs(v, _path + (k,), _ancestors)
                changed = changed or new_v is not v
                items.append((k, new_v))
            return type(obj)(items) if changed else obj
        finally:
            del _ancestors[oid]
    if isinstance(obj, Sequence) and not isinstance(obj, str):
        oid = id(obj)
        if oid in _ancestors:
            return {"$ref": "#" + _format_pointer(_ancestors[oid])}
        _ancestors[oid] = _path
        try:
            changed = False
            items = []
            for i, v in enumerate(obj):
                new_v = _cycles_to_refs(v, _path + (i,), _ancestors)
                changed = changed or new_v is not v
                items.append(new_v)
            return type(obj)(items) if changed else obj
        finally:
            del _ancestors[oid]
    return obj


def _format_pointer(path):
    """
    Format a path of keys/indices as a JSON pointer URI fragment.

    """
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


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
