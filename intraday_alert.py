#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
intraday_alert.py —— 盘中异动预警机器人（模块五）
================================================================================

做什么
------
盘中（建议 10:30 / 14:00）抓一次实时涨停 / 炸板 / 跌停数，
如果「炸板率 > 35%」或「跌停数 > 10」，判定情绪退潮，
通过**企业微信群机器人**推送到微信（免费、无条数限制）。
不满足条件就静默退出，只在控制台打印一行「情绪正常」。

为什么直接调 akshare，不用 data_fetch.fetch_zt_pool
----------------------------------------------------
    fetch_* 会把结果写进 cache/{day}_zt_pool.csv。
    盘中抓到的只是半天快照，一旦落盘，dashboard 的 build_history
    会把它当成一个完整交易日去重算，直接污染情绪周期图。

    这正是 2026-11-11 那次事故的成因（13:06 误跑，半截数据进了指标库）。
    预警只需要「几个数量」，所以整个缓存层都绕开：只读，不写。

    代价：拿不到 data_fetch 那套 ST / 北交所过滤。
    但预警关心的是「市场整体有多差」，和东财 APP 首页的数字对齐更重要，
    用原始计数反而更接近你手机上看到的。

用法
----
    python intraday_alert.py             # 检查一次
    python intraday_alert.py --force     # 强制发一条测试消息（不抓数据）
    python intraday_alert.py --dry-run   # 只打印数据，不推送
    python intraday_alert.py --date 20261111   # 指定日期（复盘用）

配合任务计划程序
----------------
    工作日 10:30、14:00 各跑一次，程序路径用 pythonw.exe（无黑框）。
    详见 README 第四节，或直接导入 task_intraday.xml。

退出码
------
    0  正常（无论是否触发）
    1  推送失败 / 数据抓取失败
    2  未配置 Token
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 阈值
# 这两个数是「情绪退潮」的经验分界，不是拍脑袋：
#   · 炸板率 35% 以上，意味着当天封板的三分之一以上被打开，
#     打板资金大面积亏损，次日承接意愿会显著下降
#   · 跌停 10 家以上，说明亏钱效应已经扩散到跌停板，
#     通常对应主线退潮或指数破位
ZB_RATE_THRESHOLD = 35.0    # 炸板率 %
DT_COUNT_THRESHOLD = 10     # 跌停家数

# 企业微信群机器人。地址形如：
#   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
# 免费、无条数限制，消息直接在微信里弹（走企业微信 App）。
# 【为什么不用 PushPlus】它已经改成收费了，免费额度基本不可用。
WECOM_WEBHOOK_ENV = "WECOM_WEBHOOK"

# 企业微信 markdown 单条上限 4096 字节，我们的消息远小于这个值

# 盘前判定：9:25 集合竞价结束前，涨停池基本是空的，抓了没意义
MARKET_OPEN_HHMM = (9, 25)


# ==============================================================================
# Webhook 配置
# ==============================================================================
def get_webhook() -> str:
    """
    读取企业微信群机器人 Webhook。优先级：环境变量 > local_secret.py。

    写成两级是为了兼容两种习惯：
        · 不想让地址落在任何文件里 -> 用 setx WECOM_WEBHOOK
        · 不想每次都开终端设环境变量 -> 填 local_secret.py（已在 .gitignore 里）
    """
    hook = os.environ.get(WECOM_WEBHOOK_ENV, "").strip()
    if hook:
        return hook

    try:
        import local_secret  # type: ignore
        hook = str(getattr(local_secret, "WECOM_WEBHOOK", "") or "").strip()
    except Exception:
        hook = ""

    return hook


# ==============================================================================
# 实时抓取（不写缓存）
# ==============================================================================
def _count_rows(fn_name: str, day: str) -> int:
    """
    调 akshare 的涨停/炸板/跌停池，返回行数。

    任何异常都返回 -1，由调用方决定是「当作 0」还是「判定失败」——
    预警脚本宁可少报，也不能因为一个接口挂了就发假警报。
    """
    try:
        import akshare as ak
    except Exception as exc:
        print(f"[alert] 无法导入 akshare：{exc}", file=sys.stderr)
        return -1

    try:
        fn = getattr(ak, fn_name)
        df = fn(date=day)
        if df is None:
            return 0
        return int(len(df))
    except Exception as exc:
        print(f"[alert] {fn_name}({day}) 失败：{type(exc).__name__}: {str(exc)[:80]}",
              file=sys.stderr)
        return -1


def fetch_snapshot(day: str) -> Optional[Dict[str, Any]]:
    """
    抓取一次实时快照。

    返回 None 表示数据不可用（三个接口全挂，或涨停+炸板都是 0）。
    「涨停 0 家」在真实盘面几乎不可能，出现即视为接口异常，不发警报。
    """
    n_zt = _count_rows("stock_zt_pool_em", day)
    n_zb = _count_rows("stock_zt_pool_zbgc_em", day)
    n_dt = _count_rows("stock_zt_pool_dtgc_em", day)

    # 跌停池经常为空，空表不是错误；-1 才是错误
    if n_dt < 0:
        n_dt = 0

    if n_zt < 0 and n_zb < 0:
        return None

    n_zt = max(n_zt, 0)
    n_zb = max(n_zb, 0)

    base = n_zt + n_zb
    if base == 0:
        return None

    zb_rate = n_zb / base * 100.0

    return {
        "日期": day,
        "涨停": n_zt,
        "炸板": n_zb,
        "跌停": n_dt,
        "封板基数": base,
        "炸板率": round(zb_rate, 2),
        "抓取时间": datetime.now().strftime("%H:%M:%S"),
    }


# ==============================================================================
# 判定
# ==============================================================================
def judge(snap: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    返回 (是否触发, 触发原因列表)。

    两个条件是「或」的关系：任意一个成立就预警。
    """
    reasons: List[str] = []

    # 注意用裸 ">"：这段文本最终进企业微信 markdown，
    # 不是 HTML，写 &gt; 会原样显示成四个字符。
    if snap["炸板率"] > ZB_RATE_THRESHOLD:
        reasons.append(f"炸板率 {snap['炸板率']:.1f}% > {ZB_RATE_THRESHOLD:.0f}%")

    if snap["跌停"] > DT_COUNT_THRESHOLD:
        reasons.append(f"跌停 {snap['跌停']} 家 > {DT_COUNT_THRESHOLD} 家")

    return bool(reasons), reasons


# ==============================================================================
# 企业微信推送
# ==============================================================================
def push_wecom(markdown: str, webhook: str) -> Tuple[bool, str]:
    """
    发一条企业微信群消息。返回 (成功, 说明)。

    企业微信响应形如 {"errcode":0,"errmsg":"ok"}，errcode != 0 都算失败。
    常见错误码：
        93000  webhook key 无效（多半是地址没复制完整）
        45009  发送频率超限（机器人上限 20 条/分钟）
        40008  消息格式不对
    """
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": markdown},
    }

    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        try:
            obj = json.loads(raw)
        except Exception:
            return False, f"响应不是 JSON：{raw[:120]}"

        code = obj.get("errcode")
        if code == 0:
            return True, str(obj.get("errmsg") or "ok")

        hint = {
            93000: "webhook 地址无效，检查 key 是否复制完整",
            45009: "发送太频繁（机器人上限 20 条/分钟）",
            40008: "消息格式不对",
        }.get(code, "")
        msg = f"errcode={code} {obj.get('errmsg')}"
        if hint:
            msg += f"；{hint}"
        return False, msg

    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"网络错误：{exc.reason}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:100]}"


def build_alert_markdown(snap: Dict[str, Any], reasons: List[str]) -> str:
    """
    预警消息正文（企业微信 markdown）。

    企业微信的 markdown 只支持很有限的子集：标题 / 加粗 / 引用 / 列表 /
    三色字体（info 绿、comment 灰、warning 橙红）。**不支持表格**，
    所以用「加粗 + 引用」排版，比硬塞表格更可靠。
    """
    d = str(snap["日期"])
    date_str = f"{d[:4]}-{d[4:6]}-{d[6:]}"

    zb_warn = 'warning' if snap["炸板率"] > ZB_RATE_THRESHOLD else "comment"
    dt_warn = 'warning' if snap["跌停"] > DT_COUNT_THRESHOLD else "comment"

    reason_lines = "\n".join(f"> {r}" for r in reasons)

    return (
        f'## 🚨 盘中预警：情绪退潮\n'
        f'> {date_str} · 抓取于 {snap["抓取时间"]}\n\n'
        f'**涨停** {snap["涨停"]} 家　'
        f'**炸板** {snap["炸板"]} 家\n'
        f'**炸板率** <font color="{zb_warn}">{snap["炸板率"]:.1f}%</font>　'
        f'**跌停** <font color="{dt_warn}">{snap["跌停"]}</font> 家\n\n'
        f'**触发条件**\n{reason_lines}\n\n'
        f'<font color="warning">建议：降低仓位，避免追高接力；'
        f'已有持仓注意止盈，不接破板。</font>'
    )


def build_test_markdown() -> str:
    """--force 的测试消息正文。"""
    now = datetime.now()
    return (
        f'## ✅ 预警通道测试\n'
        f'> {now.strftime("%Y-%m-%d %H:%M:%S")}\n\n'
        f'这是一条来自 `intraday_alert.py --force` 的测试消息。\n\n'
        f'收到说明**企业微信机器人配置正确**，盘中预警可以正常推送到这里。\n\n'
        f'<font color="comment">正式触发条件：炸板率 > {ZB_RATE_THRESHOLD:.0f}% '
        f'或 跌停 > {DT_COUNT_THRESHOLD} 家</font>'
    )


# ==============================================================================
# 主流程
# ==============================================================================
def run(args: argparse.Namespace) -> int:
    day = args.date or datetime.now().strftime("%Y%m%d")
    now = datetime.now()
    is_today = (day == now.strftime("%Y%m%d"))

    print("=" * 64)
    print(f"  盘中异动预警  |  {day}")
    print("=" * 64)

    # ---------- --force：只发测试消息，不抓数据 ----------
    if args.force:
        hook = get_webhook()
        if not hook:
            print("\n[FAIL] 未配置企业微信 Webhook。")
            print('  方式 A（推荐）：setx WECOM_WEBHOOK "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."')
            print("                  然后关掉终端重开")
            print('  方式 B：编辑 local_secret.py 里的 WECOM_WEBHOOK = "..."')
            return 2

        print("\n[force] 发送测试消息到企业微信群…")
        ok, msg = push_wecom(build_test_markdown(), hook)
        if ok:
            print(f"[OK] 已发送，去微信里看看。({msg})")
            return 0
        print(f"[FAIL] 发送失败：{msg}")
        return 1

    # ---------- 盘前判定 ----------
    if is_today:
        hm = (now.hour, now.minute)
        if hm < MARKET_OPEN_HHMM:
            print(f"\n[SKIP] 现在 {now.strftime('%H:%M')}，还没到 9:25 集合竞价结束，"
                  f"涨停池基本为空，跳过检查。")
            return 0

    # ---------- 抓数 ----------
    print("\n[1/3] 抓取实时涨停 / 炸板 / 跌停…")
    snap = fetch_snapshot(day)
    if snap is None:
        print("[FAIL] 三个接口都没拿到有效数据（可能未开盘、非交易日或接口异常）。")
        print("       为避免误报，本次不推送。")
        return 1

    print(f"      涨停 {snap['涨停']} 家 ｜ 炸板 {snap['炸板']} 家 ｜ "
          f"跌停 {snap['跌停']} 家 ｜ 炸板率 {snap['炸板率']:.2f}%")

    # ---------- 判定 ----------
    print("\n[2/3] 判定…")
    triggered, reasons = judge(snap)

    if not triggered:
        print(f"      ✅ 情绪正常：炸板率 {snap['炸板率']:.1f}%（阈值 {ZB_RATE_THRESHOLD:.0f}%），"
              f"跌停 {snap['跌停']} 家（阈值 {DT_COUNT_THRESHOLD} 家）")
        print("\n[3/3] 未触发条件，静默通过。")
        return 0

    print("      🚨 判定为情绪退潮：")
    for r in reasons:
        print(f"         · {r.replace('&gt;', '>')}")

    # ---------- 推送 ----------
    print("\n[3/3] 推送到企业微信…")
    if args.dry_run:
        print("      [dry-run] 跳过推送。消息正文如下：")
        print("-" * 56)
        print(build_alert_markdown(snap, reasons))
        print("-" * 56)
        return 0

    hook = get_webhook()
    if not hook:
        print("[FAIL] 触发预警但未配置企业微信 Webhook，无法推送。")
        print('  方式 A（推荐）：setx WECOM_WEBHOOK "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."')
        print("                  然后关掉终端重开")
        print('  方式 B：编辑 local_secret.py 里的 WECOM_WEBHOOK = "..."')
        return 2

    ok, msg = push_wecom(build_alert_markdown(snap, reasons), hook)
    if ok:
        print(f"      [OK] 已推送到企业微信群。({msg})")
        return 0

    print(f"      [FAIL] 推送失败：{msg}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="盘中异动预警：炸板率过高或跌停过多时推送微信",
    )
    parser.add_argument("--force", action="store_true",
                        help="强制发一条测试消息，不抓数据、不看条件")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印数据和判定结果，不推送")
    parser.add_argument("--date", help="指定交易日 YYYYMMDD，默认今天")
    args = parser.parse_args()

    try:
        code = run(args)
    except KeyboardInterrupt:
        print("\n已中断")
        code = 130

    raise SystemExit(code)


if __name__ == "__main__":
    main()
