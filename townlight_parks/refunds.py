"""Durable Stripe refund intents for Parks obligations."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .models import ledger, obligations, payment_intents, refund_intents
from .store import Conflict, InvalidInput, cents, identifier, new_id, text, utcnow

__all__ = ["RefundMixin"]

_RE = re.compile(r"re_[A-Za-z0-9_]+")
_PI = re.compile(r"pi_[A-Za-z0-9_]+")
_GENERIC = "refund provider request failed"
_MAX_LIMIT = 500


class RefundMixin:
    """Prepare, submit, and apply provider-authoritative refund intents."""

    def refund_prepare(
        self, payment_ledger_id, actor, amount_cents, reason, command_key, *, _conn=None
    ) -> dict[str, Any]:
        payment_ledger_id = identifier(payment_ledger_id)
        actor = text(actor, 128, field="actor")
        amount = cents(amount_cents, "amount_cents", positive=True)
        reason = text(reason, 255, field="reason")
        command_key = text(command_key, 255, field="command_key")

        with self._transaction(_conn) as conn:
            self._serialize_idempotency_key(conn, command_key)
            unpaid = self._row(conn, ledger, payment_ledger_id, lock=False)
            obligation = self._row(conn, obligations, unpaid["obligation_id"], lock=True)
            payment = self._row(conn, ledger, payment_ledger_id, lock=True)
            if (
                payment["operation"] != "payment"
                or payment["method"] != "stripe"
                or not isinstance(payment["provider_id"], str)
                or _PI.fullmatch(payment["provider_id"]) is None
            ):
                raise InvalidInput("refunds require a Stripe payment ledger entry")

            existing = conn.execute(
                select(refund_intents).where(refund_intents.c.command_key == command_key)
            ).mappings().first()
            if existing is not None:
                if (
                    existing["payment_ledger_id"] != payment_ledger_id
                    or existing["obligation_id"] != obligation["id"]
                    or existing["amount_cents"] != amount
                    or existing["reason"] != reason
                ):
                    raise Conflict("command key is already used by a different refund")
                return dict(existing)

            active = conn.execute(
                select(payment_intents.c.id).where(
                    payment_intents.c.obligation_id == obligation["id"],
                    payment_intents.c.state.in_(("prepared", "open")),
                )
            ).first()
            if active is not None:
                raise Conflict("refund is blocked by an active checkout intent")
            totals = self._totals(conn, obligation["id"])
            if amount > totals["net_paid_cents"]:
                raise Conflict("refund exceeds the actual net paid amount")
            reserved = int(
                conn.execute(
                    select(func.coalesce(func.sum(refund_intents.c.amount_cents), 0)).where(
                        refund_intents.c.payment_ledger_id == payment_ledger_id,
                        refund_intents.c.state.in_(("prepared", "pending", "succeeded")),
                    )
                ).scalar_one()
            )
            if reserved + amount > payment["amount_cents"]:
                raise Conflict("refund exceeds the remaining refundable payment amount")

            now = utcnow()
            row = {
                "id": new_id(),
                "payment_ledger_id": payment_ledger_id,
                "obligation_id": obligation["id"],
                "command_key": command_key,
                "amount_cents": amount,
                "reason": reason,
                "state": "prepared",
                "provider_refund_id": None,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            try:
                with conn.begin_nested():
                    conn.execute(refund_intents.insert().values(**row))
            except IntegrityError as exc:
                raise Conflict("refund command conflicts with an existing intent") from exc
            return row

    def refund_get(self, refund_id: Any, *, _conn=None) -> dict[str, Any]:
        refund_id = identifier(refund_id)
        with self._transaction(_conn) as conn:
            return dict(self._row(conn, refund_intents, refund_id, lock=False))

    def refund_pending(self, limit: int = 50, *, _conn=None) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= _MAX_LIMIT):
            raise InvalidInput(f"limit must be an integer between 1 and {_MAX_LIMIT}")
        with self._transaction(_conn) as conn:
            rows = conn.execute(
                select(refund_intents)
                .where(refund_intents.c.state.in_(("prepared", "pending")))
                .order_by(refund_intents.c.created_at, refund_intents.c.id)
                .limit(limit)
            ).mappings().all()
        return [dict(row) for row in rows]

    def refund_submit(self, refund_id: Any, actor, transport):
        refund_id = identifier(refund_id)
        text(actor, 128, field="actor")
        with self._transaction() as conn:
            intent = dict(self._row(conn, refund_intents, refund_id, lock=False))
            if intent["state"] in ("succeeded", "failed"):
                return intent
            payment = dict(self._row(conn, ledger, intent["payment_ledger_id"], lock=False))
        if intent["state"] == "prepared":
            try:
                result = transport.create_refund(
                    intent["id"],
                    payment["provider_id"],
                    intent["amount_cents"],
                    {"refund_intent_id": intent["id"], "obligation_id": intent["obligation_id"]},
                )
            except Exception:
                self._refund_error(refund_id, _GENERIC)
                raise
            provider_id = result.get("id") if isinstance(result, Mapping) else None
            if not isinstance(provider_id, str) or _RE.fullmatch(provider_id) is None:
                self._refund_error(refund_id, _GENERIC)
                raise InvalidInput("provider refund response has an invalid id")
            self._refund_store_provider(refund_id, provider_id)
            return self.refund_apply(refund_id, actor, result)
        provider_id = intent["provider_refund_id"]
        if not isinstance(provider_id, str) or _RE.fullmatch(provider_id) is None:
            self._refund_error(refund_id, _GENERIC)
            raise InvalidInput("stored provider refund id is invalid")
        try:
            result = transport.retrieve_refund(provider_id)
        except Exception:
            self._refund_error(refund_id, _GENERIC)
            raise
        if not isinstance(result, Mapping) or result.get("id") != provider_id:
            self._refund_error(refund_id, _GENERIC)
            raise InvalidInput("provider refund response has an invalid id")
        self._refund_error(refund_id, None)
        return self.refund_apply(refund_id, actor, result)

    def refund_apply(self, refund_id: Any, actor, provider_result, *, _conn=None) -> dict[str, Any]:
        refund_id = identifier(refund_id)
        actor = text(actor, 128, field="actor")
        if not isinstance(provider_result, Mapping):
            raise InvalidInput("provider_result must be a mapping")
        provider_id = provider_result.get("id")
        if not isinstance(provider_id, str) or _RE.fullmatch(provider_id) is None:
            raise InvalidInput("provider_result has an invalid refund id")
        status = provider_result.get("status")
        if status not in ("pending", "requires_action", "failed", "canceled", "succeeded"):
            raise InvalidInput("provider_result has an unsupported status")
        amount = provider_result.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise InvalidInput("provider_result has an invalid amount")
        currency = provider_result.get("currency")
        if not isinstance(currency, str) or len(currency) != 3:
            raise InvalidInput("provider_result has an invalid currency")
        payment_intent = provider_result.get("payment_intent")
        if not isinstance(payment_intent, str) or _PI.fullmatch(payment_intent) is None:
            raise InvalidInput("provider_result has an invalid payment intent")
        metadata = provider_result.get("metadata")
        if not isinstance(metadata, Mapping):
            raise InvalidInput("provider_result metadata is required")

        with self._transaction(_conn) as conn:
            self._serialize_idempotency_key(conn, f"refund:{refund_id}")
            unlocked = self._row(conn, refund_intents, refund_id, lock=False)
            obligation = self._row(conn, obligations, unlocked["obligation_id"], lock=True)
            intent = self._row(conn, refund_intents, refund_id, lock=True)
            payment = self._row(conn, ledger, intent["payment_ledger_id"], lock=False)
            if currency.lower() != obligation["currency"].lower():
                raise Conflict("provider refund currency does not match the obligation")
            if amount != intent["amount_cents"]:
                raise Conflict("provider refund amount does not match the intent")
            if payment_intent != payment["provider_id"]:
                raise Conflict("provider refund payment intent does not match the payment")
            if (
                metadata.get("refund_intent_id") != refund_id
                or metadata.get("obligation_id") != intent["obligation_id"]
            ):
                raise Conflict("provider refund metadata identity does not match")
            if intent["provider_refund_id"] != provider_id:
                raise Conflict("provider refund id does not match the stored intent")

            state = intent["state"]
            if state == "succeeded":
                if status != "succeeded":
                    raise Conflict("refund already succeeded")
                return dict(intent)
            if state == "failed":
                if status in ("failed", "canceled"):
                    return dict(intent)
                raise Conflict("refund already failed")
            if status in ("pending", "requires_action"):
                self._refund_set_state(conn, refund_id, "pending")
            elif status in ("failed", "canceled"):
                self._refund_set_state(conn, refund_id, "failed")
            else:
                self._record_money(
                    intent["obligation_id"],
                    actor,
                    intent["amount_cents"],
                    provider_id,
                    f"refund:{refund_id}",
                    operation="refund",
                    method="stripe",
                    provider_id=provider_id,
                    _conn=conn,
                )
                self._refund_set_state(conn, refund_id, "succeeded")
            return dict(self._row(conn, refund_intents, refund_id, lock=False))

    def _refund_store_provider(self, refund_id: str, provider_id: str) -> None:
        with self._transaction() as conn:
            row = self._row(conn, refund_intents, refund_id, lock=True)
            if row["provider_refund_id"] is not None and row["provider_refund_id"] != provider_id:
                raise Conflict("stored provider refund id does not match")
            if row["state"] == "prepared":
                conn.execute(
                    refund_intents.update()
                    .where(refund_intents.c.id == refund_id)
                    .values(
                        state="pending",
                        provider_refund_id=provider_id,
                        last_error=None,
                        updated_at=utcnow(),
                    )
                )

    def _refund_error(self, refund_id: str, message: str | None) -> None:
        with self._transaction() as conn:
            row = self._row(conn, refund_intents, refund_id, lock=True)
            if row["state"] in ("prepared", "pending"):
                conn.execute(
                    refund_intents.update()
                    .where(refund_intents.c.id == refund_id)
                    .values(last_error=message, updated_at=utcnow())
                )

    @staticmethod
    def _refund_set_state(conn, refund_id: str, state: str) -> None:
        conn.execute(
            refund_intents.update()
            .where(refund_intents.c.id == refund_id)
            .values(state=state, last_error=None, updated_at=utcnow())
        )
