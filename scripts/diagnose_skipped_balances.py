#!/usr/bin/env python
"""Read-only diagnostic: find legacy users whose balance ``migrate_shopbot`` never touched.

``migrate_shopbot``'s own safety boundary (see its module docstring) only
ever CREATES data for a legacy ``telegram_id``/``email`` that does NOT
already exist as a bedolaga user. If a bedolaga user with that identity
already existed at migration time (e.g. an admin/staff account created by
logging into the new bot before the cutover), that user's
``balance_kopeks`` is left completely untouched — the legacy wallet balance
from ``remnawave-shopbot`` is simply never applied for them.

This script does NOT modify anything. For every legacy user row it finds a
matching bedolaga user (by ``telegram_id``, or by ``auth_email`` for
email-only legacy rows) and reports:
    - legacy balance / total_spent (rubles, from ``remnawave-shopbot``)
    - current bedolaga balance (rubles)
    - whether that bedolaga user has any transaction tagged
      ``[миграция из shopbot]`` — if yes, they were migrated normally by
      ``migrate_shopbot`` and their balance should already reflect the
      legacy value; if no such transaction exists despite a matching legacy
      row, this user's balance was silently skipped and needs a manual
      decision (top up the delta? already correct by coincidence? ignore
      because it's a disposable test account?).

Only rows with a mismatch (or with no migration-tag transactions at all)
are printed, to keep the output focused on what actually needs a decision.

Usage:
    python -m scripts.diagnose_skipped_balances --source /path/to/users.db
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from sqlalchemy import func, select

from app.database.database import AsyncSessionLocal
from app.database.models import Transaction, User
from app.services.system_settings_service import bot_configuration_service


_MIGRATION_TAG = '[миграция из shopbot]'


def _load_legacy_users(source_path: Path) -> list[dict]:
    uri = f'file:{source_path}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            'SELECT telegram_id, auth_email, balance, total_spent FROM users'
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


async def _diagnose(db, legacy_users: list[dict]) -> None:
    telegram_ids = [row['telegram_id'] for row in legacy_users if not row.get('auth_email')]
    emails = [row['auth_email'].strip().lower() for row in legacy_users if row.get('auth_email')]

    users_by_tg: dict[int, User] = {}
    if telegram_ids:
        rows = (await db.execute(select(User).where(User.telegram_id.in_(telegram_ids)))).scalars().all()
        users_by_tg = {u.telegram_id: u for u in rows}

    users_by_email: dict[str, User] = {}
    if emails:
        rows = (await db.execute(select(User).where(func.lower(User.email).in_(emails)))).scalars().all()
        users_by_email = {u.email.lower(): u for u in rows if u.email}

    print()
    print('=' * 100)
    print('  Легаси-пользователи, чей текущий баланс в bedolaga НЕ гарантированно совпадает с легаси')
    print('=' * 100)

    checked = 0
    flagged = 0

    for row in legacy_users:
        auth_email = (row.get('auth_email') or '').strip().lower() or None
        bedolaga_user = users_by_email.get(auth_email) if auth_email else users_by_tg.get(row['telegram_id'])
        if bedolaga_user is None:
            continue

        checked += 1

        legacy_balance_rub = row.get('balance') or 0.0
        legacy_total_spent_rub = row.get('total_spent') or 0.0
        current_balance_rub = bedolaga_user.balance_kopeks / 100

        has_migration_txn = (
            await db.execute(
                select(Transaction.id)
                .where(Transaction.user_id == bedolaga_user.id, Transaction.description.like(f'{_MIGRATION_TAG}%'))
                .limit(1)
            )
        ).scalar_one_or_none() is not None

        mismatch = abs(current_balance_rub - legacy_balance_rub) > 0.01

        if has_migration_txn and not mismatch:
            continue

        flagged += 1
        identity = f'telegram_id={bedolaga_user.telegram_id}' if bedolaga_user.telegram_id else f'email={auth_email}'
        status = 'МИГРИРОВАН (транзакции с тегом есть)' if has_migration_txn else 'ПРОПУЩЕН (баланс не тронут миграцией)'
        print()
        print(f'  bedolaga user_id={bedolaga_user.id} {identity} — {status}')
        print(f'    легаси баланс      : {legacy_balance_rub:.2f} ₽  (total_spent={legacy_total_spent_rub:.2f} ₽)')
        print(f'    текущий баланс bedolaga : {current_balance_rub:.2f} ₽')
        print(f'    разница            : {current_balance_rub - legacy_balance_rub:+.2f} ₽')

    print()
    print('=' * 100)
    print(f'  сверено легаси-строк с существующим bedolaga-пользователем : {checked}')
    print(f'  строк с расхождением/без миграционной метки                : {flagged}')
    print('=' * 100)


async def _run(source_path: Path) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)
    legacy_users = _load_legacy_users(source_path)
    print(f'  источник: {len(legacy_users)} users')

    async with AsyncSessionLocal() as db:
        await _diagnose(db, legacy_users)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Read-only: find bedolaga users whose balance migrate_shopbot skipped')
    parser.add_argument('--source', required=True, help='path to the legacy users.db SQLite file')
    args = parser.parse_args()

    source_path = Path(args.source).expanduser()
    if not source_path.exists():
        print(f'  !! файл не найден: {source_path}')
        return 2

    return asyncio.run(_run(source_path))


if __name__ == '__main__':
    raise SystemExit(main())
