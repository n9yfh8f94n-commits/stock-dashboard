#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fix_pollution.py —— 清除「盘中误跑」写入的当日脏数据
================================================================================

背景
----
交易日 15:00 前跑 daily.bat，抓到的是半天数据，会写进三处：
    calibration/metrics_history.csv   历史指标库（最严重，污染分位数校准）
    cache/{day}_*.csv                 接口快照（后续 dashboard 会读到）
    reports/*{day}*                   报告与仪表盘

本脚本按「指定日期」精准清除这三处，并在动手前备份 metrics_history.csv。

为什么用「按行文本处理」而不是 pandas 读写
------------------------------------------
pandas 的 read_csv -> to_csv 会重排浮点精度（26.09 可能变成 26.089999999999996）、
统一换行符、可能改变编码。历史库是长期累积的资产，不该被读写一次就改掉格式。
这里只删匹配的行，其余字节原样保留。

用法
----
    python fix_pollution.py --dry-run          # 只看会删什么
    python fix_pollution.py                    # 删今天（若今天是脏数据）
    python fix_pollution.py --day 20261111     # 删指定日期
    python fix_pollution.py --keep-reports     # 保留 reports 里的文件
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import config

HISTORY_FILE = config.BASE_DIR / "calibration" / "metrics_history.csv"
BACKUP_DIR = config.BASE_DIR / "calibration" / "backup"
CACHE_DIR = config.BASE_DIR / "cache"
REPORT_DIR = config.BASE_DIR / "reports"


def remove_history_row(day: str, dry_run: bool) -> Tuple[int, int, List[str]]:
    """
    从 metrics_history.csv 里删掉指定日期的行。

    返回 (删除前总行数, 删除后总行数, 被删的行内容)

    实现要点：
      · 以 BOM 处理开头（文件是 utf-8-sig）
      · 按行切分后逐行判断，不解析列，保证其它行一个字节都不变
      · 删除前把原文件复制到 calibration/backup/
    """
    if not HISTORY_FILE.exists():
        return 0, 0, []

    raw = HISTORY_FILE.read_text(encoding="utf-8-sig")
    had_bom = HISTORY_FILE.read_bytes().startswith(b"\xef\xbb\xbf")

    lines = raw.splitlines()
    if not lines:
        return 0, 0, []

    header, body = lines[0], lines[1:]
    kept: List[str] = []
    removed: List[str] = []

    for ln in body:
        if not ln.strip():
            continue
        # 第一列就是日期
        first = ln.split(",", 1)[0].strip().strip('"')
        if first == day:
            removed.append(ln)
        else:
            kept.append(ln)

    before, after = len(body), len(kept)
    if not removed:
        return before, after, []

    print(f"  metrics_history.csv: {before} 行 -> {after} 行（删除 {len(removed)} 行）")
    for r in removed:
        print(f"    被删: {r}")

    if dry_run:
        return before, after, removed

    # ---- 备份 ----
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"metrics_history.before_{day}_{stamp}.csv"
    shutil.copy2(HISTORY_FILE, backup)
    print(f"  已备份: {backup.name}")

    # ---- 写回（保留 BOM 状态与原换行风格）----
    new_text = "\n".join([header] + kept) + "\n"
    HISTORY_FILE.write_text(new_text, encoding="utf-8-sig" if had_bom else "utf-8")
    print(f"  已写回: {HISTORY_FILE}")
    return before, after, removed


def remove_cache_files(day: str, dry_run: bool) -> int:
    """删除 cache/{day}_*.csv 及对应的 .meta。"""
    targets = sorted(CACHE_DIR.glob(f"{day}_*.csv")) + sorted(CACHE_DIR.glob(f"{day}_*.meta"))
    if not targets:
        print("  cache: 无当日快照")
        return 0
    for p in targets:
        print(f"  cache: {'(dry) ' if dry_run else ''}删除 {p.name}")
        if not dry_run:
            try:
                p.unlink()
            except Exception as exc:
                print(f"    删除失败: {exc}")
    return len(targets)


def remove_reports(day: str, dry_run: bool) -> int:
    """删除 reports/ 下当日的 md 与 html。"""
    targets = sorted(REPORT_DIR.glob(f"*{day}*"))
    if not targets:
        print("  reports: 无当日文件")
        return 0
    for p in targets:
        print(f"  reports: {'(dry) ' if dry_run else ''}删除 {p.name}")
        if not dry_run:
            try:
                p.unlink()
            except Exception as exc:
                print(f"    删除失败: {exc}")
    return len(targets)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="清除盘中误跑写入的当日脏数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python fix_pollution.py --dry-run\n"
            "  python fix_pollution.py --day 20261111\n"
            "  python fix_pollution.py --keep-reports\n"
        ),
    )
    parser.add_argument("--day", default=datetime.now().strftime("%Y%m%d"),
                        help="要清除的日期 YYYYMMDD，默认今天")
    parser.add_argument("--dry-run", action="store_true", help="只显示，不实际删除")
    parser.add_argument("--keep-reports", action="store_true", help="保留 reports 里的文件")
    args = parser.parse_args()

    day = args.day.strip()
    print("=" * 70)
    print(f"  清除 {day} 的盘中脏数据" + ("（干跑，不写入）" if args.dry_run else ""))
    print("=" * 70)

    print("\n[1/3] 历史指标库")
    before, after, removed = remove_history_row(day, args.dry_run)
    if not removed:
        print(f"  metrics_history.csv 里没有 {day} 这一行，无需处理")

    print("\n[2/3] 接口快照 cache/")
    n_cache = remove_cache_files(day, args.dry_run)

    print("\n[3/3] 本地报告 reports/")
    n_rep = 0 if args.keep_reports else remove_reports(day, args.dry_run)

    print("\n" + "=" * 70)
    if args.dry_run:
        print("  干跑结束。去掉 --dry-run 才会真正删除。")
    else:
        print(f"  完成：历史库删除 {len(removed)} 行，cache {n_cache} 个，reports {n_rep} 个")
        print("  下一步：")
        print("    python dashboard.py 20261110     # 重新生成上一交易日的仪表盘")
        print("    python deploy.py                 # 覆盖 GitHub 上的错误页面")
    print("=" * 70)


if __name__ == "__main__":
    main()
