#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
deploy.py —— 把最新仪表盘发布到 GitHub Pages
================================================================================

它做的事（3 步，一条命令）
-------------------------
    1. 在 reports/ 里找出日期最新的 dashboard_YYYYMMDD.html
    2. 复制成 docs/index.html（GitHub Pages 的固定入口）
    3. git add / commit / push

朋友访问 https://<用户名>.github.io/<仓库名>/ 时，拿到的永远是 docs/index.html，
也就是你最后一次跑出来的那份。他不需要每天找你要新链接。

为什么用 docs/ 而不是仓库根目录
------------------------------
仓库根目录上全是 .py 源码，把 index.html 扔进去会很乱，
而且 GitHub Pages 的「根目录」模式会把所有源码都当成静态文件暴露出去。
docs/ 目录模式更干净：只有你要公开给人看的那一个文件。

为什么改了 HTML 内容（加了右下角角标）
------------------------------------
朋友是小白，他看不出这份报告是今天的还是三天前的。
所以在 </body> 前注入一个固定定位的小角标，写「数据日期 20261109 · 更新于 ...」。
它用的是 position:fixed，不影响任何图表布局；用 --no-banner 可以关掉。

返回码
------
    0  成功，或「没有任何变更」（都不是错误，daily.bat 可以继续）
    1  失败（未初始化仓库 / 没有 remote / push 失败 / 找不到报告）

【重要】本脚本失败不会影响本地已有的报告和仪表盘。它们早就生成好了。

用法
----
    python deploy.py                 # 发布最新一份
    python deploy.py --dry-run       # 只演练，不提交不推送
    python deploy.py --no-banner     # 不在页面上加数据日期角标
    python deploy.py --check         # 只做环境自检，不动文件
    python deploy.py --init          # 首次使用：初始化仓库 + 首次提交
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# ==============================================================================
# 路径常量（全部基于脚本自身位置，从任何工作目录运行都正确）
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
DOCS_DIR = BASE_DIR / "docs"
INDEX_FILE = DOCS_DIR / "index.html"
NOJEKYLL_FILE = DOCS_DIR / ".nojekyll"

# 历史归档：每天一份 YYYYMMDD.html，供页面顶部的「历史归档」导航使用。
# 【为什么必须有】index.html 每次都被覆盖，昨天的报告就再也看不到了。
# 归档让「最新」和「历史」并存，且全是纯静态文件，GitHub Pages 直接能服务。
ARCHIVE_DIR = DOCS_DIR / "archive"
ARCHIVE_NAME_FMT = "{day}.html"      # day = YYYYMMDD

# 导航条上最多显示多少个日期按钮（再多的靠 archive/ 目录自己浏览）
ARCHIVE_NAV_MAX = 40

# git 命令超时：网络差时 push 可能挂很久，超时就放弃，别把 daily.bat 卡死
GIT_TIMEOUT = 150


# ==============================================================================
# 日志
# ==============================================================================
def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[deploy] WARN  {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[deploy] ERROR {msg}", file=sys.stderr, flush=True)


# ==============================================================================
# git 封装
# ==============================================================================
def git(args: list, timeout: int = GIT_TIMEOUT) -> Tuple[int, str, str]:
    """
    执行 git 命令，返回 (返回码, stdout, stderr)。

    【为什么不抛异常】这个脚本的核心要求是「失败也不能影响本地程序」。
    网络断了、git 没装、凭据过期，统统转换成返回码 + 中文提示，绝不向上抛。

    encoding 强制 utf-8：Windows 中文系统下 git 输出可能是 GBK，
    用默认 locale 解码会当场 UnicodeDecodeError，反而制造新故障。
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(BASE_DIR),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "找不到 git 命令（未安装或不在 PATH 中）"
    except subprocess.TimeoutExpired:
        return 124, "", f"git 命令执行超过 {timeout} 秒（网络可能不通）"
    except Exception as exc:  # 兜底，绝不让部署炸掉主流程
        return 1, "", f"{type(exc).__name__}: {exc}"


def is_repo() -> bool:
    rc, out, _ = git(["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


def has_remote() -> bool:
    rc, out, _ = git(["remote"])
    return rc == 0 and bool(out.strip())


def current_branch() -> str:
    rc, out, _ = git(["rev-parse", "--abbrev-ref", "HEAD"])
    return out if rc == 0 and out else "main"


# ==============================================================================
# 环境自检
# ==============================================================================
def check_env() -> Tuple[bool, str]:
    """返回 (是否就绪, 说明)。所有问题都翻译成「下一步该敲什么」。"""
    rc, out, e = git(["--version"])
    if rc != 0:
        return False, (
            "找不到 git。\n"
            "  请先安装 Git for Windows：https://git-scm.com/download/win\n"
            "  装完重开一个终端窗口（PATH 才会生效），再运行本脚本。"
        )

    if not is_repo():
        return False, (
            "当前目录还不是 git 仓库。\n"
            "  跑这一条即可自动初始化：\n"
            "      python deploy.py --init"
        )

    if not has_remote():
        return False, (
            "仓库还没有配置远程地址（remote）。\n"
            "  先到 GitHub 建一个公开仓库（例如 stock-dashboard），然后：\n"
            "      git remote add origin https://github.com/<你的用户名>/stock-dashboard.git\n"
            "      git branch -M main\n"
            "  再运行 python deploy.py"
        )

    rc, name, _ = git(["config", "user.name"])
    rc2, email, _ = git(["config", "user.email"])
    if not name or not email:
        return False, (
            "没有配置 git 用户身份。\n"
            "      git config --global user.name  \"你的名字\"\n"
            "      git config --global user.email \"你的GitHub邮箱\""
        )
    return True, f"环境就绪（git {out.replace('git version ', '')}，分支 {current_branch()}）"


def warn_placeholder_email() -> None:
    """
    邮箱是占位符时给个提醒，但**不阻止部署**。

    【为什么之前写错了】第一版把占位符当成阻塞条件，结果整个 push 流程被提前 return，
    一次都推不上去。git 本身并不校验邮箱格式，commit 和 push 都能成功，
    占位符的唯一影响是 GitHub 上那条提交记录不关联到你的账号（没有头像、不计入贡献）。
    这种事提醒一句就够了，不该让发布失败。
    """
    rc, email, _ = git(["config", "user.email"])
    if rc == 0 and email and ("your@email.com" in email or "example.com" in email):
        warn(f"git 邮箱是占位符（{email}）——推送不受影响，但 GitHub 上提交不会关联你的账号。")
        warn('  建议改一下：git config --global user.email "你的GitHub邮箱"')


# ==============================================================================
# 找最新报告
# ==============================================================================
def find_latest_dashboard() -> Optional[Path]:
    """
    在 reports/ 里找最新的 dashboard_YYYYMMDD.html。

    排序键是「文件名里的日期」优先、文件修改时间兜底，而不是只看 mtime。
    原因：如果你补跑了一天历史数据（比如今天补 20261105），
    它的 mtime 是最新的，但日期比今天的旧——按 mtime 排会把旧报告发布上线。
    """
    files = list(REPORTS_DIR.glob("dashboard_*.html"))
    if not files:
        return None

    def _key(p: Path) -> Tuple[str, float]:
        m = re.search(r"dashboard_(\d{8})", p.name)
        return (m.group(1) if m else "00000000", p.stat().st_mtime)

    return max(files, key=_key)


def extract_day(path: Path) -> str:
    m = re.search(r"dashboard_(\d{8})", path.name)
    return m.group(1) if m else ""


# ==============================================================================
# 页面角标（告诉朋友这份数据是哪天的）
# ==============================================================================
# 角标样式：刻意做小做轻。
# 第一版 12px + padding 6/13，实测会盖住表格最后一行（右下角正好是游资明细表）。
# 收到 10.5px、padding 3/10、背景透明度降到 .82，足够看清又不抢内容。
BANNER_STYLE = (
    "position:fixed;right:12px;bottom:12px;z-index:99999;"
    "background:rgba(19,26,36,.82);border:1px solid rgba(36,48,64,.9);"
    "color:#8b98a8;"
    "font:10.5px/1.55 system-ui,-apple-system,'Segoe UI','PingFang SC',sans-serif;"
    "padding:3px 10px;border-radius:999px;"
    "box-shadow:0 4px 14px rgba(0,0,0,.35);pointer-events:none;"
    "backdrop-filter:blur(3px);letter-spacing:.2px;"
)


def build_banner(day: str, mtime: float) -> str:
    """
    生成右下角角标。

    【为什么时间取文件 mtime，而不是 datetime.now()】
    第一版用 now()，结果是：同一天连续跑两次 deploy.py，
    角标里的「更新于 09:51」变成「09:53」，内容就变了，
    于是每次都会产生一个空提交、多一次 push。
    改用源 HTML 的修改时间后：只要 dashboard.py 没重新生成，
    角标就稳定不变，deploy.py 重跑会正确判定「无变更」并跳过。
    """
    if len(day) == 8 and day.isdigit():
        shown = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    else:
        shown = day or "未知"
    ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    return (
        f'<div style="{BANNER_STYLE}">'
        f'数据日期 {shown} &nbsp;·&nbsp; 更新于 {ts}'
        f"</div>\n"
    )


def inject_banner(html: str, day: str, mtime: float) -> str:
    """
    在 </body> 前注入角标。

    只在找不到 </body> 时原样返回（不报错）——
    宁可少一个角标，也不能因为格式不符就发布失败。
    """
    # 【必须从后往前找，不能用 replace 的第一处匹配】
    # 内联的 echarts.min.js 里含有 '...</body>',S=window.open() 这样的字符串字面量，
    # 用 find / replace(..., 1) 会把角标注进那串 JS 里：
    #   · 页面照常渲染，不报任何错
    #   · 但角标根本不显示（它变成了 JS 字符串的一部分）
    # 这是最典型的静默错误——看起来一切正常，功能就是没有。
    # rfind 取的是文件末尾真正的闭合标签，一定在 </html> 之前。
    pos = html.rfind("</body>")
    if pos < 0:
        # 万一格式不符（没有 </body>），原样返回，不阻断发布
        return html
    return html[:pos] + build_banner(day, mtime) + html[pos:]


# ==============================================================================
# 发布
# ==============================================================================
# ==============================================================================
# 历史归档导航
# ==============================================================================
# 纯静态实现：GitHub Pages 不能跑脚本，所以日期按钮必须在**生成网页时**写死。
# deploy.py 每次运行都会扫一遍 docs/archive/，把已有日期拼成导航条注入页面。
# 新增一个归档 -> 所有页面的导航不会自动更新，但只要当天再跑一次 deploy 就同步了
# （daily.bat 每天都会跑，所以实际上一直是同步的）。
NAV_CSS = (
    "<style>"
    ".anav{position:sticky;top:0;z-index:9998;display:flex;align-items:center;gap:6px;"
    "padding:7px 12px;background:rgba(11,14,20,.96);border-bottom:1px solid #243040;"
    "backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);"
    "overflow-x:auto;overflow-y:hidden;white-space:nowrap;"
    "font:12px/1.6 system-ui,-apple-system,'Segoe UI','PingFang SC',sans-serif;"
    "-webkit-overflow-scrolling:touch}"
    ".anav-t{color:#5b6577;flex:0 0 auto;margin-right:2px;letter-spacing:.5px}"
    ".anav-l{flex:0 0 auto;display:inline-block;padding:3px 9px;border-radius:6px;"
    "color:#9aa7b8;text-decoration:none;border:1px solid #243040;background:#0f131a}"
    ".anav-l:hover{background:#18202b;color:#e6ebf2;border-color:#31404f}"
    ".anav-l.active{background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:600}"
    ".anav-l.latest{color:#3fb950;border-color:#1f4327}"
    ".anav-l.latest.active{color:#fff;background:#238636;border-color:#238636}"
    "@media(max-width:640px){.anav{padding:6px 8px;gap:4px}"
    ".anav-l{padding:3px 7px;font-size:11px}}"
    "</style>"
)


def list_archive_days() -> list[str]:
    """返回 docs/archive/ 下所有 YYYYMMDD 归档，倒序（最新在前）。"""
    if not ARCHIVE_DIR.exists():
        return []
    days = []
    for p in ARCHIVE_DIR.glob("*.html"):
        if re.fullmatch(r"\d{8}", p.stem):
            days.append(p.stem)
    return sorted(set(days), reverse=True)


def build_nav_html(days: list[str], current_day: str, in_archive: bool) -> str:
    """
    生成导航条 HTML。

    current_day : "" 表示当前是首页（最新）；否则是该归档的日期，会高亮对应按钮
    in_archive  : True 时当前文件在 docs/archive/ 里，链接路径要用相对形式
    """
    if in_archive:
        home_href = "../index.html"
        arch_href = lambda d: f"{d}.html"            # noqa: E731
    else:
        home_href = "./"
        arch_href = lambda d: f"archive/{d}.html"    # noqa: E731

    parts = [NAV_CSS, '<nav class="anav">', '<span class="anav-t">历史归档</span>']
    latest_cls = "anav-l latest active" if not current_day else "anav-l latest"
    parts.append(f'<a class="{latest_cls}" href="{home_href}">最新</a>')

    for d in days[:ARCHIVE_NAV_MAX]:
        label = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        cls = "anav-l active" if d == current_day else "anav-l"
        parts.append(f'<a class="{cls}" href="{arch_href(d)}">{label}</a>')

    parts.append("</nav>")
    return "".join(parts)


def inject_nav(html: str, nav_html: str) -> str:
    """
    把导航条插到 <body ...> 之后。

    用 sticky 定位而不是 fixed：sticky 会占据文档流空间，
    不会盖住页面顶部原有的标题栏。
    """
    pos = html.find("<body")
    if pos < 0:
        return html
    end = html.find(">", pos)
    if end < 0:
        return html
    return html[:end + 1] + nav_html + html[end + 1:]


def sync_archive_from_reports(with_banner: bool = True) -> int:
    """
    把 reports/ 里尚未归档的 dashboard_*.html 补进 docs/archive/。

    为什么需要这一步：
        模块一上线之前已经积累了好几天的报告，它们躺在 reports/ 里。
        如果只归档「今天这一份」，导航条上就只有一天按钮，新功能等于没用。
        这个函数负责把存量补齐，之后每天由 prepare_index 增量维护。

    返回新增归档数量。
    """
    if not REPORTS_DIR.exists():
        return 0
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    # 先算出全量日期（已有归档 ∪ reports 里的报告），
    # 这样每个新写出的归档页，导航条一次就是完整的。
    report_days: list[str] = []
    for p in REPORTS_DIR.glob("dashboard_*.html"):
        d = extract_day(p)
        if d and re.fullmatch(r"\d{8}", d):
            report_days.append(d)

    all_days = sorted(set(list_archive_days()) | set(report_days), reverse=True)

    added = 0
    for p in sorted(REPORTS_DIR.glob("dashboard_*.html")):
        day = extract_day(p)
        if not day or not re.fullmatch(r"\d{8}", day):
            continue

        target = ARCHIVE_DIR / ARCHIVE_NAME_FMT.format(day=day)
        if target.exists():
            continue

        try:
            html = p.read_text(encoding="utf-8")
            if with_banner:
                html = inject_banner(html, day, p.stat().st_mtime)
            html = inject_nav(
                html, build_nav_html(all_days, current_day=day, in_archive=True)
            )
            target.write_text(html, encoding="utf-8")
            added += 1
        except Exception as exc:
            warn(f"归档 {p.name} 失败（跳过）：{exc}")

    if added:
        log(f"已把 {added} 天的历史报告补进 docs/archive/（共 {len(all_days)} 天）")
    return added


def prepare_index(src: Path, day: str, with_banner: bool) -> Tuple[bool, str]:
    """
    把最新报告复制成 docs/index.html，同时存一份到 docs/archive/{day}.html。

    返回 (是否有变更, 说明)。

    两个页面唯一的内容差别是「历史归档」导航条里的高亮项：
        index.html          -> 高亮「最新」
        archive/{day}.html  -> 高亮对应日期
    所以必须分别注入，不能简单复制同一份 HTML。
    """
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        html = src.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"读取 {src.name} 失败：{exc}"

    if with_banner:
        html = inject_banner(html, day, src.stat().st_mtime)

    # ---------- 1) 归档 ----------
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    days = list_archive_days()
    if day not in days:
        days = sorted(days + [day], reverse=True)

    archive_file = ARCHIVE_DIR / ARCHIVE_NAME_FMT.format(day=day)
    archive_html = inject_nav(
        html, build_nav_html(days, current_day=day, in_archive=True)
    )

    archive_changed = True
    if archive_file.exists():
        try:
            archive_changed = archive_file.read_text(encoding="utf-8") != archive_html
        except Exception:
            archive_changed = True

    if archive_changed:
        try:
            archive_file.write_text(archive_html, encoding="utf-8")
        except Exception as exc:
            return False, f"写入 docs/archive/{day}.html 失败：{exc}"

    # ---------- 2) 首页 ----------
    index_html = inject_nav(
        html, build_nav_html(days, current_day="", in_archive=False)
    )

    index_changed = True
    if INDEX_FILE.exists():
        try:
            index_changed = INDEX_FILE.read_text(encoding="utf-8") != index_html
        except Exception:
            index_changed = True

    if index_changed:
        try:
            INDEX_FILE.write_text(index_html, encoding="utf-8")
        except Exception as exc:
            return False, f"写入 docs/index.html 失败：{exc}"

    # .nojekyll：告诉 GitHub Pages 别用 Jekyll 处理这个目录。
    # 不加也能用，但加了能避免 Jekyll 对特殊文件名的过滤。
    try:
        if not NOJEKYLL_FILE.exists():
            NOJEKYLL_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass

    if not index_changed and not archive_changed:
        return False, "docs/index.html 与归档均无变化（今天已经发布过）"

    size_mb = len(html) / 1024 / 1024
    note = f"已更新 docs/index.html + docs/archive/{day}.html（共 {len(days)} 天归档，{size_mb:.2f} MB）"
    if not index_changed:
        note += "（仅归档变化）"
    return True, note


def push(day: str, dry_run: bool = False) -> int:
    """git add -> commit -> push。返回 0 成功 / 1 失败。"""
    branch = current_branch()
    msg = f"Auto deploy {day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else f"Auto deploy {day}"

    # ---- 1) 暂存 ----
    rc, out, e = git(["add", "-A"])
    if rc != 0:
        err(f"git add 失败：{e or out}")
        return 1

    # ---- 2) 有没有东西要提交 ----
    # git diff --cached --quiet 的返回码：0=无变更，1=有变更
    rc_diff, _, _ = git(["diff", "--cached", "--quiet"])
    if rc_diff == 0:
        log("没有需要提交的变更，跳过 commit / push。")
        return 0

    if dry_run:
        rc, out, _ = git(["status", "--short"])
        log("[dry-run] 将要提交的文件：")
        for line in out.splitlines()[:20]:
            log(f"    {line}")
        log(f"[dry-run] 提交信息：{msg}")
        log("[dry-run] 已跳过 commit 和 push。")
        return 0

    # ---- 3) 提交 ----
    rc, out, e = git(["commit", "-m", msg])
    if rc != 0:
        # 常见无害情况：没有任何改动 / 只改了被 ignore 的文件
        if "nothing to commit" in (out + e).lower():
            log("没有需要提交的变更。")
            return 0
        err(f"git commit 失败：{e or out}")
        return 1
    log(f"已提交：{msg}")

    # ---- 4) 推送 ----
    log(f"正在推送到 origin/{branch} …（首次会弹出 GitHub 登录窗口）")
    rc, out, e = git(["push", "-u", "origin", "HEAD"], timeout=GIT_TIMEOUT)

    # push 的进度信息走 stderr，成功时也有内容，所以要看返回码
    if rc == 0:
        log("推送成功。")
        log("约 1 分钟后可访问：https://<你的用户名>.github.io/<仓库名>/")
        return 0

    # ---- 5) 失败诊断：把 git 的原始报错翻译成可执行的下一步 ----
    blob = f"{e}\n{out}".lower()
    err("推送失败。本地报告不受影响，已生成的 HTML 仍然在 reports/ 里。")
    print(f"\n--- git 原始输出 ---\n{e or out}\n--------------------\n", file=sys.stderr)

    if "authentication failed" in blob or "could not read username" in blob or "403" in blob:
        err("原因：GitHub 认证失败。")
        err("  密码方式已被 GitHub 停用，必须用 Personal Access Token 或 SSH：")
        err("  方案 A（推荐，最省事）：装 Git for Windows 时会带 Git Credential Manager，")
        err("      在浏览器弹出的窗口里点「Sign in with your browser」登录一次即可。")
        err("  方案 B：GitHub → Settings → Developer settings → Personal access tokens")
        err("      生成一个 repo 权限的 token，在提示输入密码时粘贴它（不是账号密码）。")
    elif "could not resolve host" in blob or "timed out" in blob or "timeout" in blob:
        err("原因：网络不通，连不上 github.com。")
        err("  检查代理/VPN；国内网络访问 GitHub 经常超时，可稍后重试：python deploy.py")
    elif ("does not appear to be a git repository" in blob
          or "repository not found" in blob):
        err("原因：远程仓库地址不正确，或该仓库不存在/已被删除。")
        err("  先看一眼当前地址：")
        err("      git remote -v")
        err("  正确格式：https://github.com/<你的用户名>/<仓库名>.git")
        err("  另外：仓库必须是 Public（Private 仓库的 Pages 需要付费账号）。")
    elif "rejected" in blob or "non-fast-forward" in blob or "fetch first" in blob:
        err("原因：远程仓库有你本地没有的提交（比如你在网页上改过文件）。")
        err("  先同步再推送：")
        err("      git pull --rebase origin " + branch)
        err("      python deploy.py")
    elif "protected branch" in blob:
        err("原因：目标分支被保护，不允许直接推送。")
        err("  到 GitHub → Settings → Branches 里放开限制，或改用 Pull Request。")
    else:
        err("原因未知，请对照上面的 git 原始输出判断。")

    return 1


# ==============================================================================
# 初始化（首次使用）
# ==============================================================================
GITIGNORE_CONTENT = """# ============================================================
# A股复盘系统 · 公开仓库忽略清单
# 原则：只把「代码 + 最终 HTML」推到 GitHub，
#      原始数据、缓存、日志、中间产物一律留在本地。
# ============================================================

# ---- 原始行情快照：每天几十个 CSV，且是本系统最核心的资产，不公开 ----
cache/

# ---- 校准数据与长期指标库 ----
calibration/

# ---- 每日产出：报告本体在 docs/index.html，原始产物不上传 ----
reports/

# ---- 运行日志 ----
logs/
*.log

# ---- 第三方 JS 库（1MB，首次运行会自动重新下载）----
assets/

# ---- Python ----
__pycache__/
*.py[cod]
*.pyo
*.pyd
*.egg-info/
.eggs/
build/
dist/
.venv/
venv/
env/
ENV/

# ---- 编辑器 / 操作系统 ----
.vscode/
.idea/
*.swp
.DS_Store
Thumbs.db
desktop.ini

# ---- 临时文件 ----
*.tmp
*.bak
*~
"""


def do_init() -> int:
    """首次使用：git init + 写 .gitignore + 首次提交 + 提示下一步。"""
    gitignore = BASE_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_CONTENT, encoding="utf-8")
        log("已创建 .gitignore")
    else:
        log(".gitignore 已存在，保留不动")

    if not is_repo():
        rc, out, e = git(["init"])
        if rc != 0:
            err(f"git init 失败：{e or out}")
            return 1
        log("已初始化 git 仓库")
        # 统一用 main 作分支名（GitHub 新建仓库默认也是 main）
        git(["branch", "-M", "main"])

    # 首次提交前，先确认要提交什么
    git(["add", "-A"])
    rc, out, _ = git(["status", "--short"])
    n = len([x for x in out.splitlines() if x.strip()])
    if n == 0:
        log("没有可提交的文件（可能都被 .gitignore 排除了）。")
    else:
        log(f"将提交 {n} 个文件：")
        for line in out.splitlines()[:15]:
            log(f"    {line}")
        if n > 15:
            log(f"    … 另有 {n - 15} 个")

        rc, o, e = git(["commit", "-m", "Initial commit"])
        if rc == 0:
            log("已完成首次提交")
        elif "nothing to commit" in (o + e).lower():
            log("没有新内容可提交")
        else:
            err(f"首次提交失败：{e or o}")
            err("  多半是没配 git 身份，执行：")
            err("      git config --global user.name  \"你的名字\"")
            err("      git config --global user.email \"你的GitHub邮箱\"")
            return 1

    print(
        "\n"
        "============================================================\n"
        " 初始化完成。还差最后一步：关联 GitHub 远程仓库\n"
        "------------------------------------------------------------\n"
        " 1) 在 GitHub 上新建一个 Public 仓库，例如 stock-dashboard\n"
        "    （不要勾选 Add README / .gitignore / license，保持空仓库）\n"
        "\n"
        " 2) 在本目录执行（把 <用户名> 和 <仓库名> 换成你的）：\n"
        "      git remote add origin https://github.com/<用户名>/<仓库名>.git\n"
        "\n"
        " 3) 推送：\n"
        "      python deploy.py\n"
        "\n"
        " 4) 到 GitHub 仓库 Settings -> Pages，\n"
        "    Source 选 Deploy from a branch，Branch 选 main，目录选 /docs，Save。\n"
        "    等 1~3 分钟，访问 https://<用户名>.github.io/<仓库名>/\n"
        "============================================================\n",
        flush=True,
    )
    return 0


# ==============================================================================
# 主流程
# ==============================================================================
def run(dry_run: bool = False, with_banner: bool = True, quiet: bool = False) -> int:
    if not quiet:
        log("=" * 62)

    src = find_latest_dashboard()
    if src is None:
        err(f"在 {REPORTS_DIR} 里找不到 dashboard_*.html。")
        err("  先跑一次：python dashboard.py")
        return 1

    day = extract_day(src)
    log(f"最新报告：{src.name}")

    ok_env, reason = check_env()
    if not ok_env:
        # 环境没就绪时，仍然可以只更新 docs/index.html，
        # 方便用户先确认文件对不对，再去折腾 git。
        changed, msg = prepare_index(src, day, with_banner)
        log(msg)
        warn("git 环节跳过，原因如下：")
        for line in reason.splitlines():
            print(f"        {line}", flush=True)
        return 1

    warn_placeholder_email()

    # 先补齐存量归档（reports/ 里还没进 docs/archive/ 的历史报告），
    # 再处理当天这一份。顺序不能反：当天那份的导航要用到完整日期列表。
    sync_archive_from_reports(with_banner)

    changed, msg = prepare_index(src, day, with_banner)
    log(msg)

    return push(day, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把最新仪表盘发布到 GitHub Pages",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python deploy.py               发布最新一份\n"
            "  python deploy.py --dry-run     演练，不提交不推送\n"
            "  python deploy.py --check       只检查环境\n"
            "  python deploy.py --init        首次使用：初始化仓库\n"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="演练：只准备文件，不 commit / push")
    parser.add_argument("--no-banner", action="store_true", help="不在页面上加数据日期角标")
    parser.add_argument("--check", action="store_true", help="只做环境自检")
    parser.add_argument("--init", action="store_true", help="初始化 git 仓库并首次提交")
    args = parser.parse_args()

    if args.init:
        raise SystemExit(do_init())

    if args.check:
        ok, reason = check_env()
        if ok:
            log(reason)
            warn_placeholder_email()
            src = find_latest_dashboard()
            log(f"最新报告：{src.name}" if src else "reports/ 里还没有 dashboard_*.html")
            raise SystemExit(0)
        # 统一走 stdout：混用 stderr 会让「环境未就绪」这行跑到原因后面去
        warn("环境未就绪：")
        for line in reason.splitlines():
            print(f"    {line}", flush=True)
        raise SystemExit(1)

    try:
        rc = run(dry_run=args.dry_run, with_banner=not args.no_banner)
    except KeyboardInterrupt:
        warn("已中断。本地文件不受影响。")
        rc = 130
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
