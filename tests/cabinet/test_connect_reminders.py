"""Напоминание «подключитесь» тем, кто взял триал и ни разу не подключился.

Оператор хотел напоминать таким людям сам: из карточки — одному, из списка —
всем сразу. «Ни разу не подключался» знает только панель, поэтому фильтр
принимает множество её аккаунтов, а не смотрит в базу бота.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError

from app.cabinet.routes.admin_users import connect_reminder_info, connect_reminder_send
from app.cabinet.schemas.users import ConnectReminderRequest
from app.database.crud.user import get_users_count, get_users_list
from app.database.models import AdminAuditLog, Subscription, SubscriptionStatus, Tariff, User, UserStatus
from app.services import connect_reminder_service as reminders
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, Subscription.__table__, Tariff.__table__, AdminAuditLog.__table__)
NOW = datetime.now(UTC)
#: Аккаунты панели, ни разу не подключавшиеся.
NEVER = frozenset({10, 12, 13, 14, 15, 16})


def _user(telegram_id: int | None, username: str, **extra) -> User:
    return User(
        telegram_id=telegram_id,
        username=username,
        first_name=username,
        status=extra.pop('status', UserStatus.ACTIVE.value),
        language='ru',
        balance_kopeks=0,
        **extra,
    )


def _sub(user: User, panel_id: int | None, *, trial: bool = True, days: int = 3, status: str | None = None):
    return Subscription(
        user_id=user.id,
        status=status or SubscriptionStatus.ACTIVE.value,
        is_trial=trial,
        start_date=NOW - timedelta(days=1),
        end_date=NOW + timedelta(days=days),
        traffic_limit_gb=5,
        traffic_used_gb=0,
        device_limit=1,
        remnawave_id=panel_id,
        remnawave_short_id=f'short{user.id}',
    )


async def _seed(db) -> dict[str, User]:
    people = {
        'fresh': _user(1, 'fresh'),  # триал, не подключался — подходит
        'connected': _user(2, 'connected'),  # триал, уже подключался
        'paid': _user(3, 'paid'),  # не подключался, но платный
        'legacy': _user(4, 'legacy', remnawave_id=13),  # старая подписка без своего id панели
        'expired': _user(5, 'expired'),  # триал истёк
        'blocked': _user(6, 'blocked', status=UserStatus.BLOCKED.value),
        'email_only': _user(None, 'email_only', email='a@example.com'),
    }
    db.add_all(people.values())
    await db.flush()
    db.add_all(
        [
            _sub(people['fresh'], 10),
            _sub(people['connected'], 11),
            _sub(people['paid'], 12, trial=False),
            _sub(people['legacy'], None),
            _sub(people['expired'], 14, days=-1),
            _sub(people['blocked'], 15),
            _sub(people['email_only'], 16, status=SubscriptionStatus.TRIAL.value),
        ]
    )
    await db.commit()
    return people


async def test_list_filter_keeps_only_live_trials_that_never_connected(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        users = await get_users_list(db, never_connected_trial=NEVER)
        assert {user.username for user in users} == {'fresh', 'legacy', 'blocked', 'email_only'}
        assert await get_users_count(db, never_connected_trial=NEVER) == 4


async def test_empty_panel_answer_means_nobody(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await get_users_count(db, never_connected_trial=frozenset()) == 0


async def test_bulk_skips_no_telegram_blocked_and_recently_reminded(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        people = await _seed(db)
        admin = _user(99, 'admin')
        db.add(admin)
        await db.flush()
        db.add(
            AdminAuditLog(
                id=1,  # SQLite не автоинкрементит BIGINT
                user_id=admin.id,
                action=reminders.AUDIT_ACTION,
                resource_type='user',
                resource_id=str(people['legacy'].id),
                status='success',
                created_at=NOW - timedelta(hours=1),
            )
        )
        await db.commit()

        audience, skipped = await reminders.bulk_audience(db, NEVER)

        assert [user.username for user in audience] == ['fresh']
        assert skipped == 1


async def test_reminder_older_than_cooldown_does_not_block(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        people = await _seed(db)
        admin = _user(99, 'admin')
        db.add(admin)
        await db.flush()
        db.add(
            AdminAuditLog(
                id=1,  # SQLite не автоинкрементит BIGINT
                user_id=admin.id,
                action=reminders.AUDIT_ACTION,
                resource_type='user',
                resource_id=str(people['fresh'].id),
                status='success',
                created_at=NOW - timedelta(days=2),
            )
        )
        await db.commit()

        audience, skipped = await reminders.bulk_audience(db, NEVER)

        assert {user.username for user in audience} == {'fresh', 'legacy'}
        assert skipped == 0


def _loaded_user(*subs, remnawave_id=None, telegram_id=1):
    return SimpleNamespace(id=1, telegram_id=telegram_id, remnawave_id=remnawave_id, language='ru', subscriptions=subs)


def _plain_sub(panel_id, *, trial=True, status='active', days=3):
    return SimpleNamespace(is_trial=trial, status=status, end_date=NOW + timedelta(days=days), remnawave_id=panel_id)


def test_card_check_matches_the_list_filter() -> None:
    assert reminders.is_never_connected_trial(_loaded_user(_plain_sub(10)), NEVER, NOW)
    assert reminders.is_never_connected_trial(_loaded_user(_plain_sub(16, status='trial')), NEVER, NOW)
    assert reminders.is_never_connected_trial(_loaded_user(_plain_sub(None), remnawave_id=13), NEVER, NOW)
    assert not reminders.is_never_connected_trial(_loaded_user(_plain_sub(11)), NEVER, NOW)
    assert not reminders.is_never_connected_trial(_loaded_user(_plain_sub(12, trial=False)), NEVER, NOW)
    assert not reminders.is_never_connected_trial(_loaded_user(_plain_sub(14, days=-1)), NEVER, NOW)


def test_default_text_is_the_panel_reminder_without_placeholders() -> None:
    text = reminders.default_text(SimpleNamespace(language='ru'))
    assert 'подключились' in text
    assert '{' not in text


async def test_card_info_offers_text_only_when_eligible(monkeypatch) -> None:
    user = _loaded_user(_plain_sub(10))
    db = MagicMock()
    monkeypatch.setattr(reminders, 'get_never_connected_panel_ids', AsyncMock(return_value=NEVER))
    monkeypatch.setattr(reminders, 'last_reminded_at', AsyncMock(return_value={}))
    monkeypatch.setattr(reminders, 'get_template', AsyncMock(return_value=None))
    with patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user)):
        info = await connect_reminder_info(user_id=1, admin=SimpleNamespace(id=9), db=db)
    assert info.eligible and info.reason is None and 'подключились' in info.text

    monkeypatch.setattr(reminders, 'get_never_connected_panel_ids', AsyncMock(return_value=None))
    with patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user)):
        info = await connect_reminder_info(user_id=1, admin=SimpleNamespace(id=9), db=db)
    assert not info.eligible and info.reason == 'panel_unavailable'

    no_tg = _loaded_user(_plain_sub(10), telegram_id=None)
    with patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=no_tg)):
        info = await connect_reminder_info(user_id=1, admin=SimpleNamespace(id=9), db=db)
    assert info.reason == 'no_telegram'


class _BotContext:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def __aenter__(self):
        return self.bot

    async def __aexit__(self, *_exc) -> None:
        return None


async def test_send_attaches_connect_button_and_logs_result(monkeypatch) -> None:
    user = _loaded_user(_plain_sub(10))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(reminders.AuditLogCRUD, 'create', audit)
    monkeypatch.setattr(reminders, 'get_template', AsyncMock(return_value=None))
    monkeypatch.setattr('app.cabinet.routes.admin_users.settings.BOT_TOKEN', '123:abc')
    db = MagicMock()
    db.commit = AsyncMock()

    with (
        patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user)),
        patch('app.bot_factory.create_bot', return_value=_BotContext(bot)),
    ):
        response = await connect_reminder_send(
            user_id=1, payload=ConnectReminderRequest(text='  Привет  '), admin=SimpleNamespace(id=9), db=db
        )

    assert response.result == 'sent'
    args, kwargs = bot.send_message.call_args
    assert args == (1, 'Привет')
    assert kwargs['reply_markup'].inline_keyboard[0][0].text
    assert audit.await_args.kwargs['action'] == reminders.AUDIT_ACTION
    assert audit.await_args.kwargs['status'] == 'success'

    bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(method=None, message='blocked'))
    with (
        patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user)),
        patch('app.bot_factory.create_bot', return_value=_BotContext(bot)),
    ):
        response = await connect_reminder_send(
            user_id=1, payload=ConnectReminderRequest(text='Привет'), admin=SimpleNamespace(id=9), db=db
        )
    assert response.result == 'forbidden'
    assert audit.await_args.kwargs['status'] == 'failure'
