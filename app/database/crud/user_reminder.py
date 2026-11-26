"""Напоминания пользователям: выборки, состояние по человеку, статистика, аудитория."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Transaction, TransactionType, User, UserReminder, UserReminderSend, UserReminderState
from app.services.user_reminders.conditions import ReminderConditions, condition_clauses


CHANNELS_BOT = ('bot', 'both')
CHANNELS_CABINET = ('cabinet', 'both')
# Встроенные первыми: builtin_key IS NULL даёт False (0) у встроенных.
_ORDER = (UserReminder.builtin_key.is_(None), UserReminder.id)


async def list_reminders(db: AsyncSession) -> list[UserReminder]:
    return list((await db.execute(select(UserReminder).order_by(*_ORDER))).scalars())


async def list_active_reminders(db: AsyncSession, channels: tuple[str, ...]) -> list[UserReminder]:
    query = (
        select(UserReminder)
        .where(UserReminder.is_active.is_(True), UserReminder.channels.in_(channels))
        .order_by(*_ORDER)
    )
    return list((await db.execute(query)).scalars())


async def get_reminder(db: AsyncSession, reminder_id: int) -> UserReminder | None:
    return await db.get(UserReminder, reminder_id)


async def _find_state(db: AsyncSession, reminder_id: int, user_id: int) -> UserReminderState | None:
    query = select(UserReminderState).where(
        UserReminderState.reminder_id == reminder_id, UserReminderState.user_id == user_id
    )
    return (await db.execute(query)).scalar_one_or_none()


async def get_or_create_state(db: AsyncSession, reminder_id: int, user_id: int) -> UserReminderState | None:
    state = await _find_state(db, reminder_id, user_id)
    if state is not None:
        return state
    try:
        async with db.begin_nested():
            state = UserReminderState(reminder_id=reminder_id, user_id=user_id, sends_count=0)
            db.add(state)
            await db.flush((state,))
        return state
    except IntegrityError:
        # Либо параллельный запрос успел создать строку (пара уникальна) — берём её.
        # Либо это FOREIGN KEY: user/reminder удалены между select-кандидатом и этой
        # вставкой — тогда повторный поиск тоже вернёт None, и вызывающий сам решает,
        # что делать (savepoint уже откатился, внешняя транзакция жива).
        return await _find_state(db, reminder_id, user_id)


async def record_bot_attempt(db: AsyncSession, reminder_id: int, user_id: int, *, now: datetime, success: bool) -> None:
    state = await get_or_create_state(db, reminder_id, user_id)
    if state is None:
        # Пользователь/напоминание удалены между отбором кандидата и записью попытки —
        # писать некуда, но проход не должен падать целиком из-за одного кандидата.
        return
    state.last_sent_at = now
    if success:
        state.sends_count = (state.sends_count or 0) + 1
        state.last_success_at = now
        db.add(UserReminderSend(reminder_id=reminder_id, user_id=user_id, sent_at=now))
    await db.commit()


async def reminder_stats(db: AsyncSession) -> dict[int, dict]:
    query = select(
        UserReminderState.reminder_id,
        func.coalesce(func.sum(UserReminderState.sends_count), 0),
        func.count(UserReminderState.dismissed_at),
    ).group_by(UserReminderState.reminder_id)
    return {
        reminder_id: {'sent_total': int(sent), 'dismissed_total': int(dismissed)}
        for reminder_id, sent, dismissed in (await db.execute(query)).all()
    }


#: Сколько истории смотрим, считая выручку напоминаний.
REVENUE_PERIOD = timedelta(days=90)
#: Не деньги клиента: ручное начисление админом, оплата с баланса.
_NOT_REAL_MONEY = ('manual', 'balance')


@dataclass
class ReminderRevenue:
    revenue_kopeks: int = 0
    payments: int = 0
    users: int = 0


def attribution_window() -> timedelta:
    return timedelta(days=max(1, min(30, int(settings.USER_REMINDERS_ATTRIBUTION_DAYS or 3))))


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def reminder_revenue(db: AsyncSession, *, now: datetime | None = None) -> dict[int, ReminderRevenue]:
    """Выручка каждого напоминания за ``REVENUE_PERIOD``.

    Пополнение (настоящие деньги: не ручное начисление и не баланс) засчитывается
    последнему напоминанию, отправленному этому человеку не раньше чем за окно
    атрибуции до оплаты. Одно пополнение — одному напоминанию, без двойного счёта.
    """
    now = now or datetime.now(UTC)
    window = attribution_window()
    since = now - REVENUE_PERIOD - window

    sends_by_user: dict[int, list[tuple[datetime, int]]] = defaultdict(list)
    for reminder_id, user_id, sent_at in (
        await db.execute(
            select(UserReminderSend.reminder_id, UserReminderSend.user_id, UserReminderSend.sent_at).where(
                UserReminderSend.sent_at >= since
            )
        )
    ).all():
        sends_by_user[user_id].append((_aware(sent_at), reminder_id))
    if not sends_by_user:
        return {}
    for sends in sends_by_user.values():
        sends.sort()

    recipients = select(UserReminderSend.user_id).where(UserReminderSend.sent_at >= since).distinct()
    deposits = await db.execute(
        select(Transaction.user_id, Transaction.amount_kopeks, Transaction.created_at).where(
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed.is_(True),
            Transaction.payment_method.is_not(None),
            Transaction.payment_method.not_in(_NOT_REAL_MONEY),
            Transaction.amount_kopeks > 0,
            Transaction.created_at >= now - REVENUE_PERIOD,
            Transaction.user_id.in_(recipients),
        )
    )
    result: dict[int, ReminderRevenue] = defaultdict(ReminderRevenue)
    payers: dict[int, set[int]] = defaultdict(set)
    for user_id, amount, created_at in deposits.all():
        sends = sends_by_user.get(user_id)
        if not sends or created_at is None:
            continue
        paid_at = _aware(created_at)
        index = bisect_right(sends, (paid_at, float('inf'))) - 1
        if index < 0:
            continue
        sent_at, reminder_id = sends[index]
        if paid_at - sent_at > window:
            continue
        entry = result[reminder_id]
        entry.revenue_kopeks += int(amount or 0)
        entry.payments += 1
        payers[reminder_id].add(user_id)
    for reminder_id, users in payers.items():
        result[reminder_id].users = len(users)
    return dict(result)


async def count_audience(
    db: AsyncSession,
    conditions: ReminderConditions,
    *,
    now: datetime,
    telegram_only: bool,
    exclude_promo_opt_out: bool = False,
    never_connected_ids: frozenset[int] | None = None,
) -> int:
    clauses = condition_clauses(conditions, now=now, never_connected_ids=never_connected_ids)
    if telegram_only:
        clauses.append(User.telegram_id.is_not(None))
    if exclude_promo_opt_out:
        # notification_settings — JSONB (JSON на SQLite в тестах); ключа может не быть
        # вовсе (старые пользователи) или всего поля — тогда решает
        # PROMO_OFFERS_DEFAULT_ENABLED, как и при самой отправке.
        promo_flag = User.notification_settings['promo_offers_enabled'].as_boolean()
        if settings.PROMO_OFFERS_DEFAULT_ENABLED:
            clauses.append(or_(User.notification_settings.is_(None), promo_flag.is_(None), promo_flag.is_(True)))
        else:
            clauses.append(promo_flag.is_(True))
    return int((await db.execute(select(func.count(User.id)).where(*clauses))).scalar() or 0)
