import io

import pytest

import pcapng.blocks as blocks
from pcapng import FileScanner, FileWriter, export_subset
from pcapng.utils import pack_timestamp_resolution


def make_epb(shb, interface_id, ts_units, payload, options=None):
    "Build an EnhancedPacket with the given raw timestamp (in ts units)."
    return shb.new_member(
        blocks.EnhancedPacket,
        interface_id=interface_id,
        timestamp_high=(ts_units >> 32) & 0xFFFFFFFF,
        timestamp_low=ts_units & 0xFFFFFFFF,
        packet_data=payload,
        options=options or {},
    )


def build_source(endianness="<"):
    """
    Build a source capture with two interfaces (nanosecond resolution on
    the second one) and four packets at 0.5s, 1.5s, 2.5s and 3.5s.
    """
    shb = blocks.SectionHeader(
        endianness=endianness,
        options={
            "shb_hardware": "pytest",
            "shb_os": "python",
            "shb_userappl": "python-pcapng",
        },
    )
    shb.new_member(
        blocks.InterfaceDescription,
        link_type=147,
        snaplen=65535,
        options={"if_name": "unused-interface"},
    )
    shb.new_member(
        blocks.InterfaceDescription,
        link_type=1,
        snaplen=65535,
        options={
            "if_name": "used-interface",
            "if_description": "the interface packets were captured on",
            "if_tsresol": pack_timestamp_resolution(10, 9),
        },
    )

    stream = io.BytesIO()
    writer = FileWriter(stream, shb)

    packets = [
        make_epb(shb, 1, 500_000_000, b"PAYLOAD-DROP-early"),
        make_epb(shb, 1, 1_500_000_000, b"PAYLOAD-KEEP-one",
                 options={"opt_comment": "first kept"}),
        make_epb(shb, 1, 2_500_000_000, b"PAYLOAD-KEEP-two"),
        make_epb(shb, 0, 3_500_000_000, b"PAYLOAD-DROP-late"),
    ]
    for packet in packets:
        writer.write_block(packet)

    stream.seek(0)
    return stream


@pytest.mark.parametrize("endianness", ["<", ">"])
def test_export_time_range(endianness):
    source = build_source(endianness=endianness)
    output = io.BytesIO()

    exported = export_subset(source, output, start=1.0, end=3.0)

    assert exported == 2

    output.seek(0)
    out_blocks = list(FileScanner(output))
    out_shb = out_blocks[0]
    out_idb = out_blocks[1]
    out_packets = out_blocks[2:]

    # Only one section and one (actually used) interface were written
    assert isinstance(out_shb, blocks.SectionHeader)
    assert isinstance(out_idb, blocks.InterfaceDescription)
    assert len(out_packets) == 2
    assert all(isinstance(blk, blocks.EnhancedPacket) for blk in out_packets)

    # The section stays self-explanatory
    assert out_shb.options["shb_hardware"] == "pytest"
    assert out_shb.options["shb_os"] == "python"
    assert out_shb.options["shb_userappl"] == "python-pcapng"

    # The used interface is described (and remapped to id 0)
    assert out_idb.options["if_name"] == "used-interface"
    assert out_idb.options["if_description"] == (
        "the interface packets were captured on"
    )
    assert out_idb.link_type == 1
    assert out_idb.snaplen == 65535
    assert [blk.interface_id for blk in out_packets] == [0, 0]

    # Timestamp resolution is preserved, and timestamps are bit-exact
    assert out_idb.options["if_tsresol"] == pack_timestamp_resolution(10, 9)
    assert out_idb.timestamp_resolution == 1e-9
    assert [blk.timestamp_low for blk in out_packets] == [
        1_500_000_000,
        2_500_000_000,
    ]
    assert [blk.timestamp for blk in out_packets] == [1.5, 2.5]

    # Packet options are carried over
    assert out_packets[0].options["opt_comment"] == "first kept"

    # Payloads of unselected packets must not leak into the output
    output_bytes = output.getvalue()
    assert b"PAYLOAD-KEEP-one" in output_bytes
    assert b"PAYLOAD-KEEP-two" in output_bytes
    assert b"PAYLOAD-DROP-early" not in output_bytes
    assert b"PAYLOAD-DROP-late" not in output_bytes
    assert b"unused-interface" not in output_bytes


def test_export_defaults_to_all_packets():
    source = build_source()
    output = io.BytesIO()

    exported = export_subset(source, output)

    assert exported == 4
    output.seek(0)
    out_blocks = list(FileScanner(output))
    # Both interfaces are used, so both are described
    out_idbs = [b for b in out_blocks if isinstance(b, blocks.InterfaceDescription)]
    out_packets = [b for b in out_blocks if isinstance(b, blocks.EnhancedPacket)]
    assert len(out_idbs) == 2
    assert len(out_packets) == 4
    # Interface ids are remapped to the new (dense) numbering
    assert sorted(blk.interface_id for blk in out_packets) == [0, 0, 0, 1]


def build_source_with_simple_packet():
    shb = blocks.SectionHeader()
    shb.new_member(
        blocks.InterfaceDescription,
        link_type=1,
        snaplen=65535,
        options={"if_name": "simple-iface"},
    )
    stream = io.BytesIO()
    writer = FileWriter(stream, shb)
    writer.write_block(
        shb.new_member(blocks.SimplePacket, packet_data=b"SIMPLE-PACKET-DATA")
    )
    stream.seek(0)
    return stream


def test_export_simple_packet_without_fabricated_timestamp():
    source = build_source_with_simple_packet()
    output = io.BytesIO()

    exported = export_subset(source, output)

    assert exported == 1
    output.seek(0)
    out_blocks = list(FileScanner(output))
    assert len(out_blocks) == 3

    out_packet = out_blocks[2]
    # Still a simple packet: no timestamp was invented for it
    assert isinstance(out_packet, blocks.SimplePacket)
    assert not isinstance(out_packet, blocks.EnhancedPacket)
    assert not hasattr(out_packet, "timestamp")
    assert out_packet.packet_data == b"SIMPLE-PACKET-DATA"


def test_simple_packet_not_matched_by_time_bounds():
    source = build_source_with_simple_packet()
    output = io.BytesIO()

    exported = export_subset(source, output, start=1.0, end=2.0)

    assert exported == 0
    assert output.getvalue() == b""


def test_simple_packet_selected_by_predicate():
    source = build_source_with_simple_packet()
    output = io.BytesIO()

    exported = export_subset(
        source,
        output,
        start=1.0,
        end=2.0,
        predicate=lambda blk: isinstance(blk, blocks.SimplePacket),
    )

    assert exported == 1
    output.seek(0)
    out_blocks = list(FileScanner(output))
    assert isinstance(out_blocks[2], blocks.SimplePacket)


def build_two_section_source():
    stream = io.BytesIO()

    shb1 = blocks.SectionHeader(options={"shb_userappl": "section-one"})
    shb1.new_member(blocks.InterfaceDescription, link_type=1, snaplen=65535)
    writer = FileWriter(stream, shb1)
    writer.write_block(make_epb(shb1, 0, 1_000_000, b"SECTION-ONE-PACKET"))

    shb2 = blocks.SectionHeader(options={"shb_userappl": "section-two"})
    shb2.new_member(blocks.InterfaceDescription, link_type=1, snaplen=65535)
    writer.write_block(shb2)
    writer.write_block(make_epb(shb2, 0, 2_000_000, b"SECTION-TWO-PACKET"))

    stream.seek(0)
    return stream


def test_only_sections_with_selected_packets_are_exported():
    source = build_two_section_source()
    output = io.BytesIO()

    exported = export_subset(source, output, start=1.5, end=2.5)

    assert exported == 1
    output.seek(0)
    out_blocks = list(FileScanner(output))
    assert len([b for b in out_blocks if isinstance(b, blocks.SectionHeader)]) == 1
    assert out_blocks[0].options["shb_userappl"] == "section-two"
    assert b"SECTION-ONE-PACKET" not in output.getvalue()
    assert b"SECTION-TWO-PACKET" in output.getvalue()


def test_multiple_sections_exported_when_both_used():
    source = build_two_section_source()
    output = io.BytesIO()

    exported = export_subset(source, output)

    assert exported == 2
    output.seek(0)
    out_blocks = list(FileScanner(output))
    assert len([b for b in out_blocks if isinstance(b, blocks.SectionHeader)]) == 2
