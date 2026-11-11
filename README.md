# A股超短线全自动复盘系统

纯 Python + AKShare，零付费数据源。每天盘后跑一条命令，输出一份 Markdown 复盘报告到 `reports/`。

```
交易日校验 → 抓 5 池 + 龙虎榜 + 指数 → 清洗过滤 → 算情绪指标 → 匹配游资席位 → 渲染报告
```

---

## 一、安装

```bash
cd a_share_review
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

要求 Python 3.9+。实测环境：Python 3.13.14 / Windows 11 / akshare 1.20.35 / pandas 3.1.0 / pyecharts 2.1.0。

> **务必用 `python -m pip` 而不是裸 `pip`。**
> 本机若存在多个 Python（Windows Store 版、WorkBuddy 内置版等），裸 `pip` 可能把包装进另一个解释器，
> 之后运行脚本会报 `ModuleNotFoundError: pyecharts`——而且 `pip list` 里明明是装着的，极难排查。

---

## 二、命令速查

| 命令 | 作用 |
| :--- | :--- |
| **`daily.bat`** | **每日一键：报告 + 仪表盘，最常用** |
| `python main.py` | 只生成文字报告 |
| `python main.py 20261109` | 复盘指定日期 |
| `python main.py --check` | 自检：只测 7 个数据源连通性，不生成报告 |
| `python main.py --calibrate 30` | 校准：遍历最近 30 个交易日，输出分位数与阈值诊断 |
| `python main.py 20261109 --no-seat` | 跳过个股席位抓取（快约 10 秒，但看不到卖出标的） |
| `python main.py 20261109 --refresh` | 忽略本地缓存，强制重新抓取 |
| `python dashboard.py` | 生成**单页 HTML 仪表盘**并自动打开浏览器 |
| `python dashboard.py --no-seat` | 仪表盘纯离线模式（快，但没有单票级游资金额） |
| `python dashboard.py --days 30` | 情绪周期图看更长窗口（实际受接口窗口限制） |
| `python ths_fetch.py` | 同花顺通道离线自检（验证 Key 配置与字段映射） |
| `python backfill_history.py` | 用同花顺连板天梯补全 30 天历史指标 |
| `python backfill_history.py --include-today` | 连今天一起补（默认 15:00 前会跳过，避免盘中数据） |

---

## 三、目录与产出

```
a_share_review/
├── config.py          全局配置：过滤规则、游资字典、情绪阈值、字段候选表
├── utils.py           重试装饰器、交易日历、ST/北交所过滤、缓存、日志
├── data_fetch.py      数据抓取与容错层（5 池 + 龙虎榜 + 活跃营业部 + 指数）
├── analyzer.py        纯计算：炸板率、赚钱效应、连板天梯、主线集中度、盯盘清单
├── lhb.py             游资画像：双层降级 seat 匹配、口径统一聚合
├── daily.bat          每日一键运行（保持纯 ASCII，见第四节警告）
├── main.py            编排 + Markdown 渲染 + 校准
├── dashboard.py       单页 HTML 仪表盘（pyecharts + 内联 ECharts）
├── deploy.py          发布到 GitHub Pages（docs/index.html）
├── ths_fetch.py       同花顺扶摇 API 接入层（降级兜底 + 30 天补全）
├── backfill_history.py 用同花顺连板天梯回填 metrics_history.csv
├── local_secret.py.example  密钥模板（真实文件已 gitignore）
├── requirements.txt
├── assets/            echarts.min.js（首次运行自动下载，用于离线内联）
├── cache/             接口快照（同一天重复跑不再请求网络）
├── reports/           复盘_YYYYMMDD.md / dashboard_YYYYMMDD.html  ← 每日产出
├── calibration/       校准明细 CSV + metrics_history.csv（长期指标库）
└── logs/              运行日志
```

所有路径都基于 `config.BASE_DIR = Path(__file__).parent` 转成绝对路径，**从任何工作目录运行都不会写错位置**。

---

## 四、每天怎么跑

### 一条命令（推荐）

```bat
daily.bat                 :: 复盘今天（非交易日自动跳过）
daily.bat 20261109        :: 复盘指定日期
```

双击 `daily.bat` 就行。它按固定顺序做两件事：

| 步骤 | 命令 | 产出 | 耗时 |
| :--- | :--- | :--- | :--- |
| 1 | `python main.py` | `reports/复盘_YYYYMMDD.md` + 写入 `metrics_history.csv` | 约 11 秒 |
| 2 | `python dashboard.py` | `reports/dashboard_YYYYMMDD.html`（自动开浏览器） | 约 21 秒 |

**顺序不能颠倒。** `dashboard.py` 读的历史序列来自 `metrics_history.csv`，而这个文件是 `main.py` 写的。先跑 dashboard，历史样本永远不增长。

实测一次完整运行 **32.9 秒**（数据已在缓存时）。

> ⚠️ **`daily.bat` 必须保持纯 ASCII。**
> 一个带中文注释的 UTF-8 `.bat`，在 GBK 代码页下会被拆成乱码，`rem` 注释行会被当成命令去执行，
> 报一串 `'xxx' 不是内部或外部命令` 并且**卡住不退出**（实测卡了 7 分钟）。
> 想加中文提示，写到 Python 里，或者把 `.bat` 存成 GBK 编码。

### Windows 定时任务（全自动）

```bat
schtasks /Create /TN "A股复盘" ^
  /TR "C:\完整路径\a_share_review\daily.bat" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 17:30
```

或图形界面：任务计划程序 → 创建基本任务 → 每周、勾周一至周五 → 启动程序指向 `daily.bat`。

**为什么是 17:30**：15:00 收盘，但涨停池/龙虎榜在盘后陆续生成，16:00 前抓容易拿到不完整的快照。17:30 之后基本稳定。

### macOS / Linux

```bash
crontab -e
# 工作日 17:30（没有 .bat，直接串两条）
30 17 * * 1-5 cd /完整路径/a_share_review && python3 main.py >> logs/cron.log 2>&1 && python3 dashboard.py --no-open >> logs/cron.log 2>&1
```

### ⚠️ 一条比定时任务更重要的约束

**每天不跑，那天的数据会永久丢失。**

东财涨停池接口只保留约 20 个交易日。今天的快照如果没写进 `cache/`，
20 天后就再也抓不回来，情绪周期图会永远缺这一天。

真实影响：
- 定时任务依赖**电脑开机 + 联网**。关机、断网、外出，那天就缺了
- 缺一天不会报错，只是情绪周期图少一个点、`metrics_history.csv` 少一行
- 想补：必须在 20 天窗口内手动 `daily.bat 2026MMDD` 补跑

所以这套系统的可靠性不取决于代码，取决于**你是否每天都让它跑一次**。

### 需要人工做的三件事

代码能算指标，但下面三件事只有你能做：

| 时机 | 做什么 |
| :--- | :--- |
| 每天跑完 | 看一眼「情绪档位」和「仓位建议」。系统给的是区间，最终仓位还是你定 |
| 数据异常时 | 报告顶部若出现「数据源 5/7」或「涨停池为空」，说明接口挂了。当天结论不可信，别照着下单 |
| 每 30 个交易日 | 跑一次 `python main.py --calibrate 30`，看阈值是否需要调整（见第五节） |

---

## 五、关于阈值校准

`config.EMOTION_THRESHOLDS` 里的初始值（炸板率 35/50%、涨停 80 家、最高板 5）是**经验值，不是统计结论**。

```bash
python main.py --calibrate 30
```

它会输出：

1. **分位数表**（P10/P25/P50/P75/P90）——各指标在这段行情中的分布
2. **阈值诊断**——哪些绝对阈值在样本内从未达标（说明该档位形同虚设）
3. **今日百分位**——今天这个数在历史中排第几
4. **反事实检验**——若改用建议阈值，历史档位分布会怎么变

### ⚠️ 一条必须知道的硬约束

**东财涨停池只保留约 20 个交易日的数据。** 实测（2026-11-10）：

```
20261015 → 0 行（已过期，取不到）
20261022 → 33 行（仍在保留窗口内）
```

所以 `--calibrate 30` **不可能真的拿到 30 天样本**，最多 14~20 天。这不是程序 bug。

系统为此内置了两个机制：

- **指标历史库**：每次 `python main.py` 复盘时，会把当日指标自动追加到 `calibration/metrics_history.csv`。样本随天数自然累积。
- **探测 + 熔断**：校准先花 0.1 秒探测每个日期，连续 3 天取不到数据就判定超出窗口、立即停止回溯。首版没有这两步，30 天校准要跑 11 分钟（全浪费在注定失败的日期上），现在只需几秒。

**结论**：想拿到 60 天样本，唯一办法是每天跑一次，让历史库慢慢长起来。第一次校准只能拿到 14 天左右。

### ⚠️ 不要把分位数当成最优阈值

情绪档位是**绝对概念**：熊市里涨停数 P90 可能只有 45 家，把 `climax_zt_min` 从 80 改成 45，等于把「熊市里最好的一天」标成高潮，系统会持续给重仓建议。

分位数只回答「在样本内排第几」，不回答「今天好不好」。所以：

- 样本少于 10 天时，脚本**拒绝**给出建议值（刻意设计）
- 建议值之间会做**序关系校验**（例如 `ebb_zb_rate_alone` 必须严格大于 `ebb_zb_rate_min`，否则该条件永不生效）；校验不通过的直接剔除
- 换用建议阈值后会跑**反事实检验**，若「高潮」占比超过 40% 会明确警告「阈值太松，勿采用」

---

## 六、已知限制

| 限制 | 说明 |
| :--- | :--- |
| 游资席位映射 | `config.YOUZI_MAP` 里的对应关系（章盟主→江苏路、宁波桑田路→桑田路等）来自**市场民间流传**，非官方数据，营业部会迁址改名。代码用「最长关键词优先」降低误判，但仍只作关注度提示，不可作交易依据 |
| 数据源稳定性 | AKShare 用的是东财非公开接口，随时可能改版。字段改名可通过 `config` 的候选名映射表自动兼容；**接口下架**则只能换源 |
| 第二层席位覆盖 | 个股席位只抓盯盘清单里的前几只票，其余涨停股的席位明细未抓。报告里已标注 |
| 有效涨停口径 | 用「名称首字母 N/C」识别次新股，遇到停牌复牌、更名、借壳会误判 |
| 情绪阈值 | 见上一节，必须用历史数据校准，否则可能长期判「震荡」 |

---

## 七、HTML 仪表盘（dashboard.py）

`main.py` 产出的 Markdown 是「结论」，`dashboard.py` 产出的 HTML 是「形状」——
三张图分别回答三个不同的问题：

| 图 | 回答的问题 | 关键设计 |
| :--- | :--- | :--- |
| 情绪周期（柱+线） | 最近这些天，情绪在往哪个方向走？ | 柱子按当日情绪档位着色，炸板率 35%/50% 两条阈值线直接取自 `config`，不写死 |
| 连板天梯（柱状） | 情绪结构是健康的金字塔，还是畸形断层？ | **0 家的高度也画出来**，3/4/5 板断档一眼可见 |
| 游资桑基 + 买卖矩阵 | 谁在买、谁在卖、在哪只票上对打了多少？ | 桑基只画买入方向（流量必须为正），卖出交给热力图（红买绿卖同图可比） |

### 为什么用 pyecharts 而不是 Streamlit

Streamlit 是常驻 Web 服务：要开终端、占端口、用完 Ctrl+C，也不能像文件一样归档或转发。
对一个「每天盘后跑一次」的复盘系统，**静态 HTML 文件才是对的形态**——双击就开，可以收藏、可以发人。

### 离线可用

pyecharts 默认从 CDN 引 `echarts.min.js`，断网打开就是一片空白。
本模块首次运行会把 `echarts.min.js`（约 1 MB）下载到 `assets/`，之后**内联进 HTML**，
生成的单文件（约 1.06 MB）可以彻底脱离网络，拷到任何机器上打开。

### 数据来源

不重新抓数据，直接读 `cache/*.csv` 重算（口径与 Markdown 报告完全一致），
缺失的日期用 `calibration/metrics_history.csv` 补。
`--no-seat` 纯离线；默认会联网抓龙虎榜个股席位，才有「游资×股票×金额」三元组。

### 已知限制

| 限制 | 说明 |
| :--- | :--- |
| 情绪周期窗口 | 东财涨停池只保留约 20 个交易日，`cache/` 里攒了几天就只能画几天（本机实测 14 天）。想画更长只能靠每天跑 `main.py` 累积到 `metrics_history.csv` |
| 桑基只画买入 | 净卖出是反向流动，塞进桑基会让宽度失真。卖出看热力图 |
| 热力图列数 | 最多显示 18 只股票（按涉及金额排序），避免格子被压成一条线 |

---


## 八、发布到 GitHub Pages（给朋友一个固定网址）

目标：朋友打开 `https://<你的用户名>.github.io/<仓库名>/`，每天刷新就能看到你最新的复盘，
不用每天找你要链接。

原理：`deploy.py` 把 `reports/dashboard_YYYYMMDD.html` 复制成 `docs/index.html` 再 push，
GitHub Pages 把 `docs/` 目录当成静态站点发布，`index.html` 就是首页。

### ⚠️ 先说三件可能劝退你的事

1. **仓库必须是 Public**。Private 仓库的 Pages 需要 GitHub 付费账号。
2. **源码会一起公开**。默认把 `*.py` 也传上去，方便你多机备份；如果不想公开源码，
   在 `.gitignore` 里加一行 `*.py` 即可（但那样仓库里就只剩网页了）。
3. **网页内容是公开的**。包括你的游资判断、仓位建议、盯盘清单。别在里面写不该公开的东西。

### 第一步：改掉占位符邮箱（很重要）

你的 git 邮箱现在是 `your@email.com`，这是占位符。推送能成功，但 GitHub 上那条提交
不会关联到你的账号（没有头像、不计入贡献记录）。

```bat
git config --global user.email "你的GitHub注册邮箱"
git config --global user.name  "你的名字"
```

### 第二步：初始化本地仓库

在 `a_share_review` 目录下：

```bat
python deploy.py --init
```

它会做三件事：`git init`、确认 `.gitignore` 生效、首次提交。
跑完会打印一份「还差最后一步」的提示，照着做即可。

输出里会列出「将提交 N 个文件」。**确认列表里没有 `cache/`、`logs/`、`calibration/`、`reports/`**——
有的话说明 `.gitignore` 没生效，先别继续。

### 第三步：在 GitHub 上建仓库

1. 打开 https://github.com/new
2. Repository name 填 `stock-dashboard`（或你喜欢的名字，记住它）
3. 选 **Public**
4. **不要**勾选 Add a README file / Add .gitignore / Choose a license
   （保持空仓库，否则第一次 push 会因「远程有你本地没有的提交」被拒绝）
5. 点 Create repository

### 第四步：关联远程并首次推送

回到本地，把 `<用户名>` 和 `<仓库名>` 换成你的：

```bat
git remote add origin https://github.com/<用户名>/stock-dashboard.git
python deploy.py
```

首次推送会弹出 GitHub 登录窗口（Git Credential Manager），
点 **Sign in with your browser**，浏览器里授权一次，之后就再也不用登录了。

> 如果没弹窗、直接报 `Authentication failed`：GitHub 早就停用了账号密码方式。
> 要么用上面的浏览器登录，要么去 GitHub → Settings → Developer settings →
> Personal access tokens 生成一个带 `repo` 权限的 token，在提示输密码时粘贴它。

看到 `推送成功` 就好了。

### 第五步：打开 Pages

1. 进入仓库页面，点顶部的 **Settings**（齿轮图标）
2. 左侧栏找 **Pages**（在 Code and automation 那一组里）
3. **Source** 选 `Deploy from a branch`
4. **Branch** 下拉选 `main`，右边的目录下拉从 `/ (root)` 改成 **`/docs`**
5. 点 **Save**

**等 1~3 分钟。** 页面顶部会出现一行
`Your site is live at https://<用户名>.github.io/stock-dashboard/`，
点它就能看到。第一次可能要刷新几次才生效，这是正常的。

如果 3 分钟后还是 404，去仓库的 **Actions** 标签页看有没有报错的 deploy 记录。

### 之后的日常

什么都不用改。每天还是双击 `daily.bat`，它会自动跑完三步并推送：

```
[1/3] main.py       报告 + 指标库
[2/3] dashboard.py  仪表盘（自动开浏览器给你看）
[3/3] deploy.py     复制成 docs/index.html 并 push
```

朋友那边的固定网址，刷新就是最新的。

不想推送的日子（比如只想本地看看）：`daily.bat --no-push`

### 常见问题

| 现象 | 原因 / 解决 |
| :--- | :--- |
| `deploy.py` 说「不是 git 仓库」 | 没初始化，跑 `python deploy.py --init` |
| `deploy.py` 说「没有配置远程地址」 | 没做第四步，跑 `git remote add origin ...` |
| `Authentication failed` | 密码方式已停用，用浏览器登录或 Personal Access Token |
| `Could not resolve host` | 网络不通，国内访问 GitHub 常超时，挂代理或稍后重试 `python deploy.py` |
| `rejected / fetch first` | 远程有本地没有的提交（比如你在网页上改过文件），先 `git pull --rebase origin main` |
| 页面 404 | Pages 还没生效（等 3 分钟），或分支/目录选错，或仓库是 Private |
| 页面还是旧日期 | 看右下角角标的数据日期；如果是旧的，说明当天 push 没成功，看终端输出 |
| 每天推送失败但本地正常 | 这是设计好的：发布失败绝不影响本地报告。修好网络后 `python deploy.py` 补推 |

### 仓库体积会慢慢变大

每天推送一个约 1 MB 的 `index.html`，git 历史里会各留一份：

- 一年约 250 个交易日 → 仓库增长约 **250 MB**
- GitHub 对单个文件限 100 MB、仓库建议 1 GB 以内，所以能撑几年

如果哪天觉得太大，可以重建历史（**会清空所有提交记录**，但文件都还在）：

```bat
git checkout --orphan clean
git add -A
git commit -m "reset history"
git branch -D main
git branch -m main
git push -f origin main
```

`docs/index.html` 会被保留，Pages 照常工作。

### deploy.py 的全部参数

```bat
python deploy.py                 :: 发布最新一份
python deploy.py --dry-run       :: 演练：只准备文件，不 commit 不 push
python deploy.py --check         :: 只做环境自检，不动文件
python deploy.py --init          :: 首次使用：初始化仓库
python deploy.py --no-banner     :: 不在网页右下角加「数据日期」角标
```

> 角标是给朋友看的：他分不清这份报告是今天的还是三天前的。
> 用的是 `position:fixed`，不影响任何图表布局。

---


## 九、同花顺扶摇 API（可选第二数据源）

**东财仍是主数据源。** 同花顺只在三个地方补位，任何一处失败都会安静地退回东财，
不影响原有流程：

| 能力 | 解决的问题 | 入口 |
| :--- | :--- | :--- |
| **降级兜底** | 东财某个池子重试 4 次仍返回空 → 自动切同花顺 | `data_fetch.py` 的 `_fetch_generic` |
| **30 天历史补全** | 东财只留 20 个交易日 → 用同花顺连板天梯补满 | `backfill_history.py` |
| **游资官方标签** | 以前靠 `YOUZI_MAP` 猜营业部对应哪个游资 | `lhb.try_ths_youzi` |

### 第一步：安全配置 API Key（**必读**）

Key 从 https://fuyao.aicubes.cn/admin 签发。**绝对不要写进代码**，两种方式二选一：

**方式 A：环境变量（推荐）**

```bat
:: 只执行一次，然后**重开终端 / 重启 IDE**
setx FUYAO_API_KEY "你的key"
```

验证（在**新开**的窗口里）：

```bat
python -c "import os;k=os.environ.get('FUYAO_API_KEY','');print('已配置' if k else '未配置', k[:6])"
```

> `setx` 写的是注册表里的用户环境变量，**已经打开的终端读不到**。
> 不重启终端就运行，会一直显示「未配置」——这是最容易卡住的一步。

**方式 B：local_secret.py**

项目里已经有 `local_secret.py.example`，复制成 `local_secret.py` 后填 Key：

```python
FUYAO_API_KEY = "你的key"
```

`local_secret.py` 已加入 `.gitignore`，不会被推到 GitHub。
优先级：环境变量 > `local_secret.py`。

**验证配置是否生效**：

```bat
python ths_fetch.py            :: 离线自检（不联网，验证 Key / 日期工具 / 映射）
python ths_fetch.py --live     :: 联网实测（会打印真实返回）
```

### 第二步：补全 30 天历史

```bat
python backfill_history.py --dry-run    :: 先看会补哪些天，不写文件
python backfill_history.py              :: 正式补（只补空缺日期）
python backfill_history.py --no-down    :: 不抓跌停池，速度约快一倍
```

跑完再执行 `python dashboard.py`，情绪周期图立刻从 14 天变成最多 30 天。

**三条不可妥协的规则**：

1. **绝不覆盖已有样本**。`main.py` 每天跑的样本含「赚钱效应」，同花顺给不了。
   回填只补空缺日期，已有的一律跳过（要覆盖得显式加 `--force`）。
2. **不编造缺失字段**。同花顺没有「昨日涨停股今日涨跌幅」，所以回填行的
   「赚钱效应」「上涨占比」就是空值。图上的 tooltip 会显示 null，这是数据边界。
3. **回填的情绪是近似值**。少了赚钱效应这一项，只能用涨停数+炸板率判定，
   比真实判定更保守。**这些天的档位只用来给柱子上色，不要拿它做交易决策。**

### 实测验证记录（2026-11-11，真实 Key）

拿 2026-11-09 做了两源交叉验证，结果完全吻合：

| 指标 | 同花顺 | 东财 | 结论 |
| :--- | ---: | ---: | :--- |
| 涨停家数 | 54 | 54 | 一致 |
| 涨停股代码集合 | 54 只 | 54 只 | **交集 54，双向差集为空** |
| 连板分布 | {1:45, 2:8, 6:1} | {1:45, 2:8, 6:1} | 一致 |
| 炸板家数 | 44 | 44 | 一致 |
| 跌停家数 | 1（凤凰航运） | 1 | 一致 |
| 封板率 / 炸板率 | 55.10% / 44.90% | 55.10% / 44.90% | 一致 |
| 最高连板 | 6（闽发铝业） | 6 | 一致 |

`date_ms` 时区验证：传 `2026-11-09`，返回的 `trade_date` 就是 `2026-11-09`，**没有错位**。
`window.date_list` 实测 **30 条**（20260923~20261111），确认是完整 30 个交易日。

**联调中发现并修复的 5 个问题**（都是不报错的类型）：

1. **游资榜 `stock_items` 没有 `limit_reason`**
   `all` / `org` 榜的 `stock_items[]` 实测不含 `limit_reason`（只有 `hot_money` 的 `rows[]` 有）。
   原代码只读该字段，导致「上榜原因」全空。改为 `limit_reason → concept_list → 当日榜/3日榜` 三级兜底。

2. **龙虎榜去重丢记录**
   `all` 榜 `count=62` 而 `stock_count=60`——同一只票同时上「当日榜」和「3 日榜」。
   `clean_stock_df(dedup=True)` 会静默砍掉一条。龙虎榜改为 `dedup=False`。

3. **游资榜混进退市股**
   实测漏出 `萃华退(002731)`。游资榜返回的是拼接字符串，绕过了 `clean_stock_df`，
   现单独加 `_is_valid_stock()` 按同口径过滤。

4. **情绪近似判定全变 `—`**
   `analyzer._judge_emotion()` 返回的是 **4 元组**（后来加了 `status_reason`），
   代码按 3 元组解包 → `ValueError` → 被 `except` 吞成 `—`。
   改为 `res[0]`，不依赖元组长度。

5. **桑基图/热力图全空**
   两个原因叠加：`_seat_only()` 只认来源以「席位」开头的记录，同花顺的「同花顺游资榜」被过滤；
   更根本的是游资榜返回的是「游资聚合」记录（一个游资+多只票），**没有单票金额**。
   现改为按 `rows[]` 拆成「游资 × 股票 × 金额」三元组（实测 14 个游资 / 46 条记录）。

另外补了一个数据卫生保护：**盘中跑回填会跳过今天**（15:00 前）。
否则半天的数据会被当成完整交易日写进指标库，污染后续所有分位数校准，且数字看起来很正常、很难发现。

### 真实的接口与字段（已核对官方文档）

> 以下字段全部核对于 https://fuyao.aicubes.cn 的 `llms-full.txt`（2026-11-11），
> 不是推测。Base URL `https://fuyao.aicubes.cn`，请求头 `X-api-key`。

| 用途 | 端点 | 关键字段 |
| :--- | :--- | :--- |
| 涨停池 | `GET /api/a-share/special-data/limit-up-pool` | `ticker` `name` `price_change_ratio_pct` `limit_up_time` `seal_money` `continue_day_cnt` `continue_day_text` `limit_up_reason` `open_times` |
| 炸板池 | `.../limit-break-pool` | `open_times` `first_seal_time` `turnover` `turnover_ratio_pct` |
| 跌停池 | `.../limit-down-pool` | `first_limit_time` `last_limit_time` `turnover_ratio_pct` |
| 连板天梯 | `.../limit-up-ladder` | `window.date_list` + `item[].boards.{two_board…seven_over}[].{ticker,name,board_num}` |
| 涨停统计 | `.../limit-up-statistics` | `seal_rate`（**小数**）`limit_up_stock_count` `open_stock_count` `touched_limit_up_stock_count` `exact_boards[]` |
| 龙虎榜 | `.../dragon-tiger-list?board_type=all\|org\|hot_money` | `stock_items[]`（股票维度）、`hot_money_items[].{name,buying,rows[]}`（**游资维度**） |

三个必须记住的坑（都已处理）：

1. **`seal_rate` 是小数**（0.6479 而不是 64.79），直接当百分比用会让封板率永远显示 0.6% 且不报错。
2. **`change` 也是小数**（0.09994），同样要 ×100。
3. **涨停池分页**：`size` 上限 200，默认 50。只取第一页会静默丢掉几十只票——图看着正常，就是少了几家涨停。代码里翻到底并主动限速。

### 和东财的差异（不要忽略）

| 差异 | 影响 |
| :--- | :--- |
| 涨停池**没有行业字段**，只有 `limit_up_reason`（涨停原因） | `config.THS_USE_REASON_AS_SECTOR=True` 时用它顶替「所属行业」。主线统计实际变成了「题材统计」——对超短线更贴切，但**口径变了**。改成 `False` 则留空 |
| **没有「昨日涨停股今日涨跌幅」** | 赚钱效应无法从同花顺得到，回填历史的那部分是空的 |
| 龙虎榜**不给营业部席位名**，只给游资名 + 聚合金额 | 这反而是好事：不用再猜营业部对应哪个游资。但「席位明细」这类信息没有 |
| 连板天梯**只含 2 板及以上** | 涨停总家数必须从 `limit-up-statistics` 拿，不能 sum 天梯的家数（否则 30 家涨停会算成 5 家） |
| `seven_over` 是「7 板及以上」合并档 | 极端行情下真实最高板可能高于显示值 |

### 关闭同花顺

想彻底退回纯东财模式，什么都不用改代码：

```bat
setx FUYAO_FALLBACK 0
```

或者在 `config.py` 里把 `THS_ENABLE_FALLBACK` 改成 `False`。
关闭后 `backfill_history.py` 会直接退出并说明原因，其余功能一切照旧。

### 排错

| 现象 | 原因 / 解决 |
| :--- | :--- |
| `ths_fetch.py` 自检显示「未配置」 | `setx` 后没重开终端，或 Key 写在了 `local_secret.py` 但文件没保存 |
| 降级日志显示「同花顺 Key 无效」 | Key 过期或无该接口权限，去 admin 页重新签发 |
| 回填报 `code=1002` | 该日期不是交易日，或超出接口一年范围 |
| 回填很慢 | 30 天 ×（统计+跌停）= 60 次请求。用 `--no-down` 减半 |
| 自动降级没生效 | 检查 `FUYAO_FALLBACK` 是否为 1；东财若只是**部分票缺失**而非返回空表，不会触发降级（这是刻意的，避免两源数据混用） |

---

*本项目仅用于复盘研究与情绪量化，不构成任何投资建议。*
