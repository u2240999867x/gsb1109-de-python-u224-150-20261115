#!/usr/bin/python
#    This file is part of python-evtx.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""Render a RecoveryReport as text, a record manifest, or record exports."""
from __future__ import absolute_import

import datetime
import json

from . import Recovery

MANIFEST_COLUMNS = (
    "offset",
    "size",
    "end",
    "record_num",
    "chunk",
    "chunk_offset",
    "timestamp",
    "renderable",
    "trusted",
)


def _hex(value):
    return "0x{:x}".format(value) if value is not None else "-"


def _record_row(record):
    return {
        "offset": record.offset,
        "size": record.size,
        "end": record.end,
        "record_num": record.record_num,
        "chunk": record.chunk_number,
        "chunk_offset": record.chunk_relative_offset,
        "timestamp": record.timestamp.isoformat() if record.timestamp != datetime.datetime.min else "",
        "renderable": record.renderable,
        "trusted": not record.untrusted_fields,
        "render_error": record.render_error,
        "untrusted_fields": list(record.untrusted_fields),
    }


def manifest_rows(report):
    return [_record_row(record) for record in report.records]


def manifest_text(report):
    lines = ["\t".join(MANIFEST_COLUMNS)]
    for row in manifest_rows(report):
        lines.append(
            "\t".join(
                [
                    _hex(row["offset"]),
                    _hex(row["size"]),
                    _hex(row["end"]),
                    str(row["record_num"]),
                    str(row["chunk"]),
                    _hex(row["chunk_offset"]),
                    row["timestamp"],
                    "yes" if row["renderable"] else "no",
                    "yes" if row["trusted"] else "no",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def manifest_json(report):
    return json.dumps(manifest_rows(report), indent=2) + "\n"


def _trust_note(report):
    fh = report.file_header
    if fh is None or not fh.present or not fh.magic_ok:
        return "unreadable"
    if fh.dirty:
        return "dirty"
    return "clean"


def summary_lines(report):
    fh = report.file_header
    lines = []
    lines.append("file            {}".format(report.path))
    lines.append("file size       {} bytes ({})".format(report.file_size, _hex(report.file_size)))
    if fh is None or not fh.present:
        lines.append("file header     absent (file shorter than a header)")
    else:
        lines.append(
            "file header     {}  checksum {}  state {}".format(
                "present" if fh.magic_ok else "NOT EVTX (bad magic)",
                "ok" if fh.checksum_ok else "MISMATCH",
                _trust_note(report),
            )
        )
        declared_chunks = fh.declared_chunk_count
        span = "none" if declared_chunks == 0 else "indices 0..{}".format(declared_chunks - 1)
        lines.append(
            "declared chunks {} ({}), oldest {} current {}".format(
                declared_chunks, span, fh.oldest_chunk, fh.current_chunk
            )
        )
    extent = report.declared_region_end
    origin = "file header"
    if report.file_header is None or not report.file_header.present:
        extent, origin = 0, "no header, nothing declared"
    elif report.chunks and len(report.chunks) > (report.file_header.declared_chunk_count or 0):
        chunk_extent = report.chunks[-1].offset + Recovery.CHUNK_SIZE
        if chunk_extent > extent:
            extent, origin = chunk_extent, "chunk headers"
    lines.append("declared extent {} bytes ({}) [{}]".format(extent, _hex(extent), origin))
    lines.append("")

    lines.append("DECLARED RECORD COUNTS (evidence, not a record list)")
    for source, first, last, note in report.declarations():
        lines.append("  {:<34} first {}  last {}  [{}]".format(source, first, last, note))
    if not report.declarations():
        lines.append("  none readable")
    for conflict in report.declaration_conflicts():
        lines.append("  conflict: {}".format(conflict))
    empty_declarations = [
        source
        for source, first, last, note in report.declarations()
        if first is None and last is None
    ]
    if empty_declarations and report.records:
        lines.append(
            "  NOTE: {} declares nothing, yet {} verified records exist; "
            "the records, not the declaration, are the evidence".format(
                " and ".join(empty_declarations), len(report.records)
            )
        )
    lines.append("")

    lines.append("VERIFIED RECORDS")
    lines.append("  framed             {}  (magic, size and trailing size agree)".format(len(report.records)))
    lines.append("  trusted            {}  (also has BinXML root and a timestamp)".format(len(report.verified_records)))
    lines.append("  structure suspect  {}".format(len(report.untrusted_records)))
    lines.append("  XML renderable     {}".format(len(report.renderable_records)))
    lines.append(
        "  XML unrenderable   {}  (counted here, left out of exports)".format(len(report.unrenderable_records))
    )
    if report.records:
        numbers = [r.record_num for r in report.records]
        lines.append("  record number span {}..{}".format(min(numbers), max(numbers)))
    else:
        lines.append("  record number span none")
    lines.append("")

    lines.append("CHUNKS")
    lines.append(
        "  {:>4}  {:<10}  {:>8}  {:>8}  {:>7}  {:>7}  {:>6}  {}".format(
            "idx", "offset", "records", "declared", "first", "last", "hdr", "data"
        )
    )
    for chunk in report.chunks:
        if chunk.truncation is None:
            note = ""
        elif chunk.truncation.declared_end >= report.file_size:
            note = "  CUT " + chunk.truncation.detail
        else:
            note = "  DAMAGED: " + chunk.truncation.detail
        lines.append(
            "  {:>4}  {:<10}  {:>8}  {:>8}  {:>7}  {:>7}  {:>6}  {}{}".format(
                chunk.number,
                _hex(chunk.offset),
                chunk.record_count,
                chunk.declared_record_count,
                chunk.first_record_num if chunk.first_record_num is not None else "-",
                chunk.last_record_num if chunk.last_record_num is not None else "-",
                "ok" if chunk.header_checksum_ok else ("bad" if chunk.header_checksum_ok is not None else "-"),
                "ok" if chunk.data_checksum_ok else ("bad" if chunk.data_checksum_ok is not None else "-"),
                note,
            )
        )
    undeclared = report.undeclared_chunks()
    if undeclared:
        lines.append("  chunks present but not declared by the file header:")
        for chunk in undeclared:
            lines.append(
                "    idx {} offset {} records {} numbers {}..{}".format(
                    chunk.number,
                    _hex(chunk.offset),
                    chunk.record_count,
                    chunk.first_record_num,
                    chunk.last_record_num,
                )
            )
    lines.append("")

    lines.append("COMPLETENESS")
    last_record = report.last_complete_record
    last_chunk = report.last_complete_chunk
    if last_record is None:
        lines.append("  last complete record  none")
    else:
        lines.append(
            "  last complete record  #{} at {}..{} (chunk {} offset {})".format(
                last_record.record_num,
                _hex(last_record.offset),
                _hex(last_record.end),
                last_record.chunk_number,
                _hex(last_record.chunk_relative_offset),
            )
        )
    if last_chunk is None:
        lines.append("  last complete chunk   none")
    else:
        lines.append(
            "  last complete chunk   #{} at {}..{}".format(
                last_chunk.number, _hex(last_chunk.offset), _hex(last_chunk.offset + Recovery.CHUNK_SIZE)
            )
        )
    truncation = report.terminal_truncation
    if truncation is None:
        lines.append("  cut point             none (no incomplete structure at the end)")
    else:
        lines.append(
            "  cut point             {} at {} (declared {} bytes, {} present, {} missing)".format(
                truncation.kind,
                _hex(truncation.offset),
                truncation.declared_size if truncation.declared_size else "unknown",
                truncation.available,
                truncation.missing,
            )
        )
        lines.append("                        {}".format(truncation.detail))
    for coverage in report.missing_beyond_eof():
        lines.append(
            "  absent                {}..{} ({} bytes) {}".format(
                _hex(coverage.start), _hex(coverage.end), coverage.length, coverage.reason
            )
        )
    lines.append("")

    lines.append("UNACCOUNTED INSIDE THE FILE (present bytes no verified record claims)")
    gaps = report.coverage()
    if not gaps:
        lines.append("  none")
    for gap in gaps:
        lines.append(
            "  {}..{} ({:>7} bytes) {}".format(_hex(gap.start), _hex(gap.end), gap.length, gap.reason)
        )
    lines.append("")

    if report.unrenderable_records:
        lines.append("UNRENDERABLE RECORDS (framed and counted, XML not delivered)")
        for record in report.unrenderable_records:
            lines.append("  #{} at {}: {}".format(record.record_num, _hex(record.offset), record.render_error))
        lines.append("")
    if report.untrusted_records:
        lines.append("UNTRUSTED RECORDS (framed, but a structural field is inconsistent)")
        for record in report.untrusted_records:
            lines.append(
                "  #{} at {}: {}".format(record.record_num, _hex(record.offset), ", ".join(record.untrusted_fields))
            )
        lines.append("")
    return lines


def summary_text(report):
    return "\n".join(summary_lines(report)) + "\n"


def xml_text(report):
    """XML for every verified, renderable record; unrenderable ones omitted."""
    return Recovery.renderable_xml(report)


def jsonl_text(report):
    lines = []
    for record in report.records:
        row = _record_row(record)
        row["xml"] = record.xml() if record.renderable else None
        lines.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(lines) + "\n" if lines else ""
