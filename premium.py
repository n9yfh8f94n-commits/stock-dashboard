#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
premium.py —— 次日溢价预期统计（模块三）
================================================================================

要回答的问题
------------
「明天去接 3 板，历史上到底赚不赚钱？」
用近 30 个交易日的真实数据，按连板高度分组统计次日的表现。

数据来源与口径（**这一点必须看清**）
------------------------------------
唯一现成的个股级数据是 `cache/{day}_prev_zt_pool.csv`（东财「昨日涨停池」），
它的每一行是「T-1 日涨停的某只票」在 **T 日** 的结果：
    · 昨日连板数  -> 该股在 T-1 日的高度
    · 涨跌幅      -> 该股在 T 日的涨跌幅（收盘口径）

所以本模块算的是「**次日收盘涨跌幅**」，不是「次日竞价高开幅度」。

为什么不算「高开」：
    高开 = 次日开盘价 vs 昨收，必须有**开盘价**。
    东财涨停池 / 昨日涨停池都不提供开盘价字段；
    用「振幅」和「涨跌幅」两个方程解不出开盘价（三个未知量）。
    要补开盘价只能逐股抓历史日K——30 天约 500 只股票、500 次请求，
    在当前东财 push2 限流的状态下很容易批量失败。

    结论：用收盘口径。它回答的问题其实更接近你真正关心的
    「接了之后能不能赚钱」，而不是「开盘会不会红」。
    两者的差别是：低开高走的票，高开概率算它输，但收盘口径算它赢。

统计口径
--------
    · 分组：1板 / 2板 / 3板 / 4板及以上
    · 平均：次日涨跌幅的算术平均
    · 中位数：抗极值，比均值更能代表「典型情况」
    · 上涨概率：次日涨跌幅 > 0 的占比

为什么要中位数：涨停板次日的收益分布是右偏的（少数票连板，多数票小跌），
均值会被几只妖股拉高，中位数更接近你实际能拿到的结果。

异常值处理
----------
    · 涨跌幅超出 ±30% 的剔除（新股、复牌、数据错误）
    · 名称含 ST / 退 的剔除（涨跌停幅度不同，不能和主板混着统计）

样本不足
--------
    · 单档样本 < 5  -> 该档不出结论
    · 总样本 < 20   -> 整个模块降级提示，不报错
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

import config
from utils import logger

PREV_POOL_DIR = config.BASE_DIR / "cache"

# (标签, 连板数下限, 连板数上限)
BUCKETS: List[tuple] = [
    ("1板", 1, 1),
    ("2板", 2, 2),
    ("3板", 3, 3),
    ("4板+", 4, 99),
]

MIN_GROUP_SAMPLE = 5      # 单档最少样本
MIN_TOTAL_SAMPLE = 20     # 总样本下限
PCT_FLOOR, PCT_CEIL = -30.0, 30.0   # 异常值护栏


def _list_files(days: int) -> List[Path]:
    """按日期倒序取最近 N 个「昨日涨停池」快照。"""
    if not PREV_POOL_DIR.exists():
        return []
    files = sorted(PREV_POOL_DIR.glob("*_prev_zt_pool.csv"), reverse=True)
    return files[:days]


def build_premium_stats(days: int = 30) -> Dict[str, Any]:
    """
    统计各连板高度的次日表现。

    返回
    ----
    {
        "ok": bool,                 # 数据是否够用
        "days_used": int,           # 实际用了几个交易日
        "total_samples": int,       # 总样本数
        "groups": [                 # 每档一条
            {"label","n","avg","median","win_rate","enough"}
        ],
        "best": str,                # 盈亏比最好的档位（样本够的前提下）
        "note": str,                # 给页面显示的提示
    }
    """
    files = _list_files(days)
    if not files:
        return {
            "ok": False, "days_used": 0, "total_samples": 0, "groups": [],
            "best": "",
            "note": "没有找到「昨日涨停池」缓存，无法统计次日溢价。"
                    "先跑几天 main.py 积累数据；或检查 cache/*_prev_zt_pool.csv",
        }

    frames: List[pd.DataFrame] = []
    for p in files:
        try:
            df = pd.read_csv(p, dtype={"代码": str})
        except Exception as exc:
            logger.warning("读取 %s 失败（跳过）：%s", p.name, exc)
            continue
        if df.empty:
            continue

        need = {"涨跌幅", "昨日连板数"}
        if not need.issubset(df.columns):
            logger.warning("%s 缺字段 %s（跳过）", p.name, need - set(df.columns))
            continue

        sub = df[["代码", "名称", "涨跌幅", "昨日连板数"]].copy()
        sub["快照日"] = p.stem.split("_")[0]
        frames.append(sub)

    if not frames:
        return {
            "ok": False, "days_used": 0, "total_samples": 0, "groups": [],
            "best": "", "note": "快照文件都读不出有效字段，跳过统计。",
        }

    all_df = pd.concat(frames, ignore_index=True)

    # ---------- 清洗 ----------
    all_df["涨跌幅"] = pd.to_numeric(all_df["涨跌幅"], errors="coerce")
    all_df["昨日连板数"] = pd.to_numeric(all_df["昨日连板数"], errors="coerce")
    all_df = all_df.dropna(subset=["涨跌幅", "昨日连板数"])

    # 异常值：新股/复牌/数据错误
    all_df = all_df[(all_df["涨跌幅"] >= PCT_FLOOR) & (all_df["涨跌幅"] <= PCT_CEIL)]

    # ST / 退市：涨跌停幅度不同，混在一起会污染均值
    name_col = all_df["名称"].astype(str).str.upper()
    all_df = all_df[~name_col.str.contains("ST|退", na=False, regex=True)]

    total = int(len(all_df))
    if total < MIN_TOTAL_SAMPLE:
        return {
            "ok": False, "days_used": len(files), "total_samples": total,
            "groups": [],
            "best": "",
            "note": f"历史样本只有 {total} 条（少于 {MIN_TOTAL_SAMPLE} 条），"
                    f"统计结果不具备参考价值。建议继续每天运行 main.py 积累。",
        }

    # ---------- 分档统计 ----------
    groups: List[Dict[str, Any]] = []
    for label, lo, hi in BUCKETS:
        g = all_df[(all_df["昨日连板数"] >= lo) & (all_df["昨日连板数"] <= hi)]
        n = int(len(g))
        if n == 0:
            groups.append({"label": label, "n": 0, "avg": None,
                           "median": None, "win_rate": None, "enough": False})
            continue

        avg = float(g["涨跌幅"].mean())
        med = float(g["涨跌幅"].median())
        win = float((g["涨跌幅"] > 0).mean() * 100)

        groups.append({
            "label": label,
            "n": n,
            "avg": round(avg, 2),
            "median": round(med, 2),
            "win_rate": round(win, 1),
            "enough": n >= MIN_GROUP_SAMPLE,
        })

    # ---------- 选「盈亏比最好」的档 ----------
    # 用中位数 × 上涨概率 作为综合打分：
    # 只看均值会被妖股误导，只看概率会选中「涨一点跌很多」的档。
    candidates = [g for g in groups if g["enough"] and g["median"] is not None]
    best = ""
    if candidates:
        scored = sorted(
            candidates,
            key=lambda g: g["median"] * (g["win_rate"] / 100.0),
            reverse=True,
        )
        best = scored[0]["label"]

    note = (f"口径：次日收盘涨跌幅（非竞价高开）。"
            f"样本 {total} 条，覆盖 {len(files)} 个交易日快照。")

    return {
        "ok": True,
        "days_used": len(files),
        "total_samples": total,
        "groups": groups,
        "best": best,
        "note": note,
    }


def summarize(stats: Dict[str, Any]) -> str:
    """
    一句话总结，供日志或页面副标题使用。
    数据不足时返回提示语，不抛异常。
    """
    if not stats.get("ok"):
        return str(stats.get("note") or "样本不足")

    best = stats.get("best") or ""
    if not best:
        return "各档样本偏少，暂不下结论"

    for g in stats.get("groups", []):
        if g["label"] == best:
            return (f"{best} 次日平均 {g['avg']:+.2f}%、"
                    f"上涨概率 {g['win_rate']:.0f}%，是当前盈亏比最好的一档")
    return ""


if __name__ == "__main__":
    # 单独测试：直接跑这个文件
    import json
    from utils import setup_logger

    setup_logger("INFO")
    print("=" * 68)
    print("  次日溢价预期统计（模块三自检）")
    print("=" * 68)

    s = build_premium_stats(30)
    print(f"\n数据可用: {s['ok']}")
    print(f"快照天数: {s['days_used']}")
    print(f"总样本  : {s['total_samples']}")
    print()

    if s["groups"]:
        print(f"{'档位':<6}{'样本':>6}{'平均':>9}{'中位数':>9}{'上涨概率':>10}")
        print("-" * 42)
        for g in s["groups"]:
            if g["n"] == 0:
                print(f"{g['label']:<6}{0:>6}{'—':>9}{'—':>9}{'—':>10}")
                continue
            flag = "" if g["enough"] else "  (样本不足)"
            print(f"{g['label']:<6}{g['n']:>6}{g['avg']:>+8.2f}%"
                  f"{g['median']:>+8.2f}%{g['win_rate']:>9.1f}%{flag}")

    print()
    print("结论:", summarize(s))
    print("提示:", s["note"])
    print()
    print("完整结构:")
    print(json.dumps(s, ensure_ascii=False, indent=2))
