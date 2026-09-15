"""LIMITED squad (новая архитектура) — accounting-функции.

Использует in-memory SQLite (``memory_session``): функции этого модуля не
берут row-level lock и не зависят от JSONB — в отличие от
``tests/database/crud/test_limited_companion_traffic.py``, которому для
проверки ``with_for_update`` нужен настоящий PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


# ── enforcement: снятие/возврат LIMITED squad, MAIN не трогается ──


class _RecordingApi(_FakeApi):
    """Как _FakeApi, но запоминает PATCH-вызовы update_user."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.update_calls: list[dict] = []

    async def update_user(self, *, user_id, active_internal_squads):
        self.update_calls.append({'user_id': user_id, 'active_internal_squads': list(active_internal_squads)})


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
            db, user, tariff, short_id='ls-e1', panel_user_id=501, used_gb=50.0, squad_active=True,
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
            db, user, tariff, short_id='ls-e2', panel_user_id=502, used_gb=10.0, squad_active=True,
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
            db, user, tariff, short_id='ls-e3', panel_user_id=503, used_gb=50.0, squad_active=False,
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
            db, user, tariff, short_id='ls-e4', panel_user_id=504, used_gb=85.0, squad_active=True,
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


async def test_no_patch_sent_when_state_unchanged(monkeypatch) -> None:
    """Экономия вызовов к панели: состояние не поменялось — PATCH не шлём."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8205)
        tariff = await _create_tariff(db, squads=['limited-squad'], base_gb=50)
        subscription = await _create_subscription_for_enforcement(
            db, user, tariff, short_id='ls-e5', panel_user_id=505, used_gb=10.0, squad_active=True,
            main_squads=['main-squad-1'],
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is True  # уже активен, под лимитом — остаётся активен
        assert api.update_calls == []


async def test_disabled_tariff_never_touches_panel(monkeypatch) -> None:
    """Механика выключена на тарифе — sync_limited_squad_state не лезет в панель вообще."""
    from app.services.limited_squad_service import sync_limited_squad_state

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=8206)
        tariff = await _create_tariff(db, enabled=False)
        subscription = await _create_subscription_for_enforcement(
            db, user, tariff, short_id='ls-e6', panel_user_id=506, used_gb=999.0, squad_active=True,
        )

        api = _RecordingApi(nodes_by_squad={}, usage_by_node={})
        result = await sync_limited_squad_state(api, db, subscription, tariff, user)

        assert result is None
        assert api.update_calls == []
