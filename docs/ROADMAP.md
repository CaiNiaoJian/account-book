# 路线图与扩展预留 · AccountBook Desktop

> 本文件回答两个问题：**接下来做什么**，以及**未来接入新东西时要动哪里**。
> 阶段验收标准见 [PLAN.md](PLAN.md#13-阶段计划与验收标准)，功能归属见 [FEATURES.md](FEATURES.md)。

---

## 1. 阶段路线（P0 → P10）

| 阶段 | 主题 | 关键交付 | 状态 |
|---|---|---|---|
| P0 | 基座骨架 | 外壳 / 本地服务 / 设计系统 / 主题 / i18n / 导航 / 打包冒烟 | ✅ 完成 |
| **P1** | 记账核心闭环 | 数据模型、流水、账户、分类、快捷记账、预算与周期、债务与应收应付 | ✅ 完成 |
| **P2** | 资产卡片墙与可视化 | 卡片墙、GitHub 式日历、图表体系、可拖拽仪表盘、动效 | ✅ 完成 |
| **P3** | 资产 K 线与趋势 | OHLC 聚合、蜡烛图 + 均线 + 成交量 + 回撤 + 事件打点 | ✅ 完成 |
| P4 | 存钱罐与目标 | 罐子、自动归集、达成日推算 | ✅ |
| P5 | 报告与台账 | ReportDocument、日/周/月/年报告、四种导出、复式台账 | ✅ |
| P6 | 薪酬、五险一金与定时任务 | 发薪规则、工资收录表、强制补录、社保公积金 | ✅ |
| P7 | AI 与智能预留 | 规则洞察、OpenAI 兼容、ML 插槽 | ⏳ |
| P8 | 导入导出与数据治理 | 导入向导、加密备份、DB 可视化 | ⏳ |
| P9 | 插件与自定义 | 插件宿主、扩展点、主题编辑器 | ⏳ |
| P10 | 打包与打磨 | 便携 zip、安装程序、性能与文档 | ⏳ |

**排序依据**：先把"能真实记账"这条主干打通（P1），再让它好看（P2/P3），
再让它有用（P4–P6），最后让它可被扩展与分发（P7–P10）。
每个阶段结束时都必须是**可运行、可验证**的版本，不允许交付"半截功能"。

---

## 2. 预留插槽：未来接入时要改哪里

### 2.1 机器学习算法（需求 6）

```
src/accountbook/intelligence/
├─ base.py              IntelligenceProvider 协议 + Insight 数据结构（P7 落地）
├─ rule_based.py        离线规则分析器（P7）
├─ openai_compatible.py 联网 AI（P7）
└─ local_ml.py          🔮 ML 插槽（接口先定，实现后补）
```

接入步骤（P7 会写成可执行的 `docs/` 指南）：

1. 实现 `IntelligenceProvider.analyze(dataset, options) -> list[Insight]`；
2. 通过 `FeatureViewService` 取特征 —— 该服务是**唯一**允许直接读数据库的算法入口，
   保证特征口径集中且可版本化；
3. 模型文件写入 `<data>/models/`（已 gitignore），随备份一起搬运；
4. 在设置页注册为可选项，并声明"是否联网"。

预留的算法板块：消费预测、自动分类、异常检测、聚类画像、存钱罐达成日预测、K 线形态识别。

### 2.2 其它算法 / 外部服务

| 想接入的东西 | 落点 | 需要遵守 |
|---|---|---|
| 汇率 / 行情数据源 | `services/rates/` 适配器 | 必须用户显式触发；默认关闭联网 |
| OCR 票据识别 | `services/imports/ocr.py` | 本地优先；联网识别需单独开关 |
| 多设备本地同步（非云） | `services/sync/` | 仅局域网/文件交换，不得引入云端账户 |
| 语音记账 | 前端 + `services/imports/nlp.py` | 音频不上传 |

### 2.3 插件扩展点（P9）

已规划的钩子：`on_app_start/stop`、`on_transaction_created/updated/deleted`、
`register_import_parsers`、`register_report_blocks`、`register_chart_types`、
`register_commands`、`register_sidebar_items`、`register_settings_schema`、
`provide_ai_analyzers`、`register_card_renderers`、`register_piggy_skins`、
`register_schedule_kinds`、`register_calendar_metrics`。

权限模型：读流水 / 写流水 / 网络 / 文件系统 / 剪贴板 / 调度 —— **默认全部拒绝**，
首次启用时逐项向用户申请。

### 2.4 界面扩展点

| 扩展 | 落点 | 说明 |
|---|---|---|
| 新主题 | `web/src/styles/tokens.css` 覆盖变量 | P9 提供可视化编辑器 |
| 新语言 | `web/src/i18n/<code>.ts` + `LANGUAGES` | 键集合由 `satisfies Messages` 构建期校验 |
| 新图标 | `web/src/components/Icon.tsx` 的 `ICON_PATHS` | 24 网格 / 1.6 描边 / `fill="none"` |
| 新图表 | `web/src/components/` + `design/tokens.ts` 色序 | 图表颜色必须走令牌，禁止硬编码 |
| 新导航模块 | `web/src/app/navigation.ts` + i18n + `routes.tsx` | 三步，导航/路由/徽标自动同步 |
| 新设置项 | `config.UserPreferences` + `PreferencesPatch` | 未知字段向前兼容，旧版不会擦掉新版设置 |

---

## 3. 技术债与已知限制

| 项 | 影响 | 计划 |
|---|---|---|
| 原生窗口使用系统边框（未自定义 chrome） | 视觉上少一点"统一工具栏"的整体感 | P10 评估；DWM 属性已封装，切换成本低 |
| 托盘尚未接管窗口关闭事件 | 关闭窗口即退出，调度任务无法常驻 | **仍未实现**。P6 改用「错过策略」补偿：软件没运行时错过的任务，在下次启动时按 `startup` / `immediate` / `record_only` 补办，并把补办留痕标出来。托盘留待后续 |
| 仅 Windows 有原生外壳 | 其它平台降级为浏览器外壳 | 视需求扩展；`ShellAdapter` 已就绪 |
| `docs/DATA_MODEL.md` 等文档尚未落地 | 字段字典缺失 | P1 随数据模型一起交付 |
| mypy 仅在计划中对 `core/` 严格 | 类型覆盖不完整 | P1 起逐步收紧 |
| 前端尚无单元测试 | 组件回归依赖人工目视 | P2 起引入 Vitest |

---

## 4. 明确的非目标

* ❌ **云同步与账户体系** —— 与"本地不上云"的定位直接冲突；
* ❌ **遥测与使用统计** —— 同上；
* ❌ **静默安装系统组件**（如 WebView2）—— 未经用户同意的系统级更改不做；
* ❌ **自动上传账目到任何服务** —— AI 分析必须是显式动作，且提供脱敏开关。

这些不是"还没做"，而是**决定不做**。若将来要改，必须先修改本节并重新评估隐私承诺。

---

## 5. 质量门槛（每个阶段都要满足）

1. `pytest` 全绿，新增领域逻辑必须有对应用例；
2. `ruff check` 与 `ruff format --check` 通过；
3. `npm run build` 通过（含 `tsc --noEmit` 类型检查，**类型错误视同构建失败**）；
4. 深浅两种主题各留一张验收截图于 `docs/screenshots/`；
5. 新增用户可见文案必须同时补齐中英文；
6. 新增用户数据目录必须同步更新 `.gitignore`；
7. `packaging/build_backend.ps1` 的冒烟测试通过（确认打包产物真能启动并服务）。
