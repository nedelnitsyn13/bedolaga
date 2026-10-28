#!/usr/bin/env python
"""Разово выписать чеки «Мой налог» по оплатам YooKassa, прошедшим без чека.

Чеки НПД бот создаёт сам после каждой оплаты YooKassa, но только пока
включён NaloGO. Оплаты до включения (или пока налоговая была недоступна дольше
очереди) остались без чека. Скрипт находит такие пополнения и регистрирует
доход с исходным временем оплаты. Клиентам чеки НЕ рассылаются и в админ-топик
не дублируются — это ретроспективная регистрация дохода.

Что считается оплатой без чека: транзакция ``deposit`` с ``payment_method =
yookassa``, завершённая, без ``receipt_uuid``, начиная с ``--since``.

Защита от дублей:
- ``create_receipt`` сам пропускает платёж, для которого чек уже создавался
  (кэш по id платежа YooKassa);
- перед созданием скрипт сверяется со списком доходов в «Мой налог» за тот же
  день: доход на ту же сумму в пределах ``--window`` минут, не привязанный ни к
  одной транзакции бота, считается уже выписанным вручную — его uuid
  привязывается к транзакции, новый чек не создаётся;
- если список доходов получить не удалось, скрипт останавливается: без сверки
  создавать чеки нельзя.

Usage:
    python -m scripts.nalogo_backfill_receipts --since 2026-09-01           # dry run
    python -m scripts.nalogo_backfill_receipts --since 2026-09-01 --apply   # создать чеки
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog
from dateutil.parser import isoparse
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Transaction, TransactionType, User
from app.services.nalogo_service import NaloGoService
from app.utils.timezone import get_local_timezone


logger = structlog.get_logger(__name__)


@dataclass
class Candidate:
    transaction: Transaction
    telegram_id: int | None
    paid_at: datetime


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def load_candidates(db, since: datetime, until: datetime) -> list[Candidate]:
    result = await db.execute(
        select(Transaction, User.telegram_id)
        .join(User, User.id == Transaction.user_id)
        .where(
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.payment_method == 'yookassa',
            Transaction.is_completed.is_(True),
            Transaction.receipt_uuid.is_(None),
            Transaction.amount_kopeks > 0,
            Transaction.created_at >= since,
            Transaction.created_at < until,
        )
        .order_by(Transaction.created_at)
    )
    return [
        Candidate(transaction=tx, telegram_id=tg_id, paid_at=_as_utc(tx.completed_at or tx.created_at))
        for tx, tg_id in result.all()
    ]


def find_existing_income(
    incomes: list[dict], amount_rub: float, paid_at: datetime, window: timedelta, taken: set[str]
) -> str | None:
    """Доход «Мой налог» на ту же сумму рядом по времени, ещё не привязанный к транзакции."""
    for income in incomes:
        uuid = income.get('approvedReceiptUuid') or income.get('receiptUuid')
        if not uuid or uuid in taken:
            continue
        if income.get('cancellationInfo'):
            continue
        if abs(float(income.get('totalAmount', income.get('amount', 0)) or 0) - amount_rub) > 0.01:
            continue
        raw_time = income.get('operationTime')
        if not raw_time:
            continue
        try:
            operation_time = _as_utc(isoparse(raw_time))
        except (TypeError, ValueError):
            continue
        if abs(operation_time - paid_at) <= window:
            return uuid
    return None


async def _incomes_for_day(nalogo, cache: dict[date, list[dict] | None], day: date) -> list[dict] | None:
    if day not in cache:
        cache[day] = await nalogo.get_incomes(from_date=day, to_date=day + timedelta(days=1), limit=100)
    return cache[day]


async def backfill(db, nalogo, since: date, until: date, *, apply: bool, window: timedelta, delay: float = 0.0) -> int:
    """Показать оплаты без чека и (с ``apply``) выписать по ним чеки. Код выхода: 0 — всё, 2 — были сбои."""
    candidates = await load_candidates(
        db,
        datetime.combine(since, datetime.min.time(), tzinfo=UTC),
        datetime.combine(until, datetime.min.time(), tzinfo=UTC) + timedelta(days=1),
    )
    if not candidates:
        print(f'Оплат YooKassa без чека с {since} по {until} нет.')
        return 0

    total = sum(c.transaction.amount_kopeks for c in candidates)
    print(f'Оплат без чека: {len(candidates)} на {settings.format_price(total)} (с {since} по {until})')
    for c in candidates:
        print(
            f'  #{c.transaction.id}  {c.paid_at.astimezone(get_local_timezone()):%d.%m.%Y %H:%M}  '
            f'{settings.format_price(c.transaction.amount_kopeks):>10}  tg={c.telegram_id or "—"}'
        )
    if not apply:
        print('\nЭто проверка (dry run). Чтобы выписать чеки, запустите с --apply.')
        return 0

    taken_result = await db.execute(select(Transaction.receipt_uuid).where(Transaction.receipt_uuid.is_not(None)))
    taken = {uuid for (uuid,) in taken_result.all()}
    incomes_cache: dict[date, list[dict] | None] = {}
    created = linked = failed = 0

    for c in candidates:
        tx = c.transaction
        amount_rub = tx.amount_kopeks / 100
        incomes = await _incomes_for_day(nalogo, incomes_cache, c.paid_at.date())
        if incomes is None:
            failed += 1
            print(f'Не удалось получить доходы за {c.paid_at.date()} из «Мой налог» — остановка, без сверки нельзя.')
            break

        receipt_uuid = find_existing_income(incomes, amount_rub, c.paid_at, window, taken)
        if receipt_uuid:
            linked += 1
            print(f'  #{tx.id}: доход уже есть в «Мой налог» — привязан, новый чек не создан')
        else:
            receipt_uuid = await nalogo.create_receipt(
                name=settings.get_balance_payment_description(tx.amount_kopeks, telegram_user_id=c.telegram_id),
                amount=amount_rub,
                quantity=1,
                payment_id=tx.external_id,
                queue_on_failure=False,
                telegram_user_id=c.telegram_id,
                amount_kopeks=tx.amount_kopeks,
                operation_time=c.paid_at,
            )
            if not receipt_uuid:
                failed += 1
                print(f'  #{tx.id}: чек не создан (подробности в логе выше)')
                continue
            created += 1
            print(f'  #{tx.id}: чек создан')

        taken.add(receipt_uuid)
        tx.receipt_uuid = receipt_uuid
        tx.receipt_created_at = datetime.now(UTC)
        await db.commit()
        if delay:
            await asyncio.sleep(delay)

    print(f'\nГотово: создано {created}, привязано к существующим {linked}, ошибок {failed}.')
    return 2 if failed else 0


async def _run(args: argparse.Namespace) -> int:
    nalogo = NaloGoService()
    if not nalogo.configured:
        print('NaloGO не настроен (NALOGO_ENABLED / NALOGO_INN) — нечего делать.')
        return 1
    if args.apply and not await nalogo.authenticate():
        print('Не удалось войти в «Мой налог» — проверьте токен/пароль. Ничего не создано.')
        return 1
    async with AsyncSessionLocal() as db:
        return await backfill(
            db,
            nalogo,
            args.since,
            args.until,
            apply=args.apply,
            window=timedelta(minutes=args.window),
            delay=args.delay,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Чеки «Мой налог» по оплатам YooKassa без чека (без рассылки клиентам)'
    )
    parser.add_argument('--since', type=date.fromisoformat, required=True, help='с какой даты, YYYY-MM-DD (UTC)')
    parser.add_argument('--until', type=date.fromisoformat, default=datetime.now(UTC).date(), help='по какую дату')
    parser.add_argument('--window', type=int, default=2, help='окно сверки с «Мой налог», минут (по умолчанию 2)')
    parser.add_argument('--delay', type=float, default=float(settings.NALOGO_QUEUE_RECEIPT_DELAY), help='пауза, сек')
    parser.add_argument('--apply', action='store_true', help='создать чеки (по умолчанию только показать список)')
    return asyncio.run(_run(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
