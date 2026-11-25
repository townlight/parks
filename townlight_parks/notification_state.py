"""Outbox notification claim/finish/summary state transitions.

The mixin only touches the declared ``outbox`` table.  It never sends mail or
makes network calls; decryption happens before any caller can deliver a
message.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, update

from .models import outbox
from .store import Conflict, InvalidInput, NotFound, identifier, new_id, text, utcnow

__all__ = ["NotificationStateMixin"]

_GENERIC_ERROR = "notification delivery failed"
_OUTBOX_STATES = ("pending", "claimed", "sent", "failed", "uncertain")
_FINISH_OUTCOMES = frozenset(("sent", "failed", "uncertain", "retry"))


def _cipher(key: Any) -> Fernet:
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:  # key material must never be echoed
        raise InvalidInput("invalid notification encryption key") from exc


def _payload(encrypted_body: Any, cipher: Fernet) -> tuple[str, str]:
    try:
        body = bytes(encrypted_body) if isinstance(encrypted_body, memoryview) else encrypted_body
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("encrypted body is not bytes")
        decoded = json.loads(cipher.decrypt(bytes(body)).decode("utf-8"))
    except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
        raise InvalidInput("notification payload is invalid") from exc
    if not isinstance(decoded, dict):
        raise InvalidInput("notification payload is invalid")
    subject = decoded.get("subject")
    message_body = decoded.get("body")
    if not isinstance(subject, str) or not isinstance(message_body, str):
        raise InvalidInput("notification payload is invalid")
    if (
        not subject
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in subject)
        or len(subject) > 998
        or len(message_body) > 1024 * 1024
    ):
        raise InvalidInput("notification payload is invalid")
    return subject, message_body


class NotificationStateMixin:
    """Durable outbox state transitions for a repository class."""

    def claim_notification(self, *, key: Any, lease_seconds: int = 120) -> dict[str, Any] | None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 30 <= lease_seconds <= 600
        ):
            raise InvalidInput("lease_seconds must be between 30 and 600")
        cipher = _cipher(key)
        now = utcnow()
        with self.engine.begin() as conn:
            conn.execute(
                update(outbox)
                .where(
                    outbox.c.state == "claimed",
                    outbox.c.lease_until.is_not(None),
                    outbox.c.lease_until <= now,
                )
                .values(
                    state="uncertain",
                    claim_token=None,
                    lease_until=None,
                    last_error=_GENERIC_ERROR,
                )
            )
            due = (
                select(outbox)
                .where(
                    outbox.c.state == "pending",
                    outbox.c.next_attempt_at <= now,
                )
                .order_by(outbox.c.created_at, outbox.c.id)
                .limit(1)
            )
            if self.engine.dialect.name == "postgresql":
                due = due.with_for_update(skip_locked=True)
            row = conn.execute(due).mappings().first()
            if row is None:
                return None

            claim_token = new_id()
            claimed = conn.execute(
                update(outbox)
                .where(outbox.c.id == row["id"], outbox.c.state == "pending")
                .values(
                    state="claimed",
                    claim_token=claim_token,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    attempts=outbox.c.attempts + 1,
                )
            )
            if claimed.rowcount != 1:
                raise Conflict("notification could not be claimed")
            try:
                subject, body = _payload(row["encrypted_body"], cipher)
            except InvalidInput:
                conn.execute(
                    update(outbox)
                    .where(
                        outbox.c.id == row["id"],
                        outbox.c.state == "claimed",
                        outbox.c.claim_token == claim_token,
                    )
                    .values(
                        state="failed",
                        claim_token=None,
                        lease_until=None,
                        last_error=_GENERIC_ERROR,
                    )
                )
                return None

        return {
            "id": row["id"],
            "recipient": row["recipient"],
            "message_id": row["message_id"],
            "subject": subject,
            "body": body,
            "claim_token": claim_token,
        }

    def finish_notification(self, id: Any, claim_token: Any, outcome: Any) -> bool:
        notification_id = identifier(id)
        token = identifier(claim_token)
        if not isinstance(outcome, str) or outcome not in _FINISH_OUTCOMES:
            raise InvalidInput("outcome must be sent, failed, uncertain, or retry")
        now = utcnow()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(outbox)
                .where(outbox.c.id == notification_id)
                .with_for_update()
            ).mappings().first()
            if row is None:
                raise NotFound("notification not found")
            if row["state"] != "claimed" or row["claim_token"] != token:
                raise Conflict("notification claim does not match")

            values: dict[str, Any] = {
                "claim_token": None,
                "lease_until": None,
            }
            if outcome == "sent":
                values.update(state="sent", last_error=None)
            elif outcome == "retry":
                attempts = row["attempts"]
                if attempts < 5:
                    values.update(
                        state="pending",
                        next_attempt_at=now + timedelta(seconds=(2 ** attempts) * 30),
                        last_error=_GENERIC_ERROR,
                    )
                else:
                    values.update(state="failed", last_error=_GENERIC_ERROR)
            else:
                values.update(state=outcome, last_error=_GENERIC_ERROR)

            finished = conn.execute(
                update(outbox)
                .where(
                    outbox.c.id == notification_id,
                    outbox.c.state == "claimed",
                    outbox.c.claim_token == token,
                )
                .values(**values)
            )
            if finished.rowcount != 1:
                raise Conflict("notification claim does not match")
        return True

    def notification_summary(self, entity_kind: Any, id: Any) -> dict[str, Any]:
        kind = text(entity_kind, 32, field="entity_kind")
        entity_id = identifier(id)
        counts = {state: 0 for state in _OUTBOX_STATES}
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(outbox.c.state, func.count(outbox.c.id))
                .where(
                    outbox.c.entity_kind == kind,
                    outbox.c.entity_id == entity_id,
                )
                .group_by(outbox.c.state)
            ).all()
            has_error = conn.execute(
                select(func.count(outbox.c.id)).where(
                    outbox.c.entity_kind == kind,
                    outbox.c.entity_id == entity_id,
                    outbox.c.last_error.is_not(None),
                )
            ).scalar_one()
        for state, count in rows:
            if state in counts:
                counts[state] = int(count)
        return {
            "entity_kind": kind,
            "entity_id": entity_id,
            "counts": counts,
            "last_error": _GENERIC_ERROR if has_error else None,
        }
