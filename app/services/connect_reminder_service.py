"""Напоминание «подключитесь» тем, кто взял триал и ни разу не подключился.

Автоматически это делает панель: событие ``user.not_connected`` через N часов
после выдачи. Но оно приходит один раз и только если включено в панели, а
оператор, листающий триальщиков, хочет напомнить сам — одному человеку из
карточки или всем таким сразу из списка.

«Ни разу не подключался» знает только панель (``userTraffic.firstConnectedAt``),
в базе бота этого нет. Поэтому множество таких аккаунтов берётся обходом панели и
кэшируется на минуту: список и кнопка «напомнить всем» открываются подряд.

Факт отправки пишется в журнал действий админов: по нему карточка показывает
«уже напоминали», а массовая отправка пропускает тех, кому писали недавно.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.rbac import AuditLogCRUD
from app.database.crud.subscription_segments import LIVE_STATUSES, segment_condition
from app.database.models import AdminAuditLog, Subscription, User, UserStatus
from app.localization.texts import get_texts
from app.utils.miniapp_buttons import build_miniapp_or_callback_button


logger = structlog.get_logger(__name__)

AUDIT_ACTION = 'user.remind_connect'
#: Массовая отправка пропускает тех, кому напоминали за это время; карточка — предупреждает.
REMIND_COOLDOWN = timedelta(hours=24)
#: Потолок одной массовой отправки: больше за раз — это уже рассылка, а не напоминание.
BULK_LIMIT = 2000
#: Telegram терпит ~30 сообщений в секунду; с запасом.
_BULK_DELAY_SECONDS = 0.05
_CACHE_TTL_SECONDS = 60.0

SendResult = Literal['sent', 'forbidden', 'failed']


# --- Кто не подключался (по панели) ---------------------------------------------------


@dataclass
class _Cache:
    stored_at: float = float('-inf')
    ids: frozenset[int] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_cache = _Cache()


async def _fetch_never_connected() -> frozenset[int] | None:
    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    if not service.is_configured:
        return None
    async with service.get_api_client() as api:
        users = await api.get_all_users_stream(size=500)
    return frozenset(user.id for user in users if user.first_connected_at is None and user.id is not None)


async def get_never_connected_panel_ids() -> frozenset[int] | None:
    """id аккаунтов панели, ни разу не подключавшихся; ``None`` — панель не ответила («не знаем»)."""
    async with _cache.lock:
        if time.monotonic() - _cache.stored_at < _CACHE_TTL_SECONDS:
            return _cache.ids
        try:
            ids = await _fetch_never_connected()
        except Exception as error:
            logger.warning('Не удалось получить из панели неподключавшихся', error=str(error)[:200])
            ids = None
        _cache.stored_at = time.monotonic()
        _cache.ids = ids
        return ids


def never_connected_trial_condition(panel_ids: frozenset[int], now: datetime):
    """У пользователя есть живой триал, чей аккаунт в панели ни разу не подключался.

    Аккаунт триала — ``Subscription.remnawave_id``; у старых одиночных подписок его
    нет, тогда — ``User.remnawave_id``. Telegram ID не годится: им совпадает и
    другой, подключавшийся аккаунт того же человека.
    """
    if not panel_ids:
        return User.id.is_(None)  # никого
    return User.id.in_(
        select(Subscription.user_id)
        .join(User, User.id == Subscription.user_id)
        .where(
            segment_condition('trial', now),
            or_(
                Subscription.remnawave_id.in_(panel_ids),
                and_(Subscription.remnawave_id.is_(None), User.remnawave_id.in_(panel_ids)),
            ),
        )
        .correlate(None)
        .scalar_subquery()
    )


def is_never_connected_trial(user: User, panel_ids: frozenset[int], now: datetime) -> bool:
    """То же условие для уже загруженного пользователя (карточка)."""
    for sub in user.subscriptions or ():
        end = sub.end_date if sub.end_date is None or sub.end_date.tzinfo else sub.end_date.replace(tzinfo=UTC)
        # Как segment_condition('trial'): живой статус и срок не вышел.
        if not sub.is_trial or sub.status not in LIVE_STATUSES or end is None or end <= now:
            continue
        panel_id = sub.remnawave_id if sub.remnawave_id is not None else user.remnawave_id
        if panel_id in panel_ids:
            return True
    return False


# --- Текст и кнопка -------------------------------------------------------------------


def default_text(user: User) -> str:
    """Тот же текст, что у автонапоминания панели, на языке человека."""
    message = get_texts(user.language).get('WEBHOOK_USER_NOT_CONNECTED') or ''
    try:
        return message.format(tariff_label='', hours='')
    except (KeyError, IndexError):
        return message


def connect_keyboard(user: User) -> InlineKeyboardMarkup:
    button_text = get_texts(user.language).get('CONNECT_BUTTON', 'Connect')
    return InlineKeyboardMarkup(
        inline_keyboard=[[build_miniapp_or_callback_button(text=button_text, callback_data='subscription_connect')]]
    )


# --- Журнал ---------------------------------------------------------------------------


async def last_reminded_at(db: AsyncSession, user_ids: list[int]) -> dict[int, datetime]:
    if not user_ids:
        return {}
    rows = await db.execute(
        select(AdminAuditLog.resource_id, func.max(AdminAuditLog.created_at))
        .where(
            AdminAuditLog.action == AUDIT_ACTION,
            AdminAuditLog.status == 'success',
            AdminAuditLog.resource_type == 'user',
            AdminAuditLog.resource_id.in_([str(user_id) for user_id in user_ids]),
        )
        .group_by(AdminAuditLog.resource_id)
    )
    return {int(resource_id): moment for resource_id, moment in rows.all() if moment is not None}


# --- Отправка -------------------------------------------------------------------------


async def send_reminder(
    bot: Bot, db: AsyncSession, *, admin_id: int, user: User, text: str, bulk: bool = False
) -> SendResult:
    """Отправить напоминание и записать результат в журнал. Не бросает на ошибках Telegram."""
    try:
        await bot.send_message(user.telegram_id, text, parse_mode='HTML', reply_markup=connect_keyboard(user))
        result: SendResult = 'sent'
    except TelegramForbiddenError:
        result = 'forbidden'
    except TelegramBadRequest as error:
        logger.warning('Напоминание о подключении отклонено Telegram', user_id=user.id, error=str(error)[:200])
        result = 'failed'
    except Exception as error:
        logger.warning('Напоминание о подключении не отправлено', user_id=user.id, error=str(error)[:200])
        result = 'failed'

    await AuditLogCRUD.create(
        db,
        user_id=admin_id,
        action=AUDIT_ACTION,
        resource_type='user',
        resource_id=str(user.id),
        details={'result': result, 'bulk': bulk},
        status='success' if result == 'sent' else 'failure',
    )
    return result


async def bulk_audience(
    db: AsyncSession, panel_ids: frozenset[int], *, now: datetime | None = None
) -> tuple[list[User], int]:
    """Кому уйдёт массовое напоминание и скольких пропустим, потому что напоминали недавно.

    Отбор: живой триал без подключения, есть Telegram, аккаунт не заблокирован.
    """
    now = now or datetime.now(UTC)
    users = (
        (
            await db.execute(
                select(User)
                .options(selectinload(User.subscriptions))
                .where(
                    never_connected_trial_condition(panel_ids, now),
                    User.telegram_id.is_not(None),
                    User.status == UserStatus.ACTIVE.value,
                )
                .order_by(User.id)
                .limit(BULK_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    reminded = await last_reminded_at(db, [user.id for user in users])
    fresh = [user for user in users if user.id not in reminded or now - reminded[user.id] >= REMIND_COOLDOWN]
    return fresh, len(users) - len(fresh)


async def send_bulk(user_ids: list[int], admin_id: int) -> dict[str, int]:
    """Фоновая массовая отправка: своя сессия, пауза между сообщениями, итог в лог."""
    from app.bot_factory import create_bot
    from app.database.database import AsyncSessionLocal

    counters = {'sent': 0, 'forbidden': 0, 'failed': 0}
    async with AsyncSessionLocal() as db, create_bot() as bot:
        users = (await db.execute(select(User).where(User.id.in_(user_ids)).order_by(User.id))).scalars().all()
        for user in users:
            result = await send_reminder(bot, db, admin_id=admin_id, user=user, text=default_text(user), bulk=True)
            counters[result] += 1
            await db.commit()
            await asyncio.sleep(_BULK_DELAY_SECONDS)
    logger.info('Массовое напоминание о подключении завершено', admin_id=admin_id, **counters)
    return counters
