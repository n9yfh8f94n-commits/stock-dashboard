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
import premium
import sector_tracker
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


def _tooltip(trigger: str = "axis", formatter: Any = None,
             axis_pointer_type: str | None = None) -> opts.TooltipOpts:
    """
    统一 tooltip 样式。

    axis_pointer_type : None 保持各图原有行为；
                        "shadow" 适合堆叠柱（整列高亮）；
                        "cross" 是十字准星，模块六统一开启。
    """
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
    if axis_pointer_type:
        kw["axis_pointer_type"] = axis_pointer_type
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
                # 十字准星：鼠标移动时同时出现横竖参考线，
                # 方便把「涨停家数」和右轴的「炸板率」对齐读数
                axis_pointer_type="cross",
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


# ---------------------------------------------------------------- 图⑥ 题材生命周期
# 固定调色板：题材数量有限（前 5 + 其他），颜色一旦定死用户就能记住谁是谁。
# 不用 pyecharts 自动配色，因为它会随系列数量变化而错位。
SECTOR_PALETTE = [
    "#f6465d",  # 红
    "#4c9aff",  # 蓝
    "#f0b90b",  # 金
    "#0ecb81",  # 绿
    "#a371f7",  # 紫
    "#ff9f43",  # 橙
    "#2ea0c9",  # 青
    "#e05f8a",  # 粉
]


def chart_sector_life(stack: Dict[str, Any]) -> str:
    """
    题材生命周期（堆叠柱状图）：横轴近 N 日，纵轴涨停家数，颜色=题材。

    为什么主色题材按「近 N 天累计家数」选，而不是「最近一天的前 N 名」：
        后者每天剧烈跳动，图例颜色天天变，读者记不住谁是谁，
        根本看不出某个题材是在连续发酵还是在退潮。
        累计排行稳定得多，颜色一旦定下来就基本不动。

    为什么「其他」用暗灰：
        它不代表某个具体题材，只是长尾合计。
        给它上彩色会误导用户去找一支叫「其他」的行情。
    """
    if not stack or not stack.get("ok"):
        return _empty_option((stack or {}).get("note") or "题材历史数据不足")

    days = stack.get("days") or []
    series = stack.get("series") or []
    if not days or not series:
        return _empty_option("题材历史数据不足")

    x = [f"{str(d)[4:6]}-{str(d)[6:]}" for d in days]

    bar = Bar(init_opts=opts.InitOpts(bg_color="transparent"))
    bar.add_xaxis(x)

    # 颜色按「原序列位置」固定下来，避免把「其他」挪到底部时颜色跟着错位
    color_map: Dict[str, str] = {}
    for i, s in enumerate(series):
        nm = str(s.get("name") or "")
        color_map[nm] = ("rgba(57,65,79,0.34)" if nm == "其他"
                         else SECTOR_PALETTE[i % len(SECTOR_PALETTE)])

    # 「其他」放最前 -> 堆叠时位于最底部，彩色题材浮在上方便于对比
    ordered = sorted(series, key=lambda s: 0 if str(s.get("name")) == "其他" else 1)

    for s in ordered:
        name = str(s.get("name") or "")
        is_other = name == "其他"

        bar.add_yaxis(
            name,
            [int(v) for v in (s.get("data") or [])],
            stack="sec",
            bar_width="54%",
            itemstyle_opts=opts.ItemStyleOpts(color=color_map.get(name, MUTED)),
            label_opts=opts.LabelOpts(
                is_show=not is_other,      # 「其他」不标数字，避免堆叠顶部数字拥挤
                position="inside",
                color="#0b0e14",
                font_size=10,
                font_weight="bold",
            ),
        )

    bar.set_global_opts(
        title_opts=_title("题材生命周期", stack.get("note", "")),
        xaxis_opts=_axis(split=False),
        yaxis_opts=_axis("涨停家数"),
        legend_opts=opts.LegendOpts(
            is_show=True, pos_top=6, pos_left="center", item_gap=14,
            textstyle_opts=opts.TextStyleOpts(color=MUTED, font_size=11),
        ),
        tooltip_opts=_tooltip("axis", axis_pointer_type="cross"),
    )
    # 必须用 dump_options()：它返回纯 option JSON，供 c.setOption(...) 直接消费。
    # 用 render_embed() 会返回一整份 <!DOCTYPE html> 文档，
    # 塞进 setOption 里是语法错误，整个初始化脚本会静默失败、图表区一片空白。
    return dump_options(bar)


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
                # 原来在 pos_bottom="10%"，会和下面新加的缩放滑块打架，
                # 改挂到右上，给滑块让出底部空间
                pos_top="26%",
                item_width=11,
                item_height=90,
                is_calculable=True,
                textstyle_opts=opts.TextStyleOpts(color=MUTED, font_size=10),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
            # 热力图横向是 18 只股票 + 纵向十几个游资，手机上格子会小到看不清。
            # inside：滚轮/双指缩放；slider：底部可见的缩放条。
            # 注意 pyecharts 的参数名是 range_start / range_end，
            # 不是 start / end；而且默认值就是 20~80，
            # 不显式改成 0~100 的话一打开只显示中间 60%，左右两边被裁掉。
            datazoom_opts=[
                opts.DataZoomOpts(type_="inside", xaxis_index=[0],
                                  range_start=0, range_end=100),
                opts.DataZoomOpts(
                    type_="slider", xaxis_index=[0],
                    range_start=0, range_end=100,
                    pos_bottom="1%", pos_left="8%", pos_right="12%",
                    height=16,
                ),
            ],
        )
    )
    # 旋转后的 X 轴标签由 containLabel 自动预留空间，
    # 底部从 9% 放宽到 17%，给缩放滑块腾位置，否则矩阵会被挤扁。
    _with_grid(hm, top="19%", right="7%", bottom="17%", left="5%")
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

  /* ---------- 我的自选 ---------- */
  .wl{margin-bottom:20px}
  .wl-h{display:flex;align-items:center;gap:9px;font-size:14px;font-weight:600;
        margin-bottom:11px;color:var(--text)}
  .wl-h .cnt{font-size:11.5px;font-weight:400;color:var(--muted)}
  .wl-h .star{color:var(--gold)}
  .wl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
  .wl-card{background:linear-gradient(165deg,var(--panel),var(--bg));
           border:1px solid var(--border);border-left:3px solid var(--gold);
           border-radius:12px;padding:13px 15px;
           transition:transform .18s,border-color .18s}
  .wl-card:hover{transform:translateY(-2px);border-color:#3b4d66}
  .wl-card.empty{border-left-color:var(--muted);opacity:.75}
  .wl-top{display:flex;align-items:baseline;gap:8px;margin-bottom:9px;flex-wrap:wrap}
  .wl-name{font-size:16px;font-weight:700}
  .wl-code{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
  .wl-pct{margin-left:auto;font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}
  .wl-tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
  .wl-tag{font-size:11px;padding:2px 8px;border-radius:5px;
          border:1px solid var(--border);background:var(--panel2);color:var(--muted);
          white-space:nowrap}
  .wl-tag.lb{color:var(--up);border-color:rgba(246,70,93,.45)}
  .wl-tag.lb-hi{color:#fff;background:var(--up);border-color:var(--up);font-weight:600}
  .wl-tag.seal-s{color:#7ee7bb;border-color:rgba(14,203,129,.45)}
  .wl-tag.seal-w{color:#f0c75b;border-color:rgba(240,185,11,.45)}
  .wl-tag.lhb{color:var(--blue);border-color:rgba(76,154,255,.45)}
  .wl-row{font-size:12px;color:var(--muted);margin-bottom:5px;
          display:flex;gap:7px;line-height:1.55}
  .wl-row .k{color:#5b6577;flex:0 0 34px}
  .wl-row .v{color:var(--text);flex:1;word-break:break-all}
  .wl-advice{margin-top:10px;padding:7px 10px;border-radius:8px;
             background:rgba(240,185,11,.07);border:1px solid rgba(240,185,11,.2);
             font-size:12px;color:#f0c75b;line-height:1.5}
  .wl-advice.dim{background:rgba(91,101,119,.08);border-color:rgba(91,101,119,.25);
                 color:var(--muted)}
  @media(max-width:640px){.wl-grid{grid-template-columns:1fr}}

  /* ---------- 次日溢价预期（模块三） ---------- */
  .pm{margin-top:13px;border-top:1px solid var(--border);padding-top:12px}
  .pm-h{font-size:13px;font-weight:600;margin-bottom:9px;
        display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
  .pm-sub{font-size:11px;font-weight:400;color:var(--muted)}
  .pm-t{width:100%;border-collapse:collapse;font-size:12px}
  .pm-t th{font-size:11px;color:var(--muted);font-weight:500;text-align:right;
           padding:4px 6px;border-bottom:1px solid var(--border);white-space:nowrap}
  .pm-t th:first-child{text-align:left}
  .pm-t td{padding:5px 6px;text-align:right;font-variant-numeric:tabular-nums;
           border-bottom:1px solid rgba(36,48,64,.45)}
  .pm-t td:first-child{text-align:left;color:var(--text)}
  .pm-t tr:last-child td{border-bottom:none}
  .pm-t tr.best td{background:rgba(240,185,11,.06)}
  .pm-t tr.best td:first-child{color:var(--gold);font-weight:600}
  .pm-t tr.na td{color:var(--muted)}
  .pm-note{margin-top:9px;font-size:11.5px;color:#f0c75b;
           background:rgba(240,185,11,.07);border:1px solid rgba(240,185,11,.2);
           border-radius:7px;padding:7px 10px;line-height:1.5}
  .pm-note.dim{color:var(--muted);background:rgba(91,101,119,.08);
               border-color:rgba(91,101,119,.25)}
  @media(max-width:640px){.pm-t th,.pm-t td{padding:4px 3px;font-size:11px}}

  /* ---------- 卡片 ---------- */
  .card{background:var(--panel);border:1px solid var(--border);border-radius:14px;
        padding:14px 16px 8px;margin-bottom:16px;
        box-shadow:0 2px 18px rgba(0,0,0,.22)}
  .chart{width:100%}
  .h240{height:240px} .h260{height:260px} .h400{height:400px} .h340{height:340px} .h460{height:460px} .h430{height:430px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:1080px){.grid2{grid-template-columns:1fr}}

  /* ---------- 手机端（微信内打开）----------
     430px 下原来的问题：header 是横向 flex，.spacer{flex:1} 把 badges 挤到右侧，
     而 badges 拿到的是「剩余宽度」而不是整行宽度，于是 white-space:nowrap 的
     badge 直接撑破视口，右侧内容被裁掉（截图里 +10.0% 缺了半个就是这原因）。
     解法：小屏把 header 改成纵向排列，并去掉 spacer，让 badges 独占整行自然换行。 */
  @media(max-width:760px){
    /* 兜底：只要有一个元素横向超出，整页会按超宽布局，
       表现为所有卡片右边被裁。先禁止横向滚动，再逐个收窄。 */
    html,body{overflow-x:hidden;max-width:100%}
    body{padding:14px 12px 30px;font-size:13px}
    header{flex-direction:column;align-items:flex-start;gap:10px;padding-bottom:14px;margin-bottom:16px}
    .spacer{display:none}
    .badges{width:100%;gap:6px}
    .badge{font-size:10.5px;padding:3px 8px}
    .brand{font-size:19px}
    .date{font-size:13px}
    .date b{font-size:15px}

    .kpis{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:9px;margin-bottom:15px}
    .kpi{min-height:auto;padding:11px 12px;border-radius:11px}
    .kpi .v{font-size:21px}
    .kpi .s{-webkit-line-clamp:2}

    .card{padding:10px 11px 6px;border-radius:11px;margin-bottom:12px}
    .grid2{grid-template-columns:1fr;gap:11px}

    /* grid / flex 子项默认 min-width:auto，会被最长的那个子内容撑开，
       表现为卡片比视口还宽、右边被裁掉。强制归零才能真正收缩。 */
    .wl-grid{grid-template-columns:1fr}
    .wl-card,.kpi,.card,.wl-top,.wl-row{min-width:0}
    .wl-pct{flex:0 0 auto;font-size:14px}
    .wl-name{font-size:15px}
    /* 涨跌幅独占一行右对齐：420px 级宽度下「名称+代码+涨幅」挤在一行，
       margin-left:auto 会把涨幅顶到卡片外被裁掉 */
    .wl-pct{flex-basis:100%;text-align:right;margin-left:0;margin-top:1px}

    /* 图表降高度：手机上 400px 的图会占满一屏，滚起来很难受 */
    .h400,.h340,.h430,.h460{height:300px}
    .h260{height:210px}

    /* 宽表格（游资席位明细 5 列、盯盘清单 6 列、次日溢价 5 列）在 430px 下必然超宽。
       它们撑破 .card 之后整页会按超宽布局渲染，
       导致所有卡片右边被裁——这就是自选卡涨幅显示不全的真正原因。
       给每个可能超宽的容器加横向滚动，让内容自己滚，不影响外层。 */
    .card{overflow-x:auto;-webkit-overflow-scrolling:touch}
    .card > table,.pm,.pm-t{overflow-x:auto;-webkit-overflow-scrolling:touch}
    .card > table{white-space:nowrap;font-size:11.5px;min-width:0}
    .card > table th,.card > table td{padding:5px 7px}
    .pm-t{display:block;white-space:nowrap}
    .pm-t thead,.pm-t tbody{display:table;width:100%}
  }

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


# ==============================================================================
# 我的自选（模块二）
# ==============================================================================
WATCHLIST_JSON = BASE_DIR / "my_stocks.json"

# 封单质量分档（单位：元）
# 为什么用绝对值而不是「封单 / 流通市值」：
#   东财涨停池的「流通市值」字段在部分个股上为空，比值会变成 NaN，
#   反而制造数据缺口。而对打板来说绝对值已经够判：1 亿以上基本不会当天炸板，
#   3000 万以下在弱市里开板概率明显偏高。
SEAL_STRONG = 1.0e8
SEAL_MEDIUM = 3.0e7


def load_my_stocks() -> List[str]:
    """
    读取 my_stocks.json，返回 6 位股票代码列表。

    同时兼容两种写法：
        ["002578", "000525"]
        {"stocks": ["002578", "000525"]}     # 可加 _readme 等说明字段，适合当模板

    文件不存在 / 解析失败一律返回空列表，绝不抛异常：
    自选看板是增值功能，不能因为它导致整个仪表盘生不出来。
    """
    if not WATCHLIST_JSON.exists():
        return []

    try:
        raw = json.loads(WATCHLIST_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("my_stocks.json 解析失败（自选看板已跳过）：%s", exc)
        return []

    seq = raw.get("stocks") if isinstance(raw, dict) else raw
    if not isinstance(seq, list):
        logger.warning('my_stocks.json 格式不对：应为数组，或 {"stocks": [...]}')
        return []

    out: List[str] = []
    for x in seq:
        s = str(x).strip()
        if re.fullmatch(r"\d{6}", s):
            out.append(s)
        elif s:
            logger.warning("自选股代码格式不对，已跳过：%r（应为 6 位数字）", x)
    return out


def _seal_quality(seal_money: Any) -> Tuple[str, str, str]:
    """封单质量 -> (等级, 颜色, 金额文本)。"""
    try:
        if seal_money is None or pd.isna(seal_money):
            return "无封单", MUTED, ""
        v = float(seal_money)
    except (TypeError, ValueError):
        return "无封单", MUTED, ""

    if v >= SEAL_STRONG:
        return "强封", DOWN, f"{v / 1e8:.2f}亿"
    if v >= SEAL_MEDIUM:
        return "中等", GOLD, f"{v / 1e4:.0f}万"
    return "弱封", MUTED, f"{v / 1e4:.0f}万"


def _sector_formation(zt: pd.DataFrame, sector: str) -> Tuple[int, str]:
    """
    题材阵型：同一题材今天有几家涨停 —— 就是超短线说的「有没有小弟」。

    龙头带着 5 家以上补涨 = 主升阵型，资金认可、次日接力意愿强；
    只有自己一家 = 独苗，一旦断板没有承接盘。
    """
    if not sector or zt is None or zt.empty or "所属行业" not in zt.columns:
        return 0, "无题材信息"

    n = int((zt["所属行业"].astype(str) == str(sector)).sum())
    if n >= 5:
        return n, f"主升阵型（{n} 家）"
    if n >= 3:
        return n, f"成型（{n} 家）"
    if n == 2:
        return n, "双人成行（2 家）"
    return n, "独苗"


def _youzi_for_stock(youzi_df: Optional[pd.DataFrame], code: str) -> List[Dict[str, Any]]:
    """从游资表挑出涉及该股票的记录（操作股票字段形如「名称(代码)」）。"""
    if youzi_df is None or youzi_df.empty or "操作股票" not in youzi_df.columns:
        return []
    try:
        sub = youzi_df[
            youzi_df["操作股票"].astype(str).str.contains(f"({code})", regex=False, na=False)
        ]
    except Exception:
        return []
    return sub.to_dict("records")


def _advice(lianban: int, formation_n: int, youzi_net: float,
            seal_level: str, in_zt: bool) -> Tuple[str, bool]:
    """
    生成「明日操作提示」。返回 (文案, 是否弱化显示)。

    规则依据（超短线段位常识，不是玄学）：
      · 高位板（>=5）最怕两件事 —— 没有补涨小弟、游资兑现离场。
        两条同时成立时，次日高开往往就是出货窗口。
      · 中位板（3-4）是分歧位，看的是「有没有资金愿意在这个位置接」。
      · 二板看题材有没有形成阵型：阵型在，三板概率大；独苗二板多半见光死。
      · 首板只看一件事：有没有游资进场。没有就是资金不认，等二板确认。
      · 封单弱是不分位置的减分项，直接追加提示。
    """
    lb = int(lianban or 1)
    net = float(youzi_net or 0.0)
    has_youzi = abs(net) > 1.0

    if not in_zt:
        return "今日未涨停，不适用打板逻辑", True

    if lb >= 5:
        if formation_n <= 2:
            base = "高位独苗，断板即走"
        elif net < 0:
            base = "游资撤离，谨防高开低走"
        else:
            base = "高位强势，破板减仓"
    elif lb >= 3:
        # 3-4 板也怕独苗：中位板断板往往比高位更突然，因为没有情绪惯性兜着
        if formation_n <= 1:
            base = "孤军深入，断板即走"
        elif net > 0 and formation_n >= 3:
            base = "有资金 + 有阵型，关注晋级"
        elif net < 0:
            base = "游资撤退，谨慎接力"
        elif formation_n >= 3:
            base = "阵型在，等竞价确认"
        else:
            base = "看竞价强度再定"
    elif lb == 2:
        if formation_n >= 3 and seal_level == "强封":
            base = "强封 + 成阵，三板概率大"
        elif formation_n >= 3:
            base = "题材发酵中，看三板"
        else:
            base = "孤票二板，需板块配合"
    else:
        # 首板通常不上龙虎榜，游资记录为空 ≠ 没有资金，
        # 所以这里先用封单强度判断，再看有没有游资进场。
        if net > 0:
            base = "游资进场，看次日承接"
        elif seal_level == "强封" and formation_n >= 3:
            base = "强封 + 阵型完整，看二板"
        elif seal_level == "强封":
            base = "封单够硬，看能否晋级"
        elif has_youzi:
            base = "游资净卖，谨慎"
        else:
            base = "无资金关注，等二板确认"

    if seal_level == "弱封":
        base += "｜封单偏弱"

    return base, False


def _name_from_cache(code: str, limit: int = 12) -> str:
    """
    从历史涨停池缓存里查股票名称（离线可用）。

    为什么需要这一步：
        自选股当天既没涨停、也没上龙虎榜时，涨停池和龙虎榜里都查不到名字，
        页面上只能干巴巴显示「603366」这样的代码，体验很差。
        而用户持有的票大概率前几天涨停过，扫一遍最近的缓存就能找到。
    """
    try:
        files = sorted(CACHE_DIR.glob("*_zt_pool.csv"), reverse=True)[:limit]
    except Exception:
        return ""

    for p in files:
        try:
            df = pd.read_csv(p, dtype={"代码": str}, usecols=["代码", "名称"])
        except Exception:
            continue
        if df is None or df.empty:
            continue
        try:
            hit = df[df["代码"].astype(str).str.zfill(6) == code]
        except Exception:
            continue
        if not hit.empty:
            nm = str(hit.iloc[0]["名称"] or "").strip()
            if nm:
                return nm
    return ""


def build_watchlist(day: str, data: Dict[str, Any],
                    youzi_df: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    """
    组装自选股看板数据，每只股票一条 dict。

    数据来源：
        data["zt"]      涨停池 -> 名称 / 涨跌幅 / 连板数 / 封板资金 / 题材
        data["lhb"]     龙虎榜 -> 是否上榜
        youzi_df        游资表 -> 游资名称 + 净买额
        涨停池自身      -> 同题材涨停家数（阵型）
    """
    codes = load_my_stocks()
    if not codes:
        return []

    zt = data.get("zt")
    lhb = data.get("lhb")
    zt = zt if isinstance(zt, pd.DataFrame) else pd.DataFrame()
    lhb = lhb if isinstance(lhb, pd.DataFrame) else pd.DataFrame()

    # 代码 -> 涨停池行
    zt_idx: Dict[str, Any] = {}
    if not zt.empty and "代码" in zt.columns:
        for _, r in zt.iterrows():
            zt_idx[str(r["代码"]).zfill(6)] = r

    # 龙虎榜：代码集合 + 名称兜底（自选股没涨停时也能显示名字）
    lhb_codes: set = set()
    lhb_names: Dict[str, str] = {}
    if not lhb.empty and "代码" in lhb.columns:
        for _, r in lhb.iterrows():
            c = str(r["代码"]).zfill(6)
            lhb_codes.add(c)
            nm = str(r.get("名称") or "").strip()
            if nm:
                lhb_names[c] = nm

    items: List[Dict[str, Any]] = []
    for code in codes:
        row = zt_idx.get(code)

        # ---- 今天没涨停：仍然展示，并说明为什么不适用 ----
        if row is None:
            fallback_name = lhb_names.get(code, "") or _name_from_cache(code)
            items.append({
                "代码": code,
                "名称": fallback_name,
                "在涨停池": False,
                "涨跌幅": None,
                "连板数": 0,
                "封板资金": None,
                "封单等级": "—",
                "封单颜色": MUTED,
                "封单文本": "",
                "题材": "",
                "阵型家数": 0,
                "阵型": "",
                "上龙虎榜": code in lhb_codes,
                "游资": [],
                "游资净额": 0.0,
                "提示": "今日未涨停，不适用打板逻辑",
                "弱化": True,
            })
            continue

        # ---- 基础字段 ----
        name = str(row.get("名称") or lhb_names.get(code, "")).strip()

        try:
            pct = float(row.get("涨跌幅"))
        except (TypeError, ValueError):
            pct = None

        try:
            lb = int(float(row.get("连板数") or 1))
        except (TypeError, ValueError):
            lb = 1

        seal_money = row.get("封板资金")
        sector = str(row.get("所属行业") or "").strip()

        seal_level, seal_color, seal_text = _seal_quality(seal_money)
        form_n, form_desc = _sector_formation(zt, sector)

        yz = _youzi_for_stock(youzi_df, code)
        yz_net = 0.0
        for r in yz:
            try:
                yz_net += float(r.get("净买入(万)") or 0) * 1e4
            except (TypeError, ValueError):
                pass

        tip, dim = _advice(lb, form_n, yz_net, seal_level, True)

        try:
            seal_val = float(seal_money) if seal_money is not None and not pd.isna(seal_money) else None
        except (TypeError, ValueError):
            seal_val = None

        items.append({
            "代码": code,
            "名称": name,
            "在涨停池": True,
            "涨跌幅": pct,
            "连板数": lb,
            "封板资金": seal_val,
            "封单等级": seal_level,
            "封单颜色": seal_color,
            "封单文本": seal_text,
            "题材": sector,
            "阵型家数": form_n,
            "阵型": form_desc,
            "上龙虎榜": code in lhb_codes or bool(yz),
            "游资": yz,
            "游资净额": yz_net,
            "提示": tip,
            "弱化": dim,
        })

    return items


def render_watchlist(items: List[Dict[str, Any]]) -> str:
    """
    渲染「我的自选」区块。

    没有配置自选股时返回空串，页面完全不留痕迹。
    """
    if not items:
        return ""

    from html import escape as _esc

    cards: List[str] = []
    for it in items:
        in_zt = it["在涨停池"]
        card_cls = "wl-card" if in_zt else "wl-card empty"

        # 涨跌幅
        if it["涨跌幅"] is None:
            pct_html = '<span class="wl-pct grey">—</span>'
        else:
            pv = float(it["涨跌幅"])
            pc = "up" if pv > 0 else ("down" if pv < 0 else "grey")
            pct_html = f'<span class="wl-pct {pc}">{pv:+.2f}%</span>'

        # 标签：连板 / 封单 / 龙虎榜
        tags: List[str] = []
        if in_zt:
            lb = int(it["连板数"])
            lb_cls = "wl-tag lb-hi" if lb >= 5 else "wl-tag lb"
            tags.append(f'<span class="{lb_cls}">{lb}板</span>')

            if it["封单等级"] not in ("无封单", "—"):
                seal_cls = "wl-tag seal-s" if it["封单等级"] == "强封" else "wl-tag seal-w"
                tags.append(
                    f'<span class="{seal_cls}">{it["封单等级"]} {it["封单文本"]}</span>'
                )

        if it["上龙虎榜"]:
            tags.append('<span class="wl-tag lhb">龙虎榜</span>')

        tags_html = "".join(tags) if tags else '<span class="wl-tag">无标签</span>'

        # 题材行
        if in_zt and it["题材"]:
            sector_txt = (f'{_esc(it["题材"])} · {_esc(it["阵型"])}'
                          if it["阵型"] else _esc(it["题材"]))
        else:
            sector_txt = "—"

        # 游资行
        if it["游资"]:
            parts: List[str] = []
            for r in it["游资"][:3]:
                nm = _esc(str(r.get("游资") or "").strip())
                try:
                    amt = float(r.get("净买入(万)") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                sign = "买" if amt > 0 else ("卖" if amt < 0 else "平")
                color = "up" if amt > 0 else ("down" if amt < 0 else "grey")
                parts.append(f'<span class="{color}">{nm} {sign}{abs(amt):.0f}万</span>')
            youzi_txt = "、".join(parts)
            if len(it["游资"]) > 3:
                youzi_txt += f' <span class="grey">等 {len(it["游资"])} 席</span>'
        else:
            youzi_txt = '<span class="grey">无游资记录</span>'

        advice_cls = "wl-advice dim" if it["弱化"] else "wl-advice"

        cards.append(f"""
<div class="{card_cls}">
  <div class="wl-top">
    <span class="wl-name">{_esc(it["名称"]) if it["名称"] else "未命名"}</span>
    <span class="wl-code">{it["代码"]}</span>
    {pct_html}
  </div>
  <div class="wl-tags">{tags_html}</div>
  <div class="wl-row"><span class="k">题材</span><span class="v">{sector_txt}</span></div>
  <div class="wl-row"><span class="k">游资</span><span class="v">{youzi_txt}</span></div>
  <div class="{advice_cls}">💡 {_esc(it["提示"])}</div>
</div>""")

    return f"""
<section class="wl">
  <div class="wl-h"><span class="star">&#9733;</span> 我的自选
    <span class="cnt">{len(items)} 只 · 配置见 my_stocks.json</span>
  </div>
  <div class="wl-grid">{''.join(cards)}
  </div>
</section>"""


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
    watch_items: Optional[List[Dict[str, Any]]] = None,
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

    # ---------- 我的自选（模块二）----------
    # 放在 KPI 之前：用户最关心的是自己持仓/盯盘的那几只，
    # 先看它们的封单、阵型、游资，再往下看全市场概览。
    # 没有配置自选股时 render_watchlist 返回空串，页面不留痕迹。
    watch_html = render_watchlist(watch_items or [])

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

<div class="card"><div id="c_sector_life" class="chart h340"></div></div>

<div class="grid2">
  <div class="card">
    <div id="c_ladder" class="chart h260"></div>
    {_render_premium_table(res.get("premium"))}
  </div>
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

    # 我的自选紧跟在页头之后、KPI 之前：
    # 用户打开页面第一眼要看到的是自己关注的票，而不是全市场汇总。
    return header + watch_html + warn_html + kpis + charts_html + footer


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


def _render_premium_table(stats: Dict[str, Any]) -> str:
    """
    渲染「次日溢价预期」表格（模块三），嵌在连板天梯图的卡片下方。

    设计取舍：
      · 数据不足时只显示一行灰底提示，不占版面、不抛异常
      · 用「中位数」和「上涨概率」两列，而不只给均值——
        涨停次日的收益分布右偏，均值容易被少数妖股拉高
      · 盈亏比最优的那一档整行高亮，一眼能看到结论
    """
    from html import escape as _esc2

    if not stats:
        return ""

    days_used = stats.get("days_used", 0)
    total = stats.get("total_samples", 0)
    sub = f"近 {days_used} 日 · {total} 样本 · 收盘口径"
    head = f'<div class="pm-h">次日溢价预期<span class="pm-sub">{sub}</span></div>'

    # ---- 数据不足：只给提示 ----
    if not stats.get("ok"):
        note = _esc2(str(stats.get("note") or "历史样本不足，暂无法统计。"))
        return f'<div class="pm">{head}<div class="pm-note dim">{note}</div></div>'

    best = stats.get("best") or ""
    rows: List[str] = []

    for g in stats.get("groups", []):
        label = g.get("label", "?")
        n = int(g.get("n") or 0)

        if n == 0:
            rows.append(
                f'<tr class="na"><td>{label}</td><td>0</td>'
                f'<td>—</td><td>—</td><td>—</td></tr>'
            )
            continue

        avg = float(g.get("avg") or 0.0)
        med = float(g.get("median") or 0.0)
        win = float(g.get("win_rate") or 0.0)

        avg_c = "up" if avg > 0 else ("down" if avg < 0 else "grey")
        med_c = "up" if med > 0 else ("down" if med < 0 else "grey")
        win_c = "up" if win >= 50 else "down"

        # 样本不足的档打个星号，避免用户拿它当结论
        flag = "" if g.get("enough") else '<span class="grey"> *</span>'
        cls = ' class="best"' if label == best else ""

        rows.append(
            f'<tr{cls}><td>{label}{flag}</td><td>{n}</td>'
            f'<td class="{avg_c}">{avg:+.2f}%</td>'
            f'<td class="{med_c}">{med:+.2f}%</td>'
            f'<td class="{win_c}">{win:.1f}%</td></tr>'
        )

    table = (
        '<table class="pm-t">'
        '<thead><tr><th>连板高度</th><th>样本</th><th>平均</th>'
        '<th>中位数</th><th>上涨概率</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )

    summary = premium.summarize(stats)
    note_html = f'<div class="pm-note">&#128161; {_esc2(summary)}</div>' if summary else ""

    return f'<div class="pm">{head}{table}{note_html}</div>'


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
    watch_items: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """组装完整 HTML（含内联 ECharts 与初始化脚本）。"""
    body = render_body(day, res, hist, youzi_df, seat_note, health, source_tag, watch_items)

    chart_js = [
        ("c_emotion", chart_emotion(hist)),
        ("c_sector_life", chart_sector_life(res.get("sector_stack"))),
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

    # 模块三：次日溢价预期。读 cache 里的「昨日涨停池」快照，纯离线统计。
    # 挂在 res 上而不是走函数参数，避免再改一遍 render 的签名。
    res["premium"] = premium.build_premium_stats(30)
    if res["premium"].get("ok"):
        logger.info("次日溢价：%d 样本 / %d 日快照，盈亏比最优档 = %s",
                    res["premium"].get("total_samples", 0),
                    res["premium"].get("days_used", 0),
                    res["premium"].get("best") or "—")
    else:
        logger.warning("次日溢价：样本不足，页面将显示提示（%s）",
                       res["premium"].get("note", "")[:60])

    # 模块四：题材生命周期。先幂等补齐历史，再取最近 10 天给堆叠图。
    # 用 try 包住：题材统计出问题不能影响其他图表，页面必须还能出来。
    try:
        sector_tracker.update_history(60)
        res["sector_stack"] = sector_tracker.build_stack_series(10)
        _st = res["sector_stack"]
        if _st.get("ok"):
            logger.info("题材生命周期：%d 日 / %d 个系列（%s）",
                        len(_st.get("days", [])), len(_st.get("series", [])),
                        "、".join(s["name"] for s in _st.get("series", [])))
        else:
            logger.warning("题材生命周期：%s", str(_st.get("note", ""))[:60])
    except Exception as exc:
        logger.warning("题材生命周期统计失败（不影响其他图表）：%s", exc)
        res["sector_stack"] = {"ok": False, "days": [], "series": [],
                               "note": "题材统计失败，已跳过该图表"}
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

    # ④.5 我的自选（模块二）：数据在渲染前算好，渲染层只负责拼 HTML
    watch_items = build_watchlist(day, data, youzi_df)
    if watch_items:
        logger.info("自选看板：%d 只（%s）", len(watch_items),
                    "、".join(i["代码"] for i in watch_items))

    # ⑤ 渲染
    html = render_html(day, res, hist, youzi_df, seat_note, health, source_tag, watch_items)
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
