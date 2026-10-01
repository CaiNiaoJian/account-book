# 数据模型（P1）

本文件描述账本的数据结构、**为什么这样设计**、以及几条必须遵守的铁律。
实现见 `src/accountbook/db/models.py`，迁移见 `src/accountbook/db/migrations/versions/`。

---

## 1. 五条铁律

| # | 规则 | 违反后会怎样 |
|---|---|---|
| 1 | 金额一律是**整数最小单位**（`*_minor`），浮点不参与任何运算 | 余额出现 `0.30000000000000004` 这类值，且永远无法彻底修好 |
| 2 | 业务时间 `occurred_at` 用**本地墙钟**，审计时间用 **UTC** | 晚上 8 点的消费在日报里跑到第二天；跨时区比较审计时间时全部错位 |
| 3 | 只有 `services/` 与 `db/` 能碰数据库；`api/` 不得直接写查询 | 业务规则散落到路由里，无法单独测试，口径逐渐分叉 |
| 4 | 删除一律是**软删除**（`deleted_at`），审计留痕 | 误删无法恢复；"数字对不上"时没有证据可查 |
| 5 | 转账与余额校准**不计入收支统计** | "工资卡转支付宝"被算成支出，月支出凭空翻倍 |

---

## 2. 表清单

| 表 | 作用 | 软删除 |
|---|---|---|
| `currencies` | 币种字典（含**最小单位位数**：日元 0 位、人民币 2 位） | — |
| `accounts` | 账户（现金 / 储蓄卡 / 信用卡 / 电子钱包 / 投资 / 应收应付 / 储值 / 虚拟） | ✓ |
| `categories` | 分类树（自引用 + 物化路径 `path`） | ✓ |
| `tags` | 标签（跨分类的横向维度） | ✓ |
| `projects` | 项目（把一段时期的相关支出归到一处） | ✓ |
| `members` | 成员（分摊与家庭账本） | ✓ |
| `transactions` | 流水 | ✓ |
| `transaction_splits` | 分账（一笔拆到多个分类） | — |
| `transaction_tags` | 流水-标签多对多 | — |
| `app_settings` | 应用级键值（`seed_version` 等） | — |
| `audit_log` | 变更审计（**只记字段级差异，不存正文**） | — |
| `institutions` | 机构字典（P2）。用**稳定键**而非名称关联账户，改名不会让卡面失效 | ✓ |
| `card_artworks` | 卡面配方（P2）。渐变/纹理/光泽的 JSON，**不是位图** | — |
| `daily_stats` | 每日汇总缓存（P2）。日历与日粒度图表的唯一数据源 | — |
| `asset_snapshots` | 每日每账户收盘余额（P2）。一切时序图与净值曲线的唯一数据源 | — |
| `day_events` | 当日事件日志（P2）。事件/心情/纪念日/备注/待办共用，差别只在 `kind` | ✓ |
| `recurring_rules` | 周期记账规则（P1）。只存"怎么重复"，**不预先铺流水** | ✓ |
| `budgets` | 预算（P1）。总预算与分类预算各自独立；分区唯一索引保证同一范围只有一条启用中 | ✓ |
| `debts` | 债务 / 债权（P1）。可选生成应收 / 应付镜像账户，让借出借入进入净值 | ✓ |
| `debt_payments` | 还款记录（P1）。**本金与利息分开存**，否则算不出"还剩多少本金" | — |
| `asset_ohlc` | 净值 K 线聚合（P3）。可由 `asset_snapshots` 完全推出，与日结共用脏标记 | — |
| `transaction_templates` | 记账模板（P1）。存整套字段供一键填充；`tag_ids` 用 JSON 数组 | ✓ |
| `attachments` | 附件（P1 尾巴 T5 / T6）。只存**相对引用**，文件落在 `<data>/attachments/` | — |
| `piggy_banks` | 存钱罐（P4）。余额**不落库**，由 `piggy_bank_deposits` 求和 | ✓ |
| `piggy_bank_rules` | 归集规则（P4）。一个罐子最多一条 —— 多条会让"这笔钱是哪条规则归集的"说不清 | — |
| `piggy_bank_deposits` | 罐子进出（P4）。**一张表 + 带符号金额**，正存入负取出 | — |
| `goals` | 储蓄目标（P4）。进度来自关联账户的实时余额 + 手工注入，两者都不落库 | ✓ |
| `goal_contributions` | 目标的手工注入（P4）。与罐子存入分开：语义与取出规则都不同 | — |

### 2.1 P2 的派生缓存：口径见 `docs/CALENDAR_METRICS.md`

`daily_stats` 与 `asset_snapshots` 是**派生缓存，不是事实来源**。
`transactions` 才是唯一事实来源，缓存随时可由流水重算：

```
变更流水/账户  →  mark_dirty_from(受影响起始日)
读取日历/时序  →  ensure_fresh(区间)  →  脏则重算  →  返回
```

这样正确性**不依赖调用方记得刷新**：忘记标脏只是多算一次，
忘记重算会让用户看到错数据 —— 两者严重程度差得远，所以偏保守。

**净值是累计量**：插入一笔历史流水会改变此后每一天的余额，
因此重算区间一定延伸到"今天"，而不是只算被改动的那一天。

派生量**不重复落库**：例如"当日净值变化率"完全可由相邻两天的
`net_worth_minor` 推出，就在读取时现算。每个派生量都存一份会让缓存表越来越宽，
而每一列都是一次"忘了同步就出错"的机会。

### 2.2 P1 收尾与 P3 的派生约定

* **`asset_ohlc` 是纯聚合缓存**：它没有引入任何新口径，只把
  `daily_stats.net_worth_minor` 按周期折成开高低收 + 资金流动额。
  因此可以随时删掉重算，也与日结共用一套失效逻辑（见 `docs/KLINE_METRICS.md`）。
* **预算的"已用金额"不落库**：它是流水的函数，存一份就要在**每次**记流水时
  同步，一旦漏了用户会看到一个不动的进度条 —— 比没有进度条更糟。
  只有"上期结转额"这个**跨期结果**被缓存到 `carryover_minor`。
* **债务的还款计划也不落库**：它完全由（本金、年化利率、期数、方式、起始日）
  推出，只存 `repayment_method` 与 `installments` 两个参数。
  存整张分摊表就要在每次改债务时同步，而漏同步的后果是用户照着错误的计划还钱。
* **周期记账只存"怎么重复"**：把未来十年的流水提前写进 `transactions` 是错的 ——
  改一次规则就得删掉几千行，而且那些行在被生成之前并不是"发生过的事实"。
  `next_due_date` 是算出来的缓存，可随时由规则 + 上次生成日重建。
* **模板的 `tag_ids` 用 JSON 数组而不是关联表**：标签只在**套用模板时整体读取**
  一次，为它单开一张表会让一次套用变成三次查询。真要做标签统计时走
  `transaction_tags`。

---

## 3. 关键设计决策

### 3.1 余额不落库，每次聚合

`accounts` 表**没有** `balance_minor` 列。余额 = 起点余额 + 全部未删除流水的净影响：

```sql
SUM(CASE WHEN direction = 'in' THEN  amount_minor
                          ELSE -amount_minor END)   -- account_id 侧
+ SUM(amount_minor)                                  -- to_account_id 侧（转入）
```

理由：冗余余额在"导入历史数据 / 手工改单 / 进程崩溃"时极易与流水不一致，
而一旦不一致，用户看到"余额对不上账"却无从修复。
SQLite 在几万条流水规模下聚合是毫秒级，实测完全够用（`services/accounts.py`）。

### 3.2 `direction` 列让余额公式没有分支

支出恒为 `out`、收入恒为 `in`（由 CHECK 约束保证）；转账与校准由场景决定方向。
有了它，余额公式不需要按 `type` 分支 —— **分支越少，口径出错的机会越少**。

### 3.3 物化路径支撑"按大类汇总"

`categories.path` 形如 `/1/7/23`。取"某分类及其所有子分类"退化成
`path LIKE '/1/7/%'`，比递归 CTE 更快且能被索引命中。

移动分类时**必须级联重算整棵子树**的 `path` 与 `depth` ——
只改自身会让后代指向错误的祖先，而这类错误只在汇总报表里才会暴露。

### 3.4 部分唯一索引：删除后可以重建同名

```sql
CREATE UNIQUE INDEX uq_accounts_name_active ON accounts(name) WHERE deleted_at IS NULL;
```

SQLite 的唯一索引把 `NULL` 视为互不相同，因此**根级分类**（`parent_id IS NULL`）
之间的重名拦不住 —— 那部分由服务层校验兜底（`_assert_sibling_name_available`）。

### 3.5 分账金额守恒由服务层保证

"同一流水的分账之和 = 流水金额"是跨行求和，SQLite 无法用声明式约束表达，
因此由 `services/transactions._replace_splits` 校验，失败时返回
`validation_error` 与具体差额（`details.difference_minor`）。

### 3.6 审计只记差异

`audit_log.changes` 形如 `{"amount_minor": {"from": 100, "to": 9900}}`。
`note` / `payee` 这类自由文本只记"是否变化"，不记内容 ——
避免审计表成为用户隐私的副本。

---

## 4. 统计口径

| 概念 | 口径 | 实现位置 |
|---|---|---|
| 收入 / 支出 | 排除转账（`transfer`）与校准（`adjust`），排除 `void` 与已删除 | `services/transactions.summary` |
| 分类占比 | **分账优先于主分类** —— 分账记录的是钱真正的去向 | `_category_breakdown` |
| 资产 / 负债 | 按**余额正负**归类，而不是按账户类型 | `services/accounts.overview` |
| 净值 | Σ(计入净资产的账户余额) | 同上 |
| 环比 | 与"等长上一个区间"比较；上期为 0 时返回 `null`（不编造百分比） | `services/stats.previous_period` |

**资产/负债为什么按正负而不是按类型**：多还款的信用卡会变成正余额（相当于存款），
按类型硬分类会把这类情况算错。

---

## 5. 迁移

* 迁移在**应用启动时自动执行**（`db/bootstrap.py`）—— 桌面用户不会去跑 `alembic` 命令；
* `env.py` 开 `render_as_batch=True`：SQLite 不支持大多数 `ALTER TABLE`，
  batch 模式会"建新表 → 拷数据 → 换名"，这是 SQLite 上唯一可行的结构演进方式；
* 约束命名遵循 `NAMING_CONVENTION`（`pk_` / `fk_` / `uq_` / `ck_` / `ix_` 前缀）——
  不设它的话 SQLite 生成的约束名是随机的，将来无法可靠地 `drop_constraint`；
* **生成迁移后必须手工补齐 docstring**（为什么改、对既有数据的影响、能否回滚）。

### 已知的工程陷阱（都实际踩过）

| 陷阱 | 症状 | 处理 |
|---|---|---|
| `alembic.ini` 含非 ASCII 字符 | `UnicodeDecodeError: 'gbk' codec ...` —— configparser 按**区域编码**读 ini | 该文件保持纯 ASCII，理由写在文件头 |
| 同一索引被声明两次（mixin 的 `index=True` + 显式 `Index`） | 不报错，只是每次写入白慢一点 | 生成迁移后检查索引清单 |
| `@(...).Count` 之外，PowerShell 的 `` `t `` 是**制表符**转义 | 验证脚本里 URL 变成 `?<TAB>oken=`，一片 401 | 脚本中一律用字符串拼接而非反引号转义 |
