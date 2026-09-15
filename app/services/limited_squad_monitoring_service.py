"""Периодическая проверка LIMITED squad (новая архитектура лимитного трафика).

Без этого цикла весь app/services/limited_squad_service.py — мёртвый код:
usage никогда не обновляется, squad никогда не снимается/не возвращается.
Параллельно существующему мониторингу лимитного компаньона (тот держится на
панели через trafficLimitStrategy=MONTH и вообще не нуждается в отдельном
цикле бота) — эта механика требует собственного enforcement, поскольку у
неё нет отдельного панельного пользователя, на который можно было бы
повесить нативный trafficLimitBytes.

Отдельный сервис/цикл, а не довесок к traffic_monitoring_service или
monitoring_service — чтобы не увеличивать нагрузку (per-subscription запросы
к Remnawave API) на уже существующие, гораздо более частые циклы.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, Tariff
from app.services.limited_squad_service import is_limited_traffic_enabled, process_limited_traffic
from app.services.remnawave_service import RemnaWaveService


logger = structlog.get_logger(__name__)

_LIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)


async def _load_subscriptions_with_limited_traffic(db: AsyncSession) -> list[Subscription]:
    """Активные подписки на тарифах с включённым LIMITED squad.

    Фильтр по `Tariff.limited_traffic_enabled` в самом запросе — не тянуть
    все активные подписки только затем, чтобы тут же отбросить большинство.
    """
    result = await db.execute(
        select(Subscription)
        .join(Tariff, Tariff.id == Subscription.tariff_id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            Subscription.status.in_(_LIVE_STATUSES),
            Tariff.limited_traffic_enabled.is_(True),
        )
    )
    return list(result.unique().scalars().all())


class LimitedSquadMonitoringService:
    """Фоновый цикл: раз в LIMITED_SQUAD_CHECK_INTERVAL_MINUTES обновляет usage

    и переключает LIMITED squad для всех подписок на тарифах с включённой
    механикой. Best-effort по подписке — ошибка на одной не должна останавливать
    обработку остальных.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start_monitoring(self) -> None:
        interval_seconds = max(60, settings.LIMITED_SQUAD_CHECK_INTERVAL_MINUTES * 60)
        while True:
            try:
                await self._run_cycle()
            except Exception as error:
                logger.error('Ошибка цикла мониторинга LIMITED squad', error=error, exc_info=True)
            await asyncio.sleep(interval_seconds)

    async def _run_cycle(self) -> None:
        async with AsyncSessionLocal() as db:
            subscriptions = await _load_subscriptions_with_limited_traffic(db)
            if not subscriptions:
                return

            service = RemnaWaveService()
            if not service.is_configured:
                return

            processed = 0
            errors = 0
            async with service.get_api_client() as api:
                for subscription in subscriptions:
                    tariff = subscription.tariff
                    user = subscription.user
                    if not is_limited_traffic_enabled(tariff) or not user:
                        continue
                    try:
                        await process_limited_traffic(api, db, subscription, tariff, user)
                        processed += 1
                    except Exception as error:
                        errors += 1
                        logger.warning(
                            '⚠️ Ошибка обработки LIMITED squad для подписки',
                            subscription_id=subscription.id,
                            error=error,
                        )

            if processed or errors:
                logger.info(
                    '🎯 Цикл мониторинга LIMITED squad завершён',
                    processed=processed,
                    errors=errors,
                    total=len(subscriptions),
                )


limited_squad_monitoring_service = LimitedSquadMonitoringService()
