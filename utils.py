# -*- coding: utf-8 -*-
"""
utils.py —— 通用工具层（全项目地基）
================================================================================
本文件提供 5 类能力，被 data_fetch / analyzer / lhb / main 全部依赖：

  1. 日志初始化        setup_logger()
  2. 重试装饰器        @retry_request        <- 核心要求 3（指数退避 + 随机延迟）
  3. 交易日历          get_trade_calendar() / latest_trade_date() / is_trade_date()
  4. 股票过滤          clean_stock_df()       <- 核心要求 2（过滤 ST/退市/北交所）
  5. 缓存与格式化      save_cache() / load_cache() / fmt_money() / fmt_pct()

设计要点（为什么这么写）：
  - 装饰器在**彻底失败时返回空 DataFrame 而不是抛异常**：一个池子抓不到，
    不能让整份复盘报告崩掉，报告里标注"数据缺失"即可。
  - 空 DataFrame 默认视为失败并重试；但"跌停池/炸板池为空"可能是真实行情
    （全市场无跌停），所以留了 allow_empty 开关，避免在强势日空转重试 4 次。
  - 交易日历必须自己维护，不能靠"周一到周五"判断，否则节假日会白跑。
================================================================================
"""

from __future__ import annotations

import functools
import logging
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from config import (
    CACHE_DIR,
    EXCLUDE_CHINEXT,
    EXCLUDE_CODE_PREFIXES,
    EXCLUDE_NAME_KEYWORDS,
    EXCLUDE_STAR_MARKET,
    LOG_DIR,
    LOG_FILENAME,
    LOG_LEVEL,
    RETRY_BACKOFF,
    RETRY_BASE_DELAY,
    RETRY_JITTER,
    RETRY_MAX_DELAY,
    RETRY_TIMES,
)

logger = logging.getLogger("review")


# ==============================================================================
# 1. 日志
# ==============================================================================
def setup_logger(level: str = LOG_LEVEL) -> logging.Logger:
    """
    初始化全局 logger：同时输出到控制台和 logs/review.log。

    - 控制台只打 INFO 及以上，保持输出干净
    - 文件记录 DEBUG，方便出问题时回溯
    - 用 if not logger.handlers 防止重复添加 handler（重复调用不会重复打印）
    """
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # --- 控制台 handler ---
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console_fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S")
    console.setFormatter(console_fmt)
    logger.addHandler(console)

    # --- 文件 handler ---
    try:
        fh = logging.FileHandler(LOG_DIR / LOG_FILENAME, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        file_fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s [%(filename)s:%(lineno)d] %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
        fh.setFormatter(file_fmt)
        logger.addHandler(fh)
    except Exception as exc:  # 日志文件写不了不能影响主流程
        logger.warning("日志文件初始化失败（不影响运行）：%s", exc)

    return logger


# ==============================================================================
# 2. 重试装饰器（核心要求 3）
# ==============================================================================
def retry_request(
    times: int = RETRY_TIMES,
    base_delay: float = RETRY_BASE_DELAY,
    backoff: float = RETRY_BACKOFF,
    max_delay: float = RETRY_MAX_DELAY,
    jitter: float = RETRY_JITTER,
    allow_empty: bool = False,
    label: str = "",
) -> Callable:
    """
    带「指数退避 + 随机抖动」的重试装饰器。

    参数
    ----
    times      : 最大尝试次数（含首次）。times=4 表示最多试 4 次。
    base_delay : 首次退避基数（秒）。
    backoff    : 退避倍数。base=1.5, backoff=2 时等待序列为 1.5 -> 3 -> 6 -> 12
    max_delay  : 单次等待上限，防止退避到几十秒导致脚本看起来"卡死"。
    jitter     : 抖动比例。实际等待 = 理论等待 × (1 ± jitter)。
                 加抖动的目的：多个请求同时重试时错开时间，避免被风控判定为爬虫。
    allow_empty: True 时，返回空 DataFrame 视为**成功**（用于跌停池/炸板池可能出现真实 0 值的场景）。
    label      : 日志里显示的业务名称，不传则用函数名。

    失败行为
    --------
    超过 times 次仍失败 -> 返回空 DataFrame，并在 df.attrs["error"] 里写入错误原因。
    **绝不向上抛异常**，保证上层 main.py 能继续生成其余部分的报告。

    用法
    ----
        @retry_request(times=4, allow_empty=False, label="涨停池")
        def _raw_zt_pool(day: str):
            return ak.stock_zt_pool_em(date=day)
    """

    def decorator(func: Callable) -> Callable:
        name = label or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> pd.DataFrame:
            last_err: Optional[BaseException] = None

            for attempt in range(1, times + 1):
                try:
                    result = func(*args, **kwargs)

                    # ---------- 校验 1：None 视为失败 ----------
                    if result is None:
                        raise ValueError("接口返回 None（通常是接口异常或参数不被识别）")

                    # ---------- 校验 2：非 DataFrame 直接放行 ----------
                    if not isinstance(result, pd.DataFrame):
                        if attempt > 1:
                            logger.info("✅ [%s] 第 %d 次尝试成功", name, attempt)
                        return result  # type: ignore[return-value]

                    # ---------- 校验 3：空表 ----------
                    # 空表有两种可能：① 接口抽风 ② 当天真的是 0 家（如全市场无跌停）
                    if result.empty and not allow_empty:
                        raise ValueError("接口返回空 DataFrame（疑似被限流或字段变更）")

                    if attempt > 1:
                        logger.info("✅ [%s] 第 %d 次尝试成功", name, attempt)
                    return result

                except Exception as exc:  # noqa: BLE001 —— 这里就是要捕获所有异常
                    last_err = exc
                    msg = f"{type(exc).__name__}: {str(exc)[:160]}"

                    if attempt >= times:
                        logger.error("❌ [%s] 重试 %d 次全部失败：%s", name, times, msg)
                        break

                    # 指数退避：base * backoff^(attempt-1)，再乘抖动系数
                    delay = min(base_delay * (backoff ** (attempt - 1)), max_delay)
                    delay = max(0.2, delay * (1 + random.uniform(-jitter, jitter)))
                    logger.warning(
                        "⚠️ [%s] 第 %d/%d 次失败（%s），%.2fs 后重试",
                        name, attempt, times, msg, delay,
                    )
                    time.sleep(delay)

            # ---------- 兜底：返回空表 + 错误标记 ----------
            empty = pd.DataFrame()
            empty.attrs["error"] = f"{type(last_err).__name__}: {last_err}"
            empty.attrs["source"] = name
            return empty

        return wrapper

    return decorator


# ==============================================================================
# 3. 交易日历
# ==============================================================================
_CAL_CACHE_FILE = CACHE_DIR / "trade_calendar.csv"
_cal_memory: Optional[set[date]] = None   # 进程内缓存，避免多次读盘


def get_trade_calendar(force_refresh: bool = False) -> set[date]:
    """
    获取 A 股全部交易日集合（截至当年年末）。

    数据源：ak.tool_trade_date_hist_sina（新浪，返回 1990 年至今 + 当年剩余交易日）
    缓存策略：落盘到 cache/trade_calendar.csv，7 天内不重复请求。
              —— 这个接口很稳定，但没必要每次都调。
    """
    global _cal_memory
    if _cal_memory is not None and not force_refresh:
        return _cal_memory

    # 优先读本地缓存
    if not force_refresh and _CAL_CACHE_FILE.exists():
        age_days = (time.time() - _CAL_CACHE_FILE.stat().st_mtime) / 86400
        if age_days < 7:
            try:
                df = pd.read_csv(_CAL_CACHE_FILE)
                _cal_memory = {
                    datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
                    for s in df["trade_date"].astype(str)
                }
                logger.debug("交易日历：命中本地缓存，共 %d 个交易日", len(_cal_memory))
                return _cal_memory
            except Exception as exc:
                logger.warning("交易日历缓存损坏，重新拉取：%s", exc)

    # 缓存不可用 -> 走网络
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            raise ValueError("交易日历返回空")
        _cal_memory = {
            datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
            for s in df["trade_date"].astype(str)
        }
        df.to_csv(_CAL_CACHE_FILE, index=False, encoding="utf-8-sig")
        logger.info("交易日历：已更新，共 %d 个交易日", len(_cal_memory))
        return _cal_memory
    except Exception as exc:
        logger.error("交易日历获取失败：%s", exc)
        # 极端兜底：只认周一到周五（会误判节假日，但总比直接崩掉好）
        logger.warning("降级为「周一至周五」粗略判断，节假日可能误判")
        today = date.today()
        _cal_memory = {
            today - timedelta(days=i)
            for i in range(0, 400)
            if (today - timedelta(days=i)).weekday() < 5
        }
        return _cal_memory


def is_trade_date(day: date) -> bool:
    """判断某天是否为 A 股交易日。"""
    return day in get_trade_calendar()


def latest_trade_date(ref: Optional[date] = None, lookback: int = 30) -> date:
    """
    返回 <= ref 的最近一个交易日。
    ref 为空时取今天。用于「自动模式」：不传日期时自动定位最近交易日。
    """
    ref = ref or date.today()
    for i in range(lookback + 1):
        d = ref - timedelta(days=i)
        if is_trade_date(d):
            return d
    raise RuntimeError(f"最近 {lookback} 天内找不到交易日，交易日历可能异常")


def prev_trade_date(day: date, lookback: int = 30) -> date:
    """返回 < day 的上一个交易日（严格小于）。"""
    for i in range(1, lookback + 1):
        d = day - timedelta(days=i)
        if is_trade_date(d):
            return d
    raise RuntimeError(f"{day} 之前 {lookback} 天内找不到交易日")


def to_day_str(day: date | str) -> str:
    """
    统一转成 akshare 需要的 'YYYYMMDD' 字符串。
    支持传入 date / datetime / '20261109' / '2026-11-09' / '2026/11/09'。
    """
    if isinstance(day, datetime):
        return day.strftime("%Y%m%d")
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    s = str(day).strip().replace("-", "").replace("/", "").replace(".", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"无法识别的日期格式：{day!r}，请用 20261109 或 2026-11-09")
    return s


def to_date(day: date | str) -> date:
    """统一转成 datetime.date 对象。"""
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    return datetime.strptime(to_day_str(day), "%Y%m%d").date()


# ==============================================================================
# 4. 股票过滤（核心要求 2）
# ==============================================================================
def clean_stock_df(
    df: pd.DataFrame,
    code_col: str = "代码",
    name_col: str = "名称",
    exclude_star: bool = EXCLUDE_STAR_MARKET,
    exclude_chinext: bool = EXCLUDE_CHINEXT,
    dedup: bool = True,
    a_share_only: bool = True,
) -> pd.DataFrame:
    """
    统一清洗：过滤 ST股、退市股、北交所股票，并规范化代码格式。

    过滤规则
    --------
    ① 北交所：代码以 8 / 4 / 92 开头（920xxx 是 2024 年新代码段，最容易漏）
    ② ST类   ：名称含 ST（覆盖 ST / *ST / S*ST / SST）、含「退」、含「PT」
    ③ 非A股  ：只保留 6 / 0 / 3 开头的代码（默认开启，见下方说明）
    ④ 可选   ：科创板 688/689、创业板 300/301（默认不剔除，见 config）

    【为什么还要白名单（规则③）】
      实测发现龙虎榜接口会返回**可转债**，例如 '声迅转债(127080)'。
      黑名单只拦 8/4/92，拦不住 1 开头（转债）、5 开头（场内基金）。
      因此加一层 A 股白名单：代码必须以 6 / 0 / 3 开头。
      这样转债(11x/12x/13x)、基金(5xx)、回购(2xx) 全部自动排除，
      而 60x/68x/00x/30x 主板/创业板/科创板全部保留。

    其他处理
    --------
    - 代码统一补零到 6 位（东财偶尔返回 int 型，如 2578 -> 002578）
    - 按代码去重（同一只票可能因多个上榜原因出现多行，是否去重由调用方决定）

    返回
    ----
    清洗后的新 DataFrame（不改原表），索引已重置。
    """
    if df is None or df.empty:
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    out = df.copy()

    # ---- 代码列不存在则直接返回（调用方自己保证列名）----
    if code_col not in out.columns:
        logger.warning("clean_stock_df: 缺少代码列 '%s'，跳过过滤", code_col)
        return out

    # 代码规范化：转字符串 -> 去空格 -> 去掉可能存在的 .SH/.SZ 后缀 -> 补零 6 位
    code = (
        out[code_col]
        .astype("string")
        .str.strip()
        .str.upper()
        .str.replace(r"\.(SH|SZ|BJ)$", "", regex=True)
        .str.zfill(6)
    )
    out[code_col] = code

    mask = pd.Series(True, index=out.index)
    n_raw = len(out)
    removed = {"北交所": 0, "ST/退市": 0, "非A股": 0, "板块": 0}

    # ---- ① 过滤北交所（8 / 4 / 92 开头）----
    n_before = int(mask.sum())
    for prefix in EXCLUDE_CODE_PREFIXES:
        mask &= ~code.str.startswith(prefix, na=False)
    removed["北交所"] = n_before - int(mask.sum())

    # ---- ② 过滤 ST / 退市 / PT ----
    if name_col in out.columns:
        name_upper = out[name_col].astype("string").str.strip().str.upper()
        n_before = int(mask.sum())
        for kw in EXCLUDE_NAME_KEYWORDS:
            # 用 regex=False 做纯文本包含，避免 "ST" 被当正则
            mask &= ~name_upper.str.contains(kw, na=False, regex=False)
        removed["ST/退市"] = n_before - int(mask.sum())
    else:
        logger.warning("clean_stock_df: 缺少名称列 '%s'，无法过滤 ST", name_col)

    # ---- ③ 非 A 股白名单：只保留 6 / 0 / 3 开头 ----
    if a_share_only:
        n_before = int(mask.sum())
        mask &= code.str.startswith(("6", "0", "3"), na=False)
        removed["非A股"] = n_before - int(mask.sum())

    # ---- ④ 可选：剔除科创板 / 创业板 ----
    if exclude_star:
        n_before = int(mask.sum())
        mask &= ~code.str.startswith(("688", "689"), na=False)
        removed["板块"] += n_before - int(mask.sum())
    if exclude_chinext:
        n_before = int(mask.sum())
        mask &= ~code.str.startswith(("300", "301"), na=False)
        removed["板块"] += n_before - int(mask.sum())

    out = out.loc[mask].reset_index(drop=True)

    if dedup and not out.empty:
        out = out.drop_duplicates(subset=[code_col], keep="first").reset_index(drop=True)

    detail = "、".join(f"{k}{v}" for k, v in removed.items() if v)
    logger.debug(
        "清洗 %d 行 -> %d 行%s",
        n_raw, len(out), f"（剔除 {detail}）" if detail else "",
    )
    return out


# ==============================================================================
# 5. 缓存
# ==============================================================================
def cache_path(key: str, day: str) -> Path:
    """生成缓存文件路径，如 cache/20261109_zt_pool.csv"""
    return CACHE_DIR / f"{to_day_str(day)}_{key}.csv"


def save_cache(df: pd.DataFrame, key: str, day: str) -> None:
    """
    把接口原始结果落盘为 CSV。
    注意：attrs（含错误标记）不会写入 CSV，属正常现象——缓存只存数据本身。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return
    try:
        path = cache_path(key, day)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        # 附带一个 meta 文件记录抓取时间，方便判断缓存新鲜度
        meta = path.with_suffix(".meta")
        meta.write_text(
            f"key={key}\nday={to_day_str(day)}\n"
            f"rows={len(df)}\nfetched_at={datetime.now():%Y-%m-%d %H:%M:%S}\n",
            encoding="utf-8",
        )
        logger.debug("缓存已写入：%s（%d 行）", path.name, len(df))
    except Exception as exc:
        logger.warning("缓存写入失败（不影响主流程）：%s", exc)


def load_cache(key: str, day: str) -> Optional[pd.DataFrame]:
    """读取缓存；不存在或损坏返回 None。"""
    path = cache_path(key, day)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype={"代码": str}, encoding="utf-8-sig")
        if df.empty:
            return None
        logger.info("📦 [%s] 命中本地缓存 %s（%d 行）", key, path.name, len(df))
        return df
    except Exception as exc:
        logger.warning("缓存读取失败 %s：%s", path.name, exc)
        return None


# ==============================================================================
# 6. 格式化工具（给报告渲染用）
# ==============================================================================
def fmt_money(value: Any, unit: bool = True) -> str:
    """
    金额人性化：123456789 -> '1.23亿'；1234567 -> '123.46万'；None -> '-'
    unit=False 时不带单位，只返回数字字符串。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(v):
        return "-"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e8:
        s = f"{v / 1e8:.2f}亿"
    elif v >= 1e4:
        s = f"{v / 1e4:.2f}万"
    else:
        s = f"{v:.0f}"
    return f"{sign}{s}" if unit else f"{sign}{v:.0f}"


def fmt_pct(value: Any, digits: int = 2, sign: bool = True) -> str:
    """
    百分比：9.98 -> '+9.98%'；-3.2 -> '-3.20%'；None -> '-'
    注意：入参是"已经是百分数"的值（东财返回的涨跌幅就是 9.98 这种），不要再乘 100。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(v):
        return "-"
    if sign:
        return f"{v:+.{digits}f}%"
    return f"{v:.{digits}f}%"


def safe_div(numerator: Any, denominator: Any, default: float = 0.0) -> float:
    """安全除法：分母为 0 / None / NaN 时返回 default，避免 ZeroDivisionError。"""
    try:
        n, d = float(numerator), float(denominator)
        if d == 0 or pd.isna(n) or pd.isna(d):
            return default
        return n / d
    except (TypeError, ValueError):
        return default


def to_numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    """
    批量把列转成数值型（errors="coerce"，转不了的变 NaN 而不是报错）。
    东财偶尔把 涨跌幅 返回成字符串 '9.98%'，这里顺手去掉 % 号。
    """
    out = df.copy()
    for col in cols:
        if col not in out.columns:
            continue
        s = out[col]
        if s.dtype == object or str(s.dtype).startswith("string"):
            s = s.astype("string").str.replace("%", "", regex=False).str.strip()
        out[col] = pd.to_numeric(s, errors="coerce")
    return out


def banner(text: str, width: int = 78) -> str:
    """生成控制台分隔标题，纯粹为了日志好看。"""
    line = "=" * width
    return f"\n{line}\n  {text}\n{line}"
