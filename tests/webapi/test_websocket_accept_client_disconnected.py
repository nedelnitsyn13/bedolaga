"""WebAPI WS: обрыв клиента до accept() — не повод будить админа .error().

Тот же баг, что в кабинетном WS (см.
tests/cabinet/test_websocket_accept_client_disconnected.py): узкое окно
между WS-хендшейком и accept() — клиент успевает отвалиться, и это
штатное сетевое поведение, а не ошибка, которую нужно репортить на .error().
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.webapi.routes import websocket as ws_module


class ClientDisconnected(Exception):
    """Дублирует имя uvicorn.protocols.utils.ClientDisconnected — проверка в коде идёт по имени класса."""


def _fake_websocket(*, accept_side_effect) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host='203.0.113.5'),
        query_params={'token': 'tok'},
        accept=AsyncMock(side_effect=accept_side_effect),
    )


@pytest.mark.asyncio
async def test_client_disconnect_before_accept_is_logged_quietly(monkeypatch):
    monkeypatch.setattr(ws_module, 'verify_websocket_token', AsyncMock(return_value=True))
    fake_logger = MagicMock()
    monkeypatch.setattr(ws_module, 'logger', fake_logger)

    websocket = _fake_websocket(accept_side_effect=ClientDisconnected())

    await ws_module.websocket_endpoint(websocket)

    fake_logger.error.assert_not_called()
    fake_logger.debug.assert_any_call('WebSocket: client disconnected before accept', client_host='203.0.113.5')


@pytest.mark.asyncio
async def test_other_accept_failures_still_log_as_errors(monkeypatch):
    monkeypatch.setattr(ws_module, 'verify_websocket_token', AsyncMock(return_value=True))
    fake_logger = MagicMock()
    monkeypatch.setattr(ws_module, 'logger', fake_logger)

    websocket = _fake_websocket(accept_side_effect=RuntimeError('boom'))

    await ws_module.websocket_endpoint(websocket)

    fake_logger.error.assert_called_once()
    assert fake_logger.error.call_args.args[0] == 'WebSocket: Failed to accept connection from'
