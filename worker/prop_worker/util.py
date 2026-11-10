"""Small shared helpers: timestamps, slate dates, numbers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    """ISO 8601 (with Z, offsets, or 'YYYY-MM-DDTHH:MMZ') -> aware UTC datetime. None/'' -> None."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def slate_date(now: datetime) -> date:
    """The ET date whose games are 'tonight'. Until 6am ET it is still the previous night."""
    return (now.astimezone(config.ET) - timedelta(hours=config.SLATE_ROLLOVER_HOURS)).date()


def et_date(dt: datetime) -> date:
    return dt.astimezone(config.ET).date()


def is_future(provider_ts: datetime | None, fetched_at: datetime) -> bool:
    return provider_ts is not None and \
        (provider_ts - fetched_at).total_seconds() > config.FUTURE_TS_TOLERANCE_S


def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def to_int(value) -> int | None:
    """'12' -> 12, '+5' -> 5, '-3' -> -3, '' / None / '--' -> None."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "--", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def kalshi_event_code(game_date: date, away: str, home: str) -> str:
    """date(2026, 11, 10), 'MIA', 'UTA' -> '26NOV10MIAUTA' (Kalshi's event-ticker suffix)."""
    month = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    return f"{game_date:%y}{month[game_date.month - 1]}{game_date:%d}{away}{home}"
