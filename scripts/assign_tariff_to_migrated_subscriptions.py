#!/usr/bin/env python
"""One-shot: label migrated subscriptions with a tariff WITHOUT touching their real state.

``migrate_shopbot`` leaves ``tariff_id`` NULL on every subscription it imports
(no legacy-plan -> tariff mapping was defined at cutover time). Once a real
tariff is created in bedolaga that mirrors the legacy pricing, migrated users
should be labelled with it — but the cabinet's own bulk "Change tariff" action
(``BulkActionType.CHANGE_TARIFF``, see ``app/cabinet/routes/admin_bulk_actions.py``)
is NOT safe for this: it unconditionally resets ``traffic_limit_gb`` and
``device_limit`` to the tariff's base values (discarding whatever a migrated
subscription's real panel-derived limits are — see
``reconcile_device_limits.py`` / ``reconcile_traffic_limits.py``) and replaces
``connected_squads`` with the tariff's ``allowed_squads`` (which can be empty,
severing the user's actual server access).

This script does the one thing that operation was actually asked for here:
set ``Subscription.tariff_id`` on the migrated cohort and nothing else.
end_date, status, device_limit, traffic_limit_gb, traffic_used_gb and
connected_squads are left exactly as they are (i.e. whatever the earlier
migration + reconcile_* passes already established as correct).

Scope: subscriptions with ``tariff_id IS NULL`` and a ``remnawave_id`` (i.e.
the migrated cohort), in an active/trial/limited status.

Usage:
    python -m scripts.assign_tariff_to_migrated_subscriptions --tariff-id 3              # dry run
    python -m scripts.assign_tariff_to_migrated_subscriptions --tariff-id 3 --apply       # persist
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
from app.database.crud.tariff import get_tariff_by_id
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)


@dataclass
class AssignReport:
    dry_run: bool
    tariff_id: int
    tariff_name: str
    subscriptions_found: int = 0
    assigned: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


async def _assign(db, tariff, *, apply: bool) -> AssignReport:
    report = AssignReport(dry_run=not apply, tariff_id=tariff.id, tariff_name=tariff.name)

    subscriptions = (
        (
            await db.execute(
                select(Subscription).where(
                    Subscription.tariff_id.is_(None),
                    Subscription.remnawave_id.is_not(None),
                    Subscription.status.in_(_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    report.subscriptions_found = len(subscriptions)

    for subscription in subscriptions:
        report.unresolved_lines.append(
            f'subscription id={subscription.id} user_id={subscription.user_id} '
            f'remnawave_id={subscription.remnawave_id}: tariff_id None -> {tariff.id} '
            f'(device_limit={subscription.device_limit}, traffic_limit_gb={subscription.traffic_limit_gb} — unchanged)'
        )
        subscription.tariff_id = tariff.id
        report.assigned += 1

    await db.flush()
    return report


def _print_report(report: AssignReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  тариф                                  : #{report.tariff_id} "{report.tariff_name}"')
    print(f'  найдено подписок без тарифа (мигрированные): {report.subscriptions_found}')
    print(f'  проставлен tariff_id                   : {report.assigned}')
    print()
    if report.unresolved_lines:
        print(f'  строк с деталями: {len(report.unresolved_lines)} (первые 30)')
        for line in report.unresolved_lines[:30]:
            print(f'     {line}')
    print('=' * 70)


def _write_audit(report: AssignReport, *, committed: bool) -> str | None:
    directory = Path(os.environ.get('MIGRATION_AUDIT_DIR') or settings.LOG_DIR or 'logs')
    suffix = 'apply' if committed else 'dryrun'
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    path = directory / f'assign_tariff_to_migrated_subscriptions_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        payload['committed'] = committed
        payload['unresolved_lines'] = report.unresolved_lines
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning(
            'assign_tariff_to_migrated_subscriptions: не удалось записать отчёт', path=str(path), error=str(error)
        )
        return None
    return str(path)


async def _run(args: argparse.Namespace) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    async with AsyncSessionLocal() as db:
        tariff = await get_tariff_by_id(db, args.tariff_id)
        if tariff is None:
            print(f'  !! тариф #{args.tariff_id} не найден')
            return 2

        report = await _assign(db, tariff, apply=args.apply)
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
            'Assign a tariff_id to migrated subscriptions without touching their real '
            'device_limit/traffic_limit_gb/connected_squads/end_date/status'
        )
    )
    parser.add_argument('--tariff-id', required=True, type=int, help='target tariff id (see admin cabinet)')
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
