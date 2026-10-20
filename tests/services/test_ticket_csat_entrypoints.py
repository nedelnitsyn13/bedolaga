"""Оценка поддержки из бота и из кабинета, живые обновления тикетов в кабинете.

- кнопка в боте и POST в кабинете сохраняют оценку и отвечают понятной причиной отказа;
- блок оценки в кабинете виден только у тикетов, начатых в кабинете;
- живое обновление приходит автору тикета, даже когда событие о сообщении админа.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cabinet.routes import tickets as tickets_route
from app.cabinet.schemas.tickets import TicketCsatRequest
from app.cabinet.services import ticket_live_updates as live
from app.database.models import Base, Ticket, TicketMessage, TicketStatus, User
from app.handlers import tickets as bot_tickets
from app.services import ticket_csat_service as csat
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


TABLES = [User.__table__, Ticket.__table__, TicketMessage.__table__]


async def _db(monkeypatch, *tickets: dict):
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        db.add(User(id=10, telegram_id=555, first_name='Иван', language='ru'))
        db.add(User(id=20, telegram_id=None, email='a@example.org', first_name='Анна', language='ru'))
        for data in tickets:
            db.add(Ticket(title='VPN', **data))
        await db.commit()
    monkeypatch.setattr(csat, '_after_rating', AsyncMock())
    return engine, maker


# ============ бот ============


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


async def test_bot_button_saves_rating_and_thanks(monkeypatch):
    engine, maker = await _db(monkeypatch, {'id': 1, 'user_id': 10, 'status': 'closed', 'source': 'bot'})
    callback = _callback('csat_rate_1_5')

    async with maker() as db:
        await bot_tickets.rate_ticket_support(callback, User(id=10, language='ru'), db)
        assert (await db.get(Ticket, 1)).csat_rating == 5
    await engine.dispose()

    assert '⭐⭐⭐⭐⭐' in callback.message.edit_text.await_args.args[0]


async def test_bot_button_twice_says_already_rated(monkeypatch):
    engine, maker = await _db(
        monkeypatch, {'id': 1, 'user_id': 10, 'status': 'closed', 'source': 'bot', 'csat_rating': 3}
    )
    callback = _callback('csat_rate_1_1')

    async with maker() as db:
        await bot_tickets.rate_ticket_support(callback, User(id=10, language='ru'), db)
        assert (await db.get(Ticket, 1)).csat_rating == 3
    await engine.dispose()

    assert callback.answer.await_args.kwargs == {'show_alert': True}
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


# ============ кабинет ============


@pytest.fixture
def _tickets_enabled(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'both')


@pytest.mark.usefixtures('_tickets_enabled')
async def test_cabinet_rates_own_closed_ticket(monkeypatch):
    engine, maker = await _db(
        monkeypatch,
        {'id': 1, 'user_id': 20, 'status': 'closed', 'source': 'cabinet', 'csat_requested_at': datetime.now(UTC)},
    )
    user = User(id=20, telegram_id=None)

    async with maker() as db:
        response = await tickets_route.rate_ticket(1, TicketCsatRequest(rating=4), user, db)
    await engine.dispose()

    assert (response.csat_rating, response.csat_available) == (4, True)


@pytest.mark.usefixtures('_tickets_enabled')
@pytest.mark.parametrize(
    ('ticket', 'user_id', 'code'),
    [
        ({'id': 1, 'user_id': 10, 'status': 'closed'}, 20, 404),
        ({'id': 1, 'user_id': 20, 'status': 'open'}, 20, 409),
        ({'id': 1, 'user_id': 20, 'status': 'closed', 'csat_rating': 5}, 20, 409),
    ],
)
async def test_cabinet_refusals(monkeypatch, ticket, user_id, code):
    engine, maker = await _db(monkeypatch, ticket)

    async with maker() as db:
        with pytest.raises(HTTPException) as refused:
            await tickets_route.rate_ticket(1, TicketCsatRequest(rating=3), User(id=user_id), db)
    await engine.dispose()

    assert refused.value.status_code == code


async def test_cabinet_disabled_tickets_refuse_rating(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'contact')
    with pytest.raises(HTTPException) as refused:
        await tickets_route.rate_ticket(1, TicketCsatRequest(rating=3), User(id=20), AsyncMock())
    assert refused.value.status_code == 403


@pytest.mark.parametrize(
    ('source', 'telegram_id', 'requested', 'available'),
    [
        ('cabinet', 555, True, True),
        ('bot', None, True, False),
        (None, None, True, True),  # старый тикет без source: пользователь без Telegram
        (None, 555, True, False),
        ('cabinet', None, False, False),  # ещё не спрашивали — блока нет
    ],
)
def test_rating_block_only_where_ticket_started(source, telegram_id, requested, available):
    ticket = Ticket(id=1, source=source, csat_requested_at=datetime.now(UTC) if requested else None)
    assert tickets_route._csat_fields(ticket, User(telegram_id=telegram_id))['csat_available'] is available


# ============ живые обновления ============


async def test_admin_reply_reaches_ticket_owner(monkeypatch):
    ws = SimpleNamespace(send_to_user=AsyncMock(), send_to_admins=AsyncMock())
    monkeypatch.setattr(live, 'cabinet_ws_manager', ws)
    monkeypatch.setattr(live, '_owner_id', AsyncMock(return_value=10))

    # user_id в событии — автор сообщения (админ 1), а не владелец тикета.
    await live.publish_ticket_update({'ticket_id': 7, 'user_id': 1, 'is_admin': True})

    expected = {'type': 'ticket.updated', 'ticket_id': 7, 'status': None}
    ws.send_to_admins.assert_awaited_once_with(expected)
    ws.send_to_user.assert_awaited_once_with(10, expected)


async def test_status_change_carries_new_status(monkeypatch):
    ws = SimpleNamespace(send_to_user=AsyncMock(), send_to_admins=AsyncMock())
    monkeypatch.setattr(live, 'cabinet_ws_manager', ws)
    monkeypatch.setattr(live, '_owner_id', AsyncMock(return_value=None))

    await live.publish_ticket_update({'ticket_id': '7', 'new_status': TicketStatus.CLOSED.value})
    await live.publish_ticket_update({'ticket_id': None})

    ws.send_to_admins.assert_awaited_once_with({'type': 'ticket.updated', 'ticket_id': 7, 'status': 'closed'})
    ws.send_to_user.assert_not_awaited()


# ============ закрытие из админки кабинета ============


@pytest.mark.parametrize(('new_status', 'emitted'), [('closed', True), ('open', False)])
async def test_cabinet_admin_close_emits_status_changed(monkeypatch, new_status, emitted):
    from app.cabinet.routes import admin_tickets
    from app.cabinet.routes.admin_tickets import AdminStatusUpdateRequest
    from app.services.event_emitter import event_emitter

    engine, maker = await _db(monkeypatch, {'id': 1, 'user_id': 10, 'status': 'open', 'source': 'bot'})
    emit = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', emit)

    async with maker() as db:
        await admin_tickets.update_ticket_status(1, AdminStatusUpdateRequest(status=new_status), User(id=1), db)
    await engine.dispose()

    if emitted:
        event, payload = emit.await_args.args
        assert event == 'ticket.status_changed'
        assert (payload['ticket_id'], payload['old_status'], payload['new_status']) == (1, 'open', 'closed')
    else:
        emit.assert_not_awaited()
