#!/usr/bin/env python3
"""Joint feasibility of fixed clock offsets across multiple recorders.

Model
-----
Each recorder r has a fixed clock offset o_r:  local_ts = true_time + o_r.

Each confirmed one-to-one packet correspondence says: a packet was observed
at recorder u with local timestamp t_u, and later at recorder v with local
timestamp t_v, and the propagation delay on that link lies in [lo, hi].

True observation times are T_u = t_u - o_u and T_v = t_v - o_v, so:

    lo <= T_v - T_u <= hi
    =>  lo - D <= o_u - o_v <= hi - D        (D = t_v - t_u)

which is a pair of difference constraints:

    o_u - o_v <= hi - D      (edge v -> u, weight hi - D)
    o_v - o_u <= D - lo      (edge u -> v, weight D - lo)

All correspondences must hold *simultaneously* against a single offset per
recorder.  One recorder is pinned to o_ref = 0 (offsets are only determined
up to a common constant).  The feasible interval of every o_x is then

    [ -dist(x, ref), dist(ref, x) ]

where dist() are shortest-path distances in the constraint graph.  A
negative cycle means the correspondences are jointly contradictory; the
cycle itself is the conflicting correspondence chain.

Input: JSON file
----------------
{
  "reference": "A",                 # recorder pinned to offset 0
  "correspondences": [
    {"label": "c1",                 # optional
     "from": ["A", 1000.0],         # [recorder, local_ts] first observation
     "to":   ["B", 1002.0],         # [recorder, local_ts] second observation
     "delay": [0.5, 3.0]}           # propagation bounds on that link
  ]
}

Units are whatever the timestamps use (keep them consistent).

Usage:  python3 clock_offsets.py data.json
"""

import json
import math
import sys


def build_edges(correspondences):
    """Return list of (u, v, w, label) meaning o_v - o_u <= w."""
    edges = []
    for i, c in enumerate(correspondences):
        label = c.get("label", "corr#%d" % i)
        u, t_u = c["from"]
        v, t_v = c["to"]
        lo, hi = c["delay"]
        delta = t_v - t_u
        edges.append((v, u, hi - delta, "%s [%s->%s upper]" % (label, u, v)))
        edges.append((u, v, delta - lo, "%s [%s->%s lower]" % (label, u, v)))
    return edges


def bellman_ford(nodes, edges, source):
    dist = {n: math.inf for n in nodes}
    pred = {}
    dist[source] = 0.0
    for _ in range(len(nodes) - 1):
        changed = False
        for u, v, w, label in edges:
            if dist[u] + w < dist[v] - 1e-12:
                dist[v] = dist[u] + w
                pred[v] = (u, label)
                changed = True
        if not changed:
            break
    # one more pass: any node still relaxable sits on/after a negative cycle
    witness = None
    for u, v, w, label in edges:
        if dist[u] + w < dist[v] - 1e-12:
            pred[v] = (u, label)
            witness = v
            break
    return dist, pred, witness


def extract_cycle(pred, witness, nodes):
    # walk back len(nodes) steps to land inside the cycle for sure
    x = witness
    for _ in range(len(nodes)):
        x = pred[x][0]
    cycle = []
    seen = set()
    while x not in seen:
        seen.add(x)
        u, label = pred[x]
        cycle.append((u, x, label))
        x = u
    nodes_in_cycle = [n for n, _, _ in cycle]
    start = nodes_in_cycle.index(x)
    return cycle[: start + 1]


def solve(data):
    corrs = data["correspondences"]
    ref = data["reference"]
    recorders = sorted({r for c in corrs for r in (c["from"][0], c["to"][0])})
    if ref not in recorders:
        recorders.append(ref)

    ZERO = "__zero__"    # dummy node representing constant 0
    SUPER = "__super__"  # super-source so every node is reachable
    nodes = recorders + [ZERO, SUPER]
    edges = build_edges(corrs)
    # pin o_ref = 0:  o_ref <= o_ZERO and o_ZERO <= o_ref
    edges.append((ZERO, ref, 0.0, "pin %s = 0" % ref))
    edges.append((ref, ZERO, 0.0, "pin %s = 0" % ref))
    for n in nodes:
        if n != SUPER:
            edges.append((SUPER, n, 0.0, "reach"))

    dist, pred, witness = bellman_ford(nodes, edges, SUPER)
    if witness is not None:
        cycle = extract_cycle(pred, witness, nodes)
        print("INFEASIBLE: no single set of fixed offsets satisfies all "
              "correspondences simultaneously.")
        print("Conflicting correspondence chain (negative cycle):")
        total = 0.0
        for u, v, label in cycle:
            w = next(w for a, b, w, l in edges if a == u and b == v
                     and l == label)
            total += w
            print("  o_%s - o_%s <= %.6g   (%s)" % (v, u, w, label))
        print("Sum of bounds around the cycle = %.6g < 0" % total)
        return 1

    # tight per-recorder bounds: upper = dist(ref->x), lower = -dist(x->ref)
    # dist from ZERO already gives dist(ZERO->x); recompute from ref and
    # reverse graph for exact one-to-all distances.
    def distances_from(src):
        d, _, _ = bellman_ford(nodes, edges, src)
        return d

    d_from_ref = distances_from(ref)
    rev_edges = [(v, u, w, l) for u, v, w, l in edges]
    d_to_ref = bellman_ford(nodes, rev_edges, ref)[0]

    print("FEASIBLE: all correspondences hold simultaneously.")
    print("Reference: o_%s = 0" % ref)
    for r in recorders:
        lo = -d_to_ref[r]
        hi = d_from_ref[r]
        print("  o_%s in [%.6g, %.6g]  (width %.6g)" % (r, lo, hi, hi - lo))
    return 0


def main(argv):
    with open(argv[1]) as fh:
        data = json.load(fh)
    return solve(data)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
