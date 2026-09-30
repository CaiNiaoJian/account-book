# 设计系统 · AccountBook Desktop

> 目标：让界面在任何时候看起来都像"一个被认真设计过的 Apple 应用"，
> 而不是"一个会在深色模式下崩坏的网页"。
>
> 本文是**约束**，不是建议。所有界面改动都必须在此规则内完成。

---

## 1. 唯一真源：设计令牌

```
web/src/styles/tokens.css     ← 颜色变量（唯一定义处）
web/tailwind.config.js        ← 把变量注册为工具类（唯二定义处）
web/src/design/tokens.ts      ← 供 canvas / 图表读取的 JS 访问层
```

**三条铁律**

1. 组件里**禁止**出现硬编码色值（`#fff`、`rgb(...)`、`text-white`）。
   一律使用语义类：`bg-surface`、`text-label-2`、`border-separator`。
2. 新增颜色必须先写 `--ab-*` 变量，再在 `tailwind.config.js` 注册。
   ⚠️ 只写变量却忘了注册，会在构建期报
   `The 'bg-xxx' class does not exist`（这是本项目实际踩过的坑）。
3. 变量值写成 **空格分隔的 RGB 三分量**（`245 245 247`），不带 `rgb()` 包装 ——
   只有这样 Tailwind 的 `<alpha-value>` 机制才能生成 `bg-surface/60` 这类半透明类。

### 为什么坚持令牌化

| 收益 | 说明 |
|---|---|
| 日夜切换零成本 | 只换一套变量，组件代码一行不改 |
| 自定义主题可落地 | P9 的主题编辑器只需覆盖变量，不必改组件 |
| 图表与界面永不脱节 | ECharts 通过 `resolveToken()` 读同一批变量 |
| 可审查 | "这个灰色是不是设计系统里的灰"变成可机械检查的问题 |

---

## 2. 色彩

### 2.1 层次结构

| 令牌 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--ab-bg` | `245 245 247` | `28 28 30` | 窗口底色 |
| `--ab-surface` | `255 255 255` | `44 44 46` | 卡片 |
| `--ab-surface-2` | `250 250 252` | `58 58 60` | 卡片内嵌区块 |
| `--ab-surface-3` | `240 240 243` | `72 72 74` | 输入框 / 分段控件底槽 |
| `--ab-bg-elevated` | `255 255 255` | `44 44 46` | 模态与抽屉 |
| `--ab-sidebar-bg` | `246 246 248` | `36 36 38` | 侧边栏基色（配合模糊） |
| `--ab-separator` | `209 209 214` | `66 66 69` | 分隔线 |
| `--ab-hairline` | `0 0 0` | `255 255 255` | 发丝线；**始终以低透明度使用** |

### 2.2 文本三级

| 令牌 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--ab-text` | `29 29 31` | `245 245 247` | 标题与主要数值 |
| `--ab-text-2` | `110 110 115` | `152 152 157` | 正文说明、次要标签 |
| `--ab-text-3` | `142 142 147` | `124 124 128` | 占位符、图标、辅助信息 |

### 2.3 语义色

系统色语义（浅色 / 深色）：`accent` 系统蓝 `0 122 255 / 10 132 255`、
`positive` 绿 `52 199 89 / 48 209 88`（**收入**）、`negative` 红 `255 59 48 / 255 69 58`（**支出**）、
`warning` 橙、`info` 青、`purple`、`pink`、`indigo`、`teal`、`mint`、`orange`、`yellow`。

**深色不是浅色的反相**：底色更暗、层间更亮、对比度略降。
大面积纯黑会让长时间记账非常疲劳，这是刻意的取舍。

### 2.4 涨跌配色是一等公民

`money_color_scheme` 提供 `cn`（红涨绿跌，A 股习惯，**默认**）与 `intl`（绿涨红跌）。
这不是装饰性偏好 —— A 股用户看到绿色上涨会本能地以为出了问题。
K 线、热力图、瀑布图全部读取 `design/tokens.ts::moneyColors()`。

---

## 3. 排版

* 字体栈：`-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable Text", "PingFang SC", "Microsoft YaHei UI", …`
* 字号梯度（`text-ab-*`）：`caption2 10` · `caption 11` · `footnote 12` · `subhead 13` · `callout 14` ·
  `body 15` · `headline 15/600` · `title3 17/600` · `title2 20/600` · `title1 26/600` · `large 34/700`
* 大标题带负字距（`-0.02em` 起），这是 Apple 排版最容易被忽略、也最影响"像不像"的细节；
* **所有金额与数字都必须加 `.ab-tnum`**（`font-variant-numeric: tabular-nums`），
  否则列表里每一行的数字宽度不同，滚动时会产生强烈的"跳动感"。

---

## 4. 形状与材质

| 令牌 | 值 | 用途 |
|---|---|---|
| `rounded-ab-xs` | 6px | 徽标、键位提示 |
| `rounded-ab-sm` | 10px | 按钮、输入框、导航项 |
| `rounded-ab-md` | 14px | 图标底板、内嵌区块 |
| `rounded-ab-lg` | 20px | 卡片 |
| `rounded-ab-xl` | 28px | 弹层、模态 |

阴影三档（`shadow-ab-1/2/3`）全部是**极低透明度**的柔和投影，追求悬浮感而非重投影。
`shadow-ab-inset` 提供 1px 内描边，用于在浅色背景上勾勒卡片边缘。

**毛玻璃材质**：`.ab-material` = `backdrop-filter: saturate(180%) blur(24px)` + 72% 透明底色。
用于侧边栏与工具栏。深色模式下会透出一点壁纸层次，是 macOS 侧边栏的关键观感来源。

---

## 5. 动效

| 令牌 | 值 | 用途 |
|---|---|---|
| `duration-ab1` | 150ms | 悬停、按下、微反馈 |
| `duration-ab2` | 250ms | 常规状态变化 |
| `duration-ab3` | 400ms | 页面级转场 |

缓动统一：`cubic-bezier(0.32, 0.72, 0, 1)`（先快后慢、收尾柔和）；
强调场景用 `(0.2, 0, 0, 1)`；弹性反馈用 `(0.34, 1.56, 0.64, 1)`。

**克制原则**：动效只用于解释"什么从哪里来、到哪里去"。
记账工具一天要开几十次，装饰性弹跳很快会变成负担。具体表现为：

* 页面转场只做 8px 上浮 + 淡入，不做滑动；
* 悬停抬升只有 1–2px；
* 按下缩放不超过 0.975。

### 无障碍：减少动效

三个层次同时生效，任一层触发即全局关闭非必要动效：

1. 系统级 `@media (prefers-reduced-motion: reduce)`；
2. 应用内开关 → `<html class="ab-reduce-motion">`；
3. 组件级读取 `usePreferences().preferences.reduce_motion`（图表入场动画据此跳过）。

---

## 6. 组件规范

核心语义类集中定义在 `web/src/styles/app.css` 的 `@layer components`：

| 类 | 用途 | 关键约束 |
|---|---|---|
| `.ab-card` | 卡片 | 20px 圆角 + 50% 分隔线描边 + 一级阴影 |
| `.ab-card-interactive` | 可点卡片 | 悬停抬升 1px、阴影升一档 |
| `.ab-nav-item` | 侧边栏条目 | 选中为 `bg-accent/15` 药丸；未选中悬停为 `bg-hairline/5` |
| `.ab-btn-primary` | 主按钮 | 系统蓝；`disabled` 时 45% 透明且不响应悬停 |
| `.ab-btn-secondary` | 次按钮 | `surface-3/80` 填充 |
| `.ab-btn-ghost` | 图标按钮 | 悬停淡填充、按下缩到 0.94 |
| `.ab-section-label` | 分组标题 | 小号大写 + 字距加宽 |
| `.ab-metric` | KPI 数字 | 26px/600 + 表格数字 |
| `.ab-phase-badge` | 阶段徽标 | 10px 大写，用于诚实标注"计划于 Pn" |

### 焦点与键盘

* 默认 `:focus { outline: none }`，但 `:focus-visible` 会绘制 2px 系统蓝焦点环 ——
  鼠标点击不出现轮廓、键盘导航一定出现，这是现代桌面应用的正确做法；
* 所有可点元素必须有可访问名称（`aria-label` 或可见文字）；
* `Icon` 组件在提供 `title` 时渲染 `<title>` 并设置 `role="img"`，否则 `aria-hidden`。

---

## 7. 图表主题（P2 起全面启用）

* 色序：`CHART_SERIES_TOKENS = accent → positive → orange → purple → negative → teal → yellow → indigo`；
* 网格线用 `--ab-chart-grid`，坐标轴文字用 `--ab-text-3`，轴标签字号 11；
* 图表背景透明，继承卡片底色；圆角与描边遵循同一套令牌；
* 面积渐变从 `0.28` 透明度收到 `0`，避免色块过重；
* 另备**色盲友好**替代色板（P2 落地），在设置中可切换。

---

## 8. 贡献检查清单

提交界面改动前，请逐条自查：

- [ ] 没有新增硬编码色值；新颜色已在 `tokens.css` + `tailwind.config.js` 成对注册
- [ ] 金额与数字使用了 `.ab-tnum`
- [ ] 深浅两种主题下都目视检查过（`docs/screenshots` 保留验收截图）
- [ ] 键盘可达：Tab 能到达，`:focus-visible` 可见
- [ ] 提供了无障碍名称；纯图标按钮必须有 `aria-label`
- [ ] 动效在「减少动态效果」开启时被正确跳过
- [ ] 中文与英文两种语言下都没有溢出或截断
- [ ] 新增文案同时补了 `zh-CN.ts` 与 `en-US.ts`（漏译会导致构建失败）
