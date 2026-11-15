"""Reconciliation of structured PE import manifests across nightly re-scans.

A manifest describes the import edges of a collection of PE assets. Each entry
names a DLL (the dependency edge) together with the symbols imported from it
and whether the import is a regular one or a delay-load one:

    [
        {
            "dll": b"kernel32.dll",
            "binding": "normal",
            "symbols": [
                {"name": b"CreateFileW"},
                {"ordinal": 12},
            ],
        },
        {
            "dll": b"user32.dll",
            "binding": "delayed",
            "symbols": [{"name": b"MessageBoxW"}],
        },
    ]

Two nightly manifests are compared by matching DLL names with the library
name comparison rules (case-insensitive, with an optional alias table) and by
matching symbols on their identity (name or ordinal). The comparison keeps the
three situations required by the re-scan workflow apart:

* genuinely added dependencies,
* an existing dependency moving from regular to delay-load imports (or back),
* a mere reordering of the same table entries, including duplicated entries.

A DLL that is absent from a new batch is never treated as an uninstall on its
own. Dependency edges are only deleted when the new batch explicitly claims
complete coverage of the DLL, so a missing or partially collected old file can
not cause edges to be dropped.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

BINDING_NORMAL = "normal"
BINDING_DELAYED = "delayed"

_BINDINGS = (BINDING_NORMAL, BINDING_DELAYED)

COVERAGE_COMPLETE = "complete"


def _as_bytes(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def normalize_dll_name(dll: bytes | str) -> bytes:
    """Return the canonical key used to match DLL names across manifests."""
    return _as_bytes(dll).strip().lower()


def _normalize_binding(entry: dict) -> str:
    binding = entry.get("binding")
    if binding is None:
        if entry.get("delayed"):
            binding = BINDING_DELAYED
        else:
            binding = BINDING_NORMAL
    if binding not in _BINDINGS:
        raise ValueError(f"Unknown import binding {binding!r}")
    return binding


def _normalize_symbol(symbol: dict) -> dict:
    if not isinstance(symbol, dict):
        raise TypeError("Import symbols must be mappings with a name or ordinal")
    name = symbol.get("name")
    if name is not None:
        name = _as_bytes(name)
    ordinal = symbol.get("ordinal")
    if ordinal is not None:
        ordinal = int(ordinal)
    if name is None and ordinal is None:
        raise ValueError("Import symbols must provide either a name or an ordinal")
    normalized = {"name": name, "ordinal": ordinal}
    if "hint" in symbol:
        normalized["hint"] = symbol["hint"]
    return normalized


def normalize_manifest(manifest: Iterable[dict]) -> list[dict]:
    """Return a manifest with bytes names, validated bindings and symbol ids.

    The input is not modified. Entry and symbol ordering, including duplicated
    table entries, is preserved.
    """
    normalized = []
    for entry in manifest:
        if not isinstance(entry, dict):
            raise TypeError("Manifest entries must be mappings with a dll name")
        dll = entry.get("dll")
        if dll is None:
            raise ValueError("Manifest entries must provide a dll name")
        symbols = entry.get("symbols")
        if symbols is None:
            raise ValueError(f"Manifest entry for {dll!r} has no symbols list")
        normalized.append(
            {
                "dll": _as_bytes(dll),
                "binding": _normalize_binding(entry),
                "symbols": [_normalize_symbol(symbol) for symbol in symbols],
            }
        )
    return normalized


def symbol_identity(symbol: dict) -> tuple:
    """Return the hashable identity used to match a symbol across manifests.

    Named imports and ordinal imports live in separate identity spaces, so an
    ordinal can never match a same-numbered name entry.
    """
    name = symbol.get("name")
    if name is not None:
        return "name", _as_bytes(name)
    ordinal = symbol.get("ordinal")
    if ordinal is not None:
        return "ordinal", int(ordinal)
    raise ValueError("Import symbols must provide either a name or an ordinal")


def _identity_sequence(symbols: list[dict]) -> list[tuple]:
    return [symbol_identity(symbol) for symbol in symbols]


def _normalize_alias_map(aliases: dict | None) -> dict:
    if not aliases:
        return {}
    return {
        normalize_dll_name(source): normalize_dll_name(target)
        for source, target in aliases.items()
    }


class LibraryChange:
    """Per-DLL outcome of reconciling two manifests.

    Attributes:
        dll: DLL name as spelled in the new manifest, or the old spelling when
            the DLL is no longer present.
        old_binding: Binding seen in the old manifest ("normal"/"delayed") or
            None for newly added DLLs.
        new_binding: Binding seen in the new manifest or None for removed ones.
        binding_change: One of "added", "removed", "unchanged",
            "normal_to_delayed", "delayed_to_normal" or "mixed_change".
        added_symbols: Symbol entries that only the new manifest contains.
        removed_symbols: Old symbol entries absent from the new manifest.
            Populated for information; the entries only disappear from
            ``merged_manifest`` when ``fully_covered`` is True.
        retained_symbols: Old entries whose identity also appears in the new
            manifest. Duplicate entries are retained with their multiplicity.
        uncovered_missing_symbols: Entries from ``removed_symbols`` that had to
            be kept because the batch does not fully cover the DLL.
        reordered: True when the retained entries keep the same identities but
            appear in a different order in the new manifest.
        fully_covered: Whether the new batch claims complete coverage.
        action: Machine-readable classification of the reconciliation.

    """

    def __init__(self, dll: bytes):
        self.dll = dll
        self.old_binding = None
        self.new_binding = None
        self.binding_change = "unchanged"
        self.added_symbols: list[dict] = []
        self.removed_symbols: list[dict] = []
        self.retained_symbols: list[dict] = []
        self.uncovered_missing_symbols: list[dict] = []
        self.reordered = False
        self.fully_covered = False
        self.action = "unchanged"

    def __repr__(self):
        return (
            f"LibraryChange(dll={self.dll!r}, action={self.action!r}, "
            f"binding_change={self.binding_change!r}, reordered={self.reordered!r})"
        )


class ManifestReconciliation:
    """Result of reconciling an old manifest against a freshly collected one."""

    def __init__(self):
        self.libraries: list[LibraryChange] = []
        self.merged_manifest: list[dict] = []
        self.added_libraries: list[bytes] = []
        self.removed_libraries: list[bytes] = []

    def __iter__(self):
        return iter(self.libraries)

    def __len__(self):
        return len(self.libraries)


def _group_by_dll(manifest: list[dict], aliases: dict) -> OrderedDict[bytes, dict]:
    groups: OrderedDict[bytes, dict] = OrderedDict()
    for entry in manifest:
        key = normalize_dll_name(entry["dll"])
        key = aliases.get(key, key)
        group = groups.get(key)
        if group is None:
            group = {
                "dll": entry["dll"],
                "bindings": set(),
                "symbols": [],
            }
            groups[key] = group
        group["bindings"].add(entry["binding"])
        group["symbols"].extend(entry["symbols"])
    return groups


def _binding_change(old_bindings: set, new_bindings: set) -> str:
    if not old_bindings:
        return "added"
    if not new_bindings:
        return "removed"
    if old_bindings == new_bindings:
        return "unchanged"
    if old_bindings == {BINDING_NORMAL} and new_bindings == {BINDING_DELAYED}:
        return "normal_to_delayed"
    if old_bindings == {BINDING_DELAYED} and new_bindings == {BINDING_NORMAL}:
        return "delayed_to_normal"
    return "mixed_change"


def _coverage_predicate(
    coverage: None | str | Iterable[bytes | str] | Callable[[bytes], bool],
) -> Callable[[bytes], bool]:
    if coverage is None:
        return lambda _dll: False
    if coverage == COVERAGE_COMPLETE:
        return lambda _dll: True
    if callable(coverage):
        return lambda dll: bool(coverage(dll))
    covered = {normalize_dll_name(dll) for dll in coverage}
    return lambda dll: dll in covered


def _diff_groups(
    old_groups: OrderedDict[bytes, dict],
    new_groups: OrderedDict[bytes, dict],
) -> list[LibraryChange]:
    ordered_keys = list(new_groups.keys())
    for key in old_groups:
        if key not in new_groups:
            ordered_keys.append(key)

    changes = []
    for key in ordered_keys:
        old_group = old_groups.get(key)
        new_group = new_groups.get(key)
        change = LibraryChange(new_group["dll"] if new_group else old_group["dll"])

        old_symbols = old_group["symbols"] if old_group else []
        new_symbols = new_group["symbols"] if new_group else []
        old_counter = Counter(_identity_sequence(old_symbols))
        new_counter = Counter(_identity_sequence(new_symbols))

        retained_counts = Counter(
            {identity: min(count, new_counter[identity])
             for identity, count in old_counter.items()}
        )
        remaining = retained_counts.copy()
        for symbol in old_symbols:
            identity = symbol_identity(symbol)
            if remaining[identity] > 0:
                change.retained_symbols.append(symbol)
                remaining[identity] -= 1
            else:
                change.removed_symbols.append(symbol)

        added_counts = new_counter - old_counter
        remaining = added_counts.copy()
        for symbol in new_symbols:
            identity = symbol_identity(symbol)
            if remaining[identity] > 0:
                change.added_symbols.append(symbol)
                remaining[identity] -= 1

        old_retained_order = [
            symbol_identity(symbol) for symbol in change.retained_symbols
        ]
        new_retained_order = []
        retained_seen = Counter()
        for symbol in new_symbols:
            identity = symbol_identity(symbol)
            if retained_seen[identity] < retained_counts[identity]:
                new_retained_order.append(identity)
                retained_seen[identity] += 1
        change.reordered = old_retained_order != new_retained_order

        change.old_binding = (
            next(iter(old_group["bindings"])) if old_group and len(old_group["bindings"]) == 1 else None
        )
        change.new_binding = (
            next(iter(new_group["bindings"])) if new_group and len(new_group["bindings"]) == 1 else None
        )
        change.binding_change = _binding_change(
            old_group["bindings"] if old_group else set(),
            new_group["bindings"] if new_group else set(),
        )
        changes.append(change)

    return changes


def diff_import_manifests(
    old_manifest: Iterable[dict],
    new_manifest: Iterable[dict],
    dll_name_aliases: dict | None = None,
) -> list[LibraryChange]:
    """Compare two manifests without applying any deletion policy.

    The returned :class:`LibraryChange` objects describe additions, removals,
    retained entries, binding transitions and reorderings purely in terms of
    symbol identity. Duplicate entries are handled as multisets, so an entry
    repeated in both manifests is retained repeatedly. The ``action`` field is
    only assigned an authoritative value by
    :func:`reconcile_import_manifests`, which owns the coverage policy.
    """
    aliases = _normalize_alias_map(dll_name_aliases)
    old_groups = _group_by_dll(normalize_manifest(old_manifest), aliases)
    new_groups = _group_by_dll(normalize_manifest(new_manifest), aliases)
    return _diff_groups(old_groups, new_groups)


def reconcile_import_manifests(
    old_manifest: Iterable[dict],
    new_manifest: Iterable[dict],
    coverage: None | str | Iterable[bytes | str] | Callable[[bytes], bool] = None,
    dll_name_aliases: dict | None = None,
) -> ManifestReconciliation:
    """Reconcile a nightly re-scan against the previously collected manifest.

    ``coverage`` declares which DLLs the new batch covers completely. It may be
    ``None`` (nothing is fully covered), the constant
    :data:`COVERAGE_COMPLETE`, an iterable of DLL names or a predicate taking
    the normalized DLL name. Edges of DLLs without complete coverage are always
    retained, even when the DLL or some of its symbols are missing from the new
    batch.

    The returned :class:`ManifestReconciliation` carries both the per-DLL
    classification and the authoritative ``merged_manifest`` built from old
    retained entries plus genuinely new ones.
    """
    aliases = _normalize_alias_map(dll_name_aliases)
    old_groups = _group_by_dll(normalize_manifest(old_manifest), aliases)
    new_groups = _group_by_dll(normalize_manifest(new_manifest), aliases)
    is_covered = _coverage_predicate(coverage)

    result = ManifestReconciliation()
    changes = _diff_groups(old_groups, new_groups)

    for change in changes:
        key = normalize_dll_name(change.dll)
        key = aliases.get(key, key)
        old_group = old_groups.get(key)
        new_group = new_groups.get(key)
        change.fully_covered = is_covered(key)

        if change.binding_change == "added":
            change.action = "added"
            result.added_libraries.append(change.dll)
        elif change.binding_change == "removed":
            if change.fully_covered:
                change.action = "edge_removed"
                result.removed_libraries.append(change.dll)
            else:
                change.action = "retained_incomplete_coverage"
                change.uncovered_missing_symbols = list(change.removed_symbols)
        elif (
            change.added_symbols
            or change.removed_symbols
            or change.reordered
            or change.binding_change != "unchanged"
        ):
            change.action = "updated"
            if not change.fully_covered and change.removed_symbols:
                change.uncovered_missing_symbols = list(change.removed_symbols)
        else:
            change.action = "unchanged"
        result.libraries.append(change)

        if change.binding_change == "removed" and change.fully_covered:
            continue

        if new_group is not None:
            binding = next(iter(new_group["bindings"]))
            if len(new_group["bindings"]) > 1:
                binding = BINDING_DELAYED if BINDING_DELAYED in new_group["bindings"] else BINDING_NORMAL
        else:
            binding = next(iter(old_group["bindings"]))

        if change.fully_covered:
            # The new batch is authoritative for covered DLLs: its ordering
            # wins and symbols it no longer contains are dropped.
            merged_symbols = list(new_group["symbols"])
        else:
            # Coverage is incomplete: keep every old entry (including ones the
            # new batch failed to collect) and append the genuinely new ones.
            merged_symbols = list(old_group["symbols"]) if old_group else []
            merged_symbols.extend(change.added_symbols)

        result.merged_manifest.append(
            {"dll": change.dll, "binding": binding, "symbols": merged_symbols}
        )

    return result


def extract_manifest_from_pe(pe: object) -> list[dict]:
    """Build a manifest from a parsed :class:`pefile.PE` instance.

    Regular imports come from ``DIRECTORY_ENTRY_IMPORT`` and delay-load imports
    from ``DIRECTORY_ENTRY_DELAY_IMPORT``. Symbols without a resolved name are
    represented by their ordinal. The PE object is only read, never modified.
    """
    manifest = []
    for attribute, binding in (
        ("DIRECTORY_ENTRY_IMPORT", BINDING_NORMAL),
        ("DIRECTORY_ENTRY_DELAY_IMPORT", BINDING_DELAYED),
    ):
        entries = getattr(pe, attribute, None)
        if not entries:
            continue
        for entry in entries:
            symbols = []
            for symbol in entry.imports:
                if getattr(symbol, "name", None) is not None:
                    normalized = {"name": symbol.name}
                else:
                    normalized = {"ordinal": symbol.ordinal}
                if getattr(symbol, "hint", None) is not None:
                    normalized["hint"] = symbol.hint
                symbols.append(normalized)
            manifest.append(
                {"dll": entry.dll, "binding": binding, "symbols": symbols}
            )
    return manifest
