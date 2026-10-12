"""Ссылка «Управлять уведомлениями» в подвале и промо, выключенное по умолчанию.

Отчёт владельца 12.10: хостинг (Timeweb) спросил, как получатель управляет
письмами и подтверждает согласие. Ответ должен быть виден в самом письме —
отсюда строка со ссылкой на настройки уведомлений в каждом письме. Промо
(реклама по 38-ФЗ) требует согласия, а включённая по умолчанию галочка им не
является, поэтому без явного выбора промо теперь выключено.
"""

from types import SimpleNamespace

from app.cabinet.services import email_layout
from app.cabinet.services.email_layout import DEFAULT_EMAIL_LAYOUT, render_email_layout


def test_footer_links_to_notification_settings(monkeypatch):
    monkeypatch.setattr(email_layout.settings, 'CABINET_URL', 'https://cab.example.com/', raising=False)

    html = render_email_layout(DEFAULT_EMAIL_LAYOUT, 'ru', {'content': '<p>код 1234</p>'})

    assert 'href="https://cab.example.com/profile"' in html
    assert 'Управлять уведомлениями можно в настройках личного кабинета' in html


def test_footer_link_is_localized(monkeypatch):
    monkeypatch.setattr(email_layout.settings, 'CABINET_URL', 'https://cab.example.com', raising=False)

    html = render_email_layout(DEFAULT_EMAIL_LAYOUT, 'en', {'content': 'x'})

    assert 'You can manage notifications in your account settings' in html


def test_footer_has_no_link_without_cabinet_url(monkeypatch):
    """Без CABINET_URL вести некуда — строку не рисуем, а не даём битую ссылку."""
    monkeypatch.setattr(email_layout.settings, 'CABINET_URL', '', raising=False)

    html = render_email_layout(DEFAULT_EMAIL_LAYOUT, 'ru', {'content': 'x'})

    assert 'Управлять уведомлениями' not in html
    assert '{manage_notifications_block}' not in html


def test_footer_link_rejects_non_http_cabinet_url(monkeypatch):
    monkeypatch.setattr(email_layout.settings, 'CABINET_URL', 'javascript:alert(1)', raising=False)

    html = render_email_layout(DEFAULT_EMAIL_LAYOUT, 'ru', {'content': 'x'})

    assert 'javascript:' not in html


def _user(notification_settings):
    return SimpleNamespace(notification_settings=notification_settings)


def test_promo_is_off_by_default(monkeypatch):
    from app.utils import notification_prefs

    monkeypatch.setattr(notification_prefs.settings, 'PROMO_OFFERS_DEFAULT_ENABLED', False, raising=False)

    assert notification_prefs.is_promo_offers_enabled(_user(None)) is False
    assert notification_prefs.is_promo_offers_enabled(_user({})) is False
    # Явное согласие пользователя сильнее умолчания.
    assert notification_prefs.is_promo_offers_enabled(_user({'promo_offers_enabled': True})) is True
    # Новости — не реклама, их умолчание не меняется.
    assert notification_prefs.is_news_enabled(_user(None)) is True


def test_cabinet_shows_the_same_promo_default(monkeypatch):
    """Кабинет не должен показывать включённое промо тому, кому его не шлют."""
    from app.cabinet.routes.notifications import _get_notification_settings
    from app.utils import notification_prefs

    monkeypatch.setattr(notification_prefs.settings, 'PROMO_OFFERS_DEFAULT_ENABLED', False, raising=False)
    data = _get_notification_settings(_user(None))
    assert data['promo_offers_enabled'] is False
    assert data['news_enabled'] is True
    assert data['subscription_expiry_days'] == 3

    monkeypatch.setattr(notification_prefs.settings, 'PROMO_OFFERS_DEFAULT_ENABLED', True, raising=False)
    assert _get_notification_settings(_user(None))['promo_offers_enabled'] is True
    assert _get_notification_settings(_user({'promo_offers_enabled': False}))['promo_offers_enabled'] is False
