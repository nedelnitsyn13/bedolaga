"""limited_companion_traffic_purchases — докупки трафика компаньона со сроком жизни

Раньше subscriptions.limited_companion_purchased_traffic_gb был просто
аккумулятором — докупленные ГБ копились навсегда, никогда не сгорая, хотя
использованный трафик компаньона сбрасывается панелью каждые 30 дней
(TrafficLimitStrategy.MONTH). Эта таблица зеркалит уже существующий паттерн
traffic_purchases (докупка основного трафика с индивидуальным expires_at):
каждая докупка — отдельная запись с датой истечения (покупка + 30 дней),
subscriptions.limited_companion_purchased_traffic_gb пересчитывается как
сумма ещё не истёкших записей.

Отдельная таблица, а не переиспользование traffic_purchases — та завязана на
добрый десяток мест (продление, смена тарифа, pricing engine, admin-панель,
суточные тарифы, бэкапы), которые молча подразумевают, что каждая строка —
докупка ОСНОВНОГО трафика; подмешивать туда докупки компаньона означало бы
аудировать и переправлять каждое из этих мест.

Revision ID: 0121
Revises: 0120
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0121'
down_revision: Union[str, None] = '0120'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'limited_companion_traffic_purchases',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('traffic_gb', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_limited_companion_traffic_purchases_id', 'limited_companion_traffic_purchases', ['id'])
    op.create_index(
        'ix_limited_companion_traffic_purchases_expires_at', 'limited_companion_traffic_purchases', ['expires_at']
    )
    op.create_index(
        'ix_limited_companion_traffic_purchases_created_at', 'limited_companion_traffic_purchases', ['created_at']
    )
    op.create_index(
        'ix_limited_companion_traffic_purchases_sub_expires',
        'limited_companion_traffic_purchases',
        ['subscription_id', 'expires_at'],
    )


def downgrade() -> None:
    op.drop_table('limited_companion_traffic_purchases')
