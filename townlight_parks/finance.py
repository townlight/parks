"""Money operations for Parks obligations.

The public staff API is offline-only. :meth:`FinanceMixin._record_money` is
the single persistence path used by both offline staff commands and, later,
a verified Stripe adapter owned elsewhere in the application.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from .models import ledger, obligations, payment_intents
from .store import (
    Conflict,
    InvalidInput,
    cents,
    identifier,
    new_id,
    text,
    utcnow,
)

__all__ = ["FinanceMixin"]

_MAX_REFERENCE = 255
_MAX_IDEMPOTENCY_KEY = 255
_MAX_PROVIDER_ID = 255
_MAX_ACTOR = 128
_MAX_LIMIT = 500


class FinanceMixin:
    """Append-only obligation ledger and financial projections."""

    def obligation_get(self, obligation_id: Any, *, _conn=None) -> dict[str, Any]:
        obligation_id = identifier(obligation_id)
        with self._transaction(_conn) as conn:
            row = self._row(conn, obligations, obligation_id)
            totals = self._totals(conn, obligation_id)
            entries = conn.execute(
                select(ledger)
                .where(ledger.c.obligation_id == obligation_id)
                .order_by(ledger.c.created_at, ledger.c.id)
            ).mappings().all()
        result = dict(row)
        result.update(totals)
        result["ledger"] = [dict(entry) for entry in entries]
        return result

    def record_offline_payment(
        self, obligation_id, actor, amount_cents, reference, idempotency_key, *, _conn=None
    ) -> dict[str, Any]:
        return self._record_money(
            obligation_id,
            actor,
            amount_cents,
            reference,
            idempotency_key,
            operation="payment",
            method="offline",
            provider_id=None,
            _conn=_conn,
        )

    def record_offline_refund(
        self, obligation_id, actor, amount_cents, reference, idempotency_key, *, _conn=None
    ) -> dict[str, Any]:
        return self._record_money(
            obligation_id,
            actor,
            amount_cents,
            reference,
            idempotency_key,
            operation="refund",
            method="offline",
            provider_id=None,
            _conn=_conn,
        )

    def _record_money(
        self,
        obligation_id,
        actor,
        amount_cents,
        reference,
        idempotency_key,
        *,
        operation,
        method="offline",
        provider_id=None,
        _conn=None,
    ) -> dict[str, Any]:
        obligation_id = identifier(obligation_id)
        actor = text(actor, _MAX_ACTOR, field="actor")
        amount = cents(amount_cents, "amount_cents", positive=True)
        reference = text(reference, _MAX_REFERENCE, field="reference")
        key = text(idempotency_key, _MAX_IDEMPOTENCY_KEY, field="idempotency_key")
        operation = text(operation, 16, field="operation")
        if operation not in ("payment", "refund"):
            raise InvalidInput("operation must be payment or refund")
        method = text(method, 16, field="method")
        if method not in ("offline", "stripe"):
            raise InvalidInput("method must be offline or stripe")
        if provider_id is not None:
            provider_id = text(provider_id, _MAX_PROVIDER_ID, field="provider_id")
        if method == "offline" and provider_id is not None:
            raise InvalidInput("offline transactions cannot have a provider_id")
        if method == "stripe" and provider_id is None:
            raise InvalidInput("stripe transactions require a provider_id")

        with self._transaction(_conn) as conn:
            self._serialize_idempotency_key(conn, key)

            existing = conn.execute(
                select(ledger).where(ledger.c.idempotency_key == key)
            ).mappings().first()
            if existing is not None:
                self._assert_identical(
                    existing, obligation_id, operation, amount, reference, method, provider_id
                )
                return dict(existing)

            obligation = self._row(conn, obligations, obligation_id, lock=True)
            if method == "offline":
                active = conn.execute(
                    select(payment_intents.c.id).where(
                        payment_intents.c.obligation_id == obligation_id,
                        payment_intents.c.state.in_(("prepared", "open")),
                    )
                ).first()
                if active is not None:
                    raise Conflict(
                        "offline transactions are blocked by an active checkout intent"
                    )
            totals = self._totals(conn, obligation_id)
            if operation == "payment":
                available = obligation["amount_cents"] - totals["net_paid_cents"]
                if amount > available:
                    raise Conflict("payment exceeds the obligation's nonnegative balance")
            else:
                if amount > totals["net_paid_cents"]:
                    raise Conflict("refund exceeds the net paid amount")
                method_balance = self._method_balance(conn, obligation_id, method)
                if amount > method_balance:
                    raise Conflict(f"refund exceeds the actual {method} paid balance")

            entry = {
                "id": new_id(),
                "obligation_id": obligation_id,
                "operation": operation,
                "amount_cents": amount,
                "method": method,
                "reference": reference,
                "idempotency_key": key,
                "provider_id": provider_id,
                "actor": actor,
                "created_at": utcnow(),
            }
            try:
                with conn.begin_nested():
                    conn.execute(ledger.insert().values(**entry))
                    self._event(
                        conn,
                        "obligation",
                        obligation_id,
                        1,
                        f"financial.{operation}",
                        actor,
                        {
                            "ledger_id": entry["id"],
                            "operation": operation,
                            "amount_cents": amount,
                            "method": method,
                            "currency": obligation["currency"],
                        },
                    )
            except IntegrityError as exc:
                raise Conflict(
                    "ledger transaction conflicts with an existing provider or idempotency key"
                ) from exc
            return entry

    def payment_receipt(self, obligation_id: Any, *, _conn=None) -> dict[str, Any]:
        data = self.obligation_get(obligation_id, _conn=_conn)
        entity = {column.name: data[column.name] for column in obligations.c}
        totals = {
            name: data[name]
            for name in (
                "paid_cents",
                "refunded_cents",
                "net_paid_cents",
                "balance_cents",
                "deposit_cents",
            )
        }
        return {
            "schema_version": 1,
            "currency": data["currency"],
            "entity": entity,
            "ledger": data["ledger"],
            "totals": totals,
        }

    def reconciliation(self, limit: int = 100, offset: int = 0, *, _conn=None) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= _MAX_LIMIT):
            raise InvalidInput(f"limit must be an integer between 1 and {_MAX_LIMIT}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidInput("offset must be a nonnegative integer")
        with self._transaction(_conn) as conn:
            rows = conn.execute(
                select(obligations)
                .order_by(obligations.c.created_at, obligations.c.id)
                .limit(limit)
                .offset(offset)
            ).mappings().all()
            results: list[dict[str, Any]] = []
            for row in rows:
                totals = self._totals(conn, row["id"])
                results.append(
                    {
                        "obligation_id": row["id"],
                        "entity_kind": row["entity_kind"],
                        "entity_id": row["entity_id"],
                        "currency": row["currency"],
                        "amount_cents": row["amount_cents"],
                        "deposit_cents": row["deposit_cents"],
                        "created_at": row["created_at"],
                        "paid_cents": totals["paid_cents"],
                        "refunded_cents": totals["refunded_cents"],
                        "net_paid_cents": totals["net_paid_cents"],
                        "balance_cents": totals["balance_cents"],
                    }
                )
        return results

    @staticmethod
    def _serialize_idempotency_key(conn, key: str) -> None:
        if conn.dialect.name == "postgresql":
            conn.execute(
                sql_text(
                    "SELECT pg_advisory_xact_lock(hashtextextended(:idempotency_key, 0))"
                ),
                {"idempotency_key": key},
            )

    @staticmethod
    def _assert_identical(
        existing,
        obligation_id: str,
        operation: str,
        amount: int,
        reference: str,
        method: str,
        provider_id: str | None,
    ) -> None:
        if (
            existing["obligation_id"] != obligation_id
            or existing["operation"] != operation
            or existing["amount_cents"] != amount
            or existing["reference"] != reference
            or existing["method"] != method
            or existing["provider_id"] != provider_id
        ):
            raise Conflict("idempotency key is already used by a different transaction")

    @staticmethod
    def _totals(conn, obligation_id: str) -> dict[str, int]:
        paid = conn.execute(
            select(func.coalesce(func.sum(ledger.c.amount_cents), 0)).where(
                ledger.c.obligation_id == obligation_id,
                ledger.c.operation == "payment",
            )
        ).scalar_one()
        refunded = conn.execute(
            select(func.coalesce(func.sum(ledger.c.amount_cents), 0)).where(
                ledger.c.obligation_id == obligation_id,
                ledger.c.operation == "refund",
            )
        ).scalar_one()
        paid = int(paid)
        refunded = int(refunded)
        net_paid = paid - refunded
        return {
            "paid_cents": paid,
            "refunded_cents": refunded,
            "net_paid_cents": net_paid,
            "balance_cents": conn.execute(
                select(obligations.c.amount_cents).where(obligations.c.id == obligation_id)
            ).scalar_one() - net_paid,
        }

    @staticmethod
    def _method_balance(conn, obligation_id: str, method: str) -> int:
        paid = conn.execute(
            select(func.coalesce(func.sum(ledger.c.amount_cents), 0)).where(
                ledger.c.obligation_id == obligation_id,
                ledger.c.operation == "payment",
                ledger.c.method == method,
            )
        ).scalar_one()
        refunded = conn.execute(
            select(func.coalesce(func.sum(ledger.c.amount_cents), 0)).where(
                ledger.c.obligation_id == obligation_id,
                ledger.c.operation == "refund",
                ledger.c.method == method,
            )
        ).scalar_one()
        return int(paid) - int(refunded)
