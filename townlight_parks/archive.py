"""Read-only, deterministic entity archives for the Parks domain.

The archive is built entirely from actual database rows and stored attachment
bytes.  Nothing is uploaded here: callers receive the ZIP bytes and may hand
them to the private archive runtime.
"""
from __future__ import annotations

import hashlib
import io
import json
import unicodedata
import zipfile

from sqlalchemy import select

from .commands import _safe
from .models import (
    bookings,
    closures,
    events,
    households,
    integration_links,
    obligations,
    participants,
    programs,
    registrations,
    resources,
    sessions,
)
from .store import Conflict, InvalidInput, NotFound, identifier

__all__ = ["ArchiveMixin"]

_MAX_EVENTS = 10_000
_MAX_FILES = 102
_MAX_UNCOMPRESSED = 40 * 1024 * 1024
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_ENTITY_TABLES = {
    "resource": resources,
    "program": programs,
    "booking": bookings,
    "registration": registrations,
}


def _table_row(conn, kind, entity_id):
    rows = conn.execute(
        select(_ENTITY_TABLES[kind]).where(
            _ENTITY_TABLES[kind].c.id == entity_id
        ).limit(2)
    ).mappings().all()
    if not rows:
        raise NotFound(f"{kind} not found")
    if len(rows) != 1:
        raise Conflict(f"{kind} id is not unique")
    return dict(rows[0])


def _bounded_rows(conn, statement, label, limit=_MAX_EVENTS):
    rows = conn.execute(statement.limit(limit + 1)).mappings().all()
    if len(rows) > limit:
        raise Conflict(f"{label} exceeds the archive limit of {limit}")
    return [dict(row) for row in rows]


def _public_household(row):
    record = dict(row)
    record.pop("tracking_hash", None)
    record.pop("encrypted_tracking_token", None)
    return record


def _safe_attachment_name(name, fallback):
    text_value = "" if name is None else str(name)
    cleaned = []
    for char in text_value:
        if char in "/\\:" or unicodedata.category(char) == "Cc":
            cleaned.append("_")
        else:
            cleaned.append(char)
    safe = "".join(cleaned).strip()
    if not safe or safe in {".", ".."} or ".." in safe:
        safe = fallback
    if not safe or safe in {".", ".."} or ".." in safe:
        safe = "attachment"
    return safe[:200]


def _json_bytes(value):
    return json.dumps(
        _safe(value),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ArchiveMixin:
    """Mixin requiring RepositoryBase transactions and attachment/finance APIs."""

    def archive_entity(self, kind, id):
        if not isinstance(kind, str) or kind not in _ENTITY_TABLES:
            raise InvalidInput(
                "kind must be resource, program, booking, or registration"
            )
        entity_id = identifier(id)

        with self._transaction() as conn:
            current_record = _table_row(conn, kind, entity_id)
            related = {}

            if kind == "resource":
                related["closures"] = _bounded_rows(
                    conn,
                    select(closures)
                    .where(closures.c.resource_id == entity_id)
                    .order_by(closures.c.created_at, closures.c.id),
                    "resource closures",
                )
                related["maintenance"] = _bounded_rows(
                    conn,
                    select(integration_links)
                    .where(integration_links.c.resource_id == entity_id)
                    .order_by(integration_links.c.created_at, integration_links.c.id),
                    "resource maintenance",
                )
            elif kind == "program":
                related["sessions"] = _bounded_rows(
                    conn,
                    select(sessions)
                    .where(sessions.c.program_id == entity_id)
                    .order_by(sessions.c.created_at, sessions.c.id),
                    "program sessions",
                )
            elif kind == "booking":
                related["household"] = _public_household(
                    dict(
                        conn.execute(
                            select(households)
                            .where(households.c.id == current_record["household_id"])
                            .limit(1)
                        ).mappings().one()
                    )
                )
                related["resource"] = _table_row(
                    conn, "resource", current_record["resource_id"]
                )
            else:
                registration = current_record
                participant = dict(
                    conn.execute(
                        select(participants)
                        .where(participants.c.id == registration["participant_id"])
                        .limit(1)
                    ).mappings().one()
                )
                session = dict(
                    conn.execute(
                        select(sessions)
                        .where(sessions.c.id == registration["session_id"])
                        .limit(1)
                    ).mappings().one()
                )
                program = dict(
                    conn.execute(
                        select(programs)
                        .where(programs.c.id == session["program_id"])
                        .limit(1)
                    ).mappings().one()
                )
                related["participant"] = participant
                related["household"] = _public_household(
                    dict(
                        conn.execute(
                            select(households)
                            .where(households.c.id == participant["household_id"])
                            .limit(1)
                        ).mappings().one()
                    )
                )
                related["session"] = session
                related["program"] = program

            event_rows = _bounded_rows(
                conn,
                select(events)
                .where(
                    events.c.entity_kind == kind,
                    events.c.entity_id == entity_id,
                )
                .order_by(events.c.created_at, events.c.id),
                "entity events",
            )

            financial_receipt = None
            if kind in {"booking", "registration"}:
                obligation_rows = conn.execute(
                    select(obligations)
                    .where(
                        obligations.c.entity_kind == kind,
                        obligations.c.entity_id == entity_id,
                    )
                    .limit(2)
                ).mappings().all()
                if len(obligation_rows) > 1:
                    raise Conflict("entity has more than one financial obligation")
                if obligation_rows:
                    financial_receipt = self.payment_receipt(
                        obligation_rows[0]["id"], _conn=conn
                    )

            listed = self.attachment_list(kind, entity_id)
            if listed["overflow"]:
                raise Conflict("entity has more than 100 attachments")
            attachment_metadata = list(listed["items"])
            if len(attachment_metadata) + 2 > _MAX_FILES:
                raise Conflict("archive would exceed the 102-file limit")

        record = {
            "schema_version": 1,
            "kind": kind,
            "id": entity_id,
            "current_record": _safe(current_record),
            "related": _safe(related),
            "events": _safe(event_rows),
            "financial_receipt": _safe(financial_receipt),
            "attachment_metadata": _safe(attachment_metadata),
        }
        record_bytes = _json_bytes(record)

        members = [("record.json", record_bytes)]
        for item in attachment_metadata:
            fetched = self.attachment_get(item["id"])
            content = bytes(fetched["content"])
            if (
                len(content) != item["size"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise Conflict("attachment content does not match its metadata")
            safe_name = _safe_attachment_name(item.get("name"), item["id"])
            members.append(
                (f"attachments/{item['id']}/{safe_name}", content)
            )

        names = [name for name, _ in members]
        if len(set(names)) != len(names):
            raise Conflict("archive member names are not unique")

        manifest = {
            "schema_version": 1,
            "kind": kind,
            "id": entity_id,
            "files": [
                {
                    "name": name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
                for name, data in sorted(members, key=lambda item: item[0])
            ],
        }
        members.append(("manifest.json", _json_bytes(manifest)))
        members.sort(key=lambda item: item[0])

        if len(members) > _MAX_FILES:
            raise Conflict("archive would exceed the 102-file limit")
        total = sum(len(data) for _, data in members)
        if total > _MAX_UNCOMPRESSED:
            raise Conflict("archive would exceed the 40 MiB uncompressed limit")

        blob = io.BytesIO()
        with zipfile.ZipFile(
            blob, mode="w", compression=zipfile.ZIP_STORED
        ) as archive:
            for name, data in members:
                info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                archive.writestr(info, data)

        content = blob.getvalue()
        return {
            "name": f"parks-{kind}-{entity_id}.zip",
            "media_type": "application/zip",
            "content": content,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
