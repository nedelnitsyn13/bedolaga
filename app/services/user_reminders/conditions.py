"""Условия напоминаний: схема и вычислитель с двумя входами.

Бот отбирает кандидатов одним SQL-запросом (``condition_clauses``), кабинет проверяет
одного человека (``matches``). Правила одни; расхождение ловит тест с общим набором
сценариев (tests/services/user_reminders/test_conditions.py).

Условие «ни разу не подключался» знает только панель (``firstConnectedAt``), в базе
бота его нет. Поэтому оба входа принимают множество неподключавшихся аккаунтов панели
(``never_connected_ids``) — его добывает вызывающий, один раз на проход. ``None`` —
панель не ответила: такое условие тогда не выполняется ни для кого, напоминание
молчит, а не пишет подключившимся.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ColumnElement, String, and_, case, exists, false, func, literal, not_, or_

from app.database.auth_methods import OAUTH_PROVIDER_COLUMNS, compute_auth_methods
from app.database.constants import ALIVE_SUBSCRIPTION_STATUSES
from app.database.models import Subscription, SubscriptionStatus, User, UserStatus


# Та же граница, что у сегмента low_balance рассылок (app/handlers/admin/messages.py).
LOW_BALANCE_THRESHOLD_KOPEKS = 10_000
_EXCLUDED_STATUSES = (UserStatus.DELETED.value, UserStatus.BLOCKED.value)
_ALIVE = tuple(ALIVE_SUBSCRIPTION_STATUSES)

AuthCondition = Literal['telegram_only', 'email_only', 'single_method']
SubscriptionSegment = Literal['active', 'trial', 'expiring', 'expired', 'none', 'low_balance', 'tariff']


class SubscriptionCondition(BaseModel):
    model_config = ConfigDict(extra='forbid')

    segment: SubscriptionSegment
    days: int | None = Field(None, ge=1, le=365)
    tariff_id: int | None = Field(None, ge=1)

    @model_validator(mode='after')
    def _required_params(self) -> SubscriptionCondition:
        if self.segment == 'expiring' and self.days is None:
            raise ValueError('days is required for the expiring segment')
        if self.segment == 'tariff' and self.tariff_id is None:
            raise ValueError('tariff_id is required for the tariff segment')
        return self


class ReminderConditions(BaseModel):
    """Все заданные пункты — через «И»; незаданный не проверяется."""

    model_config = ConfigDict(extra='forbid')

    auth: AuthCondition | None = None
    subscription: SubscriptionCondition | None = None
    registered_days_min: int | None = Field(None, ge=0, le=3650)
    inactive_days_min: int | None = Field(None, ge=0, le=3650)
    # Живая подписка, чей аккаунт в панели ни разу не подключался к VPN.
    never_connected: bool | None = None


def parse_conditions(raw: dict | None) -> ReminderConditions:
    return ReminderConditions.model_validate(raw or {})


def needs_panel(conditions: ReminderConditions) -> bool:
    """Нужно ли спрашивать панель, чтобы проверить эти условия."""
    return bool(conditions.never_connected)


# --- SQL -------------------------------------------------------------------------------


def _filled(column) -> ColumnElement[bool]:
    """Как truthiness в compute_auth_methods: пустая строка (или 0) — не способ входа.

    Сравнение с '' — только для строковых колонок: vk_id это BIGINT, и
    COALESCE(vk_id, '') в PostgreSQL падает «COALESCE types bigint and character
    varying cannot be matched» (SQLite типы не сверяет — в тестах не видно).
    """
    if isinstance(column.type, String):
        return and_(column.is_not(None), func.coalesce(column, '') != '')
    return and_(column.is_not(None), column != 0)


def _auth_sql(auth: AuthCondition) -> ColumnElement[bool]:
    telegram = User.telegram_id.is_not(None)
    email = and_(_filled(User.email), _filled(User.password_hash))
    oauth = [_filled(getattr(User, column)) for column in OAUTH_PROVIDER_COLUMNS.values()]
    count = sum((case((flag, 1), else_=0) for flag in (telegram, email, *oauth)), literal(0))
    single = count == 1
    if auth == 'telegram_only':
        return and_(single, telegram)
    if auth == 'email_only':
        return and_(single, email)
    return single


def _alive_sub(now: datetime) -> ColumnElement[bool]:
    return and_(Subscription.user_id == User.id, Subscription.status.in_(_ALIVE), Subscription.end_date > now)


def _had_sub() -> ColumnElement[bool]:
    return exists().where(Subscription.user_id == User.id, Subscription.status != SubscriptionStatus.PENDING.value)


def _subscription_sql(condition: SubscriptionCondition, now: datetime) -> ColumnElement[bool]:
    segment = condition.segment
    if segment == 'active':
        return exists().where(_alive_sub(now), Subscription.is_trial.is_(False))
    if segment == 'trial':
        return exists().where(_alive_sub(now), Subscription.is_trial.is_(True))
    if segment == 'expiring':
        return exists().where(_alive_sub(now), Subscription.end_date <= now + timedelta(days=condition.days))
    if segment == 'tariff':
        return exists().where(_alive_sub(now), Subscription.tariff_id == condition.tariff_id)
    if segment == 'low_balance':
        return and_(User.balance_kopeks > 0, User.balance_kopeks < LOW_BALANCE_THRESHOLD_KOPEKS)
    no_alive = not_(exists().where(_alive_sub(now)))
    if segment == 'expired':
        return and_(no_alive, _had_sub())
    return and_(no_alive, not_(_had_sub()))  # none


def _never_connected_sql(panel_ids: frozenset[int] | None, now: datetime) -> ColumnElement[bool]:
    # Аккаунт подписки в панели — Subscription.remnawave_id; у старых одиночных его нет,
    # тогда — User.remnawave_id. Telegram ID не годится: им совпадает и другой,
    # подключавшийся аккаунт того же человека.
    if not panel_ids:
        return false()
    return exists().where(
        _alive_sub(now),
        or_(
            Subscription.remnawave_id.in_(panel_ids),
            and_(Subscription.remnawave_id.is_(None), User.remnawave_id.in_(panel_ids)),
        ),
    )


def condition_clauses(
    conditions: ReminderConditions, *, now: datetime, never_connected_ids: frozenset[int] | None = None
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [or_(User.status.is_(None), User.status.not_in(_EXCLUDED_STATUSES))]
    if conditions.auth:
        clauses.append(_auth_sql(conditions.auth))
    if conditions.subscription:
        clauses.append(_subscription_sql(conditions.subscription, now))
    if conditions.registered_days_min is not None:
        clauses.append(User.created_at <= now - timedelta(days=conditions.registered_days_min))
    if conditions.inactive_days_min is not None:
        cutoff = now - timedelta(days=conditions.inactive_days_min)
        # max(бот, кабинет) <= cutoff, null — «никогда». Без greatest(): его нет в SQLite.
        clauses.append(
            and_(
                or_(User.last_activity.is_(None), User.last_activity <= cutoff),
                or_(User.cabinet_last_login.is_(None), User.cabinet_last_login <= cutoff),
            )
        )
    if conditions.never_connected:
        clauses.append(_never_connected_sql(never_connected_ids, now))
    return clauses


# --- Один человек ---------------------------------------------------------------------


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _is_alive(subscription, now: datetime) -> bool:
    end = _aware(subscription.end_date)
    return subscription.status in ALIVE_SUBSCRIPTION_STATUSES and end is not None and end > now


def _auth_matches(user, auth: AuthCondition) -> bool:
    methods = compute_auth_methods(user)
    if len(methods) != 1:
        return False
    if auth == 'telegram_only':
        return methods == ['telegram']
    if auth == 'email_only':
        return methods == ['email']
    return True


def _subscription_matches(user, subscriptions: Sequence, condition: SubscriptionCondition, now: datetime) -> bool:
    alive = [s for s in subscriptions if _is_alive(s, now)]
    segment = condition.segment
    if segment == 'active':
        return any(not s.is_trial for s in alive)
    if segment == 'trial':
        return any(s.is_trial for s in alive)
    if segment == 'expiring':
        limit = now + timedelta(days=condition.days)
        return any(_aware(s.end_date) <= limit for s in alive)
    if segment == 'tariff':
        return any(s.tariff_id == condition.tariff_id for s in alive)
    if segment == 'low_balance':
        return 0 < (user.balance_kopeks or 0) < LOW_BALANCE_THRESHOLD_KOPEKS
    had = any(s.status != SubscriptionStatus.PENDING.value for s in subscriptions)
    if segment == 'expired':
        return not alive and had
    return not alive and not had  # none


def _never_connected_matches(user, subscriptions: Sequence, panel_ids: frozenset[int] | None, now: datetime) -> bool:
    if not panel_ids:
        return False
    for subscription in subscriptions:
        if not _is_alive(subscription, now):
            continue
        panel_id = subscription.remnawave_id if subscription.remnawave_id is not None else user.remnawave_id
        if panel_id in panel_ids:
            return True
    return False


def matches(
    user,
    subscriptions: Sequence,
    conditions: ReminderConditions,
    *,
    now: datetime,
    never_connected_ids: frozenset[int] | None = None,
) -> bool:
    if user.status in _EXCLUDED_STATUSES:
        return False
    if conditions.auth and not _auth_matches(user, conditions.auth):
        return False
    if conditions.subscription and not _subscription_matches(user, subscriptions, conditions.subscription, now):
        return False
    if conditions.registered_days_min is not None:
        created = _aware(user.created_at)
        if created is None or created > now - timedelta(days=conditions.registered_days_min):
            return False
    if conditions.inactive_days_min is not None:
        cutoff = now - timedelta(days=conditions.inactive_days_min)
        seen = [moment for moment in (_aware(user.last_activity), _aware(user.cabinet_last_login)) if moment]
        if seen and max(seen) > cutoff:
            return False
    if conditions.never_connected and not _never_connected_matches(user, subscriptions, never_connected_ids, now):
        return False
    return True
