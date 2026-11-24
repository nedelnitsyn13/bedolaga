"""Карточки напоминаний в кабинете: что показать человеку и закрытие."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user_reminder import CHANNELS_CABINET, get_or_create_state, get_reminder, list_active_reminders
from app.database.models import Subscription, UserReminderState
from app.services.user_reminders.abandoned import has_abandoned_payment
from app.services.user_reminders.conditions import matches, needs_panel, parse_conditions
from app.services.user_reminders.texts import render_card, validate_texts


logger = structlog.get_logger(__name__)


async def active_cards_for_user(db: AsyncSession, user, *, lang: str | None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    reminders = await list_active_reminders(db, CHANNELS_CABINET)
    if not reminders:
        return []
    dismissed: dict[int, datetime] = dict(
        (
            await db.execute(
                select(UserReminderState.reminder_id, UserReminderState.dismissed_at).where(
                    UserReminderState.user_id == user.id, UserReminderState.dismissed_at.is_not(None)
                )
            )
        ).all()
    )
    subscriptions = list((await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars())
    panel_ids: frozenset[int] | None = None
    panel_asked = False
    cards: list[dict] = []
    for reminder in reminders:
        try:
            conditions = parse_conditions(reminder.conditions)
        except ValidationError as error:
            logger.warning('Напоминание с битыми условиями пропущено', reminder_id=reminder.id, error=str(error))
            continue
        per_payment = conditions.abandoned_payment_hours is not None
        # Закрытая карточка скрыта навсегда, а у «брошенной оплаты» — до следующего
        # брошенного платежа: о новом счёте человек должен узнать.
        if reminder.id in dismissed and not per_payment:
            continue
        try:
            validate_texts(reminder.texts)
        except ValueError as error:
            logger.warning('Напоминание с битыми текстами пропущено', reminder_id=reminder.id, error=str(error))
            continue
        if needs_panel(conditions) and not panel_asked:
            from app.services.connect_reminder_service import get_never_connected_panel_ids

            panel_ids = await get_never_connected_panel_ids()
            panel_asked = True
        abandoned: bool | None = None
        if per_payment:
            abandoned = await has_abandoned_payment(
                db, user.id, conditions.abandoned_payment_hours, now=now, created_after=dismissed.get(reminder.id)
            )
        if matches(
            user, subscriptions, conditions, now=now, never_connected_ids=panel_ids, abandoned_payment=abandoned
        ):
            cards.append(render_card(reminder, lang))
    return cards


async def dismiss_reminder(db: AsyncSession, user, reminder_id: int, *, now: datetime | None = None) -> bool:
    reminder = await get_reminder(db, reminder_id)
    if reminder is None or not reminder.is_active or reminder.channels not in CHANNELS_CABINET:
        return False
    state = await get_or_create_state(db, reminder.id, user.id)
    if state is None:
        # Пользователь/напоминание удалены между проверкой и вставкой — закрывать нечего.
        return False
    # Время каждого закрытия, а не первого: «брошенная оплата» по нему решает, что
    # карточку пора показать снова — только для платежа новее закрытия.
    state.dismissed_at = now or datetime.now(UTC)
    await db.commit()
    return True
