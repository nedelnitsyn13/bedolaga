"""tickets: source and CSAT rating

Revision ID: 0138
Revises: 0137
Create Date: 2026-10-20

Где начат тикет (бот или кабинет) и оценка работы поддержки после закрытия.
Опрос показывается только там, где тикет начат; у старых тикетов source пустой.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0138'
down_revision: Union[str, None] = '0137'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('source', sa.String(16)),
    ('csat_rating', sa.SmallInteger()),
    ('csat_requested_at', sa.DateTime(timezone=True)),
    ('csat_rated_at', sa.DateTime(timezone=True)),
)


def _ticket_columns() -> set[str]:
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns('tickets')}


def upgrade() -> None:
    existing = _ticket_columns()
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column('tickets', sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    existing = _ticket_columns()
    for name, _ in reversed(_COLUMNS):
        if name in existing:
            op.drop_column('tickets', name)
