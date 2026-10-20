"""Миграция 0138: источник тикета и оценка поддержки совпадают с моделью."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Ticket, User
from tests.fixtures.postgres_db import postgres_engine


MIGRATION = Path('migrations/alembic/versions/0138_ticket_source_and_csat.py')
NEW_COLUMNS = {'source', 'csat_rating', 'csat_requested_at', 'csat_rated_at'}


def _migration():
    spec = importlib.util.spec_from_file_location('m0138', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0138', '0137')


def test_model_has_all_new_columns():
    assert NEW_COLUMNS <= {column.name for column in Ticket.__table__.columns}


def _run(connection, step: str) -> set[str]:
    module = _migration()
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(module, step)()
    return {column['name'] for column in sa.inspect(connection).get_columns('tickets')} & NEW_COLUMNS


@pytest.mark.postgres
async def test_upgrade_adds_columns_and_downgrade_drops(postgres_database):
    # Схема создана по моделям — сначала откатываем, в конце возвращаем колонки.
    async with postgres_engine(postgres_database, [User.__table__, Ticket.__table__]) as engine:
        async with engine.begin() as connection:
            try:
                await connection.run_sync(_run, 'downgrade')
                created = await connection.run_sync(_run, 'upgrade')
                again = await connection.run_sync(_run, 'upgrade')  # повтор не падает
                dropped = await connection.run_sync(_run, 'downgrade')
            finally:
                await connection.run_sync(_run, 'upgrade')
    assert created == again == NEW_COLUMNS
    assert dropped == set()
