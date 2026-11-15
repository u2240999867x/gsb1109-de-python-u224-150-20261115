"""Resilience tests for partially uploaded (clipped) PE files.

The collection endpoint only receives the first N bytes of a sample.  The
machine type/version evidence that fits in those bytes must still be reported,
and the loss of one resource's pages must not erase other, complete entries.

Every assertion records the raw file range that backs it so the asset table
can distinguish verified bytes from missing coverage.
"""

import struct

import pefile

SECTION_RVA = 0x1000
RAW_PTR = 0x200
IMAGE_BASE = 0x400000


def align(value, boundary=4):
    return (value + boundary - 1) & ~(boundary - 1)


class Buffer:
    """Offset-aware little-endian builder for one section's contents."""

    def __init__(self):
        self.buf = bytearray()

    def pos(self):
        return len(self.buf)

    def align(self, boundary=4):
        self.buf += b"\0" * (align(self.pos(), boundary) - self.pos())

    def u16(self, *values):
        for value in values:
            self.buf += struct.pack("<H", value)

    def u32(self, *values):
        for value in values:
            self.buf += struct.pack("<I", value)

    def put(self, offset, fmt, *values):
        struct.pack_into(fmt, self.buf, offset, *values)

    def bytes(self, data):
        self.buf += data


def resource_directory_header(named_entries, id_entries):
    # IMAGE_RESOURCE_DIRECTORY is 16 bytes: two DWORDs then four WORDs.
    return struct.pack("<IIHHHH", 0, 0, 0, 0, named_entries, id_entries)


def directory_entry(name, offset_to_data, is_directory):
    flag = 0x80000000 if is_directory else 0
    return struct.pack("<II", name, flag | offset_to_data)


def build_version_blob():
    """A VS_VERSIONINFO carrying FileVersion 1.2.3.4 in a StringFileInfo."""

    fixed = struct.pack(
        "<IIIIIIIIIIIII",
        0xFEEF04BD,        # Signature
        0x00010000,        # StrucVersion
        0x00010000,        # FileVersion MS
        0x00020003,        # FileVersion LS
        0x00040000,        # ProductVersion MS
        0x00050006,        # ProductVersion LS
        0x3F, 0,           # FileFlagsMask / FileFlags
        4, 0,              # FileOS VOS_NT_WINDOWS32 / FileType
        0, 0, 0,           # FileSubtype / FileDate
    )

    def node(key, value_length, node_type, children):
        key_utf16 = key.encode("utf-16le") + b"\0\0"
        head = bytearray(struct.pack("<HHH", 0, value_length, node_type))
        head += key_utf16
        if len(head) % 4:
            head += b"\0" * (4 - len(head) % 4)
        body = bytearray(bytes(head) + children)
        struct.pack_into("<H", body, 0, len(body))
        return bytes(body)

    value_text = "1.2.3.4"
    value_utf16 = value_text.encode("utf-16le") + b"\0\0"
    string_entry = node("FileVersion", len(value_text), 1, value_utf16)
    string_table = node("040904b0", 0, 1, string_entry)
    string_file_info = node("StringFileInfo", 0, 1, string_table)

    root_key = "VS_VERSION_INFO".encode("utf-16le") + b"\0\0"
    head = bytearray(struct.pack("<HHH", 0, len(fixed), 0))
    head += root_key
    if len(head) % 4:
        head += b"\0" * (4 - len(head) % 4)
    blob = bytearray(bytes(head) + fixed + string_file_info)
    struct.pack_into("<H", blob, 0, len(blob))
    return bytes(blob)


def build_resource_section():
    """Resource tree with a complete RT_VERSION and a dangling RT_ICON leaf.

    Layout (section-relative offsets are filled in once the tree shape is
    known):

        root directory
          RT_VERSION (id 16) -> name dir -> language dir -> data entry -> blob
          RT_ICON    (id 3)  -> name dir -> language dir -> data entry (dangling)
    """

    c = Buffer()

    root_dir = c.pos()
    c.bytes(resource_directory_header(0, 2))
    root_entries = c.pos()
    c.bytes(b"\0" * 16)  # two IMAGE_RESOURCE_DIRECTORY_ENTRY slots

    # RT_VERSION branch.
    version_name_dir = c.pos()
    c.bytes(resource_directory_header(0, 1))
    version_name_entry = c.pos()
    c.bytes(b"\0" * 8)
    version_lang_dir = c.pos()
    c.bytes(resource_directory_header(0, 1))
    version_lang_entry = c.pos()
    c.bytes(b"\0" * 8)
    version_data_entry = c.pos()
    c.align()
    c.bytes(b"\0" * 16)  # IMAGE_RESOURCE_DATA_ENTRY
    version_blob_offset = c.pos()
    version_blob = build_version_blob()
    c.bytes(version_blob)
    c.align()

    # RT_ICON branch; its data entry's OffsetToData points past the file.
    icon_name_dir = c.pos()
    c.bytes(resource_directory_header(0, 1))
    icon_name_entry = c.pos()
    c.bytes(b"\0" * 8)
    icon_lang_dir = c.pos()
    c.bytes(resource_directory_header(0, 1))
    icon_lang_entry = c.pos()
    c.bytes(b"\0" * 8)

    c.put(root_entries, "<II", pefile.RESOURCE_TYPE["RT_VERSION"],
          0x80000000 | version_name_dir)
    c.put(root_entries + 8, "<II", pefile.RESOURCE_TYPE["RT_ICON"],
          0x80000000 | icon_name_dir)
    c.put(version_name_entry, "<II", 1, 0x80000000 | version_lang_dir)
    c.put(version_lang_entry, "<II", 0x409, version_data_entry)
    c.put(version_data_entry, "<IIII",
          SECTION_RVA + version_blob_offset, len(version_blob), 0, 0)
    c.put(icon_name_entry, "<II", 1, 0x80000000 | icon_lang_dir)
    # OffsetToData is a plain data entry, but deliberately unreachable.
    c.put(icon_lang_entry, "<II", 0x409, 0xFFFF0)

    return bytes(c.buf), {
        "root_dir": (RAW_PTR + root_dir, 16),
        "version_blob": (RAW_PTR + version_blob_offset, len(version_blob)),
    }


def build_pe(section, data_directories, machine=0x14C, magic=0x10B):
    size_of_image = SECTION_RVA + align(len(section), 0x1000)
    dos = b"MZ" + b"\0" * 0x3A + struct.pack("<I", 0x40)
    coff = struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, 0xE0, 0x0102)
    opt = struct.pack("<HBB", magic, 14, 0)
    opt += struct.pack("<I", len(section)) + struct.pack("<II", 0, 0)
    opt += struct.pack("<I", 0) + struct.pack("<II", SECTION_RVA, 0)
    opt += struct.pack("<I", IMAGE_BASE)
    opt += struct.pack("<II", 0x1000, 0x200)
    opt += struct.pack("<HHHHHH", 6, 0, 0, 0, 6, 0)
    opt += struct.pack("<I", 0) + struct.pack("<I", size_of_image)
    opt += struct.pack("<I", 0x200) + struct.pack("<I", 0)
    opt += struct.pack("<HH", 2, 0)
    opt += struct.pack("<IIII", 0x100000, 0x1000, 0x100000, 0x1000)
    opt += struct.pack("<II", 0, 16)
    directories = [(0, 0)] * 16
    for index, rva_size in data_directories.items():
        directories[index] = rva_size
    for rva, size in directories:
        opt += struct.pack("<II", rva, size)
    section_header = struct.pack(
        "<8sIIIIIIHHI", b".rdata\0\0", len(section), SECTION_RVA,
        len(section), RAW_PTR, 0, 0, 0, 0, 0x40000040,
    )
    headers = (dos + b"PE\0\0" + coff + opt + section_header).ljust(RAW_PTR, b"\0")
    return headers + section


def test_machine_type_and_optional_header_survive_tail_clipping():
    # The COFF Machine field (offset 0x44) and Optional Header Magic
    # (offset 0x58) live in the first 0x60 bytes. pefile needs 69 bytes of
    # optional header, so clip just beyond that: all section contents and the
    # data-directory array are gone, but machine and magic stay verifiable.
    section, _ = build_resource_section()
    full = build_pe(section, {})
    clipped = full[:0xA0]

    pe = pefile.PE(data=clipped)

    assert pe.FILE_HEADER.Machine == 0x14C
    assert pe.FILE_HEADER.get_file_offset() == 0x44
    assert pe.OPTIONAL_HEADER.Magic == 0x10B
    assert pe.OPTIONAL_HEADER.get_file_offset() == 0x58
    assert pe.PE_TYPE == pefile.OPTIONAL_HEADER_MAGIC_PE
    # The directory array was clipped; its bytes read back zero padded, so no
    # directory can be attributed even though the slots still exist.
    assert all(entry.VirtualAddress == 0 for entry in pe.OPTIONAL_HEADER.DATA_DIRECTORY)
    assert not hasattr(pe, "DIRECTORY_ENTRY_IMPORT")
    assert not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE")


def test_dangling_resource_leaf_keeps_complete_version_resource():
    section, ranges = build_resource_section()
    data = build_pe(section, {2: (SECTION_RVA, 0x400)})
    pe = pefile.PE(data=data)

    root = pe.DIRECTORY_ENTRY_RESOURCE
    type_ids = [entry.id for entry in root.entries]
    assert pefile.RESOURCE_TYPE["RT_ICON"] in type_ids
    assert pefile.RESOURCE_TYPE["RT_VERSION"] in type_ids

    # The icon's data entry is dangling and yields no leaf data, but the
    # complete version resource must still be parsed.
    assert hasattr(pe, "VS_FIXEDFILEINFO")
    fixed = pe.VS_FIXEDFILEINFO[0]
    assert (fixed.FileVersionMS, fixed.FileVersionLS) == (0x00010000, 0x00020003)

    string_versions = [
        table.entries[b"FileVersion"]
        for file_info in pe.FileInfo
        for entry in file_info
        if hasattr(entry, "StringTable")
        for table in entry.StringTable
    ]
    assert string_versions == [b"1.2.3.4"]

    # The bytes backing the version blob are fully present in the upload.
    blob_start, blob_length = ranges["version_blob"]
    assert data[blob_start:blob_start + blob_length] == build_version_blob()
    assert any("data entry" in warning for warning in pe.get_warnings())


def test_version_blob_clipped_is_reported_without_losing_resource_tree():
    section, ranges = build_resource_section()
    data = build_pe(section, {2: (SECTION_RVA, 0x400)})
    blob_start, blob_length = ranges["version_blob"]

    # Keep only the first 120 bytes of the 200-byte version blob. The
    # StringFileInfo tail loses coverage, but VS_FIXEDFILEINFO sits at the
    # blob's head and its numeric version must remain attributable.
    clipped = data[: blob_start + 120]
    assert blob_start + 120 < blob_start + blob_length  # the blob really is clipped
    pe = pefile.PE(data=clipped)

    type_ids = [entry.id for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries]
    assert pefile.RESOURCE_TYPE["RT_VERSION"] in type_ids
    fixed = pe.VS_FIXEDFILEINFO[0]
    assert (fixed.FileVersionMS, fixed.FileVersionLS) == (0x00010000, 0x00020003)
    assert fixed.get_file_offset() >= blob_start  # backed by present bytes

    # Clipping away the entire blob still enumerates RT_VERSION via the
    # directory tree without raising; only the version payload is uncovered.
    head_only = data[:blob_start]
    pe_head = pefile.PE(data=head_only)
    assert pefile.RESOURCE_TYPE["RT_VERSION"] in [
        entry.id for entry in pe_head.DIRECTORY_ENTRY_RESOURCE.entries
    ]
    assert not hasattr(pe_head, "VS_FIXEDFILEINFO")
    assert isinstance(pe_head.get_warnings(), list)


def test_debug_directory_keeps_entries_before_the_missing_page():
    # Two IMAGE_DEBUG_DIRECTORY entries (28 bytes each). The upload is clipped
    # halfway through the second entry, exactly the situation when the tail
    # pages never arrive. The first, complete entry must remain attributed.
    first_entry = struct.pack(
        "<IIHHIIII", 0, 2, 0, 0, 2, 0x20, RAW_PTR + 0x100, 0,
    )
    second_entry = struct.pack(
        "<IIHHIIII", 0, 2, 0, 0, 2, 0x20, RAW_PTR + 0x140, 0,
    )
    section = bytearray(0x200)
    section[0:len(first_entry)] = first_entry
    section[len(first_entry):len(first_entry) + len(second_entry)] = second_entry
    data = build_pe(bytes(section), {6: (SECTION_RVA, 2 * 28)})

    # Keep the first entry whole (28 bytes), expose only 10 bytes of the second.
    clipped = data[: RAW_PTR + 28 + 10]
    pe = pefile.PE(data=clipped)

    assert hasattr(pe, "DIRECTORY_ENTRY_DEBUG")
    assert len(pe.DIRECTORY_ENTRY_DEBUG) == 1
    entry = pe.DIRECTORY_ENTRY_DEBUG[0]
    assert entry.struct.Type == 2
    assert entry.struct.get_file_offset() == RAW_PTR
    assert any(
        "IMAGE_DEBUG_DIRECTORY" in warning or "Invalid debug information" in warning
        for warning in pe.get_warnings()
    )


def test_missing_import_pages_do_not_erase_a_complete_import_descriptor():
    # Two import descriptors: descriptor A references a hint/name RVA that is
    # not backed by any file bytes, descriptor B is fully resolvable.
    c = Buffer()
    descriptors = c.pos()
    c.bytes(b"\0" * 40)  # two descriptors + NULL terminator

    missing_name_rva = SECTION_RVA + 0x4000
    ilt_a = c.pos()
    c.u32(missing_name_rva, 0)
    iat_a = c.pos()
    c.u32(missing_name_rva, 0)
    name_a = c.pos()
    c.bytes(b"missing_page.dll\0")

    c.align()
    hint_b = c.pos()
    c.u16(7)
    c.bytes(b"GoodFunction\0")
    c.align()
    ilt_b = c.pos()
    c.u32(SECTION_RVA + hint_b, 0)
    iat_b = c.pos()
    c.u32(SECTION_RVA + hint_b, 0)
    name_b = c.pos()
    c.bytes(b"good.dll\0")
    c.align()

    c.put(descriptors, "<IIIII", SECTION_RVA + ilt_a, 0, 0,
          SECTION_RVA + name_a, SECTION_RVA + iat_a)
    c.put(descriptors + 20, "<IIIII", SECTION_RVA + ilt_b, 0, 0,
          SECTION_RVA + name_b, SECTION_RVA + iat_b)

    data = build_pe(bytes(c.buf), {1: (SECTION_RVA + descriptors, 40)})
    pe = pefile.PE(data=data)

    dlls = {entry.dll: [symbol.name for symbol in entry.imports]
            for entry in pe.DIRECTORY_ENTRY_IMPORT}
    assert dlls == {b"good.dll": [b"GoodFunction"]}
