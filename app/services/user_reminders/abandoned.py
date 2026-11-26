"""Условие напоминаний «брошенная оплата»: создал платёж и не оплатил.

Платежи лежат в своей таблице у каждой платёжки, но у всех есть ``user_id``,
``created_at`` и признак оплаты (у CryptoBot и Heleket — статус). Брошенным считаем
платёж, который:

* создан не раньше ``LOOKBACK`` назад и не позже ``hours`` назад — человек успел
  бы оплатить, а старые счета уже никого не вернут;
* не оплачен;
* создан самим человеком — автоплатежи бота и покупки гостей с лендинга не в счёт;
* после него не было ни одного успешного пополнения — оплатил другим способом,
  значит, всё в порядке.

Напоминание приходит один раз на каждый брошенный платёж: следующий брошенный
платёж (через неделю, через месяц) снова его «взводит». Для этого условие
принимает момент, после которого платёж должен быть создан, — время последней
отправки (бот) или закрытия карточки (кабинет).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, exists, false, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import models as m
from app.database.models import Transaction, TransactionType, User, UserReminderState


#: Счета старше недели уже не вернут человека — о них не напоминаем.
LOOKBACK = timedelta(days=7)

#: Платежи, которые создал не человек: автоплатёж бота, покупка гостя с лендинга.
_SYSTEM_PURPOSES = ('recurrent_topup', 'guest_purchase')

#: Пополнения не от денег клиента: ручное начисление и списание с баланса.
_NOT_REAL_MONEY = ('manual', 'balance')

_IS_PAID_MODELS = (
    m.AnorePayment,
    m.AntilopayPayment,
    m.AuraPayPayment,
    m.CasheraPayment,
    m.CisPayPayment,
    m.CloudPaymentsPayment,
    m.DonutPayment,
    m.EtoplatezhiPayment,
    m.FreekassaPayment,
    m.JupiterPayment,
    m.KassaAiPayment,
    m.LavaPayment,
    m.MulenPayPayment,
    m.Pal24Payment,
    m.ParityPayPayment,
    m.PayHotPayment,
    m.PlategaPayment,
    m.RioPayPayment,
    m.RollyPayPayment,
    m.SeverPayPayment,
    m.TabPayPayment,
    m.WataPayment,
    m.YooKassaPayment,
)
# Как в payment_verification_service: у этих двух нет is_paid, оплату видно по статусу.
_PAID_STATUSES = {
    m.CryptoBotPayment: ('paid',),
    m.HeleketPayment: ('paid', 'paid_over'),
}

PAYMENT_MODELS = (*_IS_PAID_MODELS, *_PAID_STATUSES)


def _unpaid(model) -> ColumnElement[bool]:
    if model in _PAID_STATUSES:
        return or_(model.status.is_(None), model.status.not_in(_PAID_STATUSES[model]))
    return or_(model.is_paid.is_(None), model.is_paid.is_(False))


def _created_by_user(model) -> ColumnElement[bool]:
    metadata = getattr(model, 'metadata_json', None)
    if metadata is None:
        return true()
    purpose = metadata['purpose'].as_string()
    return or_(metadata.is_(None), purpose.is_(None), purpose.not_in(_SYSTEM_PURPOSES))


def _not_test(model) -> ColumnElement[bool]:
    for flag in ('is_test', 'is_sandbox'):
        column = getattr(model, flag, None)
        if column is not None:
            return or_(column.is_(None), column.is_(False))
    return true()


def _paid_later(model) -> ColumnElement[bool]:
    """После этого платежа было настоящее пополнение — значит, оплатил."""
    return exists().where(
        Transaction.user_id == model.user_id,
        Transaction.type == TransactionType.DEPOSIT.value,
        Transaction.is_completed.is_(True),
        Transaction.payment_method.is_not(None),
        Transaction.payment_method.not_in(_NOT_REAL_MONEY),
        Transaction.created_at >= model.created_at,
    )


def _abandoned_in(
    model,
    *,
    hours: int,
    now: datetime,
    user_id,
    reminder_id: int | None,
    created_after: datetime | None,
) -> ColumnElement[bool]:
    clauses = [
        model.user_id == user_id,
        model.created_at <= now - timedelta(hours=hours),
        model.created_at >= now - LOOKBACK,
        _unpaid(model),
        _created_by_user(model),
        _not_test(model),
        not_(_paid_later(model)),
    ]
    if reminder_id is not None:
        # Об этом платеже уже напоминали: отправка была после его создания.
        clauses.append(
            not_(
                exists().where(
                    UserReminderState.reminder_id == reminder_id,
                    UserReminderState.user_id == model.user_id,
                    UserReminderState.last_sent_at.is_not(None),
                    UserReminderState.last_sent_at >= model.created_at,
                )
            )
        )
    if created_after is not None:
        clauses.append(model.created_at > created_after)
    return exists().where(*clauses)


def abandoned_payment_sql(
    hours: int,
    *,
    now: datetime,
    reminder_id: int | None = None,
    user_id=None,
    created_after: datetime | None = None,
) -> ColumnElement[bool]:
    """У человека есть брошенный платёж хоть в одной платёжке.

    ``reminder_id`` — не считать платежи, о которых это напоминание уже писало.
    ``created_after`` — только платежи новее этого момента (закрытая карточка).
    """
    target = User.id if user_id is None else user_id
    parts = [
        _abandoned_in(model, hours=hours, now=now, user_id=target, reminder_id=reminder_id, created_after=created_after)
        for model in PAYMENT_MODELS
    ]
    return or_(*parts) if parts else false()


async def has_abandoned_payment(
    db: AsyncSession, user_id: int, hours: int, *, now: datetime, created_after: datetime | None = None
) -> bool:
    """Проверка одного человека — для карточки в кабинете."""
    query = select(abandoned_payment_sql(hours, now=now, user_id=user_id, created_after=created_after))
    return bool((await db.execute(query)).scalar())
