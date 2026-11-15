"""
Tests for the lossless options view: options must survive a read-write
round trip preserving occurrence order, duplicates, raw bytes and their
owning block; malformed option lengths must report the position within
the original block.
"""

import io

import pytest

import pcapng.blocks as blocks
from pcapng import FileScanner, FileWriter
from pcapng.exceptions import CorruptedFile
from pcapng.structs import (
    Option,
    Options,
    RawOption,
    read_options,
    read_options_raw,
    write_options,
)

SCHEMA = [Option(2, "if_name", "string")]

# Interleaved codes, a triplicated comment and vendor-private options
RAW_OPTIONS = [
    (1, b"comment one"),
    (0x6000, b"\xde\xad\xbe\xef"),  # vendor-private, unknown code
    (1, b"comment two"),
    (0x6000, b"\x01\x02\x03"),
    (1, b"comment three"),
    (2, b"eth0"),
]


def make_options():
    return Options(schema=SCHEMA, data=list(RAW_OPTIONS), endianness="<")


def roundtrip(options, endianness="<"):
    stream = io.BytesIO()
    write_options(stream, options)
    stream.seek(0)
    return read_options(stream, endianness)


def test_read_options_raw_reports_offsets():
    stream = io.BytesIO(
        b"\x01\x00\x05\x00hello\x00\x00\x00"
        b"\x02\x00\x04\x00spam"
        b"\x00\x00\x00\x00"
    )
    assert read_options_raw(stream, "<") == [
        (1, b"hello", 0),
        (2, b"spam", 12),
    ]


def test_read_options_keeps_plain_tuples():
    stream = io.BytesIO(
        b"\x01\x00\x05\x00hello\x00\x00\x00"
        b"\x02\x00\x04\x00spam"
        b"\x00\x00\x00\x00"
    )
    assert read_options(stream, "<") == [(1, b"hello"), (2, b"spam")]


def test_raw_view_preserves_order_duplicates_and_raw_bytes():
    options = make_options()
    assert [entry.code for entry in options.raw] == [1, 0x6000, 1, 0x6000, 1, 2]
    assert [entry.value for entry in options.raw] == [
        "comment one",
        b"\xde\xad\xbe\xef",
        "comment two",
        b"\x01\x02\x03",
        "comment three",
        "eth0",
    ]
    # Unmodified entries keep their original raw bytes
    for entry, (code, payload) in zip(options.raw, RAW_OPTIONS):
        assert entry.raw == payload


def test_roundtrip_is_lossless():
    options = make_options()
    assert roundtrip(options) == RAW_OPTIONS


def test_repeated_vendor_private_options_are_kept():
    # A device writing the same (unknown, vendor-private) option three
    # times must not crash the reader, and must round trip untouched
    data = [(0x4D42, b"aaa"), (0x4D42, b"bbb"), (0x4D42, b"ccc")]
    options = Options(schema=[], data=data, endianness="<")
    assert options.get_all(0x4D42) == [b"aaa", b"bbb", b"ccc"]
    assert roundtrip(options) == data


def test_edit_single_comment_does_not_reorder_duplicates():
    options = make_options()
    # Edit only the third comment through the raw view
    options.raw[4].value = "edited comment"
    # The edited entry lost its original raw bytes, the others didn't
    assert options.raw[4].raw is None
    assert options.raw[0].raw == b"comment one"
    assert options.raw[2].raw == b"comment two"

    assert roundtrip(options) == [
        (1, b"comment one"),
        (0x6000, b"\xde\xad\xbe\xef"),
        (1, b"comment two"),
        (0x6000, b"\x01\x02\x03"),
        (1, b"edited comment"),
        (2, b"eth0"),
    ]


def test_setitem_keeps_original_position():
    options = make_options()
    options["if_name"] = "renamed"
    codes = [entry.code for entry in options.raw]
    assert codes == [1, 0x6000, 1, 0x6000, 1, 2]
    assert options.raw[5].value == "renamed"
    assert options.raw[5].raw is None
    # Everything else is still byte-identical
    assert roundtrip(options)[:5] == RAW_OPTIONS[:5]


def test_delete_option_keeps_other_entries_in_place():
    options = make_options()
    del options["opt_comment"]
    assert [entry.code for entry in options.raw] == [0x6000, 0x6000, 2]
    assert roundtrip(options) == [
        (0x6000, b"\xde\xad\xbe\xef"),
        (0x6000, b"\x01\x02\x03"),
        (2, b"eth0"),
    ]


def test_raw_option_entry_attributes():
    options = make_options()
    entry = options.raw[0]
    assert isinstance(entry, RawOption)
    assert entry.code == 1
    assert entry.name == "opt_comment"
    assert entry.value == "comment one"
    assert entry.raw == b"comment one"
    # Vendor-private entries have no known name
    assert options.raw[1].name == 0x6000


def test_options_know_their_owning_block():
    shb = blocks.SectionHeader(endianness="<", options={"shb_hardware": "pytest"})
    idb = shb.new_member(
        blocks.InterfaceDescription,
        link_type=1,
        snaplen=65535,
        options={"if_name": "Interface Zero"},
    )
    stream = io.BytesIO()
    FileWriter(stream, shb)
    stream.seek(0)
    in_shb, in_idb = list(FileScanner(stream))

    assert in_shb.options.block is in_shb
    assert in_idb.options.block is in_idb
    assert in_idb.options.raw[0].block is in_idb
    assert in_idb.options.raw[0].name == "if_name"
    assert in_idb.options.raw[0].raw == b"Interface Zero"


def test_file_roundtrip_preserves_duplicate_comments():
    shb = blocks.SectionHeader(endianness="<")
    idb = shb.new_member(blocks.InterfaceDescription, link_type=1, snaplen=65535)
    idb.options.add("opt_comment", "first")
    idb.options.add("opt_comment", "second")
    idb.options.add("opt_comment", "third")

    stream = io.BytesIO()
    FileWriter(stream, shb)
    original = stream.getvalue()

    stream.seek(0)
    in_shb, in_idb = list(FileScanner(stream))
    assert in_idb.options.get_all("opt_comment") == ["first", "second", "third"]

    # Write the parsed blocks again: comments keep their original order
    out = io.BytesIO()
    FileWriter(out, in_shb)
    out.seek(0)
    _, re_idb = list(FileScanner(out))
    assert re_idb.options.get_all("opt_comment") == ["first", "second", "third"]
    assert [e.code for e in re_idb.options.raw] == [1, 1, 1]


def test_bad_option_length_points_to_block_position():
    # IDB payload: link_type(2) + reserved(2) + snaplen(4) = 8 bytes,
    # then an option header declaring a bogus length
    payload = (
        b"\x01\x00\x00\x00\xff\xff\x00\x00"  # fixed fields (8 bytes)
        b"\x01\x00\xff\xff"  # option 1, length 65535
        b"xy"  # only 2 bytes of payload available
    )
    with pytest.raises(CorruptedFile) as ctx:
        blocks.InterfaceDescription(raw=payload, endianness="<", section=None)
    message = str(ctx.value)
    assert "offset 8" in message
    assert "65535" in message


def test_bad_option_length_at_stream_level():
    stream = io.BytesIO(
        b"\x02\x00\x04\x00spam"  # one good option, 8 bytes
        b"\x01\x00\x00\x80"  # option 1 declares 32768 bytes
        b"xy"
    )
    with pytest.raises(CorruptedFile) as ctx:
        read_options(stream, "<")
    assert "offset 8" in str(ctx.value)
