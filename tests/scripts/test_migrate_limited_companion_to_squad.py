"""scripts.migrate_limited_companion_to_squad — single-subscription cutover.

Dry run must never touch the panel or commit anything; --apply must call the
same enforcement entry point the periodic monitoring job uses
(``process_limited_traffic``) and then disable the legacy companion account,
reporting failure explicitly if the disable PATCH fails (it must not swallow
the error the way the best-effort mirror-helpers in panel_sync do — this is a
deliberate one-off admin action).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.database.models import (
    LimitedCompanionTrafficPurchase,
    PromoGroup,
    Subscription,
    Tariff,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from scripts import migrate_limited_companion_to_squad as m
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    LimitedCompanionTrafficPurchase.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
]


async def _create_user(db) -> User:
    user = User(telegram_id=1, first_name='Т', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_tariff(db, *, new_arch: bool = True, squads=None, base_gb: int = 10) -> Tariff:
    tariff = Tariff(
        name='Tariff',
        limited_traffic_enabled=new_arch,
        limited_squad_uuids=list(squads if squads is not None else (['squad-1'] if new_arch else [])),
        limited_base_traffic_gb=base_gb if new_arch else 0,
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


async def _create_subscription(db, user: User, tariff: Tariff, *, companion_id: int | None = 857) -> Subscription:
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


@pytest.mark.asyncio
async def test_dry_run_reports_state_without_touching_anything(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff)

        process_mock = AsyncMock()
        disable_mock = AsyncMock()
        monkeypatch.setattr(m, 'process_limited_traffic', process_mock)
        monkeypatch.setattr(m, 'disable_companion_account', disable_mock)

        report = await m._migrate_one(db, AsyncMock(), subscription.id, apply=False)

        assert report.ok is True
        assert report.reason is None
        assert report.companion_id == 857
        assert report.squad_uuids == ['squad-1']
        assert report.base_traffic_gb == 10
        assert report.used_gb_after is None
        process_mock.assert_not_awaited()
        disable_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_fails_when_subscription_missing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        report = await m._migrate_one(db, AsyncMock(), 999999, apply=False)

        assert report.ok is False
        assert report.reason == 'подписка не найдена'


@pytest.mark.asyncio
async def test_validation_fails_when_tariff_not_on_new_architecture(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db, new_arch=False)
        subscription = await _create_subscription(db, user, tariff)

        report = await m._migrate_one(db, AsyncMock(), subscription.id, apply=False)

        assert report.ok is False
        assert 'не переведён' in report.reason


@pytest.mark.asyncio
async def test_validation_fails_when_tariff_has_no_squads(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db, new_arch=True, squads=[])
        subscription = await _create_subscription(db, user, tariff)

        report = await m._migrate_one(db, AsyncMock(), subscription.id, apply=False)

        assert report.ok is False
        assert 'limited_squad_uuids' in report.reason


@pytest.mark.asyncio
async def test_validation_fails_without_a_legacy_companion(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff, companion_id=None)

        report = await m._migrate_one(db, AsyncMock(), subscription.id, apply=False)

        assert report.ok is False
        assert 'нет legacy companion' in report.reason


@pytest.mark.asyncio
async def test_apply_runs_enforcement_then_disables_companion(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff)

        process_mock = AsyncMock()
        disable_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(m, 'process_limited_traffic', process_mock)
        monkeypatch.setattr(m, 'disable_companion_account', disable_mock)

        api = AsyncMock()
        report = await m._migrate_one(db, api, subscription.id, apply=True)

        process_mock.assert_awaited_once()
        assert process_mock.await_args.args[0] is api
        assert process_mock.await_args.args[2].id == subscription.id
        disable_mock.assert_awaited_once_with(api, process_mock.await_args.args[2])
        assert report.ok is True
        assert report.companion_disabled is True
        assert report.companion_disable_error is None


@pytest.mark.asyncio
async def test_apply_reports_failure_when_disable_raises(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff)

        process_mock = AsyncMock()
        disable_mock = AsyncMock(side_effect=RuntimeError('panel unreachable'))
        monkeypatch.setattr(m, 'process_limited_traffic', process_mock)
        monkeypatch.setattr(m, 'disable_companion_account', disable_mock)

        report = await m._migrate_one(db, AsyncMock(), subscription.id, apply=True)

        process_mock.assert_awaited_once()
        assert report.ok is False
        assert report.companion_disabled is False
        assert 'panel unreachable' in report.companion_disable_error
