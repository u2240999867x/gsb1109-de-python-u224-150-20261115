# -*- coding: utf-8 -*-
"""生成一对演示抓包：发送端（启用校验和卸载）与镜像口。

覆盖场景：
  id=0x1001 TCP   发送端卸载未填(0) / 镜像口已验证        -> 符合卸载预期
  id=0x1002 UDP   两端均已验证
  id=0x1003 TCP   发送端已验证 / 镜像口被改坏确实不符      -> 相反证据
  id=0x1004 TCP   发送端已验证 / 镜像口截断缺字节无法验证
  id=0x1005 UDP   发送端只采到首片(分片未齐) / 镜像口两片齐全重组后已验证
  id=0x1006 UDP   两端校验和均为 0（已知未填）
  id=0x1007 ICMP  两端均已验证

输出：examples/data/sender_offload.pcap、examples/data/mirror_span.pcap
"""
from __future__ import print_function

import os
import struct

import dpkt

SRC_MAC = b'\x02\x00\x00\x00\x00\x01'
DST_MAC = b'\x02\x00\x00\x00\x00\x02'
SRC_IP = b'\x0a\x00\x00\x01'
DST_IP = b'\x0a\x00\x00\x02'

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


class PcapWriter(object):
    """支持 caplen < wirelen 的最小 pcap 写器（小端）。"""

    def __init__(self, path):
        self.f = open(path, 'wb')
        self.f.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))

    def write(self, frame, ts, wirelen=None):
        self.f.write(struct.pack('<IIII', int(ts), int((ts % 1) * 1e6),
                                 len(frame), wirelen or len(frame)))
        self.f.write(frame)

    def close(self):
        self.f.close()


def eth_wrap(ip):
    return bytes(dpkt.ethernet.Ethernet(src=SRC_MAC, dst=DST_MAC, data=ip))


def tcp_frame(ip_id, payload, sport=40000, dport=80):
    tcp = dpkt.tcp.TCP(sport=sport, dport=dport, seq=1,
                       flags=dpkt.tcp.TH_PUSH | dpkt.tcp.TH_ACK,
                       win=65535, data=payload)
    return eth_wrap(dpkt.ip.IP(src=SRC_IP, dst=DST_IP, id=ip_id,
                               p=dpkt.ip.IP_PROTO_TCP, data=tcp))


def udp_frame(ip_id, payload, sport=53000, dport=53):
    udp = dpkt.udp.UDP(sport=sport, dport=dport, data=payload)
    udp.ulen = 8 + len(payload)
    return eth_wrap(dpkt.ip.IP(src=SRC_IP, dst=DST_IP, id=ip_id,
                               p=dpkt.ip.IP_PROTO_UDP, data=udp))


def icmp_frame(ip_id, payload):
    echo = dpkt.icmp.ICMP.Echo(id=1, seq=1, data=payload)
    icmp = dpkt.icmp.ICMP(type=dpkt.icmp.ICMP_ECHO, data=echo)
    return eth_wrap(dpkt.ip.IP(src=SRC_IP, dst=DST_IP, id=ip_id,
                               p=dpkt.ip.IP_PROTO_ICMP, data=icmp))


def zero_transport_sum(frame, proto):
    """把以太帧中 TCP/UDP 校验和字段清零（模拟发送端卸载未填）。"""
    off = 14 + 20 + (16 if proto == dpkt.ip.IP_PROTO_TCP else 6)
    frame = bytearray(frame)
    frame[off:off + 2] = b'\x00\x00'
    return bytes(frame)


def corrupt_last_byte(frame):
    """篡改最后一个字节（模拟线上被改坏）。"""
    frame = bytearray(frame)
    frame[-1] ^= 0xFF
    return bytes(frame)


def udp_fragments(ip_id, payload, sport=53000, dport=53):
    """把 UDP 数据报拆成两片，返回 (frag1_frame, frag2_frame)。"""
    udp = dpkt.udp.UDP(sport=sport, dport=dport, data=payload)
    udp.ulen = 8 + len(payload)
    # 先让 dpkt 算好 UDP 校验和，再取传输层字节切片
    full = dpkt.ip.IP(src=SRC_IP, dst=DST_IP, id=ip_id,
                      p=dpkt.ip.IP_PROTO_UDP, data=udp)
    bytes(full)
    segment = bytes(udp)
    cut = 24  # 分片数据长度须为 8 的倍数
    ip1 = dpkt.ip.IP(src=SRC_IP, dst=DST_IP, id=ip_id,
                     p=dpkt.ip.IP_PROTO_UDP, data=segment[:cut])
    ip1.mf = True
    ip2 = dpkt.ip.IP(src=SRC_IP, dst=DST_IP, id=ip_id,
                     p=dpkt.ip.IP_PROTO_UDP, data=segment[cut:])
    ip2.offset = cut // 8
    return eth_wrap(ip1), eth_wrap(ip2)


def main():
    sender = PcapWriter(os.path.join(DATA_DIR, 'sender_offload.pcap'))
    mirror = PcapWriter(os.path.join(DATA_DIR, 'mirror_span.pcap'))
    ts = 1763200000.0

    # 0x1001 TCP：发送端卸载未填，镜像口正常
    f = tcp_frame(0x1001, b'GET / HTTP/1.1\r\nHost: example\r\n\r\n')
    sender.write(zero_transport_sum(f, dpkt.ip.IP_PROTO_TCP), ts + 0.001)
    mirror.write(f, ts + 0.002)

    # 0x1002 UDP：两端正常
    f = udp_frame(0x1002, b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00')
    sender.write(f, ts + 0.003)
    mirror.write(f, ts + 0.004)

    # 0x1003 TCP：发送端正常，镜像口收到的被改坏
    f = tcp_frame(0x1003, b'POST /submit HTTP/1.1\r\nContent-Length: 4\r\n\r\nDATA')
    sender.write(f, ts + 0.005)
    mirror.write(corrupt_last_byte(f), ts + 0.006)

    # 0x1004 TCP：发送端正常，镜像口截断（只采到 TCP 首部）
    f = tcp_frame(0x1004, b'X' * 100)
    sender.write(f, ts + 0.007)
    mirror.write(f[:14 + 20 + 20], ts + 0.008, wirelen=len(f))

    # 0x1005 UDP 分片：发送端只采到首片，镜像口两片齐全
    frag1, frag2 = udp_fragments(0x1005, b'dns-response-payload-' + b'Y' * 40)
    sender.write(zero_transport_sum(frag1, dpkt.ip.IP_PROTO_UDP), ts + 0.009)
    mirror.write(frag1, ts + 0.010)
    mirror.write(frag2, ts + 0.011)

    # 0x1006 UDP：两端校验和均为 0
    f = zero_transport_sum(udp_frame(0x1006, b'no-checksum'), dpkt.ip.IP_PROTO_UDP)
    sender.write(f, ts + 0.012)
    mirror.write(f, ts + 0.013)

    # 0x1007 ICMP：两端正常
    f = icmp_frame(0x1007, b'ping-payload')
    sender.write(f, ts + 0.014)
    mirror.write(f, ts + 0.015)

    sender.close()
    mirror.close()
    print('已生成 %s 和 %s' % (os.path.join(DATA_DIR, 'sender_offload.pcap'),
                               os.path.join(DATA_DIR, 'mirror_span.pcap')))


if __name__ == '__main__':
    main()
