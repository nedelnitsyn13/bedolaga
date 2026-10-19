"""Журнал писем: что пишется, что не пишется никогда и как строки находят владельца.

Журнал нужен, чтобы ответить «ушло ли письмо и когда». Текст письма туда не
попадает ни при каком исходе — в нём коды подтверждения и ссылки входа.
"""

from __future__ import annotations

import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cabinet.services.email_service import EmailService
from app.database.models import Base, EmailLogEntry, User
from app.services.email_log_service import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SENT,
    EmailLogService,
)
from app.services.email_retry_service import EmailRetryService
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


SECRET_BODY = '<p>Ваш код: 123456</p>'


def _configured(service: EmailService, monkeypatch) -> None:
    monkeypatch.setattr(EmailService, 'is_configured', lambda self: True)
    monkeypatch.setattr(EmailService, 'from_email', property(lambda self: 'noreply@vpn.test'))


def _smtp_ok() -> MagicMock:
    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)
    return smtp


def _recorded(record: MagicMock) -> list[dict]:
    return [call.kwargs for call in record.call_args_list]


# ============ send_email → журнал ============


def test_successful_send_is_logged_with_type_and_without_body(monkeypatch):
    service = EmailService()
    _configured(service, monkeypatch)
    record = MagicMock()
    monkeypatch.setattr(service, '_get_smtp_connection', _smtp_ok)

    with patch('app.services.email_log_service.email_log_service.record', record):
        assert service.send_email('User@Test.dev', 'Код', SECRET_BODY, email_type='email_change_code') is True

    [entry] = _recorded(record)
    assert entry['status'] == STATUS_SENT
    assert entry['email_type'] == 'email_change_code'
    assert entry['subject'] == 'Код'
    assert '123456' not in repr(entry), 'текст письма не должен попадать в журнал'


def test_server_rejection_is_logged_as_failed(monkeypatch):
    service = EmailService()
    _configured(service, monkeypatch)
    smtp = _smtp_ok()
    smtp.sendmail.side_effect = smtplib.SMTPRecipientsRefused({'user@test.dev': (550, b'no such user')})
    monkeypatch.setattr(service, '_get_smtp_connection', lambda: smtp)
    record = MagicMock()

    with patch('app.services.email_log_service.email_log_service.record', record):
        assert service.send_email('user@test.dev', 'Тема', SECRET_BODY) is False

    [entry] = _recorded(record)
    assert entry['status'] == STATUS_FAILED
    assert 'SMTPRecipientsRefused' in entry['error']
    assert entry['email_type'] is None, 'без типа журнал сам подставит «other»'


@pytest.mark.parametrize(
    ('queue_on_failure', 'queued', 'expected'), [(True, True, STATUS_QUEUED), (False, False, STATUS_FAILED)]
)
def test_network_failure_is_queued_or_failed(monkeypatch, queue_on_failure, queued, expected):
    service = EmailService()
    _configured(service, monkeypatch)

    def refuse():
        raise ConnectionRefusedError('connection refused')

    monkeypatch.setattr(service, '_get_smtp_connection', refuse)
    monkeypatch.setattr(service, '_queue_for_retry', MagicMock(return_value=queued))
    record = MagicMock()

    with patch('app.services.email_log_service.email_log_service.record', record):
        service.send_email('user@test.dev', 'Тема', SECRET_BODY, queue_on_failure=queue_on_failure)

    [entry] = _recorded(record)
    assert entry['status'] == expected


def test_queued_letter_keeps_its_type_for_the_retry(monkeypatch):
    service = EmailService()
    _configured(service, monkeypatch)

    def refuse():
        raise ConnectionRefusedError('connection refused')

    monkeypatch.setattr(service, '_get_smtp_connection', refuse)
    queue = MagicMock(return_value=True)
    monkeypatch.setattr(service, '_queue_for_retry', queue)

    with patch('app.services.email_log_service.email_log_service.record', MagicMock()):
        service.send_email('user@test.dev', 'Тема', SECRET_BODY, email_type='password_reset')

    assert queue.call_args.kwargs['email_type'] == 'password_reset'


def test_missing_smtp_is_logged(monkeypatch):
    service = EmailService()
    monkeypatch.setattr(EmailService, 'is_configured', lambda self: False)
    record = MagicMock()

    with patch('app.services.email_log_service.email_log_service.record', record):
        assert service.send_email('user@test.dev', 'Тема', SECRET_BODY) is False

    [entry] = _recorded(record)
    assert entry['status'] == STATUS_FAILED
    assert entry['error'] == 'SMTP не настроен'


def test_record_in_log_false_writes_nothing(monkeypatch):
    service = EmailService()
    _configured(service, monkeypatch)
    monkeypatch.setattr(service, '_get_smtp_connection', _smtp_ok)
    record = MagicMock()

    with patch('app.services.email_log_service.email_log_service.record', record):
        service.send_email('user@test.dev', 'Тема', SECRET_BODY, record_in_log=False)

    record.assert_not_called()


def test_auth_letters_carry_their_type(monkeypatch):
    service = EmailService()
    send = MagicMock(return_value=True)
    monkeypatch.setattr(service, 'send_email', send)

    service.send_verification_email('a@test.dev', 'tok', 'https://cab.test/verify-email')
    service.send_password_reset_email('a@test.dev', 'tok', 'https://cab.test/reset-password')
    service.send_email_change_code('a@test.dev', '123456')
    service.send_admin_email_confirmation('a@test.dev', 'https://cab.test/confirm-email?token=adm_x')

    assert [call.kwargs['email_type'] for call in send.call_args_list] == [
        'email_verification',
        'password_reset',
        'email_change_code',
        'email_admin_set',
    ]


# ============ очередь повторов → журнал ============


def _session_mock() -> AsyncMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.commit = AsyncMock()
    return session


def _item(**kw) -> dict:
    return {
        'id': 1,
        'to_email': 'user@test.dev',
        'subject': 'Сброс пароля',
        'body_html': SECRET_BODY,
        'body_text': None,
        'unsubscribe_url': None,
        'attachments': None,
        'attempts': kw.get('attempts', 0),
        'email_type': 'password_reset',
    }


@pytest.mark.parametrize(
    ('sent', 'attempts', 'expected'),
    [(True, 0, [STATUS_SENT]), (False, 0, []), (False, 9, [STATUS_FAILED])],
)
async def test_retry_logs_only_the_outcome(sent, attempts, expected):
    """Промежуточный неуспех не пишется: строка «не ушло» при живом повторе вводит в заблуждение."""
    service = EmailRetryService()
    mail = MagicMock()
    mail.send_email = MagicMock(return_value=sent)
    record = MagicMock()

    with (
        patch('app.services.email_retry_service.AsyncSessionLocal', return_value=_session_mock()),
        patch('app.cabinet.services.email_service.email_service', mail),
        patch('app.services.email_log_service.email_log_service.record', record),
    ):
        await service._attempt(_item(attempts=attempts))

    assert mail.send_email.call_args.kwargs['record_in_log'] is False
    assert mail.send_email.call_args.kwargs['email_type'] == 'password_reset'
    assert [c['status'] for c in _recorded(record)] == expected
    for entry in _recorded(record):
        assert entry['email_type'] == 'password_reset'


# ============ запись в базу ============


async def _memory_maker(monkeypatch):
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__, EmailLogEntry.__table__]))
    return engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def test_entries_from_worker_thread_reach_the_table_with_owner(monkeypatch):
    engine, maker = await _memory_maker(monkeypatch)
    try:
        async with maker() as session:
            session.add(User(id=7, telegram_id=700, email='Owner@Test.dev', language='ru'))
            await session.commit()

        monkeypatch.setattr('app.services.email_log_service.AsyncSessionLocal', maker)
        service = EmailLogService()
        await service.start()

        def from_thread():
            service.record(to_email='OWNER@test.dev', subject='Тема', status=STATUS_SENT, email_type='broadcast')
            service.record(to_email='guest@test.dev', subject='Подарок', status=STATUS_FAILED, error='x' * 2000)

        await asyncio.to_thread(from_thread)
        await asyncio.sleep(0)  # дать call_soon_threadsafe положить записи в очередь
        await service.stop()

        async with maker() as session:
            rows = (await session.execute(select(EmailLogEntry).order_by(EmailLogEntry.id))).scalars().all()
    finally:
        await engine.dispose()

    assert [(r.to_email, r.user_id, r.email_type, r.status) for r in rows] == [
        ('owner@test.dev', 7, 'broadcast', STATUS_SENT),
        ('guest@test.dev', None, 'other', STATUS_FAILED),
    ]
    assert len(rows[1].error) <= 500


async def test_record_before_start_is_a_noop():
    EmailLogService().record(to_email='a@test.dev', subject='s', status=STATUS_SENT)


async def test_cleanup_drops_only_old_rows(monkeypatch):
    engine, maker = await _memory_maker(monkeypatch)
    try:
        now = datetime.now(tz=UTC)
        async with maker() as session:
            session.add_all(
                [
                    EmailLogEntry(
                        to_email='old@test.dev',
                        subject='s',
                        status='sent',
                        email_type='other',
                        created_at=now - timedelta(days=91),
                    ),
                    EmailLogEntry(
                        to_email='new@test.dev',
                        subject='s',
                        status='sent',
                        email_type='other',
                        created_at=now - timedelta(days=1),
                    ),
                ]
            )
            await session.commit()

        monkeypatch.setattr('app.services.email_log_service.AsyncSessionLocal', maker)
        monkeypatch.setattr('app.services.email_log_service.settings.EMAIL_LOG_RETENTION_DAYS', 90)
        deleted = await EmailLogService().cleanup_once()

        async with maker() as session:
            left = (await session.execute(select(EmailLogEntry.to_email))).scalars().all()
    finally:
        await engine.dispose()

    assert deleted == 1
    assert left == ['new@test.dev']
