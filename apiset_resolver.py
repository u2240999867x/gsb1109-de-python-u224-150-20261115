"""Resolve API-set / DLL symbol requirements to their providing library.

Given an offline repository of PE files and an explicit, per-target-version
mapping of API set names (``api-ms-*`` / ``ext-ms-*``) to real libraries,
this module builds the resolution chain from a symbol requirement
(``module!symbol``) to the library that actually provides it, following
forwarded exports.

Three distinct failure modes are reported:

* :class:`MappingMissingError` -- the API set has no entry in the mapping
  of the requested target system version.
* :class:`LibraryMissingError` -- the (mapped or real) library is not
  present in the offline repository.
* :class:`SymbolMissingError` -- the library exists but does not export
  the requested symbol.

Forwarding cycles are detected and reported (:class:`ForwardingCycleError`)
so resolution of cyclic chains always terminates finitely.  Results are
cached per target version: the same API set may map to different libraries
in different versions, so cached entries are never reused across versions.
Repository files are only ever opened read-only.
"""

import json
import os

import pefile

__all__ = [
    "ResolutionError",
    "MappingMissingError",
    "LibraryMissingError",
    "SymbolMissingError",
    "ForwardingCycleError",
    "Hop",
    "Resolution",
    "LibraryExports",
    "InMemoryRepository",
    "PefileRepository",
    "ApiSetResolver",
    "load_version_mapping",
    "normalize_module",
    "is_api_set",
    "parse_requirement",
]

API_SET_PREFIXES = ("api-ms-", "ext-ms-")

_MISSING = object()

# Hard upper bound on chain length.  Cycle detection already guarantees
# termination for finite repositories; this is a safety net so that even
# pathological inputs (e.g. endlessly generated distinct names) stop.
DEFAULT_MAX_HOPS = 256


def normalize_module(name):
    """Normalize a module name for comparison: lowercase, no ``.dll``."""
    name = name.strip().lower()
    if name.endswith(".dll"):
        name = name[: -len(".dll")]
    return name


def is_api_set(module):
    """Return True if the (normalized or not) module name is an API set."""
    return normalize_module(module).startswith(API_SET_PREFIXES)


def parse_requirement(requirement):
    """Split a ``module!symbol`` requirement string into a tuple."""
    module, sep, symbol = requirement.partition("!")
    if not sep or not module or not symbol:
        raise ValueError(
            "requirement must be of the form 'module!symbol': %r" % requirement
        )
    return module, symbol


def split_forwarder(forwarder):
    """Split a PE forwarder string ``MODULE.Symbol`` into (module, symbol)."""
    module, sep, symbol = forwarder.partition(".")
    if not sep or not module or not symbol:
        raise ValueError("invalid forwarder string: %r" % forwarder)
    return module, symbol


class ResolutionError(Exception):
    """Base class for resolution failures.

    Carries the version, module and symbol that were being resolved when
    the failure occurred.
    """

    def __init__(self, message, version=None, module=None, symbol=None):
        super(ResolutionError, self).__init__(message)
        self.version = version
        self.module = module
        self.symbol = symbol


class MappingMissingError(ResolutionError):
    """The API set (or the whole version) has no explicit mapping."""


class LibraryMissingError(ResolutionError):
    """The mapped or real library is not present in the repository."""


class SymbolMissingError(ResolutionError):
    """The library exists but does not export the requested symbol."""


class ForwardingCycleError(ResolutionError):
    """A cycle was detected while following forwarded exports."""

    def __init__(self, message, version=None, module=None, symbol=None, chain=None):
        super(ForwardingCycleError, self).__init__(
            message, version=version, module=module, symbol=symbol
        )
        self.chain = list(chain) if chain else []


class Hop(object):
    """One step of a resolution chain.

    kind is one of:

    * ``"map"``     -- an API set name was mapped to a real library
                       (``target`` is the real library name).
    * ``"forward"`` -- the export is forwarded (``target`` is the raw
                       forwarder string ``MODULE.Symbol``).
    * ``"provide"`` -- the library provides the symbol locally
                       (``target`` is None).
    """

    __slots__ = ("kind", "module", "symbol", "target")

    def __init__(self, kind, module, symbol, target=None):
        self.kind = kind
        self.module = module
        self.symbol = symbol
        self.target = target

    def __repr__(self):
        return "Hop(%r, %r, %r, %r)" % (
            self.kind,
            self.module,
            self.symbol,
            self.target,
        )

    def __eq__(self, other):
        return (
            isinstance(other, Hop)
            and self.kind == other.kind
            and self.module == other.module
            and self.symbol == other.symbol
            and self.target == other.target
        )


class Resolution(object):
    """The result of resolving one requirement for one target version."""

    __slots__ = ("version", "module", "symbol", "provider_module",
                 "provider_symbol", "hops")

    def __init__(self, version, module, symbol, provider_module,
                 provider_symbol, hops):
        self.version = version
        self.module = module
        self.symbol = symbol
        self.provider_module = provider_module
        self.provider_symbol = provider_symbol
        self.hops = list(hops)

    def chain_text(self):
        """Render the explanation chain, e.g.::

            api-ms-win-core-heap-l1-1-0!HeapAlloc
              =map=> kernelbase.dll
              =forward(ntdll.RtlAllocateHeap)=> ntdll.dll
              =provide=> ntdll.dll!RtlAllocateHeap
        """
        lines = ["%s!%s" % (self.module, self.symbol)]
        for hop in self.hops:
            if hop.kind == "map":
                lines.append("  =map=> %s" % hop.target)
            elif hop.kind == "forward":
                lines.append("  =forward=> %s" % hop.target)
            else:
                lines.append(
                    "  =provide=> %s!%s"
                    % (self.provider_module, self.provider_symbol)
                )
        return "\n".join(lines)

    def __repr__(self):
        return "Resolution(version=%r, %r!%r -> %r!%r, hops=%d)" % (
            self.version,
            self.module,
            self.symbol,
            self.provider_module,
            self.provider_symbol,
            len(self.hops),
        )


class LibraryExports(object):
    """Exports of one library.

    ``by_name`` maps an export name to its forwarder string (``None`` when
    the symbol is implemented locally).  ``by_ordinal`` does the same for
    ordinal-only exports.
    """

    __slots__ = ("by_name", "by_ordinal")

    def __init__(self, by_name=None, by_ordinal=None):
        self.by_name = dict(by_name) if by_name else {}
        self.by_ordinal = dict(by_ordinal) if by_ordinal else {}

    def lookup(self, symbol):
        """Return the forwarder for *symbol*, None if local, or _MISSING.

        ``symbol`` may be a name or an ordinal reference of the form
        ``"#42"``.
        """
        if symbol.startswith("#"):
            try:
                ordinal = int(symbol[1:], 10)
            except ValueError:
                return _MISSING
            return self.by_ordinal.get(ordinal, _MISSING)
        return self.by_name.get(symbol, _MISSING)


class InMemoryRepository(object):
    """Repository backed by a plain dict, useful for tests and embedding.

    ``libraries`` maps a library name to a dict of export name -> forwarder
    string (or None for a local symbol), e.g.::

        {"kernelbase.dll": {"HeapAlloc": "ntdll.RtlAllocateHeap"},
         "ntdll.dll": {"RtlAllocateHeap": None}}
    """

    def __init__(self, libraries):
        self._libraries = {}
        for name, exports in libraries.items():
            if isinstance(exports, LibraryExports):
                lib = exports
            else:
                lib = LibraryExports(by_name=exports)
            self._libraries[normalize_module(name)] = lib

    def get(self, module):
        """Return the LibraryExports for *module*, or None if absent."""
        return self._libraries.get(normalize_module(module))


class PefileRepository(object):
    """Repository of PE files in a directory, parsed lazily with pefile.

    Files are only ever opened read-only and are never modified; only the
    export directory is parsed.  Lookups and parsed exports are cached for
    the lifetime of the instance.
    """

    def __init__(self, root):
        self._root = root
        self._index = None
        self._exports = {}

    def _build_index(self):
        self._index = {}
        for entry in sorted(os.listdir(self._root)):
            path = os.path.join(self._root, entry)
            if not os.path.isfile(path):
                continue
            if not entry.lower().endswith(".dll"):
                continue
            self._index.setdefault(normalize_module(entry), path)

    def get(self, module):
        """Return the LibraryExports for *module*, or None if absent."""
        if self._index is None:
            self._build_index()
        name = normalize_module(module)
        if name in self._exports:
            return self._exports[name]
        path = self._index.get(name)
        if path is None:
            return None
        exports = self._read_exports(path)
        self._exports[name] = exports
        return exports

    @staticmethod
    def _read_exports(path):
        by_name = {}
        by_ordinal = {}
        with open(path, "rb") as handle:  # read-only: the file is never written
            data = handle.read()
        pe = pefile.PE(data=data, fast_load=True)
        try:
            pe.parse_data_directories(
                directories=[
                    pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]
                ]
            )
            export_dir = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
            if export_dir is not None:
                for symbol in export_dir.symbols:
                    forwarder = (
                        symbol.forwarder.decode("utf-8", "replace")
                        if symbol.forwarder
                        else None
                    )
                    if symbol.name:
                        by_name[symbol.name.decode("utf-8", "replace")] = forwarder
                    else:
                        by_ordinal[symbol.ordinal] = forwarder
        finally:
            pe.close()
        return LibraryExports(by_name=by_name, by_ordinal=by_ordinal)


def load_version_mapping(source):
    """Load an explicit version -> {API set -> real library} mapping.

    *source* is a path to a JSON file or an already-decoded dict of the
    form::

        {"10.0.19041": {"api-ms-win-core-heap-l1-1-0": "kernelbase.dll"},
         "10.0.22621": {"api-ms-win-core-heap-l1-1-0": "kernelbase.dll"}}

    Names are normalized (lowercase, ``.dll`` stripped) so lookups are
    case-insensitive.
    """
    if isinstance(source, dict):
        raw = source
    else:
        with open(source, "r", encoding="utf-8") as handle:  # read-only
            raw = json.load(handle)
    mapping = {}
    for version, entries in raw.items():
        mapping[str(version)] = {
            normalize_module(api_set): normalize_module(real_lib)
            for api_set, real_lib in entries.items()
        }
    return mapping


class ApiSetResolver(object):
    """Resolve ``module!symbol`` requirements against a repository.

    ``repository`` is any object with a ``get(module)`` method returning a
    :class:`LibraryExports` or None (e.g. :class:`PefileRepository` or
    :class:`InMemoryRepository`).  ``mapping`` is an explicit
    version -> {API set -> real library} dict as produced by
    :func:`load_version_mapping`.

    Resolutions are cached per target version: because one API set may map
    to different libraries in different versions, cached results are keyed
    by version and never reused across versions.
    """

    def __init__(self, repository, mapping, max_hops=DEFAULT_MAX_HOPS):
        self._repository = repository
        if isinstance(mapping, dict):
            mapping = load_version_mapping(mapping)
        self._mapping = mapping
        self._max_hops = max_hops
        self._cache = {}

    def resolve(self, module, symbol, version):
        """Resolve ``module!symbol`` for *version* to a :class:`Resolution`.

        Raises MappingMissingError, LibraryMissingError, SymbolMissingError
        or ForwardingCycleError on failure.
        """
        version = str(version)
        key = (version, normalize_module(module), symbol)
        if key in self._cache:
            return self._cache[key]
        resolution = self._resolve_uncached(module, symbol, version)
        self._cache[key] = resolution
        return resolution

    def resolve_requirement(self, requirement, version):
        """Resolve a ``module!symbol`` requirement string."""
        module, symbol = parse_requirement(requirement)
        return self.resolve(module, symbol, version)

    def clear_cache(self):
        """Drop all cached resolutions (all versions)."""
        self._cache.clear()

    def _resolve_uncached(self, module, symbol, version):
        version_mapping = self._mapping.get(version)
        if version_mapping is None:
            raise MappingMissingError(
                "no explicit mapping for target version %r" % version,
                version=version,
                module=module,
                symbol=symbol,
            )

        hops = []
        visited = set()
        mod = normalize_module(module)
        sym = symbol

        while True:
            state = (mod, sym)
            if state in visited:
                raise ForwardingCycleError(
                    "forwarding cycle detected at %s!%s (version %s)"
                    % (mod, sym, version),
                    version=version,
                    module=mod,
                    symbol=sym,
                    chain=hops,
                )
            visited.add(state)
            if len(hops) >= self._max_hops:
                raise ForwardingCycleError(
                    "resolution chain exceeded %d hops (version %s)"
                    % (self._max_hops, version),
                    version=version,
                    module=mod,
                    symbol=sym,
                    chain=hops,
                )

            if is_api_set(mod):
                real = version_mapping.get(mod)
                if real is None:
                    raise MappingMissingError(
                        "API set %r has no mapping for target version %r"
                        % (mod, version),
                        version=version,
                        module=mod,
                        symbol=sym,
                    )
                hops.append(Hop("map", mod, sym, real))
                mod = real
                continue

            library = self._repository.get(mod)
            if library is None:
                raise LibraryMissingError(
                    "library %r is not present in the repository "
                    "(version %s)" % (mod, version),
                    version=version,
                    module=mod,
                    symbol=sym,
                )

            forwarder = library.lookup(sym)
            if forwarder is _MISSING:
                raise SymbolMissingError(
                    "library %r does not export symbol %r (version %s)"
                    % (mod, sym, version),
                    version=version,
                    module=mod,
                    symbol=sym,
                )
            if forwarder is None:
                hops.append(Hop("provide", mod, sym))
                return Resolution(
                    version=version,
                    module=normalize_module(module),
                    symbol=symbol,
                    provider_module=mod,
                    provider_symbol=sym,
                    hops=hops,
                )

            target_module, target_symbol = split_forwarder(forwarder)
            hops.append(Hop("forward", mod, sym, forwarder))
            mod = normalize_module(target_module)
            sym = target_symbol


def main(argv=None):
    """Command line interface: build resolution chains for requirements.

    Usage::

        python apiset_resolver.py REPO_DIR MAPPING.json VERSION REQ...
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Resolve module!symbol requirements to their providing "
        "library using an explicit per-version API set mapping."
    )
    parser.add_argument("repository", help="directory containing the offline DLLs")
    parser.add_argument("mapping", help="JSON file with the explicit version mapping")
    parser.add_argument("version", help="target system version to resolve for")
    parser.add_argument(
        "requirements", nargs="+", help="module!symbol requirements to resolve"
    )
    args = parser.parse_args(argv)

    resolver = ApiSetResolver(
        PefileRepository(args.repository), load_version_mapping(args.mapping)
    )
    status = 0
    for requirement in args.requirements:
        try:
            resolution = resolver.resolve_requirement(requirement, args.version)
        except ResolutionError as exc:
            status = 1
            print("%s: %s: %s" % (requirement, type(exc).__name__, exc))
        else:
            print(resolution.chain_text())
    return status


if __name__ == "__main__":
    import sys

    sys.exit(main())
