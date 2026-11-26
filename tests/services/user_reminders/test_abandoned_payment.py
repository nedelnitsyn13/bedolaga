"""Напоминание «брошенная оплата» и выручка напоминаний.

Человек создал платёж и не оплатил — через час бот напоминает. Один раз на каждый
брошенный платёж: новый неоплаченный счёт снова «взводит» напоминание, а оплата
другим способом, автоплатёж бота и старые счета — нет. Выручка: пополнение в
окне после отправки засчитывается последнему напоминанию перед оплатой.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.user_reminder import reminder_revenue
from app.database.models import (
    Base,
    CryptoBotPayment,
    PlategaPayment,
    Transaction,
    User,
    UserReminder,
    UserReminderSend,
)
from app.services.user_reminders import dispatcher
from app.services.user_reminders.abandoned import PAYMENT_MODELS, has_abandoned_payment
from app.services.user_reminders.cabinet import active_cards_for_user, dismiss_reminder
from app.services.user_reminders.conditions import condition_clauses, parse_conditions
from app.services.user_reminders.texts import validate_button, validate_texts
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
TABLES = list(Base.metadata.sorted_tables)
CONDITIONS = parse_conditions({'abandoned_payment_hours': 1})


@pytest.fixture(autouse=True)
def reminder_settings(monkeypatch):
    monkeypatch.setattr(settings, 'USER_REMINDERS_QUIET_HOURS_START', 21)
    monkeypatch.setattr(settings, 'USER_REMINDERS_QUIET_HOURS_END', 10)
    monkeypatch.setattr(settings, 'USER_REMINDERS_DAILY_LIMIT_ENABLED', False)
    monkeypatch.setattr(settings, 'USER_REMINDERS_ATTRIBUTION_DAYS', 3)
    monkeypatch.setattr(dispatcher, 'get_local_timezone', lambda: ZoneInfo('UTC'))


def _user(uid: int) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
        created_at=NOW - timedelta(days=30),
    )


_seq = iter(range(1, 10_000))


def _platega(uid: int, ago: timedelta, *, paid: bool = False, purpose: str | None = None) -> PlategaPayment:
    n = next(_seq)
    return PlategaPayment(
        id=n,
        user_id=uid,
        correlation_id=f'c{n}',
        amount_kopeks=50_000,
        payment_method_code=2,
        status='CONFIRMED' if paid else 'PENDING',
        is_paid=paid,
        metadata_json={'purpose': purpose} if purpose else None,
        created_at=NOW - ago,
    )


def _deposit(uid: int, at: datetime, amount: int = 50_000, method: str = 'platega') -> Transaction:
    return Transaction(
        id=next(_seq),
        user_id=uid,
        type='deposit',
        amount_kopeks=amount,
        payment_method=method,
        is_completed=True,
        created_at=at,
    )


def _reminder(rid: int = 1, **kw) -> UserReminder:
    base = dict(
        id=rid,
        name='Брошенная оплата',
        channels='bot',
        category='service',
        conditions={'abandoned_payment_hours': 1},
        repeat_every_days=1,
        max_sends=1,
        texts={'ru': {'title': 't', 'body': 'b'}},
        button_kind='none',
        is_active=True,
    )
    base.update(kw)
    return UserReminder(**base)


async def _who(db, now: datetime = NOW) -> set[int]:
    rows = await db.execute(select(User.id).where(*condition_clauses(CONDITIONS, now=now)))
    return set(rows.scalars())


def test_every_payment_table_has_the_columns_the_condition_needs():
    for model in PAYMENT_MODELS:
        columns = set(model.__table__.c.keys())
        assert {'user_id', 'created_at'} <= columns, model.__name__
        assert 'is_paid' in columns or 'status' in columns, model.__name__


@pytest.mark.asyncio
async def test_who_counts_as_abandoned(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(uid) for uid in range(1, 8)])
        await db.flush()
        db.add_all(
            [
                _platega(1, timedelta(hours=2)),  # брошен — да
                _platega(2, timedelta(minutes=20)),  # ещё может оплатить — нет
                _platega(3, timedelta(hours=2), paid=True),  # оплачен — нет
                _platega(4, timedelta(days=8)),  # слишком старый — нет
                _platega(5, timedelta(hours=2), purpose='recurrent_topup'),  # автоплатёж бота — нет
                _platega(6, timedelta(hours=2)),  # оплатил другим способом — нет
                _deposit(6, NOW - timedelta(hours=1), method='yookassa'),
                _platega(7, timedelta(hours=3)),  # пополнение вручную админом не отменяет — да
                _deposit(7, NOW - timedelta(hours=1), method='manual'),
                CryptoBotPayment(
                    id=1,
                    user_id=2,
                    invoice_id='i1',
                    amount='5',
                    asset='USDT',
                    status='active',
                    created_at=NOW - timedelta(hours=5),
                ),  # CryptoBot без is_paid: статус active — брошен
            ]
        )
        await db.commit()
        assert await _who(db) == {1, 2, 7}
        assert await has_abandoned_payment(db, 1, 1, now=NOW)
        assert not await has_abandoned_payment(db, 3, 1, now=NOW)


@pytest.mark.asyncio
async def test_one_message_per_abandoned_payment_and_new_one_rearms(monkeypatch):
    sent: list[int] = []

    async def deliver(user, _reminder, _bot) -> bool:
        sent.append(user.id)
        return True

    async def no_sleep(_):
        return None

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        await db.flush()
        db.add_all([_reminder(), _platega(1, timedelta(hours=2))])
        await db.commit()

        await dispatcher.run_reminder_pass(db, object(), deliver=deliver, now=NOW, sleep=no_sleep)
        assert sent == [1]
        # Через двое суток счёт всё ещё не оплачен, но о нём уже писали — молчим.
        later = NOW + timedelta(days=2)
        await dispatcher.run_reminder_pass(db, object(), deliver=deliver, now=later, sleep=no_sleep)
        assert sent == [1]

        # Новый брошенный платёж: напоминаем снова, несмотря на max_sends=1.
        db.add(_platega(1, timedelta(hours=2) - timedelta(days=2)))
        await db.commit()
        await dispatcher.run_reminder_pass(db, object(), deliver=deliver, now=later, sleep=no_sleep)
        assert sent == [1, 1]
        sends = (await db.execute(select(UserReminderSend))).scalars().all()
        assert len(sends) == 2


@pytest.mark.asyncio
async def test_cabinet_card_returns_for_a_new_payment_after_dismissal(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(1)
        db.add(user)
        await db.flush()
        db.add_all(
            [
                _reminder(channels='cabinet', texts={'ru': {'title': 't', 'body': 'b'}}),
                _platega(1, timedelta(hours=2)),
            ]
        )
        await db.commit()
        assert len(await active_cards_for_user(db, user, lang='ru', now=NOW)) == 1
        assert await dismiss_reminder(db, user, 1, now=NOW)
        assert await active_cards_for_user(db, user, lang='ru', now=NOW) == []

        db.add(_platega(1, -timedelta(minutes=1)))  # новый счёт после закрытия
        await db.commit()
        later = NOW + timedelta(hours=2)
        assert len(await active_cards_for_user(db, user, lang='ru', now=later)) == 1


@pytest.mark.asyncio
async def test_revenue_goes_to_the_last_reminder_within_the_window(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2), _user(3)])
        await db.flush()
        db.add_all([_reminder(1), _reminder(2, name='Другое', conditions={})])
        await db.flush()
        db.add_all(
            [
                UserReminderSend(reminder_id=1, user_id=1, sent_at=NOW - timedelta(days=5)),
                UserReminderSend(reminder_id=2, user_id=1, sent_at=NOW - timedelta(days=1)),
                _deposit(1, NOW - timedelta(hours=10), amount=30_000),  # последнее перед ним — №2
                UserReminderSend(reminder_id=1, user_id=2, sent_at=NOW - timedelta(days=2)),
                _deposit(2, NOW - timedelta(days=1), amount=20_000),  # №1
                _deposit(2, NOW - timedelta(hours=1), amount=10_000),  # №1, тот же человек
                _deposit(2, NOW - timedelta(days=3), amount=99_000),  # до отправки — ничьё
                UserReminderSend(reminder_id=1, user_id=3, sent_at=NOW - timedelta(days=10)),
                _deposit(3, NOW - timedelta(days=1), amount=70_000),  # окно 3 дня прошло — ничьё
                _deposit(3, NOW - timedelta(days=9), amount=5_000, method='manual'),  # не деньги клиента
            ]
        )
        await db.commit()

        revenue = await reminder_revenue(db, now=NOW)
        assert revenue[1].revenue_kopeks == 30_000 and revenue[1].payments == 2 and revenue[1].users == 1
        assert revenue[2].revenue_kopeks == 30_000 and revenue[2].payments == 1 and revenue[2].users == 1


def _migration():
    path = Path('migrations/alembic/versions/0152_reminder_sends_and_abandoned_payment.py')
    spec = importlib.util.spec_from_file_location('m0152', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builtin_abandoned_reminder_is_shipped_disabled_and_valid():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0152', '0151')
    row = module.BUILTIN_ABANDONED_PAYMENT
    assert row['is_active'] is False
    assert parse_conditions(row['conditions']).abandoned_payment_hours == 1
    assert set(validate_texts(row['texts'])) == {'ru', 'en', 'ua', 'zh', 'fa'}
    validate_button(row['button_kind'], row['button_target'], row['texts'])
