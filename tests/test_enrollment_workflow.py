from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_catalog_households import HOUSEHOLD
from test_catalog_households import repo as repo  # noqa: PLC0414 - pytest fixture re-export

from townlight_parks.models import obligations, registrations
from townlight_parks.store import Conflict, NotFound


def _program(repository, title, *, public=True):
    return repository.catalog_create("program", "staff", {
        "title": title,
        "description": "After-school park program",
        "timezone": "UTC",
        "public": public,
        "min_age": 8,
        "max_age": 15,
    })


def _session(repository, program, capacity=1):
    start = datetime.now(UTC) + timedelta(days=10)
    return repository.session_create(
        program["id"], "staff", start, start + timedelta(hours=2),
        capacity, 50_000, "USD",
    )


def _household(repository, name, email):
    return repository.household_create("staff", {
        **HOUSEHOLD,
        "contact_name": name,
        "email": email,
    }, key=Fernet.generate_key())


def _birth_for_age(session, age):
    local_date = session["starts_at"].astimezone(UTC).date()
    return date(local_date.year - age, 1, 1)


def _participant(repository, household, name, session, age, accommodations=None):
    snapshot = {
        "name": name,
        "birth_date": _birth_for_age(session, age).isoformat(),
    }
    if accommodations is not None:
        snapshot["accommodations"] = accommodations
    return repository.participant_create(household["id"], "staff", snapshot)


def test_waitlist_obligation_cancellation_promotion_and_idempotence(repo):
    program = _program(repo, "FIFO Camp")
    session = _session(repo, program, 1)
    household = _household(repo, "FIFO Family", "fifo@example.com")
    first_child = _participant(repo, household, "First Child", session, 10)
    second_child = _participant(repo, household, "Second Child", session, 11)

    first = repo.registration_create(
        session["id"], first_child["id"], "staff", session["revision"]
    )
    second = repo.registration_create(
        session["id"], second_child["id"], "staff", session["revision"]
    )
    assert first["state"] == "confirmed"
    assert first["obligation_id"] is not None
    assert second["state"] == "waitlisted"
    assert second["obligation_id"] is None
    assert repo.registration_get(second["id"])["obligation_id"] is None

    cancelled = repo.registration_cancel(
        first["id"], "staff", first["revision"], "family change"
    )
    assert cancelled["registration"]["state"] == "cancelled"
    assert [row["id"] for row in cancelled["promoted"]] == [second["id"]]
    promoted = cancelled["promoted"][0]
    assert promoted["state"] == "confirmed"
    assert promoted["obligation_id"] is not None
    assert repo.registration_get(second["id"])["obligation_id"] == promoted["obligation_id"]
    with repo.engine.connect() as conn:
        amount = conn.execute(
            select(obligations.c.amount_cents).where(
                obligations.c.entity_kind == "registration",
                obligations.c.entity_id == second["id"],
            )
        ).scalar_one()
    assert amount == 50_000

    repeated = repo.registration_cancel(
        first["id"], "staff", cancelled["registration"]["revision"],
        "repeat cancellation",
    )
    assert repeated["registration"]["state"] == "cancelled"
    assert repeated["promoted"] == []
    with pytest.raises(Conflict):
        repo.registration_cancel(
            first["id"], "staff", first["revision"], "stale cancellation"
        )


def test_capacity_promotion_checkin_closure_and_public_visibility(repo):
    program = _program(repo, "Public Camp")
    session = _session(repo, program, 1)
    household = _household(repo, "Capacity Family", "capacity@example.com")
    first = _participant(repo, household, "Confirmed Child", session, 10)
    second = _participant(repo, household, "Promoted Child", session, 11)
    third = _participant(repo, household, "Later Child", session, 12)
    fourth = _participant(repo, household, "Closed Child", session, 13)

    confirmed = repo.registration_create(
        session["id"], first["id"], "staff", session["revision"]
    )
    waiting_two = repo.registration_create(
        session["id"], second["id"], "staff", session["revision"]
    )
    waiting_three = repo.registration_create(
        session["id"], third["id"], "staff", session["revision"]
    )
    assert confirmed["state"] == "confirmed"
    assert waiting_two["state"] == waiting_three["state"] == "waitlisted"

    increased = repo.session_update(
        session["id"], "staff", session["revision"], capacity=2
    )
    assert [row["id"] for row in increased["promoted"]] == [waiting_two["id"]]
    promoted = increased["promoted"][0]
    assert promoted["state"] == "confirmed"

    checked = repo.registration_check_in(
        promoted["id"], "staff", promoted["revision"]
    )
    assert isinstance(checked["checked_in_at"], datetime)
    checked_again = repo.registration_check_in(
        promoted["id"], "staff", promoted["revision"]
    )
    checked_at = checked["checked_in_at"]
    checked_again_at = checked_again["checked_in_at"]
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    if checked_again_at.tzinfo is None:
        checked_again_at = checked_again_at.replace(tzinfo=UTC)
    assert checked_again_at.astimezone(UTC) == checked_at.astimezone(UTC)
    with pytest.raises(Conflict):
        repo.registration_check_in(
            waiting_three["id"], "staff", waiting_three["revision"]
        )

    closed = repo.session_update(
        session["id"], "staff", increased["revision"], state="closed"
    )
    with pytest.raises(Conflict):
        repo.registration_create(
            session["id"], fourth["id"], "staff", closed["revision"]
        )

    reopened = repo.session_update(
        session["id"], "staff", closed["revision"], state="open", capacity=3
    )
    assert [row["id"] for row in reopened["promoted"]] == [waiting_three["id"]]
    assert reopened["promoted"][0]["state"] == "confirmed"

    assert repo.session_get(session["id"], public_only=True)["id"] == session["id"]
    private_program = _program(repo, "Private Camp", public=False)
    private_session = _session(repo, private_program, 2)
    with pytest.raises(NotFound):
        repo.session_get(private_session["id"], public_only=True)
    public_ids = {row["id"] for row in repo.session_list(public_only=True)}
    assert session["id"] in public_ids
    assert private_session["id"] not in public_ids


def test_age_block_roster_details_and_household_registration_isolation(repo):
    program = _program(repo, "Roster Camp")
    session = _session(repo, program, 5)
    family = _household(repo, "Roster Family", "roster@example.com")
    child = _participant(
        repo, family, "Roster Child", session, 10, "Wheelchair space"
    )
    adult = _participant(repo, family, "Roster Adult", session, 40)

    with pytest.raises(Conflict):
        repo.registration_create(
            session["id"], adult["id"], "staff", session["revision"]
        )
    family_registration = repo.registration_create(
        session["id"], child["id"], "staff", session["revision"]
    )

    other = _household(repo, "Other Family", "other-roster@example.com")
    other_child = _participant(repo, other, "Other Child", session, 11)
    other_registration = repo.registration_create(
        session["id"], other_child["id"], "staff", session["revision"]
    )

    roster = repo.roster(session["id"])
    family_row = next(
        row for row in roster if row["registration_id"] == family_registration["id"]
    )
    assert family_row["name"] == "Roster Child"
    assert family_row["birth_date"] == _birth_for_age(session, 10).isoformat()
    assert family_row["accommodations"] == "Wheelchair space"
    assert family_row["contact"]["email"] == "roster@example.com"

    family_rows = repo.registrations_for_household(family["id"])
    other_rows = repo.registrations_for_household(other["id"])
    assert {row["id"] for row in family_rows} == {family_registration["id"]}
    assert {row["id"] for row in other_rows} == {other_registration["id"]}


def test_active_edits_capacity_floor_and_reactivation_without_duplicates(repo):
    program = _program(repo, "Reactivation Camp")
    session = _session(repo, program, 2)
    household = _household(repo, "Reactivation Family", "reactivate@example.com")
    first_child = _participant(repo, household, "First Active", session, 10)
    second_child = _participant(repo, household, "Second Active", session, 11)

    first = repo.registration_create(
        session["id"], first_child["id"], "staff", session["revision"]
    )
    second = repo.registration_create(
        session["id"], second_child["id"], "staff", session["revision"]
    )
    assert first["state"] == second["state"] == "confirmed"

    current_child = repo.participant_get(first_child["id"])
    with pytest.raises(Conflict):
        repo.participant_update(first_child["id"], "staff", current_child["revision"], {
            "name": "Changed Active",
            "birth_date": _birth_for_age(session, 9).isoformat(),
        })
    with pytest.raises(Conflict):
        repo.catalog_update(
            "program", program["id"], "staff", program["revision"], {"min_age": 7}
        )

    current_session = repo.session_get(session["id"])
    with pytest.raises(Conflict):
        repo.session_update(
            session["id"], "staff", current_session["revision"], capacity=1
        )

    checked = repo.registration_check_in(first["id"], "staff", first["revision"])
    assert checked["checked_in_at"] is not None
    cancelled = repo.registration_cancel(
        first["id"], "staff", checked["revision"], "temporary withdrawal"
    )
    assert cancelled["registration"]["state"] == "cancelled"

    reactivated = repo.registration_create(
        session["id"], first_child["id"], "staff", current_session["revision"]
    )
    assert reactivated["id"] == first["id"]
    assert reactivated["state"] == "confirmed"
    assert reactivated["checked_in_at"] is None
    assert reactivated["obligation_id"] is not None
    with repo.engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(registrations).where(
                registrations.c.session_id == session["id"],
                registrations.c.participant_id == first_child["id"],
            )
        ).scalar_one()
    assert count == 1
