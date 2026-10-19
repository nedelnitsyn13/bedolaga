"""create email_log table, add email_queue.email_type

Revision ID: 0137
Revises: 0136
Create Date: 2026-10-19

Журнал писем для админки: кому, тип, тема, результат. Текст письма не хранится —
в нём коды подтверждения и ссылки входа.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0137'
down_revision: Union[str, None] = '0136'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _queue_columns() -> set[str]:
    return {column['name'] for column in _inspector().get_columns('email_queue')}


def upgrade() -> None:
    if 'email_log' not in set(_inspector().get_table_names()):
        op.create_table(
            'email_log',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('to_email', sa.String(320), nullable=False),
            sa.Column('email_type', sa.String(64), nullable=False, server_default='other'),
            sa.Column('subject', sa.Text(), nullable=False),
            sa.Column('status', sa.String(16), nullable=False),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index('ix_email_log_id', 'email_log', ['id'])
        op.create_index('ix_email_log_user_id', 'email_log', ['user_id'])
        op.create_index('ix_email_log_created_at', 'email_log', ['created_at'])
        op.create_index('ix_email_log_to_email_created_at', 'email_log', ['to_email', 'created_at'])

    if 'email_type' not in _queue_columns():
        op.add_column('email_queue', sa.Column('email_type', sa.String(64), nullable=True))


def downgrade() -> None:
    if 'email_type' in _queue_columns():
        op.drop_column('email_queue', 'email_type')
    if 'email_log' in set(_inspector().get_table_names()):
        op.drop_table('email_log')
