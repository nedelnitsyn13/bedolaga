#!/usr/bin/env python
"""One-shot fixup for `Subscription` rows imported by ``migrate_shopbot``.

``migrate_shopbot`` imports every legacy ``vpn_keys`` row as its own
independent multi-tariff ``Subscription`` — including "limited server"
companion keys, which the legacy bot never modelled as a distinct concept in
its own DB. bedolaga models a companion as two fields on the MAIN
subscription (``limited_companion_remnawave_id`` / ``limited_companion_short_uuid``,
see ``LIMITED_COMPANION_ENABLED`` and ``SubscriptionService._sync_limited_companion_user``),
not as a second ``Subscription`` row. Left unreconciled, a migrated companion
identity:
    - is invisible to ``_sync_limited_companion_user``, so it stops being
      kept in sync with the main account's status/expiry on renewal;
    - if it happens to also exist as a leftover standalone ``Subscription``
      row, shows up as a bogus second subscription in the bot UI/cabinet and
      permanently occupies the partial unique index on
      ``Subscription.remnawave_id``.

Source of truth: the external ``subscription-merger`` service's own
``mappings.json`` — NOT the legacy bot's local ``vpn_keys`` table. Two earlier
versions of this script tried the local DB first (squad membership, then
``vpn_keys.is_primary``); both undercounted by an order of magnitude (21
locally-flagged companion keys vs. 326 real panel accounts on the dedicated
squad) because most companion accounts were created directly by the merger
and never wrote back to the bot's own DB. ``mappings.json`` is a dict keyed by
the MAIN key's ``short_uuid``, with each value carrying ``limited_token`` (the
companion's ``short_uuid``) — this is the exact registration payload
``SubscriptionService._register_limited_companion_mapping`` sends going
forward, so old and new bot writes share the same shape.

For each mapping entry this script:
    1. Finds the bedolaga ``Subscription`` whose ``remnawave_short_uuid``
       equals the main key's short_uuid.
    2. Resolves the companion's numeric panel id — first by checking whether
       a leftover standalone ``Subscription`` row exists for
       ``limited_token`` (a companion that WAS separately migrated by
       ``migrate_shopbot`` via a legacy ``vpn_keys`` row; that row is then
       folded in and deleted), otherwise by asking the live Remnawave panel
       (``GET /api/users/by-short-uuid/{limited_token}``) — most companions
       only exist in the panel, never in bedolaga's DB at all.
    3. Writes ``limited_companion_remnawave_id``/``limited_companion_short_uuid``
       onto the main subscription.

Pairing is conservative on purpose: a main subscription that can't be found,
or a companion identity the panel doesn't recognise, is reported and left
untouched rather than guessed at. This is live production panel-identity
data; a wrong link corrupts which panel account the bot manages for that
user going forward.

Usage:
    python -m scripts.reconcile_limited_companions --mappings /path/to/mappings.json              # dry run
    python -m scripts.reconcile_limited_companions --mappings /path/to/mappings.json --apply       # persist
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
from app.database.models import Subscription
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    dry_run: bool
    mapping_entries: int = 0
    pairs_linked: int = 0
    linked_via_local_subscription: int = 0
    linked_via_panel_lookup: int = 0
    skipped_already_linked: int = 0
    skipped_main_not_found: int = 0
    skipped_companion_not_found: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


def _load_mappings(path: Path) -> dict:
    with path.open('r', encoding='utf-8') as handle:
        return json.load(handle)


async def _reconcile(db, api, mappings: dict, *, apply: bool) -> ReconcileReport:
    report = ReconcileReport(dry_run=not apply)
    report.mapping_entries = len(mappings)

    for main_short_uuid, info in mappings.items():
        limited_token = (info or {}).get('limited_token')
        if not limited_token:
            continue

        main_sub = (
            await db.execute(select(Subscription).where(Subscription.remnawave_short_uuid == main_short_uuid))
        ).scalar_one_or_none()
        if main_sub is None:
            report.skipped_main_not_found += 1
            continue

        if main_sub.limited_companion_remnawave_id:
            report.skipped_already_linked += 1
            continue

        # A companion that migrate_shopbot also happened to import as its own
        # standalone row (legacy vpn_keys.is_primary=0) — fold it in without a
        # panel round-trip, and delete the now-redundant row.
        limited_sub = (
            await db.execute(select(Subscription).where(Subscription.remnawave_short_uuid == limited_token))
        ).scalar_one_or_none()

        if limited_sub is not None:
            main_sub.limited_companion_remnawave_id = limited_sub.remnawave_id
            main_sub.limited_companion_short_uuid = limited_sub.remnawave_short_uuid
            await db.delete(limited_sub)
            report.linked_via_local_subscription += 1
            report.pairs_linked += 1
            continue

        try:
            companion_user = await api.get_user_by_short_uuid(limited_token)
        except Exception as error:
            report.skipped_companion_not_found += 1
            report.unresolved_lines.append(
                f'main_short_uuid={main_short_uuid}: panel lookup for limited_token={limited_token} '
                f'failed ({error}) — skipped, needs manual review'
            )
            continue

        if companion_user is None:
            report.skipped_companion_not_found += 1
            report.unresolved_lines.append(
                f'main_short_uuid={main_short_uuid}: limited_token={limited_token} not found in the panel '
                f'— skipped, needs manual review'
            )
            continue

        main_sub.limited_companion_remnawave_id = companion_user.id
        main_sub.limited_companion_short_uuid = companion_user.short_uuid or limited_token
        report.linked_via_panel_lookup += 1
        report.pairs_linked += 1

    await db.flush()
    return report


def _print_report(report: ReconcileReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  записей в mappings.json                : {report.mapping_entries}')
    print(f'  пар связано                            : {report.pairs_linked}')
    print(f'    из них через локальную Subscription  : {report.linked_via_local_subscription}')
    print(f'    из них через живой запрос в панель   : {report.linked_via_panel_lookup}')
    print(f'  пропущено (уже связано)                : {report.skipped_already_linked}')
    print(f'  пропущено (основная подписка не найдена): {report.skipped_main_not_found}')
    print(f'  пропущено (компаньон не найден в панели): {report.skipped_companion_not_found}')
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
    path = directory / f'reconcile_limited_companions_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        payload['committed'] = committed
        payload['unresolved_lines'] = report.unresolved_lines
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning('reconcile_limited_companions: не удалось записать отчёт', path=str(path), error=str(error))
        return None
    return str(path)


async def _run(args: argparse.Namespace, mappings_path: Path) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    logger.info(
        'reconcile_limited_companions: конфигурация загружена',
        limited_companion_enabled=settings.LIMITED_COMPANION_ENABLED,
        limited_companion_squad_uuid=settings.LIMITED_COMPANION_SQUAD_UUID,
    )
    if not settings.is_limited_companion_enabled():
        print(
            '  !! LIMITED_COMPANION_ENABLED/LIMITED_COMPANION_SQUAD_UUID не заданы -- '
            'без них подписки со связанным компаньоном не будут синхронизированы вперёд, продолжаю всё равно.'
        )

    mappings = _load_mappings(mappings_path)
    print(f'  источник: {len(mappings)} записей в mappings.json')

    remnawave_service = RemnaWaveService()
    async with AsyncSessionLocal() as db, remnawave_service.get_api_client() as api:
        report = await _reconcile(db, api, mappings, apply=args.apply)
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
            'Link migrated main subscriptions to their limited-server companion identity, '
            'using the subscription-merger mappings.json as the source of truth'
        )
    )
    parser.add_argument('--mappings', required=True, help='path to the subscription-merger mappings.json file')
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    mappings_path = Path(args.mappings).expanduser()
    if not mappings_path.exists():
        print(f'  !! файл не найден: {mappings_path}')
        return 2

    return asyncio.run(_run(args, mappings_path))


if __name__ == '__main__':
    raise SystemExit(main())
