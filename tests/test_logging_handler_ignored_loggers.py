"""TelegramNotifierProcessor не должен репортить админу штатные,
самовосстанавливающиеся события сторонних библиотек.

Баг: aiogram.dispatcher.error() — единственный .error()-вызов во всей
библиотеке aiogram, "Failed to fetch updates" при сбое getUpdates (сеть,
Telegram Bad Gateway). Сам aiogram после него уходит в backoff и ретраит
бесконечно (см. docstring Dispatcher._listen_updates: "you may not worry
that the polling will stop working") — но этот логгер не входил в
IGNORED_LOGGER_PREFIXES, и структлог-процессор пересылал его в админ-чат
как алерт на ровном месте.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.logging_handler import IGNORED_LOGGER_PREFIXES, TelegramNotifierProcessor


def test_aiogram_dispatcher_prefix_is_ignored():
    assert any('aiogram.dispatcher'.startswith(prefix) for prefix in IGNORED_LOGGER_PREFIXES)


def test_aiogram_dispatcher_error_is_not_forwarded(monkeypatch):
    processor = TelegramNotifierProcessor()
    processor.set_bot(MagicMock())
    schedule_mock = MagicMock()
    monkeypatch.setattr(processor, '_schedule_send', schedule_mock)

    event_dict = {
        'level': 'error',
        'logger': 'aiogram.dispatcher',
        'event': 'Failed to fetch updates - TelegramServerError: Bad Gateway',
    }
    result = processor(None, 'error', dict(event_dict))

    schedule_mock.assert_not_called()
    assert result['event'] == event_dict['event']


def test_other_error_loggers_are_still_forwarded(monkeypatch):
    processor = TelegramNotifierProcessor()
    processor.set_bot(MagicMock())
    schedule_mock = MagicMock()
    monkeypatch.setattr(processor, '_schedule_send', schedule_mock)
    monkeypatch.setattr('app.logging_handler._record_error_event', lambda *a, **kw: 'uid')

    event_dict = {
        'level': 'error',
        'logger': 'app.services.some_module',
        'event': 'Что-то реально сломалось',
    }
    processor(None, 'error', dict(event_dict))

    schedule_mock.assert_called_once()
