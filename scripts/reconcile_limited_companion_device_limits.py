#!/usr/bin/env python
"""Re-sync limited-companion accounts whose panel device limit drifted.

The companion account (`Subscription.limited_companion_remnawave_id`) is
created once with whatever device limit the subscription had at that moment,
and from then on only `_sync_limited_companion_user` keeps it in step. Until
the admin-edit paths learned to mirror `hwid_device_limit` onto the companion,
every device-limit change made from the admin panel (bot or cabinet) landed on
the main account alone — leaving companions pinned to an old, lower limit.

The panel enforces that stale limit: once the companion holds that many
devices, it rejects every further HWID registration there while the main
account still accepts it. What the user sees is a device that shows up on the
main server and silently never appears on the limited one — no error anywhere,
because the rejection happens inside the panel on the client's own
subscription fetch.

This script compares each subscription's expected device limit
(`resolve_hwid_device_limit_for_payload`) against the companion's live
`hwidDeviceLimit` and re-syncs the ones that drifted, through the same
`resync_limited_companion` the bot uses. Dry run by default.

Usage:
    python -m scripts.reconcile_limited_companion_device_limits            # dry run
    python -m scripts.reconcile_limited_companion_device_limits --apply     # fix
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.system_settings_service import bot_configuration_service
from app.utils.subscription_utils import resolve_hwid_device_limit_for_payload


# LIMITED — трафик исчерпан, но подписка живая: устройства к ней продолжают
# цепляться, и разъехавшийся лимит мешает ровно так же.
LIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)


async def _subscriptions_with_companion(db) -> list[Subscription]:
    result = await db.execute(
        select(Subscription).where(
            Subscription.limited_companion_remnawave_id.isnot(None),
            Subscription.status.in_(LIVE_STATUSES),
        )
    )
    return list(result.scalars().all())


async def _panel_device_limits(api) -> dict[int, int | None]:
    """Одним проходом по панели: {panel_user_id: hwidDeviceLimit}.

    Дешевле, чем дёргать get_user_by_id на каждую подписку — установок с
    сотнями компаньонов это разница между одним запросом и сотнями.
    """
    limits: dict[int, int | None] = {}
    cursor: str | None = None
    while True:
        page = await api.get_all_users_page_stream(cursor=cursor, size=500, enrich_happ_links=False)
        for user in page['users']:
            limits[user.id] = user.hwid_device_limit
        if not page['hasMore'] or not page['nextCursor']:
            break
        cursor = page['nextCursor']
    return limits


async def _run(apply: bool) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    remnawave_service = RemnaWaveService()
    subscription_service = SubscriptionService()

    async with AsyncSessionLocal() as db:
        subscriptions = await _subscriptions_with_companion(db)
        print(f'  активных подписок с компаньоном: {len(subscriptions)}')

        async with remnawave_service.get_api_client() as api:
            panel_limits = await _panel_device_limits(api)
        print(f'  пользователей в панели: {len(panel_limits)}')

        drifted: list[tuple[Subscription, int | None, int | None]] = []
        missing: list[Subscription] = []
        for subscription in subscriptions:
            companion_id = subscription.limited_companion_remnawave_id
            if companion_id not in panel_limits:
                missing.append(subscription)
                continue
            expected = resolve_hwid_device_limit_for_payload(subscription)
            actual = panel_limits[companion_id]
            if expected is not None and actual != expected:
                drifted.append((subscription, expected, actual))

        print()
        print('=' * 78)
        print(f'  Компаньонов с разъехавшимся лимитом устройств: {len(drifted)}')
        print('=' * 78)
        for subscription, expected, actual in drifted:
            print(
                f'  sub={subscription.id}  user={subscription.user_id}  '
                f'companion={subscription.limited_companion_remnawave_id}  '
                f'в панели={actual}  должно быть={expected}'
            )
        if missing:
            print('-' * 78)
            print(f'  Компаньон записан в базе, но не найден в панели: {len(missing)}')
            for subscription in missing:
                print(f'  sub={subscription.id}  companion={subscription.limited_companion_remnawave_id}')
            print('  (это добыча scripts/find_orphaned_limited_companions.py, здесь не трогаем)')
        print('=' * 78)

        if not drifted:
            print('  Чинить нечего.')
            return 0

        if not apply:
            print('  Это dry-run — ничего не изменено. Повторите с --apply, чтобы пересинхронизировать.')
            return 0

        synced = 0
        failed = 0
        for subscription, _expected, _actual in drifted:
            if await subscription_service.resync_limited_companion(db, subscription):
                synced += 1
            else:
                failed += 1
                print(f'  !! не удалось пересинхронизировать sub={subscription.id}')

        print()
        print(f'  Пересинхронизировано: {synced}, не удалось: {failed}')
        return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--apply', action='store_true', help='пересинхронизировать найденные компаньоны (по умолчанию — dry run)'
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == '__main__':
    raise SystemExit(main())
