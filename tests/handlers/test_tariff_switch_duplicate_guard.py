"""Смена тарифа (бот, кнопки) не должна порождать дубликат живой подписки.

См. также ``tests/webapi/test_miniapp_tariff_switch_duplicate_guard.py`` —
тот же баг, тот же фикс, три разных места входа (мгновенное переключение,
переключение на суточный тариф, админская смена тарифа), которые не делали
проверку «у пользователя уже есть живая подписка на целевой тариф» перед тем,
как выставить ``subscription.tariff_id``. Без неё commit падал на partial
unique index ``uq_subscriptions_user_tariff_active``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.subscription.tariff_purchase as tp
from app.config import Settings
from app.database.models import Tariff


SOURCE_TARIFF = Tariff(
    id=1,
    name='Источник',
    is_active=True,
    is_daily=False,
    period_prices={'30': 10000},
    daily_price_kopeks=0,
    traffic_limit_gb=0,
    device_limit=1,
)

DAILY_TARGET_TARIFF = Tariff(
    id=2,
    name='Суточный (уже занят)',
    is_active=True,
    is_daily=True,
    daily_price_kopeks=100,
    traffic_limit_gb=0,
    device_limit=1,
)

INSTANT_TARGET_TARIFF = Tariff(
    id=3,
    name='Целевой (уже занят)',
    is_active=True,
    is_daily=False,
    period_prices={'30': 15000},
    daily_price_kopeks=0,
    traffic_limit_gb=0,
    device_limit=2,
)


def _mk_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _mk_user() -> MagicMock:
    db_user = MagicMock()
    db_user.id = 1
    db_user.language = 'ru'
    db_user.balance_kopeks = 1_000_000
    return db_user


def _mk_state(data: dict | None = None) -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=data or {})
    return state


def _source_sub() -> MagicMock:
    sub = MagicMock()
    sub.id = 10
    sub.tariff_id = SOURCE_TARIFF.id
    sub.end_date = datetime.now(UTC) + timedelta(days=15)
    return sub


def _existing_target_sub(sub_id: int = 99) -> MagicMock:
    """Другая живая подписка того же пользователя, уже занявшая целевой тариф."""
    existing = MagicMock()
    existing.id = sub_id
    return existing


@pytest.fixture(autouse=True)
def multi_tariff_enabled(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)


async def test_instant_switch_refuses_already_owned_target(monkeypatch):
    """confirm_instant_switch: цель занята другой подпиской — блокируем ДО lock_user_for_pricing."""
    import app.database.crud.subscription as sub_crud
    import app.database.crud.user as user_crud

    async def fake_get_tariff(db, tariff_id):
        return {SOURCE_TARIFF.id: SOURCE_TARIFF, INSTANT_TARGET_TARIFF.id: INSTANT_TARGET_TARIFF}.get(tariff_id)

    monkeypatch.setattr(tp, 'get_tariff_by_id', fake_get_tariff)

    sub = _source_sub()
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(sub, sub.id)))

    lookup = AsyncMock(return_value=_existing_target_sub())
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)

    lock_spy = AsyncMock(return_value=_mk_user())
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', lock_spy)

    db_user = _mk_user()
    callback = _mk_callback(f'instant_sw_confirm:{INSTANT_TARGET_TARIFF.id}')
    await tp.confirm_instant_switch(callback, db_user, AsyncMock(), _mk_state())

    assert lookup.await_args.args[1:] == (db_user.id, INSTANT_TARGET_TARIFF.id)
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    lock_spy.assert_not_called()  # короткое замыкание — до пересчёта цены не дошли


async def test_instant_switch_allows_unowned_target(monkeypatch):
    """Контроль: если целевой тариф свободен, guard не мешает — lock_user_for_pricing вызывается."""
    import app.database.crud.subscription as sub_crud
    import app.database.crud.user as user_crud

    async def fake_get_tariff(db, tariff_id):
        return {SOURCE_TARIFF.id: SOURCE_TARIFF, INSTANT_TARGET_TARIFF.id: INSTANT_TARGET_TARIFF}.get(tariff_id)

    monkeypatch.setattr(tp, 'get_tariff_by_id', fake_get_tariff)

    sub = _source_sub()
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(sub, sub.id)))

    lookup = AsyncMock(return_value=None)  # тариф свободен
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)

    db_user = _mk_user()
    lock_spy = AsyncMock(return_value=db_user)
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', lock_spy)

    callback = _mk_callback(f'instant_sw_confirm:{INSTANT_TARGET_TARIFF.id}')
    await tp.confirm_instant_switch(callback, db_user, AsyncMock(), _mk_state())

    lock_spy.assert_awaited_once()  # дошли дальше guard'а


async def test_daily_switch_refuses_already_owned_target(monkeypatch):
    """confirm_daily_tariff_switch: тот же guard, но после расчёта цены/баланса."""
    import app.database.crud.subscription as sub_crud
    import app.database.crud.user as user_crud
    from app.services.pricing_engine import pricing_engine

    async def fake_get_tariff(db, tariff_id):
        return {SOURCE_TARIFF.id: SOURCE_TARIFF, DAILY_TARGET_TARIFF.id: DAILY_TARGET_TARIFF}.get(tariff_id)

    monkeypatch.setattr(tp, 'get_tariff_by_id', fake_get_tariff)

    db_user = _mk_user()
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=db_user))
    monkeypatch.setattr(
        pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=0, breakdown={})),
    )

    sub = _source_sub()
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(sub, sub.id)))

    lookup = AsyncMock(return_value=_existing_target_sub())
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)

    charge_spy = AsyncMock()
    monkeypatch.setattr(tp, 'subtract_user_balance', charge_spy)

    callback = _mk_callback(f'daily_sw_confirm:{DAILY_TARGET_TARIFF.id}')
    await tp.confirm_daily_tariff_switch(callback, db_user, AsyncMock(), _mk_state())

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    charge_spy.assert_not_called()  # до списания не дошли


async def test_admin_tariff_change_refuses_already_owned_target(monkeypatch):
    """confirm_admin_tariff_change: у выбранного юзера уже есть живая подписка на этот тариф."""
    import app.database.crud.subscription as sub_crud
    from app.handlers.admin import users as admin_users_module

    target_user = SimpleNamespace(id=42)
    monkeypatch.setattr(admin_users_module, 'get_user_by_id', AsyncMock(return_value=target_user))

    async def fake_get_tariff(db, tariff_id):
        return {SOURCE_TARIFF.id: SOURCE_TARIFF, INSTANT_TARGET_TARIFF.id: INSTANT_TARGET_TARIFF}.get(tariff_id)

    monkeypatch.setattr(admin_users_module, 'get_tariff_by_id', fake_get_tariff)

    sub = _source_sub()
    monkeypatch.setattr(admin_users_module, '_resolve_admin_subscription', AsyncMock(return_value=sub))

    lookup = AsyncMock(return_value=_existing_target_sub())
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)

    db = AsyncMock()
    callback = _mk_callback(f'admin_sub_tariff_confirm_{INSTANT_TARGET_TARIFF.id}_42')
    admin = SimpleNamespace(id=1)

    await admin_users_module.confirm_admin_tariff_change.__wrapped__.__wrapped__(callback, admin, db)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    db.commit.assert_not_called()  # мутация даже не начиналась
