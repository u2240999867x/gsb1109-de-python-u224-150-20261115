# -*- coding: utf-8 -*-
"""按采集点核对 IPv4 传输校验和。

同一批包在多个采集点（如发送端、镜像口）各采一份时，逐点给出四类结论：

  已验证            校验和字段非零且重算吻合
  已知未填          校验和字段为 0（发送端校验和卸载 / UDP 未生成校验和）
  缺字节无法验证    抓包截断（caplen < wirelen），报文字节不全
  确实不符          校验和字段非零且重算不吻合

规则：
  * 分片未齐前不判传输校验失败：先按 (src, dst, proto, id) 重组，
    齐了才验证；未齐只报“分片未齐暂不判定”。
  * 同一数据报在两个采集点结论不同时，在“跨采集点对照”中分别列出来源；
    “已验证”与“确实不符”并存视为相反证据。

用法:
    python checksum_audit.py 发送端=sender.pcap 镜像口=mirror.pcap
    python checksum_audit.py sender.pcap mirror.pcap   # 省略名字时用文件名
"""
from __future__ import print_function

import os
import struct
import sys
from collections import defaultdict

import dpkt
import dpkt.pcap
from dpkt.utils import inet_to_str

VERIFIED = '已验证'
UNFILLED = '已知未填'
TRUNCATED = '缺字节无法验证'
MISMATCH = '确实不符'
PENDING = '分片未齐暂不判定'

CATEGORIES = (VERIFIED, UNFILLED, TRUNCATED, MISMATCH)

_PROTO_NAMES = {dpkt.ip.IP_PROTO_TCP: 'TCP',
                dpkt.ip.IP_PROTO_UDP: 'UDP',
                dpkt.ip.IP_PROTO_ICMP: 'ICMP'}

# 传输层校验和字段在首部中的偏移及首部长度下限
_SUM_OFFSET = {dpkt.ip.IP_PROTO_TCP: (16, 20),
               dpkt.ip.IP_PROTO_UDP: (6, 8),
               dpkt.ip.IP_PROTO_ICMP: (2, 4)}


def read_frames(path):
    """产出 (序号, 时间戳, 捕获字节, 线上长度)。保留 caplen/wirelen 以识别截断。"""
    with open(path, 'rb') as f:
        magic = f.read(4)
        if len(magic) < 4:
            return
        if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1'):
            endian = '<'
        elif magic in (b'\xa1\xb2\xc3\xd4', b'\xa1\xb2\x3c\x4d'):
            endian = '>'
        else:
            raise ValueError('%s: 不是 pcap 文件' % path)
        f.read(20)  # 文件头剩余部分
        idx = 0
        while True:
            hdr = f.read(16)
            if len(hdr) < 16:
                break
            tv_sec, tv_usec, caplen, wirelen = struct.unpack(endian + 'IIII', hdr)
            buf = f.read(caplen)
            if len(buf) < caplen:
                break
            idx += 1
            yield idx, tv_sec + tv_usec / 1e6, buf, wirelen


def check_transport(src, dst, proto, segment):
    """对完整传输层报文给出 (结论, 说明)。"""
    name = _PROTO_NAMES.get(proto)
    if name is None:
        return None, None
    sum_off, min_hdr = _SUM_OFFSET[proto]
    if len(segment) < min_hdr:
        return TRUNCATED, '%s 首部不完整（仅 %d/%d 字节）' % (name, len(segment), min_hdr)
    stored = struct.unpack('>H', segment[sum_off:sum_off + 2])[0]
    if stored == 0:
        return UNFILLED, '%s 校验和字段为 0（卸载未填/未生成）' % name
    zeroed = segment[:sum_off] + b'\x00\x00' + segment[sum_off + 2:]
    if proto == dpkt.ip.IP_PROTO_ICMP:
        total = dpkt.in_cksum(segment)
        computed = dpkt.in_cksum(zeroed)
    else:
        pseudo = struct.pack('>4s4sxBH', src, dst, proto, len(segment))
        total = dpkt.in_cksum_done(
            dpkt.in_cksum_add(dpkt.in_cksum_add(0, pseudo), segment))
        computed = dpkt.in_cksum_done(
            dpkt.in_cksum_add(dpkt.in_cksum_add(0, pseudo), zeroed))
    if total == 0:
        return VERIFIED, '%s 校验和 0x%04x 重算吻合' % (name, stored)
    return MISMATCH, '%s 校验和不符：报文填 0x%04x，实算 0x%04x' % (name, stored, computed)


def describe(ip):
    """生成 'TCP 10.0.0.1.40000 > 10.0.0.2.80' 风格的描述。"""
    name = _PROTO_NAMES.get(ip.p, 'proto=%d' % ip.p)
    src, dst = inet_to_str(ip.src), inet_to_str(ip.dst)
    l4 = ip.data
    if isinstance(l4, (dpkt.tcp.TCP, dpkt.udp.UDP)):
        return '%s %s.%d > %s.%d' % (name, src, l4.sport, dst, l4.dport)
    return '%s %s > %s' % (name, src, dst)


class Record(object):
    def __init__(self, key, frame, ts, meta, ip_id, verdict, detail):
        self.key = key
        self.frame = frame
        self.ts = ts
        self.meta = meta
        self.ip_id = ip_id
        self.verdict = verdict
        self.detail = detail

    def line(self):
        return ('  帧#%-4d id=0x%04x %-40s %s' %
                (self.frame, self.ip_id, self.meta, self.detail))


def _missing_ranges(fills, total):
    """由已覆盖区间求缺失区间的可读描述。"""
    missing = []
    pos = 0
    for start, end in sorted(fills):
        if start > pos:
            missing.append((pos, start))
        pos = max(pos, end)
    if total is not None and pos < total:
        missing.append((pos, total))
    return missing, pos


def analyze_capture(path):
    """分析单个采集点，返回 Record 列表。"""
    records = []
    frags = {}  # (src, dst, proto, id) -> 重组上下文

    for idx, ts, buf, wirelen in read_frames(path):
        try:
            eth = dpkt.ethernet.Ethernet(buf)
        except dpkt.UnpackError:
            continue
        ip = eth.data if isinstance(eth.data, dpkt.ip.IP) else None
        if ip is None or ip.p not in _SUM_OFFSET:
            continue
        l2off = 14 + 4 * len(getattr(eth, 'vlan_tags', []))
        ihl = ip.hl * 4
        truncated = wirelen > len(buf)
        frag_key = (bytes(ip.src), bytes(ip.dst), ip.p, ip.id)

        if ip.mf or ip.offset != 0:
            ctx = frags.setdefault(frag_key, {
                'frags': {}, 'total': None, 'truncated': False,
                'meta': None, 'frame': idx, 'ts': ts, 'id': ip.id,
            })
            expect = ip.len - ihl  # 本片在线上的数据长度
            payload = buf[l2off + ihl:l2off + ip.len]
            if truncated or len(payload) < expect:
                ctx['truncated'] = True
            ctx['frags'][ip.offset * 8] = payload
            if not ip.mf:
                ctx['total'] = ip.offset * 8 + expect
            if ip.offset == 0:
                ctx['meta'] = describe(ip)
            continue

        meta = describe(ip)
        key = ('ip',) + frag_key + (ip.len,)
        need = l2off + ip.len if ip.len else len(buf)
        if truncated or len(buf) < need:
            detail = '捕获 %d 字节 < 线上 %d 字节，报文不全' % (len(buf), wirelen)
            records.append(Record(key, idx, ts, meta, ip.id, TRUNCATED, detail))
            continue
        segment = buf[l2off + ihl:l2off + ip.len]
        verdict, detail = check_transport(ip.src, ip.dst, ip.p, segment)
        if verdict is None:
            continue
        records.append(Record(key, idx, ts, meta, ip.id, verdict, detail))

    # 分片重组：齐了才验证，未齐不判失败
    for frag_key, ctx in sorted(frags.items(), key=lambda kv: kv[1]['frame']):
        src, dst, proto, ip_id = frag_key
        key = ('frag',) + frag_key
        meta = ctx['meta'] or '%s %s > %s（首片缺失）' % (
            _PROTO_NAMES.get(proto, proto), inet_to_str(src), inet_to_str(dst))
        if ctx['truncated']:
            records.append(Record(key, ctx['frame'], ctx['ts'], meta, ip_id,
                                  TRUNCATED, '存在被截断的分片，重组字节不全'))
            continue
        total = ctx['total']
        fills = [(off, off + len(data)) for off, data in ctx['frags'].items()]
        missing, covered = _missing_ranges(fills, total)
        if total is None or missing:
            if total is None:
                detail = ('已收 %d 片共 %d 字节，未见末片（MF=0），'
                          '分片未齐，不判传输校验失败' % (len(fills), covered))
            else:
                gap = '、'.join('[%d,%d)' % (s, e) for s, e in missing)
                detail = ('已收 %d/%d 字节，缺区间 %s，分片未齐，'
                          '不判传输校验失败' % (covered, total, gap))
            records.append(Record(key, ctx['frame'], ctx['ts'], meta, ip_id,
                                  PENDING, detail))
            continue
        segment = bytearray(total)
        for off, data in ctx['frags'].items():
            segment[off:off + len(data)] = data
        verdict, detail = check_transport(src, dst, proto, bytes(segment))
        detail = '重组 %d 片共 %d 字节后：%s' % (len(fills), total, detail)
        records.append(Record(key, ctx['frame'], ctx['ts'], meta, ip_id,
                              verdict, detail))
    return records


def print_capture(name, path, records):
    print('== 采集点 %s (%s) ==' % (name, path))
    buckets = defaultdict(list)
    for rec in records:
        buckets[rec.verdict].append(rec)
    for verdict in CATEGORIES + (PENDING,):
        recs = buckets.get(verdict, [])
        print('%s (%d):' % (verdict, len(recs)))
        for rec in recs:
            print(rec.line())
    print()


def compare_captures(captures):
    """captures: [(名称, [Record...]), ...]，输出跨采集点结论不一致的数据报。"""
    by_key = defaultdict(dict)
    for name, records in captures:
        for rec in records:
            by_key[rec.key][name] = rec

    contrary, offload_ok, other = [], [], []
    for key, obs in by_key.items():
        if len(obs) < 2:
            continue
        verdicts = set(rec.verdict for rec in obs.values())
        if len(verdicts) == 1:
            continue
        if MISMATCH in verdicts and verdicts & {VERIFIED, UNFILLED}:
            contrary.append(obs)
        elif verdicts <= {VERIFIED, UNFILLED}:
            offload_ok.append(obs)
        else:
            other.append(obs)

    print('== 跨采集点对照 ==')
    if not (contrary or offload_ok or other):
        print('各采集点结论一致。')
        return
    for title, groups in (('相反证据（同一数据报结论冲突，分别列出来源）', contrary),
                          ('符合校验和卸载预期（发送端未填、线上已填好）', offload_ok),
                          ('其他观测差异（截断/分片未齐等观测能力差异）', other)):
        if not groups:
            continue
        print('%s:' % title)
        for obs in groups:
            first = next(iter(obs.values()))
            print('  id=0x%04x %s' % (first.ip_id, first.meta))
            for name, rec in obs.items():
                print('    - [%s] %s：%s' % (name, rec.verdict, rec.detail))
    print()


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    captures = []
    for arg in argv[1:]:
        if '=' in arg:
            name, path = arg.split('=', 1)
        else:
            name, path = os.path.splitext(os.path.basename(arg))[0], arg
        records = analyze_capture(path)
        print_capture(name, path, records)
        captures.append((name, records))
    compare_captures(captures)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
