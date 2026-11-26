"""Кнопки и список способов оплаты в боте совпадают со «Способами оплаты» кабинета.

Раньше выключатели кабинета соблюдал только CisPay: в кабинете метод выключен,
а в боте кнопка «Lava», «Криптовалюта», «Карта (Резерв)» по-прежнему висела.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.keyboards.inline import get_payment_methods_keyboard
from app.services import payment_method_config_service as config_service
from app.services.payment_method_config_service import callback_method_target, is_callback_method_shown
from app.utils.payment_utils import get_available_payment_methods


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', True)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', '1:abc')
    monkeypatch.setattr(settings, 'LAVA_ENABLED', True)
    monkeypatch.setattr(settings, 'LAVA_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'LAVA_SECRET_KEY', 'k')
    monkeypatch.setattr(settings, 'LAVA_WEBHOOK_SECRET', 'w')
    monkeypatch.setattr(settings, 'LAVA_CARD_ENABLED', True)
    monkeypatch.setattr(settings, 'LAVA_SBP_ENABLED', True)
    monkeypatch.setattr(config_service, '_display_name_overrides', {})
    monkeypatch.setattr(config_service, '_display_sort_orders', {})


def _methods(monkeypatch, availability) -> set[str]:
    monkeypatch.setattr(config_service, '_display_availability', availability)
    markup = get_payment_methods_keyboard(10000, 'ru')
    return {
        button.callback_data.split('|')[1]
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith('topup_amount|')
    }


def test_everything_shown_without_saved_records(monkeypatch) -> None:
    assert {'stars', 'cryptobot', 'lava_card', 'lava_sbp'} <= _methods(monkeypatch, {})


def test_disabled_methods_and_sub_options_are_hidden(monkeypatch) -> None:
    shown = _methods(
        monkeypatch,
        {
            'cryptobot': (False, None),
            'lava': (True, {'card': True, 'sbp': False}),
            'telegram_stars': (True, None),
        },
    )
    assert 'cryptobot' not in shown
    assert 'lava_sbp' not in shown
    assert {'lava_card', 'stars'} <= shown


def test_text_list_matches_the_buttons(monkeypatch) -> None:
    monkeypatch.setattr(config_service, '_display_availability', {'cryptobot': (False, None)})
    ids = {method['id'] for method in get_available_payment_methods()}
    assert 'cryptobot' not in ids


def test_only_disabled_methods_leave_the_unavailable_notice(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', False)
    monkeypatch.setattr(settings, 'LAVA_ENABLED', False)
    monkeypatch.setattr(config_service, '_display_availability', {'cryptobot': (False, None)})
    markup = get_payment_methods_keyboard(10000, 'ru')
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert not any(c.startswith('topup_amount|') for c in callbacks)
    assert 'payment_methods_unavailable' in callbacks or 'topup_support' in callbacks


@pytest.mark.parametrize(
    ('callback', 'target'),
    [
        ('stars', ('telegram_stars', None)),
        ('yookassa', ('yookassa', 'card')),
        ('yookassa_sbp', ('yookassa', 'sbp')),
        ('platega_m11', ('platega', '11')),
        ('cashera_m_5', ('cashera', None)),
        ('lava_card', ('lava', 'card')),
        ('donut_sbp_qr', ('donut', 'sbp_qr')),
        ('freekassa_sbp', ('freekassa_sbp', None)),
        ('cryptobot', ('cryptobot', None)),
    ],
)
def test_callback_maps_to_record_and_sub_option(monkeypatch, callback, target) -> None:
    monkeypatch.setattr(
        config_service,
        '_display_availability',
        dict.fromkeys(('lava', 'donut', 'freekassa', 'freekassa_sbp', 'cryptobot', 'platega'), (True, None)),
    )
    assert callback_method_target(callback) == target


def test_support_is_always_shown() -> None:
    assert is_callback_method_shown('support') is True
