#!/usr/bin/env python
"""One-shot CLI to migrate a legacy `remnawave-shopbot` SQLite database into bedolaga.

Legacy bot (``remnawave-shopbot``, plain SQLite, money in rubles) and bedolaga
(Postgres, money in kopeks) are unrelated codebases. This script performs a
single point-in-time transfer: users + Telegram binding, balance + transaction
history, active VPN keys (as multi-tariff ``Subscription`` rows, reusing the
existing panel identity — no Remnawave API calls, no new panel accounts) and
the referral graph (``referred_by`` + historical ``referral_reward_events`` for
stats).

SAFETY BOUNDARY — read this before running ``--apply``:
    This script only ever CREATES data for a legacy ``telegram_id`` that does
    NOT already exist as a bedolaga user. If a bedolaga user with that
    ``telegram_id`` already exists, that user's balance/status/subscriptions/
    transactions/referred_by are left completely untouched — only the mapping
    needed to resolve *other* rows' ``referred_by`` is read. This makes the
    script idempotent and safe to re-run after a partial failure, but it also
    means it is NOT a general sync tool: it is for a true one-time cutover
    into a bot that does not yet know these customers.

Transaction type classification (confirmed against bedolaga's own
``create_transaction`` helper, not guessed):
    - YooKassa, YooKassa Autopay -> DEPOSIT, payment_method=yookassa
    - Platega                    -> DEPOSIT, payment_method=platega
    - Admin                      -> DEPOSIT, payment_method=manual
    - Referral                   -> REFERRAL_REWARD, payment_method=None
    - Balance                    -> SUBSCRIPTION_PAYMENT, payment_method=balance,
                                     amount stored NEGATIVE
      bedolaga's own create_transaction() defaults payment_method to BALANCE
      specifically for SUBSCRIPTION_PAYMENT/GIFT_PAYMENT paid out of wallet
      balance, and always stores those two types as a negative debit — the
      legacy 'Balance' payment_method rows are that exact case (a purchase
      paid from the wallet), not a top-up.
    - Test                       -> skipped by default (--include-test to import
                                     as DEPOSIT/manual)

Known limitations (out of scope for this pass, documented rather than guessed
around):
    - device_limit on imported subscriptions falls back to
      settings.DEFAULT_DEVICE_LIMIT; per-key device tiers
      (key_device_settings/device_tiers) are not migrated.
    - tariff_id is left NULL on imported subscriptions (no old-plan ->
      new-tariff mapping was defined); this does not violate multi-tariff
      uniqueness (that constraint only applies when tariff_id IS NOT NULL).
    - autopay_enabled is always imported as False regardless of the legacy
      "YooKassa Autopay" transactions, so nobody is auto-charged under a
      payment setup they never explicitly re-confirmed.
    - old.trial_used is not carried over as a standalone flag (bedolaga has
      none); a user only reads as "trial already used" if they have
      has_had_paid_subscription=True or an imported non-trial subscription.
      A user whose only history was an expired, unrenewed trial key can in
      principle reclaim a trial in bedolaga.

Usage:
    python -m scripts.migrate_shopbot --source /path/to/users.db              # dry run
    python -m scripts.migrate_shopbot --source /path/to/users.db --apply      # persist

A dry run is the default on purpose: it runs every insert against the real
Postgres transaction (so IDs, unique constraints and FK resolution are all
exercised for real) and then rolls back. Read the report before --apply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.crud.subscription import generate_unique_short_id
from app.database.crud.user import create_user_no_commit
from app.database.database import AsyncSessionLocal
from app.database.models import (
    PaymentMethod,
    ReferralEarning,
    ReferralRewardType,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)

# legacy payment_method -> (TransactionType, PaymentMethod | None, sign)
# sign=1 keeps amount positive (credit), sign=-1 stores it negative (debit),
# matching app.database.crud.transaction.create_transaction's own convention.
_DEPOSIT_METHOD_MAP = {
    'YooKassa': PaymentMethod.YOOKASSA,
    'YooKassa Autopay': PaymentMethod.YOOKASSA,
    'Platega': PaymentMethod.PLATEGA,
    'Admin': PaymentMethod.MANUAL,
}


@dataclass
class MigrationReport:
    dry_run: bool
    users_created: int = 0
    users_skipped_existing: int = 0
    users_referrer_unresolved: int = 0
    subscriptions_created: int = 0
    subscriptions_skipped_owner_exists: int = 0
    subscriptions_skipped_no_panel_link: int = 0
    subscriptions_skipped_expired: int = 0
    subscriptions_skipped_duplicate_remnawave_id: int = 0
    transactions_created: int = 0
    transactions_skipped_owner_exists: int = 0
    transactions_skipped_test: int = 0
    transactions_skipped_duplicate: int = 0
    transactions_by_method: dict = field(default_factory=dict)
    referral_earnings_created: int = 0
    referral_earnings_skipped_unresolved: int = 0
    referral_earnings_skipped_duplicate: int = 0
    unresolved_lines: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != 'unresolved_lines'}


def _parse_sqlite_dt(value: str | None) -> datetime | None:
    """Parse a SQLite ``TIMESTAMP DEFAULT CURRENT_TIMESTAMP`` string as UTC.

    SQLite's CURRENT_TIMESTAMP is UTC by definition, and the legacy schema
    never stored an offset, so naive strings are assumed UTC.
    """
    if not value:
        return None
    text = value.strip().replace(' ', 'T', 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _load_source(source_path: Path) -> dict:
    # mode=ro: never write to the legacy DB, and tolerate it still being read
    # while the old bot is up (for an early --dry-run preview); the real
    # --apply pass should only run after the old bot is stopped, per the
    # confirmed cutover plan.
    uri = f'file:{source_path}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        users = conn.execute('SELECT * FROM users').fetchall()
        vpn_keys = conn.execute('SELECT * FROM vpn_keys').fetchall()
        transactions = conn.execute('SELECT * FROM transactions').fetchall()
        referral_events = conn.execute('SELECT * FROM referral_reward_events').fetchall()
    finally:
        conn.close()
    return {
        'users': [dict(r) for r in users],
        'vpn_keys': [dict(r) for r in vpn_keys],
        'transactions': [dict(r) for r in transactions],
        'referral_events': [dict(r) for r in referral_events],
    }


async def _migrate(
    db,
    data: dict,
    *,
    apply: bool,
    include_expired: bool,
    include_test: bool,
    limit: int | None,
) -> MigrationReport:
    report = MigrationReport(dry_run=not apply)

    old_users = data['users']
    if limit:
        old_users = old_users[:limit]
    old_telegram_ids = [u['telegram_id'] for u in old_users]
    old_users_by_tg = {u['telegram_id']: u for u in old_users}

    existing_rows = (
        (
            await db.execute(
                select(User.id, User.telegram_id).where(User.telegram_id.in_(old_telegram_ids))
            )
        )
        .all()
        if old_telegram_ids
        else []
    )
    telegram_to_new_id: dict[int, int] = {tid: uid for uid, tid in existing_rows}
    newly_created_telegram_ids: set[int] = set()

    # ---- Pass 1: create missing users -------------------------------------------------
    for row in old_users:
        tg_id = row['telegram_id']
        if tg_id in telegram_to_new_id:
            report.users_skipped_existing += 1
            continue

        user = await create_user_no_commit(
            db,
            telegram_id=tg_id,
            username=row.get('username'),
        )
        balance_kopeks = round((row.get('balance') or 0.0) * 100)
        total_spent = row.get('total_spent') or 0.0
        user.balance_kopeks = balance_kopeks
        user.status = UserStatus.BLOCKED.value if row.get('is_banned') else UserStatus.ACTIVE.value
        user.has_had_paid_subscription = total_spent > 0
        user.has_made_first_topup = total_spent > 0
        created_at = _parse_sqlite_dt(row.get('registration_date'))
        if created_at:
            user.created_at = created_at
        await db.flush()

        telegram_to_new_id[tg_id] = user.id
        newly_created_telegram_ids.add(tg_id)
        report.users_created += 1

    # ---- Pass 2: resolve referred_by for the users we just created --------------------
    for row in old_users:
        tg_id = row['telegram_id']
        if tg_id not in newly_created_telegram_ids:
            continue
        referred_by = row.get('referred_by')
        if not referred_by:
            continue
        referrer_new_id = telegram_to_new_id.get(referred_by)
        if referrer_new_id is None:
            report.users_referrer_unresolved += 1
            report.unresolved_lines.append(f'user tg={tg_id}: referrer tg={referred_by} not found')
            continue
        new_user = await db.get(User, telegram_to_new_id[tg_id])
        new_user.referred_by_id = referrer_new_id

    await db.flush()

    # ---- Pass 3: vpn_keys -> Subscription rows (only for newly created owners) --------
    now = datetime.now(UTC)
    existing_remnawave_ids = {
        rid
        for (rid,) in (await db.execute(select(Subscription.remnawave_id).where(Subscription.remnawave_id.is_not(None)))).all()
    }
    seen_remnawave_ids: set[int] = set(existing_remnawave_ids)

    default_device_limit = settings.DEFAULT_DEVICE_LIMIT or 1

    for key in data['vpn_keys']:
        # vpn_keys.user_id is the legacy telegram_id (legacy schema has no
        # separate internal user pk it joins on for this table).
        owner_row = old_users_by_tg.get(key['user_id'])
        if owner_row is None:
            continue
        tg_id = owner_row['telegram_id']
        if tg_id not in newly_created_telegram_ids:
            report.subscriptions_skipped_owner_exists += 1
            continue

        remnawave_id = key.get('remnawave_user_id')
        short_uuid = key.get('short_uuid')
        if not remnawave_id or not short_uuid:
            report.subscriptions_skipped_no_panel_link += 1
            continue
        if remnawave_id in seen_remnawave_ids:
            report.subscriptions_skipped_duplicate_remnawave_id += 1
            report.unresolved_lines.append(f'vpn_key #{key.get("key_id")}: remnawave_id={remnawave_id} already imported')
            continue

        end_date = _parse_sqlite_dt(key.get('expire_at'))
        if end_date is None:
            report.subscriptions_skipped_no_panel_link += 1
            continue
        if not include_expired and end_date <= now:
            report.subscriptions_skipped_expired += 1
            continue

        traffic_limit_bytes = key.get('traffic_limit_bytes') or 0
        traffic_limit_gb = round(traffic_limit_bytes / (1024**3)) if traffic_limit_bytes > 0 else 0

        is_trial = bool(key.get('is_trial'))
        squad_uuid = key.get('squad_uuid')

        short_id = await generate_unique_short_id(db)
        subscription = Subscription(
            user_id=telegram_to_new_id[tg_id],
            status=SubscriptionStatus.TRIAL.value if is_trial else SubscriptionStatus.ACTIVE.value,
            is_trial=is_trial,
            start_date=_parse_sqlite_dt(key.get('created_at')) or now,
            end_date=end_date,
            traffic_limit_gb=traffic_limit_gb,
            traffic_used_gb=0.0,
            device_limit=default_device_limit,
            connected_squads=[squad_uuid] if squad_uuid else [],
            subscription_url=key.get('subscription_url') or None,
            remnawave_short_uuid=short_uuid,
            remnawave_id=remnawave_id,
            remnawave_uuid=key.get('remnawave_user_uuid'),
            remnawave_short_id=short_id,
            autopay_enabled=False,
            created_at=_parse_sqlite_dt(key.get('created_at')) or now,
            updated_at=_parse_sqlite_dt(key.get('updated_at')) or now,
        )
        db.add(subscription)
        await db.flush()
        seen_remnawave_ids.add(remnawave_id)
        report.subscriptions_created += 1

    # ---- Pass 4: transactions -> Transaction rows (only for newly created owners) -----
    # Scoped to this batch's own payment_ids: a brand-new user (the only owners this
    # pass ever inserts for) cannot already have production transactions, so the only
    # possible collision is with a previous partial run of this same script.
    legacy_payment_ids = [t.get('payment_id') for t in data['transactions'] if t.get('payment_id')]
    existing_external_ids = (
        {
            (ext_id, method)
            for (ext_id, method) in (
                await db.execute(
                    select(Transaction.external_id, Transaction.payment_method).where(
                        Transaction.external_id.in_(legacy_payment_ids)
                    )
                )
            ).all()
        }
        if legacy_payment_ids
        else set()
    )

    for txn in data['transactions']:
        owner_row = old_users_by_tg.get(txn['user_id'])
        if owner_row is None:
            continue
        tg_id = owner_row['telegram_id']
        if tg_id not in newly_created_telegram_ids:
            report.transactions_skipped_owner_exists += 1
            continue

        method_name = txn.get('payment_method') or ''
        amount_rub = txn.get('amount_rub') or 0.0
        amount_kopeks = round(amount_rub * 100)
        payment_id = txn.get('payment_id')
        created_at = _parse_sqlite_dt(txn.get('created_date')) or datetime.now(UTC)

        if method_name == 'Test':
            if not include_test:
                report.transactions_skipped_test += 1
                continue
            txn_type = TransactionType.DEPOSIT
            payment_method = PaymentMethod.MANUAL
            stored_amount = amount_kopeks
        elif method_name == 'Referral':
            txn_type = TransactionType.REFERRAL_REWARD
            payment_method = None
            stored_amount = amount_kopeks
        elif method_name == 'Balance':
            txn_type = TransactionType.SUBSCRIPTION_PAYMENT
            payment_method = PaymentMethod.BALANCE
            stored_amount = -amount_kopeks
        elif method_name in _DEPOSIT_METHOD_MAP:
            txn_type = TransactionType.DEPOSIT
            payment_method = _DEPOSIT_METHOD_MAP[method_name]
            stored_amount = amount_kopeks
        else:
            report.unresolved_lines.append(f'transaction {payment_id}: unknown payment_method={method_name!r}, skipped')
            continue

        method_value = payment_method.value if payment_method else None
        if (payment_id, method_value) in existing_external_ids:
            report.transactions_skipped_duplicate += 1
            continue

        transaction = Transaction(
            user_id=telegram_to_new_id[tg_id],
            type=txn_type.value,
            amount_kopeks=stored_amount,
            description=f'[миграция из shopbot] {method_name}',
            payment_method=method_value,
            external_id=payment_id,
            is_completed=True,
            completed_at=created_at,
            created_at=created_at,
        )
        db.add(transaction)
        existing_external_ids.add((payment_id, method_value))
        report.transactions_created += 1
        report.transactions_by_method[method_name] = report.transactions_by_method.get(
            method_name, {'count': 0, 'amount_rub': 0.0}
        )
        report.transactions_by_method[method_name]['count'] += 1
        report.transactions_by_method[method_name]['amount_rub'] += amount_rub

    await db.flush()

    # ---- Pass 5: referral_reward_events -> ReferralEarning (stats only, no balance touch) --
    existing_reasons = {
        reason
        for (reason,) in (
            await db.execute(select(ReferralEarning.reason).where(ReferralEarning.reason.like('legacy_shopbot:%')))
        ).all()
    }

    for event in data['referral_events']:
        referrer_tg = event.get('referrer_id')
        referred_tg = event.get('referred_user_id')
        referrer_new_id = telegram_to_new_id.get(referrer_tg)
        referred_new_id = telegram_to_new_id.get(referred_tg)
        if referrer_tg not in newly_created_telegram_ids:
            # Owner already existed before this run -- their stats are theirs.
            continue
        if referrer_new_id is None or referred_new_id is None:
            report.referral_earnings_skipped_unresolved += 1
            continue

        reason = f'legacy_shopbot:{event.get("event_type")}:{event.get("event_id")}'
        if reason in existing_reasons:
            report.referral_earnings_skipped_duplicate += 1
            continue

        earning = ReferralEarning(
            user_id=referrer_new_id,
            referral_id=referred_new_id,
            amount_kopeks=round((event.get('reward_amount') or 0.0) * 100),
            reason=reason,
            reward_type=ReferralRewardType.MONEY.value,
            level=1,
            days_granted=0,
            created_at=_parse_sqlite_dt(event.get('created_at')) or datetime.now(UTC),
        )
        db.add(earning)
        existing_reasons.add(reason)
        report.referral_earnings_created += 1

    await db.flush()
    return report


def _print_report(report: MigrationReport) -> None:
    print()
    print('=' * 70)
    print('  DRY RUN — ничего не записано' if report.dry_run else '  APPLIED')
    print('=' * 70)
    print(f'  пользователей создано          : {report.users_created}')
    print(f'  пользователей пропущено (уже есть): {report.users_skipped_existing}')
    print(f'  рефереров не разрешено         : {report.users_referrer_unresolved}')
    print()
    print(f'  подписок создано               : {report.subscriptions_created}')
    print(f'  подписок пропущено (владелец уже есть): {report.subscriptions_skipped_owner_exists}')
    print(f'  подписок пропущено (истекла)   : {report.subscriptions_skipped_expired}')
    print(f'  подписок пропущено (нет панельной связи): {report.subscriptions_skipped_no_panel_link}')
    print(f'  подписок пропущено (дубль remnawave_id): {report.subscriptions_skipped_duplicate_remnawave_id}')
    print()
    print(f'  транзакций создано             : {report.transactions_created}')
    print(f'  транзакций пропущено (владелец уже есть): {report.transactions_skipped_owner_exists}')
    print(f'  транзакций пропущено (test)    : {report.transactions_skipped_test}')
    print(f'  транзакций пропущено (дубль)   : {report.transactions_skipped_duplicate}')
    if report.transactions_by_method:
        print('  по payment_method (создано в этом прогоне):')
        for method, stats in sorted(report.transactions_by_method.items()):
            print(f'    {method:<20} шт={stats["count"]:<6} сумма_руб={stats["amount_rub"]:.2f}')
    print()
    print(f'  реферальных начислений создано : {report.referral_earnings_created}')
    print(f'  реферальных начислений пропущено (не разрешено): {report.referral_earnings_skipped_unresolved}')
    print(f'  реферальных начислений пропущено (дубль): {report.referral_earnings_skipped_duplicate}')
    print()
    if report.unresolved_lines:
        print(f'  !! строк с замечаниями: {len(report.unresolved_lines)} (первые 20)')
        for line in report.unresolved_lines[:20]:
            print(f'     {line}')
    print('=' * 70)


def _write_audit(report: MigrationReport, *, committed: bool) -> str | None:
    directory = Path(os.environ.get('MIGRATION_AUDIT_DIR') or settings.LOG_DIR or 'logs')
    suffix = 'apply' if committed else 'dryrun'
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    path = directory / f'migrate_shopbot_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        payload['committed'] = committed
        payload['unresolved_lines'] = report.unresolved_lines
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        logger.warning('migrate_shopbot: не удалось записать отчёт', path=str(path), error=str(error))
        return None
    return str(path)


async def _run(args: argparse.Namespace, source_path: Path) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)
    logger.info(
        'migrate_shopbot: конфигурация загружена',
        multi_tariff=settings.is_multi_tariff_enabled(),
        sales_mode=settings.SALES_MODE,
    )
    if not settings.is_multi_tariff_enabled():
        print(
            '  !! ВНИМАНИЕ: MULTI_TARIFF_ENABLED/SALES_MODE выключены -- пользователи '
            'с несколькими ключами получат несколько Subscription-строк, но бот может '
            'отображать/управлять только одной. Включите мультитариф перед --apply.'
        )

    data = _load_source(source_path)
    print(
        f'  источник: {len(data["users"])} users, {len(data["vpn_keys"])} vpn_keys, '
        f'{len(data["transactions"])} transactions, {len(data["referral_events"])} referral_events'
    )

    async with AsyncSessionLocal() as db:
        report = await _migrate(
            db,
            data,
            apply=args.apply,
            include_expired=args.include_expired,
            include_test=args.include_test,
            limit=args.limit,
        )
        if args.apply:
            await db.commit()
        else:
            await db.rollback()

    _print_report(report)
    audit_path = _write_audit(report, committed=args.apply)
    if audit_path:
        print(f'  полный отчёт: {audit_path}')

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Migrate a legacy remnawave-shopbot SQLite DB into bedolaga')
    parser.add_argument('--source', required=True, help='path to the legacy users.db SQLite file')
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    parser.add_argument(
        '--include-expired', action='store_true', help='also import vpn_keys whose expire_at is already in the past'
    )
    parser.add_argument(
        '--include-test', action='store_true', help="also import payment_method='Test' transactions as DEPOSIT/manual"
    )
    parser.add_argument('--limit', type=int, default=None, help='only process the first N legacy users (smoke test)')
    args = parser.parse_args()

    source_path = Path(args.source).expanduser()
    if not source_path.exists():
        print(f'  !! файл не найден: {source_path}')
        return 2

    return asyncio.run(_run(args, source_path))


if __name__ == '__main__':
    raise SystemExit(main())
