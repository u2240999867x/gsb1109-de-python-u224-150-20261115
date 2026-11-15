"""
Offline VDF package expansion.

A package is a directory containing an external index plus a number of
manifest files (``.vdf`` by default).  The index points at the root manifest
and every manifest may reference further *sub-manifests* and declare
*resources* (files).  This module expands that graph into a flat
:class:`ResourceCatalog`.

The expansion rules implemented here are:

* A shared sub-manifest is parsed only once, but every reference path that
  reaches it (and its resources) is retained.
* Reference cycles are detected and reported, never followed forever.
* Referenced manifests and declared resources that do not exist are reported
  as missing.
* A reference that would resolve outside of the package root stops at the
  boundary and is reported instead of being read.
* The same resource name contributed by different parents with different
  versions can not share a single catalog entry and is reported as a
  version conflict.

The engine works on a small normalized representation so it can be driven by
VDF files, JSON files or plain dictionaries.  :class:`VDFPackageLoader`
provides the default on-disk behaviour and :class:`Schema` allows the
expected key names to be customized.
"""

import os
import json

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2
    from collections import Mapping

import vdf


# Default key names understood when reading indexes and manifests.
DEFAULT_INDEX_ROOT_KEYS = ("root", "entry", "root_manifest")
DEFAULT_VERSION_KEY = "version"
DEFAULT_DIGEST_KEYS = ("digest", "sha", "sha256", "checksum", "hash")
DEFAULT_PATH_KEYS = ("path", "file", "filename")
DEFAULT_MANIFEST_TARGET_KEYS = ("manifest", "target", "ref")
DEFAULT_SUBMANIFEST_BLOCKS = (
    "manifests", "submanifests", "submanifest", "includes", "#include", "#base",
)
DEFAULT_RESOURCE_BLOCKS = ("resources", "files", "assets")
DEFAULT_INDEX_FILENAMES = ("index.vdf", "package.vdf", "index.json")


class PackageError(Exception):
    """Base class for every error raised while expanding a package."""


class ManifestCycleError(PackageError):
    """A manifest references (transitively) back into itself."""

    def __init__(self, reference, chain):
        self.reference = reference
        self.chain = tuple(chain)
        message = "manifest cycle detected at %r: %s" % (
            reference, " -> ".join(self.chain),
        )
        super(ManifestCycleError, self).__init__(message)


class MissingManifestError(PackageError):
    """A referenced manifest does not exist inside the package."""

    def __init__(self, reference, chain):
        self.reference = reference
        self.chain = tuple(chain)
        super(MissingManifestError, self).__init__(
            "missing manifest %r referenced from %s" % (
                reference, " -> ".join(self.chain) or "<index>",
            )
        )


class MissingResourceError(PackageError):
    """A declared resource file does not exist inside the package."""

    def __init__(self, reference, origin, chain):
        self.reference = reference
        self.origin = origin
        self.chain = tuple(chain)
        super(MissingResourceError, self).__init__(
            "missing resource %r declared by %r" % (reference, origin)
        )


class BoundaryViolationError(PackageError):
    """A reference resolves to a location outside the package root."""

    def __init__(self, reference, resolved, chain):
        self.reference = reference
        self.resolved = resolved
        self.chain = tuple(chain)
        super(BoundaryViolationError, self).__init__(
            "reference %r escapes the package root (%r)" % (reference, resolved)
        )


class VersionConflictError(PackageError):
    """The same resource is contributed with incompatible versions."""

    def __init__(self, name, versions, reference_chains):
        self.name = name
        self.versions = tuple(sorted(versions))
        self.reference_chains = [tuple(chain) for chain in reference_chains]
        super(VersionConflictError, self).__init__(
            "conflicting versions for resource %r: %s" % (
                name, ", ".join(self.versions),
            )
        )


class Schema(object):
    """Configurable key names used to interpret indexes and manifests."""

    def __init__(
        self,
        index_root_keys=DEFAULT_INDEX_ROOT_KEYS,
        version_key=DEFAULT_VERSION_KEY,
        digest_keys=DEFAULT_DIGEST_KEYS,
        path_keys=DEFAULT_PATH_KEYS,
        manifest_target_keys=DEFAULT_MANIFEST_TARGET_KEYS,
        submanifest_blocks=DEFAULT_SUBMANIFEST_BLOCKS,
        resource_blocks=DEFAULT_RESOURCE_BLOCKS,
        index_filenames=DEFAULT_INDEX_FILENAMES,
    ):
        self.index_root_keys = tuple(index_root_keys)
        self.version_key = version_key
        self.digest_keys = tuple(digest_keys)
        self.path_keys = tuple(path_keys)
        self.manifest_target_keys = tuple(manifest_target_keys)
        self.submanifest_blocks = tuple(submanifest_blocks)
        self.resource_blocks = tuple(resource_blocks)
        self.index_filenames = tuple(index_filenames)


class ResourceEntry(object):
    """A single resource flattened into the catalog."""

    def __init__(self, name, version=None, digest=None, origin=None):
        self.name = name
        # Distinct versions seen, in first-seen order. More than one means the
        # resource is involved in a version conflict.
        self.versions = [] if version is None else [version]
        self.digest = digest
        self.exists = True
        # Root-relative manifest paths that declare this resource.
        self.origins = []
        # Distinct manifest chains (tuples of root-relative manifest paths)
        # through which this resource is reachable.
        self.references = []
        if origin is not None:
            self.origins.append(origin)

    @property
    def version(self):
        return self.versions[0] if self.versions else None

    @property
    def has_conflict(self):
        return len(self.versions) > 1

    def add_version(self, version):
        if version is not None and version not in self.versions:
            self.versions.append(version)

    def add_origin(self, origin):
        if origin is not None and origin not in self.origins:
            self.origins.append(origin)

    def add_reference(self, chain):
        chain = tuple(chain)
        if chain not in self.references:
            self.references.append(chain)

    def __repr__(self):
        return "ResourceEntry(name=%r, version=%r, references=%d)" % (
            self.name, self.version, len(self.references),
        )


class ManifestNode(object):
    """A manifest that participates in the expansion, parsed at most once."""

    def __init__(self, path, version=None):
        self.path = path
        self.version = version
        self.exists = True
        # Root-relative resource paths declared by this manifest.
        self.resources = []
        # Every chain (tuple of manifest paths starting at the root) that
        # reaches this manifest.
        self.referenced_by = []

    def add_reference(self, chain):
        chain = tuple(chain)
        if chain not in self.referenced_by:
            self.referenced_by.append(chain)

    def __repr__(self):
        return "ManifestNode(path=%r, references=%d)" % (
            self.path, len(self.referenced_by),
        )


class ResourceCatalog(object):
    """The flattened result of an expansion."""

    def __init__(self, root):
        self.root = os.path.realpath(root)
        # Root-relative manifest path -> ManifestNode
        self.manifests = {}
        # Root-relative resource path -> ResourceEntry
        self.resources = {}
        self.errors = []
        # Number of times manifests were actually read from the loader.
        self.manifests_parsed = 0
        # Number of manifest visits (shared manifests count each time).
        self.manifest_visits = 0

    @property
    def ok(self):
        return not self.errors

    def add_error(self, error):
        self.errors.append(error)

    def get_manifest(self, path, version=None):
        node = self.manifests.get(path)
        if node is None:
            node = ManifestNode(path, version)
            self.manifests[path] = node
        elif version is not None and node.version is None:
            node.version = version
        return node

    def add_resource(self, name, chain, version=None, digest=None,
                     origin=None, exists=True):
        entry = self.resources.get(name)
        if entry is None:
            entry = ResourceEntry(name, version, digest, origin)
            self.resources[name] = entry
        else:
            before = set(entry.versions)
            entry.add_version(version)
            if digest is not None and entry.digest is None:
                entry.digest = digest
            # Emit one conflict error per newly discovered distinct version.
            if len(entry.versions) > len(before) and entry.has_conflict:
                self.add_error(VersionConflictError(
                    name,
                    set(entry.versions),
                    entry.references + [chain],
                ))

        entry.exists = entry.exists and exists
        entry.add_origin(origin)
        entry.add_reference(chain)
        return entry

    def summary(self):
        return {
            "root": self.root,
            "manifests": len(self.manifests),
            "manifests_parsed": self.manifests_parsed,
            "manifest_visits": self.manifest_visits,
            "resources": len(self.resources),
            "errors": len(self.errors),
        }


def _read_structured_file(path):
    """Read a VDF or JSON file into a plain mapping."""
    with open(path, "r") as fp:
        text = fp.read()
    if path.lower().endswith(".json"):
        return json.loads(text)
    return vdf.loads(text)


def _unwrap_manifest(raw):
    """Drop a single redundant ``manifest`` wrapper used by some layouts."""
    if isinstance(raw, Mapping) and len(raw) == 1:
        only_key = next(iter(raw))
        value = raw[only_key]
        if only_key == "manifest" and isinstance(value, Mapping):
            return value
    return raw


def _first(mapping, keys):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _iter_refs(block, schema, target_keys, scalar_key_is_target=False):
    """Yield normalized ref dicts carrying ``ref`` and optional metadata.

    ``scalar_key_is_target`` describes plain ``"name" "value"`` mappings.  In
    resource blocks the key is the file path and the value its version, while
    in sub-manifest blocks the value is the referenced target.
    """
    if block is None:
        return

    if isinstance(block, str):
        # Bare directive such as ``"#include" "other.vdf"``.
        yield {"ref": block}
        return

    if isinstance(block, Mapping):
        items = ((key, value) for key, value in block.items())
    elif isinstance(block, (list, tuple)):
        items = ((None, value) for value in block)
    else:
        raise PackageError("unexpected reference block type: %r" % type(block))

    for key, value in items:
        if isinstance(value, str):
            if scalar_key_is_target and key is not None:
                ref = {"ref": key, "version": value}
            else:
                ref = {"ref": value}
        elif isinstance(value, Mapping):
            target = _first(value, target_keys) or _first(value, schema.path_keys)
            if target is None:
                target = key
            if not isinstance(target, str):
                raise PackageError("reference is missing a target: %r" % value)
            ref = {"ref": target}
            version = value.get(schema.version_key)
            if version is not None:
                ref["version"] = version
            digest = _first(value, schema.digest_keys)
            if digest is not None:
                ref["digest"] = digest
        else:
            raise PackageError("unsupported reference value: %r" % (value,))
        yield ref


class VDFPackageLoader(object):
    """Loads the index and manifests from a package directory."""

    def __init__(self, schema=None):
        self.schema = schema or Schema()

    def locate_index(self, root, index_file=None):
        if index_file is not None:
            return os.path.join(root, index_file)
        for name in self.schema.index_filenames:
            candidate = os.path.join(root, name)
            if os.path.isfile(candidate):
                return candidate
        raise PackageError(
            "no index found in %r (looked for %s)" % (
                root, ", ".join(self.schema.index_filenames),
            )
        )

    def load_index(self, root, index=None, index_file=None):
        if isinstance(index, Mapping):
            raw = index
        else:
            if isinstance(index, str) and index:
                index_file = index
            path = self.locate_index(root, index_file)
            raw = _read_structured_file(path)
        return self.normalize_index(raw)

    def normalize_index(self, raw):
        raw = _unwrap_manifest(raw)
        root_ref = None
        for key in self.schema.index_root_keys:
            if key in raw:
                root_ref = raw[key]
                break
        if not isinstance(root_ref, str) or not root_ref:
            raise PackageError("index is missing a root manifest entry")
        return {"root": root_ref}

    def load_manifest(self, path):
        raw = _read_structured_file(path)
        return self.normalize_manifest(raw)

    def normalize_manifest(self, raw):
        raw = _unwrap_manifest(raw)
        schema = self.schema
        normalized = {
            "version": raw.get(schema.version_key),
            "manifests": [],
            "resources": [],
        }

        for block_key in schema.submanifest_blocks:
            for ref in _iter_refs(
                raw.get(block_key), schema, schema.manifest_target_keys
            ):
                normalized["manifests"].append(ref)

        for block_key in schema.resource_blocks:
            for ref in _iter_refs(
                raw.get(block_key), schema, schema.path_keys,
                scalar_key_is_target=True,
            ):
                normalized["resources"].append(ref)

        return normalized


def _is_within(root, target):
    root = os.path.normpath(root)
    target = os.path.normpath(target)
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False


def _to_posix(path):
    return path.replace(os.sep, "/")


class PackageExpander(object):
    """Walks a package graph and accumulates a :class:`ResourceCatalog`."""

    def __init__(self, root, loader=None, schema=None, digests=None):
        self.root = os.path.realpath(root)
        self.schema = schema or Schema()
        self.loader = loader or VDFPackageLoader(self.schema)
        self.digests = digests or {}
        self.catalog = ResourceCatalog(self.root)
        # Resolved manifest path -> normalized manifest (parse-once cache).
        self._parsed = {}
        # Resolved manifest paths currently being visited (cycle detection).
        self._active = set()

    def expand(self, index=None, index_file=None):
        index_data = self.loader.load_index(self.root, index, index_file)
        self._visit_manifest(index_data["root"], chain=(), base_dir=self.root)
        return self.catalog

    def _resolve(self, ref, base_dir):
        """Resolve ``ref`` relative to ``base_dir`` without leaving the root."""
        if os.path.isabs(ref):
            candidate = os.path.normpath(ref)
        else:
            candidate = os.path.normpath(os.path.join(base_dir, ref))
        real = os.path.realpath(candidate)
        return candidate, real

    def _visit_manifest(self, ref, chain, base_dir):
        candidate, real = self._resolve(ref, base_dir)

        if not _is_within(self.root, candidate) \
                or not _is_within(self.root, real):
            self.catalog.add_error(BoundaryViolationError(ref, real, chain))
            return None

        if not os.path.isfile(real):
            self.catalog.add_error(MissingManifestError(ref, chain))
            return None

        rel = _to_posix(os.path.relpath(real, self.root))
        next_chain = chain + (rel,)

        if real in self._active:
            self.catalog.add_error(ManifestCycleError(ref, next_chain))
            return self._parsed.get(real)

        normalized = self._parsed.get(real)
        if normalized is None:
            normalized = self.loader.load_manifest(real)
            self._parsed[real] = normalized
            self.catalog.manifests_parsed += 1

        self.catalog.manifest_visits += 1
        self._active.add(real)

        node = self.catalog.get_manifest(rel, normalized.get("version"))
        node.add_reference(next_chain)

        manifest_dir = os.path.dirname(real)
        self._collect_resources(normalized, rel, next_chain, manifest_dir)

        for sub_ref in normalized["manifests"]:
            self._visit_manifest(sub_ref["ref"], next_chain, manifest_dir)

        self._active.discard(real)
        return normalized

    def _collect_resources(self, normalized, origin, chain, manifest_dir):
        manifest_version = normalized.get("version")
        node = self.catalog.manifests[origin]
        for item in normalized["resources"]:
            ref = item["ref"]
            candidate, real = self._resolve(ref, manifest_dir)

            if not _is_within(self.root, candidate) \
                    or not _is_within(self.root, real):
                self.catalog.add_error(BoundaryViolationError(ref, real, chain))
                continue

            rel_name = _to_posix(os.path.relpath(real, self.root))
            exists = os.path.isfile(real)
            version = item.get("version", manifest_version)
            digest = item.get("digest") or self.digests.get(rel_name)

            self.catalog.add_resource(
                rel_name, chain, version=version, digest=digest,
                origin=origin, exists=exists,
            )
            if rel_name not in node.resources:
                node.resources.append(rel_name)

            if not exists:
                self.catalog.add_error(MissingResourceError(ref, origin, chain))


def build_catalog(root, index=None, index_file=None, loader=None,
                  schema=None, digests=None, strict=True):
    """Expand ``root`` into a :class:`ResourceCatalog`.

    ``index`` may be an already parsed mapping, otherwise the index is located
    on disk (using ``index_file`` or the schema's default filenames).

    When ``strict`` is ``True`` (the default) the first recorded error is
    raised.  Pass ``False`` to always receive the catalog and inspect
    ``catalog.errors`` yourself.
    """
    expander = PackageExpander(
        root, loader=loader, schema=schema, digests=digests)
    catalog = expander.expand(index=index, index_file=index_file)
    if strict and catalog.errors:
        raise catalog.errors[0]
    return catalog


def expand_package(root, index=None, index_file=None, loader=None,
                   schema=None, digests=None):
    """Convenience wrapper around :func:`build_catalog` that stays strict."""
    return build_catalog(
        root, index=index, index_file=index_file, loader=loader,
        schema=schema, digests=digests, strict=True,
    )
