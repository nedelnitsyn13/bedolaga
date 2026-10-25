"""Бот: «Обновить трафик» лимитного сервера — то же, что кнопка в кабинете.

Кнопка на экране докупки, экран подтверждения с ценой и списание только
после подтверждения — через общий ``perform_paid_limited_reset``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.subscription import traffic as handlers
from app.keyboards.inline import get_add_traffic_limited_keyboard
from app.services.limited_squad_service import LIMITED_RESET_OK, LIMITED_RESET_PANEL_UNAVAILABLE


def _tariff():
    return SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=50, limited_squad_uuids=['s'])


def _callback(data: str):
    return SimpleNamespace(data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())


def _user(balance: int):
    return SimpleNamespace(id=1, telegram_id=10, language='ru', balance_kopeks=balance, restriction_subscription=False)


@pytest.fixture
def subscription(monkeypatch):
    sub = SimpleNamespace(id=42, tariff=_tariff())

    async def _resolve(callback, db_user, db, state=None):
        return sub, 42

    monkeypatch.setattr(handlers, '_resolve_subscription', _resolve)
    monkeypatch.setattr(handlers, 'limited_reset_quote', lambda user, tariff: (36000, 45000, 20))
    return sub


def _buttons(markup):
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def test_keyboard_puts_the_reset_button_first_with_the_discount(monkeypatch) -> None:
    monkeypatch.setattr(
        type(settings), 'get_traffic_topup_packages', lambda self: [{'gb': 10, 'price': 9000, 'enabled': True}]
    )
    markup = get_add_traffic_limited_keyboard(
        'ru', 42, None, 0, reset_price_kopeks=36000, reset_base_price_kopeks=45000, reset_discount_percent=20
    )

    text, data = _buttons(markup)[0]
    assert data == 'rlt:42'
    assert '360' in text and '20%' in text and '450' in text


def test_keyboard_has_no_reset_button_without_a_price(monkeypatch) -> None:
    monkeypatch.setattr(type(settings), 'get_traffic_topup_packages', lambda self: [])
    markup = get_add_traffic_limited_keyboard('ru', 42)

    assert all(not data.startswith('rlt:') for _, data in _buttons(markup))


async def test_first_press_only_asks_for_confirmation(subscription, monkeypatch) -> None:
    perform = AsyncMock()
    monkeypatch.setattr(handlers, 'perform_paid_limited_reset', perform)
    callback = _callback('rlt:42')

    await handlers.confirm_limited_traffic_reset(callback, _user(50000), db=None)

    perform.assert_not_called()
    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert [data for _, data in _buttons(markup)] == ['rltc:42', 'blt:42']
    assert '<s>' in callback.message.edit_text.call_args.args[0]


async def test_confirmation_with_low_balance_offers_a_top_up(subscription) -> None:
    callback = _callback('rlt:42')

    await handlers.confirm_limited_traffic_reset(callback, _user(1000), db=None)

    assert 'Недостаточно средств' in callback.message.edit_text.call_args.args[0]


async def test_confirm_runs_the_shared_reset(subscription, monkeypatch) -> None:
    user = _user(50000)
    perform = AsyncMock(return_value=LIMITED_RESET_OK)
    monkeypatch.setattr(handlers, 'perform_paid_limited_reset', perform)
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(handlers, 'get_effective_limited_traffic_limit_gb', AsyncMock(return_value=50))
    db = SimpleNamespace(refresh=AsyncMock())
    callback = _callback('rltc:42')

    await handlers.reset_limited_traffic(callback, user, db)

    perform.assert_awaited_once_with(db, user, subscription, subscription.tariff, 36000)
    assert 'обновлён' in callback.message.edit_text.call_args.args[0]


async def test_panel_down_reports_no_charge(subscription, monkeypatch) -> None:
    user = _user(50000)
    monkeypatch.setattr(handlers, 'perform_paid_limited_reset', AsyncMock(return_value=LIMITED_RESET_PANEL_UNAVAILABLE))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    callback = _callback('rltc:42')

    await handlers.reset_limited_traffic(callback, user, db=None)

    callback.message.edit_text.assert_not_called()
    assert 'не списаны' in callback.answer.call_args.args[0]
