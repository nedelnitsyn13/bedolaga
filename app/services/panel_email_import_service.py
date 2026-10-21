"""Перенос почты из панели Remnawave в Telegram-аккаунты бота.

Прежний бот записывал почту клиента в панель рядом с его Telegram ID. Новый бот
при синхронизации находит такого клиента по Telegram и почту не берёт. Когда
Telegram недоступен, клиент входит в кабинет по почте, ничего не находит и
получает новый пустой аккаунт — без своей подписки.

Здесь почта из панели становится почтой Telegram-аккаунта, если у аккаунта её
ещё нет и ящик не занят никем другим (с учётом алиасов вроде ``user+1@gmail``).
Адрес считается подтверждённым: войти по нему всё равно можно только через
«Забыли пароль», а письмо со ссылкой и есть доказательство владения ящиком.
Код подтверждения при первом входе ничего бы к этому не добавил.

Чтобы владелец Telegram-аккаунта узнал о привязке (и заметил чужой адрес),
бот пишет ему, какая почта привязана и как войти по ней в кабинет.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_email_alias
from app.database.models import User, UserStatus
from app.utils.telegram_delivery import is_user_unreachable


logger = structlog.get_logger(__name__)

_NOTIFY_PAUSE_SECONDS = 0.05

_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class ImportedEmail:
    user_id: int
    telegram_id: int
    email: str
    language: str


def _normalize(email: Any) -> str | None:
    value = str(email or '').strip().lower()
    if not value or '@' not in value or value.startswith('@') or value.endswith('@'):
        return None
    return value


def collect_panel_emails(panel_users: Iterable[Mapping[str, Any]]) -> dict[int, str]:
    """Telegram ID → почта из панели. Почта на нескольких Telegram ID не берётся вовсе:
    чья она — неизвестно."""
    by_tg: dict[int, str] = {}
    owners: dict[str, set[int]] = {}
    for item in panel_users:
        telegram_id = item.get('telegramId')
        email = _normalize(item.get('email'))
        if telegram_id is None or email is None:
            continue
        try:
            telegram_id = int(telegram_id)
        except (TypeError, ValueError):
            continue
        by_tg[telegram_id] = email
        owners.setdefault(email, set()).add(telegram_id)
    ambiguous = {email for email, ids in owners.items() if len(ids) > 1}
    return {tg: email for tg, email in by_tg.items() if email not in ambiguous}


async def import_panel_emails(db: AsyncSession, panel_users: Iterable[Mapping[str, Any]]) -> list[ImportedEmail]:
    """Записать почту из панели в Telegram-аккаунты без почты. Коммитит сам."""
    if not settings.PANEL_EMAIL_IMPORT_ENABLED:
        return []

    emails_by_tg = collect_panel_emails(panel_users)
    if not emails_by_tg:
        return []

    admin_emails = {e.strip().lower() for e in settings.get_admin_emails()}
    result = await db.execute(
        select(User).where(
            User.telegram_id.in_(list(emails_by_tg)),
            User.email.is_(None),
            User.status != UserStatus.DELETED.value,
        )
    )
    candidates = result.scalars().all()

    imported: list[ImportedEmail] = []
    now = datetime.now(UTC)
    for user in candidates:
        email = emails_by_tg[user.telegram_id]
        # Админский адрес даёт права суперадмина — его из панели не берём никогда.
        if email in admin_emails:
            continue
        taken = await db.execute(select(User.id).where(func.lower(User.email) == email).limit(1))
        if taken.scalar_one_or_none() is not None:
            continue
        if await get_user_by_email_alias(db, email, exclude_user_id=user.id):
            continue

        user.email = email
        user.email_verified = True
        user.email_verified_at = now
        imported.append(
            ImportedEmail(
                user_id=user.id,
                telegram_id=user.telegram_id,
                email=email,
                language=user.language or 'ru',
            )
        )

    if imported:
        await db.commit()
        logger.info('Почта из панели перенесена в Telegram-аккаунты', count=len(imported))
    return imported


def _get_bot():
    from app.services.maintenance_service import maintenance_service

    return getattr(maintenance_service, '_bot', None)


async def notify_imported(imported: list[ImportedEmail]) -> int:
    """Сообщить в Telegram о привязанной почте. Возвращает число доставленных."""
    bot = _get_bot()
    if bot is None or not imported:
        return 0

    from app.localization.texts import get_texts

    cabinet_url = settings._normalized_cabinet_url() or ''
    delivered = 0
    for item in imported:
        texts = get_texts(item.language)
        text = texts.t(
            'PANEL_EMAIL_LINKED',
            '📧 К вашему аккаунту привязана почта <b>{email}</b>.\n\n'
            'Если Telegram будет недоступен, войдите в личный кабинет {url} по этой почте: '
            'нажмите «Забыли пароль» и задайте пароль по ссылке из письма.\n\n'
            'Если почта не ваша — напишите в поддержку.',
        ).format(email=html.escape(item.email), url=cabinet_url)
        try:
            await bot.send_message(chat_id=item.telegram_id, text=text)
            delivered += 1
        except Exception as error:
            if not is_user_unreachable(error):
                logger.warning('Не удалось сообщить о привязанной почте', user_id=item.user_id, error=str(error)[:200])
        await asyncio.sleep(_NOTIFY_PAUSE_SECONDS)
    return delivered


async def import_and_notify(db: AsyncSession, panel_users: Iterable[Mapping[str, Any]]) -> int:
    """Перенос после синхронизации; уведомления уходят в фоне. Ошибка не роняет синхронизацию."""
    try:
        imported = await import_panel_emails(db, panel_users)
    except Exception as error:
        logger.error('Перенос почты из панели не удался', error=str(error)[:300])
        await db.rollback()
        return 0
    if imported:
        task = asyncio.create_task(notify_imported(imported))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return len(imported)
