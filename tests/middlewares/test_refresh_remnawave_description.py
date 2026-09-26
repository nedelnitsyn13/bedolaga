"""«User not found» при синке описания профиля не улетает в админ-чат.

Отчёт владельца 26.09: пользователь сменил имя/юзернейм в Telegram, бот
попытался обновить description в Remnawave по устаревшему remnawave_id
(панельного юзера удалили отдельно от бота), панель ответила
«User not found», и это ушло error-уровнем в отчёт об ошибках — хотя это тот
самый случай, который update_user() у себя уже намеренно логирует warning'ом
(см. remnawave_api.py:1002-1004: error-логи буферизуются и сыплют отчётом в
админ-чат), а TelegramNotifierProcessor пересылает в чат только уровни
error/critical/exception (logging_handler.py:234-237).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.external.remnawave_api import RemnaWaveAPIError
from app.middlewares.auth import _refresh_remnawave_description


def _user_not_found_error() -> RemnaWaveAPIError:
    return RemnaWaveAPIError('User not found', 404, {'message': 'User not found'})


@pytest.mark.asyncio
async def test_user_not_found_is_warning_not_error():
    fake_api = MagicMock()
    fake_service = MagicMock()
    fake_service.get_api_client.return_value.__aenter__ = AsyncMock(return_value=fake_api)
    fake_service.get_api_client.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch('app.middlewares.auth.RemnaWaveService', return_value=fake_service),
        patch('app.services.panel_sync.patch_panel_account', new=AsyncMock(side_effect=_user_not_found_error())),
        patch('app.middlewares.auth.logger') as mock_logger,
    ):
        await _refresh_remnawave_description(remnawave_id=42, description='x', telegram_id=899027520)

    mock_logger.error.assert_not_called()
    mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_real_api_failure_is_still_an_error():
    fake_api = MagicMock()
    fake_service = MagicMock()
    fake_service.get_api_client.return_value.__aenter__ = AsyncMock(return_value=fake_api)
    fake_service.get_api_client.return_value.__aexit__ = AsyncMock(return_value=False)
    real_failure = RemnaWaveAPIError('Internal Server Error', 500, {})

    with (
        patch('app.middlewares.auth.RemnaWaveService', return_value=fake_service),
        patch('app.services.panel_sync.patch_panel_account', new=AsyncMock(side_effect=real_failure)),
        patch('app.middlewares.auth.logger') as mock_logger,
    ):
        await _refresh_remnawave_description(remnawave_id=42, description='x', telegram_id=899027520)

    mock_logger.warning.assert_not_called()
    mock_logger.error.assert_called_once()
