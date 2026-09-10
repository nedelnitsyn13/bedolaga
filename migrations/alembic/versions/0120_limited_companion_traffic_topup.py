"""subscriptions.limited_companion_* — докупленный и использованный трафик компаньона

Раньше квота компаньон-аккаунта была жёстко фиксирована LIMITED_COMPANION_TRAFFIC_GB
и нигде не отображалась пользователю отдельно от основного (безлимитного) ключа.
Эти колонки хранят докупленный сверх базовой квоты трафик (складывается с
LIMITED_COMPANION_TRAFFIC_GB при каждой отправке в панель, см.
SubscriptionService._sync_limited_companion_user) и последнее синхронизированное
значение использованного трафика (для отображения без лишнего похода в панель на
каждый рендер экрана подписки, тем же способом, что traffic_used_gb у основной).

Revision ID: 0120
Revises: 0119
"""

from alembic import op
import sqlalchemy as sa


revision = '0120'
down_revision = '0119'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('subscriptions') as batch:
        batch.add_column(
            sa.Column('limited_companion_purchased_traffic_gb', sa.Integer(), nullable=False, server_default='0')
        )
        batch.add_column(sa.Column('limited_companion_traffic_used_gb', sa.Float(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('subscriptions') as batch:
        batch.drop_column('limited_companion_traffic_used_gb')
        batch.drop_column('limited_companion_purchased_traffic_gb')
