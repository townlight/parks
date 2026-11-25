"""Real PostgreSQL concurrency tests for Townlight Parks.

These tests require an actual PostgreSQL database and never remove data.  They
create uniquely-named records, use independent repository connections, and
leave all successfully-created state in place for inspection.

Run with::

    TOWNLIGHT_PARKS_TEST_DATABASE_URL=postgresql+psycopg://... \
        pytest tests/test_postgres_concurrency.py

The environment variable is required; the module is skipped when it is absent.
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from test_catalog_households import HOUSEHOLD, RESOURCE

from townlight_parks import migrations
from townlight_parks.models import bookings, obligations, registrations
from townlight_parks.store import Conflict
from townlight_parks.workflow import ParksRepository

DATABASE_URL = os.environ.get("TOWNLIGHT_PARKS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="TOWNLIGHT_PARKS_TEST_DATABASE_URL is not configured",
)

_ALL_WEEK = {
    day: [["00:00", "24:00"]]
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


@pytest.fixture(scope="module")
def engine():
    """Create a real PostgreSQL engine and ensure the managed schema exists."""
    if not DATABASE_URL:
        pytest.skip("TOWNLIGHT_PARKS_TEST_DATABASE_URL is not configured")

    engine = create_engine(DATABASE_URL, pool_size=4)
    migrations.upgrade(engine)
    assert migrations.ready(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _repository(engine):
    """Build the public repository against the caller-provided engine."""
    return ParksRepository(engine)


def _household(repository, suffix, label):
    return repository.household_create(
        "staff",
        {
            **HOUSEHOLD,
            "contact_name": f"Concurrency {label} {suffix}",
            "email": f"concurrency-{label.lower()}-{suffix}@example.com",
        },
        key=Fernet.generate_key(),
    )


def _program(repository, suffix):
    return repository.catalog_create(
        "program",
        "staff",
        {
            "title": f"Concurrency Program {suffix}",
            "description": "Concurrency capacity program",
            "timezone": "UTC",
            "public": True,
            "min_age": 8,
            "max_age": 15,
        },
    )


def _session(repository, program):
    start = (
        datetime.now(UTC).replace(second=0, microsecond=0)
        + timedelta(days=30)
    )
    return repository.session_create(
        program["id"],
        "staff",
        start,
        start + timedelta(hours=2),
        1,
        50_000,
        "USD",
    )


def _participant(repository, household, name, session, age):
    local_date = session["starts_at"].astimezone(UTC).date()
    birth_date = date(local_date.year - age, 1, 1)
    return repository.participant_create(
        household["id"],
        "staff",
        {"name": name, "birth_date": birth_date.isoformat()},
    )


def _run_nearly_simultaneously(first, second):
    """Run two commands through independent repository connections."""
    barrier = threading.Barrier(2)

    def invoke(command):
        barrier.wait(timeout=10)
        try:
            return "ok", command()
        except Conflict as exc:
            return "conflict", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(invoke, first)
        second_future = executor.submit(invoke, second)
        return (
            first_future.result(timeout=20),
            second_future.result(timeout=20),
        )


def test_identical_public_booking_window_accepts_exactly_one(engine):
    repository = _repository(engine)
    suffix = uuid.uuid4().hex
    resource = repository.catalog_create(
        "resource",
        "staff",
        {
            **RESOURCE,
            "name": f"Concurrency Hall {suffix}",
            "public": True,
            "approval_required": False,
            "hours": dict(_ALL_WEEK),
            "buffer_before_minutes": 15,
            "buffer_after_minutes": 15,
        },
    )
    first_household = _household(repository, suffix, "First")
    second_household = _household(repository, suffix, "Second")
    start = (
        datetime.now(UTC) + timedelta(days=30)
    ).replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    occurrences = [{"starts_at": start, "ends_at": end}]

    first, second = _run_nearly_simultaneously(
        lambda: repository.booking_create(
            resource["id"],
            first_household["id"],
            "staff",
            resource["revision"],
            occurrences,
            f"Race booking first {suffix}",
        ),
        lambda: repository.booking_create(
            resource["id"],
            second_household["id"],
            "staff",
            resource["revision"],
            occurrences,
            f"Race booking second {suffix}",
        ),
    )

    statuses = [first[0], second[0]]
    assert statuses.count("ok") == 1
    assert statuses.count("conflict") == 1

    accepted = first[1] if first[0] == "ok" else second[1]
    assert len(accepted["bookings"]) == 1
    assert accepted["bookings"][0]["state"] in ("confirmed", "pending")

    # Verify the accepted booking exists using a separate actual connection.
    with repository.engine.begin() as conn:
        active_count = conn.execute(
            select(func.count())
            .select_from(bookings)
            .where(
                bookings.c.resource_id == resource["id"],
                bookings.c.state.in_(("pending", "confirmed")),
            )
        ).scalar_one()
    assert active_count == 1


def test_capacity_one_simultaneous_registration_confirms_one_and_cancel_promotes(
    engine,
):
    repository = _repository(engine)
    suffix = uuid.uuid4().hex
    program = _program(repository, suffix)
    session = _session(repository, program)
    household = _household(repository, suffix, "Enrollment")
    first_participant = _participant(
        repository,
        household,
        f"Concurrency First {suffix}",
        session,
        10,
    )
    second_participant = _participant(
        repository,
        household,
        f"Concurrency Second {suffix}",
        session,
        11,
    )

    first, second = _run_nearly_simultaneously(
        lambda: repository.registration_create(
            session["id"],
            first_participant["id"],
            "staff",
            session["revision"],
        ),
        lambda: repository.registration_create(
            session["id"],
            second_participant["id"],
            "staff",
            session["revision"],
        ),
    )

    assert first[0] == second[0] == "ok"
    outcomes = [first[1], second[1]]
    by_state = {outcome["state"]: outcome for outcome in outcomes}
    assert set(by_state) == {"confirmed", "waitlisted"}
    confirmed = by_state["confirmed"]
    waitlisted = by_state["waitlisted"]
    assert confirmed["obligation_id"] is not None
    assert waitlisted["obligation_id"] is None

    registration_ids = [confirmed["id"], waitlisted["id"]]
    with repository.engine.begin() as conn:
        confirmed_count = conn.execute(
            select(func.count())
            .select_from(registrations)
            .where(
                registrations.c.session_id == session["id"],
                registrations.c.state == "confirmed",
            )
        ).scalar_one()
        obligation_rows = conn.execute(
            select(obligations).where(
                obligations.c.entity_kind == "registration",
                obligations.c.entity_id.in_(registration_ids),
            )
        ).mappings().all()
    assert confirmed_count == 1
    assert {row["entity_id"] for row in obligation_rows} == {confirmed["id"]}

    cancelled = repository.registration_cancel(
        confirmed["id"],
        "staff",
        confirmed["revision"],
        f"Concurrency cleanup {suffix}",
    )
    assert cancelled["registration"]["state"] == "cancelled"
    assert [row["id"] for row in cancelled["promoted"]] == [waitlisted["id"]]
    promoted = cancelled["promoted"][0]
    assert promoted["state"] == "confirmed"
    assert promoted["obligation_id"] is not None

    # Confirm the promotion and its obligation using actual PostgreSQL state.
    with repository.engine.begin() as conn:
        promoted_row = conn.execute(
            select(registrations).where(registrations.c.id == waitlisted["id"])
        ).mappings().one()
        promoted_obligation = conn.execute(
            select(obligations.c.id).where(
                obligations.c.entity_kind == "registration",
                obligations.c.entity_id == waitlisted["id"],
            )
        ).scalar_one_or_none()
        final_confirmed_count = conn.execute(
            select(func.count())
            .select_from(registrations)
            .where(
                registrations.c.session_id == session["id"],
                registrations.c.state == "confirmed",
            )
        ).scalar_one()
    assert promoted_row["state"] == "confirmed"
    assert promoted_obligation is not None
    assert final_confirmed_count == 1
