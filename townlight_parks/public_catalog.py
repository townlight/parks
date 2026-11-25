"""Read-only public catalog projections for the Parks domain."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import func, select

from .commands import _safe
from .models import (
    closures,
    programs,
    registrations,
    resources,
    sessions,
)
from .store import Conflict

__all__ = ["PublicCatalogMixin"]

_RESOURCE_LIMIT = 500
_PROGRAM_LIMIT = 500
_SESSION_LIMIT = 2000
_CLOSURE_LIMIT = 1000


def _public(snapshot):
    """SQL predicate matching a public JSON snapshot."""
    return snapshot["public"].as_boolean().is_(True)


def _snapshot(row):
    value = row["snapshot"]
    return value if isinstance(value, Mapping) else {}


def _iso_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.isoformat()
    return _safe(value)


def _bounded(conn, statement, limit, label):
    rows = conn.execute(statement.limit(limit + 1)).mappings().all()
    if len(rows) > limit:
        raise Conflict(f"too many public {label}")
    return rows


def _resource(row):
    snapshot = _snapshot(row)
    return _safe({
        "id": row["id"],
        "revision": row["revision"],
        "name": snapshot.get("name"),
        "address": snapshot.get("address"),
        "timezone": snapshot.get("timezone"),
        "description": snapshot.get("description"),
        "amenities": snapshot.get("amenities"),
        "accessibility": snapshot.get("accessibility"),
        "hours": snapshot.get("hours"),
        "approval_required": snapshot.get("approval_required"),
        "buffer_before_minutes": snapshot.get("buffer_before_minutes"),
        "buffer_after_minutes": snapshot.get("buffer_after_minutes"),
        "fee_cents": snapshot.get("fee_cents"),
        "deposit_cents": snapshot.get("deposit_cents"),
        "currency": snapshot.get("currency"),
    })


def _program(row):
    snapshot = _snapshot(row)
    return _safe({
        "id": row["id"],
        "revision": row["revision"],
        "title": snapshot.get("title"),
        "description": snapshot.get("description"),
        "timezone": snapshot.get("timezone"),
        "min_age": snapshot.get("min_age"),
        "max_age": snapshot.get("max_age"),
        "eligibility_text": snapshot.get("eligibility_text"),
        "accessibility": snapshot.get("accessibility"),
    })


class PublicCatalogMixin:
    """Expose only explicitly projected public Parks records."""

    def public_catalog(self):
        """Return the bounded public catalog in deterministic id order."""
        with self._transaction() as conn:
            resource_rows = _bounded(
                conn,
                select(
                    resources.c.id,
                    resources.c.revision,
                    resources.c.snapshot,
                )
                .where(_public(resources.c.snapshot))
                .order_by(resources.c.id),
                _RESOURCE_LIMIT,
                "resources",
            )
            program_rows = _bounded(
                conn,
                select(
                    programs.c.id,
                    programs.c.revision,
                    programs.c.snapshot,
                )
                .where(_public(programs.c.snapshot))
                .order_by(programs.c.id),
                _PROGRAM_LIMIT,
                "programs",
            )

            confirmed = (
                select(
                    registrations.c.session_id.label("session_id"),
                    func.count(registrations.c.id).label("confirmed_count"),
                )
                .where(registrations.c.state == "confirmed")
                .group_by(registrations.c.session_id)
                .subquery()
            )
            session_rows = _bounded(
                conn,
                select(
                    sessions.c.id,
                    sessions.c.program_id,
                    sessions.c.revision,
                    sessions.c.starts_at,
                    sessions.c.ends_at,
                    sessions.c.capacity,
                    sessions.c.fee_cents,
                    sessions.c.currency,
                    sessions.c.state,
                    func.coalesce(
                        confirmed.c.confirmed_count, 0
                    ).label("confirmed_count"),
                )
                .join(programs, programs.c.id == sessions.c.program_id)
                .outerjoin(
                    confirmed,
                    confirmed.c.session_id == sessions.c.id,
                )
                .where(_public(programs.c.snapshot))
                .order_by(sessions.c.id),
                _SESSION_LIMIT,
                "sessions",
            )
            closure_rows = _bounded(
                conn,
                select(
                    closures.c.id,
                    closures.c.resource_id,
                    closures.c.starts_at,
                    closures.c.ends_at,
                )
                .join(resources, resources.c.id == closures.c.resource_id)
                .where(_public(resources.c.snapshot))
                .order_by(closures.c.id),
                _CLOSURE_LIMIT,
                "closures",
            )

        return _safe({
            "resources": [_resource(row) for row in resource_rows],
            "programs": [_program(row) for row in program_rows],
            "sessions": [{
                "id": row["id"],
                "program_id": row["program_id"],
                "revision": row["revision"],
                "starts_at": _iso_utc(row["starts_at"]),
                "ends_at": _iso_utc(row["ends_at"]),
                "capacity": row["capacity"],
                "available_seats": max(
                    0, int(row["capacity"]) - int(row["confirmed_count"])
                ),
                "fee_cents": row["fee_cents"],
                "currency": row["currency"],
                "state": row["state"],
            } for row in session_rows],
            "closures": [{
                "id": row["id"],
                "resource_id": row["resource_id"],
                "starts_at": _iso_utc(row["starts_at"]),
                "ends_at": _iso_utc(row["ends_at"]),
            } for row in closure_rows],
        })
