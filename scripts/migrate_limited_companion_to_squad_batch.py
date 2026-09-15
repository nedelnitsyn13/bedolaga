#!/usr/bin/env python
"""Batch-обвес вокруг ``migrate_limited_companion_to_squad``: прогоняет
ВСЕ живые подписки с legacy companion, чей тариф уже переведён на новую
LIMITED squad архитектуру (``Tariff.limited_traffic_enabled=True``).

Не дублирует логику самой миграции одной подписки — импортирует и
переиспользует ``_migrate_one``/``_print_report``/``_write_audit`` из
``migrate_limited_companion_to_squad``. Этот скрипт только отбирает
подписки и по каждой best-effort: ошибка на одной не должна прерывать
обработку остальных (тот же принцип, что и в периодическом мониторинге,
см. ``limited_squad_monitoring_service._run_cycle``).

Тарифы этот скрипт НЕ настраивает — админ должен заранее включить
``limited_traffic_enabled`` + задать ``limited_squad_uuids`` и
``limited_base_traffic_gb`` на каждом тарифе, который нужно мигрировать
(через админку/кабинет). Подписки на тарифах без этого флага скрипт
просто не увидит.

Usage:
    python -m scripts.migrate_limited_companion_to_squad_batch            # dry run по всем
    python -m scripts.migrate_limited_companion_to_squad_batch --apply    # применить ко всем
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, Tariff
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service
from scripts.migrate_limited_companion_to_squad import (
    MigrationReport,
    _migrate_one,
    _print_report,
    _write_audit,
)


_LIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)


async def _load_candidate_subscription_ids(db) -> list[int]:
    result = await db.execute(
        select(Subscription.id)
        .join(Tariff, Tariff.id == Subscription.tariff_id)
        .where(
            Subscription.status.in_(_LIVE_STATUSES),
            Subscription.limited_companion_remnawave_id.isnot(None),
            Tariff.limited_traffic_enabled.is_(True),
        )
        .order_by(Subscription.id)
    )
    return [row[0] for row in result.all()]


def _print_summary(reports: list[MigrationReport], *, apply: bool) -> None:
    ok = [r for r in reports if r.ok]
    failed = [r for r in reports if not r.ok]
    reasons = Counter(r.reason for r in failed if r.reason)
    apply_errors = [r for r in failed if r.companion_disable_error and not r.reason]

    print()
    print('#' * 70)
    print(f'  ИТОГО {"(APPLY)" if apply else "(DRY RUN)"}: {len(reports)} подписок обработано')
    print(f'  успешно: {len(ok)}')
    print(f'  пропущено/ошибка: {len(failed)}')
    if reasons:
        print('  причины пропуска:')
        for reason, count in reasons.most_common():
            print(f'    {count:>4} × {reason}')
    if apply_errors:
        print(f'  !! ошибок отключения companion при apply: {len(apply_errors)}')
        for r in apply_errors:
            print(f'    subscription #{r.subscription_id}: {r.companion_disable_error}')
    print('#' * 70)


async def _run(*, apply: bool) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    remnawave_service = RemnaWaveService()
    reports: list[MigrationReport] = []

    async with AsyncSessionLocal() as db:
        subscription_ids = await _load_candidate_subscription_ids(db)

    print(f'Найдено подписок к обработке: {len(subscription_ids)}')

    async with remnawave_service.get_api_client() as api:
        for subscription_id in subscription_ids:
            async with AsyncSessionLocal() as db:
                report = await _migrate_one(db, api, subscription_id, apply=apply)
                if apply and report.ok:
                    await db.commit()
                else:
                    await db.rollback()

            _print_report(report)
            audit_path = _write_audit(report, committed=apply and report.ok)
            if audit_path:
                print(f'  полный отчёт: {audit_path}')
            reports.append(report)

    _print_summary(reports, apply=apply)
    return 0 if all(r.ok for r in reports) else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Batch cut over ALL live subscriptions with legacy companion, on tariffs already '
            'switched to the new LIMITED squad architecture. Best-effort per subscription — one '
            'failure does not stop the rest.'
        )
    )
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()

    return asyncio.run(_run(apply=args.apply))


if __name__ == '__main__':
    raise SystemExit(main())
