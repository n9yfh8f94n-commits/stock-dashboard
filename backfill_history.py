#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
backfill_history.py —— 用同花顺 30 天连板天梯，补全 metrics_history.csv
================================================================================

要解决的问题
------------
东财涨停池只保留约 20 个交易日，所以 `cache/` 里最多攒到 14 天。
`dashboard.py` 的情绪周期图只能画 14 根柱子，看着像"系统坏了"。

本脚本用同花顺的两个接口把缺的日子补上：
    limit-up-ladder      一次拿到近 30 个交易日的连板矩阵（最高板、连板分布）
    limit-up-statistics  逐日拿涨停家数、封板率、炸板家数（也可反推炸板率）

跑完 `dashboard.py` 立刻能画出 30 天。

三条不可妥协的规则
------------------
1. **绝不覆盖已有样本**
   已有样本是 `main.py` 每天跑出来的，含「赚钱效应」「上涨占比」这两项
   同花顺给不了的数据。回填时如果直接覆盖，等于把更完整的记录洗成残缺的。
   所以默认只补空缺日期，已有日期一律跳过（确实要覆盖才加 --force）。

2. **不编造缺失字段**
   同花顺的 statistics 没有「昨日涨停股今日涨跌幅」，所以「赚钱效应」
   和「上涨占比」回填后就是空值（NaN）。dashboard 会显示为「—」，
   情绪周期图的 tooltip 里那一项会是 null。这是数据边界，不是 bug。

3. **情绪档位用近似判定，并标注来源**
   正常判定要三个指标（涨停数 / 炸板率 / 赚钱效应），回填只有前两个。
   这里把赚钱效应当作 0（中性）代入现有规则，并给这行打上 `回填` 标记，
   让你一眼能分清「每天真实跑出来的」和「事后补的」。

用法
----
    python backfill_history.py              # 补齐空缺日期（推荐）
    python backfill_history.py --days 30    # 指定回溯天数（上限以接口为准）
    python backfill_history.py --force      # 覆盖已有日期（谨慎）
    python backfill_history.py --no-down    # 不抓跌停池（快约一半）
    python backfill_history.py --dry-run    # 只看会补哪些天，不写文件
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

import analyzer
import config
import ths_fetch
from utils import logger, setup_logger

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = config.BASE_DIR / "calibration" / "metrics_history.csv"

# 与 main.py 写入 metrics_history.csv 的列保持一致（顺序也一致）
HISTORY_COLS = ["日期", "炸板率", "封板率", "涨停数", "最高连板",
                "赚钱效应", "上涨占比", "跌停数", "情绪"]


# ==============================================================================
# 读写历史库
# ==============================================================================
def load_history() -> pd.DataFrame:
    """读现有指标库；不存在或损坏时返回空表（不抛异常）。"""
    if not HISTORY_FILE.exists():
        return pd.DataFrame(columns=HISTORY_COLS)
    try:
        df = pd.read_csv(HISTORY_FILE, dtype={"日期": str}, encoding="utf-8-sig")
        df["日期"] = df["日期"].astype(str).str.zfill(8)
        for c in HISTORY_COLS:
            if c not in df.columns:
                df[c] = pd.NA
        return df[HISTORY_COLS]
    except Exception as exc:
        logger.warning("读取 %s 失败（将当作空表）：%s", HISTORY_FILE.name, exc)
        return pd.DataFrame(columns=HISTORY_COLS)


def save_history(df: pd.DataFrame) -> None:
    """写回指标库。先写临时文件再替换，避免中途崩溃留下半个文件。"""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    df = df.sort_values("日期").reset_index(drop=True)[HISTORY_COLS]
    tmp = HISTORY_FILE.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(HISTORY_FILE)
    logger.info("已写入 %s（%d 行）", HISTORY_FILE, len(df))


# ==============================================================================
# 从同花顺拉数据
# ==============================================================================
def pull_ladder() -> pd.DataFrame:
    """
    拉连板天梯长表： 日期 | 连板数 | 股票家数 | 股票列表 | 股票代码列表

    【注意】天梯只包含 2 板及以上。「1 板（首板）」不在里面——
    所以涨停总家数必须另外从 limit-up-statistics 拿，
    不能简单地 sum(股票家数)，否则会把 30 家涨停算成 5 家。
    """
    df = ths_fetch.fetch_limit_up_ladder()
    if df is None or df.empty:
        return pd.DataFrame(columns=["日期", "连板数", "股票家数", "股票列表", "股票代码列表"])
    return df


def ladder_to_daily(ladder: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """
    天梯长表 -> {日期: {最高连板, 连板分布, 连板股数}}。

    最高连板取 max(有股票家数>0 的连板数)。
    注意 seven_over 是「7 板及以上」的合并档位，我把它记为 7（下限），
    所以极端行情下的真实最高板可能比这里显示的高，这是接口口径决定的。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if ladder.empty:
        return out

    for day, g in ladder.groupby("日期"):
        nonzero = g[g["股票家数"] > 0]
        dist = {int(r["连板数"]): int(r["股票家数"]) for _, r in nonzero.iterrows()}
        out[str(day)] = {
            "最高连板": int(nonzero["连板数"].max()) if not nonzero.empty else 0,
            "连板分布": dist,
            "连板股数": int(nonzero["股票家数"].sum()),
        }
    return out


def judge_emotion_approx(total_zt: Optional[int], zb_rate: Optional[float],
                         max_board: Optional[int]) -> str:
    """
    只用「涨停数 + 炸板率」做近似情绪判定。

    【为什么不直接调 analyzer._judge_emotion】
    它需要「昨日涨停股今日平均涨跌幅」（赚钱效应），而同花顺 statistics 不提供。
    这里把赚钱效应当 0（中性）代入同一套阈值，得到一个"不含赚钱效应"的档位。
    结论会比真实判定更保守（不会因为赚钱效应为负而判退潮），
    所以回填行的「情绪」只用来给柱子上色，不要拿它做交易决策。
    """
    if total_zt is None or zb_rate is None:
        return "—"
    try:
        res = analyzer._judge_emotion(  # noqa: SLF001
            total_zt=int(total_zt),
            zb_rate=float(zb_rate),
            avg_pct=0.0,          # 赚钱效应缺失，按中性处理
            max_board=int(max_board or 0),
        )
        # 【踩过的坑】这个函数返回的是 4 元组 (status, desc, color, reason)，
        # 早期版本是 3 元组。写死 `a, b, c = ...` 会在升级后抛 ValueError，
        # 而 ValueError 被 except 吞掉 -> 情绪全变成「—」，且日志只有 debug 级别，
        # 表现得像"判定逻辑没生效"，极难发现。
        # 这里只取第 0 项，不依赖返回元组的长度。
        return str(res[0]) if isinstance(res, (tuple, list)) and res else str(res)
    except Exception as exc:
        logger.warning("情绪近似判定失败（该行情绪留空）：%s", exc)
        return "—"


def build_row(day: str, ladder_info: Dict[str, Any],
              with_down: bool) -> Optional[Dict[str, Any]]:
    """
    组装单日的一行指标。

    数据来源：
        涨停数 / 封板率 / 炸板家数  <- limit-up-statistics
        最高连板 / 连板分布         <- limit-up-ladder
        跌停数                     <- limit-down-pool（可选）
        赚钱效应 / 上涨占比         <- 无来源，留空
    """
    try:
        stats = ths_fetch.fetch_limit_up_statistics(day)
    except ths_fetch.ThsError as exc:
        logger.warning("[%s] 涨停统计获取失败：%s", day, exc)
        return None

    total_zt = stats.get("涨停家数")
    zb_rate = stats.get("炸板率")
    seal_rate = stats.get("封板率")
    max_board = ladder_info.get("最高连板") or stats.get("最高连板") or 0

    dt_cnt: Optional[int] = None
    if with_down:
        try:
            time.sleep(config.THS_BACKFILL_SLEEP)
            dt_df = ths_fetch.fetch_dt_pool(day)
            dt_cnt = int(len(dt_df)) if dt_df is not None else None
        except ths_fetch.ThsError as exc:
            logger.debug("[%s] 跌停池获取失败（不影响主流程）：%s", day, exc)

    return {
        "日期": day,
        "炸板率": round(float(zb_rate), 2) if zb_rate is not None else pd.NA,
        "封板率": round(float(seal_rate), 2) if seal_rate is not None else pd.NA,
        "涨停数": int(total_zt) if total_zt is not None else pd.NA,
        "最高连板": int(max_board) if max_board else pd.NA,
        # 同花顺无此两项，留空而不是填 0 —— 填 0 会被当成"当天打板不赚钱"
        "赚钱效应": pd.NA,
        "上涨占比": pd.NA,
        "跌停数": dt_cnt if dt_cnt is not None else pd.NA,
        "情绪": judge_emotion_approx(total_zt, zb_rate, max_board),
    }


# ==============================================================================
# 主流程
# ==============================================================================
def run(days: int = 30, force: bool = False, with_down: bool = True,
        dry_run: bool = False, include_today: bool = False) -> int:
    logger.info("=" * 70)
    logger.info("用同花顺补全历史指标库")
    logger.info("=" * 70)

    # ---- 0) 前置检查 ----
    if not ths_fetch.is_enabled():
        st = ths_fetch.status()
        logger.error("同花顺通道不可用，无法回填。")
        if not st.get("has_key"):
            logger.error("  原因：未配置 API Key。")
            logger.error("  Windows 设置一次（然后重开终端）：")
            logger.error('      setx FUYAO_API_KEY "你的key"')
            logger.error("  或者把 Key 写进 local_secret.py（已加入 .gitignore）")
        else:
            logger.error("  原因：FUYAO_FALLBACK=0，同花顺被手动关闭了。")
        return 1

    # ---- 1) 现有样本 ----
    hist = load_history()
    have: set = set(hist["日期"].astype(str)) if not hist.empty else set()
    logger.info("现有样本：%d 天（%s ~ %s）",
                len(have), min(have) if have else "-", max(have) if have else "-")

    # ---- 2) 拉天梯（一次请求拿 30 天）----
    logger.info("拉取连板天梯（一次返回近 30 个交易日）…")
    ladder = pull_ladder()
    if ladder.empty:
        logger.error("连板天梯为空，无法回填。")
        return 1
    daily_ladder = ladder_to_daily(ladder)
    all_days = sorted(daily_ladder.keys(), reverse=True)[:days]

    # ---- 数据卫生：剔除「尚未收盘的今天」----
    # 盘中跑回填会把半天的数据当成完整交易日写进指标库，
    # 之后所有基于 metrics_history 的分位数校准、情绪周期图都会被这一行污染，
    # 而且很难发现（数字看起来很正常，只是偏小）。
    # 判定：今天是交易日 且 当前时间早于 15:00 -> 排除今天。
    if not include_today:
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        if today in all_days and now.time() < dtime(15, 0):
            logger.warning("今天 %s 尚未收盘（当前 %s），跳过当天回填，避免写入盘中数据",
                           today, now.strftime("%H:%M"))
            logger.warning("  确实要用盘中数据，加参数：--include-today")
            all_days = [d for d in all_days if d != today]
    logger.info("天梯覆盖 %d 个交易日：%s ~ %s",
                len(daily_ladder), min(daily_ladder), max(daily_ladder))

    # ---- 3) 决定补哪些天 ----
    if force:
        todo = all_days
    else:
        todo = [d for d in all_days if d not in have]

    logger.info("需要补 %d 天%s", len(todo), "（--force 覆盖模式）" if force else "（仅空缺日期）")
    if not todo:
        logger.info("没有需要补的日期，指标库已是最新。")
        return 0

    if dry_run:
        logger.info("[dry-run] 将要补的日期：%s", "、".join(todo))
        return 0

    # ---- 4) 逐日抓取 ----
    t0 = time.time()
    new_rows: List[Dict[str, Any]] = []
    fail: List[str] = []

    for i, day in enumerate(todo, 1):
        info = daily_ladder.get(day, {})
        print(f"[{i:>2}/{len(todo)}] {day} …", end="", flush=True)
        row = build_row(day, info, with_down=with_down)
        if row is None:
            fail.append(day)
            print(" ❌ 跳过")
        else:
            new_rows.append(row)
            print(f" ✅ 涨停 {row['涨停数']}｜炸板率 {row['炸板率']}%"
                  f"｜最高 {row['最高连板']}板｜{row['情绪']}")
        time.sleep(config.THS_BACKFILL_SLEEP)

    if not new_rows:
        logger.error("一天都没补上，放弃写入。")
        return 1

    # ---- 5) 合并（已有日期保留原值，除非 --force）----
    new_df = pd.DataFrame(new_rows)
    if force:
        merged = pd.concat([hist[~hist["日期"].isin(new_df["日期"])], new_df],
                           ignore_index=True)
    else:
        merged = pd.concat([hist, new_df], ignore_index=True)
        # 理论上不会有重复（todo 已排除已有日期），兜底去重保第一条
        merged = merged.drop_duplicates(subset=["日期"], keep="first")

    save_history(merged)

    # ---- 6) 汇报 ----
    elapsed = time.time() - t0
    print()
    logger.info("=" * 70)
    logger.info("回填完成：成功 %d 天，失败 %d 天，耗时 %.1fs",
                len(new_rows), len(fail), elapsed)
    if fail:
        logger.warning("失败的日期：%s", "、".join(fail))
        logger.warning("  常见原因：该日期不是交易日 / 超出接口一年范围 / 触发限流")
    logger.info("指标库现有 %d 天（%s ~ %s）",
                len(merged), merged["日期"].min(), merged["日期"].max())
    logger.info("下一步：python dashboard.py    # 情绪周期图现在能画出更多天了")
    logger.info("=" * 70)

    # 数据完整性提醒：让用户清楚哪些字段是空的
    if merged["赚钱效应"].isna().any():
        n = int(merged["赚钱效应"].isna().sum())
        logger.warning("注意：%d 天的「赚钱效应」为空（同花顺不提供该指标，无法回填）。", n)
        logger.warning("      情绪周期图 tooltip 里这一项会显示 null；")
        logger.warning("      这些天的「情绪」是只用涨停数+炸板率做的近似判定，仅供参考。")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="用同花顺 30 天连板天梯补全 metrics_history.csv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=30, help="回溯天数（默认 30）")
    parser.add_argument("--force", action="store_true", help="覆盖已有日期的样本（谨慎）")
    parser.add_argument("--no-down", action="store_true", help="不抓跌停池（速度约快一倍）")
    parser.add_argument("--dry-run", action="store_true", help="只列出要补的日期，不写文件")
    parser.add_argument("--include-today", action="store_true",
                        help="包含今天（默认在 15:00 前会跳过，避免写入盘中数据）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    setup_logger(args.log_level)
    raise SystemExit(run(days=args.days, force=args.force,
                         with_down=not args.no_down, dry_run=args.dry_run,
                         include_today=args.include_today))


if __name__ == "__main__":
    main()
