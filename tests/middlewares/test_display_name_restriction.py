"""Фильтр имён: «@» в имени больше не повод для блокировки.

Отчёт владельца 05.10: «Лидия Родионова @lrodionova» (свой ник, дописанный в
фамилию) получала «имя похоже на ссылку или служебный аккаунт». Блокируем
только ссылки и слова из DISPLAY_NAME_BANNED_KEYWORDS.
"""

from __future__ import annotations

import pytest

from app.middlewares import display_name_restriction
from app.middlewares.display_name_restriction import DisplayNameRestrictionMiddleware


@pytest.fixture
def middleware(monkeypatch):
    monkeypatch.setattr(display_name_restriction.settings, 'DISPLAY_NAME_BANNED_KEYWORDS', 'vpn\nvpns', raising=False)
    return DisplayNameRestrictionMiddleware()


@pytest.mark.parametrize('name', ['Лидия Родионова @lrodionova', 'Иван ＠ivan', 'lrodionova'])
def test_at_sign_alone_is_allowed(middleware, name):
    assert middleware._is_suspicious(name) is False


@pytest.mark.parametrize('name', ['Скидки t.me/promo', 'https://spam.example', 'mukamvpn', 'Best VPNs'])
def test_links_and_banned_keywords_still_blocked(middleware, name):
    assert middleware._is_suspicious(name) is True
