"""Session commands and public reads for the Parks domain."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from .models import programs, registrations, sessions
from .store import (
    Conflict,
    InvalidInput,
    NotFound,
    cents,
    identifier,
    new_id,
    positive_revision,
    text,
    utcnow,
)
from .temporal import utc

__all__ = ["SessionMixin"]

_STATES = ("open", "closed", "cancelled")
_ACTIVE = ("confirmed", "waitlisted")


def _capacity(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput("capacity must be an integer")
    if not 1 <= value <= 100_000:
        raise InvalidInput("capacity must be between 1 and 100000")
    return value


def _state(value):
    if value not in _STATES:
        raise InvalidInput("state must be open, closed, or cancelled")
    return value


def _bounds(starts_at, ends_at):
    start, end = utc(starts_at), utc(ends_at)
    if end <= start:
        raise InvalidInput("session end must be after start")
    if end - start > timedelta(days=31):
        raise InvalidInput("session duration must be at most 31 days")
    return start, end


def _future(start):
    now = utcnow()
    if start <= now:
        raise InvalidInput("session start must be in the future")
    if start > now + timedelta(days=730):
        raise InvalidInput("session start is too far in the future")


def _stored(value):
    if not isinstance(value, datetime):
        raise InvalidInput("stored session date is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return utc(value)


def _is_public(program):
    snapshot = program.get("snapshot")
    return isinstance(snapshot, Mapping) and snapshot.get("public") is True


def _page(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidInput(f"{field} must be a nonnegative integer")
    return value


class SessionMixin:
    """Session lifecycle operations, mixed into a RepositoryBase subclass."""

    def session_create(
        self, program_id, actor, starts_at, ends_at, capacity, fee_cents,
        currency, *, _conn=None,
    ):
        program_id = identifier(program_id)
        actor = text(actor, 128, field="actor")
        capacity = _capacity(capacity)
        fee_cents = cents(fee_cents, "fee_cents")
        if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise InvalidInput("currency must be three uppercase letters")
        start, end = _bounds(starts_at, ends_at)
        _future(start)

        with self._transaction(_conn) as conn:
            self._row(conn, programs, program_id, lock=True)
            session_id, now = new_id(), utcnow()
            conn.execute(sessions.insert().values(
                id=session_id, program_id=program_id, revision=1,
                starts_at=start, ends_at=end, capacity=capacity,
                fee_cents=fee_cents, currency=currency, state="open",
                created_at=now, updated_at=now,
            ))
            self._event(conn, "session", session_id, 1, "session.created", actor, {
                "program_id": program_id,
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat(),
                "capacity": capacity,
                "fee_cents": fee_cents,
                "currency": currency,
                "state": "open",
            })
        return {
            "id": session_id, "program_id": program_id, "revision": 1,
            "starts_at": start, "ends_at": end, "capacity": capacity,
            "fee_cents": fee_cents, "currency": currency, "state": "open",
            "created_at": now, "updated_at": now,
        }

    def session_update(
        self, session_id, actor, expected_revision, *, capacity=None,
        state=None, starts_at=None, ends_at=None, _conn=None,
    ):
        actor = text(actor, 128, field="actor")
        expected_revision = positive_revision(expected_revision)
        with self._transaction(_conn) as conn:
            row = self._row(
                conn, sessions, session_id, expected_revision, lock=True
            )
            session_id = row["id"]
            confirmed = conn.execute(
                select(func.count()).select_from(registrations).where(
                    registrations.c.session_id == session_id,
                    registrations.c.state == "confirmed",
                )
            ).scalar_one()
            active = conn.execute(
                select(func.count()).select_from(registrations).where(
                    registrations.c.session_id == session_id,
                    registrations.c.state.in_(_ACTIVE),
                )
            ).scalar_one()

            changes = {}
            if capacity is not None:
                capacity = _capacity(capacity)
                if capacity < confirmed:
                    raise Conflict("capacity cannot be below confirmed registrations")
                changes["capacity"] = capacity
            else:
                capacity = row["capacity"]

            if state is not None:
                state = _state(state)
                if state == "cancelled" and active:
                    raise Conflict("cancel registrations first")
                changes["state"] = state
            else:
                state = row["state"]

            date_change = starts_at is not None or ends_at is not None
            if date_change and active:
                raise Conflict("cancel registrations first")
            new_start, new_end = _stored(row["starts_at"]), _stored(row["ends_at"])
            if starts_at is not None:
                new_start = utc(starts_at)
                _future(new_start)
                changes["starts_at"] = new_start.isoformat()
            if ends_at is not None:
                new_end = utc(ends_at)
                changes["ends_at"] = new_end.isoformat()
            if date_change:
                new_start, new_end = _bounds(new_start, new_end)

            if not changes:
                return {**dict(row), "promoted": []}
            revision = expected_revision + 1
            now = utcnow()
            result = conn.execute(
                sessions.update().where(
                    sessions.c.id == session_id,
                    sessions.c.revision == expected_revision,
                ).values(
                    revision=revision, capacity=capacity, state=state,
                    starts_at=new_start, ends_at=new_end, updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise Conflict("session revision changed")
            self._event(conn, "session", session_id, revision, "session.updated", actor, {
                "starts_at": new_start.isoformat(),
                "ends_at": new_end.isoformat(),
                "capacity": capacity,
                "state": state,
                "changed": sorted(changes),
            })
            updated_session = {
                **dict(row), "revision": revision, "capacity": capacity,
                "state": state, "starts_at": new_start, "ends_at": new_end,
                "updated_at": now,
            }
            promoted = []
            if capacity > row["capacity"] or (
                state == "open" and row["state"] != "open"
            ):
                promoted = self._promote_waitlist(conn, updated_session, actor)
        return {
            **updated_session, "promoted": promoted,
        }

    def session_get(self, session_id, public_only=False):
        if not isinstance(public_only, bool):
            raise InvalidInput("public_only must be a boolean")
        with self._transaction() as conn:
            row = self._row(conn, sessions, session_id)
            program = self._row(conn, programs, row["program_id"])
        if public_only and not _is_public(program):
            raise NotFound("session not found")
        return dict(row)

    def session_list(
        self, program_id=None, public_only=False, limit=50, offset=0
    ):
        if not isinstance(public_only, bool):
            raise InvalidInput("public_only must be a boolean")
        limit, offset = _page(limit, "limit"), _page(offset, "offset")
        statement = select(sessions)
        if program_id is not None:
            program_id = identifier(program_id)
        if public_only:
            statement = statement.join(
                programs, sessions.c.program_id == programs.c.id
            ).where(programs.c.snapshot["public"].as_boolean().is_(True))
        if program_id is not None:
            statement = statement.where(sessions.c.program_id == program_id)
        statement = statement.order_by(
            sessions.c.starts_at, sessions.c.id
        ).limit(limit).offset(offset)
        with self._transaction() as conn:
            return [dict(row) for row in conn.execute(statement).mappings()]
