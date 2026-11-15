#! /usr/bin/env python3

"""Layered inventory of received RAR packages.

Classifies each received archive into layers:

- ``verified``       - directory (file header) fields readable and
                       verifiable without a password.
- ``needs_password`` - file contents and/or the directory itself
                       require a password.
- ``corrupt``        - archive is genuinely damaged.

Registry invariants:

- A failed password attempt never overwrites already verified metadata;
  it is only appended to the attempt log.
- An archive whose directory cannot be opened is registered with an
  unknown file count (null), never as zero files.
"""

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import rarfile
from rarfile.errors import Error, NotRarFile

#: Directory fields verifiable from plaintext headers.
DIRECTORY_FIELDS = (
    "filename",
    "file_size",
    "compress_size",
    "mtime",
    "crc32",
    "host_os",
    "is_dir",
    "needs_password",
)

STATUS_VERIFIED = "verified"
STATUS_NEEDS_PASSWORD = "needs_password"
STATUS_CORRUPT = "corrupt"

ENC_NONE = "none"
ENC_CONTENT = "content"
ENC_HEADERS = "headers"

CHECK_OK = "ok"
CHECK_NEEDS_PASSWORD = "skipped_needs_password"
CHECK_UNKNOWN = "unknown"

OS_LIST = ["DOS", "OS2", "WIN", "UNIX", "MACOS", "BEOS"]

REGISTRY_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _password_fingerprint(password: str) -> str:
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return digest[:16]


def _has_header_encryption(rf: rarfile.RarFile) -> bool:
    parser = getattr(rf, "_file_parser", None)
    return bool(parser and parser.has_header_encryption())


def _read_entries(rf: rarfile.RarFile) -> list[dict]:
    """Read verifiable directory fields for all entries."""
    entries = []
    for info in rf.infolist():
        entries.append({
            "filename": info.filename,
            "file_size": info.file_size,
            "compress_size": info.compress_size,
            "mtime": info.mtime.isoformat() if info.mtime else None,
            "crc32": f"{info.CRC:08x}" if isinstance(info.CRC, int) else None,
            "host_os": OS_LIST[info.host_os] if 0 <= info.host_os < len(OS_LIST) else str(info.host_os),
            "is_dir": info.isdir(),
            "needs_password": info.needs_password(),
        })
    return entries


@dataclass
class ArchiveRecord:
    """Registry record for one received archive."""

    path: str
    status: str = STATUS_NEEDS_PASSWORD
    encryption: str | None = None
    file_count: int | None = None
    entries: list[dict] | None = None
    comment: str | None = None
    content_check: str = CHECK_UNKNOWN
    error: str | None = None
    password_attempts: list[dict] = field(default_factory=list)
    updated: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ArchiveRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def directory_verified(self) -> bool:
        return self.entries is not None


class Inventory:
    """Registry of received archives, persisted as JSON."""

    def __init__(self, registry_path: str | Path):
        self.registry_path = Path(registry_path)
        self.records: dict[str, ArchiveRecord] = {}
        self.load()

    def load(self) -> None:
        if not self.registry_path.exists():
            return
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.records = {
            path: ArchiveRecord.from_dict(rec)
            for path, rec in data.get("archives", {}).items()
        }

    def save(self) -> None:
        data = {
            "version": REGISTRY_VERSION,
            "archives": {path: asdict(rec) for path, rec in sorted(self.records.items())},
        }
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.registry_path)

    def _get(self, path: str | Path) -> ArchiveRecord:
        key = str(path)
        rec = self.records.get(key)
        if rec is None:
            rec = ArchiveRecord(path=key)
            self.records[key] = rec
        return rec

    def scan_archive(self, path: str | Path) -> ArchiveRecord:
        """Classify one archive without any password.

        Directory fields that parse without a password are stored as
        verified metadata.  A directory that cannot be opened keeps
        ``file_count=None`` / ``entries=None`` -- never zero files.
        """
        rec = self._get(path)
        rec.updated = _utcnow()
        try:
            rf = rarfile.RarFile(path, errors="strict")
        except NotRarFile as ex:
            rec.status = STATUS_CORRUPT
            rec.encryption = None
            rec.error = f"NotRarFile: {ex}"
            rec.content_check = CHECK_UNKNOWN
            return rec
        except Error as ex:
            rec.status = STATUS_CORRUPT
            rec.encryption = None
            rec.error = f"{type(ex).__name__}: {ex}"
            rec.content_check = CHECK_UNKNOWN
            return rec

        with rf:
            rec.error = None
            if _has_header_encryption(rf):
                # Directory itself is encrypted: nothing here is verifiable.
                rec.status = STATUS_NEEDS_PASSWORD
                rec.encryption = ENC_HEADERS
                rec.file_count = None
                rec.entries = None
                rec.comment = None
                rec.content_check = CHECK_NEEDS_PASSWORD
                return rec

            entries = _read_entries(rf)
            rec.entries = entries
            rec.file_count = len(entries)
            rec.comment = rf.comment
            if rf.needs_password():
                # Directory verified, file contents still need a password.
                rec.status = STATUS_NEEDS_PASSWORD
                rec.encryption = ENC_CONTENT
                rec.content_check = CHECK_NEEDS_PASSWORD
            else:
                rec.encryption = ENC_NONE
                bad = rf.testrar()
                if bad is None:
                    rec.status = STATUS_VERIFIED
                    rec.content_check = CHECK_OK
                else:
                    rec.status = STATUS_CORRUPT
                    rec.error = f"content CRC failed: {bad}"
                    rec.content_check = f"corrupt:{bad}"
        return rec

    def scan_paths(self, paths: list[str]) -> list[ArchiveRecord]:
        """Scan files and/or directories of received packages."""
        records = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                for child in sorted(p.iterdir()):
                    if child.is_file():
                        records.append(self.scan_archive(child))
            else:
                records.append(self.scan_archive(p))
        return records

    def try_password(self, path: str | Path, password: str) -> bool:
        """Try a password against one archive.

        The password is accepted only after the directory parses and all
        file contents pass their CRC checks.  A failed attempt is logged
        but never overwrites already verified metadata.
        """
        rec = self._get(path)
        rec.updated = _utcnow()
        attempt = {
            "time": rec.updated,
            "password_sha256": _password_fingerprint(password),
            "ok": False,
        }
        try:
            with rarfile.RarFile(path, errors="strict") as rf:
                rf.setpassword(password)
                entries = _read_entries(rf)
                bad = rf.testrar()
                if bad is not None:
                    raise Error(f"content CRC failed: {bad}")
                comment = rf.comment
        except Error as ex:
            attempt["error"] = f"{type(ex).__name__}: {ex}"
            rec.password_attempts.append(attempt)
            return False

        attempt["ok"] = True
        rec.password_attempts.append(attempt)
        # Password validated: safe to merge freshly decrypted metadata.
        rec.entries = entries
        rec.file_count = len(entries)
        rec.comment = comment
        rec.status = STATUS_VERIFIED
        rec.content_check = CHECK_OK
        rec.error = None
        if rec.encryption is None:
            rec.encryption = ENC_HEADERS if _has_header_encryption(rf) else ENC_CONTENT
        return True


def _layer_of(rec: ArchiveRecord) -> tuple[int, str]:
    if rec.status == STATUS_CORRUPT:
        return 3, "corrupt"
    if rec.status == STATUS_VERIFIED:
        return 1, "verified"
    if rec.encryption == ENC_CONTENT:
        return 2, "content"
    return 2, "headers"


def render_report(inv: Inventory) -> str:
    """Render layered report of collection results."""
    out = []
    records = [inv.records[k] for k in sorted(inv.records)]
    out.append(f"采集结果分层报告 (共 {len(records)} 个包)")
    out.append("=" * 48)

    verified = [r for r in records if _layer_of(r) == (1, "verified")]
    need_content = [r for r in records if _layer_of(r) == (2, "content")]
    need_headers = [r for r in records if _layer_of(r) == (2, "headers")]
    corrupt = [r for r in records if _layer_of(r) == (3, "corrupt")]

    out.append("")
    out.append(f"第1层 · 目录字段可验证, 无需密码 ({len(verified)} 个)")
    for r in verified:
        out.append(f"  {r.path}: {r.file_count} 个条目, 内容校验 {r.content_check}")
        out.append(f"    可验证字段: {', '.join(DIRECTORY_FIELDS)}")

    out.append("")
    out.append(f"第2层 · 需密码 ({len(need_content) + len(need_headers)} 个)")
    out.append(f"  [2a] 仅文件内容加密, 目录字段已验证 ({len(need_content)} 个)")
    for r in need_content:
        out.append(f"    {r.path}: {r.file_count} 个条目, 内容需密码")
    out.append(f"  [2b] 目录头也加密, 目录字段不可验证 ({len(need_headers)} 个)")
    for r in need_headers:
        out.append(f"    {r.path}: 条目数未知 (目录需密码, 未登记为零文件)")

    out.append("")
    out.append(f"第3层 · 确实损坏 ({len(corrupt)} 个)")
    for r in corrupt:
        out.append(f"  {r.path}: {r.error}")
    return "\n".join(out)


def render_record(inv: Inventory, path: str) -> str:
    """Render field-level verification status for one archive."""
    rec = inv.records.get(path)
    if rec is None:
        return f"{path}: 未登记"
    out = [f"{rec.path}: status={rec.status} encryption={rec.encryption}"]
    if rec.entries is None:
        for name in DIRECTORY_FIELDS:
            out.append(f"  {name}: 需密码 (未验证)")
        out.append("  file_count: 未知 (目录打不开, 未登记为零文件)")
    else:
        out.append(f"  file_count: {rec.file_count} (已验证)")
        for name in DIRECTORY_FIELDS:
            out.append(f"  {name}: 可验证")
        if rec.content_check == CHECK_NEEDS_PASSWORD:
            out.append("  file_content: 需密码")
        else:
            out.append(f"  file_content: {rec.content_check}")
    if rec.error:
        out.append(f"  error: {rec.error}")
    for att in rec.password_attempts:
        mark = "ok" if att.get("ok") else f"failed ({att.get('error', '')})"
        out.append(f"  password_attempt {att.get('password_sha256')}: {mark}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="collect_inventory",
        description="Layered inventory of received RAR packages.")
    ap.add_argument("--registry", default="inventory.json",
                    help="registry JSON file (default: inventory.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="scan received packages (no password)")
    sp.add_argument("paths", nargs="+", help="archive files or inbox directories")

    sp = sub.add_parser("try-password", help="try a password against one archive")
    sp.add_argument("archive")
    sp.add_argument("-p", "--password", required=True)

    sp = sub.add_parser("report", help="print layered report")

    sp = sub.add_parser("show", help="show field-level status of one archive")
    sp.add_argument("archive")

    args = ap.parse_args(argv)
    inv = Inventory(args.registry)

    if args.cmd == "scan":
        inv.scan_paths(args.paths)
        inv.save()
        print(render_report(inv))
    elif args.cmd == "try-password":
        ok = inv.try_password(args.archive, args.password)
        inv.save()
        print(render_record(inv, args.archive))
        return 0 if ok else 1
    elif args.cmd == "report":
        print(render_report(inv))
    elif args.cmd == "show":
        print(render_record(inv, args.archive))
    return 0


if __name__ == "__main__":
    sys.exit(main())
