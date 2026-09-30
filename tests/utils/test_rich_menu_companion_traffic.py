"""_companion_traffic_text() должна знать про обе архитектуры лимитного сервера.

Отчёт владельца 30.09: в rich-меню на главной («📱 Подписки») строка
«🌐 Лимитный сервер» не появлялась, хотя в «Мои подписки»
(handlers/subscription/my_subscriptions.py, уже поддерживает обе схемы) —
появлялась. Причина: _companion_traffic_text() проверяла только legacy
companion-аккаунт (limited_companion_remnawave_id), а подписка владельца
работает на новой архитектуре LIMITED squad (tariff.limited_traffic_enabled).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.utils.rich_menu import _companion_traffic_text


def _texts():
    from app.localization.texts import Texts

    return Texts('ru')


def test_limited_squad_architecture_shows_traffic():
    tariff = SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=50)
    subscription = SimpleNamespace(
        tariff=tariff,
        limited_traffic_used_gb=1.5,
        limited_companion_remnawave_id=None,
    )

    assert _companion_traffic_text(subscription, _texts()) == '1.5/50+ ГБ'


def test_limited_squad_unlimited_base():
    tariff = SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=0)
    subscription = SimpleNamespace(
        tariff=tariff,
        limited_traffic_used_gb=12.0,
        limited_companion_remnawave_id=None,
    )

    assert _companion_traffic_text(subscription, _texts()) == '∞'


def test_legacy_companion_still_works(monkeypatch):
    from app.utils import rich_menu

    monkeypatch.setattr(rich_menu.settings, 'LIMITED_COMPANION_ENABLED', True, raising=False)
    monkeypatch.setattr(rich_menu.settings, 'LIMITED_COMPANION_SQUAD_UUID', 'squad-uuid', raising=False)

    tariff = SimpleNamespace(limited_traffic_enabled=False, limited_base_traffic_gb=0)
    subscription = SimpleNamespace(
        tariff=tariff,
        is_trial=False,
        limited_traffic_used_gb=0,
        limited_companion_remnawave_id=777,
        limited_companion_purchased_traffic_gb=10,
        limited_companion_traffic_used_gb=2.0,
    )

    result = _companion_traffic_text(subscription, _texts())
    assert result is not None
    assert result.startswith('2 ГБ/')


def test_neither_architecture_returns_none():
    tariff = SimpleNamespace(limited_traffic_enabled=False, limited_base_traffic_gb=0)
    subscription = SimpleNamespace(
        tariff=tariff,
        limited_traffic_used_gb=0,
        limited_companion_remnawave_id=None,
    )

    assert _companion_traffic_text(subscription, _texts()) is None
