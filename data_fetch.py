# -*- coding: utf-8 -*-
"""
data_fetch.py —— 模块一：数据抓取与容错层
================================================================================
职责边界：只负责"把干净、结构统一的 DataFrame 交出去"，不做任何业务计算。

对外接口
--------
    fetch_zt_pool(day)        今日涨停池
    fetch_dt_pool(day)        今日跌停池
    fetch_zb_pool(day)        今日炸板池
    fetch_prev_zt_pool(day)   昨日涨停池（含这批票"今日"的涨跌幅）
    fetch_lhb(day)            今日龙虎榜明细
    fetch_active_yyb(day)     今日活跃营业部（游资画像主数据源）
    fetch_lhb_seats(code,day) 个股买卖前五席位（逐票抓，失败率高，必须容错）
    fetch_index_snapshot(day) 三大指数涨跌幅
    fetch_all(day)            一次性抓全部，返回 (data_dict, health_dict)

三层容错设计
------------
    第 1 层  @retry_request   ：网络超时/限流 -> 指数退避重试
    第 2 层  _normalize_fields：字段改名 -> 候选名映射，缺字段填 NA 并告警
    第 3 层  本地缓存降级     ：重试全失败 -> 读 cache/ 里的历史快照

【重要实测结论，避免踩坑】
  · stock_zh_index_spot_em 返回的是**实时快照**，复盘历史日会拿到今天的数据。
    因此指数一律走 stock_zh_index_daily_em（历史日线），并按日期精确切片。
  · stock_lhb_stock_detail_em 对部分个股直接抛 TypeError
    （'NoneType' object is not subscriptable），必须逐票 try-except。
  · 涨停池原生带「连板数」「涨停统计」「所属行业」字段，无需自己回溯计算。
================================================================================
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import config
from config import (
    DT_POOL_SPEC,
    INTERFACE_SLEEP,
    LHB_SPEC,
    PREV_ZT_POOL_SPEC,
    SEAT_SPEC,
    USE_CACHE,
    YYB_SPEC,
    ZB_POOL_SPEC,
    ZT_POOL_SPEC,
)
from utils import (
    clean_stock_df,
    is_trade_date,
    load_cache,
    logger,
    retry_request,
    save_cache,
    to_date,
    to_day_str,
    to_numeric,
)

# akshare 采用延迟导入的写法放在函数内会显得啰嗦，这里顶层导入但做好保护：
# 好处是 import 阶段就能发现依赖缺失；缺失时给出明确提示而不是一堆 NameError。
try:
    import akshare as ak
except ImportError as _e:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    logger.error("未安装 akshare：pip install akshare -i https://pypi.tuna.tsinghua.edu.cn/simple")


# ==============================================================================
# 内部工具
# ==============================================================================
def _require_ak() -> None:
    """每次抓数前确认 akshare 可用，避免反复写 if ak is None。"""
    if ak is None:
        raise RuntimeError("akshare 未安装，请先执行：pip install akshare")


def _normalize_fields(df: pd.DataFrame, spec: Dict[str, List[str]], source: str) -> pd.DataFrame:
    """
    字段标准化（第 2 层容错）。

    逻辑：
      1. 若标准字段名已存在 -> 跳过
      2. 否则在候选名列表里找第一个存在的列 -> 复制一份并改用标准名
         （保留原列，避免下游用到未预期的字段时 KeyError）
      3. 都找不到 -> 建一个全 NA 的标准列 + WARNING 日志
         （这样下游 df["连板数"] 永远不报 KeyError，只是值为空）

    这是"AKShare 字段改名"这一最大风险点的核心防御：改名字只需改 config 的候选列表。
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    missing: List[str] = []
    renamed: List[str] = []

    for target, aliases in spec.items():
        if target in out.columns:
            continue
        hit = next((a for a in aliases if a in out.columns), None)
        if hit is not None:
            out[target] = out[hit]
            renamed.append(f"{hit}->{target}")
        else:
            out[target] = pd.NA
            missing.append(target)

    if renamed:
        logger.debug("[%s] 字段已映射：%s", source, ", ".join(renamed))
    if missing:
        logger.warning(
            "[%s] 以下字段在接口返回中未找到，已填空值（可能是东财改版）：%s",
            source, ", ".join(missing),
        )

    return out


def _record_health(
    health: Dict[str, Dict[str, Any]],
    source: str,
    df: pd.DataFrame,
    elapsed: float,
    note: str = "",
) -> None:
    """
    记录单个数据源的健康状态，最终会渲染到报告的「数据源状态」一栏。
    df.attrs["error"] 由 retry_request 在彻底失败时写入。
    """
    err = df.attrs.get("error", "") if isinstance(df, pd.DataFrame) else ""
    ok = isinstance(df, pd.DataFrame) and not df.empty and not err
    health[source] = {
        "ok": ok,
        "rows": 0 if df is None or not isinstance(df, pd.DataFrame) else len(df),
        "seconds": round(elapsed, 2),
        "error": str(err)[:200] if err else "",
        "note": note,
    }


# ------------------------------------------------------------------------------
# 同花顺兜底函数（薄封装）
# ------------------------------------------------------------------------------
# 为什么不直接把 ths_fetch.fetch_zt_pool 传进去：
#   ① 延迟导入 —— 没配 Key 的用户不该在 import data_fetch 时就加载这个模块
#   ② 统一异常边界 —— 任何 ImportError / 属性错误都在这里收敛
#   ③ 命名对齐 —— ths_fetch 里的函数名与东财的 fetch_* 容易混淆，加 _ths_ 前缀区分

def _ths_zt_pool(day_str: str) -> pd.DataFrame:
    import ths_fetch
    return ths_fetch.fetch_zt_pool(day_str)


def _ths_zb_pool(day_str: str) -> pd.DataFrame:
    import ths_fetch
    return ths_fetch.fetch_zb_pool(day_str)


def _ths_dt_pool(day_str: str) -> pd.DataFrame:
    import ths_fetch
    return ths_fetch.fetch_dt_pool(day_str)


def _ths_lhb(day_str: str) -> pd.DataFrame:
    """龙虎榜兜底：先用 all 榜；all 空则退回 org 榜（有些票只上机构榜）。"""
    import ths_fetch
    df = ths_fetch.fetch_lhb_stock(day_str, board_type="all")
    if df is not None and not df.empty:
        return df
    return ths_fetch.fetch_lhb_stock(day_str, board_type="org")


def _try_ths_fallback(ths_func, day_str: str, source: str) -> Optional[pd.DataFrame]:
    """
    调用同花顺兜底函数，把一切异常收敛成「返回 None」。

    【为什么要吞异常】降级本身是"锦上添花"。东财已经挂了，
    如果同花顺再抛异常把整个 fetch_all 打断，报告就彻底出不来了。
    这里必须保证：同花顺的任何问题都不会让主流程更糟。

    但"吞掉"不等于"不报"——失败原因一定写日志，
    否则用户会以为「同花顺没数据」，而实际是「Key 没配好」。
    """
    try:
        # 延迟导入：没装 Key 的用户根本不该加载这个模块
        import ths_fetch

        if not ths_fetch.is_enabled():
            st = ths_fetch.status()
            if not st.get("has_key"):
                logger.info("[%s] 同花顺未配置 API Key，跳过降级", source)
            else:
                logger.info("[%s] 同花顺通道已关闭（FUYAO_FALLBACK=0），跳过降级", source)
            return None

        df = ths_func(day_str)
        if df is None:
            return None
        return df

    except Exception as exc:
        # 用 warning 而不是 debug：降级失败是用户需要知道的事
        logger.warning("[%s] 同花顺降级失败：%s: %s", source, type(exc).__name__, exc)
        return None


def _fetch_generic(
    key: str,
    day: str,
    raw_func,
    spec: Dict[str, List[str]],
    source: str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
    allow_empty: bool = False,
    sleep: float = INTERFACE_SLEEP,
    ths_func=None,
) -> pd.DataFrame:
    """
    通用抓取流水线：缓存 -> 重试抓取 -> [降级同花顺] -> 字段标准化 -> 数值化 -> 回写缓存

    所有股池函数都走这一条流水线，保证行为完全一致，也方便统一改逻辑。

    参数 ths_func
    -------------
    可选的同花顺兜底函数，签名 `f(day_str) -> DataFrame`。
    东财彻底失败（重试 4 次仍为空）时才会被调用，且**返回列已与东财一致**
    （由 ths_fetch.py 保证），所以这里不需要再走一遍字段映射。

    降级顺序的取舍
    --------------
        缓存 → 东财 → 同花顺 → 历史缓存兜底 → 空表
    为什么同花顺排在"历史缓存兜底"前面：
        历史缓存可能是好几天前的快照，而同花顺给的是**当日真实数据**。
        能拿到今天的正确数据，就不该用一个过期的旧快照糊弄过去。
    """
    day_str = to_day_str(day)

    # ---------- 第 0 层容错：非交易日直接短路 ----------
    # 为什么要提前判断：非交易日调东财接口只会拿到空表，然后白跑 4 次重试
    # （指数退避累计约 10 秒），既浪费时间又增加被限流的风险。
    try:
        if not is_trade_date(to_date(day_str)):
            logger.info("[%s] %s 非交易日，跳过抓取", source, day_str)
            empty = pd.DataFrame()
            empty.attrs["error"] = "非交易日"
            return empty
    except Exception:  # 日历本身异常时不阻断主流程
        pass

    # ---------- 第 3 层容错：缓存优先 ----------
    if use_cache and not refresh:
        cached = load_cache(key, day_str)
        if cached is not None:
            cached = _normalize_fields(cached, spec, source)
            return _coerce(cached, spec)

    # ---------- 第 1 层容错：带重试的抓取 ----------
    t0 = time.time()
    raw = raw_func(day_str)
    elapsed = time.time() - t0

    # 抓完停一下，降低被东财风控的概率（串行抓 6~8 个接口时很重要）
    if sleep > 0:
        time.sleep(sleep)

    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        err = raw.attrs.get("error", "") if isinstance(raw, pd.DataFrame) else "返回非 DataFrame"
        logger.warning("[%s] 东财无数据（%s）", source, err or "空表")

        # ---------- 第 1.5 层容错：降级到同花顺 ----------
        # 触发条件：东财重试 4 次后仍为空（且不是"非交易日"短路）
        if ths_func is not None and getattr(config, "THS_ENABLE_FALLBACK", False):
            ths_df = _try_ths_fallback(ths_func, day_str, source)
            if ths_df is not None and not ths_df.empty:
                # 同花顺返回的已是标准列，这里仍走一遍标准化 + 数值化，
                # 保证 dtype 与东财路径完全一致（下游 analyzer 依赖这一点）
                out = _normalize_fields(ths_df, spec, source)
                out = _coerce(out, spec)
                try:
                    save_cache(ths_df, key, day_str)   # 落盘，避免下次又请求
                except Exception as exc:
                    logger.debug("[%s] 同花顺结果写入缓存失败：%s", source, exc)
                out.attrs["provider"] = "ths"
                out.attrs["fallback"] = True
                logger.warning("[%s] ✅ 已降级到同花顺数据源（%d 行）", source, len(out))
                return out
            logger.warning("[%s] 同花顺也拿不到，继续找历史缓存", source)

        logger.warning("[%s] 尝试用历史缓存兜底", source)
        fallback = load_cache(key, day_str)
        if fallback is not None:
            logger.info("[%s] 已启用缓存兜底，数据可能不是最新", source)
            fallback = _normalize_fields(fallback, spec, source)
            fallback.attrs["from_cache"] = True
            return _coerce(fallback, spec)
        return pd.DataFrame()

    # ---------- 第 2 层容错：字段标准化 ----------
    out = _normalize_fields(raw, spec, source)
    out = _coerce(out, spec)

    # ---------- 数据清洗：ST / 退市 / 北交所 ----------
    # 注意：龙虎榜/营业部表结构不同，由各自函数决定是否调用 clean_stock_df
    save_cache(raw, key, day_str)

    logger.info("[%s] 抓取成功，%d 行，耗时 %.2fs", source, len(out), elapsed)
    return out


def _coerce(df: pd.DataFrame, spec: Dict[str, List[str]]) -> pd.DataFrame:
    """
    把 spec 里声明的字段统一转成数值型。
    为什么必须做：东财同一字段有时返回 '9.98' 有时返回 '9.98%' 有时返回 int，
    pandas 混类型列会让后面所有 > < 比较和 mean() 计算全部报错或给出错误结果。
    """
    if df is None or df.empty:
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    numeric_cols = [
        "涨跌幅", "最新价", "成交额", "流通市值", "总市值", "换手率", "封板资金",
        "封单资金", "炸板次数", "开板次数", "连板数", "昨日连板数", "连续跌停",
        "振幅", "涨停价", "板上成交额", "涨速", "动态市盈率",
        "龙虎榜净买额", "龙虎榜买入额", "龙虎榜卖出额", "龙虎榜成交额",
        "市场总成交额", "净买额占总成交比", "成交额占总成交比", "收盘价",
        "买入总金额", "卖出总金额", "总买卖净额", "买入个股数", "卖出个股数",
        "买入金额", "卖出金额", "净额",
        "买入金额-占总成交比例", "卖出金额-占总成交比例",
    ]
    cols = [c for c in numeric_cols if c in df.columns]
    return to_numeric(df, cols)


# ==============================================================================
# 原始接口封装（只做"调用 + 重试"，不做任何加工）
# 单独抽出来是为了：① 重试装饰器可单独配置次数/是否允许空 ② 便于单测 mock
# ==============================================================================
@retry_request(times=4, allow_empty=False, label="涨停池")
def _raw_zt_pool(day: str) -> pd.DataFrame:
    """涨停股池（东财）。空表一定是异常：A股再差也不会 0 家涨停。"""
    _require_ak()
    return ak.stock_zt_pool_em(date=day)


@retry_request(times=3, allow_empty=True, label="炸板池")
def _raw_zb_pool(day: str) -> pd.DataFrame:
    """
    炸板股池。allow_empty=True 的原因：极端强势日可能出现"所有涨停全部封死、0 炸板"，
    这种空表是**真实行情**而非接口故障，不该白等重试。
    """
    _require_ak()
    return ak.stock_zt_pool_zbgc_em(date=day)


@retry_request(times=3, allow_empty=True, label="跌停池")
def _raw_dt_pool(day: str) -> pd.DataFrame:
    """跌停股池。allow_empty=True 同理：普涨日 0 跌停是正常的。"""
    _require_ak()
    return ak.stock_zt_pool_dtgc_em(date=day)


@retry_request(times=4, allow_empty=False, label="昨日涨停池")
def _raw_prev_zt_pool(day: str) -> pd.DataFrame:
    """昨日涨停股池。空表异常：任何一天都有昨日涨停股。"""
    _require_ak()
    return ak.stock_zt_pool_previous_em(date=day)


@retry_request(times=4, allow_empty=True, label="龙虎榜")
def _raw_lhb(day: str) -> pd.DataFrame:
    """
    龙虎榜明细（区间查询，起止传同一天）。
    注意：allow_empty=True —— 极少数情况下（如节前最后一天）确实可能无龙虎榜。
    """
    _require_ak()
    return ak.stock_lhb_detail_em(start_date=day, end_date=day)


@retry_request(times=4, allow_empty=True, label="活跃营业部")
def _raw_active_yyb(day: str) -> pd.DataFrame:
    """每日活跃营业部（东财）。游资画像的首选数据源，覆盖率最高。"""
    _require_ak()
    return ak.stock_lhb_hyyyb_em(start_date=day, end_date=day)


@retry_request(times=2, allow_empty=True, label="个股席位")
def _raw_lhb_seats(code: str, day: str, flag: str) -> pd.DataFrame:
    """
    个股龙虎榜买卖前五席位。
    times=2 且 allow_empty=True：这个接口实测不稳定（部分个股直接抛
    TypeError: 'NoneType' object is not subscriptable），重试多了纯属浪费时间，
    失败就跳过——因为活跃营业部表已经能覆盖大部分游资识别需求。
    """
    _require_ak()
    return ak.stock_lhb_stock_detail_em(symbol=code, date=day, flag=flag)


@retry_request(times=3, allow_empty=False, label="指数日线")
def _raw_index_daily(symbol: str) -> pd.DataFrame:
    """指数历史日线。symbol 形如 sh000001 / sz399001 / sz399006。"""
    _require_ak()
    return ak.stock_zh_index_daily_em(symbol=symbol)


# ==============================================================================
# 对外：五大股池
# ==============================================================================
def probe_zt_available(day: date | str) -> bool:
    """
    轻量探测：某一交易日的涨停池是否还能取到数据。

    【为什么需要这个函数】
      东财涨停池只保留约 20 个交易日。`--calibrate 30` 回溯时会碰到一批
      已过期的日期，它们**必然**返回空表。如果对每个这样的日期都走完整的
      fetch_all（6 个接口 × 4 次指数退避重试），实测单个日期要 29 秒，
      30 天校准里 10 分钟全花在注定失败的日子上。

      本函数只调 1 个接口、**不重试**，几百毫秒就能判断出"这天没数据"，
      让校准逻辑快速跳过。

    返回
    ----
    True  : 有数据（还在保留窗口内）
    False : 无数据（超出窗口 / 停市 / 网络异常，调用方按"跳过"处理）
    """
    day_str = to_day_str(day)
    try:
        _require_ak()
        df = ak.stock_zt_pool_em(date=day_str)
        return df is not None and not df.empty
    except Exception as exc:
        logger.debug("[探测 %s] 涨停池不可用：%s", day_str, exc)
        return False


def fetch_zt_pool(
    day: date | str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
) -> pd.DataFrame:
    """
    今日涨停池（已过滤 ST / 退市 / 北交所）。

    返回列（标准名，缺失时为 NA）：
        代码 名称 涨跌幅 最新价 成交额 流通市值 换手率 封板资金
        首次封板时间 最后封板时间 炸板次数 涨停统计 连板数 所属行业
    """
    df = _fetch_generic(
        key="zt_pool", day=day, raw_func=_raw_zt_pool, spec=ZT_POOL_SPEC,
        source="涨停池", use_cache=use_cache, refresh=refresh, allow_empty=False,
        ths_func=_ths_zt_pool,
    )
    if df.empty:
        return df
    df = clean_stock_df(df)
    # 连板数缺失时用「涨停统计」（格式 'n天m板'）兜底解析
    df = _fill_lianban_from_stat(df)
    return df


def fetch_zb_pool(
    day: date | str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
) -> pd.DataFrame:
    """
    今日炸板池（曾涨停但收盘未封住）。已过滤 ST / 退市 / 北交所。

    返回列：代码 名称 涨跌幅 最新价 涨停价 成交额 换手率
            首次封板时间 炸板次数 涨停统计 振幅 所属行业
    """
    df = _fetch_generic(
        key="zb_pool", day=day, raw_func=_raw_zb_pool, spec=ZB_POOL_SPEC,
        source="炸板池", use_cache=use_cache, refresh=refresh, allow_empty=True,
        ths_func=_ths_zb_pool,
    )
    return clean_stock_df(df) if not df.empty else df


def fetch_dt_pool(
    day: date | str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
) -> pd.DataFrame:
    """
    今日跌停池。已过滤 ST / 退市 / 北交所。

    返回列：代码 名称 涨跌幅 最新价 成交额 换手率 封单资金
            最后封板时间 连续跌停 开板次数 所属行业
    """
    df = _fetch_generic(
        key="dt_pool", day=day, raw_func=_raw_dt_pool, spec=DT_POOL_SPEC,
        source="跌停池", use_cache=use_cache, refresh=refresh, allow_empty=True,
        ths_func=_ths_dt_pool,
    )
    return clean_stock_df(df) if not df.empty else df


def fetch_prev_zt_pool(
    day: date | str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
) -> pd.DataFrame:
    """
    昨日涨停池 —— 打板赚钱效应的核心数据。

    【关键理解】这个接口虽叫"昨日涨停"，但返回的「涨跌幅」是这批股票
    **今日**的涨跌幅。所以：
        昨日涨停股今日平均涨跌幅 = 这个表「涨跌幅」列的均值
    均值 > 0 = 打板/接力有钱赚（赚钱效应正）；< 0 = 亏钱效应。

    返回列：代码 名称 涨跌幅 最新价 成交额 换手率 振幅
            昨日封板时间 昨日连板数 所属行业
    """
    df = _fetch_generic(
        key="prev_zt_pool", day=day, raw_func=_raw_prev_zt_pool, spec=PREV_ZT_POOL_SPEC,
        source="昨日涨停池", use_cache=use_cache, refresh=refresh, allow_empty=False,
    )
    return clean_stock_df(df, dedup=False) if not df.empty else df


# ==============================================================================
# 对外：龙虎榜
# ==============================================================================
def fetch_lhb(
    day: date | str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
) -> pd.DataFrame:
    """
    今日龙虎榜明细（已过滤 ST / 退市 / 北交所）。

    【注意】同一只股票可能因多个「上榜原因」出现多行，这里**不去重**
    （去重会丢掉上榜原因），由 lhb.py 聚合时自己处理。

    返回列：代码 名称 上榜日 解读 收盘价 涨跌幅 龙虎榜净买额
            龙虎榜买入额 龙虎榜卖出额 换手率 流通市值 上榜原因
    """
    df = _fetch_generic(
        key="lhb", day=day, raw_func=_raw_lhb, spec=LHB_SPEC,
        source="龙虎榜", use_cache=use_cache, refresh=refresh, allow_empty=True,
        ths_func=_ths_lhb,
    )
    return clean_stock_df(df, dedup=False) if not df.empty else df


def fetch_active_yyb(
    day: date | str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
) -> pd.DataFrame:
    """
    今日活跃营业部（游资画像主数据源）。

    【为什么不走 clean_stock_df】这张表的"主体"是营业部，不是股票，
    北交所/ST 过滤在这里无意义，过滤动作放在 lhb.py 按个股拆分时做。

    返回列：营业部名称 上榜日 买入个股数 卖出个股数 买入总金额
            卖出总金额 总买卖净额 买入股票 营业部代码
    """
    return _fetch_generic(
        key="active_yyb", day=day, raw_func=_raw_active_yyb, spec=YYB_SPEC,
        source="活跃营业部", use_cache=use_cache, refresh=refresh, allow_empty=True,
    )


def fetch_lhb_seats(
    code: str,
    day: date | str,
    flag: str = "买入",
    sleep: float = 0.3,
) -> pd.DataFrame:
    """
    单只股票的龙虎榜买卖前五席位。

    参数
    ----
    code : 6 位股票代码，如 '002310'
    flag : '买入' -> 买入前五；'卖出' -> 卖出前五

    返回列：交易营业部名称 买入金额 买入金额-占总成交比例
            卖出金额 卖出金额-占总成交比例 净额

    【坑位提示】东财对"没有该方向席位数据"的股票会返回异常页，
    akshare 内部解析时抛 TypeError。这里已经用 try 包住，直接返回空表。
    """
    day_str = to_day_str(day)
    if sleep > 0:
        time.sleep(sleep)   # 逐票抓必须限速，否则几十只票必然被限流
    try:
        df = _raw_lhb_seats(str(code).zfill(6), day_str, flag)
        if df is None or df.empty:
            return pd.DataFrame()
        return _coerce(_normalize_fields(df, SEAT_SPEC, f"席位{code}"), SEAT_SPEC)
    except Exception as exc:  # 双保险：装饰器已兜底，这里再兜一层
        logger.debug("[席位 %s %s] 抓取失败：%s", code, flag, exc)
        return pd.DataFrame()


# ==============================================================================
# 对外：指数
# ==============================================================================
INDEX_SYMBOLS: Dict[str, str] = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
}


def fetch_index_snapshot(day: date | str) -> pd.DataFrame:
    """
    三大指数在指定交易日的涨跌幅。

    【为什么不用 stock_zh_index_spot_em】那个接口返回**实时快照**，
    复盘历史日期时会拿到"今天"的数据，静默给出错误结论——这种错误最难发现。
    因此统一走历史日线 stock_zh_index_daily_em，按日期精确定位后再算涨跌幅。

    返回列：指数 收盘 涨跌幅 成交额
    """
    day_str = to_day_str(day)
    rows: List[Dict[str, Any]] = []

    for name, symbol in INDEX_SYMBOLS.items():
        try:
            df = _raw_index_daily(symbol)
            if df is None or df.empty:
                continue

            # 该接口返回 date 为 'YYYY-MM-DD' 字符串，统一成同格式再比对
            df = df.copy()
            df["date"] = df["date"].astype(str).str[:10]
            target = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:]}"

            hit = df.index[df["date"] == target].tolist()
            if not hit:
                logger.debug("[指数 %s] 无 %s 数据（可能当日未收盘）", name, target)
                continue

            i = hit[0]
            close = float(df.at[i, "close"])
            amount = float(df.at[i, "amount"]) if "amount" in df.columns else float("nan")

            # 涨跌幅需要前一根K线的收盘价
            if i == 0:
                pct = float("nan")
            else:
                prev_close = float(df.at[i - 1, "close"])
                pct = (close / prev_close - 1) * 100 if prev_close else float("nan")

            rows.append({"指数": name, "收盘": close, "涨跌幅": pct, "成交额": amount})
            time.sleep(0.2)

        except Exception as exc:
            logger.warning("[指数 %s] 抓取失败：%s", name, exc)

    return pd.DataFrame(rows)


# ==============================================================================
# 内部：连板数兜底解析
# ==============================================================================
def _fill_lianban_from_stat(df: pd.DataFrame) -> pd.DataFrame:
    """
    连板数容错：若「连板数」为空但有「涨停统计」（格式如 '11/7' = 11天7板），
    则取斜杠后的数字作为连板数。

    说明：'n天m板' 里的 m 严格说是"涨停次数"，不完全等于"连续涨停数"，
    但在东财口径下二者对超短线接力判断等价（中间断板不会被记入连板池）。
    仅作为**兜底**，正常情况接口自带的「连板数」优先。
    """
    if df is None or df.empty:
        return df
    if "涨停统计" not in df.columns:
        return df

    out = df.copy()
    need_fill = out["连板数"].isna() if "连板数" in out.columns else pd.Series(True, index=out.index)
    if not need_fill.any():
        return out

    if "连板数" not in out.columns:
        out["连板数"] = pd.NA

    parsed = (
        out["涨停统计"].astype("string").str.strip()
        .str.extract(r"^\s*\d+\s*/\s*(\d+)\s*$")[0]
    )
    parsed_num = pd.to_numeric(parsed, errors="coerce")

    out.loc[need_fill, "连板数"] = parsed_num[need_fill]
    out["连板数"] = pd.to_numeric(out["连板数"], errors="coerce").fillna(1).astype(int)

    n_filled = int((need_fill & parsed_num.notna()).sum())
    if n_filled:
        logger.info("连板数兜底：从「涨停统计」解析补齐 %d 只", n_filled)
    return out


# ==============================================================================
# 对外：一键抓全部
# ==============================================================================
def fetch_all(
    day: date | str,
    use_cache: bool = USE_CACHE,
    refresh: bool = False,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Dict[str, Any]]]:
    """
    一次性抓取复盘所需的全部数据。

    返回
    ----
    data   : dict，键为 zt / zb / dt / prev_zt / lhb / yyb / index
    health : dict，每个数据源的 {ok, rows, seconds, error, note}

    设计原则：**任何一个源失败都不中断**，失败的源在 health 里标 ok=False，
    最终在报告顶部显示"数据源状态"，让你一眼知道这份报告缺了哪块。
    """
    day_str = to_day_str(day)
    logger.info("开始抓取 %s 的全部数据...", day_str)

    data: Dict[str, pd.DataFrame] = {}
    health: Dict[str, Dict[str, Any]] = {}

    tasks = [
        ("zt",      "涨停池",     lambda: fetch_zt_pool(day_str, use_cache, refresh)),
        ("zb",      "炸板池",     lambda: fetch_zb_pool(day_str, use_cache, refresh)),
        ("dt",      "跌停池",     lambda: fetch_dt_pool(day_str, use_cache, refresh)),
        ("prev_zt", "昨日涨停池", lambda: fetch_prev_zt_pool(day_str, use_cache, refresh)),
        ("lhb",     "龙虎榜",     lambda: fetch_lhb(day_str, use_cache, refresh)),
        ("yyb",     "活跃营业部", lambda: fetch_active_yyb(day_str, use_cache, refresh)),
    ]

    for key, label, fn in tasks:
        t0 = time.time()
        try:
            df = fn()
        except Exception as exc:  # 最后一道防线：单源异常绝不影响其它源
            logger.error("[%s] 未捕获异常：%s", label, exc)
            df = pd.DataFrame()
            df.attrs["error"] = f"{type(exc).__name__}: {exc}"
        data[key] = df
        _record_health(health, label, df, time.time() - t0)
        if not health[label]["ok"]:
            logger.warning("[%s] 数据源不可用：%s", label, health[label]["error"] or "空表")

    # 指数单独处理（内部已逐指数容错，不会抛异常）
    t0 = time.time()
    try:
        idx_df = fetch_index_snapshot(day_str)
    except Exception as exc:
        logger.warning("指数抓取失败：%s", exc)
        idx_df = pd.DataFrame()
    data["index"] = idx_df
    _record_health(health, "指数", idx_df, time.time() - t0)

    ok_cnt = sum(1 for v in health.values() if v["ok"])
    logger.info("数据抓取完成：%d/%d 个数据源正常", ok_cnt, len(health))

    return data, health


# ==============================================================================
# 自检入口：python data_fetch.py 20261109
# ==============================================================================
if __name__ == "__main__":
    import sys

    from utils import latest_trade_date, setup_logger

    setup_logger()
    # 不传日期则自动定位最近交易日
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg:
        from utils import to_date
        target = to_date(arg)
    else:
        target = latest_trade_date()

    print(f"\n>>> 自检日期：{target}\n")

    d, h = fetch_all(target, use_cache=False, refresh=True)

    print("\n" + "=" * 70)
    print(f"{'数据源':<12}{'状态':<6}{'行数':>8}{'耗时(s)':>10}  备注")
    print("-" * 70)
    for name, info in h.items():
        status = "✅ OK" if info["ok"] else "❌ FAIL"
        note = info["error"][:34] if info["error"] else info["note"]
        print(f"{name:<12}{status:<6}{info['rows']:>8}{info['seconds']:>10.2f}  {note}")
    print("=" * 70)

    if not d["zt"].empty:
        cols = [c for c in ["代码", "名称", "连板数", "涨停统计", "所属行业"] if c in d["zt"].columns]
        print("\n涨停池前 5 行：")
        print(d["zt"][cols].head().to_string(index=False))

    if not d["prev_zt"].empty:
        print(f"\n昨日涨停股今日平均涨跌幅：{d['prev_zt']['涨跌幅'].mean():+.2f}%")

    if not d["index"].empty:
        print("\n指数：")
        print(d["index"].to_string(index=False))
