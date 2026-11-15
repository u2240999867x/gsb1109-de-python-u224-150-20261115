#!/usr/bin/env python
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
"""
Offline recovery of a possibly damaged EVTX file.

The walk is layout driven, never signature driven:

  * chunks are reached at their fixed geometric offsets
    (0x1000 + chunk_index * 0x10000), so a destroyed chunk header does not
    block access to the following chunks;
  * record boundaries are the chunk data origin and each prior record's
    declared size, so a record magic (0x2a2a) appearing inside a payload is
    never mistaken for the start of a record;
  * each record header is checked for its magic, size sanity and trailing
    size copy, and chunk header/data CRC32 checksums are reported separately.

Only records that pass the structural checks are presented.  Every byte range
that could not be recovered is reported as an absolute range of the original
file, so nothing is silently dropped.
"""
import argparse

import Evtx.Evtx as evtx
import Evtx.Views as e_views


def _fmt_checksum(value):
    if value is None:
        return "n/a"
    return "pass" if value else "fail"


def _fmt_range(skipped_range):
    if skipped_range is None:
        return "-"
    start, end = skipped_range
    return "0x%08x-0x%08x (%d bytes)" % (start, end, end - start)


def print_report(result, file_header):
    print("EVTX offline recovery report")
    print("=" * 28)
    print("File header magic   : %s" % ("ok" if file_header.check_magic() else "INVALID"))
    print("File header checksum: %s" % _fmt_checksum(file_header.calculate_checksum() == file_header.checksum()))
    print("")

    print(
        "%-5s %-12s %-22s %-7s %-7s %-7s %-7s %s"
        % ("chunk", "offset", "status", "hdrCRC", "dataCRC", "recs", "render", "skipped range (original file)")
    )
    total_records = 0
    total_render_failures = 0

    for chunk_result in result.chunk_results:
        valid_results = [rr for rr in chunk_result.records if rr.is_valid]
        render_failures = [rr for rr in valid_results if not rr.renders]
        total_records += len(valid_results)
        total_render_failures += len(render_failures)
        print(
            "%-5d 0x%010x %-22s %-7s %-7s %-7d %-7d %s"
            % (
                chunk_result.chunk_index,
                chunk_result.chunk_offset,
                chunk_result.status,
                _fmt_checksum(chunk_result.header_checksum_ok),
                _fmt_checksum(chunk_result.data_checksum_ok),
                len(valid_results),
                len(render_failures),
                _fmt_range(chunk_result.skipped_range),
            )
        )
        if chunk_result.error:
            print("        reason: %s" % chunk_result.error)
        for record_result in chunk_result.records:
            if not record_result.is_valid:
                print(
                    "        record boundary at 0x%08x rejected (%s): %s"
                    % (record_result.offset, record_result.status, record_result.error)
                )
            elif record_result.render_error:
                print(
                    "        record %d at 0x%08x structurally valid but payload failed: %s"
                    % (record_result.record_num, record_result.offset, record_result.render_error)
                )

    print("")
    print("Totals")
    print("-" * 6)
    print("Structurally valid records recovered : %d" % total_records)
    print("Of those, payloads that fail to render: %d" % total_render_failures)
    print("Skipped raw ranges                   : %d" % len(result.skipped_ranges))
    for start, end in result.skipped_ranges:
        print("  0x%08x - 0x%08x  (%d bytes)" % (start, end, end - start))
    print("Total skipped bytes                  : %d" % result.total_skipped_bytes)


def write_recovered_xml(result, output_path):
    """
    Write only the records whose binary XML renders, preserving file order.
    Structurally valid records whose payload is corrupt are omitted here but
    still counted and located in the report.
    """
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(e_views.XML_HEADER)
        f.write("<Events>\n")
        for record_result in result.record_results:
            if record_result.renders:
                f.write(record_result.record.xml())
                f.write("\n")
        f.write("</Events>\n")


def main():
    parser = argparse.ArgumentParser(description="Recover records from a possibly damaged EVTX file.")
    parser.add_argument("evtx", type=str, help="Path to the Windows EVTX event log file")
    parser.add_argument(
        "--xml",
        type=str,
        default=None,
        help="Optional path to write the recovered, renderable records as XML",
    )
    args = parser.parse_args()

    with evtx.Evtx(args.evtx) as log:
        result = log.recover()
        result.verify_renderable_records()
        print_report(result, log.get_file_header())
        if args.xml:
            write_recovered_xml(result, args.xml)
            print("")
            print("Recovered renderable records written to %s" % args.xml)


if __name__ == "__main__":
    main()
