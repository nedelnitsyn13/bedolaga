"""Full sync must not create a fake second Subscription for a user's
limited-server companion panel account.

Repro: the "panel is truth" full-sync backfill (multi-tariff path) matches
any panel user it can't find in `Subscription.remnawave_id` to a bot user by
telegram_id/email and creates a new Subscription for them. The limited-
companion account (see `_sync_limited_companion_user`) is exactly such an
"unmatched" panel user — same telegram_id as the real user, but tracked via
`Subscription.limited_companion_remnawave_id`, not `remnawave_id` — so every
real full sync turned each user's companion account into a bogus extra
subscription. Companion panel ids must be excluded from the backfill.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.grace_access_runtime as grace_runtime_mod
from app.config import Settings
from app.services.remnawave_service import RemnaWaveService


def _panel_account(panel_id: int, *, telegram_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=panel_id,
        short_uuid=f'short-{panel_id}',
        username=f'user_{telegram_id}_lim',
        status=SimpleNamespace(value='ACTIVE'),
        telegram_id=telegram_id,
        email=None,
        expire_at=datetime.now(UTC) + timedelta(days=30),
        traffic_limit_bytes=0,
        used_traffic_bytes=0,
        hwid_device_limit=None,
        subscription_url='https://panel.example/sub',
        happ_crypto_link=None,
        active_internal_squads=[],
    )


@pytest.mark.asyncio
async def test_companion_panel_account_is_not_backfilled_as_a_new_subscription(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(grace_runtime_mod, 'get_open_grace_subscription_ids', AsyncMock(return_value=set()))

    telegram_id = 555
    companion_panel_id = 102

    # The user's real subscription is linked via remnawave_id=101 and tracks
    # its companion account's panel id (102) separately.
    main_sub = SimpleNamespace(remnawave_id=101, limited_companion_remnawave_id=companion_panel_id)
    bot_user = SimpleNamespace(id=7, telegram_id=telegram_id, email=None, subscriptions=[])

    subs_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [main_sub]))
    companion_ids_result = SimpleNamespace(all=lambda: [(companion_panel_id,)])
    legacy_users_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))
    all_users_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [bot_user]))

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[subs_result, companion_ids_result, legacy_users_result, all_users_result])

    api = AsyncMock()
    roster = [_panel_account(companion_panel_id, telegram_id=telegram_id)]
    api.get_all_users_page_stream = AsyncMock(return_value={'users': roster, 'hasMore': False, 'nextCursor': None})

    svc = RemnaWaveService()
    svc._config_error = None

    @asynccontextmanager
    async def fake_client():
        yield api

    monkeypatch.setattr(svc, 'get_api_client', fake_client)

    stats = await svc.sync_users_from_panel(db, 'all')

    assert stats['created'] == 0, 'companion panel account must not spawn a fake subscription'
    assert stats['errors'] == 0
