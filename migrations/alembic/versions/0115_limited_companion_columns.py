"""subscriptions.limited_companion_* — привязка к компаньон-аккаунту лимитного сервера

Компаньон-аккаунт (LIMITED_COMPANION_ENABLED) — второй панельный пользователь
с фиксированной квотой трафика на отдельном сквадe, чья подписка склеивается
с основной сервисом subscription-merger на стороне панели. Колонки хранят
панельный id и shortUuid этого второго аккаунта, чтобы бот мог найти и
обновить/отключить его вместе с основным без повторного поиска по панели.

Revision ID: 0115
Revises: 0114
"""

from alembic import op
import sqlalchemy as sa


revision = '0115'
down_revision = '0114'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('subscriptions') as batch:
        batch.add_column(sa.Column('limited_companion_remnawave_id', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('limited_companion_short_uuid', sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('subscriptions') as batch:
        batch.drop_column('limited_companion_short_uuid')
        batch.drop_column('limited_companion_remnawave_id')
