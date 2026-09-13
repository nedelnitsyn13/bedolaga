"""A blank ``.env`` value for an optional numeric setting must not crash the bot.

Incident: an admin cleared ``ADMIN_REPORTS_TOPIC_ID=`` in ``.env`` (leaving the
key but an empty value) instead of deleting the line or leaving it unset.
Pydantic does not coerce an empty string to ``None`` for an ``int | None``
field on its own, so ``Settings()`` raised a ``ValidationError`` at import
time and the whole bot crash-looped — not just the reporting feature that
was actually being configured.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    'field_name',
    [
        'ADMIN_NOTIFICATIONS_TOPIC_ID',
        'ADMIN_REPORTS_TOPIC_ID',
        'DEVICES_SELECTION_DISABLED_AMOUNT',
        'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID',
        'MULENPAY_SHOP_ID',
        'FREEKASSA_SHOP_ID',
        'LOG_ROTATION_TOPIC_ID',
        'BACKUP_SEND_TOPIC_ID',
    ],
)
def test_blank_env_value_becomes_none_instead_of_crashing(monkeypatch, field_name):
    monkeypatch.setenv('BOT_TOKEN', '123:abc')
    monkeypatch.setenv(field_name, '')

    settings = Settings()

    assert getattr(settings, field_name) is None


def test_a_real_numeric_value_still_parses(monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', '123:abc')
    monkeypatch.setenv('ADMIN_REPORTS_TOPIC_ID', '148')

    settings = Settings()

    assert settings.ADMIN_REPORTS_TOPIC_ID == 148


def test_garbage_value_still_fails_loudly(monkeypatch):
    """Only blank strings are forgiven — a typo'd value should still fail fast."""
    monkeypatch.setenv('BOT_TOKEN', '123:abc')
    monkeypatch.setenv('ADMIN_REPORTS_TOPIC_ID', 'not-a-number')

    with pytest.raises(ValidationError):
        Settings()
