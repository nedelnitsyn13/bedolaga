"""Миграция 0145: запись CisPay включается, только если CisPay настроен в .env."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.config import settings


MIGRATION = Path('migrations/alembic/versions/0145_enable_configured_cispay_method.py')


def _migration():
    spec = importlib.util.spec_from_file_location('m0145', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0145', '0144')


def _run(monkeypatch, *, configured: bool) -> dict[str, bool]:
    monkeypatch.setattr(type(settings), 'is_cispay_enabled', lambda self: configured)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE payment_method_configs (method_id TEXT, is_enabled BOOLEAN)'))
        connection.execute(
            sa.text("INSERT INTO payment_method_configs VALUES ('cispay', 0), ('yookassa', 0), ('anore', 1)")
        )
        with Operations.context(MigrationContext.configure(connection)):
            _migration().upgrade()
        rows = connection.execute(sa.text('SELECT method_id, is_enabled FROM payment_method_configs')).all()
    return {method_id: bool(enabled) for method_id, enabled in rows}


def test_configured_cispay_row_is_enabled_and_others_untouched(monkeypatch):
    assert _run(monkeypatch, configured=True) == {'cispay': True, 'yookassa': False, 'anore': True}


def test_unconfigured_cispay_row_is_left_alone(monkeypatch):
    assert _run(monkeypatch, configured=False) == {'cispay': False, 'yookassa': False, 'anore': True}
