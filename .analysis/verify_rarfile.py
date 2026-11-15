#!/usr/bin/env python3
"""Authoritative verified-content evidence using rarfile's own decompressor.

rarfile's RarFile.read() verifies CRC32 (and BLAKE2sp where present) on every
member and raises BadRarFile on mismatch, so a produced SHA-256 is backed by an
actual successful read+hash check rather than by header size/CRC alone.
"""
import hashlib
import json
import os
import stat
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import rarfile  # noqa: E402

FILES = os.path.join(ROOT, "test", "files")
WORK = os.path.join(ROOT, ".analysis")
PASSWORD = "password"

VOLUME_FIRST = {
    "rar3-vols.part2.rar": "rar3-vols.part1.rar",
    "rar3-vols.part3.rar": "rar3-vols.part1.rar",
    "rar5-vols.part2.rar": "rar5-vols.part1.rar",
    "rar5-vols.part3.rar": "rar5-vols.part1.rar",
    "rar3-old.r00": "rar3-old.rar",
    "rar3-old.r01": "rar3-old.rar",
}


def main():
    cat = json.load(open(os.path.join(WORK, "catalog.json"), encoding="utf-8"))
    verified = {}
    for fname, c in sorted(cat.items()):
        rec = {"status": None, "members": {}, "error": None}
        if c["parse_error"]:
            rec["status"] = "UNREAD"
            rec["error"] = c["parse_error"]
            verified[fname] = rec
            continue
        try:
            rf = rarfile.RarFile(os.path.join(FILES, c["opened_as"]))
            if rf.needs_password():
                rf.setpassword(PASSWORD)
            for ent in c["entries"]:
                name = ent["name"]
                if ent["is_dir"]:
                    rec["members"][name] = {"kind": "DIR"}
                    continue
                if ent["redir"] is not None:
                    rec["members"][name] = {
                        "kind": "REDIR",
                        "rtype": ent["redir"]["type"],
                        "target": ent["redir"]["target"],
                    }
                    continue
                data = rf.read(name)  # CRC32/BLAKE2sp verified, raises on mismatch
                if ent["mode"] and stat.S_ISLNK(int(ent["mode"], 8)):
                    # RAR3 stores a symlink as an S_IFLNK regular file whose bytes
                    # are the link target; rf.read() returns those CRC-checked bytes.
                    rec["members"][name] = {
                        "kind": "LINK",
                        "target": data.decode("utf-8", "surrogateescape"),
                        "sha256_target": hashlib.sha256(data).hexdigest(),
                    }
                    continue
                rec["members"][name] = {
                    "kind": "FILE",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "nbytes": len(data),
                }
            rec["status"] = "READ"
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "UNREAD"
            rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        verified[fname] = rec
    with open(os.path.join(WORK, "verified.json"), "w", encoding="utf-8") as fh:
        json.dump(verified, fh, ensure_ascii=False, indent=1)
    n_read = sum(1 for r in verified.values() if r["status"] == "READ")
    print("verified READ=%d UNREAD=%d total=%d" % (n_read, len(verified) - n_read, len(verified)))
    for f, r in sorted(verified.items()):
        if r["status"] != "READ":
            print("  UNREAD", f, "->", r["error"])


if __name__ == "__main__":
    main()
