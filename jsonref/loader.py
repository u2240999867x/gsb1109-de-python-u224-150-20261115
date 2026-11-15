"""Manifest-authorized JSON loading.

This module is intentionally separate from the permissive :func:`jsonref.jsonloader`.
A :class:`JsonLoader` may only read documents that are explicitly named by a
directory manifest.  The manifest is also the authority for aliases and for the
final address returned by an opener after a redirect.

The loader is deliberately paranoid about local paths.  Identifiers are
normalised before lookup, and both the lexical path and its resolved target are
required to stay inside the manifest root.  This prevents ``..`` traversal,
percent-encoded traversal, and symlinks from turning an authorized name into a
read of an unlisted file.
"""

import hashlib
import hmac
import json
import os
import posixpath
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib import parse as urlparse
from urllib.request import HTTPRedirectHandler, build_opener, url2pathname

try:
    import requests
except ImportError:  # pragma: no cover - the default opener supports urllib
    requests = None


MANIFEST_KEYS = (
    "files",
    "entries",
    "allowed",
    "allow",
    "paths",
    "json",
    "documents",
    "resources",
    "items",
)
_ENTRY_ALIAS_KEYS = ("aliases", "alias", "aliases_of", "shared_as")
_ENTRY_TARGET_KEYS = (
    "target",
    "source",
    "canonical",
    "path",
    "file",
    "filename",
    "uri",
    "url",
    "location",
)
_ENTRY_DIGEST_KEYS = ("sha256", "digest", "checksum", "hash", "content_sha256")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_DANGEROUS_PERCENT_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_DIGEST_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ManifestError(ValueError):
    """The directory manifest is missing, malformed, or internally unsafe."""


class AuthorizationError(PermissionError):
    """A reference or its final address is outside the authorized manifest."""


class DigestMismatch(AuthorizationError):
    """The loaded bytes do not match the digest authorized by the manifest."""


def _normalize_percent(value):
    def replace(match):
        char = chr(int(match.group(1), 16))
        if char in _UNRESERVED:
            return char
        return "%" + match.group(1).upper()

    return _PERCENT_RE.sub(replace, value)


def _decode_local_path(value):
    decoded = urlparse.unquote(value)
    # Decode a second time to catch double-encoded traversal.  Iterate a
    # bounded number of times so a malicious value cannot cause endless work.
    for _ in range(2):
        again = urlparse.unquote(decoded)
        if again == decoded:
            break
        decoded = again
    return decoded


def _is_within(path, root):
    try:
        return os.path.commonpath([os.fspath(path), os.fspath(root)]) == os.fspath(
            root
        )
    except ValueError:
        return False


def _check_local_path(value, root):
    """Return a lexical absolute path if it is safely below ``root``."""
    root = os.path.realpath(os.fspath(root))
    if _CONTROL_RE.search(value) or "\\" in value:
        raise AuthorizationError("unsafe character in local path: %r" % value)
    if _DANGEROUS_PERCENT_RE.search(value):
        raise AuthorizationError("encoded path separator in local path: %r" % value)
    decoded = _decode_local_path(value)
    if _CONTROL_RE.search(decoded) or "\\" in decoded:
        raise AuthorizationError("unsafe character in local path: %r" % value)

    path = Path(decoded)
    if not path.is_absolute():
        path = Path(root) / path
    lexical = os.path.abspath(os.path.normpath(os.fspath(path)))
    if not _is_within(lexical, root):
        raise AuthorizationError("path escapes the manifest root: %r" % value)

    # Resolve symlinks as well.  A lexical path can be inside the root while a
    # symlink in the middle of it points somewhere else entirely.
    resolved = os.path.realpath(lexical)
    if not _is_within(resolved, root):
        raise AuthorizationError("resolved path escapes the manifest root: %r" % value)
    return lexical, resolved


def _path_to_uri(path):
    return Path(path).absolute().as_uri()


def _uri_to_local_path(uri):
    parts = urlparse.urlsplit(uri)
    if parts.scheme not in ("", "file"):
        raise ManifestError("not a local URI: %r" % uri)
    if parts.netloc not in ("", "localhost"):
        raise AuthorizationError("remote file authority is not allowed: %r" % uri)
    if parts.query or parts.fragment:
        raise AuthorizationError("file URIs may not have query or fragment: %r" % uri)
    return url2pathname(parts.path)


def _canonicalize_url(uri, base_uri=None):
    parts = urlparse.urlsplit(uri)
    if not parts.scheme:
        if base_uri is None:
            raise ManifestError("relative URI requires a base: %r" % uri)
        parts = urlparse.urlsplit(urlparse.urljoin(base_uri, uri))
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ManifestError("unsupported URI scheme: %r" % parts.scheme)
    if parts.username or parts.password:
        raise AuthorizationError("userinfo is not allowed in an authorized URI")
    host = parts.hostname
    if not host:
        raise ManifestError("URI has no host: %r" % uri)
    try:
        port = parts.port
    except ValueError as exc:
        raise ManifestError("invalid port in URI: %r" % uri) from exc
    host = host.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = "%s:%d" % (host, port)
    if _DANGEROUS_PERCENT_RE.search(parts.path):
        raise AuthorizationError("encoded path separator or dot in URI: %r" % uri)
    path = urlparse.unquote(parts.path) or "/"
    if _CONTROL_RE.search(path) or "\\" in path:
        raise AuthorizationError("unsafe character in URI path: %r" % uri)
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = "/" + path
    if parts.path.endswith("/") and not path.endswith("/"):
        path += "/"
    return urlparse.urlunsplit(
        (scheme, host, _normalize_percent(path), parts.query, "")
    )


def canonicalize_uri(uri, root=None, base_uri=None):
    """Normalize an authorized identifier.

    Local paths are made absolute, dot segments are collapsed, percent escapes
    are checked, and the result is required to remain inside ``root`` when one
    is supplied.  HTTP(S) URLs are normalized without silently changing the
    authority or allowing encoded separators to hide a path segment.
    """
    if not isinstance(uri, str) or not uri:
        raise ManifestError("URI must be a non-empty string")
    parts = urlparse.urlsplit(uri)
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        if parts.fragment:
            raise AuthorizationError("fragments are not part of a document URI")
        return _canonicalize_url(uri, base_uri=base_uri)
    if scheme == "" and base_uri:
        base_scheme = urlparse.urlsplit(base_uri).scheme.lower()
        if base_scheme in ("http", "https"):
            # With an explicit remote base, a relative identifier follows the
            # usual URI rules rather than being treated as a local path.
            return _canonicalize_url(uri, base_uri=base_uri)
    if scheme in ("", "file"):
        if scheme == "file":
            if parts.fragment or parts.query:
                raise AuthorizationError(
                    "file URIs may not have query or fragment: %r" % uri
                )
            raw_path = _uri_to_local_path(uri)
        else:
            if parts.fragment:
                raise AuthorizationError("fragments are not part of a document URI")
            if parts.query:
                raise AuthorizationError("local paths may not have a query")
            raw_path = uri
        if root is None:
            root = os.getcwd()
        lexical, _ = _check_local_path(raw_path, root)
        return _path_to_uri(lexical)
    raise ManifestError("unsupported URI scheme: %r" % parts.scheme)


def _normalize_digest(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError("digest must be a string")
    digest = value.strip().lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    elif digest.startswith("sha256="):
        digest = digest[7:]
    if not _DIGEST_RE.match(digest):
        raise ManifestError("expected a 64 character sha256 digest, got %r" % value)
    return digest


@dataclass(frozen=True)
class ManifestEntry:
    """One logical name in a manifest.

    ``key`` is the name a reference is allowed to use.  ``target`` is the
    physical document to read.  Aliases point at the same target, but they do
    not make the target itself a readable name.
    """

    key: str
    target: str
    digest: str = None
    aliases: tuple = ()
    redirects: tuple = ()
    alias_of: str = None
    metadata: Mapping = field(default_factory=dict, compare=False)

    @property
    def uri(self):
        return self.key

    @property
    def sha256(self):
        return self.digest

    def __getitem__(self, key):
        return getattr(self, key)


@dataclass(frozen=True)
class LoadResult:
    """A loaded document together with its audited provenance."""

    document: object
    final_uri: str
    digest: str
    requested_uri: str = None
    alias_of: str = None
    redirect_chain: tuple = ()
    record: object = None

    @property
    def data(self):
        return self.document

    @property
    def uri(self):
        return self.final_uri

    @property
    def url(self):
        return self.final_uri

    @property
    def final_url(self):
        return self.final_uri

    @property
    def source(self):
        return self.final_uri

    @property
    def source_uri(self):
        return self.final_uri

    @property
    def source_url(self):
        return self.final_uri

    @property
    def sha256(self):
        return self.digest

    # Make the result behave like the document it wraps for callers that only
    # care about the data, while keeping the provenance attributes explicit.
    def __getitem__(self, key):
        return self.document[key]

    def __iter__(self):
        return iter(self.document)

    def __len__(self):
        return len(self.document)

    def __contains__(self, item):
        return item in self.document

    def __eq__(self, other):
        if isinstance(other, LoadResult):
            return self.document == other.document
        return self.document == other

    def __hash__(self):
        return hash(self.digest)

    def get(self, key, default=None):
        return self.document.get(key, default)

    def keys(self):
        return self.document.keys()

    def values(self):
        return self.document.values()

    def items(self):
        return self.document.items()


class LoadRecord(Mapping):
    """Audit record for one reference load.

    Records behave like read-only mappings and also expose attributes, which
    keeps them convenient both for logging and for assertions in tests.
    """

    __slots__ = ("_data",)

    _fields = (
        "requested_uri",
        "final_uri",
        "digest",
        "authorized_uri",
        "alias_of",
        "from_cache",
        "digest_scope",
    )

    def __init__(self, **values):
        data = {name: values.get(name) for name in self._fields}
        data.update(
            {key: value for key, value in values.items() if key not in self._fields}
        )
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        raise AttributeError("LoadRecord is immutable")

    @property
    def sha256(self):
        return self._data["digest"]

    @property
    def source(self):
        return self._data["final_uri"]

    @property
    def source_uri(self):
        return self._data["final_uri"]

    @property
    def source_url(self):
        return self._data["final_uri"]

    @property
    def final_url(self):
        return self._data["final_uri"]

    @property
    def url(self):
        return self._data["final_uri"]

    @property
    def requested_url(self):
        return self._data["requested_uri"]

    @property
    def authorized_url(self):
        return self._data["authorized_uri"]

    def as_dict(self):
        return dict(self._data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return "LoadRecord(%r)" % self._data


class Manifest:
    """An immutable allow-list of JSON documents and their aliases."""

    def __init__(self, manifest, root=None):
        self.manifest_path = None
        self._root = None
        self._root_explicit = False
        self._url_base = None
        self._entries = {}
        self._targets = {}
        self._redirects = {}
        self._aliases = {}

        data = self._read_manifest(manifest, root)
        self._load(data)

    @classmethod
    def from_directory(cls, directory, pattern="*.json", recursive=False):
        """Build a manifest from JSON files present in a directory.

        This is a convenience for callers that already have a trusted local
        directory listing.  The resulting entries are ordinary relative paths.
        """
        directory = Path(directory).resolve()
        files = directory.rglob(pattern) if recursive else directory.glob(pattern)
        names = [
            str(path.relative_to(directory))
            for path in files
            if path.is_file()
        ]
        return cls({"root": str(directory), "files": names})

    @classmethod
    def from_manifest(cls, manifest, root=None):
        return manifest if isinstance(manifest, cls) else cls(manifest, root=root)

    @classmethod
    def from_json(cls, value, root=None):
        """Build a manifest from an already parsed JSON object."""
        return cls(value, root=root)

    @classmethod
    def from_file(cls, path, root=None):
        return cls(path, root=root)

    @classmethod
    def from_list(cls, names, root=None):
        return cls({"files": list(names)}, root=root)

    @property
    def root(self):
        return self._root

    @property
    def base_uri(self):
        return self._url_base

    @property
    def entries(self):
        return tuple(self._entries.values())

    @property
    def aliases(self):
        return dict(self._aliases)

    def _read_manifest(self, manifest, root):
        if isinstance(manifest, (str, os.PathLike)):
            path = Path(manifest).expanduser().resolve()
            if not path.is_file():
                raise ManifestError("manifest does not exist: %s" % path)
            self.manifest_path = path
            try:
                with path.open("r", encoding="utf-8") as stream:
                    data = json.load(stream)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ManifestError("cannot read manifest %s: %s" % (path, exc)) from exc
            default_root = path.parent
            if root is not None:
                self._root = Path(root).expanduser().resolve()
                self._root_explicit = True
            else:
                self._root = default_root
        elif isinstance(manifest, Mapping):
            data = dict(manifest)
            if root is not None:
                self._root = Path(root).expanduser().resolve()
                self._root_explicit = True
        elif isinstance(manifest, Sequence) and not isinstance(
            manifest, (str, bytes, bytearray)
        ):
            data = {"files": list(manifest)}
            if root is not None:
                self._root = Path(root).expanduser().resolve()
                self._root_explicit = True
        else:
            raise ManifestError("manifest must be a path, mapping, or sequence")
        return data

    def _load(self, data):
        if not isinstance(data, Mapping):
            raise ManifestError("manifest JSON must be an object")

        if self._root is None:
            root_value = data.get("root") or data.get("directory")
            if root_value:
                root_value = os.path.expanduser(os.fspath(root_value))
                if not os.path.isabs(root_value) and self.manifest_path:
                    root_value = self.manifest_path.parent / root_value
                self._root = Path(root_value).resolve()
                self._root_explicit = True
            else:
                self._root = Path.cwd().resolve()
        root = Path(self._root).resolve()
        if not root.is_dir():
            raise ManifestError("manifest root is not a directory: %s" % root)
        # Store the resolved root, but keep lexical identities based on it.
        self._root = root

        url_base = data.get("base_uri") or data.get("base_url")
        if url_base:
            self._url_base = _canonicalize_url(str(url_base))
        if self._root_explicit and self._url_base:
            # A manifest that declares a local root and a remote base is
            # ambiguous.  Refuse it rather than guessing which filesystem or
            # host a relative entry belongs to.
            raise ManifestError(
                "manifest cannot declare both a local root and base_uri"
            )

        raw_entries = self._extract_entries(data)
        top_aliases = data.get("aliases") or {}
        if not isinstance(top_aliases, Mapping):
            raise ManifestError("aliases must be an object")

        redirects = data.get("redirects") or {}
        if isinstance(redirects, Mapping):
            for source, targets in redirects.items():
                if isinstance(targets, str):
                    targets = [targets]
                if not isinstance(targets, Sequence):
                    raise ManifestError("redirect targets must be strings")
                self._redirects.setdefault(
                    self._normalize_identifier(source), []
                ).extend(self._normalize_identifier(target) for target in targets)
        elif redirects:
            raise ManifestError("redirects must be an object")

        staged = []
        for name, spec in raw_entries:
            staged.append(self._parse_entry(name, spec))

        # Register canonical names first, so an alias can refer to one by name
        # regardless of the order entries appear in the manifest.
        for identifier, target, digest, aliases, redirects_, metadata in staged:
            self._register(identifier, target, digest, redirects_, metadata)

        for identifier, target, digest, aliases, redirects_, metadata in staged:
            for alias in aliases:
                self._register_alias(alias, identifier, digest=digest)

        for alias, canonical in top_aliases.items():
            alias = self._normalize_identifier(alias)
            canonical = self._normalize_identifier(canonical)
            # A top-level alias may only name content that is already
            # authorized under another name.  It must never introduce a new
            # physical target that the manifest did not already include.
            if canonical not in self._entries and canonical not in self._targets:
                raise ManifestError(
                    "alias %s points at an unlisted target: %s" % (alias, canonical)
                )
            self._register_alias(alias, canonical)

        # Validate that every alias resolves to an authorized target.
        for alias, entry in self._entries.items():
            if entry.alias_of is not None and entry.alias_of not in self._entries:
                if entry.target not in self._targets:
                    self._targets[entry.target] = (entry.digest, entry.metadata)
            self._aliases[alias] = entry.target

    def _extract_entries(self, data):
        entries = []
        seen_container = False
        for key in MANIFEST_KEYS:
            if key not in data:
                continue
            seen_container = True
            value = data[key]
            if isinstance(value, Mapping):
                for name, spec in value.items():
                    entries.append((name, spec))
            elif isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                for spec in value:
                    entries.append((None, spec))
            else:
                raise ManifestError("%s must be a list or object" % key)

        if not seen_container:
            # A top-level object of names to paths is a compact manifest.
            ignored = {
                "root",
                "directory",
                "base_uri",
                "base_url",
                "aliases",
                "redirects",
                "version",
                "batch_id",
            }
            for name, spec in data.items():
                if name not in ignored:
                    entries.append((name, spec))
        return entries

    def _normalize_identifier(self, value):
        if isinstance(value, Path):
            value = os.fspath(value)
        if not isinstance(value, str):
            raise ManifestError("manifest identifier must be a string")
        if value.startswith(("http://", "https://")):
            return _canonicalize_url(value, base_uri=self._url_base)
        if self._url_base:
            return canonicalize_uri(value, root=self._root, base_uri=self._url_base)
        return canonicalize_uri(value, root=self._root)

    def _parse_entry(self, name, spec):
        identifier = name
        target = name
        digest = None
        aliases = []
        redirects = []
        metadata = {}

        if isinstance(spec, str):
            if name is None:
                identifier = spec
                target = spec
            elif _DIGEST_RE.match(spec.strip().lower().split("=")[-1].split(":")[-1]):
                digest = spec
            else:
                target = spec
        elif isinstance(spec, Mapping):
            metadata = dict(spec)
            identifier = (
                spec.get("id")
                or spec.get("key")
                or spec.get("name")
                or name
            )
            target = next(
                (spec[key] for key in _ENTRY_TARGET_KEYS if spec.get(key)),
                name,
            )
            digest = next(
                (spec[key] for key in _ENTRY_DIGEST_KEYS if spec.get(key)),
                None,
            )
            aliases = next(
                (spec[key] for key in _ENTRY_ALIAS_KEYS if spec.get(key)),
                [],
            )
            redirects = spec.get("redirects") or spec.get("redirect") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(redirects, str):
                redirects = [redirects]
            if not isinstance(aliases, Sequence) or isinstance(
                aliases, (str, bytes, bytearray)
            ):
                raise ManifestError("aliases must be a string or list")
            if not isinstance(redirects, Sequence) or isinstance(
                redirects, (str, bytes, bytearray)
            ):
                raise ManifestError("redirects must be a string or list")
        elif spec is None or spec is True:
            if name is None:
                raise ManifestError("manifest entry has no identifier")
        else:
            raise ManifestError("unsupported manifest entry: %r" % (spec,))

        if identifier is None:
            identifier = target
        if target is None:
            target = identifier
        digest = _normalize_digest(digest)
        identifier = self._normalize_identifier(identifier)
        target = self._normalize_identifier(target)
        aliases = [self._normalize_identifier(alias) for alias in aliases]
        redirects = [self._normalize_identifier(item) for item in redirects]
        return identifier, target, digest, aliases, redirects, metadata

    def _register(self, identifier, target, digest, redirects, metadata):
        if identifier in self._entries:
            existing = self._entries[identifier]
            if existing.target != target or existing.digest != digest:
                raise ManifestError("conflicting manifest entries for %s" % identifier)
            return
        entry = ManifestEntry(
            key=identifier,
            target=target,
            digest=digest,
            redirects=tuple(redirects),
            metadata=metadata,
        )
        self._entries[identifier] = entry
        old = self._targets.get(target)
        if old is not None:
            old_digest, old_metadata = old
            if digest is not None and old_digest is not None and digest != old_digest:
                raise ManifestError("conflicting digests for %s" % target)
            merged = dict(old_metadata)
            merged.update(metadata)
            self._targets[target] = (digest or old_digest, merged)
        else:
            self._targets[target] = (digest, metadata)

    def _register_alias(self, alias, canonical, digest=None):
        """Register ``alias`` without making ``canonical`` readable by itself."""
        canonical_entry = self._entries.get(canonical)
        if canonical_entry is not None:
            target = canonical_entry.target
            if digest is None:
                digest = canonical_entry.digest
            if canonical_entry.digest and digest and canonical_entry.digest != digest:
                raise ManifestError("alias digest conflicts with target")
            redirects = canonical_entry.redirects
            metadata = canonical_entry.metadata
        else:
            target = canonical
            redirects = ()
            metadata = {}
        existing = self._entries.get(alias)
        if existing is not None:
            if existing.target != target or (
                existing.digest and digest and existing.digest != digest
            ):
                raise ManifestError("conflicting alias entry for %s" % alias)
            return
        entry = ManifestEntry(
            key=alias,
            target=target,
            digest=digest,
            redirects=tuple(redirects),
            alias_of=canonical,
            metadata=metadata,
        )
        self._entries[alias] = entry
        if target not in self._targets:
            self._targets[target] = (digest, metadata)

    def authorize(self, uri):
        """Return the entry for ``uri`` or raise :class:`AuthorizationError`."""
        try:
            key = self._normalize_identifier(uri)
        except (ManifestError, OSError, ValueError) as exc:
            raise AuthorizationError("invalid authorized URI %r: %s" % (uri, exc)) from exc
        entry = self._entries.get(key)
        if entry is None:
            raise AuthorizationError("document is not in the manifest: %s" % key)
        return entry

    def is_authorized(self, uri):
        try:
            self.authorize(uri)
        except AuthorizationError:
            return False
        return True

    def authorize_final(self, entry, final_uri):
        """Check a final address after an opener followed a redirect.

        The final address must either be the entry's own target, one of its
        declared redirect destinations, or another logical name that is itself
        authorized by the manifest.  Anything else is rejected, so a redirect
        can never turn one authorized name into a read of an unlisted file.
        """
        return self._authorize_destination(entry, entry.target, final_uri)

    def _authorize_destination(self, entry, current_uri, destination):
        if not isinstance(destination, str) or not destination:
            raise AuthorizationError(
                "redirect destination must be a non-empty string: %r"
                % (destination,)
            )
        parts = urlparse.urlsplit(destination)
        if not parts.scheme:
            if current_uri.startswith(("http://", "https://")):
                destination = urlparse.urljoin(current_uri, destination)
            elif current_uri.startswith("file:"):
                resolved = urlparse.urljoin(current_uri, destination)
                # urljoin keeps file: schemes but drops the authority rules
                # we enforce later; canonicalization does the real check.
                destination = resolved
            else:
                destination = str(Path(current_uri) / destination)
        try:
            final_key = self._normalize_identifier(destination)
        except (ManifestError, OSError, ValueError) as exc:
            raise AuthorizationError(
                "invalid redirect destination %r: %s" % (destination, exc)
            ) from exc
        if final_key == entry.target or final_key in entry.redirects:
            return final_key
        # A different name is acceptable only when that name is itself
        # authorized by the manifest.  The final address is still audited on
        # the load record, so a redirect may share another listed document but
        # can never reach an unlisted file.
        if final_key in self._entries:
            return final_key
        if final_key in self._redirects.get(entry.key, ()):
            return final_key
        # An alias target is not a readable name of its own; only the entry
        # that declared it may read that physical target.
        raise AuthorizationError(
            "redirect destination is not authorized for %s: %s"
            % (entry.key, final_key)
        )

    def authorize_chain(self, entry, redirects, final_uri=None):
        """Authorize every hop of a redirect chain, returning the final key.

        A chain is accepted only when each address is itself authorized for
        this entry.  This prevents a permitted first hop from laundering an
        arbitrary second hop.
        """
        current = entry.target
        for hop in redirects or ():
            current = self._authorize_destination(entry, current, hop)
        if final_uri is not None:
            current = self._authorize_destination(entry, current, final_uri)
        return current

    def target_digest(self, target):
        value = self._targets.get(target)
        return value[0] if value else None

    def __contains__(self, uri):
        return self.is_authorized(uri)

    def __repr__(self):
        return "Manifest(root=%r, entries=%d)" % (self._root, len(self._entries))


def _document_digest(document):
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unwrap_loaded(value):
    """Accept the loader result shapes used by jsonref and by custom openers."""
    if isinstance(value, LoadResult):
        return value
    if isinstance(value, tuple) and len(value) in (2, 3):
        document, final_uri = value[0], value[1]
        if not isinstance(final_uri, str):
            return None
        digest = value[2] if len(value) == 3 else None
        return LoadResult(document, final_uri, digest)
    if isinstance(value, Mapping):
        keys = set(value)
        has_document = "document" in keys or (
            "data" in keys and len(keys) <= 4
        )
        if has_document and any(
            key in keys
            for key in ("final_uri", "final_url", "uri", "url", "source")
        ):
            document = value.get("document", value.get("data"))
            final_uri = (
                value.get("final_uri")
                or value.get("final_url")
                or value.get("uri")
                or value.get("url")
                or value.get("source")
            )
            digest = (
                value.get("digest")
                or value.get("sha256")
                or value.get("checksum")
            )
            return LoadResult(document, final_uri, digest)
    return None


def _decode_json(raw, parse_kwargs):
    return json.loads(raw.decode("utf-8-sig"), **parse_kwargs)


class _RecordingRedirectHandler(HTTPRedirectHandler):
    """urllib redirect handler that records every hop before following it."""

    def __init__(self, redirects):
        super().__init__()
        self.redirects = redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_default(uri, parse_kwargs=None, **kwargs):
    """Fetch ``uri`` and return a :class:`LoadResult` with raw provenance.

    Redirects are not allowed blindly.  Every hop is recorded and exposed on
    the result so :class:`JsonLoader` can authorize the whole chain, not only
    the last address it happens to see.
    """
    parse_kwargs = dict(parse_kwargs or {})
    parse_kwargs.update(kwargs)
    parts = urlparse.urlsplit(uri)
    redirects = []
    if parts.scheme in ("http", "https") and requests is not None:
        response = requests.get(uri, allow_redirects=True)
        response.raise_for_status()
        raw = response.content
        final_uri = response.url
        redirects = [item.headers["location"] for item in response.history]
    else:
        handler = _RecordingRedirectHandler(redirects)
        opener = build_opener(handler)
        with opener.open(uri) as response:
            raw = response.read()
            final_uri = response.geturl()
    if not isinstance(raw, (bytes, bytearray)):
        raise ManifestError("opener returned non-byte content for %s" % uri)
    raw = bytes(raw)
    return LoadResult(
        document=_decode_json(raw, parse_kwargs),
        final_uri=final_uri,
        digest=hashlib.sha256(raw).hexdigest(),
        redirect_chain=tuple(redirects),
    )


class JsonLoader:
    """A manifest-authorized loader suitable for ``replace_refs(loader=...)``.

    ``manifest`` can be a path to a JSON manifest, an already parsed mapping,
    a sequence of allowed names, or a :class:`Manifest` instance.  Every call is
    authorized before the document is read.  If the opener follows a redirect,
    the final address is authorized again and the digest of the loaded bytes is
    checked against the manifest when one is supplied.
    """

    def __init__(
        self,
        manifest,
        root=None,
        opener=None,
        loader=None,
        cache=True,
        verify_digest=True,
        parse_kwargs=None,
        **json_kwargs
    ):
        if isinstance(manifest, JsonLoader):
            self.manifest = manifest.manifest
        else:
            self.manifest = Manifest.from_manifest(manifest, root=root)
        self.opener = opener or loader or _open_default
        self.cache_enabled = bool(cache)
        self.verify_digest = bool(verify_digest)
        self.parse_kwargs = dict(parse_kwargs or {})
        self.parse_kwargs.update(json_kwargs)
        self._cache = {}
        self._documents = {}
        self._records = []
        self._lock = threading.RLock()

    @classmethod
    def from_manifest(cls, manifest, **kwargs):
        return cls(manifest, **kwargs)

    @classmethod
    def from_json(cls, value, root=None, **kwargs):
        return cls(value, root=root, **kwargs)

    @classmethod
    def from_file(cls, path, root=None, **kwargs):
        return cls(path, root=root, **kwargs)

    @classmethod
    def from_list(cls, names, root=None, **kwargs):
        return cls({"files": list(names)}, root=root, **kwargs)

    def load(self, uri, **kwargs):
        """Load one authorized document."""
        return self(uri, **kwargs)

    def load_uri(self, uri, **kwargs):
        return self(uri, **kwargs)

    def load_document(self, uri, **kwargs):
        return self(uri, **kwargs).document

    @property
    def records(self):
        with self._lock:
            return tuple(self._records)

    @property
    def audit_log(self):
        return self.records

    @property
    def history(self):
        return self.records

    @property
    def last_record(self):
        with self._lock:
            return self._records[-1] if self._records else None

    @property
    def last_source(self):
        record = self.last_record
        return record.final_uri if record else None

    @property
    def last_digest(self):
        record = self.last_record
        return record.digest if record else None

    def authorize(self, uri):
        """Expose the manifest check without loading anything."""
        return self.manifest.authorize(uri)

    authorize_uri = authorize

    def resolve(self, uri):
        return self.authorize(uri)

    def authorize_final(self, requested_uri, final_uri):
        """Re-check a loader result before a caller trusts its final address."""
        entry = self.manifest.authorize(requested_uri)
        return self.manifest.authorize_final(entry, final_uri)

    def _cache_key(self, target, kwargs):
        parse = tuple(sorted((key, repr(value)) for key, value in kwargs.items()))
        return target, parse

    def _check_digest(self, entry, digest, final_key=None):
        expected = self._expected_digest(entry, final_key)
        if self.verify_digest and expected is not None:
            if not hmac.compare_digest(expected, digest):
                raise DigestMismatch(
                    "digest mismatch for %s: expected %s, got %s"
                    % (final_key or entry.target, expected, digest)
                )

    def _expected_digest(self, entry, final_key=None):
        if final_key and final_key != entry.target:
            destination = self.manifest._entries.get(final_key)
            if destination is not None:
                return destination.digest or self.manifest.target_digest(
                    destination.target
                )
        return entry.digest or self.manifest.target_digest(entry.target)

    def _record(self, **values):
        record = LoadRecord(**values)
        with self._lock:
            self._records.append(record)
        return record

    def __call__(self, uri, **kwargs):
        entry = self.manifest.authorize(uri)
        requested_key = entry.key
        options = dict(self.parse_kwargs)
        options.update(kwargs)
        cache_key = self._cache_key(entry.target, options)

        # Serialize the cache check and the opener call.  This keeps a
        # concurrent miss from opening the same target twice, while the
        # reentrant lock still allows the record log to be appended.
        with self._lock:
            cached = self._cache.get(cache_key) if self.cache_enabled else None
            if cached is not None:
                try:
                    return self._load_from_cache(
                        entry, requested_key, cache_key, cached
                    )
                except AuthorizationError:
                    # The same physical target may have been reached through a
                    # different entry whose redirect chain does not apply to
                    # this one.  Fall through to a fresh, separately
                    # authorized load.
                    pass
            return self._load_from_opener(
                entry, requested_key, cache_key, options
            )

    def _load_from_cache(self, entry, requested_key, cache_key, cached):
        final_uri, digest, digest_scope, accepted = cached
        # Re-authorize the whole chain and re-check the digest even when the
        # bytes come from the in-process cache.  A cache hit must not be able
        # to skip either decision.
        self.manifest.authorize_chain(entry, accepted, final_uri)
        self._check_digest(entry, digest, final_uri)
        record = self._record(
            requested_uri=requested_key,
            final_uri=final_uri,
            digest=digest,
            authorized_uri=entry.key,
            alias_of=entry.alias_of,
            from_cache=True,
            digest_scope=digest_scope,
            redirect_chain=tuple(accepted),
        )
        return LoadResult(
            self._documents[(cache_key, digest)],
            final_uri,
            digest,
            requested_uri=requested_key,
            alias_of=entry.alias_of,
            redirect_chain=tuple(accepted),
            record=record,
        )

    def _load_from_opener(self, entry, requested_key, cache_key, options):
        loaded = self.opener(entry.target, **options)
        result = _unwrap_loaded(loaded)
        if result is None:
            # A custom opener may return the parsed document directly.  There
            # is no byte stream to hash, so use a canonical document digest.
            document = loaded
            final_uri = entry.target
            digest = _document_digest(document)
            digest_scope = "document"
            redirects = ()
        else:
            document = result.document
            final_uri = result.final_uri or entry.target
            digest = result.digest
            digest_scope = "bytes" if digest else "document"
            redirects = tuple(getattr(result, "redirect_chain", ()) or ())
            if digest is None:
                digest = _document_digest(document)
                digest_scope = "document"
            else:
                digest = _normalize_digest(digest)

        final_key = self.manifest.authorize_chain(entry, redirects, final_uri)
        self._check_digest(entry, digest, final_key)

        # Share the actual document object between aliases that point at the
        # same content.  Authorization was still performed per alias above,
        # so sharing never widens a readable name.
        shared_key = (cache_key, digest)
        existing = self._documents.get(shared_key)
        if existing is not None:
            document = existing
        else:
            self._documents[shared_key] = document
        if self.cache_enabled:
            self._cache[cache_key] = (
                final_key,
                digest,
                digest_scope,
                redirects,
            )

        record = self._record(
            requested_uri=requested_key,
            final_uri=final_key,
            digest=digest,
            authorized_uri=entry.key,
            alias_of=entry.alias_of,
            from_cache=False,
            digest_scope=digest_scope,
            redirect_chain=redirects,
        )
        return LoadResult(
            document,
            final_key,
            digest,
            requested_uri=requested_key,
            alias_of=entry.alias_of,
            redirect_chain=redirects,
            record=record,
        )


# Compatibility spellings for callers that describe the loader by its job.
ManifestLoader = JsonLoader
AuthorizedJsonLoader = JsonLoader
SecureJsonLoader = JsonLoader


def load_manifest(manifest, root=None):
    """Load and validate a directory manifest without reading any JSON data."""
    return Manifest.from_manifest(manifest, root=root)


def manifest_loader(manifest, **kwargs):
    """Create a :class:`JsonLoader` from a manifest path or object."""
    return JsonLoader(manifest, **kwargs)
