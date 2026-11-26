"""выручка напоминаний и встроенное «брошенная оплата»

Revision ID: 0152
Revises: 0151
Create Date: 2026-11-24

Каждая успешная отправка напоминания в бот теперь записывается отдельной строкой:
по ним кабинет считает, сколько денег принесло каждое напоминание. Прошлые
отправки известны только последней на человека (``last_success_at``) — переносим
её, чтобы выручка была видна сразу, а не через неделю.

Встроенное «Брошенная оплата»: создал платёж и не оплатил за час. Поставляется
ВЫКЛЮЧЕННЫМ, как и прочие встроенные: текст, задержку и кнопку админ правит в
разделе «Напоминания» и включает сам.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0152'
down_revision: Union[str, None] = '0151'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BUILTIN_ABANDONED_PAYMENT = {
    'name': 'Брошенная оплата',
    'is_active': False,
    'builtin_key': 'abandoned_payment',
    'channels': 'bot',
    'category': 'service',
    # Час — чтобы не писать тому, кто ещё вводит данные карты или ждёт перевода.
    'conditions': {'abandoned_payment_hours': 1},
    # Один раз на каждый брошенный платёж — это проверяет само условие.
    'repeat_every_days': 1,
    'max_sends': 1,
    'button_kind': 'cabinet',
    'button_target': '/balance',
    'texts': {
        'ru': {
            'title': 'Оплата не завершилась',
            'body': (
                'Похоже, оплата не прошла до конца. Попробуйте ещё раз или выберите другой способ — '
                'это займёт минуту. Если деньги списались, а баланс не пополнился, напишите в поддержку.'
            ),
            'button': 'Пополнить баланс',
        },
        'en': {
            'title': 'Your payment was not completed',
            'body': (
                "It looks like the payment didn't go through. Try again or choose another method — "
                'it takes a minute. If you were charged but your balance was not topped up, contact support.'
            ),
            'button': 'Top up balance',
        },
        'ua': {
            'title': 'Оплата не завершилася',
            'body': (
                'Схоже, оплата не пройшла до кінця. Спробуйте ще раз або оберіть інший спосіб — '
                'це займе хвилину. Якщо гроші списалися, а баланс не поповнився, напишіть у підтримку.'
            ),
            'button': 'Поповнити баланс',
        },
        'zh': {
            'title': '付款未完成',
            'body': '付款似乎没有完成。请重试或选择其他付款方式，只需一分钟。如果已扣款但余额未到账，请联系客服。',
            'button': '充值余额',
        },
        'fa': {
            'title': 'پرداخت کامل نشد',
            'body': (
                'به نظر می‌رسد پرداخت تا انتها انجام نشده است. دوباره امتحان کنید یا روش دیگری را انتخاب کنید — '
                'یک دقیقه طول می‌کشد. اگر مبلغ کسر شد ولی موجودی شارژ نشد، به پشتیبانی پیام دهید.'
            ),
            'button': 'شارژ موجودی',
        },
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'user_reminder_sends' not in tables:
        op.create_table(
            'user_reminder_sends',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'reminder_id', sa.Integer(), sa.ForeignKey('user_reminders.id', ondelete='CASCADE'), nullable=False
            ),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index('ix_user_reminder_sends_user_sent', 'user_reminder_sends', ['user_id', 'sent_at'])
        op.create_index('ix_user_reminder_sends_reminder_sent', 'user_reminder_sends', ['reminder_id', 'sent_at'])
        if 'user_reminder_states' in tables:
            op.execute(
                sa.text(
                    'INSERT INTO user_reminder_sends (reminder_id, user_id, sent_at) '
                    'SELECT reminder_id, user_id, last_success_at FROM user_reminder_states '
                    'WHERE last_success_at IS NOT NULL'
                )
            )

    if 'user_reminders' not in tables:
        return
    exists = bind.execute(
        sa.text('SELECT 1 FROM user_reminders WHERE builtin_key = :key'),
        {'key': BUILTIN_ABANDONED_PAYMENT['builtin_key']},
    ).first()
    if exists is not None:
        return
    # JSON-колонки объявлены с типом: иначе bulk_insert не сериализует dict.
    table = sa.table(
        'user_reminders',
        sa.column('name', sa.String()),
        sa.column('is_active', sa.Boolean()),
        sa.column('builtin_key', sa.String()),
        sa.column('channels', sa.String()),
        sa.column('category', sa.String()),
        sa.column('conditions', sa.JSON()),
        sa.column('repeat_every_days', sa.Integer()),
        sa.column('max_sends', sa.Integer()),
        sa.column('button_kind', sa.String()),
        sa.column('button_target', sa.String()),
        sa.column('texts', sa.JSON()),
    )
    op.bulk_insert(table, [BUILTIN_ABANDONED_PAYMENT])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM user_reminders WHERE builtin_key = 'abandoned_payment'"))
    op.drop_index('ix_user_reminder_sends_reminder_sent', table_name='user_reminder_sends')
    op.drop_index('ix_user_reminder_sends_user_sent', table_name='user_reminder_sends')
    op.drop_table('user_reminder_sends')
