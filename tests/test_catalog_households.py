from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, select

from townlight_parks import migrations
from townlight_parks.models import households
from townlight_parks.store import (
    Conflict,
    InvalidInput,
    NotFound,
    RepositoryBase,
    Unauthorized,
)
from townlight_parks.workflow import ParksRepository

HOURS = {
    day: [["00:00", "24:00"]]
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}
RESOURCE = {
    "name": "Community Hall",
    "address": "1 Park Lane",
    "timezone": "UTC",
    "description": "Neighborhood room",
    "amenities": ["restroom", "parking"],
    "accessibility": "Step-free entry and accessible restroom",
    "hours": HOURS,
    "public": False,
    "approval_required": True,
    "buffer_before_minutes": 0,
    "buffer_after_minutes": 0,
    "fee_cents": 100,
    "deposit_cents": 20,
    "currency": "USD",
}
HOUSEHOLD = {
    "contact_name": "Ada Lovelace",
    "email": "ada@example.com",
    "phone": None,
    "address": None,
    "language": "en",
}


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parks.sqlite3'}")
    migrations.upgrade(engine)
    repository = ParksRepository(engine)
    yield repository
    engine.dispose()


def test_missing_migration_raises_then_upgrade_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
    with pytest.raises(migrations.MigrationError):
        RepositoryBase(engine)
    assert not inspect(engine).has_table("resources")
    migrations.upgrade(engine)
    migrations.upgrade(engine)
    assert migrations.ready(engine)
    ParksRepository(engine)
    engine.dispose()


def test_resource_visibility_pagination_revision_closure_and_reopen(repo):
    private = repo.catalog_create("resource", "staff", {**RESOURCE, "name": "Private"})
    first = repo.catalog_create(
        "resource", "staff", {**RESOURCE, "name": "Public A", "public": True}
    )
    second = repo.catalog_create(
        "resource", "staff", {**RESOURCE, "name": "Public B", "public": True}
    )
    listed = repo.catalog_list("resource", public_only=True)
    assert {row["id"] for row in listed} == {first["id"], second["id"]}
    page0 = repo.catalog_list("resource", public_only=True, limit=1, offset=0)
    page1 = repo.catalog_list("resource", public_only=True, limit=1, offset=1)
    assert {page0[0]["id"], page1[0]["id"]} == {first["id"], second["id"]}
    with pytest.raises(NotFound):
        repo.catalog_get("resource", private["id"], public_only=True)
    with pytest.raises(Conflict):
        repo.catalog_update("resource", private["id"], "staff", 7, {"name": "Nope"})
    start = datetime(2030, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 2, tzinfo=UTC)
    closure = repo.closure_add(first["id"], "staff", 1, start, end, "maintenance")
    assert repo.closure_list(first["id"])[0]["id"] == closure["id"]
    assert repo.closure_list(second["id"]) == []
    with pytest.raises(NotFound):
        repo.closure_remove(second["id"], closure["id"], "staff", 1, "wrong owner")
    path = repo.engine.url.database
    private_name = private["snapshot"]["name"]
    repo.engine.dispose()
    reopened = ParksRepository(create_engine(f"sqlite:///{path}"))
    assert reopened.catalog_get("resource", private["id"])["snapshot"]["name"] == private_name
    reopened.engine.dispose()


def test_household_tokens_participant_dob_isolation_and_reopen(repo):
    key = Fernet.generate_key()
    household = repo.household_create("staff", HOUSEHOLD, key=key)
    token = household["tracking_token"]
    assert repo.household_by_token(token)["id"] == household["id"]
    with repo.engine.connect() as conn:
        stored = conn.execute(
            select(households.c.tracking_hash, households.c.encrypted_tracking_token)
        ).mappings().one()
    assert stored["tracking_hash"] == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert stored["tracking_hash"] != token
    assert token.encode("ascii") not in stored["encrypted_tracking_token"]
    assert Fernet(key).decrypt(stored["encrypted_tracking_token"]) == token.encode("ascii")
    with pytest.raises(Unauthorized):
        repo.household_by_token("é" * 32)
    other = repo.household_create(
        "staff", {**HOUSEHOLD, "contact_name": "Other", "email": "other@example.com"}, key=key
    )
    participant = repo.participant_create(
        household["id"], "staff", {"name": "Kid", "birth_date": "2010-05-06"}
    )
    assert repo.participant_list(other["id"]) == []
    updated = repo.participant_update(
        participant["id"], "staff", 1, {"name": "Kid", "birth_date": "2010-05-07"}
    )
    assert updated["revision"] == 2
    assert repo.participant_get(participant["id"])["snapshot"]["birth_date"] == "2010-05-07"
    with pytest.raises(Conflict):
        repo.participant_update(
            participant["id"], "staff", 1, {"name": "Stale", "birth_date": "2010-05-07"}
        )
    with pytest.raises(InvalidInput):
        repo.participant_create(
            other["id"], "staff", {"name": "Bad", "birth_date": "05/06/2010"}
        )
    path = repo.engine.url.database
    repo.engine.dispose()
    reopened = ParksRepository(create_engine(f"sqlite:///{path}"))
    assert reopened.household_by_token(token)["id"] == household["id"]
    with reopened.engine.connect() as conn:
        assert conn.execute(
            select(households.c.id).where(households.c.id == household["id"])
        ).scalar_one() == household["id"]
    reopened.engine.dispose()
