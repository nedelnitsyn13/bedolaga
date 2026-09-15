"""Fix subscriptions.limited_squad_active default False + backfill

Revision ID: 0125
Revises: 0124
Create Date: 2026-09-15

0124 gave ``limited_squad_active`` a server_default of ``'true'`` — every
existing subscription got that value on the ALTER TABLE, including the ~300
subscriptions that have never touched the new LIMITED squad architecture
(their tariff still has ``limited_traffic_enabled=False``). This made them
indistinguishable, in ``limited_squad_monitoring_service._load_subscriptions_with_orphaned_limited_squad``
(``limited_squad_active=True AND tariff.limited_traffic_enabled=False``),
from subscriptions that really did get a LIMITED squad and then had it
disabled on their tariff — every regular subscription was queried and
iterated on every enforcement cycle for nothing (observed in production:
first cycle after deploy processed all 311 existing subscriptions).

``limited_squad_service.sync_limited_squad_state`` itself no longer depends
on this flag's prior value for its own decisions (it always reconciles the
panel state from computed usage, see the "reconcile, not delta" fix in the
same PR) — so flipping the default to False is safe for that function and
only fixes the orphaned-query's aim: false = "never touched the pool",
which the enforcement job then turns True on the first cycle that actually
adds the squad.
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0125'
down_revision: Union[str, None] = '0124'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('subscriptions', 'limited_squad_active', server_default='false')

    # Стандартный подзапрос, а не UPDATE ... FROM — работает и на Postgres, и
    # на SQLite (тесты), см. докстринг выше.
    op.execute(
        """
        UPDATE subscriptions
        SET limited_squad_active = false
        WHERE limited_squad_active = true
          AND (
            tariff_id IS NULL
            OR tariff_id IN (
                SELECT id FROM tariffs WHERE COALESCE(limited_traffic_enabled, false) = false
            )
          )
        """
    )


def downgrade() -> None:
    op.alter_column('subscriptions', 'limited_squad_active', server_default='true')
