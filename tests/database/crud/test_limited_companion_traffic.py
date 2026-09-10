"""Докупка трафика лимитного сервера-компаньона — на настоящем PostgreSQL.

``add_limited_companion_traffic`` берёт row-level lock (``with_for_update``) на
подписке — на SQLite это молча игнорируется, поэтому конкурентный сценарий
там проверить нельзя в принципе. См. tests/fixtures/postgres_db.py.

Главное свойство, которое здесь проверяется: докупленные ГБ живут ровно 30
дней (как и у основного трафика — TrafficPurchase), а не копятся навсегда.
Раньше limited_companion_purchased_traffic_gb был просто аккумулятором.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.crud.subscription import (
    add_limited_companion_traffic,
    housekeep_limited_companion_traffic,
)
from app.database.models import LimitedCompanionTrafficPurchase, Subscription, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = [User.__table__, Subscription.__table__, LimitedCompanionTrafficPurchase.__table__]


async def _create_user(db, *, telegram_id: int) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_subscription(db, user: User, *, short_id: str, purchased_gb: int = 0) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id=short_id,
        limited_companion_remnawave_id=999,
        limited_companion_purchased_traffic_gb=purchased_gb,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def test_add_limited_companion_traffic_creates_purchase_with_30_day_expiry(postgres_database) -> None:
    async with postgres_session(postgres_database, TABLES) as db:
        user = await _create_user(db, telegram_id=9001)
        subscription = await _create_subscription(db, user, short_id='lc-test-1')

        before = datetime.now(UTC)
        purchased_gb = await add_limited_companion_traffic(db, subscription, 50)
        after = datetime.now(UTC)

        assert purchased_gb == 50
        assert subscription.limited_companion_purchased_traffic_gb == 50

        rows = (
            (
                await db.execute(
                    select(LimitedCompanionTrafficPurchase).where(
                        LimitedCompanionTrafficPurchase.subscription_id == subscription.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        purchase = rows[0]
        assert purchase.traffic_gb == 50
        # expires_at = момент покупки + 30 дней, с запасом на время выполнения теста.
        assert before + timedelta(days=30) <= purchase.expires_at <= after + timedelta(days=30)


async def test_add_limited_companion_traffic_accumulates_across_purchases(postgres_database) -> None:
    async with postgres_session(postgres_database, TABLES) as db:
        user = await _create_user(db, telegram_id=9002)
        subscription = await _create_subscription(db, user, short_id='lc-test-2')

        await add_limited_companion_traffic(db, subscription, 50)
        total = await add_limited_companion_traffic(db, subscription, 100)

        assert total == 150
        assert subscription.limited_companion_purchased_traffic_gb == 150

        rows = (
            (
                await db.execute(
                    select(LimitedCompanionTrafficPurchase).where(
                        LimitedCompanionTrafficPurchase.subscription_id == subscription.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2


async def test_housekeep_drops_expired_purchase_and_keeps_active_one(postgres_database) -> None:
    async with postgres_session(postgres_database, TABLES) as db:
        user = await _create_user(db, telegram_id=9003)
        # Инвариант уже расходится: аккумулятор (100) не совпадает с тем, что
        # дадут активные записи (60) — ровно то состояние, в котором были все
        # подписки до этого фикса.
        subscription = await _create_subscription(db, user, short_id='lc-test-3', purchased_gb=100)

        now = datetime.now(UTC)
        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id, traffic_gb=40, expires_at=now - timedelta(days=1)
            )
        )
        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id, traffic_gb=60, expires_at=now + timedelta(days=29)
            )
        )
        await db.commit()

        purchased_gb = await housekeep_limited_companion_traffic(db, subscription)

        assert purchased_gb == 60
        assert subscription.limited_companion_purchased_traffic_gb == 60

        rows = (
            (
                await db.execute(
                    select(LimitedCompanionTrafficPurchase).where(
                        LimitedCompanionTrafficPurchase.subscription_id == subscription.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].traffic_gb == 60


async def test_housekeep_is_noop_when_nothing_expired(postgres_database) -> None:
    async with postgres_session(postgres_database, TABLES) as db:
        user = await _create_user(db, telegram_id=9004)
        subscription = await _create_subscription(db, user, short_id='lc-test-4')
        await add_limited_companion_traffic(db, subscription, 50)

        purchased_gb = await housekeep_limited_companion_traffic(db, subscription)

        assert purchased_gb == 50
        assert subscription.limited_companion_purchased_traffic_gb == 50


async def test_housekeep_zeroes_out_when_all_purchases_expired(postgres_database) -> None:
    async with postgres_session(postgres_database, TABLES) as db:
        user = await _create_user(db, telegram_id=9005)
        subscription = await _create_subscription(db, user, short_id='lc-test-5', purchased_gb=50)

        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id,
                traffic_gb=50,
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await db.commit()

        purchased_gb = await housekeep_limited_companion_traffic(db, subscription)

        assert purchased_gb == 0
        assert subscription.limited_companion_purchased_traffic_gb == 0
