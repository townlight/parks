"""Attachment persistence for Townlight Parks entities.

Attachments are stored as actual bytes.  The database row carries the byte
hash and length so reads can prove integrity, while events and list results
never include attachment content.
"""

from __future__ import annotations

import hashlib
import unicodedata

from sqlalchemy import func, select

from .models import attachments, bookings, programs, registrations, resources
from .store import (
    Conflict,
    InvalidInput,
    NotFound,
    identifier,
    new_id,
    text,
    utcnow,
)

__all__ = ["AttachmentMixin"]

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ENTITY_BYTES = 40 * 1024 * 1024
MAX_ATTACHMENTS_PER_ENTITY = 100

_ENTITY_TABLES = {
    "resource": resources,
    "program": programs,
    "booking": bookings,
    "registration": registrations,
}

_ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "image/png",
        "image/jpeg",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)

_METADATA_COLUMNS = (
    attachments.c.id,
    attachments.c.entity_kind,
    attachments.c.entity_id,
    attachments.c.name,
    attachments.c.media_type,
    attachments.c.sha256,
    attachments.c.size,
    attachments.c.actor,
    attachments.c.created_at,
)


def _entity_kind(value):
    if not isinstance(value, str) or value not in _ENTITY_TABLES:
        raise InvalidInput(
            "entity_kind must be resource, program, booking, or registration"
        )
    return value


def _attachment_name(value):
    name = text(value, 255, field="name")
    if not name.strip() or name in {".", ".."} or ".." in name:
        raise InvalidInput("name must be a safe basename")
    for char in name:
        if char in "/\\:" or unicodedata.category(char) == "Cc":
            raise InvalidInput("name must be a safe basename")
    return name


def _attachment_media_type(value):
    if not isinstance(value, str):
        raise InvalidInput("media_type must be text")
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise InvalidInput("media_type is not allowed")
    return media_type


def _attachment_content(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        content = bytes(value)
    else:
        raise InvalidInput("contentbytes must be bytes")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise InvalidInput("attachment must be at most 10 MiB")
    return content


class AttachmentMixin:
    """Mixin requiring the RepositoryBase transaction/row/event primitives."""

    def attachment_add(
        self,
        entity_kind,
        entity_id,
        actor,
        name,
        media_type,
        contentbytes,
    ):
        kind = _entity_kind(entity_kind)
        entity_id = identifier(entity_id)
        actor = text(actor, 128, field="actor")
        name = _attachment_name(name)
        media_type = _attachment_media_type(media_type)
        content = _attachment_content(contentbytes)
        digest = hashlib.sha256(content).hexdigest()
        size = len(content)

        with self._transaction() as conn:
            # Serialize additions for one entity before checking count/bytes.
            entity = self._row(conn, _ENTITY_TABLES[kind], entity_id, lock=True)
            duplicate_id = conn.execute(
                select(attachments.c.id)
                .where(
                    attachments.c.entity_kind == kind,
                    attachments.c.entity_id == entity_id,
                    attachments.c.name == name,
                    attachments.c.sha256 == digest,
                )
                .limit(1)
            ).scalar_one_or_none()
            if duplicate_id is not None:
                return duplicate_id

            count, total = conn.execute(
                select(
                    func.count(attachments.c.id),
                    func.coalesce(func.sum(attachments.c.size), 0),
                ).where(
                    attachments.c.entity_kind == kind,
                    attachments.c.entity_id == entity_id,
                )
            ).one()
            if int(count) >= MAX_ATTACHMENTS_PER_ENTITY:
                raise Conflict("entity already has the maximum 100 attachments")
            if int(total) + size > MAX_ENTITY_BYTES:
                raise Conflict("entity attachments would exceed 40 MiB")

            attachment_id = new_id()
            conn.execute(
                attachments.insert().values(
                    id=attachment_id,
                    entity_kind=kind,
                    entity_id=entity_id,
                    name=name,
                    media_type=media_type,
                    content=content,
                    sha256=digest,
                    size=size,
                    actor=actor,
                    created_at=utcnow(),
                )
            )
            self._event(
                conn,
                kind,
                entity_id,
                entity["revision"],
                "attachment_added",
                actor,
                {
                    "attachment_id": attachment_id,
                    "name": name,
                    "media_type": media_type,
                    "sha256": digest,
                    "size": size,
                },
            )
            return attachment_id

    def attachment_get(self, attachment_id):
        attachment_id = identifier(attachment_id)
        with self._transaction() as conn:
            row = conn.execute(
                select(attachments).where(attachments.c.id == attachment_id)
            ).mappings().first()
        if row is None:
            raise NotFound("attachment not found")
        result = dict(row)
        content = bytes(result["content"] or b"")
        if (
            len(content) != result["size"]
            or hashlib.sha256(content).hexdigest() != result["sha256"]
        ):
            raise Conflict("attachment content failed integrity check")
        result["content"] = content
        return result

    def attachment_list(self, entity_kind, entity_id, limit=100):
        kind = _entity_kind(entity_kind)
        entity_id = identifier(entity_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InvalidInput("limit must be between 1 and 100")
        with self._transaction() as conn:
            rows = conn.execute(
                select(*_METADATA_COLUMNS)
                .where(
                    attachments.c.entity_kind == kind,
                    attachments.c.entity_id == entity_id,
                )
                .order_by(attachments.c.created_at, attachments.c.id)
                .limit(limit + 1)
            ).mappings().all()
        return {
            "items": [dict(row) for row in rows[:limit]],
            "overflow": len(rows) > limit,
        }
