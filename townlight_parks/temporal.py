"""Timezone-aware booking windows and read-only availability checks."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Table, select

from .models import closures
from .store import InvalidInput, identifier, utcnow

__all__ = ["closure_conflict", "normalize_window", "overlapping", "utc"]

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def utc(value: datetime) -> datetime:
    """Return an actual timezone-aware datetime normalized to UTC."""
    if not isinstance(value, datetime):
        raise InvalidInput("time must be a datetime")
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidInput("time must have a valid timezone offset") from exc
    if value.tzinfo is None or offset is None:
        raise InvalidInput("time must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise InvalidInput("time cannot be normalized to UTC") from exc


def _window(start, end):
    start_utc, end_utc = utc(start), utc(end)
    if end_utc <= start_utc:
        raise InvalidInput("window end must be after window start")
    return start_utc, end_utc


def _minutes(snapshot, key):
    value = snapshot.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidInput("buffer minutes must be nonnegative integers")
    return value


def _clock(value, day, zone, *, end=False):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise InvalidInput("opening hours must use HH:MM")
    try:
        hour, minute = int(value[:2]), int(value[3:])
    except ValueError as exc:
        raise InvalidInput("opening hours must use HH:MM") from exc
    if hour == 24:
        if not end or minute != 0:
            raise InvalidInput("only an opening end may use 24:00")
        day, hour = day + timedelta(days=1), 0
    elif hour > 23:
        raise InvalidInput("opening hours must use HH:MM")
    if minute > 59:
        raise InvalidInput("opening hours must use HH:MM")
    local = datetime.combine(day, time(hour, minute))
    return local.replace(tzinfo=zone).astimezone(UTC)


def normalize_window(starts_at, ends_at, resource_snapshot):
    """Validate a requested window and its buffered occupied interval."""
    start, end = _window(starts_at, ends_at)
    duration = end - start
    if duration > timedelta(days=31):
        raise InvalidInput("window duration must be at most 31 days")
    now = utcnow()
    if start < now - timedelta(minutes=5):
        raise InvalidInput("window start is too far in the past")
    if start > now + timedelta(days=730):
        raise InvalidInput("window start is too far in the future")

    if not isinstance(resource_snapshot, Mapping):
        raise InvalidInput("resource snapshot is invalid")
    zone_name = resource_snapshot.get("timezone")
    if not isinstance(zone_name, str) or not zone_name:
        raise InvalidInput("resource timezone is invalid")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise InvalidInput("resource timezone is invalid") from exc

    before = _minutes(resource_snapshot, "buffer_before_minutes")
    after = _minutes(resource_snapshot, "buffer_after_minutes")
    try:
        occupied_from = start - timedelta(minutes=before)
        occupied_until = end + timedelta(minutes=after)
    except OverflowError as exc:
        raise InvalidInput("buffer minutes are invalid") from exc

    hours = resource_snapshot.get("hours")
    if not isinstance(hours, Mapping):
        raise InvalidInput("resource hours are invalid")
    local_start = start.astimezone(zone)
    configured = hours.get(_DAYS[local_start.weekday()])
    if not isinstance(configured, (list, tuple)) or not configured:
        raise InvalidInput("resource is not open on the local start day")

    for pair in configured:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise InvalidInput("resource hours are invalid")
        opens = _clock(pair[0], local_start.date(), zone)
        closes = _clock(pair[1], local_start.date(), zone, end=True)
        if closes <= opens:
            raise InvalidInput("resource hours are invalid")
        if opens <= occupied_from and occupied_until <= closes:
            return start, end, occupied_from, occupied_until
    raise InvalidInput("requested window is outside resource open hours")


def _bookings_table(table):
    required = ("id", "resource_id", "state", "occupied_from", "occupied_until")
    if not isinstance(table, Table) or any(name not in table.c for name in required):
        raise InvalidInput("bookings table is invalid")


def overlapping(conn, table, resource_id, start, end, exclude_id=None):
    """Return whether an active booking overlaps a half-open window."""
    _bookings_table(table)
    resource_id = identifier(resource_id)
    start, end = _window(start, end)
    statement = select(table.c.id).where(
        table.c.resource_id == resource_id,
        table.c.state.in_(("pending", "confirmed")),
        table.c.occupied_from < end,
        table.c.occupied_until > start,
    )
    if exclude_id is not None:
        statement = statement.where(table.c.id != identifier(exclude_id))
    return conn.execute(statement).first() is not None


def closure_conflict(conn, resource_id, start, end):
    """Return whether a resource closure overlaps a half-open window."""
    resource_id = identifier(resource_id)
    start, end = _window(start, end)
    statement = select(closures.c.id).where(
        closures.c.resource_id == resource_id,
        closures.c.starts_at < end,
        closures.c.ends_at > start,
    )
    return conn.execute(statement).first() is not None
