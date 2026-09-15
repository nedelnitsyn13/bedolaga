"""Периодический цикл LIMITED squad: отбор подписок и best-effort по каждой."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.database.models import (
    LimitedCompanionTrafficPurchase,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services.limited_squad_monitoring_service import _load_subscriptions_with_limited_traffic
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    LimitedCompanionTrafficPurchase.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
]


async def _create_user(db, *, telegram_id: int) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_tariff(db, *, name: str, limited_enabled: bool) -> Tariff:
    tariff = Tariff(
        name=name,
        limited_traffic_enabled=limited_enabled,
        limited_squad_uuids=['limited-squad'] if limited_enabled else [],
        limited_base_traffic_gb=50 if limited_enabled else 0,
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


async def _create_subscription(
    db, user: User, tariff: Tariff, *, short_id: str, status: str = SubscriptionStatus.ACTIVE.value
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=status,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id=short_id,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def test_only_subscriptions_on_limited_enabled_tariffs_are_selected(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9301)
        limited_tariff = await _create_tariff(db, name='LIMITED', limited_enabled=True)
        plain_tariff = await _create_tariff(db, name='Обычный', limited_enabled=False)

        target = await _create_subscription(db, user, limited_tariff, short_id='lm-1')
        await _create_subscription(db, user, plain_tariff, short_id='lm-2')

        subscriptions = await _load_subscriptions_with_limited_traffic(db)

        assert [s.id for s in subscriptions] == [target.id]


async def test_expired_subscriptions_are_excluded(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9302)
        tariff = await _create_tariff(db, name='LIMITED', limited_enabled=True)

        await _create_subscription(db, user, tariff, short_id='lm-3', status=SubscriptionStatus.EXPIRED.value)

        subscriptions = await _load_subscriptions_with_limited_traffic(db)

        assert subscriptions == []


async def test_trial_and_limited_status_subscriptions_are_included(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9303)
        # Разные тарифы — у user+tariff есть частичный уникальный индекс на
        # живые статусы, две подписки одного user на одном тарифе тут не годятся.
        trial_tariff = await _create_tariff(db, name='LIMITED trial', limited_enabled=True)
        limited_tariff = await _create_tariff(db, name='LIMITED limited', limited_enabled=True)

        trial_sub = await _create_subscription(
            db, user, trial_tariff, short_id='lm-4', status=SubscriptionStatus.TRIAL.value
        )
        limited_sub = await _create_subscription(
            db, user, limited_tariff, short_id='lm-5', status=SubscriptionStatus.LIMITED.value
        )

        subscriptions = await _load_subscriptions_with_limited_traffic(db)

        assert {s.id for s in subscriptions} == {trial_sub.id, limited_sub.id}
