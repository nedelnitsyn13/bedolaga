"""Снимок панели не откатывает лимит трафика, за который недавно заплатили.

Симметрично test_paid_date_hold.py, но для traffic_limit_gb: PATCH нового
лимита в панель при покупке/конверсии триала уходит не мгновенно, и вебхук по
несвязанному поводу (например, обновление расхода трафика) может принести
снимок, снятый панелью ДО того, как её PATCH применился — тогда он откатывает
только что оплаченный лимит обратно на старый (подписка #1170: конверсия
триала в безлимитный тариф откатилась на лимит триала первым же вебхуком,
2026-09-25).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.panel_sync.projection import (
    PAID_DATE_HOLD,
    WEBHOOK,
    PanelSnapshot,
    panel_traffic_limit_behind_paid_purchase,
    project_onto_subscription,
)


NOW = datetime(2026, 9, 25, 11, 6, tzinfo=UTC)


def _subscription(traffic_limit_gb: int) -> SimpleNamespace:
    return SimpleNamespace(
        status='active',
        end_date=NOW + timedelta(days=25),
        traffic_used_gb=0.1,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=3,
        connected_squads=['s1'],
        remnawave_short_uuid='abc',
        subscription_url=None,
        subscription_crypto_link=None,
        grace_session_open=False,
        grace_candidate_reason=None,
        grace_candidate_at=None,
        updated_at=NOW - timedelta(minutes=6),
        last_webhook_update_at=None,
    )


def _snapshot(traffic_limit_gb: int) -> PanelSnapshot:
    return PanelSnapshot(
        status='ACTIVE',
        expire_at=NOW + timedelta(days=25),
        traffic_limit_gb=traffic_limit_gb,
        squads=('s1',),
        short_uuid='abc',
    )


def test_stale_smaller_panel_limit_is_held_right_after_a_purchase():
    """Подписка #1170: триал(5GB) -> Базовый(0=безлимит), панель ещё не догнала."""
    sub = _subscription(0)
    paid_at = NOW - timedelta(minutes=6)
    stale = _snapshot(5)

    assert panel_traffic_limit_behind_paid_purchase(sub, stale, paid_at=paid_at, now=NOW)
    changed = project_onto_subscription(sub, stale, now=NOW, policy=WEBHOOK, paid_at=paid_at)

    assert sub.traffic_limit_gb == 0
    assert 'traffic_limit_gb' not in changed


def test_stale_smaller_finite_panel_limit_is_held_too():
    sub = _subscription(100)
    paid_at = NOW - timedelta(minutes=6)
    stale = _snapshot(30)

    changed = project_onto_subscription(sub, stale, now=NOW, policy=WEBHOOK, paid_at=paid_at)

    assert sub.traffic_limit_gb == 100
    assert 'traffic_limit_gb' not in changed


def test_larger_panel_limit_is_still_taken_after_a_purchase():
    """Расширили лимит ещё и в панели (вручную) — это не откат, берём как раньше."""
    sub = _subscription(50)
    paid_at = NOW - timedelta(minutes=6)
    bigger = _snapshot(200)

    changed = project_onto_subscription(sub, bigger, now=NOW, policy=WEBHOOK, paid_at=paid_at)

    assert sub.traffic_limit_gb == 200
    assert 'traffic_limit_gb' in changed


def test_hold_expires_with_the_window_and_without_a_payment():
    stale = _snapshot(5)

    old_payment = _subscription(0)
    project_onto_subscription(
        old_payment, stale, now=NOW, policy=WEBHOOK, paid_at=NOW - PAID_DATE_HOLD - timedelta(minutes=1)
    )
    assert old_payment.traffic_limit_gb == 5, 'оплата давно — панель снова истина'

    never_paid = _subscription(0)
    project_onto_subscription(never_paid, stale, now=NOW, policy=WEBHOOK, paid_at=None)
    assert never_paid.traffic_limit_gb == 5


def test_equal_limit_is_a_noop():
    sub = _subscription(0)
    assert not panel_traffic_limit_behind_paid_purchase(sub, _snapshot(0), paid_at=NOW, now=NOW)
