from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update
from test_catalog_households import HOUSEHOLD, RESOURCE
from test_catalog_households import repo as repo  # noqa: PLC0414 - pytest fixture re-export

from townlight_parks.models import attachments
from townlight_parks.store import Conflict, InvalidInput


def _keys(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= _keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= _keys(item)
    return found


def _zip_parts(result):
    with zipfile.ZipFile(io.BytesIO(result["content"])) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    return files, manifest


def _verify_manifest(result):
    files, manifest = _zip_parts(result)
    record = json.loads(files["record.json"])
    assert manifest["kind"] == record["kind"]
    assert manifest["id"] == record["id"]
    listed = {entry["name"]: entry for entry in manifest["files"]}
    assert set(files) - {"manifest.json"} == set(listed)
    for name, entry in listed.items():
        data = files[name]
        assert len(data) == entry["size"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert hashlib.sha256(result["content"]).hexdigest() == result["sha256"]
    assert len(result["content"]) == result["size"]
    return files, record


def _res(repo, name):
    return repo.catalog_create("resource", "staff", {**RESOURCE, "name": name})


def _household(repo):
    return repo.household_create("staff", HOUSEHOLD, key=Fernet.generate_key())


def _booking(repo, resource=None, name="Archive Hall"):
    resource = resource or _res(repo, name)
    household = _household(repo)
    start = datetime.now(UTC).replace(
        minute=0, second=0, microsecond=0
    ) + timedelta(days=9)
    created = repo.booking_create(
        resource["id"], household["id"], "staff", resource["revision"],
        [{"starts_at": start, "ends_at": start + timedelta(hours=1)}],
        "Archive booking",
    )
    return resource, household, created["bookings"][0]


def test_booking_archive_is_offline_deterministic_and_integrity_checked(repo):
    resource, household, booking = _booking(repo)
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    attachment_id = repo.attachment_add(
        "booking", booking["id"], "staff", "waiver.pdf", "application/pdf", pdf
    )
    assert repo.attachment_get(attachment_id)["content"] == pdf

    repo.record_offline_payment(
        booking["obligation_id"], "staff", 120, "cash", "offline"
    )

    result = repo.archive_entity("booking", booking["id"])
    assert result["media_type"] == "application/zip"
    files, record = _verify_manifest(result)
    assert record["kind"] == "booking" and record["id"] == booking["id"]
    receipt = record["financial_receipt"]
    assert receipt["totals"]["paid_cents"] == 120
    assert receipt["totals"]["balance_cents"] == 0
    assert record["related"]["household"]["id"] == household["id"]
    assert record["related"]["resource"]["id"] == resource["id"]
    assert not (
        _keys(record["related"]["household"])
        & {"tracking_hash", "encrypted_tracking_token", "capability",
           "capability_hash", "raw_capability"}
    )
    record_text = files["record.json"].decode("utf-8")
    assert '"amount_cents":120' in record_text
    assert "attachment_added" in record_text
    assert files[f"attachments/{attachment_id}/waiver.pdf"] == pdf

    again = repo.archive_entity("booking", booking["id"])
    assert again["content"] == result["content"]
    assert again["sha256"] == result["sha256"]

    with pytest.raises(InvalidInput):
        repo.attachment_add(
            "booking", booking["id"], "staff", "../waiver.pdf",
            "application/pdf", pdf,
        )
    with pytest.raises(InvalidInput):
        repo.attachment_add(
            "booking", booking["id"], "staff", "nested/waiver.pdf",
            "application/pdf", pdf,
        )
    with repo.engine.begin() as conn:
        conn.execute(
            update(attachments)
            .where(attachments.c.id == attachment_id)
            .values(content=b"tampered")
        )
    with pytest.raises(Conflict):
        repo.archive_entity("booking", booking["id"])


def test_maintenance_link_and_aggregate_report_use_actual_rows(repo):
    resource = _res(repo, "Maintenance Hall")
    other = _res(repo, "Other Hall")
    request_id = str(uuid.uuid4())
    command_id = str(uuid.uuid4())
    link_id = repo.maintenance_link(
        resource["id"], "staff", request_id, command_id
    )
    assert repo.maintenance_link(
        resource["id"], "staff", request_id, command_id
    ) == link_id
    with pytest.raises(Conflict):
        repo.maintenance_link(
            resource["id"], "staff", str(uuid.uuid4()), command_id
        )
    with pytest.raises(Conflict):
        repo.maintenance_link(
            other["id"], "staff", request_id, str(uuid.uuid4())
        )

    archive = repo.archive_entity("resource", resource["id"])
    _files, record = _verify_manifest(archive)
    assert record["kind"] == "resource" and record["id"] == resource["id"]
    links = record["related"]["maintenance"]
    assert [link["id"] for link in links] == [link_id]
    assert links[0]["request_id"] == request_id
    assert links[0]["command_id"] == command_id

    _booking(repo, resource=resource)
    report = repo.aggregate_report()
    assert sum(
        row["count"] for row in report if row["metric"] == "bookings"
    ) == 1
    outstanding = [
        row for row in report if row["metric"] == "outstanding"
    ]
    assert outstanding == [{
        "metric": "outstanding", "state": "all", "count": 1,
        "amount_cents": 120, "minutes": 0,
    }]
    linked = [
        row for row in report if row["metric"] == "maintenance_linked"
    ]
    assert linked[0]["count"] == 1

    assert _keys(report) <= {"metric", "state", "count", "amount_cents", "minutes"}
    report_text = json.dumps(report)
    for forbidden in (
        "tracking_hash", "encrypted_tracking_token", "capability",
        "email", "dob", "date_of_birth", "accommodations", "name",
    ):
        assert forbidden not in report_text
    assert re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        report_text,
    ) is None
