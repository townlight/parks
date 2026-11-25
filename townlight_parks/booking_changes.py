"""Booking state changes, rescheduling, and public availability."""
from collections.abc import Mapping

from sqlalchemy import select

from .models import bookings, ledger, obligations, resources
from .store import Conflict, NotFound, identifier, positive_revision, text, utcnow
from .temporal import closure_conflict, normalize_window, overlapping

_TRANSITIONS = {
    "pending": frozenset(("confirmed", "rejected", "cancelled")),
    "confirmed": frozenset(("cancelled",)),
}
_TERMINAL = frozenset(("cancelled", "rejected"))


def _approval_required(snapshot):
    return (
        isinstance(snapshot, Mapping)
        and snapshot.get("approval_required") is True
    )


class BookingChangesMixin:
    def booking_transition(
        self, booking_id, actor, expected_revision, state, reason, *, _conn=None
    ):
        booking_id = identifier(booking_id)
        actor = text(actor, 128, field="actor")
        expected = positive_revision(expected_revision)
        state = text(state, 16, field="state")
        reason = text(reason, 2000, field="reason")
        with self._transaction(_conn) as conn:
            actual = self._row(conn, bookings, booking_id)
            self._row(conn, resources, actual["resource_id"], lock=True)
            row = self._row(conn, bookings, booking_id, expected, lock=True)
            current = row["state"]
            if current == state and current in _TERMINAL:
                result = dict(row)
                if current == "cancelled":
                    result["financial_obligation"] = self._financial_obligation(
                        conn, booking_id
                    )
                return result
            if current in _TERMINAL or state not in _TRANSITIONS.get(current, ()):
                raise Conflict(
                    f"booking cannot transition from {current} to {state}"
                )
            now, revision = utcnow(), row["revision"] + 1
            conn.execute(
                bookings.update()
                .where(
                    bookings.c.id == booking_id,
                    bookings.c.revision == expected,
                )
                .values(revision=revision, state=state, updated_at=now)
            )
            self._event(
                conn, "booking", booking_id, revision, "booking.transitioned",
                actor, {
                    "from_state": current,
                    "to_state": state,
                    "reason": reason,
                    "resource_id": row["resource_id"],
                },
            )
            result = dict(row)
            result.update(revision=revision, state=state, updated_at=now)
            if state == "cancelled":
                result["financial_obligation"] = self._financial_obligation(
                    conn, booking_id
                )
            return result

    def booking_reschedule(
        self, booking_id, actor, expected_revision, starts_at, ends_at, reason,
        *, _conn=None,
    ):
        booking_id = identifier(booking_id)
        actor = text(actor, 128, field="actor")
        expected = positive_revision(expected_revision)
        reason = text(reason, 2000, field="reason")
        with self._transaction(_conn) as conn:
            actual = self._row(conn, bookings, booking_id)
            resource = self._row(
                conn, resources, actual["resource_id"], lock=True
            )
            row = self._row(conn, bookings, booking_id, expected, lock=True)
            if row["state"] not in ("pending", "confirmed"):
                raise Conflict("only pending or confirmed bookings can move")
            start, end, occupied_from, occupied_until = normalize_window(
                starts_at, ends_at, resource.get("snapshot")
            )
            resource_id = row["resource_id"]
            if closure_conflict(
                conn, resource_id, occupied_from, occupied_until
            ):
                raise Conflict("requested window conflicts with a closure")
            if overlapping(
                conn, bookings, resource_id, occupied_from, occupied_until,
                exclude_id=booking_id,
            ):
                raise Conflict("requested window overlaps another booking")
            previous, revision, now = row["state"], row["revision"] + 1, utcnow()
            new_state = previous
            if previous == "confirmed" and _approval_required(
                resource.get("snapshot")
            ):
                new_state = "pending"
            conn.execute(
                bookings.update()
                .where(
                    bookings.c.id == booking_id,
                    bookings.c.revision == expected,
                )
                .values(
                    revision=revision, state=new_state, starts_at=start,
                    ends_at=end, occupied_from=occupied_from,
                    occupied_until=occupied_until, updated_at=now,
                )
            )
            self._event(
                conn, "booking", booking_id, revision, "booking.rescheduled",
                actor, {
                    "from_state": previous,
                    "to_state": new_state,
                    "starts_at": start.isoformat(),
                    "ends_at": end.isoformat(),
                    "reason": reason,
                    "resource_id": resource_id,
                    "approval_required": new_state == "pending"
                    and previous == "confirmed",
                },
            )
            result = dict(row)
            result.update(
                revision=revision, state=new_state, starts_at=start, ends_at=end,
                occupied_from=occupied_from, occupied_until=occupied_until,
                updated_at=now,
            )
            return result

    def availability(self, resource_id, starts_at, ends_at):
        resource_id = identifier(resource_id)
        with self._transaction(None) as conn:
            resource = self._row(conn, resources, resource_id)
            snapshot = resource.get("snapshot")
            if not isinstance(snapshot, Mapping) or snapshot.get("public") is not True:
                raise NotFound("public resource not found")
            start, end, occupied_from, occupied_until = normalize_window(
                starts_at, ends_at, snapshot
            )
            if closure_conflict(
                conn, resource_id, occupied_from, occupied_until
            ):
                available, reason = False, "closure_conflict"
            elif overlapping(
                conn, bookings, resource_id, occupied_from, occupied_until
            ):
                available, reason = False, "booking_conflict"
            else:
                available, reason = True, "available"
            return {
                "resource_id": resource_id,
                "starts_at": start,
                "ends_at": end,
                "available": available,
                "reason": reason,
                "occupied_from": occupied_from,
                "occupied_until": occupied_until,
            }

    def _financial_obligation(self, conn, booking_id):
        obligation = conn.execute(
            select(obligations).where(
                obligations.c.entity_kind == "booking",
                obligations.c.entity_id == booking_id,
            )
        ).mappings().first()
        if obligation is None:
            return None
        entries = conn.execute(
            select(ledger.c.operation, ledger.c.amount_cents).where(
                ledger.c.obligation_id == obligation["id"]
            )
        ).all()
        paid = sum(amount for operation, amount in entries if operation == "payment")
        refunded = sum(
            amount for operation, amount in entries if operation == "refund"
        )
        pending = max(paid - refunded, 0)
        return {
            "id": obligation["id"],
            "amount_cents": obligation["amount_cents"],
            "deposit_cents": obligation["deposit_cents"],
            "currency": obligation["currency"],
            "pending_refund_cents": pending,
            "refund_pending": pending > 0,
        }
