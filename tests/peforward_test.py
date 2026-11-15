import json
import struct
import subprocess
import sys

import pytest

import pefile
import peforward

IMAGE_BASE = 0x10000000
RDATA_RVA = 0x1000
RDATA_RAW = 0x200
TEXT_RVA = 0x2000
TEXT_RAW = 0x400


def _align(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def build_pe(exports, dll_name="test.dll", image_base=IMAGE_BASE):
    """Build a minimal PE32 DLL with a controlled export directory.

    ``exports`` is a list of tuples ``(name, target)``.  ``target`` is either
    ``("local", offset)`` or ``("forward", "module.symbol")``.  Name may be
    None to create an ordinal-only export.
    """

    entry_size = 40
    count = len(exports)
    base = 1
    function_rva = RDATA_RVA + entry_size
    names_rva = function_rva + 4 * count
    ordinals_rva = names_rva + 4 * count

    cursor = ordinals_rva + 2 * count
    strings_rva = cursor
    blob = bytearray()

    def add_string(text):
        nonlocal cursor
        data = text.encode("ascii") + b"\0"
        rva = cursor
        blob.extend(data)
        cursor += len(data)
        return rva

    dll_name_rva = add_string(dll_name)
    export_names = []
    for name, _ in exports:
        export_names.append(add_string(name) if name else 0)
    forwarders = []
    for _, target in exports:
        if target[0] == "forward":
            forwarders.append(add_string(target[1]))
        else:
            forwarders.append(None)

    rdata = bytearray(_align(cursor - RDATA_RVA, 0x200))
    rdata[strings_rva - RDATA_RVA:strings_rva - RDATA_RVA + len(blob)] = blob

    # IMAGE_EXPORT_DIRECTORY
    struct.pack_into(
        "<IIHHIIIIIII",
        rdata,
        0,
        0,  # Characteristics
        0,  # TimeDateStamp
        0,  # MajorVersion
        0,  # MinorVersion
        dll_name_rva,
        base,
        count,
        sum(1 for name, _ in exports if name),
        function_rva,
        names_rva,
        ordinals_rva,
    )

    name_index = 0
    for index, (name, target) in enumerate(exports):
        if target[0] == "forward":
            address = forwarders[index]
        else:
            address = TEXT_RVA + target[1]
        struct.pack_into("<I", rdata, function_rva - RDATA_RVA + 4 * index, address)
        if name:
            struct.pack_into(
                "<I", rdata, names_rva - RDATA_RVA + 4 * name_index, export_names[index]
            )
            struct.pack_into(
                "<H", rdata, ordinals_rva - RDATA_RVA + 2 * name_index, index
            )
            name_index += 1

    text = bytearray(0x200)
    export_size = len(rdata)

    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)

    file_header = bytearray(struct.pack(
        "<4sHHIIIHH",
        b"PE\0\0",
        0x14C,  # i386
        2,  # NumberOfSections
        0,  # TimeDateStamp
        0,  # PointerToSymbolTable
        0,  # NumberOfSymbols
        0xE0,  # SizeOfOptionalHeader
        0x2102,  # Characteristics: DLL, 32-bit, executable
    ))

    optional = bytearray(0xE0)
    struct.pack_into("<HBB", optional, 0, 0x10B, 0, 0)
    struct.pack_into("<IIII", optional, 4, 0x200, 0x200, 0, 0)
    struct.pack_into("<III", optional, 20, TEXT_RVA, RDATA_RVA, image_base)
    struct.pack_into("<II", optional, 32, 0x1000, 0x200)
    struct.pack_into("<HHHHHH", optional, 40, 0, 0, 0, 0, 4, 0)
    struct.pack_into("<I", optional, 52, 0)
    struct.pack_into("<III", optional, 56, 0x3000, 0x200, 0)
    struct.pack_into("<HH", optional, 68, 2, 0)
    struct.pack_into("<IIII", optional, 72, 0x100000, 0x1000, 0x100000, 0x1000)
    struct.pack_into("<II", optional, 88, 0, 16)
    struct.pack_into("<II", optional, 96, RDATA_RVA, export_size)

    sections = bytearray()
    sections.extend(
        struct.pack(
            "<8sIIIIIIHHI",
            b".rdata".ljust(8, b"\0"),
            len(rdata),
            RDATA_RVA,
            len(rdata),
            RDATA_RAW,
            0,
            0,
            0,
            0,
            0x40000040,
        )
    )
    sections.extend(
        struct.pack(
            "<8sIIIIIIHHI",
            b".text".ljust(8, b"\0"),
            len(text),
            TEXT_RVA,
            len(text),
            TEXT_RAW,
            0,
            0,
            0,
            0,
            0x60000020,
        )
    )

    headers = bytes(dos + file_header + optional + sections)
    data = bytearray(max(_align(len(headers), 0x200), TEXT_RAW + len(text)))
    data[0:len(headers)] = headers
    data[RDATA_RAW:RDATA_RAW + len(rdata)] = rdata
    data[TEXT_RAW:TEXT_RAW + len(text)] = text
    return bytes(data)


def make_library(name, exports, version=None, path=None):
    data = build_pe(exports, dll_name=name)
    return peforward.Library(
        pefile.PE(data=data),
        name,
        display_name=name,
        path=path,
        version=version,
    )


def by_name(results):
    collected = {}
    for result in results:
        name = peforward._text(result.name)
        collected[(result.source.display_name, name or "#%d" % result.ordinal)] = result
    return collected


def test_simple_chain_resolves_to_local_code():
    c = make_library("c.dll", [("Target", ("local", 0x10))])
    b = make_library(
        "b.dll",
        [("Middle", ("forward", "c.Target")), ("Other", ("local", 0x20))],
    )
    a = make_library("a.dll", [("Entry", ("forward", "b.Middle"))])

    resolver = peforward.ExportResolver([a, b, c])
    result = resolver.resolve(a, name=b"Entry")

    assert result.status == peforward.LOCAL
    assert [hop.library for hop in result.chain] == ["a.dll", "b.dll", "c.dll"]
    assert [hop.symbol for hop in result.chain] == ["Entry", "Middle", "Target"]
    assert result.target_library == "c.dll"
    assert result.target_name == b"Target"
    assert result.target_rva == TEXT_RVA + 0x10
    assert result.target_address == IMAGE_BASE + TEXT_RVA + 0x10


def test_missing_library_does_not_break_other_exports():
    b = make_library("b.dll", [("Good", ("local", 0x10))])
    a = make_library(
        "a.dll",
        [("Missing", ("forward", "nosuch.Func")), ("Good", ("forward", "b.Good"))],
    )

    resolver = peforward.ExportResolver([a, b])
    results = by_name(resolver.resolve_all())

    missing = results[("a.dll", "Missing")]
    assert missing.status == peforward.MISSING_LIBRARY
    assert missing.missing_library == "nosuch"
    assert missing.missing_symbol == "Func"

    good = results[("a.dll", "Good")]
    assert good.status == peforward.LOCAL
    assert good.target_library == "b.dll"


def test_missing_export_in_existing_library():
    b = make_library("b.dll", [("Present", ("local", 0x10))])
    a = make_library("a.dll", [("Gone", ("forward", "b.Absent"))])

    resolver = peforward.ExportResolver([a, b])
    result = resolver.resolve(a, name="Gone")

    assert result.status == peforward.MISSING_EXPORT
    assert result.missing_library == "b.dll"
    assert result.missing_symbol == "Absent"


def test_ordinal_forwarding():
    c = make_library("c.dll", [(None, ("local", 0x30)), ("Named", ("local", 0x40))])
    # Second export in c.dll has ordinal 2 because Base is 1.
    b = make_library("b.dll", [("ByOrdinal", ("forward", "c.#2"))])

    resolver = peforward.ExportResolver([b, c])
    result = resolver.resolve(b, name="ByOrdinal", ordinal=1)

    assert result.status == peforward.LOCAL
    assert result.target_name == b"Named"
    assert result.target_ordinal == 2
    assert result.target_rva == TEXT_RVA + 0x40


def test_cycle_is_detected_and_terminates():
    a = make_library("a.dll", [("Loop", ("forward", "b.Loop"))])
    b = make_library("b.dll", [("Loop", ("forward", "a.Loop"))])

    resolver = peforward.ExportResolver([a, b])
    result = resolver.resolve(a, name="Loop")

    assert result.status == peforward.CYCLE
    labels = [hop.label() for hop in result.cycle]
    # The cycle starts at a.dll!Loop and repeats it after closing the loop.
    assert labels[0] == "a.dll!Loop"
    assert labels[-1] == "a.dll!Loop"
    assert len(labels) == 3
    assert result.cycle_start == 0


def test_three_dll_cycle_listed_once_per_entry():
    a = make_library("a.dll", [("X", ("forward", "b.X"))])
    b = make_library("b.dll", [("X", ("forward", "c.X"))])
    c = make_library("c.dll", [("X", ("forward", "a.X"))])

    resolver = peforward.ExportResolver([a, b, c])
    report = resolver.report()

    cycles = report["cycles"]
    assert len(cycles) == 1
    assert cycles[0]["entries"] == 3
    assert cycles[0]["cycle"][0] == "a.dll!X"
    assert cycles[0]["cycle"][-1] == "a.dll!X"
    assert report["summary"]["cycle"] == 3


def test_max_depth_limits_unbounded_growth():
    libraries = []
    for index in range(40):
        exports = [("Next", ("forward", "d%d.Next" % (index + 1)))]
        libraries.append(make_library("d%d.dll" % index, exports))
    libraries.append(make_library("d40.dll", [("Next", ("local", 0x10))]))

    resolver = peforward.ExportResolver(libraries, max_depth=5)
    result = resolver.resolve(libraries[0], name="Next")

    assert result.status == peforward.DEPTH_EXCEEDED
    assert len(result.chain) == 5


def test_same_named_libraries_require_caller_selection():
    old = make_library(
        "version.dll", [("GetVersion", ("forward", "core.Old"))], version="6.1"
    )
    new = make_library("core.dll", [("Old", ("local", 0x10)), ("New", ("local", 0x20))])
    new_version = make_library(
        "version.dll", [("GetVersion", ("forward", "core.New"))], version="10.0"
    )
    host = make_library("host.dll", [("Check", ("forward", "version.GetVersion"))])

    resolver = peforward.ExportResolver([host, old, new_version, new])
    unresolved = resolver.resolve(host, name="Check")
    assert unresolved.status == peforward.AMBIGUOUS_LIBRARY
    assert len(unresolved.candidates) == 2

    resolver.select_version("version", "10.0")
    resolved = resolver.resolve(host, name="Check")
    assert resolved.status == peforward.LOCAL
    assert resolved.target_rva == TEXT_RVA + 0x20

    resolver.select_version("version", "6.1")
    resolved = resolver.resolve(host, name="Check")
    assert resolved.status == peforward.LOCAL
    assert resolved.target_rva == TEXT_RVA + 0x10


def test_version_selector_callback_receives_candidates():
    low = make_library("same.dll", [("F", ("local", 0x10))], version="low")
    high = make_library("same.dll", [("F", ("local", 0x20))], version="high")
    host = make_library("host.dll", [("G", ("forward", "same.F"))])

    seen = []

    def selector(name, candidates):
        seen.append((name, [candidate.version for candidate in candidates]))
        return candidates[1]

    resolver = peforward.ExportResolver([host, low, high], version_selector=selector)
    result = resolver.resolve(host, name="G")

    assert seen == [("same", ["low", "high"])]
    assert result.status == peforward.LOCAL
    assert result.target_rva == TEXT_RVA + 0x20


def test_forwarder_range_is_never_local_code():
    # Point the "export" at bytes inside the export directory that look like
    # code but are not a valid forwarder string.
    data = build_pe([("Bogus", ("local", 0x0))], dll_name="bogus.dll")
    pe = pefile.PE(data=data)
    rdata_offset = RDATA_RAW
    # The first byte of the export directory, 0x00 0x00 0x00 0x00, is inside
    # the declared range and cannot be resolved as code.
    pe.set_bytes_at_offset(rdata_offset, b"\x00\x00\x00\x00")
    pe.DIRECTORY_ENTRY_EXPORT.symbols[0].address = RDATA_RVA
    resolver = peforward.ExportResolver()
    library = resolver.add_pe(
        pefile.PE(data=pe.write()), name="bogus.dll", version=None
    )

    result = resolver.resolve(library, name="Bogus")
    assert result.status == peforward.INVALID_FORWARDER
    assert result.status != peforward.LOCAL
    assert result.chain[-1].rva == RDATA_RVA


def test_forwarder_bytes_shaped_like_code_are_not_local():
    # A forwarder-range RVA holding ASCII that is also a plausible byte
    # pattern must still be treated as a forwarder, not a local function.
    host = make_library("host.dll", [("Entry", ("forward", "target.Func"))])
    target = make_library("target.dll", [("Func", ("local", 0x50))])

    resolver = peforward.ExportResolver([host, target])
    export = host.exports_by_name[b"Entry"][0]
    assert host.is_forwarder_rva(export.address)
    result = resolver.resolve(host, name="Entry")
    assert result.status == peforward.LOCAL
    assert result.target_library == "target.dll"


def test_export_outside_range_pointing_at_forwarder_like_bytes_is_local():
    # The bytes at the address happen to read like "a.b", but the address is
    # in .text, outside the declared export range, so it is local code.
    data = bytearray(build_pe([("Real", ("local", 0x0))], dll_name="host.dll"))
    data[TEXT_RAW:TEXT_RAW + 3] = b"x.y"
    resolver = peforward.ExportResolver()
    library = resolver.add_pe(pefile.PE(data=bytes(data)), name="host.dll")

    result = resolver.resolve(library, name="Real")
    assert result.status == peforward.LOCAL
    assert result.target_rva == TEXT_RVA
    assert result.chain[-1].forwarder is None


def test_fast_load_pe_still_resolves_exports():
    host_data = build_pe([("Entry", ("forward", "b.Impl"))], dll_name="host.dll")
    target_data = build_pe([("Impl", ("local", 0x10))], dll_name="b.dll")

    host = pefile.PE(data=host_data, fast_load=True)
    resolver = peforward.ExportResolver()
    host_library = resolver.add_pe(host, name="host.dll")
    target_library = resolver.add_pe(pefile.PE(data=target_data), name="b.dll")

    result = resolver.resolve(host_library, name="Entry")
    assert result.status == peforward.LOCAL
    assert result.target_library == "b.dll"


def test_long_acyclic_chain_resolves_without_depth_cap():
    libraries = []
    for index in range(60):
        libraries.append(
            make_library(
                "d%d.dll" % index,
                [("Next", ("forward", "d%d.Next" % (index + 1)))],
            )
        )
    libraries.append(make_library("d60.dll", [("Next", ("local", 0x10))]))

    resolver = peforward.ExportResolver(libraries)
    result = resolver.resolve(libraries[0], name="Next")

    assert result.status == peforward.LOCAL
    assert len(result.chain) == 61


def test_ordinal_alias_exports_are_not_ambiguous():
    # Several names can share one ordinal and one RVA; following an ordinal
    # to that slot is not ambiguous.
    b = make_library(
        "b.dll",
        [("Alpha", ("local", 0x10)), ("Beta", ("local", 0x10))],
    )
    a = make_library("a.dll", [("Entry", ("forward", "b.#1"))])

    resolver = peforward.ExportResolver([a, b])
    result = resolver.resolve(a, name="Entry")

    assert result.status == peforward.LOCAL
    assert result.target_rva == TEXT_RVA + 0x10
    assert result.target_ordinal == 1


def test_parse_forwarder_module_with_dots_and_ordinal():
    target = peforward.parse_forwarder(
        b"api-ms-win-core-file-l1-1-0.CreateFileW"
    )
    assert target.module == "api-ms-win-core-file-l1-1-0"
    assert target.name == b"CreateFileW"
    assert target.ordinal is None

    target = peforward.parse_forwarder("ntdll.#77")
    assert target.module == "ntdll"
    assert target.ordinal == 77
    assert target.symbol == "#77"

    with pytest.raises(peforward.ForwarderParseError):
        peforward.parse_forwarder("noDotHere")
    with pytest.raises(peforward.ForwarderParseError):
        peforward.parse_forwarder("module.#notanumber")
    with pytest.raises(peforward.ForwarderParseError):
        peforward.parse_forwarder("")


def test_select_path_picks_same_named_library():
    one = make_library(
        "dup.dll", [("F", ("local", 0x10))], version="one", path="/tmp/one/dup.dll"
    )
    two = make_library(
        "dup.dll", [("F", ("local", 0x20))], version="two", path="/tmp/two/dup.dll"
    )
    host = make_library("host.dll", [("G", ("forward", "dup.F"))])

    resolver = peforward.ExportResolver([host, one, two])
    resolver.select_path("dup", "/tmp/two/dup.dll")
    result = resolver.resolve(host, name="G")

    assert result.status == peforward.LOCAL
    assert result.target_rva == TEXT_RVA + 0x20


def test_selection_that_matches_nothing_is_reported():
    one = make_library(
        "dup.dll", [("F", ("local", 0x10))], version="one", path="/tmp/one/dup.dll"
    )
    two = make_library(
        "dup.dll", [("F", ("local", 0x20))], version="two", path="/tmp/two/dup.dll"
    )
    host = make_library("host.dll", [("G", ("forward", "dup.F"))])

    resolver = peforward.ExportResolver([host, one, two])
    resolver.select_version("dup", "missing")
    result = resolver.resolve(host, name="G")

    assert result.status == peforward.AMBIGUOUS_LIBRARY
    assert "matched 0 libraries" in result.message


def test_display_name_uses_file_name_when_path_given(tmp_path):
    path = tmp_path / "weird_stem.dll"
    path.write_bytes(build_pe([("F", ("local", 0x10))], dll_name="inside.dll"))
    resolver = peforward.ExportResolver()
    library = resolver.add_library(path)
    assert library.display_name == "weird_stem.dll"
    assert library.name == "weird_stem"

    b = make_library("b.dll", [("Impl", ("local", 0x10))])
    a = make_library("a.dll", [(None, ("forward", "b.Impl"))])

    resolver = peforward.ExportResolver([a, b])
    result = resolver.resolve(a, ordinal=1)

    assert result.status == peforward.LOCAL
    assert result.name is None
    assert result.ordinal == 1
    assert result.chain[0].symbol == "#1"


def test_case_and_extension_insensitive_module_lookup():
    b = make_library("b.dll", [("Impl", ("local", 0x10))])
    a = make_library("a.dll", [("Entry", ("forward", "B.DLL.Impl"))])

    resolver = peforward.ExportResolver([a, b])
    result = resolver.resolve(a, name="Entry")
    assert result.status == peforward.LOCAL
    assert result.target_library == "b.dll"
    assert result.target_rva == TEXT_RVA + 0x10


def test_duplicate_library_names_are_reported():
    one = make_library(
        "dup.dll", [("F", ("local", 0x10))], version="1", path="/tmp/a/dup.dll"
    )
    two = make_library(
        "dup.dll", [("F", ("local", 0x20))], version="2", path="/tmp/b/dup.dll"
    )

    resolver = peforward.ExportResolver([one, two])
    report = resolver.report()

    assert list(report["duplicate_names"]) == ["dup"]
    assert len(report["duplicate_names"]["dup"]) == 2
    assert report["summary"]["ambiguous_library"] == 0


def test_report_is_json_serializable():
    b = make_library("b.dll", [("Impl", ("local", 0x10))])
    a = make_library("a.dll", [("Entry", ("forward", "b.Impl"))])
    resolver = peforward.ExportResolver([a, b])
    report = resolver.report()
    encoded = json.dumps(report, sort_keys=True)
    assert "local" in encoded


def test_format_report_mentions_cycles_and_summary():
    a = make_library("a.dll", [("Loop", ("forward", "b.Loop"))])
    b = make_library("b.dll", [("Loop", ("forward", "a.Loop"))])
    resolver = peforward.ExportResolver([a, b])
    rendered = peforward.format_report(resolver.report())
    assert "cycles:" in rendered
    assert "cycle=" in rendered
    assert "a.dll!Loop -> b.dll!Loop -> a.dll!Loop" in rendered


def test_cli_json_and_strict(tmp_path):
    (tmp_path / "b.dll").write_bytes(
        build_pe([("Impl", ("local", 0x10))], dll_name="b.dll")
    )
    (tmp_path / "a.dll").write_bytes(
        build_pe([("Entry", ("forward", "b.Impl"))], dll_name="a.dll")
    )

    output = subprocess.run(
        [sys.executable, "peforward.py", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        cwd=str(__file__).rsplit("/", 2)[0],
    )
    assert output.returncode == 0, output.stderr
    report = json.loads(output.stdout)
    assert report["summary"]["local"] == 2

    strict = subprocess.run(
        [sys.executable, "peforward.py", str(tmp_path), "--strict"],
        capture_output=True,
        text=True,
        cwd=str(__file__).rsplit("/", 2)[0],
    )
    assert strict.returncode == 0, strict.stderr


def test_cli_missing_target_is_nonzero_only_in_strict(tmp_path):
    (tmp_path / "a.dll").write_bytes(
        build_pe([("Entry", ("forward", "gone.X"))], dll_name="a.dll")
    )
    base = str(__file__).rsplit("/", 2)[0]

    normal = subprocess.run(
        [sys.executable, "peforward.py", str(tmp_path), "--json"],
        capture_output=True, text=True, cwd=base,
    )
    assert normal.returncode == 0
    report = json.loads(normal.stdout)
    assert report["summary"]["missing_library"] == 1

    strict = subprocess.run(
        [sys.executable, "peforward.py", str(tmp_path), "--strict"],
        capture_output=True, text=True, cwd=base,
    )
    assert strict.returncode == 1
