"""同构建判定工具：比对同路径、同显示版本且构建时间被置零的 PE 样本。

对每份样本提取四类证据：
  1. 版本资源（VS_FIXEDFILEINFO 与 StringFileInfo 字符串，语言标签单独记录）
  2. 调试标识（CodeView RSDS/NB10 的 GUID/签名、Age、PDB 路径）
  3. 指定代码范围（节名或 RVA/文件偏移范围的字节哈希）
  4. 签名身份（Authenticode 证书表哈希，仅作身份记录，不作为代码差异依据）

判定规则：
  - 版本资源、调试标识或指定代码范围任一不同        -> 有差异
  - 仅资源语言标签不同（内容一致）                  -> 资源语言差异（单列）
  - 以上证据一致                                    -> 同构建
  - 指定代码范围无法取得、且无其它可比证据          -> 证据不足
  - 签名变了而指定代码不变：仍判同构建，但两种签名身份都保留，
    不得仅凭整文件哈希变化就认定代码更新。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import pefile

VERDICT_SAME_BUILD = "同构建"
VERDICT_DIFFERENT = "有差异"
VERDICT_LANG_ONLY = "资源语言差异"
VERDICT_INSUFFICIENT = "证据不足"

FIXED_FILE_INFO_FIELDS = (
    "Signature",
    "StrucVersion",
    "FileVersionMS",
    "FileVersionLS",
    "ProductVersionMS",
    "ProductVersionLS",
    "FileFlagsMask",
    "FileFlags",
    "FileOS",
    "FileType",
    "FileSubtype",
    "FileDateMS",
    "FileDateLS",
)

SECURITY_DIRECTORY_INDEX = 4


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "backslashreplace")
    return value


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 代码范围解析
# ---------------------------------------------------------------------------


def parse_range_specs(spec_text):
    """解析 --ranges 参数。返回 [('section'|'rva'|'raw', ...), ...]。"""
    specs = []
    for item in spec_text.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("rva:") or item.startswith("raw:"):
            kind = item[:3]
            bounds = item[4:].split("-")
            if len(bounds) != 2:
                raise ValueError(f"无法解析范围: {item}")
            start = int(bounds[0], 0)
            end = int(bounds[1], 0)
            if end <= start:
                raise ValueError(f"范围终点必须大于起点: {item}")
            specs.append((kind, start, end))
        else:
            specs.append(("section", item))
    return specs


def _default_specs(pe):
    """未指定范围时，默认取所有可执行节。"""
    specs = []
    for section in pe.sections:
        if section.Characteristics & 0x20000000:  # IMAGE_SCN_MEM_EXECUTE
            name = section.Name.rstrip(b"\0").decode("ascii", "backslashreplace")
            specs.append(("section", name))
    return specs


def _resolve_ranges(pe, raw, specs):
    """把范围规格解析为 (名称, 字节)。无法解析的记入 missing。"""
    resolved = []
    missing = []
    for spec in specs:
        kind = spec[0]
        if kind == "section":
            name = spec[1]
            target = name.encode("ascii", "backslashreplace")
            section = None
            for sec in pe.sections:
                if sec.Name.rstrip(b"\0") == target:
                    section = sec
                    break
            if section is None:
                missing.append(f"节 {name} 不存在")
                continue
            data = section.get_data()
            resolved.append(
                {
                    "name": f"section:{name}",
                    "rva": section.VirtualAddress,
                    "size": len(data),
                    "sha256": _sha256(data),
                }
            )
        elif kind == "rva":
            start, end = spec[1], spec[2]
            try:
                data = pe.get_data(start, end - start)
            except pefile.PEFormatError:
                data = None
            if data is None or len(data) != end - start:
                missing.append(f"RVA 范围 {start:#x}-{end:#x} 不可读")
                continue
            resolved.append(
                {
                    "name": f"rva:{start:#x}-{end:#x}",
                    "rva": start,
                    "size": len(data),
                    "sha256": _sha256(data),
                }
            )
        else:  # raw
            start, end = spec[1], spec[2]
            data = raw[start:end]
            if len(data) != end - start:
                missing.append(f"文件偏移范围 {start:#x}-{end:#x} 不可读")
                continue
            resolved.append(
                {
                    "name": f"raw:{start:#x}-{end:#x}",
                    "rva": None,
                    "size": len(data),
                    "sha256": _sha256(data),
                }
            )
    return resolved, missing


# ---------------------------------------------------------------------------
# 证据提取
# ---------------------------------------------------------------------------


def _extract_version(pe):
    if not hasattr(pe, "VS_FIXEDFILEINFO") or not pe.VS_FIXEDFILEINFO:
        return None
    fixed = {}
    ffi = pe.VS_FIXEDFILEINFO[0]
    for field_name in FIXED_FILE_INFO_FIELDS:
        fixed[field_name] = getattr(ffi, field_name, None)

    string_tables = {}
    for file_info in getattr(pe, "FileInfo", []) or []:
        for info in file_info:
            if getattr(info, "Key", None) != b"StringFileInfo":
                continue
            for table in getattr(info, "StringTable", []) or []:
                lang = _decode(getattr(table, "LangID", b""))
                entries = {}
                for key, value in (getattr(table, "entries", {}) or {}).items():
                    entries[_decode(key)] = _decode(value)
                string_tables[lang] = entries

    # 语言无关的内容视图：所有语言表的 (键, 值) 排序列表
    content = sorted(
        (key, value)
        for entries in string_tables.values()
        for key, value in entries.items()
    )
    return {
        "fixed": fixed,
        "string_tables": string_tables,
        "languages": sorted(string_tables.keys()),
        "content": content,
    }


def _extract_debug(pe):
    entries = []
    for debug in getattr(pe, "DIRECTORY_ENTRY_DEBUG", []) or []:
        struct = debug.struct
        entry = getattr(debug, "entry", None)
        record = {"type": struct.Type, "guid": None, "age": None, "pdb": None}
        if entry is not None and struct.Type == 2:
            if hasattr(entry, "Signature_Data1"):
                record["guid"] = (
                    f"{entry.Signature_Data1:08X}"
                    f"{entry.Signature_Data2:04X}"
                    f"{entry.Signature_Data3:04X}"
                    f"{entry.Signature_Data4:02X}"
                    f"{entry.Signature_Data5:02X}"
                    f"{entry.Signature_Data6.hex().upper()}"
                )
                record["age"] = entry.Age
                record["pdb"] = _decode(
                    getattr(entry, "PdbFileName", b"").rstrip(b"\0")
                )
            elif hasattr(entry, "Signature"):  # NB10
                record["guid"] = f"{entry.Signature:08X}"
                record["age"] = entry.Age
                record["pdb"] = _decode(
                    getattr(entry, "PdbFileName", b"").rstrip(b"\0")
                )
        entries.append(record)
    return entries


def _extract_signature(pe, raw):
    directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[SECURITY_DIRECTORY_INDEX]
    offset = directory.VirtualAddress  # 安全目录的 VirtualAddress 是文件偏移
    size = directory.Size
    if not offset or not size:
        return {"present": False, "sha256": None, "size": 0}
    blob = raw[offset : offset + size]
    return {"present": True, "sha256": _sha256(blob), "size": len(blob)}


def _extract_resource_languages(pe):
    languages = set()
    resource_dir = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if resource_dir is None:
        return []
    for res_type in resource_dir.entries:
        type_id = getattr(res_type, "id", None)
        type_dir = getattr(res_type, "directory", None)
        if type_dir is None:
            continue
        for res_name in type_dir.entries:
            name_id = getattr(res_name, "id", None)
            sub = getattr(res_name, "directory", None)
            if sub is None:
                continue
            for res_lang in sub.entries:
                data = getattr(res_lang, "data", None)
                if data is None:
                    continue
                languages.add(
                    f"type={type_id}/name={name_id}/"
                    f"lang={data.lang:#x}/sublang={data.sublang:#x}"
                )
    return sorted(languages)


def extract_identity(path, range_specs=None):
    """提取一份 PE 样本的比对身份。"""
    with open(path, "rb") as handle:
        raw = handle.read()
    pe = pefile.PE(data=raw, fast_load=False)

    specs = range_specs if range_specs else _default_specs(pe)
    code_ranges, code_missing = _resolve_ranges(pe, raw, specs)

    return {
        "path": path,
        "file_sha256": _sha256(raw),
        "file_size": len(raw),
        "code_ranges": code_ranges,
        "code_ranges_missing": code_missing,
        "version": _extract_version(pe),
        "debug": _extract_debug(pe),
        "signature": _extract_signature(pe, raw),
        "resource_languages": _extract_resource_languages(pe),
        "warnings": list(pe.get_warnings()),
    }


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------


def compare_identities(a, b):
    """比对两份身份，返回判定结果。"""
    differences = []
    evidence_missing = []
    compared = {"code": [], "version": False, "debug": False}

    # 1. 指定代码范围
    a_ranges = {item["name"]: item for item in a["code_ranges"]}
    b_ranges = {item["name"]: item for item in b["code_ranges"]}
    for name in sorted(a_ranges.keys() & b_ranges.keys()):
        compared["code"].append(name)
        if a_ranges[name]["sha256"] != b_ranges[name]["sha256"]:
            differences.append(f"代码范围 {name} 不同")
    for name in sorted(a_ranges.keys() ^ b_ranges.keys()):
        evidence_missing.append(f"代码范围 {name} 仅一方可取到")
    for note in a["code_ranges_missing"] + b["code_ranges_missing"]:
        evidence_missing.append(f"代码范围缺失: {note}")

    # 2. 版本资源（语言标签不参与内容比对）
    language_difference = None
    if a["version"] is not None and b["version"] is not None:
        compared["version"] = True
        if a["version"]["fixed"] != b["version"]["fixed"]:
            differences.append("版本资源固定信息(VS_FIXEDFILEINFO)不同")
        if a["version"]["content"] != b["version"]["content"]:
            differences.append("版本资源字符串内容不同")
        lang_tags_a = set(a["version"]["languages"])
        lang_tags_b = set(b["version"]["languages"])
        res_lang_a = set(a["resource_languages"])
        res_lang_b = set(b["resource_languages"])
        if lang_tags_a != lang_tags_b or res_lang_a != res_lang_b:
            language_difference = {
                "string_table_languages": {
                    "a": sorted(lang_tags_a),
                    "b": sorted(lang_tags_b),
                },
                "resource_languages": {
                    "a": sorted(res_lang_a),
                    "b": sorted(res_lang_b),
                },
            }
    elif a["version"] is not None or b["version"] is not None:
        evidence_missing.append("版本资源仅一方存在")

    # 3. 调试标识
    if a["debug"] and b["debug"]:
        compared["debug"] = True
        key = ("type", "guid", "age", "pdb")
        ids_a = sorted(tuple(item[k] for k in key) for item in a["debug"])
        ids_b = sorted(tuple(item[k] for k in key) for item in b["debug"])
        if ids_a != ids_b:
            differences.append(
                f"调试标识不同: {ids_a} vs {ids_b}"
            )
    elif a["debug"] or b["debug"]:
        evidence_missing.append("调试信息仅一方存在")

    # 4. 签名身份：只记录，不作为代码差异依据
    sig_a = a["signature"]
    sig_b = b["signature"]
    signature_note = None
    if sig_a["present"] and sig_b["present"]:
        if sig_a["sha256"] != sig_b["sha256"]:
            signature_note = "签名不同，两种身份均保留"
    elif sig_a["present"] != sig_b["present"]:
        signature_note = "仅一方有签名，两种身份均保留"

    # 判定
    if differences:
        verdict = VERDICT_DIFFERENT
    elif not compared["code"]:
        verdict = VERDICT_INSUFFICIENT
    elif language_difference is not None:
        verdict = VERDICT_LANG_ONLY
    else:
        verdict = VERDICT_SAME_BUILD

    return {
        "a": a["path"],
        "b": b["path"],
        "verdict": verdict,
        "differences": differences,
        "language_difference": language_difference,
        "signature": {
            "note": signature_note,
            "a": sig_a["sha256"] if sig_a["present"] else None,
            "b": sig_b["sha256"] if sig_b["present"] else None,
        },
        "evidence_missing": evidence_missing,
        "compared": compared,
        "file_sha256": {"a": a["file_sha256"], "b": b["file_sha256"]},
    }


# ---------------------------------------------------------------------------
# 汇总报告
# ---------------------------------------------------------------------------


def _union_find_groups(paths, same_pairs):
    parent = {p: p for p in paths}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for x, y in same_pairs:
        parent[find(x)] = find(y)

    groups = {}
    for p in paths:
        groups.setdefault(find(p), []).append(p)
    return [sorted(members) for members in groups.values() if len(members) > 1]


def summarize(identities, pairs):
    same = [p for p in pairs if p["verdict"] == VERDICT_SAME_BUILD]
    different = [p for p in pairs if p["verdict"] == VERDICT_DIFFERENT]
    lang_only = [p for p in pairs if p["verdict"] == VERDICT_LANG_ONLY]
    insufficient = [p for p in pairs if p["verdict"] == VERDICT_INSUFFICIENT]

    paths = [item["path"] for item in identities]
    groups = _union_find_groups(paths, [(p["a"], p["b"]) for p in same])

    sig_by_path = {}
    for item in identities:
        sig = item["signature"]
        sig_by_path[item["path"]] = sig["sha256"] if sig["present"] else None

    return {
        "同构建组": [
            {
                "members": members,
                "签名身份": {m: sig_by_path[m] for m in members},
            }
            for members in groups
        ],
        "有差异": different,
        "资源语言差异": lang_only,
        "证据不足": insufficient,
    }


def format_report(summary):
    lines = []
    lines.append("== 同构建 ==")
    if summary["同构建组"]:
        for idx, group in enumerate(summary["同构建组"], 1):
            lines.append(f"组{idx}: " + ", ".join(group["members"]))
            identities = group["签名身份"]
            distinct = {v for v in identities.values() if v}
            if len(distinct) > 1 or any(v is None for v in identities.values()):
                lines.append("  签名身份（均保留）:")
                for member, sig in identities.items():
                    lines.append(f"    {member}: {sig or '未签名'}")
    else:
        lines.append("（无）")

    lines.append("")
    lines.append("== 有差异 ==")
    if summary["有差异"]:
        for pair in summary["有差异"]:
            lines.append(f"{pair['a']}  vs  {pair['b']}")
            for diff in pair["differences"]:
                lines.append(f"  - {diff}")
    else:
        lines.append("（无）")

    lines.append("")
    lines.append("== 资源语言差异（单列） ==")
    if summary["资源语言差异"]:
        for pair in summary["资源语言差异"]:
            lines.append(f"{pair['a']}  vs  {pair['b']}")
            lang = pair["language_difference"] or {}
            st = lang.get("string_table_languages", {})
            lines.append(f"  字符串表语言: {st.get('a')} vs {st.get('b')}")
    else:
        lines.append("（无）")

    lines.append("")
    lines.append("== 证据不足 ==")
    if summary["证据不足"]:
        for pair in summary["证据不足"]:
            lines.append(f"{pair['a']}  vs  {pair['b']}")
            for note in pair["evidence_missing"]:
                lines.append(f"  - {note}")
    else:
        lines.append("（无）")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="同路径同版本号 PE 样本的同构建判定"
    )
    parser.add_argument("files", nargs="+", help="待比对的 PE 文件")
    parser.add_argument(
        "--ranges",
        default=None,
        help="指定代码范围，逗号分隔：节名(如 .text)、rva:0x1000-0x1200、"
        "raw:0x200-0x400；缺省为全部可执行节",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="JSON 报告输出路径")
    args = parser.parse_args(argv)

    range_specs = parse_range_specs(args.ranges) if args.ranges else None

    identities = [extract_identity(path, range_specs) for path in args.files]

    pairs = []
    for i in range(len(identities)):
        for j in range(i + 1, len(identities)):
            pairs.append(compare_identities(identities[i], identities[j]))

    summary = summarize(identities, pairs)
    print(format_report(summary))

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
