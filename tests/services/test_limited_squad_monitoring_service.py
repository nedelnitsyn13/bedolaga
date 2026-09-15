"""Периодический цикл LIMITED squad: отбор подписок и best-effort по каждой."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import (
    LimitedCompanionTrafficPurchase,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services.limited_squad_monitoring_service import (
    LimitedSquadMonitoringService,
    _load_subscriptions_with_limited_traffic,
    _load_subscriptions_with_orphaned_limited_squad,
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


async def _create_tariff(db, *, name: str, limited_enabled: bool) -> Tariff:
    tariff = Tariff(
        name=name,
        limited_traffic_enabled=limited_enabled,
        limited_squad_uuids=['limited-squad'] if limited_enabled else [],
        limited_base_traffic_gb=50 if limited_enabled else 0,
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


async def _create_subscription(
    db,
    user: User,
    tariff: Tariff,
    *,
    short_id: str,
    status: str = SubscriptionStatus.ACTIVE.value,
    limited_squad_active: bool = True,
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=status,
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_short_id=short_id,
        limited_squad_active=limited_squad_active,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def test_only_subscriptions_on_limited_enabled_tariffs_are_selected(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9301)
        limited_tariff = await _create_tariff(db, name='LIMITED', limited_enabled=True)
        plain_tariff = await _create_tariff(db, name='Обычный', limited_enabled=False)

        target = await _create_subscription(db, user, limited_tariff, short_id='lm-1')
        await _create_subscription(db, user, plain_tariff, short_id='lm-2')

        subscriptions = await _load_subscriptions_with_limited_traffic(db)

        assert [s.id for s in subscriptions] == [target.id]


async def test_expired_subscriptions_are_excluded(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9302)
        tariff = await _create_tariff(db, name='LIMITED', limited_enabled=True)

        await _create_subscription(db, user, tariff, short_id='lm-3', status=SubscriptionStatus.EXPIRED.value)

        subscriptions = await _load_subscriptions_with_limited_traffic(db)

        assert subscriptions == []


async def test_trial_and_limited_status_subscriptions_are_included(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9303)
        # Разные тарифы — у user+tariff есть частичный уникальный индекс на
        # живые статусы, две подписки одного user на одном тарифе тут не годятся.
        trial_tariff = await _create_tariff(db, name='LIMITED trial', limited_enabled=True)
        limited_tariff = await _create_tariff(db, name='LIMITED limited', limited_enabled=True)

        trial_sub = await _create_subscription(
            db, user, trial_tariff, short_id='lm-4', status=SubscriptionStatus.TRIAL.value
        )
        limited_sub = await _create_subscription(
            db, user, limited_tariff, short_id='lm-5', status=SubscriptionStatus.LIMITED.value
        )

        subscriptions = await _load_subscriptions_with_limited_traffic(db)

        assert {s.id for s in subscriptions} == {trial_sub.id, limited_sub.id}


# ── orphaned: тариф выключили, а LIMITED squad на подписке ещё активен ──


async def test_orphaned_query_finds_subscriptions_left_active_after_tariff_disabled(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9304)
        tariff = await _create_tariff(db, name='Был LIMITED', limited_enabled=False)

        orphaned = await _create_subscription(db, user, tariff, short_id='lm-6', limited_squad_active=True)

        subscriptions = await _load_subscriptions_with_orphaned_limited_squad(db)

        assert [s.id for s in subscriptions] == [orphaned.id]


async def test_orphaned_query_ignores_still_enabled_tariffs(monkeypatch) -> None:
    """limited_squad_active=True на включённом тарифе — штатный случай, его
    обрабатывает _load_subscriptions_with_limited_traffic, а не orphaned-запрос."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9305)
        tariff = await _create_tariff(db, name='LIMITED', limited_enabled=True)

        await _create_subscription(db, user, tariff, short_id='lm-7', limited_squad_active=True)

        subscriptions = await _load_subscriptions_with_orphaned_limited_squad(db)

        assert subscriptions == []


async def test_orphaned_query_ignores_subscriptions_with_squad_already_off(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9306)
        tariff = await _create_tariff(db, name='Был LIMITED', limited_enabled=False)

        await _create_subscription(db, user, tariff, short_id='lm-8', limited_squad_active=False)

        subscriptions = await _load_subscriptions_with_orphaned_limited_squad(db)

        assert subscriptions == []


# ── _notify_exhausted: персональное уведомление при отключении squad'а ──


def _notify_user(**overrides) -> SimpleNamespace:
    base = dict(telegram_id=555, language='ru', notification_settings=None)
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_notify_exhausted_sends_message_with_topup_button(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    service = LimitedSquadMonitoringService(bot=AsyncMock())
    user = _notify_user()
    subscription = SimpleNamespace(id=42)
    tariff = SimpleNamespace(name='LIMITED тариф')

    await service._notify_exhausted(user, subscription, tariff)

    service.bot.send_message.assert_awaited_once()
    call = service.bot.send_message.await_args
    assert call.args[0] == 555
    assert 'LIMITED тариф' in call.args[1]
    assert call.kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'blt:42'


@pytest.mark.asyncio
async def test_notify_exhausted_noop_without_bot():
    service = LimitedSquadMonitoringService(bot=None)
    user = _notify_user()

    await service._notify_exhausted(user, SimpleNamespace(id=1), SimpleNamespace(name='T'))
    # Не должно упасть без бота — тест проходит, если исключения не было.


@pytest.mark.asyncio
async def test_notify_exhausted_noop_without_telegram_id(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    service = LimitedSquadMonitoringService(bot=AsyncMock())
    user = _notify_user(telegram_id=None)

    await service._notify_exhausted(user, SimpleNamespace(id=1), SimpleNamespace(name='T'))

    service.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_exhausted_respects_global_notifications_toggle(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', False)
    service = LimitedSquadMonitoringService(bot=AsyncMock())
    user = _notify_user()

    await service._notify_exhausted(user, SimpleNamespace(id=1), SimpleNamespace(name='T'))

    service.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_exhausted_respects_user_traffic_warning_pref(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    service = LimitedSquadMonitoringService(bot=AsyncMock())
    user = _notify_user(notification_settings={'traffic_warning_enabled': False})

    await service._notify_exhausted(user, SimpleNamespace(id=1), SimpleNamespace(name='T'))

    service.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_exhausted_swallows_send_errors(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError('telegram down')
    service = LimitedSquadMonitoringService(bot=bot)
    user = _notify_user()

    await service._notify_exhausted(user, SimpleNamespace(id=1), SimpleNamespace(name='T'))  # must not raise


# ── _run_cycle: реально дёргает _notify_exhausted на переходе True→False ──


class _FakeApi:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeRemnaWaveService:
    is_configured = True

    def get_api_client(self):
        return _FakeApi()


async def test_run_cycle_notifies_on_transition_to_inactive(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9307)
        tariff = await _create_tariff(db, name='LIMITED', limited_enabled=True)
        subscription = await _create_subscription(db, user, tariff, short_id='lm-9', limited_squad_active=True)

        import app.services.limited_squad_monitoring_service as mod

        # AsyncSessionLocal обычно контекстный менеджер (async with ... as db) —
        # memory_session уже отдаёт открытую сессию, оборачиваем в no-op CM.
        class _SessionCtx:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *exc):
                return False

        monkeypatch.setattr(mod, 'AsyncSessionLocal', lambda: _SessionCtx())
        monkeypatch.setattr(mod, 'RemnaWaveService', _FakeRemnaWaveService)

        async def _fake_process(api, db_arg, sub, trf, usr):
            sub.limited_squad_active = False

        monkeypatch.setattr(mod, 'process_limited_traffic', _fake_process)

        service = mod.LimitedSquadMonitoringService(bot=AsyncMock())
        notify_mock = AsyncMock()
        service._notify_exhausted = notify_mock

        await service._run_cycle()

        notify_mock.assert_awaited_once()
        assert notify_mock.await_args.args[1].id == subscription.id


async def test_run_cycle_does_not_notify_when_still_active(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _create_user(db, telegram_id=9308)
        tariff = await _create_tariff(db, name='LIMITED', limited_enabled=True)
        await _create_subscription(db, user, tariff, short_id='lm-10', limited_squad_active=True)

        import app.services.limited_squad_monitoring_service as mod

        class _SessionCtx:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *exc):
                return False

        monkeypatch.setattr(mod, 'AsyncSessionLocal', lambda: _SessionCtx())
        monkeypatch.setattr(mod, 'RemnaWaveService', _FakeRemnaWaveService)

        async def _fake_process_noop(api, db_arg, sub, trf, usr):
            pass  # состояние не меняется — squad остаётся активным

        monkeypatch.setattr(mod, 'process_limited_traffic', _fake_process_noop)

        service = mod.LimitedSquadMonitoringService(bot=AsyncMock())
        notify_mock = AsyncMock()
        service._notify_exhausted = notify_mock

        await service._run_cycle()

        notify_mock.assert_not_awaited()
