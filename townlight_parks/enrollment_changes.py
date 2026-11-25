"""Enrollment lifecycle changes for the Parks domain.

This mixin is meant to be combined with :class:`townlight_parks.store.RepositoryBase`.
It only uses the explicit SQLAlchemy Core tables declared in
:mod:`townlight_parks.models` and never reflects, guesses column aliases, or
makes external requests.
"""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, select

from .models import (
    households,
    participants,
    registrations,
    sessions,
)
from .store import (
    Conflict,
    NotFound,
    identifier,
    positive_revision,
    text,
    utcnow,
)

__all__ = ["EnrollmentChangesMixin"]

ACTIVE_STATES = ("confirmed", "waitlisted")
ROSTER_LIMIT = 10_000
HOUSEHOLD_LIMIT = 1_000


def _snapshot(value):
    return value if isinstance(value, Mapping) else {}


class EnrollmentChangesMixin:
    """Registration cancellation, check-in, waitlist and household reads."""

    # -- waitlist promotion ---------------------------------------------
    def _promote_waitlist(self, conn, sessionrow, actor):
        """Promote eligible waitlisted registrations in FIFO order."""
        actor = text(actor, 128, field="actor")
        session_id = sessionrow["id"]
        if sessionrow["state"] != "open":
            return []
        confirmed = conn.execute(
            select(func.count()).select_from(registrations).where(
                registrations.c.session_id == session_id,
                registrations.c.state == "confirmed",
            )
        ).scalar_one()
        need = sessionrow["capacity"] - confirmed
        if need <= 0:
            return []
        waiting = conn.execute(
            select(registrations).where(
                registrations.c.session_id == session_id,
                registrations.c.state == "waitlisted",
            ).order_by(registrations.c.queue_order, registrations.c.id)
        ).mappings().all()
        promoted = []
        for row in waiting:
            if need <= 0:
                break
            participant = conn.execute(
                select(participants)
                .where(participants.c.id == row["participant_id"])
                .with_for_update()
            ).mappings().first()
            try:
                self._check_eligibility(conn, sessionrow, participant)
            except (Conflict, NotFound) as exc:
                revision = row["revision"] + 1
                now = utcnow()
                changed = conn.execute(
                    registrations.update().where(
                        registrations.c.id == row["id"],
                        registrations.c.revision == row["revision"],
                        registrations.c.state == "waitlisted",
                    ).values(state="cancelled", revision=revision, updated_at=now)
                )
                if changed.rowcount == 1:
                    self._event(
                        conn, "registration", row["id"], revision,
                        "registration.cancelled", actor,
                        {"reason": "ineligible", "detail": str(exc)},
                    )
                continue
            revision = row["revision"] + 1
            now = utcnow()
            changed = conn.execute(
                registrations.update().where(
                    registrations.c.id == row["id"],
                    registrations.c.revision == row["revision"],
                    registrations.c.state == "waitlisted",
                ).values(state="confirmed", revision=revision, updated_at=now)
            )
            if changed.rowcount != 1:
                continue
            self._event(
                conn, "registration", row["id"], revision,
                "registration.promoted", actor, {"from": "waitlisted"},
            )
            obligation_id = self._enrollment_obligation(conn, row["id"], sessionrow)
            promoted.append({
                **dict(row),
                "revision": revision,
                "state": "confirmed",
                "updated_at": now,
                "obligation_id": obligation_id,
            })
            need -= 1
        return promoted

    # -- cancellation ---------------------------------------------------
    def registration_cancel(
        self, registration_id, actor, expected_revision, reason, *, _conn=None
    ):
        registration_id = identifier(registration_id)
        actor = text(actor, 128, field="actor")
        expected_revision = positive_revision(expected_revision)
        reason = text(reason, 1024, field="reason")
        with self._transaction(_conn) as conn:
            located = conn.execute(
                select(registrations).where(registrations.c.id == registration_id)
            ).mappings().first()
            if located is None:
                raise NotFound("registration not found")
            sessionrow = self._row(
                conn, sessions, located["session_id"], lock=True
            )
            row = self._row(
                conn, registrations, registration_id, expected_revision, lock=True
            )
            if row["state"] == "cancelled":
                return {"registration": dict(row), "promoted": []}
            if row["state"] not in ACTIVE_STATES:
                raise Conflict("registration cannot be cancelled")
            revision = row["revision"] + 1
            now = utcnow()
            changed = conn.execute(
                registrations.update().where(
                    registrations.c.id == registration_id,
                    registrations.c.revision == row["revision"],
                ).values(state="cancelled", revision=revision, updated_at=now)
            )
            if changed.rowcount != 1:
                raise Conflict("registration revision changed")
            self._event(
                conn, "registration", registration_id, revision,
                "registration.cancelled", actor, {"reason": reason},
            )
            # No automatic refund is issued; financial flows are explicit.
            promoted = self._promote_waitlist(conn, sessionrow, actor)
            cancelled = {
                **dict(row), "revision": revision, "state": "cancelled",
                "updated_at": now,
            }
        return {"registration": cancelled, "promoted": promoted}

    # -- check-in -------------------------------------------------------
    def registration_check_in(
        self, registration_id, actor, expected_revision, *, _conn=None
    ):
        registration_id = identifier(registration_id)
        actor = text(actor, 128, field="actor")
        expected_revision = positive_revision(expected_revision)
        with self._transaction(_conn) as conn:
            located = conn.execute(
                select(registrations).where(registrations.c.id == registration_id)
            ).mappings().first()
            if located is None:
                raise NotFound("registration not found")
            self._row(conn, sessions, located["session_id"], lock=True)
            row = self._row(conn, registrations, registration_id, lock=True)
            if row["state"] != "confirmed":
                raise Conflict("only confirmed registrations can be checked in")
            if row["checked_in_at"] is not None:
                return dict(row)
            if row["revision"] != expected_revision:
                raise Conflict("registration revision changed")
            revision = row["revision"] + 1
            now = utcnow()
            changed = conn.execute(
                registrations.update().where(
                    registrations.c.id == registration_id,
                    registrations.c.revision == row["revision"],
                ).values(checked_in_at=now, revision=revision, updated_at=now)
            )
            if changed.rowcount != 1:
                raise Conflict("registration revision changed")
            self._event(
                conn, "registration", registration_id, revision,
                "registration.checked_in", actor, {},
            )
        return {
            **dict(row), "revision": revision, "checked_in_at": now,
            "updated_at": now,
        }

    # -- reads ----------------------------------------------------------
    def roster(self, session_id):
        session_id = identifier(session_id)
        statement = (
            select(
                registrations.c.id.label("registration_id"),
                registrations.c.participant_id,
                registrations.c.state,
                registrations.c.queue_order,
                registrations.c.revision,
                registrations.c.checked_in_at,
                participants.c.household_id,
                participants.c.snapshot.label("participant_snapshot"),
                households.c.snapshot.label("household_snapshot"),
            )
            .select_from(
                registrations.join(
                    participants, registrations.c.participant_id == participants.c.id
                ).join(
                    households, participants.c.household_id == households.c.id
                )
            )
            .where(
                registrations.c.session_id == session_id,
                registrations.c.state.in_(ACTIVE_STATES),
            )
            .order_by(
                registrations.c.state,
                registrations.c.queue_order,
                registrations.c.id,
            )
            .limit(ROSTER_LIMIT + 1)
        )
        with self._transaction() as conn:
            self._row(conn, sessions, session_id)
            rows = conn.execute(statement).mappings().all()
        if len(rows) > ROSTER_LIMIT:
            raise Conflict("roster exceeds 10000 participants")
        roster = []
        for row in rows:
            psnap = _snapshot(row["participant_snapshot"])
            hsnap = _snapshot(row["household_snapshot"])
            roster.append({
                "registration_id": row["registration_id"],
                "participant_id": row["participant_id"],
                "state": row["state"],
                "queue_order": row["queue_order"],
                "revision": row["revision"],
                "checked_in_at": row["checked_in_at"],
                "name": psnap.get("name"),
                "birth_date": psnap.get("birth_date"),
                "accommodations": psnap.get("accommodations"),
                "contact": {
                    "contact_name": hsnap.get("contact_name"),
                    "email": hsnap.get("email"),
                    "phone": hsnap.get("phone"),
                    "address": hsnap.get("address"),
                },
            })
        return roster

    def registrations_for_household(self, household_id):
        household_id = identifier(household_id)
        statement = (
            select(registrations)
            .select_from(
                registrations.join(
                    participants, registrations.c.participant_id == participants.c.id
                )
            )
            .where(participants.c.household_id == household_id)
            .order_by(registrations.c.session_id, registrations.c.id)
            .limit(HOUSEHOLD_LIMIT + 1)
        )
        with self._transaction() as conn:
            rows = conn.execute(statement).mappings().all()
        if len(rows) > HOUSEHOLD_LIMIT:
            raise Conflict("household registrations exceed 1000 rows")
        return [dict(row) for row in rows]
