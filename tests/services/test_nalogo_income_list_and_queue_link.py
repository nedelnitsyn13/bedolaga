"""«Мой налог»: запрос списка доходов и привязка чека из очереди к транзакции.

Список доходов уходил с голыми датами и sortBy=OPERATION_TIME — налоговая его
отклоняла, и сверка дублей (очередь, выписка задним числом) не работала. Чек,
созданный очередью, не записывался в транзакцию — оплата выглядела «без чека».
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import PromoGroup, Transaction, User, UserPromoGroup
from app.lib.nalogo.income import IncomeAPI
from app.services.nalogo_queue_service import NalogoQueueService
from tests.fixtures.sqlite_memory import memory_session


class _Http:
    def __init__(self):
        self.paths: list[str] = []

    async def get(self, path):
        self.paths.append(path)
        return SimpleNamespace(json=lambda: {'content': [], 'hasMore': False})


async def test_income_list_uses_rfc3339_and_operation_time_sort() -> None:
    api = IncomeAPI.__new__(IncomeAPI)
    api.http = _Http()

    await api.get_list(from_date=date(2026, 9, 1), to_date=date(2026, 9, 2), limit=500)

    query = parse_qs(urlparse(api.http.paths[0]).query)
    assert query['from'] == ['2026-09-01T00:00:00.000+00:00']
    assert query['to'] == ['2026-09-02T00:00:00.000+00:00']
    assert query['sortBy'] == ['operation_time:desc']
    assert query['limit'] == ['100']
    assert 'sortOrder' not in query


async def test_queue_receipt_is_written_to_the_payment_transaction(monkeypatch) -> None:
    async with memory_session(
        monkeypatch, [User.__table__, Transaction.__table__, PromoGroup.__table__, UserPromoGroup.__table__]
    ) as db:
        user = User(telegram_id=7, first_name='Т', language='ru', balance_kopeks=0)
        db.add(user)
        await db.commit()
        db.add(
            Transaction(
                user_id=user.id,
                type='deposit',
                amount_kopeks=20000,
                payment_method='yookassa',
                external_id='yk-queued',
                is_completed=True,
                created_at=datetime(2026, 10, 28, tzinfo=UTC),
            )
        )
        await db.commit()
        monkeypatch.setattr(
            'app.database.database.AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False)
        )

        await NalogoQueueService._link_receipt_to_transaction('yk-queued', 'queue-uuid')

        db.expire_all()
        tx = (await db.execute(select(Transaction).where(Transaction.external_id == 'yk-queued'))).scalar_one()
        assert tx.receipt_uuid == 'queue-uuid'
        assert tx.receipt_created_at is not None
