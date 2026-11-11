#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
guard_popup.py —— 盘中防呆的「入口弹窗层」
================================================================================

由 daily.bat 在**最开头**调用，早于任何抓数 / 写文件 / 推送动作。

为什么不改 main.py 里已有的 guard_intraday
------------------------------------------
utils.guard_intraday 已经把判断逻辑写得很完整（历史日期放行、周末放行、
15:00 前拒绝、--allow-intraday 显式放行），main.py 也已经在调用它。
它是一道「最后防线」：无论从哪个入口进来都拦得住。

但它只写日志。用户从桌面快捷方式双击时，窗口是隐藏的，日志根本看不见，
所以需要在 bat 入口再加一层**看得见的中文弹窗**。
本脚本只负责「复用判断 + 弹窗 + 返回退出码」，不重复实现规则。

退出码约定（daily.bat 依赖这个）
--------------------------------
    0  放行，正常继续
    2  被盘中防呆拦截（daily.bat 静默退出，避免 vbs 再弹一个错误框）
    1  本脚本自身异常

用法
----
    python guard_popup.py                      # 检查今天
    python guard_popup.py 20261110             # 历史日期，直接放行
    python guard_popup.py --allow-intraday    # 明确强制
    python guard_popup.py --no-popup           # 测试用，不弹窗
"""

from __future__ import annotations

import ctypes
import re
import sys
from datetime import datetime

from utils import guard_intraday

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2

# MB_ICONSTOP = 0x10, MB_OK = 0x0
MB_ICONSTOP = 0x10

WARN_TITLE = "A股复盘 · 盘中防呆保护"
WARN_TEXT = (
    "目前尚未收盘，数据不完整，禁止运行当天的复盘！\n"
    "\n"
    "盘中抓到的只是半天快照，它会被写进三处：\n"
    "  · calibration/metrics_history.csv  历史指标库（污染分位数校准）\n"
    "  · reports/dashboard_*.html         情绪周期图\n"
    "  · docs/index.html                  并推送到 GitHub Pages\n"
    "\n"
    "最麻烦的是：这些数字看起来完全正常，事后极难发现。\n"
    "\n"
    "请在 15:00 之后再运行。\n"
    "\n"
    "如果只是补跑历史数据，带一个日期参数即可，例如：\n"
    "    run_silent.vbs 20261110\n"
    "历史日期不受此限制。"
)


def _popup(text: str, title: str) -> None:
    """Unicode 版 MessageBox，中文不会乱码。"""
    ctypes.windll.user32.MessageBoxW(0, text, title, MB_ICONSTOP)


def main() -> int:
    argv = sys.argv[1:]

    # --no-popup 仅用于自动化测试，避免弹窗把测试进程挂住
    no_popup = "--no-popup" in argv
    argv = [a for a in argv if a != "--no-popup"]

    allow_intraday = "--allow-intraday" in argv

    # 找第一个 YYYYMMDD 参数；找不到就代表「跑今天」
    day = None
    for a in argv:
        if re.fullmatch(r"\d{8}", a):
            day = a
            break
    if day is None:
        day = datetime.now().strftime("%Y%m%d")

    try:
        allowed, reason = guard_intraday(day, allow_intraday=allow_intraday)
    except Exception as exc:
        print(f"[guard] 判断失败（放行以免误伤）：{type(exc).__name__}: {exc}")
        return EXIT_OK

    if allowed:
        return EXIT_OK

    # ---- 被拦截 ----
    print("[guard] 盘中防呆拦截：")
    for line in reason.splitlines():
        print("   " + line)

    if not no_popup:
        try:
            _popup(WARN_TEXT, WARN_TITLE)
        except Exception as exc:
            # 某些无桌面会话（服务/远程）弹不了窗，至少把原因打到日志
            print(f"[guard] 弹窗失败：{type(exc).__name__}: {exc}")

    return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
