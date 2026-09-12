"""Admin device-limit edits must mirror hwid_device_limit onto the companion.

Repro: the admin bot's "изменить лимит устройств" screen pushes only the main
panel account via a narrow PATCH (_push_narrow_change_to_panel), bypassing
SubscriptionService._sync_limited_companion_user entirely — so the limited-
companion account (Subscription.limited_companion_remnawave_id) kept its old
device limit after an admin changed it. Squads and traffic-limit edits go
through the same helper but must NOT mirror to the companion — it has its own
independent squad and traffic quota.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.admin import users as admin_users_module


def _fake_service(api):
    class _Service:
        def get_api_client(self):
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=api)
            ctx.__aexit__ = AsyncMock(return_value=None)
            return ctx

    return _Service


@pytest.mark.asyncio
async def test_device_limit_push_mirrors_onto_the_companion(monkeypatch):
    api = AsyncMock()
    companion_calls = []

    async def fake_push_subscription(_api, _user, _subscription, **_kwargs):
        return None

    async def fake_sync_companion_device_limit(_api, subscription):
        companion_calls.append(subscription.id)

    monkeypatch.setattr(admin_users_module, 'RemnaWaveService', _fake_service(api))
    monkeypatch.setattr('app.services.panel_sync.push_subscription', fake_push_subscription)
    monkeypatch.setattr('app.services.panel_sync.sync_companion_device_limit', fake_sync_companion_device_limit)
    monkeypatch.setattr('app.services.grace_access_runtime.update_panel_user_grace_safe', AsyncMock())

    user = SimpleNamespace(id=1)
    subscription = SimpleNamespace(id=101, limited_companion_remnawave_id=857, tariff=None)
    db = AsyncMock()

    await admin_users_module._push_narrow_change_to_panel(
        db, user, subscription, fields={'hwid_device_limit'}, sync_companion_device_limit_too=True
    )

    assert companion_calls == [101]


@pytest.mark.asyncio
async def test_squad_and_traffic_pushes_do_not_touch_the_companion(monkeypatch):
    """Only the device-limit screen opts into companion mirroring."""
    api = AsyncMock()
    companion_calls = []

    async def fake_push_subscription(_api, _user, _subscription, **_kwargs):
        return None

    async def fake_sync_companion_device_limit(_api, subscription):
        companion_calls.append(subscription.id)

    monkeypatch.setattr(admin_users_module, 'RemnaWaveService', _fake_service(api))
    monkeypatch.setattr('app.services.panel_sync.push_subscription', fake_push_subscription)
    monkeypatch.setattr('app.services.panel_sync.sync_companion_device_limit', fake_sync_companion_device_limit)
    monkeypatch.setattr('app.services.grace_access_runtime.update_panel_user_grace_safe', AsyncMock())

    user = SimpleNamespace(id=1)
    subscription = SimpleNamespace(id=101, limited_companion_remnawave_id=857, tariff=None)
    db = AsyncMock()

    await admin_users_module._push_narrow_change_to_panel(db, user, subscription, fields={'active_internal_squads'})
    await admin_users_module._push_narrow_change_to_panel(
        db, user, subscription, fields={'traffic_limit_bytes', 'traffic_limit_strategy'}
    )

    assert companion_calls == []
