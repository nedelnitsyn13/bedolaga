"""Человекочитаемое описание устройства из записи HWID панели Remnawave.

Панель при каждом запросе подписки с HWID обновляет у устройства ``userAgent``
и ``updatedAt`` (upsert), а ``createdAt`` — момент первого подключения. Отсюда:

- по ``userAgent`` видно приложение (Happ, INCY, v2rayNG…) — пользователи
  часто не понимают, какое из двух одинаковых устройств — какое приложение;
- ``updatedAt`` — когда устройство последний раз обновляло подписку.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any


# Префикс User-Agent (в нижнем регистре, без пробелов/дефисов) → имя приложения.
# Длинные префиксы проверяются раньше коротких (flclashx раньше flclash).
_KNOWN_APPS: dict[str, str] = {
    'happ': 'Happ',
    'incy': 'INCY',
    'v2rayng': 'v2rayNG',
    'v2raytun': 'v2RayTun',
    'v2box': 'V2Box',
    'hiddifynext': 'Hiddify',
    'hiddify': 'Hiddify',
    'streisand': 'Streisand',
    'shadowrocket': 'Shadowrocket',
    'stash': 'Stash',
    'flclashx': 'FlClashX',
    'flclash': 'FlClash',
    'clashmetaforandroid': 'Clash Meta',
    'clashverge': 'Clash Verge',
    'clashmi': 'Clash Mi',
    'koalaclash': 'Koala Clash',
    'mihomoparty': 'Mihomo Party',
    'mihomo': 'Mihomo',
    'karing': 'Karing',
    'nekobox': 'NekoBox',
    'nekoray': 'NekoRay',
    'throne': 'Throne',
    'prizrakbox': 'Prizrak-Box',
    'exclave': 'Exclave',
    'husi': 'Husi',
    'foxray': 'FoXray',
    'singbox': 'sing-box',
    'sfa': 'sing-box',
    'sfi': 'sing-box',
    'sfm': 'sing-box',
    'sft': 'sing-box',
    'loon': 'Loon',
    'quantumult': 'Quantumult X',
    'surge': 'Surge',
    'outline': 'Outline',
}
_KNOWN_ORDER = sorted(_KNOWN_APPS, key=len, reverse=True)

# Общие HTTP-клиенты и браузеры: по ним приложение не определить.
_GENERIC = {
    'mozilla',
    'okhttp',
    'dart',
    'dalvik',
    'cfnetwork',
    'darwin',
    'go',
    'gohttpclient',
    'curl',
    'wget',
    'pythonrequests',
    'python',
    'axios',
    'node',
    'java',
    'apachehttpclient',
    'ktor',
}

_TOKEN_RE = re.compile(r'^[A-Za-z][A-Za-z0-9 ._+-]{0,30}')
_MAX_UNKNOWN_LEN = 24


def _norm(value: str) -> str:
    return re.sub(r'[\s._+-]', '', value).lower()


def client_app_name(user_agent: str | None) -> str | None:
    """Имя приложения по User-Agent; None — если определить нельзя."""
    if not user_agent:
        return None
    ua = user_agent.strip()
    if not ua:
        return None
    head = re.split(r'[/(;]', ua, maxsplit=1)[0].strip()
    normalized = _norm(head)
    for prefix in _KNOWN_ORDER:
        if normalized.startswith(prefix):
            return _KNOWN_APPS[prefix]
    if not normalized or normalized in _GENERIC:
        return None
    match = _TOKEN_RE.match(head)
    if not match:
        return None
    return match.group(0).strip()[:_MAX_UNKNOWN_LEN] or None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_panel_datetime(value: Any) -> datetime | None:
    """ISO-время панели (всегда UTC) → aware datetime; мусор → None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = _str_or_none(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def device_details(device: dict[str, Any]) -> dict[str, str | None]:
    """Поля для отображения: приложение, ОС, первое подключение, последнее обновление."""
    user_agent = _str_or_none(device.get('userAgent'))
    return {
        'user_agent': user_agent,
        'app_name': client_app_name(user_agent),
        'os_version': _str_or_none(device.get('osVersion')),
        'first_seen_at': _str_or_none(device.get('createdAt')),
        'last_seen_at': _str_or_none(device.get('updatedAt') or device.get('lastSeen')),
    }


def device_button_label(device: dict[str, Any]) -> str:
    """Подпись устройства без пользовательского имени: «Happ · SM-A235F»."""
    platform = _str_or_none(device.get('platform')) or 'Unknown'
    model = _str_or_none(device.get('deviceModel'))
    app = client_app_name(_str_or_none(device.get('userAgent')))
    if app:
        return f'{app} · {model or platform}'
    return f'{platform} - {model or "Unknown"}'


def format_device_details_line(device: dict[str, Any], texts: Any) -> str:
    """Строка под устройством в боте: приложение и последнее обновление подписки."""
    from app.utils.timezone import format_local_datetime

    details = device_details(device)
    parts: list[str] = []
    if details['app_name']:
        parts.append(texts.t('DEVICE_APP', '📲 {app}').format(app=details['app_name']))
    last_seen = parse_panel_datetime(details['last_seen_at'])
    if last_seen:
        parts.append(
            texts.t('DEVICE_LAST_SEEN', '🔄 обновлял подписку {date}').format(
                date=format_local_datetime(last_seen, '%d.%m.%Y %H:%M')
            )
        )
    return ' · '.join(parts)
