#!/usr/bin/env python
"""One-shot backfill for ``device_limit`` on subscriptions imported by ``migrate_shopbot``.

``migrate_shopbot`` has no way to know each legacy user's real per-key device
tier (the legacy schema doesn't track it), so every imported ``Subscription``
was created with the same ``settings.DEFAULT_DEVICE_LIMIT`` fallback (see its
own "Known limitations" docstring). The REAL device limit for each user has
lived in the Remnawave panel the whole time — it's just never been pulled
back into bedolaga's DB.

This matters before ``REMNAWAVE_AUTO_SYNC_ENABLED`` is turned on: that flag
pushes bedolaga's DB state TO the panel, so until this backfill runs, turning
it on would overwrite every migrated user's real panel device limit with the
wrong default.

This script does the opposite direction, once: for every active/trial/limited
``Subscription`` row that has a ``remnawave_id`` (i.e. every migrated
subscription still in use), it reads the current ``hwidDeviceLimit`` from the
live panel and writes it onto ``Subscription.device_limit``. Rows whose panel
value already matches, or where the panel identity can't be resolved, are
reported and left untouched rather than guessed at.

Note: this only pulls ``device_limit``. Wallet balances were migrated as a
final snapshot by ``migrate_shopbot`` and are not re-synced here — the legacy
bot has since been shut down, so that snapshot is final.

Usage:
    python -m scripts.reconcile_device_limits              # dry run
    python -m scripts.reconcile_device_limits --apply       # persist
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)


@dataclass
class ReconcileReport:
    dry_run: bool
    subscriptions_checked: int = 0
    updated: int = 0
    already_correct: int = 0
    skipped_panel_not_found: int = 0
    skipped_no_panel_limit: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


async def _reconcile(db, api, *, apply: bool) -> ReconcileReport:
    report = ReconcileReport(dry_run=not apply)

    subscriptions = (
        (
            await db.execute(
                select(Subscription).where(
                    Subscription.remnawave_id.is_not(None),
                    Subscription.status.in_(_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    report.subscriptions_checked = len(subscriptions)

    for subscription in subscriptions:
        try:
            panel_user = await api.get_user_by_id(subscription.remnawave_id)
        except Exception as error:
            report.skipped_panel_not_found += 1
            report.unresolved_lines.append(
                f'subscription id={subscription.id} remnawave_id={subscription.remnawave_id}: '
                f'panel lookup failed ({error}) — skipped, needs manual review'
            )
            continue

        if panel_user is None:
            report.skipped_panel_not_found += 1
            report.unresolved_lines.append(
                f'subscription id={subscription.id} remnawave_id={subscription.remnawave_id}: '
                f'not found in the panel — skipped, needs manual review'
            )
            continue

        real_limit = panel_user.hwid_device_limit
        if not real_limit:
            report.skipped_no_panel_limit += 1
            report.unresolved_lines.append(
                f'subscription id={subscription.id} remnawave_id={subscription.remnawave_id}: '
                f'panel has no hwidDeviceLimit set — left untouched (device_limit={subscription.device_limit})'
            )
            continue

        if subscription.device_limit == real_limit:
            report.already_correct += 1
            continue

        report.unresolved_lines.append(
            f'subscription id={subscription.id} remnawave_id={subscription.remnawave_id}: '
            f'device_limit {subscription.device_limit} -> {real_limit}'
        )
        subscription.device_limit = real_limit
        report.updated += 1

    await db.flush()
    return report


def _print_report(report: ReconcileReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  подписок проверено                     : {report.subscriptions_checked}')
    print(f'  обновлено (device_limit из панели)     : {report.updated}')
    print(f'  уже совпадало                          : {report.already_correct}')
    print(f'  пропущено (не найдено в панели)        : {report.skipped_panel_not_found}')
    print(f'  пропущено (в панели нет hwidDeviceLimit): {report.skipped_no_panel_limit}')
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
    path = directory / f'reconcile_device_limits_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        payload['committed'] = committed
        payload['unresolved_lines'] = report.unresolved_lines
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning('reconcile_device_limits: не удалось записать отчёт', path=str(path), error=str(error))
        return None
    return str(path)


async def _run(args: argparse.Namespace) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    remnawave_service = RemnaWaveService()
    async with AsyncSessionLocal() as db, remnawave_service.get_api_client() as api:
        report = await _reconcile(db, api, apply=args.apply)
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
            'Backfill Subscription.device_limit from the live Remnawave panel for subscriptions '
            'imported by migrate_shopbot with the DEFAULT_DEVICE_LIMIT fallback'
        )
    )
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
