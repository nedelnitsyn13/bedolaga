"""Описание устройства из записи HWID панели: приложение и последнее обновление.

Пользователи пишут в поддержку «какое устройство удалить — Happ или INCY?»:
два одинаковых SM-A235F отличаются только приложением и временем активности.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.localization.texts import get_texts
from app.utils import device_client as dc


@pytest.mark.parametrize(
    ('user_agent', 'app'),
    [
        ('Happ/3.10.1/ios CFNetwork/1568.100.1 Darwin/24.0.0', 'Happ'),
        ('Happ/2.0.5/Android', 'Happ'),
        ('INCY/1.4.2 (Android 14)', 'INCY'),
        ('v2rayNG/1.10.7', 'v2rayNG'),
        ('v2raytun/android', 'v2RayTun'),
        ('HiddifyNext/2.5.7 (android) like ClashMeta v2ray sing-box', 'Hiddify'),
        ('Streisand/1.6.40', 'Streisand'),
        ('Shadowrocket/2620 CFNetwork/1498.700.2 Darwin/23.6.0', 'Shadowrocket'),
        ('FlClash X/v0.2.2 Platform/android', 'FlClashX'),
        ('clash-verge/v2.2.3', 'Clash Verge'),
        ('SFA/1.11.4 (Android 14)', 'sing-box'),
        ('Koala-Clash/1.0', 'Koala Clash'),
        ('SomeNewApp/0.1', 'SomeNewApp'),
        ('okhttp/4.12.0', None),
        ('Mozilla/5.0 (Windows NT 10.0)', None),
        ('Dart/3.5 (dart:io)', None),
        ('', None),
        (None, None),
        ('   ', None),
        ('/weird', None),
    ],
)
def test_client_app_name(user_agent, app):
    assert dc.client_app_name(user_agent) == app


def test_unknown_app_name_is_bounded():
    assert len(dc.client_app_name('A' * 200 + '/1.0')) <= 24


def test_device_details_maps_panel_fields():
    details = dc.device_details(
        {
            'hwid': 'abc',
            'platform': 'Android',
            'osVersion': '14',
            'deviceModel': 'SM-A235F',
            'userAgent': 'Happ/2.0.5/Android',
            'createdAt': '2026-10-19T08:00:00.000Z',
            'updatedAt': '2026-10-20T09:07:00.000Z',
        }
    )
    assert details == {
        'user_agent': 'Happ/2.0.5/Android',
        'app_name': 'Happ',
        'os_version': '14',
        'first_seen_at': '2026-10-19T08:00:00.000Z',
        'last_seen_at': '2026-10-20T09:07:00.000Z',
    }


def test_device_details_tolerates_old_panels():
    details = dc.device_details({'hwid': 'abc', 'platform': 'iOS', 'userAgent': None})
    assert set(details.values()) == {None}


def test_parse_panel_datetime():
    assert dc.parse_panel_datetime('2026-10-20T09:07:00.000Z') == datetime(2026, 10, 20, 9, 7, tzinfo=UTC)
    assert dc.parse_panel_datetime('2026-10-20T09:07:00') == datetime(2026, 10, 20, 9, 7, tzinfo=UTC)
    assert dc.parse_panel_datetime('вчера') is None
    assert dc.parse_panel_datetime(None) is None


def test_button_label_prefers_app():
    assert dc.device_button_label({'platform': 'Android', 'deviceModel': 'SM-A235F', 'userAgent': 'INCY/1.0'}) == (
        'INCY · SM-A235F'
    )
    assert dc.device_button_label({'platform': 'Android', 'deviceModel': 'SM-A235F'}) == 'Android - SM-A235F'
    assert dc.device_button_label({}) == 'Unknown - Unknown'


def test_bot_details_line(monkeypatch):
    monkeypatch.setattr('app.utils.timezone.get_local_timezone', lambda: UTC)
    texts = get_texts('ru')
    line = dc.format_device_details_line(
        {'userAgent': 'Happ/2.0.5/Android', 'updatedAt': '2026-10-20T09:07:00Z'}, texts
    )
    assert line == '📲 Happ · 🔄 обновлял подписку 20.10.2026 09:07'
    assert dc.format_device_details_line({'platform': 'Android'}, texts) == ''
