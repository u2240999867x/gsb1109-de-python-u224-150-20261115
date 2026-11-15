import io

import pytest

import pcapng.blocks as blocks
from pcapng import FileScanner, FileWriter
from pcapng.correlation import (
    CandidateGroup,
    ClockAnchor,
    ClockModel,
    PacketObservation,
    correlate,
    correlate_captures,
    observations_from_blocks,
    read_observations,
)


def observation(timestamp, data, packet_len=None, index=None):
    return PacketObservation(
        timestamp=timestamp,
        packet_data=data,
        packet_len=packet_len,
        index=index,
    )


def test_clock_model_uses_anchor_slope_and_offset():
    anchors = [(100.0, 10.0), (210.0, 110.0)]
    model = ClockModel.from_anchors(anchors)

    assert model.slope == pytest.approx(1.1)
    assert model.offset == pytest.approx(89.0)
    assert model.to_first(110.0) == pytest.approx(210.0)
    assert model.to_second(210.0) == pytest.approx(110.0)
    assert model.delay(212.0, 110.0) == pytest.approx(2.0)


def test_clock_model_with_single_anchor_only_shifts():
    model = ClockModel.from_anchors([(100.0, 40.0)])

    assert model.slope == 1.0
    assert model.offset == 60.0
    assert model.to_first(50.0) == 110.0


def test_clock_model_requires_finite_positive_parameters():
    with pytest.raises(ValueError):
        ClockModel(slope=0)
    with pytest.raises(ValueError):
        ClockModel(offset=float("nan"))
    with pytest.raises(ValueError):
        ClockModel.from_anchors([])
    with pytest.raises(ValueError):
        ClockModel.from_anchors([(1.0, 2.0), (2.0, 1.0)])


def test_clock_anchor_accepts_observations_and_attributes():
    anchor = ClockAnchor(
        observation(10.0, b"a", index="first"),
        observation(4.0, b"a", index="second"),
    )

    assert anchor.first_timestamp == 10.0
    assert anchor.second_timestamp == 4.0
    assert ClockModel.from_anchors(anchor).offset == 6.0


def test_two_anchor_pairs_are_not_misread_as_one_bare_pair():
    model = ClockModel.from_anchors([(100.0, 10.0), (210.0, 110.0)])

    assert model.slope == pytest.approx(1.1)


def test_empty_anchor_list_is_rejected():
    with pytest.raises(ValueError):
        ClockModel.from_anchors([])


def test_single_correspondence_with_clock_skew():
    first = [observation(1000.0, b"payload", index="f")]
    second = [observation(900.0, b"payload", index="s")]

    result = correlate(
        first,
        second,
        anchors=[(1000.0, 900.0)],
        window=0.5,
    )

    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair.first.index == "f"
    assert pair.second.index == "s"
    assert pair.delay == pytest.approx(0.0)
    assert result.candidate_groups == ()
    assert result.unmatched_first == ()
    assert result.unmatched_second == ()


def test_window_is_applied_after_clock_adjustment():
    first = [observation(1000.5, b"payload", index="f")]
    second = [observation(900.0, b"payload", index="s")]

    result = correlate(first, second, anchors=[(1000.0, 900.0)], window=0.25)
    assert result.pairs == ()
    assert result.unmatched_first[0].index == "f"
    assert result.unmatched_second[0].index == "s"


def test_asymmetric_window_bounds_are_honored():
    first = [observation(1002.0, b"payload", index="f")]
    second = [observation(900.0, b"payload", index="s")]
    model = ClockModel(1.0, 100.0)

    result = correlate(
        first,
        second,
        clock=model,
        window=(1.5, 3.0),
    )
    assert len(result.pairs) == 1
    assert result.pairs[0].delay == pytest.approx(2.0)

    result = correlate(
        first,
        second,
        clock=model,
        window=(2.5, 3.0),
    )
    assert result.pairs == ()


def test_window_must_be_valid():
    first = [observation(1.0, b"x")]
    second = [observation(1.0, b"x")]

    with pytest.raises(ValueError):
        correlate(first, second, anchors=[(1.0, 1.0)])
    with pytest.raises(ValueError):
        correlate(first, second, anchors=[(1.0, 1.0)], window=-1)
    with pytest.raises(ValueError):
        correlate(first, second, anchors=[(1.0, 1.0)], window=(2, 1))


def test_payload_prefix_comparison_keeps_each_captured_range():
    first = [observation(10.0, b"abcdef", packet_len=60, index="f")]
    second = [observation(10.0, b"abc", packet_len=60, index="s")]

    result = correlate(
        first,
        second,
        anchors=[(10.0, 10.0)],
        window=0.1,
    )

    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair.compared_len == 3
    assert pair.first.captured_range == (0, 6)
    assert pair.second.captured_range == (0, 3)
    assert pair.first_range == (0, 6)
    assert pair.second_range == (0, 3)
    assert pair.shorter_capture == "second"
    assert pair.truncated_sides == ("first", "second")


def test_conflicting_payloads_do_not_match():
    first = [observation(10.0, b"abcdef", packet_len=6, index="f")]
    second = [observation(10.0, b"abcxef", packet_len=6, index="s")]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=0.1)

    assert result.pairs == ()
    assert len(result.unmatched_first) == 1
    assert len(result.unmatched_second) == 1


def test_wire_lengths_must_agree_when_both_are_known():
    first = [observation(10.0, b"abcdef", packet_len=60, index="f")]
    second = [observation(10.0, b"abc", packet_len=61, index="s")]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=0.1)
    assert result.pairs == ()


def test_different_link_types_do_not_match():
    first = [PacketObservation(10.0, b"abc", packet_len=3, link_type=1)]
    second = [PacketObservation(10.0, b"abc", packet_len=3, link_type=101)]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=0.1)
    assert result.pairs == ()


def test_identical_payloads_are_two_transmissions_not_merged():
    first = [
        observation(10.0, b"same", index="f0"),
        observation(11.0, b"same", index="f1"),
    ]
    second = [
        observation(10.0, b"same", index="s0"),
        observation(11.0, b"same", index="s1"),
    ]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=1.5)

    assert result.pairs == ()
    assert len(result.candidate_groups) == 1
    assert {pair.first.index for pair in result.iter_candidates()} == {"f0", "f1"}
    assert {pair.second.index for pair in result.iter_candidates()} == {"s0", "s1"}
    assert len(list(result.iter_candidates())) == 4
    assert result.unmatched_first == ()
    assert result.unmatched_second == ()


def test_equal_payloads_with_distinct_timing_are_matched_one_to_one():
    first = [
        observation(10.0, b"same", index="f0"),
        observation(11.0, b"same", index="f1"),
    ]
    second = [
        observation(10.0, b"same", index="s0"),
        observation(11.8, b"same", index="s1"),
    ]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=1.0)

    assert len(result.pairs) == 2
    assert [(p.first.index, p.second.index) for p in result.pairs] == [
        ("f0", "s0"),
        ("f1", "s1"),
    ]
    assert result.candidate_groups == ()


def test_all_multiple_solutions_are_kept_as_candidates():
    first = [
        observation(10.0, b"same", index="f0"),
        observation(10.0, b"same", index="f1"),
        observation(10.0, b"same", index="f2"),
    ]
    second = [
        observation(10.0, b"same", index="s0"),
        observation(10.0, b"same", index="s1"),
        observation(10.0, b"same", index="s2"),
    ]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=0.1)

    assert result.pairs == ()
    assert len(result.candidate_groups) == 1
    group = result.candidate_groups[0]
    assert isinstance(group, CandidateGroup)
    assert len(group.first) == 3
    assert len(group.second) == 3
    assert len(group.candidates) == 9
    assert len({candidate.first.index for candidate in group.candidates}) == 3
    assert len({candidate.second.index for candidate in group.candidates}) == 3


def test_ambiguity_is_local_and_other_pairs_still_resolve():
    first = [
        observation(10.0, b"dup", index="f0"),
        observation(10.0, b"dup", index="f1"),
        observation(20.0, b"unique", packet_len=20, index="f2"),
    ]
    second = [
        observation(10.0, b"dup", index="s0"),
        observation(20.0, b"unique", packet_len=20, index="s1"),
        observation(10.0, b"dup", index="s2"),
    ]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=0.1)

    assert len(result.pairs) == 1
    assert result.pairs[0].first.index == "f2"
    assert result.pairs[0].second.index == "s1"
    assert len(result.candidate_groups) == 1
    assert {item.index for item in result.candidate_groups[0].first} == {"f0", "f1"}
    assert {item.index for item in result.candidate_groups[0].second} == {"s0", "s2"}


def test_file_order_does_not_decide_ambiguity():
    first = [
        observation(10.0, b"same", index="f0"),
        observation(10.5, b"same", index="f1"),
    ]
    second = [
        observation(10.0, b"same", index="s0"),
        observation(10.5, b"same", index="s1"),
    ]

    forward = correlate(first, second, anchors=[(10.0, 10.0)], window=1.0)
    backward = correlate(
        list(reversed(first)),
        list(reversed(second)),
        anchors=[(10.0, 10.0)],
        window=1.0,
    )

    assert forward.pairs == ()
    assert backward.pairs == ()
    assert len(forward.candidate_groups[0].candidates) == len(
        backward.candidate_groups[0].candidates
    )


def test_candidate_groups_only_hold_viable_maximum_matching_edges():
    first = [
        observation(10.0, b"same", index="f0"),
        observation(10.0, b"same", index="f1"),
    ]
    second = [
        observation(10.0, b"same", index="s0"),
        observation(10.0, b"same", index="s1"),
        observation(10.0, b"same", index="s2"),
    ]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=0.1)

    assert result.pairs == ()
    assert len(result.candidate_groups) == 1
    group = result.candidate_groups[0]
    assert len(group.candidates) == 6
    assert len(group.first) == 2
    assert len(group.second) == 3
    assert result.unmatched_second == ()
    assert result.unmatched_first == ()


def test_two_duplicates_competing_for_one_observation_stay_ambiguous():
    first = [
        observation(10.0, b"same", index="f0"),
        observation(10.5, b"same", index="f1"),
    ]
    second = [observation(10.2, b"same", index="s0")]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=1.0)

    assert result.pairs == ()
    assert len(result.candidate_groups) == 1
    assert {
        (candidate.first.index, candidate.second.index)
        for candidate in result.candidate_groups[0]
    } == {("f0", "s0"), ("f1", "s0")}
    assert result.unmatched_first == ()


def test_duplicate_payload_does_not_merge_distinct_observations():
    first = [
        observation(10.0, b"same", index="f0"),
        observation(20.0, b"same", index="f1"),
    ]
    second = [
        observation(10.0, b"same", index="s0"),
        observation(20.0, b"same", index="s1"),
    ]

    result = correlate(first, second, anchors=[(10.0, 10.0)], window=0.1)

    assert [(pair.first.index, pair.second.index) for pair in result.pairs] == [
        ("f0", "s0"),
        ("f1", "s1"),
    ]
    assert len(result.pairs) == 2


def test_observation_can_be_built_from_pcapng_packet_blocks():
    section = blocks.SectionHeader()
    section.new_member(blocks.InterfaceDescription, link_type=1, snaplen=65535)
    packet = section.new_member(
        blocks.EnhancedPacket,
        timestamp_high=0,
        timestamp_low=1500000,
        packet_len=40,
        packet_data=b"abcdef",
    )

    observation = PacketObservation.from_block(packet, index=7)

    assert observation.timestamp == pytest.approx(1.5)
    assert observation.captured_len == 6
    assert observation.packet_len == 40
    assert observation.captured_range == (0, 6)
    assert observation.link_type == 1
    assert observation.index == 7


def test_observations_from_blocks_skips_non_packet_blocks():
    section = blocks.SectionHeader()
    section.new_member(blocks.InterfaceDescription, link_type=1, snaplen=65535)
    packet = section.new_member(
        blocks.EnhancedPacket,
        timestamp_high=0,
        timestamp_low=1000,
        packet_data=b"abc",
    )

    observations = observations_from_blocks([section, packet])

    assert len(observations) == 1
    assert observations[0].packet_data == b"abc"


def test_correlate_captures_reads_two_written_pcapng_streams():
    def write_capture(rows, snaplen):
        section = blocks.SectionHeader()
        section.new_member(blocks.InterfaceDescription, link_type=1, snaplen=snaplen)
        stream = io.BytesIO()
        writer = FileWriter(stream, section)
        for timestamp, data, packet_len in rows:
            writer.write_block(
                section.new_member(
                    blocks.EnhancedPacket,
                    timestamp_high=0,
                    timestamp_low=int(timestamp * 1e6),
                    packet_len=packet_len,
                    packet_data=data,
                )
            )
        stream.seek(0)
        return stream

    first_stream = write_capture(
        [(2.0, b"abcdef", 60), (3.0, b"unrelated", 30)],
        snaplen=65535,
    )
    second_stream = write_capture([(1.0, b"abc", 60)], snaplen=32)

    result = correlate_captures(
        read_observations(first_stream),
        read_observations(second_stream),
        anchors=[(2.0, 1.0)],
        window=0.5,
    )

    assert len(result.pairs) == 1
    assert result.pairs[0].second.captured_range == (0, 3)
    assert result.pairs[0].first.captured_range == (0, 6)
    assert len(result.unmatched_first) == 1


def test_missing_anchor_or_clock_is_rejected():
    first = [observation(1.0, b"x")]
    second = [observation(1.0, b"x")]

    with pytest.raises(ValueError):
        correlate(first, second, window=1.0)


def test_explicit_clock_pair_is_accepted():
    first = [observation(11.0, b"x", index="f")]
    second = [observation(1.0, b"x", index="s")]

    result = correlate(first, second, clock=(1.0, 10.0), window=0.0)

    assert len(result.pairs) == 1


def test_file_scanner_inputs_are_supported(tmp_path):
    def write_capture(path, snap, rows):
        section = blocks.SectionHeader()
        section.new_member(blocks.InterfaceDescription, link_type=1, snaplen=snap)
        with open(path, "wb") as stream:
            writer = FileWriter(stream, section)
            for timestamp, data in rows:
                writer.write_block(
                    section.new_member(
                        blocks.EnhancedPacket,
                        timestamp_high=0,
                        timestamp_low=int(timestamp * 1e6),
                        packet_len=len(data),
                        packet_data=data,
                    )
                )

    first_path = tmp_path / "first.pcapng"
    second_path = tmp_path / "second.pcapng"
    write_capture(first_path, 65535, [(5.0, b"abc")])
    write_capture(second_path, 65535, [(5.0, b"abc")])

    with open(first_path, "rb") as first_stream:
        with open(second_path, "rb") as second_stream:
            result = correlate_captures(
                FileScanner(first_stream),
                FileScanner(second_stream),
                anchors=[(5.0, 5.0)],
                window=0.1,
            )

    assert len(result.pairs) == 1


def test_invalid_inputs_are_rejected():
    with pytest.raises(TypeError):
        PacketObservation(timestamp="now", packet_data=b"x")
    with pytest.raises(ValueError):
        PacketObservation(timestamp=1.0, packet_data=b"x", captured_len=2)
    with pytest.raises(TypeError):
        correlate(
            [object()],
            [observation(1.0, b"x")],
            anchors=[(1.0, 1.0)],
            window=1.0,
        )
    with pytest.raises(ValueError):
        correlate(
            [observation(1.0, b"x")],
            [observation(1.0, b"x")],
            anchors=[(1.0, 1.0)],
            window=float("inf"),
        )
