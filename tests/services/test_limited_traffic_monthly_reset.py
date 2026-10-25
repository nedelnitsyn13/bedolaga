"""LIMITED-пул: ежемесячный сброс 1-го числа и платное «обновить трафик сейчас»."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.database.models import (
    LimitedCompanionTrafficPurchase,
    PromoGroup,
    Subscription,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services import limited_squad_service
from app.services.limited_squad_service import (
    apply_limited_traffic_reset,
    limited_reset_price_kopeks,
    limited_usage_window,
    measure_limited_reset_baseline,
    next_limited_reset_at,
    refresh_limited_traffic_usage,
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
GB = 1024**3
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _sub(**fields) -> SimpleNamespace:
    return SimpleNamespace(
        start_date=fields.get('start_date', datetime(2026, 8, 7, tzinfo=UTC)),
        created_at=None,
        limited_traffic_reset_at=fields.get('reset_at'),
        limited_traffic_reset_offset_bytes=fields.get('offset', 0),
    )


# ── окно расчёта ──


def test_window_starts_on_the_first_of_the_month() -> None:
    assert limited_usage_window(_sub(), NOW) == (date(2026, 10, 1), 0)


def test_window_starts_with_a_subscription_that_began_this_month() -> None:
    assert limited_usage_window(_sub(start_date=datetime(2026, 10, 12, 9, tzinfo=UTC)), NOW) == (
        date(2026, 10, 12),
        0,
    )


def test_paid_reset_this_month_moves_the_window_and_keeps_the_offset() -> None:
    sub = _sub(reset_at=datetime(2026, 10, 20, 15, tzinfo=UTC), offset=3 * GB)
    assert limited_usage_window(sub, NOW) == (date(2026, 10, 20), 3 * GB)


def test_last_months_paid_reset_is_forgotten_on_the_first() -> None:
    sub = _sub(reset_at=datetime(2026, 9, 28, tzinfo=UTC), offset=3 * GB)
    assert limited_usage_window(sub, NOW) == (date(2026, 10, 1), 0)


def test_naive_start_date_is_treated_as_utc() -> None:
    assert limited_usage_window(_sub(start_date=datetime(2026, 10, 3)), NOW) == (date(2026, 10, 3), 0)


def test_next_reset_is_the_first_of_next_month() -> None:
    assert next_limited_reset_at(NOW) == datetime(2026, 11, 1, tzinfo=UTC)
    assert next_limited_reset_at(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)


# ── цена ──


def test_reset_price_is_gb_price_times_pool_size(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'LIMITED_TRAFFIC_GB_PRICE_KOPEKS', 300)
    tariff = SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=50, limited_squad_uuids=['s'])
    assert limited_reset_price_kopeks(tariff) == 15000


def _packages(monkeypatch, packages: list[dict]) -> None:
    monkeypatch.setattr(type(settings), 'get_traffic_topup_packages', lambda self: packages)


def test_reset_price_falls_back_to_the_matching_topup_package(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'LIMITED_TRAFFIC_GB_PRICE_KOPEKS', 0)
    _packages(monkeypatch, [{'gb': 10, 'price': 5000, 'enabled': True}, {'gb': 50, 'price': 20000, 'enabled': True}])
    tariff = SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=50, limited_squad_uuids=['s'])
    assert limited_reset_price_kopeks(tariff) == 20000


def test_reset_price_uses_the_smallest_package_rate_without_a_match(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'LIMITED_TRAFFIC_GB_PRICE_KOPEKS', 0)
    _packages(
        monkeypatch,
        [
            {'gb': 10, 'price': 5000, 'enabled': True},
            {'gb': 5, 'price': 4000, 'enabled': False},
            {'gb': 100, 'price': 30000, 'enabled': True},
        ],
    )
    tariff = SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=30, limited_squad_uuids=['s'])
    assert limited_reset_price_kopeks(tariff) == 15000


def test_reset_price_is_zero_without_a_price_or_a_pool(monkeypatch) -> None:
    tariff = SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=50, limited_squad_uuids=['s'])
    monkeypatch.setattr(settings, 'LIMITED_TRAFFIC_GB_PRICE_KOPEKS', 0)
    _packages(monkeypatch, [])
    assert limited_reset_price_kopeks(tariff) == 0

    monkeypatch.setattr(settings, 'LIMITED_TRAFFIC_GB_PRICE_KOPEKS', 300)
    assert limited_reset_price_kopeks(None) == 0
    unlimited = SimpleNamespace(limited_traffic_enabled=True, limited_base_traffic_gb=0, limited_squad_uuids=['s'])
    assert limited_reset_price_kopeks(unlimited) == 0


# ── панель ──


class _Node:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class _Api:
    def __init__(self, used_bytes: int) -> None:
        self.used_bytes = used_bytes
        self.usage_calls: list[tuple[str, str]] = []
        self.update_calls: list[list[str]] = []

    async def get_internal_squad_accessible_nodes(self, uuid: str):
        return [_Node('node-1')]

    async def get_bandwidth_stats_nodes_usage(self, node_uuids, start_date, end_date, min_total_bytes=0):
        self.usage_calls.append((start_date, end_date))
        return {'nodes': [{'uuid': 'node-1', 'users': [{'id': 77, 'totalBytes': self.used_bytes}]}]}

    async def update_user(self, *, user_id, active_internal_squads, external_squad_uuid=None):
        self.update_calls.append(list(active_internal_squads))


async def _setup(db, *, used_gb: float, squad_active: bool = True):
    user = User(telegram_id=8801, first_name='Тест', language='ru', balance_kopeks=0, remnawave_id=77)
    db.add(user)
    tariff = Tariff(
        name='LIMITED', limited_traffic_enabled=True, limited_squad_uuids=['limited-squad'], limited_base_traffic_gb=50
    )
    db.add(tariff)
    await db.commit()
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        start_date=datetime.now(UTC) - timedelta(days=90),
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id='reset-1',
        remnawave_id=77,
        connected_squads=['main-squad'],
        limited_traffic_used_gb=used_gb,
        limited_squad_active=squad_active,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return user, tariff, subscription


async def test_refresh_counts_from_the_first_of_the_month(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _setup(db, used_gb=0)
        api = _Api(used_bytes=10 * GB)

        await refresh_limited_traffic_usage(api, db, subscription, tariff, user)

        today = datetime.now(UTC)
        assert api.usage_calls == [(today.strftime('%Y-%m-01'), today.strftime('%Y-%m-%d'))]
        assert subscription.limited_traffic_used_gb == 10.0


async def test_refresh_subtracts_what_was_used_before_a_paid_reset(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _setup(db, used_gb=0)
        subscription.limited_traffic_reset_at = datetime.now(UTC)
        subscription.limited_traffic_reset_offset_bytes = 4 * GB
        await db.commit()
        api = _Api(used_bytes=5 * GB)

        await refresh_limited_traffic_usage(api, db, subscription, tariff, user)

        today = datetime.now(UTC).strftime('%Y-%m-%d')
        assert api.usage_calls == [(today, today)]
        assert subscription.limited_traffic_used_gb == 1.0


async def test_baseline_is_none_when_the_panel_fails(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _setup(db, used_gb=0)

        class _Down(_Api):
            async def get_bandwidth_stats_nodes_usage(self, *args, **kwargs):
                raise RuntimeError('panel down')

        assert await measure_limited_reset_baseline(_Down(0), subscription, tariff, user) is None
        assert await measure_limited_reset_baseline(_Api(2 * GB), subscription, tariff, user) == 2 * GB


async def test_apply_reset_zeroes_usage_and_returns_the_squad(monkeypatch) -> None:
    pushed: list[float] = []

    async def _push(subscription, user, used_gb, limit_gb, *, is_trial=False):
        pushed.append(used_gb)

    monkeypatch.setattr(limited_squad_service, 'push_limited_traffic_usage', _push)

    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _setup(db, used_gb=50.0, squad_active=False)
        api = _Api(used_bytes=0)

        await apply_limited_traffic_reset(api, db, subscription, tariff, user, 3 * GB)

        assert subscription.limited_traffic_used_gb == 0.0
        assert subscription.limited_traffic_reset_offset_bytes == 3 * GB
        assert subscription.limited_traffic_reset_at is not None
        assert subscription.limited_squad_active is True
        assert api.update_calls == [['main-squad', 'limited-squad']]
        assert pushed == [0.0]
