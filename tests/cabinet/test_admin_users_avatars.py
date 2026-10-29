"""Фото Telegram в списке пользователей админки.

Раньше в списке у всех были кружки с первой буквой имени, хотя бот умеет брать
фото профиля сам (так уже работает шапка кабинета). POST /cabinet/admin/users/avatars
отдаёт подписанные ссылки сразу на порцию ленты: без фото и без Telegram — нет ключа.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import PhotoSize, UserProfilePhotos

from app.cabinet.routes.admin_users import get_users_avatars
from app.cabinet.schemas.users import UserAvatarsRequest
from app.services import user_avatar_service


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, expire=None) -> bool:
        self.store[key] = value
        return True


class _BotContext:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def __aenter__(self):
        return self.bot

    async def __aexit__(self, *_exc) -> None:
        return None


def _db(rows: list[tuple[int, int | None]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _photos_for(with_photo: set[int]):
    async def get_user_profile_photos(user_id: int, limit: int):
        if user_id not in with_photo:
            return UserProfilePhotos(total_count=0, photos=[])
        file_id = f'F{user_id}'.ljust(32, 'A')
        return UserProfilePhotos(
            total_count=1,
            photos=[[PhotoSize(file_id=file_id, file_unique_id=f'u{user_id}', width=320, height=320)]],
        )

    return get_user_profile_photos


def _request() -> MagicMock:
    request = MagicMock()
    request.url_for.side_effect = lambda _name, file_id: f'https://api.example/cabinet/media/{file_id}'
    return request


async def test_returns_signed_urls_only_for_users_with_a_photo(monkeypatch) -> None:
    monkeypatch.setattr(user_avatar_service, 'cache', _FakeCache())
    bot = MagicMock()
    bot.get_user_profile_photos = AsyncMock(side_effect=_photos_for({111, 333}))
    # Пользователь 4 вошёл по почте: telegram_id отрицательный — Telegram не спрашиваем.
    db = _db([(1, 111), (2, 222), (3, 333), (4, -5)])

    with patch('app.bot_factory.create_bot', return_value=_BotContext(bot)):
        response = await get_users_avatars(
            request=_request(),
            payload=UserAvatarsRequest(user_ids=[1, 2, 3, 4, 1]),
            admin=SimpleNamespace(id=99),
            db=db,
        )

    assert set(response.avatars) == {1, 3}
    assert response.avatars[1].startswith('https://api.example/cabinet/media/F111')
    assert 'token=' in response.avatars[3]
    assert bot.get_user_profile_photos.await_count == 3


async def test_empty_request_does_not_touch_db_or_telegram() -> None:
    db = _db([])
    response = await get_users_avatars(
        request=_request(), payload=UserAvatarsRequest(user_ids=[]), admin=SimpleNamespace(id=99), db=db
    )
    assert response.avatars == {}
    db.execute.assert_not_awaited()
