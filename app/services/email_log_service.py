"""Журнал писем: кому, какого типа, с какой темой и чем закончилась отправка.

Раньше узнать, ушло ли письмо человеку, можно было только по логам бота —
там один адрес без темы и только до пересоздания контейнера. Журнал хранит
строку на каждую попытку отправки и показывается в карточке пользователя.

Текст письма в журнал не попадает никогда: в нём коды подтверждения и ссылки
входа.

``send_email`` синхронный и выполняется в рабочем потоке, поэтому запись идёт
так же, как в ``email_retry_service``: через ``call_soon_threadsafe`` в
``asyncio.Queue``, а в базу её переносит фоновый писатель пачками.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, insert, select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import EmailLogEntry, User


logger = structlog.get_logger(__name__)

STATUS_SENT = 'sent'
STATUS_QUEUED = 'queued'
STATUS_FAILED = 'failed'

DEFAULT_EMAIL_TYPE = 'other'

QUEUE_MAX = 5000
BATCH_SIZE = 200
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

_SUBJECT_LIMIT = 500
_ERROR_LIMIT = 500


class EmailLogService:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._writer: asyncio.Task | None = None
        self._cleanup: asyncio.Task | None = None
        self._dropped = 0

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._writer and not self._writer.done():
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._writer = asyncio.create_task(self._run_writer())
        self._cleanup = asyncio.create_task(self._run_cleanup())
        logger.info('Журнал писем запущен', retention_days=settings.get_email_log_retention_days())

    async def stop(self, drain_timeout: float = 5.0) -> None:
        """Дописать принятые записи и остановить фоновые задачи."""
        queue = self._queue
        if queue is not None and self._writer and not self._writer.done():
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(queue.join(), timeout=drain_timeout)

        for task in (self._writer, self._cleanup):
            if task and not task.done():
                task.cancel()
                await asyncio.wait([task])
        self._writer = None
        self._cleanup = None

    # ------------------------------------------------------------------
    # Публичный API — вызывается из синхронного send_email в чужом потоке
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        to_email: str,
        subject: str,
        status: str,
        email_type: str | None = None,
        error: str | None = None,
    ) -> None:
        """Записать попытку отправки. Никогда не бросает: журнал не должен ронять почту."""
        loop = self._loop
        if self._queue is None or loop is None:
            return

        entry = {
            'to_email': (to_email or '').strip().lower()[:320],
            'subject': (subject or '')[:_SUBJECT_LIMIT],
            'status': status,
            'email_type': (email_type or DEFAULT_EMAIL_TYPE)[:64],
            'error': error[:_ERROR_LIMIT] if error else None,
            'created_at': datetime.now(tz=UTC),
        }

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        try:
            if running_loop is loop:
                self._put_nowait(entry)
            else:
                loop.call_soon_threadsafe(self._put_nowait, entry)
        except RuntimeError:
            # Цикл уже закрыт (остановка бота) — запись теряется, письмо нет.
            pass

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _put_nowait(self, entry: dict[str, Any]) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning('Журнал писем переполнен, записи теряются', dropped=self._dropped)

    async def _run_writer(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            batch = [await queue.get()]
            while len(batch) < BATCH_SIZE:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._write_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning('Не удалось записать журнал писем', error=str(e)[:200], count=len(batch))
            finally:
                for _ in batch:
                    queue.task_done()

    async def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        addresses = {entry['to_email'] for entry in batch if entry['to_email']}
        async with AsyncSessionLocal() as session:
            owners: dict[str, int] = {}
            if addresses:
                rows = await session.execute(
                    select(func.lower(User.email), User.id).where(func.lower(User.email).in_(addresses))
                )
                owners = {email: user_id for email, user_id in rows.all()}
            await session.execute(
                insert(EmailLogEntry),
                [{**entry, 'user_id': owners.get(entry['to_email'])} for entry in batch],
            )
            await session.commit()

    async def _run_cleanup(self) -> None:
        while True:
            try:
                await self.cleanup_once()
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning('Сбой очистки журнала писем', error=str(e)[:200])
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

    async def cleanup_once(self) -> int:
        days = settings.get_email_log_retention_days()
        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        async with AsyncSessionLocal() as session:
            result = await session.execute(delete(EmailLogEntry).where(EmailLogEntry.created_at < cutoff))
            await session.commit()
        deleted = result.rowcount or 0
        if deleted:
            logger.info('Очищен журнал писем', deleted=deleted, days=days)
        return deleted


email_log_service = EmailLogService()
