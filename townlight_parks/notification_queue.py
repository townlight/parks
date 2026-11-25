"""Notification queue priming for the Parks domain."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import exists, literal, select

from .models import (
    bookings,
    events,
    households,
    outbox,
    participants,
    programs,
    registrations,
    resources,
    sessions,
)
from .store import InvalidInput, canonical_bytes, new_id, utcnow

_EVENT_KINDS = (
    "booking.created",
    "booking.transitioned",
    "booking.rescheduled",
    "registration.created",
    "registration.promoted",
    "registration.cancelled",
    "registration.reactivated",
)


class NotificationQueueMixin:
    """Create pending outbox messages for queued domain events."""

    def queue_notification_events(self, *, key, portal_base_url, limit=50):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise InvalidInput("limit must be an integer from 1 to 50")
        if not isinstance(portal_base_url, str):
            raise InvalidInput("portal_base_url must be text")
        if len(portal_base_url) > 2048 or any(
            ord(ch) < 32 or ord(ch) == 127 for ch in portal_base_url
        ):
            raise InvalidInput("portal_base_url is invalid")
        parsed = urlsplit(portal_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise InvalidInput("portal_base_url must be an absolute HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidInput("portal_base_url must not contain credentials")
        if parsed.fragment or parsed.query:
            raise InvalidInput("portal_base_url must not contain a query or fragment")
        if parsed.scheme == "http":
            host = parsed.hostname.lower()
            if host != "localhost":
                try:
                    is_loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    is_loopback = False
                if not is_loopback:
                    raise InvalidInput("HTTP portal_base_url must use a loopback host")
        base = portal_base_url.rstrip("/")
        try:
            fernet = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise InvalidInput("key must be a Fernet key") from exc

        queued = 0
        with self._transaction() as conn:
            already_queued = exists(
                select(outbox.c.id).where(
                    outbox.c.message_id
                    == literal("<parks-")
                    + events.c.id
                    + literal("@townlight.invalid>")
                )
            )
            statement = (
                select(events)
                .where(
                    events.c.entity_kind.in_(("booking", "registration")),
                    events.c.kind.in_(_EVENT_KINDS),
                    ~already_queued,
                )
                .order_by(events.c.created_at, events.c.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            selected = conn.execute(statement).mappings().all()
            for event in selected:
                if event["entity_kind"] == "booking":
                    entity = self._row(conn, bookings, event["entity_id"])
                    household_id = entity["household_id"]
                    resource = self._row(conn, resources, entity["resource_id"])
                    snapshot = resource.get("snapshot") or {}
                    title = snapshot.get("title") or snapshot.get("name")
                    if not isinstance(title, str) or not title:
                        title = "your booking"
                    state = entity["state"]
                    noun = "booking"
                elif event["entity_kind"] == "registration":
                    entity = self._row(conn, registrations, event["entity_id"])
                    participant = self._row(
                        conn, participants, entity["participant_id"]
                    )
                    household_id = participant["household_id"]
                    session = self._row(conn, sessions, entity["session_id"])
                    program = self._row(conn, programs, session["program_id"])
                    snapshot = program.get("snapshot") or {}
                    title = snapshot.get("title") or snapshot.get("name")
                    if not isinstance(title, str) or not title:
                        title = "your program"
                    state = entity["state"]
                    noun = "registration"
                else:
                    raise InvalidInput("unsupported notification entity")

                household = self._row(conn, households, household_id)
                household_snapshot = household.get("snapshot") or {}
                recipient = household_snapshot.get("email")
                if (
                    not isinstance(recipient, str)
                    or not recipient
                    or len(recipient) > 320
                    or recipient.count("@") != 1
                    or any(
                        ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                        for ch in recipient
                    )
                ):
                    raise InvalidInput("household email is invalid")
                local, domain = recipient.split("@", 1)
                if not local or not domain or "." not in domain:
                    raise InvalidInput("household email is invalid")

                encrypted_token = household.get("encrypted_tracking_token")
                try:
                    token_bytes = fernet.decrypt(bytes(encrypted_token))
                    token = token_bytes.decode("utf-8")
                except (InvalidToken, TypeError, ValueError, UnicodeDecodeError) as exc:
                    raise InvalidInput("household tracking token is invalid") from exc
                if (
                    not token
                    or any(
                        ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                        for ch in token
                    )
                    or any(ch in token for ch in "/?#")
                ):
                    raise InvalidInput("household tracking token is invalid")

                subject = "Townlight Parks update"
                body = (
                    f"Your {noun} for {title} is now {state}.\n\n"
                    f"View your private household page:\n"
                    f"{base}/parks/household/{token}/\n"
                )
                encrypted_body = fernet.encrypt(
                    canonical_bytes({"subject": subject, "body": body})
                )
                now = utcnow()
                conn.execute(
                    outbox.insert().values(
                        id=new_id(),
                        entity_kind=event["entity_kind"],
                        entity_id=event["entity_id"],
                        recipient=recipient,
                        encrypted_body=encrypted_body,
                        message_id=f"<parks-{event['id']}@townlight.invalid>",
                        state="pending",
                        claim_token=None,
                        lease_until=None,
                        attempts=0,
                        next_attempt_at=now,
                        last_error=None,
                        created_at=now,
                    )
                )
                queued += 1
        return queued
