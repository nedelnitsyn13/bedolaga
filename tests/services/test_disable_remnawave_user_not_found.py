"""«User not found» при отключении RemnaWave-пользователя не улетает в админ-чат.

Отчёт владельца 29.09: попытка отключить панельного юзера, которого на панели
уже нет (удалён отдельно от бота — например, в связке с внешним антифрод-API),
падает с RemnaWaveAPIError «User not found» и раньше улетала error-уровнем —
тот же класс бага, что уже правили в auth.py (см.
tests/middlewares/test_refresh_remnawave_description.py), только в другом
вызывающем месте.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.external.remnawave_api import RemnaWaveAPIError
from app.services.subscription_service import SubscriptionService


def _user_not_found_error() -> RemnaWaveAPIError:
    return RemnaWaveAPIError('User not found', 404, {'message': 'User not found'})


@asynccontextmanager
async def _fake_api_client():
    yield MagicMock()


@pytest.mark.asyncio
async def test_disable_user_not_found_is_warning_not_error():
    service = SubscriptionService.__new__(SubscriptionService)

    with (
        patch.object(SubscriptionService, 'get_api_client', return_value=_fake_api_client()),
        patch(
            'app.services.grace_access_runtime.set_panel_user_enabled_state_grace_safe',
            new=AsyncMock(side_effect=_user_not_found_error()),
        ),
        patch('app.services.subscription_service.logger') as mock_logger,
    ):
        result = await service.disable_remnawave_user(42)

    assert result is False
    mock_logger.error.assert_not_called()
    mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_disable_user_real_api_failure_is_still_an_error():
    service = SubscriptionService.__new__(SubscriptionService)
    real_failure = RemnaWaveAPIError('Internal Server Error', 500, {})

    with (
        patch.object(SubscriptionService, 'get_api_client', return_value=_fake_api_client()),
        patch(
            'app.services.grace_access_runtime.set_panel_user_enabled_state_grace_safe',
            new=AsyncMock(side_effect=real_failure),
        ),
        patch('app.services.subscription_service.logger') as mock_logger,
    ):
        result = await service.disable_remnawave_user(42)

    assert result is False
    mock_logger.warning.assert_not_called()
    mock_logger.error.assert_called_once()
