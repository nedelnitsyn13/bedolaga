"""subscriptions: limited traffic paid reset point

Revision ID: 0139
Revises: 0138
Create Date: 2026-10-25

LIMITED-пул теперь сбрасывается 1-го числа каждого месяца, а клиент может
заплатить за «обновить трафик сейчас». Момент обновления и расход за этот
день до него хранятся на подписке: панель отдаёт расход только по датам.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0139'
down_revision: Union[str, None] = '0138'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns('subscriptions')}


def upgrade() -> None:
    existing = _columns()
    if 'limited_traffic_reset_at' not in existing:
        op.add_column('subscriptions', sa.Column('limited_traffic_reset_at', sa.DateTime(timezone=True), nullable=True))
    if 'limited_traffic_reset_offset_bytes' not in existing:
        op.add_column(
            'subscriptions',
            sa.Column('limited_traffic_reset_offset_bytes', sa.BigInteger(), server_default='0', nullable=False),
        )


def downgrade() -> None:
    existing = _columns()
    for name in ('limited_traffic_reset_offset_bytes', 'limited_traffic_reset_at'):
        if name in existing:
            op.drop_column('subscriptions', name)
