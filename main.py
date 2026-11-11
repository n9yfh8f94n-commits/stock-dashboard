# -*- coding: utf-8 -*-
"""
================================================================================
 main.py —— A股超短线全自动复盘系统 · 总装线
================================================================================
职责：
    1. 编排：交易日校验 -> 数据抓取 -> 情绪分析 -> 游资画像 -> 渲染 -> 落盘
    2. 渲染：把计算结果组装成一份可直接阅读的 Markdown 报告
    3. 校准：--calibrate 遍历最近 N 个交易日，输出分位数表 + 阈值诊断

设计原则：
    · 任何一个环节异常，都不中断整体 —— 报告里标注"数据缺失"，其余照常输出。
      这是"每晚无人值守自动跑"的前提。
    · 报告的每个结论都必须能追溯到具体数字。不写"情绪较好"这种无法验证的话。
    · 数据缺失时**必须显式告警**，绝不能把"接口挂了"渲染成"今天市场冰点"。

命令行：
    python main.py                        # 复盘最近交易日
    python main.py 20261109               # 复盘指定日期
    python main.py 20261109 --check       # 只测接口连通性，不生成报告
    python main.py --calibrate 30         # 校准：用最近 30 个交易日算分位数
    python main.py 20261109 --no-seat     # 跳过第二层席位抓取（更快，但看不到卖出标的）
================================================================================
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

import analyzer
import config
import lhb
from data_fetch import fetch_all, probe_zt_available
from utils import (
    fmt_money,
    fmt_pct,
    guard_intraday,
    get_trade_calendar,
    is_trade_date,
    latest_trade_date,
    setup_logger,
    to_date,
    to_day_str,
)

logger = setup_logger()

# 输出目录：统一从 config 取（config 里已基于 __file__ 转成绝对路径），
# 保证从任何工作目录运行脚本都不会把文件写到意外位置——定时任务必须依赖这一点。
REPORT_DIR = config.REPORT_DIR
CALIB_DIR = config.BASE_DIR / "calibration"

# 指标历史库：每次复盘自动追加一行，作为校准的长期样本来源。
#
# 【为什么必须有这个文件】
#   实测 2026-11-10：东财涨停池接口只保留约 20 个交易日的数据。
#       20261015 -> 0 行（已过期，取不到）
#       20261022 -> 33 行（仍在保留窗口内）
#   也就是说 `--calibrate 30` **永远不可能**真的拿到 30 天样本，
#   最多 14~20 天。用户会以为程序坏了，实际是接口的硬限制。
#
#   解法：每天复盘时顺手把当日指标存下来。样本随时间自然累积，
#   30 天、60 天、半年都能做到——只要求你每天都跑。
HISTORY_FILE = CALIB_DIR / "metrics_history.csv"


def _load_history() -> pd.DataFrame:
    """读取指标历史库（不存在或损坏时返回空表，绝不抛异常）。"""
    if not HISTORY_FILE.exists():
        return pd.DataFrame()
    try:
        h = pd.read_csv(HISTORY_FILE, dtype={"日期": str})
        h["日期"] = h["日期"].astype(str).str.zfill(8)
        return h
    except Exception as exc:
        logger.warning("指标历史库读取失败（将忽略）：%s", exc)
        return pd.DataFrame()


def _append_history(row: Any) -> None:
    """
    把指标写入历史库，支持单行 dict 或 dict 列表（批量）。

    同一日期重复写入会**覆盖**（不是追加），这样重跑同一天
    不会产生重复样本、也不会污染分位数。
    """
    try:
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        new = pd.DataFrame(row if isinstance(row, (list, tuple)) else [row])
        if new.empty or "日期" not in new.columns:
            return
        new["日期"] = new["日期"].astype(str).str.zfill(8)

        h = _load_history()
        if not h.empty:
            h = h[~h["日期"].isin(set(new["日期"]))]
            h = pd.concat([h, new], ignore_index=True)
        else:
            h = new
        h = h.sort_values("日期").reset_index(drop=True)
        h.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")
        logger.debug("指标历史库已更新：共 %d 天", len(h))
    except Exception as exc:   # 历史库是辅助功能，写失败不能影响报告生成
        logger.warning("指标历史库写入失败（不影响本次报告）：%s", exc)


# ==============================================================================
# 一、渲染层：数据 -> Markdown
# ==============================================================================
def _render_health(health: Dict[str, Dict[str, Any]]) -> str:
    """
    数据源健康状态栏。

    【为什么必须放在报告最顶部】复盘报告的结论完全依赖数据完整性。
    如果涨停池挂了而报告只写"涨停 0 家 -> 冰点"，看起来完全合理，
    但结论是错的。这类错误最危险，所以状态栏必须第一眼可见。
    """
    if not health:
        return "（无数据源信息）"

    parts: List[str] = []
    for name, info in health.items():
        if info.get("ok"):
            parts.append(f"✅ {name} {info.get('rows', 0)}行")
        else:
            err = (info.get("error") or "空表")[:28]
            parts.append(f"❌ {name}（{err}）")
    return " ｜ ".join(parts)


def _render_failed_sources(health: Dict[str, Dict[str, Any]]) -> str:
    """失效数据源的醒目告警块；全部正常则返回空串（不占版面）。"""
    failed = [k for k, v in (health or {}).items() if not v.get("ok")]
    if not failed:
        return ""
    return (
        "\n> ⚠️ **数据缺失警告**：本次有 "
        f"{len(failed)} 个数据源失败（{'、'.join(failed)}）。\n"
        "> 相关指标可能为 0 或空白，**请勿据此判断市场情绪**，先执行 "
        "`python main.py --check` 排查接口。\n"
    )


def _render_index(idx_df: pd.DataFrame) -> str:
    """三大指数概况（取不到就静默返回空串，不占版面）。"""
    if idx_df is None or idx_df.empty:
        return ""
    cols = [c for c in ["指数", "收盘", "涨跌幅", "成交额"] if c in idx_df.columns]
    if len(cols) < 3:
        return ""

    parts: List[str] = []
    for _, r in idx_df.iterrows():
        pct = r.get("涨跌幅")
        try:
            pct_f = float(pct)
        except (TypeError, ValueError):
            pct_f = 0.0
        # 中国市场惯例：涨=红、跌=绿（与欧美相反）
        arrow = "🔴" if pct_f > 0 else ("🟢" if pct_f < 0 else "⚪")
        parts.append(f"{r.get('指数', '-')} {float(r.get('收盘', 0)):.2f} {arrow}{pct_f:+.2f}%")
    return "\n> **大盘**：" + " ｜ ".join(parts) + "\n"


def _render_emotion_table(e: Dict[str, Any]) -> str:
    """情绪指标明细表。每一项都带"这个数字意味着什么"的解读。"""
    zb_rate = e.get("zb_rate", 0.0)
    seal_rate = e.get("seal_rate", 0.0)
    avg_pct = e.get("avg_prev_zt_pct", 0.0)

    # 炸板率分档说明（阈值来自 config，避免报告和逻辑两套口径）
    t = config.EMOTION_THRESHOLDS
    if zb_rate >= t["ebb_zb_rate_alone"]:
        zb_note = "封板体系已崩"
    elif zb_rate >= t["ebb_zb_rate_min"]:
        zb_note = "封板质量差"
    elif zb_rate <= t["climax_zb_rate_max"]:
        zb_note = "封板扎实"
    else:
        zb_note = "一般"

    money_note = "接力有溢价" if avg_pct > 0 else ("接力即亏钱" if avg_pct < 0 else "持平")
    money_icon = "🟢" if avg_pct > 0 else ("🔴" if avg_pct < 0 else "⚪")

    return (
        "| 指标 | 数值 | 解读 |\n"
        "| :--- | ---: | :--- |\n"
        f"| 真实涨停 | {e.get('total_zt', 0)} 家 | 东财涨停池全量（已剔 ST/退市/北交所） |\n"
        f"| 有效涨停 | {e.get('valid_zt', 0)} 家 | 再剔除 N/C 字头次新股，剔除了 {e.get('new_stock_cnt', 0)} 家 |\n"
        f"| 炸板 | {e.get('total_zb', 0)} 家 | 盘中触板未封住 |\n"
        f"| 炸板率 | **{zb_rate:.2f}%** | {zb_note} |\n"
        f"| 封板率 | {seal_rate:.2f}% | 涨停 /（涨停 + 炸板），越低越差 |\n"
        f"| 跌停 | {e.get('dt_cnt', 0)} 家 | 涨跌停比 "
        f"{e.get('total_zt', 0)}:{e.get('dt_cnt', 0) if e.get('dt_cnt', 0) else '∞'} |\n"
        f"| 最高连板 | **{e.get('max_board', 0)} 板** | {('、'.join(e.get('max_board_stocks', [])) or '-')} |\n"
        f"| 2 板以上 | {e.get('emotional_cnt', 0)} 家 | 连板分布 {e.get('board_dist', {})} |\n"
        f"| 赚钱效应(均值) | **{avg_pct:+.2f}%** | {money_icon} 昨日涨停股今日表现，{money_note} |\n"
        f"| 赚钱效应(中位数) | {e.get('med_prev_zt_pct', 0.0):+.2f}% | 中位数 > 均值说明少数票拉高了平均 |\n"
        f"| 上涨占比 | {e.get('prev_zt_up_ratio', 0.0):.2f}% | 昨日涨停股今日红盘的比例 |\n"
        f"| 晋级率 | {e.get('prev_zt_lu_ratio', 0.0):.2f}% | 昨日涨停今日继续涨停的比例 |\n"
        f"| 昨日涨停今跌停 | {e.get('prev_zt_dt_cnt', 0)} 家 | 出现即说明有票被核 |\n"
        f"| 昨日连板股均值 | {e.get('high_board_avg_pct', 0.0):+.2f}% | 只统计昨日 2 板以上的票 |\n"
    )


def _render_ladder(ladder: pd.DataFrame) -> str:
    """连板天梯（复用 analyzer 的渲染，保持单一实现）。"""
    return analyzer.render_ladder_md(ladder)


def _render_main_sector(main_df: pd.DataFrame, conc: Dict[str, Any]) -> str:
    """
    主线板块表 + 集中度结论。

    【为什么必须给集中度】只看"第一名是谁"没用——第一名只占 9% 时，
    说明根本没有主线，硬说"主线是通用设备"会误导操作。
    集中度低就该只做首板快进快出。
    """
    if main_df is None or main_df.empty:
        return "> 无板块数据（涨停池为空或缺少「所属行业」字段）。\n"

    md = "| 排名 | 板块 | 涨停家数 | 占比 | 最高连板 | 龙头 | 合计封板资金 |\n"
    md += "| :---: | :--- | ---: | ---: | :---: | :--- | ---: |\n"
    for _, r in main_df.head(5).iterrows():
        flag = "🔥 " if bool(r.get("是主线")) else ""
        md += (
            f"| {int(r.get('排名', 0))} | {flag}{r.get('板块', '-')} | {int(r.get('涨停家数', 0))} "
            f"| {float(r.get('占比(%)', 0)):.1f}% | {int(r.get('最高连板', 0))}板 "
            f"| {r.get('龙头股', '-')} | {fmt_money(r.get('合计封板资金', 0))} |\n"
        )

    md += (
        f"\n> **集中度：{conc.get('level', '-')}** —— 第一方向 "
        f"{conc.get('top1_sector', '-')} 占 {float(conc.get('top1_share', 0)):.1f}%。"
        f"{conc.get('desc', '')}\n"
    )
    return md


def _fmt_net(v: Any) -> str:
    """
    净额格式化：正负号 + 红绿点 + 单位。

    【为什么把单位写进函数】第一版返回纯数字、调用处再拼 " 万"，
    结果 None 被渲染成 "- 万"，表格里出现一堆这种残缺单元格。
    单位跟着值走，就没有拼错的可能。
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    amt = float(v)
    icon = "🟢" if amt > 0 else ("🔴" if amt < 0 else "⚪")
    return f"{icon}{amt:+,.0f} 万"


def _short_stocks(s: str, max_len: int = 58) -> str:
    """
    截断股票名单，避免撑破 Markdown 表格。

    【为什么要截】营业部合计口径的名单可能长达 10 只票带代码（150+ 字符），
    在 Typora 里会把表格拉得很宽，其它列被挤到看不见。
    按「只」截断而不是按字符硬切，保证不会出现 "超声电子(" 这种半截股票名。
    """
    if not s:
        return "-"
    parts = [p for p in str(s).split("、") if p]
    out: List[str] = []
    length = 0
    for p in parts:
        if length + len(p) + 1 > max_len:
            out.append(f"…等{len(parts)}只")
            break
        out.append(p)
        length += len(p) + 1
    return "、".join(out) if out else "-"


def _render_lhb_rank(lhb_df: pd.DataFrame, top_n: int = 6) -> str:
    """
    龙虎榜个股净买卖排名 + 机构动向。

    【为什么单独一块】游资画像回答"谁在动手"，这块回答"哪只票被资金盯上了"。
    两者互补：游资进了某只没上榜的票，或者某只票被一堆没识别出的席位买入，
    都要靠这块发现。
    """
    rank = lhb.fetch_lhb_net_rank(lhb_df, top_n=top_n)
    if rank is None or rank.empty:
        return "> 龙虎榜数据缺失或为空。\n"

    md = "| 代码 | 名称 | 涨跌幅 | 龙虎榜净买额 | 上榜原因 |\n"
    md += "| :--- | :--- | ---: | ---: | :--- |\n"
    for _, r in rank.iterrows():
        amt = r.get("龙虎榜净买额(万)")
        try:
            amt_f = float(amt)
            icon = "🟢" if amt_f > 0 else ("🔴" if amt_f < 0 else "⚪")
            amt_s = f"{icon}{amt_f:+,.0f} 万"
        except (TypeError, ValueError):
            amt_s = "-"

        pct = r.get("涨跌幅")
        try:
            pct_s = f"{float(pct):+.2f}%"
        except (TypeError, ValueError):
            pct_s = "-"

        md += (
            f"| {r.get('代码', '-')} | {r.get('名称', '-')} | {pct_s} | {amt_s} "
            f"| {r.get('上榜原因', '-')} |\n"
        )

    # 机构动向（从「解读」字段统计）
    try:
        inst = lhb.institutional_flow(lhb_df)
        if inst and inst.get("total"):
            md += (
                f"\n> **机构动向**：机构买入 {inst.get('inst_buy_cnt', 0)} 家 / "
                f"卖出 {inst.get('inst_sell_cnt', 0)} 家"
            )
            if inst.get("inst_buy_stocks"):
                md += f"；机构买入标的：{'、'.join(inst['inst_buy_stocks'][:5])}"
            md += "\n"
    except Exception as exc:   # 解读字段格式变更时不要影响主流程
        logger.debug("机构动向统计失败：%s", exc)
    return md


def _render_youzi(youzi_df: pd.DataFrame, youzi_sum: pd.DataFrame) -> str:
    """
    游资画像：聚合表（谁在动手）→ 个股席位明细（在哪只票动手）→ 营业部合计（整体态度）。

    【为什么明细要拆成两张表】
      两个口径的金额范围不同，混在一张按金额排序的表里会出现
      "同一席位一会儿 4 亿一会儿 6000 万"的矛盾观感。
      分开展示并标注口径，读者能看懂"整体 4 亿、其中这只票 5973 万"。
    """
    if youzi_sum is None or youzi_sum.empty:
        return (
            "> 今日未命中知名游资席位，或龙虎榜/活跃营业部数据缺失。\n"
            "> （注：未命中 ≠ 没有游资，只说明不在 `config.YOUZI_MAP` 的识别范围内）\n"
        )

    # ---------- 表 1：按游资聚合（双口径并列） ----------
    # 【为什么删掉了「方向」列】第一版有这一列，但它只能表达一个方向。
    #   实测炒股养家：个股席位 -607 万（卖日出东方），营业部合计 +2.02 亿（整体大买）。
    #   单列写"净卖出"就和右边的 2 亿自相矛盾，写"净买入"又掩盖了卖日出东方的事实。
    #   金额本身自带符号 + 红绿点，方向信息已经完整，多一列只会制造歧义。
    md = "### 按游资聚合\n\n"
    md += "| 游资别称 | 个股席位净额 | 营业部合计净额 | 涉及股票 | 席位/营业部数 | 提示 |\n"
    md += "| :--- | ---: | ---: | :--- | :---: | :--- |\n"
    for _, r in youzi_sum.iterrows():
        cnt = f"{int(r.get('席位记录数', 0))}/{int(r.get('营业部数', 0))}"
        seat_net = r.get("席位净额(万)")
        yyb_net = r.get("营业部净额(万)")

        # 两口径方向相反时提示——这往往是最有价值的信息（整体在做多，但某只票在出货）
        tip = ""
        try:
            if (seat_net is not None and yyb_net is not None
                    and not pd.isna(seat_net) and not pd.isna(yyb_net)
                    and float(seat_net) * float(yyb_net) < 0):
                tip = "⚠️ 席位与合计方向相反"
        except (TypeError, ValueError):
            tip = ""

        md += (
            f"| {r.get('游资', '-')} | {_fmt_net(seat_net)} "
            f"| {_fmt_net(yyb_net)} "
            f"| {r.get('涉及股票', '-')} | {cnt} | {tip} |\n"
        )
    md += (
        "\n> **口径说明**：个股席位净额 = 在**具体某只票**上的净买卖（来自席位明细）；"
        "营业部合计净额 = 该席位当日**全部股票**的净买卖（来自活跃营业部表）。\n"
        "> 两者范围重叠，**不可相加**。席位列为空表示该游资未进入盯盘清单的席位扫描范围。\n"
    )

    if youzi_df is None or youzi_df.empty:
        return md

    # ---------- 表 2：个股席位明细 ----------
    seat = youzi_df[youzi_df["口径"] == "个股席位"] if "口径" in youzi_df.columns else pd.DataFrame()
    if not seat.empty:
        md += "\n### 个股席位明细（按金额绝对值排序）\n\n"
        md += "| 游资 | 营业部 | 净额 | 操作股票 | 买卖构成 |\n"
        md += "| :--- | :--- | ---: | :--- | :--- |\n"
        for _, r in seat.head(12).iterrows():
            yyb = str(r.get("营业部", ""))
            yyb_short = yyb if len(yyb) <= 20 else yyb[:20] + "…"
            md += (
                f"| {r.get('游资', '-')} | {yyb_short} | {_fmt_net(r.get('净买入(万)'))} "
                f"| {r.get('操作股票', '-')} | {r.get('备注', '') or '-'} |\n"
            )
        if len(seat) > 12:
            md += f"\n> 另有 {len(seat) - 12} 条席位记录未列出。\n"

    # ---------- 表 3：营业部合计 ----------
    yyb_df = youzi_df[youzi_df["口径"] == "营业部合计"] if "口径" in youzi_df.columns else pd.DataFrame()
    if not yyb_df.empty:
        md += "\n### 营业部合计动向（该席位当日全部股票）\n\n"
        md += "| 游资 | 营业部 | 净额 | 该席位买入的股票 |\n"
        md += "| :--- | :--- | ---: | :--- |\n"
        for _, r in yyb_df.head(12).iterrows():
            yyb = str(r.get("营业部", ""))
            yyb_short = yyb if len(yyb) <= 20 else yyb[:20] + "…"
            note = str(r.get("备注", "") or "")
            stocks = _short_stocks(str(r.get("操作股票", "")), max_len=48)
            # 净卖出的席位：股票列是"买入名单"，必须标注清楚，否则会被误读成他在买这些票
            if note and float(r.get("净买入(万)", 0)) < 0:
                stocks = f"（买入名单）{stocks}"
            md += (
                f"| {r.get('游资', '-')} | {yyb_short} | {_fmt_net(r.get('净买入(万)'))} "
                f"| {stocks} |\n"
            )
        if len(yyb_df) > 12:
            md += f"\n> 另有 {len(yyb_df) - 12} 条营业部记录未列出。\n"
    return md


def _render_mermaid_cycle(e: Dict[str, Any]) -> str:
    """
    情绪周期定位图（Mermaid）。

    【注意】Markdown 里用 ```mermaid 代码块，Typora / Obsidian / GitHub 原生渲染，
    不需要任何额外依赖。若你的编辑器不支持，这段会退化成一段代码文本，不影响阅读。
    """
    cur = e.get("status", "震荡")
    order = ["冰点", "退潮", "震荡", "回暖", "高潮"]
    emoji = {"冰点": "🔵", "退潮": "🟢", "震荡": "⚪", "回暖": "🟡", "高潮": "🔴"}

    # 节点定义：当前档位用粗边框 + 浅黄底高亮，其余灰底
    lines = ["```mermaid", "graph LR"]
    ids = {lv: chr(ord("A") + i) for i, lv in enumerate(order)}
    for lv in order:
        lines.append(f'    {ids[lv]}["{emoji.get(lv, "")} {lv}"]')

    # 连线：顺序 + 跳到当前档位的箭头
    for i in range(len(order) - 1):
        lines.append(f"    {ids[order[i]]} --> {ids[order[i + 1]]}")

    # 样式：当前档位高亮（Mermaid 的 fill 用十六进制，不要用变量）
    for lv in order:
        if lv == cur:
            lines.append(f"    style {ids[lv]} fill:#fff3cd,stroke:#d4a017,stroke-width:3px,color:#000")
        else:
            lines.append(f"    style {ids[lv]} fill:#f6f8fa,stroke:#d0d7de,color:#57606a")

    lines.append("```")
    return "\n".join(lines) + "\n"


def _decide_action(seal_rate: float, avg_pct: float) -> Dict[str, str]:
    """
    仓位动作判定。**独立成函数**，因为图和结论必须走同一套阈值。

    【为什么会写这个函数】第一版把阈值硬编码在 Mermaid 字符串里（60%），
    而结论用的是另一套（50%），结果图上写"轻仓"、结论写"半仓" ——
    同一份报告自相矛盾。这类错误不会报错，只有人读到才发现。

    阈值依据：封板率 50% 是"封得住 vs 封不住"的分水岭；
              赚钱效应正负是"接力有钱赚 vs 接了就被埋"。
    """
    if seal_rate >= 60 and avg_pct > 0:
        return {"key": "R1", "action": "进攻：主线核心接力",
                "note": f"封板率 {seal_rate:.1f}% 扎实 + 接力赚钱 {avg_pct:+.2f}%，可做主线龙头",
                "level": "进攻"}
    if seal_rate >= 50 and avg_pct > 0:
        return {"key": "R2", "action": "半仓：只做主线首板/二板",
                "note": f"封板率 {seal_rate:.1f}% 一般但接力有钱赚 {avg_pct:+.2f}%，控制单票仓位",
                "level": "半仓"}
    if avg_pct > 0:
        return {"key": "R3", "action": "轻仓分歧：快进快出",
                "note": f"封板率仅 {seal_rate:.1f}% 但接力不亏 {avg_pct:+.2f}%，典型分歧市，不恋战",
                "level": "轻仓"}
    if seal_rate >= 50:
        return {"key": "R4", "action": "防守：减仓",
                "note": f"封板率 {seal_rate:.1f}% 尚可但接力亏钱 {avg_pct:+.2f}%，降低暴露",
                "level": "防守"}
    return {"key": "R5", "action": "防守：空仓等待",
            "note": f"封板率 {seal_rate:.1f}% 差 + 接力亏钱 {avg_pct:+.2f}%，任何接力都是接刀",
            "level": "空仓"}


def _render_mermaid_decision(e: Dict[str, Any]) -> str:
    """
    仓位决策分支图：把"封板率 / 赚钱效应"两个数翻译成一个动作。

    【为什么不直接给结论】给结论容易被无脑执行。
    把判断路径画出来，你能看出这个结论是在什么前提下成立的，
    数据一变结论就该跟着变。图中高亮的那条路径，就是本次数据实际走的那条。
    """
    seal = float(e.get("seal_rate", 0.0))
    avg = float(e.get("avg_prev_zt_pct", 0.0))
    cur = e.get("status", "震荡")
    act = _decide_action(seal, avg)

    lines = [
        "```mermaid",
        "graph TD",
        f'    A["今日：{cur}"] --> B{{"封板率 {seal:.1f}%"}}',
        f'    B -->|"≥ 60%"| C1{{"赚钱效应 {avg:+.2f}%"}}',
        f'    B -->|"50% ~ 60%"| C2{{"赚钱效应 {avg:+.2f}%"}}',
        f'    B -->|"< 50%"| C3{{"赚钱效应 {avg:+.2f}%"}}',
        '    C1 -->|"为正"| R1["✅ 进攻：主线核心接力"]',
        '    C1 -->|"为负"| R4["⚠️ 防守：减仓"]',
        '    C2 -->|"为正"| R2["🟡 半仓：主线首板/二板"]',
        '    C2 -->|"为负"| R4',
        '    C3 -->|"为正"| R3["🟠 轻仓分歧：快进快出"]',
        '    C3 -->|"为负"| R5["❌ 防守：空仓等待"]',
    ]
    # 高亮本次实际走到的节点
    status_fill = {
        "R1": ("#dafbe1", "#1a7f37"),
        "R2": ("#fff8c5", "#d4a017"),
        "R3": ("#ffe8cc", "#bc4c00"),
        "R4": ("#ffebe9", "#cf222e"),
        "R5": ("#ffebe9", "#cf222e"),
    }
    for rid in ["R1", "R2", "R3", "R4", "R5"]:
        fill, stroke = status_fill[rid]
        width = "3px" if rid == act["key"] else "1px"
        lines.append(f'    style {rid} fill:{fill},stroke:{stroke},stroke-width:{width},color:#000')
    lines.append("```")
    return "\n".join(lines) + f"\n\n> **系统判定动作：{act['action']}** —— {act['note']}\n"


def render_report(
    day_str: str,
    res: Dict[str, Any],
    youzi_df: pd.DataFrame,
    youzi_sum: pd.DataFrame,
    health: Dict[str, Dict[str, Any]],
    data: Dict[str, pd.DataFrame],
    seat_note: str = "",
) -> str:
    """
    组装完整报告。

    注意：这里所有字段都从 res['emotion'] / res['ladder'] 等**嵌套结构**取值。
    直接 res['total_zt'] 会 KeyError —— analyze() 返回的是分组后的 dict。
    """
    e: Dict[str, Any] = res["emotion"]
    ladder: pd.DataFrame = res["ladder"]
    main_df: pd.DataFrame = res["main_sector"]
    conc: Dict[str, Any] = res["concentration"]
    watch: pd.DataFrame = res["watchlist"]

    # 显示用日期：20261109 -> 2026-11-09（纯数字串在标题里不好读）
    day_disp = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:]}" if len(day_str) == 8 else day_str

    # ---------- 首屏一句话速览 ----------
    headline = (
        f"**{e.get('status_label', '-')}** ｜ 涨停 {e.get('total_zt', 0)} 家 ｜ "
        f"炸板率 {e.get('zb_rate', 0):.1f}% ｜ 赚钱效应 {e.get('avg_prev_zt_pct', 0):+.2f}% ｜ "
        f"最高 {e.get('max_board', 0)} 板"
    )

    # ---------- 告警 ----------
    warn_list = e.get("warnings") or []
    warn_md = "\n".join(f"- {w}" for w in warn_list) if warn_list else "- 无异常告警"

    # ---------- 判定依据 ----------
    reason = e.get("status_reason") or "未命中任何规则，按兜底档位处理"
    diverge = ""
    if e.get("is_divergence"):
        diverge = "\n> ⚠️ **分歧市标记**：封板质量差但接力仍赚钱。仓位要降，但不必空仓。"

    # ---------- 盯盘清单 ----------
    if watch is not None and not watch.empty:
        watch_md = "| 代码 | 名称 | 连板 | 行业 | 封板资金 | 盯盘理由 |\n"
        watch_md += "| :--- | :--- | :---: | :--- | ---: | :--- |\n"
        for _, r in watch.iterrows():
            watch_md += (
                f"| {r.get('代码', '-')} | {r.get('名称', '-')} | {int(r.get('连板数', 1))} "
                f"| {r.get('所属行业', '-')} | {fmt_money(r.get('封板资金', 0))} "
                f"| {r.get('盯盘理由', '-')} |\n"
            )
    else:
        watch_md = "> 无盯盘清单（涨停池为空）。\n"

    # ---------- 指数 ----------
    index_md = _render_index(data.get("index", pd.DataFrame()))

    report = f"""# 📅 {day_disp}（{day_str}）超短线复盘与作战计划

> **数据源**：{_render_health(health)}
> **生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{index_md}{_render_failed_sources(health)}
## 今日速览

{headline}

---

## 一、市场情绪

### {e.get('status_label', '-')}

{e.get('status_desc', '')}

> **判定依据**：{reason}{diverge}

{_render_emotion_table(e)}

**告警**

{warn_md}

### 情绪周期定位

{_render_mermaid_cycle(e)}
### 仓位决策路径

{_render_mermaid_decision(e)}

---

## 二、连板天梯

{_render_ladder(ladder)}
---

## 三、主线题材与集中度

{_render_main_sector(main_df, conc)}
---

## 四、龙虎榜与游资画像

### 龙虎榜个股净买卖（绝对值前 {config.LHB_RANK_TOP_N}）

{_render_lhb_rank(data.get("lhb", pd.DataFrame()), top_n=config.LHB_RANK_TOP_N)}
### 游资动向

{_render_youzi(youzi_df, youzi_sum)}
{f'> 抓取说明：{seat_note}' if seat_note else ''}

---

## 五、明日作战计划

### 总仓位建议

**{e.get('position', '-')}**

### 核心盯盘目标

{watch_md}
### 操作纪律

1. **空间板是风向标**：当前最高 {e.get('max_board', 0)} 板，若开盘不及预期（低开无承接），
   严格按分时均线止损，不摊平、不幻想。
2. **断板即清跟风**：最高板一旦断板，立刻清掉同题材的中位跟风股，防止 A 杀。
3. **赚钱效应为负时不接力**：本日赚钱效应 {e.get('avg_prev_zt_pct', 0):+.2f}%，
   打板买入前先确认这个数是正还是负。
4. **数据缺失不下结论**：见顶部数据源状态，任一关键源失败时，本报告仅作参考。

---

*免责声明：本报告由程序自动生成，数据源于 AKShare 公开接口，可能存在延迟或错误，
不构成任何投资建议。股市有风险，入市需谨慎。*
"""
    return report


# ==============================================================================
# 二、校准层：历史分位数 + 阈值诊断
# ==============================================================================
# 要采集的指标：(报告列名, 取数函数, 取不到时的默认值)
_CALIB_METRICS = [
    ("炸板率", lambda e: e.get("zb_rate", 0.0)),
    ("封板率", lambda e: e.get("seal_rate", 0.0)),
    ("涨停数", lambda e: e.get("total_zt", 0)),
    ("最高连板", lambda e: e.get("max_board", 0)),
    ("赚钱效应", lambda e: e.get("avg_prev_zt_pct", 0.0)),
    ("上涨占比", lambda e: e.get("prev_zt_up_ratio", 0.0)),
    ("跌停数", lambda e: e.get("dt_cnt", 0)),
]

# 阈值键 -> (依赖的指标, 建议取值用的分位数, 取整方式, 最少需要多少天样本)
#
# 【为什么每个参数都带 min_days】极端分位数（P90/P95）在小样本下毫无意义。
#   实测 5 天样本时 P95 只比 P80 高 2.1 个百分点，机械套用会让
#   ebb_zb_rate_alone(44.2) 和 ebb_zb_rate_min(42.1) 几乎重叠，
#   两条判定条件退化成一个，逻辑等于失效。
#   P95 至少要 20 个样本才有一点点意义，P90 至少 10 个。
#
# 【重要】这张表只给"参考值"，不自动改 config。原因见 _diagnose_thresholds 的注释。
_THRESHOLD_HINTS = [
    # (阈值键, 指标, 分位数, 取整方式, 最少样本天数)
    ("ice_zb_rate_min",      "炸板率",   0.90, "round1", 10),
    ("ebb_zb_rate_min",      "炸板率",   0.80, "round1", 10),
    ("ebb_zb_rate_alone",    "炸板率",   0.95, "round1", 20),
    ("climax_zb_rate_max",   "炸板率",   0.20, "round1", 10),
    ("climax_zt_min",        "涨停数",   0.90, "int",    10),
    ("climax_max_board_min", "最高连板", 0.75, "int_up", 10),
    ("ice_zt_max",           "涨停数",   0.20, "int",    10),
]

# 阈值间必须满足的大小关系：(小者, 大者, 说明)
# 【为什么需要】分位数是各自独立算出来的，不知道彼此的语义关系。
# 实测出现过 ebb_zb_rate_alone(44.2) < ebb_zb_rate_min(42.1) 的荒谬组合，
# 而 alone 的语义是"炸板率单独就判退潮的兜底线"，必须严格大于 min。
_THRESHOLD_ORDER = [
    ("ebb_zb_rate_min", "ebb_zb_rate_alone",
     "ebb_zb_rate_alone 是『炸板率单独判退潮』的兜底线，必须高于 ebb_zb_rate_min，否则该条件永远不生效"),
    ("ebb_zb_rate_min", "ice_zb_rate_min",
     "冰点比退潮更极端，ice_zb_rate_min 应不低于 ebb_zb_rate_min"),
]


def run_calibration(end_date: str, days: int = 30, refresh: bool = False) -> Optional[Path]:
    """
    遍历最近 N 个交易日，输出各指标分位数 + 阈值诊断。

    与"拍脑袋定阈值"和"机械套用分位数"都不同，本函数做两件事：
        ① 分位数表：告诉你今天这个数在历史里处于什么位置（相对定位）
        ② 阈值诊断：检查 config 里的绝对阈值在这段样本期内是否**可达**，
           如果不可达（比如 30 天内从未有 80 家涨停），说明阈值对该阶段过严，
           你会一直被判定为"震荡"，从而错过机会。
    """
    # ---------- 1. 生成交易日列表 ----------
    cal = get_trade_calendar()
    if not cal:
        logger.error("❌ 交易日历获取失败（新浪接口不可用），无法校准。")
        return None

    end = to_date(end_date)
    # 只取 <= end 的交易日，倒序后截断 N 个，再正序（便于看进度）
    candidates = sorted((d for d in cal if d <= end), reverse=True)[:days]
    if not candidates:
        logger.error("❌ %s 之前没有任何交易日，校准失败。", end)
        return None
    day_list = sorted(candidates)

    logger.info("⚙️ 校准开始：%s ~ %s，共 %d 个交易日",
                day_list[0], day_list[-1], len(day_list))
    if len(day_list) < 20:
        logger.warning("⚠️ 样本仅 %d 天，分位数参考价值有限（建议 ≥ 30 天）", len(day_list))

    # ---------- 2. 逐日抓取 + 分析 ----------
    # ---------- 2. 采集：本地历史库优先，缺的才联网 ----------
    # 【为什么先查本地】东财涨停池只保留约 20 个交易日，更早的日期接口返回空表。
    # 所以"最近 30 个交易日"里的前 10 多天永远抓不到，
    # 只能靠每天复盘时累积下来的 metrics_history.csv 补。
    hist = _load_history()
    hist_map: Dict[str, Dict[str, Any]] = {}
    if not hist.empty:
        for _, hr in hist.iterrows():
            hist_map[str(hr["日期"])] = hr.to_dict()

    rows: List[Dict[str, Any]] = []
    fail_days: List[str] = []
    need_net: List[Any] = []
    hit_local = 0
    t_start = time.time()

    for d in day_list:
        ds = to_day_str(d)
        hr = hist_map.get(ds)
        if hr:
            # 本地有，且关键指标齐全 -> 直接用，不联网
            row: Dict[str, Any] = {"日期": ds}
            complete = True
            for name, _ in _CALIB_METRICS:
                v = hr.get(name)
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    complete = False
                    break
                row[name] = float(v)
            if complete:
                row["情绪"] = hr.get("情绪", "-")
                rows.append(row)
                hit_local += 1
                continue
        need_net.append(d)

    if hit_local:
        print(f"📦 本地历史库命中 {hit_local} 天，需联网补 {len(need_net)} 天")

    # 【从新往旧抓 + 连续失败熔断】
    # 东财只保留约 20 个交易日，更早的日期必然返回空表。如果按旧->新顺序硬抓，
    # 每个失败日期都要走完 4 次指数退避重试（6 个接口），实测 30 天校准要 11 分钟，
    # 其中 10 分钟全浪费在注定失败的日期上。
    # 改为从新往旧抓，连续 3 个日期涨停池为空即判定超出保留窗口，直接停止回溯。
    OUT_OF_WINDOW_LIMIT = 3
    consecutive_fail = 0
    stopped_early = False

    for i, d in enumerate(reversed(need_net), 1):
        ds = to_day_str(d)
        print(f"[{i:>3}/{len(need_net)}] {ds} 探测中…", end="", flush=True)
        t0 = time.time()

        # 先花几百毫秒探测：过期日期的涨停池必然为空，没必要走完整抓取+重试
        if not refresh and not probe_zt_available(ds):
            print(f" ⏭️ 涨停池为空（{(time.time() - t0):.1f}s）")
            fail_days.append(ds)
            consecutive_fail += 1
            if consecutive_fail >= OUT_OF_WINDOW_LIMIT and i < len(need_net):
                rest = len(need_net) - i
                print(f"   ⛔ 连续 {OUT_OF_WINDOW_LIMIT} 个日期取不到涨停池，"
                      f"判定已超出接口保留窗口（约 20 个交易日），停止回溯剩余 {rest} 天")
                print(f"   　 解决办法：每天跑一次 python main.py，"
                      f"指标会自动累积到 calibration/metrics_history.csv，样本随天数自然增长")
                stopped_early = True
                break
            continue
        consecutive_fail = 0

        try:
            data, health = fetch_all(d, use_cache=True, refresh=refresh)
            # 涨停池缺失 -> 当日所有指标都不可信，直接跳过（不要污染分位数）
            if not health.get("涨停池", {}).get("ok"):
                print(f" ⏭️ 涨停池为空（超出接口保留窗口或停市），跳过（{time.time() - t0:.1f}s）")
                fail_days.append(ds)
                consecutive_fail += 1
                if consecutive_fail >= OUT_OF_WINDOW_LIMIT and i < len(need_net):
                    rest = len(need_net) - i
                    print(f"   ⛔ 连续 {OUT_OF_WINDOW_LIMIT} 个日期取不到涨停池，"
                          f"停止回溯剩余 {rest} 天")
                    stopped_early = True
                    break
                continue
            consecutive_fail = 0

            res = analyzer.analyze(data)
            e = res["emotion"]
            row = {"日期": ds}
            for name, getter in _CALIB_METRICS:
                row[name] = getter(e)
            row["情绪"] = e.get("status", "-")
            rows.append(row)
            print(f" ✅ {e.get('status', '-'):<2} 涨停{e.get('total_zt', 0):>3} "
                  f"炸板率{e.get('zb_rate', 0):>5.1f}% ({time.time() - t0:.1f}s)")
        except Exception as exc:  # 单日异常不中断整轮
            print(f" ❌ {type(exc).__name__}: {str(exc)[:40]}")
            fail_days.append(ds)
            continue

    # 把本轮新抓到的样本沉淀进历史库，下次校准就不必再联网
    if rows:
        _append_history(rows)

    if not rows:
        logger.error("❌ 全部 %d 个交易日都失败，校准终止。请先跑 python main.py --check",
                     len(day_list))
        return None

    df = pd.DataFrame(rows)
    # 联网部分是从新往旧抓的，rows 顺序会乱；统一按日期正序，
    # 因为后面的"今日百分位"假设最后一行是最新日期。
    df = df.sort_values("日期").reset_index(drop=True)

    # ---------- 3. 分位数表 ----------
    print("\n" + "=" * 76)
    print(f"📊 历史分位数统计（{len(df)} 个有效交易日，{df['日期'].iloc[0]} ~ {df['日期'].iloc[-1]}）")
    print("=" * 76)
    if fail_days:
        print(f"⚠️  {len(fail_days)} 天数据缺失已跳过：{'、'.join(fail_days[:8])}"
              f"{' …' if len(fail_days) > 8 else ''}")
        if stopped_early:
            print("    原因：超出东财涨停池的保留窗口（约 20 个交易日），不是程序错误。")
            print("    这些日期只能靠每天复盘时累积到 metrics_history.csv 补上。")
        print()

    qs = [0.10, 0.25, 0.50, 0.75, 0.90]
    header = f"{'指标':<8}" + "".join(f"{'P' + str(int(q * 100)):>9}" for q in qs) + \
             f"{'均值':>9}{'最小':>9}{'最大':>9}"
    print(header)
    print("-" * 76)
    for name, _ in _CALIB_METRICS:
        s = pd.to_numeric(df[name], errors="coerce").dropna()
        if s.empty:
            continue
        cells = "".join(f"{s.quantile(q):>9.2f}" for q in qs)
        print(f"{name:<8}{cells}{s.mean():>9.2f}{s.min():>9.2f}{s.max():>9.2f}")

    # ---------- 4. 情绪档位分布 ----------
    print("\n【情绪档位分布】")
    dist = df["情绪"].value_counts()
    for k, v in dist.items():
        pct = v / len(df) * 100
        bar = "█" * max(1, int(round(pct / 4)))
        print(f"  {k:<4} {v:>3} 天  {pct:>5.1f}%  {bar}")

    # ---------- 5. 阈值诊断（本模块的核心价值）----------
    kept = _diagnose_thresholds(df)

    # ---------- 6. 落盘 ----------
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = CALIB_DIR / f"calibration_{to_day_str(day_list[-1])}_{len(df)}d.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n📁 明细已保存：{csv_path}")

    # 生成可直接粘贴的 config 片段
    if kept:
        print("\n" + "=" * 76)
        print("📋 可直接粘贴到 config.py（替换 EMOTION_THRESHOLDS 对应行）")
        print("=" * 76)
        for k, v in kept.items():
            print(f'    "{k}": {v},')
        print("\n⚠️  粘贴前请阅读上方的诊断说明。分位数是**相对定位**，不是最优阈值。")

    print(f"\n✅ 校准完成，总耗时 {time.time() - t_start:.1f}s")

    # 反事实检验：如果改用建议阈值，历史档位分布会怎么变
    _counterfactual(df, kept)
    return csv_path


def _diagnose_thresholds(df: pd.DataFrame) -> Dict[str, float]:
    """
    阈值诊断 + 建议值生成。

    【为什么不能机械替换成 P75/P90】
    分位数回答的是"在**这段样本内**排第几"，而情绪档位是绝对概念：
      · 炸板率 60% 无论哪个阶段都是灾难
      · 涨停 80 家不是每段行情都会有；熊市里 P90 可能只有 45 家
    如果直接把 climax_zt_min 从 80 改成 45，等于把"熊市里最好的一天"标成"高潮"，
    系统会持续给出重仓建议 —— 这比拍脑袋定阈值更危险。

    所以本函数只做四件事：
      ① 提示哪些绝对阈值在本样本内**不可达**（说明该阶段一直判不出高潮/冰点）
      ② 给出分位数参考值，但样本不足时**拒绝给值**（极端分位在小样本下没有意义）
      ③ 校验阈值之间的序关系（alone 必须大于 min 这类语义约束）
      ④ 不自动写入 config，交给人决定
    """
    print("\n【阈值诊断】")
    n_days = len(df)
    hints: Dict[str, float] = {}
    cur = config.EMOTION_THRESHOLDS

    for key, metric, q, rounding, min_days in _THRESHOLD_HINTS:
        if metric not in df.columns:
            continue
        s = pd.to_numeric(df[metric], errors="coerce").dropna()
        if s.empty:
            continue

        old = cur.get(key)

        # ---- 样本量不足：拒绝给参考值 ----
        if n_days < min_days:
            print(f"  {key:<22} 当前 {old:>7} → 样本不足（需 ≥{min_days} 天，现有 {n_days} 天），不给参考值")
            continue

        v = s.quantile(q)
        if rounding == "int":
            v = int(round(v))
        elif rounding == "int_up":
            import math
            v = int(math.ceil(v))
        else:
            v = round(float(v), 1)

        hints[key] = v

        # ---- 当前阈值在样本内的可达性 ----
        reach_note = ""
        if key == "climax_zt_min" and not bool((s >= old).any()):
            reach_note = f"⚠️ 样本内无一日达标（最高 {s.max():.0f}），『高潮』档不可达"
        elif key == "ice_zt_max" and not bool((s <= old).any()):
            reach_note = f"⚠️ 样本内无一日达标（最低 {s.min():.0f}），『冰点』档不可达"
        elif key == "climax_max_board_min" and not bool((s >= old).any()):
            reach_note = f"⚠️ 样本内最高仅 {s.max():.0f} 板，『高潮』档因此永不可达"

        arrow = "=" if (old is not None and abs(float(old) - float(v)) < 1e-9) else "→"
        print(f"  {key:<22} 当前 {old:>7} {arrow} 参考 {v:>7}（{metric} P{int(q * 100)}）")
        if reach_note:
            print(f"      {reach_note}")

    # ---- 序关系校验：分位数各自独立算，可能算出语义上荒谬的组合 ----
    bad: List[str] = []
    for small, big, why in _THRESHOLD_ORDER:
        if small in hints and big in hints and hints[big] <= hints[small]:
            print(f"\n  ❌ 建议值自相矛盾：{big}({hints[big]}) ≤ {small}({hints[small]})")
            print(f"     {why}")
            print(f"     → 已剔除 {big}，该参数请保留 config 中的原值")
            hints.pop(big, None)
            bad.append(big)

    # ---- 冰点组合条件的可达性（AND 关系，单独看单指标会漏） ----
    if {"炸板率", "涨停数"} <= set(df.columns):
        zb = pd.to_numeric(df["炸板率"], errors="coerce")
        zt = pd.to_numeric(df["涨停数"], errors="coerce")
        hit = int(((zb >= cur["ice_zb_rate_min"]) & (zt <= cur["ice_zt_max"])).sum())
        print(f"\n  『冰点』组合条件（炸板率≥{cur['ice_zb_rate_min']}% 且 涨停≤{cur['ice_zt_max']}家）")
        print(f"      样本内命中 {hit}/{n_days} 天"
              + ("  ← 命中为 0，该档位实际从未触发，冰点阈值形同虚设" if hit == 0 else ""))

    # ---- 当前值在历史中的百分位（比"P90=43.5"更好理解） ----
    print("\n  【今日各指标的历史百分位】")
    for name in ["炸板率", "封板率", "涨停数", "最高连板", "赚钱效应"]:
        if name not in df.columns or len(df) < 2:
            continue
        s = pd.to_numeric(df[name], errors="coerce").dropna()
        if s.empty:
            continue
        today = s.iloc[-1]                      # df 按日期正序，最后一行是今天
        pct = float((s <= today).mean() * 100)
        print(f"      {name:<6} 今日 {today:>7.2f}  → 处于历史 P{pct:.0f}")

    if n_days < 15:
        print(f"\n  ⚠️ 仅 {n_days} 天样本，以上诊断只能看趋势，**不足以据此修改阈值**。")
        print("     建议至少跑 30 个交易日（python main.py --calibrate 30）。")

    print("\n  【说明】参考值 = 该指标在样本内的分位数，只反映『相对位置』。")
    print("        情绪档位是绝对概念，熊市里 P90 的涨停数也不够 80 家。")
    print("        建议：若你长期只做当前阶段，可用参考值；若做全周期，保留绝对值并接受『长期判震荡』。")
    return hints


def _counterfactual(df: pd.DataFrame, hints: Dict[str, float]) -> None:
    """
    反事实检验：用建议阈值重跑历史档位分布，看会不会"天天高潮"。

    这是防止校准翻车的最后一道闸门。
    如果换成建议阈值后，『高潮』占比超过 40%，说明阈值定得太松，
    系统会持续给重仓建议 —— 直接指出问题，而不是让你拿去用。
    """
    if not hints or "炸板率" not in df.columns:
        return

    zb = pd.to_numeric(df["炸板率"], errors="coerce").fillna(0)
    zt = pd.to_numeric(df["涨停数"], errors="coerce").fillna(0)
    mb = pd.to_numeric(df["最高连板"], errors="coerce").fillna(0)
    avg = pd.to_numeric(df["赚钱效应"], errors="coerce").fillna(0)

    # 用与 analyzer._judge_emotion 相同的判定顺序
    levels = []
    for i in range(len(df)):
        if zb.iloc[i] >= hints.get("ice_zb_rate_min", 45) and zt.iloc[i] <= hints.get("ice_zt_max", 30):
            levels.append("冰点")
        elif ((zb.iloc[i] >= hints.get("ebb_zb_rate_min", 35) and avg.iloc[i] < 0)
              or zb.iloc[i] >= hints.get("ebb_zb_rate_alone", 50)):
            levels.append("退潮")
        elif (zt.iloc[i] >= hints.get("climax_zt_min", 80)
              and zb.iloc[i] <= hints.get("climax_zb_rate_max", 20)
              and mb.iloc[i] >= hints.get("climax_max_board_min", 5)):
            levels.append("高潮")
        elif zb.iloc[i] <= 30 and avg.iloc[i] > 0:
            levels.append("回暖")
        else:
            levels.append("震荡")

    dist = pd.Series(levels).value_counts()
    print("\n【反事实检验】若改用参考阈值，历史档位分布会变成：")
    for k in ["冰点", "退潮", "震荡", "回暖", "高潮"]:
        v = int(dist.get(k, 0))
        print(f"  {k:<4} {v:>3} 天  {v / len(df) * 100:>5.1f}%")

    climax_pct = dist.get("高潮", 0) / len(df) * 100
    if climax_pct > 40:
        print(f"\n  ❌ 警告：『高潮』占比 {climax_pct:.0f}% 明显过高，说明参考阈值太松，")
        print("     系统会持续给出重仓建议。请勿直接采用，改用绝对阈值或提高分位点。")
    elif climax_pct == 0:
        print("\n  ⚠️ 『高潮』档在样本内仍为 0，说明这段行情确实没有主升浪，")
        print("     此时系统长期判『震荡/回暖』是**正确**的，不要为了出结论而调低阈值。")
    else:
        print(f"\n  ✅ 『高潮』占比 {climax_pct:.0f}%，分布合理，参考阈值可用。")
        print("     注意：这是对历史样本的回测，不代表未来表现。")


# ==============================================================================
# 三、主流程
# ==============================================================================
def run_check(day_str: str) -> int:
    """自检模式：只测接口连通性。用于发现 AKShare 改版 / 网络问题。"""
    logger.info("🔍 自检模式：%s", day_str)
    _, health = fetch_all(day_str, use_cache=False, refresh=True)

    print("\n" + "=" * 78)
    print(f"{'数据源':<12}{'状态':<8}{'行数':>7}{'耗时(s)':>10}  备注")
    print("-" * 78)
    ok_cnt = 0
    for name, info in health.items():
        ok = bool(info.get("ok"))
        ok_cnt += int(ok)
        status = "✅ OK" if ok else "❌ FAIL"
        note = (info.get("error") or info.get("note") or "")[:38]
        print(f"{name:<12}{status:<8}{info.get('rows', 0):>7}{info.get('seconds', 0):>10.2f}  {note}")
    print("=" * 78)
    print(f"结果：{ok_cnt}/{len(health)} 个数据源正常")

    if ok_cnt < len(health):
        print("\n排查建议：")
        print("  1. 非交易日/停市日会返回空表，属正常")
        print("  2. 网络问题：设置代理或重试；AKShare 偶发限流，稍后再试")
        print("  3. 接口改版：pip install -U akshare，然后检查 config.py 的字段候选表")
        return 1
    return 0


def run_review(day_str: str, use_seat: bool = True,
               allow_intraday: bool = False) -> Optional[Path]:
    """
    复盘主流程：抓数 -> 分析 -> 游资 -> 渲染 -> 落盘。

    【盘中防呆】这是整个系统的最后一道防线。
    2026-11-11 13:06 的事故就是从这里进去的：盘中跑了一次，
    把半天的涨停池写进了 metrics_history.csv 并推到 GitHub。
    无论调用方是 daily.bat、桌面快捷方式、定时任务还是裸 python，
    最终都会走到这里，所以闸设在这里最有效。
    """
    # ---------- 0. 盘中防呆 ----------
    allowed, reason = guard_intraday(day_str, allow_intraday=allow_intraday)
    if not allowed:
        logger.error("=" * 68)
        for line in reason.splitlines():
            logger.error(line)
        logger.error("=" * 68)
        return None
    if reason:
        logger.warning(reason)

    t_start = time.time()
    logger.info("🚀 开始生成 %s 复盘报告…", day_str)

    # ---------- 1. 抓取 ----------
    data, health = fetch_all(day_str)

    # 关键数据源全挂时提前退出，避免产出"看着正常但全是 0"的假报告
    if not health.get("涨停池", {}).get("ok") and not health.get("炸板池", {}).get("ok"):
        logger.error("❌ 涨停池与炸板池均抓取失败，无法生成有意义的报告。"
                     "请先执行 python main.py %s --check 排查。", day_str)
        return None

    # ---------- 2. 分析 ----------
    res = analyzer.analyze(data)
    e = res["emotion"]

    # 沉淀当日指标到历史库。
    # 【为什么要在这里做】东财只保留约 20 个交易日的数据，过了窗口就再也抓不到。
    # 不趁今天就存下来，将来做阈值校准时样本永远不够。
    try:
        hist_row: Dict[str, Any] = {"日期": day_str}
        for name, getter in _CALIB_METRICS:
            hist_row[name] = getter(e)
        hist_row["情绪"] = e.get("status", "-")
        _append_history(hist_row)
    except Exception as exc:
        logger.warning("指标历史库写入失败（不影响报告）：%s", exc)

    # ---------- 3. 游资 ----------
    # 【为什么要跑两遍】
    #   只扫盯盘清单（5 只票）会漏掉全市场其它涨停股上的游资动作，
    #   而"全市场第一层"是纯内存计算（活跃营业部表已在手），零额外请求成本。
    #   两个来源合并后才既有"今天游资整体在干什么"，又有"这几只票上谁在买卖"。
    #
    #   注意：target_codes 必须传**代码**。lhb 内部会转成名称再做集合匹配，
    #   直接拿代码去 `in` 名称串永远为 False（这就是上一轮踩到的静默错误）。
    watch = res["watchlist"]
    if watch is not None and not watch.empty and "代码" in watch.columns:
        target_codes = [str(c).zfill(6) for c in watch["代码"].tolist()]
    else:
        target_codes = []

    yyb_df = data.get("yyb", pd.DataFrame())
    lhb_df = data.get("lhb", pd.DataFrame())
    zt_df = data.get("zt", pd.DataFrame())

    # 第二层（逐票席位）是唯一能看到"卖出标的"的来源：
    # 活跃营业部表只给"买了哪些股"，没有卖出标的。
    # 只对盯盘清单扫，5 只 × 2 方向 ≈ 10 次请求，约 10 秒，成本可接受。
    if use_seat and target_codes:
        fetcher = lhb.make_seat_fetcher(day_str)
        seat_note = (f"全市场第一层 + 盯盘清单 {len(target_codes)} 只票的席位明细"
                     f"（第二层只覆盖盯盘股，其余涨停股的席位未抓取）")
        logger.info("游资：第一层全市场 + 第二层席位（%d 只盯盘股）", len(target_codes))
    else:
        fetcher = None
        seat_note = "仅第一层活跃营业部表（全市场），未抓取个股席位明细"

    try:
        # 第一遍：全市场（sead_fetcher=None，不触发第二层，纯内存过滤）
        recs_market = lhb.match_youzi(
            active_yyb_df=yyb_df,
            lhb_detail_df=lhb_df,
            target_codes=None,
            zt_df=zt_df,
        )
        # 第二遍：盯盘清单（第一层过滤 + 第二层席位）
        recs_watch = lhb.match_youzi(
            active_yyb_df=yyb_df,
            lhb_detail_df=lhb_df,
            target_codes=target_codes,
            zt_df=zt_df,
            seat_fetcher=fetcher,
            day=day_str,
        ) if target_codes else []

        # 合并：第一层重复记录由 to_dataframe 在同口径内去重
        youzi_df = lhb.to_dataframe(recs_market + recs_watch)
        youzi_sum = lhb.summarize_by_youzi(youzi_df)
    except Exception as exc:
        # 游资模块属于"锦上添花"，挂了不能让整份报告失败
        logger.error("游资画像生成失败（不影响其它模块）：%s", exc)
        youzi_df = pd.DataFrame()
        youzi_sum = pd.DataFrame()

    # ---------- 4. 渲染 ----------
    report_md = render_report(day_str, res, youzi_df, youzi_sum, health, data, seat_note)

    # ---------- 5. 落盘 ----------
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"复盘_{day_str}.md"
    path.write_text(report_md, encoding="utf-8")

    logger.info("✅ 报告已生成：%s（耗时 %.1fs，%d 字符）",
                path, time.time() - t_start, len(report_md))
    print(f"\n📄 {path}")

    # ---------- 6. 控制台摘要 ----------
    print("\n" + "=" * 62)
    print(f"  {day_str}  {e.get('status_label', '-')}")
    print("=" * 62)
    print(f"  涨停 {e.get('total_zt', 0)} 家（有效 {e.get('valid_zt', 0)}）｜ "
          f"炸板率 {e.get('zb_rate', 0):.2f}% ｜ 跌停 {e.get('dt_cnt', 0)} 家")
    print(f"  赚钱效应 {e.get('avg_prev_zt_pct', 0):+.2f}% ｜ "
          f"最高 {e.get('max_board', 0)} 板 ｜ 2板以上 {e.get('emotional_cnt', 0)} 家")
    print(f"  仓位建议：{e.get('position', '-')}")
    if not youzi_sum.empty:
        print(f"  游资命中 {len(youzi_sum)} 个（明细 {len(youzi_df)} 条）")
    return path


# ==============================================================================
# 四、命令行入口
# ==============================================================================
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="A股超短线全自动复盘系统（数据源：AKShare）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py                     复盘最近交易日\n"
            "  python main.py 20261109            复盘指定日期\n"
            "  python main.py --check             接口自检\n"
            "  python main.py --calibrate 30      校准最近 30 个交易日\n"
            "  python main.py 20261109 --no-seat  跳过第二层席位抓取\n"
        ),
    )
    p.add_argument("date", nargs="?", help="交易日 YYYYMMDD，缺省=最近交易日")
    p.add_argument("--check", action="store_true", help="自检模式：只测接口，不生成报告")
    p.add_argument("--calibrate", nargs="?", type=int, const=30, metavar="N",
                   help="校准模式：遍历最近 N 个交易日（默认 30），输出分位数与阈值诊断")
    p.add_argument("--refresh", action="store_true", help="忽略本地缓存，强制重新抓取")
    p.add_argument("--no-seat", action="store_true", help="跳过第二层个股席位抓取（更快）")
    p.add_argument("--allow-intraday", action="store_true",
                   help="盘中强制运行（默认拒绝：交易日 15:00 前不跑当天，防止污染历史库）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    # ---------- 确定目标日期 ----------
    # 【坑】is_trade_date 要 date 对象，传字符串会静默返回 False 然后"不是交易日"退出。
    #      这里统一在入口做一次转换，后续全部用 date 对象。
    if args.date:
        try:
            target = to_date(args.date)
        except ValueError as exc:
            logger.error("❌ 日期格式错误：%s（应为 YYYYMMDD）", exc)
            return 2
    else:
        if args.calibrate is not None:
            # 校准默认从最近交易日往前推
            target = latest_trade_date()
        else:
            target = latest_trade_date()

    day_str = to_day_str(target)

    # 非交易日处理：自检模式允许跑（正好验证容错），其余模式直接退出
    if not is_trade_date(target):
        if args.check:
            logger.warning("⚠️ %s 非交易日，仍执行自检（用于验证容错分支）", day_str)
        else:
            logger.error("❌ %s 不是交易日，退出。", day_str)
            return 0

    # ---------- 分派 ----------
    if args.check:
        return run_check(day_str)

    if args.calibrate is not None:
        n = max(1, int(args.calibrate))
        run_calibration(day_str, days=n, refresh=args.refresh)
        return 0

    path = run_review(day_str, use_seat=not args.no_seat, allow_intraday=args.allow_intraday)
    return 0 if path else 1


if __name__ == "__main__":
    sys.exit(main())
