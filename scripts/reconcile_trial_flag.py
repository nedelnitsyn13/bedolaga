#!/usr/bin/env python
"""One-shot fixup: migrated subscriptions carrying a stale ``is_trial`` flag.

``migrate_shopbot`` sets ``Subscription.is_trial``/``status`` straight from the
legacy ``vpn_keys.is_trial`` column (see its own code:
``is_trial = bool(key.get('is_trial'))``). The legacy shopbot's own
``is_trial`` flag on a key was apparently never cleared once a user actually
paid — so a real, long-paying customer whose current key happened to be
created as a trial key years ago now shows up in bedolaga as "⭐ Триал" on a
subscription that is anything but a trial (real end_date years out, real
device counts, real legacy balance/spend history).

This script finds exactly that mismatch: a migrated subscription
(``remnawave_id IS NOT NULL``) with ``is_trial=True`` whose legacy user row
has ``total_spent > 0`` (i.e. they paid real money at some point in the old
bot) — a trial-only user has ``total_spent == 0`` by definition. For those,
it clears ``is_trial`` and flips ``status`` from ``TRIAL`` to ``ACTIVE``,
leaving everything else (end_date, device_limit, traffic_limit_gb, tariff_id,
squads) untouched.

A subscription whose legacy user has ``total_spent == 0`` is left alone —
for them ``is_trial=True`` may well be correct.

Usage:
    python -m scripts.reconcile_trial_flag --source /path/to/users.db              # dry run
    python -m scripts.reconcile_trial_flag --source /path/to/users.db --apply      # persist
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
from app.database.models import Subscription, SubscriptionStatus, User
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


def _load_legacy_total_spent(source_path: Path) -> dict[int, float]:
    uri = f'file:{source_path}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('SELECT telegram_id, total_spent FROM users').fetchall()
    finally:
        conn.close()
    return {row['telegram_id']: row['total_spent'] or 0.0 for row in rows}


@dataclass
class ReconcileReport:
    dry_run: bool
    subscriptions_checked: int = 0
    fixed: int = 0
    left_as_trial: int = 0
    skipped_no_legacy_match: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


async def _reconcile(db, legacy_total_spent: dict[int, float], *, apply: bool) -> ReconcileReport:
    report = ReconcileReport(dry_run=not apply)

    rows = (
        (
            await db.execute(
                select(Subscription, User)
                .join(User, User.id == Subscription.user_id)
                .where(
                    Subscription.remnawave_id.is_not(None),
                    Subscription.is_trial.is_(True),
                    Subscription.status == SubscriptionStatus.TRIAL.value,
                )
            )
        )
        .all()
    )
    report.subscriptions_checked = len(rows)

    for subscription, user in rows:
        if user.telegram_id is None or user.telegram_id not in legacy_total_spent:
            report.skipped_no_legacy_match += 1
            continue

        total_spent = legacy_total_spent[user.telegram_id]
        if total_spent <= 0:
            report.left_as_trial += 1
            continue

        report.unresolved_lines.append(
            f'subscription id={subscription.id} user_id={user.id} telegram_id={user.telegram_id}: '
            f'is_trial True -> False, status TRIAL -> ACTIVE (legacy total_spent={total_spent:.2f} ₽)'
        )
        subscription.is_trial = False
        subscription.status = SubscriptionStatus.ACTIVE.value
        report.fixed += 1

    await db.flush()
    return report


def _print_report(report: ReconcileReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  подписок с is_trial=True проверено     : {report.subscriptions_checked}')
    print(f'  исправлено (реально платили в старом боте): {report.fixed}')
    print(f'  оставлено триалом (total_spent=0)      : {report.left_as_trial}')
    print(f'  пропущено (нет legacy telegram_id)     : {report.skipped_no_legacy_match}')
    print()
    if report.unresolved_lines:
        print(f'  строк с деталями: {len(report.unresolved_lines)} (первые 30)')
        for line in report.unresolved_lines[:30]:
            print(f'     {line}')
    print('=' * 70)


def _write_audit(report: ReconcileReport, *, committed: bool) -> str | None:
    directory = Path(os.environ.get('MIGRATION_AUDIT_DIR') or settings.LOG_DIR or 'logs')
    suffix = 'apply' if committed else 'dryrun'
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    path = directory / f'reconcile_trial_flag_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        payload['committed'] = committed
        payload['unresolved_lines'] = report.unresolved_lines
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning('reconcile_trial_flag: не удалось записать отчёт', path=str(path), error=str(error))
        return None
    return str(path)


async def _run(args: argparse.Namespace, source_path: Path) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    legacy_total_spent = _load_legacy_total_spent(source_path)
    print(f'  источник: {len(legacy_total_spent)} users')

    async with AsyncSessionLocal() as db:
        report = await _reconcile(db, legacy_total_spent, apply=args.apply)
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
            'Clear a stale is_trial flag on migrated subscriptions whose legacy user actually '
            'paid (total_spent > 0), inherited as-is from vpn_keys.is_trial by migrate_shopbot'
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
