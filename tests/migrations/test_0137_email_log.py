"""Миграция 0137: журнал писем и тип письма в очереди повторов совпадают с моделями."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import EmailLogEntry, User
from tests.fixtures.postgres_db import postgres_engine


MIGRATION = Path('migrations/alembic/versions/0137_create_email_log.py')


def _migration():
    spec = importlib.util.spec_from_file_location('m0137', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0137', '0136')


def _run(connection, step: str) -> dict:
    module = _migration()
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(module, step)()
    inspector = sa.inspect(connection)
    log_columns = (
        {column['name'] for column in inspector.get_columns('email_log')}
        if 'email_log' in inspector.get_table_names()
        else set()
    )
    queue_columns = {column['name'] for column in inspector.get_columns('email_queue')}
    return {'log': log_columns, 'queue_has_type': 'email_type' in queue_columns}


def _restore_model_tables(connection) -> None:
    EmailLogEntry.__table__.create(connection, checkfirst=True)
    if 'email_type' not in {c['name'] for c in sa.inspect(connection).get_columns('email_queue')}:
        connection.execute(sa.text('ALTER TABLE email_queue ADD COLUMN email_type VARCHAR(64)'))


@pytest.mark.postgres
async def test_upgrade_matches_model_and_downgrade_drops(postgres_database):
    # Схема тестовой базы создана по моделям — миграцию сначала откатываем, иначе она
    # увидит готовую таблицу и ничего не проверит; в конце возвращаем для остальных тестов.
    async with postgres_engine(postgres_database, [User.__table__]) as engine:
        async with engine.begin() as connection:
            try:
                await connection.run_sync(_run, 'downgrade')
                created = await connection.run_sync(_run, 'upgrade')
                again = await connection.run_sync(_run, 'upgrade')  # повтор не падает
                dropped = await connection.run_sync(_run, 'downgrade')
            finally:
                await connection.run_sync(_restore_model_tables)
    assert created['log'] == {column.name for column in EmailLogEntry.__table__.columns}
    assert created['queue_has_type'] is True
    assert again == created
    assert dropped == {'log': set(), 'queue_has_type': False}
