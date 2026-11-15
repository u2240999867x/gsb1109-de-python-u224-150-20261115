"""
Tests for the address dimension table built from Name Resolution Blocks.
"""

import io

import pytest

import pcapng.blocks as blocks
from pcapng import FileScanner, FileWriter
from pcapng.names import (
    AddressBook,
    AddressTable,
    NameDeclaration,
    build_address_table,
)

ADDRESS = "192.0.2.1"


def nrb(shb, records):
    return shb.new_member(blocks.NameResolution, records=records)


def ipv4(address=ADDRESS, names=(), record_type=1):
    return {"type": record_type, "address": address, "names": list(names)}


def packet(shb, micros, data=b"x"):
    return shb.new_member(
        blocks.EnhancedPacket,
        timestamp_high=micros >> 32,
        timestamp_low=micros & 0xFFFFFFFF,
        packet_data=data,
    )


def roundtrip(*write_blocks, shb):
    out = io.BytesIO()
    writer = FileWriter(out, shb)
    for block in write_blocks:
        writer.write_block(block)
    out.seek(0)
    return list(FileScanner(out))


def test_later_nrb_is_alias_not_resolution():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    first_packet = packet(shb, 1_000_000)
    nrb(shb, [ipv4(names=["appended.example"])])

    table = shb.address_table()
    entry = table[ADDRESS]

    assert entry.names == ["appended.example"]
    assert entry.proven_names == []
    assert entry.display_aliases == ["appended.example"]
    assert table.resolve(ADDRESS) is None
    assert table.display_name(ADDRESS) == "appended.example"

    declaration = entry.declarations[0]
    assert declaration.source is shb.blocks[-1]
    assert declaration.section is shb
    assert declaration.applies_from == first_packet.block_index + 1
    assert declaration.proven is False
    assert declaration.kind == "display_alias"


def test_early_nrb_is_proven_for_later_packets():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    early = nrb(shb, [ipv4(names=["early.example"])])
    first_packet = packet(shb, 1_000_000)
    packet(shb, 2_000_000)

    table = shb.address_table()
    entry = table[ADDRESS]

    assert entry.proven_names == ["early.example"]
    assert entry.display_aliases == []
    assert table.resolve(ADDRESS) == "early.example"
    assert table.resolve(ADDRESS, at=first_packet) == "early.example"

    declaration = entry.declarations[0]
    assert declaration.source is early
    assert declaration.applies_from == early.block_index
    assert declaration.applies_from_time == first_packet.timestamp
    assert declaration.proven is True


def test_all_names_are_retained_and_resolution_is_time_aware():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    early = nrb(
        shb, [ipv4(names=["localhost", "localhost.localdomain"])]
    )
    first = packet(shb, 1_000_000)
    second = packet(shb, 2_000_000)
    later = nrb(shb, [ipv4(names=["renamed.example"])])

    table = shb.address_table()
    entry = table[ADDRESS]

    assert entry.names == [
        "localhost",
        "localhost.localdomain",
        "renamed.example",
    ]
    assert entry.proven_names == ["localhost", "localhost.localdomain"]
    assert entry.display_aliases == ["renamed.example"]

    assert table.resolve(ADDRESS, at=first) == "localhost"
    assert table.resolve(ADDRESS, at=second) == "localhost"
    assert table.resolve(ADDRESS, at=later) is None
    assert table.resolve(ADDRESS, at=later, include_aliases=True) == (
        "renamed.example"
    )
    assert table.resolve(ADDRESS, at=second.timestamp) == "localhost"

    assert entry.proven_names_at(first) == ["localhost", "localhost.localdomain"]
    assert entry.aliases_at(first) == ["renamed.example"]


def test_old_declaration_is_not_overwritten():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    old_nrb = nrb(shb, [ipv4(names=["old.example"])])
    packet(shb, 1_000_000)
    new_nrb = nrb(shb, [ipv4(names=["new.example"])])

    declarations = shb.address_table()[ADDRESS].declarations
    assert [d.source for d in declarations] == [old_nrb, new_nrb]
    assert declarations[0].applies_until == new_nrb.block_index - 1
    assert declarations[1].applies_from == new_nrb.block_index
    assert declarations[0].names == ("old.example",)
    assert declarations[1].names == ("new.example",)


def test_other_section_does_not_override():
    first_section = blocks.SectionHeader()
    first_section.new_member(blocks.InterfaceDescription)
    nrb(
        first_section,
        [
            ipv4(names=["first.example"]),
            ipv4("198.51.100.1", ["other.example"]),
        ],
    )
    packet(first_section, 1_000_000)

    second_section = blocks.SectionHeader(endianness=">")
    second_section.new_member(blocks.InterfaceDescription)
    packet(second_section, 3_000_000)
    second_nrb = nrb(second_section, [ipv4(names=["second.example"])])

    table = build_address_table([first_section, second_section])
    entry = table[ADDRESS]

    assert entry.names == ["first.example", "second.example"]
    assert [d.section for d in entry.declarations] == [
        first_section,
        second_section,
    ]
    assert [d.section_index for d in entry.declarations] == [0, 1]

    first_table = table.for_section(first_section)
    second_table = table.for_section(1)
    assert first_table[ADDRESS].names == ["first.example"]
    assert second_table[ADDRESS].names == ["second.example"]
    assert second_table[ADDRESS].proven_names == []
    assert second_table[ADDRESS].display_aliases == ["second.example"]
    assert table.resolve(ADDRESS, section=first_section) == "first.example"
    assert table.resolve(ADDRESS, section=1) is None
    assert table.resolve(ADDRESS) == "first.example"
    assert table.proven_names_at(ADDRESS, section=1) == []
    assert table.resolve(ADDRESS, section=1, include_aliases=True) == (
        "second.example"
    )
    assert table.display_name(ADDRESS) == "first.example"


def test_unknown_record_types_round_trip():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    unknown_one = {"type": 0x0009, "raw": b"\xde\xad\xbe\xef"}
    unknown_two = {"type": 0x77AA, "raw": b"\x01\x02\x03\x04\x05"}
    source = nrb(shb, [unknown_one, ipv4(names=["known.example"]), unknown_two])
    packet(shb, 1_000_000)

    table = shb.address_table()
    assert table.raw_records == (unknown_one, unknown_two)
    assert table[ADDRESS].names == ["known.example"]
    assert source.unknown_records == (
        source.records[0],
        source.records[2],
    )


def test_unknown_record_types_survive_write_read():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    unknown = {"type": 0x1234, "raw": b"\xaa\xbb\xcc\xdd"}
    source = nrb(shb, [unknown, ipv4(names=["known.example"])])

    read = roundtrip(source, shb=shb)
    read_shb = read[0]
    read_nrb = read[2]

    assert read_nrb.records[0] == unknown
    assert read_nrb.records[1]["address"] == ADDRESS
    rebuilt = read_shb.address_table().to_records()
    assert rebuilt == [
        {"type": 0x1234, "raw": b"\xaa\xbb\xcc\xdd"},
        {"type": 1, "address": ADDRESS, "names": ["known.example"]},
    ]


@pytest.mark.parametrize("endianness", ["<", ">"])
def test_round_trip_preserves_readable_records(endianness):
    shb = blocks.SectionHeader(endianness=endianness)
    shb.new_member(blocks.InterfaceDescription)
    source = nrb(
        shb,
        [
            ipv4("127.0.0.1", ["localhost", "localhost.localdomain"]),
            {
                "type": 2,
                "address": "::1",
                "names": ["localhost", "localhost.localdomain"],
            },
            {"type": 0x00A1, "raw": b"\x11\x22\x33"},
        ],
    )

    read = roundtrip(source, shb=shb)
    read_nrb = read[2]

    assert read_nrb.records[0]["address"] == "127.0.0.1"
    assert read_nrb.records[0]["names"] == ["localhost", "localhost.localdomain"]
    assert read_nrb.records[1]["address"] == "::1"
    assert dict(read_nrb.records[2]) == {"type": 0x00A1, "raw": b"\x11\x22\x33"}


def test_address_table_is_section_scoped_and_order_preserving():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    nrb(shb, [ipv4("10.0.0.1", ["a.example"]), ipv4(ADDRESS, ["b.example"])])

    table = shb.address_table()
    assert isinstance(table, AddressTable)
    assert isinstance(table, AddressBook)
    assert table.addresses == ("10.0.0.1", ADDRESS)
    assert table.as_dict() == {
        "10.0.0.1": ["a.example"],
        ADDRESS: ["b.example"],
    }
    assert all(isinstance(d, NameDeclaration) for d in table.declarations)


def test_empty_section_has_empty_table():
    shb = blocks.SectionHeader()
    table = shb.address_table()
    assert len(table) == 0
    assert table.addresses == ()
    assert table.resolve(ADDRESS) is None
    assert table.raw_records == ()


def test_scanner_exposes_section_aware_table():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    source = nrb(shb, [ipv4(names=["scanned.example"])])

    out = io.BytesIO()
    FileWriter(out, shb).write_block(source)
    out.seek(0)
    scanner = FileScanner(out)
    list(scanner)

    table = scanner.address_table()
    assert table[ADDRESS].names == ["scanned.example"]
    assert table[ADDRESS].declarations[0].section is scanner.current_section
    assert table[ADDRESS].declarations[0].section_index == 0


def test_records_keep_provenance_annotations():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    source = nrb(shb, [ipv4(names=["before.example"])])
    packet(shb, 1_000_000)

    record = source.records[0]
    assert record.source is source
    assert record.section is shb
    assert record.applies_from == source.block_index
    assert record.declaration.names == ("before.example",)
    assert record.proven is True


def test_superseded_name_is_not_a_display_alias_at_its_time():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    nrb(shb, [ipv4(names=["old.example"])])
    first_packet = packet(shb, 1_000_000)
    later = nrb(shb, [ipv4(names=["new.example"])])

    entry = shb.address_table()[ADDRESS]
    applied = entry.applicable_names_at(first_packet)
    aliases = entry.aliases_at(first_packet)

    assert applied == ["old.example"]
    assert aliases == ["new.example"]
    assert entry.names == ["old.example", "new.example"]


def test_unknown_records_preserve_raw_bytes_and_order():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    unknown = {"type": 0x00FF, "raw": b"\x00\x01not-a-name"}
    source = nrb(shb, [unknown, ipv4(names=["known.example"])])

    read = roundtrip(source, shb=shb)
    read_record = read[2].records[0]

    assert read_record["type"] == 0x00FF
    assert read_record["raw"] == b"\x00\x01not-a-name"
    assert read[0].address_table().to_records() == [
        {"type": 0x00FF, "raw": b"\x00\x01not-a-name"},
        {"type": 1, "address": ADDRESS, "names": ["known.example"]},
    ]


def test_address_table_from_records():
    table = AddressTable.from_records(
        [
            ipv4("10.0.0.1", ["a.example"]),
            {"type": 0x0042, "raw": b"\xff"},
            ipv4("10.0.0.1", ["b.example"]),
        ]
    )
    assert table["10.0.0.1"].names == ["a.example", "b.example"]
    assert table.raw_records == ({"type": 0x0042, "raw": b"\xff"},)


def test_kinds_distinguish_superseded_and_alias():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    nrb(shb, [ipv4(names=["old.example"])])
    shb.new_member(blocks.NameResolution, records=[ipv4(names=["new.example"])])

    kinds = [d.kind for d in shb.address_table().declarations]
    assert kinds == ["superseded", "display_alias"]

    after_packet = blocks.SectionHeader()
    after_packet.new_member(blocks.InterfaceDescription)
    packet(after_packet, 1_000_000)
    nrb(after_packet, [ipv4(names=["appended.example"])])

    assert after_packet.address_table().declarations[0].kind == "display_alias"


def test_every_name_is_proven_or_an_alias():
    shb = blocks.SectionHeader()
    shb.new_member(blocks.InterfaceDescription)
    nrb(shb, [ipv4(names=["old.example"])])
    packet(shb, 1_000_000)
    shb.new_member(blocks.NameResolution, records=[ipv4(names=["new.example"])])

    entry = shb.address_table()[ADDRESS]
    assert set(entry.names) == set(entry.proven_names) | set(entry.display_aliases)
    assert entry.proven_names == ["old.example"]
    assert entry.display_aliases == ["new.example"]

