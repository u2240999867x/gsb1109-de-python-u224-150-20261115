import io

import pytest

import pcapng.blocks as blocks
from pcapng import FileScanner, FileWriter
from pcapng.strictness import Strictness, set_strictness
from pcapng.structs import write_bytes_padded, write_int
from pcapng.truncate import truncate_stream


def _write_raw_unknown_block(stream, block_type, data, endianness="<"):
    total_length = 12 + len(data) + (-len(data) % 4)
    write_int(block_type, stream, 32, endianness=endianness)
    write_int(total_length, stream, 32, endianness=endianness)
    write_bytes_padded(stream, data)
    write_int(total_length, stream, 32, endianness=endianness)


def _build_input(endianness="<"):
    """A section with two interfaces, packets only on the first one"""
    shb = blocks.SectionHeader(
        endianness=endianness,
        options={"shb_userappl": "test-truncate"},
    )
    shb.new_member(blocks.InterfaceDescription, link_type=1, snaplen=65535)
    shb.new_member(blocks.InterfaceDescription, link_type=1, snaplen=0)

    stream = io.BytesIO()
    writer = FileWriter(stream, shb)

    for num, packet_len in enumerate((200, 100, 50)):
        writer.write_block(
            shb.new_member(
                blocks.EnhancedPacket,
                interface_id=0,
                timestamp_high=0,
                timestamp_low=num,
                packet_len=packet_len,
                packet_data=bytes(bytearray([0xAA] * packet_len)),
                options={"epb_hash": [(2, b"fancyhash")]},
            )
        )

    _write_raw_unknown_block(stream, 0x00000BAD, b"custom-data!", endianness)

    writer.write_block(
        shb.new_member(
            blocks.InterfaceStatistics,
            interface_id=0,
            timestamp_high=0,
            timestamp_low=100,
            options={
                "isb_starttime": 10,
                "isb_endtime": 90,
                "isb_ifrecv": 1000,
                "isb_ifdrop": 12,
            },
        )
    )
    # Interface 1 has no packets, but still declares capture coverage
    writer.write_block(
        shb.new_member(
            blocks.InterfaceStatistics,
            interface_id=1,
            timestamp_high=0,
            timestamp_low=100,
            options={
                "isb_starttime": 10,
                "isb_endtime": 90,
                "isb_ifrecv": 5,
                "isb_ifdrop": 5,
            },
        )
    )

    stream.seek(0)
    return stream


@pytest.mark.parametrize("endianness", ["<", ">"])
def test_truncate_enhanced_packets(endianness):
    outstream = io.BytesIO()
    truncate_stream(_build_input(endianness), outstream, 64)

    outstream.seek(0)
    in_blocks = list(FileScanner(outstream))

    shb = in_blocks[0]
    assert shb.options["shb_userappl"] == "test-truncate"

    idb0, idb1 = in_blocks[1], in_blocks[2]
    assert idb0.snaplen == 64  # min(65535, 64)
    assert idb1.snaplen == 64  # was unlimited, now the truncation limit

    packets = [b for b in in_blocks if isinstance(b, blocks.EnhancedPacket)]
    assert len(packets) == 3
    # Captured length / block length are rewritten, wire length is kept
    assert [p.captured_len for p in packets] == [64, 64, 50]
    assert [p.packet_len for p in packets] == [200, 100, 50]
    assert packets[0].packet_data == b"\xAA" * 64
    # Hashes of the original full packet must not survive truncation
    assert "epb_hash" not in packets[0].options


@pytest.mark.parametrize("endianness", ["<", ">"])
def test_truncate_interface_statistics(endianness):
    outstream = io.BytesIO()
    truncate_stream(_build_input(endianness), outstream, 64)

    outstream.seek(0)
    in_blocks = list(FileScanner(outstream))
    stats = [b for b in in_blocks if isinstance(b, blocks.InterfaceStatistics)]
    assert len(stats) == 2

    isb0, isb1 = stats
    # Coverage declarations are preserved
    assert isb0.options["isb_starttime"] == 10
    assert isb0.options["isb_endtime"] == 90
    # Sub-file actual statistics
    assert isb0.options["isb_usrdeliv"] == 3
    # Original full-capture counters are not passed off as sub-file results
    assert "isb_ifrecv" not in isb0.options
    assert "isb_ifdrop" not in isb0.options
    comments = isb0.options.get_all("opt_comment")
    assert any("isb_ifrecv=1000" in c and "isb_ifdrop=12" in c for c in comments)

    # Interface without packets still has its coverage declaration
    assert isb1.options["isb_usrdeliv"] == 0
    assert isb1.options["isb_starttime"] == 10
    assert "isb_ifrecv" not in isb1.options
    comments = isb1.options.get_all("opt_comment")
    assert any("isb_ifrecv=5" in c and "isb_ifdrop=5" in c for c in comments)


def test_truncate_preserves_unknown_blocks():
    outstream = io.BytesIO()
    truncate_stream(_build_input(), outstream, 64)

    outstream.seek(0)
    unknown = [
        b for b in FileScanner(outstream) if isinstance(b, blocks.UnknownBlock)
    ]
    assert len(unknown) == 1
    assert unknown[0].block_type == 0x00000BAD
    assert unknown[0].data == b"custom-data!"


def test_truncate_simple_packets():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription, link_type=1, snaplen=0)

    stream = io.BytesIO()
    writer = FileWriter(stream, shb)
    writer.write_block(
        shb.new_member(
            blocks.SimplePacket,
            packet_len=100,
            packet_data=bytes(bytearray(range(100))),
        )
    )

    stream.seek(0)
    outstream = io.BytesIO()
    truncate_stream(stream, outstream, 40)

    outstream.seek(0)
    _, idb, spb = list(FileScanner(outstream))
    assert idb.snaplen == 40
    assert spb.captured_len == 40
    assert spb.packet_len == 100  # original wire length preserved
    assert spb.packet_data == bytes(bytearray(range(40)))


def test_truncate_obsolete_packet_becomes_enhanced():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription, link_type=1, snaplen=0)

    stream = io.BytesIO()
    writer = FileWriter(stream, shb)
    set_strictness(Strictness.WARN)
    try:
        with pytest.warns(Warning, match="obsolete"):
            writer.write_block(
                shb.new_member(
                    blocks.ObsoletePacket,
                    interface_id=0,
                    drops_count=7,
                    packet_len=80,
                    packet_data=bytes(bytearray([0xBB] * 80)),
                )
            )
    finally:
        set_strictness(Strictness.FORBID)

    stream.seek(0)
    outstream = io.BytesIO()
    truncate_stream(stream, outstream, 16)

    outstream.seek(0)
    in_blocks = list(FileScanner(outstream))
    packets = [b for b in in_blocks if isinstance(b, blocks.EnhancedPacket)]
    assert len(packets) == 1
    assert packets[0].captured_len == 16
    assert packets[0].packet_len == 80
    assert packets[0].options["epb_dropcount"] == 7


def test_truncate_reports_per_section_stats():
    outstream = io.BytesIO()
    (delivered,) = truncate_stream(_build_input(), outstream, 64)
    assert delivered == {0: 3}


def test_truncate_invalid_snaplen():
    with pytest.raises(ValueError):
        truncate_stream(io.BytesIO(), io.BytesIO(), 0)
