import pytest

from pcapng.field_extract import (
    FieldExtractionError,
    FieldMapping,
    FieldSpec,
    Fragment,
    LayerRange,
    extract_field_fragments,
    field_at_offset,
    reassemble,
    verify_extraction,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_packet(ip_ihl_words=5, tcp_data_offset_words=5):
    """Build a minimal Ethernet/IPv4/TCP packet with adjustable headers."""
    ip_header_len = ip_ihl_words * 4
    tcp_header_len = tcp_data_offset_words * 4

    ethernet = bytearray(14)
    ethernet[12:14] = b"\x08\x00"

    ip_header = bytearray(ip_header_len)
    ip_header[0] = (4 << 4) | ip_ihl_words  # version + IHL
    # total length covers the whole IP packet
    total_length = ip_header_len + tcp_header_len + 4  # 4 payload bytes
    ip_header[2:4] = total_length.to_bytes(2, "big")
    ip_header[12:16] = b"\x0a\x00\x00\x01"  # src
    ip_header[16:20] = b"\x0a\x00\x00\x02"  # dst
    if ip_ihl_words > 5:
        # Private option in the extra header bytes, still inside the
        # verified IP layer range.
        ip_header[20] = 0x1F

    tcp_header = bytearray(tcp_header_len)
    tcp_header[0:2] = b"\x12\x34"  # src port
    tcp_header[2:4] = b"\x00\x50"  # dst port
    if tcp_data_offset_words > 5:
        tcp_header[20] = 0x2A  # private TCP option byte

    payload = b"\xde\xad\xbe\xef"
    packet = bytes(ethernet) + bytes(ip_header) + bytes(tcp_header) + payload

    layers = {
        "eth": LayerRange(0, 14),
        "ip": LayerRange(14, 14 + ip_header_len),
        "tcp": LayerRange(
            14 + ip_header_len, 14 + ip_header_len + tcp_header_len
        ),
    }
    return packet, layers


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------


def test_single_field_returns_exact_bytes_and_mapping():
    packet, layers = make_packet()
    result = extract_field_fragments(
        packet, layers, [FieldSpec("ethertype", "eth", 12, 14)]
    )
    assert result.fragments == [Fragment(b"\x08\x00", 12, 14)]
    assert result.field_mappings == [
        FieldMapping("ethertype", "eth", 12, 14)
    ]
    assert result.layer_ranges == layers


def test_fields_with_gap_produce_two_fragments_without_payload():
    packet, layers = make_packet()
    result = extract_field_fragments(
        packet,
        layers,
        [
            FieldSpec("ethertype", "eth", 12, 14),
            FieldSpec("ip_src", "ip", 26, 30),
        ],
    )
    # Bytes 14..25 (unrequested IP header) are not delivered.
    assert result.fragments == [
        Fragment(packet[12:14], 12, 14),
        Fragment(packet[26:30], 26, 30),
    ]
    assert len(result.field_mappings) == 2


def test_adjacent_fields_are_merged_into_one_fragment():
    packet, layers = make_packet()
    result = extract_field_fragments(
        packet,
        layers,
        [
            FieldSpec("ip_src", "ip", 26, 30),
            FieldSpec("ip_dst", "ip", 30, 34),
        ],
    )
    assert result.fragments == [Fragment(packet[26:34], 26, 34)]


def test_overlapping_fields_deliver_shared_bytes_once_keep_all_mappings():
    packet, layers = make_packet()
    result = extract_field_fragments(
        packet,
        layers,
        [
            FieldSpec("ip_src_part", "ip", 26, 30),
            FieldSpec("ip_src_dst", "ip", 28, 34),
        ],
    )
    assert result.fragments == [Fragment(packet[26:34], 26, 34)]
    assert result.field_mappings == [
        FieldMapping("ip_src_part", "ip", 26, 30),
        FieldMapping("ip_src_dst", "ip", 28, 34),
    ]
    delivered = set()
    for fragment in result.fragments:
        delivered.update(
            range(fragment.original_start, fragment.original_end)
        )
    assert delivered == set(range(26, 34))  # every byte exactly once


def test_empty_field_list_returns_no_fragments():
    packet, layers = make_packet()
    result = extract_field_fragments(packet, layers, [])
    assert result.fragments == []
    assert result.field_mappings == []


def test_accepts_tuple_and_dict_specs_and_ranges():
    packet, layers = make_packet()
    result = extract_field_fragments(
        packet,
        {"eth": (0, 14), "ip": {"start": 14, "end": 34}},
        [
            ("ethertype", "eth", 12, 14),
            {"name": "ip_src", "layer": "ip", "start": 26, "end": 30},
        ],
    )
    assert [f.data for f in result.fragments] == [
        packet[12:14],
        packet[26:30],
    ]
    assert result.layer_ranges["eth"] == LayerRange(0, 14)
    assert result.layer_ranges["ip"] == LayerRange(14, 34)


def test_accepts_bytes_like_input():
    packet, layers = make_packet()
    result = extract_field_fragments(
        bytearray(packet), layers, [FieldSpec("ethertype", "eth", 12, 14)]
    )
    assert result.fragments[0].data == b"\x08\x00"
    assert isinstance(result.fragments[0].data, bytes)


# ---------------------------------------------------------------------------
# Variable-length headers and private options
# ---------------------------------------------------------------------------


def test_variable_length_ip_header_uses_actual_layer_range():
    # Two packets, different IHL -> TCP layer starts at a different offset.
    packet_short, layers_short = make_packet(ip_ihl_words=5)
    packet_long, layers_long = make_packet(ip_ihl_words=6)

    tcp_fields = [
        field_at_offset("tcp_src_port", "tcp", layers_short, 0, 2),
        field_at_offset("tcp_dst_port", "tcp", layers_short, 2, 2),
    ]
    result_short = extract_field_fragments(packet_short, layers_short, tcp_fields)
    assert result_short.fragments == [
        Fragment(packet_short[34:38], 34, 38)
    ]

    tcp_fields_long = [
        field_at_offset("tcp_src_port", "tcp", layers_long, 0, 2),
        field_at_offset("tcp_dst_port", "tcp", layers_long, 2, 2),
    ]
    result_long = extract_field_fragments(packet_long, layers_long, tcp_fields_long)
    # 14 (eth) + 24 (IP with options) = 38
    assert result_long.fragments == [Fragment(packet_long[38:42], 38, 42)]


def test_private_option_inside_verified_layer_is_collectable():
    packet, layers = make_packet(ip_ihl_words=6)
    # Private option byte lives at IP offset 20 -> absolute 34.
    result = extract_field_fragments(
        packet, layers, [FieldSpec("ip_private_opt", "ip", 34, 35)]
    )
    assert result.fragments == [Fragment(b"\x1f", 34, 35)]


def test_private_tcp_option_inside_verified_tcp_range():
    packet, layers = make_packet(tcp_data_offset_words=6)
    tcp_start = layers["tcp"].start
    result = extract_field_fragments(
        packet,
        layers,
        [field_at_offset("tcp_private_opt", "tcp", layers, 20, 1)],
    )
    assert result.fragments == [
        Fragment(b"\x2a", tcp_start + 20, tcp_start + 21)
    ]


def test_field_in_unparsed_payload_is_rejected():
    packet, layers = make_packet()
    payload_start = layers["tcp"].end
    with pytest.raises(FieldExtractionError):
        extract_field_fragments(
            packet,
            layers,
            [FieldSpec("payload_guess", "tcp", payload_start, payload_start + 1)],
        )


def test_field_crossing_layer_boundary_is_rejected():
    packet, layers = make_packet()
    with pytest.raises(FieldExtractionError):
        # Last IP byte + first TCP byte: not fully inside either layer.
        extract_field_fragments(
            packet, layers, [FieldSpec("cross", "ip", 32, 35)]
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_layer_is_rejected():
    packet, layers = make_packet()
    with pytest.raises(FieldExtractionError):
        extract_field_fragments(
            packet, layers, [FieldSpec("x", "sctp", 0, 1)]
        )


def test_layer_range_outside_packet_is_rejected():
    packet, layers = make_packet()
    bad_layers = dict(layers)
    bad_layers["ip"] = LayerRange(14, len(packet) + 10)
    with pytest.raises(FieldExtractionError):
        extract_field_fragments(
            packet, bad_layers, [FieldSpec("ip_src", "ip", 26, 30)]
        )


def test_malformed_range_is_rejected():
    packet, layers = make_packet()
    with pytest.raises(FieldExtractionError):
        extract_field_fragments(
            packet, layers, [FieldSpec("bad", "eth", 10, 10)]
        )
    with pytest.raises(FieldExtractionError):
        extract_field_fragments(
            packet, layers, [FieldSpec("bad", "eth", -1, 2)]
        )


def test_field_at_offset_unknown_layer():
    packet, layers = make_packet()
    with pytest.raises(FieldExtractionError):
        field_at_offset("x", "udp", layers, 0, 1)


# ---------------------------------------------------------------------------
# Reassembly and verification
# ---------------------------------------------------------------------------


def test_reassemble_rebuilds_each_field_from_fragments():
    packet, layers = make_packet()
    specs = [
        FieldSpec("ethertype", "eth", 12, 14),
        FieldSpec("ip_src", "ip", 26, 30),
        FieldSpec("ip_dst", "ip", 30, 34),
        FieldSpec("tcp_ports", "tcp", 34, 38),
    ]
    result = extract_field_fragments(packet, layers, specs)
    for spec in specs:
        assert reassemble(result, spec.start, spec.end) == packet[
            spec.start:spec.end
        ]


def test_reassemble_refuses_range_with_gap():
    packet, layers = make_packet()
    result = extract_field_fragments(
        packet, layers, [FieldSpec("ethertype", "eth", 12, 14)]
    )
    with pytest.raises(FieldExtractionError):
        reassemble(result, 12, 16)  # bytes 14-15 were not delivered


def test_verify_extraction_round_trip_on_complex_packet():
    packet, layers = make_packet(ip_ihl_words=6, tcp_data_offset_words=6)
    specs = [
        FieldSpec("ethertype", "eth", 12, 14),
        field_at_offset("ip_src", "ip", layers, 12, 4),
        field_at_offset("ip_private_opt", "ip", layers, 20, 1),
        field_at_offset("tcp_src_port", "tcp", layers, 0, 2),
        field_at_offset("tcp_private_opt", "tcp", layers, 20, 1),
        # Overlapping field referencing bytes already collected.
        FieldSpec("ip_src_overlap", "ip", 26, 28),
    ]
    result = extract_field_fragments(packet, layers, specs)
    assert verify_extraction(packet, result) is True
    # Total delivered bytes must be smaller than the verified layers.
    delivered = sum(
        fragment.original_end - fragment.original_start
        for fragment in result.fragments
    )
    verified_total = sum(
        layer.end - layer.start for layer in layers.values()
    )
    assert delivered < verified_total


def test_verify_detects_tampered_fragment():
    packet, layers = make_packet()
    result = extract_field_fragments(
        packet, layers, [FieldSpec("ethertype", "eth", 12, 14)]
    )
    tampered = result._replace(
        fragments=[Fragment(b"\x00\x00", 12, 14)]
    )
    with pytest.raises(FieldExtractionError):
        verify_extraction(packet, tampered)
