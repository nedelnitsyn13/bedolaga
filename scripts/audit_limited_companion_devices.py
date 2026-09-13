#!/usr/bin/env python
"""Сверить HWID-устройства основного аккаунта и лимитного компаньона.

Бот устройства не регистрирует — в API панели вообще нет ручки «добавить
HWID» (см. `app/external/remnawave_api.py`: только чтение, reset и remove).
Регистрацию делает сама панель, когда приложение забирает ссылку подписки
с заголовком hwid. Ссылка у пользователя одна: её отдаёт subscription-merger,
который под капотом ходит и в основной аккаунт, и в компаньона — поэтому
одно добавление устройства в приложении должно появиться сразу на обоих.

Если устройство осело только на одном аккаунте, значит панель отказала на
втором. Единственная причина отказа — на том аккаунте уже выбран
`hwidDeviceLimit`. Ошибки при этом не видит никто: отказ происходит внутри
панели на запросе самого клиента, бот в этот момент вообще не участвует.

Скрипт строит по одному проходу карты устройств и лимитов панели, сверяет
их с подписками и печатает расхождения с вероятной причиной. Только чтение:
дописать устройство на отстающий аккаунт всё равно нечем — чинится либо
лимитом (`scripts/reconcile_limited_companion_device_limits.py`), либо
переподключением в приложении.

Usage:
    python -m scripts.audit_limited_companion_devices
    python -m scripts.audit_limited_companion_devices --limit 40
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, User
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service


LIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)


@dataclass
class Divergence:
    subscription_id: int
    user_id: int
    main_id: int
    companion_id: int
    main_hwids: set[str]
    companion_hwids: set[str]
    main_limit: int | None
    companion_limit: int | None

    @property
    def only_on_main(self) -> set[str]:
        return self.main_hwids - self.companion_hwids

    @property
    def only_on_companion(self) -> set[str]:
        return self.companion_hwids - self.main_hwids

    def reason(self) -> str:
        """Почему устройство осело только на одном аккаунте.

        Панель отказывает в регистрации ровно когда аккаунт упёрся в свой
        `hwidDeviceLimit`, поэтому «отстающая сторона заполнена под завязку» —
        это не догадка, а прямое объяснение. Всё остальное честно помечаем
        как необъяснённое, чтобы не выдавать предположение за диагноз.
        """
        if self.only_on_main and self.companion_limit is not None:
            if len(self.companion_hwids) >= self.companion_limit:
                return 'компаньон забит под лимит — панель отклоняет новые устройства'
        if self.only_on_companion and self.main_limit is not None:
            if len(self.main_hwids) >= self.main_limit:
                return 'основной забит под лимит — панель отклоняет новые устройства'
        if self.main_limit != self.companion_limit:
            return f'лимиты разъехались: основной={self.main_limit}, компаньон={self.companion_limit}'
        return 'причина не установлена (устройство могли удалить на одной стороне вручную)'


async def _load_subscriptions(db) -> list[tuple[Subscription, User]]:
    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.limited_companion_remnawave_id.isnot(None),
            Subscription.status.in_(LIVE_STATUSES),
        )
    )
    return list(result.all())


async def _panel_maps(api) -> tuple[dict[int, set[str]], dict[int, int | None]]:
    """Одним проходом: {panel_user_id: {hwid}} и {panel_user_id: hwidDeviceLimit}.

    Дёргать `get_user_devices` на каждую подписку — это два запроса на строку,
    то есть сотни запросов к панели вместо двух постраничных обходов.
    """
    devices: dict[int, set[str]] = {}
    payload = await api.get_all_hwid_devices()
    for device in payload.get('devices', []):
        panel_user_id = device.get('userId', device.get('user_id'))
        hwid = device.get('hwid')
        if panel_user_id is None or not hwid:
            continue
        devices.setdefault(int(panel_user_id), set()).add(hwid)

    limits: dict[int, int | None] = {}
    cursor: str | None = None
    while True:
        page = await api.get_all_users_page_stream(cursor=cursor, size=500, enrich_happ_links=False)
        for user in page['users']:
            limits[user.id] = user.hwid_device_limit
        if not page['hasMore'] or not page['nextCursor']:
            break
        cursor = page['nextCursor']

    return devices, limits


async def _run(max_rows: int) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    multi_tariff = settings.is_multi_tariff_enabled()

    async with AsyncSessionLocal() as db:
        rows = await _load_subscriptions(db)
        print(f'  активных подписок с компаньоном: {len(rows)}')

        async with RemnaWaveService().get_api_client() as api:
            devices, limits = await _panel_maps(api)
        print(f'  устройств в панели: {sum(len(v) for v in devices.values())}')

        in_sync = 0
        no_devices = 0
        unlinked: list[int] = []
        divergences: list[Divergence] = []

        for subscription, user in rows:
            main_id = subscription.remnawave_id if multi_tariff else user.remnawave_id
            companion_id = subscription.limited_companion_remnawave_id
            if not main_id:
                unlinked.append(subscription.id)
                continue

            main_hwids = devices.get(int(main_id), set())
            companion_hwids = devices.get(int(companion_id), set())

            if not main_hwids and not companion_hwids:
                no_devices += 1
                continue
            if main_hwids == companion_hwids:
                in_sync += 1
                continue

            divergences.append(
                Divergence(
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                    main_id=int(main_id),
                    companion_id=int(companion_id),
                    main_hwids=main_hwids,
                    companion_hwids=companion_hwids,
                    main_limit=limits.get(int(main_id)),
                    companion_limit=limits.get(int(companion_id)),
                )
            )

        print()
        print('=' * 78)
        print('  СВОДКА')
        print('=' * 78)
        print(f'  списки устройств совпадают:      {in_sync}')
        print(f'  устройств нет ни там, ни там:    {no_devices}')
        print(f'  РАСХОЖДЕНИЙ:                     {len(divergences)}')
        if unlinked:
            print(f'  без id основного аккаунта:       {len(unlinked)} (sub {unlinked[:10]})')
        print('=' * 78)

        if not divergences:
            print('  Расхождений нет: каждое устройство зарегистрировано на обоих аккаунтах.')
            return 0

        print()
        for item in sorted(divergences, key=lambda d: -len(d.only_on_main))[:max_rows]:
            print(
                f'  sub={item.subscription_id}  user={item.user_id}  '
                f'основной={item.main_id} ({len(item.main_hwids)}/{item.main_limit})  '
                f'компаньон={item.companion_id} ({len(item.companion_hwids)}/{item.companion_limit})'
            )
            if item.only_on_main:
                print(f'      только на основном:  {len(item.only_on_main)} → {sorted(item.only_on_main)[:3]}')
            if item.only_on_companion:
                print(
                    f'      только на лимитном:  {len(item.only_on_companion)} → {sorted(item.only_on_companion)[:3]}'
                )
            print(f'      причина: {item.reason()}')

        if len(divergences) > max_rows:
            print(f'  ... и ещё {len(divergences) - max_rows} (покажет --limit)')

        print()
        print('  Дописать устройство на отстающий аккаунт нечем — в API панели нет')
        print('  такой ручки. Если причина в лимите: прогнать')
        print('  scripts/reconcile_limited_companion_device_limits.py --apply, после чего')
        print('  пользователю переподключиться в приложении (или сбросить ему устройства).')
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=25, help='сколько расхождений печатать (по умолчанию 25)')
    args = parser.parse_args()
    return asyncio.run(_run(args.limit))


if __name__ == '__main__':
    raise SystemExit(main())
