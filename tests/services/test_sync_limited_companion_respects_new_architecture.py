"""SubscriptionService._sync_limited_companion_user must stand down once a
subscription's tariff has switched to the new LIMITED squad architecture
(app/services/limited_squad_service.py, Tariff.limited_traffic_enabled).

Without this guard, ``scripts/migrate_limited_companion_to_squad.py`` disabling
a companion account would be undone on the very next subscription push: this
method mirrors the main panel account's status back onto the companion
whenever ``LIMITED_COMPANION_ENABLED`` is on, regardless of the subscription's
own tariff.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import (
    LimitedCompanionTrafficPurchase,
    PromoGroup,
    Subscription,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    LimitedCompanionTrafficPurchase.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
]


async def _create_user(db) -> User:
    user = User(telegram_id=1, first_name='Т', language='ru', balance_kopeks=0)
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


async def _create_subscription(db, user: User, tariff: Tariff, *, companion_id: int = 857) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id='abc',
        limited_companion_remnawave_id=companion_id,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


def _main_user():
    return SimpleNamespace(id=1, username='main', status='ACTIVE', expire_at=None, short_uuid='m-short')


@pytest.mark.asyncio
async def test_new_architecture_tariff_skips_companion_sync(monkeypatch):
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_ENABLED', True)
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_SQUAD_UUID', 'legacy-squad')

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db, new_arch=True)
        subscription = await _create_subscription(db, user, tariff)
        await db.refresh(subscription, ['tariff'])

        write_mock = AsyncMock()
        monkeypatch.setattr('app.services.subscription_service.write_companion_account', write_mock)
        api = AsyncMock()

        await SubscriptionService()._sync_limited_companion_user(api, db, user, subscription, _main_user())

        write_mock.assert_not_awaited()
        api.get_user_by_id.assert_not_awaited()
        api.update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_tariff_still_syncs_companion(monkeypatch):
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_ENABLED', True)
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_SQUAD_UUID', 'legacy-squad')

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db, new_arch=False)
        subscription = await _create_subscription(db, user, tariff)
        await db.refresh(subscription, ['tariff'])

        write_mock = AsyncMock(
            return_value=SimpleNamespace(id=857, short_uuid='lim-short', username='comp', used_traffic_bytes=0)
        )
        monkeypatch.setattr('app.services.subscription_service.write_companion_account', write_mock)
        api = AsyncMock()

        await SubscriptionService()._sync_limited_companion_user(api, db, user, subscription, _main_user())

        write_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_tariff_at_all_still_syncs_companion(monkeypatch):
    """A subscription with no tariff row (single-mode legacy account) must keep
    behaving exactly as before — the new-arch check treats a missing tariff as
    "not the new architecture", never as a reason to skip."""
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_ENABLED', True)
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_SQUAD_UUID', 'legacy-squad')

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        subscription = Subscription(
            user_id=user.id,
            tariff_id=None,
            end_date=datetime.now(UTC) + timedelta(days=30),
            remnawave_short_id='abc',
            limited_companion_remnawave_id=857,
        )
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)

        write_mock = AsyncMock(
            return_value=SimpleNamespace(id=857, short_uuid='lim-short', username='comp', used_traffic_bytes=0)
        )
        monkeypatch.setattr('app.services.subscription_service.write_companion_account', write_mock)
        api = AsyncMock()

        await SubscriptionService()._sync_limited_companion_user(api, db, user, subscription, _main_user())

        write_mock.assert_awaited_once()
