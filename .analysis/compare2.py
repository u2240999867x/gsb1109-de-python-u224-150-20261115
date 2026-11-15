#!/usr/bin/env python3
"""Normalized two-vendor backup comparison with three explicit buckets.

Logical content key (independent of container representation):
  regular file            -> ('FILE',  verified sha256)
  symlink/junction        -> ('LINK',  target path string)
  hardlink / file-copy    -> ('ALIAS', resolved target file sha256)
  directory               -> ('DIR',)
Identity requires an actually read, CRC32/BLAKE2sp-verified key. Unreadable
members go to INSUFFICIENT; header size/CRC agreement alone never promotes them.
Attribute deltas (OS, mode, timestamps, compression, encryption, representation,
format/version, entry order) are reported separately from the content bucket.
"""
import collections
import hashlib
import json
import os
import stat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, ".analysis")
cat = json.load(open(os.path.join(WORK, "catalog.json"), encoding="utf-8"))
ver = json.load(open(os.path.join(WORK, "verified.json"), encoding="utf-8"))

SYMLINK_REDIR = {1, 2, 3}
ALIAS_REDIR = {4, 5}

families = collections.defaultdict(list)
for fname, c in cat.items():
    if c["parse_error"]:
        continue
    key = hashlib.sha1(
        "\n".join(sorted(e["name"] for e in c["entries"])).encode()
    ).hexdigest()[:8]
    families[key].append(fname)


def entries_by_name(fname):
    out = collections.defaultdict(list)
    for ent in cat[fname]["entries"]:
        out[ent["name"]].append(ent)
    return out


def key_for(fname, ent):
    """Verified logical content key, or None when not readable."""
    if ver[fname]["status"] != "READ":
        return None
    m = ver[fname]["members"].get(ent["name"])
    if m is None:
        return None
    redir = ent["redir"]
    if ent["is_dir"]:
        return ("DIR",)
    if redir is not None and redir["type"] in SYMLINK_REDIR:
        return ("LINK", redir["target"])
    if redir is not None and redir["type"] in ALIAS_REDIR:
        target = ver[fname]["members"].get(redir["target"])
        return ("FILE", target["sha256"]) if target and target["kind"] == "FILE" else None
    if m["kind"] == "LINK":  # RAR3 S_IFLNK carrying the target path bytes
        return ("LINK", m["target"])
    if m["kind"] == "FILE":
        return ("FILE", m["sha256"])
    return None


def attr_sig(ent):
    is_siflnk = bool(ent["mode"]) and stat.S_ISLNK(int(ent["mode"], 8))
    return {
        "host_os": ent["host_os"],
        "mode": ent["mode"],
        "mtime": ent["mtime"],
        "ctime": ent["ctime"],
        "compress_type": ent["compress_type"],
        "encrypted": ent["encrypted"],
        "crc32_header": ent["crc32"],
        "blake2sp_header": ent["blake2sp"],
        "file_size": ent["file_size"],
        "representation": (
            ("redir:%d" % ent["redir"]["type"]) if ent["redir"]
            else ("siflnk" if is_siflnk else ("dir" if ent["is_dir"] else "file"))
        ),
    }


def compare_pair(a, b):
    ea, eb = entries_by_name(a), entries_by_name(b)
    names = sorted(set(ea) | set(eb))
    res = {"only_in_a": [], "only_in_b": [], "items": []}
    for name in names:
        la, lb = ea.get(name, []), eb.get(name, [])
        if la and not lb:
            res["only_in_a"].append(name); continue
        if lb and not la:
            res["only_in_b"].append(name); continue
        for x, y in zip(la, lb):  # multiset pairing: duplicate names stay distinct
            ka, kb = key_for(a, x), key_for(b, y)
            if ka is None or kb is None:
                bucket, reason = "INSUFFICIENT", "not readable/verified"
            elif ka == kb:
                bucket, reason = "IDENTICAL", ""
            else:
                bucket, reason = "DIFFERENT", "verified content keys differ"
            sa, sb = attr_sig(x), attr_sig(y)
            delta = {k: [sa[k], sb[k]] for k in sa if sa[k] != sb[k]}
            res["items"].append({
                "name": name, "bucket": bucket, "reason": reason,
                "key_a": ka, "key_b": kb, "attr_delta": delta,
            })
    res["order_changed"] = [e["name"] for e in cat[a]["entries"]] != \
                           [e["name"] for e in cat[b]["entries"]]
    return res


report = {"families": {}}
for key, members in sorted(families.items()):
    fam = {"members": members, "pairs": {}}
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            fam["pairs"][members[i] + "  ||  " + members[j]] = compare_pair(members[i], members[j])
    report["families"][key] = fam

content_index = collections.defaultdict(list)
for fname, r in ver.items():
    if r["status"] != "READ":
        continue
    for name, m in r["members"].items():
        if m["kind"] == "FILE":
            content_index[m["sha256"]].append([fname, name, m["nbytes"]])
report["content_groups"] = {
    sha[:16]: occ for sha, occ in content_index.items() if len(occ) > 1
}
report["unread_archives"] = [
    [f, r["error"]] for f, r in ver.items() if r["status"] != "READ"
]

with open(os.path.join(WORK, "report2.json"), "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=1)

buck = collections.Counter()
for fam in report["families"].values():
    for r in fam["pairs"].values():
        for it in r["items"]:
            buck[it["bucket"]] += 1
print("buckets:", dict(buck))
print("unread:", [x[0] for x in report["unread_archives"]])
print("content groups >1:", len(report["content_groups"]))
