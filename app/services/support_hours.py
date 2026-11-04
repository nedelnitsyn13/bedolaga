"""Часы работы поддержки и ожидаемое время ответа на тикет.

Тикет можно оставить в любое время — он просто встаёт в очередь. Человеку же
важно знать, когда ему ответят: «примерно через 20 минут» днём или «после 10:00»
ночью. Время ответа не задаётся руками, а считается по реальной статистике:
медиана времени до первого ответа поддержки за последние 30 дней, в рабочих
минутах. Мало данных — берётся порог SLA из настроек.

Рабочее время учитывает и SLA: тикет, пришедший ночью, не «просрочен» к открытию,
а напоминания админам вне часов не уходят.

Часовой пояс — общий ``TIMEZONE`` бота. Часы выключены — поддержка считается
открытой всегда, рабочая минута равна обычной.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Ticket, TicketMessage
from app.utils.timezone import get_local_timezone


logger = structlog.get_logger(__name__)

#: За какой период считаем типичное время ответа.
STATS_WINDOW = timedelta(days=30)
#: Меньше ответов — статистике не верим, берём порог SLA.
MIN_SAMPLES = 5
#: Нижняя граница оценки: «ответим через минуту» обещать не стоит.
MIN_ESTIMATE_MINUTES = 5
#: Ответ быстрее этого — тикет создал сам админ, это не ответ на обращение.
_ADMIN_OPENED_SECONDS = 30
_CACHE_TTL_SECONDS = 600.0
#: Предохранитель обхода окон: год вперёд хватит любому расписанию.
_MAX_DAYS = 400


@dataclass(frozen=True)
class SupportHours:
    enabled: bool
    start: int  # час открытия, 0–23
    end: int  # час закрытия, 1–24; меньше start — смена через полночь
    tz: ZoneInfo

    @property
    def duration(self) -> timedelta:
        hours = (self.end - self.start) % 24
        return timedelta(hours=hours or 24)


def _hour(value, default: int, *, low: int, high: int) -> int:
    # Кривое значение в .env или базе не должно ронять поддержку — берём стандартное.
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return hour if low <= hour <= high else default


def current_hours() -> SupportHours:
    return SupportHours(
        enabled=bool(settings.SUPPORT_HOURS_ENABLED),
        start=_hour(settings.SUPPORT_HOURS_START, 10, low=0, high=23),
        end=_hour(settings.SUPPORT_HOURS_END, 22, low=1, high=24),
        tz=get_local_timezone(),
    )


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _windows(hours: SupportHours, since: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Рабочие окна по порядку, начиная с того, что ещё не кончилось к ``since``."""
    local_day = since.astimezone(hours.tz).date() - timedelta(days=1)
    for offset in range(_MAX_DAYS):
        day = local_day + timedelta(days=offset)
        opens = datetime(day.year, day.month, day.day, hours.start, tzinfo=hours.tz)
        closes = opens + hours.duration
        if closes > since:
            yield opens.astimezone(UTC), closes.astimezone(UTC)


def is_open(hours: SupportHours, now: datetime) -> bool:
    if not hours.enabled:
        return True
    now = _aware(now)
    opens, closes = next(_windows(hours, now))
    return opens <= now < closes


def next_open(hours: SupportHours, now: datetime) -> datetime:
    """Ближайший момент, когда поддержка на связи: ``now``, если уже открыто."""
    now = _aware(now)
    if not hours.enabled:
        return now
    opens, _ = next(_windows(hours, now))
    return max(opens, now)


def working_minutes_between(hours: SupportHours, start: datetime, end: datetime) -> float:
    start, end = _aware(start), _aware(end)
    if end <= start:
        return 0.0
    if not hours.enabled:
        return (end - start).total_seconds() / 60
    total = 0.0
    for opens, closes in _windows(hours, start):
        if opens >= end:
            break
        total += (min(closes, end) - max(opens, start)).total_seconds()
    return total / 60


def add_working_minutes(hours: SupportHours, start: datetime, minutes: float) -> datetime:
    """Момент, когда от ``start`` пройдёт ``minutes`` рабочих минут."""
    start = _aware(start)
    if not hours.enabled:
        return start + timedelta(minutes=minutes)
    remaining = timedelta(minutes=minutes)
    for opens, closes in _windows(hours, start):
        begin = max(opens, start)
        available = closes - begin
        if available >= remaining:
            return begin + remaining
        remaining -= available
    return start + timedelta(minutes=minutes)  # недостижимо при разумном расписании


# --- Типичное время ответа ------------------------------------------------------------


@dataclass
class _Cache:
    stored_at: float = float('-inf')
    minutes: int | None = None


_cache = _Cache()


def _sla_fallback() -> int:
    return max(MIN_ESTIMATE_MINUTES, int(settings.SUPPORT_TICKET_SLA_MINUTES or 60))


async def typical_reply_minutes(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Медиана рабочих минут до первого ответа поддержки за 30 дней; мало данных — порог SLA."""
    if time.monotonic() - _cache.stored_at < _CACHE_TTL_SECONDS and _cache.minutes is not None:
        return _cache.minutes

    now = now or datetime.now(UTC)
    since = now - STATS_WINDOW
    first_reply = (
        select(TicketMessage.ticket_id, func.min(TicketMessage.created_at).label('replied_at'))
        .where(TicketMessage.is_from_admin.is_(True), TicketMessage.created_at >= since)
        .group_by(TicketMessage.ticket_id)
        .subquery()
    )
    rows = await db.execute(
        select(Ticket.created_at, first_reply.c.replied_at)
        .join(first_reply, first_reply.c.ticket_id == Ticket.id)
        .where(Ticket.created_at >= since)
    )
    hours = current_hours()
    samples = [
        working_minutes_between(hours, created, replied)
        for created, replied in rows.all()
        if created is not None
        and replied is not None
        and (_aware(replied) - _aware(created)).total_seconds() >= _ADMIN_OPENED_SECONDS
    ]
    if len(samples) < MIN_SAMPLES:
        minutes = _sla_fallback()
    else:
        minutes = max(MIN_ESTIMATE_MINUTES, math.ceil(statistics.median(samples)))

    _cache.stored_at = time.monotonic()
    _cache.minutes = minutes
    return minutes


def round_estimate(minutes: float) -> int:
    """По-человечески: до часа — кратно 5 минутам, до трёх часов — получасам, дальше — часам."""
    minutes = max(MIN_ESTIMATE_MINUTES, minutes)
    if minutes <= 60:
        return int(math.ceil(minutes / 5) * 5)
    if minutes < 180:
        return int(round(minutes / 30) * 30)
    return int(round(minutes / 60) * 60)


@dataclass(frozen=True)
class SupportStatus:
    hours: SupportHours
    open_now: bool
    reply_minutes: int  # типичное время ответа, рабочих минут, уже округлено
    opens_at: datetime | None  # когда откроемся, если сейчас закрыто


async def support_status(db: AsyncSession, *, now: datetime | None = None) -> SupportStatus:
    now = _aware(now or datetime.now(UTC))
    hours = current_hours()
    open_now = is_open(hours, now)
    return SupportStatus(
        hours=hours,
        open_now=open_now,
        reply_minutes=round_estimate(await typical_reply_minutes(db, now=now)),
        opens_at=None if open_now else next_open(hours, now),
    )


async def expected_reply_at(db: AsyncSession, waiting_since: datetime) -> datetime:
    """Когда примерно ответят тикету, ждущему ответа с ``waiting_since``."""
    return add_working_minutes(current_hours(), waiting_since, await typical_reply_minutes(db))


# --- Тексты для бота ------------------------------------------------------------------


def timezone_label(tz: ZoneInfo, language: str | None, now: datetime | None = None) -> str:
    if getattr(tz, 'key', '') == 'Europe/Moscow':
        return 'МСК' if (language or 'ru').split('-')[0].lower() in ('ru', 'ua', 'uk') else 'MSK'
    offset = _aware(now or datetime.now(UTC)).astimezone(tz).utcoffset() or timedelta()
    total = int(offset.total_seconds() // 60)
    sign = '+' if total >= 0 else '-'
    hours_part, minutes_part = divmod(abs(total), 60)
    return f'UTC{sign}{hours_part}' + (f':{minutes_part:02d}' if minutes_part else '')


def _hour_label(hour: int) -> str:
    return f'{hour % 24:02d}:00' if hour != 24 else '24:00'


def format_estimate(texts, minutes: int) -> str:
    if minutes < 60:
        return texts.t('SUPPORT_ETA_MINUTES', '~{minutes} мин').format(minutes=minutes)
    value = minutes / 60
    shown = f'{value:.1f}'.rstrip('0').rstrip('.').replace('.', ',')
    return texts.t('SUPPORT_ETA_HOURS', '~{hours} ч').format(hours=shown)


def hours_line(texts, language: str | None, *, now: datetime | None = None) -> str:
    """«🕙 Поддержка работает с 10:00 до 22:00 (МСК)» и, если закрыто, «сейчас не на связи»."""
    hours = current_hours()
    if not hours.enabled:
        return ''
    line = texts.t('SUPPORT_HOURS_LINE', '🕙 Поддержка работает с {start} до {end} ({tz})').format(
        start=_hour_label(hours.start), end=_hour_label(hours.end), tz=timezone_label(hours.tz, language, now)
    )
    if not is_open(hours, now or datetime.now(UTC)):
        line += '\n' + texts.t(
            'SUPPORT_CLOSED_NOW', 'Сейчас мы не на связи — тикет можно оставить, ответим после {start}.'
        ).format(start=_hour_label(hours.start))
    return line


def eta_line(texts, status: SupportStatus, language: str | None) -> str:
    """Строка для подтверждения тикета: когда примерно ответят."""
    eta = format_estimate(texts, status.reply_minutes)
    if status.open_now:
        return texts.t('TICKET_ETA_OPEN', '⏱ Ответим примерно через {eta}.').format(eta=eta)
    return texts.t(
        'TICKET_ETA_CLOSED', '⏱ Сейчас поддержка не работает. Ответим после {start} ({tz}), обычно в течение {eta}.'
    ).format(start=_hour_label(status.hours.start), tz=timezone_label(status.hours.tz, language), eta=eta)
