import functools
import json
import re
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

# A JSON pointer array index is either "0" or a non-zero digit followed by more
# digits.  In particular, values accepted by ``int()`` such as "-1", "+1" and
# "01" are *not* valid JSON pointer array indices.
_ARRAY_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
# "~" may only appear as part of the "~0" or "~1" escapes.
_INVALID_POINTER_ESCAPE_RE = re.compile(r"~(?![01])")
# Every "%" in a URI fragment must introduce two hexadecimal digits.
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class PointerSteps(tuple):
    """
    An ordered tuple of pointer steps.

    Steps are strings for object member names and integers for array indices.
    Comparing with a list is supported so callers can use either sequence type.
    """

    __slots__ = ()

    def __new__(cls, steps=()):
        return super(PointerSteps, cls).__new__(cls, steps)

    def __eq__(self, other):
        if isinstance(other, list):
            return list(self) == other
        if isinstance(other, tuple):
            return tuple(self) == tuple(other)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = tuple.__hash__


class PointerParseError(ValueError):
    """Raised when a JSON pointer URI fragment is syntactically invalid."""

    def __init__(
        self,
        message,
        pointer,
        token_index=None,
        token=None,
        char_index=None,
        pointer_tokens=(),
    ):
        super().__init__(message)
        self.message = message
        self.pointer = pointer
        self.token_index = token_index
        self.token = token
        self.char_index = char_index
        self.pointer_tokens = tuple(pointer_tokens)

    @property
    def step_index(self):
        return self.token_index

    @property
    def step(self):
        return self.token

    @property
    def tokens(self):
        return self.pointer_tokens


class PointerResolutionError(ValueError):
    """Raised when a syntactically valid pointer cannot be resolved."""

    def __init__(
        self,
        message,
        pointer,
        pointer_tokens=(),
        steps=(),
        step=None,
        step_index=None,
        char_index=None,
        cause=None,
    ):
        super().__init__(message)
        self.message = message
        self.pointer = pointer
        self.pointer_tokens = tuple(pointer_tokens)
        self.steps = PointerSteps(steps)
        self.step = step
        self.step_index = step_index
        self.char_index = char_index
        self.cause = self.__cause__ = cause

    @property
    def tokens(self):
        return self.pointer_tokens


def parse_pointer(pointer):
    """
    Parse a JSON pointer (a URI fragment without the leading ``#``) into a
    :class:`PointerSteps` of decoded string tokens.

    A URI fragment is percent-decoded exactly once and only then split on
    ``/``; this matches RFC 6901's URI fragment representation, where the
    fragment is decoded before the pointer is parsed.  Each decoded token is
    then unescaped with ``~1`` -> ``/`` and ``~0`` -> ``~``.  Percent escapes
    are deliberately not decoded a second time: doing so would turn text such
    as ``%252F`` into ``/`` and silently resolve to a different member.

    Empty member names are preserved: the pointer ``/`` has a single token
    ``""``, and ``//foo`` starts with an empty token.  Array-index conversion
    is intentionally left to the resolver, because the same token is a string
    member name when applied to an object and an index when applied to an
    array.

    :raises PointerParseError: if an escape sequence or percent escape is
        invalid.
    """
    if pointer is None:
        return ()
    if not isinstance(pointer, str):
        raise PointerParseError(
            "JSON pointer must be a string, not %s" % type(pointer).__name__,
            pointer,
        )
    if pointer.startswith("#"):
        # Also accept a complete URI fragment (with its '#' delimiter).
        pointer = pointer[1:]
    if not pointer:
        return ()

    invalid = _INVALID_PERCENT_ESCAPE_RE.search(pointer)
    if invalid is not None:
        # Attribute the failure to the token containing the '%' so callers can
        # identify both the step and the exact character.
        has_leading_slash = pointer.startswith("/")
        raw_tokens = pointer[1:].split("/") if has_leading_slash else pointer.split("/")
        token_index = 0
        consumed = 1 if has_leading_slash else 0
        for index, raw_token in enumerate(raw_tokens):
            if consumed <= invalid.start() <= consumed + len(raw_token):
                token_index = index
                break
            consumed += len(raw_token) + 1
        raise PointerParseError(
            "invalid percent escape in JSON pointer %r at character %d"
            % (pointer, invalid.start()),
            pointer,
            token_index=token_index,
            token=raw_tokens[token_index] if raw_tokens else None,
            char_index=invalid.start(),
        )

    try:
        decoded = unquote(pointer, errors="strict")
    except UnicodeDecodeError as e:
        # Locate the offending token for error reporting.  This second pass is
        # only used to build the error; it does not affect resolution.
        bad_index = None
        for token_index, raw_token in enumerate(pointer[1:].split("/")):
            try:
                unquote(raw_token, errors="strict")
            except UnicodeDecodeError:
                bad_index = token_index
                break
        raise PointerParseError(
            "invalid UTF-8 escape in JSON pointer %r: %s" % (pointer, e),
            pointer,
            token_index=bad_index,
            token=pointer[1:].split("/")[bad_index] if bad_index is not None else None,
        ) from e

    tokens = []
    # Strip exactly one leading '/', not all of them.  The remaining empty
    # strings are significant tokens (empty member names).  Fragments without
    # a leading slash are kept as pointer text for backwards compatibility.
    has_leading_slash = decoded.startswith("/")
    raw_tokens = pointer[1:].split("/") if has_leading_slash else pointer.split("/")
    decoded_tokens = (
        decoded[1:].split("/") if has_leading_slash else decoded.split("/")
    )
    # Character offsets are only meaningful while raw and decoded tokens line
    # up; a percent-encoded slash can change the token count.
    if len(raw_tokens) == len(decoded_tokens):
        offsets = []
        offset = 1 if has_leading_slash else 0
        for raw_token in raw_tokens:
            offsets.append(offset)
            offset += len(raw_token) + 1
    else:
        offsets = [None] * len(decoded_tokens)

    for token_index, token in enumerate(decoded_tokens):
        if _INVALID_POINTER_ESCAPE_RE.search(token):
            raise PointerParseError(
                "invalid '~' escape in token %d (%r) of JSON pointer %r"
                % (token_index, token, pointer),
                pointer,
                token_index=token_index,
                token=token,
                char_index=offsets[token_index],
                pointer_tokens=tuple(tokens),
            )
        tokens.append(token.replace("~1", "/").replace("~0", "~"))
    return PointerSteps(tokens)


class PointerResult(tuple):
    """
    A pair of ``(target, steps)`` returned when pointer steps are requested.

    It is a real tuple, so ``target, steps = result`` works, and the target and
    steps are also available as the :attr:`target` / :attr:`value` and
    :attr:`steps` attributes.
    """

    __slots__ = ()

    def __new__(cls, target, steps):
        return super(PointerResult, cls).__new__(cls, (target, PointerSteps(steps)))

    @property
    def target(self):
        return self[0]

    @property
    def value(self):
        return self[0]

    @property
    def steps(self):
        return self[1]

    def __repr__(self):
        return "PointerResult(target=%r, steps=%r)" % (self[0], self[1])


def _resolve_pointer_tokens(document, pointer, tokens, own_reference=None):
    """Apply parsed pointer tokens, returning ``(target, steps)``."""
    steps = []
    for step_index, token in enumerate(tokens):
        if own_reference is not None and document is own_reference:
            # A reference object may point inside itself; in that case the
            # path is interpreted against the reference object itself.
            document = own_reference.__reference__

        if isinstance(document, Sequence) and not isinstance(
            document, (str, bytes, bytearray)
        ):
            try:
                # Array tokens must be canonical decimal indices.  int() on
                # its own is too permissive (it accepts "01", "+1", "-1"...).
                step = _sequence_index(document, token)
            except (TypeError, IndexError) as e:
                raise PointerResolutionError(
                    "Unresolvable JSON pointer: %r at step %d (%r): %s"
                    % (pointer, step_index, token, e),
                    pointer,
                    pointer_tokens=tokens,
                    steps=steps,
                    step=token,
                    step_index=step_index,
                    char_index=_token_char_index(pointer, step_index),
                    cause=e,
                ) from e
        else:
            # Object member names are always strings, even numeric ones.
            # Containers that are not Mapping subclasses (including proxies)
            # are still indexed duck-typing style, as before.
            step = token

        try:
            document = document[step]
        except (TypeError, LookupError) as e:
            raise PointerResolutionError(
                "Unresolvable JSON pointer: %r at step %d (%r): %s"
                % (pointer, step_index, token, e),
                pointer,
                pointer_tokens=tokens,
                steps=steps,
                step=step,
                step_index=step_index,
                char_index=_token_char_index(pointer, step_index),
                cause=e,
            ) from e
        steps.append(step)
    return document, tuple(steps)


def resolve_pointer(document, pointer, return_steps=False):
    """
    Resolve a JSON pointer ``pointer`` within ``document``.

    :param document: the document to resolve against
    :param str pointer: a JSON pointer URI fragment (without the leading
        ``#``), or a full pointer beginning with ``/``
    :param bool return_steps: if true, return a :class:`PointerResult`
        containing both the target and the ordered steps.  Array-index steps
        are integers; object-key steps are strings.
    :raises PointerParseError: if the pointer has an invalid escape
    :raises PointerResolutionError: if a token does not exist, an array index
        is not a valid canonical index, or an index is out of range

    When ``return_steps`` is false the target itself is returned, preserving
    the historical behavior.  This module-level helper and
    :meth:`JsonRef.resolve_pointer` share the same resolution semantics.
    """
    tokens = parse_pointer(pointer)
    target, steps = _resolve_pointer_tokens(document, pointer, tokens)
    if return_steps:
        return PointerResult(target, steps)
    return target


def resolve_pointer_steps(document, pointer):
    """
    Resolve ``pointer`` within ``document`` and return ``(target, steps)``.

    This is a convenience wrapper around :func:`resolve_pointer` with
    ``return_steps=True``.  The result is a :class:`PointerResult`, which
    unpacks like a plain pair and also exposes ``.target`` and ``.steps``.
    """
    return resolve_pointer(document, pointer, return_steps=True)


def _token_char_index(pointer, token_index):
    """Return the character offset of a pointer token, or None."""
    if not pointer or token_index is None:
        return None
    if pointer.startswith("#"):
        pointer = pointer[1:]
    offset = 1
    if not pointer.startswith("/"):
        offset = 0
    for index, raw_token in enumerate(pointer[1:].split("/")):
        if index == token_index:
            return offset
        offset += len(raw_token) + 1
    return None


def _sequence_index(document, token):
    """Convert an array token to an index, or raise a TypeError."""
    if not _ARRAY_INDEX_RE.match(token):
        raise TypeError(
            "invalid array index %r; array indices must be '0' or a "
            "non-zero digit followed by digits" % (token,)
        )
    try:
        index = int(token)
    except ValueError as e:
        raise TypeError("invalid array index %r: %s" % (token, e)) from e
    if index >= len(document):
        raise IndexError(
            "array index %d is out of range for an array of length %d"
            % (index, len(document))
        )
    return index


class JsonRefError(Exception):
    def __init__(self, message, reference, uri="", base_uri="", path=(), cause=None):
        self.message = message
        self.reference = reference
        self.uri = uri
        self.base_uri = base_uri
        self.path = path
        self.cause = self.__cause__ = cause
        # Details about where a JSON pointer failed to resolve.  These are
        # populated for pointer errors and default to empty values otherwise.
        self.pointer = None
        self.pointer_tokens = ()
        self.steps = PointerSteps()
        self.step = None
        self.step_index = None
        self.token_index = None
        self.char_index = None

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

    def resolve_pointer(
        self, document, pointer, return_steps=False, return_path=False
    ):
        """
        Resolve a json pointer ``pointer`` within the referenced ``document``.

        The fragment is percent-decoded exactly once and then split on
        literal ``/``, so percent escapes are never decoded a second time and
        cannot silently land on a different member.  Each token is then
        unescaped (``~1`` -> ``/`` and ``~0`` -> ``~``).  When a token is
        applied to an array it is converted to a canonical integer index; when
        it is applied to an object it always remains a string key, even if it
        looks numeric.

        :argument document: the referent document
        :argument str pointer: a json pointer URI fragment to resolve within it
        :argument bool return_steps: if true, return a :class:`PointerResult`
            with both the target and the ordered sequence of applied steps.
            Array steps are integers; object steps are strings.
        :argument bool return_path: alias for ``return_steps``.

        """
        if return_path:
            return_steps = True
        try:
            tokens = parse_pointer(pointer)
        except PointerParseError as e:
            raise self._error(
                "Unresolvable JSON pointer: %r at step %s: %s"
                % (
                    pointer,
                    "?" if e.token_index is None else e.token_index,
                    e,
                ),
                cause=e,
                pointer=pointer,
                pointer_tokens=e.pointer_tokens,
                step=getattr(e, "token", None),
                step_index=getattr(e, "token_index", None),
                char_index=getattr(e, "char_index", None),
            ) from e

        try:
            target, steps = _resolve_pointer_tokens(
                document, pointer, tokens, own_reference=self
            )
        except PointerResolutionError as e:
            raise self._error(
                e.args[0],
                cause=e.cause,
                pointer=pointer,
                pointer_tokens=e.pointer_tokens,
                steps=e.steps,
                step=e.step,
                step_index=e.step_index,
                char_index=e.char_index,
            ) from e

        if return_steps:
            return PointerResult(target, steps)
        return target

    def _error(
        self,
        message,
        cause=None,
        pointer=None,
        pointer_tokens=(),
        steps=(),
        step=None,
        step_index=None,
        char_index=None,
    ):
        message = "Error while resolving `{}`: {}".format(self.full_uri, message)
        error = JsonRefError(
            message,
            self.__reference__,
            uri=self.full_uri,
            base_uri=self.base_uri,
            path=self.path,
            cause=cause,
        )
        error.pointer = pointer
        error.pointer_tokens = tuple(pointer_tokens)
        error.steps = PointerSteps(steps)
        error.step = step
        error.step_index = step_index
        error.token_index = step_index
        error.char_index = char_index
        return error

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
