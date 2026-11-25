from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from test_booking_workflow import _at, _resource
from test_catalog_households import HOUSEHOLD
from test_catalog_households import repo as repo  # noqa: PLC0414

from townlight_parks.models import outbox
from townlight_parks.store import Conflict, utcnow


def test_queue_outbox_encrypts_payload_and_claims_once(repo):
    key = Fernet.generate_key()
    household = repo.household_create("staff", HOUSEHOLD, key=key)
    token = household["tracking_token"]
    resource = _resource(repo, "Public Hall", public=True, approval_required=False)
    start, end = _at()
    created = repo.booking_create(
        resource["id"], household["id"], "staff", resource["revision"],
        [{"starts_at": start, "ends_at": end}], "Queue once",
    )
    booking_id = created["bookings"][0]["id"]
    assert repo.booking_get(booking_id)["state"] == "confirmed"

    base = "http://localhost"
    assert repo.queue_notification_events(key=key, portal_base_url=base) == 1
    assert repo.queue_notification_events(key=key, portal_base_url=base) == 0

    with repo.engine.connect() as conn:
        row = conn.execute(select(outbox)).mappings().one()
    blob = bytes(row["encrypted_body"])
    assert token.encode("ascii") not in blob
    assert HOUSEHOLD["email"].encode() not in blob
    assert b"Your booking" not in blob
    assert b"Public Hall" not in blob

    claimed = repo.claim_notification(key=key)
    assert claimed is not None
    assert claimed["recipient"] == HOUSEHOLD["email"]
    assert claimed["subject"] == "Townlight Parks update"
    assert "is now confirmed" in claimed["body"]
    assert f"{base}/parks/household/{token}/" in claimed["body"]

    assert repo.finish_notification(
        claimed["id"], claimed["claim_token"], "sent"
    ) is True
    summary = repo.notification_summary("booking", booking_id)
    assert summary["counts"] == {
        "pending": 0, "claimed": 0, "sent": 1, "failed": 0, "uncertain": 0,
    }
    assert summary["last_error"] is None
    assert repo.claim_notification(key=key) is None


def test_expired_claim_retry_conflict_and_malformed_payload(repo):
    key = Fernet.generate_key()
    household = repo.household_create("staff", HOUSEHOLD, key=key)
    token = household["tracking_token"]
    resource = _resource(repo, "Public Hall", public=True, approval_required=False)
    for hour in (12, 14):
        start, end = _at(hour=hour)
        repo.booking_create(
            resource["id"], household["id"], "staff", resource["revision"],
            [{"starts_at": start, "ends_at": end}], f"Booking {hour}",
        )
    base = "http://localhost"
    assert repo.queue_notification_events(key=key, portal_base_url=base) == 2

    first = repo.claim_notification(key=key)
    assert first is not None
    with repo.engine.begin() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.id == first["id"])
            .values(lease_until=utcnow() - timedelta(seconds=1))
        )

    second = repo.claim_notification(key=key)
    assert second is not None and second["id"] != first["id"]
    with repo.engine.connect() as conn:
        expired = conn.execute(
            select(outbox).where(outbox.c.id == first["id"])
        ).mappings().one()
    assert expired["state"] == "uncertain"
    assert expired["claim_token"] is None
    assert expired["last_error"] is not None

    with pytest.raises(Conflict):
        repo.finish_notification(
        second["id"], "00000000-0000-0000-0000-000000000000", "sent"
    )
    assert repo.finish_notification(
        second["id"], second["claim_token"], "retry"
    ) is True
    with repo.engine.connect() as conn:
        retried = conn.execute(
            select(outbox).where(outbox.c.id == second["id"])
        ).mappings().one()
    assert retried["state"] == "pending"
    assert retried["attempts"] == 1
    assert retried["next_attempt_at"] is not None
    assert repo.claim_notification(key=key) is None

    with repo.engine.begin() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.id == second["id"])
            .values(
                encrypted_body=b"malformed",
                next_attempt_at=utcnow() - timedelta(seconds=1),
            )
        )
    start, end = _at(hour=16)
    repo.booking_create(
        resource["id"], household["id"], "staff", resource["revision"],
        [{"starts_at": start, "ends_at": end}], "Valid after malformed",
    )
    assert repo.queue_notification_events(key=key, portal_base_url=base) == 1

    assert repo.claim_notification(key=key) is None
    with repo.engine.connect() as conn:
        failed = conn.execute(
            select(outbox).where(outbox.c.id == second["id"])
        ).mappings().one()
    assert failed["state"] == "failed"
    assert failed["claim_token"] is None
    assert failed["last_error"] is not None

    valid = repo.claim_notification(key=key)
    assert valid is not None
    assert valid["subject"] == "Townlight Parks update"
    assert f"{base}/parks/household/{token}/" in valid["body"]
    assert repo.finish_notification(
        valid["id"], valid["claim_token"], "sent"
    ) is True
    with repo.engine.connect() as conn:
        still_uncertain = conn.execute(
            select(outbox).where(outbox.c.id == first["id"])
        ).mappings().one()
    assert still_uncertain["state"] == "uncertain"
