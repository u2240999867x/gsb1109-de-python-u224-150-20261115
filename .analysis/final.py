#!/usr/bin/env python3
"""Final tri-partite report: identical content / attribute changes / insufficient.

Adds a fourth explicit finding VERIFIED-DIFFERENT for entries whose bytes were
read and confirmed unequal (the same-name/same-size trap), since they belong to
neither 'identical' nor 'insufficient'.
"""
import collections
import datetime
import json
import os
import stat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, ".analysis")
cat = json.load(open(os.path.join(WORK, "catalog.json"), encoding="utf-8"))
ver = json.load(open(os.path.join(WORK, "verified.json"), encoding="utf-8"))
rep = json.load(open(os.path.join(WORK, "report2.json"), encoding="utf-8"))


def epoch(ts):
    if not ts:
        return None
    dt = datetime.datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


# Re-derive attribute deltas but normalize timestamps to UTC instants.
def norm_attr(ent):
    is_siflnk = bool(ent["mode"]) and stat.S_ISLNK(int(ent["mode"], 8))
    return {
        "host_os": ent["host_os"],
        "mode": ent["mode"],
        "mtime_epoch": epoch(ent["mtime"]),
        "ctime_epoch": epoch(ent["ctime"]),
        "compress_type": ent["compress_type"],
        "needs_password": ent["needs_password"],
        "representation": (
            ("redir:%d" % ent["redir"]["type"]) if ent["redir"]
            else ("siflnk" if is_siflnk else ("dir" if ent["is_dir"] else "file"))
        ),
    }


def by_name(fname):
    out = collections.defaultdict(list)
    for ent in cat[fname]["entries"]:
        out[ent["name"]].append(ent)
    return out


summary = {"pairs": [], "counts": collections.Counter(),
           "attr_kinds": collections.Counter(), "insufficient": [], "different": []}

for fam in rep["families"].values():
    members = fam["members"]
    for pair, res in fam["pairs"].items():
        a, b = pair.split("  ||  ")
        ea, eb = by_name(a), by_name(b)
        verdicts = collections.Counter(it["bucket"] for it in res["items"])
        attr_fields = set()
        tz_only = True
        for it in res["items"]:
            for x in ea[it["name"]]:
                pass
            xa = ea[it["name"]][0]
            xb = eb[it["name"]][0]
            sa, sb = norm_attr(xa), norm_attr(xb)
            for k in sa:
                if sa[k] != sb[k]:
                    attr_fields.add(k)
                    if k in ("mtime_epoch", "ctime_epoch") and abs((sa[k] or 0) - (sb[k] or 0)) < 0.001:
                        continue
                    if k in ("mtime_epoch", "ctime_epoch"):
                        tz_only = False
        for k in attr_fields:
            summary["attr_kinds"][k] += 1
        overall = ("INSUFFICIENT" if verdicts.get("INSUFFICIENT") and not verdicts.get("IDENTICAL")
                   else "DIFFERENT" if verdicts.get("DIFFERENT")
                   else "IDENTICAL")
        summary["pairs"].append({
            "pair": pair, "verdict": overall,
            "n_identical": verdicts.get("IDENTICAL", 0),
            "n_different": verdicts.get("DIFFERENT", 0),
            "n_insufficient": verdicts.get("INSUFFICIENT", 0),
            "attr_fields": sorted(attr_fields),
            "order_changed": res["order_changed"],
        })
        summary["counts"][overall] += 1
        for it in res["items"]:
            if it["bucket"] == "DIFFERENT":
                summary["different"].append({"pair": pair, "name": it["name"],
                                             "key_a": it["key_a"], "key_b": it["key_b"]})

for f, r in ver.items():
    if r["status"] != "READ":
        summary["insufficient"].append({"archive": f, "reason": r["error"],
                                        "entries_visible": len(cat[f]["entries"])})

out = {
    "pair_count": len(summary["pairs"]),
    "pair_verdicts": dict(summary["counts"]),
    "attribute_field_changes": dict(summary["attr_kinds"]),
    "pairs": summary["pairs"],
    "verified_different_entries": summary["different"],
    "insufficient_archives": summary["insufficient"],
}
with open(os.path.join(WORK, "final.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)

print("pair verdicts:", dict(summary["counts"]), "of", len(summary["pairs"]), "pairs")
print("attribute field change frequency:", dict(summary["attr_kinds"]))
print("verified different entries:", len(summary["different"]))
print("insufficient archives:", [x["archive"] for x in summary["insufficient"]])
