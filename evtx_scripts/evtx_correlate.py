#!/usr/bin/env python3
#   This file is part of python-evtx.
#
#   Purpose: Correlate the same physical events across overlapping EVTX
#   backups while preserving every original location. RecordIDs are treated as
#   machine/channel scoped, identical bodies alone never confirm a copy, and
#   genuine repeated events stay on separate rows.
import json
import os

from Evtx.correlate import (
    REASON_DISTINCT_RECORD_ID,
    REASON_REUSED_RECORD_ID,
    correlate_sources,
)


def parse_source_spec(spec):
    """
    Parse a GENERATION=PATH argument.

    A bare path is accepted, in which case the file name is used as the
    generation label.
    """
    if "=" in spec:
        generation, path = spec.split("=", 1)
        return path, generation
    return spec, os.path.basename(spec)


def print_report(result, sources):
    print("EVTX cross-backup correlation")
    print("=" * 30)
    print("sources scanned: {}".format(len(sources)))
    print("observations:    {}".format(sum(len(event.observations) for event in result.logical_events)))
    print("logical events:  {}".format(len(result.logical_events)))
    print("  unique:        {}".format(len(result.unique_events())))
    print("  duplicates:    {}".format(len(result.duplicates())))
    print("candidate links: {}".format(len(result.candidate_links)))
    print("parse errors:    {}".format(len(result.parse_errors)))

    for event in result.duplicates():
        print("")
        print(
            "DUP {machine} {channel} RecordID={record_id} {timestamp}".format(
                machine=event.machine,
                channel=event.channel,
                record_id=event.record_num,
                timestamp=event.timestamp,
            )
        )
        for evidence in event.evidence:
            print("  evidence: {}".format(evidence))
        for location in event.locations:
            print(
                "  copy: {source} generation={generation} chunk={chunk} offset=0x{offset:x} "
                "file_ordinal={ordinal}".format(
                    source=location.source,
                    generation=location.generation,
                    chunk=location.chunk_index,
                    offset=location.offset,
                    ordinal=location.file_record_num,
                )
            )

    distinct = [link for link in result.candidate_links if link.reason == REASON_DISTINCT_RECORD_ID]
    reused = [link for link in result.candidate_links if link.reason == REASON_REUSED_RECORD_ID]
    if distinct:
        print("")
        print("Genuine repeated events kept as separate rows ({}):".format(len(distinct)))
        for link in distinct:
            print(
                "  {machine} {channel} body={body}".format(
                    machine=link.machine, channel=link.channel, body=link.body_hash[:12]
                )
            )
            for location in link.locations:
                print(
                    "    row: {source} generation={generation} RecordID={record_id} offset=0x{offset:x}".format(
                        source=location.source,
                        generation=location.generation,
                        record_id=location.record_num,
                        offset=location.offset,
                    )
                )
    if reused:
        print("")
        print("Unconfirmed RecordID reuse, not merged ({}):".format(len(reused)))
        for link in reused:
            print(
                "  {machine} {channel} body={body}".format(
                    machine=link.machine, channel=link.channel, body=link.body_hash[:12]
                )
            )
            for location in link.locations:
                print(
                    "    candidate: {source} generation={generation} RecordID={record_id} offset=0x{offset:x}".format(
                        source=location.source,
                        generation=location.generation,
                        record_id=location.record_num,
                        offset=location.offset,
                    )
                )

    if result.parse_errors:
        print("")
        print("Parse errors:")
        for error in result.parse_errors:
            print("  {}".format(error))


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Correlate copies of the same events across overlapping EVTX backups."
    )
    parser.add_argument(
        "sources",
        nargs="+",
        type=str,
        help="EVTX files as GENERATION=PATH (or a bare PATH; the file name is used as generation).",
    )
    parser.add_argument("-o", "--output", type=str, help="Optional path to write the full result as JSON.")
    args = parser.parse_args()

    sources = [parse_source_spec(spec) for spec in args.sources]
    result = correlate_sources(sources)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as outfile:
            json.dump(result.to_dict(), outfile, indent=2)
    else:
        print_report(result, sources)


if __name__ == "__main__":
    main()
