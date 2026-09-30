# 个人记账本桌面工具 · 总计划（AccountBook Desktop）

> 版本：**v1.1**（在 v1.0 基础上并入 6 项新增需求）
> 状态：待确认
> 适用平台：Windows 10/11 x64（架构保留跨平台能力）

## 变更记录

| 版本 | 变更 |
|---|---|
| v1.0 | 初版：技术选型、数据模型、功能架构、设计系统、P0–P7 路线 |
| **v1.1** | 并入新增需求 15–20：资产卡片墙 · 存钱罐 · 资产 K 线与长期趋势 · 五险一金 · 定时任务与发薪日 · 日历可视化；阶段扩展为 P0–P10 |

### 新增需求解读（一处歧义已按最合理语义采纳）

> 原文「要把是否**登记手指**情况也作为颜色的变化指标之一」

按上下文判定为「是否**登记收支**情况」的输入法误写（shouzhi）。计划中采纳为：
**日历格子的颜色主指标之一 = 当日是否已完成记账登记**（未登记 / 部分登记 / 已完成三类）。
若你的原意不同，告知我即可，该指标层就是可插拔的（见 §4.7 日历颜色指标分层），改一层配置即可。

---

## 0. 一句话定位

一款 **Windows 桌面端、纯本地、离线可用、Apple 美学、可视化极强** 的个人记账与财务分析工具：
Python 负责数据与业务内核，前端库负责界面与图表，支持 **zip 便携版** 与 **.exe 安装程序** 双形态交付，
并为 **插件**、**AI 分析**、**未来机器学习算法** 预留标准接口。

### 已核实的环境事实

| 项 | 值 |
|---|---|
| 操作系统 | Windows 11 家庭中文版 |
| Python | 3.12.3（`C:\Python312\python.exe`） |
| Node.js / npm | v24.19.0 / 可用 |
| 网络 | PyPI 200 OK、npm registry PONG（构建期需要） |
| WebView2 运行时 | 154.0.4258.37 **已安装**（pywebview 可直接使用） |
| Inno Setup | **未安装**，需一次性下载（便携免管理员安装） |
| 磁盘 | D 盘剩余 56.4 GB |
| 已装相关库 | fastapi 0.135.3、uvicorn 0.24.0、SQLAlchemy 2.0.49、alembic 1.18.4、pydantic 2.10.4、pandas 2.2.3、pillow 11.0.0、httpx 0.28.1 |
| 待安装 | pywebview、pyinstaller、reportlab、apscheduler、charset-normalizer、xlsxwriter 等 |

### 已确认的取舍

1. 界面栈：**pywebview(WebView2) + React/TS + ECharts**
2. 交付节奏：**分阶段**，先可运行骨架 + 记账核心闭环，再逐期补齐
3. 安装包：**Inno Setup 6**，便携免管理员方式安装工具链
4. 语言：**中文为主 + 内置 i18n 框架预留英文**

---

## 1. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 桌面外壳 | pywebview 5.x（Edge WebView2 后端） | 复用系统 WebView2，体积小、启动快；以 `ShellAdapter` 抽象预留 Qt 兜底 |
| 界面 | React 18 + TypeScript + Vite | Apple 风格动效与组件化能力最强 |
| 样式 | Tailwind CSS + CSS 变量令牌层 + Framer Motion | 令牌驱动主题，日/夜/跟随系统一键切换 |
| 图表 | ECharts 5（自定义 Apple 主题，含 **K 线 / 日历热力 / 桑基 / 旭日**）+ 自研 SVG 微图表 | 图表类型齐全，可深度定制美术 |
| 图标 | 自绘 SF Symbols 风格 SVG sprite（无 CDN） | 优雅统一、可换色、离线 |
| 后端 | FastAPI + Uvicorn（127.0.0.1 随机端口 + 随机 token） | 同源托管前端 + REST + SSE 流式（AI/导入进度）+ 未来可被外部 API 复用 |
| 数据库 | SQLite + SQLAlchemy 2.0 + Alembic | 纯本地、零配置、可迁移、可可视化 |
| 金额 | 整数最小单位（分） | 杜绝浮点误差 |
| 校验/配置 | Pydantic v2 / pydantic-settings | 已在环境中 |
| **调度** | **APScheduler 3.x（AsyncIOScheduler + SQLAlchemyJobStore）** | 成熟的持久化定时任务；与之配套的自研 `pending_prompts` 待办队列负责漏跑补办与强制拦截 |
| 导入解析 | pandas + openpyxl + xlsxwriter + charset-normalizer | 部分已在环境中 |
| 报告 PDF | ReportLab + 内嵌 OFL 中文字体（Noto Sans SC 子集） | 无浏览器依赖、跨环境稳定 |
| 插件 | 轻量 Hook 注册表（pluggy 风格，自研以减少依赖） | 生命周期/权限/日志完全可控 |
| 打包 | PyInstaller（onedir）+ Inno Setup 6 + PowerShell 脚本 | 便携 zip 与安装程序双交付 |
| 质量 | pytest · ruff · mypy（核心层）· Vitest · Playwright（可选） | |

### 数据流

```
React 界面  →  同源 REST / SSE  →  FastAPI 路由  →  Service 领域层  →  Repository  →  SQLite
     ↑                                                                                    │
     │                       ┌──────────── 调度器（APScheduler）────────────┐              │
     └── 事件总线（events）←─┤ 发薪日 / 账单日 / 周期记账 / 月结 / 备份     ├─→ pending_prompts（强制待办）
                             └──────────────────────────────────────────────┘              │
                                                                                          ▼
                                              插件 / 规则引擎 / 未来 ML 算法 ←── 特征视图 ──┘
```

pywebview 只负责开窗并加载 `http://127.0.0.1:<port>/`，业务逻辑零依赖界面层，
因此未来可无缝替换为纯浏览器访问、或对接外部 API。

---

## 2. 目录结构

```
accountbook/
├─ .gitignore                  # 严格隔离数据 / 密钥 / 构建产物
├─ README.md   LICENSE   pyproject.toml
├─ docs/
│   PLAN.md  ARCHITECTURE.md  DATA_MODEL.md  FEATURES.md
│   DESIGN_SYSTEM.md  PLUGIN_SDK.md  ROADMAP.md  REPORT_SCHEMA.md
│   CARD_ART_GUIDE.md  KLINE_METRICS.md  PAYROLL_AND_INSURANCE.md
├─ src/accountbook/
│   ├─ __main__.py  app.py  config.py  paths.py  logging_setup.py
│   ├─ shell/          # pywebview 外壳、ShellAdapter、DWM 窗口美化、托盘
│   ├─ api/            # FastAPI 路由、依赖注入、SSE 端点
│   ├─ core/           # 领域规则（金额、分摊、周期、预算、工作日、K线聚合）
│   ├─ db/             # ORM 模型、仓储、会话、迁移（alembic/versions）
│   ├─ services/
│   │   ├─ assets/     # 资产卡片、卡面、净值快照、OHLC 聚合
│   │   ├─ piggy/      # 存钱罐与自动归集
│   │   ├─ payroll/    # 发薪规则、工资收录、薪资组成
│   │   ├─ insurance/  # 五险一金档案、缴纳、账户、提取、年结
│   │   ├─ schedule/   # 调度器、工作日日历、待办队列、通知
│   │   ├─ calendar/   # 每日汇总、日历指标、事件日志
│   │   ├─ imports/  exports/  reports/  backup/
│   ├─ intelligence/   # IntelligenceProvider + 规则 / OpenAI / ML 插槽
│   ├─ plugins_sdk/    # 插件 SDK + 钩子 + 权限 + 内置示例插件
│   └─ resources/
│       ├─ fonts/  icons/  themes/
│       ├─ cards/      # 内置卡面资源（品牌授权说明见 CARD_ART_GUIDE.md）
│       ├─ calendars/  # 内置中国法定节假日与调休数据（2024–2026，可更新）
│       └─ web_dist/   # 前端构建产物
├─ web/                # React 前端（design / components / features / charts / i18n）
├─ packaging/          # build_all.ps1 · accountbook.spec · installer.iss · version_info.txt
├─ scripts/  tests/  data/（git 忽略）
```

---

## 3. 数据模型

### 3.1 表清单

| 分组 | 表 |
|---|---|
| 账户与卡片 | `accounts`、`institutions`（银行/机构字典）、`card_artworks`（卡面资源） |
| 分类 | `categories`、`tags`、`transaction_tags` |
| 流水 | `transactions`、`transaction_splits`、`attachments` |
| 计划 | `budgets`、`recurring_rules`、`debts`、`goals` |
| **存钱罐** | `piggy_banks`、`piggy_bank_deposits`、`piggy_bank_rules` |
| **资产时序** | `asset_snapshots`（周期末余额快照）、`asset_ohlc`（K 线聚合缓存）、`net_worth_history` |
| **薪酬** | `payday_rules`、`pay_sources`、`pay_components`、`payroll_records`、`payslips`（附件） |
| **五险一金** | `insurance_profiles`、`insurance_items`（险种字典）、`insurance_contributions`、`insurance_accounts`、`insurance_withdrawals`、`insurance_annual_statements` |
| **调度与提醒** | `scheduled_tasks`、`task_runs`、`workday_calendar`、`pending_prompts`、`notifications` |
| **日历** | `daily_stats`（每日汇总缓存）、`day_events`（当日事件日志） |
| 组织 | `projects`、`members` |
| 币种 | `currencies`、`exchange_rates` |
| 数据治理 | `imports`、`import_rows`、`audit_log`、`settings`、`events` |
| 智能 | `ai_analyses`、`report_snapshots` |
| 扩展 | `plugins`、`alembic_version` |

### 3.2 账户与资产卡片（需求 15）

- `accounts` 扩展：`card_style`（卡面主题）、`brand_key`（招行/建行/工行/微信/支付宝/自定义）、
  `card_network`（银联/Visa/Mastercard/无）、`card_art_ref`（卡面资源或渐变/纹理定义）、
  `card_no_tail`（尾号，默认脱敏）、`credit_limit_minor`、`bill_day`、`due_day`、
  `include_in_net_worth`、`display_order`、`theme_tint`。
- `institutions`：机构字典（名称、类型、品牌主色、Logo 引用），支持用户新增。
- `card_artworks`：内置卡面 + 用户上传卡面（`data/attachments/cards/`），记录尺寸、作者、授权来源。
- **卡片墙**视图按 `display_order` 排列，支持拖拽排序、分组（银行卡 / 电子钱包 / 投资 / 负债）、
  折叠与"聚焦单卡"动画；卡面显示余额、尾号、可用额度、账期进度、本月变动迷你趋势。
- 资产总览：净值、总资产、总负债、可用资金、信用可用额度、资产构成环形图、按机构/类型的堆叠条。

> 卡面美术使用**自绘抽象卡面**（渐变 + 几何 + 品牌主色 + 文字排版），
> 不复制受版权保护的银行/第三方商标图样；用户可自行上传图片作为私人卡面。详见 `docs/CARD_ART_GUIDE.md`。

### 3.3 存钱罐（需求 16）

- `piggy_banks`：`name`、`target_name`（要买的东西）、`target_image`、`target_amount_minor`、
  `currency`、`deadline`、`kind`（一次性目标 / 长期攒钱 / 共享罐）、
  `status`（进行中 / 已达成 / 暂停 / 放弃）、`member_id`、`priority`、
  `skin`（罐体外观主题）、`hide_amount`（隐私模式，只显示百分比）、
  `auto_rule_id`、`achieved_at`、`celebrated`。
- `piggy_bank_deposits`：`amount_minor`（正=存入，负=取出）、`occurred_at`、
  `source_account_id`、`transaction_id`（可回链流水）、`kind`（手动 / 自动归集 / 四舍五入 / 零钱 / 里程碑）、`note`。
- `piggy_bank_rules`：自动归集策略 —— 每笔消费四舍五入入罐 / 每日或每周定额 / 收入百分比 /
  月度结余归集 / 指定分类触发 / 转账到罐即从账户扣减（可选）。
- **可视化**：罐体液面填充动画（SVG + Framer Motion）、硬币投入微动效、进度环、
  里程碑（25/50/75/100%）庆祝动效、**预计达成日推算**（按当前存入速度线性 + 加权两种口径）、
  与目标期限的差距提示；达成后可一键生成"购买支出"流水并结清罐子。

### 3.4 资产 K 线与长期趋势（需求 17）

- `asset_snapshots`：`snapshot_date`、`account_id`、`balance_minor`、`net_worth_minor`、
  `source`（自动日结 / 手动校准）。这是**一切时序图与 K 线的唯一数据源**，避免每次全表重算。
- `asset_ohlc`：`period`（日/周/月/年）、`period_start`、`open`、`high`、`low`、`close`、
  `volume_in_minor`、`volume_out_minor`、`tx_count`、`volatility`、`ma5/ma10/ma20/ma60`、`macd/dif/dea`、`rsi`。
- **把收支类比股市模型**（口径在 `docs/KLINE_METRICS.md` 明确写死，避免自欺）：
  - 开盘 = 期初净值；收盘 = 期末净值；最高/最低 = 期内净值极值（按流水时点重算的日频序列取极值）
  - 成交量 = 期内收入总额 + 支出总额；量比 = 当期流量 / 近 N 期均量
  - 影线长度直观反映期内波动剧烈程度；连续阴线 = 持续净流出
  - 均线 MA5/10/20/60 交叉 = 短期与长期收支节奏的拐点
  - MACD/RSI 可选叠加，**并在图内明确标注"这是你的现金流指标，不是投资建议"**
- **可视化**：蜡烛图（红涨绿跌可切换为国际配色）、叠加均线、成交量子图、
  区间框选对比（任选两段时期并排对比）、净值 vs 累计结余双轴、
  回撤曲线（净值从峰值的回落百分比）、里程碑标记（发薪日/大额支出/存钱罐达成打点）。
- 性能：K 线聚合走 `asset_ohlc` 缓存 + 增量刷新；超长区间降采样。

### 3.5 五险一金与薪酬（需求 18、19）

- `insurance_profiles`：参保档案（城市、社保基数、公积金基数、单位名称、生效区间、参保人 = 本人或家庭成员）。
- `insurance_items` 字典：养老保险、医疗保险、失业保险、工伤保险、生育保险、
  住房公积金、补充公积金、企业年金、大病医疗 —— 含**个人比例**、**单位比例**、
  **是否计入个人账户**、封顶/保底规则、城市政策备注。
- `insurance_contributions`：逐月缴纳记录（`period`、险种、基数、个人额、单位额、计入个人账户额、来源、关联流水）。
- `insurance_accounts`：个人账户余额（养老个人账户 / 医疗个人账户 / 公积金账户 / 年金账户）。
- `insurance_withdrawals`：提取记录（购房、租房、退休、医疗、离职销户），可关联流水。
- `insurance_annual_statements`：年度对账（各险种累计个人/单位缴纳、账户余额、与对账单差异）。
- **统计与积累**：累计个人缴纳 / 单位缴纳 / 合计、单位匹配额（"隐形收入"，可计入总收益视图）、
  公积金余额与首付进度、养老缴费年限进度（对标 15 年门槛并明确写"政策会变，仅供自省"）、
  五险一金构成环形与逐年累积堆叠面积、单位 vs 个人对比柱、年结差异表。
- **与工资收录表联动**：工资表中的五险一金代扣项一键写入 `insurance_contributions` 与账户余额，双向可追溯。

### 3.6 定时任务与发薪日（需求 19）

- `scheduled_tasks`：`code`、`name`、`kind`（发薪日 / 账单日 / 还款日 / 周期记账 / 预算月结 / 报表生成 / 自动备份 / 自定义）、
  `rule`（JSON：频率、月份日期、工作日调整策略）、`next_run_at`、`last_run_at`、`enabled`、
  `catch_up_policy`（错过→启动时补办 / 立即补 / 仅记录）、`priority`。
- `task_runs`：每次执行留痕（状态、耗时、结果摘要、错误），可视化"任务历史与健康度"。
- `workday_calendar`：`date`、`is_workday`、`kind`（正常工作日 / 周末 / 法定节假日 / 调休上班）、
  `name`（春节、国庆…）、`source`（内置 / 用户自定义）。内置 2024–2026 中国节假日与调休数据，
  **可被用户逐日覆盖**，并按年提供更新入口（离线也可手动编辑）。
- **发薪规则 `payday_rules`**：
  - 发薪日 `day_of_month` = **X，可在设置中自定义**（也支持"每月最后一天""每月第 N 个工作日"）
  - 周末调整策略：**遇周末提前到当周最后一个工作日**（默认）/ 顺延 / 不调整；
    并且遇法定节假日时按同一策略提前至节前最后一个工作日（可关闭）
  - **多来源**：一个规则可挂多个薪资来源（公司 A / 兼职 / 理财分红 / 租金…），各自独立金额与账户
  - `require_form`（强制填写工资收录表）、`remind_at`（提前提醒时刻）、`grace_days`（宽限天数）
- **薪资来源 `pay_sources`**：名称、类型、默认入账账户、默认分类、单位名、启用状态、排序。
- **薪资组成 `pay_components`**：按来源配置模板 —— 基本工资 / 绩效 / 加班费 / 餐补 / 交通补 /
  年终奖 / 提成 / 报销 / 税前扣除项 / 个税 / 五险一金代扣；支持固定值、按基数比例、按公式（JSON）计算。
- **工资收录表 `payroll_records`**：期间、发薪日、应发、实发、逐项组成明细、五险一金代扣快照、
  个税、入账流水引用、状态（草稿 / 已填写 / 已跳过）、填写时间、工资条附件（截图或 PDF）。
- **强制弹出与漏填拦截**（需求的硬性行为，需谨慎设计以免变成骚扰）：
  1. 发薪日当天：调度器触发 → 写入 `pending_prompts`（`blocking_level = 强制`）→ 前端立即弹出工资收录表。
  2. 当日未填写：状态保持 `pending`；**下次启动软件时进入"必须处理"队列**，逐个呈现，处理完才进入主界面。
  3. 提供合规出口，避免死锁式打扰：**稍后提醒**（次数上限，默认 3 次，每次 30 分钟）、
     **本月跳过**（必须选择原因并留痕，可事后补录）。这两条均可由用户在设置中收紧为"禁止跳过"。
  4. 若软件未运行且发薪日已过 → 启动时 `catch_up_policy` 自动补建待办，并显示"补办"标记。
- `notifications`：通知中心（级别、标题、正文、动作按钮、已读状态）。

### 3.7 日历可视化（需求 20）

- `daily_stats`（每日汇总缓存）：`date`、`income_minor`、`expense_minor`、`net_minor`、
  `net_worth_minor`、`tx_count`、`entry_state`（**未登记 / 部分登记 / 已登记**）、
  `top_category_id`、`anomaly_score`、`event_count`、`has_attachment`。
  由流水变更增量重算，是日历与所有日粒度图表的数据源。
- `day_events`（当日事件日志）：`date`、`kind`（事件 / 心情 / 纪念日 / 备注 / 待办）、
  `title`、`body`、`tags`、`attachments`。
- **GitHub 贡献图式日历**：53 周 × 7 天矩阵，支持年切换与连续多年滚动；
  悬浮显示当日收支与状态；点击下钻；可导出 PNG。
- **颜色指标分层（可切换主指标，可叠加角标）** —— 这就是"颜色变化指标"的可插拔设计：
  1. **是否登记收支**（默认主指标）：灰 = 未登记；浅 = 部分登记；实心 = 已完整登记
  2. 支出强度（GitHub 式 5 级色阶）
  3. 收入强度
  4. 净流入 / 净流出（红绿发散色阶）
  5. 资产变化幅度（相对当日净值的百分比）
  6. 异常分数（与历史同期基线偏离度）
  - 角标叠加：有无事件日志、有无附件、是否有发薪/账单/还款、是否达预算上限
  - 支持自定义色板与"色盲友好"替代色板；日/夜主题各自校准
- **点击某日 → 当日详情面板**（需求的完整闭环）：
  - 当日资产变化曲线：按流水时点重算的余额阶梯曲线（含日内多笔时间轴）
  - 当日收支构成热力/构成环 + 与近 30 日同星期均值的对比
  - 当日流水清单（可直接编辑）
  - 事件与日志时间轴（可新增事件、写备注）
  - 当日净值相对昨日的变动归因（哪几笔造成的变化）
  - 未登记时提供"补登记"快捷入口

### 3.8 其余关键设计（沿用 v1.0）

- 金额一律 `amount_minor INTEGER`；多币种另存 `currency` + `fx_rate` + 本位币折算。
- `transactions.type ∈ {expense, income, transfer, adjust}`；转账用 `account_id` + `to_account_id`。
- 分类：`kind` + `parent_id` 自引用 + 物化路径 `path`；内置精细分类**全部可改可删可增、可拖拽排序、可自定义图标与配色**。
- `meta JSON` 作为自定义字段落点；软删除 + `audit_log` 全量留痕，回收站可恢复。
- 流水状态机 `pending → cleared → reconciled`（`void` 作废），支撑台账与对账。
- 索引：`(occurred_at)`、`(account_id, occurred_at)`、`(category_id, occurred_at)`、`(external_id)` 唯一、`(snapshot_date, account_id)`。

**数据目录**：安装版 `%LOCALAPPDATA%\AccountBook\`；便携版 exe 同级 `./data/`（由 `portable.flag` 判定）。
无遥测、无启动联网、无云同步。

---

## 4. 功能信息架构

侧边栏分组，内容多样、可折叠、可搜索、可自定义排序与显隐。

### 4.1 记账流
概览仪表盘 · 流水（列表 / 日历 / 时间轴三视图）· 快捷记账（全局快捷键 + 托盘浮窗 + 模板 + 自然语言/剪贴板解析）·
详细填表 · 账户 · 分类 · 标签/项目/成员 · 周期记账 · 债务与应收应付 · 储蓄目标 · 预算

### 4.2 资产与卡片（需求 15）
**卡片墙**（银行卡 / 微信 / 支付宝 / 钱包 / 投资 / 负债分组）· 卡面自定义 · 资产总览 ·
账户详情（余额走势、账期、对账）· 机构字典管理

### 4.3 存钱罐（需求 16）
罐子列表（进行中 / 已达成 / 已暂停）· 罐子详情（液面动画、存入记录、预计达成日）·
自动归集规则 · 里程碑与庆祝 · 共享罐成员分摊

### 4.4 趋势与行情（需求 17）
**资产 K 线** · 净值趋势（日/周/月/年）· 均线与 MACD/RSI 叠加 · 回撤曲线 ·
区间对比 · 收支流量柱 · 指标口径说明页

### 4.5 薪酬与五险一金（需求 18、19）
发薪规则（多来源、X 日、周末/节假日调整）· 薪资组成模板 · **工资收录表** ·
工资历史与同比 · 五险一金档案 · 逐月缴纳明细 · 个人账户余额与积累 ·
提取记录 · 年度对账 · 五险一金统计可视化

### 4.6 台账与报告
**台账 Ledger**（复式台账、余额连续性校验、对账、可打印）· **报表**（日/周/月/年，含同比环比、结构分解、异常洞察）·
报告渲染器（导出 PDF / HTML / Markdown / PNG，一键送 AI 分析）

### 4.7 日历与时间线（需求 20）
**GitHub 式年历热力图**（6 类颜色主指标 + 角标叠加）· 点击当日详情（资产变化曲线、收支构成、流水清单、事件日志、变动归因）·
月视图 · 周视图 · 事件日志管理 · 导出日历图

### 4.8 系统与扩展
定时任务与提醒中心 · 通知中心 · 数据（导入/导出/备份/恢复/去重/清理）·
**数据库可视化**（表结构、ER 图、只读 SQL 查询台、数据体检修复）· 智能 Intelligence ·
插件 · 自定义（主题编辑器 / 自定义字段 / 快捷键 / 小组件 / 报表模板）· 设置 · 关于

### 4.9 可视化总清单（美术优先）

**沿用**：净资产堆叠面积 · 现金流瀑布 · 月度收支对比 · 分类构成堆叠 · 旭日图 · 桑基图 ·
雷达（财务画像）· 矩形树图 · 箱线（异常）· 气泡（大额分布）· 预算仪表盘 · KPI 卡 + 自研 SVG Sparkline/进度环

**新增**：
- **卡片墙**：卡面材质（渐变 / 噪点 / 光泽扫过动效）、堆叠轮播、聚焦放大
- **存钱罐**：罐体液面填充、硬币投入、进度环、里程碑礼花、预计达成日刻度条
- **资产 K 线**：蜡烛图 + 均线 + 成交量子图 + MACD/RSI + 回撤曲线 + 事件打点
- **日历热力图**：GitHub 式矩阵、多层色阶切换、当日余额阶梯曲线、当日构成热力
- **五险一金**：构成环形、逐年累积堆叠面积、单位 vs 个人对比、账户余额进度条（公积金首付 / 养老年限）
- **薪酬**：应发实发瀑布（税前 → 五险一金 → 个税 → 实发）、薪资组成堆叠柱、历年同比

### 4.10 动效体系
页面转场、数字滚动计数、列表 FLIP、图表入场与联动高亮、骨架屏 shimmer、卡片 3D 倾斜与光泽扫过、
罐体液面波动、蜡烛图逐根生长、日历格子波次点亮、按钮与开关的 iOS 手感；
全部遵循 `prefers-reduced-motion`（可一键关闭）。

---

## 5. 设计系统（Apple 美学）

`docs/DESIGN_SYSTEM.md` + `web/src/design/tokens.ts` 双层令牌，界面 100% 引用令牌，**不允许硬编码色值**。

- **色彩**：系统蓝主色 + 语义色板（红/橙/黄/绿/青/紫/粉/靛），日/夜各一套校准值；
  图表另备 **色盲友好** 替代板；**涨跌配色可切换**（中式红涨绿跌 / 国际绿涨红跌）
- **字体**：`-apple-system, "SF Pro Text", "Segoe UI Variable", "PingFang SC", "Microsoft YaHei"`，字重 400/500/600
- **尺度**：8pt 网格、圆角 10/14/20/28、三档阴影、半透明材质（WebView2 支持 `backdrop-filter`）
- **主题**：浅色 / 深色 / 跟随系统，带过渡动画；原生标题栏经 DWM API 同步（圆角、深色标题、可选 Mica）
- **无障碍**：焦点环、键盘全可达、对比度 ≥ 4.5:1、减少动效、字号缩放
- **曲线**：`cubic-bezier(0.32, 0.72, 0, 1)`，时长 150 / 250 / 400 ms

---

## 6. 报告引擎与 AI 接入

- **统一 `ReportDocument` JSON Schema**（封面 / KPI / 图表 / 表格 / 洞察 / 附录），
  前端 `<ReportRenderer>` 单点渲染，同一 Schema 复用于界面、导出与外部 API。
- 报告类型：日报、周报、月报、年报 + 自定义区间；内容含收支概览、结构分解、同比环比、
  账户与卡片变动、**存钱罐进度**、**K 线趋势与回撤**、**薪酬与五险一金积累**、
  预算执行、日历登记完整度、异常与亮点、AI 洞察。
- 导出：PDF（ReportLab 内嵌中文）/ HTML（自包含）/ Markdown / PNG。
- **AI 接入**：`POST /api/reports/{id}/ai-analyze`（SSE 流式）→ 组装结构化摘要 →
  OpenAI 兼容接口（`base_url` / `model` / `key` 可配，兼容 DeepSeek）→ 存档 `ai_analyses` 并嵌入报告。
  默认 **脱敏商户名与金额** 可开关；无 key 或断网时自动回落离线规则分析并明确提示。

---

## 7. 智能与未来算法预留

`IntelligenceProvider.analyze(dataset, options) -> Insight[]`（含标题/级别/证据/建议/置信度）。

- 现有：`RuleBasedAnalyzer`（离线：预算超支、异常支出、重复订阅、收支结构突变、大额提醒、
  **发薪漏填提醒、五险一金基数异常、存钱罐进度落后、日历连续未登记**）
- 联网：`OpenAICompatibleProvider`
- **ML 插槽**：`LocalMLProvider` + 特征视图服务 + `data/models/`（gitignore）+ ROADMAP 接入步骤。
  预留算法：消费预测、自动分类、异常检测、聚类画像、**存钱罐达成日预测**、**现金流 K 线形态识别**。
- 事件总线（`events` 表 + 内存分发）为插件、调度器与算法提供统一输入。

---

## 8. 插件与自定义

目录 `plugins/<id>/plugin.json + main.py`；钩子覆盖生命周期、流水增删改、导入解析器、报告区块、
图表类型、命令、侧边栏条目、设置项、AI 分析器，**并新增**：`register_card_renderers`（自定义卡面渲染）、
`register_piggy_skins`（罐体皮肤）、`register_schedule_kinds`（自定义定时任务类型）、
`register_calendar_metrics`（自定义日历颜色指标）。

权限声明（读/写流水、网络、文件、剪贴板、调度）**默认拒绝**，首次启用需确认；
异常捕获 + 超时 + 独立日志，插件失败绝不影响主程序。
自定义含主题令牌编辑器、自定义字段、快捷键、报表模板、仪表盘拖拽布局、**涨跌配色与日历指标偏好**。

---

## 9. 文件导入与导出

**导入**：CSV（列映射向导 + 支付宝 / 微信 / 云闪付 / 银行卡预设）、Excel、JSON、OFX/QFX、QIF、
剪贴板与拖拽、自然语言单行记账，**新增：工资条（图片 PDF 可手动录入，OCR 列为后续 ROADMAP）、
社保/公积金年度对账单、银行资产快照**。
编码探测、流式分块、分批事务、进度可中断、外部 ID 去重 + 相似度去重、预览确认、错误行导出。

**导出**：CSV / Excel / JSON / Markdown / PDF / HTML / SQLite 全量备份 / **加密备份**（`.abk` 文件关联），
**新增：日历图 PNG、K 线图 PNG/PDF、卡片墙全景图、工资与五险一金年度汇总表**。
备份轮转、一键恢复、数据体检修复。

---

## 10. 打包与交付物

`packaging/build_all.ps1` 一键：环境自检 → 依赖 → 前端构建 → PyInstaller → 便携 zip → Inno Setup → SHA256 校验和。

| 交付物 | 说明 |
|---|---|
| `AccountBook-Portable-<ver>-win-x64.zip` | 解压即用；数据写入同级 `data/` |
| `AccountBook-Setup-<ver>.exe` | 安装目录选择、开始菜单、桌面快捷方式、`.abk` 关联、**开机自启（可选，托盘常驻以支撑定时任务）**、标准卸载器、静默参数 |
| `SHA256SUMS.txt` | 完整性校验 |

细节：DPI per-monitor V2、程序图标与版本信息、WebView2 缺失检测与引导（不静默安装）、
首次运行引导与示例数据、托盘常驻与"最小化到托盘"、启动速度优化。

---

## 11. 安全、隐私与 .gitignore 隔离

- 本地 API：仅绑定 `127.0.0.1`、每次启动随机端口 + 随机 token、Host/Origin 校验，阻断本机其他程序与网页访问。
- 敏感信息：**卡号默认脱敏**（仅显示尾号）、**工资与五险一金金额可设"隐私模式"**（界面与截图默认遮罩）、
  可选应用锁（PIN/密码哈希加盐）、日志默认不含金额与商户原文。
- 无云同步、无遥测、无启动联网；AI 与更新检查为显式动作，默认关闭。
- `.gitignore` 明确隔离：`data/`、`*.db` / `*.sqlite*`、`*.abk`、备份、附件、
  **卡面与工资条等用户上传资源**、`logs/`、`models/`、本地插件实例、导入导出样本、
  `dist/`、`build/`、`web/dist/`、`node_modules/`、`__pycache__/`、`.venv/`、`.env`、
  `secrets.json`、`*.key` / `*.pem`、用户自定义主题与截图。

---

## 12. 代码注释与工程规范（需求 11）

- 文件头：模块用途 / 边界 / 依赖 / 所属阶段。
- 类与函数：docstring 含参数、返回、异常、副作用、线程约束。
- 复杂算法（预算结转、周期展开、去重、报告口径、**工作日与节假日推算、K 线聚合与指标计算、
  存钱罐达成日预测、社保基数与比例计算**）逐段行内注释说明「为什么」。
- **需求追溯标记** `# REQ-4.2`、阶段标记 `# [P9] TODO`，可全局检索。
- 统一格式化：ruff format + ruff check + eslint / prettier；核心领域层 mypy 严格。
- 结构化日志 + 崩溃转储 + 首次运行自检报告。

---

## 13. 阶段计划与验收标准

> **进度更新**：**P0 已交付**，验收证据见 [ACCEPTANCE_P0.md](ACCEPTANCE_P0.md)。

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0 基座骨架** ✅ | 仓库结构、依赖锁定、.gitignore、设计令牌 v1、pywebview 外壳 + FastAPI 同源服务 + token、前端脚手架（路由 / i18n / 主题 / 布局）、配置与数据目录、托盘、日志、DWM 美化 | **已达成**：`python run.py` 开窗；日夜主题可切并持久化；健康检查通过；72 项 pytest 全绿；PyInstaller 打包 + 冒烟测试通过 |
| **P1 记账核心闭环** | ORM + Alembic 初始迁移、金额 / 分摊 / 软删 / 审计、CRUD API、流水三视图、快捷记账、详细填表、账户管理、分类树 + 图标选择器 | 能真实记账 / 编辑 / 删除 / 筛选 / 搜索；余额正确；可导出备份 |
| **P2 资产卡片墙与可视化体系** | `asset_snapshots` + `daily_stats` 缓存、**卡片墙与卡面美术**、资产总览、**GitHub 式日历 + 当日详情面板**、ECharts Apple 主题 + 12 类图表、KPI 卡 + 微图表、可拖拽仪表盘、动效体系 | 卡片墙可排序可自定义卡面；日历六类颜色指标可切换；点击某日能看到当日余额曲线与构成；数据正确、主题联动 |
| **P3 资产 K 线与长期趋势** | `asset_ohlc` 聚合与增量刷新、蜡烛图 + MA/MACD/RSI + 成交量、回撤曲线、区间对比、事件打点、指标口径说明页 | K 线口径与流水可逐笔核对；切换周期数据一致；长区间降采样流畅 |
| **P4 存钱罐与目标储蓄** | `piggy_banks` / `deposits` / `rules`、罐体动画与里程碑、自动归集（四舍五入 / 定额 / 百分比 / 结余）、预计达成日推算、达成转支出 | 自动归集金额可从流水复原；达成可一键结清并生成流水；动画流畅 |
| **P5 报告引擎与台账** | ReportDocument Schema + 渲染器、日/周/月/年报告（含卡片、罐子、K 线、薪酬、登记完整度）、PDF/HTML/MD/PNG 导出、复式台账与对账 | 四类报告可生成可导出，导出件与界面一致 |
| **P6 薪酬、五险一金与定时任务** | 调度器 + `workday_calendar`（节假日与调休）+ `payday_rules`（X 日 / 周末与节假日提前 / 多来源）+ 薪资组成 + **工资收录表与强制待办拦截** + 五险一金档案 / 缴纳 / 账户 / 提取 / 年结 + 统计可视化 | 发薪日按规则正确命中（含周末提前到当周最后一个工作日）；当日弹表、未填则下次启动强制补录；工资代扣一键入社保账户；年结可对账 |
| **P7 AI 与智能预留** | IntelligenceProvider、离线规则分析（含新增 4 类规则）、OpenAI 兼容 SSE、报告内嵌洞察、脱敏开关、ML 插槽与特征视图 | 断网可用规则分析；有 key 可用 AI；无 key 不报错 |
| **P8 导入导出与数据治理** | 导入向导 + 预设解析器（含工资条 / 社保对账单）+ 去重 + 预览、全套导出与新增导出物、加密备份恢复、DB 可视化体检 | 支付宝 / 微信真实样例正确入库；社保对账单可导入；加密备份可完整恢复 |
| **P9 插件与自定义** | 插件宿主 + 钩子（含卡面 / 罐体 / 调度类型 / 日历指标扩展点）+ 权限 + 2 个示例插件、前端扩展点、主题编辑器、自定义字段 / 快捷键 / 模板 | 示例插件即插即用且不破坏主程序；自定义主题与日历指标可保存切换 |
| **P10 打包打磨与文档** | PyInstaller + 便携 zip + Inno Setup + 校验和、WebView2 引导、设置 / 关于 / 帮助、无障碍与色盲配色、性能优化、全量测试与文档 | 干净 Win11 上 zip 直接跑、安装包正常装卸、数据目录正确、文档齐全 |

### 13.1 需求追溯矩阵

| 需求 | 实现模块 | 阶段 |
|---|---|---|
| 1 打包 zip 便携版 + 安装程序 | packaging/ | P10（P0 冒烟） |
| 2 定位：个人记账本 | 全程 | 全程 |
| 3 功能强大、可视化美观、种类齐全 | 全模块 | P1–P5 |
| 4 分类精细、可自定义、图标优雅 | 分类模块 + 图标集 | P1 |
| 5 动效 + 可视化 + DB 可视化 + 台账 + 日/周/月/年报告 + 导出 + AI | 统计 / 台账 / 报告 / DB Explorer / 智能 | P2–P8 |
| 6 快捷录入 + 详细填表 + 离线 + 功能栏 + ML 预留 | 记账流 / config / 侧边栏 / intelligence | P1、P7、P10 |
| 7 日夜间切换与全局兼容 | 设计令牌 + DWM + 图表主题 | P0 起持续 |
| 8 插件与自定义 | plugins_sdk + 自定义模块 | P9 |
| 9 文件导入与导出解析 | services/imports、exports | P8 |
| 10 设置、关于、预留 | 设置 / 关于 / ROADMAP | P10 |
| 11 详细注释与标记 | 规范 + CI 检查 | 全程 |
| 12 Apple 美术风格 | DESIGN_SYSTEM.md | P0 起持续 |
| 13 可视化美术格外重要 | 图表主题 + 自研微图表 + 动效 | P2–P5 |
| 14 本地不上云 + gitignore 隔离 | paths / .gitignore | P0 执行、全程守护 |
| **15 账户加银行卡 / 微信 / 支付宝等卡片与资产可视化** | accounts 扩展 + card_artworks + 卡片墙 | **P2** |
| **16 为买 XX 攒钱的存钱罐** | piggy 模块 | **P4** |
| **17 趋势图 + 资产 K 线（收支类比股市模型）** | asset_ohlc + 行情视图 | **P3** |
| **18 五险一金使用统计与积累** | insurance 模块 | **P6** |
| **19 定时任务 + 发薪日（X 日 / 周末提前 / 多来源 / 强制收录 / 漏填补录）** | schedule + payroll 模块 | **P6** |
| **20 日历可视化（登记情况作颜色指标 + 点击看当日资产曲线 / 热力 / 事件日志 + GitHub 式日历）** | calendar 模块 | **P2** |

---

## 14. 风险与对策

| 风险 | 对策 |
|---|---|
| PyInstaller 与 pythonnet / WebView2 打包坑 | 在 **P0 就做打包冒烟测试**，不拖到 P10 |
| 无边框窗口体验风险（拖拽 / 缩放 / 贴靠） | 默认保留原生边框 + DWM 圆角 / 深色标题；自定义 chrome 仅作可选开关 |
| **银行/第三方卡面商标版权** | 内置卡面一律**自绘抽象风格**（渐变 + 几何 + 品牌主色），不复制商标图样；用户可上传私人卡面并附授权提示 |
| **K 线被误读为投资建议** | 口径文档 `KLINE_METRICS.md` 写死定义；图内常驻口径说明与免责提示；指标可整体关闭 |
| **中国节假日与调休数据时效** | 内置 2024–2026 并标注数据版本；用户可逐日覆盖；提供按年更新入口与"未知日期按周末规则"兜底 |
| **强制弹窗变成骚扰** | 提供"稍后提醒（限次）"与"跳过（需填原因留痕）"出口，默认开启；可由用户收紧为禁止跳过；漏填状态全程可见不静默 |
| **五险一金各地政策差异大** | 比例与基数全部走可配置字典 `insurance_items`（按城市档案），内置值标注"参考口径"，不承诺政策准确性；年结支持与官方对账单比对 |
| 中文 PDF 字体授权与体积 | 只用 OFL 字体（Noto Sans SC 子集）并随包附许可证 |
| 金额精度 | 全整数最小单位 + 边界 Decimal 转换 + 单元测试 |
| SQLite 并发（调度器 + 界面同时写） | WAL + 单写者 + 会话集中管理 + 调度任务串行化 + 写锁重试 |
| 大文件导入与长区间 K 线内存 | 流式分块 + 分批事务 + 聚合缓存 + 降采样 |
| Inno Setup 便携安装失败 | 备选：系统自带 iexpress 生成自解压安装器 + 说明文档 |
| 插件安全 | 权限声明（新增调度权限）+ 默认拒绝 + 沙箱 + 白名单 API + 独立日志 |
| 需求体量大 | 分阶段交付，每阶段可独立运行与验证 |

---

## 15. 获批后立即执行的动作

1. 写入 `.gitignore`（含全部数据隔离规则）、`README.md`、`pyproject.toml` 与目录骨架。
2. 补齐 `docs/` 文档（ARCHITECTURE / DATA_MODEL / DESIGN_SYSTEM / FEATURES / ROADMAP / REPORT_SCHEMA /
   CARD_ART_GUIDE / KLINE_METRICS / PAYROLL_AND_INSURANCE）。
3. 完成 **P0**：pywebview + FastAPI + token 安全 + 前端脚手架 + 日志与数据目录 + 托盘 + DWM 美化。
4. P0 结束时交付一次 PyInstaller 冒烟打包结果，确认打包链路无阻。
5. 随后 P1 → P10 推进，每阶段结束交付可运行版本与验收说明。
