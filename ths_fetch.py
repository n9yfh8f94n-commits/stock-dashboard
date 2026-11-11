#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ths_fetch.py —— 同花顺扶摇 API 接入层（第二数据源）
================================================================================

定位
----
**东财为主，同花顺兜底。** 本模块不改变任何现有逻辑，只提供「另一条取数通道」：
    ① 东财某个池子返回空 / 重试 4 次全失败  → 降级到同花顺
    ② 需要超过 20 个交易日的历史           → 用同花顺的 30 天连板天梯补

对外承诺
--------
本模块所有 fetch_* 函数返回的 DataFrame，**列名、列顺序、数据类型与东财版本完全一致**，
下游 analyzer.py / lhb.py / dashboard.py 不需要知道数据来自哪家。
差异（比如同花顺没有「换手率」）用 NaN 占位，绝不丢列——丢列会让下游 KeyError。

字段来源（重要）
----------------
下列端点与字段名全部核对自官方文档 https://fuyao.aicubes.cn 的 llms-full.txt
（2026-11-11 抓取），**不是推测**：

    GET /api/a-share/special-data/limit-up-pool          涨停池
    GET /api/a-share/special-data/limit-down-pool       跌停池
    GET /api/a-share/special-data/limit-break-pool      炸板池
    GET /api/a-share/special-data/limit-up-ladder       连板天梯（近 30 交易日）
    GET /api/a-share/special-data/limit-up-statistics   封板率/涨停家数/连板分布
    GET /api/a-share/special-data/dragon-tiger-list     龙虎榜（all / org / hot_money）

认证：请求头 `X-api-key`。Key 从环境变量 FUYAO_API_KEY 或 local_secret.py 读取，
**绝不硬编码**。没有 Key 时本模块整体禁用，系统退回纯东财模式。

关于「同花顺涨停池没有行业字段」
--------------------------------
官方文档明确：涨停池「返回字段聚焦涨停语义，不含资金流 / 行业 / 分时预览等通用字段」。
它给的是 `limit_up_reason`（涨停原因，如「存储芯片」）。
config.THS_USE_REASON_AS_SECTOR = True 时，用它顶替「所属行业」——
对超短线来说「涨停原因」比申万行业更贴近主线，但这**改变了主线统计口径**，
所以做成开关，并在 DataFrame.attrs 里留痕。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

import config
from utils import clean_stock_df, logger, retry_request, to_numeric

# ==============================================================================
# 异常类型
# ==============================================================================
class ThsError(Exception):
    """同花顺通道的所有错误的基类。"""


class ThsNotConfigured(ThsError):
    """没配 API Key，或开关被关掉了。这是「预期内的不可用」，不是故障。"""


class ThsAuthError(ThsError):
    """Key 无效 / 无权限（官方 code=2001 / 2003）。重试没有意义。"""


class ThsRateLimit(ThsError):
    """触发限流（HTTP 429 或官方 code=4001）。"""


class ThsApiError(ThsError):
    """业务错误（参数非法、非交易日等）。"""


# ==============================================================================
# 一、API Key 管理
# ==============================================================================
_KEY_CACHE: Dict[str, Optional[str]] = {}

# 全局熔断：一旦确认 Key 无效/无权限，后续所有请求直接失败，不再重试。
# 没有这层保护的话，30 天历史回填会做 30 次注定失败的请求，
# 每次都走完 1s+2s 的退避，白等一分半钟。
_DISABLED_REASON: Optional[str] = None


def get_api_key() -> Optional[str]:
    """
    读取 API Key，优先级：
        1) 环境变量 FUYAO_API_KEY        ← 推荐（setx 设置后重启终端生效）
        2) 项目根目录 local_secret.py    ← 次选（必须已加入 .gitignore）
        3) 都没有 → None（本模块自动禁用）

    结果缓存，避免每次请求都走一遍文件 IO。
    """
    if "key" in _KEY_CACHE:
        return _KEY_CACHE["key"]

    key: Optional[str] = None

    # ---- 1) 环境变量 ----
    key = os.environ.get(config.THS_API_KEY_ENV, "").strip() or None

    # ---- 2) local_secret.py（懒导入，文件不存在时什么都不做）----
    if not key:
        try:
            import importlib

            mod = importlib.import_module("local_secret")
            candidate = str(getattr(mod, "FUYAO_API_KEY", "") or "").strip()
            # 排除占位符，避免把 "<your-api-key>" 当成真 Key 去请求
            if candidate and not candidate.startswith("<") and len(candidate) >= 8:
                key = candidate
                logger.debug("同花顺 API Key 来源：local_secret.py")
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("读取 local_secret.py 失败（忽略）：%s", exc)

    if key:
        logger.debug("同花顺 API Key 来源：环境变量 %s", config.THS_API_KEY_ENV)

    _KEY_CACHE["key"] = key
    return key


def is_enabled() -> bool:
    """
    同花顺通道当前是否可用：开关打开 + 有 Key。

    设计成「不可用就静默返回 False」，调用方据此决定要不要降级，
    而不是抛异常——大部分时候用户根本没配 Key，这属于正常状态。
    """
    if _DISABLED_REASON:
        return False
    if not config.THS_ENABLE_FALLBACK:
        return False
    return bool(get_api_key())


def status() -> Dict[str, Any]:
    """给 --check 用的自检信息。"""
    key = get_api_key()
    return {
        "enabled": config.THS_ENABLE_FALLBACK,
        "has_key": bool(key),
        "key_hint": (key[:4] + "…" + key[-4:]) if key and len(key) > 10 else ("已配置" if key else "未配置"),
        "base_url": config.THS_BASE_URL,
        "usable": is_enabled(),
    }


# ==============================================================================
# 二、日期工具
# ==============================================================================
# 【为什么不用 zoneinfo】Windows 上 zoneinfo 依赖 tzdata 包，不一定装了。
# 用固定 UTC+8 偏移最稳：A股就是这个时区，也没有夏令时。
_TZ_CN = timezone(timedelta(hours=8))


def to_day_str(day: date | str) -> str:
    """统一成 'YYYYMMDD' 字符串。"""
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    s = str(day).strip().replace("-", "").replace("/", "").replace(".", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期格式无法识别：{day!r}（应为 YYYYMMDD 或 date 对象）")
    return s


def to_iso_date(day: date | str) -> str:
    """'YYYYMMDD' -> 'YYYY-MM-DD'（dragon-tiger-list / limit-up-statistics 要这个格式）。"""
    s = to_day_str(day)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def to_date_ms(day: date | str) -> int:
    """
    'YYYYMMDD' -> 当日上海时区零点的毫秒时间戳。

    limit-up-pool / limit-down-pool / limit-break-pool 用的是 `date_ms` 参数，
    官方定义为「查询交易日 Unix 毫秒戳（Asia/Shanghai 00:00:00）」。
    """
    s = to_day_str(day)
    dt = datetime(int(s[:4]), int(s[4:6]), int(s[6:]), 0, 0, 0, tzinfo=_TZ_CN)
    return int(dt.timestamp() * 1000)


# ==============================================================================
# 三、HTTP 底层
# ==============================================================================
# times=2 而不是 3：Key 无效/权限不足这类错误重试没有意义，
# 但装饰器无法区分异常类型，只能靠减少次数把白等压到最低（约 1 秒）。
# 网络抖动/限流这类可恢复错误，2 次也够用（第一次失败后等 1s 再试）。
@retry_request(times=2, base_delay=1.0, allow_empty=True, label="同花顺")
def _request_raw(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    发一个 GET 请求并返回 data 字段。

    【为什么要 allow_empty=True】
    这个装饰器默认把「空 DataFrame」当失败。本函数返回的是 dict，
    装饰器对非 DataFrame 直接放行，所以 allow_empty 在这里不起作用——
    真正的失败判定在函数内部：业务 code 非 0 一律抛异常，让装饰器去重试。

    绝不把错误吞成空 dict：那会让「Key 失效」和「今天真的没有涨停」
    变成同一种表现，是最难排查的一类问题。
    """
    global _DISABLED_REASON

    if _DISABLED_REASON:
        raise ThsNotConfigured(_DISABLED_REASON)

    key = get_api_key()
    if not key:
        # 用不可重试的方式表达：装饰器会重试 3 次，但每次都立刻失败，代价很小
        raise ThsNotConfigured("未配置 FUYAO_API_KEY，同花顺通道不可用")

    url = f"{config.THS_BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

    req = urllib.request.Request(url, headers={
        "X-api-key": key,
        "User-Agent": "a-share-review/1.0",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=config.THS_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 429 是官方文档明确提到的限流信号
        if exc.code == 429:
            raise ThsRateLimit(f"HTTP 429 限流（{path}）") from exc
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise ThsApiError(f"HTTP {exc.code}（{path}）：{body}") from exc
    except urllib.error.URLError as exc:
        raise ThsApiError(f"网络不可达（{path}）：{exc.reason}") from exc
    except Exception as exc:
        raise ThsApiError(f"请求异常（{path}）：{type(exc).__name__}: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        raise ThsApiError(f"响应不是合法 JSON（{path}）：{raw[:120]!r}") from exc

    code = payload.get("code")
    msg = payload.get("message") or payload.get("msg") or ""

    if code == config.THS_CODE_OK:
        data = payload.get("data")
        if data is None:
            raise ThsApiError(f"code=0 但 data 为空（{path}）")
        return data if isinstance(data, dict) else {"_raw": data}

    if code in config.THS_CODE_KEY_INVALID:
        # Key 问题重试无用：直接熔断，后续请求不再尝试
        _DISABLED_REASON = f"API Key 无效或无权限（code={code}，{msg}）"
        raise ThsAuthError(_DISABLED_REASON)   # 由 _request 统一转成 warning 日志
    if code == config.THS_CODE_RATE_LIMIT:
        raise ThsRateLimit(f"触发限流（code={code}，{msg}）")

    raise ThsApiError(f"业务错误 code={code}：{msg}（{path}）")


def _request(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    对外统一入口：调被重试装饰的内层函数，并把「失败返回值」翻译成异常。

    【为什么需要这一层】
    utils.retry_request 的设计是「彻底失败时返回空 DataFrame + attrs['error']，
    绝不抛异常」——这对行情抓取是对的（不能让一个池子挂掉整个报告），
    但对我们这个返回 dict 的函数就是个陷阱：
        内层失败 -> 返回 DataFrame -> 外层 data.get(...) -> AttributeError
    而且 AttributeError 的报错信息完全看不出真实原因（比如 Key 无效）。

    所以这里显式判类型，把 attrs['error'] 里的原因提出来重抛。
    """
    global _DISABLED_REASON

    if _DISABLED_REASON:
        raise ThsNotConfigured(_DISABLED_REASON)

    result = _request_raw(path, params)

    if isinstance(result, dict):
        return result

    # 走到这里说明装饰器兜底了：提取真实失败原因
    if isinstance(result, pd.DataFrame):
        reason = str(result.attrs.get("error") or "多次重试后仍失败")
    else:
        reason = f"返回了意外类型 {type(result).__name__}"

    # 把 Key 类错误识别出来，触发全局熔断，避免后续无意义的重复等待
    low = reason.lower()
    if "2001" in reason or "2003" in reason or "api key" in low:
        _DISABLED_REASON = f"同花顺 Key 无效或无权限：{reason}"
        # 用 WARNING 而不是 ERROR：这是配置问题，不是系统故障。
        # 下游已经回退到东财，报告照常产出，没必要吓人。
        logger.warning("同花顺 Key 无效或无权限，已回退东财（不影响报告）：%s", reason)
        logger.warning("  到 https://fuyao.aicubes.cn/admin 检查或重新签发 Key")
        raise ThsAuthError(_DISABLED_REASON)

    raise ThsApiError(f"同花顺请求失败（{path}）：{reason}")


def _paginate(path: str, params: Dict[str, Any], item_key: str = "item") -> List[Dict[str, Any]]:
    """
    翻页取全量。

    官方：size 取值 1..200，默认 50；涨停池在极端行情能超 200 家。
    只取第一页会静默丢数据（图看着正常，就是少了几十只票），所以必须翻到底。

    翻页之间主动 sleep，避免触发 code=4001 限流。
    """
    out: List[Dict[str, Any]] = []
    seen_pages = 0

    for page in range(1, config.THS_MAX_PAGES + 1):
        q = dict(params)
        q["page"] = page
        q["size"] = config.THS_PAGE_SIZE

        data = _request(path, q)
        items = data.get(item_key) or []
        if not isinstance(items, list):
            break

        out.extend([x for x in items if isinstance(x, dict)])
        seen_pages = page

        pag = data.get("pagination") or {}
        total_pages = pag.get("pages")
        try:
            total_pages = int(total_pages) if total_pages is not None else 1
        except (TypeError, ValueError):
            total_pages = 1

        if page >= total_pages or not items:
            break
        time.sleep(config.THS_SLEEP_BETWEEN)

    logger.debug("同花顺分页 %s：%d 页，%d 条", path, seen_pages, len(out))
    return out


# ==============================================================================
# 四、通用清洗与列对齐
# ==============================================================================
def _empty_with_cols(cols: Sequence[str]) -> pd.DataFrame:
    """构造一个「列齐全但 0 行」的空表，保证下游列名不 KeyError。"""
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


def _finalize(df: pd.DataFrame, cols: Sequence[str], source: str,
              dedup: bool = True) -> pd.DataFrame:
    """
    统一收尾：
      ① 补齐缺失列（NaN），按标准列顺序裁剪
      ② 过滤 ST / 退市 / 北交所（同花顺涨停池默认含北交所板块，必须滤）
         dedup=False 时只过滤不去重——龙虎榜必须这样：同一只票会同时上
         「当日榜」和「3 日榜」，去重会静默丢掉一条记录。
         （实测 all 榜 count=62 而 stock_count=60，就是这 2 条的差）
      ③ 打上来源标记，方便报告里注明「本日数据来自同花顺」

    【为什么一定要补齐列】下游 analyzer.py 会直接读 df["所属行业"] /
    df["连板数"]。少一列就是 KeyError，整个报告出不来。
    NaN 占位虽然信息缺失，但至少流程能走完。
    """
    if df is None:
        df = pd.DataFrame()

    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    out = out[list(cols)]

    if not out.empty and "代码" in out.columns and "名称" in out.columns:
        try:
            out = clean_stock_df(out, dedup=dedup)
        except Exception as exc:
            logger.warning("同花顺数据清洗失败（保留原始行）：%s", exc)

    out.attrs["source"] = source
    out.attrs["provider"] = "ths"
    return out.reset_index(drop=True)


# 与东财完全一致的标准列（顺序也一致，方便 diff 和落盘缓存）
COLS_ZT = ["代码", "名称", "涨跌幅", "最新价", "成交额", "流通市值", "换手率",
           "封板资金", "首次封板时间", "最后封板时间", "炸板次数", "涨停统计",
           "连板数", "所属行业"]
COLS_ZB = ["代码", "名称", "涨跌幅", "最新价", "涨停价", "成交额", "换手率",
           "首次封板时间", "炸板次数", "涨停统计", "振幅", "所属行业"]
COLS_DT = ["代码", "名称", "涨跌幅", "最新价", "成交额", "换手率", "封单资金",
           "最后封板时间", "连续跌停", "开板次数", "所属行业"]
COLS_LHB = ["代码", "名称", "上榜日", "解读", "收盘价", "涨跌幅", "龙虎榜净买额",
            "龙虎榜买入额", "龙虎榜卖出额", "换手率", "流通市值", "上榜原因"]


# 与 utils.clean_stock_df 保持同一口径。
# 【为什么游资榜要单独判一次】游资榜返回的是紧凑的字符串列表
# "上纬新材(688585)、萃华退(002731)"，它不经过 DataFrame 清洗，
# 所以 clean_stock_df 那层过滤对它完全无效。
# 实测 2026-11-09 就漏出了「萃华退(002731)」这个退市股。
_EXCLUDE_NAME_KW = ("ST", "退", "PT")
_EXCLUDE_CODE_PREFIX = ("8", "4", "92")


def _is_valid_stock(name: Any, ticker: Any) -> bool:
    """按现有系统口径判断一只票是否该保留（剔除 ST / 退市 / 北交所）。"""
    n = str(name or "").strip().upper()
    if not n:
        return False
    if any(kw in n for kw in _EXCLUDE_NAME_KW):
        return False
    t = str(ticker or "").strip()
    if t.startswith(_EXCLUDE_CODE_PREFIX):
        return False
    return True


def _pick(d: Dict[str, Any], *names: str) -> Any:
    """从 dict 里按候选名取第一个非 None 的值（应对上游偶尔改字段）。"""
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return None


def _to_float(v: Any, scale: float = 1.0) -> Optional[float]:
    """安全转 float；None / 空串 / 非法值 -> None。scale 用于小数转百分数。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return f * scale


# ==============================================================================
# 五、接口 1：涨停池
# ==============================================================================
def fetch_zt_pool(day: date | str, use_cache: bool = False) -> pd.DataFrame:
    """
    同花顺涨停池 → 东财同款 14 列。

    字段映射（官方 limit-up-pool 文档）：
        ticker                  -> 代码
        name                    -> 名称
        price_change_ratio_pct  -> 涨跌幅   （文档：已乘以 100，与东财一致）
        last_price              -> 最新价
        seal_money              -> 封板资金
        limit_up_time           -> 首次封板时间
        open_times              -> 炸板次数
        continue_day_text       -> 涨停统计   （如「5天4板」）
        continue_day_cnt        -> 连板数
        limit_up_reason         -> 所属行业   （见下方说明，受开关控制）
        成交额/流通市值/换手率/最后封板时间  -> 同花顺不提供，NaN 占位
    """
    day_str = to_day_str(day)
    params = {
        "date_ms": to_date_ms(day_str),
        # 排序字段选 continue_day_cnt：分页时按连板数降序比较稳定，
        # 不会因为「多只票同一分钟涨停」导致翻页重复或漏票。
        "sort_field": "continue_day_cnt",
        "sort_dir": "desc",
    }
    items = _paginate(config.THS_EP_LIMIT_UP_POOL, params)
    if not items:
        logger.warning("同花顺涨停池为空：%s", day_str)
        return _finalize(_empty_with_cols(COLS_ZT), COLS_ZT, "ths-limit-up-pool")

    rows: List[Dict[str, Any]] = []
    for it in items:
        reason = _pick(it, "limit_up_reason")
        sector = reason if (config.THS_USE_REASON_AS_SECTOR and reason) else None
        rows.append({
            "代码": _pick(it, "ticker"),
            "名称": _pick(it, "name"),
            "涨跌幅": _to_float(_pick(it, "price_change_ratio_pct")),
            "最新价": _to_float(_pick(it, "last_price")),
            "成交额": _to_float(_pick(it, "turnover", "amount")),
            "流通市值": _to_float(_pick(it, "float_market_value")),
            "换手率": _to_float(_pick(it, "turnover_ratio_pct")),
            "封板资金": _to_float(_pick(it, "seal_money")),
            "首次封板时间": _pick(it, "limit_up_time"),
            "最后封板时间": _pick(it, "last_limit_time"),
            "炸板次数": _pick(it, "open_times"),
            "涨停统计": _pick(it, "continue_day_text"),
            "连板数": _pick(it, "continue_day_cnt"),
            "所属行业": sector,
        })

    df = pd.DataFrame(rows)
    df["连板数"] = pd.to_numeric(df["连板数"], errors="coerce").fillna(1).astype("Int64")
    out = _finalize(df, COLS_ZT, "ths-limit-up-pool")
    logger.info("同花顺涨停池 %s：%d 条（含北交所/ST 已过滤前）", day_str, len(df))
    return out


# ==============================================================================
# 六、接口 2：炸板池
# ==============================================================================
def fetch_zb_pool(day: date | str) -> pd.DataFrame:
    """同花顺炸板池 → 东财同款 12 列。"""
    day_str = to_day_str(day)
    params = {"date_ms": to_date_ms(day_str), "sort_field": "open_times", "sort_dir": "desc"}
    items = _paginate(config.THS_EP_LIMIT_BREAK_POOL, params)
    if not items:
        return _finalize(_empty_with_cols(COLS_ZB), COLS_ZB, "ths-limit-break-pool")

    rows = [{
        "代码": _pick(it, "ticker"),
        "名称": _pick(it, "name"),
        "涨跌幅": _to_float(_pick(it, "price_change_ratio_pct")),
        "最新价": _to_float(_pick(it, "last_price")),
        "涨停价": _to_float(_pick(it, "limit_up_price")),
        "成交额": _to_float(_pick(it, "turnover")),
        "换手率": _to_float(_pick(it, "turnover_ratio_pct")),
        "首次封板时间": _pick(it, "first_seal_time"),
        "炸板次数": _pick(it, "open_times"),
        "涨停统计": _pick(it, "continue_day_text"),
        "振幅": _to_float(_pick(it, "amplitude")),
        "所属行业": _pick(it, "limit_up_reason") if config.THS_USE_REASON_AS_SECTOR else None,
    } for it in items]

    return _finalize(pd.DataFrame(rows), COLS_ZB, "ths-limit-break-pool")


# ==============================================================================
# 七、接口 3：跌停池
# ==============================================================================
def fetch_dt_pool(day: date | str) -> pd.DataFrame:
    """同花顺跌停池 → 东财同款 11 列。"""
    day_str = to_day_str(day)
    params = {"date_ms": to_date_ms(day_str),
              "sort_field": "last_limit_time", "sort_dir": "desc"}
    items = _paginate(config.THS_EP_LIMIT_DOWN_POOL, params)
    if not items:
        return _finalize(_empty_with_cols(COLS_DT), COLS_DT, "ths-limit-down-pool")

    rows = [{
        "代码": _pick(it, "ticker"),
        "名称": _pick(it, "name"),
        "涨跌幅": _to_float(_pick(it, "price_change_ratio_pct")),
        "最新价": _to_float(_pick(it, "last_price")),
        "成交额": _to_float(_pick(it, "turnover")),
        "换手率": _to_float(_pick(it, "turnover_ratio_pct")),
        "封单资金": _to_float(_pick(it, "seal_money")),
        "最后封板时间": _pick(it, "last_limit_time"),
        "连续跌停": _pick(it, "continue_down_day_cnt"),
        "开板次数": _pick(it, "open_times"),
        "所属行业": None,
    } for it in items]

    return _finalize(pd.DataFrame(rows), COLS_DT, "ths-limit-down-pool")


# ==============================================================================
# 八、接口 4：连板天梯（近 30 个交易日）
# ==============================================================================
# 板位键（官方固定六个，缺失补 []）
_LADDER_KEYS = ["two_board", "three_board", "four_board", "five_board", "six_board", "seven_over"]


def _ladder_key_to_num(key: str) -> Optional[int]:
    """板位键 -> 连板数。seven_over 是「7 板及以上」，返回 7 作为下限。"""
    return {
        "two_board": 2, "three_board": 3, "four_board": 4,
        "five_board": 5, "six_board": 6, "seven_over": 7,
    }.get(key)


def fetch_limit_up_ladder(raw: bool = False) -> Any:
    """
    连板天梯（无入参，一次返回近 30 个交易日）。

    raw=True  返回官方原始结构 dict（调试用）
    raw=False 返回「长表」DataFrame：
              日期 | 连板数 | 股票家数 | 股票列表 | 股票代码列表

    【为什么默认转长表】官方是按「日期 -> 6 个板位 -> 股票数组」嵌套的，
    下游要按日期聚合、按板位统计，嵌套结构每用一次就要拆一次，很容易写错。
    拍平成一行 = (日期, 连板数) 后，groupby 一次就能算最高板、连板分布。
    """
    data = _request(config.THS_EP_LIMIT_UP_LADDER, None)
    if raw:
        return data

    window = data.get("window") or {}
    items = data.get("item") or []
    if not isinstance(items, list) or not items:
        logger.warning("同花顺连板天梯返回空")
        return pd.DataFrame(columns=["日期", "连板数", "股票家数", "股票列表", "股票代码列表"])

    rows: List[Dict[str, Any]] = []
    for day_item in items:
        if not isinstance(day_item, dict):
            continue
        d = str(day_item.get("date") or "").replace("-", "")
        boards = day_item.get("boards") or {}
        if not d or not isinstance(boards, dict):
            continue

        for bkey in _LADDER_KEYS:
            level = _ladder_key_to_num(bkey)
            arr = boards.get(bkey) or []
            if not isinstance(arr, list):
                arr = []
            names, codes = [], []
            for s in arr:
                if not isinstance(s, dict):
                    continue
                nm = str(s.get("name") or "").strip()
                tk = str(s.get("ticker") or "").strip()
                if nm:
                    names.append(nm)
                if tk:
                    codes.append(tk)
            rows.append({
                "日期": d,
                "连板数": level,
                "股票家数": len(arr),
                "股票列表": "、".join(names),
                "股票代码列表": ",".join(codes),
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["日期", "连板数"]).reset_index(drop=True)
    df.attrs["window_length"] = window.get("length")
    df.attrs["date_list"] = window.get("date_list") or []
    df.attrs["provider"] = "ths"
    logger.info("同花顺连板天梯：%d 个交易日（窗口 %s）",
                df["日期"].nunique() if not df.empty else 0, window.get("length"))
    return df


# ==============================================================================
# 九、接口 5：涨停统计（封板率 / 涨停家数 / 连板分布）
# ==============================================================================
def fetch_limit_up_statistics(day: date | str) -> Dict[str, Any]:
    """
    单日涨停汇总。返回 dict（不是 DataFrame，因为它是标量指标集合）。

    字段映射（官方 limit-up-statistics 文档）：
        trade_date                       -> 交易日
        seal_rate                        -> 封板率，**小数**，需 ×100
        limit_up_stock_count             -> 涨停家数
        open_stock_count                 -> 涨停打开（炸板）家数
        touched_limit_up_stock_count     -> 封板率基数（涨停 + 炸板）
        page_boards[]                    -> 页面档位（one/two/three/four/five_plus）
        exact_boards[]                   -> 精确连板家数 [{board_num, stock_count}]

    【易错点】seal_rate 是 0.6479 这种小数，不是 64.79。直接当百分比用会让
    封板率永远显示 0.6%，而且不会报错。这里统一 ×100 后再返回。
    """
    day_str = to_day_str(day)
    data = _request(config.THS_EP_LIMIT_UP_STATISTICS, {"date": to_iso_date(day_str)})

    seal_rate = _to_float(_pick(data, "seal_rate"), scale=100.0)
    limit_up_cnt = _pick(data, "limit_up_stock_count")
    touched = _pick(data, "touched_limit_up_stock_count")
    open_cnt = _pick(data, "open_stock_count")

    # 炸板率 = 打开家数 / 基数。基数缺失时用 limit_up + open 反推。
    try:
        touched_i = int(touched) if touched is not None else (
            int(limit_up_cnt or 0) + int(open_cnt or 0)
        )
    except (TypeError, ValueError):
        touched_i = 0

    zb_rate = None
    if touched_i > 0 and open_cnt is not None:
        try:
            zb_rate = round(int(open_cnt) / touched_i * 100, 2)
        except (TypeError, ValueError):
            zb_rate = None
    elif seal_rate is not None:
        zb_rate = round(100 - seal_rate, 2)

    exact = data.get("exact_boards") or []
    board_dist: Dict[int, int] = {}
    max_board = 0
    if isinstance(exact, list):
        for b in exact:
            if not isinstance(b, dict):
                continue
            try:
                n = int(b.get("board_num"))
                c = int(b.get("stock_count") or 0)
            except (TypeError, ValueError):
                continue
            if n > 0:
                board_dist[n] = board_dist.get(n, 0) + c
                max_board = max(max_board, n)

    page_boards = data.get("page_boards") or []

    return {
        "日期": day_str,
        "涨停家数": int(limit_up_cnt) if limit_up_cnt is not None else None,
        "炸板家数": int(open_cnt) if open_cnt is not None else None,
        "封板基数": touched_i,
        "封板率": round(seal_rate, 2) if seal_rate is not None else None,
        "炸板率": zb_rate,
        "最高连板": max_board,
        "连板分布": board_dist,
        "页面档位": page_boards,
        "provider": "ths",
    }


# ==============================================================================
# 十、接口 6：龙虎榜（all / org / hot_money）
# ==============================================================================
LHB_BOARD_TYPES = ("all", "org", "hot_money")


def fetch_dragon_tiger(day: date | str, board_type: str = "all") -> Dict[str, Any]:
    """
    龙虎榜榜单原始结构。

    board_type:
        all       -> stock_items 有值，hot_money_items 为空
        org       -> stock_items 有值（含 org_* 机构字段）
        hot_money -> hot_money_items 有值，stock_items 为空

    【关键差异】这个接口**不给营业部席位名称**，只给：
        · 股票维度的买卖额（stock_items）
        · 游资维度的聚合净买入（hot_money_items[].name + buying + rows）
    也就是说它天然是「游资名称」而不是「营业部全称」——
    对我们反而是好事：不用再用 YOUZI_MAP 去猜营业部对应哪个游资了。
    """
    if board_type not in LHB_BOARD_TYPES:
        raise ValueError(f"board_type 必须是 {LHB_BOARD_TYPES} 之一，收到 {board_type!r}")
    day_str = to_day_str(day)
    data = _request(config.THS_EP_DRAGON_TIGER_LIST,
                    {"board_type": board_type, "date": to_iso_date(day_str)})
    data["_day"] = day_str
    return data


def _lhb_reason(it: Dict[str, Any]) -> str:
    """
    推导「上榜原因」。

    【实测发现的字段差异】
      · board_type=hot_money 的 rows[] 有 limit_reason
      · board_type=all / org 的 stock_items[] **没有** limit_reason
    所以不能只读 limit_reason，否则 all 榜的上榜原因全是空的。

    兜底顺序：limit_reason -> concept_list 里的概念名 -> 「当日榜/3日榜」
    最后一档虽然不含题材信息，但至少能让读者分清是当日榜还是三日累计榜
    （这两者的金额口径不同，看龙虎榜时必须分清）。
    """
    reason = _pick(it, "limit_reason")
    if reason:
        return str(reason)

    concepts = it.get("concept_list") or []
    names: List[str] = []
    if isinstance(concepts, list):
        for c in concepts:
            if isinstance(c, dict):
                nm = c.get("name")
                if nm:
                    names.append(str(nm))
            elif isinstance(c, str) and c:
                names.append(c)
    if names:
        return "、".join(names[:3])

    return "3日榜" if _pick(it, "range_days") == 3 else "当日榜"


def fetch_lhb_stock(day: date | str, board_type: str = "all") -> pd.DataFrame:
    """
    龙虎榜「股票维度」→ 东财同款 12 列。

    用于替代 akshare 的 stock_lhb_detail_em。
    官方字段：change 是小数（0.09994），需 ×100 才是涨跌幅百分比。
    """
    day_str = to_day_str(day)
    try:
        data = fetch_dragon_tiger(day_str, board_type)
    except ThsError as exc:
        logger.warning("同花顺龙虎榜（%s）失败：%s", board_type, exc)
        return _finalize(_empty_with_cols(COLS_LHB), COLS_LHB, "ths-dragon-tiger")

    items = data.get("stock_items") or []
    if not items:
        logger.warning("同花顺龙虎榜 stock_items 为空（board_type=%s, %s）", board_type, day_str)
        return _finalize(_empty_with_cols(COLS_LHB), COLS_LHB, "ths-dragon-tiger")

    trade_date = str(data.get("trade_date") or "").replace("-", "") or day_str

    rows: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        org_net = _to_float(_pick(it, "org_net_value"))
        hot_net = _to_float(_pick(it, "hot_money_net_value"))
        # 「解读」列：东财用它写「机构买 N 家 / 卖 M 家」，
        # 同花顺给了 org_buy_num / org_sell_num，可以生成等价文本。
        parts = []
        buy_n = _pick(it, "org_buy_num")
        sell_n = _pick(it, "org_sell_num")
        if buy_n is not None or sell_n is not None:
            parts.append(f"机构买{int(buy_n or 0)}家 卖{int(sell_n or 0)}家")
        if org_net:
            parts.append(f"机构净{'买' if org_net > 0 else '卖'}{abs(org_net) / 1e4:.0f}万")
        if hot_net:
            parts.append(f"游资净{'买' if hot_net > 0 else '卖'}{abs(hot_net) / 1e4:.0f}万")

        rows.append({
            "代码": _pick(it, "ticker"),
            "名称": _pick(it, "name"),
            "上榜日": trade_date,
            "解读": " · ".join(parts) if parts else "",
            "收盘价": _to_float(_pick(it, "close")),
            "涨跌幅": _to_float(_pick(it, "change"), scale=100.0),
            "龙虎榜净买额": _to_float(_pick(it, "net_value")),
            "龙虎榜买入额": _to_float(_pick(it, "buy_value")),
            "龙虎榜卖出额": _to_float(_pick(it, "sell_value")),
            "换手率": _to_float(_pick(it, "turnover_ratio_pct")),
            "流通市值": _to_float(_pick(it, "float_market_value")),
            "上榜原因": _lhb_reason(it),
        })

    # dedup=False：同一只票可能同时有「当日榜」和「3 日榜」两条记录，
    # 去重会丢掉其中一条，金额就不完整了。
    return _finalize(pd.DataFrame(rows), COLS_LHB,
                     f"ths-dragon-tiger-{board_type}", dedup=False)


def fetch_lhb_hot_money(day: date | str) -> List[Dict[str, Any]]:
    """
    游资榜 → 游资动作记录（供 lhb.py 直接消费）。

    返回 list[dict]，每条：
        游资          官方游资名称（如「章盟主」「量化打板」）
        营业部        同花顺不给席位名，填「同花顺游资榜·聚合」
        净买入        单位：元（与 lhb.py 内部一致）
        操作股票      「名称(代码)」串，供热力图 _parse_stock 解析
        方向 / 来源 / 备注

    【为什么这是本轮最大的收益】
    以前要靠 YOUZI_MAP 把「国泰海通证券上海长宁区江苏路证券营业部」模糊匹配成
    「章盟主」，那是民间说法、会因迁址改名失效。同花顺直接给游资名，
    这一步从「猜」变成了「读」。
    """
    day_str = to_day_str(day)
    try:
        data = fetch_dragon_tiger(day_str, "hot_money")
    except ThsError as exc:
        logger.warning("同花顺游资榜失败（%s）：%s", day_str, exc)
        return []

    items = data.get("hot_money_items") or []
    if not isinstance(items, list) or not items:
        logger.info("同花顺游资榜 %s：无游资记录", day_str)
        return []

    # ---- 关键：拆成「游资 × 股票 × 金额」三元组 ----
    # 接口给的是 hot_money_items[] = 一个游资一条聚合记录（buying=该游资当日合计），
    # 明细在 rows[] 里，每行才是「这只票上的净额」hot_money_item_net_value。
    #
    # 第一版直接按游资聚合输出，结果 dashboard 的桑基图/热力图全空——
    # 因为那两张图要的是三元组，聚合记录里根本没有单票金额。
    # 现在每个 (游资, 股票) 拆成一条，金额取 hot_money_item_net_value，
    # 游资总额放进备注供核对。
    records: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        youzi = str(it.get("name") or "").strip()
        if not youzi:
            continue
        total = _to_float(it.get("buying")) or 0.0

        rows = it.get("rows") or []
        kept = 0
        for r in rows:
            if not isinstance(r, dict):
                continue
            nm = str(r.get("name") or "").strip()
            tk = str(r.get("ticker") or "").strip()

            # 剔除 ST / 退市 / 北交所（实测漏出过「萃华退(002731)」）
            if not _is_valid_stock(nm, tk):
                logger.debug("游资榜跳过不合规标的：%s(%s)", nm, tk)
                continue

            # 单票净额：优先 hot_money_item_net_value（该游资在该股上的净额），
            # 缺失时退回 net_value（该股整体龙虎榜净额），并标注口径
            amt = _to_float(_pick(r, "hot_money_item_net_value"))
            note = ""
            if amt is None:
                amt = _to_float(_pick(r, "net_value"))
                note = "单票净额缺失，退回该股龙虎榜净额"
            if amt is None:
                continue

            stock = f"{nm}({tk})" if (nm and tk) else (nm or tk)
            records.append({
                "游资": youzi,
                "营业部": "同花顺游资榜",
                "营业部代码": "",
                "净买入": float(amt),
                "操作股票": stock,
                "方向": "买入" if amt > 0 else ("卖出" if amt < 0 else "对倒"),
                "来源": "同花顺游资榜",
                "备注": f"该游资当日合计 {total / 1e4:,.0f}万" + (f"；{note}" if note else ""),
            })
            kept += 1

        if kept == 0:
            logger.debug("游资 %s 的标的全部被过滤或缺失，跳过", youzi)

    logger.info("同花顺游资榜 %s：%d 个游资 / %d 条「游资×股票」记录",
                day_str, len({r["游资"] for r in records}), len(records))
    return records
    return records


# ==============================================================================
# 十一、自检
# ==============================================================================
def _selftest() -> None:
    """离线自检：不联网，只验证 Key 配置、日期工具、映射函数。"""
    from utils import setup_logger

    setup_logger("INFO")
    print("=" * 70)
    print("  同花顺通道自检（离线，不发请求）")
    print("=" * 70)
    st = status()
    for k, v in st.items():
        print(f"  {k:<10}: {v}")

    print("\n--- 日期工具 ---")
    print(f"  to_day_str(date(2026,11,9)) = {to_day_str(date(2026, 11, 9))}")
    print(f"  to_iso_date('20261109')     = {to_iso_date('20261109')}")
    print(f"  to_date_ms('20261109')      = {to_date_ms('20261109')}"
          f"  （应等于 2026-11-09 00:00:00 +08:00）")
    back = datetime.fromtimestamp(to_date_ms("20261109") / 1000, _TZ_CN)
    print(f"  反解校验                     = {back.isoformat()}")

    print("\n--- 官方示例 JSON 的映射校验 ---")
    sample = {
        "code": 0,
        "data": {
            "trade_date": "2026-10-12",
            "seal_rate": 0.647887323943662,
            "open_stock_count": 25,
            "limit_up_stock_count": 46,
            "touched_limit_up_stock_count": 71,
            "exact_boards": [{"board_num": 10, "stock_count": 1},
                             {"board_num": 2, "stock_count": 14}],
        },
    }
    d = sample["data"]
    seal = _to_float(d["seal_rate"], scale=100.0)
    zb = round(d["open_stock_count"] / d["touched_limit_up_stock_count"] * 100, 2)
    print(f"  封板率 0.6479 -> {seal}%  （应约 64.79）")
    print(f"  炸板率 25/71  -> {zb}%  （应约 35.21）")
    print(f"  自洽性：封板率 + 炸板率 = {round(seal + zb, 2)}%  （应约 100）")

    print("\n--- 结论 ---")
    if st["usable"]:
        print("  ✅ Key 已配置，可以联网测试：python ths_fetch.py --live 20261109")
    else:
        print("  ⚠️ 未配置 FUYAO_API_KEY，同花顺通道保持禁用（系统会自动退回纯东财模式）")
        print("     Windows 设置：setx FUYAO_API_KEY \"你的key\"  然后重开终端")


def _live(day_str: str) -> None:
    """联网实测（需要 Key）。"""
    from utils import setup_logger

    setup_logger("INFO")
    print(f"\n=== 联网实测 {day_str} ===\n")

    try:
        stats = fetch_limit_up_statistics(day_str)
        print(f"【涨停统计】涨停 {stats['涨停家数']} 家｜炸板 {stats['炸板家数']} 家"
              f"｜封板率 {stats['封板率']}%｜炸板率 {stats['炸板率']}%"
              f"｜最高 {stats['最高连板']} 板")
    except ThsError as exc:
        print(f"【涨停统计】失败：{exc}")

    for label, fn in (("涨停池", fetch_zt_pool), ("炸板池", fetch_zb_pool), ("跌停池", fetch_dt_pool)):
        try:
            df = fn(day_str)
            print(f"【{label}】{len(df)} 行，列 {list(df.columns)[:6]}…")
        except ThsError as exc:
            print(f"【{label}】失败：{exc}")

    try:
        ladder = fetch_limit_up_ladder()
        print(f"【连板天梯】{len(ladder)} 行，覆盖 {ladder['日期'].nunique()} 个交易日")
        if not ladder.empty:
            print(ladder.head(6).to_string(index=False))
    except ThsError as exc:
        print(f"【连板天梯】失败：{exc}")

    try:
        recs = fetch_lhb_hot_money(day_str)
        print(f"【游资榜】{len(recs)} 条")
        for r in recs[:5]:
            print(f"   {r['游资']:<10} {r['净买入']/1e4:>10,.0f}万  {r['操作股票'][:40]}")
    except ThsError as exc:
        print(f"【游资榜】失败：{exc}")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args and args[0] == "--live":
        _live(args[1] if len(args) > 1 else datetime.now().strftime("%Y%m%d"))
    else:
        _selftest()
