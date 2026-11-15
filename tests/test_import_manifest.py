import types

import pytest

from importmanifest import (
    BINDING_DELAYED,
    BINDING_NORMAL,
    COVERAGE_COMPLETE,
    diff_import_manifests,
    extract_manifest_from_pe,
    normalize_manifest,
    reconcile_import_manifests,
    symbol_identity,
)


def entry(dll, symbols, binding=BINDING_NORMAL):
    return {"dll": dll, "binding": binding, "symbols": symbols}


def named(symbol):
    return {"name": symbol}


def ordinal(number):
    return {"ordinal": number}


def change_for(changes, dll):
    key = dll.lower()
    for change in changes:
        if change.dll.lower() == key:
            return change
    raise KeyError(dll)


def identities(symbols):
    return [symbol_identity(symbol) for symbol in symbols]


def test_normalize_manifest_converts_names_and_keeps_order_and_duplicates():
    manifest = [
        entry(
            "KERNEL32.DLL",
            [named("CreateFileW"), named("CreateFileW"), ordinal(3)],
        )
    ]
    normalized = normalize_manifest(manifest)
    assert normalized[0]["dll"] == b"KERNEL32.DLL"
    assert normalized[0]["binding"] == BINDING_NORMAL
    assert identities(normalized[0]["symbols"]) == [
        ("name", b"CreateFileW"),
        ("name", b"CreateFileW"),
        ("ordinal", 3),
    ]
    # The original manifest must not be mutated.
    assert isinstance(manifest[0]["dll"], str)


def test_normalize_manifest_accepts_delayed_flag_and_rejects_bad_data():
    assert normalize_manifest([{"dll": b"a.dll", "delayed": True, "symbols": []}])[0][
        "binding"
    ] == BINDING_DELAYED
    with pytest.raises(ValueError, match="Unknown import binding"):
        normalize_manifest([entry(b"a.dll", [], binding="static")])
    with pytest.raises(ValueError, match="dll name"):
        normalize_manifest([{"binding": BINDING_NORMAL, "symbols": []}])
    with pytest.raises(ValueError, match="name or an ordinal"):
        normalize_manifest([entry(b"a.dll", [{"hint": 1}])])


def test_dll_matching_is_case_insensitive():
    old = [entry(b"KERNEL32.DLL", [named(b"CreateFileW")])]
    new = [entry(b"kernel32.dll", [named(b"CreateFileW")])]
    changes = diff_import_manifests(old, new)
    assert len(changes) == 1
    assert changes[0].binding_change == "unchanged"
    assert changes[0].added_symbols == []
    assert changes[0].removed_symbols == []


def test_dll_name_aliases_match_renamed_libraries():
    old = [entry(b"wsock32.dll", [named(b"recv")])]
    new = [entry(b"ws2_32.dll", [named(b"recv")])]
    changes = diff_import_manifests(
        old, new, dll_name_aliases={b"wsock32.dll": b"ws2_32.dll"}
    )
    assert len(changes) == 1
    assert changes[0].dll == b"ws2_32.dll"
    assert changes[0].retained_symbols[0]["name"] == b"recv"


def test_added_dependency_is_classified_as_added():
    old = [entry(b"a.dll", [named(b"f")])]
    new = [
        entry(b"a.dll", [named(b"f")]),
        entry(b"b.dll", [named(b"g")], binding=BINDING_DELAYED),
    ]
    result = reconcile_import_manifests(old, new)
    assert result.added_libraries == [b"b.dll"]
    assert result.removed_libraries == []
    change = change_for(result.libraries, b"b.dll")
    assert change.action == "added"
    assert change.binding_change == "added"
    assert change.new_binding == BINDING_DELAYED


def test_missing_dll_is_not_uninstall_without_complete_coverage():
    old = [
        entry(b"a.dll", [named(b"f")]),
        entry(b"missing.dll", [named(b"g"), ordinal(2)]),
    ]
    new = [entry(b"a.dll", [named(b"f")])]
    result = reconcile_import_manifests(old, new)
    assert result.removed_libraries == []
    missing = change_for(result.libraries, b"missing.dll")
    assert missing.action == "retained_incomplete_coverage"
    assert missing.fully_covered is False
    assert identities(missing.uncovered_missing_symbols) == [
        ("name", b"g"),
        ("ordinal", 2),
    ]
    merged_dlls = [entry_["dll"] for entry_ in result.merged_manifest]
    assert b"missing.dll" in merged_dlls
    missing_merged = next(e for e in result.merged_manifest if e["dll"] == b"missing.dll")
    assert identities(missing_merged["symbols"]) == [
        ("name", b"g"),
        ("ordinal", 2),
    ]


def test_complete_coverage_allows_deleting_dependency_edges():
    old = [
        entry(b"a.dll", [named(b"f")]),
        entry(b"gone.dll", [named(b"g")]),
    ]
    new = [entry(b"a.dll", [named(b"f")])]
    result = reconcile_import_manifests(old, new, coverage=COVERAGE_COMPLETE)
    assert result.removed_libraries == [b"gone.dll"]
    gone = change_for(result.libraries, b"gone.dll")
    assert gone.action == "edge_removed"
    assert gone.fully_covered is True
    assert [e["dll"] for e in result.merged_manifest] == [b"a.dll"]


def test_coverage_can_be_scoped_per_dll_by_iterable_and_predicate():
    old = [
        entry(b"covered.dll", [named(b"f"), named(b"dropped")]),
        entry(b"partial.dll", [named(b"h"), named(b"kept")]),
    ]
    new = [
        entry(b"covered.dll", [named(b"f")]),
        entry(b"partial.dll", [named(b"h")]),
    ]
    result = reconcile_import_manifests(old, new, coverage=[b"COVERED.dll"])
    covered = change_for(result.libraries, b"covered.dll")
    partial = change_for(result.libraries, b"partial.dll")
    assert covered.fully_covered is True
    assert partial.fully_covered is False
    covered_merged = next(e for e in result.merged_manifest if e["dll"] == b"covered.dll")
    partial_merged = next(e for e in result.merged_manifest if e["dll"] == b"partial.dll")
    assert identities(covered_merged["symbols"]) == [("name", b"f")]
    assert identities(partial_merged["symbols"]) == [
        ("name", b"h"),
        ("name", b"kept"),
    ]

    predicate_result = reconcile_import_manifests(
        old, new, coverage=lambda dll: dll == b"partial.dll"
    )
    assert change_for(predicate_result.libraries, b"partial.dll").fully_covered is True
    assert change_for(predicate_result.libraries, b"covered.dll").fully_covered is False


def test_normal_to_delayed_transition_is_distinct_from_addition():
    old = [entry(b"a.dll", [named(b"f"), named(b"g")])]
    new = [entry(b"a.dll", [named(b"f"), named(b"g")], binding=BINDING_DELAYED)]
    result = reconcile_import_manifests(old, new)
    change = result.libraries[0]
    assert change.binding_change == "normal_to_delayed"
    assert change.old_binding == BINDING_NORMAL
    assert change.new_binding == BINDING_DELAYED
    assert change.action == "updated"
    assert change.added_symbols == []
    assert change.removed_symbols == []
    assert result.merged_manifest[0]["binding"] == BINDING_DELAYED


def test_delayed_to_normal_transition():
    old = [entry(b"a.dll", [named(b"f")], binding=BINDING_DELAYED)]
    new = [entry(b"a.dll", [named(b"f")])]
    change = diff_import_manifests(old, new)[0]
    assert change.binding_change == "delayed_to_normal"
    assert change.retained_symbols[0]["name"] == b"f"


def test_reordering_is_detected_without_adding_or_removing_entries():
    old = [entry(b"a.dll", [named(b"a"), named(b"b"), named(b"c")])]
    new = [entry(b"a.dll", [named(b"c"), named(b"a"), named(b"b")])]
    change = diff_import_manifests(old, new)[0]
    assert change.reordered is True
    assert change.added_symbols == []
    assert change.removed_symbols == []
    assert len(change.retained_symbols) == 3
    result = reconcile_import_manifests(old, new)
    assert change_for(result.libraries, b"a.dll").action == "updated"
    # The new ordering is authoritative under complete coverage.
    covered = reconcile_import_manifests(old, new, coverage=COVERAGE_COMPLETE)
    assert identities(covered.merged_manifest[0]["symbols"]) == [
        ("name", b"c"),
        ("name", b"a"),
        ("name", b"b"),
    ]


def test_identical_order_is_not_flagged_as_reordered():
    manifest = [entry(b"a.dll", [named(b"a"), ordinal(1)])]
    change = diff_import_manifests(manifest, manifest)[0]
    assert change.reordered is False
    assert change.binding_change == "unchanged"


def test_duplicate_entries_are_retained_with_their_multiplicity():
    old = [entry(b"a.dll", [named(b"f"), named(b"f"), named(b"g")])]
    new = [entry(b"a.dll", [named(b"g"), named(b"f"), named(b"f")])]
    change = diff_import_manifests(old, new)[0]
    assert len(change.retained_symbols) == 3
    assert change.added_symbols == []
    assert change.removed_symbols == []
    assert change.reordered is True

    reduced = [entry(b"a.dll", [named(b"g"), named(b"f")])]
    incomplete = reconcile_import_manifests(old, reduced)
    merged = incomplete.merged_manifest[0]
    assert identities(merged["symbols"]).count(("name", b"f")) == 2
    change = change_for(incomplete.libraries, b"a.dll")
    assert len(change.uncovered_missing_symbols) == 1

    covered = reconcile_import_manifests(old, reduced, coverage=COVERAGE_COMPLETE)
    assert identities(covered.merged_manifest[0]["symbols"]) == [
        ("name", b"g"),
        ("name", b"f"),
    ]


def test_name_and_ordinal_are_distinct_symbol_identities():
    old = [entry(b"a.dll", [ordinal(5)])]
    new = [entry(b"a.dll", [named(b"5")])]
    change = diff_import_manifests(old, new)[0]
    assert len(change.added_symbols) == 1
    assert len(change.removed_symbols) == 1
    assert change.retained_symbols == []


def test_symbol_level_additions_and_removals():
    old = [entry(b"a.dll", [named(b"keep"), named(b"drop"), ordinal(7)])]
    new = [entry(b"a.dll", [named(b"keep"), named(b"added"), ordinal(7)])]
    change = diff_import_manifests(old, new)[0]
    assert [s["name"] for s in change.added_symbols] == [b"added"]
    assert [s["name"] for s in change.removed_symbols] == [b"drop"]
    assert {symbol_identity(s) for s in change.retained_symbols} == {
        ("name", b"keep"),
        ("ordinal", 7),
    }


def test_nightly_rescan_keeps_every_edge_when_batch_is_incomplete():
    old = [
        entry(b"a.dll", [named(b"f")]),
        entry(b"b.dll", [named(b"g")], binding=BINDING_DELAYED),
        entry(b"c.dll", [named(b"h")]),
    ]
    # The collector could not reach b.dll at all and only saw part of c.dll.
    new = [
        entry(b"a.dll", [named(b"f"), named(b"new")]),
        entry(b"c.dll", []),
        entry(b"d.dll", [named(b"i")]),
    ]
    result = reconcile_import_manifests(old, new)
    merged = {e["dll"]: e for e in result.merged_manifest}
    assert set(merged) == {b"a.dll", b"b.dll", b"c.dll", b"d.dll"}
    assert identities(merged[b"a.dll"]["symbols"]) == [
        ("name", b"f"),
        ("name", b"new"),
    ]
    assert merged[b"b.dll"]["binding"] == BINDING_DELAYED
    assert identities(merged[b"b.dll"]["symbols"]) == [("name", b"g")]
    assert identities(merged[b"c.dll"]["symbols"]) == [("name", b"h")]
    assert result.added_libraries == [b"d.dll"]
    assert result.removed_libraries == []


def test_extract_manifest_from_pe_adapters_both_import_directories():
    normal_module = types.SimpleNamespace(
        dll=b"kernel32.dll",
        imports=[
            types.SimpleNamespace(name=b"CreateFileW", ordinal=None, hint=0),
            types.SimpleNamespace(name=None, ordinal=12, hint=None),
        ],
    )
    delayed_module = types.SimpleNamespace(
        dll=b"user32.dll",
        imports=[types.SimpleNamespace(name=b"MessageBoxW", ordinal=None, hint=5)],
    )
    fake_pe = types.SimpleNamespace(
        DIRECTORY_ENTRY_IMPORT=[normal_module],
        DIRECTORY_ENTRY_DELAY_IMPORT=[delayed_module],
    )
    manifest = extract_manifest_from_pe(fake_pe)
    assert manifest == [
        entry(
            b"kernel32.dll",
            [{"name": b"CreateFileW", "hint": 0}, {"ordinal": 12}],
        ),
        entry(
            b"user32.dll",
            [{"name": b"MessageBoxW", "hint": 5}],
            binding=BINDING_DELAYED,
        ),
    ]


def test_extract_manifest_from_pe_handles_missing_directories():
    fake_pe = types.SimpleNamespace()
    assert extract_manifest_from_pe(fake_pe) == []
