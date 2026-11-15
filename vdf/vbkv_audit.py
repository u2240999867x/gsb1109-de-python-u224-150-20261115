"""
Read-only VBKV audit helpers.

A VBKV container is::

    offset 0..3   magic b'VBKV'
    offset 4..7   CRC32 checksum, little endian 32 bit (see vbkv_loads)
    offset 8..    payload: binary VDF using the alt-format end marker (0x0B)

The functions in this module inspect containers without ever modifying them
and without treating a failed checksum as valid.  The audit is reported in
three independent sections:

* ``header``   - magic and byte-range boundaries
* ``checksum`` - stored CRC32 plus an independently recomputed CRC32
* ``body``     - structural walk and library parse of the payload

Structurally complete payloads are still quarantined whenever the checksum
does not match.  Nothing here rewrites a bad checksum.
"""

import argparse
import hashlib
import json
import struct
import sys
from binascii import crc32
from io import BytesIO

import vdf

VBKV_MAGIC = b'VBKV'
HEADER_SIZE = 8
CRC_STRUCT = struct.Struct('<i')
ALT_END_MARKER = 0x0B

TYPE_NAMES = {
    0x00: 'none',
    0x01: 'string',
    0x02: 'int32',
    0x03: 'float32',
    0x04: 'pointer',
    0x05: 'widestring',
    0x06: 'color',
    0x07: 'uint64',
    0x08: 'end',
    0x0A: 'int64',
    0x0B: 'end_alt',
}

_FIXED_WIDTH_TYPES = {
    0x02: 4,
    0x03: 4,
    0x04: 4,
    0x06: 4,
    0x07: 8,
    0x0A: 8,
}


class StructureError(Exception):
    """Raised while walking a malformed binary VDF body."""


def _read_cstring(fp, wide=False):
    start = fp.tell()
    parts = []
    terminator = b'\x00\x00' if wide else b'\x00'

    while True:
        chunk = fp.read(64)
        if not chunk:
            raise StructureError("unterminated string at offset %d" % start)
        parts.append(chunk)
        end = b''.join(parts).find(terminator)
        if end != -1:
            if wide:
                end += end % 2
            fp.seek(start + end + (2 if wide else 1))
            return end


def scan_body_structure(payload):
    """
    Independently walk an alt-format binary VDF payload.

    Returns a dict of structural observations.  Structural problems are
    collected in ``errors`` instead of aborting the whole audit, so callers
    always receive a complete report.
    """
    stats = {
        'length': len(payload),
        'nodes': 0,
        'max_depth': 0,
        'type_counts': {},
        'terminator_offset': None,
        'trailing_bytes': 0,
        'errors': [],
    }

    fp = BytesIO(payload)
    depth = 0

    try:
        while True:
            offset = fp.tell()
            type_byte = fp.read(1)
            if not type_byte:
                if depth:
                    stats['errors'].append(
                        "EOF at depth %d: %d unclosed block(s)"
                        % (depth, depth))
                break

            token = type_byte[0]
            type_name = TYPE_NAMES.get(token, 'unknown(0x%02X)' % token)
            stats['type_counts'][type_name] = stats['type_counts'].get(type_name, 0) + 1

            if token == ALT_END_MARKER:
                if depth == 0:
                    stats['terminator_offset'] = offset
                    break
                depth -= 1
                continue

            if token not in TYPE_NAMES or token in (0x08,):
                raise StructureError(
                    "unexpected type 0x%02X at offset %d (alt format expects 0x0B ends)"
                    % (token, offset))

            stats['nodes'] += 1
            _read_cstring(fp)

            if token == 0x00:
                depth += 1
                stats['max_depth'] = max(stats['max_depth'], depth)
            elif token == 0x05:
                _read_cstring(fp, wide=True)
            elif token in _FIXED_WIDTH_TYPES:
                width = _FIXED_WIDTH_TYPES[token]
                value = fp.read(width)
                if len(value) != width:
                    raise StructureError(
                        "truncated %s value at offset %d" % (type_name, offset))
            elif token == 0x01:
                _read_cstring(fp)
            else:
                raise StructureError(
                    "unsupported type 0x%02X at offset %d" % (token, offset))

        stats['trailing_bytes'] = len(payload) - fp.tell()
        if stats['trailing_bytes']:
            stats['errors'].append(
                "%d trailing byte(s) after root terminator at offset %d"
                % (stats['trailing_bytes'], stats['terminator_offset']))
    except StructureError as ex:
        stats['errors'].append(str(ex))

    stats['structurally_complete'] = not stats['errors']
    return stats


def audit_bytes(data, source=None, include_data=True):
    """
    Audit raw VBKV bytes without modifying them.

    The result keeps header, checksum and body findings separate.  Parsed
    fields are included only as an untrusted preview when the body parses;
    ``catalog_action`` is ``"accept"`` only when every section verifies.
    """
    size = len(data)

    header = {
        'magic_offset': 0,
        'magic_expected': VBKV_MAGIC.decode('ascii'),
        'magic_actual': data[:4].decode('latin-1') if size >= 4 else None,
        'magic_ok': data[:4] == VBKV_MAGIC,
        'header_complete': size >= HEADER_SIZE,
        'size_bytes': size,
        'checksum_offset': [4, 8],
        'payload_offset': [HEADER_SIZE, size],
        'payload_length': max(0, size - HEADER_SIZE),
    }

    checksum = {
        'algorithm': 'CRC-32 (binascii.crc32, poly 0xEDB88320 reflected)',
        'checksummed_range': [HEADER_SIZE, size],
        'stored_raw_hex': None,
        'stored_signed': None,
        'stored_unsigned': None,
        'actual_signed': None,
        'actual_unsigned': None,
        'actual_recomputable_with': 'zlib.crc32(data[8:]) & 0xffffffff',
        'payload_sha256': None,
        'match': False,
        'library_accepts_checksum': False,
    }

    body = {
        'format': 'binary VDF payload, alt end marker 0x%02X' % ALT_END_MARKER,
        'scan': None,
        'library_parse_ok': False,
        'library_parse_error': None,
        'parsed_preview': None,
        'preview_trust': 'untrusted',
        'structurally_complete': False,
    }

    reasons = []

    if not header['magic_ok']:
        reasons.append('bad magic')
    if not header['header_complete']:
        reasons.append('truncated header')

    payload = data[HEADER_SIZE:] if size >= HEADER_SIZE else b''

    checksum['payload_sha256'] = hashlib.sha256(payload).hexdigest()
    actual_unsigned = crc32(payload) & 0xffffffff
    actual_signed = CRC_STRUCT.unpack(struct.pack('<I', actual_unsigned))[0]
    checksum['actual_unsigned'] = actual_unsigned
    checksum['actual_signed'] = actual_signed

    if header['header_complete']:
        stored_raw = data[4:8]
        stored_signed = CRC_STRUCT.unpack(stored_raw)[0]
        stored_unsigned = struct.unpack('<I', stored_raw)[0]
        checksum.update({
            'stored_raw_hex': stored_raw.hex(),
            'stored_signed': stored_signed,
            'stored_unsigned': stored_unsigned,
        })
        checksum['match'] = stored_unsigned == actual_unsigned
        # vdf.vbkv_loads compares a signed unpack against binascii's unsigned
        # CRC on Python 3, so mirror that exact acceptance rule here.
        checksum['library_accepts_checksum'] = stored_signed == actual_unsigned
        if not checksum['match']:
            reasons.append('checksum mismatch')

    scan = scan_body_structure(payload)
    body['scan'] = scan
    body['structurally_complete'] = scan['structurally_complete']
    if not scan['structurally_complete']:
        reasons.append('structural errors in body')

    try:
        parsed = vdf.binary_loads(
            payload, mapper=dict, merge_duplicate_keys=True,
            alt_format=True, raise_on_remaining=True)
        body['library_parse_ok'] = True
        if include_data:
            body['parsed_preview'] = parsed
    except (SyntaxError, TypeError, ValueError) as ex:
        body['library_parse_error'] = '%s: %s' % (type(ex).__name__, ex)

    trusted = bool(header['magic_ok'] and header['header_complete']
                   and checksum['match'] and body['structurally_complete'])
    if not trusted and 'checksum mismatch' in reasons and body['structurally_complete']:
        body['preview_trust'] = 'untrusted: structure intact, checksum failed'

    return {
        'source': source,
        'trusted': trusted,
        'reasons': reasons,
        'catalog_action': 'accept' if trusted else 'quarantine',
        'header': header,
        'checksum': checksum,
        'body': body,
    }


def audit_file(path, include_data=True):
    """Read a file in binary mode and audit it; the file is never written."""
    with open(path, 'rb') as fp:
        data = fp.read()
    return audit_bytes(data, source=path, include_data=include_data)


def render_text_report(result):
    lines = []
    source = result['source'] or '<bytes>'
    lines.append('VBKV 只读核查报告: %s' % source)
    lines.append('=' * 72)

    if result['header'] is None:
        lines.append('')
        lines.append('[结论 Verdict]')
        lines.append('  可信            : False')
        lines.append('  目录处置        : %s' % result['catalog_action'])
        lines.append('  原因            : %s' % '; '.join(result['reasons']))
        lines.append('')
        return '\n'.join(lines)

    header = result['header']
    lines.append('')
    lines.append('[头部 Header]')
    lines.append('  魔数            : %r (期望 %r, %s)'
                 % (header['magic_actual'], header['magic_expected'],
                    '一致' if header['magic_ok'] else '不一致'))
    lines.append('  完整头部        : %s (至少 %d 字节, 实际 %d 字节)'
                 % (header['header_complete'], HEADER_SIZE,
                    header['size_bytes']))
    lines.append('  校验和字节范围  : [%d, %d)'
                 % tuple(header['checksum_offset']))
    lines.append('  正文字节范围    : [%d, %d), 长度 %d 字节'
                 % (header['payload_offset'][0], header['payload_offset'][1],
                    header['payload_length']))

    checksum = result['checksum']
    lines.append('')
    lines.append('[校验和 Checksum]')
    lines.append('  算法            : %s' % checksum['algorithm'])
    lines.append('  被校验范围      : [%d, %d) (即头部之后的全部字节)'
                 % tuple(checksum['checksummed_range']))
    lines.append('  原值原始字节    : %s' % checksum['stored_raw_hex'])
    lines.append('  原值(有符号)    : %s' % checksum['stored_signed'])
    lines.append('  原值(无符号)    : %s (0x%08X)'
                 % (checksum['stored_unsigned'],
                    checksum['stored_unsigned'] or 0))
    lines.append('  实算(无符号)    : %s (0x%08X)'
                 % (checksum['actual_unsigned'], checksum['actual_unsigned']))
    lines.append('  实算(有符号)    : %s' % checksum['actual_signed'])
    lines.append('  正文 SHA-256    : %s' % checksum['payload_sha256'])
    lines.append('  32 位比对       : %s'
                 % ('一致' if checksum['match'] else '不符'))
    if not checksum['library_accepts_checksum'] and checksum['match']:
        lines.append('  库行为提示      : vdf.vbkv_loads 按有符号比较, 会拒绝此文件')
    lines.append('  独立复算        : python -c "import zlib;'
                 'd=open(%r,\'rb\').read();print(hex(zlib.crc32(d[8:])))"'
                 % source)

    body = result['body']
    scan = body['scan']
    lines.append('')
    lines.append('[正文结构 Body]')
    lines.append('  格式            : %s' % body['format'])
    lines.append('  结构完整        : %s' % body['structurally_complete'])
    lines.append('  节点数          : %d, 最大嵌套深度: %d'
                 % (scan['nodes'], scan['max_depth']))
    lines.append('  类型分布        : %s'
                 % json.dumps(scan['type_counts'], ensure_ascii=False, sort_keys=True))
    lines.append('  根结束符偏移    : %s' % scan['terminator_offset'])
    lines.append('  尾部多余字节    : %d' % scan['trailing_bytes'])
    if scan['errors']:
        lines.append('  结构错误        :')
        for error in scan['errors']:
            lines.append('    - %s' % error)
    lines.append('  库解析结果      : %s'
                 % ('成功' if body['library_parse_ok']
                    else '失败: %s' % body['library_parse_error']))
    if body['parsed_preview'] is not None:
        lines.append('  字段预览(不可信): %s'
                     % json.dumps(body['parsed_preview'], ensure_ascii=False,
                                  default=int, sort_keys=True))

    lines.append('')
    lines.append('[结论 Verdict]')
    lines.append('  可信            : %s' % result['trusted'])
    lines.append('  目录处置        : %s' % result['catalog_action'])
    if result['reasons']:
        lines.append('  原因            : %s' % '; '.join(result['reasons']))
    lines.append('  备注            : 本核查为只读, 不修复任何校验和')
    lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m vdf.vbkv_audit',
        description='Read-only VBKV header/checksum/body audit (no repair).')
    parser.add_argument('paths', nargs='+', help='VBKV files to audit')
    parser.add_argument('--json', action='store_true',
                        help='emit machine-readable JSON instead of text')
    parser.add_argument('--no-data', action='store_true',
                        help='omit parsed field previews from the report')
    args = parser.parse_args(argv)

    results = []
    io_failed = False
    for path in args.paths:
        try:
            results.append(audit_file(path, include_data=not args.no_data))
        except OSError as ex:
            io_failed = True
            results.append({
                'source': path,
                'trusted': False,
                'reasons': ['unreadable: %s' % ex],
                'catalog_action': 'quarantine',
                'header': None,
                'checksum': None,
                'body': None,
            })

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2, default=int)
        sys.stdout.write('\n')
    else:
        sys.stdout.write('\n\n'.join(render_text_report(r) for r in results))

    if io_failed:
        return 2
    return 0 if all(r['trusted'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
