#!/usr/bin/env python3
"""Build structured catalogs + verified-content evidence for all RAR test archives.

- Structured catalog: parsed via rarfile (names as multisets, sizes, CRC32/BLAKE2sp,
  mode/os/timestamps, compression, redirection/links, encryption, volumes).
- Verified evidence: actually extract each regular file with bsdtar and hash the
  bytes with SHA-256. Failures (solid RAR3, header encryption, volumes, ...) are
  recorded explicitly as NOT-READ so size/CRC agreement alone is never treated as
  content identity.
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import rarfile  # noqa: E402

FILES = os.path.join(ROOT, "test", "files")
PASSWORD = "password"

# Multi-volume sets must be opened from the first volume.
VOLUME_FIRST = {
    "rar3-vols.part2.rar": "rar3-vols.part1.rar",
    "rar3-vols.part3.rar": "rar3-vols.part1.rar",
    "rar5-vols.part2.rar": "rar5-vols.part1.rar",
    "rar5-vols.part3.rar": "rar5-vols.part1.rar",
    "rar3-old.r00": "rar3-old.rar",
    "rar3-old.r01": "rar3-old.rar",
}


def fmt_hash(b):
    return b.hex() if b else None


def catalog_archive(fname):
    path = os.path.join(FILES, fname)
    open_name = VOLUME_FIRST.get(fname, fname)
    open_path = os.path.join(FILES, open_name)
    cat = {
        "archive": fname,
        "opened_as": open_name,
        "format": None,
        "needs_password": None,
        "entries": [],
        "parse_error": None,
    }
    try:
        rf = rarfile.RarFile(open_path)
        cat["needs_password"] = rf.needs_password()
        infos = rf.infolist()
        cat["format"] = type(infos[0]).__name__ if infos else type(rf).__name__
        # only report entries belonging to this physical volume
        for idx, inf in enumerate(infos):
            vf = getattr(inf, "volume_file", None)
            if vf is not None and os.path.basename(vf) != fname and fname != open_name:
                continue
            redir = inf.file_redir
            entry = {
                "idx": idx,
                "name": inf.filename,
                "is_dir": bool(inf.is_dir()),
                "is_symlink": bool(inf.is_symlink()),
                "file_size": inf.file_size,
                "compress_size": inf.compress_size,
                "crc32": (inf.CRC or 0) and hex(inf.CRC),
                "blake2sp": fmt_hash(getattr(inf, "blake2sp_hash", None)),
                "host_os": inf.host_os,
                "mode": oct(inf.mode) if inf.mode is not None else None,
                "mtime": inf.mtime.isoformat() if getattr(inf, "mtime", None) else None,
                "ctime": inf.ctime.isoformat() if getattr(inf, "ctime", None) else None,
                "atime": inf.atime.isoformat() if getattr(inf, "atime", None) else None,
                "compress_type": inf.compress_type,
                "needs_password": bool(inf.needs_password),
                "encrypted": bool(inf.needs_password),
                "redir": (
                    {"type": redir[0], "flags": redir[1], "target": redir[2]}
                    if redir else None
                ),
                "volume": getattr(inf, "volume", None),
            }
            cat["entries"].append(entry)
    except Exception as exc:  # noqa: BLE001
        cat["parse_error"] = "%s: %s" % (type(exc).__name__, exc)
    return cat


def bsdtar_read(fname, name, encrypted):
    """Extract one entry's bytes via bsdtar; return (sha256, nbytes, status, detail)."""
    path = os.path.join(FILES, VOLUME_FIRST.get(fname, fname))
    cmd = ["bsdtar"]
    if encrypted:
        cmd += ["--passphrase", PASSWORD]
    cmd += ["-xOf", path, name]
    proc = subprocess.run(cmd, capture_output=True)
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return None, 0, "UNREAD", err or ("exit %d" % proc.returncode)
    data = proc.stdout
    return hashlib.sha256(data).hexdigest(), len(data), "READ", err


def main():
    fnames = sorted(
        f for f in os.listdir(FILES)
        if not f.endswith(".exp") and os.path.isfile(os.path.join(FILES, f))
    )
    out = {}
    for fname in fnames:
        cat = catalog_archive(fname)
        for ent in cat["entries"]:
            ent["content"] = None
            if cat["parse_error"]:
                ent["content"] = {"status": "UNREAD", "reason": cat["parse_error"]}
                continue
            if ent["is_dir"]:
                ent["content"] = {"status": "DIR", "reason": "directory, no bytes"}
                continue
            if ent["redir"] is not None and ent["redir"]["type"] in (1, 2, 3):
                # symlink / junction: identity is the link target string in header
                ent["content"] = {
                    "status": "LINK",
                    "reason": "redirection target",
                    "target": ent["redir"]["target"],
                }
                continue
            if ent["redir"] is not None and ent["redir"]["type"] in (4, 5):
                # hard link / file copy: no own bytes, resolves to target entry
                ent["content"] = {
                    "status": "ALIAS",
                    "reason": "hardlink/file-copy",
                    "target": ent["redir"]["target"],
                }
                continue
            sha, nbytes, status, detail = bsdtar_read(
                VOLUME_FIRST.get(fname, fname), ent["name"], ent["encrypted"]
            )
            ent["content"] = {
                "status": status,
                "sha256": sha,
                "nbytes": nbytes,
                "detail": detail,
            }
        out[fname] = cat
    dest = os.path.join(ROOT, ".analysis", "catalog.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("wrote", dest, "archives:", len(out))


if __name__ == "__main__":
    main()
