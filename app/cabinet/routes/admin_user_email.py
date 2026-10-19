"""Почта пользователя в админке: указать, удалить, отправить ссылку ещё раз, журнал писем.

Админ вводит адрес, почта сохраняется неподтверждённой, а на новый адрес уходит
письмо со ссылкой. Ссылка только подтверждает адрес и в аккаунт не входит:
при опечатке письмо получит посторонний, и входа он получить не должен.

Журнал писем показывает, что и когда уходило на адрес пользователя — без текста
письма (в нём коды и ссылки входа).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import is_email_taken
from app.database.models import EmailLogEntry, User
from app.services.disposable_email_service import disposable_email_service
from app.services.permission_service import PermissionService

from ..auth.email_verification import generate_admin_email_token, get_verification_expires_at
from ..dependencies import get_cabinet_db, require_permission
from ..schemas.users import (
    AdminSetUserEmailRequest,
    AdminUserEmailLogItem,
    AdminUserEmailLogResponse,
    AdminUserEmailResponse,
)
from ..services.email_service import email_service
from ..services.email_template_overrides import get_rendered_override


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/users', tags=['Cabinet Admin Users'])

CONFIRM_PATH = '/confirm-email'
_INT4_MAX = 2_147_483_647


def _cabinet_base_url() -> str:
    base = (settings.CABINET_URL or '').strip().rstrip('/')
    return base if base.startswith(('http://', 'https://')) else ''


def _has_other_login(user: User) -> bool:
    """Есть ли у аккаунта вход помимо почты — без него удалять почту нельзя."""
    return bool(user.telegram_id or user.google_id or user.yandex_id or user.discord_id or user.vk_id)


def _response(user: User, *, email_sent: bool | None = None) -> AdminUserEmailResponse:
    return AdminUserEmailResponse(
        user_id=user.id,
        email=user.email,
        email_verified=bool(user.email_verified),
        email_sent=email_sent,
    )


async def _load_user(db: AsyncSession, user_id: int) -> User:
    # users.id — int4: номер вне диапазона PostgreSQL отверг бы ошибкой, а не «не найден».
    # Подписки и прочие связи здесь не нужны, поэтому без get_user_by_id с его подгрузками.
    user = None
    if 0 < user_id <= _INT4_MAX:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
    return user


def _require_mail_ready() -> str:
    """Почтовый сервер и адрес кабинета нужны до изменения данных, а не после."""
    if not email_service.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Email service is not configured',
        )
    base = _cabinet_base_url()
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='CABINET_URL is not configured',
        )
    return base


def _issue_token(user: User) -> str:
    token = generate_admin_email_token()
    user.email_verification_token = token
    user.email_verification_expires = get_verification_expires_at()
    return token


async def _send_confirmation(db: AsyncSession, user: User, base_url: str, token: str) -> bool:
    language = user.language or 'ru'
    confirm_url = f'{base_url}{CONFIRM_PATH}?token={token}'
    override = await get_rendered_override(
        'email_admin_set',
        language,
        context={
            'username': user.first_name or '',
            'email': user.email,
            'confirm_url': confirm_url,
            'expire_hours': str(settings.get_cabinet_email_verification_expire_hours()),
        },
        db=db,
        required_vars=['confirm_url'],
    )
    custom_subject, custom_body = override or (None, None)
    try:
        return bool(
            await asyncio.to_thread(
                email_service.send_admin_email_confirmation,
                to_email=user.email,
                confirm_url=confirm_url,
                username=user.first_name,
                language=language,
                custom_subject=custom_subject,
                custom_body_html=custom_body,
            )
        )
    except Exception as e:
        logger.error('Не удалось отправить ссылку подтверждения почты', user_id=user.id, error=str(e)[:200])
        return False


@router.put('/{user_id}/email', response_model=AdminUserEmailResponse)
async def set_user_email(
    user_id: int,
    request: AdminSetUserEmailRequest,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Указать пользователю почту. Она неподтверждена, пока человек не перейдёт по ссылке."""
    user = await _load_user(db, user_id)
    new_email = str(request.email).strip().lower()

    if user.email and user.email.lower() == new_email and user.email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Email is already set and verified')

    # Адрес из ADMIN_EMAILS после подтверждения дал бы права администратора.
    if new_email in settings.get_admin_emails():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='This email address cannot be linked to a user account.',
        )

    if disposable_email_service.is_disposable(new_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail='Disposable email addresses are not allowed'
        )

    if await is_email_taken(db, new_email, exclude_user_id=user.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='This email is already registered')

    base_url = _require_mail_ready()

    old_email = user.email
    user.email = new_email
    user.email_verified = False
    user.email_verified_at = None
    user.email_verification_source = None
    # Незавершённая смена почты самим пользователем теряет смысл.
    user.email_change_new = None
    user.email_change_code = None
    user.email_change_expires = None
    token = _issue_token(user)
    user.updated_at = datetime.now(UTC)

    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action='set_user_email',
        resource_type='user',
        resource_id=str(user.id),
        details={'old_email': old_email, 'new_email': new_email},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='This email is already registered')

    sent = await _send_confirmation(db, user, base_url, token)
    logger.info(
        'Admin set email for user',
        admin_id=admin.id,
        user_id=user.id,
        had_email=bool(old_email),
        confirmation_sent=sent,
    )
    return _response(user, email_sent=sent)


@router.post('/{user_id}/email/resend', response_model=AdminUserEmailResponse)
async def resend_user_email_confirmation(
    user_id: int,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Отправить ссылку подтверждения ещё раз (старая ссылка перестаёт работать)."""
    user = await _load_user(db, user_id)
    if not user.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='User has no email')
    if user.email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Email is already verified')

    base_url = _require_mail_ready()
    token = _issue_token(user)
    user.updated_at = datetime.now(UTC)
    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action='resend_user_email_confirmation',
        resource_type='user',
        resource_id=str(user.id),
        details={'email': user.email},
    )
    await db.commit()

    sent = await _send_confirmation(db, user, base_url, token)
    logger.info('Admin resent email confirmation', admin_id=admin.id, user_id=user.id, confirmation_sent=sent)
    return _response(user, email_sent=sent)


@router.delete('/{user_id}/email', response_model=AdminUserEmailResponse)
async def delete_user_email(
    user_id: int,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Убрать почту из аккаунта. Нельзя, если почта — единственный способ входа."""
    user = await _load_user(db, user_id)
    if not user.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='User has no email')
    if not _has_other_login(user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Email is the only login method for this account',
        )

    old_email = user.email
    user.email = None
    user.email_verified = False
    user.email_verified_at = None
    user.email_verification_source = None
    user.email_verification_token = None
    user.email_verification_expires = None
    user.password_reset_token = None
    user.password_reset_expires = None
    user.email_change_new = None
    user.email_change_code = None
    user.email_change_expires = None
    user.updated_at = datetime.now(UTC)

    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action='delete_user_email',
        resource_type='user',
        resource_id=str(user.id),
        details={'old_email': old_email},
    )
    await db.commit()

    logger.info('Admin removed email from user', admin_id=admin.id, user_id=user.id)
    return _response(user)


@router.get('/{user_id}/emails', response_model=AdminUserEmailLogResponse)
async def get_user_email_log(
    user_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Письма пользователя: по владельцу на момент отправки и по текущему адресу."""
    user = await _load_user(db, user_id)

    conditions = [EmailLogEntry.user_id == user.id]
    if user.email:
        conditions.append(EmailLogEntry.to_email == user.email.strip().lower())
    where = or_(*conditions)

    total = (await db.execute(select(func.count(EmailLogEntry.id)).where(where))).scalar_one()
    rows = (
        (
            await db.execute(
                select(EmailLogEntry)
                .where(where)
                .order_by(EmailLogEntry.created_at.desc(), EmailLogEntry.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return AdminUserEmailLogResponse(
        items=[
            AdminUserEmailLogItem(
                id=row.id,
                to_email=row.to_email,
                email_type=row.email_type,
                subject=row.subject,
                status=row.status,
                error=row.error,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        retention_days=settings.get_email_log_retention_days(),
    )
