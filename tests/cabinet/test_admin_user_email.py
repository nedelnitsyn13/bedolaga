"""Почта пользователя из админки: указать, отправить ссылку ещё раз, удалить, журнал писем.

Главное свойство: ссылка из письма «почту указал администратор» только
подтверждает адрес и в аккаунт не пускает. Админ может опечататься в адресе, и
тогда письмо получит посторонний — вход в чужой аккаунт он получить не должен.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.cabinet.auth import email_auth_gate as gate
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import admin_user_email as route, auth
from app.database.models import EmailLogEntry, SystemSetting, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SystemSetting.__table__, User.__table__, EmailLogEntry.__table__)
ADMIN = SimpleNamespace(id=1, telegram_id=111)
CABINET = 'https://cab.vpn.test'


def _override_permissions(app: FastAPI) -> None:
    for candidate in route.router.routes:
        for dependant in candidate.dependant.dependencies:
            call = dependant.call
            if getattr(call, '__name__', '') == 'dependency' and getattr(call, '__module__', '').endswith(
                'cabinet.dependencies'
            ):
                app.dependency_overrides[call] = lambda: ADMIN


@contextlib.asynccontextmanager
async def _client(monkeypatch, *, mail_configured: bool = True):
    monkeypatch.setattr(route.settings, 'CABINET_URL', CABINET)
    monkeypatch.setattr(route.settings, 'ADMIN_EMAILS', 'boss@vpnbro.org')
    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)

    async def no_override(*_args, **_kwargs):
        return None

    async def not_limited(*_args, **_kwargs):
        return False

    monkeypatch.setattr(route, 'get_rendered_override', no_override)
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', not_limited)
    audit: list[dict] = []

    async def log_action(_db, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(route.PermissionService, 'log_action', log_action)
    sender = MagicMock(return_value=True)
    sender.audit = audit
    monkeypatch.setattr(route.email_service, 'is_configured', MagicMock(return_value=mail_configured))
    monkeypatch.setattr(route.email_service, 'send_admin_email_confirmation', sender)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(User(id=1, telegram_id=111, first_name='Админ', language='ru'))
        db.add(User(id=2, telegram_id=222, first_name='Иван', language='ru'))
        db.add(
            User(
                id=3,
                telegram_id=None,
                auth_type='email',
                email='only@vpnbro.org',
                email_verified=True,
                first_name='Пётр',
                language='ru',
            )
        )
        await db.commit()

        app = FastAPI()
        app.include_router(route.router, prefix='/cabinet')
        app.include_router(auth.router, prefix='/cabinet')

        async def _db():
            yield db

        app.dependency_overrides[get_cabinet_db] = _db
        _override_permissions(app)

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://cabinet') as http:
            yield http, db, sender


async def _user(db, user_id: int) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    await db.refresh(user)
    return user


# ============ указать почту ============


@pytest.mark.asyncio
async def test_admin_sets_email_and_link_goes_to_the_new_address(monkeypatch):
    async with _client(monkeypatch) as (http, db, sender):
        response = await http.put('/cabinet/admin/users/2/email', json={'email': 'Ivan@Example.org'})

        assert response.status_code == 200, response.text
        assert response.json() == {
            'user_id': 2,
            'email': 'ivan@example.org',
            'email_verified': False,
            'email_sent': True,
        }

        user = await _user(db, 2)
        assert user.email == 'ivan@example.org'
        assert user.email_verified is False
        assert user.email_verification_token.startswith('adm_')

        assert sender.call_count == 1
        kwargs = sender.call_args.kwargs
        assert kwargs['to_email'] == 'ivan@example.org'
        assert kwargs['confirm_url'] == f'{CABINET}/confirm-email?token={user.email_verification_token}'

        assert [(a['action'], a['resource_id'], a['details']['new_email']) for a in sender.audit] == [
            ('set_user_email', '2', 'ivan@example.org')
        ]


@pytest.mark.asyncio
async def test_taken_email_is_refused(monkeypatch):
    async with _client(monkeypatch) as (http, db, sender):
        response = await http.put('/cabinet/admin/users/2/email', json={'email': 'ONLY@vpnbro.org'})

        assert response.status_code == 409
        assert (await _user(db, 2)).email is None
        sender.assert_not_called()


@pytest.mark.asyncio
async def test_admin_email_cannot_be_given_to_a_user(monkeypatch):
    """Адрес из ADMIN_EMAILS после подтверждения дал бы пользователю права администратора."""
    async with _client(monkeypatch) as (http, db, sender):
        response = await http.put('/cabinet/admin/users/2/email', json={'email': 'Boss@vpnbro.org'})

        assert response.status_code == 403
        assert (await _user(db, 2)).email is None
        sender.assert_not_called()


@pytest.mark.asyncio
async def test_without_mail_server_nothing_changes(monkeypatch):
    async with _client(monkeypatch, mail_configured=False) as (http, db, sender):
        response = await http.put('/cabinet/admin/users/2/email', json={'email': 'ivan@example.org'})

        assert response.status_code == 503
        assert (await _user(db, 2)).email is None
        sender.assert_not_called()


@pytest.mark.asyncio
async def test_same_verified_email_is_not_reset(monkeypatch):
    async with _client(monkeypatch) as (http, db, _sender):
        response = await http.put('/cabinet/admin/users/3/email', json={'email': 'only@vpnbro.org'})

        assert response.status_code == 400
        assert (await _user(db, 3)).email_verified is True


# ============ ссылка из письма ============


@pytest.mark.asyncio
async def test_confirm_link_verifies_without_logging_in(monkeypatch):
    async with _client(monkeypatch) as (http, db, _sender):
        await http.put('/cabinet/admin/users/2/email', json={'email': 'ivan@example.org'})
        token = (await _user(db, 2)).email_verification_token

        response = await http.post('/cabinet/auth/email/confirm', json={'token': token})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {'success': True, 'email': 'ivan@example.org'}
        assert 'access_token' not in response.text and 'refresh_token' not in response.text

        user = await _user(db, 2)
        assert user.email_verified is True
        assert user.email_verification_source == 'admin_link'
        assert user.email_verification_token is None

        again = await http.post('/cabinet/auth/email/confirm', json={'token': token})
        assert again.status_code == 400, 'ссылка одноразовая'


@pytest.mark.asyncio
async def test_admin_link_does_not_work_as_a_login_link(monkeypatch):
    """/email/verify выдаёт токены входа — ссылку, набранную админом, он принимать не должен."""
    async with _client(monkeypatch) as (http, db, _sender):
        await http.put('/cabinet/admin/users/2/email', json={'email': 'ivan@example.org'})
        token = (await _user(db, 2)).email_verification_token

        response = await http.post('/cabinet/auth/email/verify', json={'token': token})

        assert response.status_code == 400
        user = await _user(db, 2)
        assert user.email_verified is False
        assert user.email_verification_token == token, 'ссылка должна остаться рабочей для /email/confirm'


@pytest.mark.asyncio
async def test_regular_tokens_are_not_accepted_by_confirm(monkeypatch):
    async with _client(monkeypatch) as (http, db, _sender):
        user = await _user(db, 2)
        user.email = 'ivan@example.org'
        user.email_verification_token = 'plain-registration-token'
        user.email_verification_expires = datetime.now(UTC) + timedelta(hours=1)
        await db.commit()

        response = await http.post('/cabinet/auth/email/confirm', json={'token': 'plain-registration-token'})

        assert response.status_code == 400
        assert (await _user(db, 2)).email_verified is False


@pytest.mark.asyncio
async def test_expired_link_is_refused(monkeypatch):
    async with _client(monkeypatch) as (http, db, _sender):
        await http.put('/cabinet/admin/users/2/email', json={'email': 'ivan@example.org'})
        user = await _user(db, 2)
        token = user.email_verification_token
        user.email_verification_expires = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

        response = await http.post('/cabinet/auth/email/confirm', json={'token': token})

        assert response.status_code == 400
        assert (await _user(db, 2)).email_verified is False


# ============ отправить ещё раз ============


@pytest.mark.asyncio
async def test_resend_issues_a_new_link_and_kills_the_old_one(monkeypatch):
    async with _client(monkeypatch) as (http, db, sender):
        await http.put('/cabinet/admin/users/2/email', json={'email': 'ivan@example.org'})
        old_token = (await _user(db, 2)).email_verification_token

        response = await http.post('/cabinet/admin/users/2/email/resend')

        assert response.status_code == 200, response.text
        new_token = (await _user(db, 2)).email_verification_token
        assert new_token != old_token and new_token.startswith('adm_')
        assert sender.call_count == 2

        stale = await http.post('/cabinet/auth/email/confirm', json={'token': old_token})
        assert stale.status_code == 400


@pytest.mark.asyncio
async def test_resend_for_verified_email_is_refused(monkeypatch):
    async with _client(monkeypatch) as (http, _db, sender):
        response = await http.post('/cabinet/admin/users/3/email/resend')

        assert response.status_code == 400
        sender.assert_not_called()


# ============ удалить почту ============


@pytest.mark.asyncio
async def test_email_only_account_keeps_its_email(monkeypatch):
    """Почта — единственный вход: без неё человек потеряет доступ к аккаунту."""
    async with _client(monkeypatch) as (http, db, _sender):
        response = await http.delete('/cabinet/admin/users/3/email')

        assert response.status_code == 400
        assert (await _user(db, 3)).email == 'only@vpnbro.org'


@pytest.mark.asyncio
async def test_delete_clears_email_and_pending_links(monkeypatch):
    async with _client(monkeypatch) as (http, db, _sender):
        await http.put('/cabinet/admin/users/2/email', json={'email': 'ivan@example.org'})

        response = await http.delete('/cabinet/admin/users/2/email')

        assert response.status_code == 200, response.text
        assert response.json()['email'] is None
        user = await _user(db, 2)
        assert (user.email, user.email_verified, user.email_verification_token) == (None, False, None)


# ============ журнал писем ============


@pytest.mark.asyncio
async def test_email_log_shows_letters_of_this_user_only(monkeypatch):
    monkeypatch.setattr(route.settings, 'EMAIL_LOG_RETENTION_DAYS', 90)
    async with _client(monkeypatch) as (http, db, _sender):
        now = datetime.now(UTC)
        db.add_all(
            [
                # Привязано к пользователю при отправке (адрес с тех пор сменился)
                EmailLogEntry(
                    user_id=3,
                    to_email='old@vpnbro.org',
                    email_type='password_reset',
                    subject='Сброс',
                    status='sent',
                    created_at=now - timedelta(days=2),
                ),
                # Текущий адрес, владелец при отправке не определился
                EmailLogEntry(
                    user_id=None,
                    to_email='only@vpnbro.org',
                    email_type='broadcast',
                    subject='Новости',
                    status='failed',
                    error='550',
                    created_at=now - timedelta(days=1),
                ),
                # Чужое письмо
                EmailLogEntry(
                    user_id=2,
                    to_email='someone@vpnbro.org',
                    email_type='broadcast',
                    subject='Чужое',
                    status='sent',
                    created_at=now,
                ),
            ]
        )
        await db.commit()

        response = await http.get('/cabinet/admin/users/3/emails')

        assert response.status_code == 200, response.text
        body = response.json()
        assert body['total'] == 2
        assert body['retention_days'] == 90
        assert [(i['subject'], i['status'], i['email_type']) for i in body['items']] == [
            ('Новости', 'failed', 'broadcast'),
            ('Сброс', 'sent', 'password_reset'),
        ]
        assert set(body['items'][0]) == {'id', 'to_email', 'email_type', 'subject', 'status', 'error', 'created_at'}
