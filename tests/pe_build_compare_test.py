"""pe_build_compare 的测试：手工构造最小 PE，覆盖各判定分类。"""

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pe_build_compare


# ---------------------------------------------------------------------------
# 最小 PE 构造器
# ---------------------------------------------------------------------------

TEXT_RVA = 0x1000
TEXT_RAW = 0x200
RSRC_RVA = 0x2000
RSRC_RAW = 0x400
RDATA_RVA = 0x3000
RDATA_RAW = 0x600
CERT_OFFSET = 0x800


def _u16z(text):
    return text.encode("utf-16-le") + b"\x00\x00"


def _pad4(data, base):
    return data + b"\x00" * (-(base + len(data)) % 4)


def _ver_string(key, value, base):
    key_bytes = _pad4(_u16z(key), base + 6)
    val_bytes = _u16z(value)
    length = 6 + len(key_bytes) + len(val_bytes)
    return struct.pack("<HHH", length, len(value) + 1, 1) + key_bytes + val_bytes


def _ver_string_table(lang, pairs, base):
    key_bytes = _pad4(_u16z(lang), base + 6)
    body = bytearray(key_bytes)
    offset = base + 6 + len(key_bytes)
    for key, value in pairs:
        blob = _ver_string(key, value, offset)
        body += blob
        offset += len(blob)
        pad = -(offset) % 4
        body += b"\x00" * pad
        offset += pad
    return struct.pack("<HHH", 6 + len(body), 0, 1) + bytes(body)


def _ver_string_file_info(lang, pairs, base):
    key_bytes = _pad4(_u16z("StringFileInfo"), base + 6)
    table = _ver_string_table(lang, pairs, base + 6 + len(key_bytes))
    body = key_bytes + table
    return struct.pack("<HHH", 6 + len(body), 0, 1) + body


def _version_info(lang, pairs, file_version_ms, file_version_ls):
    key_bytes = _pad4(_u16z("VS_VERSION_INFO"), 6)
    ffi = struct.pack(
        "<13I",
        0xFEEF04BD,  # Signature
        0x00010000,  # StrucVersion
        file_version_ms,
        file_version_ls,
        file_version_ms,
        file_version_ls,
        0x3F,  # FileFlagsMask
        0,  # FileFlags
        0x40004,  # FileOS
        1,  # FileType
        0,  # FileSubtype
        0,
        0,
    )
    sfi_base = 6 + len(key_bytes) + len(ffi)
    sfi = _ver_string_file_info(lang, pairs, sfi_base)
    length = 6 + len(key_bytes) + len(ffi) + len(sfi)
    return struct.pack("<HHH", length, len(ffi), 1) + key_bytes + ffi + sfi


def _resource_section(lang, pairs, file_version_ms, file_version_ls, resource_lang):
    blob = _version_info(lang, pairs, file_version_ms, file_version_ls)
    data_rva = RSRC_RVA + 0x58
    section = bytearray(0x200)
    # 根目录：1 个 ID 条目（RT_VERSION=16）
    struct.pack_into("<IIHHHH", section, 0x00, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", section, 0x10, 16, 0x80000018)
    # 名称目录
    struct.pack_into("<IIHHHH", section, 0x18, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", section, 0x28, 1, 0x80000030)
    # 语言目录
    struct.pack_into("<IIHHHH", section, 0x30, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", section, 0x40, resource_lang, 0x48)
    # 数据条目
    struct.pack_into("<IIII", section, 0x48, data_rva, len(blob), 0, 0)
    section[0x58 : 0x58 + len(blob)] = blob
    return bytes(section)


def _rdata_section(guid, age, pdb):
    cv = (
        b"RSDS"
        + struct.pack(
            "<IHHBB6sI",
            guid,
            0x1234,
            0x5678,
            0x9A,
            0xBC,
            b"\x01\x02\x03\x04\x05\x06",
            age,
        )
        + pdb
        + b"\x00"
    )
    section = bytearray(0x200)
    struct.pack_into(
        "<IIHHIIII",
        section,
        0,
        0,  # Characteristics
        0,  # TimeDateStamp（置零，模拟统一清零的构建时间）
        0,
        0,
        2,  # Type = CODEVIEW
        len(cv),
        RDATA_RVA + 28,
        RDATA_RAW + 28,
    )
    section[28 : 28 + len(cv)] = cv
    return bytes(section)


def make_pe(
    *,
    code=b"\x90" * 64,
    strings=(("FileDescription", "demo"), ("FileVersion", "1.0")),
    string_lang="040904b0",
    resource_lang=0x409,
    file_version_ms=0x00010000,
    file_version_ls=0,
    debug=True,
    guid=0x11223344,
    age=1,
    pdb=b"demo.pdb",
    cert=None,
    include_version=True,
):
    headers = bytearray(0x200)
    headers[0:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x80)
    headers[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into(
        "<HHIIIHH", headers, 0x84, 0x14C, 3, 0, 0, 0, 0xE0, 0x0102
    )

    opt = bytearray(0xE0)
    struct.pack_into(
        "<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII",
        opt,
        0,
        0x10B,  # Magic PE32
        0,
        0,
        0x200,  # SizeOfCode
        0x400,  # SizeOfInitializedData
        0,
        TEXT_RVA,  # AddressOfEntryPoint
        TEXT_RVA,  # BaseOfCode
        RSRC_RVA,  # BaseOfData
        0x400000,  # ImageBase
        0x1000,  # SectionAlignment
        0x200,  # FileAlignment
        6,
        0,
        0,
        0,
        6,
        0,
        0,
        0x4000,  # SizeOfImage
        0x200,  # SizeOfHeaders
        0,
        3,  # Subsystem
        0,
        0x100000,
        0x1000,
        0x100000,
        0x1000,
        0,
        16,  # NumberOfRvaAndSizes
    )
    if include_version:
        struct.pack_into("<II", opt, 96 + 2 * 8, RSRC_RVA, 0x200)
    if debug:
        struct.pack_into("<II", opt, 96 + 6 * 8, RDATA_RVA, 28)
    if cert is not None:
        cert_table = struct.pack("<IHH", 8 + len(cert), 0x0200, 0x0002) + cert
        struct.pack_into("<II", opt, 96 + 4 * 8, CERT_OFFSET, len(cert_table))
    else:
        cert_table = b""
    headers[0x98 : 0x98 + 0xE0] = opt

    section_headers = (
        struct.pack(
            "<8sIIIIIIHHI",
            b".text\0\0\0",
            len(code),
            TEXT_RVA,
            0x200,
            TEXT_RAW,
            0,
            0,
            0,
            0,
            0x60000020,
        )
        + struct.pack(
            "<8sIIIIIIHHI",
            b".rsrc\0\0\0",
            0x200,
            RSRC_RVA,
            0x200,
            RSRC_RAW,
            0,
            0,
            0,
            0,
            0x40000040,
        )
        + struct.pack(
            "<8sIIIIIIHHI",
            b".rdata\0\0\0",
            0x200,
            RDATA_RVA,
            0x200,
            RDATA_RAW,
            0,
            0,
            0,
            0,
            0x40000040,
        )
    )
    headers[0x178 : 0x178 + len(section_headers)] = section_headers

    text = bytearray(0x200)
    text[: len(code)] = code
    rsrc = (
        _resource_section(
            string_lang, strings, file_version_ms, file_version_ls, resource_lang
        )
        if include_version
        else bytes(0x200)
    )
    rdata = _rdata_section(guid, age, pdb) if debug else bytes(0x200)

    image = bytes(headers) + bytes(text) + rsrc + rdata
    if cert_table:
        image += cert_table
    return image


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


@pytest.fixture()
def write_pe(tmp_path):
    counter = {"n": 0}

    def _write(data):
        counter["n"] += 1
        path = tmp_path / f"sample{counter['n']}.exe"
        path.write_bytes(data)
        return str(path)

    return _write


def _compare(path_a, path_b, ranges=None):
    specs = pe_build_compare.parse_range_specs(ranges) if ranges else None
    a = pe_build_compare.extract_identity(path_a, specs)
    b = pe_build_compare.extract_identity(path_b, specs)
    return pe_build_compare.compare_identities(a, b)


def test_same_build_only_signature_changed(write_pe):
    path_a = write_pe(make_pe(cert=b"CERT-A"))
    path_b = write_pe(make_pe(cert=b"CERT-B"))
    result = _compare(path_a, path_b)
    # 整文件哈希不同，但不得据此认定代码更新
    assert result["file_sha256"]["a"] != result["file_sha256"]["b"]
    assert result["verdict"] == pe_build_compare.VERDICT_SAME_BUILD
    # 两种签名身份都保留
    assert result["signature"]["note"] is not None
    assert result["signature"]["a"] != result["signature"]["b"]
    assert result["signature"]["a"] is not None
    assert result["signature"]["b"] is not None


def test_same_build_signed_vs_unsigned(write_pe):
    path_a = write_pe(make_pe(cert=b"CERT-A"))
    path_b = write_pe(make_pe(cert=None))
    result = _compare(path_a, path_b)
    assert result["verdict"] == pe_build_compare.VERDICT_SAME_BUILD
    assert result["signature"]["note"] is not None


def test_code_difference(write_pe):
    path_a = write_pe(make_pe(code=b"\x90" * 64))
    path_b = write_pe(make_pe(code=b"\x91" * 64))
    result = _compare(path_a, path_b)
    assert result["verdict"] == pe_build_compare.VERDICT_DIFFERENT
    assert any("代码范围" in diff for diff in result["differences"])


def test_debug_identifier_difference(write_pe):
    path_a = write_pe(make_pe(guid=0x11223344))
    path_b = write_pe(make_pe(guid=0xAABBCCDD))
    result = _compare(path_a, path_b)
    assert result["verdict"] == pe_build_compare.VERDICT_DIFFERENT
    assert any("调试标识" in diff for diff in result["differences"])


def test_version_resource_difference(write_pe):
    path_a = write_pe(make_pe(strings=(("FileDescription", "demo"),)))
    path_b = write_pe(make_pe(strings=(("FileDescription", "demo2"),)))
    result = _compare(path_a, path_b)
    assert result["verdict"] == pe_build_compare.VERDICT_DIFFERENT
    assert any("版本资源" in diff for diff in result["differences"])


def test_fixed_file_info_difference(write_pe):
    path_a = write_pe(make_pe(file_version_ms=0x00010000))
    path_b = write_pe(make_pe(file_version_ms=0x00020000))
    result = _compare(path_a, path_b)
    assert result["verdict"] == pe_build_compare.VERDICT_DIFFERENT
    assert any("VS_FIXEDFILEINFO" in diff for diff in result["differences"])


def test_resource_language_only_difference(write_pe):
    path_a = write_pe(make_pe(string_lang="040904b0", resource_lang=0x409))
    path_b = write_pe(make_pe(string_lang="080404b0", resource_lang=0x804))
    result = _compare(path_a, path_b)
    assert result["verdict"] == pe_build_compare.VERDICT_LANG_ONLY
    assert result["language_difference"] is not None


def test_insufficient_evidence_when_code_range_missing(write_pe):
    path_a = write_pe(make_pe())
    path_b = write_pe(make_pe())
    result = _compare(path_a, path_b, ranges="rva:0x9000-0x9100")
    assert result["verdict"] == pe_build_compare.VERDICT_INSUFFICIENT
    assert result["evidence_missing"]


def test_extract_identity_contents(write_pe):
    path = write_pe(make_pe(cert=b"CERT-X"))
    identity = pe_build_compare.extract_identity(path)
    assert identity["version"]["fixed"]["FileVersionMS"] == 0x00010000
    assert identity["version"]["string_tables"]["040904b0"]["FileDescription"] == "demo"
    assert identity["version"]["languages"] == ["040904b0"]
    assert identity["debug"][0]["guid"].startswith("11223344")
    assert identity["debug"][0]["age"] == 1
    assert identity["debug"][0]["pdb"] == "demo.pdb"
    assert identity["signature"]["present"] is True
    assert identity["code_ranges"]
    assert identity["resource_languages"]


def test_explicit_section_and_raw_ranges(write_pe):
    path_a = write_pe(make_pe())
    path_b = write_pe(make_pe())
    result = _compare(path_a, path_b, ranges=".text,raw:0x200-0x240")
    assert result["verdict"] == pe_build_compare.VERDICT_SAME_BUILD
    assert set(result["compared"]["code"]) == {
        "section:.text",
        "raw:0x200-0x240",
    }


def test_summarize_groups(write_pe):
    path_a = write_pe(make_pe(cert=b"CERT-A"))
    path_b = write_pe(make_pe(cert=b"CERT-B"))
    path_c = write_pe(make_pe(code=b"\x91" * 64))
    identities = [pe_build_compare.extract_identity(p) for p in (path_a, path_b, path_c)]
    pairs = [
        pe_build_compare.compare_identities(identities[i], identities[j])
        for i in range(3)
        for j in range(i + 1, 3)
    ]
    summary = pe_build_compare.summarize(identities, pairs)
    assert len(summary["同构建组"]) == 1
    assert summary["同构建组"][0]["members"] == [path_a, path_b]
    # 同构建组内两种签名身份都保留
    sigs = summary["同构建组"][0]["签名身份"]
    assert sigs[path_a] != sigs[path_b]
    assert len(summary["有差异"]) == 2
    report = pe_build_compare.format_report(summary)
    assert "同构建" in report and "有差异" in report
