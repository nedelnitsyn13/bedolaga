"""Переключение тарифа в Mini App не должно порождать дубликат живой подписки.

Баг: ``switch_tariff_endpoint`` менял ``subscription.tariff_id`` на выбранный
тариф без проверки, нет ли у пользователя уже другой живой (active/trial/
limited) подписки на этот же тариф. В production это падало прямо в БД —
``IntegrityError: duplicate key value violates unique constraint
"uq_subscriptions_user_tariff_active"`` — потому что частичный уникальный
индекс (user_id, tariff_id) не пускает две живые подписки на один тариф.
Кабинетный аналог (``tariff_switch.py``) эту проверку уже делал; Mini App —
нет. Фикс добавляет тот же guard перед мутацией.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    Base.metadata.tables['users'],
    Base.metadata.tables['tariffs'],
    Base.metadata.tables['subscriptions'],
    Base.metadata.tables['promo_groups'],
    Base.metadata.tables['tariff_promo_groups'],
]


@pytest.fixture(autouse=True)
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)


def _tariff(tariff_id: int, name: str) -> Tariff:
    return Tariff(
        id=tariff_id,
        name=name,
        description='',
        is_active=True,
        is_daily=False,
        period_prices={'30': 10000},
        traffic_limit_gb=100,
        device_limit=1,
        allowed_squads=['squad-1'],
        display_order=tariff_id,
    )


async def _setup(db) -> tuple[User, Subscription, Subscription]:
    now = datetime.now(UTC)
    db.add_all(
        [
            User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0),
            _tariff(1, 'Тариф A'),
            _tariff(2, 'Тариф B'),
            Subscription(
                id=10,
                remnawave_short_id='src',
                user_id=1,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=1),
                end_date=now + timedelta(days=20),
                traffic_limit_gb=100,
                traffic_used_gb=0.0,
                device_limit=1,
                tariff_id=1,
                connected_squads=['squad-1'],
            ),
            Subscription(
                id=20,
                remnawave_short_id='tgt',
                user_id=1,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=1),
                end_date=now + timedelta(days=20),
                traffic_limit_gb=100,
                traffic_used_gb=0.0,
                device_limit=1,
                tariff_id=2,
                connected_squads=['squad-1'],
            ),
        ]
    )
    await db.commit()

    loaded = await db.execute(select(User).options(selectinload(User.subscriptions)).where(User.id == 1))
    user = loaded.scalar_one()
    source = await db.get(Subscription, 10)
    target = await db.get(Subscription, 20)
    return user, source, target


@pytest.mark.asyncio
async def test_switch_to_already_owned_tariff_is_rejected(monkeypatch):
    """Пользователь пытается переключить подписку #10 (тариф A) на тариф B,
    который у него уже занят живой подпиской #20 — раньше это падало в БД."""
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppTariffSwitchRequest

    async with memory_session(monkeypatch, TABLES) as db:
        user, source, target = await _setup(db)

        async def _fake_authorize(init_data, session):
            return user

        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', _fake_authorize)

        with pytest.raises(HTTPException) as exc:
            await miniapp.switch_tariff_endpoint(
                payload=MiniAppTariffSwitchRequest(init_data='stub', tariff_id=2, subscriptionId=10),
                db=db,
            )

        source_after = await db.get(Subscription, 10)
        target_after = await db.get(Subscription, 20)

    assert exc.value.status_code == 409
    assert exc.value.detail['code'] == 'tariff_already_owned'
    # Ничего не должно было измениться — не докатились даже до мутации.
    assert source_after.tariff_id == 1
    assert target_after.tariff_id == 2
