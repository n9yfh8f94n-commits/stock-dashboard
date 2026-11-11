# -*- coding: utf-8 -*-
"""
lhb.py —— 模块三：龙虎榜游资画像
================================================================================
这是全系统逻辑最绕的模块，因为它要处理三个真实存在的数据缺陷：

  缺陷 1｜代码 vs 名称不匹配（最致命，静默无结果）
      活跃营业部表的「买入股票」字段里存的是**股票名称**（"红太阳 超声电子 …"，
      空格分隔），不是代码。而盯盘清单传进来的是**代码**。
      草稿里的 `[c for c in target_codes if c in bought_stocks]` 永远匹配不上，
      不报错、不告警，就是静默返回空列表——这种 bug 最难发现。
      修法：先建 代码->名称 映射表，再用名称去做子串匹配（做好边界切分，防"东方"匹配到"东方通信"）。

  缺陷 2｜同一营业部重复出现（金额虚高）
      实测：国泰海通上海长宁区江苏路 / 长宁区江苏路 / 上海江苏路 是**同一个营业部**
      （营业部代码都是 10025080），在表里出现 3 行，直接相加会算 3 倍。
      修法：按「营业部代码」去重，保留金额最大的那行。

  缺陷 3｜活跃营业部表没有「卖出股票」字段
      字段只有：买入个股数 / 卖出个股数 / 买入总金额 / 卖出总金额 / 总买卖净额 / 买入股票。
      **没有卖出股票名单**。所以第一层能告诉你"某游资净卖出 X 万"，但无法告诉你"砸的是哪只票"。
      要定位卖出标的，只能走第二层（个股席位接口）。

  缺陷 4｜机构席位在第一层完全不可见
      实测「机构专用」在活跃营业部表的命中数是 0，但在个股席位表里前 16 只票出现 25 次。
      结论：机构动向必须靠第二层，第一层永远抓不到"机构专用"。

双层降级策略
------------
    第一层（活跃营业部全表扫描）
        优点：一次请求覆盖全市场所有上榜营业部，覆盖率最高、速度最快
        缺点：只有买入名单、无机构席位、无法定位卖出标的
    第二层（逐票查买卖前五席位）
        优点：能看到机构专用、能看到具体买卖方向和金额、能定位卖出
        缺点：一股一请求，60 只票要 30 秒以上，且接口对"无数据"的股票会抛异常
    策略：第一层先跑（快），第二层只补第一层没覆盖到的目标股 + 全部目标股查卖出侧

对外接口
--------
    match_youzi(...)          主入口：双层匹配，返回 list[dict]
    to_dataframe(records)     list -> DataFrame（给报告渲染用）
    summarize_by_youzi(df)    按游资别称聚合（同一游资多个席位合并）
    fetch_lhb_net_rank(df,n)  龙虎榜净买/净卖排名
    institutional_flow(df)    从「解读」字段统计机构参与度
================================================================================
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

from config import (
    ENABLE_BROAD_YOUZI,
    LHB_SEAT_MAX_CODES,
    LHB_SEAT_PREFER_SINGLE_DAY,
    YOUZI_BROAD,
    YOUZI_MAP,
    YOUZI_MAX_STOCKS,
    YOUZI_MIN_KEYWORD_LEN,
)
from utils import logger, safe_div

# ==============================================================================
# 常量与内部工具
# ==============================================================================
# 这些关键词代表"非游资"的特殊席位，识别出来后单独归类，不和游资混在一起
SPECIAL_SEATS: Dict[str, str] = {
    "机构专用": "机构",
    "沪股通专用": "北向资金",
    "深股通专用": "北向资金",
    "股通": "北向资金",
}

# 游资关键词 -> 游资别称，按关键词长度倒序预排序
# 【为什么要按长度倒序】短关键词会吃掉长关键词（如"江苏路" vs "路"），
# 先匹配最长的关键词，命中即停，保证最具体的席位名优先。
_KEYWORD_INDEX: List[Tuple[str, str]] = sorted(
    [
        (kw, name)
        for name, kws in {**YOUZI_MAP, **(YOUZI_BROAD if ENABLE_BROAD_YOUZI else {})}.items()
        for kw in kws
        if len(kw) >= YOUZI_MIN_KEYWORD_LEN
    ],
    key=lambda x: len(x[0]),
    reverse=True,
)


def _as_df(df: Any) -> pd.DataFrame:
    """None / 非 DataFrame 统一成空 DataFrame。"""
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def build_code_name_map(*dfs: pd.DataFrame) -> Dict[str, str]:
    """
    从多个含「代码 / 名称」的 DataFrame 建 代码 -> 名称 映射。

    为什么必须建：活跃营业部表的「买入股票」是名称串，只有映射到名称才能比对。
    传入多个表是为了覆盖率——同一只票可能只在涨停池出现、或只在龙虎榜出现。
    """
    mapping: Dict[str, str] = {}
    for df in dfs:
        d = _as_df(df)
        if d.empty or "代码" not in d.columns or "名称" not in d.columns:
            continue
        for code, name in zip(d["代码"].astype(str), d["名称"].astype(str)):
            code = code.strip().zfill(6)
            if code and name:
                mapping.setdefault(code, name.strip())
    return mapping


def match_youzi_name(seat_name: str) -> Optional[str]:
    """
    营业部名称 -> 游资别称（模糊匹配，最长关键词优先）。

    匹配逻辑：
      "国泰海通证券股份有限公司上海长宁区江苏路证券营业部"
        -> 命中关键词 "江苏路"（长度 3）
        -> 返回 "章盟主"
      "中国国际金融股份有限公司上海分公司"
        -> 命中关键词 "上海分公司"（长度 5，优先于"上海"）
        -> 返回 "赵老哥"

    返回 None 表示未命中任何已知席位。
    """
    if not seat_name:
        return None
    s = str(seat_name)

    # 特殊席位优先判断（机构专用 / 股通），避免被宽泛的游资关键词吃掉
    for kw, label in SPECIAL_SEATS.items():
        if kw in s:
            return label

    # 再按关键词长度倒序匹配游资
    for kw, name in _KEYWORD_INDEX:
        if kw in s:
            return name
    return None


def _split_stocks(text: Any) -> Set[str]:
    """
    把「买入股票」字段切成名称集合。

    实测格式：'红太阳 超声电子 润贝航科 东方新能 …'（空格分隔）
    返回 set 便于 O(1) 精确比对，避免"东方"误匹配"东方通信"这类子串问题。
    """
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return set()
    s = str(text).replace("，", " ").replace(",", " ").replace("、", " ")
    return {t.strip() for t in s.split() if t.strip()}


def _dedup_by_yyb_code(df: pd.DataFrame) -> pd.DataFrame:
    """
    营业部去重（修缺陷 2）。

    实测同一营业部因名称写法不同会出现多行，例如：
        国泰海通证券股份有限公司上海长宁区江苏路证券营业部  (代码 10025080)
        国泰海通证券股份有限公司长宁区江苏路证券营业部      (代码 10025080)
        国泰海通证券股份有限公司上海江苏路营业部            (代码 10025080)
    三行数据完全相同（净额都是 4.04 亿），相加会变 12 亿。

    去重键：优先「营业部代码」，无代码时退回「营业部名称」。

    【关键：不能简单 drop_duplicates，否则会丢股票名单】
    实测 2026-11-09 的代码 10025080（江苏路）出现 3 行，净额都是 4.04 亿，
    但「买入股票」字段被**分摊到三行**里：
        第 1 行：红太阳 超声电子 润贝航科 东方新能 新亚制程 雪人集团 …
        第 2 行：金健米业 阳光照明 东方创业 大东方 文投控股 龙江交通 …
        第 3 行：艾能聚
    简单保留第一行会丢掉后两行的股票名单，导致"该席位买了什么"严重不全。
    所以这里按代码分组后**合并买入股票**，净额取第一行（三行数值相同）。
    """
    if df.empty:
        return df

    d = df.copy()
    key = "营业部代码" if "营业部代码" in d.columns else None

    if key is None:
        # 没有代码列时按名称去重（退而求其次，无法处理"同一营业部不同写法"）
        return d.drop_duplicates(subset=["营业部名称"], keep="first").reset_index(drop=True)

    # 净额可能为 NaN，转成数值后按绝对值排序，保证保留的那行数据最完整
    d["_net_abs"] = pd.to_numeric(d.get("总买卖净额"), errors="coerce").abs().fillna(0.0)
    d = d.sort_values("_net_abs", ascending=False)

    before = len(d)
    if "买入股票" in d.columns:
        def _merge_stocks(s: pd.Series) -> str:
            """把同一营业部多行的股票名单合并去重（保持出现顺序）。"""
            tokens: List[str] = []
            for x in s:
                for w in str(x).split():
                    if w and w.lower() != "nan" and w not in tokens:
                        tokens.append(w)
            return " ".join(tokens)

        agg_map = {
            c: ("first" if c not in (key, "买入股票") else _merge_stocks)
            for c in d.columns
            if c not in (key, "_net_abs")
        }
        agg_map["买入股票"] = _merge_stocks
        d = d.groupby(key, as_index=False, sort=False).agg(agg_map)
    else:
        d = d.drop_duplicates(subset=[key], keep="first")

    d = d.drop(columns=["_net_abs"], errors="ignore")
    removed = before - len(d)
    if removed > 0:
        logger.info("营业部去重：%d -> %d 行（合并 %d 行重复席位，股票名单已合并）",
                    before, len(d), removed)
    return d.reset_index(drop=True)


def _pick_seat_side(seat_df: pd.DataFrame, prefer_single_day: bool = LHB_SEAT_PREFER_SINGLE_DAY) -> pd.DataFrame:
    """
    席位表口径选择（修缺陷：同一营业部在不同「上榜原因」下重复出现）。

    实测 000525 买入榜返回 10 行，原因：
        5 行属于「日涨幅偏离值达到7%的前5只证券」（单日口径）
        5 行属于「连续三个交易日内，涨幅偏离值累计达到20%的证券」（三日累计口径）
    两组的金额区间**重叠**，直接 sum 会重复计算近一倍。

    策略：按「类型」分组，只保留一组
        prefer_single_day=True  -> 选类型不含"连续"的那组（贴近"今日"动作）
        prefer_single_day=False -> 选类型含"连续"的那组（覆盖更全，适合看锁仓）
    若目标口径不存在，则退回行数最多的那组。
    """
    if seat_df.empty or "类型" not in seat_df.columns:
        return seat_df

    groups = list(seat_df.groupby("类型", dropna=False))
    if len(groups) <= 1:
        return seat_df

    def _is_continuous(t: Any) -> bool:
        return "连续" in str(t)

    picked = None
    for gtype, g in groups:
        if prefer_single_day and not _is_continuous(gtype):
            picked = g
            break
        if (not prefer_single_day) and _is_continuous(gtype):
            picked = g
            break

    if picked is None:
        picked = max((g for _, g in groups), key=len)

    logger.debug("席位口径选择：从 %d 组中取「%s」（%d 行）",
                 len(groups), str(picked["类型"].iloc[0])[:24], len(picked))
    return picked


# ==============================================================================
# 第一层：活跃营业部全表扫描
# ==============================================================================
def _layer1_active_yyb(
    active_yyb_df: pd.DataFrame,
    code_name: Dict[str, str],
    target_codes: Optional[Sequence[str]],
    only_buy: bool = False,
) -> List[Dict[str, Any]]:
    """
    第一层：扫活跃营业部表，识别知名席位。

    参数
    ----
    code_name   : 代码 -> 名称 映射（修缺陷 1 的关键）
    target_codes: 目标股票代码列表。None 表示不筛选（全市场扫描）
    only_buy    : True 时只保留净买入的席位

    返回记录列表，每条含：游资 / 营业部 / 营业部代码 / 净买入(元) / 操作股票 / 方向 / 来源
    """
    d = _as_df(active_yyb_df)
    if d.empty or "营业部名称" not in d.columns:
        logger.debug("第一层跳过：活跃营业部表为空或无营业部名称列")
        return []

    d = _dedup_by_yyb_code(d)

    # 【修 ST/北交所泄漏】营业部表的「买入股票」是纯名称串，绕过了 clean_stock_df。
    # 实测泄漏样例：*ST亿晶、ST信通、萃华退、森峰激光(920085)、宇特光电(920157)。
    # 修法：用 code_name（来自已过滤的涨停池/龙虎榜）构建"合法股票池"，
    #       只保留出现在池内的名称 —— 因为涨停池与龙虎榜都已剔除 ST/退市/北交所，
    #       不在池内的名称天然被排除。
    valid_names: Set[str] = set(code_name.values())

    # 双保险：即使映射表混入了脏数据，也按关键词再挡一次
    def _is_valid(name: str) -> bool:
        n = str(name).upper()
        if any(kw in n for kw in ("ST", "退", "PT")):
            return False
        # 代码维度兜底：非 6/0/3 开头 = 转债/基金/北交所，全部排除
        code = next((c for c, nm in code_name.items() if nm == name), "")
        if code and not code.startswith(("6", "0", "3")):
            return False
        return True

    # 目标股票名称集合（target_codes 为空 -> None 表示不过滤）
    target_names: Optional[Set[str]] = None
    if target_codes:
        target_names = {code_name.get(str(c).zfill(6), "") for c in target_codes}
        target_names = {n for n in target_names if n}
        if not target_names:
            logger.warning("第一层：目标代码无法映射到名称，本层将无法匹配（检查是否传入了错误代码）")
            return []

    records: List[Dict[str, Any]] = []
    dropped_invalid: Set[str] = set()

    for _, row in d.iterrows():
        yyb_name = str(row.get("营业部名称", ""))
        youzi = match_youzi_name(yyb_name)
        if youzi is None:
            continue

        net = pd.to_numeric(pd.Series([row.get("总买卖净额")]), errors="coerce").iloc[0]
        net = 0.0 if pd.isna(net) else float(net)

        if only_buy and net <= 0:
            continue

        bought = _split_stocks(row.get("买入股票", ""))

        # 过滤掉不在合法池内 / 命中 ST·退市·北交所 的名称
        if valid_names:
            bought = {n for n in bought if n in valid_names}
        invalid = {n for n in bought if not _is_valid(n)}
        if invalid:
            dropped_invalid |= invalid
            bought -= invalid

        # 【修缺陷 1】用名称做集合交集，而不是把代码往名称串里塞
        if target_names is not None:
            hit_names = bought & target_names
            if not hit_names:
                continue   # 该席位今天没买我们的目标股，跳过
        else:
            hit_names = bought
            if not hit_names:
                continue   # 全是 ST/北交所，过滤后为空，整条丢弃

        # 名称 -> 代码回显（报告里带代码更好核对）
        name_to_code = {v: k for k, v in code_name.items()}
        stock_str = "、".join(
            f"{n}({name_to_code.get(n, '')})" if name_to_code.get(n) else n
            for n in sorted(hit_names)
        )[:120]

        records.append({
            "游资": youzi,
            "营业部": yyb_name,
            "营业部代码": str(row.get("营业部代码", "")),
            "净买入": net,
            "操作股票": stock_str,
            "方向": "净买入" if net > 0 else ("净卖出" if net < 0 else "对倒"),
            "来源": "活跃营业部",
            # 第一层无法定位卖出标的，必须明确标注，别让报告读者以为这是卖出名单
            "备注": "" if net > 0 else "净卖出；该营业部无公开卖出名单，标的未定位",
        })

    if dropped_invalid:
        logger.info("第一层：过滤掉 ST/退市/北交所标的 %d 个（%s）",
                    len(dropped_invalid), "、".join(sorted(dropped_invalid)[:6]))

    logger.info("第一层：命中 %d 条知名席位记录（目标股 %s）",
                len(records), "全部" if target_names is None else len(target_codes or []))
    return records


# ==============================================================================
# 第二层：逐票查买卖前五席位
# ==============================================================================
def make_seat_fetcher(day: str, sleep: float = 0.3) -> Callable[[str, str], pd.DataFrame]:
    """
    构造第二层需要的 seat_fetcher（生产环境用这个）。

    【为什么需要这个工厂函数】
        data_fetch.fetch_lhb_seats 的签名是 (code, day, flag="买入")，
        而第二层约定的签名是 (code, side)。
        直接把它当 seat_fetcher 传进去，side 会被误当成 day 参数：
            fetch_lhb_seats("002578", "买入")  ->  to_day_str("买入") 抛 ValueError
        异常又被逐票 try-except 吞掉 -> "成功 0 只" 却完全不报错。
        这是实测踩到的静默失败，因此统一用工厂函数包一层，杜绝签名错配。

    用法
    ----
        recs = match_youzi(..., seat_fetcher=make_seat_fetcher("20261109"))
    """
    from data_fetch import fetch_lhb_seats   # 延迟导入：保持本模块无网络依赖可单测

    def _fetcher(code: str, side: str) -> pd.DataFrame:
        return fetch_lhb_seats(code, day, flag=side, sleep=sleep)

    return _fetcher


def _layer2_seats(
    target_codes: Sequence[str],
    code_name: Dict[str, str],
    seat_fetcher: Optional[Callable[[str, str], pd.DataFrame]],
    day: str,
    sides: Sequence[str] = ("买入", "卖出"),
    max_codes: int = LHB_SEAT_MAX_CODES,
) -> List[Dict[str, Any]]:
    """
    第二层：对目标股逐票抓买卖前五席位，识别机构与知名席位。

    参数
    ----
    seat_fetcher : 抓取函数，签名 (code, flag) -> DataFrame
                   不传则不执行本层（保证本模块**断网可单测**）
    sides        : 抓哪几个方向，默认买入+卖出

    为什么要把 seat_fetcher 做成参数而不是直接 import data_fetch：
        ① 本模块要求"纯逻辑可单测"，import 网络层就破坏了可测性
        ② 单测时可以注入一个返回固定 DataFrame 的假函数
        ③ 生产环境由 main.py 传入 data_fetch.fetch_lhb_seats
    """
    if seat_fetcher is None:
        logger.info("第二层跳过：未注入 seat_fetcher（离线模式）")
        return []

    codes = [str(c).zfill(6) for c in (target_codes or [])][:max_codes]
    if not codes:
        return []

    records: List[Dict[str, Any]] = []
    ok_cnt = 0
    err_cnt = 0          # 失败计数：用于识别"签名错配"这类静默故障
    first_err = ""

    for code in codes:
        stock_name = code_name.get(code, "")
        got_any = False

        for side in sides:
            # 接口对"该股当日无此方向席位"会抛异常，必须逐次 try
            try:
                seat_df = seat_fetcher(code, side)
                # 防御：返回值必须是 DataFrame，否则说明签名/用法不对
                if seat_df is not None and not isinstance(seat_df, pd.DataFrame):
                    raise TypeError(
                        f"seat_fetcher 返回了 {type(seat_df).__name__}，"
                        f"应返回 DataFrame（void 检查函数签名是否为 (code, side)）"
                    )
            except Exception as exc:  # 兜底：绝不让单票异常中断整轮
                err_cnt += 1
                if not first_err:
                    first_err = f"{type(exc).__name__}: {exc}"
                logger.debug("[席位 %s %s] 抓取异常：%s", code, side, exc)
                continue

            seat_df = _as_df(seat_df)
            if seat_df.empty:
                continue

            got_any = True
            seat_df = _pick_seat_side(seat_df)

            for _, r in seat_df.iterrows():
                seat_name = str(r.get("交易营业部名称", ""))
                youzi = match_youzi_name(seat_name)
                if youzi is None:
                    continue

                buy_amt = pd.to_numeric(pd.Series([r.get("买入金额")]), errors="coerce").iloc[0]
                sell_amt = pd.to_numeric(pd.Series([r.get("卖出金额")]), errors="coerce").iloc[0]
                net = pd.to_numeric(pd.Series([r.get("净额")]), errors="coerce").iloc[0]

                buy_amt = 0.0 if pd.isna(buy_amt) else float(buy_amt)
                sell_amt = 0.0 if pd.isna(sell_amt) else float(sell_amt)
                net = (buy_amt - sell_amt) if pd.isna(net) else float(net)

                records.append({
                    "游资": youzi,
                    "营业部": seat_name,
                    "营业部代码": "",
                    "净买入": net,
                    "操作股票": f"{stock_name}({code})" if stock_name else code,
                    "方向": "净买入" if net > 0 else ("净卖出" if net < 0 else "对倒"),
                    "来源": f"席位-{side}",
                    "备注": f"买入 {buy_amt / 1e4:.0f}万 / 卖出 {sell_amt / 1e4:.0f}万",
                })

        if got_any:
            ok_cnt += 1

    logger.info("第二层：扫描 %d 只票，成功获取 %d 只，命中 %d 条席位记录",
                len(codes), ok_cnt, len(records))

    # 【静默失败告警】全部失败时大概率不是"真的没数据"，而是用法错误
    if ok_cnt == 0 and err_cnt > 0:
        logger.warning(
            "第二层全部失败（%d 次异常），极可能是 seat_fetcher 签名不匹配。"
            "请使用 lhb.make_seat_fetcher(day) 构造，首个异常：%s",
            err_cnt, first_err or "-",
        )
    return records


# ==============================================================================
# 主入口：双层降级匹配
# ==============================================================================
def try_ths_youzi(
    day: str,
    target_codes: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """
    同花顺游资榜通道（第 0 层，优先级最高）。

    返回 list[dict]，格式与东财两层完全一致，可直接交给 to_dataframe()。
    返回空列表表示「不可用」，调用方应回退到东财双轨。

    为什么它比 YOUZI_MAP 更可靠
    ---------------------------
    东财路径是：拿到「国泰海通证券股份有限公司上海长宁区江苏路证券营业部」，
    再用民间流传的映射表猜"这是章盟主"。营业部一迁址、一合并，映射就失效，
    而且猜错了不会报错。
    同花顺直接返回官方游资名（hot_money_items[].name），这一步从「猜」变成「读」。

    仍然保留回退的原因
    -----------------
      · 没配 Key 的用户，这条通道整个不存在
      · 同花顺偶尔会缺当日游资榜（返回空数组）
      · 接口变更风险：它的字段是官方定义的，改了我们只能靠回退保命
    """
    if not day:
        return []

    try:
        import ths_fetch  # 延迟导入：没配 Key 就不该付出加载成本

        if not ths_fetch.is_enabled():
            st = ths_fetch.status()
            if not st.get("has_key"):
                logger.debug("同花顺未配置 Key，游资画像走东财双轨")
            return []

        recs = ths_fetch.fetch_lhb_hot_money(day)
        if not recs:
            logger.info("同花顺游资榜 %s 无数据，回退东财双轨", day)
            return []

        # ---- 不做 target_codes 过滤（刻意的设计取舍）----
        # 第一版按 target_codes 筛，结果 2026-11-09 的 14 个游资只剩 8 个：
        # 另外 6 个游资的标的恰好不在盯盘清单里，信息被静默砍掉。
        #
        # 不筛更好的理由：
        #   · 同花顺游资榜本身就是「全市场」榜单，不是某只票的席位明细
        #   · 每条记录自带「操作股票」，调用方想筛随时能筛
        #   · 热力图/桑基图恰恰需要尽可能多的 游资×股票 对，筛了就画不出全景
        # 所以这里返回全量，把「要不要聚焦」的决定权交给调用方。
        logger.info("游资：使用同花顺官方游资榜（%d 条，%d 个游资，全市场）",
                    len(recs), len({r.get("游资") for r in recs}))
        return recs

    except Exception as exc:
        # 任何异常都不阻断：游资画像本来就是"锦上添花"，不能因此让报告出不来
        logger.warning("同花顺游资通道异常，回退东财双轨：%s: %s", type(exc).__name__, exc)
        return []


def match_youzi(
    active_yyb_df: Optional[pd.DataFrame] = None,
    lhb_detail_df: Optional[pd.DataFrame] = None,
    target_codes: Optional[Sequence[str]] = None,
    zt_df: Optional[pd.DataFrame] = None,
    seat_fetcher: Optional[Callable[[str, str], pd.DataFrame]] = None,
    day: str = "",
    enable_layer2: bool = True,
    max_codes: int = LHB_SEAT_MAX_CODES,
    prefer_ths: bool = True,
) -> List[Dict[str, Any]]:
    """
    游资画像主入口（双层降级）。

    参数
    ----
    active_yyb_df : 活跃营业部表（第一层数据源）
    lhb_detail_df : 龙虎榜明细（用于补全 代码->名称 映射，以及上榜家数）
    target_codes  : 目标股票代码列表。None/空 = 全市场扫描（不限股票）
    zt_df         : 涨停池（用于补全 代码->名称 映射，覆盖率更全）
    seat_fetcher  : 第二层的抓取函数，**签名必须是 (code, side) -> DataFrame**。
                    推荐用 `make_seat_fetcher(day)` 构造（内部已适配 data_fetch 的参数顺序）。
                    不传 = 只跑第一层（离线可测）
    day           : 交易日 YYYYMMDD（写进日志）
    enable_layer2 : 是否启用第二层
    max_codes     : 第二层最多扫几只票
    prefer_ths    : 是否优先使用同花顺官方游资榜（默认 True）。
                    关掉它 = 强制走原来的东财双轨，用于对比两种口径的结果差异。

    返回
    ----
    list[dict]，每条是一次"游资动作"。用 to_dataframe() 转成表。
    同一条记录可能同时来自第一层和第二层（去重由 to_dataframe 负责）。

    【降级逻辑（四层）】
      0. 同花顺官方游资榜  有 Key 且当日有数据 -> 直接返回（最准）
      1. 东财活跃营业部表  全市场覆盖，金额是营业部合计口径
      2. 东财个股席位      能定位"在哪只票上买卖"，但逐票请求
      3. 全挂 -> 返回空列表，调用方渲染"数据缺失"，不让程序崩
      另外：seat_fetcher 为 None（离线）时只跑第一层。
    """
    # ---------- 第 0 层：同花顺官方游资榜 ----------
    # 命中就直接返回，不再跑东财两层。
    # 为什么不混合两种口径：它们的金额含义不同（同花顺=游资聚合净买入，
    # 东财第一层=营业部全票净额，第二层=单票净额），混在一起相加就是重复计算。
    if prefer_ths:
        ths_records = try_ths_youzi(day, target_codes)
        if ths_records:
            return ths_records

    code_name = build_code_name_map(zt_df, lhb_detail_df, active_yyb_df)
    logger.info("代码-名称映射：%d 条（来自涨停池/龙虎榜）", len(code_name))

    records: List[Dict[str, Any]] = []

    # ---------- 第一层 ----------
    records += _layer1_active_yyb(active_yyb_df, code_name, target_codes)

    # ---------- 第二层 ----------
    if enable_layer2 and seat_fetcher is not None:
        # 只在有明确目标时才逐票抓；全市场扫描（target_codes 为空）会请求几十次，
        # 盘后跑一次尚可接受，但不作为默认行为
        codes_for_seats = list(target_codes or [])
        if not codes_for_seats and lhb_detail_df is not None:
            d = _as_df(lhb_detail_df)
            if not d.empty and "代码" in d.columns:
                codes_for_seats = d["代码"].astype(str).str.zfill(6).drop_duplicates().tolist()
                logger.info("第二层：未指定目标股，自动取龙虎榜全部 %d 只", len(codes_for_seats))

        records += _layer2_seats(
            target_codes=codes_for_seats,
            code_name=code_name,
            seat_fetcher=seat_fetcher,
            day=day,
            max_codes=max_codes,
        )

    if not records:
        logger.warning("游资匹配：两层均无命中（数据源可能缺失，或今日无知名席位上榜）")

    return records


def to_dataframe(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    list[dict] -> DataFrame，附带「口径」标记，并在同口径内去重。

    【口径为什么必须显式标注，而不是混在一张表里】
      第一层（活跃营业部）：净额 = 该席位当日**全部股票**合计 —— 是"这家店今天总共买了多少"
      第二层（个股席位）  ：净额 = 该席位在**某一只股票**上的买卖净额 —— 是"这只票他买了多少"

      实测踩到的坑：同一个江苏路席位，第一层显示 +4.04 亿（全天所有票合计），
      第二层显示东方新能 +5973 万（单票）。如果混在一张按金额排序的表里，
      读者会看到 4 亿和 6000 万两个数，以为系统算错了。

      正确做法：分开标注、分开展示。两个数都对，只是粒度不同。

    去重只在**同口径内**做：同一 (游资, 营业部, 操作股票, 口径) 只留一条。
    """
    cols = ["游资", "营业部", "净买入(万)", "方向", "口径", "操作股票", "来源", "备注"]
    if not records:
        return pd.DataFrame(columns=cols)

    d = pd.DataFrame(records)
    d["净买入(万)"] = (pd.to_numeric(d["净买入"], errors="coerce").fillna(0.0) / 1e4).round(1)

    # 口径标记：来源以"席位"开头的是个股口径，其余是营业部合计口径
    d["口径"] = d["来源"].astype(str).str.startswith(("席位", "同花顺游资榜")).map(
        {True: "个股席位", False: "营业部合计"}
    )

    # ------------------------------------------------------------------
    # 去重：两个口径用**不同的键**，这是踩过坑的地方
    #
    # 【坑】营业部合计口径不能用「操作股票」做 key 的一部分。
    #   main.py 会跑两遍 match_youzi：一遍全市场（target_codes=None），
    #   一遍只盯盘清单（target_codes=5 只票）。第一层记录里的「操作股票」
    #   是按 target_names 过滤后的结果，两遍长度不同：
    #       全市场版：  "红太阳(000525)、超声电子(000823)、…"（120字截断）
    #       盯盘版  ：  "东方新能(002310)、东方嘉盛(002889)"
    #   key 不同 -> 没去重 -> 同一营业部 4.04 亿被算两次 -> 聚合出 8.07 亿。
    #
    # 【正确做法】
    #   营业部合计：一个营业部一天只有一条合计记录 -> 按 (游资, 营业部) 去重
    #   个股席位  ：一个营业部在一只票上有一条   -> 按 (游资, 营业部, 股票) 去重
    # 排序后 keep="first" 会优先保留"全市场版"（股票名单更全）。
    # ------------------------------------------------------------------
    seat = d[d["口径"] == "个股席位"].copy()
    yyb = d[d["口径"] == "营业部合计"].copy()

    if not seat.empty:
        seat = seat.drop_duplicates(
            subset=["游资", "营业部", "操作股票"], keep="first"
        )
    if not yyb.empty:
        yyb = yyb.drop_duplicates(subset=["游资", "营业部"], keep="first")

    d = pd.concat([seat, yyb], ignore_index=True) if (not seat.empty or not yyb.empty) else d

    # 展示排序：金额绝对值大的在前（大买大卖都值得看）
    d["_abs"] = d["净买入(万)"].abs()
    d = d.sort_values("_abs", ascending=False).reset_index(drop=True)

    return d[cols]


def summarize_by_youzi(records_df: pd.DataFrame) -> pd.DataFrame:
    """
    按游资别称聚合，**同时输出两个口径的净额**，不做二选一。

    【为什么两个都给，而不是只留一个】
      第一层（营业部合计）：反映"这家席位今天整体在做多还是做空" —— 适合判断态度
      第二层（个股席位）  ：反映"他在某只票上具体下了多少" —— 适合判断力度

      只给一个都会丢信息：
        · 只给营业部合计 -> 章盟主买 4 亿，但不知道买的是哪只
        · 只给个股席位   -> 只看盯盘清单里的 5 只票，会漏掉他在清单外的动作

      两个数并排标注口径，读者一眼能看出"整体 4 亿、其中这只票 5973 万"。

    【绝对不能相加】两个口径范围重叠（营业部合计已包含个股席位那笔），
    相加就是重复计算。实测章盟主会虚高到 5.16 亿。

    返回列：游资 席位净额(万) 营业部净额(万) 方向 涉及股票 席位记录数 营业部数
    """
    cols = ["游资", "席位净额(万)", "营业部净额(万)", "方向", "涉及股票", "席位记录数", "营业部数"]
    if records_df is None or records_df.empty:
        return pd.DataFrame(columns=cols)

    d = records_df.copy()
    d["净买入(万)"] = pd.to_numeric(d["净买入(万)"], errors="coerce").fillna(0.0)
    # 兼容没有「口径」列的旧数据（按来源推断）
    if "口径" not in d.columns:
        d["口径"] = d["来源"].astype(str).str.startswith(("席位", "同花顺游资榜")).map(
            {True: "个股席位", False: "营业部合计"}
        )

    rows: List[Dict[str, Any]] = []
    for youzi, g in d.groupby("游资"):
        seat = g[g["口径"] == "个股席位"]
        yyb = g[g["口径"] == "营业部合计"]

        seat_net = float(seat["净买入(万)"].sum()) if not seat.empty else None
        yyb_net = float(yyb["净买入(万)"].sum()) if not yyb.empty else None

        # 方向判断：优先用个股席位口径（更贴近"他在这几只票上"的真实动作）
        basis = seat_net if seat_net is not None else (yyb_net or 0.0)

        # 提取股票名（去掉括号里的代码，避免聚合后太长）
        stocks: List[str] = []
        for s in g["操作股票"].astype(str):
            for part in s.replace("、", " ").split():
                name = part.split("(")[0]
                if name and name not in stocks:
                    stocks.append(name)

        rows.append({
            "游资": youzi,
            "席位净额(万)": None if seat_net is None else round(seat_net, 1),
            "营业部净额(万)": None if yyb_net is None else round(yyb_net, 1),
            "方向": "净买入" if basis > 0 else ("净卖出" if basis < 0 else "对倒"),
            "涉及股票": "、".join(stocks[:YOUZI_MAX_STOCKS])
                        + ("…" if len(stocks) > YOUZI_MAX_STOCKS else ""),
            "席位记录数": len(seat),
            "营业部数": yyb["营业部"].nunique() if not yyb.empty else 0,
        })

    out = pd.DataFrame(rows)
    # 排序用两个口径里绝对值更大的那个（大买大卖都排前面）
    out["_sort"] = out[["席位净额(万)", "营业部净额(万)"]].abs().max(axis=1).fillna(0.0)
    return out.sort_values("_sort", ascending=False).drop(columns=["_sort"]).reset_index(drop=True)[cols]


# ==============================================================================
# 龙虎榜净额排名
# ==============================================================================
def fetch_lhb_net_rank(lhb_df: Optional[pd.DataFrame], top_n: int = 10) -> pd.DataFrame:
    """
    龙虎榜个股净买/净卖排名。

    【必须去重】同一只票会因多个「上榜原因」出现多行，但「龙虎榜净买额」在
    这些行里是**同一个值**（整只票的净额），直接 sum 会翻倍。
    所以按代码去重，保留第一行即可（值相同，取哪行都一样）。
    """
    cols = ["代码", "名称", "涨跌幅", "龙虎榜净买额(万)", "上榜原因"]
    d = _as_df(lhb_df)
    if d.empty or "龙虎榜净买额" not in d.columns:
        return pd.DataFrame(columns=cols)

    d = d.copy()
    d["龙虎榜净买额"] = pd.to_numeric(d["龙虎榜净买额"], errors="coerce").fillna(0.0)
    d = d.drop_duplicates(subset=["代码"], keep="first")

    d["龙虎榜净买额(万)"] = (d["龙虎榜净买额"] / 1e4).round(0)
    d["_abs"] = d["龙虎榜净买额(万)"].abs()
    d = d.sort_values("_abs", ascending=False).head(top_n)

    out = d.copy()
    if "涨跌幅" in out.columns:
        out["涨跌幅"] = pd.to_numeric(out["涨跌幅"], errors="coerce").round(2)
    if "上榜原因" in out.columns:
        out["上榜原因"] = out["上榜原因"].astype(str).str[:26]

    return out[cols].reset_index(drop=True)


def institutional_flow(lhb_df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """
    从龙虎榜「解读」字段统计机构参与度。

    实测「解读」字段样例：
        '1家机构买入，成功率37.39%'
        '4家机构卖出，成功率20.00%'
        '5家机构买入，成功率37.81%'
        '主力做T，成功率23.14%'
        '买一主买，成功率33.03%'

    注意：这是东财对**席位构成**的文本概括，不是精确的机构成交金额。
         只能用作"机构参与度高不高"的定性判断。
    """
    d = _as_df(lhb_df)
    result = {
        "total": 0,
        "inst_buy_cnt": 0, "inst_sell_cnt": 0,
        "inst_buy_stocks": [], "inst_sell_stocks": [],
        "note": "",
    }
    if d.empty or "解读" not in d.columns:
        result["note"] = "无龙虎榜数据或无解读字段"
        return result

    # 先去重再统计总数，否则 total 会等于"行数"（同一只票多个上榜原因重复计数）
    d = d.drop_duplicates(subset=["代码"], keep="first")
    result["total"] = len(d)
    buy_stocks: List[str] = []
    sell_stocks: List[str] = []

    for _, r in d.iterrows():
        text = str(r.get("解读", ""))
        name = str(r.get("名称", ""))
        if "机构" in text and "买" in text and "卖" not in text.split("机构")[0][-6:]:
            if "机构卖出" in text or "家机构卖" in text:
                sell_stocks.append(name)
            else:
                buy_stocks.append(name)
        elif "机构卖出" in text or "家机构卖" in text:
            sell_stocks.append(name)

    result.update({
        "inst_buy_cnt": len(buy_stocks),
        "inst_sell_cnt": len(sell_stocks),
        "inst_buy_stocks": buy_stocks[:10],
        "inst_sell_stocks": sell_stocks[:10],
        "note": "依据「解读」文本统计，为定性参考，非机构成交精确金额",
    })
    return result


# ==============================================================================
# 离线单测入口：python lhb.py 20261109 [目标代码1 目标代码2 ...]
# ==============================================================================
if __name__ == "__main__":
    import sys
    from pathlib import Path

    from config import CACHE_DIR
    from utils import setup_logger, clean_stock_df

    setup_logger()
    day = sys.argv[1] if len(sys.argv) > 1 else "20261109"
    targets = sys.argv[2:] if len(sys.argv) > 2 else None

    def _load(key: str, code_col: bool = True) -> pd.DataFrame:
        p: Path = CACHE_DIR / f"{day}_{key}.csv"
        if not p.exists():
            print(f"[跳过] 缓存不存在：{p.name}")
            return pd.DataFrame()
        return pd.read_csv(p, dtype={"代码": str} if code_col else None, encoding="utf-8-sig")

    yyb = _load("active_yyb", code_col=False)
    lhb_df = _load("lhb")
    zt = _load("zt_pool")

    # 离线数据是原始快照，先清洗到与线上一致的口径（滤掉 ST/北交所/可转债）
    if not lhb_df.empty:
        lhb_df = clean_stock_df(lhb_df, dedup=False)
    if not zt.empty:
        zt = clean_stock_df(zt)

    # 离线模式：不传 seat_fetcher，只验第一层
    recs = match_youzi(
        active_yyb_df=yyb,
        lhb_detail_df=lhb_df,
        target_codes=targets,
        zt_df=zt,
        seat_fetcher=None,      # 离线：第二层关闭
        day=day,
    )

    print("\n" + "=" * 78)
    print(f"  {day} 龙虎榜游资画像（离线第一层）")
    print(f"  目标股票：{targets if targets else '全市场（不筛选）'}")
    print("=" * 78)

    df = to_dataframe(recs)
    if df.empty:
        print("  今日无命中知名游资，或活跃营业部数据缺失。")
    else:
        agg = summarize_by_youzi(df)
        print("\n--- 按游资聚合 ---")
        print(agg.to_string(index=False))
        print("\n--- 明细 ---")
        show = df.copy()
        show["营业部"] = show["营业部"].astype(str).str[:34]
        show["操作股票"] = show["操作股票"].astype(str).str[:30]
        print(show.to_string(index=False))

    print("\n--- 龙虎榜净额 Top10 ---")
    print(fetch_lhb_net_rank(lhb_df, 10).to_string(index=False))

    print("\n--- 机构参与度 ---")
    print(institutional_flow(lhb_df))
    print()
