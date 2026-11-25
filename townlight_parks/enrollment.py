"""Registration enrollment commands for the Parks domain."""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .models import obligations, participants, programs, registrations, sessions
from .store import (
    Conflict,
    InvalidInput,
    identifier,
    new_id,
    positive_revision,
    text,
    utcnow,
)

__all__ = ["EnrollmentMixin"]

_ACTIVE = ("confirmed", "waitlisted")


class EnrollmentMixin:
    """Registration creation and reads, mixed into a RepositoryBase subclass."""

    def _check_eligibility(self, conn, session, participant):
        program = self._row(conn, programs, session["program_id"])
        program_snapshot = program.get("snapshot")
        if not isinstance(program_snapshot, dict):
            raise InvalidInput("program snapshot is invalid")
        starts_at = session.get("starts_at")
        if not isinstance(starts_at, datetime):
            raise InvalidInput("stored session date is invalid")
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=UTC)
        else:
            starts_at = starts_at.astimezone(UTC)
        try:
            local_date = starts_at.astimezone(
                ZoneInfo(program_snapshot["timezone"])
            ).date()
        except (KeyError, TypeError, ValueError):
            raise InvalidInput("program timezone is invalid") from None
        participant_snapshot = participant.get("snapshot")
        if not isinstance(participant_snapshot, dict):
            raise InvalidInput("participant snapshot is invalid")
        birth_value = participant_snapshot["birth_date"]
        if isinstance(birth_value, datetime):
            raise InvalidInput("participant birth_date must be an ISO date")
        if isinstance(birth_value, date):
            birth_date = birth_value
        elif isinstance(birth_value, str):
            try:
                birth_date = date.fromisoformat(birth_value)
            except ValueError:
                raise InvalidInput(
                    "participant birth_date must be an ISO date"
                ) from None
        else:
            raise InvalidInput("participant birth_date must be an ISO date")
        age = local_date.year - birth_date.year - (
            (local_date.month, local_date.day) < (birth_date.month, birth_date.day)
        )
        minimum = program_snapshot.get("min_age")
        maximum = program_snapshot.get("max_age")
        if minimum is not None and age < minimum:
            raise Conflict("participant does not meet the minimum age")
        if maximum is not None and age > maximum:
            raise Conflict("participant exceeds the maximum age")

    def registration_create(
        self, session_id, participant_id, actor, expected_session_revision,
        *, _conn=None,
    ):
        session_id = identifier(session_id)
        participant_id = identifier(participant_id)
        actor = text(actor, 128, field="actor")
        expected_session_revision = positive_revision(expected_session_revision)

        with self._transaction(_conn) as conn:
            located = self._row(conn, sessions, session_id)
            self._row(conn, programs, located["program_id"], lock=True)
            session = self._row(
                conn, sessions, session_id, expected_session_revision, lock=True
            )
            participant = self._row(
                conn, participants, participant_id, lock=True
            )
            if session["state"] != "open":
                raise Conflict("session is not open")
            starts_at = session.get("starts_at")
            if not isinstance(starts_at, datetime):
                raise InvalidInput("stored session date is invalid")
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=UTC)
            else:
                starts_at = starts_at.astimezone(UTC)
            if starts_at <= utcnow():
                raise Conflict("session has already started")
            self._check_eligibility(conn, session, participant)

            existing = conn.execute(
                select(registrations).where(
                    registrations.c.session_id == session_id,
                    registrations.c.participant_id == participant_id,
                )
            ).mappings().first()
            if existing is not None and existing["state"] in _ACTIVE:
                obligation_id = None
                if existing["state"] == "confirmed":
                    obligation_id = conn.execute(
                        select(obligations.c.id).where(
                            obligations.c.entity_kind == "registration",
                            obligations.c.entity_id == existing["id"],
                        )
                    ).scalar_one_or_none()
                return {**dict(existing), "obligation_id": obligation_id}

            confirmed = conn.execute(
                select(func.count()).select_from(registrations).where(
                    registrations.c.session_id == session_id,
                    registrations.c.state == "confirmed",
                )
            ).scalar_one()
            highest = conn.execute(
                select(func.max(registrations.c.queue_order)).where(
                    registrations.c.session_id == session_id
                )
            ).scalar_one()
            queue_order = 0 if highest is None else highest + 1
            state = (
                "confirmed"
                if confirmed < session["capacity"]
                else "waitlisted"
            )
            now = utcnow()

            if existing is not None:
                registration_id = existing["id"]
                revision = existing["revision"] + 1
                result = conn.execute(
                    registrations.update().where(
                        registrations.c.id == registration_id,
                        registrations.c.revision == existing["revision"],
                    ).values(
                        revision=revision,
                        state=state,
                        queue_order=queue_order,
                        checked_in_at=None,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise Conflict("registration revision changed")
                row = {
                    **dict(existing),
                    "revision": revision,
                    "state": state,
                    "queue_order": queue_order,
                    "checked_in_at": None,
                    "updated_at": now,
                }
                event_kind = "registration.reactivated"
            else:
                registration_id = new_id()
                revision = 1
                conn.execute(registrations.insert().values(
                    id=registration_id,
                    session_id=session_id,
                    participant_id=participant_id,
                    revision=revision,
                    state=state,
                    queue_order=queue_order,
                    checked_in_at=None,
                    created_at=now,
                    updated_at=now,
                ))
                row = {
                    "id": registration_id,
                    "session_id": session_id,
                    "participant_id": participant_id,
                    "revision": revision,
                    "state": state,
                    "queue_order": queue_order,
                    "checked_in_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
                event_kind = "registration.created"

            obligation_id = None
            if state == "confirmed":
                obligation_id = self._enrollment_obligation(
                    conn, registration_id, session
                )
            self._event(
                conn, "registration", registration_id, revision, event_kind,
                actor, {
                    "session_id": session_id,
                    "participant_id": participant_id,
                    "state": state,
                },
            )
        return {**row, "obligation_id": obligation_id}

    def _enrollment_obligation(self, conn, registration_id, session):
        existing = conn.execute(
            select(obligations.c.id).where(
                obligations.c.entity_kind == "registration",
                obligations.c.entity_id == registration_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        obligation_id = new_id()
        conn.execute(obligations.insert().values(
            id=obligation_id,
            entity_kind="registration",
            entity_id=registration_id,
            amount_cents=session["fee_cents"],
            deposit_cents=0,
            currency=session["currency"],
            created_at=utcnow(),
        ))
        return obligation_id

    def registration_get(self, registration_id):
        with self._transaction() as conn:
            row = self._row(conn, registrations, registration_id)
            obligation_id = conn.execute(
                select(obligations.c.id).where(
                    obligations.c.entity_kind == "registration",
                    obligations.c.entity_id == row["id"],
                )
            ).scalar_one_or_none()
        return {**row, "obligation_id": obligation_id}
