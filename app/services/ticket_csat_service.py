"""Опрос удовлетворённости (CSAT) после закрытия тикета.

Спрашиваем один раз на тикет и только там, где тикет начат:
- в боте — сообщением с кнопками 1–5;
- в кабинете — блоком оценки в карточке закрытого тикета (кабинет узнаёт
  о запросе по WebSocket и из ответа API).

Запрос запускается событием ``ticket.status_changed`` → ``closed``: его шлют все
пути закрытия (бот, кабинет, Web API). Массовое «закрыть все открытые» события
не шлёт — сотни опросов за раз по старым тикетам были бы спамом.

Полученная оценка уходит админам в тикет-топик и видна в карточке тикета.
"""

from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Ticket, TicketStatus


logger = structlog.get_logger(__name__)

SOURCE_BOT = 'bot'
SOURCE_CABINET = 'cabinet'

MIN_RATING = 1
MAX_RATING = 5

CALLBACK_PREFIX = 'csat_rate_'

_tasks: set[asyncio.Task] = set()
_registered = False


class CsatError(Exception):
    """Оценку принять нельзя; ``code`` — машинная причина для API."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def effective_source(ticket: Ticket) -> str:
    """Где начат тикет. У старых тикетов поля нет — судим по наличию Telegram."""
    if ticket.source in (SOURCE_BOT, SOURCE_CABINET):
        return ticket.source
    user = getattr(ticket, 'user', None)
    return SOURCE_BOT if user is not None and user.telegram_id else SOURCE_CABINET


def stars(rating: int) -> str:
    return '⭐' * max(MIN_RATING, min(MAX_RATING, rating))


def build_rating_keyboard(ticket_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=f'{value}⭐', callback_data=f'{CALLBACK_PREFIX}{ticket_id}_{value}')
                for value in range(MIN_RATING, MAX_RATING + 1)
            ]
        ]
    )


def parse_callback(data: str | None) -> tuple[int, int] | None:
    """``csat_rate_<ticket>_<rating>`` → (ticket_id, rating); мусор → None."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    parts = data[len(CALLBACK_PREFIX) :].split('_')
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    ticket_id, rating = int(parts[0]), int(parts[1])
    if not MIN_RATING <= rating <= MAX_RATING:
        return None
    return ticket_id, rating


async def _load_ticket(db: AsyncSession, ticket_id: int) -> Ticket | None:
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.user)))
    return result.scalar_one_or_none()


def _get_bot():
    from app.services.maintenance_service import maintenance_service

    return getattr(maintenance_service, '_bot', None)


# ---------------------------------------------------------------------------
# Запрос оценки
# ---------------------------------------------------------------------------


async def request_csat(ticket_id: int) -> bool:
    """Попросить оценку у автора закрытого тикета. True — запрос отправлен."""
    if not settings.TICKET_CSAT_ENABLED:
        return False

    async with AsyncSessionLocal() as db:
        ticket = await _load_ticket(db, ticket_id)
        if ticket is None or ticket.status != TicketStatus.CLOSED.value:
            return False
        if ticket.csat_requested_at is not None or ticket.csat_rating is not None:
            return False  # один раз на тикет: повторное закрытие не спамит

        source = effective_source(ticket)
        user = ticket.user
        ticket.csat_requested_at = datetime.now(UTC)
        await db.commit()

        if source == SOURCE_BOT:
            if user is None or not user.telegram_id:
                return False
            return await _send_bot_prompt(ticket.id, user.telegram_id, user.language or 'ru')

        # Кабинет: блок оценки покажется в карточке тикета; открытой странице
        # сообщаем сразу, чтобы блок появился без обновления.
        try:
            from app.cabinet.ws_manager import cabinet_ws_manager

            await cabinet_ws_manager.send_to_user(
                ticket.user_id, {'type': 'ticket.csat_requested', 'ticket_id': ticket.id}
            )
        except Exception as e:
            logger.debug('CSAT: кабинет не уведомлён', ticket_id=ticket.id, error=str(e)[:200])
        return True


async def _send_bot_prompt(ticket_id: int, telegram_id: int, language: str) -> bool:
    from app.localization.texts import get_texts

    bot = _get_bot()
    if bot is None:
        logger.warning('CSAT: бот недоступен, опрос не отправлен', ticket_id=ticket_id)
        return False
    texts = get_texts(language)
    text = texts.t(
        'TICKET_CSAT_PROMPT',
        '✅ Обращение #{ticket_id} закрыто.\n\nОцените, пожалуйста, работу поддержки:',
    ).format(ticket_id=ticket_id)
    try:
        await bot.send_message(chat_id=telegram_id, text=text, reply_markup=build_rating_keyboard(ticket_id))
        return True
    except Exception as e:
        # Пользователь заблокировал бота и т.п. — не ошибка поддержки.
        logger.info('CSAT: опрос в Telegram не доставлен', ticket_id=ticket_id, error=str(e)[:200])
        return False


# ---------------------------------------------------------------------------
# Приём оценки
# ---------------------------------------------------------------------------


async def save_rating(db: AsyncSession, *, ticket_id: int, user_id: int, rating: int) -> Ticket:
    """Сохранить оценку автора тикета. Бросает CsatError с причиной."""
    if not MIN_RATING <= rating <= MAX_RATING:
        raise CsatError('invalid_rating')
    ticket = await _load_ticket(db, ticket_id)
    if ticket is None or ticket.user_id != user_id:
        raise CsatError('not_found')
    if ticket.status != TicketStatus.CLOSED.value:
        raise CsatError('not_closed')
    if ticket.csat_rating is not None:
        raise CsatError('already_rated')

    ticket.csat_rating = rating
    ticket.csat_rated_at = datetime.now(UTC)
    await db.commit()

    _spawn(_after_rating(ticket.id))
    return ticket


async def _after_rating(ticket_id: int) -> None:
    async with AsyncSessionLocal() as db:
        ticket = await _load_ticket(db, ticket_id)
        if ticket is None or ticket.csat_rating is None:
            return
        await _notify_admins(ticket)
    try:
        from app.cabinet.ws_manager import cabinet_ws_manager

        await cabinet_ws_manager.send_to_admins(
            {'type': 'ticket.updated', 'ticket_id': ticket_id, 'status': TicketStatus.CLOSED.value}
        )
    except Exception as e:
        logger.debug('CSAT: админы в кабинете не уведомлены', error=str(e)[:200])


async def _notify_admins(ticket: Ticket) -> None:
    if not settings.is_admin_notifications_enabled():
        return
    bot = _get_bot()
    if bot is None:
        return
    from app.services.admin_notification_service import AdminNotificationService

    user = ticket.user
    name = html.escape(user.full_name or '') if user else '—'
    contact = (user.telegram_id or user.email or f'#{user.id}') if user else '—'
    title = html.escape((ticket.title or '').strip()[:60]) or '—'
    where = 'бот' if effective_source(ticket) == SOURCE_BOT else 'кабинет'
    text = (
        f'{stars(ticket.csat_rating)} <b>ОЦЕНКА ПОДДЕРЖКИ: {ticket.csat_rating}/5</b>\n\n'
        f'🆔 <b>Тикет:</b> <code>{ticket.id}</code> ({where})\n'
        f'📝 <b>Заголовок:</b> {title}\n'
        f'👤 <b>Пользователь:</b> {name}\n'
        f'🆔 <b>ID:</b> <code>{contact}</code>\n'
    )
    try:
        await AdminNotificationService(bot).send_ticket_event_notification(text)
    except Exception as e:
        logger.warning('CSAT: уведомление админам не отправлено', ticket_id=ticket.id, error=str(e)[:200])


# ---------------------------------------------------------------------------
# Подписка на события
# ---------------------------------------------------------------------------


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _guarded_request(ticket_id: int) -> None:
    try:
        await request_csat(ticket_id)
    except Exception as e:  # слушатель не должен ронять шину событий
        logger.warning('CSAT: запрос оценки не удался', ticket_id=ticket_id, error=str(e)[:200])


def _on_status_changed(event_data: dict[str, Any]) -> None:
    payload = event_data.get('payload') if isinstance(event_data, dict) else None
    if not isinstance(payload, dict) or payload.get('new_status') != TicketStatus.CLOSED.value:
        return
    try:
        ticket_id = int(payload.get('ticket_id'))
    except (TypeError, ValueError):
        return
    _spawn(_guarded_request(ticket_id))


def register_ticket_csat_listener() -> None:
    """Подписать опрос на закрытие тикетов (идемпотентно)."""
    global _registered
    if _registered:
        return
    from app.services.event_emitter import event_emitter

    event_emitter.on('ticket.status_changed', _on_status_changed)
    _registered = True
