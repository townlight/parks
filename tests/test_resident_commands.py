from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_catalog_households import HOUSEHOLD, RESOURCE
from test_catalog_households import repo as repo  # noqa: PLC0414

from townlight_parks.models import command_receipts, households
from townlight_parks.store import Conflict, InvalidInput, NotFound, Unauthorized


def _new_household(repository, key, name, email):
    return repository.execute_resident_command(
        str(uuid.uuid4()),
        "create_household",
        {"household": {**HOUSEHOLD, "contact_name": name, "email": email}},
        key=key,
    )


def _new_child(repository, key, household, name, birth_date):
    return repository.execute_resident_command(
        str(uuid.uuid4()),
        "add_participant",
        {
            "tracking_token": household["tracking_token"],
            "participant": {"name": name, "birth_date": birth_date},
        },
        key=key,
    )


def _book(repository, key, household, resource, starts_at, ends_at, purpose):
    result = repository.execute_resident_command(
        str(uuid.uuid4()),
        "create_booking",
        {
            "tracking_token": household["tracking_token"],
            "resource_id": resource["id"],
            "expected_resource_revision": resource["revision"],
            "occurrences": [{"starts_at": starts_at, "ends_at": ends_at}],
            "purpose": purpose,
        },
        key=key,
    )
    return result["bookings"][0]


def test_create_household_replay_receipt_encryption_conflict_and_bad_key(repo):
    key = Fernet.generate_key()
    command_id = str(uuid.uuid4())
    payload = {"household": {**HOUSEHOLD, "email": "replay@example.com"}}
    first = repo.execute_resident_command(
        command_id, "create_household", payload, key=key
    )
    replay = repo.execute_resident_command(
        command_id, "create_household", payload, key=key
    )
    assert replay == first
    assert replay["tracking_token"] == first["tracking_token"]
    with repo.engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(households)
        ).scalar_one() == 1
        receipt = conn.execute(
            select(command_receipts.c.result).where(
                command_receipts.c.command_id == command_id
            )
        ).scalar_one()
    assert set(receipt) == {"encrypted"}
    assert type(receipt["encrypted"]) is str
    serialized = json.dumps(receipt)
    assert payload["household"]["email"] not in serialized
    assert first["tracking_token"] not in serialized
    decrypted = Fernet(key).decrypt(receipt["encrypted"].encode("ascii"))
    assert json.loads(decrypted.decode("utf-8")) == first
    with pytest.raises(Conflict):
        repo.execute_resident_command(
            command_id,
            "create_household",
            {"household": {**HOUSEHOLD, "email": "different@example.com"}},
            key=key,
        )
    with pytest.raises(InvalidInput):
        repo.execute_resident_command(
            str(uuid.uuid4()), "create_household", payload, key=b"bad-key"
        )


def test_resident_booking_read_isolation_failures_and_balances(repo):
    key = Fernet.generate_key()
    own = _new_household(repo, key, "Booking Family", "booking@example.com")
    child = _new_child(repo, key, own, "Booking Child", "2015-05-06")
    public = repo.catalog_create("resource", "staff", {
        **RESOURCE, "name": "Public Hall", "public": True,
        "approval_required": False,
    })
    private = repo.catalog_create("resource", "staff", {
        **RESOURCE, "name": "Private Hall", "public": False,
        "approval_required": False,
    })
    start = (
        datetime.now(UTC) + timedelta(days=10)
    ).replace(hour=12, minute=0, second=0, microsecond=0)
    ends_at = start + timedelta(hours=1)
    booking = _book(
        repo, key, own, public, start.isoformat(), ends_at.isoformat(),
        "Family gathering",
    )
    assert start.isoformat() in json.dumps(booking)
    private_command = str(uuid.uuid4())
    with pytest.raises(NotFound):
        repo.execute_resident_command(
            private_command,
            "create_booking",
            {
                "tracking_token": own["tracking_token"],
                "resource_id": private["id"],
                "expected_resource_revision": private["revision"],
                "occurrences": [{
                    "starts_at": "2030-01-01T10:00:00Z",
                    "ends_at": "2030-01-01T11:00:00Z",
                }],
                "purpose": "Blocked",
            },
            key=key,
        )

    other = _new_household(repo, key, "Other Booking Family", "other-book@example.com")
    other_child = _new_child(repo, key, other, "Other Child", "2014-05-06")
    current_public = repo.catalog_get("resource", public["id"])
    other_booking = _book(
        repo, key, other, current_public,
        (start + timedelta(days=1)).isoformat(),
        (start + timedelta(days=1, hours=1)).isoformat(),
        "Other gathering",
    )
    wrong_command = str(uuid.uuid4())
    with pytest.raises(Unauthorized):
        repo.execute_resident_command(
            wrong_command,
            "cancel_booking",
            {
                "tracking_token": other["tracking_token"],
                "booking_id": booking["id"],
                "expected_revision": booking["revision"],
                "reason": "Not mine",
            },
            key=key,
        )
    with repo.engine.connect() as conn:
        failed_ids = set(conn.execute(
            select(command_receipts.c.command_id).where(
                command_receipts.c.command_id.in_(
                    [private_command, wrong_command]
                )
            )
        ).scalars())
    assert failed_ids == set()

    read = repo.execute_resident_command(
        str(uuid.uuid4()),
        "read_household",
        {"tracking_token": own["tracking_token"]},
        key=key,
    )
    assert read["household"]["id"] == own["id"]
    assert "tracking_hash" not in read["household"]
    assert "encrypted_tracking_token" not in read["household"]
    assert {row["id"] for row in read["participants"]} == {child["id"]}
    assert {row["id"] for row in read["bookings"]} == {booking["id"]}
    assert other_child["id"] not in {row["id"] for row in read["participants"]}
    assert other_booking["id"] not in {row["id"] for row in read["bookings"]}
    assert read["registrations"] == []
    assert {row["entity_id"] for row in read["obligations"]} == {booking["id"]}
    assert read["obligations"]
    obligation = read["obligations"][0]
    obligation_amount_cents = obligation["amount_cents"]
    assert type(obligation_amount_cents) is int
    payment_cents = obligation_amount_cents
    refund_cents = payment_cents // 2
    assert 0 < refund_cents < payment_cents
    payment_idempotency_key = str(uuid.uuid4())
    refund_idempotency_key = str(uuid.uuid4())
    repo.record_offline_payment(
        obligation["id"], "staff", payment_cents, "resident payment",
        payment_idempotency_key,
    )
    repo.record_offline_refund(
        obligation["id"], "staff", refund_cents, "resident partial refund",
        refund_idempotency_key,
    )

    reread = repo.execute_resident_command(
        str(uuid.uuid4()),
        "read_household",
        {"tracking_token": own["tracking_token"]},
        key=key,
    )
    assert reread["household"]["id"] == own["id"]
    assert {row["id"] for row in reread["bookings"]} == {booking["id"]}
    assert {row["entity_id"] for row in reread["obligations"]} == {
        booking["id"]
    }
    obligation = reread["obligations"][0]
    financial = obligation["financial"]
    net_cents = payment_cents - refund_cents
    expected_financial = {
        "paid": payment_cents,
        "refunded": refund_cents,
        "net": net_cents,
        "balance": obligation_amount_cents - net_cents,
    }
    for field, value in expected_financial.items():
        assert type(financial[field]) is int
        assert financial[field] == value
    entries = financial["entries"]
    assert [
        (entry["operation"], entry["amount_cents"], entry["reference"])
        for entry in entries
    ] == [
        ("payment", payment_cents, "resident payment"),
        ("refund", refund_cents, "resident partial refund"),
    ]
    for entry in entries:
        assert type(entry["amount_cents"]) is int
        assert not ({
            "actor", "provider", "provider_id", "idempotency",
            "idempotency_key",
        } & set(entry))
        assert payment_idempotency_key not in json.dumps(entry)
        assert refund_idempotency_key not in json.dumps(entry)


def test_program_promotion_is_hidden_from_resident_cancel_response(repo):
    key = Fernet.generate_key()
    program = repo.catalog_create("program", "staff", {
        "title": "Resident Promotion Camp",
        "description": "After-school park program",
        "timezone": "UTC",
        "public": True,
        "min_age": 8,
        "max_age": 15,
    })
    start = datetime.now(UTC) + timedelta(days=10)
    session = repo.session_create(
        program["id"], "staff", start, start + timedelta(hours=2),
        1, 50_000, "USD",
    )

    def birth_for_age(age):
        local = session["starts_at"].astimezone(UTC).date()
        return date(local.year - age, 1, 1).isoformat()

    first = _new_household(repo, key, "First Resident", "first-resident@example.com")
    second = _new_household(repo, key, "Second Resident", "second-resident@example.com")
    first_child = _new_child(repo, key, first, "First Child", birth_for_age(10))
    second_child = _new_child(repo, key, second, "Second Child", birth_for_age(11))

    def register(household, child):
        return repo.execute_resident_command(
            str(uuid.uuid4()),
            "register",
            {
                "tracking_token": household["tracking_token"],
                "session_id": session["id"],
                "participant_id": child["id"],
                "expected_session_revision": session["revision"],
            },
            key=key,
        )

    confirmed = register(first, first_child)
    waitlisted = register(second, second_child)
    assert confirmed["state"] == "confirmed"
    assert waitlisted["state"] == "waitlisted"

    cancel_id = str(uuid.uuid4())
    cancel_payload = {
        "tracking_token": first["tracking_token"],
        "registration_id": confirmed["id"],
        "expected_revision": confirmed["revision"],
        "reason": "Family change",
    }
    cancelled = repo.execute_resident_command(
        cancel_id, "cancel_registration", cancel_payload, key=key
    )
    assert set(cancelled) == {"registration"}
    response = json.dumps(cancelled, sort_keys=True, separators=(",", ":"))
    for hidden in (second["id"], second_child["id"], waitlisted["id"]):
        assert hidden not in response
    assert "promoted" not in response
    assert repo.registration_get(waitlisted["id"])["state"] == "confirmed"

    replay = repo.execute_resident_command(
        cancel_id, "cancel_registration", cancel_payload, key=key
    )
    assert json.dumps(
        replay, sort_keys=True, separators=(",", ":")
    ) == response
    with repo.engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(command_receipts).where(
                command_receipts.c.command_id == cancel_id
            )
        ).scalar_one() == 1
