import os
import struct

import pytest

from Evtx import Recovery, RecoveryReport

SYSTEM = os.path.join(os.path.dirname(__file__), "data", "system.evtx")
ISSUE_43 = os.path.join(os.path.dirname(__file__), "data", "issue_43.evtx")

# Chunk 8 of system.evtx (the tail chunk).  Record boundaries were read from
# the file itself, so the tests below describe bytes that really exist.
TAIL_CHUNK = 0x1000 + 8 * 0x10000
REC_13647 = 0x902A0  # size 0x158
REC_13648 = 0x903F8  # size 0x138
REC_13649 = 0x90530  # size 0x160, the last record in the log


def _truncate(tmp_path, source, cut, name="cut.evtx"):
    with open(source, "rb") as f:
        data = f.read(cut)
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _rewrite_file_header(path, mutator):
    """Apply mutator to a copy of the file header, keeping its CRC valid."""
    import binascii

    with open(path, "r+b") as f:
        data = bytearray(f.read())
        mutator(data)
        struct.pack_into("<I", data, 0x7C, binascii.crc32(bytes(data[0:0x78])) & 0xFFFFFFFF)
        f.seek(0)
        f.write(data)


def test_cut_inside_record_keeps_only_complete_records(tmp_path):
    """A record cut by the truncation is reported, never counted or padded."""
    path = _truncate(tmp_path, SYSTEM, REC_13648 + 0x40, "mid_record.evtx")
    report = Recovery.analyze(path)

    assert report.file_header.magic_ok is True
    assert report.file_header.declared_chunk_count == 9
    assert len(report.records) == 1599
    assert report.records[-1].record_num == 13647
    assert report.records[-1].end == REC_13648

    truncation = report.terminal_truncation
    assert truncation.kind == "record"
    assert truncation.offset == REC_13648
    assert truncation.declared_size == 0x138
    assert truncation.available == 0x40
    assert truncation.missing == 0x138 - 0x40

    # The cut record contributes nothing, even though 0x40 of its bytes exist.
    assert 13648 not in [r.record_num for r in report.records]
    assert report.last_complete_record.end == REC_13648


def test_cut_on_record_boundary(tmp_path):
    """Cutting exactly between records leaves every preceding record complete."""
    path = _truncate(tmp_path, SYSTEM, REC_13649, "boundary.evtx")
    report = Recovery.analyze(path)

    assert len(report.records) == 1600
    assert report.last_complete_record.record_num == 13648
    assert report.last_complete_record.end == REC_13649

    truncation = report.terminal_truncation
    assert truncation.kind == "chunk"
    assert truncation.offset == REC_13649
    assert truncation.missing == TAIL_CHUNK + Recovery.CHUNK_SIZE - REC_13649

    missing = report.missing_beyond_eof()
    assert len(missing) == 1
    assert missing[0].start == REC_13649
    assert missing[0].end == TAIL_CHUNK + Recovery.CHUNK_SIZE


def test_cut_inside_chunk_header(tmp_path):
    """A chunk whose header was cut yields no records and no guessed fields."""
    path = _truncate(tmp_path, SYSTEM, TAIL_CHUNK + 0x100, "cut_header.evtx")
    report = Recovery.analyze(path)

    assert report.last_complete_record.record_num == 13472
    assert report.last_complete_chunk.number == 7

    tail = report.chunks[-1]
    assert tail.number == 8
    assert tail.magic_ok is True
    assert tail.record_count == 0
    # The first 0x38 bytes of a chunk header arrived, so the log span is
    # readable evidence even though the header itself is incomplete.
    assert tail.declared_log_first == 13473
    assert tail.truncation.kind == "chunk header"
    assert tail.truncation.available == 0x100


def test_declared_count_is_evidence_not_a_record_list(tmp_path):
    """A stale file header cannot hide records, and a silent one cannot erase them."""
    path = str(tmp_path / "stale.evtx")
    import shutil

    shutil.copyfile(SYSTEM, path)
    _rewrite_file_header(
        path,
        lambda data: (
            struct.pack_into("<Q", data, 0x18, 0),  # next_record_number = 0
            struct.pack_into("<H", data, 0x2A, 0),  # chunk_count = 0
        ),
    )
    report = Recovery.analyze(path)

    assert len(report.records) == 1601
    assert report.records[0].record_num == 12049
    assert report.records[-1].record_num == 13649

    fh = report.file_header
    assert fh.declared_next_record_number == 0
    assert fh.declared_chunk_count == 0
    assert fh.checksum_ok is True
    assert any("declares no records" in note for _, _, _, note in report.declarations())

    # Chunks the header never declared are still reported from their own bytes.
    assert {c.number for c in report.undeclared_chunks()} == set(range(9))
    assert sum(c.record_count for c in report.chunks) == 1601


def test_header_disagreement_is_surfaced(tmp_path):
    """Chunk headers and the file header disagreeing is reported, not resolved."""
    report = Recovery.analyze(SYSTEM)

    # system.evtx is dirty: the file header stops at 13527 but chunk headers
    # describe records through 13649, and the bytes agree with the chunks.
    assert report.file_header.dirty is True
    conflicts = "\n".join(report.declaration_conflicts())
    assert "13527" in conflicts
    assert "13649" in conflicts
    assert report.records[-1].record_num == 13649


def test_unrenderable_record_is_counted_but_not_exported(tmp_path):
    """A framed record whose BinXML is corrupt stays in the manifest, not the XML."""
    with open(ISSUE_43, "rb") as f:
        data = f.read()

    report_all = Recovery.analyze(ISSUE_43)
    bad = [r for r in report_all.unrenderable_records if r.record_num == 508]
    assert bad, "fixture record 508 should not render"

    path = _truncate(tmp_path, ISSUE_43, bad[0].end, "unrenderable.evtx")
    report = Recovery.analyze(path)
    assert 508 in [r.record_num for r in report.records]
    assert len(report.unrenderable_records) >= 1

    xml = RecoveryReport.xml_text(report)
    assert "<Events>" in xml
    assert "EventID" in xml  # renderable records are present

    rows = RecoveryReport.manifest_rows(report)
    row_508 = [r for r in rows if r["record_num"] == 508][0]
    assert row_508["renderable"] is False
    assert row_508["render_error"]
    # The record is still reported as verifiable framing, so it is not "made up".
    assert row_508["trusted"] is True


def test_coverage_accounts_every_byte_without_double_counting(tmp_path):
    """Present bytes are either structure, slack, or the cut; nothing is counted twice."""
    path = _truncate(tmp_path, SYSTEM, REC_13648 + 0x40, "mid_record.evtx")
    report = Recovery.analyze(path)

    gaps = report.coverage()
    assert gaps, "chunk slack and the partial cut record should be reported"

    claimed = []
    claimed.append((0, report.file_size and min(Recovery.FILE_HEADER_SIZE, report.file_size)))
    for chunk in report.chunks:
        claimed.append((chunk.offset, min(chunk.offset + Recovery.CHUNK_HEADER_SIZE, report.file_size)))
    for record in report.records:
        claimed.append((record.offset, record.end))
    for gap in gaps:
        claimed.append((gap.start, gap.end))

    claimed.sort()
    end = 0
    for start, stop in claimed:
        assert start >= end, "ranges overlap at {}".format(hex(start))
        end = stop
    assert end == report.file_size, "every byte should be accounted for"

    partial = [g for g in gaps if "partial bytes" in g.reason]
    assert len(partial) == 1
    assert partial[0].start == REC_13648
    assert partial[0].end == REC_13648 + 0x40


def test_clean_file_reports_no_cut():
    report = Recovery.analyze(SYSTEM)
    assert report.terminal_truncation is None
    assert report.file_size == Recovery.FILE_HEADER_SIZE + 17 * Recovery.CHUNK_SIZE
    assert len(report.chunks) == 9
    assert sum(c.record_count for c in report.chunks) == 1601
    assert report.missing_beyond_eof() == []


def test_report_text_names_the_required_evidence(tmp_path):
    path = _truncate(tmp_path, SYSTEM, REC_13648 + 0x40, "mid_record.evtx")
    text = RecoveryReport.summary_text(Recovery.analyze(path))

    assert "DECLARED RECORD COUNTS" in text
    assert "VERIFIED RECORDS" in text
    assert "last complete record" in text
    assert "cut point" in text
    assert "absent" in text
    assert "UNACCOUNTED INSIDE THE FILE" in text
    assert "0x90438" in text  # end of the last complete record / start of the cut
