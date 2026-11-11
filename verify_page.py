#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_page.py —— 回滚后的最终校验（供 ROLLBACK_20261111.bat 调用）

把中文正则放在 .py 里，而不是嵌在 .bat 的 python -c 里：
bat 是纯 ASCII，一旦命令行里出现 UTF-8 中文，GBK 代码页下会乱码甚至卡死。
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
html_path = BASE / "docs" / "index.html"
hist_path = BASE / "calibration" / "metrics_history.csv"

print()
print("  ---------------- 最终校验 ----------------")

if not html_path.exists():
    print("  [FAIL] docs/index.html 不存在")
else:
    h = html_path.read_text(encoding="utf-8")
    m = re.search(r"数据日期 ([\d-]+).*?更新于 ([\d\-: ]+)", h)
    banner = m.group(0)[:70] if m else "未找到角标"
    print(f"  网页角标 : {banner}")
    print(f"  文件大小 : {len(h) / 1024 / 1024:.2f} MB")
    if m and m.group(1) == "2026-11-10":
        print("  [OK] 页面已恢复为 11-10 的完整数据")
    elif m and m.group(1) == "2026-11-11":
        print("  [FAIL] 页面仍是 11-11 盘中数据！deploy 可能选错了文件")
    else:
        print("  [WARN] 角标日期不是预期的 11-10，请人工确认")

if not hist_path.exists():
    print("  [FAIL] metrics_history.csv 不存在")
else:
    d = pd.read_csv(hist_path, dtype={"日期": str})
    d["日期"] = d["日期"].astype(str).str.zfill(8)
    print(f"  指标库   : {len(d)} 行（{d['日期'].min()} ~ {d['日期'].max()}）")
    has_dirty = bool((d["日期"] == "20261111").any())
    print(f"  20261111 残留: {has_dirty}  -> {'[FAIL]' if has_dirty else '[OK] 已清除'}")

print("  -----------------------------------------")
