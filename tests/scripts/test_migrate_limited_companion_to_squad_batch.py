"""scripts.migrate_limited_companion_to_squad_batch — отбор подписок-кандидатов.

Сама миграция одной подписки (_migrate_one) уже покрыта
test_migrate_limited_companion_to_squad.py — здесь только логика отбора:
только живые статусы, только с реальным legacy companion, только тарифы
уже на новой архитектуре.
"""

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
from scripts.migrate_limited_companion_to_squad_batch import _load_candidate_subscription_ids
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
    user = User(telegram_id=telegram_id, first_name='Т', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_tariff(db, *, new_arch: bool) -> Tariff:
    tariff = Tariff(
        name='Tariff',
        limited_traffic_enabled=new_arch,
        limited_squad_uuids=['squad-1'] if new_arch else [],
        limited_base_traffic_gb=10 if new_arch else 0,
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


async def _create_subscription(
    db,
    user: User,
    tariff: Tariff,
    *,
    short_id: str,
    status: str = SubscriptionStatus.ACTIVE.value,
    companion_id: int | None = 857,
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=status,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id=short_id,
        limited_companion_remnawave_id=companion_id,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def test_selects_live_subscription_with_companion_on_migrated_tariff(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=1)
        tariff = await _create_tariff(db, new_arch=True)
        target = await _create_subscription(db, user, tariff, short_id='b-1')

        ids = await _load_candidate_subscription_ids(db)

        assert ids == [target.id]


async def test_ignores_subscriptions_on_tariffs_not_yet_migrated(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=2)
        tariff = await _create_tariff(db, new_arch=False)
        await _create_subscription(db, user, tariff, short_id='b-2')

        ids = await _load_candidate_subscription_ids(db)

        assert ids == []


async def test_ignores_subscriptions_without_a_legacy_companion(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=3)
        tariff = await _create_tariff(db, new_arch=True)
        await _create_subscription(db, user, tariff, short_id='b-3', companion_id=None)

        ids = await _load_candidate_subscription_ids(db)

        assert ids == []


async def test_ignores_expired_subscriptions(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=4)
        tariff = await _create_tariff(db, new_arch=True)
        await _create_subscription(db, user, tariff, short_id='b-4', status=SubscriptionStatus.EXPIRED.value)

        ids = await _load_candidate_subscription_ids(db)

        assert ids == []


async def test_includes_trial_and_limited_statuses(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=5)
        trial_tariff = await _create_tariff(db, new_arch=True)
        limited_tariff = await _create_tariff(db, new_arch=True)

        trial_sub = await _create_subscription(
            db, user, trial_tariff, short_id='b-5', status=SubscriptionStatus.TRIAL.value
        )
        limited_sub = await _create_subscription(
            db, user, limited_tariff, short_id='b-6', status=SubscriptionStatus.LIMITED.value
        )

        ids = await _load_candidate_subscription_ids(db)

        assert set(ids) == {trial_sub.id, limited_sub.id}
