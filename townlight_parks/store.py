"""Shared persistence primitives for Townlight Parks.

The schema is owned by :mod:`townlight_parks.models`; this module never
reflects, guesses at, or creates application tables on behalf of callers.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, select

from .models import events

__all__ = [
    "Conflict",
    "InvalidInput",
    "NotFound",
    "RepositoryBase",
    "Unauthorized",
    "canonical_bytes",
    "cents",
    "identifier",
    "new_id",
    "positive_revision",
    "text",
    "utcnow",
]


class Conflict(ValueError):
    """The requested mutation conflicts with current durable state."""


class InvalidInput(ValueError):
    """A caller supplied a value outside the public contract."""


class NotFound(ValueError):
    """An addressed durable record does not exist."""


class Unauthorized(ValueError):
    """The actor is not allowed to perform the requested operation."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidInput("value must be valid JSON") from exc
    return encoded.encode("utf-8")


def text(
    value: Any,
    max_length: int = 1024,
    *,
    field: str = "value",
    allow_empty: bool = False,
) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise InvalidInput(f"{field} must be text")
    if not allow_empty and not value:
        raise InvalidInput(f"{field} must not be empty")
    if max_length < 1 or len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


def identifier(value: Any) -> str:
    value = text(value, 36, field="identifier")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise InvalidInput("identifier must be a UUID") from exc
    if str(parsed) != value.lower():
        raise InvalidInput("identifier must use canonical UUID form")
    return str(parsed)


def positive_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidInput("revision must be a positive integer")
    return value


def cents(
    value: Any, field: str = "amount", *, positive: bool = False
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput(f"{field} must be integer cents")
    if value < 0 or (positive and value == 0):
        raise InvalidInput(f"{field} must be {'positive' if positive else 'nonnegative'}")
    return value


class RepositoryBase:
    """Own the engine and prove the imported schema is ready.

    ``bootstrap=True`` is an explicit development/test upgrade request.
    The default path only verifies the migration state and never creates
    production tables by reflection or convenience.
    """

    def __init__(self, engine, *, bootstrap: bool = False):
        if engine is None:
            raise InvalidInput("engine is required")
        if engine.dialect.name == "sqlite":
            engine = engine.execution_options(
                schema_translate_map={"townlight_parks": None}
            )
        self.engine = engine
        if bootstrap:
            from . import migrations

            migrations.upgrade(engine)
        else:
            from . import migrations

            migrations.assert_ready(engine)

    @contextmanager
    def _transaction(self, conn=None):
        if conn is not None:
            yield conn
            return
        with self.engine.begin() as transaction:
            yield transaction

    @classmethod
    def _row(
        cls,
        conn,
        table,
        identifier_value: Any,
        expected_revision: Any | None = None,
        lock: bool = False,
    ) -> dict[str, Any]:
        return _row(conn, table, identifier_value, expected_revision, lock)

    @classmethod
    def _event(
        cls,
        conn,
        entity_kind: Any,
        entity_id: Any,
        revision: Any,
        kind: Any,
        actor: Any,
        payload: Any,
    ) -> None:
        return _event(conn, entity_kind, entity_id, revision, kind, actor, payload)


def _row(
    conn,
    table,
    identifier_value: Any,
    expected_revision: Any | None = None,
    lock: bool = False,
) -> dict[str, Any]:
    identifier_value = identifier(identifier_value)
    expected = (
        None if expected_revision is None else positive_revision(expected_revision)
    )
    if not isinstance(table, Table):
        raise InvalidInput("table must be a Table object")
    table_obj = table
    statement = select(table_obj).where(table_obj.c.id == identifier_value)
    if lock:
        statement = statement.with_for_update()
    row = conn.execute(statement).mappings().first()
    if row is None:
        raise NotFound(f"{table_obj.name} not found")
    result = dict(row)
    if expected is not None and result.get("revision") != expected:
        raise Conflict(
            f"{table_obj.name} revision changed; expected {expected}, "
            f"found {result.get('revision')}"
        )
    return result


def _event(
    conn,
    entity_kind: Any,
    entity_id: Any,
    revision: Any,
    kind: Any,
    actor: Any,
    payload: Any,
) -> None:
    entity_kind = text(entity_kind, 32, field="entity_kind")
    entity_id = identifier(entity_id)
    revision = positive_revision(revision)
    kind = text(kind, 64, field="event kind")
    actor = text(actor, 128, field="actor")
    encoded = canonical_bytes(payload)
    if len(encoded) > 64 * 1024:
        raise InvalidInput("event payload must be at most 64 KiB")
    conn.execute(
        events.insert().values(
            id=new_id(),
            entity_kind=entity_kind,
            entity_id=entity_id,
            revision=revision,
            kind=kind,
            actor=actor,
            payload=payload,
            created_at=utcnow(),
        )
    )
