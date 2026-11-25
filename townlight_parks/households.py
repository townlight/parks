"""Household and participant operations for the Townlight Parks domain.

The public token returned by :meth:`HouseholdMixin.household_create` is the
only capability that may resolve a private household.  It is never stored
directly, logged, or written to an event.  Callers of token resolution and
household lookup are responsible for keeping those private results inside the
authenticated/capability boundary.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import or_, select, update

from .models import households, participants, registrations, sessions
from .store import (
    Conflict,
    InvalidInput,
    Unauthorized,
    identifier,
    new_id,
    positive_revision,
    text,
    utcnow,
)
from .validation import (
    HouseholdSpec,
    ParticipantSpec,
)
from .validation import (
    snapshot as validated_snapshot,
)

__all__ = ["HouseholdMixin"]

_HOUSEHOLD_PRIVATE = {"tracking_hash", "encrypted_tracking_token"}


def _public_household(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in _HOUSEHOLD_PRIVATE
    }


def _public_participant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "household_id": row["household_id"],
        "revision": row["revision"],
        "snapshot": row["snapshot"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class HouseholdMixin:
    """Persistence operations for households and their participants."""

    def household_create(
        self, actor: Any, payload: Any, *, key: Any, _conn: Any = None
    ) -> dict[str, Any]:
        actor = text(actor, 128, field="actor")
        household_snapshot = validated_snapshot(HouseholdSpec, payload)
        tracking_token = secrets.token_urlsafe(32)
        tracking_hash = hashlib.sha256(tracking_token.encode("ascii")).hexdigest()
        encrypted_token = Fernet(key).encrypt(tracking_token.encode("ascii"))
        household_id = new_id()
        now = utcnow()
        with self._transaction(_conn) as conn:
            conn.execute(
                households.insert().values(
                    id=household_id,
                    tracking_hash=tracking_hash,
                    encrypted_tracking_token=encrypted_token,
                    snapshot=household_snapshot,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._event(
                conn,
                "household",
                household_id,
                1,
                "household.created",
                actor,
                {"household_id": household_id},
            )
        return {
            "id": household_id,
            "tracking_token": tracking_token,
            "snapshot": household_snapshot,
        }

    def household_by_token(self, token: Any) -> dict[str, Any]:
        """Resolve a capability token to its sanitized private household row.

        The raw token is only hashed for lookup; it is never used as a query
        parameter, logged, or included in an event.
        """
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise Unauthorized("invalid tracking token")
        try:
            token_bytes = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise Unauthorized("invalid tracking token") from exc
        token_hash = hashlib.sha256(token_bytes).hexdigest()
        with self.engine.connect() as conn:
            row = conn.execute(
                select(households).where(households.c.tracking_hash == token_hash)
            ).mappings().first()
        if row is None or not hmac.compare_digest(row["tracking_hash"], token_hash):
            raise Unauthorized("invalid tracking token")
        return _public_household(dict(row))

    def household_get(self, household_id: Any) -> dict[str, Any]:
        """Staff-domain lookup of a real household with private secrets removed."""
        with self.engine.connect() as conn:
            row = self._row(conn, households, household_id)
        return _public_household(row)

    def household_search(
        self, query: str = "", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str):
            raise InvalidInput("query must be a string")
        if len(query) > 200:
            raise InvalidInput("query must be at most 200 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InvalidInput("limit must be between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidInput("offset must be non-negative")
        statement = select(households)
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(
                    households.c.snapshot["contact_name"].as_string().ilike(pattern),
                    households.c.snapshot["email"].as_string().ilike(pattern),
                )
            )
        statement = (
            statement.order_by(households.c.created_at, households.c.id)
            .limit(limit)
            .offset(offset)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(statement).mappings().all()
        return [_public_household(dict(row)) for row in rows]

    def participant_create(
        self, household_id: Any, actor: Any, payload: Any, *, _conn: Any = None
    ) -> dict[str, Any]:
        household_id = identifier(household_id)
        actor = text(actor, 128, field="actor")
        participant_snapshot = validated_snapshot(ParticipantSpec, payload)
        participant_id = new_id()
        now = utcnow()
        with self._transaction(_conn) as conn:
            self._row(conn, households, household_id)
            conn.execute(
                participants.insert().values(
                    id=participant_id,
                    household_id=household_id,
                    revision=1,
                    snapshot=participant_snapshot,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._event(
                conn,
                "participant",
                participant_id,
                1,
                "participant.created",
                actor,
                {
                    "household_id": household_id,
                    "participant_id": participant_id,
                },
            )
        return {
            "id": participant_id,
            "household_id": household_id,
            "revision": 1,
            "snapshot": participant_snapshot,
            "created_at": now,
            "updated_at": now,
        }

    def participant_update(
        self,
        participant_id: Any,
        actor: Any,
        expected_revision: Any,
        payload: Any,
        *,
        _conn: Any = None,
    ) -> dict[str, Any]:
        participant_id = identifier(participant_id)
        actor = text(actor, 128, field="actor")
        expected_revision = positive_revision(expected_revision)
        participant_snapshot = validated_snapshot(ParticipantSpec, payload)
        now = utcnow()
        with self._transaction(_conn) as conn:
            existing = self._row(
                conn, participants, participant_id, expected_revision, lock=True
            )
            old_snapshot = existing["snapshot"]
            if old_snapshot.get("birth_date") != participant_snapshot["birth_date"]:
                active = conn.execute(
                    select(registrations.c.id)
                    .join(
                        sessions,
                        registrations.c.session_id == sessions.c.id,
                    )
                    .where(
                        registrations.c.participant_id == participant_id,
                        registrations.c.state.in_(("confirmed", "waitlisted")),
                        sessions.c.ends_at > now,
                    )
                    .limit(1)
                ).first()
                if active is not None:
                    raise Conflict(
                        "participant birth date cannot change while registered "
                        "for a future active session"
                    )
            revision = expected_revision + 1
            result = conn.execute(
                update(participants)
                .where(
                    participants.c.id == participant_id,
                    participants.c.revision == expected_revision,
                )
                .values(
                    revision=revision,
                    snapshot=participant_snapshot,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise Conflict("participant revision changed")
            self._event(
                conn,
                "participant",
                participant_id,
                revision,
                "participant.updated",
                actor,
                {
                    "household_id": existing["household_id"],
                    "participant_id": participant_id,
                },
            )
        return {
            "id": participant_id,
            "household_id": existing["household_id"],
            "revision": revision,
            "snapshot": participant_snapshot,
            "created_at": existing["created_at"],
            "updated_at": now,
        }

    def participant_list(self, household_id: Any) -> list[dict[str, Any]]:
        household_id = identifier(household_id)
        with self.engine.connect() as conn:
            self._row(conn, households, household_id)
            rows = conn.execute(
                select(participants)
                .where(participants.c.household_id == household_id)
                .order_by(participants.c.created_at, participants.c.id)
            ).mappings().all()
        return [_public_participant(dict(row)) for row in rows]

    def participant_get(self, participant_id: Any) -> dict[str, Any]:
        with self.engine.connect() as conn:
            row = self._row(conn, participants, participant_id)
        return _public_participant(row)
