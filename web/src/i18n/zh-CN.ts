/**
 * 简体中文语言包（主语言）。
 *
 * 组织约定
 * --------
 * * 以「域」为一层分组（`nav` / `topbar` / `dashboard` / `scope` / `common`），
 *   键名使用小驼峰，禁止出现界面文案以外的语义。
 * * **本文件是语言包的「结构真源」**：`en-US.ts` 必须实现完全相同的键集合，
 *   TypeScript 会用 `Messages` 类型强制这一点 —— 漏译会在构建期报错，
 *   而不是在界面上显示成空白或英文走中文。
 * * 文案中的占位符统一用 `{name}` 形式，由 i18n 层做简单插值。
 *
 * 需求追溯：REQ-10（设置与本地化）、取舍「中文为主 + 预留英文」
 */

export const zhCN = {
  app: {
    name: '记账本',
    tagline: '本地、离线、属于你自己的财务账本',
  },

  /** 侧边栏分组标题 */
  group: {
    bookkeeping: '记账流',
    insight: '分析与呈现',
    data: '数据与系统',
  },

  /** 侧边栏与页面标题 */
  nav: {
    dashboard: '概览仪表盘',
    transactions: '流水',
    quickAdd: '快捷记账',
    accounts: '账户',
    cards: '资产卡片墙',
    categories: '分类',
    tagsProjects: '标签与项目',
    recurring: '周期记账',
    budgets: '预算',
    piggy: '存钱罐',
    debts: '债务与应收应付',
    goals: '储蓄目标',
    statistics: '统计可视化',
    kline: '趋势与资产 K 线',
    ledger: '台账',
    reports: '报表',
    calendar: '日历',
    payroll: '薪酬与五险一金',
    scheduler: '定时任务',
    dataManager: '数据管理',
    dbExplorer: '数据库可视化',
    intelligence: '智能分析',
    plugins: '插件',
    customization: '自定义',
    settings: '设置',
    about: '关于',
  },

  /** 每个模块「将来会做什么」的一句话说明（用于占位页，也是需求 10 的诚实交代） */
  scope: {
    dashboard: '净资产、本月收支、预算进度、待办与提醒的一屏总览。',
    transactions: '列表 / 日历 / 时间轴三视图，支持虚拟滚动、批量编辑与高级筛选。',
    quickAdd: '全局快捷键唤起、托盘浮窗、模板与自然语言解析的极速录入。',
    accounts: '账户余额、账期、对账与余额走势，支持全部账户类型。',
    cards: '把银行卡、微信、支付宝等账户做成可自定义的卡片墙，直观呈现资产分布。',
    categories: '树形分类管理、图标与配色自定义、批量迁移与合并同类。',
    tagsProjects: '以标签、项目、成员三个维度组织流水，支持家庭分摊。',
    recurring: '按规则自动生成流水（周 / 月 / 季 / 年 / 自定义），到期提醒。',
    budgets: '总额 / 分类 / 账户三维预算，支持结转与超额提醒。',
    piggy: '为想要的东西攒钱：罐子进度、自动归集、里程碑与达成日推算。',
    debts: '借贷台账、还款计划与利息计算，覆盖应收应付。',
    goals: '储蓄目标进度与自动归集。',
    statistics: '时间 × 分类 × 账户 × 标签 × 成员的多维透视与十余种图表。',
    kline: '把收支类比股市模型：资产蜡烛图、均线、成交量与回撤曲线。',
    ledger: '复式台账、余额连续性校验、对账状态与可打印版式。',
    reports: '日 / 周 / 月 / 年报告，内嵌渲染、多格式导出并可送 AI 分析。',
    calendar: 'GitHub 风格的年度热力图；颜色可反映是否登记、支出强度等，点击查看当日明细。',
    payroll: '发薪规则、工资收录表，以及五险一金的逐月缴纳、账户余额与年度对账。',
    scheduler: '发薪日、账单日、月结、备份等定时任务的规则配置与执行历史。',
    dataManager: '导入向导、全套导出、加密备份与恢复、去重与数据体检。',
    dbExplorer: '表结构、ER 图、只读 SQL 查询台与数据体检修复。',
    intelligence: '离线规则洞察、可接入的 AI 分析，以及未来的机器学习算法插槽。',
    plugins: '插件的安装、启用、权限确认与日志查看。',
    customization: '主题令牌编辑器、自定义字段、快捷键、仪表盘小组件与报表模板。',
    settings: '外观、本地化、数据路径、快捷键、AI 提供商、安全锁与通知。',
    about: '版本与构建信息、开源许可、致谢与诊断工具。',
  },

  topbar: {
    searchPlaceholder: '搜索流水、账户、分类…（P1 起可用）',
    filterPlaceholder: '筛选模块',
    quickAdd: '记一笔',
    themeLight: '浅色',
    themeDark: '深色',
    themeSystem: '跟随系统',
    cycleTheme: '切换主题（当前：{theme}）',
    reduceMotion: '减少动态效果',
    privacyMode: '隐私模式（金额打码）',
    collapseSidebar: '折叠侧边栏',
    expandSidebar: '展开侧边栏',
    language: '语言',
  },

  dashboard: {
    greetingMorning: '早上好',
    greetingAfternoon: '下午好',
    greetingEvening: '晚上好',
    subtitle: '这是你的财务总览。数据全部保存在本机，不上传任何服务器。',
    netWorth: '净资产',
    monthIncome: '本月收入',
    monthExpense: '本月支出',
    monthBalance: '本月结余',
    awaitingData: '待录入',
    placeholderNote: '示意图形 · 数据接入将于 {phase} 完成',
    trendTitle: '资产趋势（示意）',
    emptyTitle: '还没有任何账目',
    emptyBody: 'P1 阶段完成后，你可以在这里看到账户余额与近期流水。',
    emptyAction: '了解路线图',
    systemTitle: '运行状态',
    systemConnected: '本地服务已连接',
    systemDisconnected: '无法连接本地服务',
    fieldVersion: '版本',
    fieldPhase: '当前阶段',
    fieldPython: 'Python',
    fieldPlatform: '运行平台',
    fieldUptime: '已运行',
    fieldPort: '服务端口',
    fieldDataDir: '数据目录',
    fieldLogFile: '日志文件',
    fieldMode: '运行模式',
    fieldShell: '界面外壳',
    shellNative: '原生窗口（WebView2）',
    shellBrowser: '系统浏览器（已降级）',
    browserFallbackWarning:
      '原生窗口在当前环境不可用，已自动改用系统浏览器打开。功能完全一致（同一套界面与本地服务）。若希望使用原生窗口，请安装或修复 .NET Framework 4.7.2+ / .NET 桌面运行时，然后重启应用（详见 docs/ARCHITECTURE.md 的「已知限制」）。',
    modePortable: '便携版（数据随程序目录）',
    modeInstalled: '安装版（数据在用户目录）',
    openDataDir: '打开数据目录',
    revealFailed: '打开数据目录失败：{message}',
    adminWarning: '当前以管理员身份运行，建议改用普通权限以避免文件权限混乱。',
    degradedWarning: '便携目录不可写，已自动降级到用户数据目录。',
  },

  phase: {
    badge: '计划于 {phase} 实现',
    current: '当前进度：{phase}',
    title: '{name}',
    roadmap: '路线图位置',
    scopeLabel: '该模块将提供',
    backToDashboard: '返回概览',
    note: 'P0 只交付基座骨架：导航、主题、i18n 与本地服务链路。本页内容将在 {phase} 阶段实现，届时会以可运行、可验证的形式交付。',
  },

  settings: {
    appearance: '外观',
    appearanceNote: '主题会同时作用于界面、图表与 Windows 原生标题栏。',
    theme: '主题',
    reduceMotion: '减少动态效果',
    reduceMotionNote: '关闭页面转场与装饰性动画。系统已开启「减少动画」时会自动生效。',
    privacy: '隐私模式',
    privacyNote: '金额显示为遮罩（保留位数形状），适合演示或公共场合。',
    languageSection: '语言',
    languageNote: '语言包在构建期做键集合校验，切换后立即生效。',
    dataSection: '数据与隐私',
    dataNote: '账本、附件与备份全部保存在本机，不上传任何服务器，也没有遥测。',
    dataDir: '数据目录',
    openFolder: '打开数据目录',
    upcoming: '后续设置项',
    upcomingNote: '以下分组已在路线图中预留，届时会出现在本页。',
    upcomingHotkeys: '快捷键与快捷录入',
    upcomingScheduler: '定时任务与通知',
    upcomingAi: 'AI 提供商与脱敏策略',
    upcomingSecurity: '应用锁与敏感字段',
    upcomingPlugins: '插件权限管理',
  },

  about: {
    version: '版本',
    phase: '实现阶段',
    buildType: '构建类型',
    buildSource: '源码运行',
    buildFrozen: '已打包',
    python: 'Python',
    platform: '运行平台',
    port: '本地服务端口',
    pid: '进程号',
    license: '许可',
    licenseNote: '本项目为个人使用工具。第三方组件（如字体、图标资源）的许可见 docs 目录。',
    privacyTitle: '隐私与数据',
    privacyNote:
      '所有数据都存放在本机数据目录，应用不含任何遥测或云同步；AI 分析为可选的显式操作，默认关闭。',
    docsTitle: '项目文档',
    docsNote: '架构、数据模型、设计系统、插件 SDK 与路线图都在 docs 目录中。',
    diagnosticsTitle: '诊断',
    diagnosticsNote: '遇到问题时，可在仓库根目录执行以下命令获取环境自检报告：',
  },

  common: {
    connected: '已连接',
    disconnected: '未连接',
    retry: '重试',
    loading: '加载中…',
    saving: '正在保存…',
    saveFailed: '设置未能保存：{message}',
    unknown: '未知',
    notFoundTitle: '页面不存在',
    notFoundBody: '链接可能已失效，或该模块已改名。可以从左侧导航继续操作。',
    goHome: '回到概览',
    yes: '是',
    no: '否',
  },
}

/**
 * 语言包的结构类型。
 *
 * 注意这里**刻意不使用 `as const`**：
 * 用了它，所有值会被推断成字符串字面量类型，
 * 于是 `en-US.ts` 的英文文案会因为"不等于 '概览仪表盘' 这个字面量"而报错。
 * 我们要约束的是**键集合与嵌套结构**，而不是值的具体内容。
 *
 * `en-US.ts` 用 `satisfies Messages` 声明，任何键缺失或拼错都会在构建期暴露。
 */
export type Messages = typeof zhCN
