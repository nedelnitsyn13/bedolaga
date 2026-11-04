"""Часы работы поддержки и ожидаемое время ответа.

Поддержка отвечает с 10 до 22 по Москве, но тикет можно оставить всегда. Человек
должен видеть, когда ему ответят («примерно через 20 мин» днём, «после 10:00»
ночью), время ответа считается по реальной статистике, а SLA не будит админов
ночью и не считает ночные часы просрочкой.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.database.models import Base, Ticket, TicketMessage, User
from app.localization.texts import get_texts
from app.services import support_hours
from app.services.support_hours import (
    SupportHours,
    add_working_minutes,
    eta_line,
    is_open,
    next_open,
    round_estimate,
    timezone_label,
    typical_reply_minutes,
    working_minutes_between,
)
from tests.fixtures.sqlite_memory import memory_session


MSK = ZoneInfo('Europe/Moscow')
HOURS = SupportHours(enabled=True, start=10, end=22, tz=MSK)


def msk(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MSK)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(support_hours, '_cache', support_hours._Cache())


def test_open_and_closed():
    assert is_open(HOURS, msk(2, 10))
    assert is_open(HOURS, msk(2, 21, 59))
    assert not is_open(HOURS, msk(2, 22))
    assert not is_open(HOURS, msk(2, 3))
    assert next_open(HOURS, msk(2, 23, 40)) == msk(3, 10)
    assert next_open(HOURS, msk(3, 3)) == msk(3, 10)
    assert next_open(HOURS, msk(3, 15)) == msk(3, 15)


def test_disabled_hours_mean_always_open_and_wall_clock():
    off = SupportHours(enabled=False, start=10, end=22, tz=MSK)
    assert is_open(off, msk(2, 3))
    assert working_minutes_between(off, msk(2, 21), msk(2, 23)) == 120
    assert add_working_minutes(off, msk(2, 23), 30) == msk(2, 23, 30)


def test_night_does_not_count_as_waiting():
    # Тикет в 21:30, ответ в 10:15 — поддержка ждала 45 рабочих минут, а не 12 часов.
    assert working_minutes_between(HOURS, msk(2, 21, 30), msk(3, 10, 15)) == 45
    assert working_minutes_between(HOURS, msk(2, 23), msk(3, 9)) == 0


def test_expected_reply_moves_to_the_morning():
    assert add_working_minutes(HOURS, msk(2, 23, 40), 25) == msk(3, 10, 25)
    assert add_working_minutes(HOURS, msk(2, 21, 50), 30) == msk(3, 10, 20)
    assert add_working_minutes(HOURS, msk(2, 12), 30) == msk(2, 12, 30)


def test_overnight_shift_and_round_the_clock():
    night = SupportHours(enabled=True, start=20, end=4, tz=MSK)
    assert is_open(night, msk(2, 23)) and is_open(night, msk(3, 2))
    assert not is_open(night, msk(3, 12))
    always = SupportHours(enabled=True, start=10, end=10, tz=MSK)
    assert is_open(always, msk(2, 3))


def test_estimates_are_rounded_like_a_human_would():
    assert round_estimate(2) == 5
    assert round_estimate(23) == 25
    assert round_estimate(60) == 60
    assert round_estimate(100) == 90
    assert round_estimate(250) == 240


def test_timezone_label():
    assert timezone_label(MSK, 'ru') == 'МСК'
    assert timezone_label(MSK, 'en') == 'MSK'
    assert timezone_label(ZoneInfo('Asia/Yekaterinburg'), 'ru') == 'UTC+5'


def test_eta_texts_for_open_and_closed():
    texts = get_texts('ru')
    status = support_hours.SupportStatus(hours=HOURS, open_now=True, reply_minutes=25, opens_at=None)
    assert eta_line(texts, status, 'ru') == '⏱ Ответим примерно через ~25 мин.'
    closed = support_hours.SupportStatus(hours=HOURS, open_now=False, reply_minutes=90, opens_at=msk(3, 10))
    assert (
        eta_line(texts, closed, 'ru')
        == '⏱ Сейчас поддержка не работает. Ответим после 10:00 (МСК), обычно в течение ~1,5 ч.'
    )


TABLES = list(Base.metadata.sorted_tables)


async def _seed_replies(db, waits: list[tuple[datetime, datetime]]) -> None:
    db.add(User(id=1, telegram_id=10, first_name='U', language='ru', status='active', balance_kopeks=0))
    await db.flush()
    for number, (created, replied) in enumerate(waits, start=1):
        db.add(Ticket(id=number, user_id=1, title='t', status='answered', created_at=created, updated_at=replied))
        db.add(TicketMessage(ticket_id=number, user_id=1, message_text='q', is_from_admin=False, created_at=created))
        db.add(TicketMessage(ticket_id=number, user_id=1, message_text='a', is_from_admin=True, created_at=replied))
    await db.commit()


@pytest.mark.asyncio
async def test_typical_reply_is_the_median_in_working_minutes(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_START', 10)
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_END', 22)
    monkeypatch.setattr(support_hours, 'get_local_timezone', lambda: MSK)
    now = msk(5, 12)
    waits = [
        (msk(2, 12), msk(2, 12, 20)),  # 20
        (msk(2, 13), msk(2, 13, 30)),  # 30
        (msk(2, 21, 50), msk(3, 10, 30)),  # ночью: 40 рабочих, а не 12 часов
        (msk(3, 15), msk(3, 15, 10)),  # 10
        (msk(3, 16), msk(3, 17)),  # 60
    ]
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_replies(db, waits)
        assert await typical_reply_minutes(db, now=now) == 30


@pytest.mark.asyncio
async def test_too_little_data_falls_back_to_sla(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_SLA_MINUTES', 45)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_replies(db, [(msk(2, 12), msk(2, 12, 20))])
        assert await typical_reply_minutes(db, now=msk(5, 12)) == 45


def test_sla_checker_skips_night_and_counts_working_minutes(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_ENABLED', True)
    monkeypatch.setattr(support_hours, 'get_local_timezone', lambda: MSK)
    hours = support_hours.current_hours()
    # В 10:20 тикет, пришедший в 21:50, ждёт 30 рабочих минут — при SLA 60 ещё не просрочен.
    ticket = SimpleNamespace(updated_at=msk(2, 21, 50))
    assert working_minutes_between(hours, ticket.updated_at, msk(3, 10, 20)) == 30
    assert not is_open(hours, msk(3, 3))


def test_broken_settings_fall_back_to_defaults(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_START', 'утро')
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_END', 99)
    hours = support_hours.current_hours()
    assert (hours.start, hours.end) == (10, 22)


@pytest.mark.asyncio
async def test_cabinet_support_status_and_ticket_eta(monkeypatch):
    from app.cabinet.routes import tickets as routes

    monkeypatch.setattr(settings, 'SUPPORT_HOURS_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_START', 10)
    monkeypatch.setattr(settings, 'SUPPORT_HOURS_END', 22)
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_SLA_MINUTES', 30)
    monkeypatch.setattr(support_hours, 'get_local_timezone', lambda: MSK)
    async with memory_session(monkeypatch, TABLES) as db:
        status = await routes.get_support_status(user=SimpleNamespace(id=1), db=db)
        assert status.hours_enabled and (status.start_hour, status.end_hour) == (10, 22)
        assert status.timezone == 'Europe/Moscow'
        assert status.reply_minutes == 30

        waiting = SimpleNamespace(id=1, status='open', updated_at=msk(2, 23), created_at=msk(2, 23))
        assert await routes._expected_reply_at(db, waiting) == msk(3, 10, 30)
        answered = SimpleNamespace(id=2, status='answered', updated_at=msk(2, 12), created_at=msk(2, 12))
        assert await routes._expected_reply_at(db, answered) is None
