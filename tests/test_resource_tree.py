import struct

import pefile


SECTION_VA = 0x1000
SECTION_RAW = 0x200

DIR_HEADER = struct.Struct("<IIHHHH")
DIR_ENTRY = struct.Struct("<II")
DATA_ENTRY = struct.Struct("<IIII")


def make_pe(rsrc):
    """Wrap a .rsrc blob in a minimal PE32 with one resource section."""
    dos = b"MZ" + b"\x00" * 0x3A + struct.pack("<I", 0x40)
    coff = struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 0xE0, 0x2102)
    raw_size = (len(rsrc) + 0x1FF) & ~0x1FF
    size_of_image = SECTION_VA + ((len(rsrc) + 0xFFF) & ~0xFFF)
    opt = struct.pack("<HBBIIIIII", 0x10B, 0, 0, 0, 0, 0, 0, 0, 0)
    opt += struct.pack("<III", 0x400000, 0x1000, 0x200)
    opt += struct.pack("<HHHHHH", 4, 0, 0, 0, 4, 0)
    opt += struct.pack("<I", 0)
    opt += struct.pack("<II", size_of_image, SECTION_RAW)
    opt += struct.pack("<IHH", 0, 2, 0)
    opt += struct.pack("<IIII", 0, 0, 0, 0)
    opt += struct.pack("<II", 0, 16)
    data_directories = [(0, 0)] * 16
    data_directories[2] = (SECTION_VA, len(rsrc))
    for rva, size in data_directories:
        opt += struct.pack("<II", rva, size)
    section = struct.pack(
        "<8sIIIIIIHHI",
        b".rsrc\x00\x00\x00",
        raw_size,
        SECTION_VA,
        len(rsrc),
        SECTION_RAW,
        0,
        0,
        0,
        0,
        0x40000040,
    )
    headers = (dos + b"PE\x00\x00" + coff + opt + section).ljust(SECTION_RAW, b"\x00")
    return headers + rsrc


def _name_blob(text):
    return struct.pack("<H", len(text)) + text.encode("utf-16-le") + b"\x00\x00"


def build_rsrc(directories, payloads):
    """Serialize a resource directory graph into a .rsrc blob.

    directories: mapping key -> list of entries. Each entry is
    (kind, value, target) with kind "id" or "name". Targets:
        ("dir", key)                        child or shared directory
        ("back", key)                       real back edge to an ancestor
        ("data", payload_key, code_page)    leaf data entry
        ("baddir", offset)                  directory flag, out of section
        ("baddata", offset)                 data flag, out of section
    payloads: mapping payload_key -> raw bytes.
    """
    # Region 1: directory headers and entry slots, in declaration order.
    dir_offset = {}
    cursor = 0
    for key, entries in directories.items():
        dir_offset[key] = cursor
        cursor += DIR_HEADER.size + DIR_ENTRY.size * len(entries)
    directory_region_end = cursor

    # Region 2: unique name strings.
    name_offset = {}
    name_region = bytearray()
    for entries in directories.values():
        for kind, value, _target in entries:
            if kind == "name" and value not in name_offset:
                name_offset[value] = cursor + len(name_region)
                name_region += _name_blob(value)
    cursor += len(name_region)

    # Region 3: data entry slots, one per leaf edge.
    data_slots = []
    for key, entries in directories.items():
        for index, (_kind, _value, target) in enumerate(entries):
            if target[0] == "data":
                data_slots.append((key, index, cursor))
                cursor += DATA_ENTRY.size

    # Region 4: dword-aligned payloads, deduplicated by payload key.
    payload_offset = {}
    payload_order = []
    for _key, entries in directories.items():
        for _kind, _value, target in entries:
            if target[0] == "data" and target[1] not in payload_offset:
                cursor = (cursor + 3) & ~3
                payload_order.append(target[1])
                payload_offset[target[1]] = cursor
                cursor += len(payloads[target[1]])

    blob = bytearray(cursor)

    # Pack directory headers and entry slots.
    for key, entries in directories.items():
        base = dir_offset[key]
        named = sum(1 for kind, _v, _t in entries if kind == "name")
        DIR_HEADER.pack_into(
            blob, base, 0, 0, 0, 0, named, len(entries) - named
        )
        for index, (kind, value, target) in enumerate(entries):
            slot = base + DIR_HEADER.size + index * DIR_ENTRY.size
            name_field = (
                0x80000000 | name_offset[value] if kind == "name" else value
            )

            if target[0] in ("dir", "back", "baddir"):
                if target[0] == "baddir":
                    offset_field = 0x80000000 | target[1]
                else:
                    offset_field = 0x80000000 | dir_offset[target[1]]
            elif target[0] == "baddata":
                offset_field = target[1]
            else:
                offset_field = next(
                    slot_off
                    for slot_key, slot_index, slot_off in data_slots
                    if slot_key == key and slot_index == index
                )
            DIR_ENTRY.pack_into(blob, slot, name_field, offset_field)

    blob[
        directory_region_end : directory_region_end + len(name_region)
    ] = bytes(name_region)

    # Pack data entries with absolute payload RVAs.
    for slot_key, slot_index, slot_off in data_slots:
        target = directories[slot_key][slot_index][2]
        payload_key = target[1]
        code_page = target[2]
        payload_rva = SECTION_VA + payload_offset[payload_key]
        DATA_ENTRY.pack_into(
            blob,
            slot_off,
            payload_rva,
            len(payloads[payload_key]),
            code_page,
            0,
        )

    # Copy payloads into their reserved slots.
    for payload_key in payload_order:
        off = payload_offset[payload_key]
        blob[off : off + len(payloads[payload_key])] = payloads[payload_key]

    return bytes(blob)


def _leaf_ids(tree_map):
    return [[entry.id for entry in ref.path] for ref in tree_map.references]


def test_basic_tree_keeps_ids_names_codepage_and_language():
    directories = {
        "root": [
            ("id", 24, ("dir", "manifest")),
            ("name", "MYRES", ("dir", "named")),
        ],
        "manifest": [("id", 1, ("dir", "manifest_lang"))],
        "manifest_lang": [
            ("id", 0x409, ("data", "mani", 1252)),
        ],
        "named": [("id", 7, ("dir", "named_lang"))],
        "named_lang": [
            ("id", 0x0411, ("data", "blob", 0)),
        ],
    }
    payloads = {"mani": b"MANI", "blob": b"DATA"}
    pe = pefile.PE(data=make_pe(build_rsrc(directories, payloads)))

    tree_map = pe.traverse_resource_tree()

    assert len(tree_map.entities) == 5
    assert len(tree_map.references) == 2

    manifest_ref, named_ref = tree_map.references
    assert [entry.id for entry in manifest_ref.path] == [24, 1, 0x409]
    assert manifest_ref.cyclic is False
    assert manifest_ref.leaf.language_id == 0x409
    assert manifest_ref.leaf.lang == (0x409 & 0x3FF)
    assert manifest_ref.leaf.sublang == (0x409 >> 10)
    assert manifest_ref.leaf.code_page == 1252
    assert manifest_ref.leaf.size == 4
    assert pe.get_data(manifest_ref.leaf.data_rva, 4) == b"MANI"

    assert [entry.id for entry in named_ref.path] == [None, 7, 0x411]
    assert named_ref.path[0].name == b"MYRES"
    assert named_ref.path[0].name_is_string is True
    assert manifest_ref.path[0].name_is_string is False
    assert named_ref.leaf.language_id == 0x411
    assert named_ref.leaf.lang == 0x11
    assert named_ref.leaf.sublang == 1
    assert named_ref.leaf.code_page == 0


def test_unknown_resource_type_is_not_guessed():
    unknown_id = 32768
    assert unknown_id not in pefile.RESOURCE_TYPE
    directories = {
        "root": [("id", unknown_id, ("dir", "a"))],
        "a": [("id", 1, ("dir", "b"))],
        "b": [("id", 0x409, ("data", "x", 0))],
    }
    pe = pefile.PE(data=make_pe(build_rsrc(directories, {"x": b"X"})))

    tree_map = pe.traverse_resource_tree()
    first = tree_map.references[0].path[0]
    assert first.id == unknown_id
    assert first.name is None


def test_shared_subtree_keeps_every_legal_path():
    directories = {
        "root": [
            ("id", 1, ("dir", "x")),
            ("id", 2, ("dir", "y")),
        ],
        "x": [("id", 10, ("dir", "shared"))],
        "y": [("id", 20, ("dir", "shared"))],
        "shared": [("id", 0x409, ("data", "leaf", 0x4E4))],
    }
    pe = pefile.PE(
        data=make_pe(build_rsrc(directories, {"leaf": b"SHARED"}))
    )

    tree_map = pe.traverse_resource_tree()

    shared_entities = [
        entity for entity in tree_map.entities if entity.rva is not None
    ]
    shared_entity = next(
        entity
        for entity in shared_entities
        if entity.entries is pe.DIRECTORY_ENTRY_RESOURCE.entries[0]
        .directory.entries[0]
        .directory
    )
    # The shared directory is reached by two routes.
    assert len(shared_entity.paths) == 2

    assert _leaf_ids(tree_map) == [
        [1, 10, 0x409],
        [2, 20, 0x409],
    ]
    first_ref, second_ref = tree_map.references
    # Both routes resolve to the same physical data entry structure.
    assert first_ref.leaf.struct is second_ref.leaf.struct
    assert first_ref.leaf.code_page == 0x4E4

    # The parsed tree reuses one object for the shared subtree.
    assert (
        pe.DIRECTORY_ENTRY_RESOURCE.entries[0]
        .directory.entries[0]
        .directory
        is pe.DIRECTORY_ENTRY_RESOURCE.entries[1]
        .directory.entries[0]
        .directory
    )


def test_real_back_edge_is_marked_cyclic_and_terminates():
    directories = {
        "root": [("id", 1, ("dir", "a"))],
        "a": [("id", 10, ("dir", "b"))],
        "b": [
            ("id", 0x409, ("data", "good", 0)),
            ("id", 999, ("back", "root")),
        ],
    }
    pe = pefile.PE(data=make_pe(build_rsrc(directories, {"good": b"OK"})))

    tree_map = pe.traverse_resource_tree()

    leaf_ref = next(ref for ref in tree_map.references if not ref.cyclic)
    cyclic_ref = next(ref for ref in tree_map.references if ref.cyclic)

    assert [entry.id for entry in leaf_ref.path] == [1, 10, 0x409]
    assert leaf_ref.leaf.size == 2

    assert [entry.id for entry in cyclic_ref.path] == [1, 10, 999]
    assert cyclic_ref.leaf is None
    assert cyclic_ref.cycle_target_rva == pe.DIRECTORY_ENTRY_RESOURCE.directory_rva

    back_entry = (
        pe.DIRECTORY_ENTRY_RESOURCE.entries[0]
        .directory.entries[0]
        .directory.entries[1]
    )
    assert back_entry.back_edge is True
    assert not hasattr(back_entry, "directory")
    assert not hasattr(back_entry, "data")


def test_one_bad_edge_does_not_drop_sibling_resources():
    far_offset = 0x100000
    directories = {
        "root": [
            ("id", 1, ("baddir", far_offset)),
            ("id", 2, ("dir", "a")),
            ("id", 3, ("baddata", far_offset)),
            ("id", 4, ("dir", "c")),
        ],
        "a": [("id", 20, ("dir", "b"))],
        "b": [("id", 0x409, ("data", "one", 0))],
        "c": [("id", 40, ("dir", "d"))],
        "d": [("id", 0x409, ("data", "two", 0))],
    }
    pe = pefile.PE(
        data=make_pe(build_rsrc(directories, {"one": b"ONE", "two": b"TWO"}))
    )

    root_ids = [entry.id for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries]
    assert root_ids == [2, 4]

    tree_map = pe.traverse_resource_tree()
    assert _leaf_ids(tree_map) == [
        [2, 20, 0x409],
        [4, 40, 0x409],
    ]
    assert all(not ref.cyclic for ref in tree_map.references)
    assert pe.get_warnings()


def test_path_entries_record_their_edge_rva():
    directories = {
        "root": [("id", 3, ("dir", "a"))],
        "a": [("id", 1, ("dir", "b"))],
        "b": [("id", 0x409, ("data", "x", 0))],
    }
    pe = pefile.PE(data=make_pe(build_rsrc(directories, {"x": b"X"})))

    tree_map = pe.traverse_resource_tree()
    edge_rvas = [entry.rva for entry in tree_map.references[0].path]
    assert all(rva is not None for rva in edge_rvas)
    assert edge_rvas == sorted(edge_rvas)
    assert all(rva >= SECTION_VA for rva in edge_rvas)
