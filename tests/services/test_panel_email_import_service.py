"""Перенос почты из панели в Telegram-аккаунты.

Прежний бот хранил почту клиента в панели рядом с Telegram ID, новый её не брал.
Без Telegram человек входил в кабинет по почте и получал пустой аккаунт вместо
своего с подпиской. Переносим, но только там, где ящик ничей и ни с кем не спорит.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, User, UserStatus
from app.services import panel_email_import_service as svc
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


async def _db(monkeypatch, *users: dict):
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        for data in users:
            db.add(User(language='ru', **data))
        await db.commit()
    monkeypatch.setattr(svc.settings, 'PANEL_EMAIL_IMPORT_ENABLED', True)
    monkeypatch.setattr(type(svc.settings), 'get_admin_emails', lambda self: ['boss@example.org'])
    return engine, maker


def _panel(*pairs):
    return [{'telegramId': tg, 'email': email} for tg, email in pairs]


async def _emails(maker) -> dict[int, tuple[str | None, bool]]:
    async with maker() as db:
        rows = (await db.execute(select(User))).scalars().all()
        return {u.id: (u.email, u.email_verified) for u in rows}


def test_collect_drops_email_shared_by_several_telegram_ids():
    pairs = svc.collect_panel_emails(
        _panel(
            (1, ' Ivan@Example.org '), (2, 'shared@example.org'), (3, 'shared@example.org'), (4, None), (None, 'x@y.z')
        )
    )
    assert pairs == {1: 'ivan@example.org'}


async def test_email_moves_to_telegram_account_as_verified(monkeypatch):
    engine, maker = await _db(monkeypatch, {'id': 1, 'telegram_id': 111})

    async with maker() as db:
        imported = await svc.import_panel_emails(db, _panel((111, 'Ivan@Example.org')))
    emails = await _emails(maker)
    await engine.dispose()

    assert [(i.user_id, i.email) for i in imported] == [(1, 'ivan@example.org')]
    assert emails[1] == ('ivan@example.org', True)


@pytest.mark.parametrize(
    ('users', 'panel_email', 'reason'),
    [
        ([{'id': 1, 'telegram_id': 111, 'email': 'own@example.org'}], 'other@example.org', 'своя почта уже есть'),
        (
            [{'id': 1, 'telegram_id': 111}, {'id': 2, 'telegram_id': None, 'email': 'ivan@example.org'}],
            'ivan@example.org',
            'ящик занят другим аккаунтом',
        ),
        (
            [{'id': 1, 'telegram_id': 111}, {'id': 2, 'telegram_id': None, 'email': 'iv.an@gmail.com'}],
            'ivan+vpn@gmail.com',
            'тот же ящик под алиасом',
        ),
        ([{'id': 1, 'telegram_id': 111}], 'boss@example.org', 'админский адрес'),
        ([{'id': 1, 'telegram_id': 111, 'status': UserStatus.DELETED.value}], 'ivan@example.org', 'удалённый аккаунт'),
        ([{'id': 1, 'telegram_id': 111}], 'not-an-email', 'не адрес'),
    ],
)
async def test_email_is_not_taken(monkeypatch, users, panel_email, reason):
    engine, maker = await _db(monkeypatch, *users)
    before = await _emails(maker)

    async with maker() as db:
        imported = await svc.import_panel_emails(db, _panel((111, panel_email)))
    after = await _emails(maker)
    await engine.dispose()

    assert imported == [], reason
    assert after == before, reason


async def test_disabled_setting_changes_nothing(monkeypatch):
    engine, maker = await _db(monkeypatch, {'id': 1, 'telegram_id': 111})
    monkeypatch.setattr(svc.settings, 'PANEL_EMAIL_IMPORT_ENABLED', False)

    async with maker() as db:
        assert await svc.import_panel_emails(db, _panel((111, 'ivan@example.org'))) == []
    assert (await _emails(maker))[1] == (None, False)
    await engine.dispose()


async def test_owner_is_told_in_telegram_and_blocked_bot_is_not_an_error(monkeypatch):
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=[None, TelegramForbiddenError(method=MagicMock(), message='Forbidden: bot was blocked by the user')]
    )
    monkeypatch.setattr(svc, '_get_bot', lambda: bot)
    monkeypatch.setattr(svc, '_NOTIFY_PAUSE_SECONDS', 0)
    monkeypatch.setattr(svc.settings, 'CABINET_URL', 'https://cabinet.example.org')

    delivered = await svc.notify_imported(
        [svc.ImportedEmail(1, 111, 'ivan@example.org', 'ru'), svc.ImportedEmail(2, 222, 'anna@example.org', 'ru')]
    )

    assert delivered == 1
    first = bot.send_message.await_args_list[0].kwargs
    assert first['chat_id'] == 111
    assert 'ivan@example.org' in first['text']
    assert 'https://cabinet.example.org' in first['text']
    assert 'Забыли пароль' in first['text']


async def test_sync_failure_of_import_is_contained(monkeypatch):
    db = MagicMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(svc, 'import_panel_emails', AsyncMock(side_effect=RuntimeError('boom')))

    assert await svc.import_and_notify(db, []) == 0
    db.rollback.assert_awaited_once()
