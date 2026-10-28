"""Разовая выписка чеков «Мой налог» по оплатам YooKassa без чека.

Сторожим: берутся только завершённые пополнения YooKassa без чека с нужной
даты; клиентам ничего не рассылается; доход, уже выписанный вручную, не
дублируется; без сверки со списком доходов чеки не создаются.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

from app.database.models import PromoGroup, Transaction, User, UserPromoGroup
from scripts import nalogo_backfill_receipts as script
from tests.fixtures.sqlite_memory import memory_session


TABLES = [User.__table__, Transaction.__table__, PromoGroup.__table__, UserPromoGroup.__table__]
PAID = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=2)


async def _seed(db) -> User:
    user = User(telegram_id=555, first_name='Тест', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()

    def tx(**kw):
        base = dict(
            user_id=user.id,
            type='deposit',
            amount_kopeks=20000,
            payment_method='yookassa',
            external_id='yk-1',
            is_completed=True,
            created_at=PAID,
            completed_at=PAID,
        )
        base.update(kw)
        return Transaction(**base)

    db.add_all(
        [
            tx(),
            tx(external_id='yk-old', created_at=datetime(2026, 8, 31, tzinfo=UTC)),  # до --since
            tx(external_id='yk-has', receipt_uuid='already'),  # чек уже есть
            tx(external_id='yk-cp', payment_method='cryptobot'),  # не YooKassa
            tx(external_id='yk-pending', is_completed=False),  # не завершена
            tx(external_id=None, type='subscription_payment', amount_kopeks=-20000),  # списание
        ]
    )
    await db.commit()
    return user


class _FakeNalogo:
    def __init__(self, incomes=None):
        self.incomes = [] if incomes is None else incomes
        self.create_receipt = AsyncMock(return_value='new-uuid')

    async def get_incomes(self, from_date, to_date, limit=100):
        return self.incomes


async def test_dry_run_lists_only_matching_payments_and_creates_nothing(monkeypatch, capsys) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        nalogo = _FakeNalogo()

        code = await script.backfill(db, nalogo, date(2026, 9, 1), date(2026, 10, 28), apply=False, window=WINDOW)

        assert code == 0
        out = capsys.readouterr().out
        assert 'Оплат без чека: 1' in out
        nalogo.create_receipt.assert_not_awaited()


async def test_apply_creates_receipt_with_original_time_and_saves_it(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        nalogo = _FakeNalogo()

        code = await script.backfill(db, nalogo, date(2026, 9, 1), date(2026, 10, 28), apply=True, window=WINDOW)

        assert code == 0
        kwargs = nalogo.create_receipt.await_args.kwargs
        assert kwargs['operation_time'] == PAID
        assert kwargs['payment_id'] == 'yk-1'
        assert kwargs['amount'] == 200.0
        assert kwargs['queue_on_failure'] is False
        tx = (await db.execute(script.select(Transaction).where(Transaction.external_id == 'yk-1'))).scalar_one()
        assert tx.receipt_uuid == 'new-uuid'


async def test_income_already_in_moy_nalog_is_linked_not_duplicated(monkeypatch) -> None:
    income = {'approvedReceiptUuid': 'manual-uuid', 'totalAmount': 200, 'operationTime': '2026-09-15T15:01:00+03:00'}
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        nalogo = _FakeNalogo([income])

        await script.backfill(db, nalogo, date(2026, 9, 1), date(2026, 10, 28), apply=True, window=WINDOW)

        nalogo.create_receipt.assert_not_awaited()
        tx = (await db.execute(script.select(Transaction).where(Transaction.external_id == 'yk-1'))).scalar_one()
        assert tx.receipt_uuid == 'manual-uuid'


async def test_no_receipts_without_income_list(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        nalogo = _FakeNalogo(None)
        nalogo.incomes = None

        code = await script.backfill(db, nalogo, date(2026, 9, 1), date(2026, 10, 28), apply=True, window=WINDOW)

        assert code == 2
        nalogo.create_receipt.assert_not_awaited()


def test_find_existing_income_rules() -> None:
    base = {'approvedReceiptUuid': 'u1', 'totalAmount': 200, 'operationTime': '2026-09-15T12:01:00Z'}
    assert script.find_existing_income([base], 200.0, PAID, WINDOW, set()) == 'u1'
    assert script.find_existing_income([base], 200.0, PAID, WINDOW, {'u1'}) is None  # уже привязан
    assert script.find_existing_income([base], 350.0, PAID, WINDOW, set()) is None  # другая сумма
    far = {**base, 'operationTime': '2026-09-15T12:10:00Z'}
    assert script.find_existing_income([far], 200.0, PAID, WINDOW, set()) is None  # далеко по времени
    cancelled = {**base, 'cancellationInfo': {'comment': 'возврат'}}
    assert script.find_existing_income([cancelled], 200.0, PAID, WINDOW, set()) is None
