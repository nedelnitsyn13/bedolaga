#!/usr/bin/env python
"""One-shot fixup for `Subscription` rows imported by ``migrate_shopbot``.

``migrate_shopbot`` imports every legacy ``vpn_keys`` row as its own
independent multi-tariff ``Subscription`` — including "limited server"
companion keys, which the legacy bot never modelled as a distinct concept in
its own schema (that link lived entirely in the external
``subscription-merger`` service). bedolaga itself models a companion as two
fields on the MAIN subscription (``limited_companion_remnawave_id`` /
``limited_companion_short_uuid``, see ``LIMITED_COMPANION_ENABLED`` and
``SubscriptionService._sync_limited_companion_user``), not as a second
``Subscription`` row. Left unreconciled, a migrated companion key:
    - is invisible to ``_sync_limited_companion_user``, so it stops being
      kept in sync with the main account's status/expiry on renewal;
    - shows up as a bogus second, independent subscription in the bot UI
      and cabinet;
    - permanently occupies the partial unique index on
      ``Subscription.remnawave_id``, so any future real re-sync of that
      panel identity would collide with it.

Pairing key: legacy ``vpn_keys.is_primary`` — NOT ``squad_uuid``. An earlier
version of this script matched by squad membership, on the assumption that
the legacy schema put companion keys on a dedicated squad; in practice a real
legacy database had exactly one ``squad_uuid`` value across every row
(main and companion alike), so that signal does not exist here. The legacy
schema does carry a real primary/companion flag though: ``is_primary`` (1 for
the main key, 0 for the limited-server companion). This script re-reads the
same legacy SQLite source ``migrate_shopbot`` was pointed at, groups
``vpn_keys`` by the legacy ``user_id`` (telegram_id, real or synthetic),
splits each group by ``is_primary``, and uses each side's
``remnawave_user_id`` to find the two ``Subscription`` rows migrate_shopbot
already created (matched on ``Subscription.remnawave_id``) — then folds the
companion's identity into the main row's ``limited_companion_*`` fields and
deletes the now-redundant standalone row.

Pairing is conservative on purpose: a user with more than one candidate on
either side of the split is left untouched and reported, rather than guessed
at. This is live production panel-identity data; a wrong link corrupts
which panel account the bot manages for that user going forward.

Usage:
    python -m scripts.reconcile_limited_companions --source /path/to/users.db              # dry run
    python -m scripts.reconcile_limited_companions --source /path/to/users.db --apply       # persist
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
from app.database.models import Subscription
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    dry_run: bool
    legacy_companion_rows: int = 0
    pairs_linked: int = 0
    skipped_already_linked: int = 0
    skipped_ambiguous_multiple_companion: int = 0
    skipped_ambiguous_multiple_main: int = 0
    skipped_companion_not_migrated: int = 0
    skipped_main_not_migrated: int = 0
    skipped_no_panel_link: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


def _load_legacy_vpn_keys(source_path: Path) -> list[dict]:
    uri = f'file:{source_path}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('SELECT user_id, is_primary, remnawave_user_id, key_id FROM vpn_keys').fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


async def _reconcile(db, legacy_vpn_keys: list[dict], *, apply: bool) -> ReconcileReport:
    report = ReconcileReport(dry_run=not apply)

    by_user: dict[int, list[dict]] = {}
    for row in legacy_vpn_keys:
        by_user.setdefault(row['user_id'], []).append(row)

    async def _sub_by_remnawave_id(remnawave_id: int) -> Subscription | None:
        return (
            await db.execute(select(Subscription).where(Subscription.remnawave_id == remnawave_id))
        ).scalar_one_or_none()

    for legacy_user_id, keys in by_user.items():
        companions = [k for k in keys if not k['is_primary']]
        if not companions:
            continue
        report.legacy_companion_rows += len(companions)

        primaries = [k for k in keys if k['is_primary']]

        if len(companions) > 1:
            report.skipped_ambiguous_multiple_companion += 1
            report.unresolved_lines.append(
                f'legacy user_id={legacy_user_id}: {len(companions)} companion (is_primary=0) '
                f'vpn_keys (key_id={[k["key_id"] for k in companions]}) — skipped, needs manual review'
            )
            continue

        companion_key = companions[0]

        if len(primaries) != 1:
            report.skipped_ambiguous_multiple_main += 1
            report.unresolved_lines.append(
                f'legacy user_id={legacy_user_id}: {len(primaries)} primary (is_primary=1) vpn_keys '
                f'for companion key_id={companion_key["key_id"]} — skipped, needs manual review'
            )
            continue

        main_key = primaries[0]

        companion_remnawave_id = companion_key['remnawave_user_id']
        main_remnawave_id = main_key['remnawave_user_id']
        if not companion_remnawave_id or not main_remnawave_id:
            report.skipped_no_panel_link += 1
            report.unresolved_lines.append(
                f'legacy user_id={legacy_user_id}: companion key_id={companion_key["key_id"]} or main '
                f'key_id={main_key["key_id"]} has no remnawave_user_id — skipped, needs manual review'
            )
            continue

        limited_sub = await _sub_by_remnawave_id(companion_remnawave_id)
        if limited_sub is None:
            # Companion key wasn't migrated as its own Subscription (e.g. skipped as
            # already-expired by migrate_shopbot) — nothing to fold in, not an error.
            report.skipped_companion_not_migrated += 1
            continue

        main_sub = await _sub_by_remnawave_id(main_remnawave_id)
        if main_sub is None:
            report.skipped_main_not_migrated += 1
            report.unresolved_lines.append(
                f'legacy user_id={legacy_user_id}: companion Subscription id={limited_sub.id} exists but '
                f'main key (remnawave_id={main_remnawave_id}) was not migrated — leftover companion left '
                f'untouched, needs manual review'
            )
            continue

        if main_sub.limited_companion_remnawave_id:
            report.skipped_already_linked += 1
            report.unresolved_lines.append(
                f'legacy user_id={legacy_user_id}: main Subscription id={main_sub.id} already has a '
                f'companion linked (remnawave_id={main_sub.limited_companion_remnawave_id}); leftover '
                f'companion Subscription id={limited_sub.id} left untouched — needs manual review'
            )
            continue

        main_sub.limited_companion_remnawave_id = limited_sub.remnawave_id
        main_sub.limited_companion_short_uuid = limited_sub.remnawave_short_uuid
        await db.delete(limited_sub)
        report.pairs_linked += 1

    await db.flush()
    return report


def _print_report(report: ReconcileReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  лимитных ключей в легаси (is_primary=0)      : {report.legacy_companion_rows}')
    print(f'  пар связано                                  : {report.pairs_linked}')
    print(f'  пропущено (уже связано)                      : {report.skipped_already_linked}')
    print(f'  пропущено (несколько лимитных на пользователя): {report.skipped_ambiguous_multiple_companion}')
    print(f'  пропущено (несколько основных на пользователя): {report.skipped_ambiguous_multiple_main}')
    print(f'  пропущено (лимитный ключ не мигрирован)      : {report.skipped_companion_not_migrated}')
    print(f'  пропущено (основной ключ не мигрирован)      : {report.skipped_main_not_migrated}')
    print(f'  пропущено (нет remnawave_user_id)            : {report.skipped_no_panel_link}')
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


async def _run(args: argparse.Namespace, source_path: Path) -> int:
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

    legacy_vpn_keys = _load_legacy_vpn_keys(source_path)
    print(f'  источник: {len(legacy_vpn_keys)} vpn_keys')

    async with AsyncSessionLocal() as db:
        report = await _reconcile(db, legacy_vpn_keys, apply=args.apply)
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
            'Fold migrate_shopbot-imported "limited server" companion Subscription rows into '
            'the limited_companion_* fields of their main sibling subscription'
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
