import json
import os
import unittest

import apiset_resolver
from apiset_resolver import (
    ApiSetResolver,
    ForwardingCycleError,
    InMemoryRepository,
    LibraryExports,
    LibraryMissingError,
    MappingMissingError,
    PefileRepository,
    SymbolMissingError,
    load_version_mapping,
    parse_requirement,
)

# 32-bit PE exporting the symbols TST! .. TST~ (ordinals 1..29), no
# forwarders. Same blob as used in export_test.py.
PE_32_HEX = (
    b"4D5A90000300000004000000FFFF0000B8000000000000004000000000000000000000000000000000000000000000000000000000000000"
    b"00000000B00000000E1FBA0E00B409CD21B8014CCD21546869732070726F6772616D2063616E6E6F742062652072756E20696E20444F5320"
    b"6D6F64652E0D0D0A2400000000000000AD723BDFE913558CE913558CE913558C1B64558DE813558C1B64578DE813558C52696368E913558C"
    b"0000000000000000504500004C0101009DE473630000000000000000E00002210B010E1E00000000800200000000000000000000D0010000"
    b"D00100000000001010000000100000000600000000000000060000000000000050040000D001000000000000020040050000100000100000"
    b"00001000001000000000000010000000F0010000E80100000000000000000000000000000000000000000000000000000000000000000000"
    b"0000000000000000D00100001C00000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    b"00000000000000000000000000000000000000000000000000000000000000002E7264617461000074020000D001000080020000D0010000"
    b"00000000000000000000000040000040000000009DE47363000000000D00000054000000F0030000F00300000000000000000000FFFFFFFF"
    b"000000003A030000010000001D0000001D000000180200008C02000000030000000000000000000000000000000000000000000000000000"
    b"0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    b"000000000000000000000000000000000000000000000000000000000000000000000000450300004A0300004F0300005403000059030000"
    b"5E03000063030000680300006D03000072030000770300007C03000081030000860300008B03000090030000950300009A0300009F030000"
    b"A4030000A9030000AE030000B3030000B8030000BD030000C2030000C7030000CC030000D103000000000100020003000400050006000700"
    b"080009000A000B000C000D000E000F0010001100120013001400150016001700180019001A001B001C007465737433322E646C6C00545354"
    b"2100545354220054535423005453542400545354250054535426005453542700545354280054535429005453542A005453542B005453542C"
    b"005453542D005453542E005453542F005453543A005453543C005453543E005453543F005453545B005453545C005453545D005453545E00"
    b"5453545F0054535460005453547B005453547C005453547D005453547E000000180000000080008000000000000000000000000000000000"
    b"00000000D0010000200000002E72646174610000F0010000E80100002E65646174610000D8030000180000002E726461746124766F6C746D"
    b"64000000F0030000540000002E7264617461247A7A7A646267000000000000000000000000000000"
)

V1 = "10.0.19041"
V2 = "10.0.22621"

REPO = {
    "kernelbase.dll": {
        "HeapAlloc": "ntdll.RtlAllocateHeap",
        "LocalFree": None,
    },
    "ntdll.dll": {
        "RtlAllocateHeap": None,
    },
    "v1only.dll": {"Func": None},
    "v2only.dll": {"Func": None},
}

MAPPING = {
    V1: {
        "api-ms-win-core-heap-l1-1-0": "kernelbase.dll",
        "api-ms-win-core-missing-l1-1-0": "nothere.dll",
        "api-ms-win-core-versioned-l1-1-0": "v1only.dll",
    },
    V2: {
        "api-ms-win-core-heap-l1-1-0": "kernelbase.dll",
        "api-ms-win-core-versioned-l1-1-0": "v2only.dll",
    },
}


def make_resolver(repo=None, mapping=None):
    return ApiSetResolver(
        InMemoryRepository(REPO if repo is None else repo),
        MAPPING if mapping is None else mapping,
    )


class TestHelpers(unittest.TestCase):
    def test_parse_requirement(self):
        self.assertEqual(
            parse_requirement("kernel32.dll!CreateFileW"),
            ("kernel32.dll", "CreateFileW"),
        )
        self.assertRaises(ValueError, parse_requirement, "kernel32.dll")
        self.assertRaises(ValueError, parse_requirement, "!Func")

    def test_normalize_module(self):
        self.assertEqual(apiset_resolver.normalize_module("Kernel32.DLL"), "kernel32")
        self.assertTrue(apiset_resolver.is_api_set("API-MS-WIN-CORE-HEAP-L1-1-0.dll"))
        self.assertFalse(apiset_resolver.is_api_set("kernel32.dll"))


class TestResolution(unittest.TestCase):
    def test_direct_export(self):
        res = make_resolver().resolve("ntdll.dll", "RtlAllocateHeap", V1)
        self.assertEqual(res.provider_module, "ntdll")
        self.assertEqual(res.provider_symbol, "RtlAllocateHeap")
        self.assertEqual([hop.kind for hop in res.hops], ["provide"])

    def test_api_set_forward_chain(self):
        res = make_resolver().resolve(
            "api-ms-win-core-heap-l1-1-0.dll", "HeapAlloc", V1
        )
        self.assertEqual(res.provider_module, "ntdll")
        self.assertEqual(res.provider_symbol, "RtlAllocateHeap")
        self.assertEqual(
            [hop.kind for hop in res.hops], ["map", "forward", "provide"]
        )
        self.assertEqual(res.hops[0].target, "kernelbase")
        self.assertEqual(res.hops[1].target, "ntdll.RtlAllocateHeap")
        self.assertIn("ntdll!RtlAllocateHeap", res.chain_text())

    def test_forward_to_api_set(self):
        repo = dict(REPO)
        repo["shim.dll"] = {"Func": "api-ms-win-core-heap-l1-1-0.HeapAlloc"}
        res = make_resolver(repo=repo).resolve("shim.dll", "Func", V1)
        self.assertEqual(res.provider_module, "ntdll")
        self.assertEqual(
            [hop.kind for hop in res.hops], ["forward", "map", "forward", "provide"]
        )

    def test_mapping_missing(self):
        resolver = make_resolver()
        with self.assertRaises(MappingMissingError) as ctx:
            resolver.resolve("api-ms-win-core-unknown-l1-1-0", "Func", V1)
        self.assertEqual(ctx.exception.module, "api-ms-win-core-unknown-l1-1-0")
        # A version with no explicit mapping at all is also a mapping miss.
        with self.assertRaises(MappingMissingError):
            resolver.resolve("ntdll.dll", "RtlAllocateHeap", "6.1.7601")

    def test_library_missing(self):
        with self.assertRaises(LibraryMissingError) as ctx:
            make_resolver().resolve("api-ms-win-core-missing-l1-1-0", "Func", V1)
        self.assertEqual(ctx.exception.module, "nothere")

    def test_symbol_missing(self):
        with self.assertRaises(SymbolMissingError) as ctx:
            make_resolver().resolve("ntdll.dll", "NoSuchSymbol", V1)
        self.assertEqual(ctx.exception.module, "ntdll")
        self.assertEqual(ctx.exception.symbol, "NoSuchSymbol")

    def test_forward_cycle_terminates(self):
        repo = {
            "a.dll": {"f": "b.g"},
            "b.dll": {"g": "a.f"},
        }
        with self.assertRaises(ForwardingCycleError):
            make_resolver(repo=repo).resolve("a.dll", "f", V1)

    def test_self_forward_cycle_terminates(self):
        repo = {"a.dll": {"f": "a.f"}}
        with self.assertRaises(ForwardingCycleError):
            make_resolver(repo=repo).resolve("a.dll", "f", V1)

    def test_api_set_self_map_cycle_terminates(self):
        mapping = {V1: {"api-ms-win-loop-l1-1-0": "api-ms-win-loop-l1-1-0.dll"}}
        with self.assertRaises(ForwardingCycleError):
            make_resolver(mapping=mapping).resolve(
                "api-ms-win-loop-l1-1-0", "Func", V1
            )

    def test_ordinal_forward(self):
        repo = {
            "a.dll": LibraryExports(by_name={"Func": "b.#7"}),
            "b.dll": LibraryExports(by_ordinal={7: None}),
        }
        res = make_resolver(repo=repo).resolve("a.dll", "Func", V1)
        self.assertEqual(res.provider_module, "b")
        self.assertEqual(res.provider_symbol, "#7")


class TestVersionIsolation(unittest.TestCase):
    def test_same_set_different_versions(self):
        resolver = make_resolver()
        res_v1 = resolver.resolve("api-ms-win-core-versioned-l1-1-0", "Func", V1)
        res_v2 = resolver.resolve("api-ms-win-core-versioned-l1-1-0", "Func", V2)
        self.assertEqual(res_v1.provider_module, "v1only")
        self.assertEqual(res_v2.provider_module, "v2only")

    def test_cache_not_reused_across_versions(self):
        # Resolve V2 first: the V1 result must not reuse the V2 mapping.
        resolver = make_resolver()
        res_v2 = resolver.resolve("api-ms-win-core-versioned-l1-1-0", "Func", V2)
        res_v1 = resolver.resolve("api-ms-win-core-versioned-l1-1-0", "Func", V1)
        self.assertEqual(res_v2.provider_module, "v2only")
        self.assertEqual(res_v1.provider_module, "v1only")
        # Cached results are returned for repeated queries of the same version.
        self.assertIs(
            res_v1, resolver.resolve("api-ms-win-core-versioned-l1-1-0", "Func", V1)
        )

    def test_mapping_only_in_other_version(self):
        # api-ms-win-core-missing-l1-1-0 exists in V1's mapping but not V2's.
        with self.assertRaises(MappingMissingError):
            make_resolver().resolve("api-ms-win-core-missing-l1-1-0", "Func", V2)


class TestMappingFile(unittest.TestCase):
    def test_load_version_mapping_json(self):
        import tempfile

        raw = {V1: {"API-MS-WIN-CORE-HEAP-L1-1-0.dll": "KernelBase.dll"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mapping.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(raw, handle)
            mapping = load_version_mapping(path)
        self.assertEqual(
            mapping, {V1: {"api-ms-win-core-heap-l1-1-0": "kernelbase"}}
        )


class TestPefileRepository(unittest.TestCase):
    def setUp(self):
        import tempfile
        from binascii import unhexlify

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dll_path = os.path.join(self._tmp.name, "test32.dll")
        data = bytearray(unhexlify(PE_32_HEX))
        for idx in range(29):
            data[0x218 + 4 * idx] = 0x01
        self.dll_bytes = bytes(data)
        with open(self.dll_path, "wb") as handle:
            handle.write(self.dll_bytes)

    def test_resolve_from_real_pe(self):
        repo = PefileRepository(self._tmp.name)
        resolver = ApiSetResolver(repo, {V1: {}})
        res = resolver.resolve("test32.dll", "TST!", V1)
        self.assertEqual(res.provider_module, "test32")
        self.assertEqual(res.provider_symbol, "TST!")
        with self.assertRaises(SymbolMissingError):
            resolver.resolve("test32.dll", "NOPE", V1)
        with self.assertRaises(LibraryMissingError):
            resolver.resolve("absent.dll", "TST!", V1)

    def test_files_are_read_only(self):
        before_stat = os.stat(self.dll_path)
        repo = PefileRepository(self._tmp.name)
        ApiSetResolver(repo, {V1: {}}).resolve("test32.dll", "TST!", V1)
        with open(self.dll_path, "rb") as handle:
            self.assertEqual(handle.read(), self.dll_bytes)
        after_stat = os.stat(self.dll_path)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(before_stat.st_size, after_stat.st_size)


if __name__ == "__main__":
    unittest.main()
