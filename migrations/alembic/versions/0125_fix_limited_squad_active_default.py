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

Two things this migration is deliberately careful about (both caught in
review, see PR #31):

* SQLite is a real supported ``DATABASE_MODE`` for this bot (see
  ``app/config.py``/``app/database/database.py``), not just a test-only
  backend — SQLite has no ``ALTER COLUMN ... SET DEFAULT``, so the default
  change goes through ``batch_alter_table`` like every other SQLite-targeting
  migration in this repo (e.g. 0119, 0122).
* The backfill only resets subscriptions whose tariff has BOTH
  ``limited_traffic_enabled=False`` AND an empty/null ``limited_squad_uuids``
  — i.e. a tariff that has never had the new architecture configured at all.
  A tariff that genuinely had squads configured and was later disabled keeps
  its ``limited_squad_uuids`` (disabling only flips the boolean — see the
  admin toggle in ``app/handlers/admin/tariff_limited_squad.py``); resetting
  those subscriptions' flag too would erase the exact marker
  ``deactivate_orphaned_limited_squad`` needs to find and actually remove the
  squad from the panel, leaving it stuck there. Done via plain
  SQLAlchemy Core select/update in Python rather than a single raw SQL
  UPDATE — comparing a JSON column for "empty" is dialect-specific enough
  (bare JSON has no ``=`` operator on Postgres, hence the JSON/JSONB
  comparison error seen while manually verifying this migration against a
  full chain) that doing it in Python is the more robust path across both
  supported backends, and the row counts here (tariffs, subscriptions) are
  small enough that a one-time Python-side pass costs nothing.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0125'
down_revision: Union[str, None] = '0124'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscriptions') as batch:
        batch.alter_column('limited_squad_active', existing_type=sa.Boolean(), server_default=sa.false())

    bind = op.get_bind()
    tariffs = sa.table(
        'tariffs',
        sa.column('id', sa.Integer),
        sa.column('limited_traffic_enabled', sa.Boolean),
        sa.column('limited_squad_uuids', sa.JSON),
    )
    subscriptions = sa.table(
        'subscriptions',
        sa.column('id', sa.Integer),
        sa.column('tariff_id', sa.Integer),
        sa.column('limited_squad_active', sa.Boolean),
    )

    never_configured_tariff_ids = {
        row.id
        for row in bind.execute(
            sa.select(tariffs.c.id, tariffs.c.limited_traffic_enabled, tariffs.c.limited_squad_uuids)
        )
        if not row.limited_traffic_enabled and not (row.limited_squad_uuids or [])
    }

    to_reset = [
        row.id
        for row in bind.execute(
            sa.select(subscriptions.c.id, subscriptions.c.tariff_id).where(
                subscriptions.c.limited_squad_active.is_(True)
            )
        )
        if row.tariff_id is None or row.tariff_id in never_configured_tariff_ids
    ]

    if to_reset:
        bind.execute(subscriptions.update().where(subscriptions.c.id.in_(to_reset)).values(limited_squad_active=False))


def downgrade() -> None:
    with op.batch_alter_table('subscriptions') as batch:
        batch.alter_column('limited_squad_active', existing_type=sa.Boolean(), server_default=sa.true())
