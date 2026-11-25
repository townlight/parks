"""Explicit schema migration and readiness checks for townlight_parks."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.schema import CreateSchema

from .models import SCHEMA, metadata, schema_versions


class MigrationError(RuntimeError):
    """Raised when the managed schema cannot be upgraded or is not ready."""




def _schema(engine):
    return None if engine.dialect.name == "sqlite" else SCHEMA


def _connection(conn):
    if conn.dialect.name == "sqlite" and SCHEMA:
        return conn.execution_options(schema_translate_map={SCHEMA: None})
    return conn


def _inspect_columns(inspector, table, schema):
    actual = {column["name"] for column in inspector.get_columns(table.name, schema=schema)}
    missing = {column.name for column in table.columns} - actual
    if missing:
        raise MigrationError(
            f"table {table.name!r} is missing columns: {', '.join(sorted(missing))}"
        )


def _assert_ready(engine):
    schema = _schema(engine)
    with engine.connect() as raw:
        conn = _connection(raw)
        inspector = sa.inspect(conn)
        if schema is not None and not inspector.has_schema(schema):
            raise MigrationError(f"schema {schema!r} is missing")
        for table in metadata.tables.values():
            if not inspector.has_table(table.name, schema=schema):
                raise MigrationError(f"managed table {table.name!r} is missing")
            _inspect_columns(inspector, table, schema)
        current = conn.execute(
            sa.select(sa.func.max(schema_versions.c.version))
        ).scalar_one()
        if current != 1:
            raise MigrationError(
                f"managed schema version is {current!r}; expected exactly 1"
            )


def assert_ready(engine):
    """Raise MigrationError unless every managed table, column, and version 1 exist."""
    try:
        _assert_ready(engine)
    except MigrationError:
        raise
    except sa.exc.SQLAlchemyError as exc:  # Keep readiness failures actionable and consistent.
        raise MigrationError(f"managed schema is not ready: {exc}") from exc


def ready(engine) -> bool:
    """Return True only when the complete managed schema is version 1."""
    try:
        _assert_ready(engine)
        return True
    except (MigrationError, sa.exc.SQLAlchemyError):
        return False


def upgrade(engine):
    """Create the managed schema transactionally, rejecting future versions."""
    schema = _schema(engine)
    with engine.begin() as raw:
        conn = _connection(raw)
        if schema:
            conn.execute(CreateSchema(schema, if_not_exists=True))

        inspector = sa.inspect(conn)
        current = None
        if inspector.has_table(schema_versions.name, schema=schema):
            _inspect_columns(inspector, schema_versions, schema)
            current = conn.execute(
                sa.select(sa.func.max(schema_versions.c.version))
            ).scalar_one()
            if current is not None and current > 1:
                raise MigrationError(
                    f"database schema version {current} is newer than this build "
                    "supports (1); refusing to modify it"
                )
            if current is not None and current != 1:
                raise MigrationError(
                    f"database schema version {current!r} is unsupported; expected 1"
                )

        metadata.create_all(conn)
        if current is None:
            conn.execute(
                schema_versions.insert().values(
                    version=1,
                    applied_at=sa.func.now(),
                )
            )
