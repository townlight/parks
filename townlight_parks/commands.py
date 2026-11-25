"""Atomic resident command dispatch with encrypted replay receipts."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, or_, select
from sqlalchemy import text as sql_text

from .models import (
    bookings,
    command_receipts,
    households,
    ledger,
    obligations,
    participants,
    payment_intents,
    programs,
    registrations,
    resources,
    sessions,
)
from .store import (
    Conflict,
    InvalidInput,
    NotFound,
    Unauthorized,
    canonical_bytes,
    identifier,
    text,
    utcnow,
)

__all__ = ["ResidentCommandMixin"]

_REQUIRED = {
    "availability": frozenset({"resource_id", "starts_at", "ends_at"}),
    "create_household": frozenset({"household"}),
    "read_household": frozenset({"tracking_token"}),
    "add_participant": frozenset({"tracking_token", "participant"}),
    "update_participant": frozenset({
        "tracking_token", "participant_id", "expected_revision", "participant"
    }),
    "create_booking": frozenset({
        "tracking_token", "resource_id", "expected_resource_revision",
        "occurrences", "purpose",
    }),
    "cancel_booking": frozenset({
        "tracking_token", "booking_id", "expected_revision", "reason"
    }),
    "reschedule_booking": frozenset({
        "tracking_token", "booking_id", "expected_revision", "starts_at",
        "ends_at", "reason",
    }),
    "register": frozenset({
        "tracking_token", "session_id", "participant_id",
        "expected_session_revision",
    }),
    "cancel_registration": frozenset({
        "tracking_token", "registration_id", "expected_revision", "reason"
    }),
    "prepare_checkout": frozenset({"tracking_token", "obligation_id"}),
    "read_checkout": frozenset({"tracking_token", "intent_id"}),
}


def _exact(payload, keys):
    if type(payload) is not dict:
        raise InvalidInput("payload must be a JSON object")
    if set(payload) != set(keys):
        raise InvalidInput("payload keys do not match the operation")


def _safe(value):
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidInput("result contains a non-finite number")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidInput("result JSON keys must be text")
            result[str(key)] = _safe(item)
        return result
    if type(value) in (list, tuple):
        return [_safe(item) for item in value]
    raise InvalidInput("result is not JSON serializable")


def _time(value):
    value = text(value, 64, field="timestamp")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInput("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidInput("timestamp must include a timezone")
    return parsed


def _resolve(conn, token):
    if type(token) is not str:
        raise Unauthorized("invalid tracking token")
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise Unauthorized("invalid tracking token") from exc
    if not 32 <= len(raw) <= 128:
        raise Unauthorized("invalid tracking token")
    digest = hashlib.sha256(raw).hexdigest()
    row = conn.execute(
        select(households).where(households.c.tracking_hash == digest)
    ).mappings().first()
    if row is None or not hmac.compare_digest(row["tracking_hash"], digest):
        raise Unauthorized("invalid tracking token")
    return dict(row)


def _owned_participant(conn, household_id, participant_id):
    row = conn.execute(
        select(participants).where(
            participants.c.id == identifier(participant_id)
        )
    ).mappings().first()
    if row is None:
        raise NotFound("participant not found")
    if row["household_id"] != household_id:
        raise Unauthorized("participant is not owned by this household")
    return dict(row)


def _owned_booking(conn, household_id, booking_id):
    row = conn.execute(
        select(bookings).where(bookings.c.id == identifier(booking_id))
    ).mappings().first()
    if row is None:
        raise NotFound("booking not found")
    if row["household_id"] != household_id:
        raise Unauthorized("booking is not owned by this household")
    return dict(row)


def _public_resource(conn, resource_id):
    row = conn.execute(
        select(resources).where(resources.c.id == identifier(resource_id))
    ).mappings().first()
    if row is None:
        raise NotFound("resource not found")
    snapshot = row["snapshot"]
    if not isinstance(snapshot, Mapping) or snapshot.get("public") is not True:
        raise NotFound("public resource not found")
    return row


def _origin_tuple(value):
    if type(value) is not str:
        raise Unauthorized("public origin is required")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise Unauthorized("public origin is invalid") from exc
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise Unauthorized("public origin must be an HTTPS origin")
    return parts.hostname.lower(), port or 443


def _require_public_origin(value, configured):
    if configured is None:
        raise Unauthorized("public origin is not configured")
    if _origin_tuple(value) != _origin_tuple(configured):
        raise Unauthorized("public origin does not match configuration")
    return value.rstrip("/")


def _owned_obligation(conn, household_id, obligation_id):
    row = conn.execute(
        select(obligations).where(
            obligations.c.id == identifier(obligation_id)
        )
    ).mappings().first()
    if row is None:
        raise NotFound("obligation not found")
    obligation = dict(row)
    if obligation["entity_kind"] == "booking":
        _owned_booking(conn, household_id, obligation["entity_id"])
    elif obligation["entity_kind"] == "registration":
        registration = conn.execute(
            select(registrations).where(
                registrations.c.id == identifier(obligation["entity_id"])
            )
        ).mappings().first()
        if registration is None:
            raise NotFound("registration not found")
        _owned_participant(conn, household_id, registration["participant_id"])
    else:
        raise Unauthorized("obligation is not owned by this household")
    return obligation


def _financial_receipt(conn, booking_ids, registration_ids):
    clauses = []
    if booking_ids:
        clauses.append(and_(
            obligations.c.entity_kind == "booking",
            obligations.c.entity_id.in_(booking_ids),
        ))
    if registration_ids:
        clauses.append(and_(
            obligations.c.entity_kind == "registration",
            obligations.c.entity_id.in_(registration_ids),
        ))
    if not clauses:
        return []
    rows = conn.execute(
        select(obligations)
        .where(or_(*clauses))
        .order_by(obligations.c.created_at, obligations.c.id)
    ).mappings().all()
    result = [dict(row) for row in rows]
    if not result:
        return result
    entries = {}
    for row in conn.execute(
        select(ledger)
        .where(ledger.c.obligation_id.in_([item["id"] for item in result]))
        .order_by(ledger.c.created_at, ledger.c.id)
    ).mappings():
        item = dict(row)
        safe_entry = {}
        for key in (
            "id", "operation", "amount_cents", "method", "reference",
            "created_at",
        ):
            if key in item:
                safe_entry[key] = _safe(item[key])
        entries.setdefault(item["obligation_id"], []).append(safe_entry)
    for item in result:
        safe_entries = entries.get(item["id"], [])
        paid = 0
        refunded = 0
        for entry in safe_entries:
            amount_cents = entry.get("amount_cents")
            if type(amount_cents) is not int:
                continue
            operation = entry.get("operation")
            if operation == "payment":
                paid += amount_cents
            elif operation == "refund":
                refunded += amount_cents
        net = paid - refunded
        obligation_amount_cents = item.get("amount_cents")
        balance = (
            obligation_amount_cents - net
            if type(obligation_amount_cents) is int
            else net
        )
        item["financial"] = {
            "paid": paid,
            "refunded": refunded,
            "net": net,
            "balance": balance,
            "entries": safe_entries,
        }
        if safe_entries and "reference" in safe_entries[-1]:
            item["reference"] = safe_entries[-1]["reference"]
    return result


def _read_household(conn, household):
    household_id = household["id"]
    public = {
        key: value for key, value in household.items()
        if key not in ("tracking_hash", "encrypted_tracking_token")
    }
    participants_rows = [
        dict(row) for row in conn.execute(
            select(participants)
            .where(participants.c.household_id == household_id)
            .order_by(participants.c.created_at, participants.c.id)
            .limit(1001)
        ).mappings()
    ]
    if len(participants_rows) > 1000:
        raise InvalidInput("household has too many participants")
    bookings_rows = [
        dict(row) for row in conn.execute(
            select(bookings)
            .where(bookings.c.household_id == household_id)
            .order_by(bookings.c.starts_at, bookings.c.id)
            .limit(1001)
        ).mappings()
    ]
    if len(bookings_rows) > 1000:
        raise InvalidInput("household has too many bookings")
    registration_rows = [
        dict(row) for row in conn.execute(
            select(registrations)
            .join(
                participants,
                registrations.c.participant_id == participants.c.id,
            )
            .where(participants.c.household_id == household_id)
            .order_by(registrations.c.session_id, registrations.c.id)
            .limit(1001)
        ).mappings()
    ]
    if len(registration_rows) > 1000:
        raise InvalidInput("household has too many registrations")
    obligations_rows = _financial_receipt(
        conn,
        [row["id"] for row in bookings_rows],
        [row["id"] for row in registration_rows],
    )
    intent_rows = []
    if obligations_rows:
        intent_rows = [
            dict(row) for row in conn.execute(
                select(payment_intents)
                .where(payment_intents.c.obligation_id.in_(
                    [row["id"] for row in obligations_rows]
                ))
                .order_by(payment_intents.c.created_at, payment_intents.c.id)
                .limit(1001)
            ).mappings()
        ]
        if len(intent_rows) > 1000:
            raise InvalidInput("household has too many payment intents")
    intent_fields = (
        "id", "obligation_id", "state", "amount_cents", "currency",
        "checkout_url", "last_error",
    )
    return {
        "household": public,
        "participants": participants_rows,
        "bookings": bookings_rows,
        "registrations": registration_rows,
        "obligations": obligations_rows,
        "payment_intents": [
            {key: row[key] for key in intent_fields} for row in intent_rows
        ],
    }


class ResidentCommandMixin:
    """Resident-facing, transactionally receipted Parks commands."""

    def execute_resident_command(self, command_id, operation, payload, *, key, public_origin=None):
        command_id = identifier(command_id)
        operation = text(operation, 128, field="operation")
        if operation not in _REQUIRED:
            raise InvalidInput("unsupported resident operation")
        _exact(payload, _REQUIRED[operation])
        encoded = canonical_bytes(payload)
        if len(encoded) > 64 * 1024:
            raise InvalidInput("payload must be at most 64 KiB")
        fingerprint = hashlib.sha256(
            operation.encode("utf-8") + b"\0" + encoded
        ).hexdigest()
        try:
            cipher = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise InvalidInput("key must be a valid Fernet key") from exc

        with self._transaction() as conn:
            self._lock_command(conn, command_id)
            receipt = conn.execute(
                select(command_receipts)
                .where(command_receipts.c.command_id == command_id)
                .with_for_update()
            ).mappings().first()
            if receipt is not None:
                if (
                    receipt["operation"] != operation
                    or not hmac.compare_digest(
                        receipt["payload_sha256"], fingerprint
                    )
                ):
                    raise Conflict("command id already used for another request")
                try:
                    token = receipt["result"]["encrypted"].encode("ascii")
                    return json.loads(cipher.decrypt(token).decode("utf-8"))
                except (
                    KeyError, TypeError, UnicodeError, ValueError, InvalidToken,
                ) as exc:
                    raise Conflict("stored command receipt is invalid") from exc

            result = _safe(self._dispatch(
                conn, command_id, operation, payload, key, public_origin
            ))
            token = cipher.encrypt(canonical_bytes(result)).decode("ascii")
            conn.execute(command_receipts.insert().values(
                command_id=command_id,
                operation=operation,
                payload_sha256=fingerprint,
                result={"encrypted": token},
                created_at=utcnow(),
            ))
            return result

    @staticmethod
    def _lock_command(conn, command_id):
        if conn.dialect.name != "postgresql":
            return
        value = int(command_id.replace("-", "")[:16], 16)
        if value >= 1 << 63:
            value -= 1 << 64
        conn.execute(
            sql_text("SELECT pg_advisory_xact_lock(:key)"), {"key": value}
        )

    def _dispatch(self, conn, command_id, operation, payload, key, public_origin):
        if operation == "create_household":
            return self.household_create(
                "resident", payload["household"], key=key, _conn=conn
            )
        if operation == "availability":
            _public_resource(conn, payload["resource_id"])
            return self.availability(
                payload["resource_id"], _time(payload["starts_at"]),
                _time(payload["ends_at"]),
            )
        household = _resolve(conn, payload["tracking_token"])
        household_id = household["id"]
        actor = f"resident:{household_id}"

        if operation == "read_household":
            return _read_household(conn, household)
        if operation == "prepare_checkout":
            obligation = _owned_obligation(
                conn, household_id, payload["obligation_id"]
            )
            configured = getattr(self, "public_origin", None)
            if configured is None:
                configured = getattr(self, "_public_origin", None)
            origin = _require_public_origin(public_origin, configured)
            result = self.checkout_prepare(
                obligation["id"], actor, command_id,
                origin + "/parks/payments/return/",
                origin + "/parks/household/", _conn=conn,
            )
            return {key: result[key] for key in (
                "id", "state", "amount_cents", "currency", "checkout_url",
            )}
        if operation == "read_checkout":
            intent = conn.execute(
                select(payment_intents).where(
                    payment_intents.c.id == identifier(payload["intent_id"])
                )
            ).mappings().first()
            if intent is None:
                raise NotFound("payment intent not found")
            _owned_obligation(conn, household_id, intent["obligation_id"])
            return {key: intent[key] for key in (
                "id", "obligation_id", "state", "amount_cents", "currency",
                "checkout_url", "last_error",
            )}
        if operation == "add_participant":
            return self.participant_create(
                household_id, actor, payload["participant"], _conn=conn
            )
        if operation == "update_participant":
            _owned_participant(
                conn, household_id, payload["participant_id"]
            )
            return self.participant_update(
                payload["participant_id"], actor, payload["expected_revision"],
                payload["participant"], _conn=conn,
            )
        if operation == "create_booking":
            _public_resource(conn, payload["resource_id"])
            occurrences = payload["occurrences"]
            if type(occurrences) is not list:
                raise InvalidInput("occurrences must be a list")
            parsed = []
            for occurrence in occurrences:
                if type(occurrence) is not dict or set(occurrence) != {
                    "starts_at", "ends_at"
                }:
                    raise InvalidInput("occurrence keys are invalid")
                parsed.append({
                    "starts_at": _time(occurrence["starts_at"]),
                    "ends_at": _time(occurrence["ends_at"]),
                })
            return self.booking_create(
                payload["resource_id"], household_id, actor,
                payload["expected_resource_revision"], parsed,
                payload["purpose"], _conn=conn,
            )
        if operation == "cancel_booking":
            _owned_booking(conn, household_id, payload["booking_id"])
            return self.booking_transition(
                payload["booking_id"], actor, payload["expected_revision"],
                "cancelled", payload["reason"], _conn=conn,
            )
        if operation == "reschedule_booking":
            _owned_booking(conn, household_id, payload["booking_id"])
            return self.booking_reschedule(
                payload["booking_id"], actor, payload["expected_revision"],
                _time(payload["starts_at"]), _time(payload["ends_at"]),
                payload["reason"], _conn=conn,
            )
        if operation == "register":
            session = conn.execute(
                select(sessions).where(
                    sessions.c.id == identifier(payload["session_id"])
                )
            ).mappings().first()
            if session is None:
                raise NotFound("session not found")
            program = conn.execute(
                select(programs).where(
                    programs.c.id == session["program_id"]
                )
            ).mappings().first()
            if program is None:
                raise NotFound("program not found")
            snapshot = program["snapshot"]
            if not isinstance(snapshot, Mapping) or snapshot.get("public") is not True:
                raise NotFound("public program not found")
            _owned_participant(conn, household_id, payload["participant_id"])
            return self.registration_create(
                payload["session_id"], payload["participant_id"], actor,
                payload["expected_session_revision"], _conn=conn,
            )
        if operation == "cancel_registration":
            registration = conn.execute(
                select(registrations).where(
                    registrations.c.id == identifier(
                        payload["registration_id"]
                    )
                )
            ).mappings().first()
            if registration is None:
                raise NotFound("registration not found")
            _owned_participant(
                conn, household_id, registration["participant_id"]
            )
            result = self.registration_cancel(
                payload["registration_id"], actor, payload["expected_revision"],
                payload["reason"], _conn=conn,
            )
            return {"registration": result["registration"]}
        raise InvalidInput("unsupported resident operation")


