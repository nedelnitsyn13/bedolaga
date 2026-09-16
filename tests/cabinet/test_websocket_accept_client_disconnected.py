"""Кабинетный WS: обрыв клиента до accept() — не повод будить админа .error().

Баг: ``uvicorn.protocols.utils.ClientDisconnected`` при попытке принять
соединение (клиент закрыл вкладку/ушёл со страницы в узком окне между
WS-хендшейком и нашим ``accept()``) ловился общим ``except Exception`` и
логировался на уровне ``.error()`` — обычное сетевое поведение долетало
до админа как алерт «Ошибка во время работы».
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.routes import websocket as ws_module


class ClientDisconnected(Exception):
    """Дублирует имя uvicorn.protocols.utils.ClientDisconnected — проверка в коде идёт по имени класса,
    без импорта внутреннего модуля uvicorn, так что подделка с тем же именем ведёт себя идентично."""


def _fake_websocket(*, accept_side_effect) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host='203.0.113.5'),
        query_params={'token': 'tok'},
        accept=AsyncMock(side_effect=accept_side_effect),
    )


@pytest.mark.asyncio
async def test_client_disconnect_before_accept_is_logged_quietly(monkeypatch):
    monkeypatch.setattr(ws_module, 'verify_cabinet_ws_token', AsyncMock(return_value=(1, False)))
    fake_logger = MagicMock()
    monkeypatch.setattr(ws_module, 'logger', fake_logger)

    websocket = _fake_websocket(accept_side_effect=ClientDisconnected())

    await ws_module.cabinet_websocket_endpoint(websocket)

    fake_logger.error.assert_not_called()
    fake_logger.debug.assert_any_call('Cabinet WS: client disconnected before accept', client_host='203.0.113.5')


@pytest.mark.asyncio
async def test_other_accept_failures_still_log_as_errors(monkeypatch):
    monkeypatch.setattr(ws_module, 'verify_cabinet_ws_token', AsyncMock(return_value=(1, False)))
    fake_logger = MagicMock()
    monkeypatch.setattr(ws_module, 'logger', fake_logger)

    websocket = _fake_websocket(accept_side_effect=RuntimeError('boom'))

    await ws_module.cabinet_websocket_endpoint(websocket)

    fake_logger.error.assert_called_once()
    assert fake_logger.error.call_args.args[0] == 'Cabinet WS: Failed to accept from'
