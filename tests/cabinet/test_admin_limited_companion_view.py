"""Cabinet admin sees the limited-companion account and can re-sync it.

The companion (``Subscription.limited_companion_remnawave_id``) is a separate
panel account with its own traffic quota, so nothing in the main subscription
block describes it. The admin view exposed only its *limit*, which is not
enough to answer the one question an operator actually has — "has this user
burned through the limited server?" — and ``limited_companion_traffic_used_gb``
is written only by a top-up's resync and the periodic monitoring pass, so it
can sit stale (often ``0``) for days with no way to refresh it.

Route/builder functions are called directly (no HTTP client), the house pattern
for ``admin_users.py`` — see ``tests/cabinet/test_platega_recurrent_admin.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateSubscriptionRequest
from app.config import Settings, settings


PANEL_ID = 857


@pytest.fixture(autouse=True)
def companion_enabled(monkeypatch):
    monkeypatch.setattr(Settings, 'is_limited_companion_enabled', lambda self: True)
    monkeypatch.setattr(Settings, 'is_platega_recurrent_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50, raising=False)
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_SQUAD_UUID', 'squad-lim', raising=False)


def _subscription(**overrides) -> SimpleNamespace:
    base = dict(
        id=42,
        user_id=7,
        status='active',
        is_trial=False,
        start_date=None,
        end_date=None,
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=1,
        tariff_id=None,
        autopay_enabled=False,
        limited_companion_remnawave_id=PANEL_ID,
        limited_companion_traffic_used_gb=12.5,
        limited_companion_purchased_traffic_gb=0,
        grace_session_open=False,
        grace_overlay_expire_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _db_with_empty_traffic_purchases() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return db


# --- _build_subscription_info_async -----------------------------------------


async def test_builder_exposes_companion_usage_not_just_its_limit():
    info = await admin_users._build_subscription_info_async(_db_with_empty_traffic_purchases(), _subscription())

    assert info.has_limited_companion is True
    assert info.limited_companion_panel_id == PANEL_ID
    assert info.limited_companion_traffic_used_gb == 12.5
    # Companion's own quota (50), not the main subscription's 100.
    assert info.limited_companion_traffic_limit_gb == 50


async def test_builder_adds_active_top_ups_to_the_companion_limit():
    info = await admin_users._build_subscription_info_async(
        _db_with_empty_traffic_purchases(),
        _subscription(limited_companion_purchased_traffic_gb=30),
    )

    assert info.limited_companion_purchased_traffic_gb == 30
    assert info.limited_companion_traffic_limit_gb == 80


async def test_builder_leaves_companion_fields_at_defaults_without_a_companion():
    info = await admin_users._build_subscription_info_async(
        _db_with_empty_traffic_purchases(),
        _subscription(limited_companion_remnawave_id=None),
    )

    assert info.has_limited_companion is False
    assert info.limited_companion_panel_id is None
    assert info.limited_companion_traffic_used_gb == 0.0


# --- POST /{user_id}/subscription  action=sync_limited_companion -------------


def _user_with(subscription) -> SimpleNamespace:
    return SimpleNamespace(id=7, subscriptions=[subscription])


async def _call_sync(monkeypatch, subscription, *, resync_result: bool) -> tuple:
    calls: list[int] = []

    async def fake_resync(_self, _db, subscription_arg):
        calls.append(subscription_arg.id)
        return resync_result

    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_user_with(subscription)))
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.resync_limited_companion', fake_resync)

    response = await admin_users.update_user_subscription(
        7,
        UpdateSubscriptionRequest(action='sync_limited_companion'),
        admin=SimpleNamespace(id=1),
        db=_db_with_empty_traffic_purchases(),
    )
    return response, calls


async def test_sync_action_resyncs_and_returns_fresh_companion_state(monkeypatch):
    subscription = _subscription()
    subscription.is_active = True

    response, calls = await _call_sync(monkeypatch, subscription, resync_result=True)

    assert calls == [subscription.id]
    assert response.success is True
    assert response.subscription.limited_companion_panel_id == PANEL_ID


async def test_sync_action_surfaces_a_failed_panel_sync(monkeypatch):
    subscription = _subscription()
    subscription.is_active = True

    with pytest.raises(HTTPException) as excinfo:
        await _call_sync(monkeypatch, subscription, resync_result=False)

    assert excinfo.value.status_code == 502


async def test_sync_action_rejects_a_subscription_without_a_companion(monkeypatch):
    subscription = _subscription(limited_companion_remnawave_id=None)
    subscription.is_active = True

    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_user_with(subscription)))

    with pytest.raises(HTTPException) as excinfo:
        await admin_users.update_user_subscription(
            7,
            UpdateSubscriptionRequest(action='sync_limited_companion'),
            admin=SimpleNamespace(id=1),
            db=_db_with_empty_traffic_purchases(),
        )

    assert excinfo.value.status_code == 400
