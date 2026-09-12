#!/usr/bin/env python
"""Find (and optionally delete) orphaned limited-companion panel accounts.

Incident: the v4.10.0 full-sync backfill briefly created a bogus extra
`Subscription` row for every user's limited-companion panel account (see
the fix in `RemnaWaveService._sync_users_from_panel_multi`). While those
bogus rows existed in the DB, routine per-subscription syncs treated each
one like any other subscription and ran `_sync_limited_companion_user`
against it — which only ever checks `Subscription.limited_companion_remnawave_id`
to decide whether a companion already exists (see that method's docstring).
A freshly-created bogus row never has that field set, so the sync created
a brand-new companion panel account for it every time it got touched
during that window.

The bogus `Subscription` rows have since been deleted directly in the DB,
but the panel-side companion accounts created *for* them were never
cleaned up — they're real RemnaWave users, just untracked by bedolaga now,
so they show up in the panel as a second/third `_lim`-suffixed account for
users who already have a legitimate one.

This script finds any panel user whose username ends with the companion
suffix (`_lim`, see `Settings.build_remnawave_subscription_username`) but
whose numeric id is not `limited_companion_remnawave_id` on any current
`Subscription`. Dry run by default; `--apply` deletes them from the panel.

Usage:
    python -m scripts.find_orphaned_limited_companions              # dry run
    python -m scripts.find_orphaned_limited_companions --apply       # delete
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service


COMPANION_USERNAME_SUFFIX = '_lim'


async def _known_companion_ids(db) -> set[int]:
    result = await db.execute(
        select(Subscription.limited_companion_remnawave_id).where(
            Subscription.limited_companion_remnawave_id.isnot(None)
        )
    )
    return {row[0] for row in result.all() if row[0] is not None}


async def _fetch_all_panel_users(api) -> list:
    users = []
    cursor: str | None = None
    while True:
        page = await api.get_all_users_page_stream(cursor=cursor, size=500, enrich_happ_links=False)
        users.extend(page['users'])
        if not page['hasMore'] or not page['nextCursor']:
            break
        cursor = page['nextCursor']
    return users


async def _run(apply: bool) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    remnawave_service = RemnaWaveService()
    async with AsyncSessionLocal() as db:
        known_ids = await _known_companion_ids(db)
        print(f'  подписок со связанным компаньоном в базе: {len(known_ids)}')

        async with remnawave_service.get_api_client() as api:
            panel_users = await _fetch_all_panel_users(api)
        print(f'  всего пользователей в панели: {len(panel_users)}')

        orphans = [
            u for u in panel_users if (u.username or '').endswith(COMPANION_USERNAME_SUFFIX) and u.id not in known_ids
        ]

        print()
        print('=' * 70)
        print(f'  Найдено потенциальных орфанов-компаньонов: {len(orphans)}')
        print('=' * 70)
        for u in orphans:
            print(f'  id={u.id}  username={u.username}  status={u.status.value}  telegramId={u.telegram_id}')
        print('=' * 70)

        if not orphans:
            print('  Ничего чистить не нужно.')
            return 0

        if not apply:
            print('  Это dry-run — ничего не удалено. Повторите с --apply, чтобы удалить из панели.')
            return 0

        deleted = 0
        errors = 0
        async with remnawave_service.get_api_client() as api:
            for u in orphans:
                try:
                    await api.delete_user(u.id)
                    deleted += 1
                except Exception as error:
                    errors += 1
                    print(f'  !! не удалось удалить id={u.id}: {error}')

        print()
        print(f'  Удалено: {deleted}, ошибок: {errors}')
        return 0 if errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--apply', action='store_true', help='удалить найденные орфаны из панели (по умолчанию — dry run)'
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == '__main__':
    raise SystemExit(main())
