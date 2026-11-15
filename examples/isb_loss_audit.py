#!/usr/bin/env python
"""核算 pcapng 接口统计块(ISB)中各采集窗可证明的缺损(丢包)。

背景: isb_ifrecv / isb_ifdrop 是累计计数器, 设备重启(或新采集节)后归零。

核算规则:
- 以"节 + 接口"分组, 按统计截止时间排序, 只按时间轴上的增量计费,
  重叠窗口不重复相加;
- 已知重启点(``--reboot``)之间的计数倒退视为归零: 重启后的读数本身
  即重启以来的可证明缺损; 重启间隙内的缺损不可证明, 单列;
- 缺少计数的窗口不当零: 记为未知, 其后第一条有计数的读数会与
  最近一条有计数的读数跨洞求差(差值仍是可证明的);
- 计数倒退且无已知重启证据的区段单独列出, 不计入可证明总量。

用法:
    python examples/isb_loss_audit.py 文件.pcapng [--reboot 时间 ...]

``--reboot`` 接受 Unix 秒或 ISO8601(缺省按 UTC)。
"""

import argparse
import datetime
import sys
from collections import namedtuple

from pcapng.blocks import InterfaceStatistics, SectionHeader
from pcapng.exceptions import PcapngLoadError
from pcapng.scanner import FileScanner

Observation = namedtuple(
    "Observation",
    ["section", "interface_id", "interface_name", "timestamp", "start", "end",
     "recv", "drop"],
)

Segment = namedtuple("Segment", ["start", "end", "amount", "note"])
Anomaly = namedtuple("Anomaly", ["prev", "cur", "field"])
Unknown = namedtuple("Unknown", ["start", "end", "note"])


def parse_time(text):
    try:
        return float(text)
    except ValueError:
        pass
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def fmt_time(value):
    if value is None:
        return "<未知>"
    return datetime.datetime.fromtimestamp(
        value, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_observations(path):
    observations = []
    with open(path, "rb") as stream:
        section_index = -1
        for block in FileScanner(stream):
            if isinstance(block, SectionHeader):
                section_index += 1
            elif isinstance(block, InterfaceStatistics):
                resolution = block.interface.timestamp_resolution
                options = block.options
                start = options.get("isb_starttime")
                end = options.get("isb_endtime")
                observations.append(
                    Observation(
                        section=section_index,
                        interface_id=block.interface_id,
                        interface_name=block.interface.options.get(
                            "if_name", "<未命名>"
                        ),
                        timestamp=block.timestamp,
                        start=start * resolution if start is not None else None,
                        end=(
                            end * resolution
                            if end is not None
                            else block.timestamp
                        ),
                        recv=options.get("isb_ifrecv"),
                        drop=options.get("isb_ifdrop"),
                    )
                )
    return observations


def account_counter(observations, field, reboots):
    """对单个累计计数器做核算, 返回 (可证明区段, 倒退异常, 未知区段, 重叠跳过)"""
    segments = []
    anomalies = []
    unknowns = []
    skipped = []
    baseline = None  # 最近一条带有该计数器的观测
    missing_hole = False  # baseline 之后是否跨过了缺统计的观测
    max_end = None

    for obs in sorted(observations, key=lambda item: (item.end, item.start or 0)):
        value = getattr(obs, field)
        if max_end is not None and obs.end <= max_end:
            # 重叠窗口: 时间段已被覆盖, 不重复相加
            skipped.append(obs)
            continue
        if baseline is None:
            if value is None:
                unknowns.append(
                    Unknown(obs.start, obs.end, "首条统计缺少计数, 不计为零")
                )
            else:
                segments.append(Segment(obs.start, obs.end, value, "累计起点"))
                baseline = obs
        elif value is None:
            unknowns.append(
                Unknown(obs.start, obs.end, "缺少计数, 不计为零")
            )
            missing_hole = True
        else:
            resets = [r for r in reboots if baseline.end < r <= obs.end]
            if resets:
                reboot = max(resets)
                unknowns.append(
                    Unknown(baseline.end, reboot, "重启间隙, 缺损不可证明")
                )
                segments.append(
                    Segment(reboot, obs.end, value, "已知重启后重新累计")
                )
            else:
                delta = value - getattr(baseline, field)
                if delta < 0:
                    anomalies.append(Anomaly(baseline, obs, field))
                else:
                    note = "跨缺统计窗口求差" if missing_hole else "窗口增量"
                    segments.append(Segment(baseline.end, obs.end, delta, note))
            baseline = obs
            missing_hole = False
        max_end = obs.end

    return segments, anomalies, unknowns, skipped


def report_group(key, observations, reboots):
    section, interface_id, interface_name = key
    print(
        "== 节 #{} 接口 {} ({}) ==".format(section, interface_id, interface_name)
    )
    total = 0
    for field, label in (("drop", "丢包"), ("recv", "接收")):
        segments, anomalies, unknowns, skipped = account_counter(
            observations, field, reboots
        )
        subtotal = sum(segment.amount for segment in segments)
        if field == "drop":
            total = subtotal
        print("-- 可证明{}区段:".format(label))
        for segment in segments:
            print(
                "   [{} .. {}] +{}  ({})".format(
                    fmt_time(segment.start),
                    fmt_time(segment.end),
                    segment.amount,
                    segment.note,
                )
            )
        print("   {}小计: {}".format(label, subtotal))
        if skipped:
            print("-- 重叠窗口(已覆盖, 不重复相加):")
            for obs in skipped:
                print(
                    "   [{} .. {}] {}={}".format(
                        fmt_time(obs.start),
                        fmt_time(obs.end),
                        field,
                        getattr(obs, field),
                    )
                )
        if unknowns:
            print("-- 不可证明/未知区段(不计为零):")
            for unknown in unknowns:
                print(
                    "   [{} .. {}] {}".format(
                        fmt_time(unknown.start), fmt_time(unknown.end),
                        unknown.note,
                    )
                )
        if anomalies:
            print("-- 计数倒退且无重启证据(单列, 不计入总量):")
            for anomaly in anomalies:
                print(
                    "   [{} .. {}] {} -> {} (Δ{})".format(
                        fmt_time(anomaly.prev.end),
                        fmt_time(anomaly.cur.end),
                        getattr(anomaly.prev, field),
                        getattr(anomaly.cur, field),
                        getattr(anomaly.cur, field)
                        - getattr(anomaly.prev, field),
                    )
                )
    print("本组可证明丢包合计: {}".format(total))
    print()
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="核算 ISB 各采集窗可证明的缺损(丢包)"
    )
    parser.add_argument("capture", help="pcapng 文件路径")
    parser.add_argument(
        "--reboot",
        action="append",
        default=[],
        metavar="时间",
        help="已知重启点(Unix 秒或 ISO8601), 可多次指定",
    )
    args = parser.parse_args(argv)

    reboots = sorted(parse_time(text) for text in args.reboot)
    try:
        observations = collect_observations(args.capture)
    except PcapngLoadError as exc:
        print("解析文件失败: {}".format(exc), file=sys.stderr)
        return 1
    if not observations:
        print("文件中没有接口统计块(ISB)", file=sys.stderr)
        return 1

    groups = {}
    for obs in observations:
        key = (obs.section, obs.interface_id, obs.interface_name)
        groups.setdefault(key, []).append(obs)

    if reboots:
        print("已知重启点: {}".format(", ".join(fmt_time(r) for r in reboots)))
        print()

    grand_total = 0
    for key in sorted(groups):
        grand_total += report_group(key, groups[key], reboots)

    print("== 全部采集窗可证明丢包总计: {} ==".format(grand_total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
