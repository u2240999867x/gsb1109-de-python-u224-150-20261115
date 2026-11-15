"""Resolve PE export forwarding chains across an offline set of DLLs.

Windows DLLs may export a symbol whose RVA points inside the export
directory.  Such an entry is not code: it is an ASCII string of the form
``MODULE.Symbol`` or ``MODULE.#Ordinal`` that the loader follows instead.
This module walks those chains across a set of DLL files that are available
locally, and reports where every export eventually lands.

The resolver is deliberately explicit about two things that cannot be
guessed from a file name:

* When several loaded libraries share a name (for example two builds of
  ``version.dll``), the caller selects the one to use, either up front with
  :meth:`ExportResolver.select_version` / :meth:`ExportResolver.select_path`
  or through a ``version_selector`` callback.
* A forwarding RVA is recognised from the export directory range declared
  in the data directory.  An address inside that range is never treated as
  local code, even when the bytes there do not parse as a valid forwarder.

Typical use::

    resolver = peforward.ExportResolver()
    resolver.add_directory("vendor/bin")
    resolver.select_version("version", "10.0.19041.1")
    report = resolver.report()
    print(peforward.format_report(report))

The same functionality is available from the command line::

    python peforward.py vendor/bin --json
    python peforward.py vendor/bin --dll kernel32 --name Sleep
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pefile

# Resolution statuses.  They are plain strings so reports stay JSON-friendly.
LOCAL = "local"
MISSING_LIBRARY = "missing_library"
MISSING_EXPORT = "missing_export"
AMBIGUOUS_LIBRARY = "ambiguous_library"
AMBIGUOUS_EXPORT = "ambiguous_export"
CYCLE = "cycle"
DEPTH_EXCEEDED = "depth_exceeded"
INVALID_FORWARDER = "invalid_forwarder"
NOT_FOUND = "not_found"
ERROR = "error"

ALL_STATUSES = (
    LOCAL,
    MISSING_LIBRARY,
    MISSING_EXPORT,
    AMBIGUOUS_LIBRARY,
    AMBIGUOUS_EXPORT,
    CYCLE,
    DEPTH_EXCEEDED,
    INVALID_FORWARDER,
    NOT_FOUND,
    ERROR,
)


DEFAULT_MAX_DEPTH = 0  # 0 means follow until local code or a detected cycle
DLL_EXTENSIONS = (".dll", ".ocx", ".sys")


def _preferred_export(exports):
    """Pick a deterministic representative among alias exports."""

    named = [item for item in exports if getattr(item, "name", None)]
    pool = named or list(exports)
    return min(pool, key=lambda item: (getattr(item, "name", b"") or b"", item.ordinal))


def _text(value):
    """Return a printable string for a name that may be bytes."""

    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "backslashreplace")
    return str(value)


def normalize_export_name(name):
    """Normalize an export name to the bytes form used by ``pefile``.

    Callers may pass either ``str`` or ``bytes``; export names read from a
    PE file are byte strings.
    """

    if name is None or isinstance(name, bytes):
        return name
    if isinstance(name, bytearray):
        return bytes(name)
    return str(name).encode("latin-1")


def normalize_module_name(name):
    """Normalize a module or file name for lookup.

    Lookups are case-insensitive, ignore any directory component, and treat
    ``foo`` and ``foo.dll`` as the same module.  This mirrors how forwarder
    strings are written: they normally omit the extension, while files on
    disk carry it.
    """

    if name is None:
        return ""
    if isinstance(name, bytes):
        name = name.decode("latin-1")
    name = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    lowered = name.lower()
    for extension in DLL_EXTENSIONS:
        if lowered.endswith(extension):
            lowered = lowered[: -len(extension)]
            break
    return lowered


def module_name_from_path(path):
    """Derive a module name from a file path."""

    return normalize_module_name(os.path.basename(str(path)))


class ForwardTarget:
    """A parsed forwarder string: module plus symbol name or ordinal."""

    __slots__ = ("module", "name", "ordinal", "raw")

    def __init__(self, module, name=None, ordinal=None, raw=None):
        self.module = module
        self.name = name
        self.ordinal = ordinal
        self.raw = raw

    @property
    def symbol(self):
        if self.ordinal is not None:
            return "#%d" % self.ordinal
        return _text(self.name)

    def to_dict(self):
        return {
            "module": self.module,
            "name": _text(self.name),
            "ordinal": self.ordinal,
            "raw": self.raw,
        }

    def __repr__(self):
        return "ForwardTarget(%r, name=%r, ordinal=%r)" % (
            self.module,
            _text(self.name),
            self.ordinal,
        )


class ForwarderParseError(ValueError):
    """Raised when an export string is inside the forwarder range but invalid."""


def parse_forwarder(raw):
    """Parse ``MODULE.Symbol`` / ``MODULE.#Ordinal`` into a ForwardTarget.

    Raises :class:`ForwarderParseError` for malformed strings.  The split is
    done on the last dot so module names that contain dots (for example
    ``api-ms-win-core-file-l1-1-0.dll``) are preserved.
    """

    if isinstance(raw, bytes):
        text = raw.decode("latin-1")
    elif raw is None:
        raise ForwarderParseError("empty forwarder string")
    else:
        text = str(raw)

    text = text.strip()
    if not text:
        raise ForwarderParseError("empty forwarder string")

    module, separator, symbol = text.rpartition(".")
    if not separator or not module or not symbol:
        raise ForwarderParseError(
            "forwarder string %r is not MODULE.Symbol" % text
        )

    module = module.strip()
    symbol = symbol.strip()
    if not module:
        raise ForwarderParseError("forwarder string %r has an empty module" % text)
    if not symbol:
        raise ForwarderParseError("forwarder string %r has an empty symbol" % text)

    if symbol.startswith("#"):
        digits = symbol[1:]
        if not digits.isdigit():
            raise ForwarderParseError(
                "forwarder string %r has an invalid ordinal" % text
            )
        return ForwardTarget(module, ordinal=int(digits), raw=text)

    return ForwardTarget(module, name=symbol.encode("latin-1"), raw=text)


class Library:
    """A single loaded DLL and its export table.

    ``name`` is the normalized lookup name, ``display_name`` is the name to
    show in reports, and ``version`` is an opaque caller-supplied label used
    to tell same-named libraries apart.
    """

    def __init__(self, pe, name, display_name=None, path=None, version=None):
        self.pe = pe
        self.name = normalize_module_name(name)
        self.display_name = display_name or name
        self.path = str(path) if path is not None else None
        self.version = version
        self.image_base = int(getattr(pe.OPTIONAL_HEADER, "ImageBase", 0) or 0)

        export_entry = None
        try:
            export_entry = pe.OPTIONAL_HEADER.DATA_DIRECTORY[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]
            ]
        except (AttributeError, IndexError):
            export_entry = None

        self.export_rva = int(getattr(export_entry, "VirtualAddress", 0) or 0)
        self.export_size = int(getattr(export_entry, "Size", 0) or 0)
        self.exports = []
        self.exports_by_name = {}
        self.exports_by_ordinal = {}

        if not hasattr(pe, "DIRECTORY_ENTRY_EXPORT") and self.export_rva:
            # fast_load defers directory parsing.  Parse just the export
            # directory so this module works without forcing a full load.
            try:
                export_dir = pe.parse_export_directory(
                    self.export_rva, self.export_size
                )
            except Exception:  # noqa: BLE001 - malformed directory
                export_dir = None
            if export_dir is not None:
                pe.DIRECTORY_ENTRY_EXPORT = export_dir

        export_dir = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
        if export_dir is None or not hasattr(export_dir, "symbols"):
            return

        for export in export_dir.symbols:
            if export is None or getattr(export, "address", None) is None:
                continue
            self.exports.append(export)
            ordinal = getattr(export, "ordinal", None)
            if ordinal is not None:
                self.exports_by_ordinal.setdefault(ordinal, []).append(export)
            name = getattr(export, "name", None)
            if name:
                self.exports_by_name.setdefault(name, []).append(export)

    def forwarder_range(self):
        """Return the half-open export directory range used for forwarding."""

        if not self.export_size:
            return None
        return (self.export_rva, self.export_rva + self.export_size)

    def is_forwarder_rva(self, rva):
        """Whether ``rva`` lies in the declared export directory range."""

        bounds = self.forwarder_range()
        return bool(bounds and bounds[0] <= rva < bounds[1])

    def forwarder_bytes(self, export):
        """Return the forwarder bytes for ``export`` or raise PEFormatError."""

        raw = getattr(export, "forwarder", None)
        if raw is not None:
            return raw
        return self.pe.get_string_at_rva(export.address)

    def is_forwarder(self, export):
        # The declared range is authoritative: an export whose RVA falls in
        # that range is never local code.  The parser's own flag is also
        # honoured in case a producer wrote an inconsistent directory size.
        return self.is_forwarder_rva(export.address) or getattr(
            export, "forwarder", None
        ) is not None

    def to_dict(self):
        bounds = self.forwarder_range()
        return {
            "name": self.display_name,
            "lookup_name": self.name,
            "path": self.path,
            "version": self.version,
            "image_base": self.image_base,
            "export_rva": self.export_rva,
            "export_size": self.export_size,
            "forwarder_range": list(bounds) if bounds else None,
            "export_count": len(self.exports),
        }

    def __repr__(self):
        return "Library(%r, path=%r, version=%r)" % (
            self.display_name,
            self.path,
            self.version,
        )


class Hop:
    """One export table entry visited while following a chain."""

    __slots__ = (
        "library",
        "path",
        "version",
        "name",
        "ordinal",
        "rva",
        "address",
        "forwarder",
        "target",
    )

    def __init__(
        self,
        library,
        name=None,
        ordinal=None,
        rva=None,
        address=None,
        forwarder=None,
        target=None,
        path=None,
        version=None,
    ):
        self.library = library
        self.path = path
        self.version = version
        self.name = name
        self.ordinal = ordinal
        self.rva = rva
        self.address = address
        self.forwarder = forwarder
        self.target = target

    @property
    def symbol(self):
        if self.name:
            return _text(self.name)
        if self.ordinal is not None:
            return "#%d" % self.ordinal
        return "?"

    def to_dict(self):
        return {
            "library": self.library,
            "path": self.path,
            "version": self.version,
            "name": _text(self.name),
            "ordinal": self.ordinal,
            "rva": self.rva,
            "address": self.address,
            "forwarder": self.forwarder,
            "target": self.target.to_dict() if self.target else None,
        }

    def label(self):
        return "%s!%s" % (self.library, self.symbol)

    def __repr__(self):
        return "Hop(%r, rva=%r, forwarder=%r)" % (
            self.label(),
            self.rva,
            self.forwarder,
        )


class Resolution:
    """The outcome of following one export to its terminal state."""

    def __init__(self, source, name=None, ordinal=None):
        self.source = source
        self.name = name
        self.ordinal = ordinal
        self.status = NOT_FOUND
        self.chain = []
        self.cycle = []
        self.cycle_start = None
        self.target_library = None
        self.target_path = None
        self.target_version = None
        self.target_name = None
        self.target_ordinal = None
        self.target_rva = None
        self.target_address = None
        self.missing_library = None
        self.missing_symbol = None
        self.candidates = []
        self.message = None

    @property
    def ok(self):
        return self.status == LOCAL

    def to_dict(self):
        return {
            "source_library": self.source.display_name
            if isinstance(self.source, Library)
            else self.source,
            "source_path": (
                self.source.path if isinstance(self.source, Library) else None
            ),
            "name": _text(self.name),
            "ordinal": self.ordinal,
            "status": self.status,
            "message": self.message,
            "chain": [hop.to_dict() for hop in self.chain],
            "cycle": [hop.to_dict() for hop in self.cycle],
            "cycle_start": self.cycle_start,
            "target": {
                "library": self.target_library,
                "path": self.target_path,
                "version": self.target_version,
                "name": _text(self.target_name),
                "ordinal": self.target_ordinal,
                "rva": self.target_rva,
                "address": self.target_address,
            }
            if self.status == LOCAL
            else None,
            "missing_library": self.missing_library,
            "missing_symbol": _text(self.missing_symbol),
            "candidates": self.candidates,
        }

    def label(self):
        source = (
            self.source.display_name
            if isinstance(self.source, Library)
            else self.source
        )
        if self.name:
            return "%s!%s" % (source, _text(self.name))
        return "%s!#%s" % (source, self.ordinal)

    def __repr__(self):
        return "Resolution(%r, status=%r)" % (self.label(), self.status)


class ExportResolver:
    """Resolve exports across a caller-managed set of PE libraries.

    ``version_selector`` is an optional callback invoked as
    ``selector(name, candidates)`` whenever more than one library matches a
    forwarder target and no explicit selection was made.  It must return one
    of ``candidates`` (or its index) or ``None`` to leave the target
    ambiguous.  The resolver never picks a version on its own.
    """

    def __init__(
        self,
        libraries=None,
        version_selector=None,
        max_depth=DEFAULT_MAX_DEPTH,
        case_insensitive_exports=False,
    ):
        self.max_depth = int(max_depth)
        if self.max_depth < 0:
            raise ValueError("max_depth must be 0 (unbounded) or positive")
        self.version_selector = version_selector
        self.case_insensitive_exports = bool(case_insensitive_exports)
        self.libraries = []
        self.load_errors = []
        self._by_name = {}
        self._selection = {}
        if libraries:
            for library in libraries:
                if isinstance(library, Library):
                    self.add_library_object(library)
                elif isinstance(library, str):
                    self.add_library(library)

    # -- collection -----------------------------------------------------

    def add_library_object(self, library):
        self.libraries.append(library)
        self._by_name.setdefault(library.name, []).append(library)
        return library

    def add_pe(self, pe, name=None, version=None, path=None):
        """Add an already-parsed :class:`pefile.PE` object."""

        if name is None:
            name = module_name_from_path(path) if path else "unnamed"
        display_name = os.path.basename(str(path)) if path else name
        return self.add_library_object(
            Library(
                pe,
                name,
                display_name=display_name,
                path=path,
                version=version,
            )
        )

    def add_library(self, path, name=None, version=None):
        """Parse and add one DLL file.

        ``name`` overrides the lookup name derived from the file name.
        ``version`` is an arbitrary caller label (a version resource string,
        a timestamp, ``"x86"``) used to disambiguate same-named files.
        """

        pe = pefile.PE(str(path))
        return self.add_pe(pe, name=name, version=version, path=path)

    def add_directory(self, directory, recursive=True, extensions=DLL_EXTENSIONS):
        """Add every matching library below ``directory``.

        Load failures are recorded in :attr:`load_errors`; one unreadable
        file never prevents the remaining files from being resolved.
        """

        extensions = tuple(extension.lower() for extension in extensions)
        for path in iter_library_files(
            directory, recursive=recursive, extensions=extensions
        ):
            try:
                self.add_library(path)
            except Exception as error:  # noqa: BLE001 - report and keep going
                self.load_errors.append(
                    {
                        "path": str(path),
                        "error": "%s: %s" % (type(error).__name__, error),
                    }
                )

    def candidates(self, name):
        return list(self._by_name.get(normalize_module_name(name), ()))

    # -- caller-controlled version selection ---------------------------

    def select_version(self, name, version):
        """Use the library whose caller label equals ``version``."""

        self._selection[normalize_module_name(name)] = ("version", version)

    def select_path(self, name, path):
        """Use the library loaded from ``path`` (compared as real paths)."""

        self._selection[normalize_module_name(name)] = (
            "path",
            os.path.realpath(str(path)),
        )

    def clear_selection(self, name=None):
        if name is None:
            self._selection.clear()
        else:
            self._selection.pop(normalize_module_name(name), None)

    def _choose(self, name, candidates):
        """Return ``(library, error_message)`` for a forwarder target name."""

        if not candidates:
            return None, None

        selection = self._selection.get(normalize_module_name(name))
        if selection is not None:
            kind, wanted = selection
            if kind == "version":
                matches = [item for item in candidates if item.version == wanted]
            else:
                matches = [
                    item
                    for item in candidates
                    if item.path is not None
                    and os.path.realpath(item.path) == wanted
                ]
            if len(matches) == 1:
                return matches[0], None
            return None, (
                "selection %s=%r matched %d libraries named %r"
                % (kind, wanted, len(matches), name)
            )

        if len(candidates) == 1:
            return candidates[0], None

        if self.version_selector is not None:
            try:
                chosen = self.version_selector(name, tuple(candidates))
            except Exception as error:  # noqa: BLE001 - caller callback
                return None, "version selector failed: %s" % error
            if chosen is None:
                return None, None
            if isinstance(chosen, int) and not isinstance(chosen, bool):
                if 0 <= chosen < len(candidates):
                    return candidates[chosen], None
                return None, "version selector returned an out-of-range index"
            for item in candidates:
                if chosen is item or (
                    isinstance(chosen, str)
                    and item.path is not None
                    and os.path.realpath(item.path) == os.path.realpath(chosen)
                ):
                    return item, None
            return None, "version selector returned an unknown library"

        return None, None

    def _targets(self, library_name):
        candidates = self.candidates(library_name)
        chosen, error = self._choose(library_name, candidates)
        return chosen, candidates, error

    # -- lookup helpers -------------------------------------------------

    def _find_export(self, library, name=None, ordinal=None, forwarder=None):
        """Find an export by name/ordinal.

        Returns ``(export, candidates)``; ``candidates`` is a non-empty list
        only when the lookup is ambiguous.
        """

        if forwarder is not None:
            name = None if forwarder.ordinal is not None else forwarder.name
            ordinal = forwarder.ordinal

        name = normalize_export_name(name)

        if ordinal is not None:
            matches = library.exports_by_ordinal.get(ordinal, [])
            if name is not None and matches:
                # For an explicit (name, ordinal) lookup a name mismatch
                # means the export is absent, not that the ordinal is
                # ambiguous.
                name_matches = [
                    item
                    for item in matches
                    if getattr(item, "name", None) == name
                ]
                if not name_matches:
                    return None, []
                matches = name_matches
            if len(matches) > 1:
                # Names that alias one ordinal share the AddressOfFunctions
                # slot and therefore the same RVA.  Collapse aliases when
                # they agree; only genuinely conflicting entries are
                # ambiguous.
                addresses = {getattr(item, "address", None) for item in matches}
                if len(addresses) == 1:
                    return _preferred_export(matches), []
                return None, matches
            return (matches[0] if matches else None), []

        key = name
        if self.case_insensitive_exports and isinstance(key, bytes):
            wanted = key.lower()
            matches = []
            for exported_name, exports in library.exports_by_name.items():
                if exported_name.lower() == wanted:
                    matches.extend(exports)
        else:
            matches = library.exports_by_name.get(key, [])

        if len(matches) > 1:
            return None, matches
        return (matches[0] if matches else None), []

    def _missing_symbol(self, name, ordinal, forwarder):
        if forwarder is not None:
            if forwarder.ordinal is not None:
                return "#%d" % forwarder.ordinal
            return _text(forwarder.name)
        if name is not None:
            return _text(name)
        return "#%d" % ordinal if ordinal is not None else None

    def _hop(self, library, export):
        raw = None
        if library.is_forwarder(export):
            try:
                raw_bytes = library.forwarder_bytes(export)
                raw = _text(raw_bytes)
            except Exception:  # noqa: BLE001 - malformed directory data
                raw = None
        return Hop(
            library=library.display_name,
            path=library.path,
            version=library.version,
            name=getattr(export, "name", None),
            ordinal=getattr(export, "ordinal", None),
            rva=getattr(export, "address", None),
            address=None
            if getattr(export, "address", None) is None
            else library.image_base + export.address,
            forwarder=raw,
        )

    # -- resolution -----------------------------------------------------

    def resolve(self, library, name=None, ordinal=None):
        """Resolve one export selected by name, by ordinal, or both.

        ``library`` may be a :class:`Library` object or a module name.  When
        a name has several loaded versions and no selection was made, the
        result is ``ambiguous_library`` rather than an arbitrary choice.
        """

        result = Resolution(library, name=name, ordinal=ordinal)

        if isinstance(library, Library):
            chosen = library
            candidates = [library]
        else:
            chosen, candidates, error = self._targets(library)
            result.source = chosen if chosen is not None else library
            if chosen is None:
                result.status = AMBIGUOUS_LIBRARY if candidates else MISSING_LIBRARY
                result.missing_library = None if candidates else _text(library)
                result.candidates = [item.to_dict() for item in candidates]
                result.message = error or (
                    "no loaded library named %r" % _text(library)
                    if not candidates
                    else "several libraries named %r; caller must select a version"
                    % _text(library)
                )
                return result

        if name is None and ordinal is None:
            result.status = NOT_FOUND
            result.message = "either name or ordinal is required"
            return result

        export, matches = self._find_export(chosen, name=name, ordinal=ordinal)
        if export is None:
            if matches:
                result.status = AMBIGUOUS_EXPORT
                result.candidates = [
                    self._hop(chosen, item).to_dict() for item in matches
                ]
                result.message = "several exports match in %r" % chosen.display_name
            else:
                result.status = NOT_FOUND
                result.missing_library = chosen.display_name
                result.missing_symbol = (
                    "#%d" % ordinal if ordinal is not None else _text(name)
                )
                result.message = "export not found in %r" % chosen.display_name
            return result

        return self._resolve_export(chosen, export, result)

    def resolve_library(self, library):
        """Resolve every export of one library, isolating per-export errors."""

        if not isinstance(library, Library):
            chosen, candidates, error = self._targets(library)
            if chosen is None:
                result = Resolution(library)
                result.status = AMBIGUOUS_LIBRARY if candidates else MISSING_LIBRARY
                result.candidates = [item.to_dict() for item in candidates]
                result.message = error
                return [result]
            library = chosen
        return [self._resolve_one(library, export) for export in library.exports]

    def _resolve_one(self, library, export):
        try:
            return self._resolve_export(library, export, Resolution(library))
        except Exception as error:  # noqa: BLE001 - isolate one export
            result = Resolution(library)
            result.name = getattr(export, "name", None)
            result.ordinal = getattr(export, "ordinal", None)
            result.status = ERROR
            result.message = "%s: %s" % (type(error).__name__, error)
            return result

    def resolve_all(self):
        """Resolve every export of every loaded library.

        Each export is independent: a missing or ambiguous target is recorded
        on that entry only, and resolution continues with the next one.
        """

        results = []
        for library in self.libraries:
            results.extend(
                self._resolve_one(library, export) for export in library.exports
            )
        return results

    def _resolve_export(self, library, export, result):
        result.name = getattr(export, "name", None)
        result.ordinal = getattr(export, "ordinal", None)

        current_library = library
        current_export = export
        visited = {}

        while True:
            key = (id(current_library), getattr(current_export, "ordinal", None))
            if key in visited:
                cycle_start = visited[key]
                result.cycle_start = cycle_start
                result.cycle = list(result.chain[cycle_start:])
                closing = self._hop(current_library, current_export)
                result.cycle.append(closing)
                result.chain.append(closing)
                result.status = CYCLE
                result.message = "forwarding cycle: " + " -> ".join(
                    hop.label() for hop in result.cycle
                )
                return result

            if self.max_depth and len(result.chain) >= self.max_depth:
                result.status = DEPTH_EXCEEDED
                result.message = (
                    "forwarding chain exceeded max_depth=%d" % self.max_depth
                )
                return result

            visited[key] = len(result.chain)
            hop = self._hop(current_library, current_export)
            result.chain.append(hop)

            if not current_library.is_forwarder(current_export):
                # Outside the declared export directory range this RVA is a
                # local address, even if the bytes it points at happen to
                # look like a forwarder string.
                result.status = LOCAL
                result.target_library = current_library.display_name
                result.target_path = current_library.path
                result.target_version = current_library.version
                result.target_name = getattr(current_export, "name", None)
                result.target_ordinal = getattr(current_export, "ordinal", None)
                result.target_rva = current_export.address
                result.target_address = (
                    current_library.image_base + current_export.address
                )
                return result

            # The RVA is inside the export directory: it must be a string.
            if hop.forwarder is None:
                try:
                    raw = current_library.forwarder_bytes(current_export)
                except Exception as error:  # noqa: BLE001 - corrupt export data
                    result.status = INVALID_FORWARDER
                    result.message = "cannot read forwarder string: %s" % error
                    return result
                hop.forwarder = _text(raw)

            try:
                target = parse_forwarder(hop.forwarder)
            except ForwarderParseError as error:
                result.status = INVALID_FORWARDER
                result.message = str(error)
                return result

            hop.target = target
            target_library, candidates, error = self._targets(target.module)
            if target_library is None:
                if candidates:
                    result.status = AMBIGUOUS_LIBRARY
                    result.candidates = [item.to_dict() for item in candidates]
                    result.message = error or (
                        "several libraries named %r; caller must select a version"
                        % target.module
                    )
                else:
                    result.status = MISSING_LIBRARY
                    result.missing_library = target.module
                    result.missing_symbol = target.symbol
                    result.message = (
                        "forwarded library %r is not available" % target.module
                    )
                return result

            next_export, matches = self._find_export(
                target_library, forwarder=target
            )
            if next_export is None:
                if matches:
                    result.status = AMBIGUOUS_EXPORT
                    result.candidates = [
                        self._hop(target_library, item).to_dict() for item in matches
                    ]
                    result.message = "several exports match in %r" % (
                        target_library.display_name
                    )
                else:
                    result.status = MISSING_EXPORT
                    result.missing_library = target_library.display_name
                    result.missing_symbol = target.symbol
                    result.message = "export %s!%s not found" % (
                        target_library.display_name,
                        target.symbol,
                    )
                return result

            current_library = target_library
            current_export = next_export

    # -- reporting ------------------------------------------------------

    def report(self, results=None):
        """Build a JSON-friendly report for the loaded libraries."""

        if results is None:
            results = self.resolve_all()

        duplicate_names = {
            name: [item.to_dict() for item in items]
            for name, items in sorted(self._by_name.items())
            if len(items) > 1
        }
        summary = {status: 0 for status in ALL_STATUSES}
        cycles = {}
        for result in results:
            summary[result.status] = summary.get(result.status, 0) + 1
            if result.status == CYCLE:
                signature = _canonical_cycle(
                    [hop.label() for hop in result.cycle]
                )
                cycles.setdefault(signature, {"cycle": signature, "entries": 0})
                cycles[signature]["entries"] += 1

        return {
            "libraries": [library.to_dict() for library in self.libraries],
            "duplicate_names": duplicate_names,
            "load_errors": list(self.load_errors),
            "summary": summary,
            "cycles": [cycles[key] for key in sorted(cycles)],
            "resolutions": [result.to_dict() for result in results],
        }


def iter_library_files(directory, recursive=True, extensions=DLL_EXTENSIONS):
    """Yield DLL-like files below ``directory`` in deterministic order."""

    extensions = tuple(extension.lower() for extension in extensions)
    directory = str(directory)
    if os.path.isfile(directory):
        yield directory
        return
    if not recursive:
        entries = [os.path.join(directory, name) for name in os.listdir(directory)]
        for path in sorted(entries, key=lambda item: item.lower()):
            if os.path.isfile(path) and path.lower().endswith(extensions):
                yield path
        return

    for root, dirs, files in os.walk(directory):
        dirs.sort(key=lambda item: item.lower())
        for name in sorted(files, key=lambda item: item.lower()):
            if name.lower().endswith(extensions):
                yield os.path.join(root, name)


def resolve_directory(
    directory,
    recursive=True,
    version_selector=None,
    max_depth=DEFAULT_MAX_DEPTH,
):
    """Convenience helper: load a directory and resolve every export."""

    resolver = ExportResolver(
        version_selector=version_selector, max_depth=max_depth
    )
    resolver.add_directory(directory, recursive=recursive)
    return resolver, resolver.resolve_all()


def _canonical_cycle(labels):
    """Return a rotation-independent signature for one cycle.

    The closing label (a repeat of the first) is dropped, the remaining
    labels are rotated so the lexicographically smallest one comes first, and
    the first label is repeated at the end so the closed loop stays visible.
    """

    if not labels:
        return tuple()
    body = list(labels)
    if len(body) > 1 and body[-1] == body[0]:
        body = body[:-1]
    if not body:
        return tuple()
    smallest = min(range(len(body)), key=lambda index: body[index])
    rotated = body[smallest:] + body[:smallest]
    return tuple(rotated + [rotated[0]])


def format_resolution(result):
    """Format one :class:`Resolution` as a single human-readable line."""

    prefix = result.status
    label = result.label()
    if result.status == LOCAL:
        return "%s %s -> %s RVA 0x%X VA 0x%X" % (
            prefix,
            label,
            result.target_library,
            result.target_rva,
            result.target_address,
        )
    if result.status == CYCLE:
        return "%s %s" % (
            prefix,
            " -> ".join(hop.label() for hop in result.cycle),
        )
    if result.status in (MISSING_LIBRARY, MISSING_EXPORT, AMBIGUOUS_LIBRARY):
        return "%s %s -> %s (%s)" % (
            prefix,
            label,
            result.missing_library or "?",
            result.message,
        )
    return "%s %s (%s)" % (prefix, label, result.message)


def format_report(report):
    """Format a report dictionary as a compact human-readable summary."""

    lines = []
    for library in report["libraries"]:
        version = (
            " version=%s" % library["version"]
            if library["version"] is not None
            else ""
        )
        path = " %s" % library["path"] if library["path"] else ""
        lines.append(
            "library %s%s%s exports=%d"
            % (library["name"], version, path, library["export_count"])
        )

    if report["duplicate_names"]:
        lines.append("")
        lines.append("same-named libraries (caller must select a version):")
        for name, items in report["duplicate_names"].items():
            for item in items:
                lines.append(
                    "  %s version=%r path=%s"
                    % (name, item["version"], item["path"])
                )

    if report["load_errors"]:
        lines.append("")
        lines.append("load errors:")
        for item in report["load_errors"]:
            lines.append("  %s: %s" % (item["path"], item["error"]))

    lines.append("")
    lines.append("resolutions:")
    for result in report["resolutions"]:
        lines.append("  " + _format_resolution_dict(result))

    lines.append("")
    lines.append(
        "summary: "
        + " ".join(
            "%s=%d" % (status, report["summary"].get(status, 0))
            for status in ALL_STATUSES
            if report["summary"].get(status)
        )
    )

    if report["cycles"]:
        lines.append("")
        lines.append("cycles:")
        for cycle in report["cycles"]:
            lines.append(
                "  %s (from %d export%s)"
                % (
                    " -> ".join(cycle["cycle"]),
                    cycle["entries"],
                    "" if cycle["entries"] == 1 else "s",
                )
            )

    return "\n".join(lines)


def _format_resolution_dict(result):
    source = result["source_library"]
    if result["name"] is not None:
        label = "%s!%s" % (source, result["name"])
    else:
        label = "%s!#%s" % (source, result["ordinal"])

    if result["status"] == LOCAL:
        target = result["target"]
        return "local %s -> %s RVA 0x%X VA 0x%X" % (
            label,
            target["library"],
            target["rva"],
            target["address"],
        )
    if result["status"] == CYCLE:
        return "cycle %s" % " -> ".join(
            "%s!%s"
            % (
                hop["library"],
                hop["name"] if hop["name"] is not None else "#%s" % hop["ordinal"],
            )
            for hop in result["cycle"]
        )
    return "%s %s (%s)" % (result["status"], label, result["message"])


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="peforward",
        description="Resolve PE export forwarding chains across offline DLLs.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="DLL files or directories containing DLLs",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="do not descend into subdirectories",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the full report as JSON",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="write the report to FILE instead of stdout",
    )
    parser.add_argument(
        "--dll",
        metavar="NAME",
        help="only report exports of this library (name or file stem)",
    )
    parser.add_argument(
        "--name",
        metavar="SYMBOL",
        help="resolve one export by name (requires --dll)",
    )
    parser.add_argument(
        "--ordinal",
        type=int,
        metavar="N",
        help="resolve one export by ordinal (requires --dll)",
    )
    parser.add_argument(
        "--select-version",
        action="append",
        default=[],
        metavar="NAME=VERSION",
        help="pick a same-named library by caller version label (repeatable)",
    )
    parser.add_argument(
        "--select-path",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="pick a same-named library by file path (repeatable)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help="stop after N forwarder hops (default: 0, follow until local code "
        "or a cycle is detected)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any export is not resolved locally",
    )
    return parser


def _parse_assignment(value, option):
    name, separator, chosen = value.partition("=")
    if not separator or not name or not chosen:
        raise ValueError("%s expects NAME=VALUE, got %r" % (option, value))
    return name, chosen


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if (args.name is not None or args.ordinal is not None) and args.dll is None:
        parser.error("--name/--ordinal require --dll")
    if args.name is not None and args.ordinal is not None:
        parser.error("use either --name or --ordinal, not both")

    resolver = ExportResolver(max_depth=args.max_depth)

    for value in args.select_version:
        try:
            name, version = _parse_assignment(value, "--select-version")
        except ValueError as error:
            parser.error(str(error))
        resolver.select_version(name, version)
    for value in args.select_path:
        try:
            name, path = _parse_assignment(value, "--select-path")
        except ValueError as error:
            parser.error(str(error))
        resolver.select_path(name, path)

    for path in args.paths:
        if not os.path.exists(path):
            parser.error("path does not exist: %s" % path)
        if os.path.isdir(path):
            resolver.add_directory(path, recursive=not args.no_recursive)
        else:
            try:
                resolver.add_library(path)
            except Exception as error:  # noqa: BLE001 - keep processing other files
                resolver.load_errors.append(
                    {"path": path, "error": "%s: %s" % (type(error).__name__, error)}
                )

    if args.dll is not None:
        if args.name is not None or args.ordinal is not None:
            result = resolver.resolve(args.dll, name=args.name, ordinal=args.ordinal)
            results = [result]
        else:
            chosen, candidates, error = resolver._targets(args.dll)
            if chosen is None:
                results = []
            else:
                results = resolver.resolve_library(chosen)
    else:
        results = resolver.resolve_all()

    report = resolver.report(results=results)
    if args.dll is not None:
        chosen, candidates, error = resolver._targets(args.dll)
        if chosen is None:
            report["error"] = error or "no unambiguous library named %r" % args.dll
            report["candidates"] = [item.to_dict() for item in candidates]

    if args.json:
        rendered = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    else:
        rendered = format_report(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            if not rendered.endswith("\n"):
                stream.write("\n")
    else:
        print(rendered)

    if args.strict and any(
        result["status"] != LOCAL for result in report["resolutions"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
