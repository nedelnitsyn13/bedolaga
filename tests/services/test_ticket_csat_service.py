"""Опрос «оцените поддержку» после закрытия тикета.

Главные свойства:
- спрашиваем один раз на тикет и только там, где тикет начат (бот или кабинет);
- оценку принимает только автор закрытого тикета, и только один раз;
- запрос запускает событие закрытия, общее для всех путей.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Ticket, TicketMessage, TicketStatus, User
from app.services import ticket_csat_service as csat
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


TABLES = [User.__table__, Ticket.__table__, TicketMessage.__table__]


@pytest.fixture
def maker(monkeypatch):
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    ready = False

    def make():
        return _Session(session_maker, engine)

    class _Session:
        def __init__(self, inner, eng):
            self._inner, self._engine, self._session = inner, eng, None

        async def __aenter__(self):
            nonlocal ready
            if not ready:
                async with self._engine.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
                ready = True
            self._session = self._inner()
            return await self._session.__aenter__()

        async def __aexit__(self, *exc):
            return await self._session.__aexit__(*exc)

    monkeypatch.setattr(csat, 'AsyncSessionLocal', make)
    monkeypatch.setattr(csat.settings, 'TICKET_CSAT_ENABLED', True)
    return make


@pytest.fixture
def bot(monkeypatch):
    fake = MagicMock()
    fake.send_message = AsyncMock()
    monkeypatch.setattr(csat, '_get_bot', lambda: fake)
    return fake


@pytest.fixture
def ws(monkeypatch):
    manager = SimpleNamespace(send_to_user=AsyncMock(), send_to_admins=AsyncMock())
    monkeypatch.setattr('app.cabinet.ws_manager.cabinet_ws_manager', manager)
    return manager


async def _seed(maker, *, source, telegram_id=555, status=TicketStatus.CLOSED.value, ticket_id=1, user_id=10):
    async with maker() as db:
        if await db.get(User, user_id) is None:
            db.add(User(id=user_id, telegram_id=telegram_id, first_name='Иван', language='ru'))
        db.add(Ticket(id=ticket_id, user_id=user_id, title='Не работает VPN', status=status, source=source))
        await db.commit()


async def _ticket(maker, ticket_id=1) -> Ticket:
    async with maker() as db:
        return (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()


# ============ где начат тикет ============


def test_effective_source_prefers_stored_value():
    assert csat.effective_source(Ticket(source='cabinet', user=User(telegram_id=1))) == 'cabinet'
    assert csat.effective_source(Ticket(source='bot', user=User(telegram_id=None))) == 'bot'


def test_effective_source_for_old_tickets_follows_telegram():
    assert csat.effective_source(Ticket(source=None, user=User(telegram_id=1))) == 'bot'
    assert csat.effective_source(Ticket(source=None, user=User(telegram_id=None))) == 'cabinet'


def test_parse_callback_accepts_only_valid_ratings():
    assert csat.parse_callback('csat_rate_12_5') == (12, 5)
    assert csat.parse_callback('csat_rate_12_0') is None
    assert csat.parse_callback('csat_rate_12_6') is None
    assert csat.parse_callback('csat_rate_x_5') is None
    assert csat.parse_callback('close_ticket_12') is None


def test_rating_keyboard_has_five_buttons():
    keyboard = csat.build_rating_keyboard(7)
    assert [b.callback_data for b in keyboard.inline_keyboard[0]] == [f'csat_rate_7_{n}' for n in range(1, 6)]


# ============ запрос оценки ============


async def test_bot_ticket_gets_telegram_prompt_once(maker, bot, ws):
    await _seed(maker, source='bot')

    assert await csat.request_csat(1) is True
    assert await csat.request_csat(1) is False, 'второе закрытие не должно спамить'

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['chat_id'] == 555
    assert '#1' in kwargs['text']
    ws.send_to_user.assert_not_awaited()
    assert (await _ticket(maker)).csat_requested_at is not None


async def test_cabinet_ticket_is_asked_in_cabinet_only(maker, bot, ws):
    await _seed(maker, source='cabinet', telegram_id=555)

    assert await csat.request_csat(1) is True

    bot.send_message.assert_not_awaited()
    ws.send_to_user.assert_awaited_once_with(10, {'type': 'ticket.csat_requested', 'ticket_id': 1})


async def test_open_ticket_is_not_asked(maker, bot, ws):
    await _seed(maker, source='bot', status=TicketStatus.OPEN.value)

    assert await csat.request_csat(1) is False
    bot.send_message.assert_not_awaited()


async def test_disabled_setting_skips_everything(maker, bot, ws, monkeypatch):
    monkeypatch.setattr(csat.settings, 'TICKET_CSAT_ENABLED', False)
    await _seed(maker, source='bot')

    assert await csat.request_csat(1) is False
    assert (await _ticket(maker)).csat_requested_at is None


# ============ приём оценки ============


async def test_owner_rates_closed_ticket_once(maker, monkeypatch):
    after = AsyncMock()
    monkeypatch.setattr(csat, '_after_rating', after)
    await _seed(maker, source='bot')

    async with maker() as db:
        await csat.save_rating(db, ticket_id=1, user_id=10, rating=4)
        with pytest.raises(csat.CsatError) as again:
            await csat.save_rating(db, ticket_id=1, user_id=10, rating=5)
    await asyncio.sleep(0)

    assert again.value.code == 'already_rated'
    ticket = await _ticket(maker)
    assert (ticket.csat_rating, ticket.csat_rated_at is not None) == (4, True)
    after.assert_called_once_with(1)


@pytest.mark.parametrize(
    ('status', 'user_id', 'rating', 'code'),
    [
        (TicketStatus.CLOSED.value, 99, 5, 'not_found'),
        (TicketStatus.OPEN.value, 10, 5, 'not_closed'),
        (TicketStatus.CLOSED.value, 10, 6, 'invalid_rating'),
    ],
)
async def test_rating_is_refused(maker, monkeypatch, status, user_id, rating, code):
    monkeypatch.setattr(csat, '_after_rating', AsyncMock())
    await _seed(maker, source='bot', status=status)

    async with maker() as db:
        with pytest.raises(csat.CsatError) as refused:
            await csat.save_rating(db, ticket_id=1, user_id=user_id, rating=rating)

    assert refused.value.code == code
    assert (await _ticket(maker)).csat_rating is None


async def test_admins_get_the_rating(maker, monkeypatch, ws):
    await _seed(maker, source='cabinet')
    async with maker() as db:
        ticket = await db.get(Ticket, 1)
        ticket.csat_rating = 2
        await db.commit()
    notify = AsyncMock()
    monkeypatch.setattr(csat, '_notify_admins', notify)

    await csat._after_rating(1)

    assert notify.await_args.args[0].csat_rating == 2
    ws.send_to_admins.assert_awaited_once()


# ============ запуск по событию закрытия ============


async def test_close_event_triggers_request(monkeypatch):
    requested: list[int] = []

    async def fake_request(ticket_id: int) -> bool:
        requested.append(ticket_id)
        return True

    monkeypatch.setattr(csat, 'request_csat', fake_request)

    csat._on_status_changed({'payload': {'ticket_id': 7, 'new_status': 'closed'}})
    csat._on_status_changed({'payload': {'ticket_id': 8, 'new_status': 'answered'}})
    csat._on_status_changed({'payload': {'ticket_id': 'oops', 'new_status': 'closed'}})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert requested == [7]
