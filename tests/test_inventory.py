import io

from pcapng.blocks import InterfaceDescription, SectionHeader
from pcapng.inventory import (
    Asset,
    AssetConflict,
    InterfaceInventory,
    build_inventory,
    collect_interface_records,
    scan_inventory,
)
from pcapng.writer import FileWriter


def make_blocks(*sections):
    """Build scan-ordered block lists; each section is a list of option dicts."""
    blocks = []
    for interfaces in sections:
        shb = SectionHeader()
        blocks.append(shb)
        for options in interfaces:
            idb = shb.new_member(
                InterfaceDescription, link_type=1, snaplen=0xFFFF, options=options
            )
            blocks.append(idb)
    return blocks


def test_same_mac_across_sections_merges_into_single_asset():
    blocks = make_blocks(
        [{"if_name": "eth0", "if_MACaddr": "00:11:22:33:44:55"}],
        [{"if_name": "eth0", "if_MACaddr": "00:11:22:33:44:55"}],
    )

    inventory = build_inventory(blocks)

    assert isinstance(inventory, InterfaceInventory)
    assert inventory.conflicts == []
    assert len(inventory.assets) == 1
    asset = inventory.assets[0]
    assert isinstance(asset, Asset)
    assert asset.mac == "00:11:22:33:44:55"
    assert asset.aliases == ["eth0"]
    assert [r.section_index for r in asset.records] == [0, 1]


def test_name_only_interfaces_merge_via_manual_mapping():
    blocks = make_blocks(
        [{"if_name": "eth0"}],
        [{"if_name": "ens3"}, {"if_name": "wlan0"}],
    )

    # Names alone never merge anything
    inventory = build_inventory(blocks)
    assert len(inventory.assets) == 3

    inventory = build_inventory(blocks, name_map={"eth0": "host-a", "ens3": "host-a"})
    assert inventory.conflicts == []
    assert len(inventory.assets) == 2
    merged = [a for a in inventory.assets if len(a.records) == 2][0]
    assert merged.aliases == ["ens3", "eth0"]
    assert merged.mac is None


def test_same_name_different_mac_not_merged():
    # Device kept its old name after a card swap: the name is only an
    # alias, so identical names with different MACs stay separate.
    blocks = make_blocks(
        [{"if_name": "eth0", "if_MACaddr": "00:11:22:33:44:55"}],
        [{"if_name": "eth0", "if_MACaddr": "66:77:88:99:aa:bb"}],
    )

    inventory = build_inventory(blocks)

    assert inventory.conflicts == []
    assert len(inventory.assets) == 2
    assert {a.mac for a in inventory.assets} == {
        "00:11:22:33:44:55",
        "66:77:88:99:aa:bb",
    }


def test_conflicting_macs_block_manual_mapping_merge():
    blocks = make_blocks(
        [{"if_name": "eth0", "if_MACaddr": "00:11:22:33:44:55"}],
        [{"if_name": "eth0-old", "if_MACaddr": "66:77:88:99:aa:bb"}],
    )

    inventory = build_inventory(
        blocks, name_map={"eth0": "router-1", "eth0-old": "router-1"}
    )

    # Merge blocked: the two interfaces stay in separate assets
    assert len(inventory.assets) == 2

    assert len(inventory.conflicts) == 1
    conflict = inventory.conflicts[0]
    assert isinstance(conflict, AssetConflict)
    assert conflict.identifier == "if_MACaddr"
    assert set(conflict.values) == {"00:11:22:33:44:55", "66:77:88:99:aa:bb"}
    assert conflict.reason == "manual_mapping"
    assert conflict.reason_detail == "router-1"

    # The conflict lists the original description blocks that caused it
    idbs = [b for b in blocks if isinstance(b, InterfaceDescription)]
    assert sorted(map(id, conflict.blocks)) == sorted(map(id, idbs))
    assert {r.name for r in conflict.records} == {"eth0", "eth0-old"}


def test_conflicting_eui_blocks_merge_on_shared_mac():
    blocks = make_blocks(
        [
            {
                "if_name": "eth0",
                "if_MACaddr": "00:11:22:33:44:55",
                "if_EUIaddr": "00:11:22:33:44:55:66:77",
            }
        ],
        [
            {
                "if_name": "eth0",
                "if_MACaddr": "00:11:22:33:44:55",
                "if_EUIaddr": "88:99:aa:bb:cc:dd:ee:ff",
            }
        ],
    )

    inventory = build_inventory(blocks)

    assert len(inventory.assets) == 2
    assert len(inventory.conflicts) == 1
    conflict = inventory.conflicts[0]
    assert conflict.identifier == "if_EUIaddr"
    assert conflict.reason == "shared_mac"
    assert conflict.reason_detail == "00:11:22:33:44:55"
    assert len(conflict.blocks) == 2


def test_transitive_merge_combines_aliases_and_identifiers():
    blocks = make_blocks(
        [{"if_name": "eth0", "if_MACaddr": "00:11:22:33:44:55"}],
        [{"if_name": "ens3"}],  # name-only, mapped manually
        [{"if_name": "ens3", "if_MACaddr": "00:11:22:33:44:55"}],
    )

    inventory = build_inventory(blocks, name_map={"ens3": "host-a"})

    assert inventory.conflicts == []
    assert len(inventory.assets) == 1
    asset = inventory.assets[0]
    assert asset.mac == "00:11:22:33:44:55"
    assert asset.aliases == ["ens3", "eth0"]
    assert len(asset.blocks) == 3


def test_collect_interface_records_tracks_sections():
    blocks = make_blocks([{"if_name": "a"}, {"if_name": "b"}], [{"if_name": "c"}])

    records = collect_interface_records(blocks)

    assert [(r.section_index, r.interface_id, r.name) for r in records] == [
        (0, 0, "a"),
        (0, 1, "b"),
        (1, 0, "c"),
    ]


def test_scan_inventory_roundtrip_through_file():
    stream = io.BytesIO()
    shb1 = SectionHeader()
    shb1.new_member(
        InterfaceDescription,
        link_type=1,
        snaplen=0xFFFF,
        options={"if_name": "eth0", "if_MACaddr": "00:11:22:33:44:55"},
    )
    writer = FileWriter(stream, shb1)
    shb2 = SectionHeader()
    shb2.new_member(
        InterfaceDescription,
        link_type=1,
        snaplen=0xFFFF,
        options={"if_name": "eth0", "if_MACaddr": "00:11:22:33:44:55"},
    )
    shb2.new_member(
        InterfaceDescription,
        link_type=1,
        snaplen=0xFFFF,
        options={"if_name": "lo"},
    )
    writer.write_block(shb2)

    stream.seek(0)
    inventory = scan_inventory(stream)

    assert inventory.conflicts == []
    assert len(inventory.assets) == 2
    by_alias = {tuple(a.aliases): a for a in inventory.assets}
    assert len(by_alias[("eth0",)].records) == 2
    assert by_alias[("eth0",)].mac == "00:11:22:33:44:55"
    assert by_alias[("lo",)].mac is None
