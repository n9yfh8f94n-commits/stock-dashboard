# -*- coding: utf-8 -*-
"""给 install_task.ps1 增加 -Auto 无交互模式"""
import pathlib

p = pathlib.Path("install_task.ps1")
s = p.read_text(encoding="ascii")

# 1) 在最前面加 param 块（BOM 之后、注释之前也合法）
old_head = "# ==========================================================================="
new_head = (
    "param(\n"
    "    # -Auto : non-interactive mode used by ONE_CLICK_INSTALL.bat.\n"
    "    #   No 'Press Enter' prompt, and it fires the task once at the end\n"
    "    #   so the user can immediately see whether it works.\n"
    "    [switch]$Auto\n"
    ")\n"
    "\n"
    "# ==========================================================================="
)
assert old_head in s
s = s.replace(old_head, new_head, 1)

# 2) 把所有 Read-Host 改成条件式：Auto 模式下不等输入，直接退出
n_readhost = s.count("Read-Host")
s = s.replace(
    "Read-Host 'Press Enter to exit'\n    exit 1",
    "if (-not $Auto) { Read-Host 'Press Enter to exit' }\n    exit 1",
)
# 单独出现的（结尾那个）替换为 Auto 判断
s = s.replace(
    "Write-Host ''\nRead-Host 'Press Enter to exit'\n",
    "Write-Host ''\nif (-not $Auto) { Read-Host 'Press Enter to exit' }\n",
)

# 3) 结尾追加：Auto 模式下自动触发一次并等待
old_tail = "if (-not $Auto) { Read-Host 'Press Enter to exit' }\n"
new_tail = (
    "if ($Auto) {\n"
    "    Write-Host '--- Firing the task once to verify -------------------------' \n"
    "    try {\n"
    "        Start-ScheduledTask -TaskName $TaskName\n"
    "        Write-Host '[OK] Task started. It runs in the background.' -ForegroundColor Green\n"
    "        Write-Host '     Watch reports\\ and logs\\review.log for the result.'\n"
    "        Write-Host '     A browser tab will open when the review finishes.'\n"
    "    }\n"
    "    catch {\n"
    "        Write-Host '[WARN] Could not start the task immediately:' -ForegroundColor Yellow\n"
    "        Write-Host $_.Exception.Message\n"
    "        Write-Host '       The task itself is registered; run it from Task Scheduler.'\n"
    "    }\n"
    "    Start-Sleep -Seconds 3\n"
    "}\n"
    "else {\n"
    "    Read-Host 'Press Enter to exit'\n"
    "}\n"
)
assert s.count(old_tail) == 1, f"尾部锚点出现 {s.count(old_tail)} 次"
s = s.replace(old_tail, new_tail)

p.write_text(s, encoding="ascii")

d = p.read_bytes()
print("已加入 -Auto 模式")
print("非 ASCII:", sum(1 for b in d if b > 127))
print("行数:", len(d.splitlines()))
print("param 块在第 1 行:", d.decode("ascii").splitlines()[0].strip())
print("Read-Host 出现次数:", d.decode("ascii").count("Read-Host"), "(应为 1，且在 else 分支)")
