"""P2 日结 / 日历 / 卡片墙用例。

覆盖重点是**会悄悄算错的地方**：
* 转账不得改变净值（否则日内曲线会出现虚假尖峰）；
* 净值必须随流水**累计**（否则曲线每天都从头算）；
* 回填历史流水必须让此后每一天的净值都跟着变（这是最容易被漏掉的一条）；
* 日历的强度分级必须由服务端统一给出（否则图例与格子对不上）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from accountbook.core.domain import AccountType, TransactionType
from accountbook.core.errors import ConflictError, ProtectedEntityError, ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.models import AssetSnapshot, DailyStat
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import assets as assets_service
from accountbook.services import categories as categories_service
from accountbook.services import daily as daily_service
from accountbook.services import transactions as transactions_service

TODAY = date.today()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ledger.db")
    run_migrations(database)
    with database.session() as session:
        ensure_seed_data(session)
    yield database
    database.dispose()


@pytest.fixture
def session(db: Database):
    with db.session() as active:
        yield active


def _accounts(session: Session) -> list:
    return accounts_service.list_accounts(session)


def _category(session: Session, name: str) -> int:
    for item in categories_service.list_categories(session, kind="expense"):
        if item.name == name:
            return item.id
    raise AssertionError(f"未找到分类 {name}")


def _at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


def _spend(session: Session, *, day: date, amount: int, category: str = "午餐", hour: int = 12):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.EXPENSE.value,
        account_id=_accounts(session)[0].id,
        category_id=_category(session, category),
        amount_minor=amount,
        occurred_at=_at(day, hour),
    )


class TestNetWorthAccumulation:
    def test_net_worth_accumulates_across_days(self, session: Session) -> None:
        """净值是累计量：后一天必须在前一天的基础上变化。"""
        account = _accounts(session)[0]
        account.initial_balance_minor = 100_000
        session.flush()

        _spend(session, day=TODAY, amount=3_500)
        _spend(session, day=TODAY - timedelta(days=1), amount=2_000)

        cells = daily_service.get_calendar(
            session, start=TODAY - timedelta(days=2), end=TODAY, metric="expense"
        )
        by_day = {cell.date: cell for cell in cells}
        assert by_day[TODAY - timedelta(days=2)].net_worth_minor == 100_000
        assert by_day[TODAY - timedelta(days=1)].net_worth_minor == 98_000
        assert by_day[TODAY].net_worth_minor == 94_500

    def test_transfer_does_not_change_net_worth(self, session: Session) -> None:
        """转账只是换个口袋 —— 净值曲线不能因此出现尖峰。"""
        first, second = _accounts(session)[:2]
        transactions_service.create_transaction(
            session,
            type=TransactionType.TRANSFER.value,
            account_id=first.id,
            to_account_id=second.id,
            amount_minor=50_000,
            occurred_at=_at(TODAY, 15),
        )

        cells = daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="expense")
        assert cells[0].net_worth_minor == 0
        # 但转账角标要出现，否则用户会以为这笔没记上
        assert "transfer" in cells[0].badges

    def test_backdated_transaction_updates_later_days(self, session: Session) -> None:
        """回填历史流水必须让此后每一天的净值都跟着变。

        这是"增量重算"最容易漏的一条：只重算被改动的那一天，
        后面的曲线会全部停在旧值，而表面上"数据看起来是有的"。
        """
        _spend(session, day=TODAY, amount=1_000)
        before = daily_service.get_calendar(
            session, start=TODAY - timedelta(days=2), end=TODAY, metric="expense"
        )
        assert before[-1].net_worth_minor == -1_000

        # 回填到三天前
        _spend(session, day=TODAY - timedelta(days=3), amount=5_000)
        after = daily_service.get_calendar(
            session, start=TODAY - timedelta(days=3), end=TODAY, metric="expense"
        )
        by_day = {cell.date: cell.net_worth_minor for cell in after}
        assert by_day[TODAY - timedelta(days=3)] == -5_000
        assert by_day[TODAY - timedelta(days=2)] == -5_000, "中间没有被改动的日子也必须重算"
        assert by_day[TODAY] == -6_000

    def test_deleting_transaction_rolls_back_net_worth(self, session: Session) -> None:
        transaction = _spend(session, day=TODAY, amount=4_000)
        transactions_service.delete_transaction(session, transaction.id)
        cells = daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="expense")
        assert cells[0].net_worth_minor == 0
        assert cells[0].expense_minor == 0

    def test_account_opening_balance_change_recomputes_history(self, session: Session) -> None:
        """改账户起点余额会影响整条净值曲线 —— 必须整体重算。"""
        account = _accounts(session)[0]
        _spend(session, day=TODAY - timedelta(days=1), amount=1_000)
        accounts_service.update_account(session, account.id, initial_balance_minor=500_000)
        cells = daily_service.get_calendar(
            session, start=TODAY - timedelta(days=2), end=TODAY, metric="expense"
        )
        assert cells[0].net_worth_minor == 500_000, "起点余额变化必须追溯影响历史"


class TestCalendarMetrics:
    def test_every_day_is_present(self, session: Session) -> None:
        cells = daily_service.get_calendar(
            session, start=TODAY - timedelta(days=364), end=TODAY, metric="entry"
        )
        assert len(cells) == 365, "日历必须逐日占位，缺格会让用户以为应用坏了"

    def test_entry_state_three_levels(self, session: Session) -> None:
        empty = daily_service.get_calendar(
            session, start=TODAY - timedelta(days=1), end=TODAY - timedelta(days=1), metric="entry"
        )[0]
        assert empty.entry_state == "none" and empty.level == 0

        _spend(session, day=TODAY, amount=1_000)
        logged = daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="entry")[0]
        assert logged.entry_state == "logged" and logged.level == 1

        daily_service.confirm_day(session, TODAY, confirmed=True)
        confirmed = daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="entry")[0]
        assert confirmed.entry_state == "confirmed" and confirmed.level == 2

    def test_event_alone_makes_day_logged(self, session: Session) -> None:
        """只有事件、没有流水的一天也算"已登记" —— 事件本身也是记录。"""
        daily_service.create_event(session, day=TODAY - timedelta(days=1), title="搬家")
        cell = daily_service.get_calendar(
            session, start=TODAY - timedelta(days=1), end=TODAY - timedelta(days=1), metric="entry"
        )[0]
        assert cell.entry_state == "logged"
        assert "events" in cell.badges

    @pytest.mark.parametrize("metric", list(daily_service.CALENDAR_METRICS))
    def test_all_six_metrics_compute(self, session: Session, metric: str) -> None:
        _spend(session, day=TODAY, amount=8_000)
        cells = daily_service.get_calendar(session, start=TODAY - timedelta(days=3), end=TODAY, metric=metric)  # type: ignore[arg-type]
        assert len(cells) == 4
        assert all(0 <= cell.level <= 4 for cell in cells)

    def test_levels_are_relative_not_absolute(self, session: Session) -> None:
        """分级用分位数：量级完全不同的两套数据都应把色域用满。

        固定阈值的后果是"月支出 300 的人整月最浅色、月支出 30 万的人整月最深色"。
        """
        for index in range(5):
            _spend(session, day=TODAY - timedelta(days=index), amount=(index + 1) * 10)
        levels = {
            cell.level
            for cell in daily_service.get_calendar(
                session, start=TODAY - timedelta(days=4), end=TODAY, metric="expense"
            )
        }
        # 五个递增值应铺满四档（0 档留给"这天没花钱"）
        assert levels == {1, 2, 3, 4}, f"五档分级不均衡：{levels}"

    def test_zero_days_share_the_lowest_level(self, session: Session) -> None:
        """没有支出的日子统一是 0 档，不参与分位数计算。"""
        _spend(session, day=TODAY, amount=5_000)
        cells = {
            cell.date: cell
            for cell in daily_service.get_calendar(
                session, start=TODAY - timedelta(days=1), end=TODAY, metric="expense"
            )
        }
        assert cells[TODAY - timedelta(days=1)].level == 0
        assert cells[TODAY].level >= 1

    def test_net_metric_is_diverging(self, session: Session) -> None:
        """净值指标是发散色阶：收入与支出必须落在不同侧。"""
        _spend(session, day=TODAY - timedelta(days=1), amount=2_000)
        transactions_service.create_transaction(
            session,
            type=TransactionType.INCOME.value,
            account_id=_accounts(session)[0].id,
            amount_minor=9_000,
            occurred_at=_at(TODAY, 10),
        )
        cells = {
            cell.date: cell
            for cell in daily_service.get_calendar(
                session, start=TODAY - timedelta(days=1), end=TODAY, metric="net"
            )
        }
        assert cells[TODAY - timedelta(days=1)].level >= 3, "净流出应落在暖色侧"
        assert cells[TODAY].level <= 2, "净流入应落在冷色侧"

    def test_anomaly_score_uses_median_baseline(self, session: Session) -> None:
        """异常分数基于同星期中位数：一次大额消费不该把所有正常消费判成异常。"""
        weekday = TODAY.weekday()
        # 造 6 周的同星期历史：4 次 1000，1 次 100000（离群值）
        for weeks in range(2, 8):
            day = TODAY - timedelta(weeks=weeks)
            if day.weekday() != weekday:
                continue
            amount = 100_000 if weeks == 4 else 1_000
            _spend(session, day=day, amount=amount)

        # 今天花 1200（略高于中位数 1000）
        _spend(session, day=TODAY, amount=1_200)
        cell = daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="anomaly")[0]
        # 若基线用均值，均值会被 100000 拉到 ~17500，1200 会被判成"正常"（0）；
        # 用中位数则 1200 相对 1000 只偏离 20% → 分数很小但不为 0
        assert cell.anomaly_score < 0.2, f"中位数基线下不该判为异常：{cell.anomaly_score}"

    def test_badges_for_bill_and_due(self, session: Session) -> None:
        account = _accounts(session)[0]
        accounts_service.update_account(session, account.id, bill_day=5, due_day=20)
        month_start = TODAY.replace(day=1)
        cells = {
            cell.date: cell
            for cell in daily_service.get_calendar(
                session, start=month_start, end=month_start + timedelta(days=27), metric="entry"
            )
        }
        assert "bill" in cells[month_start.replace(day=5)].badges
        assert "due" in cells[month_start.replace(day=20)].badges


class TestDayDetail:
    @pytest.mark.parametrize('direction, expected', [('in', 12345), ('out', -12345)])
    def test_adjustment_curve_matches_daily_close(self, session, direction, expected):
        account = _accounts(session)[0]
        transactions_service.create_transaction(
            session, type='adjust', direction=direction, account_id=account.id,
            amount_minor=12345, occurred_at=_at(TODAY, 9),
        )
        detail = daily_service.get_day_detail(session, TODAY)
        assert detail['net_worth_series'][-1]['net_worth_minor'] == expected
        assert detail['stat']['net_worth_minor'] == expected
        assert detail['stat']['income_minor'] == detail['stat']['expense_minor'] == 0

    @pytest.mark.parametrize('source_counted, target_counted, expected', [
        (True, True, 0), (False, False, 0), (True, False, -12345), (False, True, 12345),
    ])
    def test_transfer_curve_respects_both_account_flags(self, session, source_counted, target_counted, expected):
        source, target = _accounts(session)[:2]
        accounts_service.update_account(session, source.id, include_in_net_worth=source_counted)
        accounts_service.update_account(session, target.id, include_in_net_worth=target_counted)
        transactions_service.create_transaction(
            session, type='transfer', account_id=source.id, to_account_id=target.id,
            amount_minor=12345, occurred_at=_at(TODAY, 9),
        )
        detail = daily_service.get_day_detail(session, TODAY)
        assert detail['net_worth_series'][-1]['net_worth_minor'] == expected
        assert detail['stat']['net_worth_minor'] == expected
        assert sum(item['delta_minor'] for item in detail['contributions']) == expected

    def test_excluded_foreign_void_and_deleted_entries_do_not_change_curve(self, session):
        counted, excluded = _accounts(session)[:2]
        accounts_service.update_account(session, excluded.id, include_in_net_worth=False)
        foreign = accounts_service.create_account(session, name='美元', type='cash', currency='USD')
        for account, status, deleted in [
            (excluded, 'cleared', False), (foreign, 'cleared', False),
            (counted, 'void', False), (counted, 'cleared', True),
        ]:
            row = transactions_service.create_transaction(
                session, type='expense', account_id=account.id, amount_minor=12345,
                status=status, occurred_at=_at(TODAY, 9),
            )
            if deleted:
                transactions_service.delete_transaction(session, row.id)
        detail = daily_service.get_day_detail(session, TODAY)
        assert detail['net_worth_series'][-1]['net_worth_minor'] == detail['stat']['net_worth_minor'] == 0
        assert detail['contributions'] == []
        assert sum(item['amount_minor'] for item in detail['composition']) == detail['stat']['expense_minor'] == 12345

    def test_mixed_day_curve_closes_at_the_same_net_worth_after_flag_change(self, session):
        source, target = _accounts(session)[:2]
        accounts_service.update_account(session, source.id, initial_balance_minor=100000)
        accounts_service.update_account(session, target.id, initial_balance_minor=10000)
        transactions_service.create_transaction(
            session, type='income', account_id=source.id, amount_minor=10000,
            occurred_at=_at(TODAY - timedelta(days=1), 8),
        )
        for hour, kind, account, to_account, direction, amount, status in [
            (8, 'income', source, None, 'in', 20000, 'cleared'),
            (9, 'adjust', source, None, 'out', 1000, 'cleared'),
            (10, 'transfer', source, target, 'out', 30000, 'cleared'),
            (11, 'expense', target, None, 'out', 500, 'cleared'),
            (12, 'expense', source, None, 'out', 400, 'void'),
        ]:
            transactions_service.create_transaction(
                session, type=kind, account_id=account.id, to_account_id=to_account.id if to_account else None,
                direction=direction, amount_minor=amount, status=status, occurred_at=_at(TODAY, hour),
            )
        for included, expected in [(True, 138500), (False, 99000), (True, 138500)]:
            accounts_service.update_account(session, target.id, include_in_net_worth=included)
            detail = daily_service.get_day_detail(session, TODAY)
            assert detail['net_worth_series'][-1]['net_worth_minor'] == detail['stat']['net_worth_minor'] == expected
            assert detail['stat']['opening_net_worth_minor'] + sum(row['delta_minor'] for row in detail['contributions']) == expected

    def test_ladder_curve_follows_real_timestamps(self, session: Session) -> None:
        """余额阶梯曲线的拐点必须落在流水真实发生的那一刻。"""
        _spend(session, day=TODAY, amount=1_000, hour=9)
        _spend(session, day=TODAY, amount=2_000, category="打车", hour=18)

        detail = daily_service.get_day_detail(session, TODAY)
        series = detail["net_worth_series"]
        assert series[0]["label"] == "opening"
        assert len(series) == 3, "两笔流水应各产生一个拐点"
        assert series[1]["net_worth_minor"] == -1_000
        assert series[2]["net_worth_minor"] == -3_000
        assert series[1]["at"].endswith("09:00:00")
        assert series[2]["at"].endswith("18:00:00")

    def test_composition_prefers_splits(self, session: Session) -> None:
        """当日构成同样遵守"分账优先"。"""
        account = _accounts(session)[0]
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=10_000,
            occurred_at=_at(TODAY, 20),
            splits=[
                {"category_id": _category(session, "午餐"), "amount_minor": 7_000},
                {"category_id": _category(session, "打车"), "amount_minor": 3_000},
            ],
        )
        detail = daily_service.get_day_detail(session, TODAY)
        by_name = {item["category_name"]: item["amount_minor"] for item in detail["composition"]}
        assert by_name["午餐"] == 7_000
        assert by_name["打车"] == 3_000

    def test_contributions_exclude_transfers(self, session: Session) -> None:
        first, second = _accounts(session)[:2]
        _spend(session, day=TODAY, amount=1_000)
        transactions_service.create_transaction(
            session,
            type=TransactionType.TRANSFER.value,
            account_id=first.id,
            to_account_id=second.id,
            amount_minor=9_000,
            occurred_at=_at(TODAY, 16),
        )
        detail = daily_service.get_day_detail(session, TODAY)
        assert len(detail["contributions"]) == 1, "归因不该把转账算成净值变化"
        assert detail["contributions"][0]["delta_minor"] == -1_000


class TestSnapshots:
    def test_snapshot_written_per_account_per_day(self, session: Session) -> None:
        accounts = _accounts(session)
        daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="entry")
        rows = session.query(AssetSnapshot).filter(AssetSnapshot.snapshot_date == TODAY).all()
        assert len(rows) == len(accounts)

    def test_manual_snapshot_not_overwritten(self, session: Session) -> None:
        """手动校准过的快照不被自动日结覆盖 —— 那是用户明确表达过的口径。"""
        daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="entry")
        account = _accounts(session)[0]
        row = (
            session.query(AssetSnapshot)
            .filter(AssetSnapshot.snapshot_date == TODAY, AssetSnapshot.account_id == account.id)
            .one()
        )
        row.source = "manual"
        row.balance_minor = 999_999
        session.flush()

        _spend(session, day=TODAY, amount=1_000)
        daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="expense")
        session.refresh(row)
        assert row.balance_minor == 999_999

    def test_daily_stat_stored_once_per_day(self, session: Session) -> None:
        _spend(session, day=TODAY, amount=1_000)
        daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="entry")
        daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="entry")
        count = session.query(DailyStat).filter(DailyStat.date == TODAY).count()
        assert count == 1, "重复读取不应产生重复行"


class TestAssetWall:
    def test_wall_groups_and_summary(self, session: Session) -> None:
        cash = _accounts(session)[0]
        accounts_service.update_account(session, cash.id, initial_balance_minor=50_000)
        card = accounts_service.create_account(
            session,
            name="信用卡",
            type=AccountType.CREDIT_CARD.value,
            credit_limit_minor=1_000_000,
            initial_balance_minor=-200_000,
        )
        payload = assets_service.wall(session)

        group_keys = [group["key"] for group in payload["groups"]]
        assert "cash" in group_keys and "bank" in group_keys
        cards = {card["id"]: card for group in payload["groups"] for card in group["cards"]}
        assert cards[card.id]["credit_used_minor"] == 200_000
        assert cards[card.id]["credit_available_minor"] == 800_000
        assert payload["summary"]["available_minor"] == 50_000
        assert payload["summary"]["credit_available_minor"] == 800_000
        assert payload["artworks"], "卡片墙要带上卡面字典，否则前端画不出卡"

    def test_reorder_is_all_or_nothing(self, session: Session) -> None:
        accounts = _accounts(session)
        order = list(reversed([account.id for account in accounts]))
        assert assets_service.reorder_accounts(session, order) == len(order)
        refreshed = [account.id for account in accounts_service.list_accounts(session)]
        assert refreshed == order

        from accountbook.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            assets_service.reorder_accounts(session, [*order, 999_999])

    def test_update_card_links_institution_name(self, session: Session) -> None:
        account = _accounts(session)[0]
        assets_service.update_account_cards(session, account.id, brand_key="cmb", card_style="royal")
        session.refresh(account)
        assert account.institution == "招商银行"
        assert account.card_style == "royal"

    def test_unknown_card_style_rejected(self, session: Session) -> None:
        from accountbook.core.errors import NotFoundError

        account = _accounts(session)[0]
        with pytest.raises(NotFoundError):
            assets_service.update_account_cards(session, account.id, card_style="不存在的卡面")

    def test_unknown_card_network_rejected(self, session: Session) -> None:
        account = _accounts(session)[0]
        with pytest.raises(ValidationError):
            assets_service.update_account_cards(session, account.id, card_network="paypal")


class TestInstitutionsAndArtworks:
    def test_builtin_institution_protected(self, session: Session) -> None:
        institution = next(item for item in assets_service.list_institutions(session) if item.key == "cmb")
        with pytest.raises(ProtectedEntityError):
            assets_service.delete_institution(session, institution.id)

    def test_custom_institution_lifecycle(self, session: Session) -> None:
        created = assets_service.create_institution(
            session, key="my_bank", name="我的银行", brand_color="teal"
        )
        assert created.key == "my_bank"
        with pytest.raises(ConflictError):
            assets_service.create_institution(session, key="my_bank", name="重复")

        account = _accounts(session)[0]
        assets_service.update_account_cards(session, account.id, brand_key="my_bank")
        with pytest.raises(ConflictError, match="账户"):
            assets_service.delete_institution(session, created.id)

    def test_institution_key_validation(self, session: Session) -> None:
        with pytest.raises(ValidationError):
            assets_service.create_institution(session, key="有中文", name="X")
        with pytest.raises(ValidationError):
            assets_service.create_institution(session, key="A", name="X")

    def test_artwork_spec_is_validated(self, session: Session) -> None:
        """卡面配方必须受限：一个拼错的键只会让卡片变白，那种失败很难定位。"""
        with pytest.raises(ValidationError, match="色标"):
            assets_service.create_card_artwork(session, key="bad1", name="X", spec={"stops": [["#fff", 0]]})
        with pytest.raises(ValidationError):
            assets_service.create_card_artwork(
                session, key="bad2", name="X", spec={"stops": [["not-a-color", 0], ["#ffffff", 100]]}
            )
        with pytest.raises(ValidationError, match="纹理"):
            assets_service.create_card_artwork(
                session,
                key="bad3",
                name="X",
                spec={"stops": [["#000000", 0], ["#ffffff", 100]], "texture": "未知"},
            )

    def test_custom_artwork_lifecycle(self, session: Session) -> None:
        artwork = assets_service.create_card_artwork(
            session,
            key="ocean",
            name="海洋",
            spec={
                "stops": [["#003366", 0], ["#66ccff", 100]],
                "texture": "aurora",
                "ink": "light",
                "sheen": 90,
            },
        )
        assert artwork.kind == "uploaded"
        account = _accounts(session)[0]
        assets_service.update_account_cards(session, account.id, card_style="ocean")
        with pytest.raises(ConflictError, match="账户"):
            assets_service.delete_card_artwork(session, artwork.id)

    def test_builtin_artwork_protected(self, session: Session) -> None:
        artwork = assets_service.list_card_artworks(session)[0]
        with pytest.raises(ProtectedEntityError):
            assets_service.delete_card_artwork(session, artwork.id)
