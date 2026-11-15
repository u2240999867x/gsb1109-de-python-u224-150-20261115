import struct

import pytest

import pefile
from export_test import PE_32, PE_64


class ImportImageBuilder:
    """Builds a PE carrying both a normal and a delay import directory."""

    def __init__(self, fixture, is_64):
        self.pe = pefile.PE(data=bytearray(fixture), fast_load=True)
        self.is_64 = is_64
        self.thunk_size = 8 if is_64 else 4
        self.thunk_format = "<Q" if is_64 else "<I"
        self.ordinal_flag = (
            0x8000000000000000 if is_64 else 0x80000000
        )
        section = self.pe.sections[0]
        self.base_offset = section.PointerToRawData + section.SizeOfRawData
        self.base_rva = section.VirtualAddress + (
            self.base_offset - section.PointerToRawData
        )
        self.blob = bytearray()

    def _put(self, data):
        rva = self.base_rva + len(self.blob)
        self.blob.extend(data)
        return rva

    def _thunk(self, value):
        return struct.pack(self.thunk_format, value)

    def _thunk_table(self, values):
        return b"".join(self._thunk(value) for value in values) + bytes(
            self.thunk_size
        )

    def build(self):
        # Strings and hint/name tables.
        rva_foobar = self._put(b"FooBar.DLL\0")
        rva_create = self._put(struct.pack("<H", 7) + b"CreateFileA\0")
        rva_write = self._put(struct.pack("<H", 0x42) + b"WriteFile\0")
        rva_ws2 = self._put(b"WS2_32.dll\0")
        rva_recv = self._put(struct.pack("<H", 3) + b"recv\0")

        # Normal import: FooBar.DLL with a named and an ordinal import.
        # The IAT slot of the named import is bound, so its content differs
        # from the ILT and pefile records the bound address.
        normal_ilt_rva = self._put(
            self._thunk_table([rva_create, self.ordinal_flag | 12])
        )
        normal_iat_rva = self._put(
            self._thunk_table([rva_ws2, self.ordinal_flag | 12])
        )

        # Delay imports: FooBar.DLL again (different import type) and
        # WS2_32.dll only present in the delay directory.
        delay_foobar_int_rva = self._put(self._thunk_table([rva_write]))
        delay_foobar_iat_rva = self._put(self._thunk_table([rva_write]))
        delay_ws_int_rva = self._put(self._thunk_table([rva_recv]))
        delay_ws_iat_rva = self._put(self._thunk_table([rva_recv]))

        normal_descriptor_rva = self._put(
            struct.pack(
                "<IIIII",
                normal_ilt_rva,
                0,
                0,
                rva_foobar,
                normal_iat_rva,
            )
            + bytes(20)
        )
        delay_foobar_descriptor_rva = self._put(
            struct.pack(
                "<IIIIIIII",
                0,
                rva_foobar,
                0,
                delay_foobar_iat_rva,
                delay_foobar_int_rva,
                0,
                0,
                0,
            )
        )
        delay_ws_descriptor_rva = self._put(
            struct.pack(
                "<IIIIIIII",
                0,
                rva_ws2,
                0,
                delay_ws_iat_rva,
                delay_ws_int_rva,
                0,
                0,
                0,
            )
            + bytes(32)
        )

        section = self.pe.sections[0]
        self.pe.__data__[self.base_offset : self.base_offset] = self.blob
        section.Misc_VirtualSize = (
            self.base_rva - section.VirtualAddress + len(self.blob)
        )
        section.SizeOfRawData = section.SizeOfRawData + len(self.blob)
        self.pe.OPTIONAL_HEADER.SizeOfImage = self.base_rva + len(self.blob)

        directories = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY
        directories[1].VirtualAddress = normal_descriptor_rva
        directories[1].Size = 40
        directories[13].VirtualAddress = delay_foobar_descriptor_rva
        directories[13].Size = 64

        return self.pe.write(), {
            "bound_value": rva_ws2,
            "create_name_rva": rva_create,
            "write_name_rva": rva_write,
            "recv_name_rva": rva_recv,
            "normal_ilt_rva": normal_ilt_rva,
            "normal_iat_rva": normal_iat_rva,
            "normal_descriptor_rva": normal_descriptor_rva,
            "delay_foobar_descriptor_rva": delay_foobar_descriptor_rva,
            "delay_ws_descriptor_rva": delay_ws_descriptor_rva,
            "delay_ws_int_rva": delay_ws_int_rva,
        }


@pytest.fixture(params=[(PE_32, False), (PE_64, True)], ids=["pe32", "pe64"])
def loaded_graph(request):
    fixture, is_64 = request.param
    raw, layout = ImportImageBuilder(fixture, is_64).build()
    pe = pefile.PE(data=raw)
    return pe, pe.get_import_graph(), layout, is_64


def test_graph_contains_module_and_library_nodes(loaded_graph):
    pe, graph, layout, is_64 = loaded_graph

    assert graph.module.kind == "module"
    assert graph.module.name is None
    assert graph.module.key is None
    assert graph.nodes[0] is graph.module

    library_names = {node.name for node in graph.nodes if node.kind == "library"}
    assert library_names == {b"FooBar.DLL", b"WS2_32.dll"}
    for node in graph.nodes:
        if node.kind == "library":
            assert node.key == node.name.lower()


def test_same_library_uses_one_entity_for_both_import_types(loaded_graph):
    pe, graph, layout, is_64 = loaded_graph

    foobar = graph.get_library_node(b"FOOBAR.DLL")
    assert foobar is not None
    assert foobar.name == b"FooBar.DLL"

    foobar_edges = graph.get_edges(b"FooBar.DLL")
    assert {edge.import_type for edge in foobar_edges} == {"import", "delay"}
    assert all(edge.target is foobar for edge in foobar_edges)
    assert all(edge.source is graph.module for edge in foobar_edges)

    # The two import types must not be collapsed into one untyped edge.
    assert len(foobar_edges) == 3
    assert all(edge.import_type in ("import", "delay") for edge in graph.edges)
    assert len(graph.get_edges(b"FooBar.DLL", import_type="import")) == 2
    assert len(graph.get_edges(b"FooBar.DLL", import_type="delay")) == 1


def test_named_and_ordinal_imports_keep_their_data(loaded_graph):
    pe, graph, layout, is_64 = loaded_graph
    thunk_size = 8 if is_64 else 4
    image_base = pe.OPTIONAL_HEADER.ImageBase

    normal_edges = graph.get_edges(b"FooBar.DLL", import_type="import")
    assert [edge.table_index for edge in normal_edges] == [0, 1]

    named, ordinal = normal_edges
    assert named.name == b"CreateFileA"
    assert named.ordinal is None
    assert named.import_by_ordinal is False
    assert named.hint == 7
    assert named.hint_name_table_rva == layout["create_name_rva"]
    assert named.name_offset == pe.get_offset_from_rva(
        layout["create_name_rva"] + 2
    )

    assert ordinal.name is None
    assert ordinal.ordinal == 12
    assert ordinal.import_by_ordinal is True
    assert ordinal.hint is None

    for index, edge in enumerate(normal_edges):
        expected_iat_rva = layout["normal_iat_rva"] + index * thunk_size
        assert edge.iat_rva == expected_iat_rva
        assert edge.address == image_base + expected_iat_rva
        assert edge.iat_offset == pe.get_offset_from_rva(expected_iat_rva)
        assert edge.thunk_rva == layout["normal_ilt_rva"] + index * thunk_size
        assert edge.thunk_offset == pe.get_offset_from_rva(edge.thunk_rva)


def test_bound_address_and_table_positions_are_preserved(loaded_graph):
    pe, graph, layout, is_64 = loaded_graph

    named = graph.get_edges(b"FooBar.DLL", import_type="import")[0]
    assert named.bound == layout["bound_value"]
    assert graph.get_edges(b"FooBar.DLL", import_type="import")[1].bound is None

    assert named.descriptor_index == 0
    assert named.descriptor_rva == layout["normal_descriptor_rva"]
    assert named.descriptor_offset == pe.get_offset_from_rva(
        layout["normal_descriptor_rva"]
    )

    delay_edges = graph.get_edges(import_type="delay")
    assert [
        (edge.dll, edge.descriptor_index, edge.table_index) for edge in delay_edges
    ] == [(b"FooBar.DLL", 0, 0), (b"WS2_32.dll", 1, 0)]

    ws_edge = graph.get_edges(b"WS2_32.dll")[0]
    assert ws_edge.name == b"recv"
    assert ws_edge.hint == 3
    assert ws_edge.descriptor_rva == layout["delay_ws_descriptor_rva"]
    assert ws_edge.thunk_rva == layout["delay_ws_int_rva"]


def test_edges_are_linked_from_both_endpoints(loaded_graph):
    pe, graph, layout, is_64 = loaded_graph

    assert len(graph.module.edges) == len(graph.edges)
    for edge in graph.edges:
        assert edge in edge.source.edges
        assert edge in edge.target.edges


def test_empty_or_unparsed_import_tables_are_not_failures():
    for fixture in (PE_32, PE_64):
        pe = pefile.PE(data=fixture)
        graph = pe.get_import_graph()
        assert graph.nodes == [graph.module]
        assert graph.edges == []
        assert graph.get_library_node(b"anything.dll") is None

        fast_pe = pefile.PE(data=fixture, fast_load=True)
        fast_graph = fast_pe.get_import_graph()
        assert fast_graph.edges == []
        assert fast_graph.nodes == [fast_graph.module]


def test_dll_names_only_keeps_library_without_edges(loaded_graph):
    pe, graph, layout, is_64 = loaded_graph

    dll_only = pefile.PE(data=pe.write())
    dll_only.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]],
        import_dllnames_only=True,
    )
    names_only_graph = dll_only.get_import_graph()

    foobar = names_only_graph.get_library_node(b"FooBar.DLL")
    assert foobar is not None
    assert foobar.name == b"FooBar.DLL"
    # Symbols were not parsed, so the standard directory contributes no edges.
    assert names_only_graph.get_edges(import_type="import") == []
    # The separately parsed delay directory still produces its edges.
    assert len(names_only_graph.get_edges(import_type="delay")) == 2


def test_broken_table_does_not_abort_graph_for_other_directory():
    raw, layout = ImportImageBuilder(PE_32, False).build()
    pe = pefile.PE(data=raw, fast_load=True)
    # Point the standard import directory at the all-zeroes descriptor that
    # terminates it: parsing it yields no descriptors, while the delay
    # directory stays valid.
    directories = pe.OPTIONAL_HEADER.DATA_DIRECTORY
    directories[1].VirtualAddress = layout["normal_descriptor_rva"] + 20
    pe = pefile.PE(data=pe.write())

    graph = pe.get_import_graph()
    assert graph.get_edges(import_type="import") == []
    assert len(graph.get_edges(import_type="delay")) == 2
    assert graph.get_library_node(b"WS2_32.dll") is not None
