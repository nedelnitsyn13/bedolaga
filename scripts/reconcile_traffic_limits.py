#!/usr/bin/env python
"""One-shot backfill for ``Subscription.traffic_limit_gb`` from the live Remnawave panel.

Same gap as ``reconcile_device_limits.py``, different field: ``migrate_shopbot``
set every imported subscription's ``traffic_limit_gb`` from the legacy
``vpn_keys.traffic_limit_bytes`` column — a snapshot of the OLD shopbot's own
SQLite row, not necessarily the real, current panel value at cutover time.

The regular "Из панели" bulk sync / ``REMNAWAVE_AUTO_SYNC_ENABLED`` background
job (``BULK_SNAPSHOT`` policy, see ``app/services/panel_sync/projection.py``)
deliberately does NOT pull ``trafficLimitBytes`` back from the panel during
routine passes — that field is normally owned by the bot's own tariff/purchase
logic, and blindly trusting the panel there would let an admin's one-off panel
tweak silently override a paying customer's purchased traffic tier. Only an
explicit "panel wins" action (``ADMIN_PULL``) touches it, and only per-user
from the admin UI — there is no bulk equivalent, hence this one-off script for
the migrated cohort specifically.

Panel semantics: ``trafficLimitBytes=0`` means unlimited, matching how
``migrate_shopbot`` itself already treats a legacy 0 value (0 GB = unlimited
in bedolaga too) — so no special-casing is needed here, unlike the device
limit's None/0 distinction.

Scope: only subscriptions with ``tariff_id IS NULL``. A tariffed subscription's
traffic_limit_gb is governed by its ``Tariff.traffic_limit_gb``, not the panel —
overwriting it from a live panel read would fight the tariff system for any
subscription that has since been assigned one. ``migrate_shopbot`` leaves
``tariff_id`` NULL on every subscription it imports (no legacy-plan ->
tariff mapping exists), so this filter targets exactly the migrated cohort
and never touches a tariff-governed subscription.

Usage:
    python -m scripts.reconcile_traffic_limits              # dry run
    python -m scripts.reconcile_traffic_limits --apply       # persist
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

_BYTES_PER_GB = 1024**3


@dataclass
class ReconcileReport:
    dry_run: bool
    subscriptions_checked: int = 0
    updated: int = 0
    already_correct: int = 0
    skipped_panel_not_found: int = 0
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
                    Subscription.tariff_id.is_(None),
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

        real_limit_bytes = panel_user.traffic_limit_bytes or 0
        real_limit_gb = real_limit_bytes // _BYTES_PER_GB if real_limit_bytes > 0 else 0

        if subscription.traffic_limit_gb == real_limit_gb:
            report.already_correct += 1
            continue

        report.unresolved_lines.append(
            f'subscription id={subscription.id} remnawave_id={subscription.remnawave_id}: '
            f'traffic_limit_gb {subscription.traffic_limit_gb} -> {real_limit_gb}'
        )
        subscription.traffic_limit_gb = real_limit_gb
        report.updated += 1

    await db.flush()
    return report


def _print_report(report: ReconcileReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  подписок проверено                     : {report.subscriptions_checked}')
    print(f'  обновлено (traffic_limit_gb из панели)  : {report.updated}')
    print(f'  уже совпадало                           : {report.already_correct}')
    print(f'  пропущено (не найдено в панели)         : {report.skipped_panel_not_found}')
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
    path = directory / f'reconcile_traffic_limits_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        payload['committed'] = committed
        payload['unresolved_lines'] = report.unresolved_lines
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning('reconcile_traffic_limits: не удалось записать отчёт', path=str(path), error=str(error))
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
            'Backfill Subscription.traffic_limit_gb from the live Remnawave panel for subscriptions '
            'imported by migrate_shopbot from a stale legacy snapshot'
        )
    )
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
