from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


def parse_target_date(value: str) -> date:
    return date.fromisoformat(value)


def get_timezone(timezone_name: str) -> ZoneInfo:
    return ZoneInfo(timezone_name)


def day_bounds(target_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    tz = get_timezone(timezone_name)
    day = parse_target_date(target_date)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day, time.max.replace(microsecond=0), tzinfo=tz)
    return start, end


def normalize_datetime(value: str | int | float | datetime, timezone_name: str) -> datetime:
    tz = get_timezone(timezone_name)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            if raw.isdigit():
                return normalize_datetime(int(raw), timezone_name)
            raise
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def normalize_datetime_string(
    value: str | int | float | datetime,
    timezone_name: str,
) -> str:
    return normalize_datetime(value, timezone_name).isoformat()


def is_same_target_date(send_time: str, target_date: str, timezone_name: str) -> bool:
    return normalize_datetime(send_time, timezone_name).date().isoformat() == target_date


def now_iso(timezone_name: str) -> str:
    return datetime.now(get_timezone(timezone_name)).replace(microsecond=0).isoformat()
