"""Миграция 0139: колонки платного обновления LIMITED-пула совпадают с моделью."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.database.models import Subscription


MIGRATION = Path('migrations/alembic/versions/0139_limited_traffic_reset.py')
NEW_COLUMNS = {'limited_traffic_reset_at', 'limited_traffic_reset_offset_bytes'}


def _migration():
    spec = importlib.util.spec_from_file_location('m0139', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0139', '0138')


def test_model_has_all_new_columns():
    assert NEW_COLUMNS <= {column.name for column in Subscription.__table__.columns}
