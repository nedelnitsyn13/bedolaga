"""Трафик лимитного сервера-компаньона в опциях покупки тарифа (кабинет).

До покупки в кабинете было видно только основной трафик тарифа — фактически
компаньон (LIMITED_COMPANION_SQUAD_UUID) оставался невидим до момента покупки,
хотя бот эту информацию уже показывает в списке/карточке тарифа.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules import purchase as purchase_routes


def _tariff(**overrides) -> SimpleNamespace:
    values = {
        'id': 1,
        'name': 'Стандарт',
        'description': None,
        'is_active': True,
        'is_highlighted': False,
        'tier_level': 2,
        'traffic_limit_gb': 0,
        'device_limit': 5,
        'device_price_kopeks': 0,
        'allowed_squads': [],
        'server_traffic_limits': {},
        'period_prices': {},
        'highlight_period_days': None,
        'custom_days_enabled': False,
        'price_per_day_kopeks': 0,
        'min_days': 1,
        'max_days': 365,
        'custom_traffic_enabled': False,
        'traffic_price_per_gb_kopeks': 0,
        'min_traffic_gb': 1,
        'max_traffic_gb': 1000,
        'traffic_topup_enabled': False,
        'max_topup_traffic_gb': 0,
        'is_daily': False,
        'daily_price_kopeks': 0,
        'traffic_reset_mode': None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_companion_traffic_uses_tariff_override(monkeypatch):
    settings_cls = type(purchase_routes.settings)
    monkeypatch.setattr(settings_cls, 'is_limited_companion_enabled', lambda self: True)
    monkeypatch.setattr(purchase_routes.settings, 'LIMITED_COMPANION_SQUAD_UUID', 'companion-squad')
    monkeypatch.setattr(purchase_routes.settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)

    tariff = _tariff(server_traffic_limits={'companion-squad': {'traffic_limit_gb': 250}})

    response = await purchase_routes._build_tariff_response(db=None, tariff=tariff)

    assert response['limited_companion_traffic_gb'] == 250
    assert response['limited_companion_traffic_label'] == '250 ГБ'


@pytest.mark.asyncio
async def test_companion_traffic_falls_back_to_global_setting(monkeypatch):
    settings_cls = type(purchase_routes.settings)
    monkeypatch.setattr(settings_cls, 'is_limited_companion_enabled', lambda self: True)
    monkeypatch.setattr(purchase_routes.settings, 'LIMITED_COMPANION_SQUAD_UUID', 'companion-squad')
    monkeypatch.setattr(purchase_routes.settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)

    tariff = _tariff()  # server_traffic_limits={} — нет override

    response = await purchase_routes._build_tariff_response(db=None, tariff=tariff)

    assert response['limited_companion_traffic_gb'] == 50
    assert response['limited_companion_traffic_label'] == '50 ГБ'


@pytest.mark.asyncio
async def test_companion_traffic_absent_when_feature_disabled(monkeypatch):
    settings_cls = type(purchase_routes.settings)
    monkeypatch.setattr(settings_cls, 'is_limited_companion_enabled', lambda self: False)

    tariff = _tariff()

    response = await purchase_routes._build_tariff_response(db=None, tariff=tariff)

    assert 'limited_companion_traffic_gb' not in response
    assert 'limited_companion_traffic_label' not in response
