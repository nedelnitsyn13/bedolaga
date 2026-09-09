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

This script finds those pairs by squad membership and folds each companion
row into its sibling's ``limited_companion_*`` fields, then deletes the now
-redundant standalone row. Because ``write_companion_account`` (used by
``_sync_limited_companion_user`` going forward) never creates a second
``Subscription`` row, EVERY row whose ``connected_squads`` contains the
limited-companion squad UUID is, by construction, a migration leftover —
never a legitimately-created companion.

Pairing is conservative on purpose: a user with more than one candidate on
either side of the split is left untouched and reported, rather than guessed
at. This is live production panel-identity data; a wrong link corrupts
which panel account the bot manages for that user going forward.

Usage:
    python -m scripts.reconcile_limited_companions              # dry run
    python -m scripts.reconcile_limited_companions --apply       # persist
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
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    dry_run: bool
    users_with_limited_rows: int = 0
    pairs_linked: int = 0
    skipped_already_linked: int = 0
    skipped_ambiguous_multiple_limited: int = 0
    skipped_ambiguous_multiple_main: int = 0
    skipped_orphan_no_main: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


def _has_squad(subscription: Subscription, squad_uuid: str) -> bool:
    squads = subscription.connected_squads or []
    return squad_uuid in squads


async def _reconcile(db, squad_uuid: str, *, apply: bool) -> ReconcileReport:
    report = ReconcileReport(dry_run=not apply)

    rows = (await db.execute(select(Subscription))).scalars().all()

    by_user: dict[int, list[Subscription]] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)

    for user_id, subs in by_user.items():
        limited = [s for s in subs if _has_squad(s, squad_uuid)]
        if not limited:
            continue
        report.users_with_limited_rows += 1

        main = [s for s in subs if s not in limited]

        if len(limited) > 1:
            report.skipped_ambiguous_multiple_limited += 1
            report.unresolved_lines.append(
                f'user_id={user_id}: {len(limited)} companion-squad subscriptions '
                f'(ids={[s.id for s in limited]}) — skipped, needs manual review'
            )
            continue

        limited_sub = limited[0]

        if not main:
            report.skipped_orphan_no_main += 1
            report.unresolved_lines.append(
                f'user_id={user_id}: companion subscription id={limited_sub.id} has no sibling '
                f'main subscription — skipped, needs manual review'
            )
            continue

        if len(main) > 1:
            report.skipped_ambiguous_multiple_main += 1
            report.unresolved_lines.append(
                f'user_id={user_id}: {len(main)} candidate main subscriptions '
                f'(ids={[s.id for s in main]}) for companion id={limited_sub.id} — skipped, needs manual review'
            )
            continue

        main_sub = main[0]

        if main_sub.limited_companion_remnawave_id:
            report.skipped_already_linked += 1
            report.unresolved_lines.append(
                f'user_id={user_id}: main subscription id={main_sub.id} already has a companion '
                f'linked (remnawave_id={main_sub.limited_companion_remnawave_id}); leftover companion '
                f'subscription id={limited_sub.id} left untouched — needs manual review'
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
    print(f'  пользователей с лимитными строками : {report.users_with_limited_rows}')
    print(f'  пар связано                        : {report.pairs_linked}')
    print(f'  пропущено (уже связано)            : {report.skipped_already_linked}')
    print(f'  пропущено (несколько лимитных)     : {report.skipped_ambiguous_multiple_limited}')
    print(f'  пропущено (несколько основных)     : {report.skipped_ambiguous_multiple_main}')
    print(f'  пропущено (нет основной подписки)  : {report.skipped_orphan_no_main}')
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


async def _run(args: argparse.Namespace) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    squad_uuid = settings.LIMITED_COMPANION_SQUAD_UUID
    logger.info(
        'reconcile_limited_companions: конфигурация загружена',
        limited_companion_enabled=settings.LIMITED_COMPANION_ENABLED,
        limited_companion_squad_uuid=squad_uuid,
    )
    if not squad_uuid:
        print('  !! LIMITED_COMPANION_SQUAD_UUID не задан в конфигурации -- нечего искать, выход.')
        return 2

    async with AsyncSessionLocal() as db:
        report = await _reconcile(db, squad_uuid, apply=args.apply)
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
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
