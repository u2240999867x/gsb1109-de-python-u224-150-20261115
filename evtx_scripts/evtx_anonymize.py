#!/usr/bin/env python3
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
"""Export events with only policy-selected identifiers anonymized.

The policy can come from a JSON document (``--policy``) or from repeated
command line options (``--path``, ``--map``, ``--namespace``). The original
EVTX file is never modified.

Examples::

    evtx_anonymize security.evtx --policy policy.json -o events.json
    evtx_anonymize security.evtx \\
        --path "/Event/System/Security/@UserID" \\
        --path "/Event/EventData/Data[@Name='TargetUserName']/text()" \\
        --map "S-1-5-18=USER-0001" -o events.xml
"""

import argparse
import json
import os
import sys

import Evtx.Anonymize as anonymize
import Evtx.Evtx as evtx


def _format_for(path, explicit):
    if explicit:
        return explicit
    if path:
        extension = os.path.splitext(path)[1].lower()
        if extension == ".json":
            return "json"
        if extension == ".jsonl":
            return "jsonl"
        if extension == ".xml":
            return "xml"
    return "xml"


def _write_xml(stream, anonymizer, log):
    stream.write('<?xml version="1.0" encoding="utf-8" standalone="yes" ?>\n')
    stream.write("<Events>\n")
    for record in log.records():
        stream.write(anonymizer.anonymize_record(record))
        stream.write("\n")
    stream.write("</Events>\n")


def _write_json(stream, anonymizer, log):
    events = []
    for xml in anonymize.anonymize_records(log.records(), anonymizer):
        events.append(anonymize.xml_to_dict(anonymize.parse_xml(xml)))
    json.dump(events, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def _write_jsonl(stream, anonymizer, log):
    for xml in anonymize.anonymize_records(log.records(), anonymizer):
        stream.write(json.dumps(anonymize.xml_to_dict(anonymize.parse_xml(xml)), ensure_ascii=False))
        stream.write("\n")


WRITERS = {"xml": _write_xml, "json": _write_json, "jsonl": _write_jsonl}


def _parse_pairs(values, label):
    result = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit("%s expects OLD=NEW, got %r" % (label, value))
        old, new = value.split("=", 1)
        result[old] = new
    return result


def _parse_namespaces(values):
    namespaces = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit("--namespace expects PREFIX=URI, got %r" % value)
        prefix, uri = value.split("=", 1)
        namespaces[prefix] = uri
    return namespaces


def _read_paths(path):
    paths = []
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(line)
    return paths


def _build_policy(args):
    if args.policy:
        if args.path or args.map or args.relation:
            raise SystemExit("--policy cannot be combined with --path/--map/--relation")
        return anonymize.AnonymizationPolicy.from_file(args.policy)

    paths = list(args.path or [])
    for path_file in args.paths_file or []:
        paths.extend(_read_paths(path_file))
    mapping = _parse_pairs(args.map, "--map")
    if args.mapping_file:
        with open(args.mapping_file, "r", encoding="utf-8") as stream:
            mapping.update(anonymize._parse_mapping(json.load(stream), "mapping"))
    if not paths:
        raise SystemExit("provide --policy, --path or --paths-file")

    data = {
        "paths": paths,
        "mapping": mapping,
        "namespaces": _parse_namespaces(args.namespace),
        "prefix": args.prefix,
        "salt": args.salt,
        "digest_length": args.digest_length,
        "scheme": args.scheme,
    }
    if args.default_namespace is not None:
        data["default_namespace"] = args.default_namespace
    relations = []
    for group in args.relation or []:
        # A JSON object avoids ambiguities when paths themselves contain "=".
        try:
            relation = json.loads(group)
        except ValueError:
            raise SystemExit(
                '--relation expects a JSON object like {"paths": ["p1", "p2"], "mapping": {"OLD": "NEW"}}: %r' % group
            )
        relations.append(relation)
    if relations:
        data["relations"] = relations
    return anonymize.AnonymizationPolicy(data)


def main():
    parser = argparse.ArgumentParser(
        description="Anonymize only the user identifiers selected by a policy, leaving other fields intact."
    )
    parser.add_argument("evtx", type=str, help="Path to the Windows EVTX event log file")
    parser.add_argument("--policy", type=str, help="Path to a JSON anonymization policy")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Allowed path to anonymize; repeat for multiple paths",
    )
    parser.add_argument(
        "--paths-file",
        action="append",
        default=[],
        help="File with one allowed path per line",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Explicit identifier replacement; repeat for multiple entries",
    )
    parser.add_argument("--mapping-file", type=str, help="JSON object or from/to list of replacements")
    parser.add_argument(
        "--relation",
        action="append",
        default=[],
        metavar="JSON",
        help='Related paths and their mapping, as a JSON object: {"paths": [...], "mapping": {...}}',
    )
    parser.add_argument(
        "--namespace",
        action="append",
        default=[],
        metavar="PREFIX=URI",
        help="Namespace prefix used by paths; repeat for multiple prefixes",
    )
    parser.add_argument("--default-namespace", type=str, help="Namespace for unprefixed path segments")
    parser.add_argument("--prefix", type=str, default=anonymize.DEFAULT_PREFIX, help="Generated pseudonym prefix")
    parser.add_argument("--salt", type=str, default=anonymize.DEFAULT_SALT, help="Salt for generated pseudonyms")
    parser.add_argument(
        "--digest-length",
        type=int,
        default=anonymize.DEFAULT_DIGEST_LENGTH,
        help="Hex characters kept from the SHA-256 digest",
    )
    parser.add_argument(
        "--scheme",
        choices=("sha256", "counter"),
        default="sha256",
        help="Generated pseudonym scheme",
    )
    parser.add_argument("-o", "--output", type=str, help="Path of the exported file (default: stdout)")
    parser.add_argument(
        "--format",
        choices=sorted(WRITERS),
        help="Export format. Defaults to the output extension, else xml.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the structure check that guarantees exported events remain parseable.",
    )
    args = parser.parse_args()

    policy = _build_policy(args)
    output_format = _format_for(args.output, args.format)
    anonymizer = anonymize.Anonymizer(policy)

    with evtx.Evtx(args.evtx) as log:
        if not args.no_verify:
            # Check every event before writing anything, so a policy that
            # would damage the structure never produces a partial export.
            for record in log.records():
                original = anonymize.parse_xml(record.xml())
                anonymized = anonymize.parse_xml(anonymizer.anonymize_record(record))
                anonymize.assert_structure_preserved(original, anonymized)

        stream = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
        try:
            WRITERS[output_format](stream, anonymizer, log)
        finally:
            if args.output:
                stream.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
