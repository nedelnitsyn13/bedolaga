"""LIMITED squad на основном Remnawave user — параллельно limited_companion_*

Revision ID: 0124
Revises: 0123
Create Date: 2026-09-15

Новая архитектура лимитного трафика: вместо отдельного компаньон-пользователя
LIMITED squad подключается/отключается на activeInternalSquads ОСНОВНОГО
Remnawave user. Эта миграция только добавляет колонки — никакой существующей
логики (limited_companion_*) не трогает и не заменяет; обе схемы работают
параллельно до отдельно согласованного этапа очистки.

tariffs:
    limited_traffic_enabled  — включена ли LIMITED-механика на тарифе
    limited_squad_uuids      — список UUID squad'ов LIMITED-пула (общий лимит
                                на все ноды пула, не по 1 лимиту на ноду)
    limited_base_traffic_gb  — базовый лимит пула в ГБ, без докупок

subscriptions:
    limited_traffic_used_gb  — последнее синхронизированное значение
                                использованного трафика LIMITED-пула
    limited_squad_active     — включён ли сейчас LIMITED squad у юзера
                                (чтобы enforcement-джоба не слала лишний PATCH,
                                если состояние не изменилось)

Докупки LIMITED-пула переиспользуют существующую таблицу
limited_companion_traffic_purchases (LimitedCompanionTrafficPurchase) как
есть — у неё нет схемной привязки к компаньон-аккаунту, только
subscription_id/traffic_gb/expires_at.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0124'
down_revision: Union[str, None] = '0123'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('tariffs', 'limited_traffic_enabled'):
        op.add_column(
            'tariffs',
            sa.Column('limited_traffic_enabled', sa.Boolean(), server_default='false', nullable=False),
        )
    if not _has_column('tariffs', 'limited_squad_uuids'):
        op.add_column(
            'tariffs',
            sa.Column('limited_squad_uuids', sa.JSON(), server_default='[]', nullable=True),
        )
    if not _has_column('tariffs', 'limited_base_traffic_gb'):
        op.add_column(
            'tariffs',
            sa.Column('limited_base_traffic_gb', sa.Integer(), server_default='0', nullable=False),
        )

    if not _has_column('subscriptions', 'limited_traffic_used_gb'):
        op.add_column(
            'subscriptions',
            sa.Column('limited_traffic_used_gb', sa.Float(), server_default='0', nullable=True),
        )
    if not _has_column('subscriptions', 'limited_squad_active'):
        op.add_column(
            'subscriptions',
            sa.Column('limited_squad_active', sa.Boolean(), server_default='true', nullable=False),
        )


def downgrade() -> None:
    if _has_column('subscriptions', 'limited_squad_active'):
        op.drop_column('subscriptions', 'limited_squad_active')
    if _has_column('subscriptions', 'limited_traffic_used_gb'):
        op.drop_column('subscriptions', 'limited_traffic_used_gb')

    if _has_column('tariffs', 'limited_base_traffic_gb'):
        op.drop_column('tariffs', 'limited_base_traffic_gb')
    if _has_column('tariffs', 'limited_squad_uuids'):
        op.drop_column('tariffs', 'limited_squad_uuids')
    if _has_column('tariffs', 'limited_traffic_enabled'):
        op.drop_column('tariffs', 'limited_traffic_enabled')
