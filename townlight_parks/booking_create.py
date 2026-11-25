"""Booking creation, retrieval, and bounded listing."""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select

from .models import bookings, households, obligations, resources
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
from .temporal import closure_conflict, normalize_window, overlapping

__all__ = ["BookingCreateMixin"]


class BookingCreateMixin:
    def booking_create(
        self,
        resource_id,
        household_id,
        actor,
        expected_resource_revision,
        occurrences,
        purpose,
        *,
        _conn=None,
    ):
        resource_id = identifier(resource_id)
        household_id = identifier(household_id)
        actor = text(actor, 128, field="actor")
        purpose = text(purpose, 2000, field="purpose")
        expected_resource_revision = positive_revision(expected_resource_revision)
        if type(occurrences) is not list or not 1 <= len(occurrences) <= 52:
            raise InvalidInput("occurrences must be a list of 1 to 52 items")
        for occurrence in occurrences:
            if type(occurrence) is not dict or set(occurrence) != {
                "starts_at",
                "ends_at",
            }:
                raise InvalidInput(
                    "each occurrence must contain exactly starts_at and ends_at"
                )

        with self._transaction(_conn) as conn:
            # Lock the resource before any availability query or booking insert.
            resource = self._row(
                conn,
                resources,
                resource_id,
                expected_resource_revision,
                lock=True,
            )
            resource_id = resource["id"]
            resource_snapshot = resource["snapshot"]
            if not isinstance(resource_snapshot, Mapping):
                raise InvalidInput("resource snapshot is invalid")
            approval_required = resource_snapshot.get("approval_required", True)
            if not isinstance(approval_required, bool):
                raise InvalidInput("resource approval policy is invalid")
            state = "pending" if approval_required else "confirmed"
            fee_cents = cents(resource_snapshot.get("fee_cents"), "fee_cents")
            deposit_cents = cents(
                resource_snapshot.get("deposit_cents"), "deposit_cents"
            )
            currency = text(
                resource_snapshot.get("currency"), 3, field="currency"
            )
            if (
                len(currency) != 3
                or not currency.isascii()
                or not currency.isalpha()
                or not currency.isupper()
            ):
                raise InvalidInput("currency must be three uppercase letters")

            self._row(conn, households, household_id)

            prepared = []
            for occurrence in occurrences:
                start, end, occupied_from, occupied_until = normalize_window(
                    occurrence["starts_at"],
                    occurrence["ends_at"],
                    resource_snapshot,
                )
                if closure_conflict(
                    conn, resource_id, occupied_from, occupied_until
                ):
                    raise Conflict("occurrence conflicts with a resource closure")
                if overlapping(
                    conn,
                    bookings,
                    resource_id,
                    occupied_from,
                    occupied_until,
                ):
                    raise Conflict("occurrence overlaps an actual booking")
                for earlier in prepared:
                    if (
                        occupied_from < earlier["occupied_until"]
                        and occupied_until > earlier["occupied_from"]
                    ):
                        raise Conflict("occurrences overlap each other")
                prepared.append(
                    {
                        "starts_at": start,
                        "ends_at": end,
                        "occupied_from": occupied_from,
                        "occupied_until": occupied_until,
                    }
                )

            series_id = new_id()
            created_rows = []
            for window in prepared:
                booking_id = new_id()
                at = utcnow()
                values = {
                    "id": booking_id,
                    "resource_id": resource_id,
                    "household_id": household_id,
                    "revision": 1,
                    "series_id": series_id,
                    "starts_at": window["starts_at"],
                    "ends_at": window["ends_at"],
                    "occupied_from": window["occupied_from"],
                    "occupied_until": window["occupied_until"],
                    "state": state,
                    "purpose": purpose,
                    "created_at": at,
                    "updated_at": at,
                }
                conn.execute(bookings.insert().values(**values))
                obligation_id = new_id()
                conn.execute(
                    obligations.insert().values(
                        id=obligation_id,
                        entity_kind="booking",
                        entity_id=booking_id,
                        amount_cents=fee_cents + deposit_cents,
                        deposit_cents=deposit_cents,
                        currency=currency,
                        created_at=at,
                    )
                )
                self._event(
                    conn,
                    "booking",
                    booking_id,
                    1,
                    "booking.created",
                    actor,
                    {
                        "series_id": series_id,
                        "booking_id": booking_id,
                        "resource_id": resource_id,
                        "household_id": household_id,
                        "starts_at": window["starts_at"].isoformat(),
                        "ends_at": window["ends_at"].isoformat(),
                        "purpose": purpose,
                    },
                )
                created_rows.append({**values, "obligation_id": obligation_id})

        return {"series_id": series_id, "bookings": created_rows}

    def booking_get(self, id, *, _conn=None):
        booking_id = identifier(id)
        with self._transaction(_conn) as conn:
            booking = self._row(conn, bookings, booking_id)
            obligation = conn.execute(
                select(obligations).where(
                    obligations.c.entity_kind == "booking",
                    obligations.c.entity_id == booking_id,
                )
            ).mappings().first()
            if obligation is None:
                raise NotFound("booking obligation not found")
            result = dict(booking)
            result["obligation_id"] = obligation["id"]
            return result

    def booking_list(
        self,
        resource_id=None,
        household_id=None,
        limit=50,
        offset=0,
        *,
        _conn=None,
    ):
        resource_id = None if resource_id is None else identifier(resource_id)
        household_id = (
            None if household_id is None else identifier(household_id)
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise InvalidInput("limit must be an integer from 1 to 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidInput("offset must be a nonnegative integer")
        statement = select(bookings)
        if resource_id is not None:
            statement = statement.where(bookings.c.resource_id == resource_id)
        if household_id is not None:
            statement = statement.where(bookings.c.household_id == household_id)
        statement = (
            statement.order_by(bookings.c.starts_at, bookings.c.id)
            .limit(limit)
            .offset(offset)
        )
        with self._transaction(_conn) as conn:
            return [dict(row) for row in conn.execute(statement).mappings()]
