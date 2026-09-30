# P0 验收报告 · 基座骨架

> 交付日期：2026-09-30
> 版本：`0.1.0` · 阶段 `P0`
> 对应计划：[PLAN.md](PLAN.md) 第 13 节 · 功能清单：[FEATURES.md](FEATURES.md#p0--基座骨架已交付)

---

## 1. 验收结论

**P0 通过。** 基座骨架可用、可打包、可测试，且所有 P0 承诺项均已落地。
唯一的环境层面偏差（原生窗口在本开发机上自动降级为浏览器外壳）已在
[ARCHITECTURE.md § 8.4](ARCHITECTURE.md#84-已知限制本机环境下的原生窗口) 中完整记录，
并已在界面中显式呈现给用户。

---

## 2. 验收项逐条核对

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 应用可启动并加载界面 | ✅ | 本地服务 + 前端静态托管；`/health` 返回 `{"status":"ok","version":"0.1.0","phase":"P0"}` |
| 2 | 日/夜/跟随系统主题可切换并持久化 | ✅ | `PATCH /api/system/preferences` 写入 `config.json`；重启后沿用（pytest `TestPreferences`） |
| 3 | 主题同时作用于 Windows 原生标题栏 | ✅ | `config.subscribe → shell.apply_theme → DwmSetWindowAttribute(20)` 链路（DWM 不可用时静默跳过） |
| 4 | 本地服务安全边界生效 | ✅ | 无令牌 → 401；非环回 Host → 403；非白名单 Origin → 403；越权路径穿越被拦截（`tests/test_api.py`） |
| 5 | 数据目录双模式 + 不可写降级 | ✅ | `tests/test_paths.py` 覆盖安装版 / 便携版 / 环境变量覆盖 / 便携目录不可写降级 |
| 6 | 数据与密钥被 git 隔离 | ✅ | `.gitignore` 排除 `data/`、`*.db`、`*.abk`、备份、附件、日志、模型、`dist/`、构建产物与本地临时目录 |
| 7 | 中英双语可切换，漏译会在构建期暴露 | ✅ | `en-US.ts` 使用 `satisfies Messages`；`tsc --noEmit` 通过 |
| 8 | 深浅两套主题视觉验收 | ✅ | [docs/screenshots](screenshots) 中的 5 张截图（浅色/深色仪表盘、占位页、设置、关于） |
| 9 | 后端测试全绿 | ✅ | `pytest` **72 项全部通过** |
| 10 | 静态检查与格式化通过 | ✅ | `ruff check` 与 `ruff format --check` 无告警 |
| 11 | 前端类型检查与构建通过 | ✅ | `npm run build`（含 `tsc --noEmit`）产出 ~27KB CSS + ~380KB JS（未压缩） |
| 12 | **PyInstaller 打包 + 冒烟测试通过** | ✅ | onedir 产物 **49.2 MB**；冒烟测试：启动 → 本地服务就绪 → `/health` 正常 → 无令牌首页返回 401 |
| 13 | 打包产物不暴露控制台窗口 | ✅ | `console=False`（windowed）；诊断输出经 `_emit()` 保护，`sys.stdout is None` 时不会崩溃 |
| 14 | 引导期异常可观测 | ✅ | `--` 崩溃报告写入 `%TEMP%/AccountBook-startup-error.txt` 后再弹窗（本次开发中实际捕获到 1 个真实缺陷） |

---

## 3. 交付物清单

### Python 包 `src/accountbook/`

| 模块 | 职责 |
|---|---|
| `__init__.py` | 版本、应用标识、当前阶段（单一真源） |
| `__main__.py` | 入口：参数解析、内部探测开关、引导期崩溃报告、`--doctor` / `--print-paths` |
| `app.py` | 编排：日志 → 单实例 → 后端 → 外壳 → 生命周期收敛；外壳失败自动降级 |
| `paths.py` | 双模式数据目录、只读资源定位、可写性探测与降级 |
| `config.py` | 运行时设置（env）+ 用户偏好（原子写 JSON、向前兼容、变更订阅） |
| `logging_setup.py` | 轮转日志、崩溃转储、脱敏工具、全局/线程异常兜底 |
| `core/security.py` | 会话令牌、恒定时间比较、Host/Origin 白名单判定 |
| `core/single_instance.py` | 基于文件锁的单实例守卫（崩溃后由系统自动释放） |
| `api/server.py` | FastAPI 装配、四道访问关卡、安全响应头、SPA 托管与启动注入 |
| `api/routes/system.py` | 运行信息与偏好读写、打开数据目录 |
| `api/state.py` | 应用上下文与 `ShellBridge` 协议 |
| `shell/window.py` | `ShellAdapter` / `WebViewShell` / `BrowserShell`、CLR 引用预热、窗口几何持久化 |
| `shell/dwm.py` | Windows 圆角、深色标题栏、DPI 感知、主题解析 |
| `shell/clr_runtime.py` | .NET 运行时探测、缓存与宿主配置 |
| `shell/tray.py` | 系统托盘（pystray 可选，缺失即静默跳过） |

### 前端 `web/`

设计令牌（`styles/tokens.css` + `tailwind.config.js` + `design/tokens.ts`）、动效预设、
偏上下下文（乐观更新 + 失败回滚）、自研 i18n（构建期键校验）、
30+ 自绘 SVG 图标、基础组件库、自研 SVG 微图表、
导航清单（26 模块 · 阶段徽标）、侧边栏 / 工具栏 / 外壳布局、
概览仪表盘、设置页、关于页、通用占位页、路由表。

### 工程与文档

`.gitignore`、`pyproject.toml`、`run.py`、
`packaging/`（`build_frontend.ps1`、`build_backend.ps1`、`accountbook.spec`、`version_info.txt`、`entry.py`）、
`scripts/generate_icon.py`（程序化图标，无第三方素材）、
`tests/`（72 项用例）、
`docs/`（PLAN、ARCHITECTURE、DESIGN_SYSTEM、FEATURES、ROADMAP、ACCEPTANCE_P0、screenshots）。

---

## 4. 本次开发中修复的真实缺陷（保留记录）

| 缺陷 | 影响 | 修复 |
|---|---|---|
| Tailwind 颜色表漏注册 `hairline` | 构建期报 `bg-hairline does not exist`，且报错位置指向使用者而非定义处 | 在 `tailwind.config.js` 注册，并在注释中写明"变量与注册必须成对出现" |
| PowerShell 脚本无 BOM | 中文注释被 PS 5.1 按 GBK 解析 → 大面积语法错误 | 统一改为 **UTF-8 with BOM** |
| `npm` 沙箱管道限制 | `spawn EPERM` 导致依赖安装失败 | 记录并规避（详见提交说明）；改用可用镜像源 |
| PyInstaller 版本资源校验过严 | `0.1.0` 与 `0.1.0.0` 被判为不一致而中止打包 | 归一化到四段比较，并新增"版本号必须为纯数字"的显式校验 |
| 冒烟测试误判 | `-WindowStyle Hidden` + `HasExited` 轮询把"正常运行"判为"已退出" | 改为重定向输出 + 超时等待；且数据目录改放仓库内（`%TEMP%` 在受限环境下不可写） |
| CoreCLR 缺 WinForms | coreclr 能启动但 `System.Windows.Forms` 加载失败 | 随包提供 `python.runtimeconfig.json`（引用 `Microsoft.WindowsDesktop.App`） |
| pywebview 漏声明 `SystemEvents` | coreclr 下报误导性的 "pythonnet not installed" | `_warm_up_clr()` 在开窗前补上引用 |
| windowed 构建静默退出 | 用户双击无任何反应、无任何线索 | 新增引导期崩溃报告（先落盘、再弹窗；有控制台时不弹窗以免阻塞自动化） |
| 主按钮禁用态无视觉差异 | "记一笔"在 P0 不可用却看起来可点 | 降级为次级按钮 + 阶段徽标；并补齐 `disabled` 样式 |

---

## 5. 验证命令（可复现）

```powershell
# 环境自检
python run.py --doctor

# 只起服务（用于端到端验证 / 自带浏览器）
python run.py --serve-only --data-dir .\data

# 后端测试与静态检查
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests run.py packaging\entry.py
.\.venv\Scripts\python.exe -m ruff format --check src tests

# 前端类型检查与构建
cd web; npm run build

# 打包 + 冒烟测试
powershell -ExecutionPolicy Bypass -File packaging\build_backend.ps1
```

实测结果：`pytest` 72 passed；`ruff` All checks passed；
`npm run build` ✓ built；打包产物 49.2 MB 且冒烟测试通过。

---

## 6. 遗留事项（已转入后续阶段，非遗漏）

| 事项 | 归属 |
|---|---|
| 数据库与账目模型 | P1 |
| 快捷记账、流水三视图、分类树 | P1 |
| 卡片墙、GitHub 式日历、ECharts 图表体系 | P2 |
| 资产 K 线 | P3 |
| 存钱罐 | P4 |
| 报告引擎与台账 | P5 |
| 薪酬 / 五险一金 / 定时任务与发薪日强制收录 | P6 |
| AI 分析与 ML 插槽 | P7 |
| 导入导出、加密备份、DB 可视化 | P8 |
| 插件宿主与自定义 | P9 |
| 便携 zip + Inno Setup 安装程序 | P10 |
| 原生窗口在本机环境的启用 | 依赖 pywebview / pythonnet 上游修复；已降级并可视化提示 |
