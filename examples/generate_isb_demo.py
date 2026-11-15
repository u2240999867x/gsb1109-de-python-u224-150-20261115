#!/usr/bin/env python
"""生成一份带接口统计块(ISB)的演示 pcapng 文件, 覆盖以下情形:

- 正常的累计计数递增(多个采集窗);
- 一条重叠窗口的 ISB(不应重复相加);
- 一条缺少 isb_ifdrop 的 ISB(缺统计, 不能当零);
- 已知重启点之后计数归零(重启点见输出提示);
- 无重启证据的计数倒退(应单列);
- 第二个节(新采集, 计数重新从零开始不算倒退)。

用法:
    python examples/generate_isb_demo.py [输出路径]
"""

import calendar
import sys

from pcapng import FileWriter, blocks

BASE = calendar.timegm((2026, 11, 15, 0, 0, 0, 0, 0, 0))  # 2026-11-15T00:00:00Z
REBOOT_OFFSET = 250  # 已知重启点: BASE + 250 秒


def _ticks(offset_seconds):
    """秒偏移 -> 微秒时间戳(默认 tsresol=1e-6)"""
    return int((BASE + offset_seconds) * 1e6)


def make_isb(shb, start_s, end_s, recv=None, drop=None):
    options = {
        "isb_starttime": _ticks(start_s),
        "isb_endtime": _ticks(end_s),
    }
    if recv is not None:
        options["isb_ifrecv"] = recv
    if drop is not None:
        options["isb_ifdrop"] = drop
    stamp = _ticks(end_s)
    return shb.new_member(
        blocks.InterfaceStatistics,
        interface_id=0,
        timestamp_high=stamp >> 32,
        timestamp_low=stamp & 0xFFFFFFFF,
        options=options,
    )


def new_section(writer=None):
    shb = blocks.SectionHeader(
        endianness="<",
        options={
            "shb_hardware": "demo-generator",
            "shb_os": "python",
            "shb_userappl": "python-pcapng",
        },
    )
    shb.new_member(
        blocks.InterfaceDescription,
        link_type=1,
        snaplen=65535,
        options={"if_name": "eth0", "if_tsresol": b"\x06"},
    )
    return shb


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "isb_demo.pcapng"

    with open(out_path, "wb") as stream:
        shb = new_section()
        writer = FileWriter(stream, shb)

        # ---- 节 #0: 60 秒一个采集窗
        writer.write_block(make_isb(shb, 0, 60, recv=1000, drop=10))
        writer.write_block(make_isb(shb, 60, 120, recv=2500, drop=25))
        # 部分重叠窗口([30,90] 落在 [0,60] 与 [60,120] 之间),
        # 核算时只按时间轴增量计费, 不重复相加
        writer.write_block(make_isb(shb, 30, 90, recv=1800, drop=18))
        # 完全冗余的窗口(与 [60,120] 截止时间相同), 核算时直接跳过
        writer.write_block(make_isb(shb, 60, 120, recv=2500, drop=25))
        # 缺 isb_ifdrop 的统计, 不能当零
        writer.write_block(make_isb(shb, 120, 180, recv=4000))
        writer.write_block(make_isb(shb, 180, 240, recv=6000, drop=55))
        # BASE+250 处设备重启, 计数归零后重新累计
        writer.write_block(make_isb(shb, REBOOT_OFFSET, 300, recv=800, drop=7))
        writer.write_block(make_isb(shb, 300, 360, recv=1600, drop=20))
        # 计数倒退(20 -> 5), 但没有任何重启证据 -> 应单列
        writer.write_block(make_isb(shb, 360, 420, recv=2000, drop=5))
        writer.write_block(make_isb(shb, 420, 480, recv=2600, drop=9))

        # ---- 节 #1: 新采集, 计数重新从零开始(属正常归零, 不是倒退)
        shb2 = new_section()
        writer.write_block(shb2)
        writer.write_block(make_isb(shb2, 600, 660, recv=500, drop=4))

    print("已生成 {}".format(out_path))
    print(
        "已知重启点: {} (BASE+{}s)".format(
            _ticks(REBOOT_OFFSET) / 1e6, REBOOT_OFFSET
        )
    )


if __name__ == "__main__":
    main()
