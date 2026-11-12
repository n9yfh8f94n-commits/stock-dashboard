#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sector_tracker.py —— 题材生命周期追踪（模块四）的数据层
================================================================================

做什么
------
每天从涨停池里提取「哪些题材有票涨停、各几家」，把 Top N 沉淀到
`calibration/sector_history.csv`，供堆叠柱状图展示发酵/退潮。

题材字段从哪来（**当前是降级状态**）
------------------------------------
    优先级 1：同花顺 limit_up_reason  —— 形如「光伏边框+液冷铝材+机器人布局」
    优先级 2：东财 所属行业           —— 形如「光伏设备」「通用设备」

怎么区分？
    同花顺的题材串几乎必然含 `+` 或明显长于东财行业名。
    实测 2026-11 的 16 天快照里，含 `+` 的比例是 0 —— 也就是说
    现在**全部是东财行业口径**，粒度比题材粗（「通用设备」而不是「低空经济」）。

    代码不写死来源：每次现判。等哪天同花顺降级真的生效了，
    数据会自动变成题材口径，图不用改。

为什么每天只存 Top N，而不是全部
--------------------------------
    长尾题材（1 家涨停）天天在变，全存会让文件迅速膨胀且图更乱。
    存 Top 8 足够支撑「前 5 + 其他」的画法，文件一年也就几千行。

幂等
----
    update_history() 按日期去重：同一天重复运行不会写重复行。
    dashboard.py 每次生成都会调一次，所以历史会自动补齐 + 增量更新。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

import config
from utils import logger

CACHE_DIR = config.BASE_DIR / "cache"
HISTORY_FILE = config.BASE_DIR / "calibration" / "sector_history.csv"

TOP_SAVE = 30    # 每天最多存多少个题材（防极端情况，正常 10-15 个）
TOP_SHOW = 5     # 图上固定展示前 5 个，其余归「其他」
MIN_STOCKS = 2   # 只有 1 家涨停的题材不进榜（噪音太大）

# 【为什么不再按 Top8 截断】
# 第一版每天只存前 8 个题材，画图时补齐成 日期×题材 矩阵，
# 没进 Top8 的格子被填 0 —— 于是「汽车零部」在某些天显示 0 家，
# 其实那天有票涨停，只是排第 9 被截掉了。
# 假 0 会让「题材退潮」的判断完全失真，所以改成全量存储（>=2 家的都存）。
# 实测 16 天全量也才 100 多行，一年约 3000 行 / 100 KB，完全可以接受。

COLUMNS = ["日期", "题材", "涨停家数", "来源"]


def _is_ths_style(series: pd.Series) -> bool:
    """
    判断这一列是同花顺题材串还是东财行业名。

    依据：同花顺的 limit_up_reason 是「A+B+C」这种拼接，
    实测几乎必然含 `+`；东财行业名是 3-5 字的申万行业，不含 `+`。
    """
    if series.empty:
        return False
    try:
        plus_ratio = float(series.str.contains("+", regex=False).mean())
    except Exception:
        return False
    return plus_ratio > 0.3


def extract_day_sectors(df: pd.DataFrame) -> Tuple[List[Tuple[str, int]], str]:
    """
    从单日涨停池提取题材分布。

    返回 ([(题材, 家数), ...] 按家数降序截断到 TOP_SAVE, 来源标记 'ths'/'em')
    """
    col = "所属行业"
    if df is None or df.empty or col not in df.columns:
        return [], ""

    series = df[col].dropna().astype(str).str.strip()
    series = series[series != ""]
    if series.empty:
        return [], ""

    source = "ths" if _is_ths_style(series) else "em"

    # 同花顺题材串太长（如「光伏边框+液冷铝材+机器人布局+三季报扭亏」）时，
    # 直接整串当题材名会让图例长到没法看。
    # 处理：只取第一个 `+` 之前的部分作为「主题材」，其余视为附加逻辑。
    if source == "ths":
        series = series.str.split("+").str[0].str.strip()

    counts = series.value_counts()
    counts = counts[counts >= MIN_STOCKS]

    items = [(str(k), int(v)) for k, v in counts.head(TOP_SAVE).items()]
    return items, source


def load_history() -> pd.DataFrame:
    """读历史；不存在或损坏时返回空表（列结构仍然完整）。"""
    if not HISTORY_FILE.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(HISTORY_FILE, dtype={"日期": str}, encoding="utf-8-sig")
    except Exception as exc:
        logger.warning("读取 %s 失败（按空表处理）：%s", HISTORY_FILE.name, exc)
        return pd.DataFrame(columns=COLUMNS)

    df["日期"] = df["日期"].astype(str).str.zfill(8)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[COLUMNS]


def save_history(df: pd.DataFrame) -> None:
    """写回（先写临时文件再替换，避免中途崩溃留下半个文件）。"""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = df.sort_values(["日期", "涨停家数"], ascending=[True, False]).reset_index(drop=True)
    tmp = HISTORY_FILE.with_suffix(".csv.tmp")
    out.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(HISTORY_FILE)


def update_history(days: int = 60, force_days: List[str] | None = None) -> int:
    """
    扫描 cache 里的涨停池快照，把缺失/需要刷新的日期写进 sector_history.csv。

    参数
    ----
    days        : 最多回溯多少个快照
    force_days  : 强制重算这些日期（用于口径变更后刷新）

    返回新增/更新的行数。

    【幂等设计】已有日期默认跳过；force_days 里的日期会先删后写。
    """
    if not CACHE_DIR.exists():
        return 0

    # 只匹配 {YYYYMMDD}_zt_pool.csv，排除 prev_zt_pool / zb_pool 等
    files: List[Path] = []
    for p in CACHE_DIR.glob("*.csv"):
        if re.fullmatch(r"\d{8}_zt_pool\.csv", p.name):
            files.append(p)
    files.sort(reverse=True)
    files = files[:days]

    if not files:
        return 0

    hist = load_history()
    have = set(hist["日期"].astype(str)) if not hist.empty else set()
    if force_days:
        have -= set(force_days)

    new_rows: List[Dict[str, Any]] = []
    for p in files:
        day = p.stem.split("_")[0]
        if day in have:
            continue
        try:
            df = pd.read_csv(p, dtype={"代码": str})
        except Exception as exc:
            logger.warning("读取 %s 失败（跳过）：%s", p.name, exc)
            continue

        items, source = extract_day_sectors(df)
        if not items:
            continue
        for name, cnt in items:
            new_rows.append({"日期": day, "题材": name, "涨停家数": cnt, "来源": source})

    if not new_rows:
        return 0

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)

    if hist.empty:
        merged = new_df
    else:
        keep = hist
        if force_days:
            keep = hist[~hist["日期"].isin(force_days)]
        merged = pd.concat([keep, new_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=["日期", "题材"], keep="last")

    save_history(merged)
    logger.info("题材历史：新增/更新 %d 行，累计 %d 行（%d 天）",
                len(new_rows), len(merged), merged["日期"].nunique())
    return len(new_rows)


def build_stack_series(days: int = 10, top_show: int = TOP_SHOW) -> Dict[str, Any]:
    """
    构造堆叠柱状图所需的数据。

    返回
    ----
    {
        "ok": bool,
        "days": ["20261101", ...],          # 横轴，升序
        "series": [                          # 每个题材一条
            {"name": "通用设备", "data": [3, 5, 7, ...]},
            ...
        ],
        "note": str,
    }

    题材选择规则
    ------------
    用「近 N 天累计涨停家数」排前 top_show 名，而不是「最近一天的前 N 名」。
    原因：后者每天剧烈跳动，图例颜色天天变，根本看不出趋势。
    累计排行更稳定，颜色一旦确定就基本不变，能看出题材的连续发酵与退潮。
    """
    hist = load_history()
    if hist.empty:
        return {"ok": False, "days": [], "series": [],
                "note": "题材历史为空。首次生成仪表盘会自动创建 calibration/sector_history.csv"}

    all_days = sorted(hist["日期"].unique())[-days:]
    if not all_days:
        return {"ok": False, "days": [], "series": [], "note": "没有可用的日期"}

    sub = hist[hist["日期"].isin(all_days)].copy()
    sub["涨停家数"] = pd.to_numeric(sub["涨停家数"], errors="coerce").fillna(0)

    # 近 N 天累计，挑出主色题材
    total_by_sector = sub.groupby("题材")["涨停家数"].sum().sort_values(ascending=False)
    top_sectors = list(total_by_sector.head(top_show).index)

    # 透视成 日期 × 题材
    pivot = sub.pivot_table(index="日期", columns="题材", values="涨停家数",
                            aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(all_days, fill_value=0)

    series: List[Dict[str, Any]] = []
    for name in top_sectors:
        if name in pivot.columns:
            series.append({"name": str(name),
                           "data": [int(v) for v in pivot[name].tolist()]})

    # 其余归「其他」
    rest_cols = [c for c in pivot.columns if c not in top_sectors]
    if rest_cols:
        rest = pivot[rest_cols].sum(axis=1).tolist()
        if any(int(v) > 0 for v in rest):
            series.append({"name": "其他", "data": [int(v) for v in rest]})

    return {
        "ok": True,
        "days": [str(d) for d in all_days],
        "series": series,
        "note": f"近 {len(all_days)} 个交易日 · 口径：东财行业（同花顺题材缺失时自动降级）",
    }


if __name__ == "__main__":
    # 单独测试：python sector_tracker.py
    import json
    from utils import setup_logger

    setup_logger("INFO")
    print("=" * 68)
    print("  题材生命周期追踪（模块四自检）")
    print("=" * 68)

    n = update_history(60)
    print(f"\n本次写入 {n} 行")

    hist = load_history()
    if hist.empty:
        print("历史为空。")
        raise SystemExit(0)

    print(f"历史文件: {HISTORY_FILE}")
    print(f"累计 {len(hist)} 行 / {hist['日期'].nunique()} 天")
    print(f"来源分布: {hist['来源'].value_counts().to_dict()}")
    print()

    last_day = hist["日期"].max()
    print(f"最近一天（{last_day}）的题材分布：")
    d = hist[hist["日期"] == last_day].sort_values("涨停家数", ascending=False)
    for _, r in d.iterrows():
        print(f"  {r['题材']:<14} {int(r['涨停家数']):>3} 家")

    print()
    print("堆叠图数据结构：")
    stack = build_stack_series(10)
    print(f"  ok={stack['ok']} 天数={len(stack['days'])} 系列数={len(stack['series'])}")
    print(f"  日期: {stack['days']}")
    for s in stack["series"]:
        print(f"  {s['name']:<14} {s['data']}")
    print()
    print("提示:", stack["note"])
