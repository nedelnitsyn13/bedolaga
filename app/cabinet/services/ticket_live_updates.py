"""Живое обновление тикетов в кабинете.

Страницы тикетов в кабинете обновлялись только после действий самого зрителя:
ответ пользователя из Telegram (или админа из бота) появлялся у второй стороны
лишь после перезагрузки страницы. Прямые WebSocket-уведомления кабинет слал
только для сообщений, написанных в самом кабинете.

Здесь общая шина событий тикетов (``ticket.created`` / ``ticket.message_added`` /
``ticket.status_changed``) — её шлют все пути: бот, кабинет, Web API — переводится
в тихое событие ``ticket.updated`` для админов и автора тикета. Кабинет по нему
перечитывает тикет без тостов: тосты остаются за прежними событиями.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import select

from app.cabinet.ws_manager import cabinet_ws_manager
from app.database.database import AsyncSessionLocal
from app.database.models import Ticket


logger = structlog.get_logger(__name__)

EVENT_TYPE = 'ticket.updated'
_SOURCE_EVENTS = ('ticket.created', 'ticket.message_added', 'ticket.status_changed')

_tasks: set[asyncio.Task] = set()
_registered = False


async def _owner_id(ticket_id: int) -> int | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Ticket.user_id).where(Ticket.id == ticket_id))).scalar_one_or_none()


async def publish_ticket_update(payload: dict[str, Any]) -> None:
    try:
        ticket_id = int(payload.get('ticket_id'))
    except (TypeError, ValueError):
        return

    status = payload.get('status') or payload.get('new_status')
    message = {'type': EVENT_TYPE, 'ticket_id': ticket_id, 'status': status}

    # В message_added поле user_id — автор сообщения (у ответа поддержки это админ),
    # поэтому владельца тикета берём из базы, а не из события.
    owner_id = await _owner_id(ticket_id)

    await cabinet_ws_manager.send_to_admins(message)
    if owner_id is not None:
        await cabinet_ws_manager.send_to_user(owner_id, message)


async def _guarded(payload: dict[str, Any]) -> None:
    try:
        await publish_ticket_update(payload)
    except Exception as e:  # никогда не роняем шину событий
        logger.debug('Живое обновление тикета не отправлено', error=str(e)[:200])


def _listener(event_data: dict[str, Any]) -> None:
    payload = event_data.get('payload') if isinstance(event_data, dict) else None
    if not isinstance(payload, dict):
        return
    task = asyncio.create_task(_guarded(payload))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def register_ticket_live_updates() -> None:
    """Подписаться на события тикетов (идемпотентно)."""
    global _registered
    if _registered:
        return
    from app.services.event_emitter import event_emitter

    for event_type in _SOURCE_EVENTS:
        event_emitter.on(event_type, _listener)
    _registered = True
