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

| 运行方式 | 数据目录 |
|---|---|
| 安装版 | `%LOCALAPPDATA%\AccountBook\` |
| 便携版（存在 `portable.flag`） | 程序目录下的 `data\`；不可写时自动降级到用户目录并记录警告 |

目录内包含 `accountbook.db`、`attachments/`、`backups/`、`logs/`、`plugins/`、`models/`、`exports/`、`cache/` 与 `webview/`。
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
| P1 | 记账核心闭环：数据模型、流水、账户、分类、快捷记账 | ⏳ 计划中 |
| P2 | 资产卡片墙与可视化体系（含 GitHub 式日历） | ⏳ |
| P3 | 资产 K 线与长期趋势 | ⏳ |
| P4 | 存钱罐与目标储蓄 | ⏳ |
| P5 | 报告引擎与台账 | ⏳ |
| P6 | 薪酬、五险一金与定时任务 | ⏳ |
| P7 | AI 分析与智能预留 | ⏳ |
| P8 | 导入导出与数据治理 | ⏳ |
| P9 | 插件与自定义 | ⏳ |
| P10 | 打包、安装程序与打磨 | ⏳ |

界面截图见 [docs/screenshots](docs/screenshots)。

---

## 许可

本项目为个人使用工具。随包分发的第三方资源（字体、图标）各自遵循其原始许可，
详见 `docs/` 中的说明；应用图标与内置卡面均为**程序化自绘**，不含任何第三方商标素材。
