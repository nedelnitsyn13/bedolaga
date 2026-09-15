"""Кабинет: /subscription/limited-traffic* — новая ветка (LIMITED squad), не companion.

Проверяет, что существующие эндпоинты правильно ответвляются на новую
архитектуру, когда у тарифа подписки включён `limited_traffic_enabled`, не
трогая legacy-ветку (limited_companion_*) — она используется только когда
у подписки есть `limited_companion_remnawave_id`, чего у этих тестовых
подписок нет ни в одном сценарии.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.cabinet.routes.subscription_modules.traffic import (
    get_limited_companion_traffic,
    get_limited_companion_traffic_packages,
    purchase_limited_companion_traffic,
)
from app.cabinet.schemas.subscription import TrafficPurchaseRequest
from app.config import settings
from app.database.models import (
    LimitedCompanionTrafficPurchase,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    LimitedCompanionTrafficPurchase.__table__,
    Transaction.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
]


async def _create_user(db, *, telegram_id: int, balance_kopeks: int = 0) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', balance_kopeks=balance_kopeks)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_tariff(db, *, base_gb: int = 50) -> Tariff:
    tariff = Tariff(
        name='LIMITED test tariff',
        limited_traffic_enabled=True,
        limited_squad_uuids=['limited-squad'],
        limited_base_traffic_gb=base_gb,
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


async def _create_subscription(db, user: User, tariff: Tariff, *, used_gb: float = 0.0) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id='lt-new-1',
        limited_traffic_used_gb=used_gb,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    # user.subscription property lazily reads user.subscriptions — refresh relationship.
    await db.refresh(user, ['subscriptions'])
    return subscription


async def test_get_limited_traffic_reports_new_architecture_fields(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=7401)
        tariff = await _create_tariff(db, base_gb=50)
        await _create_subscription(db, user, tariff, used_gb=32.4)

        response = await get_limited_companion_traffic(user=user, db=db, subscription_id=None)

        assert response.available is True
        assert response.base_limit_gb == 50
        assert response.purchased_gb == 0
        assert response.total_limit_gb == 50
        assert response.used_gb == 32.4
        assert response.used_percent == pytest.approx(64.8, abs=0.1)


async def test_get_limited_traffic_includes_active_purchases(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=7402)
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff, used_gb=10.0)

        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id, traffic_gb=20, expires_at=datetime.now(UTC) + timedelta(days=10)
            )
        )
        await db.commit()

        response = await get_limited_companion_traffic(user=user, db=db, subscription_id=None)

        assert response.purchased_gb == 20
        assert response.total_limit_gb == 70


async def test_get_limited_traffic_packages_available_without_legacy_companion(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_PACKAGES_CONFIG', '20:10000:true')

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=7403)
        tariff = await _create_tariff(db, base_gb=50)
        await _create_subscription(db, user, tariff)

        packages = await get_limited_companion_traffic_packages(user=user, db=db, subscription_id=None)

        assert len(packages) == 1
        assert packages[0].gb == 20


async def test_purchase_charges_balance_and_persists_purchase(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_PACKAGES_CONFIG', '20:10000:true')

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=7404, balance_kopeks=50000)
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff)

        response = await purchase_limited_companion_traffic(
            request=TrafficPurchaseRequest(gb=20),
            user=user,
            db=db,
            subscription_id=None,
        )

        assert response['success'] is True
        assert response['gb_added'] == 20
        assert response['new_purchased_traffic_gb'] == 20
        assert response['new_total_limit_gb'] == 70
        assert response['amount_paid_kopeks'] == 10000
        assert response['new_balance_kopeks'] == 40000

        await db.refresh(subscription)
        # Purchase persisted via the shared limited_companion_traffic_purchases
        # table, but the LEGACY companion accumulator field must stay untouched —
        # this subscription uses the new architecture, not a companion account.
        assert subscription.limited_companion_purchased_traffic_gb == 0


async def test_purchase_rejects_when_new_architecture_base_is_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_PACKAGES_CONFIG', '20:10000:true')

    from fastapi import HTTPException

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=7405, balance_kopeks=50000)
        tariff = await _create_tariff(db, base_gb=0)  # безлимит
        await _create_subscription(db, user, tariff)

        with pytest.raises(HTTPException) as exc_info:
            await purchase_limited_companion_traffic(
                request=TrafficPurchaseRequest(gb=20),
                user=user,
                db=db,
                subscription_id=None,
            )
        assert exc_info.value.status_code == 400


async def test_purchase_saves_cart_on_insufficient_balance(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_PACKAGES_CONFIG', '20:10000:true')

    from fastapi import HTTPException

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=7406, balance_kopeks=100)  # мало денег
        tariff = await _create_tariff(db, base_gb=50)
        await _create_subscription(db, user, tariff)

        with pytest.raises(HTTPException) as exc_info:
            await purchase_limited_companion_traffic(
                request=TrafficPurchaseRequest(gb=20),
                user=user,
                db=db,
                subscription_id=None,
            )
        assert exc_info.value.status_code == 402
        assert exc_info.value.detail['code'] == 'insufficient_funds'
