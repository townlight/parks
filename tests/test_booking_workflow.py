from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_catalog_households import HOUSEHOLD, RESOURCE
from test_catalog_households import repo as repo  # noqa: PLC0414 - pytest fixture re-export

from townlight_parks.models import bookings, obligations
from townlight_parks.store import Conflict, InvalidInput, NotFound

LIMITED = {
    day: [["09:00", "17:00"]]
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


def _at(hour=12, minutes=60):
    start = datetime.now(UTC).replace(
        hour=12, minute=0, second=0, microsecond=0
    ) + timedelta(days=10)
    start = start.replace(hour=hour)
    return start, start + timedelta(minutes=minutes)


def _resource(repo, name, **overrides):
    data = {
        **RESOURCE,
        "name": name,
        "buffer_before_minutes": 15,
        "buffer_after_minutes": 15,
        **overrides,
    }
    return repo.catalog_create("resource", "staff", data)


def _household(repo):
    return repo.household_create("staff", HOUSEHOLD, key=Fernet.generate_key())


def _db_state(repo, resource_id):
    with repo.engine.connect() as conn:
        booking_rows = conn.execute(
            select(bookings).where(bookings.c.resource_id == resource_id)
        ).mappings().all()
        obligation_rows = conn.execute(
            select(obligations).where(
                obligations.c.entity_kind == "booking",
                obligations.c.entity_id.in_([r["id"] for r in booking_rows]),
            )
        ).mappings().all()
    return booking_rows, obligation_rows


def test_booking_create_persists_two_occurrences_and_full_obligation(repo):
    resource = _resource(repo, "Hall A")
    household = _household(repo)
    start, end = _at()
    occurrences = [
        {"starts_at": start, "ends_at": end},
        {
            "starts_at": start + timedelta(hours=2),
            "ends_at": start + timedelta(hours=3),
        },
    ]
    created = repo.booking_create(
        resource["id"], household["id"], "staff", resource["revision"],
        occurrences, "Birthday",
    )
    assert len(created["bookings"]) == 2
    assert len({b["series_id"] for b in created["bookings"]}) == 1
    rows, obligation_rows = _db_state(repo, resource["id"])
    assert len(rows) == 2
    assert {r["series_id"] for r in rows} == {created["series_id"]}
    assert {r["state"] for r in rows} == {"pending"}
    assert len(obligation_rows) == 2
    assert {r["id"] for r in obligation_rows} == {
        b["obligation_id"] for b in created["bookings"]
    }
    assert all(
        r["amount_cents"] == 120 and r["deposit_cents"] == 20
        for r in obligation_rows
    )
    assert all(r["currency"] == "USD" for r in obligation_rows)


def test_buffered_overlap_conflicts_and_recurrence_batch_is_atomic(repo):
    resource = _resource(repo, "Hall B")
    household = _household(repo)
    start, end = _at()
    repo.booking_create(
        resource["id"], household["id"], "staff", resource["revision"],
        [{"starts_at": start, "ends_at": end}], "First booking",
    )
    before_bookings, before_obligations = _db_state(repo, resource["id"])
    with pytest.raises(Conflict):
        repo.booking_create(
            resource["id"], household["id"], "staff", resource["revision"],
            [{"starts_at": start + timedelta(minutes=70),
              "ends_at": start + timedelta(minutes=120)}],
            "Buffer overlap",
        )
    free = {
        "starts_at": start + timedelta(hours=4),
        "ends_at": start + timedelta(hours=5),
    }
    with pytest.raises(Conflict):
        repo.booking_create(
            resource["id"], household["id"], "staff", resource["revision"],
            [free, {"starts_at": start + timedelta(minutes=70),
                    "ends_at": start + timedelta(minutes=120)}],
            "Atomic recurrence batch",
        )
    after_bookings, after_obligations = _db_state(repo, resource["id"])
    assert len(after_bookings) == len(before_bookings) == 1
    assert len(after_obligations) == len(before_obligations) == 1


def test_approval_reschedule_stale_cancel_closure_and_private_availability(repo):
    public = _resource(repo, "Public Hall", public=True, approval_required=True)
    private = _resource(repo, "Private Hall")
    household = _household(repo)
    start, end = _at()
    created = repo.booking_create(
        public["id"], household["id"], "staff", public["revision"],
        [{"starts_at": start, "ends_at": end}], "Approval flow",
    )
    booking_id = created["bookings"][0]["id"]
    obligation_id = repo.booking_get(booking_id)["obligation_id"]
    confirmed = repo.booking_transition(
        booking_id, "staff", 1, "confirmed", "approved"
    )
    assert confirmed["state"] == "confirmed"
    moved_start, moved_end = _at(hour=14)
    moved = repo.booking_reschedule(
        booking_id, "staff", confirmed["revision"], moved_start, moved_end,
        "later",
    )
    assert moved["state"] == "pending"
    assert repo.booking_get(booking_id)["obligation_id"] == obligation_id
    with pytest.raises(Conflict):
        repo.booking_reschedule(
            booking_id, "staff", confirmed["revision"], *_at(hour=16), "stale"
        )
    before_cancel = repo.availability(public["id"], moved_start, moved_end)
    assert before_cancel["available"] is False
    cancelled = repo.booking_transition(
        booking_id, "staff", moved["revision"], "cancelled", "released"
    )
    assert cancelled["state"] == "cancelled"
    assert cancelled["financial_obligation"]["id"] == obligation_id
    after_cancel = repo.availability(public["id"], moved_start, moved_end)
    assert after_cancel["available"] is True
    closure_start, closure_end = _at(hour=11, minutes=50)
    repo.closure_add(
        public["id"], "staff", public["revision"], closure_start, closure_end,
        "maintenance",
    )
    blocked = repo.availability(public["id"], start, end)
    assert blocked["available"] is False
    assert blocked["reason"] == "closure_conflict"
    with pytest.raises(NotFound):
        repo.availability(private["id"], start, end)


def test_naive_and_outside_hours_windows_are_rejected(repo):
    resource = _resource(
        repo, "Limited Hall", hours=LIMITED, public=True,
        approval_required=False,
    )
    household = _household(repo)
    late_start, late_end = _at(hour=18)
    with pytest.raises(InvalidInput):
        repo.booking_create(
            resource["id"], household["id"], "staff", resource["revision"],
            [{"starts_at": late_start, "ends_at": late_end}], "Too late",
        )
    naive_start, naive_end = _at()
    naive_start = naive_start.replace(tzinfo=None)
    naive_end = naive_end.replace(tzinfo=None)
    with pytest.raises(InvalidInput):
        repo.booking_create(
            resource["id"], household["id"], "staff", resource["revision"],
            [{"starts_at": naive_start, "ends_at": naive_end}], "Naive",
        )
    rows, obligation_rows = _db_state(repo, resource["id"])
    assert rows == []
    assert obligation_rows == []
