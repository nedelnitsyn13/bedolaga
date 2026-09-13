"""Экран подписки в админке показывает лимитный профиль и умеет его синхронизировать.

Компаньон лимитного сервера — отдельный аккаунт панели со своей квотой трафика,
и до этого на экране подписки в админке его не было вообще: строка «Трафик»
описывает только основной аккаунт, так что по ней нельзя понять, что на лимитном
сервере у пользователя трафик уже кончился. Плюс `limited_companion_traffic_used_gb`
пишут только докупка и фоновый мониторинг — без кнопки синхронизации это число
могло сутками стоять нулём.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, settings
from app.handlers.admin import users as admin_users_module


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 101,
        'is_trial': False,
        'traffic_limit_gb': 200,
        'limited_companion_remnawave_id': 857,
        'limited_companion_traffic_used_gb': 12.34,
        'limited_companion_purchased_traffic_gb': 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def companion_enabled(monkeypatch):
    monkeypatch.setattr(Settings, 'is_limited_companion_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)
    monkeypatch.setattr(settings, 'LIMITED_COMPANION_SQUAD_UUID', 'squad-lim')


def test_block_shows_panel_id_and_companion_traffic(companion_enabled):
    block = admin_users_module._format_limited_companion_block(_subscription(), None)

    assert '🌐 <b>Лимитный профиль</b>' in block
    assert '<code>857</code>' in block
    # Своя квота компаньона (50), а не traffic_limit_gb основной подписки (200).
    assert '12.3/50 ГБ' in block


def test_block_reports_top_ups_on_top_of_the_base_quota(companion_enabled):
    block = admin_users_module._format_limited_companion_block(
        _subscription(limited_companion_purchased_traffic_gb=30), None
    )

    assert '12.3/80 ГБ' in block
    assert 'Из них докуплено:</b> 30 ГБ' in block


def test_block_mirrors_main_limit_for_trial_subscriptions(companion_enabled):
    """Триальный компаньон зеркалит лимит самого триала, а не LIMITED_COMPANION_TRAFFIC_GB."""
    block = admin_users_module._format_limited_companion_block(_subscription(is_trial=True, traffic_limit_gb=5), None)

    assert '12.3/5 ГБ' in block


def test_block_marks_an_unlimited_companion(companion_enabled):
    block = admin_users_module._format_limited_companion_block(
        _subscription(is_trial=True, traffic_limit_gb=0, limited_companion_purchased_traffic_gb=30), None
    )

    # Безлимит докупками не портится — иначе «∞» превратилось бы в «30 ГБ».
    assert '12.3/♾️ ГБ' in block


def test_block_is_empty_without_a_companion(companion_enabled):
    assert (
        admin_users_module._format_limited_companion_block(_subscription(limited_companion_remnawave_id=None), None)
        == ''
    )


def test_block_is_empty_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(Settings, 'is_limited_companion_enabled', lambda self: False)

    assert admin_users_module._format_limited_companion_block(_subscription(), None) == ''


@pytest.mark.asyncio
async def test_sync_button_resyncs_the_companion_and_redraws(monkeypatch):
    subscription = _subscription()
    resynced: list[int] = []
    rendered: list[tuple[int, int | None]] = []

    async def fake_resolve(_db, _user_id, subscription_id=None, tariff_id=None):
        return subscription

    async def fake_resync(_self, _db, subscription_arg):
        resynced.append(subscription_arg.id)
        return True

    async def fake_render(_callback, _db, user_id, subscription_id=None):
        rendered.append((user_id, subscription_id))
        return True

    monkeypatch.setattr(admin_users_module, '_resolve_admin_subscription', fake_resolve)
    monkeypatch.setattr(admin_users_module, '_render_user_subscription_overview', fake_render)
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.resync_limited_companion', fake_resync)

    callback = SimpleNamespace(data='admin_user_limsync_42_s101', answer=AsyncMock())

    await admin_users_module.admin_sync_limited_companion.__wrapped__.__wrapped__(
        callback, SimpleNamespace(id=1), AsyncMock()
    )

    assert resynced == [101]
    assert rendered == [(42, 101)]
    callback.answer.assert_awaited_once()
    assert '✅' in callback.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_sync_button_reports_a_failed_panel_sync(monkeypatch):
    subscription = _subscription()
    rendered: list = []

    async def fake_resolve(_db, _user_id, subscription_id=None, tariff_id=None):
        return subscription

    async def fake_resync(_self, _db, _subscription):
        return False

    async def fake_render(*_args, **_kwargs):
        rendered.append(True)
        return True

    monkeypatch.setattr(admin_users_module, '_resolve_admin_subscription', fake_resolve)
    monkeypatch.setattr(admin_users_module, '_render_user_subscription_overview', fake_render)
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.resync_limited_companion', fake_resync)

    callback = SimpleNamespace(data='admin_user_limsync_42', answer=AsyncMock())

    await admin_users_module.admin_sync_limited_companion.__wrapped__.__wrapped__(
        callback, SimpleNamespace(id=1), AsyncMock()
    )

    # Экран не перерисовываем: показывать несинхронизированные цифры как свежие хуже,
    # чем оставить старые и сказать об ошибке.
    assert rendered == []
    callback.answer.assert_awaited_once()
    assert '❌' in callback.answer.await_args.args[0]
