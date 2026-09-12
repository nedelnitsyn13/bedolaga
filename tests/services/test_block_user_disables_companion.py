"""Blocking a user must also disable their limited-server companion account.

Repro reported live: an admin blocked a user, but the user's separate
limited-companion RemnaWave account (Subscription.limited_companion_remnawave_id)
kept working normally until its own traffic/time limit ran out on schedule —
`UserService.block_user` only ever disabled the main panel account
(`Subscription.remnawave_id` / `User.remnawave_id`) via the narrow
`disable_remnawave_user`, which — unlike the fuller `update_remnawave_user`
used by `unblock_user` — never touches the companion.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.user_service import UserService


def _subscription(*, remnawave_id, companion_id, is_trial=True, status='active'):
    return SimpleNamespace(
        id=1,
        remnawave_id=remnawave_id,
        limited_companion_remnawave_id=companion_id,
        is_trial=is_trial,
        status=status,
        end_date=None,
    )


@pytest.mark.asyncio
async def test_block_user_disables_companion_alongside_main_account(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    sub = _subscription(remnawave_id=101, companion_id=796)
    user = SimpleNamespace(id=7, telegram_id=555, email=None, subscriptions=[sub], remnawave_id=None)

    from app.services import user_service as user_service_module

    monkeypatch.setattr(user_service_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(user_service_module, 'update_user', AsyncMock())
    monkeypatch.setattr('app.services.rbac_bootstrap_service.is_protected_from_blocking', lambda u: False)
    monkeypatch.setattr('app.database.crud.subscription.deactivate_subscription', AsyncMock())

    disable_mock = AsyncMock(return_value=True)
    with patch('app.services.subscription_service.SubscriptionService.disable_remnawave_user', disable_mock):
        result = await UserService().block_user(AsyncMock(), user.id, admin_id=1)

    assert result is True
    disabled_ids = {call.args[0] for call in disable_mock.await_args_list}
    assert disabled_ids == {101, 796}, f'expected both main (101) and companion (796) disabled, got {disabled_ids}'
