"""Условие напоминаний «ни разу не подключался к VPN».

В базе бота этого признака нет — его знает панель. Условие получает множество
неподключавшихся аккаунтов панели; SQL (рассылка, счётчик аудитории) и проверка
одного человека (карточка в кабинете) обязаны отвечать одинаково, а при молчащей
панели — не писать никому.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, User, UserReminder
from app.services import connect_reminder_service
from app.services.user_reminders import dispatcher
from app.services.user_reminders.conditions import condition_clauses, matches, parse_conditions
from app.services.user_reminders.texts import validate_button, validate_texts
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
TABLES = list(Base.metadata.sorted_tables)
NEVER = frozenset({10, 13, 14, 15})
CONDITIONS = parse_conditions({'subscription': {'segment': 'trial'}, 'never_connected': True})


def _user(uid: int, **kw) -> User:
    base = dict(
        id=uid,
        telegram_id=1000 + uid,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
        created_at=NOW - timedelta(days=5),
    )
    base.update(kw)
    return User(**base)


def _sub(sid: int, uid: int, panel_id: int | None, *, trial: bool = True, days: int = 3) -> Subscription:
    return Subscription(
        id=sid,
        user_id=uid,
        remnawave_short_id=f's{sid}',
        remnawave_id=panel_id,
        status='active',
        is_trial=trial,
        start_date=NOW - timedelta(days=1),
        end_date=NOW + timedelta(days=days),
    )


USERS = [_user(1), _user(2), _user(3, remnawave_id=13), _user(4), _user(5)]
SUBS = [
    _sub(1, 1, 10),  # триал, не подключался
    _sub(2, 2, 11),  # триал, подключался
    _sub(3, 3, None),  # старая подписка: аккаунт панели — у пользователя
    _sub(4, 4, 14, days=-1),  # триал истёк
    _sub(5, 5, 15, trial=False),  # платный — не триал
]
EXPECTED = {1, 3}


async def _seed(db) -> None:
    db.add_all([_user(u.id, remnawave_id=u.remnawave_id) for u in USERS])
    await db.flush()
    db.add_all([_sub(s.id, s.user_id, s.remnawave_id, trial=s.is_trial, days=(s.end_date - NOW).days) for s in SUBS])
    await db.commit()


@pytest.mark.asyncio
async def test_sql_and_single_person_check_agree(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        rows = await db.execute(
            select(User.id).where(*condition_clauses(CONDITIONS, now=NOW, never_connected_ids=NEVER))
        )
        assert set(rows.scalars()) == EXPECTED

    by_user = {}
    for sub in SUBS:
        by_user.setdefault(sub.user_id, []).append(sub)
    single = {
        user.id
        for user in USERS
        if matches(user, by_user.get(user.id, []), CONDITIONS, now=NOW, never_connected_ids=NEVER)
    }
    assert single == EXPECTED


@pytest.mark.asyncio
async def test_unknown_panel_means_nobody(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        rows = await db.execute(select(User.id).where(*condition_clauses(CONDITIONS, now=NOW)))
        assert list(rows.scalars()) == []
    assert not matches(USERS[0], [SUBS[0]], CONDITIONS, now=NOW, never_connected_ids=None)


@pytest.mark.asyncio
async def test_dispatcher_skips_the_reminder_when_panel_is_silent(monkeypatch):
    monkeypatch.setattr(settings, 'USER_REMINDERS_QUIET_HOURS_START', 21)
    monkeypatch.setattr(settings, 'USER_REMINDERS_QUIET_HOURS_END', 10)
    monkeypatch.setattr(settings, 'USER_REMINDERS_DAILY_LIMIT_ENABLED', False)
    monkeypatch.setattr(dispatcher, 'get_local_timezone', lambda: ZoneInfo('UTC'))
    reminder = UserReminder(
        id=1,
        name='r',
        channels='bot',
        category='service',
        conditions={'subscription': {'segment': 'trial'}, 'never_connected': True},
        repeat_every_days=1,
        max_sends=2,
        texts={'ru': {'title': 't', 'body': 'b'}},
        button_kind='none',
        is_active=True,
    )
    sent: list[int] = []

    async def deliver(user, _reminder, _bot) -> bool:
        sent.append(user.id)
        return True

    async def no_sleep(_):
        return None

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        db.add(reminder)
        await db.commit()

        silent = AsyncMock(return_value=None)
        monkeypatch.setattr(connect_reminder_service, 'get_never_connected_panel_ids', silent)
        await dispatcher.run_reminder_pass(db, object(), deliver=deliver, now=NOW, sleep=no_sleep)
        assert sent == []

        answered = AsyncMock(return_value=NEVER)
        monkeypatch.setattr(connect_reminder_service, 'get_never_connected_panel_ids', answered)
        await dispatcher.run_reminder_pass(db, object(), deliver=deliver, now=NOW, sleep=no_sleep)
        assert set(sent) == EXPECTED
        answered.assert_awaited_once()  # панель спрашиваем раз на проход, а не на каждого


def _migration():
    path = Path('migrations/alembic/versions/0146_reminder_trial_not_connected.py')
    spec = importlib.util.spec_from_file_location('m0146', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builtin_trial_reminder_is_shipped_disabled_and_valid():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0146', '0145')
    row = module.BUILTIN_TRIAL_NOT_CONNECTED
    assert row['builtin_key'] == connect_reminder_service.BUILTIN_KEY
    assert row['is_active'] is False
    conditions = parse_conditions(row['conditions'])
    assert conditions.never_connected is True
    assert conditions.subscription.segment == 'trial'
    assert set(validate_texts(row['texts'])) == {'ru', 'en', 'ua', 'zh', 'fa'}
    validate_button(row['button_kind'], row['button_target'], row['texts'])


@pytest.mark.asyncio
async def test_manual_reminder_text_comes_from_the_template(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        assert await connect_reminder_service.get_template(db) is None
        db.add(
            UserReminder(
                name='Триал без подключения',
                builtin_key=connect_reminder_service.BUILTIN_KEY,
                channels='both',
                category='service',
                conditions={},
                repeat_every_days=1,
                max_sends=2,
                texts={'ru': {'title': 'Свой <заголовок>', 'body': 'Своё тело', 'button': 'Жми'}},
                button_kind='cabinet',
                button_target='/subscription',
            )
        )
        await db.commit()
        template = await connect_reminder_service.get_template(db)

    user = _user(1)
    text = connect_reminder_service.default_text(user, template)
    assert text == '<b>Свой &lt;заголовок&gt;</b>\n\nСвоё тело'
    keyboard = connect_reminder_service.connect_keyboard(user, template)
    assert keyboard.inline_keyboard[0][0].text == 'Жми'
    # Без шаблона — стандартный текст и подпись из переводов.
    assert 'подключились' in connect_reminder_service.default_text(user)
