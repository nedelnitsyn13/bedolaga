"""LIMITED squad на основном Remnawave user — новая архитектура лимитного трафика.

Параллельно ``app/services/subscription_service.py::_sync_limited_companion_user``
(см. ``LIMITED_COMPANION_ENABLED``) — та схема держит отдельного панельного
пользователя на LIMITED_COMPANION_SQUAD_UUID; этот модуль вместо этого считает
и enforce'ит LIMITED-пул прямо на ``activeInternalSquads`` ОСНОВНОГО юзера.
Ничего из companion-кода этот модуль не трогает и не заменяет — обе схемы
работают параллельно, пока не согласован отдельный этап очистки.

Ключевая техническая находка: у Remnawave 3.0.0+ есть
``POST /api/bandwidth-stats/nodes/usage`` — потребление конкретного
пользователя по конкретным нодам за произвольный период (уже используется в
``app/cabinet/routes/admin_traffic.py``). Это снимает главный риск задачи: не
нужно ставить глобальный ``trafficLimitBytes`` (задел бы MAIN-трафик) и не
нужно городить собственный baseline/cumulative-счётчик — можно просто
спросить у панели «сколько этот юзер прогнал через эти ноды с даты X по
сейчас».

Design-решение, требующее подтверждения: usage считается НАКОПИТЕЛЬНО с
``subscription.start_date`` без периодического сброса — так же, как ведут
себя докупки LIMITED-пула в примерах ТЗ (лимит растёт докупками, а не
обнуляется по календарю). Если нужен periodic reset (по аналогии с
MONTH-сбросом компаньона) — это отдельное решение, здесь сознательно не
реализовано.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiohttp
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import LimitedCompanionTrafficPurchase, Subscription, Tariff


logger = structlog.get_logger(__name__)

GB_IN_BYTES = 1024**3


def is_limited_traffic_enabled(tariff: Tariff | None) -> bool:
    """Включена ли LIMITED-механика (новая архитектура) на тарифе."""
    return bool(tariff and getattr(tariff, 'limited_traffic_enabled', False))


def get_limited_squad_uuids(tariff: Tariff | None) -> list[str]:
    """UUID squad'ов LIMITED-пула тарифа. Пусто, если механика выключена."""
    if not is_limited_traffic_enabled(tariff):
        return []
    return get_raw_limited_squad_uuids(tariff)


def get_raw_limited_squad_uuids(tariff: Tariff | None) -> list[str]:
    """То же самое, но без гейта на ``limited_traffic_enabled``.

    Для мест, которым нужно «эти сквады — не MAIN», а не «механика включена»:
    см. ``get_limited_squad_uuids_for_subscription`` и
    ``deactivate_orphaned_limited_squad`` — та тоже читает список напрямую
    именно потому, что вызывается ПОСЛЕ выключения флага.
    """
    raw = getattr(tariff, 'limited_squad_uuids', None) or []
    return [str(squad_uuid) for squad_uuid in raw if squad_uuid]


async def get_limited_squad_uuids_for_subscription(db: AsyncSession, subscription: Subscription) -> list[str]:
    """То же самое, но по подписке — сама подгружает тариф по ``tariff_id``.

    ``subscription.tariff`` — ленивая связь, трогать её напрямую в async-коде
    небезопасно (может уйти в синхронный запрос вне сессии), если он заранее
    не прогружен ``selectinload``. Отдельный SELECT по id — цена корректности
    при переносе squad'ов из панели (``project_onto_subscription``), где
    заранее прогруженного тарифа обычно нет. Если он ЕСТЬ (например, в
    полном multi-tariff проходе, где ``Subscription.tariff`` уже подгружен
    ``selectinload``) — вызывающему дешевле позвать ``get_raw_limited_squad_uuids``
    напрямую и не платить лишним запросом на каждую подписку.

    НЕ гейтится на ``limited_traffic_enabled`` — окно между выключением
    флага на тарифе и ``deactivate_orphaned_limited_squad`` (которая снимает
    сквад с панели) панель ещё реально держит его активным: любой pull/
    webhook в это время записал бы его в ``connected_squads`` как основной,
    а deactivate тогда PATCH'ит панель уже заражённым списком и не снимает
    сквад вовсе, хотя выставляет ``limited_squad_active=False`` — доступ
    остаётся включённым навсегда и никто больше это не перепроверит.
    """
    tariff_id = getattr(subscription, 'tariff_id', None)
    if not tariff_id:
        return []
    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id, with_promo_groups=False)
    return get_raw_limited_squad_uuids(tariff)


def get_limited_base_traffic_gb(tariff: Tariff | None) -> int:
    """Базовый лимит LIMITED-пула в ГБ, без докупок. 0 = механика выключена."""
    if not is_limited_traffic_enabled(tariff):
        return 0
    return int(getattr(tariff, 'limited_base_traffic_gb', 0) or 0)


def resolve_main_panel_user_id(subscription: Subscription, user) -> int | None:
    """Тот же id, что уходит в panel_sync для MAIN-аккаунта подписки."""
    if settings.is_multi_tariff_enabled():
        return subscription.remnawave_id
    return user.remnawave_id if user else None


async def get_active_limited_traffic_purchases_gb(db: AsyncSession, subscription: Subscription) -> int:
    """Housekeep + сумма активных докупок LIMITED-пула.

    Переиспользует ``limited_companion_traffic_purchases`` как хранилище —
    таблица не привязана к схеме компаньон-аккаунта (только
    ``subscription_id``/``traffic_gb``/``expires_at``). В отличие от
    ``crud.subscription.housekeep_limited_companion_traffic`` НЕ пишет в
    ``subscription.limited_companion_purchased_traffic_gb`` — то поле
    принадлежит старой архитектуре; здесь сумма просто возвращается
    вызывающему.
    """
    now = datetime.now(UTC)

    await db.execute(
        delete(LimitedCompanionTrafficPurchase)
        .where(
            LimitedCompanionTrafficPurchase.subscription_id == subscription.id,
            LimitedCompanionTrafficPurchase.expires_at <= now,
        )
        .execution_options(synchronize_session='fetch')
    )
    result = await db.execute(
        select(LimitedCompanionTrafficPurchase.traffic_gb).where(
            LimitedCompanionTrafficPurchase.subscription_id == subscription.id,
            LimitedCompanionTrafficPurchase.expires_at > now,
        )
    )
    return sum(result.scalars().all())


async def add_limited_traffic_purchase(db: AsyncSession, subscription: Subscription, gb: int) -> None:
    """Регистрирует докупку LIMITED-пула с истечением через 30 дней.

    Аналог ``crud.subscription.add_limited_companion_traffic`` — тот же
    паттерн (индивидуальный ``expires_at``, не навсегда) на той же таблице.
    """
    if gb <= 0:
        return

    now = datetime.now(UTC)
    db.add(
        LimitedCompanionTrafficPurchase(
            subscription_id=subscription.id,
            traffic_gb=gb,
            expires_at=now + timedelta(days=30),
        )
    )
    await db.commit()


async def remove_limited_traffic_purchase(db: AsyncSession, subscription: Subscription, gb: int) -> int:
    """Списывает докупленный LIMITED-пул, не трогая базу тарифа.

    Симметрична ``add_limited_traffic_purchase`` — гасит активные докупки
    (старейшие первыми), а не базовый лимит тарифа: у него нет отдельной
    записи-докупки, которую можно было бы уменьшить. Возвращает реально
    списанное количество ГБ — может быть меньше запрошенного, если докупок
    меньше, чем просят снять.
    """
    if gb <= 0:
        return 0

    now = datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(LimitedCompanionTrafficPurchase)
                .where(
                    LimitedCompanionTrafficPurchase.subscription_id == subscription.id,
                    LimitedCompanionTrafficPurchase.expires_at > now,
                )
                .order_by(LimitedCompanionTrafficPurchase.expires_at.asc())
            )
        )
        .scalars()
        .all()
    )

    remaining = gb
    removed = 0
    for purchase in rows:
        if remaining <= 0:
            break
        if purchase.traffic_gb <= remaining:
            remaining -= purchase.traffic_gb
            removed += purchase.traffic_gb
            await db.delete(purchase)
        else:
            purchase.traffic_gb -= remaining
            removed += remaining
            remaining = 0

    await db.commit()
    return removed


async def get_effective_limited_traffic_limit_gb(
    db: AsyncSession, subscription: Subscription, tariff: Tariff | None
) -> int:
    """База тарифа + активные докупки. 0 = безлимит (либо механика выключена)."""
    base_gb = get_limited_base_traffic_gb(tariff)
    if base_gb <= 0:
        return 0
    purchased_gb = await get_active_limited_traffic_purchases_gb(db, subscription)
    return base_gb + purchased_gb


async def resolve_limited_node_uuids(api, squad_uuids: list[str]) -> list[str]:
    """Ноды всех LIMITED squad'ов тарифа, одним списком без дублей."""
    node_uuids: list[str] = []
    seen: set[str] = set()
    for squad_uuid in squad_uuids:
        try:
            nodes = await api.get_internal_squad_accessible_nodes(squad_uuid)
        except Exception as error:
            logger.warning(
                '⚠️ Не удалось получить ноды LIMITED squad',
                squad_uuid=squad_uuid,
                error=error,
            )
            continue
        for node in nodes:
            if node.uuid not in seen:
                seen.add(node.uuid)
                node_uuids.append(node.uuid)
    return node_uuids


async def fetch_limited_used_bytes(
    api, panel_user_id: int, node_uuids: list[str], start_date: str, end_date: str
) -> int | None:
    """Потребление конкретного юзера по конкретным LIMITED-нодам за период.

    ``start_date``/``end_date`` строго ``YYYY-MM-DD`` — так у панели, см.
    докстринг ``RemnaWaveAPI.get_bandwidth_stats_nodes_usage``.

    Возвращает ``None``, если панель не ответила — это НЕ то же самое, что
    «трафика не было»: вызывающий не должен затирать прошлое сохранённое
    значение нулём и тем самым случайно реактивировать squad, отключённый за
    превышение лимита, из-за временного сбоя панели (fail open).
    """
    if not node_uuids:
        return 0
    try:
        usage = await api.get_bandwidth_stats_nodes_usage(node_uuids, start_date, end_date)
    except Exception as error:
        logger.warning(
            '⚠️ Не удалось получить usage LIMITED-нод',
            panel_user_id=panel_user_id,
            error=error,
        )
        return None

    total_bytes = 0
    for node_entry in usage.get('nodes') or []:
        for user_entry in node_entry.get('users') or []:
            if user_entry.get('id') == panel_user_id:
                total_bytes += int(user_entry.get('totalBytes') or 0)
    return total_bytes


async def refresh_limited_traffic_usage(
    api, db: AsyncSession, subscription: Subscription, tariff: Tariff | None, user
) -> float | None:
    """Пересчитывает и сохраняет ``subscription.limited_traffic_used_gb``.

    Возвращает новое значение в ГБ, либо ``None``, если механика выключена на
    тарифе, у подписки нет панельного аккаунта/нод LIMITED-пула, или панель не
    ответила на запрос usage (тогда прошлое сохранённое значение остаётся как
    было — см. ``fetch_limited_used_bytes``).
    """
    squad_uuids = get_limited_squad_uuids(tariff)
    if not squad_uuids:
        return None

    panel_user_id = resolve_main_panel_user_id(subscription, user)
    if not panel_user_id:
        return None

    node_uuids = await resolve_limited_node_uuids(api, squad_uuids)
    if not node_uuids:
        return None

    start_date = subscription.start_date or subscription.created_at
    now = datetime.now(UTC)
    used_bytes = await fetch_limited_used_bytes(
        api,
        panel_user_id,
        node_uuids,
        start_date.strftime('%Y-%m-%d'),
        now.strftime('%Y-%m-%d'),
    )
    if used_bytes is None:
        return None
    used_gb = used_bytes / GB_IN_BYTES

    subscription.limited_traffic_used_gb = used_gb
    await db.flush((subscription,))
    await db.commit()
    return used_gb


async def push_limited_traffic_usage(
    subscription: Subscription, user, used_gb: float, limit_gb: int, *, is_trial: bool = False
) -> None:
    """Публикует usage LIMITED-пула на сторону панели (``subscription-merger``).

    Remnawave отдаёт в ``subscription-userinfo`` только суммарный расход по
    ВСЕМУ аккаунту (MAIN + LIMITED squad'ы объединены в одном юзере) — панель
    не умеет отдать расход конкретно LIMITED-пула. merger использует эти цифры,
    чтобы дописать отдельную строку в текст ``announce`` подписки и, при
    исчерпанном лимите, маркер в список серверов. ``is_trial`` переключает
    формулировку заголовка анонса на «пробный период» — бот знает это
    достоверно из своей БД, а не гадает по статусу панели. Best-effort:
    недоступность приёмника не должна ронять enforcement-цикл.
    """
    if not settings.SUBSCRIPTION_MERGER_URL or not settings.SUBSCRIPTION_MERGER_TOKEN:
        return

    # remnawave_short_id — суффикс панельного username, не токен подписки.
    # Публичный URL (и путь, который слушает subscription-merger) собирается
    # из remnawave_short_uuid — см. get_subscription_info/get_subscription_link
    # в app/external/remnawave_api.py (GET /api/sub/{short_uuid}).
    token = subscription.remnawave_short_uuid
    if not token or limit_gb <= 0:
        return

    payload = {
        'token': token,
        'used_gb': round(used_gb, 3),
        'limit_gb': limit_gb,
        'username': (user.username or user.full_name or str(user.telegram_id)) if user else None,
        'is_trial': is_trial,
    }
    url = settings.SUBSCRIPTION_MERGER_URL.rstrip('/') + '/usage'
    headers = {'Authorization': f'Bearer {settings.SUBSCRIPTION_MERGER_TOKEN}'}
    timeout = aiohttp.ClientTimeout(total=settings.SUBSCRIPTION_MERGER_REQUEST_TIMEOUT)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(url, json=payload, headers=headers) as response,
        ):
            if response.status >= 400:
                body = await response.text()
                logger.warning(
                    '⚠️ subscription-merger отклонил usage LIMITED-пула',
                    status=response.status,
                    body=body[:500],
                )
    except Exception as error:
        logger.warning(
            '⚠️ Не удалось отправить usage LIMITED-пула в subscription-merger',
            error=error,
        )


async def sync_limited_squad_state(
    api, db: AsyncSession, subscription: Subscription, tariff: Tariff | None, user
) -> bool | None:
    """Снимает/возвращает LIMITED squad на ``activeInternalSquads`` ОСНОВНОГО юзера.

    MAIN squads (``subscription.connected_squads``) никогда не трогаются и не
    переписываются в БД — этот модуль лишь добавляет/убирает LIMITED squad(ы)
    поверх них в самом PATCH-запросе к панели. ``connected_squads`` остаётся
    источником истины для обычной MAIN-синхронизации (``panel_sync``).

    Всегда PATCH'ит актуальный целевой список сквадов — не пропускает запрос
    по тому, что ``subscription.limited_squad_active`` уже "совпадает" с
    вычисленным состоянием. Два случая, из-за которых пропуск ломал фичу:
    у новой/только что мигрированной подписки это поле по умолчанию ``True``
    ещё до того, как панель хоть раз получила LIMITED squad в PATCH'е — то
    есть "совпадение" было бы случайным и squad так и не добавился бы; а
    любая последующая обычная синхронизация подписки (продление и т.п.)
    шлёт панели ``connected_squads`` без LIMITED squad'ов и молча его снимает,
    не трогая это поле — следующий цикл видел бы "без изменений" и не
    восстановил бы доступ. Reconcile, а не delta: дороже по числу PATCH-
    запросов на цикл enforcement-джобы, зато самовосстанавливается после
    любого внешнего расхождения.

    Возвращает новое состояние ``limited_squad_active``, либо ``None``, если
    механика выключена, у подписки нет панельного аккаунта, или PATCH не
    удался (тогда предыдущее состояние сохраняется как было).
    """
    squad_uuids = get_limited_squad_uuids(tariff)
    if not squad_uuids:
        return None

    panel_user_id = resolve_main_panel_user_id(subscription, user)
    if not panel_user_id:
        return None

    effective_limit_gb = await get_effective_limited_traffic_limit_gb(db, subscription, tariff)
    used_gb = subscription.limited_traffic_used_gb or 0.0

    # 0 = безлимит (условность, унаследованная от companion-версии — база
    # тарифа 0 означает «лимит не задан», а не «доступ закрыт»).
    should_be_active = effective_limit_gb <= 0 or used_gb < effective_limit_gb

    previously_active = bool(subscription.limited_squad_active)
    main_squads = list(getattr(subscription, 'connected_squads', None) or [])
    target_squads = main_squads + squad_uuids if should_be_active else main_squads

    try:
        # Запись в панель только через panel_sync (см. patch_panel_squads) —
        # см. tests/services/panel_sync/test_no_bypass.py. external_squad_uuid
        # передаём текущий тарифный, чтобы не сбросить его этим узким PATCH'ем.
        from app.services.panel_sync import patch_panel_squads

        await patch_panel_squads(
            api,
            user_id=panel_user_id,
            squads=target_squads,
            external_squad_uuid=getattr(tariff, 'external_squad_uuid', None),
        )
    except Exception as error:
        logger.warning(
            '⚠️ Не удалось переключить LIMITED squad',
            subscription_id=subscription.id,
            should_be_active=should_be_active,
            error=error,
        )
        return None

    subscription.limited_squad_active = should_be_active
    await db.flush((subscription,))
    await db.commit()
    if should_be_active != previously_active:
        logger.info(
            '🔀 LIMITED squad переключён',
            subscription_id=subscription.id,
            active=should_be_active,
            used_gb=used_gb,
            limit_gb=effective_limit_gb,
        )
    return should_be_active


async def process_limited_traffic(
    api, db: AsyncSession, subscription: Subscription, tariff: Tariff | None, user
) -> None:
    """Один цикл для подписки: обновить usage, затем при необходимости
    переключить LIMITED squad. Точка входа для периодической enforcement-джобы.

    Best-effort — ошибки здесь не должны ронять весь цикл мониторинга других
    подписок, вызывающий код оборачивает per-subscription try/except.
    """
    await refresh_limited_traffic_usage(api, db, subscription, tariff, user)
    await sync_limited_squad_state(api, db, subscription, tariff, user)

    limit_gb = await get_effective_limited_traffic_limit_gb(db, subscription, tariff)
    await push_limited_traffic_usage(
        subscription,
        user,
        subscription.limited_traffic_used_gb or 0.0,
        limit_gb,
        is_trial=bool(subscription.is_trial),
    )


async def deactivate_orphaned_limited_squad(
    api, db: AsyncSession, subscription: Subscription, tariff: Tariff | None, user
) -> bool | None:
    """Снимает LIMITED squad(ы) тарифа с ОСНОВНОГО юзера подписки, чей тариф
    больше не на новой архитектуре, но ``subscription.limited_squad_active``
    всё ещё ``True``.

    Нужна отдельно от ``sync_limited_squad_state``: та гейтится на
    ``is_limited_traffic_enabled(tariff)`` и сразу возвращает ``None``, если
    механика на тарифе выключена — то есть если админ выключил
    ``limited_traffic_enabled`` у тарифа, чьи подписки уже получили LIMITED
    squad, штатный enforcement-цикл (``_load_subscriptions_with_limited_traffic``,
    фильтрующий именно по включённому тарифу) их больше не видит, и доступ
    остаётся навсегда. Эта функция — обратный случай: вызывается ИМЕННО
    когда механика на тарифе уже выключена, list на снятие берёт из
    ``tariff.limited_squad_uuids`` напрямую (тариф мог сохранить список,
    выключив только флаг).

    Если тариф удалён вовсе (``tariff is None``) — снимать нечего, список
    сквадов неизвестен; такая подписка остаётся необработанной (редкий край,
    требует ручного вмешательства).
    """
    if tariff is None:
        return None

    squad_uuids = get_raw_limited_squad_uuids(tariff)
    if not squad_uuids:
        return None

    panel_user_id = resolve_main_panel_user_id(subscription, user)
    if not panel_user_id:
        return None

    main_squads = list(getattr(subscription, 'connected_squads', None) or [])

    try:
        # Запись в панель только через panel_sync — см. sync_limited_squad_state.
        from app.services.panel_sync import patch_panel_squads

        await patch_panel_squads(
            api,
            user_id=panel_user_id,
            squads=main_squads,
            external_squad_uuid=getattr(tariff, 'external_squad_uuid', None),
        )
    except Exception as error:
        logger.warning(
            '⚠️ Не удалось снять LIMITED squad с подписки выключенного тарифа',
            subscription_id=subscription.id,
            error=error,
        )
        return None

    subscription.limited_squad_active = False
    await db.flush((subscription,))
    await db.commit()
    logger.info(
        '🔻 LIMITED squad снят — тариф больше не на новой архитектуре',
        subscription_id=subscription.id,
    )
    return False
