"""Email verification token generation and validation."""

import secrets
from datetime import UTC, datetime, timedelta

from app.config import settings


def generate_email_change_code() -> str:
    """
    Generate a 6-digit verification code for email change.

    Returns:
        6-digit numeric string
    """
    return str(secrets.randbelow(900000) + 100000)


def get_email_change_expires_at() -> datetime:
    """
    Get the expiration datetime for an email change code.

    Returns:
        Datetime when the email change code expires
    """
    minutes = settings.get_cabinet_email_change_code_expire_minutes()
    return datetime.now(UTC) + timedelta(minutes=minutes)


def generate_verification_token() -> str:
    """
    Generate a secure random verification token.

    Returns:
        32-character hex token string
    """
    return secrets.token_hex(32)


# Ссылки из писем «почту указал администратор» помечены префиксом: такая ссылка
# только подтверждает адрес и в аккаунт не входит. Обычная проверка почты
# (/email/verify) такой токен не принимает — иначе опечатка админа в адресе
# дала бы постороннему вход в чужой аккаунт.
ADMIN_EMAIL_TOKEN_PREFIX = 'adm_'


def generate_admin_email_token() -> str:
    """Токен ссылки подтверждения почты, указанной администратором."""
    return ADMIN_EMAIL_TOKEN_PREFIX + secrets.token_hex(32)


def is_admin_email_token(token: str | None) -> bool:
    return bool(token) and token.startswith(ADMIN_EMAIL_TOKEN_PREFIX)


def generate_password_reset_token() -> str:
    """
    Generate a secure random password reset token.

    Returns:
        32-character hex token string
    """
    return secrets.token_hex(32)


def get_verification_expires_at() -> datetime:
    """
    Get the expiration datetime for a verification token.

    Returns:
        Datetime when the verification token expires
    """
    hours = settings.get_cabinet_email_verification_expire_hours()
    return datetime.now(UTC) + timedelta(hours=hours)


def get_password_reset_expires_at() -> datetime:
    """
    Get the expiration datetime for a password reset token.

    Returns:
        Datetime when the password reset token expires
    """
    hours = settings.get_cabinet_password_reset_expire_hours()
    return datetime.now(UTC) + timedelta(hours=hours)


def is_token_expired(expires_at: datetime | None) -> bool:
    """
    Check if a token has expired.

    Args:
        expires_at: Token expiration datetime

    Returns:
        True if expired or no expiration set, False otherwise
    """
    if expires_at is None:
        return True
    return datetime.now(UTC) > expires_at
