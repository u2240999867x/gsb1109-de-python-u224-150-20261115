"""Tests for language-aware version resource selection.

The fixtures are minimal PE32 images with one RT_VERSION resource containing
several language leaves. They keep the tests independent of the external
pefile-tests corpus.
"""

import struct

import pytest

import pefile


def _block(body, value_length=0):
    body = bytearray(body)
    struct.pack_into("<HHH", body, 0, len(body), value_length, 1)
    return bytes(body)


def _pad(data, alignment=4):
    data = bytearray(data)
    while len(data) % alignment:
        data.append(0)
    return data


def _string(key, value_bytes, value_length):
    if isinstance(key, bytes):
        key = key.decode("ascii")
    body = bytearray(6)
    body += key.encode("utf-16-le") + b"\0\0"
    body = _pad(body)
    body += value_bytes
    return _block(body, value_length)


def _string_table(lang_id, entries):
    body = bytearray(6)
    body += lang_id.encode("utf-16-le") + b"\0\0"
    body = _pad(body)
    for key, (value_bytes, value_length) in entries.items():
        body += _string(key, value_bytes, value_length)
    return _block(body)


def _string_file_info(tables):
    body = bytearray(6)
    body += "StringFileInfo".encode("utf-16-le") + b"\0\0"
    body = _pad(body)
    for table in tables:
        body += table
    return _block(body)


def _version_info(children):
    key = "VS_VERSION_INFO".encode("utf-16-le") + b"\0\0"
    fixed = struct.pack(
        "<IIIIIIIIIIIII",
        0xFEEF04BD,
        0x00010000,
        0x00010000,
        0,
        0x00010000,
        0,
        0x3F,
        0,
        0x00040004,
        0,
        0,
        0,
        0,
    )
    body = bytearray(6)
    body += key
    body = _pad(body)
    body += fixed
    for child in children:
        body += child
    return _block(body, len(fixed))


def _version_leaf(tables):
    return _version_info([_string_file_info(tables)])


def utf16(text):
    return text.encode("utf-16-le") + b"\0\0", len(text) + 1


def ansi(text, encoding):
    raw = text.encode(encoding) + b"\0"
    return raw, len(raw)


def _resource_blob(leaves, va):
    """Build the .rsrc bytes for one RT_VERSION type entry."""
    count = len(leaves)
    lang_offset = 0x30
    data_offset = lang_offset + 16 + 8 * count
    blob_offset = data_offset + 16 * count

    placed = []
    cursor = blob_offset
    for lang_id, blob in leaves:
        placed.append((lang_id, blob, cursor))
        cursor = (cursor + len(blob) + 3) & ~3

    resource = bytearray(cursor)
    struct.pack_into("<IIHHHH", resource, 0x00, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", resource, 0x10, 16, 0x80000000 | 0x18)
    struct.pack_into("<IIHHHH", resource, 0x18, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", resource, 0x28, 1, 0x80000000 | lang_offset)
    struct.pack_into("<IIHHHH", resource, lang_offset, 0, 0, 0, 0, 0, count)
    for idx in range(count):
        struct.pack_into(
            "<II",
            resource,
            lang_offset + 16 + 8 * idx,
            placed[idx][0],
            data_offset + 16 * idx,
        )
    for idx, (_lang_id, blob, offset) in enumerate(placed):
        struct.pack_into(
            "<IIII", resource, data_offset + 16 * idx, va + offset, len(blob), 0, 0
        )
        resource[offset : offset + len(blob)] = blob
    return bytes(resource)


def make_pe(leaves, va=0x1A0, pointer_to_raw_data=0x160):
    """Build a PE32 whose resource directory contains ``leaves``.

    ``leaves`` is a list of ``(lang_id_word, version_leaf_bytes)`` pairs.
    """
    resource = _resource_blob(leaves, va)
    dos = b"MZ" + b"\0" * 0x3A + struct.pack("<I", 0x40)
    coff = struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 0xE0, 0x2102)
    size_of_image = (va + len(resource) + 0xF) & ~0xF
    optional = struct.pack("<HBBIIIIII", 0x10B, 0, 0, 0, 0, 0, 0, 0, 0)
    optional += struct.pack("<III", 0x400000, 0x10, 0x10)
    optional += struct.pack("<HHHHHH", 4, 0, 0, 0, 4, 0)
    optional += struct.pack("<I", 0)
    optional += struct.pack("<II", size_of_image, pointer_to_raw_data)
    optional += struct.pack("<IHH", 0, 2, 0)
    optional += struct.pack("<IIII", 0, 0, 0, 0)
    optional += struct.pack("<II", 0, 16)
    directories = [(0, 0)] * 16
    directories[2] = (va, len(resource))
    for rva, size in directories:
        optional += struct.pack("<II", rva, size)
    section = struct.pack(
        "<8sIIIIIIHHI",
        b".rsrc\0\0\0",
        len(resource),
        va,
        len(resource),
        pointer_to_raw_data,
        0,
        0,
        0,
        0,
        0x40000040,
    )
    return dos + b"PE\0\0" + coff + optional + section + resource


def leaf(lang_id, entries):
    return _version_leaf([_string_table(lang_id, entries)])


EN = leaf("040904B0", {b"FileDescription": utf16("English")})
FR = leaf("040C04B0", {b"FileDescription": utf16("Francais")})


def test_all_version_leaves_are_kept():
    pe = pefile.PE(data=make_pe([(0x0409, EN), (0x040C, FR)]))

    assert len(pe.FileInfo) == 2
    assert [
        table.LangID for file_info in pe.FileInfo for table in file_info[0].StringTable
    ] == [b"040904B0", b"040C04B0"]
    assert [table.entries for table in pe.get_version_string_tables(0)] == [
        {b"FileDescription": b"English"}
    ]


def test_display_name_follows_caller_fallback_order():
    pe = pefile.PE(data=make_pe([(0x0409, EN), (0x040C, FR)]))

    assert pe.get_version_display_name(["fr-FR", "en-US"]) == b"Francais"
    assert pe.get_version_display_name(["en-US", "fr-FR"]) == b"English"
    assert pe.get_version_display_name(["ja-JP", "fr-FR"]) == b"Francais"
    assert pe.get_version_display_name(["ja-JP"]) is None


def test_base_language_matches_any_sublanguage():
    canadian = _version_leaf(
        [_string_table("100C04B0", {b"FileDescription": utf16("Canadien")})]
    )
    pe = pefile.PE(data=make_pe([(0x100C, canadian)]))

    assert pe.get_version_display_name(["fr"]) == b"Canadien"
    assert pe.get_version_display_name(["fr-FR"]) is None


def test_later_leaf_does_not_override_earlier_requested_language():
    pe = pefile.PE(data=make_pe([(0x0409, EN), (0x040C, FR)]))

    assert pe.get_version_display_name(["en-US", "fr-FR"]) == b"English"
    assert pe.get_version_display_name(["fr-FR", "en-US"]) == b"Francais"


def test_bare_language_names_are_not_hexadecimal():
    pe = pefile.PE(data=make_pe([(0x0409, EN), (0x040C, FR)]))
    assert pe.get_version_display_name(["de"]) is None
    assert pe.get_version_display_name(["fr"]) == b"Francais"


def test_swapping_directory_order_does_not_change_result():
    forward = pefile.PE(data=make_pe([(0x0409, EN), (0x040C, FR)]))
    reverse = pefile.PE(data=make_pe([(0x040C, FR), (0x0409, EN)]))

    for order in (["en-US"], ["fr-FR"], ["de-DE", "en-US"], ["de-DE", "fr-FR"]):
        assert forward.get_version_display_name(order) == (
            reverse.get_version_display_name(order)
        )


def test_string_tables_are_read_with_their_own_code_page():
    ansi_de = leaf("040704E4", {b"FileDescription": ansi("Grüße", "cp1252")})
    unicode_de = leaf("040704B0", {b"FileDescription": utf16("Grüße")})

    ansi_pe = pefile.PE(data=make_pe([(0x0407, ansi_de)]))
    unicode_pe = pefile.PE(data=make_pe([(0x0407, unicode_de)]))

    assert ansi_pe.get_version_display_name(["de-DE"]) == "Grüße".encode()
    assert unicode_pe.get_version_display_name(["de-DE"]) == "Grüße".encode()


def test_language_declarations_are_preserved():
    pe = pefile.PE(data=make_pe([(0x0409, EN)]))
    table = pe.get_version_string_tables()[0]

    assert table.LangID == b"040904B0"
    assert table.LanguageID == 0x0409
    assert table.CodePage == 0x04B0
    assert table.entries == {b"FileDescription": b"English"}


def test_value_can_be_text_with_encoding():
    pe = pefile.PE(data=make_pe([(0x0409, EN)]))
    assert pe.get_version_display_name(["en-US"], encoding="utf-8") == "English"
    assert pe.get_version_info(
        b"FileDescription", ["en-US"], encoding="utf-8"
    ) == "English"
    assert pe.get_display_name(["en-US"], encoding="utf-8") == "English"


def test_unmodified_file_writes_back_identical_bytes():
    for entries, lang_id in (
        ({b"FileDescription": utf16("English")}, "040904B0"),
        ({b"FileDescription": ansi("Grüße", "cp1252")}, "040904E4"),
    ):
        data = make_pe(
            [(0x0409, _version_leaf([_string_table(lang_id, entries)]))]
        )
        pe = pefile.PE(data=data)
        assert pe.write() == data


def test_missing_key_does_not_stop_fallback():
    partial = leaf("040C04B0", {b"ProductName": utf16("Produit")})
    pe = pefile.PE(data=make_pe([(0x040C, partial), (0x0409, EN)]))

    assert pe.get_version_display_name(["fr-FR", "en-US"]) == b"English"
    assert pe.get_version_display_name(["fr-FR"]) is None


def test_unknown_key_returns_unselected():
    pe = pefile.PE(data=make_pe([(0x0409, EN)]))
    assert pe.get_version_string(b"NotThere", languages=["en-US"]) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
