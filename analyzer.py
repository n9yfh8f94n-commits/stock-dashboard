# -*- coding: utf-8 -*-
"""
analyzer.py —— 模块二：情绪与赚钱效应计算（纯计算层）
================================================================================
设计原则（非常重要）：
    本模块**不发任何网络请求**，输入是 DataFrame，输出是 dict / DataFrame。
    这样你可以：
      ① 断网单测（用 cache/ 里的 csv 直接跑）
      ② 回测历史任意一天
      ③ 改完指标公式立刻验证，不用等接口

对外接口
--------
    calc_market_emotion(zt, zb, dt, prev_zt)      情绪红绿灯 + 8 项核心指标
    build_lianban_ladder(zt)                      连板天梯（结构化 DataFrame）
    render_ladder_md(ladder_df)                   天梯 -> Markdown 表格
    find_main_sector(zt, top_n)                   主线板块识别
    build_watchlist(zt, n)                        明日核心盯盘标的
    analyze(data)                                 一步到位，返回全部结果

【口径说明 —— 为什么这么算】
    真实涨停数 = 涨停池行数（收盘时点仍封住涨停的家数）
    有效涨停数 = 真实涨停数 - 次新股（名称以 N/C 开头）
                 N = 上市首日，C = 上市第 2~5 日。这两类没有接力价值，
                 算进情绪会虚高。
    炸板率     = 炸板数 / (涨停数 + 炸板数)
                 分母是"今日曾涨停过的全部家数"，衡量的是**封板成功率**。
                 炸板率越高 = 分歧越大 = 越容易退潮。
    赚钱效应   = 昨日涨停股在**今日**的平均涨跌幅
                 这是全系统最重要的单一指标：它直接回答"昨天打板的人今天赚钱了吗"。
                 负数 = 亏钱效应 = 任何接力都是接刀。
================================================================================
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from config import (
    EMOTION_RULES,
    EMOTION_THRESHOLDS,
    LADDER_MAX_LEVEL,
    MAINLINE_TOP_N,
    POSITION_ADVICE,
    SECTOR_ALIAS,
)
from utils import logger, safe_div

# ==============================================================================
# 常量
# ==============================================================================
# 次新股识别：交易所对上市初期股票的简称规则
#   N 开头 = 上市首日；C 开头 = 上市第 2~5 日
# 【注意】这是"名称规则"，会被临时停牌/更名影响，属近似识别。
NEW_STOCK_PATTERN = r"^[NC]"

# 情绪档位 -> 展示用 emoji（与 config.EMOTION_RULES 保持一致）
STATUS_EMOJI: Dict[str, str] = {
    "冰点": "🔵",
    "退潮": "🟢",   # A 股习惯：跌/亏钱用绿色
    "震荡": "⚪",
    "回暖": "🟡",
    "高潮": "🔴",   # 涨/赚钱用红色
}


# ==============================================================================
# 一、情绪红绿灯
# ==============================================================================
def calc_market_emotion(
    zt_df: Optional[pd.DataFrame] = None,
    zb_df: Optional[pd.DataFrame] = None,
    dt_df: Optional[pd.DataFrame] = None,
    prev_zt_df: Optional[pd.DataFrame] = None,
    lhb_df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    计算市场情绪全景。

    参数
    ----
    zt_df      : 今日涨停池（data_fetch.fetch_zt_pool 的输出，已过滤 ST/北交所）
    zb_df      : 今日炸板池
    dt_df      : 今日跌停池
    prev_zt_df : 昨日涨停池（其「涨跌幅」= 这批票今日的表现）
    lhb_df     : 今日龙虎榜（可选，用于统计上榜家数）

    返回 dict 的键（全部为 Python 原生类型，方便直接进 JSON / 模板）
    --------------------------------------------------------------
    total_zt          真实涨停家数
    valid_zt          有效涨停家数（剔除 N/C 次新）
    new_stock_cnt     被剔除的次新家数
    total_zb          炸板家数
    zb_rate           炸板率（%）
    seal_rate         封板率（%）= 涨停 / (涨停+炸板)
    dt_cnt            跌停家数
    zt_dt_ratio       涨跌停比 = 涨停 / 跌停
    max_board         最高连板高度
    max_board_stocks  最高板股票名（可能多只）
    board_dist        连板分布 dict，如 {1: 45, 2: 8, 6: 1}
    emotional_cnt     2板及以上家数（接力情绪的核心观察对象）
    prev_zt_cnt       昨日涨停股家数
    avg_prev_zt_pct   昨日涨停股今日平均涨跌幅（赚钱效应核心指标）
    med_prev_zt_pct   中位数（抗极端值，比均值更稳）
    prev_zt_up_ratio  昨日涨停股今日上涨占比（%）
    prev_zt_lu_ratio  昨日涨停股今日继续涨停占比（%）= 连板晋级率
    prev_zt_dt_cnt    昨日涨停股今日跌停家数（"打板变接刀"）
    high_board_avg_pct 昨日连板(>=2板)股今日均涨幅（接力溢价，比全体更精准）
    status            情绪档位：冰点/退潮/震荡/回暖/高潮
    status_label      带 emoji 的展示文本，如 "🔴 高潮"
    status_desc       该档位的解释文案
    color             展示色（A股习惯：红涨绿跌）
    position          仓位建议文本
    warnings          告警列表（如亏钱效应、炸板率过高）
    """

    # ---------- 空表统一处理：任何输入都可能是 None 或空表 ----------
    zt = _as_df(zt_df)
    zb = _as_df(zb_df)
    dt = _as_df(dt_df)
    pz = _as_df(prev_zt_df)
    lhb = _as_df(lhb_df)

    # ---------- 1. 真实涨停 / 有效涨停 ----------
    total_zt = len(zt)
    if total_zt and "名称" in zt.columns:
        is_new = zt["名称"].astype("string").str.strip().str.match(NEW_STOCK_PATTERN, na=False)
        new_cnt = int(is_new.sum())
        valid_zt = total_zt - new_cnt
    else:
        new_cnt = 0
        valid_zt = total_zt

    # ---------- 2. 炸板率 / 封板率 ----------
    total_zb = len(zb)
    denom = total_zt + total_zb
    zb_rate = safe_div(total_zb, denom) * 100 if denom > 0 else 0.0
    seal_rate = safe_div(total_zt, denom) * 100 if denom > 0 else 0.0

    # ---------- 3. 跌停 ----------
    dt_cnt = len(dt)
    # 涨跌停比：跌停为 0 时返回 None（渲染时显示 ∞），不要用 0 或假数字冒充，
    # 否则报告里会出现"涨跌停比 54.0"这种看不懂的数
    zt_dt_ratio = round(total_zt / dt_cnt, 2) if dt_cnt > 0 else None

    # ---------- 4. 连板高度与分布 ----------
    max_board, max_board_stocks, board_dist, emotional_cnt = _board_stats(zt)

    # ---------- 5. 赚钱效应（本模块的核心） ----------
    money = _money_effect(pz)

    # ---------- 6. 情绪判定 ----------
    status, status_desc, color, status_reason = _judge_emotion(
        total_zt=total_zt,
        zb_rate=zb_rate,
        avg_pct=money["avg_prev_zt_pct"],
        max_board=max_board,
    )

    # 分歧市标记：封板质量差（炸板率高）但接力仍赚钱 —— 最容易做错的环境
    # 这种环境下"看起来要退潮"，实际是资金在换手，仓位该降但不必空仓
    is_divergence = bool(
        zb_rate >= EMOTION_THRESHOLDS["ebb_zb_rate_min"]
        and money["avg_prev_zt_pct"] >= 0
        and status == "震荡"
    )

    # ---------- 7. 告警 ----------
    warnings: List[str] = []
    if money["avg_prev_zt_pct"] < 0:
        warnings.append(
            f"🔴 亏钱效应：昨日涨停股今日平均 {money['avg_prev_zt_pct']:+.2f}%，"
            f"打板即亏，禁止接力任何高位股"
        )
    if zb_rate >= EMOTION_THRESHOLDS["ebb_zb_rate_min"]:
        warnings.append(
            f"⚠️ 炸板率 {zb_rate:.2f}% 超过 {EMOTION_THRESHOLDS['ebb_zb_rate_min']:.0f}%，"
            f"封板质量极差，注意分歧转一致失败"
        )
    if money["prev_zt_dt_cnt"] >= 3:
        warnings.append(f"💀 昨日涨停股中 {money['prev_zt_dt_cnt']} 只今日跌停（打板变接刀）")
    if max_board >= 6:
        warnings.append(f"👀 最高板已达 {max_board} 板，注意空间压制与断板风险")
    if total_zt == 0:
        warnings.append("❌ 涨停池为空，数据可能缺失，本报告情绪结论不可信")
    elif total_zt + total_zb < 10:
        # 样本过小：此时炸板率这类比例指标极不稳定（1只炸板就能让炸板率达到 50%），
        # 若不告警，字段改名导致的"部分数据缺失"会被误读成"市场冰点"
        warnings.append(
            f"⚠️ 涨停+炸板仅 {total_zt + total_zb} 家，样本过小，"
            f"炸板率等比例指标统计意义有限，请先确认数据是否完整"
        )

    return {
        # 涨停 / 炸板 / 跌停
        "total_zt": total_zt,
        "valid_zt": valid_zt,
        "new_stock_cnt": new_cnt,
        "total_zb": total_zb,
        "zb_rate": round(zb_rate, 2),
        "seal_rate": round(seal_rate, 2),
        "dt_cnt": dt_cnt,
        "zt_dt_ratio": zt_dt_ratio,
        # 连板
        "max_board": max_board,
        "max_board_stocks": max_board_stocks,
        "board_dist": board_dist,
        "emotional_cnt": emotional_cnt,
        # 赚钱效应
        **money,
        # 情绪结论
        "status": status,
        "status_label": f"{STATUS_EMOJI.get(status, '⚪')} {status}",
        "status_desc": status_desc,
        "status_reason": status_reason,   # 判定依据（命中哪条规则），避免"结论无来源"
        "is_divergence": is_divergence,   # 分歧市标记
        "color": color,
        "position": POSITION_ADVICE.get(status, "无建议"),
        "warnings": warnings,
        # 龙虎榜上榜家数（有就带上）
        "lhb_cnt": len(lhb),
    }


def _board_stats(zt: pd.DataFrame) -> Tuple[int, List[str], Dict[int, int], int]:
    """
    连板统计。

    返回 (最高板, 最高板股票名列表, 连板分布, 2板及以上家数)

    注意：连板数缺失时按 1 板处理（首板），而不是丢弃——
         丢掉会让涨停家数统计和天梯对不上。
    """
    if zt.empty or "连板数" not in zt.columns:
        return 0, [], {}, 0

    lb = pd.to_numeric(zt["连板数"], errors="coerce").fillna(1).astype(int)
    max_board = int(lb.max())

    if "名称" in zt.columns:
        names = zt.loc[lb == max_board, "名称"].astype(str).tolist()
    else:
        names = []

    dist = lb.value_counts().sort_index().to_dict()
    dist = {int(k): int(v) for k, v in dist.items()}
    emotional = int((lb >= 2).sum())

    return max_board, names, dist, emotional


def _money_effect(pz: pd.DataFrame) -> Dict[str, Any]:
    """
    打板赚钱效应 —— 全系统最重要的指标。

    输入：昨日涨停池（其「涨跌幅」是这批票**今日**的表现）

    为什么同时给均值和中位数：
        均值会被个别 20cm 连板（+20%）严重拉高。2026-11-09 实测均值 +4.17%，
        但实际大部分昨日涨停股今天是小幅震荡——只看均值会误判为"赚钱效应极强"。
        中位数更接近你的真实体感。
    """
    empty = {
        "prev_zt_cnt": len(pz),
        "avg_prev_zt_pct": 0.0,
        "med_prev_zt_pct": 0.0,
        "prev_zt_up_ratio": 0.0,
        "prev_zt_lu_ratio": 0.0,
        "prev_zt_dt_cnt": 0,
        "high_board_avg_pct": 0.0,
    }
    if pz.empty or "涨跌幅" not in pz.columns:
        return empty

    pct = pd.to_numeric(pz["涨跌幅"], errors="coerce").dropna()
    if pct.empty:
        return empty

    cnt = len(pct)
    # 上涨占比：>0 家数 / 总家数
    up_ratio = safe_div((pct > 0).sum(), cnt) * 100
    # 继续涨停占比（连板晋级率）：>=9.8% 近似视为涨停（覆盖 10cm 主板）
    lu_ratio = safe_div((pct >= 9.8).sum(), cnt) * 100
    # 跌停家数：<= -9.8%
    dt_cnt = int((pct <= -9.8).sum())

    # 昨日连板股（>=2板）今日表现 —— 接力溢价，比全体均值更能反映"接力能不能赚钱"
    high_avg = 0.0
    if "昨日连板数" in pz.columns:
        hb = pd.to_numeric(pz["昨日连板数"], errors="coerce").fillna(1)
        sub = pct[hb >= 2]
        if len(sub):
            high_avg = float(sub.mean())

    return {
        "prev_zt_cnt": cnt,
        "avg_prev_zt_pct": round(float(pct.mean()), 2),
        "med_prev_zt_pct": round(float(pct.median()), 2),
        "prev_zt_up_ratio": round(up_ratio, 2),
        "prev_zt_lu_ratio": round(lu_ratio, 2),
        "prev_zt_dt_cnt": dt_cnt,
        "high_board_avg_pct": round(high_avg, 2),
    }


def _judge_emotion(total_zt: int, zb_rate: float, avg_pct: float, max_board: int) -> Tuple[str, str, str, str]:
    """
    情绪档位判定（顺序敏感，命中即返回）。

    判定顺序：冰点 -> 退潮 -> 高潮 -> 回暖 -> 震荡（兜底）

    为什么这个顺序：
        冰点必须在退潮之前——冰点的炸板率也高，若不先判冰点，
        每次冰点都会被误判成普通的"退潮"，错过最该空仓的信号。

    为什么退潮要求 and（这是用实测数据修出的关键修正）：
        初版写成 `炸板率 >= 35% or 赚钱效应 < 0`，结果 2026-11-09 被判成退潮，
        但当天昨日涨停股均值 +4.17%、上涨占比 65.6%——打板的人在赚钱。
        高炸板率 + 正赚钱效应 = **分歧**（多空对打），不是退潮（一致看空）。
        真退潮必须是"封不住 + 接了就被埋"同时成立。
        只有在炸板率极端到 50% 以上时，才单条件判退潮（封板体系已崩）。

    返回 (档位, 描述, 颜色, 判定依据)
    """
    t = EMOTION_THRESHOLDS
    rule_map = {r["level"]: r for r in EMOTION_RULES}

    def _pack(level: str, reason: str) -> Tuple[str, str, str, str]:
        r = rule_map.get(level, {})
        return level, r.get("desc", ""), r.get("color", "#57606a"), reason

    # ---- 冰点：炸板率极高 且 涨停家数萎缩 ----
    if zb_rate >= t["ice_zb_rate_min"] and total_zt <= t["ice_zt_max"]:
        return _pack(
            "冰点",
            f"炸板率 {zb_rate:.1f}% ≥ {t['ice_zb_rate_min']:.0f}% 且涨停仅 {total_zt} 家"
            f" ≤ {t['ice_zt_max']:.0f} 家",
        )

    # ---- 退潮：封板差 + 亏钱（双条件），或炸板率极端 ----
    if (zb_rate >= t["ebb_zb_rate_min"] and avg_pct < t["ebb_prev_zt_pct_max"]) \
            or zb_rate >= t["ebb_zb_rate_alone"]:
        if zb_rate >= t["ebb_zb_rate_alone"]:
            reason = f"炸板率 {zb_rate:.1f}% ≥ {t['ebb_zb_rate_alone']:.0f}%（封板体系已崩）"
        else:
            reason = (f"炸板率 {zb_rate:.1f}% ≥ {t['ebb_zb_rate_min']:.0f}% "
                      f"且赚钱效应 {avg_pct:+.2f}% < 0")
        return _pack("退潮", reason)

    # ---- 高潮：家数多 + 封板好 + 有高度 ----
    if (total_zt >= t["climax_zt_min"]
            and zb_rate <= t["climax_zb_rate_max"]
            and max_board >= t["climax_max_board_min"]):
        return _pack(
            "高潮",
            f"涨停 {total_zt} 家 ≥ {t['climax_zt_min']:.0f}、炸板率 {zb_rate:.1f}% "
            f"≤ {t['climax_zb_rate_max']:.0f}%、最高 {max_board} 板 ≥ {t['climax_max_board_min']:.0f}",
        )

    # ---- 回暖：封板改善 + 打板有钱赚 ----
    if zb_rate <= t["warm_zb_rate_max"] and avg_pct > t["warm_prev_zt_pct_min"]:
        return _pack(
            "回暖",
            f"炸板率 {zb_rate:.1f}% ≤ {t['warm_zb_rate_max']:.0f}% 且赚钱效应 {avg_pct:+.2f}% > 0",
        )

    # ---- 震荡（兜底）：给出"为什么是震荡"的解释，而不是一句废话 ----
    if zb_rate >= t["ebb_zb_rate_min"] and avg_pct >= 0:
        reason = (
            f"炸板率 {zb_rate:.1f}% 偏高但赚钱效应 {avg_pct:+.2f}% 为正 —— "
            f"多空分歧（封板质量差 ≠ 接力亏钱），属于典型分歧市"
        )
    elif zb_rate < t["warm_zb_rate_max"] and avg_pct <= 0:
        reason = (
            f"炸板率 {zb_rate:.1f}% 不高但赚钱效应 {avg_pct:+.2f}% 为负 —— "
            f"能封住但接力不赚，属于缩量存量博弈"
        )
    else:
        reason = f"炸板率 {zb_rate:.1f}%、赚钱效应 {avg_pct:+.2f}%，未触发任何极值条件"
    return _pack("震荡", reason)


# ==============================================================================
# 二、连板天梯
# ==============================================================================
def build_lianban_ladder(
    zt_df: Optional[pd.DataFrame],
    max_level: int = LADDER_MAX_LEVEL,
) -> pd.DataFrame:
    """
    构建连板天梯（结构化输出，渲染交给 render_ladder_md）。

    返回列
    ------
    连板数    : 1, 2, 3 ... 最高板
    家数      : 该高度的涨停家数
    股票      : "名称(代码)" 用 、 连接
    领涨股    : 该高度封板资金最大的那只（"龙头"的候选）
    主线        : 该高度出现次数最多的行业
    合计封板资金: 该高度所有股票的封板资金之和（衡量该高度的资金承接强度）
    高度标记  : 👑 空间板 / 🔥 高位 / ''

    【与草稿的区别】
      1. 不修改入参 DataFrame（草稿里 `zt_df['连板数'] = ...` 会污染调用方数据）
      2. 不只是"名称用逗号拼起来"，而是带上代码、封板资金、行业——
         因为"6板闽发铝业"和"6板某只ST股"的信息量完全不同
      3. 明确标注空间板（最高板）
    """
    cols = ["连板数", "家数", "股票", "领涨股", "主线", "合计封板资金", "高度标记"]
    zt = _as_df(zt_df)
    if zt.empty or "连板数" not in zt.columns:
        return pd.DataFrame(columns=cols)

    # 复制一份再改，绝不动调用方的原始表
    df = zt.copy()
    df["连板数"] = pd.to_numeric(df["连板数"], errors="coerce").fillna(1).astype(int)

    if "封板资金" in df.columns:
        df["封板资金"] = pd.to_numeric(df["封板资金"], errors="coerce").fillna(0.0)
    else:
        df["封板资金"] = 0.0

    if "名称" not in df.columns:
        df["名称"] = ""
    if "代码" not in df.columns:
        df["代码"] = ""
    if "所属行业" not in df.columns:
        df["所属行业"] = "未分类"

    df["所属行业"] = df["所属行业"].map(_normalize_sector)

    max_board = int(df["连板数"].max())
    rows: List[Dict[str, Any]] = []

    # 从高到低排列，符合看盘习惯
    for level in range(max_board, 0, -1):
        tier = df[df["连板数"] == level]
        if tier.empty:
            continue

        # 该高度按封板资金降序，第一只作为"领涨股"
        tier = tier.sort_values("封板资金", ascending=False)
        lead = tier.iloc[0]

        stocks = "、".join(
            f"{str(r['名称'])}({str(r['代码'])})" for _, r in tier.iterrows()
        )

        # 该高度的主导行业（家数最多的那个）
        # 【为什么要求 >= 2 家】实测 2026-11-09 的 2 板档有 8 只票，
        # 分属 8 个不同行业，第一名"厨卫电器"只有 1 家。这种"1 家也算主导行业"
        # 会给人"这档集中在厨卫电器"的错觉，实际是完全分散。
        sector_series = tier["所属行业"].value_counts()
        if len(sector_series) == 0:
            top_sector = "-"
        elif len(sector_series) == 1:
            # 只有一只票，它自己的行业就是"主导行业"，别再标"分散"
            top_sector = str(sector_series.index[0])
        elif int(sector_series.iloc[0]) >= 2:
            top_sector = f"{sector_series.index[0]}({int(sector_series.iloc[0])})"
        else:
            top_sector = f"分散({len(sector_series)}个行业)"

        # 高度标记
        if level == max_board and max_board >= 3:
            flag = "👑 空间板"
        elif level >= 4:
            flag = "🔥 高位"
        elif level == 1:
            flag = "🌱 首板"
        else:
            flag = ""

        rows.append({
            "连板数": level,
            "家数": len(tier),
            "股票": stocks,
            "领涨股": f"{lead['名称']}({lead['代码']})",
            "主线": top_sector,
            "合计封板资金": float(tier["封板资金"].sum()),
            "高度标记": flag,
        })

        # 限制展示层数，避免高位板极多时表格过长（低位截断，保留高板）
        if len(rows) >= max_level:
            logger.debug("天梯层数超过 %d，已截断低位板", max_level)
            break

    return pd.DataFrame(rows, columns=cols)


def render_ladder_md(ladder: pd.DataFrame, max_stocks_per_row: int = 15) -> str:
    """
    天梯 DataFrame -> Markdown 表格字符串。

    单独拆出来的原因：analyzer 只产数据，渲染属表现层。
                    这样同一份天梯既能进 Markdown，也能进 HTML/图表。

    max_stocks_per_row：单行最多列几只股票。首板经常 40~50 只，
    全列出来会把 Markdown 表格撑成一条长蛇，可读性归零。
    超出时截断为"前 N 只 … 等 M 只"，完整名单仍在 DataFrame 里。
    """
    if ladder is None or ladder.empty:
        return "> ⚠️ 暂无连板数据（涨停池为空或接口异常）\n"

    def _money(v: float) -> str:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "-"
        if v >= 1e8:
            return f"{v / 1e8:.2f}亿"
        return f"{v / 1e4:.0f}万"

    md = "| 连板高度 | 家数 | 股票池 | 领涨股(封板资金最大) | 主导行业 | 合计封板资金 |\n"
    md += "| :---: | :---: | :--- | :--- | :--- | ---: |\n"
    for _, r in ladder.iterrows():
        stocks = str(r["股票"])
        cnt = int(r["家数"])
        if cnt > max_stocks_per_row:
            shown = stocks.split("、")[:max_stocks_per_row]
            stocks = "、".join(shown) + f" … 等 {cnt} 只"

        md += (
            f"| {r['高度标记']} **{int(r['连板数'])}板** | {cnt} "
            f"| {stocks} | {r['领涨股']} | {r['主线']} | {_money(r['合计封板资金'])} |\n"
        )
    return md


# ==============================================================================
# 三、主线板块识别
# ==============================================================================
def find_main_sector(
    zt_df: Optional[pd.DataFrame],
    top_n: int = MAINLINE_TOP_N,
) -> pd.DataFrame:
    """
    从涨停池的「所属行业」找当日最强主线。

    【为什么不只是 value_counts()】
      草稿里 `value_counts().head(3)` 只回答了"哪个板块涨停最多"，
      但主线判断还需要三个信息：
        ① 有没有高度（板块内最高几板）—— 没高度的板块只是"普涨"，不是主线
        ② 有没有龙头（板块内封板资金最大的那只）—— 主线必须有旗帜
        ③ 占比多少（该板块涨停数 / 全市场涨停数）—— 判断是"主线"还是"分散轮动"

    返回列
    ------
    排名 板块 涨停家数 占比(%) 最高连板 连板家数 龙头股 合计封板资金 是主线
    """
    cols = ["排名", "板块", "涨停家数", "占比(%)", "最高连板", "连板家数",
            "龙头股", "合计封板资金", "是主线"]
    zt = _as_df(zt_df)
    if zt.empty or "所属行业" not in zt.columns:
        return pd.DataFrame(columns=cols)

    df = zt.copy()
    df["所属行业"] = df["所属行业"].map(_normalize_sector)
    df = df[df["所属行业"].notna() & (df["所属行业"].astype(str).str.strip() != "")]
    if df.empty:
        return pd.DataFrame(columns=cols)

    df["连板数"] = pd.to_numeric(df.get("连板数"), errors="coerce").fillna(1).astype(int)
    df["封板资金"] = pd.to_numeric(df.get("封板资金"), errors="coerce").fillna(0.0)
    total = len(df)

    grouped = df.groupby("所属行业", dropna=False)
    records: List[Dict[str, Any]] = []

    for sector, g in grouped:
        cnt = len(g)
        max_lb = int(g["连板数"].max())
        lb_cnt = int((g["连板数"] >= 2).sum())
        # 龙头 = 板块内 (连板数, 封板资金) 双维度最强的那只：先比连板，再比封单
        lead_row = g.sort_values(["连板数", "封板资金"], ascending=False).iloc[0]
        lead = f"{lead_row.get('名称', '')}({lead_row.get('代码', '')})"

        records.append({
            "板块": str(sector),
            "涨停家数": cnt,
            "占比(%)": round(safe_div(cnt, total) * 100, 1),
            "最高连板": max_lb,
            "连板家数": lb_cnt,
            "龙头股": lead,
            "合计封板资金": float(g["封板资金"].sum()),
        })

    out = pd.DataFrame(records)
    # 排序逻辑：先看家数，家数相同看高度 —— 因为"5家涨停1个高板" 比 "5家全是首板" 更强
    out = out.sort_values(["涨停家数", "最高连板", "合计封板资金"],
                          ascending=[False, False, False]).reset_index(drop=True)

    # 主线判定：家数达标 且 占比达标
    out["是主线"] = (
        (out["涨停家数"] >= EMOTION_THRESHOLDS["mainline_min_count"])
        & (out["占比(%)"] >= EMOTION_THRESHOLDS["mainline_concentration"] * 100)
    )
    out.insert(0, "排名", range(1, len(out) + 1))

    return out.head(top_n)[cols]


def sector_concentration(main_df: pd.DataFrame) -> Dict[str, Any]:
    """
    主线集中度：衡量当日涨停是"扎堆一个方向"还是"散乱轮动"。

    集中度高（top1 占比 > 25%）= 主线明确，敢重仓做龙头
    集中度低（top1 占比 < 15%）= 轮动散乱，只能做首板快进快出
    """
    if main_df is None or main_df.empty:
        return {"top1_sector": "-", "top1_share": 0.0, "level": "无数据", "desc": ""}

    top1 = main_df.iloc[0]
    share = float(top1["占比(%)"])

    if share >= 25:
        level, desc = "高集中", "主线明确，可围绕龙头做接力，容错率较高"
    elif share >= 15:
        level, desc = "中集中", "有主线但不够强，做主线内低位补涨比追高安全"
    else:
        level, desc = "低集中", "题材散乱轮动，无明确主线，只做首板且快进快出"

    return {
        "top1_sector": str(top1["板块"]),
        "top1_share": share,
        "level": level,
        "desc": desc,
    }


# ==============================================================================
# 四、明日核心盯盘标的
# ==============================================================================
def build_watchlist(
    zt_df: Optional[pd.DataFrame],
    n: int = 5,
    main_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    生成明日盯盘清单。

    选股逻辑（比草稿的"nlargest(5, '连板数')"更实用）
    ------------------------------------------------
      草稿的做法只按连板数取前 5 名，问题是：
        ① 如果空间板是独立个股（没有板块效应），它断板对市场影响不大，不值得盯
        ② 真正该盯的是"主线里的最高板"——它断板才会带崩整个板块

    但实测 2026-11-09 暴露了反面问题：完全按"是否主线内"优先排序时，
    6 板空间板闽发铝业（属工业金属，不在主线前 5）被挤出了盯盘清单。
    空间板是次日全市场的情绪锚，**必须永远在清单里**。

    因此最终排序键（优先级从高到低）：
        ① 是否空间板（最高连板）
        ② 是否真主线内（main_df["是主线"] == True）
        ③ 连板数
        ④ 封板资金

    如果当日没有任何板块达到主线标准，则用「相对最强方向」（top1 板块）
    给盯盘理由打标，并明确标注"未达主线标准"，不假装有主线。
    """
    cols = ["代码", "名称", "连板数", "所属行业", "封板资金", "盯盘理由"]
    zt = _as_df(zt_df)
    if zt.empty:
        return pd.DataFrame(columns=cols)

    df = zt.copy()
    df["连板数"] = pd.to_numeric(df.get("连板数"), errors="coerce").fillna(1).astype(int)
    df["封板资金"] = pd.to_numeric(df.get("封板资金"), errors="coerce").fillna(0.0)

    # 行业列缺失时的安全处理（不要用 if/else 给标量，直接建列）
    if "所属行业" in df.columns:
        df["所属行业"] = df["所属行业"].map(_normalize_sector)
    else:
        df["所属行业"] = "未分类"

    # ---- 主线集合：区分"真主线"和"相对最强方向" ----
    real_main: set = set()
    rel_strong: Optional[str] = None
    if main_df is not None and not main_df.empty:
        if "是主线" in main_df.columns:
            real_main = set(main_df.loc[main_df["是主线"], "板块"].astype(str).tolist())
        rel_strong = str(main_df.iloc[0]["板块"])

    max_board = int(df["连板数"].max())
    df["_is_space"] = (df["连板数"] == max_board).astype(int)   # ① 空间板
    df["_in_main"] = df["所属行业"].isin(real_main).astype(int)  # ② 真主线内

    df = df.sort_values(
        ["_is_space", "_in_main", "连板数", "封板资金"],
        ascending=[False, False, False, False],
    )
    top = df.head(n).copy()

    # ---- 逐只生成盯盘理由（说清"为什么盯它"）----
    reasons: List[str] = []
    for _, r in top.iterrows():
        parts: List[str] = []
        is_space = int(r["连板数"]) == max_board and max_board >= 2
        if is_space:
            parts.append(f"空间板{max_board}连")
        elif int(r["连板数"]) >= 2:
            parts.append(f"{int(r['连板数'])}连板")   # 空间板已含连板数，不重复写
        if r["_in_main"] == 1:
            parts.append("主线内")
        elif rel_strong and str(r["所属行业"]) == rel_strong:
            parts.append("相对最强方向")
        if r["封板资金"] >= 3e8:
            parts.append("封单超3亿")
        reasons.append("·".join(parts) if parts else "首板观察")

    top["盯盘理由"] = reasons
    return top[cols].reset_index(drop=True)


# ==============================================================================
# 五、一步到位
# ==============================================================================
def analyze(data: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """
    输入 data_fetch.fetch_all() 返回的 data 字典，输出报告所需的全部计算结果。

    data 的键约定：zt / zb / dt / prev_zt / lhb / yyb / index
    """
    zt = data.get("zt", pd.DataFrame())
    zb = data.get("zb", pd.DataFrame())
    dt = data.get("dt", pd.DataFrame())
    pz = data.get("prev_zt", pd.DataFrame())
    lhb_df = data.get("lhb", pd.DataFrame())

    emotion = calc_market_emotion(zt, zb, dt, pz, lhb_df)
    ladder = build_lianban_ladder(zt)
    main_df = find_main_sector(zt)
    conc = sector_concentration(main_df)
    watch = build_watchlist(zt, n=5, main_df=main_df)

    logger.info(
        "情绪：%s | 涨停 %d(有效%d) 炸板 %d 跌停 %d | 炸板率 %.2f%% | 赚钱效应 %+.2f%% | 最高 %d 板",
        emotion["status_label"], emotion["total_zt"], emotion["valid_zt"],
        emotion["total_zb"], emotion["dt_cnt"], emotion["zb_rate"],
        emotion["avg_prev_zt_pct"], emotion["max_board"],
    )

    return {
        "emotion": emotion,
        "ladder": ladder,
        "main_sector": main_df,
        "concentration": conc,
        "watchlist": watch,
    }


# ==============================================================================
# 内部工具
# ==============================================================================
def _as_df(df: Any) -> pd.DataFrame:
    """把 None / 非 DataFrame 统一成 DataFrame，避免满屏 if df is None。"""
    if isinstance(df, pd.DataFrame):
        return df
    return pd.DataFrame()


def _normalize_sector(name: Any) -> str:
    """
    行业名规范化：修掉东财截断（'汽车零部' -> '汽车零部件'）。
    未收录的名字原样返回，不影响聚合。
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return "未分类"
    s = str(name).strip()
    if not s:
        return "未分类"
    return SECTOR_ALIAS.get(s, s)


# ==============================================================================
# 断网单测入口：python analyzer.py 20261109
# ==============================================================================
if __name__ == "__main__":
    import sys

    from utils import setup_logger, clean_stock_df

    setup_logger()
    day = sys.argv[1] if len(sys.argv) > 1 else "20261109"

    # 直接读本地缓存，完全不联网
    from pathlib import Path
    from config import CACHE_DIR

    def _load(key: str, code_col: bool = True) -> pd.DataFrame:
        p: Path = CACHE_DIR / f"{day}_{key}.csv"
        if not p.exists():
            print(f"[跳过] 缓存不存在：{p.name}")
            return pd.DataFrame()
        return pd.read_csv(p, dtype={"代码": str} if code_col else None, encoding="utf-8-sig")

    data = {
        "zt": _load("zt_pool"),
        "zb": _load("zb_pool"),
        "dt": _load("dt_pool"),
        "prev_zt": _load("prev_zt_pool"),
        "lhb": _load("lhb"),
        "yyb": _load("active_yyb", code_col=False),
    }

    # 离线读取的是"接口原始快照"，必须先走一遍清洗，
    # 否则单测口径会与线上（fetch_* 已清洗）不一致 —— 例如龙虎榜里的可转债
    for _k in ("zt", "zb", "dt", "prev_zt", "lhb"):
        if not data[_k].empty:
            data[_k] = clean_stock_df(data[_k], dedup=(_k != "lhb"),
                                      name_col="名称" if "名称" in data[_k].columns else "名称")

    res = analyze(data)
    e = res["emotion"]

    # 控制台打印时限制列宽，否则"股票池"那一列有 45 只票会把表格撑爆
    pd.set_option("display.max_colwidth", 40)
    pd.set_option("display.width", 200)

    print("\n" + "=" * 74)
    print(f"  {day} 情绪复盘（离线模式，数据来自本地缓存）")
    print("=" * 74)
    print(f"  情绪档位      : {e['status_label']}  —— {e['status_desc']}")
    print(f"  判定依据      : {e['status_reason']}")
    if e["is_divergence"]:
        print("  分歧标记      : ⚠️ 分歧市（封板差但接力仍赚钱，仓位降但不必空仓）")
    print(f"  真实/有效涨停 : {e['total_zt']} / {e['valid_zt']} 家（剔除次新 {e['new_stock_cnt']}）")
    print(f"  炸板 / 炸板率 : {e['total_zb']} / {e['zb_rate']}%   封板率 {e['seal_rate']}%")
    print(f"  跌停/涨跌停比 : {e['dt_cnt']} / {e['zt_dt_ratio'] if e['zt_dt_ratio'] else '∞'}")
    print(f"  最高连板      : {e['max_board']} 板（{'、'.join(e['max_board_stocks'])}）")
    print(f"  连板分布      : {e['board_dist']}   2板以上 {e['emotional_cnt']} 家")
    print(f"  赚钱效应      : 均值 {e['avg_prev_zt_pct']:+.2f}% / 中位数 {e['med_prev_zt_pct']:+.2f}%")
    print(f"                  上涨占比 {e['prev_zt_up_ratio']}% / 晋级率 {e['prev_zt_lu_ratio']}% / 跌停 {e['prev_zt_dt_cnt']} 家")
    print(f"                  昨日连板股今日均值 {e['high_board_avg_pct']:+.2f}%")
    print(f"  仓位建议      : {e['position']}")
    for w in e["warnings"]:
        print(f"  {w}")

    print("\n--- 连板天梯 ---")
    if res["ladder"].empty:
        print("无数据")
    else:
        for _, r in res["ladder"].iterrows():
            print(f"  {r['高度标记']:>6} {int(r['连板数'])}板  家数{int(r['家数']):>2}  {r['股票']}")

    print("\n--- 主线板块 ---")
    print(res["main_sector"].to_string(index=False) if not res["main_sector"].empty else "无数据")
    c = res["concentration"]
    print(f"\n主线集中度：{c['level']} —— 第一方向 {c['top1_sector']} 占 {c['top1_share']}% ｜ {c['desc']}")

    print("\n--- 明日盯盘 ---")
    print(res["watchlist"].to_string(index=False) if not res["watchlist"].empty else "无数据")
    print()
