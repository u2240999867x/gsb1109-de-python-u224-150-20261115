"""
Tests for the RecoveringFileScanner: reading possibly-damaged files,
yielding only blocks backed by boundary evidence, and reporting the
first untrusted range.
"""

import io
import struct

from pcapng import RecoveringFileScanner
from pcapng.blocks import InterfaceDescription, SectionHeader, SimplePacket


def make_section_header():
    body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    length = 12 + len(body)
    return b"\x0a\x0d\x0d\x0a" + struct.pack("<I", length) + body + struct.pack(
        "<I", length
    )


def make_interface_description():
    body = struct.pack("<HHI", 1, 0, 0)
    length = 12 + len(body)
    return struct.pack("<II", 1, length) + body + struct.pack("<I", length)


def make_unknown_block(body, block_type=0x00001234, trailing_length=None):
    padding = (4 - (len(body) % 4)) % 4
    body = body + bytes(padding)
    length = 12 + len(body)
    if trailing_length is None:
        trailing_length = length
    return struct.pack("<II", block_type, length) + body + struct.pack(
        "<I", trailing_length
    )


def scan(data):
    scanner = RecoveringFileScanner(io.BytesIO(data))
    verified = list(scanner)
    return verified, scanner.untrusted_range, scanner.untrusted_reason


def test_valid_file_is_fully_trusted():
    with open("test_data/test010.ntar", "rb") as fp:
        scanner = RecoveringFileScanner(fp)
        verified = list(scanner)

    assert [type(b) for b in verified] == [
        SectionHeader,
        InterfaceDescription,
        SimplePacket,
    ]
    assert scanner.untrusted_range is None
    assert scanner.untrusted_reason is None


def test_truncated_tail_half_block():
    with open("test_data/test010.ntar", "rb") as fp:
        data = fp.read()

    # Drop the tail end of the last (simple packet) block: the archive
    # tail stops in the middle of a block.
    truncated = data[:-20]

    verified, untrusted_range, reason = scan(truncated)

    assert [type(b) for b in verified] == [SectionHeader, InterfaceDescription]
    assert untrusted_range == (96, len(truncated))
    assert "truncated" in reason


def test_truncated_block_header():
    with open("test_data/test010.ntar", "rb") as fp:
        data = fp.read()

    # Only a few bytes of the next block header made it to disk
    truncated = data[:236] + b"\x03\x00\x00"

    verified, untrusted_range, reason = scan(truncated)

    assert len(verified) == 3
    assert untrusted_range == (236, len(truncated))
    assert "truncated block header" in reason


def test_damaged_test006_truncated_block_content():
    # test006.ntar declares a packet block length of 167, one byte
    # short of the actual (fixed) content: the boundary lengths agree
    # with each other, but the block content cannot be decoded, so
    # the block and everything after it is untrusted.
    with open("test_data/test006.ntar", "rb") as fp:
        scanner = RecoveringFileScanner(fp)
        verified = list(scanner)

    assert [type(b) for b in verified] == [SectionHeader, InterfaceDescription]
    assert scanner.untrusted_range == (96, 264)
    assert "failed to decode" in scanner.untrusted_reason


def test_mismatching_lengths_not_trusted_on_either_end():
    # The trailing length disagrees with the leading one: we must not
    # trust either end to skip ahead, so the scan stops here even
    # though a perfectly valid section follows.
    shb = make_section_header()
    idb = make_interface_description()
    bad = make_unknown_block(b"payload", trailing_length=0xDEAD)
    good = make_section_header() + make_interface_description()

    verified, untrusted_range, reason = scan(shb + idb + bad + good)

    assert [type(b) for b in verified] == [SectionHeader, InterfaceDescription]
    assert untrusted_range == (len(shb) + len(idb), len(shb) + len(idb) + len(bad) + len(good))
    assert "mismatching block lengths" in reason


def test_no_resync_on_section_magic_inside_block_body():
    # The section-header magic appears inside a damaged block body:
    # it must not be used to resynchronize, as it may be packet data.
    shb = make_section_header()
    idb = make_interface_description()
    bad = make_unknown_block(
        b"\x0a\x0d\x0d\x0a" + bytes(16), trailing_length=0xDEAD
    )
    good = make_section_header() + make_interface_description()

    verified, untrusted_range, reason = scan(shb + idb + bad + good)

    assert len(verified) == 2
    assert untrusted_range[0] == len(shb) + len(idb)
    assert untrusted_range[1] == len(shb) + len(idb) + len(bad) + len(good)


def test_not_starting_with_section_header():
    verified, untrusted_range, reason = scan(make_interface_description())

    assert verified == []
    assert untrusted_range == (0, 20)
    assert "section header" in reason


def test_empty_stream():
    verified, untrusted_range, reason = scan(b"")

    assert verified == []
    assert untrusted_range is None
    assert reason is None


def test_multiple_sections():
    data = (
        make_section_header()
        + make_interface_description()
        + make_section_header()
        + make_interface_description()
    )

    verified, untrusted_range, reason = scan(data)

    assert [type(b) for b in verified] == [
        SectionHeader,
        InterfaceDescription,
        SectionHeader,
        InterfaceDescription,
    ]
    assert untrusted_range is None
