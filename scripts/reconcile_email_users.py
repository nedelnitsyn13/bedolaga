#!/usr/bin/env python
"""One-shot fixup for legacy email-registered users migrated before this fix existed.

``migrate_shopbot`` originally imported every legacy ``users`` row as a
Telegram-auth account, carrying its ``telegram_id`` over verbatim. For rows
that actually registered through the legacy web/email flow that value is a
synthetic integer (the legacy schema's ``telegram_id`` is a NOT NULL primary
key, so it has no real "email-only" concept) — not a real Telegram id. A
later fix taught ``migrate_shopbot`` to recognise ``users.auth_email`` and
import such rows as proper bedolaga email-auth accounts instead
(``auth_type='email'``, ``telegram_id=NULL``, ``email``/``password_hash``
set) — but that fix does nothing for rows a PREVIOUS run of the script
already created the old (wrong) way: migrate_shopbot's own dedup keys off
``telegram_id``, so it just sees "already exists" and skips them, leaving
the broken record in place forever.

This script finds exactly those already-migrated rows (by matching the
legacy row's synthetic ``telegram_id`` against an existing bedolaga user)
and converts them in place to proper email accounts — same identity fixup
``migrate_shopbot`` would have done, applied after the fact. Converting the
SAME row (not creating a new one) means every subscription/transaction/
referral already linked to that user's internal id stays linked; only the
identity fields change.

Usage:
    python -m scripts.reconcile_email_users --source /path/to/users.db              # dry run
    python -m scripts.reconcile_email_users --source /path/to/users.db --apply      # persist
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.system_settings_service import bot_configuration_service
from scripts.migrate_shopbot import _LEGACY_EMAIL_VERIFICATION_SOURCE, _looks_like_bcrypt_hash


logger = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    dry_run: bool
    legacy_email_rows: int = 0
    already_correct: int = 0
    not_migrated_yet: int = 0
    converted: int = 0
    password_carried: int = 0
    password_needs_reset: int = 0
    skipped_email_conflict: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


def _load_legacy_users(source_path: Path) -> list[dict]:
    uri = f'file:{source_path}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('SELECT telegram_id, auth_email, auth_pass FROM users').fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


async def _reconcile(db, legacy_users: list[dict], *, apply: bool) -> ReconcileReport:
    report = ReconcileReport(dry_run=not apply)

    email_rows = [row for row in legacy_users if row.get('auth_email')]
    report.legacy_email_rows = len(email_rows)

    for row in email_rows:
        old_tg_id = row['telegram_id']
        auth_email = row['auth_email'].strip().lower()

        existing_email_user = (await db.execute(select(User).where(User.email == auth_email))).scalar_one_or_none()

        if existing_email_user is not None and existing_email_user.auth_type == 'email':
            report.already_correct += 1
            continue

        broken_user = (await db.execute(select(User).where(User.telegram_id == old_tg_id))).scalar_one_or_none()

        if broken_user is None:
            report.not_migrated_yet += 1
            continue

        if existing_email_user is not None and existing_email_user.id != broken_user.id:
            report.skipped_email_conflict += 1
            report.unresolved_lines.append(
                f'legacy telegram_id={old_tg_id} email={auth_email}: email already belongs to a '
                f'DIFFERENT bedolaga user (id={existing_email_user.id}) — skipped, needs manual review'
            )
            continue

        broken_user.telegram_id = None
        broken_user.auth_type = 'email'
        broken_user.email = auth_email
        broken_user.email_verified = True
        broken_user.email_verification_source = _LEGACY_EMAIL_VERIFICATION_SOURCE

        auth_pass = row.get('auth_pass')
        if _looks_like_bcrypt_hash(auth_pass):
            broken_user.password_hash = auth_pass
            report.password_carried += 1
        else:
            report.password_needs_reset += 1
            report.unresolved_lines.append(
                f'user id={broken_user.id} email={auth_email}: auth_pass is not a bcrypt hash, '
                f'password not carried over — user must reset their password'
            )

        report.converted += 1

    await db.flush()
    return report


def _print_report(report: ReconcileReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  email-строк в легаси            : {report.legacy_email_rows}')
    print(f'  уже мигрировано верно           : {report.already_correct}')
    print(f'  ещё не мигрировано (не эта задача): {report.not_migrated_yet}')
    print(f'  сконвертировано                 : {report.converted}')
    print(f'  пароль перенесён                : {report.password_carried}')
    print(f'  нужен сброс пароля              : {report.password_needs_reset}')
    print(f'  пропущено (конфликт email)      : {report.skipped_email_conflict}')
    print()
    if report.unresolved_lines:
        print(f'  !! строк с замечаниями: {len(report.unresolved_lines)} (первые 30)')
        for line in report.unresolved_lines[:30]:
            print(f'     {line}')
    print('=' * 70)


def _write_audit(report: ReconcileReport, *, committed: bool) -> str | None:
    directory = Path(os.environ.get('MIGRATION_AUDIT_DIR') or settings.LOG_DIR or 'logs')
    suffix = 'apply' if committed else 'dryrun'
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    path = directory / f'reconcile_email_users_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        payload['committed'] = committed
        payload['unresolved_lines'] = report.unresolved_lines
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning('reconcile_email_users: не удалось записать отчёт', path=str(path), error=str(error))
        return None
    return str(path)


async def _run(args: argparse.Namespace, source_path: Path) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    legacy_users = _load_legacy_users(source_path)
    print(f'  источник: {len(legacy_users)} users')

    async with AsyncSessionLocal() as db:
        report = await _reconcile(db, legacy_users, apply=args.apply)
        if args.apply:
            await db.commit()
        else:
            await db.rollback()

    _print_report(report)
    audit_path = _write_audit(report, committed=args.apply)
    if audit_path:
        print(f'  полный отчёт: {audit_path}')

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Convert already-migrated legacy email-registered users (created as broken '
            'Telegram-auth rows by an older migrate_shopbot run) into proper email accounts'
        )
    )
    parser.add_argument('--source', required=True, help='path to the legacy users.db SQLite file')
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    source_path = Path(args.source).expanduser()
    if not source_path.exists():
        print(f'  !! файл не найден: {source_path}')
        return 2

    return asyncio.run(_run(args, source_path))


if __name__ == '__main__':
    raise SystemExit(main())
