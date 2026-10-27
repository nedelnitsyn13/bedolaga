"""Кнопки CisPay в боте совпадают с записью «Способов оплаты».

Баг 5.1.0: .env включал CisPay и кнопки рисовались, а оплату проверяла и
запись payment_method_configs (is_enabled=false) — «Способ оплаты недоступен».
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.keyboards.inline import get_payment_methods_keyboard
from app.services import payment_method_config_service as config_service


@pytest.fixture(autouse=True)
def cispay_env(monkeypatch):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'key')
    monkeypatch.setattr(settings, 'CISPAY_CARD_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_SBP_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', True)
    monkeypatch.setattr(config_service, '_display_name_overrides', {})
    monkeypatch.setattr(config_service, '_display_sort_orders', {})


def _cispay_callbacks(monkeypatch, availability) -> set[str]:
    monkeypatch.setattr(config_service, '_display_availability', availability)
    markup = get_payment_methods_keyboard(10000, 'ru')
    return {
        button.callback_data.split('|')[1]
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and 'cispay' in button.callback_data
    }


def test_all_buttons_without_a_saved_record(monkeypatch) -> None:
    assert _cispay_callbacks(monkeypatch, {}) == {'cispay_card', 'cispay_sbp', 'cispay_crypto'}


def test_disabled_record_hides_every_cispay_button(monkeypatch) -> None:
    assert _cispay_callbacks(monkeypatch, {'cispay': (False, {'card': True, 'sbp': True})}) == set()


def test_disabled_sub_option_hides_only_that_button(monkeypatch) -> None:
    callbacks = _cispay_callbacks(monkeypatch, {'cispay': (True, {'card': True, 'sbp': False})})
    # crypto нет в sub_options — как и в checkout-политике, считается включённым.
    assert callbacks == {'cispay_card', 'cispay_crypto'}


def test_is_display_option_enabled_defaults() -> None:
    assert config_service.is_display_option_enabled('unknown-method', 'card') is True
