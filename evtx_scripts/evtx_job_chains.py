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

import os
import sys
import json


def provider_role_map(values):
    result = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError("provider role must use PROVIDER=ROLE: {}".format(value))
        provider, role = value.split("=", 1)
        result[provider.strip()] = role.strip()
    return result


def event_id_role_map(values):
    result = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError("event role must use EVENT_ID=ROLE: {}".format(value))
        event_id, role = value.split("=", 1)
        result[event_id.strip()] = role.strip()
    return result


def load_events(path):
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            child = os.path.join(path, name)
            if os.path.isfile(child):
                for event in load_events(child):
                    yield event
        return

    if path.lower().endswith((".json", ".jsonl", ".ndjson")):
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except ValueError:
            payload = [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(payload, dict):
            if "events" in payload and isinstance(payload["events"], list):
                payload = payload["events"]
            else:
                payload = [payload]
        for event in payload:
            yield event
        return

    try:
        from Evtx.Evtx import Evtx

        with Evtx(path) as log:
            for record in log.records():
                yield record
    except Exception:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(payload, dict):
            payload = payload.get("events", [payload])
        for event in payload:
            yield event


def format_chain(chain):
    lines = []
    status = "complete" if chain.complete else "incomplete"
    lines.append(
        "{}: {} activities, {} events, {} ({})".format(
            chain.chain_id, len(chain.nodes), chain.workload, status, " -> ".join(chain.roots) or "-"
        )
    )
    for node in chain.nodes.values():
        flags = []
        if node.missing_start:
            flags.append("missing start")
        if node.missing_end:
            flags.append("missing end")
        if node.parents:
            flags.append("after {}".format(", ".join(sorted(chain.display(p) for p in node.parents))))
        if node.children:
            flags.append("before {}".format(", ".join(sorted(chain.display(c) for c in node.children))))
        lines.append(
            "  {} [{}] {} event(s){}".format(
                node.activity_id, node.status, node.event_count, "  " + "; ".join(flags) if flags else ""
            )
        )
    if chain.cyclic:
        lines.append(
            "  cycles: {}".format(" | ".join(" -> ".join(cycle) for cycle in chain.cycles))
        )
    if chain.missing_start:
        lines.append("  MISSING START: {}".format(", ".join(chain.missing_start_nodes)))
    if chain.missing_end:
        lines.append("  MISSING END: {}".format(", ".join(chain.missing_end_nodes)))
    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Reconstruct collection job chains from EVTX or JSON events.")
    parser.add_argument("path", help="EVTX file, JSON/JSONL event export, or directory")
    parser.add_argument(
        "--provider-role",
        action="append",
        metavar="PROVIDER=ROLE",
        help="map a provider to start/transfer/end",
    )
    parser.add_argument(
        "--event-role",
        action="append",
        metavar="EVENT_ID=ROLE",
        help="map an event id to start/transfer/end",
    )
    parser.add_argument("--default-role", default="unknown", help="role for events that cannot be classified")
    parser.add_argument(
        "--edge-direction",
        default="related_to_activity",
        choices=("related_to_activity", "activity_to_related"),
        help="draw the edge from the related activity (default) or from the reporting activity",
    )
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when a chain is missing a start or end")
    args = parser.parse_args()

    from Evtx.JobChain import build_job_chains, job_chains_from_file, is_evtx_file

    provider_roles = provider_role_map(args.provider_role)
    event_id_roles = event_id_role_map(args.event_role)

    if is_evtx_file(args.path):
        report = job_chains_from_file(
            args.path,
            provider_roles=provider_roles,
            event_id_roles=event_id_roles,
            default_role=args.default_role,
            edge_direction=args.edge_direction,
        )
    else:
        report = build_job_chains(
            load_events(args.path),
            provider_roles=provider_roles,
            event_id_roles=event_id_roles,
            default_role=args.default_role,
            edge_direction=args.edge_direction,
        )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=False))
    else:
        if not report.chains:
            print("no job chains found")
        for chain in report.chains:
            print(format_chain(chain))
            print()
        print(
            "total: {} chain(s), {} unique event(s), {} missing start, {} missing end, {} cyclic".format(
                len(report.chains),
                report.workload,
                len(report.missing_start_chains),
                len(report.missing_end_chains),
                len(report.cyclic_chains),
            )
        )

    if args.strict and (report.missing_start_chains or report.missing_end_chains):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
