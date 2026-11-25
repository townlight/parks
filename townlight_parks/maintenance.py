"""Maintenance integration-link operations.

The 311 request itself is created and authorized by the owning runtime.  This
module records only the durable link to that externally-owned request and
never invents a local closure/resolved status.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import integration_links, resources
from .store import (
    Conflict,
    InvalidInput,
    NotFound,
    identifier,
    new_id,
    text,
    utcnow,
)

__all__ = ["MaintenanceMixin"]

_EVENT_KIND = "integration_link.created"


class MaintenanceMixin:
    """Resource-scoped maintenance link methods for a RepositoryBase."""

    def maintenance_link(
        self,
        resource_id: Any,
        actor: Any,
        request_id: Any,
        command_id: Any,
    ) -> str:
        """Record the idempotent link between a resource and a 311 request.

        The caller/runtime remains responsible for the real cross-module
        authorization and 311 idempotency.  This method only owns the local
        durable link and its resource event.
        """
        resource_id = identifier(resource_id)
        actor = text(actor, 128, field="actor")
        request_id = text(request_id, 255, field="request_id")
        command_id = identifier(command_id)

        with self._transaction() as conn:
            resource = conn.execute(
                select(resources.c.revision)
                .where(resources.c.id == resource_id)
                .with_for_update()
            ).mappings().first()
            if resource is None:
                raise NotFound("resources not found")

            by_command = conn.execute(
                select(integration_links).where(
                    integration_links.c.command_id == command_id
                )
            ).mappings().first()
            if by_command is not None:
                if (
                    by_command["resource_id"] == resource_id
                    and by_command["request_id"] == request_id
                ):
                    return by_command["id"]
                raise Conflict("command_id is already linked to another request")

            by_request = conn.execute(
                select(integration_links).where(
                    integration_links.c.request_id == request_id
                )
            ).mappings().first()
            if by_request is not None:
                if by_request["resource_id"] == resource_id:
                    return by_request["id"]
                raise Conflict("request_id is already linked to another resource")

            link_id = new_id()
            conn.execute(
                integration_links.insert().values(
                    id=link_id,
                    resource_id=resource_id,
                    request_id=request_id,
                    command_id=command_id,
                    created_at=utcnow(),
                )
            )
            self._event(
                conn,
                "resource",
                resource_id,
                resource["revision"],
                _EVENT_KIND,
                actor,
                {
                    "link_id": link_id,
                    "request_id": request_id,
                    "command_id": command_id,
                },
            )
            return link_id

    def maintenance_links(
        self, resource_id: Any, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return bounded, resource-scoped durable link rows.

        No locally-invented status field is added.  Callers that need live 311
        state must fetch it from the owning 311 system; the local row is not a
        substitute for that state.
        """
        resource_id = identifier(resource_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InvalidInput("limit must be an integer between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidInput("offset must be a nonnegative integer")

        with self._transaction() as conn:
            exists = conn.execute(
                select(resources.c.id).where(resources.c.id == resource_id)
            ).first()
            if exists is None:
                raise NotFound("resources not found")
            rows = conn.execute(
                select(integration_links)
                .where(integration_links.c.resource_id == resource_id)
                .order_by(
                    integration_links.c.created_at,
                    integration_links.c.id,
                )
                .limit(limit)
                .offset(offset)
            ).mappings()
            return [dict(row) for row in rows]

    def maintenance_get(self, link_id: Any) -> dict[str, Any]:
        """Return the actual durable link row for ``link_id``."""
        link_id = identifier(link_id)
        with self._transaction() as conn:
            row = conn.execute(
                select(integration_links).where(integration_links.c.id == link_id)
            ).mappings().first()
            if row is None:
                raise NotFound("integration_links not found")
            return dict(row)
