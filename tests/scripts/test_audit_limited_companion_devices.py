"""Классификация расхождений между устройствами основного аккаунта и компаньона.

Скрипт печатает по каждому расхождению вероятную причину, и человек по ней
решает, что чинить. Неверная подсказка отправит чинить не то: «лимит»
лечится пересинхронизацией, а «удалили вручную» — не лечится вообще.
Панель отклоняет регистрацию HWID ровно в одном случае — аккаунт упёрся
в свой `hwidDeviceLimit`, поэтому только этот вывод скрипт делает уверенно.
"""

from __future__ import annotations

from scripts.audit_limited_companion_devices import Divergence


def _divergence(**overrides) -> Divergence:
    base = dict(
        subscription_id=101,
        user_id=7,
        main_id=28,
        companion_id=759,
        main_hwids={'a', 'b', 'c'},
        companion_hwids={'a', 'b'},
        main_limit=10,
        companion_limit=10,
    )
    base.update(overrides)
    return Divergence(**base)


def test_blames_a_full_companion_when_the_device_is_only_on_main():
    item = _divergence(companion_hwids={'a', 'b'}, companion_limit=2)

    assert item.only_on_main == {'c'}
    assert 'компаньон забит под лимит' in item.reason()


def test_blames_a_full_main_when_the_device_is_only_on_the_companion():
    item = _divergence(main_hwids={'a', 'b'}, companion_hwids={'a', 'b', 'c'}, main_limit=2)

    assert item.only_on_companion == {'c'}
    assert 'основной забит под лимит' in item.reason()


def test_reports_drifted_limits_when_neither_side_is_full():
    item = _divergence(companion_limit=3)

    # Компаньон не заполнен (2 из 3), значит отказ по лимиту ни при чём —
    # но расходящиеся лимиты сами по себе стоит показать.
    assert 'лимиты разъехались' in item.reason()
    assert 'основной=10' in item.reason()
    assert 'компаньон=3' in item.reason()


def test_admits_when_nothing_explains_the_gap():
    """Лимиты одинаковые и обе стороны свободны — панель отказать не могла."""
    item = _divergence()

    assert 'причина не установлена' in item.reason()


def test_does_not_blame_a_limit_the_panel_never_reported():
    """hwidDeviceLimit=None — безлимит: обвинять его в отказе нельзя."""
    item = _divergence(companion_limit=None, main_limit=None)

    assert 'причина не установлена' in item.reason()
