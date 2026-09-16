"""LIMITED squad (новая архитектура) — accounting-функции.

Использует in-memory SQLite (``memory_session``): функции этого модуля не
берут row-level lock и не зависят от JSONB — в отличие от
``tests/database/crud/test_limited_companion_traffic.py``, которому для
проверки ``with_for_update`` нужен настоящий PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database.models import (
    LimitedCompanionTrafficPurchase,
    PromoGroup,
    Subscription,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services.limited_squad_service import (
    fetch_limited_used_bytes,
    get_active_limited_traffic_purchases_gb,
    get_effective_limited_traffic_limit_gb,
    get_limited_base_traffic_gb,
    get_limited_squad_uuids,
    get_limited_squad_uuids_for_subscription,
    get_raw_limited_squad_uuids,
    is_limited_traffic_enabled,
    resolve_limited_node_uuids,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    LimitedCompanionTrafficPurchase.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
]


async def _create_user(db, *, telegram_id: int) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_tariff(db, *, enabled: bool = True, squads=None, base_gb: int = 50) -> Tariff:
    tariff = Tariff(
        name='LIMITED test tariff',
        limited_traffic_enabled=enabled,
        limited_squad_uuids=list(squads or ['squad-limited-1']),
        limited_base_traffic_gb=base_gb,
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


async def _create_subscription(db, user: User, tariff: Tariff, *, short_id: str) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id=short_id,
        # Реальный токен подписки (путь в её публичном URL) — отдельное поле,
        # remnawave_short_id это суффикс панельного username. В тестах, где это
        # неважно, оставляем оба равными short_id.
        remnawave_short_uuid=short_id,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


class _FakeNode:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class _FakeApi:
    """Минимальный двойник RemnaWaveAPI для этих тестов."""

    def __init__(self, *, nodes_by_squad: dict[str, list[str]], usage_by_node: dict[str, dict[int, int]]):
        self._nodes_by_squad = nodes_by_squad
        self._usage_by_node = usage_by_node

    async def get_internal_squad_accessible_nodes(self, uuid: str):
        return [_FakeNode(node_uuid) for node_uuid in self._nodes_by_squad.get(uuid, [])]

    async def get_bandwidth_stats_nodes_usage(self, node_uuids, start_date, end_date, min_total_bytes=0):
        nodes = []
        for node_uuid in node_uuids:
            per_user = self._usage_by_node.get(node_uuid, {})
            nodes.append(
                {
                    'uuid': node_uuid,
                    'users': [{'id': user_id, 'totalBytes': total} for user_id, total in per_user.items()],
                }
            )
        return {'nodes': nodes}


class _FailingApi(_FakeApi):
    """Как _FakeApi, но панель падает на запросе usage (транзиентная ошибка)."""

    async def get_bandwidth_stats_nodes_usage(self, node_uuids, start_date, end_date, min_total_bytes=0):
        raise RuntimeError('panel unreachable')


# ── pure functions: is_limited_traffic_enabled / get_limited_squad_uuids / get_limited_base_traffic_gb ──


async def test_disabled_tariff_reports_no_squads_and_zero_base(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        tariff = await _create_tariff(db, enabled=False, squads=['squad-1'], base_gb=50)

        assert is_limited_traffic_enabled(tariff) is False
        assert get_limited_squad_uuids(tariff) == []
        assert get_limited_base_traffic_gb(tariff) == 0


async def test_enabled_tariff_reports_squads_and_base(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        tariff = await _create_tariff(db, enabled=True, squads=['squad-1', 'squad-2'], base_gb=50)

        assert is_limited_traffic_enabled(tariff) is True
        assert get_limited_squad_uuids(tariff) == ['squad-1', 'squad-2']
        assert get_limited_base_traffic_gb(tariff) == 50


async def test_none_tariff_is_disabled() -> None:
    assert is_limited_traffic_enabled(None) is False
    assert get_limited_squad_uuids(None) == []
    assert get_limited_base_traffic_gb(None) == 0


async def test_get_limited_squad_uuids_for_subscription_loads_tariff_by_id(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8401)
        tariff = await _create_tariff(db, enabled=True, squads=['squad-1', 'squad-2'], base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='fk-1')

        assert await get_limited_squad_uuids_for_subscription(db, subscription) == ['squad-1', 'squad-2']


async def test_get_limited_squad_uuids_for_subscription_without_tariff_id(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8402)
        tariff = await _create_tariff(db, enabled=True, squads=['squad-1'], base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='fk-2')
        subscription.tariff_id = None

        assert await get_limited_squad_uuids_for_subscription(db, subscription) == []


async def test_get_limited_squad_uuids_for_subscription_ignores_the_enabled_flag(monkeypatch) -> None:
    """Codex-ревью PR #40 (P1): окно между выключением limited_traffic_enabled
    и deactivate_orphaned_limited_squad, который реально снимает сквад с
    панели — до этого момента панель ещё держит его активным, а он не
    должен считаться MAIN только потому, что флаг уже выключен."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8403)
        tariff = await _create_tariff(db, enabled=False, squads=['squad-orphaned'], base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='fk-3')

        assert get_limited_squad_uuids(tariff) == []  # гейтится — механика выключена
        assert await get_limited_squad_uuids_for_subscription(db, subscription) == ['squad-orphaned']


def test_get_raw_limited_squad_uuids_ignores_the_enabled_flag() -> None:
    tariff = SimpleNamespace(limited_traffic_enabled=False, limited_squad_uuids=['squad-orphaned'])

    assert get_raw_limited_squad_uuids(tariff) == ['squad-orphaned']


def test_get_raw_limited_squad_uuids_none_tariff() -> None:
    assert get_raw_limited_squad_uuids(None) == []


# ── 30-дневные докупки на переиспользуемой таблице ──


async def test_purchase_lives_exactly_30_days_from_its_own_purchase_time(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8101)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-1')

        from app.services.limited_squad_service import add_limited_traffic_purchase

        before = datetime.now(UTC)
        await add_limited_traffic_purchase(db, subscription, 20)
        after = datetime.now(UTC)

        from sqlalchemy import select

        rows = (
            (
                await db.execute(
                    select(LimitedCompanionTrafficPurchase).where(
                        LimitedCompanionTrafficPurchase.subscription_id == subscription.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].traffic_gb == 20
        assert before + timedelta(days=30) <= rows[0].expires_at <= after + timedelta(days=30)


async def test_multiple_purchases_have_independent_expires_at(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8102)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-2')

        from app.services.limited_squad_service import add_limited_traffic_purchase

        await add_limited_traffic_purchase(db, subscription, 20)
        purchased = await get_active_limited_traffic_purchases_gb(db, subscription)
        assert purchased == 20

        await add_limited_traffic_purchase(db, subscription, 50)
        purchased = await get_active_limited_traffic_purchases_gb(db, subscription)
        assert purchased == 70


async def test_remove_purchase_takes_from_oldest_first(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8105)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-5')

        from app.services.limited_squad_service import (
            add_limited_traffic_purchase,
            remove_limited_traffic_purchase,
        )

        await add_limited_traffic_purchase(db, subscription, 20)
        await add_limited_traffic_purchase(db, subscription, 50)

        removed = await remove_limited_traffic_purchase(db, subscription, 30)

        assert removed == 30
        purchased = await get_active_limited_traffic_purchases_gb(db, subscription)
        # 20 (старая, съедена целиком) + 50 (новая, откушено 10) = 40 осталось
        assert purchased == 40


async def test_remove_purchase_caps_at_available_amount(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8106)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-6')

        from app.services.limited_squad_service import (
            add_limited_traffic_purchase,
            remove_limited_traffic_purchase,
        )

        await add_limited_traffic_purchase(db, subscription, 15)

        removed = await remove_limited_traffic_purchase(db, subscription, 100)

        assert removed == 15
        purchased = await get_active_limited_traffic_purchases_gb(db, subscription)
        assert purchased == 0


async def test_remove_purchase_ignores_expired_rows(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8107)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-7')

        from app.services.limited_squad_service import remove_limited_traffic_purchase

        now = datetime.now(UTC)
        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id, traffic_gb=999, expires_at=now - timedelta(seconds=1)
            )
        )
        await db.commit()

        removed = await remove_limited_traffic_purchase(db, subscription, 10)

        assert removed == 0


async def test_remove_purchase_noop_for_non_positive_amount(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8108)
        tariff = await _create_tariff(db)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-8')

        from app.services.limited_squad_service import (
            add_limited_traffic_purchase,
            remove_limited_traffic_purchase,
        )

        await add_limited_traffic_purchase(db, subscription, 20)

        assert await remove_limited_traffic_purchase(db, subscription, 0) == 0
        assert await remove_limited_traffic_purchase(db, subscription, -5) == 0
        purchased = await get_active_limited_traffic_purchases_gb(db, subscription)
        assert purchased == 20


async def test_expired_purchase_lowers_effective_limit(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8103)
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-3')

        now = datetime.now(UTC)
        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id, traffic_gb=20, expires_at=now - timedelta(seconds=1)
            )
        )
        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id, traffic_gb=50, expires_at=now + timedelta(days=10)
            )
        )
        await db.commit()

        effective = await get_effective_limited_traffic_limit_gb(db, subscription, tariff)
        # 50 (база) + 50 (активная докупка) — истёкшая (+20) не считается
        assert effective == 100


async def test_effective_limit_is_zero_unlimited_when_base_is_zero(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8104)
        tariff = await _create_tariff(db, base_gb=0)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-4')

        from app.services.limited_squad_service import add_limited_traffic_purchase

        await add_limited_traffic_purchase(db, subscription, 20)
        effective = await get_effective_limited_traffic_limit_gb(db, subscription, tariff)
        # база 0 = безлимит — докупки не складываются, как и у companion-версии
        assert effective == 0


# ── подсчёт usage: только LIMITED-ноды, MAIN не участвует, несколько нод суммируются ──


@pytest.mark.asyncio
async def test_main_traffic_never_enters_limited_usage() -> None:
    """MAIN-нода не передаётся в fetch — даже если панель вернула бы по ней данные, их некому суммировать."""
    api = _FakeApi(
        nodes_by_squad={'limited-squad': ['node-limited-1']},
        usage_by_node={
            'node-limited-1': {42: 5 * 1024**3},
            'node-main-1': {42: 999 * 1024**3},  # не в LIMITED squad — не должен попасть в запрос
        },
    )
    node_uuids = await resolve_limited_node_uuids(api, ['limited-squad'])
    assert node_uuids == ['node-limited-1']

    used_bytes = await fetch_limited_used_bytes(api, 42, node_uuids, '2026-09-01', '2026-09-15')
    assert used_bytes == 5 * 1024**3


@pytest.mark.asyncio
async def test_multiple_limited_nodes_are_summed() -> None:
    api = _FakeApi(
        nodes_by_squad={'limited-squad': ['node-1', 'node-2', 'node-3']},
        usage_by_node={
            'node-1': {42: 18 * 1024**3},
            'node-2': {42: 21 * 1024**3},
            'node-3': {42: 11 * 1024**3},
        },
    )
    node_uuids = await resolve_limited_node_uuids(api, ['limited-squad'])
    used_bytes = await fetch_limited_used_bytes(api, 42, node_uuids, '2026-09-01', '2026-09-15')
    assert used_bytes == 50 * 1024**3


@pytest.mark.asyncio
async def test_usage_ignores_other_users_on_same_nodes() -> None:
    api = _FakeApi(
        nodes_by_squad={'limited-squad': ['node-1']},
        usage_by_node={'node-1': {42: 5 * 1024**3, 999: 500 * 1024**3}},
    )
    node_uuids = await resolve_limited_node_uuids(api, ['limited-squad'])
    used_bytes = await fetch_limited_used_bytes(api, 42, node_uuids, '2026-09-01', '2026-09-15')
    assert used_bytes == 5 * 1024**3


@pytest.mark.asyncio
async def test_multiple_limited_squads_pool_their_nodes() -> None:
    api = _FakeApi(
        nodes_by_squad={'squad-a': ['node-1'], 'squad-b': ['node-2']},
        usage_by_node={'node-1': {42: 10 * 1024**3}, 'node-2': {42: 15 * 1024**3}},
    )
    node_uuids = await resolve_limited_node_uuids(api, ['squad-a', 'squad-b'])
    assert set(node_uuids) == {'node-1', 'node-2'}
    used_bytes = await fetch_limited_used_bytes(api, 42, node_uuids, '2026-09-01', '2026-09-15')
    assert used_bytes == 25 * 1024**3


@pytest.mark.asyncio
async def test_fetch_returns_none_not_zero_when_panel_call_fails() -> None:
    """Транзиентный сбой панели — это не «трафика не было»: fetch не должен
    возвращать 0, иначе вызывающий затрёт прошлое значение нулём и снятый за
    перелимит squad случайно реактивируется (fail open)."""
    api = _FailingApi(nodes_by_squad={}, usage_by_node={})

    used_bytes = await fetch_limited_used_bytes(api, 42, ['node-1'], '2026-09-01', '2026-09-15')

    assert used_bytes is None


async def test_refresh_preserves_previous_usage_when_panel_call_fails(monkeypatch) -> None:
    from app.services.limited_squad_service import refresh_limited_traffic_usage

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8110)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-fail')
        user.remnawave_id = 601
        subscription.limited_traffic_used_gb = 42.0
        await db.commit()

        api = _FailingApi(nodes_by_squad={'limited-squad': ['node-1']}, usage_by_node={})
        result = await refresh_limited_traffic_usage(api, db, subscription, tariff, user)

        assert result is None
        assert subscription.limited_traffic_used_gb == 42.0


# ── enforcement: снятие/возврат LIMITED squad, MAIN не трогается ──


class _RecordingApi(_FakeApi):
    """Как _FakeApi, но запоминает PATCH-вызовы update_user."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.update_calls: list[dict] = []

    async def update_user(self, *, user_id, active_internal_squads, external_squad_uuid=None):
        self.update_calls.append(
            {
                'user_id': user_id,
                'active_internal_squads': list(active_internal_squads),
                'external_squad_uuid': external_squad_uuid,
            }
        )


async def _create_subscription_for_enforcement(
    db,
    user: User,
    tariff: Tariff,
    *,
    short_id: str,
    panel_user_id: int,
    used_gb: float,
    squad_active: bool,
    main_squads=None,
) -> Subscription:
    user.remnawave_id = panel_user_id
    await db.commit()

    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id=short_id,
        remnawave_id=panel_user_id,
        connected_squads=list(main_squads or ['main-squad-1']),
        limited_traffic_used_gb=used_gb,
        limited_squad_active=squad_active,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def test_squad_removed_when_usage_reaches_base_limit(monkeypatch) -> None:
    """п.3: база 50 ГБ, usage 50 ГБ → LIMITED squad снимается."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8201)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription_for_enforcement(
            db,
            user,
            tariff,
            short_id='ls-e1',
            panel_user_id=501,
            used_gb=50.0,
            squad_active=True,
            main_squads=['main-squad-1'],
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is False
        assert subscription.limited_squad_active is False
        assert len(api.update_calls) == 1
        # п.4: MAIN squad остаётся в PATCH-списке, LIMITED — нет.
        assert api.update_calls[0]['active_internal_squads'] == ['main-squad-1']
        assert api.update_calls[0]['user_id'] == 501


async def test_main_squads_untouched_when_limited_removed(monkeypatch) -> None:
    """п.4 отдельно: несколько MAIN squads — все остаются после снятия LIMITED."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8202)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=10)
        subscription = await _create_subscription_for_enforcement(
            db,
            user,
            tariff,
            short_id='ls-e2',
            panel_user_id=502,
            used_gb=10.0,
            squad_active=True,
            main_squads=['main-1', 'main-2', 'main-3'],
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert api.update_calls[0]['active_internal_squads'] == ['main-1', 'main-2', 'main-3']


async def test_purchase_restores_limited_squad(monkeypatch) -> None:
    """п.5: +20 ГБ докупка поднимает effective limit выше usage → LIMITED возвращается."""
    from app.services.limited_squad_service import add_limited_traffic_purchase, sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8203)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription_for_enforcement(
            db,
            user,
            tariff,
            short_id='ls-e3',
            panel_user_id=503,
            used_gb=50.0,
            squad_active=False,
            main_squads=['main-squad-1'],
        )

        await add_limited_traffic_purchase(db, subscription, 20)

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is True
        assert subscription.limited_squad_active is True
        assert set(api.update_calls[0]['active_internal_squads']) == {'main-squad-1', 'limited-squad'}


async def test_expired_purchase_removes_limited_squad_again(monkeypatch) -> None:
    """п.10: usage=85, лимит был 100 (база 50 + докупка 50) — докупка истекла,
    новый лимит 50 — 85 >= 50 → LIMITED снимается."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8204)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription_for_enforcement(
            db,
            user,
            tariff,
            short_id='ls-e4',
            panel_user_id=504,
            used_gb=85.0,
            squad_active=True,
            main_squads=['main-squad-1'],
        )

        # Докупка уже истекла — не учитывается в effective limit.
        db.add(
            LimitedCompanionTrafficPurchase(
                subscription_id=subscription.id, traffic_gb=50, expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is False
        assert subscription.limited_squad_active is False


async def test_patch_is_sent_even_when_computed_state_matches_the_flag(monkeypatch) -> None:
    """Reconcile, а не delta: PATCH шлётся каждый цикл, даже когда вычисленное
    состояние совпадает с ``limited_squad_active`` — иначе не отличить «squad
    правда уже на панели» от «флаг случайно совпал с default'ом» (новая/только
    что мигрированная подписка) или «squad молча снят обычной синхронизацией
    подписки, а флаг остался прежним» (продление и т.п.)."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8205)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription_for_enforcement(
            db,
            user,
            tariff,
            short_id='ls-e5',
            panel_user_id=505,
            used_gb=10.0,
            squad_active=True,
            main_squads=['main-squad-1'],
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is True  # уже активен, под лимитом — остаётся активен
        assert len(api.update_calls) == 1
        assert set(api.update_calls[0]['active_internal_squads']) == {'main-squad-1', 'limited-squad'}


async def test_first_activation_adds_squad_despite_default_active_flag(monkeypatch) -> None:
    """Регрессия: даже когда вычисленное ``should_be_active`` для новой подписки
    ОТЛИЧАЕТСЯ от дефолта колонки (``limited_squad_active=False``, см. 0125 —
    исходный дефолт ``True`` делал orphaned-запрос неотличимым от «вообще не
    касалась новой архитектуры», см. миграцию), sync всё равно шлёт PATCH и
    правильно включает squad — а не полагается на то, что дефолт когда-либо
    "случайно совпадёт" с реальным состоянием панели."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8207)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='ls-e7')
        user.remnawave_id = 507
        subscription.connected_squads = ['main-squad-1']
        subscription.limited_traffic_used_gb = 5.0
        # Дефолт колонки — False (новая подписка ещё не была в LIMITED-пуле);
        # это первый цикл enforcement для неё, usage < базы, значит должна
        # активироваться.
        assert subscription.limited_squad_active is False
        await db.commit()

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is True
        assert len(api.update_calls) == 1
        assert set(api.update_calls[0]['active_internal_squads']) == {'main-squad-1', 'limited-squad'}


async def test_disabled_tariff_never_touches_panel(monkeypatch) -> None:
    """Механика выключена на тарифе — sync_limited_squad_state не лезет в панель вообще."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8206)
        tariff = await _create_tariff(db, enabled=False)
        subscription = await _create_subscription_for_enforcement(
            db,
            user,
            tariff,
            short_id='ls-e6',
            panel_user_id=506,
            used_gb=999.0,
            squad_active=True,
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is None
        assert api.update_calls == []


# ── deactivate_orphaned_limited_squad: тариф выключили после того, как
# подписка уже получила LIMITED squad на панели ──


async def test_deactivate_orphaned_removes_squad_and_clears_flag(monkeypatch) -> None:
    from app.services.limited_squad_service import deactivate_orphaned_limited_squad

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8208)
        # limited_enabled=False — тариф уже выключен, но список сквадов сохранён.
        tariff = await _create_tariff(db, enabled=False, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription_for_enforcement(
            db,
            user,
            tariff,
            short_id='ls-e8',
            panel_user_id=508,
            used_gb=10.0,
            squad_active=True,
            main_squads=['main-squad-1'],
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await deactivate_orphaned_limited_squad(api, db, subscription, tariff, user)

        assert result is False
        assert subscription.limited_squad_active is False
        assert len(api.update_calls) == 1
        assert api.update_calls[0]['active_internal_squads'] == ['main-squad-1']


async def test_deactivate_orphaned_noop_without_a_tariff(monkeypatch) -> None:
    from app.services.limited_squad_service import deactivate_orphaned_limited_squad

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8209)
        tariff = await _create_tariff(db, enabled=False, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription_for_enforcement(
            db, user, tariff, short_id='ls-e9', panel_user_id=509, used_gb=10.0, squad_active=True
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await deactivate_orphaned_limited_squad(api, db, subscription, None, user)

        assert result is None
        assert api.update_calls == []


# ── push_limited_traffic_usage: публикация usage на сторону панели ──


class _FakeResponse:
    def __init__(self, status: int, body: str = '') -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse, calls: list) -> None:
        self._response = response
        self._calls = calls

    def post(self, url, json=None, headers=None):
        self._calls.append({'url': url, 'json': json, 'headers': headers})
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _patch_merger_settings(monkeypatch, *, url='https://merger.local', token='secret-token') -> None:  # noqa: S107
    from app.config import settings

    monkeypatch.setattr(settings, 'SUBSCRIPTION_MERGER_URL', url)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_MERGER_TOKEN', token)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_MERGER_REQUEST_TIMEOUT', 10)


def _patch_aiohttp(monkeypatch, *, status: int = 200, body: str = '') -> list:
    import app.services.limited_squad_service as m

    calls: list = []
    response = _FakeResponse(status, body)
    monkeypatch.setattr(m.aiohttp, 'ClientSession', lambda timeout=None: _FakeSession(response, calls))
    return calls


async def test_push_usage_noop_without_merger_settings(monkeypatch) -> None:
    from app.config import settings
    from app.services.limited_squad_service import push_limited_traffic_usage

    monkeypatch.setattr(settings, 'SUBSCRIPTION_MERGER_URL', None)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_MERGER_TOKEN', None)
    calls = _patch_aiohttp(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8301)
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='push-1')

        await push_limited_traffic_usage(subscription, user, 10.0, 50)

    assert calls == []


async def test_push_usage_noop_without_a_token(monkeypatch) -> None:
    _patch_merger_settings(monkeypatch)
    calls = _patch_aiohttp(monkeypatch)
    from app.services.limited_squad_service import push_limited_traffic_usage

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8302)
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='push-2')
        subscription.remnawave_short_uuid = None

        await push_limited_traffic_usage(subscription, user, 10.0, 50)

    assert calls == []


async def test_push_usage_noop_when_limit_is_zero(monkeypatch) -> None:
    """limit_gb<=0 = безлимит — показывать в списке серверов нечего."""
    _patch_merger_settings(monkeypatch)
    calls = _patch_aiohttp(monkeypatch)
    from app.services.limited_squad_service import push_limited_traffic_usage

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8303)
        tariff = await _create_tariff(db, base_gb=0)
        subscription = await _create_subscription(db, user, tariff, short_id='push-3')

        await push_limited_traffic_usage(subscription, user, 10.0, 0)

    assert calls == []


async def test_push_usage_sends_expected_payload(monkeypatch) -> None:
    _patch_merger_settings(monkeypatch)
    calls = _patch_aiohttp(monkeypatch)
    from app.services.limited_squad_service import push_limited_traffic_usage

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8304)
        user.username = 'ivan'
        await db.commit()
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='push-4')

        await push_limited_traffic_usage(subscription, user, 12.3456, 70)

    assert len(calls) == 1
    call = calls[0]
    assert call['url'] == 'https://merger.local/usage'
    assert call['headers'] == {'Authorization': 'Bearer secret-token'}
    assert call['json'] == {
        'token': 'push-4',
        'used_gb': 12.346,
        'limit_gb': 70,
        'username': 'ivan',
        'is_trial': False,
    }


async def test_push_usage_marks_trial_subscriptions(monkeypatch) -> None:
    """Триал определяется по данным бота (is_trial), а не по статусу панели."""
    _patch_merger_settings(monkeypatch)
    calls = _patch_aiohttp(monkeypatch)
    from app.services.limited_squad_service import push_limited_traffic_usage

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8307)
        tariff = await _create_tariff(db, base_gb=5)
        subscription = await _create_subscription(db, user, tariff, short_id='push-7')
        subscription.is_trial = True
        await db.commit()

        await push_limited_traffic_usage(subscription, user, 1.0, 5, is_trial=True)

    assert calls[0]['json']['is_trial'] is True


async def test_push_usage_logs_but_does_not_raise_on_rejection(monkeypatch) -> None:
    _patch_merger_settings(monkeypatch)
    _patch_aiohttp(monkeypatch, status=401, body='bad token')
    from app.services.limited_squad_service import push_limited_traffic_usage

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8305)
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='push-5')

        await push_limited_traffic_usage(subscription, user, 1.0, 50)


async def test_push_usage_logs_but_does_not_raise_on_network_error(monkeypatch) -> None:
    _patch_merger_settings(monkeypatch)
    import app.services.limited_squad_service as m

    def _raise(timeout=None):
        raise RuntimeError('connection refused')

    monkeypatch.setattr(m.aiohttp, 'ClientSession', _raise)
    from app.services.limited_squad_service import push_limited_traffic_usage

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8306)
        tariff = await _create_tariff(db, base_gb=50)
        subscription = await _create_subscription(db, user, tariff, short_id='push-6')

        await push_limited_traffic_usage(subscription, user, 1.0, 50)
