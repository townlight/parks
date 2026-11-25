"""Catalog operations for public resources and programs.

Only the shared ``resources`` and ``programs`` tables are catalog records.
Closures are operational records attached to resources and never public
catalog rows.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, select

from .models import (
    bookings,
    closures,
    programs,
    registrations,
    resources,
    sessions,
)
from .store import (
    Conflict,
    InvalidInput,
    NotFound,
    identifier,
    new_id,
    text,
    utcnow,
)
from .temporal import utc
from .validation import ProgramSpec, ResourceSpec, snapshot

_KINDS = {
    "resource": (resources, ResourceSpec),
    "program": (programs, ProgramSpec),
}
_RESOURCE_OPERATIONAL = (
    "timezone",
    "hours",
    "buffer_before_minutes",
    "buffer_after_minutes",
)
def _kind(kind: Any):
    if not isinstance(kind, str) or kind not in _KINDS:
        raise InvalidInput("kind must be resource or program")
    return _KINDS[kind]


def _public_only(value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidInput("public_only must be boolean")
    return value


def _page(limit: Any, offset: Any) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise InvalidInput("limit must be an integer between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidInput("offset must be a nonnegative integer")
    return limit, offset


def _window(starts_at: Any, ends_at: Any):
    try:
        start = utc(starts_at)
        end = utc(ends_at)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("closure window must contain aware timestamps") from exc
    if start >= end:
        raise InvalidInput("ends_at must be later than starts_at")
    return start, end


def _changed(old: Mapping[str, Any], new: Mapping[str, Any], keys) -> bool:
    return any(old.get(key) != new.get(key) for key in keys)


def _age_changed(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    return _changed(old, new, ("min_age", "max_age", "eligibility_text"))


class CatalogMixin:
    """Catalog and closure methods for a repository using the shared engine."""

    def catalog_create(self, kind: str, actor: str, payload: Mapping[str, Any]):
        table, model = _kind(kind)
        actor = text(actor, 128, field="actor")
        body = snapshot(model, payload)
        record_id = new_id()
        now = utcnow()
        row = {
            "id": record_id,
            "revision": 1,
            "snapshot": body,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as conn:
            conn.execute(table.insert().values(**row))
            self._event(
                conn, kind, record_id, 1, f"{kind}.created", actor,
                {"snapshot": body},
            )
        return dict(row)

    def catalog_update(
        self,
        kind: str,
        record_id: str,
        actor: str,
        expected_revision: int,
        payload: Mapping[str, Any],
    ):
        table, model = _kind(kind)
        actor = text(actor, 128, field="actor")
        record_id = identifier(record_id)
        now = utcnow()
        with self.engine.begin() as conn:
            row = self._row(conn, table, record_id, expected_revision, lock=True)
            old = row.get("snapshot") or {}
            if not isinstance(old, Mapping):
                raise InvalidInput("stored snapshot is not an object")
            if not isinstance(payload, Mapping):
                raise InvalidInput("payload must be an object")
            candidate = dict(old)
            candidate.update(payload)
            body = snapshot(model, candidate)
            if kind == "resource":
                if _changed(old, body, _RESOURCE_OPERATIONAL):
                    busy = conn.execute(
                        select(bookings.c.id).where(
                            bookings.c.resource_id == record_id,
                            bookings.c.state.in_(("pending", "confirmed")),
                            bookings.c.occupied_until > now,
                        ).limit(1)
                    ).first()
                    if busy is not None:
                        raise Conflict(
                            "resource changes require operator resolution of "
                            "future active bookings"
                        )
            elif _age_changed(old, body):
                active = conn.execute(
                    select(registrations.c.id)
                    .join(sessions, registrations.c.session_id == sessions.c.id)
                    .where(
                        sessions.c.program_id == record_id,
                        sessions.c.starts_at > now,
                        registrations.c.state.in_(("confirmed", "waitlisted")),
                    )
                    .limit(1)
                ).first()
                if active is not None:
                    raise Conflict(
                        "age eligibility changes require operator resolution "
                        "of future active registrations"
                    )
            revision = row["revision"] + 1
            conn.execute(
                table.update()
                .where(table.c.id == record_id)
                .values(revision=revision, snapshot=body, updated_at=now)
            )
            self._event(
                conn, kind, record_id, revision, f"{kind}.updated", actor,
                {"from_revision": row["revision"], "snapshot": body},
            )
            updated = dict(row)
            updated.update(revision=revision, snapshot=body, updated_at=now)
            return updated

    def catalog_get(self, kind: str, record_id: str, public_only: bool = False):
        table, _ = _kind(kind)
        public_only = _public_only(public_only)
        with self.engine.begin() as conn:
            row = self._row(conn, table, record_id)
        body = row.get("snapshot") or {}
        if public_only and (not isinstance(body, Mapping) or body.get("public") is not True):
            raise NotFound(f"{kind} not found")
        return dict(row)

    def catalog_list(
        self,
        kind: str,
        public_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ):
        table, _ = _kind(kind)
        public_only = _public_only(public_only)
        limit, offset = _page(limit, offset)
        statement = select(table)
        if public_only:
            statement = statement.where(
                table.c.snapshot["public"].as_boolean().is_(True)
            )
        statement = (
            statement.order_by(table.c.created_at, table.c.id)
            .limit(limit)
            .offset(offset)
        )
        with self.engine.begin() as conn:
            return [dict(row) for row in conn.execute(statement).mappings()]

    def closure_add(
        self,
        resource_id: str,
        actor: str,
        expected_revision: int,
        starts_at: Any,
        ends_at: Any,
        reason: str,
    ):
        resource_id = identifier(resource_id)
        actor = text(actor, 128, field="actor")
        reason = text(reason, 1024, field="reason")
        start, end = _window(starts_at, ends_at)
        now = utcnow()
        closure_id = new_id()
        with self.engine.begin() as conn:
            row = self._row(conn, resources, resource_id, expected_revision, lock=True)
            overlap = conn.execute(
                select(bookings.c.id).where(
                    bookings.c.resource_id == resource_id,
                    bookings.c.state.in_(("pending", "confirmed")),
                    bookings.c.occupied_from < end,
                    bookings.c.occupied_until > start,
                ).limit(1)
            ).first()
            if overlap is not None:
                raise Conflict("closure overlaps an active booking")
            closure = {
                "id": closure_id,
                "resource_id": resource_id,
                "starts_at": start,
                "ends_at": end,
                "reason": reason,
                "actor": actor,
                "created_at": now,
            }
            conn.execute(closures.insert().values(**closure))
            revision = row["revision"] + 1
            conn.execute(
                resources.update()
                .where(resources.c.id == resource_id)
                .values(revision=revision, updated_at=now)
            )
            self._event(
                conn, "resource", resource_id, revision,
                "resource.closure_added", actor,
                {
                    **closure,
                    "starts_at": start.isoformat(),
                    "ends_at": end.isoformat(),
                    "created_at": now.isoformat(),
                },
            )
        return dict(closure)

    def closure_remove(
        self,
        resource_id: str,
        closure_id: str,
        actor: str,
        expected_revision: int,
        reason: str,
    ):
        resource_id = identifier(resource_id)
        closure_id = identifier(closure_id)
        actor = text(actor, 128, field="actor")
        reason = text(reason, 1024, field="reason")
        now = utcnow()
        with self.engine.begin() as conn:
            row = self._row(conn, resources, resource_id, expected_revision, lock=True)
            closure = self._row(conn, closures, closure_id, lock=True)
            if closure["resource_id"] != resource_id:
                raise NotFound("closure not found for resource")
            conn.execute(delete(closures).where(closures.c.id == closure_id))
            revision = row["revision"] + 1
            conn.execute(
                resources.update()
                .where(resources.c.id == resource_id)
                .values(revision=revision, updated_at=now)
            )
            self._event(
                conn, "resource", resource_id, revision,
                "resource.closure_removed", actor,
                {"closure_id": closure_id, "reason": reason},
            )
        return dict(closure)

    def closure_list(self, resource_id: str):
        resource_id = identifier(resource_id)
        statement = (
            select(closures)
            .where(closures.c.resource_id == resource_id)
            .order_by(closures.c.starts_at, closures.c.id)
            .limit(300)
        )
        with self.engine.begin() as conn:
            return [dict(row) for row in conn.execute(statement).mappings()]
