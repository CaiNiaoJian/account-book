# 记账本 · AccountBook Desktop

> 本地优先、离线可用、Apple 美学的个人记账与财务分析桌面工具。
> 账本只存在你自己的电脑上 —— 没有云同步、没有遥测、没有启动联网。

**当前阶段：P0（基座骨架）已交付。** 功能路线与验收标准见 [docs/PLAN.md](docs/PLAN.md)。

---

## 这是什么

一个 Windows 桌面应用，用 **Python 做内核**、**前端库做界面**：

| 层 | 技术 | 说明 |
|---|---|---|
| 桌面外壳 | pywebview + Edge WebView2 | 复用系统运行时，安装包体积小；不可用时自动降级为浏览器外壳 |
| 本地服务 | FastAPI + Uvicorn | 只监听 `127.0.0.1`，随机端口 + 每次启动的随机令牌 |
| 界面 | React 18 + TypeScript + Vite + Tailwind | Apple 风格设计令牌驱动，日/夜/跟随系统 |
| 图表 | ECharts + 自研 SVG 微图表 | 分析图表用 ECharts，KPI 卡片用轻量 SVG |
| 数据 | SQLite + SQLAlchemy + Alembic | 全本地，可在应用内可视化浏览 |

详细架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，设计规范见 [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md)。

---

## 快速开始

### 环境要求

* Windows 10/11 x64
* Python 3.11+（推荐 3.12）
* Node.js 18+（仅前端开发与构建需要）
* Microsoft Edge WebView2 Runtime —— Windows 11 默认已安装；缺失时程序会自动降级为浏览器外壳

### 从源码运行

```powershell
# 1. 创建虚拟环境并安装 Python 依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"   # 或按需单独安装

# 2. 构建前端（首次必须执行）
powershell -ExecutionPolicy Bypass -File packaging\build_frontend.ps1

# 3. 启动
.\.venv\Scripts\python.exe run.py
```

### 常用命令

```powershell
python run.py                    # 正常启动（原生窗口）
python run.py --dev              # 配合 `cd web; npm run dev` 做前端热更新
python run.py --serve-only       # 只起本地服务，不建窗口（自动化 / 自带浏览器）
python run.py --doctor           # 环境自检：Python、依赖、WebView2、数据目录可写性
python run.py --print-paths      # 打印路径与运行模式（JSON），排查用
python run.py --data-dir D:\账本  # 指定数据目录（多档案隔离）
```

### 打包

```powershell
# 前端 → 后端 → 便携 zip / 安装包（一键）
powershell -ExecutionPolicy Bypass -File packaging\build_all.ps1

# 仅打包后端（含冒烟测试）
powershell -ExecutionPolicy Bypass -File packaging\build_backend.ps1
```

---

## 数据放在哪里

应用会**按优先级探测多个位置并真实写入验证**，第一个可用者即为数据目录：

| 顺序 | 安装版 | 便携版 |
|---|---|---|
| 1 | `%LOCALAPPDATA%\AccountBook\` | `<程序目录>\data\` |
| 2 | `%APPDATA%\AccountBook\` | `%LOCALAPPDATA%\AccountBook\` |
| 3 | `%USERPROFILE%\Documents\AccountBook\` | （同上，依次回退） |
| 4 | `<程序目录>\data\` | … |
| 5 | `%TEMP%\AccountBook\`（可能被系统清理） | … |

* 用 `--data-dir "D:\我的账本"` 可显式指定；**显式指定时不做任何回退**（尊重用户意图）。
* 一旦发生回退，概览页的「运行状态」会显示黄色提示：当前数据目录、来源，以及被跳过的位置与原因。
* 目录内包含 `accountbook.db`、`attachments/`、`backups/`、`logs/`、`plugins/`、`models/`、`exports/`、`cache/` 与 `webview/`。
  **这些内容全部被 [.gitignore](.gitignore) 排除**，不会被误提交到版本库。

---

## 隐私与安全

* 本地服务仅绑定 `127.0.0.1`，端口由系统随机分配；
* 每次启动生成一次性会话令牌；静态资源与接口双重校验 Host / Origin，阻断同机其它程序与网页的访问；
* 响应头强制 `connect-src 'self'`，页面无法把数据发往任何外部地址；
* 无遥测、无云同步、无启动联网；AI 分析与更新检查都是显式的用户动作，默认关闭；
* 卡号默认脱敏，另提供隐私模式（金额打码）供演示与公共场合使用。

细节与威胁模型见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#安全模型)。

---

## 常见问题

**双击程序后没有任何反应，或没有窗口？**

先看数据目录下的日志：`<数据目录>\logs\accountbook.log`（数据目录位置见上一节）。
常见原因与对策：

| 日志中的迹象 | 原因 | 对策 |
|---|---|---|
| `已选定 CLR 运行时：coreclr` 且随后 `桌面外壳启动失败` | 程序所在目录被 .NET Framework 判定为**网络位置**（网络驱动器、映射盘，或被安全策略/沙箱标记的目录）。.NET Framework 会拒绝加载该目录下的程序集，于是 netfx 宿主不可用 | 把程序移动到本地磁盘的普通目录（例如 `C:\Apps\AccountBook`），删除数据目录下的 `runtime-cache.json` 后重新启动 |
| 弹出「请在浏览器中打开界面」提示框 | 原生窗口不可用，且未能确认浏览器窗口出现 | 提示框里有可复制的地址，粘贴到浏览器即可使用；点「取消」会退出程序（不会残留后台进程） |
| `未找到可用的 .NET 运行时` | 未安装 .NET Framework 4.7.2+ 或 .NET 6+ 桌面运行时 | 安装任一运行时后重试 |

**提示**：无论是否降级，应用**一定会给你可见的反馈** —— 要么原生窗口，要么浏览器，
要么一个带地址的提示框。绝不会静默变成一个看不见的后台进程。

---

## 项目结构

```
accountbook/
├─ src/accountbook/         Python 包（paths / config / logging / core / api / shell）
│   └─ resources/           只读资源：图标、字体、前端构建产物 web_dist
├─ web/                     React 前端（design / components / app / pages / i18n）
├─ packaging/               构建脚本、PyInstaller spec、版本资源
├─ scripts/                 开发辅助脚本（图标生成等）
├─ tests/                   pytest 用例
├─ docs/                    计划、架构、设计系统、功能清单、路线图
└─ data/                    运行时数据（git 忽略）
```

---

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 基座骨架：外壳、本地服务、设计系统、主题、i18n、导航 | ✅ 已完成 |
| **P1** | 记账核心闭环：数据模型、流水、账户、分类、快捷记账 | ✅ 已完成 |
| **P2** | 资产卡片墙与可视化体系（含 GitHub 式日历） | ✅ 已完成 |
| **P3** | 资产 K 线与长期趋势 | ✅ 已完成 |
| P4 | 存钱罐与目标储蓄 | ⏳ |
| P5 | 报告引擎与台账 | ⏳ |
| P6 | 薪酬、五险一金与定时任务 | ⏳ |
| P7 | AI 分析与智能预留 | ⏳ |
| P8 | 导入导出与数据治理 | ⏳ |
| P9 | 插件与自定义 | ⏳ |
| P10 | 打包、安装程序与打磨 | ⏳ |

**P1 已可真实使用**：建账户 → 记流水（快捷记账或详细填表，支持分账）→
看余额与本月收支。共 109 个内置分类、12 种币种、127 个自绘图标；
数据落在本地 SQLite，启动时自动建库与迁移。
验收记录见 [docs/ACCEPTANCE_P1.md](docs/ACCEPTANCE_P1.md)，
数据结构见 [docs/DATA_MODEL.md](docs/DATA_MODEL.md)。

**P2 已可用**：资产卡片墙（自绘卡面、拖拽排序、额度进度）、GitHub 式年历热力图
（六类颜色指标、事件角标、导出 PNG）、当日详情（余额阶梯曲线、构成、变动归因、事件日志）、
ECharts 图表（Apple 主题随日夜切换）。

验收记录见 [docs/ACCEPTANCE_P2.md](docs/ACCEPTANCE_P2.md)，
日历与日结指标口径见 [docs/CALENDAR_METRICS.md](docs/CALENDAR_METRICS.md)。

**P3 已可用**：资产 K 线（蜡烛图 + 均线 + MACD/RSI + 回撤曲线 + 事件打点，
长区间**自动降周期**并明确告知）、指标口径说明页。口径见
[docs/KLINE_METRICS.md](docs/KLINE_METRICS.md)。

**P4 已可用**：存钱罐（液面填充动画、里程碑庆祝、六种自动归集策略、
**线性 + 加权双口径**预计达成日、达成后一键生成购买支出）、
储蓄目标（进度来自关联账户的实时余额或手工注入）。
验收记录见 [docs/ACCEPTANCE_P4.md](docs/ACCEPTANCE_P4.md)。

**P1–P3 的收尾**（本轮补齐）：流水批量编辑与批量删除、列表与时间轴的
**虚拟滚动**、回收站集中页面（12 类软删除实体）、流水附件（魔数校验 +
体积上限 + 路径穿越防护）、私人卡面图片上传、列表 FLIP 动效、
K 线区间框选对比、应用内全局快捷键（`Ctrl/Cmd+K`）。

界面截图见 [docs/screenshots](docs/screenshots)。

---

## 许可

本项目为个人使用工具。随包分发的第三方资源（字体、图标）各自遵循其原始许可，
详见 `docs/` 中的说明；应用图标与内置卡面均为**程序化自绘**，不含任何第三方商标素材。
