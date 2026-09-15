#!/usr/bin/env python
"""Cut over ONE subscription from the legacy limited-companion architecture
to the new LIMITED squad architecture (see ``app/services/limited_squad_service.py``).

Background
----------
Legacy architecture (``LIMITED_COMPANION_ENABLED``, global): a second,
separate Remnawave user ("companion") on ``LIMITED_COMPANION_SQUAD_UUID``,
mirroring the main account's status/expiry, with its own fixed traffic quota.

New architecture (per-tariff ``Tariff.limited_traffic_enabled``): the MAIN
Remnawave user itself gets the tariff's ``limited_squad_uuids`` added to/removed
from ``activeInternalSquads`` depending on usage vs. ``limited_base_traffic_gb``
+ active top-ups. No second panel user.

Both architectures read/write the SAME ``LimitedCompanionTrafficPurchase`` table
(keyed by ``subscription_id`` only, no companion-specific columns) — top-up
history needs no migration at all, this script does not touch that table.

What this script does for one subscription, once its tariff has been switched
to ``limited_traffic_enabled=True`` (admin panel / bot admin, LIMITED squad
screen — NOT done by this script):
    1. Validates preconditions (tariff loaded, new arch enabled+configured,
       subscription actually has a legacy companion to migrate away from).
    2. Runs one cycle of the new-arch enforcement
       (``limited_squad_service.process_limited_traffic``) immediately, instead
       of waiting for the periodic monitoring job's next cycle — so the admin
       can verify the outcome (squad added/kept, usage computed) right away.
    3. Disables the legacy companion panel account (status DISABLED; a narrow
       PATCH — squads/traffic/expiry/local DB fields untouched, see
       ``panel_sync.disable_companion_account``).

Why step 3 doesn't need to be repeated per-subscription forever: as of this
script, ``SubscriptionService._sync_limited_companion_user`` itself now skips
subscriptions whose tariff has the new architecture enabled — so once step 3
disables the companion once, nothing resurrects it back to ACTIVE on the next
renewal/admin edit. Before that guard existed, disabling here would have been
undone by the very next subscription push.

What this script deliberately does NOT do:
    - Flip the tariff to the new architecture — that's an admin decision made
      in the UI (LIMITED squad screen), on purpose reviewed per tariff.
    - Delete the companion panel account, or clear
      ``limited_companion_remnawave_id``/``limited_companion_short_uuid`` on the
      subscription — kept as history/audit trail; nothing reads them once the
      tariff has switched (see the guard above).
    - Touch the external subscription-merger's registration for this pair —
      there is no unregister endpoint in this codebase (only ``/register``,
      see ``SubscriptionService._register_limited_companion_mapping``); the
      merger keeps the mapping, but the companion side of it goes inert once
      DISABLED. If your merger deployment needs an explicit unregister, do
      that out of band.
    - Touch more than one subscription. This is intentionally single-target —
      per the migration plan, verify on one real subscription before ever
      considering anything broader.

Caveat worth knowing before running with --apply: the new architecture's
usage counter (``limited_traffic_used_gb``) starts by querying the panel for
how much traffic the MAIN account (not the companion) has already pushed
through the tariff's LIMITED nodes since ``subscription.start_date``. Since
the main account was never assigned to those nodes under the legacy
architecture, this will normally compute to 0 GB on migration — i.e. the
user's LIMITED-pool usage effectively resets to 0 on cutover. This is
expected, not a bug, but the admin should know if `--apply` is used on a user
close to their companion's traffic limit.

Usage:
    python -m scripts.migrate_limited_companion_to_squad --subscription-id 123            # dry run
    python -m scripts.migrate_limited_companion_to_squad --subscription-id 123 --apply    # persist + disable companion
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from app.config import settings
from app.database.crud.subscription import get_subscription_by_id
from app.database.crud.user import get_user_by_id
from app.database.database import AsyncSessionLocal
from app.services.limited_squad_service import (
    get_active_limited_traffic_purchases_gb,
    get_effective_limited_traffic_limit_gb,
    get_limited_base_traffic_gb,
    get_limited_squad_uuids,
    is_limited_traffic_enabled,
    process_limited_traffic,
    resolve_main_panel_user_id,
)
from app.services.panel_sync import disable_companion_account
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


@dataclass
class MigrationReport:
    dry_run: bool
    subscription_id: int
    ok: bool = False
    reason: str | None = None
    companion_id: int | None = None
    main_panel_user_id: int | None = None
    squad_uuids: list[str] | None = None
    base_traffic_gb: int | None = None
    active_purchases_gb: int | None = None
    effective_limit_gb: int | None = None
    used_gb_before: float | None = None
    used_gb_after: float | None = None
    squad_active_after: bool | None = None
    companion_disabled: bool = False
    companion_disable_error: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


async def _validate(db, subscription) -> tuple[bool, str | None]:
    if subscription is None:
        return False, 'подписка не найдена'

    tariff = subscription.tariff
    if not is_limited_traffic_enabled(tariff):
        return False, (
            'тариф подписки не переведён на новую LIMITED squad архитектуру '
            '(Tariff.limited_traffic_enabled=false) — сначала настройте тариф в админке'
        )

    if not get_limited_squad_uuids(tariff):
        return False, "у тарифа не заданы limited_squad_uuids — нечего enforce'ить"

    if not subscription.limited_companion_remnawave_id:
        return False, 'у подписки нет legacy companion (limited_companion_remnawave_id пуст) — мигрировать нечего'

    return True, None


async def _migrate_one(db, api, subscription_id: int, *, apply: bool) -> MigrationReport:
    report = MigrationReport(dry_run=not apply, subscription_id=subscription_id)

    subscription = await get_subscription_by_id(db, subscription_id)
    ok, reason = await _validate(db, subscription)
    if not ok:
        report.reason = reason
        return report

    tariff = subscription.tariff
    user = await get_user_by_id(db, subscription.user_id)

    report.companion_id = subscription.limited_companion_remnawave_id
    report.main_panel_user_id = resolve_main_panel_user_id(subscription, user)
    report.squad_uuids = get_limited_squad_uuids(tariff)
    report.base_traffic_gb = get_limited_base_traffic_gb(tariff)
    report.active_purchases_gb = await get_active_limited_traffic_purchases_gb(db, subscription)
    report.effective_limit_gb = await get_effective_limited_traffic_limit_gb(db, subscription, tariff)
    report.used_gb_before = subscription.limited_traffic_used_gb or 0.0

    if not apply:
        report.ok = True
        return report

    await process_limited_traffic(api, db, subscription, tariff, user)
    await db.refresh(subscription)
    report.used_gb_after = subscription.limited_traffic_used_gb or 0.0
    report.squad_active_after = subscription.limited_squad_active

    try:
        report.companion_disabled = await disable_companion_account(api, subscription)
    except Exception as error:
        report.companion_disable_error = str(error)
        logger.warning(
            '⚠️ Не удалось отключить legacy companion-аккаунт после миграции',
            subscription_id=subscription_id,
            error=error,
        )

    report.ok = report.companion_disable_error is None
    return report


def _print_report(report: MigrationReport) -> None:
    print()
    print('=' * 70)
    print(f'  {"DRY RUN — ничего не записано" if report.dry_run else "APPLIED"} — subscription #{report.subscription_id}')
    print('=' * 70)
    if report.reason:
        print(f'  !! {report.reason}')
        print('=' * 70)
        return
    print(f'  companion panel id (legacy)         : {report.companion_id}')
    print(f'  main panel id (новая архитектура)   : {report.main_panel_user_id}')
    print(f'  squad_uuids тарифа                  : {report.squad_uuids}')
    print(f'  база LIMITED-пула, ГБ                : {report.base_traffic_gb}')
    print(f'  активные докупки, ГБ                 : {report.active_purchases_gb}')
    print(f'  итоговый лимит, ГБ (0=безлимит)      : {report.effective_limit_gb}')
    print(f'  usage до                             : {report.used_gb_before:.3f} ГБ')
    if not report.dry_run:
        print(f'  usage после process_limited_traffic  : {report.used_gb_after:.3f} ГБ')
        print(f'  LIMITED squad активен после          : {report.squad_active_after}')
        print(f'  companion отключён (status=DISABLED) : {report.companion_disabled}')
        if report.companion_disable_error:
            print(f'  !! ошибка отключения companion       : {report.companion_disable_error}')
    print('=' * 70)


def _write_audit(report: MigrationReport, *, committed: bool) -> str | None:
    directory = Path(os.environ.get('MIGRATION_AUDIT_DIR') or settings.LOG_DIR or 'logs')
    suffix = 'apply' if committed else 'dryrun'
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    path = directory / f'migrate_limited_companion_to_squad_{report.subscription_id}_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8') as handle:
            json.dump(report.as_dict(), handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning('migrate_limited_companion_to_squad: не удалось записать отчёт', path=str(path), error=str(error))
        return None
    return str(path)


async def _run(subscription_id: int, *, apply: bool) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    remnawave_service = RemnaWaveService()
    async with AsyncSessionLocal() as db, remnawave_service.get_api_client() as api:
        report = await _migrate_one(db, api, subscription_id, apply=apply)
        if apply and report.ok:
            await db.commit()
        else:
            await db.rollback()

    _print_report(report)
    audit_path = _write_audit(report, committed=apply and report.ok)
    if audit_path:
        print(f'  полный отчёт: {audit_path}')

    return 0 if report.ok or report.reason is None else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Cut over ONE subscription from the legacy limited-companion account to the new '
            'per-tariff LIMITED squad architecture. Single subscription only — no batch/--all mode, '
            'by design: verify on one real subscription before considering anything broader.'
        )
    )
    parser.add_argument('--subscription-id', type=int, required=True, help='ID подписки (не пользователя)')
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    return asyncio.run(_run(args.subscription_id, apply=args.apply))


if __name__ == '__main__':
    raise SystemExit(main())
