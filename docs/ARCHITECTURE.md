# 架构说明 · AccountBook Desktop

> 面向维护者。阅读顺序建议：本文件 → [DATA_MODEL](DATA_MODEL.md) → [DESIGN_SYSTEM](DESIGN_SYSTEM.md) → [PLAN](PLAN.md)。

---

## 1. 整体形态：不是 Electron，也不是纯 Qt

```
┌─────────────────────────────────────────────────────────────────┐
│  AccountBook.exe（PyInstaller onedir）                          │
│                                                                 │
│  ┌───────────────────────┐        ┌──────────────────────────┐  │
│  │  桌面外壳 shell/       │        │  本地 HTTP 服务 api/      │  │
│  │  pywebview + WebView2 │◀──URL──│  FastAPI + Uvicorn       │  │
│  │  DWM 美化 / 托盘       │        │  127.0.0.1 : 随机端口     │  │
│  └───────────┬───────────┘        └────────────┬─────────────┘  │
│              │ 加载页面                        │ 同源 REST/SSE   │
│              ▼                                 ▼                │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  React 前端（构建产物内嵌于 resources/web_dist）              │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

**为什么这样切分**

| 备选方案 | 放弃原因 |
|---|---|
| Electron / Tauri | 需要 Node 或 Rust 运行时；与"Python 内核"的既定方向冲突 |
| PySide6 + QWidgets | Apple 级动效与图表表现力不足，开发量大 |
| PySide6 + QtWebEngine | 要把整个 Chromium 打进安装包（数百 MB） |
| **pywebview + 系统 WebView2** | ✅ 复用系统运行时；外壳仅 5MB 级；前端生态完整 |

代价与对策：pywebview 依赖 pythonnet/CLR，在受限环境可能无法初始化。
因此外壳被抽象为 `ShellAdapter`，失败时自动降级为 `BrowserShell`（用系统浏览器打开同一 URL），
功能完全等价 —— 业务代码一行都不用改。

---

## 2. 分层与依赖方向

```
api/  ──────▶ services/ ──────▶ db/ ──────▶ SQLite
 │                │
 │                └──────▶ core/（纯规则，无 IO）
 │
shell/ ◀── 仅通过 api/state.py::ShellBridge 协议被反向调用
```

**硬性规则**

1. `core/` 不 import 任何 IO 框架（FastAPI / webview / SQLAlchemy 一律禁止），
   因此它可以被完全单元测试覆盖，也被未来的算法模块安全复用。
2. `shell/` 与 `api/` **互不 import**，只通过 `AppContext` 与 `ShellBridge` 协议通信。
3. 前端只与 `api/` 通信，永远不直接触碰数据库或文件系统。
4. 金额在**所有**层都是整数最小单位（分）；浮点只允许出现在图表渲染的最后一步。

---

## 3. 启动与退出时序

```
run.py / AccountBook.exe
   │
   ├─ 1. paths.get_paths()          解析数据目录（便携 / 安装 / 环境变量覆盖）
   ├─ 2. paths.ensure()             建目录；失败 → 可读提示 + 退出码 2
   ├─ 3. logging_setup.setup_logging + install_excepthook
   ├─ 4. InstanceLock.acquire()     已有实例 → 提示并退出（避免 SQLite 写冲突）
   ├─ 5. ConfigStore(paths.config_file)  读偏好；损坏 → 回退默认值并记录
   ├─ 6. BackendServer.start()
   │      ├─ socket.bind(('127.0.0.1', 0))   ← 先占端口，再交给 uvicorn
   │      ├─ uvicorn.Server.run(sockets=[sock])（守护线程）
   │      └─ 轮询 /health 直到就绪（最多 5s）
   ├─ 7. create_shell()             WebViewShell 或 BrowserShell
   ├─ 8. config.subscribe(→ shell.apply_theme)   主题变更同步原生标题栏
   ├─ 9. shell.start()              阻塞：进入 GUI 事件循环
   └─ 10. finally: tray.stop() → server.stop() → lock.release()
```

**为什么先自己 bind socket**：让端口在 uvicorn 启动前就确定，
避免"启动后再去问 uvicorn 用了哪个端口"的竞态。

**为什么开窗前要探活**：WebView 一旦加载到尚未监听的端口就会显示错误页，
用户只会得出"应用坏了"的结论。花几毫秒换一次可靠等待是划算的。

---

## 4. 安全模型

### 4.1 威胁模型

环回地址**不等于**安全。三个真实威胁：

| 威胁 | 场景 | 对策 |
|---|---|---|
| 同机其它进程 | 任何本机程序都能访问 `127.0.0.1:<port>` | 一次性会话令牌 |
| CSRF | 恶意网页用 `fetch` 向本机服务发写请求（CORS 拦不住"发出"） | Origin/Referer 白名单 + SameSite=Strict Cookie |
| DNS rebinding | 攻击者域名解析到 127.0.0.1，浏览器带着攻击者的 Host 头访问本机 | Host 头必须命中环回地址集合 |

### 4.2 四道关卡（按请求顺序）

1. **Host 校验** —— `core/security.is_loopback_host()`；
2. **Origin 校验** —— 带 Origin/Referer 的请求必须精确命中 `http://127.0.0.1:<port>` 等白名单；
3. **令牌校验** —— 受保护路径必须携带有效令牌（`Authorization: Bearer` / `ab_session` Cookie / 首次导航的 `?token=`）；
4. **静态资源** —— 仅托管 `web_dist` 内的文件，并用**解析后的绝对路径**做前缀判断防穿越。

### 4.3 受保护路径的判定

| 路径 | 是否需要令牌 | 理由 |
|---|---|---|
| `/health` | 否 | 启动自检与外部探活；只回固定的最小响应 |
| `/api/**` | 是 | 业务数据 |
| `/`、`/index.html`、无扩展名的 SPA 深链 | 是 | 其中注入了会话令牌，不能匿名获取 |
| 带扩展名的静态资源（.js/.css/.woff2/.svg） | 否 | 只是我们自己的前端代码，不含用户数据；放行可彻底避免"Cookie 未生效 → 白屏" |

### 4.4 令牌生命周期

`secrets.token_urlsafe(32)`，**仅在内存中**，进程退出即失效，绝不写入磁盘或 localStorage。
`SessionToken.__repr__` 已重写为打码形式，防止它意外出现在日志或异常里。

### 4.5 响应头

由 `api/server.py::SECURITY_HEADERS` 统一注入：

* `Content-Security-Policy` —— 其中 `connect-src 'self'` 是"不上云"的**技术**保证，
  即使未来误引入第三方脚本，页面也无法把账目发往外部地址；
* `X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Permissions-Policy` 关闭一切浏览器特性。

---

## 5. 数据存放

### 5.1 选择策略：逐个探测、真实写入、明确告知

首选位置**不可写时不再直接失败**，而是按优先级尝试候选目录，
每个候选都做**真实写入探测**（不是 `os.access` 那种不可靠的判断），
第一个通过者胜出。最终结果连同"试过哪里、为什么失败"一起记录下来。

**安装模式**

| 顺序 | 位置 | 说明 |
|---|---|---|
| 1 | `%LOCALAPPDATA%\AccountBook` | 首选：本机数据语义，不漫游 |
| 2 | `%APPDATA%\AccountBook` | 漫游目录 |
| 3 | `%USERPROFILE%\Documents\AccountBook` | 用户可见、易备份 |
| 4 | `<程序目录>\data` | 程序被放在可写位置时可用 |
| 5 | `%TEMP%\AccountBook` | 最后手段；可能被系统清理 |

**便携模式**（存在 `portable.flag`）：`<程序目录>\data` 优先，其后同上。

**显式指定**（`--data-dir` / `ACCOUNTBOOK_DATA_DIR`）：**不探测、不回退**。
用户已经明确表达了意图，悄悄换个位置存账本比直接报错更不可接受。

只有当**全部候选都不可写**时才会弹出致命错误对话框，
且消息中会列出完整的尝试记录（路径 + 来源 + 具体错误）。

### 5.2 为什么这么做

"双击一个程序，只看到一个失败对话框"是最糟糕的用户体验，
而把账本放到第二、第三备选位置，用户几乎没有损失。
反过来，**默默换了位置却不告诉用户**同样不可接受 —— 用户会以为数据丢了。

因此选中的位置、来源与尝试记录通过三条路径暴露出来：

* 启动日志（`data_dir` / `data_dir_source` / `degraded` 三行）；
* `GET /api/system/info` → `paths.data_dir_source` 与 `paths.data_dir_attempts`；
* 概览页「运行状态」卡片：降级时显示黄色提示、当前来源，并可展开查看被跳过的位置。

### 5.3 目录布局

```
<data>/
├─ accountbook.db        主库（P1 起）
├─ config.json           用户偏好（原子替换写入）
├─ runtime-cache.json    机器事实缓存（如 CLR 运行时选择）
├─ attachments/          附件、用户卡面
├─ backups/              *.abk 加密备份
├─ logs/                 轮转日志 + crash-*.txt
├─ plugins/              用户安装的插件实例
├─ models/               ML 模型产物（P7+）
├─ exports/              导出的报表
├─ cache/                可重建缓存
└─ webview/              WebView2 用户数据（localStorage / Cookie 持久化）
```

`webview/` 目录不可省略：若不设置 `private_mode=False` + `storage_path`，
localStorage 每次启动都会清空，"记住用户选择的主题"就无法实现。

---

## 6. 关键实现决策

| 决策 | 取舍 |
|---|---|
| 自研 i18n（60 行）而非 i18next | 只需类型安全键 + 插值；省下 ~40KB 运行时。键集合由 `satisfies Messages` 在构建期校验 |
| 自绘图标而非图标库 | 离线要求；风格统一（24 网格 / 1.6 描边）；体积 < 6KB |
| 微图表用 SVG、分析图表用 ECharts | KPI 卡片数量多、尺寸小、无需交互，SVG 更快且样式自动跟随主题 |
| 偏好只存后端，不写 localStorage | 避免两份真源；便携模式下配置随 U 盘移动，语义更清晰 |
| 乐观更新 + 失败回滚 | 界面即时响应；失败时回滚并告警，绝不停留在"假的成功"状态 |
| 保留原生窗口边框 + DWM 美化 | 无边框窗口在贴靠 / 缩放 / 多屏移动上坑多；圆角与深色标题用 DWM 属性即可，风险最低 |
| K 线用 `asset_snapshots` 做唯一时序源 | 避免每次全表重算；聚合结果缓存进 `asset_ohlc` |

---

## 7. 扩展点（为后续阶段预留）

| 扩展点 | 位置 | 阶段 |
|---|---|---|
| 新外壳（Qt / 纯浏览器 / 移动端） | 实现 `ShellAdapter` 协议 | 任意 |
| 新图表类型 | `web/src/components/`（ECharts 主题见 `design/tokens.ts`） | P2 |
| 新主题（自定义配色） | 覆盖 `tokens.css` 的 CSS 变量 | P9 |
| 新设置项 | `config.UserPreferences` + `api/routes/system.py` 的 `PreferencesPatch` | 任意 |
| 新算法 / AI 提供方 | 实现 `IntelligenceProvider` | P7 |
| 新导入格式 | 注册到导入解析器表 | P8 |
| 插件 | `plugins_sdk` + 钩子注册表 | P9 |

**新增一个侧边栏模块的完整步骤**（P0 起即可用）：

1. `web/src/app/navigation.ts` 加一行（id / path / icon / phase）；
2. `web/src/i18n/zh-CN.ts` 与 `en-US.ts` 同时补 `nav.<id>` 与 `scope.<id>`（漏译会在构建期报错）；
3. 实现页面组件，并在 `web/src/routes.tsx` 的 `IMPLEMENTED_ROUTES` 中登记。

导航、路由、阶段徽标、占位页会**自动**同步，不存在"菜单里有但点进去 404"的可能。

---

## 8. 外壳选择与 CLR 运行时（重要，排障必读）

原生窗口依赖 `pywebview → pythonnet → 一个 .NET 运行时`。这条链上有三个**彼此独立**
的失败点，任何一处出问题都会导致"应用没有窗口"。因此本项目实现了三级选择与显式降级。

### 8.1 选择顺序

```
create_shell()
 ├─ 1. ACCOUNTBOOK_SHELL=browser ？        → BrowserShell
 ├─ 2. 非 Windows ？                       → BrowserShell
 ├─ 3. resolve_clr_runtime() 返回 None ？  → BrowserShell
 ├─ 4. import webview 失败 ？              → BrowserShell
 └─ 5. WebViewShell（原生窗口）
        └─ start() 抛异常 ？ → 记日志 + BrowserShell（第二次机会）
```

### 8.2 CLR 运行时探测（`shell/clr_runtime.py`）

| 运行时 | 说明 |
|---|---|
| `netfx`（.NET Framework） | pythonnet 的默认选择；pywebview 的 WinForms + WebView2 封装**只**在它下面兼容 |
| `coreclr`（.NET 6+） | 备选；需要额外的宿主配置才能加载 WinForms |

探测**必须在子进程中进行**：一个进程只能宿主一个 CLR，在当前进程里先失败再切换，
第二次初始化大概率失败且状态已被污染。结论用**退出码**传递
（windowed 打包产物没有 stdout，退出码是唯一可靠的跨形态信道），
结果缓存到 `<data>/runtime-cache.json`，之后启动零额外开销。

### 8.3 已修复的两个真实缺陷

1. **CoreCLR 缺 WinForms**：pythonnet 默认的 CoreCLR 配置只引用 `Microsoft.NETCore.App`，
   其中没有 `System.Windows.Forms`。因此随包提供
   `resources/clr/python.runtimeconfig.json`，改为引用
   `Microsoft.WindowsDesktop.App`（`rollForward: LatestMajor` 以兼容 .NET 6/7/8/9），
   并通过 `PYTHONNET_CORECLR_RUNTIME_CONFIG` 生效。
2. **pywebview 漏声明 CLR 引用**：`webview/platforms/winforms.py` 会
   `from Microsoft.Win32 import SystemEvents`，但它只为
   `System.Windows.Forms / System.Collections / System.Threading / System.Reflection`
   调用了 `clr.AddReference`。在 netfx 下该类型随 `System.dll` 提供所以无碍；
   在 coreclr 下它是独立程序集，于是抛出极具误导性的
   *"You must have pythonnet installed"*。
   `WebViewShell._warm_up_clr()` 在 `webview.start()` 之前补上该引用。

### 8.4 已知限制：本机环境下的原生窗口

在**当前开发机**上，原生窗口的两条路径都被环境层面阻断，与上述代码无关：

| 路径 | 现象 | 判断 |
|---|---|---|
| `netfx` | `clr_loader` 的原生加载器 `pyclr_get_function` 返回空，无法解析 `Python.Runtime.Loader.Initialize`；显式提供 `netfx.config`（`supportedRuntime v4.0` + `useLegacyV2RuntimeActivationPolicy`）亦无效 | `Python.Runtime.dll` 确认为 `.NETStandard,Version=v2.0`，理论上可被 .NET Framework 4.8 加载，问题出在 clr_loader 的原生宿主侧，不在本项目代码内 |
| `coreclr` | CLR 与 WinForms 均可加载（见 8.3 的修复），但随后 `Microsoft.Web.WebView2.WinForms` 报 `Could not load type 'System.Windows.Forms.ContextMenu'` | 该类型在 .NET Core 中已被移除，说明 pywebview 随包的 WebView2 WinForms 封装是 .NET Framework 目标，**coreclr 路线在 pywebview 6.2.1 上走不通** |

**后果**：应用自动降级为浏览器外壳并正常打开界面 —— 同一套前端、同一套本地服务、同样的令牌，
功能完全等价。概览页的「运行状态」会明确显示"界面外壳：系统浏览器（已降级）"并给出修复提示。

**在大多数 Windows 机器上 `netfx` 是正常的**，原生窗口会直接可用；
本节的记录是为了让后续维护者不必重新踩一遍这两个坑。

### 8.5 已知限制（功能层面）

* 未实现原生窗口的无边框外观（当前使用原生边框 + DWM 圆角/深色标题）；
* 托盘仅在 Windows 生效，且尚未接入"最小化到托盘"的窗口事件（P6）；
* 除 Windows 外的平台一律使用浏览器外壳；
* 数据库尚未建立（P1 落地），因此概览页的 KPI 显示为 `—` 而非假数据；
* ECharts 尚未接入（P2），当前趋势图为标注了 `DEMO` 水印的自研 SVG 示意。

上述每一条都在 [PLAN.md](PLAN.md) 中有明确的阶段归属。
