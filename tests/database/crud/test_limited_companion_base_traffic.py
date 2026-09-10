"""``get_limited_companion_base_traffic_gb`` — единая точка расчёта базового
(без докупок) лимита трафика лимитного сервера-компаньона.

Чистая функция, БД не трогает — юнит-тест без фикстур.

Раньше все вызывающие места (синк с панелью, экраны бота/кабинета, автопокупка)
жёстко брали ``settings.LIMITED_COMPANION_TRAFFIC_GB`` напрямую, из-за чего
триальный компаньон получал фиксированную квоту (например 50 ГБ), даже если
сам триал был урезан до меньшего значения (например 10 ГБ) — компаньон триала
оказывался щедрее самого триала. Теперь для триальных подписок базовый лимит
компаньона зеркалит ``traffic_limit_gb`` основной подписки.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.database.crud.subscription import (
    get_limited_companion_base_traffic_gb,
    get_limited_companion_total_traffic_limit_gb,
)


def test_trial_subscription_uses_own_traffic_limit(monkeypatch) -> None:
    from app.database.crud import subscription as subscription_crud

    monkeypatch.setattr(subscription_crud.settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)
    trial_subscription = SimpleNamespace(is_trial=True, traffic_limit_gb=10)

    assert get_limited_companion_base_traffic_gb(trial_subscription) == 10


def test_trial_subscription_with_unlimited_traffic_stays_unlimited(monkeypatch) -> None:
    from app.database.crud import subscription as subscription_crud

    monkeypatch.setattr(subscription_crud.settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)
    # 0 ГБ на основной подписке — осознанный безлимит, не «не задано».
    trial_subscription = SimpleNamespace(is_trial=True, traffic_limit_gb=0)

    assert get_limited_companion_base_traffic_gb(trial_subscription) == 0


def test_paid_subscription_uses_fixed_settings_value(monkeypatch) -> None:
    from app.database.crud import subscription as subscription_crud

    monkeypatch.setattr(subscription_crud.settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)
    paid_subscription = SimpleNamespace(is_trial=False, traffic_limit_gb=200)

    assert get_limited_companion_base_traffic_gb(paid_subscription) == 50


def test_total_limit_adds_purchased_to_fixed_base(monkeypatch) -> None:
    from app.database.crud import subscription as subscription_crud

    monkeypatch.setattr(subscription_crud.settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)
    paid_subscription = SimpleNamespace(is_trial=False, traffic_limit_gb=200)

    assert get_limited_companion_total_traffic_limit_gb(paid_subscription, 20) == 70


def test_total_limit_stays_unlimited_when_base_is_unlimited(monkeypatch) -> None:
    """Регрессия: раньше ``база(0) + докупки`` превращало безлимитного триального
    компаньона в конечный лимит, равный только докупкам (например 20 ГБ вместо ∞) —
    докупка "портила" безлимит вместо того, чтобы быть в него бессмысленной."""
    from app.database.crud import subscription as subscription_crud

    monkeypatch.setattr(subscription_crud.settings, 'LIMITED_COMPANION_TRAFFIC_GB', 50)
    unlimited_trial_subscription = SimpleNamespace(is_trial=True, traffic_limit_gb=0)

    assert get_limited_companion_total_traffic_limit_gb(unlimited_trial_subscription, 20) == 0
