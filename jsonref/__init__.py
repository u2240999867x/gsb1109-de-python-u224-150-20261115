import enum
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


class SiblingProps(enum.Enum):
    """
    Explicit rules for the properties sitting next to a ``$ref`` key.

    Vendors disagree about these "sibling" properties: some ignore them, others
    treat them as overrides of the referenced data. The rule must be chosen
    explicitly for every projection instead of being left to a library default.
    """

    IGNORE = "ignore"
    OVERRIDE = "override"


# String aliases for the rules, convenient to pass directly to the API.
REF_IGNORE_SIBLINGS = SiblingProps.IGNORE.value
REF_OVERRIDE_SIBLINGS = SiblingProps.OVERRIDE.value
REF_SIBLING_RULES = frozenset(rule.value for rule in SiblingProps)


def _normalize_sibling_rule(sibling_props, merge_props):
    """
    Resolve the explicitly selected sibling rule to one of the string values in
    :data:`REF_SIBLING_RULES`.

    The legacy boolean ``merge_props`` flag is only consulted when no explicit
    rule was given, so old callers keep their behavior while new callers state
    the rule outright.
    """
    if sibling_props is None:
        return (
            REF_OVERRIDE_SIBLINGS if merge_props else REF_IGNORE_SIBLINGS
        )
    if isinstance(sibling_props, SiblingProps):
        sibling_props = sibling_props.value
    if sibling_props not in REF_SIBLING_RULES:
        raise ValueError(
            "Invalid sibling property rule %r, expected one of %r"
            % (sibling_props, sorted(REF_SIBLING_RULES))
        )
    return sibling_props


def _pointer_for_path(path):
    """Render an in-document tuple of keys as a JSON Pointer fragment."""
    return "#" + "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


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
        "effective_rule",
        "effective_sources",
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
        sibling_props=None,
        _path=(),
        _store=None,
    ):
        if not isinstance(refobj.get("$ref"), str):
            raise ValueError("Not a valid json reference object: %s" % refobj)
        # Keep the reference object exactly as it appeared in the source
        # document, so re-projection under another rule reads untouched data.
        self.__reference__ = refobj
        self.base_uri = base_uri
        self.loader = loader or jsonloader
        self.jsonschema = jsonschema
        self.load_on_repr = load_on_repr
        self.merge_props = merge_props
        self.sibling_props = _normalize_sibling_rule(sibling_props, merge_props)
        self.path = _path
        self.store = _store  # Use the same object to be shared with children
        if self.store is None:
            self.store = URIDict()
        # The selected rule is known up front and visible without resolving
        # (which could trigger a remote load); detailed provenance is populated
        # lazily on resolution.
        self.effective_rule = self.sibling_props
        self.effective_sources = None

    @property
    def _ref_kwargs(self):
        return dict(
            base_uri=self.base_uri,
            loader=self.loader,
            jsonschema=self.jsonschema,
            load_on_repr=self.load_on_repr,
            merge_props=self.merge_props,
            sibling_props=self.sibling_props,
            path=self.path,
            store=self.store,
        )

    @property
    def full_uri(self):
        return urlparse.urljoin(self.base_uri, self.__reference__["$ref"])

    def callback(self):
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
        return self._apply_sibling_rule(result, uri, fragment)

    def _sibling_pointer(self, key):
        """JSON Pointer to a sibling key in the original reference object."""
        return _pointer_for_path(self.path + (key,))

    def _apply_sibling_rule(self, result, uri, fragment):
        """
        Apply the explicitly selected sibling rule to the resolved referent.

        Only the top level of an object referent can be overridden. Sibling
        mappings are never deep-merged into the referent's nested properties:
        a conflicting nested value is replaced wholesale. Provenance for every
        sibling key is recorded on the proxy before the result is returned.
        """
        rule = self.sibling_props
        siblings = {
            key: value
            for key, value in self.__reference__.items()
            if key != "$ref"
        }
        key_sources = [
            {
                "key": key,
                "pointer": self._sibling_pointer(key),
                "status": "ignored",
            }
            for key in siblings
        ]
        self.effective_rule = rule
        self.effective_sources = {
            "rule": rule,
            "reference_uri": self.full_uri,
            "reference_path": _pointer_for_path(self.path),
            "referent_uri": uri,
            "referent_pointer": fragment,
            "keys": key_sources,
        }

        if rule == SiblingProps.IGNORE.value or not siblings:
            return result

        # OVERRIDE: only object referents can carry sibling keys. A list or
        # scalar referent cannot be extended, so the siblings stay dropped and
        # their "ignored" status already explains why.
        if not isinstance(result, Mapping):
            return result

        merged = dict(result)
        for record in key_sources:
            key = record["key"]
            record["status"] = "override" if key in result else "added"
            merged[key] = siblings[key]
        return merged

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
    sibling_props=None,
    proxies=True,
    lazy_load=True,
):
    """
    Returns a deep copy of `obj` with all contained JSON reference objects
    replaced with :class:`JsonRef` instances.

    The input `obj` is never mutated and its reference objects stay intact;
    each call projects fresh output from the original structure, so switching
    rules never reads a document produced by a previous projection.

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
    :param merge_props: Deprecated boolean alias for :attr:`sibling_props`.
        Maps to the override rule when ``True`` and the ignore rule when
        ``False``. Prefer passing an explicit :class:`SiblingProps` rule.
    :param sibling_props: Explicit rule for the properties sitting next to
        ``$ref``: ``SiblingProps.IGNORE`` (``"ignore"``) discards them,
        ``SiblingProps.OVERRIDE`` (``"override"``) applies them on top of the
        referenced object at the top level. Nested mappings override wholesale
        and are never deep-merged. Defaults to the ignore rule. After
        resolution, :attr:`JsonRef.effective_sources` reports the rule and the
        origin of every sibling key.
    :param proxies: If `True`, references will be replaced with transparent
        proxy objects. Otherwise, they will be replaced directly with the
        referred data. (defaults to ``True``)
    :param lazy_load: When proxy objects are used, and this is `True`, the
        references will not be resolved until that section of the JSON
        document is accessed. (defaults to ``True``)

    """
    rule = _normalize_sibling_rule(sibling_props, merge_props)
    result = _replace_refs(
        obj,
        base_uri=base_uri,
        loader=loader,
        jsonschema=jsonschema,
        load_on_repr=load_on_repr,
        merge_props=merge_props,
        sibling_props=rule,
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
    sibling_props,
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
                sibling_props=sibling_props,
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
                sibling_props=sibling_props,
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
            sibling_props=sibling_props,
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
    sibling_props=None,
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
        sibling_props=sibling_props,
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
    sibling_props=None,
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
        sibling_props=sibling_props,
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
    sibling_props=None,
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
        sibling_props=sibling_props,
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
