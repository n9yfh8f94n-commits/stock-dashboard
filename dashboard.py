#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dashboard.py —— A股超短线复盘 · 单页 HTML 仪表盘
================================================================================

设计定位
--------
main.py 产出的是「文字报告」（给眼睛读的复盘结论）；
dashboard.py 产出的是「图形仪表盘」（给眼睛扫趋势的，一眼看出结构和异动）。

两者共用同一套数据与计算，不重复实现逻辑：
    数据来源 = cache/*.csv（接口快照） + calibration/metrics_history.csv（长期累积）
    计算逻辑 = analyzer.py / lhb.py（直接 import，绝不复制粘贴公式）

为什么选 pyecharts 而不是 Streamlit
-----------------------------------
Streamlit 本质是一个常驻的 Web 服务进程：要开着终端、占用端口，
每天复盘完还得手动 Ctrl+C 关掉，也无法像文件一样发给别人或存档。

pyecharts 产出的是**静态 HTML 文件**，双击就开，可以放进归档目录、
可以用浏览器收藏、可以发给别人。对一个「每天盘后跑一次」的复盘系统，
静态文件才是对的形态。

离线可用（重要）
----------------
pyecharts 默认从 CDN 引 echarts.min.js，断网打开就是一片空白。
本模块把 echarts.min.js 下载到 assets/ 并**内联进 HTML**，
生成的单文件可以彻底脱离网络、拷到任何机器上打开。

三个图表
--------
① 情绪周期图  折线+柱：近 N 个交易日的「真实涨停数」与「炸板率」双轴，
              柱子按当日情绪档位着色，附炸板率 35%/50% 两条阈值线。
② 连板天梯图  柱状：各连板高度的家数，高板更红，tooltip 列出该档全部个股。
③ 游资买卖图  桑基图（钱从游资流向哪只票）+ 热力图（游资×股票的买卖矩阵）。

用法
----
    python dashboard.py                  # 最近交易日，自动开浏览器
    python dashboard.py 20261109         # 指定日期
    python dashboard.py --days 30        # 情绪周期图看更长的窗口
    python dashboard.py --no-seat        # 跳过联网抓个股席位（纯离线，快）
    python dashboard.py --no-open        # 只生成文件，不开浏览器

依赖
----
    pip install pyecharts        # 唯一新增依赖，其余复用现有 requirements
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

import analyzer
import config
import lhb as lhb_mod
from utils import clean_stock_df, logger, setup_logger

try:
    from pyecharts import options as opts
    from pyecharts.charts import Bar, HeatMap, Line, Sankey
    from pyecharts.commons.utils import JsCode
except ImportError:  # pragma: no cover
    print(
        "\n[错误] 缺少 pyecharts，仪表盘无法生成。\n"
        "       安装命令：  python -m pip install pyecharts\n"
        "       （注意用 python -m pip，不要用裸 pip——本机可能存在多个 Python，\n"
        "         裸 pip 会装到另一个解释器里，之后 import 仍会失败）\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


# ==============================================================================
# 0. 主题常量
# ==============================================================================
# 配色遵循 A 股习惯：红涨绿跌。
# 注意「炸板率」不适用红绿语义（它不是价格方向，是健康度指标），
# 所以单独用「风险色阶」：低=青绿（封得住），高=橙红（封不住）。
BG        = "#0b0e14"   # 页面底色
PANEL     = "#131a24"   # 卡片底色
PANEL_2   = "#1a2230"   # 卡片内嵌区块
BORDER    = "#243040"   # 描边
TEXT      = "#e6edf3"   # 主文字
MUTED     = "#8b98a8"   # 次要文字
UP        = "#f6465d"   # 涨 / 买入 / 风险高
DOWN      = "#0ecb81"   # 跌 / 卖出 / 健康
GOLD      = "#f0b90b"   # 强调
ORANGE    = "#ff9f43"   # 炸板率线
BLUE      = "#4c9aff"   # 辅助

# 情绪档位 -> 颜色（柱子着色、徽章都用它，保证全页语义一致）
STATUS_COLOR: Dict[str, str] = {
    "冰点": "#5b6b82",
    "退潮": "#0ecb81",
    "震荡": "#f0b90b",
    "回暖": "#4c9aff",
    "高潮": "#f6465d",
}

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = config.BASE_DIR / "cache"
CALIB_FILE = config.BASE_DIR / "calibration" / "metrics_history.csv"
ASSET_JS = BASE_DIR / "assets" / "echarts.min.js"
REPORT_DIR = config.BASE_DIR / "reports"

# 情绪周期图默认窗口。
# 原来是 20（受东财 20 交易日窗口限制）。接入同花顺历史回填后能拿到 29~30 天，
# 所以默认放到 30——数据有多少画多少，不足时 build_history 会自动缩短并在页头说明。
DEFAULT_DAYS = 30


# ==============================================================================
# 1. JS 回调处理（pyecharts 的一个坑，必须处理）
# ==============================================================================
# 【问题】
#   pyecharts 的 dump_options_with_quotes() 会把 JsCode 一起 json 序列化，
#   结果浏览器收到的是 "function(p){...}" 这个**字符串**，而不是函数。
#   ECharts 拿到字符串会静默忽略该配置——不报错，但效果全无。
#   （实测确认：dump 出来的内容里 "formatter": "function(...)" 带引号）
#
# 【解法】
#   给 JS 代码包一对独一无二的标记，序列化后按标记精确还原。
#   用正则只匹配标记内部，绝不会误伤普通字符串数据。
_JS_TOKEN = "@@__JS__@@"


def js(code: str) -> Any:
    """把一个 JS 函数体声明为「真·JS」，导出时还原成非字符串。"""
    return JsCode(f"{_JS_TOKEN}{code}{_JS_TOKEN}")


def dump_options(chart: Any) -> str:
    """导出 ECharts option JSON，并把占位标记还原成真正的 JS 函数。"""
    raw = chart.dump_options_with_quotes()

    def _restore(m: "re.Match[str]") -> str:
        # 标记内部的文本被 JSON 转义过（双引号变 \"），用 json.loads 反解回原文
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)

    return re.sub(rf'"{re.escape(_JS_TOKEN)}(.*?){re.escape(_JS_TOKEN)}"',
                  _restore, raw, flags=re.S)


# ==============================================================================
# 2. 数据层
# ==============================================================================
def _read_cache(day: str, key: str, code_col: bool = True) -> pd.DataFrame:
    """
    读单日缓存快照。不存在 / 损坏 / 空表一律返回空 DataFrame。

    这个函数是仪表盘「离线可跑」的基础：只要 cache/ 里有快照，
    不联网也能把整套图渲染出来。
    """
    p = CACHE_DIR / f"{day}_{key}.csv"
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(p, dtype={"代码": str} if code_col else None, encoding="utf-8-sig")
        return df if not df.empty else pd.DataFrame()
    except Exception as exc:
        logger.warning("缓存读取失败 %s：%s", p.name, exc)
        return pd.DataFrame()


def load_day(day: str) -> Dict[str, pd.DataFrame]:
    """
    加载单日全部数据，并做与线上一致的清洗。

    【为什么必须清洗】cache 里存的是接口原始快照，龙虎榜里含可转债/ST 股。
    如果这里偷懒不清洗，仪表盘的统计口径就会和 main.py 的报告不一致——
    同一个日期两张表给出两个涨停家数，是这类系统最让人失去信任的 bug。
    """
    data = {
        "zt": _read_cache(day, "zt_pool"),
        "zb": _read_cache(day, "zb_pool"),
        "dt": _read_cache(day, "dt_pool"),
        "prev_zt": _read_cache(day, "prev_zt_pool"),
        "lhb": _read_cache(day, "lhb"),
        "yyb": _read_cache(day, "active_yyb", code_col=False),
    }
    for k in ("zt", "zb", "dt", "prev_zt", "lhb"):
        if not data[k].empty:
            data[k] = clean_stock_df(data[k], dedup=(k != "lhb"))
    return data


def list_cached_days() -> List[str]:
    """扫描 cache/ 里所有有涨停池快照的交易日（升序）。"""
    days = set()
    for p in CACHE_DIR.glob("*_zt_pool.csv"):
        head = p.name[:8]
        if head.isdigit() and len(head) == 8:
            days.add(head)
    return sorted(days)


def _load_metrics_history() -> pd.DataFrame:
    """读长期指标库（calibration/metrics_history.csv），不存在返回空表。"""
    if not CALIB_FILE.exists():
        return pd.DataFrame()
    try:
        h = pd.read_csv(CALIB_FILE, dtype={"日期": str}, encoding="utf-8-sig")
        h["日期"] = h["日期"].astype(str).str.zfill(8)
        return h
    except Exception as exc:
        logger.warning("指标库读取失败：%s", exc)
        return pd.DataFrame()


def build_history(days: int = DEFAULT_DAYS) -> pd.DataFrame:
    """
    构造情绪周期图所需的时间序列。

    数据来源的优先级（这是本函数唯一需要小心的地方）
    ------------------------------------------------
      ① cache/ 里有涨停池快照的日期  → **重新计算**（最可信，口径与报告完全一致）
      ② cache/ 缺失但 metrics_history.csv 有 → 用历史库里的存量值补

    为什么优先重算而不是直接读历史库：
        历史库是「当时那次运行算出来的」，如果之后修正了情绪判定规则，
        历史库里存的是旧结论；重算能保证整张图用的是同一套现行规则。

    关于天数：东财涨停池接口只保留约 20 个交易日，更早的日期取不到。
    因此实际能画的天数往往小于请求的 days —— 这是接口硬限制，不是 bug。
    返回值里带 attrs["requested"] / attrs["actual"]，供页头如实说明。
    """
    hist_store = _load_metrics_history()
    store_map: Dict[str, Dict[str, Any]] = {}
    if not hist_store.empty:
        for _, r in hist_store.iterrows():
            store_map[str(r["日期"])] = r.to_dict()

    cached = list_cached_days()
    rows: List[Dict[str, Any]] = []

    for day in cached:
        try:
            data = load_day(day)
            e = analyzer.calc_market_emotion(
                data["zt"], data["zb"], data["dt"], data["prev_zt"], data["lhb"],
            )
            rows.append({
                "日期": day,
                "涨停数": int(e.get("total_zt", 0) or 0),
                "炸板率": float(e.get("zb_rate", 0.0) or 0.0),
                "封板率": float(e.get("seal_rate", 0.0) or 0.0),
                "赚钱效应": float(e.get("avg_prev_zt_pct", 0.0) or 0.0),
                "最高连板": int(e.get("max_board", 0) or 0),
                "跌停数": int(e.get("dt_cnt", 0) or 0),
                "情绪": str(e.get("status", "-")),
                "_from_cache": True,
            })
        except Exception as exc:
            logger.warning("历史日 %s 计算失败（跳过）：%s", day, exc)

    have = {r["日期"] for r in rows}

    # 用指标库补 cache 缺失的日期
    for day, rec in store_map.items():
        if day in have:
            continue
        try:
            rows.append({
                "日期": day,
                "涨停数": int(float(rec.get("涨停数", 0) or 0)),
                "炸板率": float(rec.get("炸板率", 0.0) or 0.0),
                "封板率": float(rec.get("封板率", 0.0) or 0.0),
                "赚钱效应": float(rec.get("赚钱效应", 0.0) or 0.0),
                "最高连板": int(float(rec.get("最高连板", 0) or 0)),
                "跌停数": int(float(rec.get("跌停数", 0) or 0)),
                "情绪": str(rec.get("情绪", "-")),
                "_from_cache": False,
            })
        except Exception:
            continue

    if not rows:
        out = pd.DataFrame(columns=["日期", "涨停数", "炸板率", "封板率", "赚钱效应",
                                    "最高连板", "跌停数", "情绪", "_from_cache"])
        out.attrs["requested"] = days
        out.attrs["actual"] = 0
        return out

    df = pd.DataFrame(rows).sort_values("日期").reset_index(drop=True)
    df = df.tail(days).reset_index(drop=True)
    df["短日期"] = df["日期"].str[4:6] + "-" + df["日期"].str[6:8]

    df.attrs["requested"] = days
    df.attrs["actual"] = len(df)
    df.attrs["cached_cnt"] = int(df["_from_cache"].sum())
    return df


def load_youzi(day: str, use_seat: bool = True) -> Tuple[pd.DataFrame, str]:
    """
    游资明细：双层降级，与 lhb.py 完全一致。

    返回 (明细 DataFrame, 口径说明字符串)

    为什么第二层（个股席位）必须默认开启
    ------------------------------------
    第一层「活跃营业部」表只能告诉你「这个营业部今天合计净买入 4 亿」，
    不能告诉你「这 4 亿分别买在哪只票上各多少钱」。
    而桑基图/热力图必须精确到单只票的金额，否则同一笔钱会在多只票上
    重复计算，画出来的图是假的。

    第二层对每只票各查一次买卖席位，才能拿到 游资×股票×金额 三元组。
    代价是联网约 10~20 秒，所以留了 --no-seat 开关。
    """
    data = load_day(day)

    # 二级扫描对象：龙虎榜里净买额绝对值最大的若干只票
    # （涨停但没上龙虎榜的票，席位接口本来就查不到，扫了也是浪费请求）
    lhb_df = data.get("lhb", pd.DataFrame())
    codes: List[str] = []
    if not lhb_df.empty and "代码" in lhb_df.columns:
        tmp = lhb_df.drop_duplicates(subset=["代码"]).copy()
        col = "龙虎榜净买额" if "龙虎榜净买额" in tmp.columns else None
        if col:
            tmp["_abs"] = pd.to_numeric(tmp[col], errors="coerce").abs().fillna(0.0)
            tmp = tmp.sort_values("_abs", ascending=False)
        codes = [str(c).zfill(6) for c in tmp["代码"].tolist()[: config.LHB_SEAT_MAX_CODES]]

    fetcher = None
    note = "仅第一层（活跃营业部合计口径），单票金额不可得"
    if use_seat and codes:
        # 【不要用 try 包住构造过程】
        # 这行最初写成 `from data_fetch import make_seat_fetcher`（其实它在 lhb.py 里），
        # 外层套了 try/except，ImportError 被吞掉 -> fetcher=None -> 静默降级成第一层。
        # 表现是「跑得飞快、不报错、图上没有单票金额」，最难查。
        # make_seat_fetcher 只是返回一个闭包，本身不会失败，所以直接调用让它自己炸。
        #
        # 若真出现导入问题，这里会抛 ImportError 并带上模块名，一眼可见。
        fetcher = lhb_mod.make_seat_fetcher(day)
        note = f"双层：活跃营业部 + {len(codes)} 只龙虎榜个股席位"

    try:
        records = lhb_mod.match_youzi(
            active_yyb_df=data.get("yyb", pd.DataFrame()),
            lhb_detail_df=lhb_df,
            target_codes=codes if fetcher else None,
            zt_df=data.get("zt", pd.DataFrame()),
            seat_fetcher=fetcher,
            day=day,
        )
        df = lhb_mod.to_dataframe(records)
    except Exception as exc:
        logger.error("游资数据生成失败（不影响其它图表）：%s", exc)
        df = pd.DataFrame()

    return df, note


# ==============================================================================
# 3. 图表层（pyecharts -> option JSON）
# ==============================================================================
def _axis(name: str = "", split: bool = True, inverse: bool = False) -> opts.AxisOpts:
    """统一的坐标轴样式（深色主题下手写每个颜色，别指望默认值）。"""
    return opts.AxisOpts(
        name=name,
        is_inverse=inverse,
        name_textstyle_opts=opts.TextStyleOpts(color=MUTED, font_size=11),
        axislabel_opts=opts.LabelOpts(color=MUTED, font_size=11),
        axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color=BORDER)),
        axistick_opts=opts.AxisTickOpts(is_show=False),
        splitline_opts=opts.SplitLineOpts(
            is_show=split,
            linestyle_opts=opts.LineStyleOpts(color=BORDER, type_="dashed", opacity=0.45),
        ),
    )


def _tooltip(trigger: str = "axis", formatter: Any = None) -> opts.TooltipOpts:
    kw: Dict[str, Any] = dict(
        trigger=trigger,
        background_color="#1b2431",
        border_color=BORDER,
        border_width=1,
        textstyle_opts=opts.TextStyleOpts(color=TEXT, font_size=12),
        extra_css_text="box-shadow:0 8px 24px rgba(0,0,0,.5);border-radius:8px;",
    )
    if formatter is not None:
        kw["formatter"] = formatter
    return opts.TooltipOpts(**kw)


def _title(text: str, sub: str = "") -> opts.TitleOpts:
    """
    图标题。

    【为什么 pos_top 用数字而不是百分比】
    第一版写 pos_top="1%"，在 340~460px 高的容器里只有 3~5px，
    副标题几乎贴在画布上边缘，视觉上和主标题糊成一行。
    改成固定 px 后在任何容器高度下都稳定。
    """
    return opts.TitleOpts(
        title=text,
        subtitle=sub,
        title_textstyle_opts=opts.TextStyleOpts(color=TEXT, font_size=15, font_weight="bold"),
        subtitle_textstyle_opts=opts.TextStyleOpts(color=MUTED, font_size=10.5),
        pos_left="1.2%",
        pos_top=8,
        item_gap=7,
    )


def _with_grid(chart: Any, top: str = "22%", right: str = "6%",
               bottom: str = "14%", left: str = "8%") -> Any:
    """
    给直角坐标系图表预留「标题区」空间。

    pyecharts 没有暴露 grid 参数，但它内部就是 dict，直接注入即可。
    （已实测：dump_options_with_quotes 会把这个键带出去）

    【为什么必须做】不注入时 ECharts 默认 grid.top=60px，
    双行标题 + 双 Y 轴名称会压到柱子上；而百分比 top 会随容器高度缩放，
    在 340px 和 460px 两种卡片里表现不一致。
    """
    chart.options["grid"] = {
        "top": top, "left": left, "right": right, "bottom": bottom,
        "containLabel": True,
    }
    return chart


# ---------------------------------------------------------------- 图① 情绪周期
def chart_emotion(hist: pd.DataFrame) -> str:
    """
    近 N 日「真实涨停数（柱）+ 炸板率（线）」。

    柱子按当日情绪档位着色 —— 这一层着色让「日期↔档位」的对应关系肉眼可读，
    比在下面加一行 tag 文字有效得多。

    炸板率加两条阈值线：35%（分歧警戒）与 50%（退潮兜底线），
    直接取自 config.EMOTION_THRESHOLDS，不写死数字，
    避免以后调阈值时图和规则脱节。
    """
    if hist.empty:
        return _empty_option("暂无历史数据（cache/ 与 metrics_history.csv 均为空）")

    x = hist["短日期"].tolist()
    zt = hist["涨停数"].tolist()
    zb = hist["炸板率"].tolist()
    status = hist["情绪"].tolist()
    money = hist["赚钱效应"].tolist()
    colors = [STATUS_COLOR.get(s, "#5b6b82") for s in status]

    # 柱子：value 用 dict 形式带 itemStyle，就能逐根上色（不依赖 JS 回调）
    data_items = [
        {"value": v, "itemStyle": {"color": c, "borderRadius": [3, 3, 0, 0]}}
        for v, c in zip(zt, colors)
    ]

    bar = (
        Bar(init_opts=opts.InitOpts(bg_color="transparent"))
        .add_xaxis(x)
        .add_yaxis(
            "真实涨停数",
            data_items,
            yaxis_index=0,
            bar_width="52%",
            label_opts=opts.LabelOpts(is_show=False),
        )
        .extend_axis(
            yaxis=opts.AxisOpts(
                name="炸板率 %",
                position="right",
                min_=0,
                max_=100,
                name_textstyle_opts=opts.TextStyleOpts(color=ORANGE, font_size=11),
                axislabel_opts=opts.LabelOpts(color=ORANGE, font_size=11, formatter="{value}%"),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color=ORANGE)),
                axistick_opts=opts.AxisTickOpts(is_show=False),
                splitline_opts=opts.SplitLineOpts(is_show=False),
            )
        )
        .set_global_opts(
            title_opts=_title("情绪周期", "柱=涨停家数（按档位着色） · 线=炸板率 · 虚线=阈值线"),
            xaxis_opts=_axis(split=False),
            yaxis_opts=_axis("涨停家数"),
            tooltip_opts=_tooltip(
                "axis",
                js(
                    "function(ps){"
                    "  var i = ps[0].dataIndex;"
                    f"  var st = {json.dumps(status, ensure_ascii=False)};"
                    f"  var mo = {json.dumps([None if pd.isna(m) else round(float(m),2) for m in money], ensure_ascii=False)};"
                    "  var out = '<b>' + ps[0].axisValue + '</b>　' + st[i] + '<br/>';"
                    "  for (var k = 0; k < ps.length; k++) {"
                    "    var s = ps[k].seriesName;"
                    "    var v = (s.indexOf('炸板率') >= 0) ? (ps[k].value + '%') : (ps[k].value + ' 家');"
                    "    out += ps[k].marker + s + '：<b>' + v + '</b><br/>';"
                    "  }"
                    "  out += '赚钱效应：<b>' + (mo[i] >= 0 ? '+' : '') + mo[i] + '%</b>';"
                    "  return out;"
                    "}"
                ),
            ),
            legend_opts=opts.LegendOpts(
                pos_right="2%", pos_top=8,
                textstyle_opts=opts.TextStyleOpts(color=MUTED, font_size=11),
            ),
        )
    )

    th = config.EMOTION_THRESHOLDS
    line = (
        Line()
        .add_xaxis(x)
        .add_yaxis(
            "炸板率",
            zb,
            yaxis_index=1,
            is_smooth=True,
            is_symbol_show=True,
            symbol_size=6,
            color=ORANGE,
            linestyle_opts=opts.LineStyleOpts(width=2.4, color=ORANGE),
            itemstyle_opts=opts.ItemStyleOpts(color=ORANGE, border_color=BG, border_width=2),
            label_opts=opts.LabelOpts(is_show=False),
            markline_opts=opts.MarkLineOpts(
                symbol=["none", "none"],
                label_opts=opts.LabelOpts(
                    color=MUTED, font_size=10, position="insideEndTop",
                    formatter="{b}",
                ),
                linestyle_opts=opts.LineStyleOpts(width=1, type_="dashed", opacity=0.75),
                data=[
                    opts.MarkLineItem(
                        y=th.get("ebb_zb_rate_min", 35),
                        name=f"分歧线 {th.get('ebb_zb_rate_min', 35):.0f}%",
                        linestyle_opts=opts.LineStyleOpts(color=GOLD, type_="dashed", width=1),
                    ),
                    opts.MarkLineItem(
                        y=th.get("ebb_zb_rate_alone", 50),
                        name=f"退潮线 {th.get('ebb_zb_rate_alone', 50):.0f}%",
                        linestyle_opts=opts.LineStyleOpts(color=UP, type_="dashed", width=1),
                    ),
                ],
            ),
        )
        .set_global_opts(xaxis_opts=_axis(split=False))
    )

    bar.overlap(line)
    _with_grid(bar, top="27%", right="7%", bottom="14%", left="9%")
    return dump_options(bar)


# ---------------------------------------------------------------- 图② 连板天梯
def chart_ladder(zt: pd.DataFrame) -> str:
    """
    各连板高度的家数分布。

    【为什么要把「0 家」的高度也画出来】
    第一版只画有数据的高度：20261109 只有 1板(45家)、2板(8家)、6板(1家) 三根柱子。
    三根柱子撑满整个横轴，柱子之间的巨大空隙没有任何含义——
    读者只会觉得"图没画满"，却看不出**3/4/5 板全部断档**这件最要命的事。

    补齐成 1..最高板 的完整序列后，3/4/5 板显示为 0 的空柱，
    情绪结构的「断层」立刻肉眼可见：要么是逐级递减的健康金字塔，
    要么是像今天这样中间空掉、直接跳到 6 板的畸形结构（说明接力资金缺席，
    只有一只资金硬顶的空间板在裸奔）。
    """
    ladder = analyzer.build_lianban_ladder(zt)
    if ladder is None or ladder.empty:
        return _empty_option("今日无涨停数据")

    d = ladder.sort_values("连板数").reset_index(drop=True)
    by_level = {int(r["连板数"]): r for _, r in d.iterrows()}
    top = int(d["连板数"].max())

    # 至少铺到 6 档，避免只有 3 个类目时横轴被拉得过稀
    levels = list(range(1, max(top, 6) + 1))
    x = [f"{lv}板" for lv in levels]
    y = [int(by_level[lv]["家数"]) if lv in by_level else 0 for lv in levels]
    stocks = [str(by_level[lv]["股票"]) if lv in by_level else "该高度今日断档（0 家）"
              for lv in levels]
    money = [float(by_level[lv]["合计封板资金"]) if lv in by_level else 0.0
             for lv in levels]
    gaps = [lv for lv in levels if lv not in by_level and lv < top]

    # 高度越高越红：青蓝 -> 橙 -> 正红，与「情绪温度」一致
    def _c(level: int, cnt: int) -> str:
        if cnt == 0:
            return "#2b3648"          # 断档：暗灰空柱，不抢视觉
        if level >= max(3, top - 1):
            return UP
        if level == 2:
            return "#f08a5d"
        return "#3d7fd1"

    items = [
        {"value": v, "itemStyle": {"color": _c(lv, v), "borderRadius": [4, 4, 0, 0]}}
        for v, lv in zip(y, levels)
    ]

    sub = "各高度涨停家数"
    if gaps:
        sub += " · 断档：" + "/".join(f"{g}板" for g in gaps)

    bar = (
        Bar(init_opts=opts.InitOpts(bg_color="transparent"))
        .add_xaxis(x)
        .add_yaxis(
            "家数",
            items,
            bar_width="44%",
            label_opts=opts.LabelOpts(
                is_show=True, position="top", color=TEXT, font_size=12, font_weight="bold",
                formatter=js("function(p){ return p.value > 0 ? p.value : ''; }"),
            ),
        )
        .set_global_opts(
            title_opts=_title("连板天梯", sub),
            xaxis_opts=_axis(split=False),
            yaxis_opts=opts.AxisOpts(
                name="家数",
                name_location="middle",
                name_gap=40,
                name_textstyle_opts=opts.TextStyleOpts(color=MUTED, font_size=11),
                axislabel_opts=opts.LabelOpts(color=MUTED, font_size=11),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color=BORDER)),
                axistick_opts=opts.AxisTickOpts(is_show=False),
                splitline_opts=opts.SplitLineOpts(
                    is_show=True,
                    linestyle_opts=opts.LineStyleOpts(color=BORDER, type_="dashed", opacity=0.45),
                ),
            ),
            tooltip_opts=_tooltip(
                "item",
                js(
                    "function(p){"
                    f"  var names = {json.dumps(stocks, ensure_ascii=False)};"
                    f"  var famt = {json.dumps([round(m/1e8, 2) for m in money], ensure_ascii=False)};"
                    "  var i = p.dataIndex;"
                    "  if (p.value === 0) {"
                    "    return '<b>' + p.name + '</b><br/><span style=\"color:#8b98a8\">今日断档，无个股</span>';"
                    "  }"
                    "  return '<b>' + p.name + '</b>　' + p.value + ' 家<br/>' +"
                    "         '封板资金合计：<b>' + famt[i] + ' 亿</b><br/>' +"
                    "         '<span style=\"color:#8b98a8\">' + names[i] + '</span>';"
                    "}"
                ),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
        )
    )
    _with_grid(bar, top="24%", right="4%", bottom="14%", left="9%")
    return dump_options(bar)


# ---------------------------------------------------------------- 图③ 游资桑基
def _seat_only(df: pd.DataFrame) -> pd.DataFrame:
    """只保留「个股席位」口径的行（唯一能给出单票金额的来源）。"""
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["_is_seat"] = d["来源"].astype(str).str.startswith(("席位", "同花顺游资榜"))
    return d[d["_is_seat"]]


def _parse_stock(cell: Any) -> Tuple[str, str]:
    """把 '雪人集团(002639)' 拆成 (名称, 代码)；无括号则代码为空。"""
    s = str(cell)
    m = re.match(r"^(.*?)\((\d{6})\)$", s.strip())
    if m:
        return m.group(1), m.group(2)
    return s.strip(), ""


def chart_youzi_sankey(df: pd.DataFrame) -> str:
    """
    桑基图：净买入的钱从「游资」流向「股票」。

    【为什么只画买入方向】
    桑基图的流量必须为正，语义是「从 A 流向 B」。
    净卖出是反向流动，硬塞进同一张桑基会让宽度失真、方向反直觉。
    卖出行为单独交给下面的热力图（红色=买、绿色=卖，正负同图可比），
    两张图配合才完整，而不是用一张图把两种语义混在一起。
    """
    seat = _seat_only(df)
    if seat.empty:
        return _empty_option(
            "无个股席位数据 —— 桑基图需要「游资×股票×金额」三元组\n"
            "请去掉 --no-seat 重新运行（会联网抓取龙虎榜个股买卖前五席位）"
        )

    d = seat.copy()
    d["净买入(万)"] = pd.to_numeric(d["净买入(万)"], errors="coerce").fillna(0.0)
    buy = d[d["净买入(万)"] > 0].copy()
    if buy.empty:
        return _empty_option("今日席位数据中无净买入记录")

    # 单票金额聚合（同一游资同一只票可能有多条席位记录）
    buy["股票名"] = buy["操作股票"].map(lambda s: _parse_stock(s)[0])
    buy["股票码"] = buy["操作股票"].map(lambda s: _parse_stock(s)[1])
    agg = (buy.groupby(["游资", "股票名", "股票码"], as_index=False)["净买入(万)"].sum())
    agg = agg.sort_values("净买入(万)", ascending=False)

    youzi_net = d.groupby("游资")["净买入(万)"].sum().to_dict()
    # 【截断策略：按游资分组，而不是全局取 Top N】
    # 第一版用全局 head(40) -> 连线糊成一团；改成 head(25) 后又出现新问题：
    # 小额游资的线全被挤掉，桑基里只剩 5 个游资，而热力图有 7 个，
    # 两张图对不上，读者会以为数据算错了。
    # 改成「每个游资各保留金额最大的 3 条」：游资全覆盖，连线数可控，
    # 而且正好回答『这个游资今天主要打哪几只票』。
    TOP_PER_YOUZI = 3
    agg["_rk"] = agg.groupby("游资")["净买入(万)"].rank(ascending=False, method="first")
    agg = agg[agg["_rk"] <= TOP_PER_YOUZI].drop(columns=["_rk"])
    agg = agg.sort_values("净买入(万)", ascending=False)

    stock_labels: Dict[str, str] = {}
    for _, r in agg.iterrows():
        key = f"{r['股票名']}({r['股票码']})" if r["股票码"] else r["股票名"]
        stock_labels[key] = key

    n_nodes = list(agg["游资"].unique()) + list(stock_labels.keys())
    nodes = []
    for n in n_nodes:
        if n in youzi_net:
            nodes.append({"name": n, "itemStyle": {"color": UP if youzi_net[n] > 0 else DOWN}})
        else:
            nodes.append({"name": n, "itemStyle": {"color": "#3d7fd1"}})

    links = []
    for _, r in agg.iterrows():
        key = f"{r['股票名']}({r['股票码']})" if r["股票码"] else r["股票名"]
        links.append({
            "source": r["游资"],
            "target": key,
            "value": round(float(r["净买入(万)"]), 1),
            "lineStyle": {"color": "gradient", "opacity": 0.28},
        })

    sk = (
        Sankey(init_opts=opts.InitOpts(bg_color="transparent"))
        .add(
            "净买入流向",
            nodes,
            links,
            pos_left="6%",
            pos_right="24%",
            pos_top="20%",
            pos_bottom="7%",
            node_width=13,
            # node_gap 从 10 提到 15：右侧十几只股票的流量差异极大，
            # 小流量节点高度不足 10px，标签会叠在一起；加大间距是唯一有效的缓解。
            node_gap=15,
            is_draggable=True,
            linestyle_opt=opts.LineStyleOpts(opacity=0.28, curve=0.5, color="gradient"),
            label_opts=opts.LabelOpts(
                position="right", color=TEXT, font_size=10.5,
            ),
            itemstyle_opts=opts.ItemStyleOpts(border_width=0),
        )
        .set_global_opts(
            title_opts=_title("游资买入流向", "左=游资 · 右=个股 · 线宽=净买入金额（万元）"),
            tooltip_opts=_tooltip(
                "item",
                js(
                    "function(p){"
                    "  if (p.dataType === 'edge') {"
                    "    return p.data.source + ' → ' + p.data.target + '<br/>净买入 <b>' + p.data.value + ' 万元</b>';"
                    "  }"
                    "  return '<b>' + p.name + '</b>';"
                    "}"
                ),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
        )
    )
    return dump_options(sk)


# ---------------------------------------------------------------- 图④ 游资热力图
def chart_youzi_heatmap(df: pd.DataFrame, top_stocks: int = 18) -> str:
    """
    热力图：行=游资，列=个股，颜色=净买卖金额（红买 / 绿卖），空白=未参与。

    这张图回答的是桑基图回答不了的问题：
        「红太阳这只票上，谁在买、谁在卖、对打了多少」
    多空对打是超短线最有价值的信息之一，只有正负同图才看得出来。
    桑基的流量必须为正，天然表达不了这一层。

    标签处理（第一版踩的坑）
    ------------------------
    直接把 '上纬新材(688585)' 当横轴标签，18 列挤在 1450px 里，
    ECharts 会自动隐藏一半标签，剩下的还被截断——读者根本对不上列。
    改成：横轴只显示股票名（代码进 tooltip），字号 10、旋转 34°、interval=0 强制全显。
    纵轴同理，把 '拉萨天团(散户/对倒)' 这种带说明的名字压成 '拉萨天团'。
    """
    seat = _seat_only(df)
    if seat.empty:
        return _empty_option(
            "无个股席位数据 —— 热力图需要「游资×股票×金额」三元组\n"
            "请去掉 --no-seat 重新运行"
        )

    d = seat.copy()
    d["净买入(万)"] = pd.to_numeric(d["净买入(万)"], errors="coerce").fillna(0.0)
    d["股票名"] = d["操作股票"].map(lambda x: _parse_stock(x)[0])
    d["股票码"] = d["操作股票"].map(lambda x: _parse_stock(x)[1])
    d["股票全称"] = d.apply(
        lambda r: f"{r['股票名']}({r['股票码']})" if r["股票码"] else r["股票名"], axis=1
    )

    pivot = d.pivot_table(index="游资", columns="股票全称", values="净买入(万)",
                          aggfunc="sum", fill_value=None)

    # 列：按该股涉及金额总量取前 N，否则格子会被压成一条线
    col_score = d.groupby("股票全称")["净买入(万)"].apply(lambda x: x.abs().sum())
    cols = col_score.sort_values(ascending=False).head(top_stocks).index.tolist()
    # 行：按净额绝对值排序
    rows = d.groupby("游资")["净买入(万)"].sum().abs().sort_values(ascending=False).index.tolist()

    pivot = pivot.reindex(index=rows, columns=cols)

    # 显示名：股票去代码（重名才补末两位），游资去括号说明
    col_short = {}
    used = set()
    for key in cols:
        nm, code = _parse_stock(key)
        disp = nm
        if disp in used:
            disp = f"{nm}{code[-2:]}" if code else nm
        used.add(disp)
        col_short[key] = disp

    row_short = [re.sub(r"[（(].*?[）)]$", "", str(r)) for r in rows]

    data = []
    vmax = 1.0
    for yi, youzi in enumerate(rows):
        for xi, stock in enumerate(cols):
            v = pivot.at[youzi, stock] if (youzi in pivot.index and stock in pivot.columns) else None
            if v is None or pd.isna(v):
                continue
            v = round(float(v), 1)
            data.append([xi, yi, v])
            vmax = max(vmax, abs(v))

    if not data:
        return _empty_option("席位数据无法构成矩阵")

    hm = (
        HeatMap(init_opts=opts.InitOpts(bg_color="transparent"))
        .add_xaxis([col_short[c] for c in cols])
        .add_yaxis(
            "净买入(万)",
            row_short,
            data,
            label_opts=opts.LabelOpts(
                is_show=True, color=TEXT, font_size=10,
                formatter=js(
                    "function(p){"
                    "  var v = p.value[2];"
                    "  var a = Math.abs(v);"
                    "  var s = a >= 10000 ? (a / 10000).toFixed(2) + '亿' : Math.round(a) + '万';"
                    "  return (v > 0 ? '+' : '-') + s;"
                    "}"
                ),
            ),
        )
        .set_global_opts(
            title_opts=_title("游资 × 个股 买卖矩阵",
                              "红=净买入 · 绿=净卖出 · 空白=该席位未参与该股 · 悬停看全称"),
            xaxis_opts=opts.AxisOpts(
                type_="category",
                splitarea_opts=opts.SplitAreaOpts(is_show=False),
                axislabel_opts=opts.LabelOpts(
                    color=MUTED, font_size=10, rotate=34, interval=0,
                ),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color=BORDER)),
                axistick_opts=opts.AxisTickOpts(is_show=False),
                splitline_opts=opts.SplitLineOpts(is_show=False),
            ),
            yaxis_opts=opts.AxisOpts(
                type_="category",
                splitarea_opts=opts.SplitAreaOpts(is_show=False),
                axislabel_opts=opts.LabelOpts(color=MUTED, font_size=11),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color=BORDER)),
                axistick_opts=opts.AxisTickOpts(is_show=False),
                splitline_opts=opts.SplitLineOpts(is_show=False),
            ),
            tooltip_opts=_tooltip(
                "item",
                js(
                    "function(p){"
                    f"  var keys = {json.dumps(cols, ensure_ascii=False)};"
                    "  var v = p.value[2];"
                    "  var col = v > 0 ? '#f6465d' : '#0ecb81';"
                    "  var a = Math.abs(v);"
                    "  var amt = a >= 10000 ? (a / 10000).toFixed(2) + ' 亿元' : a.toFixed(0) + ' 万元';"
                    "  return '<b>' + p.value[1] + '</b><br/>' + keys[p.value[0]] + '<br/>' +"
                    "         '净额 <b style=\"color:' + col + '\">' + (v > 0 ? '+' : '-') + amt + '</b>';"
                    "}"
                ),
            ),
            visualmap_opts=opts.VisualMapOpts(
                is_show=True,
                min_=-vmax,
                max_=vmax,
                range_color=[DOWN, "#1b2431", UP],
                pos_right="1%",
                pos_bottom="10%",
                item_width=11,
                item_height=90,
                is_calculable=True,
                textstyle_opts=opts.TextStyleOpts(color=MUTED, font_size=10),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
        )
    )
    # 旋转后的 X 轴标签由 containLabel 自动预留空间，
    # 这里再把 bottom 收紧，避免矩阵被挤成细细一条、下方留一大片空白。
    _with_grid(hm, top="19%", right="7%", bottom="9%", left="5%")
    return dump_options(hm)


def _empty_option(msg: str) -> str:
    """
    空数据占位图。

    【为什么不是直接 return None】
    浏览器端每个容器都要 setOption，否则会报 "Initialize failed"。
    给一个只显示文字的合法 option，比留白更清楚：读者能看到「为什么没数据」。
    """
    opt = {
        "backgroundColor": "transparent",
        "title": {
            "text": msg,
            "left": "center",
            "top": "middle",
            "textStyle": {"color": MUTED, "fontSize": 13, "fontWeight": "normal", "lineHeight": 22},
        },
    }
    return json.dumps(opt, ensure_ascii=False)


# ==============================================================================
# 4. HTML 渲染
# ==============================================================================
def _echarts_js() -> str:
    """取 ECharts 库源码：优先本地 assets/，缺失则下载，全失败则退回 CDN 引用。"""
    if ASSET_JS.exists() and ASSET_JS.stat().st_size > 500_000:
        return ASSET_JS.read_text(encoding="utf-8", errors="ignore")

    ASSET_JS.parent.mkdir(parents=True, exist_ok=True)
    for url in (
        "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js",
        "https://unpkg.com/echarts@5.5.0/dist/echarts.min.js",
        "https://registry.npmmirror.com/echarts/5.5.0/files/dist/echarts.min.js",
    ):
        try:
            logger.info("正在下载 ECharts 到本地：%s", url)
            with urllib.request.urlopen(url, timeout=30) as r:
                raw = r.read()
            if len(raw) > 500_000:
                ASSET_JS.write_bytes(raw)
                return raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            logger.warning("下载失败 %s：%s", url, exc)

    logger.warning("ECharts 本地化失败，HTML 将引用 CDN（断网时图表不显示）")
    return ""


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>__TITLE__</title>
<style>
  :root{
    --bg:__BG__; --panel:__PANEL__; --panel2:__PANEL2__; --border:__BORDER__;
    --text:__TEXT__; --muted:__MUTED__; --up:__UP__; --down:__DOWN__;
    --gold:__GOLD__; --blue:__BLUE__;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(246,70,93,.07), transparent 60%),
      radial-gradient(900px 500px at 85% 0%, rgba(76,154,255,.07), transparent 55%),
      var(--bg);
    color:var(--text);
    font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    font-size:14px;line-height:1.6;
    padding:26px 30px 40px;
    min-height:100vh;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:1480px;margin:0 auto}

  /* ---------- 页头 ---------- */
  header{display:flex;flex-wrap:wrap;align-items:flex-end;gap:16px;
         padding-bottom:18px;border-bottom:1px solid var(--border);margin-bottom:22px}
  .brand{font-size:23px;font-weight:700;letter-spacing:.5px}
  .brand .accent{color:var(--up)}
  .date{font-size:15px;color:var(--muted);font-variant-numeric:tabular-nums}
  .date b{color:var(--text);font-size:17px}
  .spacer{flex:1}
  .badges{display:flex;flex-wrap:wrap;gap:8px}
  .badge{font-size:11.5px;padding:4px 10px;border-radius:999px;
         border:1px solid var(--border);background:var(--panel2);color:var(--muted);
         white-space:nowrap}
  .badge b{color:var(--text)}
  .badge.ok{border-color:rgba(14,203,129,.4);color:#7ee7bb}
  .badge.warn{border-color:rgba(240,185,11,.4);color:#f0c75b}

  /* ---------- KPI ---------- */
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));
        gap:13px;margin-bottom:20px}
  .kpi{background:linear-gradient(160deg,var(--panel),var(--bg));
       border:1px solid var(--border);border-radius:13px;padding:15px 17px;
       min-height:116px;
       position:relative;overflow:hidden;transition:transform .18s,border-color .18s}
  .kpi:hover{transform:translateY(-2px);border-color:#33445c}
  .kpi::after{content:"";position:absolute;left:0;top:0;width:3px;height:100%;background:var(--accent,var(--border))}
  .kpi .k{font-size:11.5px;color:var(--muted);letter-spacing:.4px;margin-bottom:7px}
  .kpi .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.15}
  .kpi .v small{font-size:13px;font-weight:500;color:var(--muted);margin-left:4px}
  .kpi .s{font-size:11.5px;color:var(--muted);margin-top:6px;
         display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
         overflow:hidden;line-height:1.5}
  .kpi .s em{font-style:normal;color:var(--text)}
  .up{color:var(--up)} .down{color:var(--down)} .gold{color:var(--gold)}
  .blue{color:var(--blue)} .grey{color:var(--muted)}

  /* ---------- 卡片 ---------- */
  .card{background:var(--panel);border:1px solid var(--border);border-radius:14px;
        padding:14px 16px 8px;margin-bottom:16px;
        box-shadow:0 2px 18px rgba(0,0,0,.22)}
  .chart{width:100%}
  .h400{height:400px} .h340{height:340px} .h460{height:460px} .h430{height:430px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:1080px){.grid2{grid-template-columns:1fr}}

  /* ---------- 表格 ---------- */
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:left;color:var(--muted);font-weight:500;font-size:11.5px;
     padding:9px 8px;border-bottom:1px solid var(--border);letter-spacing:.3px;
     white-space:nowrap}
  td{padding:9px 8px;border-bottom:1px solid rgba(36,48,64,.55);
     font-variant-numeric:tabular-nums;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tbody tr{transition:background .15s}
  tbody tr:hover{background:rgba(76,154,255,.055)}
  .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;color:var(--muted)}
  .tag{display:inline-block;font-size:10.5px;padding:1.5px 7px;border-radius:999px;
       border:1px solid var(--border);color:var(--muted);margin-right:4px;white-space:nowrap}
  .tag.up{border-color:rgba(246,70,93,.45);color:#ff8b9e;background:rgba(246,70,93,.08)}
  .tag.down{border-color:rgba(14,203,129,.45);color:#68e3b0;background:rgba(14,203,129,.08)}
  .tag.seat{border-color:rgba(76,154,255,.45);color:#8fbfff;background:rgba(76,154,255,.08)}

  .note{font-size:11.5px;color:var(--muted);padding:9px 4px 6px;line-height:1.75}
  .note code{background:var(--panel2);padding:1px 5px;border-radius:4px;
             font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#9fb4cc}
  h3.sec{font-size:13px;color:var(--muted);font-weight:600;letter-spacing:.6px;
         margin:0 0 10px;text-transform:uppercase}
  footer{margin-top:26px;padding-top:16px;border-top:1px solid var(--border);
         color:var(--muted);font-size:11.5px;display:flex;flex-wrap:wrap;gap:12px}
  footer .spacer{flex:1}
  .warnbox{border-left:3px solid var(--gold);background:rgba(240,185,11,.06);
           padding:10px 14px;border-radius:0 8px 8px 0;font-size:12px;
           color:#e8d18a;margin-bottom:16px;line-height:1.75}
</style>
</head>
<body>
<div class="wrap">
__BODY__
</div>
<script>__ECHARTS__</script>
<script>
(function(){
  var charts = [];
  function boot(){
    if(typeof echarts === 'undefined'){ return; }
__INIT__
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
</body>
</html>
"""


def _fmt(v: Any, digits: int = 2, sign: bool = False) -> str:
    """数值格式化，None/NaN 显示为破折号而不是 'nan' 或 'None 万'。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(f):
        return "—"
    return f"{f:+.{digits}f}" if sign else f"{f:.{digits}f}"


def _clip(text: Any, limit: int) -> str:
    """
    按「显示宽度」截断中文文本。

    【为什么不是 len()】中文一个字占两个字符宽，"炸板率 44.90% 偏高但赚钱效应…"
    用 len() 算 30 个字符其实是 60 个显示宽度，卡片照样被撑爆。
    这里把非 ASCII 字符按 2 倍宽度估算，再截断补省略号。
    """
    t = str(text)
    width = 0
    out = []
    for ch in t:
        width += 2 if ord(ch) > 127 else 1
        if width > limit * 2:
            return "".join(out) + "…"
        out.append(ch)
    return t


def _kpi(label: str, value: str, sub: str = "", accent: str = BORDER, cls: str = "") -> str:
    sub_html = f'<div class="s">{sub}</div>' if sub else ""
    return (
        f'<div class="kpi" style="--accent:{accent}">'
        f'<div class="k">{label}</div>'
        f'<div class="v {cls}">{value}</div>'
        f"{sub_html}</div>"
    )


def render_body(
    day: str,
    res: Dict[str, Any],
    hist: pd.DataFrame,
    youzi_df: pd.DataFrame,
    seat_note: str,
    health: Dict[str, Any],
    source_tag: str,
) -> str:
    """拼页面主体（不含 <script>）。图表初始化代码在 render_html 里统一拼。"""
    e = res["emotion"]
    status = str(e.get("status", "震荡"))
    s_color = STATUS_COLOR.get(status, MUTED)

    # ---------- 页头 ----------
    ok_cnt = sum(1 for v in health.values() if v.get("ok")) if health else 0
    tot_cnt = len(health) if health else 0
    hi = ""
    if tot_cnt:
        cls = "ok" if ok_cnt == tot_cnt else "warn"
        hi = f'<span class="badge {cls}">数据源 <b>{ok_cnt}/{tot_cnt}</b></span>'
    actual = hist.attrs.get("actual", len(hist))
    req = hist.attrs.get("requested", DEFAULT_DAYS)
    win_note = (
        f'<span class="badge">情绪周期窗口 <b>{actual}</b> 日'
        + (f'（请求 {req}）' if req != actual else "")
        + "</span>"
    )

    header = f"""
<header>
  <div>
    <div class="brand">A股超短线 · <span class="accent">情绪仪表盘</span></div>
    <div class="date" style="margin-top:5px">交易日 <b>{day[:4]}-{day[4:6]}-{day[6:]}</b>
      　·　生成 {datetime_now()}</div>
  </div>
  <div class="spacer"></div>
  <div class="badges">
    <span class="badge" style="border-color:{s_color};color:{s_color}">情绪 <b>{e.get('status_label', status)}</b></span>
    {hi}{win_note}
    <span class="badge">口径 <b>{source_tag}</b></span>
  </div>
</header>"""

    # ---------- KPI ----------
    zb_rate = float(e.get("zb_rate", 0) or 0)
    avg_pct = float(e.get("avg_prev_zt_pct", 0) or 0)
    # 炸板率用「风险色」：高=橙红(封不住)，低=绿(封得住)。与红涨绿跌无冲突，因为不是价格方向
    zb_color = UP if zb_rate >= 40 else (GOLD if zb_rate >= 30 else DOWN)
    pct_color = UP if avg_pct > 0 else (DOWN if avg_pct < 0 else MUTED)

    kpis = f"""
<div class="kpis">
  {_kpi("情绪档位", f'<span style="color:{s_color}">{status}</span>',
        _clip(e.get("status_reason", "") or "—", 30), s_color)}
  {_kpi("真实涨停 / 有效", f'{e.get("total_zt", 0)}<small>/ {e.get("valid_zt", 0)}</small>',
        f'剔除次新 {e.get("new_stock_cnt", 0)} 家 · 跌停 {e.get("dt_cnt", 0)} 家', UP)}
  {_kpi("炸板率", f'{zb_rate:.1f}<small>%</small>',
        f'封板率 <em>{_fmt(e.get("seal_rate"), 1)}%</em> · 炸板 {e.get("total_zb", 0)} 家',
        zb_color, "up" if zb_rate >= 40 else ("gold" if zb_rate >= 30 else "down"))}
  {_kpi("打板赚钱效应", f'{avg_pct:+.2f}<small>%</small>',
        f'上涨占比 <em>{_fmt(e.get("prev_zt_up_ratio"), 1)}%</em> · 晋级率 <em>{_fmt(e.get("prev_zt_lu_ratio"), 1)}%</em>',
        pct_color, "up" if avg_pct > 0 else ("down" if avg_pct < 0 else "grey"))}
  {_kpi("最高连板", f'{e.get("max_board", 0)}<small>板</small>',
        "、".join(e.get("max_board_stocks", []) or [])[:26] or "—", GOLD, "gold")}
  {_kpi("明日仓位建议",
        f'<span style="font-size:15px">{str(e.get("position", "—")).split("（")[0]}</span>',
        str(e.get("position", "")).split("（")[-1].rstrip("）") if "（" in str(e.get("position", "")) else "—",
        BLUE, "")}
</div>"""

    # ---------- 警告条 ----------
    warns = e.get("warnings") or []
    warn_html = ""
    if warns:
        items = "".join(f"<div>{w}</div>" for w in warns)
        warn_html = f'<div class="warnbox">{items}</div>'

    # ---------- 图表容器 ----------
    charts_html = f"""
<div class="card"><div id="c_emotion" class="chart h400"></div></div>

<div class="grid2">
  <div class="card"><div id="c_ladder" class="chart h340"></div></div>
  <div class="card">
    <h3 class="sec">主线板块</h3>
    {_render_sector_table(res)}
  </div>
</div>

<div class="card"><div id="c_sankey" class="chart h460"></div>
  <div class="note">口径：{seat_note}。桑基仅画净买入方向，净卖出见下方矩阵。</div>
</div>

<div class="card"><div id="c_heat" class="chart h430"></div></div>

<div class="card">
  <h3 class="sec">游资席位明细</h3>
  {_render_youzi_table(youzi_df)}
  <div class="note">金额单位：万元。来源标注 <span class="tag seat">席位</span>
    为「该股票单票口径」，可跨游资横向比较。</div>
</div>

<div class="card">
  <h3 class="sec">明日盯盘清单</h3>
  {_render_watch_table(res)}
</div>"""

    footer = f"""
<footer>
  <div>数据源：AKShare（东方财富公开接口） · 快照缓存于 <code>cache/</code></div>
  <div class="spacer"></div>
  <div>本页由程序自动生成，不构成任何投资建议。股市有风险，入市需谨慎。</div>
</footer>"""

    return header + warn_html + kpis + charts_html + footer


def _render_sector_table(res: Dict[str, Any]) -> str:
    """主线板块表：让读者看到「第一主线占多少」而不是只给一个名字。"""
    md = res.get("main_sector", pd.DataFrame())
    conc = res.get("concentration", {}) or {}
    if md is None or md.empty:
        return '<div class="note">今日无板块数据。</div>'

    rows = ""
    for _, r in md.head(6).iterrows():
        is_main = bool(r.get("是主线", False))
        star = " ★" if is_main else ""
        color = UP if is_main else MUTED
        rows += (
            f"<tr><td style='color:{color};font-weight:600'>{r.get('板块', '-')}{star}</td>"
            f"<td style='text-align:right'>{int(r.get('涨停家数', 0))}</td>"
            f"<td style='text-align:right'>{_fmt(r.get('占比(%)'), 1)}%</td>"
            f"<td style='text-align:right'>{int(r.get('最高连板', 0))} 板</td>"
            f"<td class='mono'>{str(r.get('龙头股', '-'))[:12]}</td></tr>"
        )

    level = conc.get("level", "-")
    desc = conc.get("desc", "")
    lv_color = {"高集中": UP, "中集中": GOLD, "低集中": DOWN}.get(level, MUTED)
    return f"""
<table>
  <thead><tr><th>板块</th><th style="text-align:right">涨停</th>
    <th style="text-align:right">占比</th><th style="text-align:right">最高</th><th>龙头</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<div class="note">集中度 <b style="color:{lv_color}">{level}</b> —— {desc}</div>"""


def _render_youzi_table(df: pd.DataFrame, top: int = 14) -> str:
    """席位明细表。按金额绝对值排序，买卖同表（用颜色区分）。"""
    if df is None or df.empty:
        return '<div class="note">今日未命中知名游资席位，或龙虎榜数据缺失。</div>'

    d = df.copy()
    d["净买入(万)"] = pd.to_numeric(d["净买入(万)"], errors="coerce").fillna(0.0)
    d["_abs"] = d["净买入(万)"].abs()
    d = d.sort_values("_abs", ascending=False).head(top)

    rows = ""
    for _, r in d.iterrows():
        amt = float(r["净买入(万)"])
        color = UP if amt > 0 else DOWN
        icon = "▲" if amt > 0 else "▼"
        src = str(r.get("来源", ""))
        # 同花顺游资榜也是「单票级」口径（金额精确到单只票），
        # 和东财第二层的席位明细同类，一起标成 seat 样式
        tag_cls = "seat" if src.startswith(("席位", "同花顺游资榜")) else ""
        yyb = str(r.get("营业部", ""))
        yyb_short = yyb if len(yyb) <= 20 else yyb[:20] + "…"
        rows += (
            f"<tr><td><b>{r.get('游资', '-')}</b></td>"
            f"<td class='mono' title='{yyb}'>{yyb_short}</td>"
            f"<td style='text-align:right;color:{color};font-weight:600'>{icon} {amt:+,.0f}</td>"
            f"<td>{str(r.get('操作股票', '-'))[:22]}</td>"
            f"<td><span class='tag {tag_cls}'>{src}</span></td></tr>"
        )
    return f"""
<table>
  <thead><tr><th>游资</th><th>营业部</th><th style="text-align:right">净额(万)</th>
    <th>操作股票</th><th>来源</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""


def _render_watch_table(res: Dict[str, Any]) -> str:
    """盯盘清单。"""
    w = res.get("watchlist", pd.DataFrame())
    if w is None or w.empty:
        return '<div class="note">今日无可盯盘标的。</div>'
    rows = ""
    for _, r in w.iterrows():
        lv = int(r.get("连板数", 1))
        color = UP if lv >= 3 else (GOLD if lv == 2 else MUTED)
        rows += (
            f"<tr><td class='mono'>{r.get('代码', '')}</td>"
            f"<td><b>{r.get('名称', '')}</b></td>"
            f"<td style='text-align:right;color:{color};font-weight:600'>{lv} 板</td>"
            f"<td>{r.get('所属行业', '-')}</td>"
            f"<td style='text-align:right'>{float(r.get('封板资金', 0) or 0)/1e8:.2f} 亿</td>"
            f"<td class='mono'>{r.get('盯盘理由', '-')}</td></tr>"
        )
    return f"""
<table>
  <thead><tr><th>代码</th><th>名称</th><th style="text-align:right">连板</th>
    <th>行业</th><th style="text-align:right">封单</th><th>盯盘理由</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""


def datetime_now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def render_html(
    day: str,
    res: Dict[str, Any],
    hist: pd.DataFrame,
    youzi_df: pd.DataFrame,
    seat_note: str,
    health: Dict[str, Any],
    source_tag: str,
) -> str:
    """组装完整 HTML（含内联 ECharts 与初始化脚本）。"""
    body = render_body(day, res, hist, youzi_df, seat_note, health, source_tag)

    chart_js = [
        ("c_emotion", chart_emotion(hist)),
        ("c_ladder", chart_ladder(res.get("zt", pd.DataFrame()))),
        ("c_sankey", chart_youzi_sankey(youzi_df)),
        ("c_heat", chart_youzi_heatmap(youzi_df)),
    ]

    init_lines = []
    for dom_id, opt in chart_js:
        # 用 JSON.parse 传 option：比直接内联对象字面量更耐受特殊字符
        init_lines.append(
            f"    (function(){{"
            f"var el = document.getElementById('{dom_id}');"
            f"if(!el) return;"
            f"var c = echarts.init(el, null, {{renderer:'canvas'}});"
            f"c.setOption({opt});"
            f"charts.push(c);"
            f"}})();"
        )

    init = "\n".join(init_lines) + """
    window.addEventListener('resize', function(){
      charts.forEach(function(c){ try{ c.resize(); }catch(e){} });
    });
"""

    echarts_src = _echarts_js()
    script_tag = (
        echarts_src
        if echarts_src
        else "/* ECharts 本地化失败，请手动下载 echarts.min.js 到 assets/ */"
    )

    html = (
        HTML_TEMPLATE
        .replace("__TITLE__", f"A股复盘仪表盘 {day}")
        .replace("__BG__", BG).replace("__PANEL__", PANEL).replace("__PANEL2__", PANEL_2)
        .replace("__BORDER__", BORDER).replace("__TEXT__", TEXT).replace("__MUTED__", MUTED)
        .replace("__UP__", UP).replace("__DOWN__", DOWN).replace("__GOLD__", GOLD)
        .replace("__BLUE__", BLUE)
        .replace("__BODY__", body)
        .replace("__INIT__", init)
        .replace("__ECHARTS__", script_tag)
    )
    return html


# ==============================================================================
# 5. 主流程
# ==============================================================================
def run(day: str, days: int = DEFAULT_DAYS, use_seat: bool = True,
        open_browser: bool = True) -> Optional[Path]:
    """生成仪表盘 HTML 并（可选）打开浏览器。"""
    logger.info("=" * 68)
    logger.info("📊 生成仪表盘：%s", day)
    logger.info("=" * 68)

    data = load_day(day)
    if data["zt"].empty:
        logger.error("❌ %s 涨停池为空，无法生成仪表盘。"
                     "（检查 cache/%s_zt_pool.csv 是否存在）", day, day)
        return None

    # ① 当日分析（复用 analyzer，口径与 Markdown 报告完全一致）
    res = analyzer.analyze(data)
    res["zt"] = data["zt"]           # analyze 内部用的是清洗后的表，这里显式挂回去供画图用
    e = res["emotion"]
    logger.info("情绪：%s ｜ 涨停 %s ｜ 炸板率 %.1f%% ｜ 赚钱效应 %+.2f%%",
                e.get("status"), e.get("total_zt"), float(e.get("zb_rate", 0)),
                float(e.get("avg_prev_zt_pct", 0)))

    # ② 历史序列
    hist = build_history(days)
    logger.info("情绪周期：实际 %d 日（请求 %d 日；cache 重算 %d 日）",
                hist.attrs.get("actual", 0), days, hist.attrs.get("cached_cnt", 0))
    if hist.attrs.get("actual", 0) < days:
        logger.warning("可用历史不足 %d 日——东财涨停池接口只保留约 20 个交易日，"
                       "更早日期无法回溯；每天运行 main.py 会累积到 metrics_history.csv",
                       days)

    # ③ 游资
    youzi_df, seat_note = load_youzi(day, use_seat=use_seat)
    logger.info("游资：%d 条记录（%s）", 0 if youzi_df is None else len(youzi_df), seat_note)

    # ④ 健康状态（离线时没有接口健康表，用缓存文件存在性代替）
    health: Dict[str, Any] = {}
    for label, key in (("涨停池", "zt_pool"), ("炸板池", "zb_pool"), ("跌停池", "dt_pool"),
                       ("昨日涨停", "prev_zt_pool"), ("龙虎榜", "lhb"), ("活跃营业部", "active_yyb")):
        health[label] = {"ok": (CACHE_DIR / f"{day}_{key}.csv").exists()}

    source_tag = "离线缓存" if not use_seat else "缓存+联网席位"

    # ⑤ 渲染
    html = render_html(day, res, hist, youzi_df, seat_note, health, source_tag)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"dashboard_{day}.html"
    out.write_text(html, encoding="utf-8")
    size_mb = out.stat().st_size / 1024 / 1024
    logger.info("✅ 已生成：%s（%.2f MB，含内联 ECharts，可离线打开）", out, size_mb)

    if open_browser:
        try:
            webbrowser.open(out.as_uri())
            logger.info("🌐 已在默认浏览器打开")
        except Exception as exc:
            logger.warning("自动打开浏览器失败（文件已生成，可手动打开）：%s", exc)

    return out


def _default_day() -> str:
    """没有指定日期时：取 cache 里最近的交易日。"""
    days = list_cached_days()
    if days:
        return days[-1]
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A股超短线复盘 · 单页 HTML 仪表盘",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python dashboard.py                    # 最近交易日，自动开浏览器
  python dashboard.py 20261109           # 指定日期
  python dashboard.py --days 30          # 情绪周期看更长窗口
  python dashboard.py --no-seat          # 纯离线（快，但无单票级金额）
  python dashboard.py --no-open          # 只生成文件
""",
    )
    parser.add_argument("date", nargs="?", help="交易日 YYYYMMDD，缺省取 cache 中最近一日")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"情绪周期图窗口（默认 {DEFAULT_DAYS}）")
    parser.add_argument("--no-seat", action="store_true",
                        help="跳过联网抓个股席位（只跑活跃营业部第一层）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    args = parser.parse_args()

    setup_logger(args.log_level)
    day = args.date or _default_day()

    try:
        out = run(day, days=args.days, use_seat=not args.no_seat,
                  open_browser=not args.no_open)
    except KeyboardInterrupt:
        logger.warning("已中断")
        raise SystemExit(130)

    if out is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
