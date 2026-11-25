"""Hosted-checkout intents for Parks obligations.

The provider transport is injected and only ever called outside a database
transaction.  Browser return URLs and provider redirects are never evidence
of payment; only a verified provider session may record money.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, update

from .models import bookings, obligations, participants, payment_intents, registrations
from .store import Conflict, InvalidInput, NotFound, Unauthorized, identifier, new_id, text, utcnow

_CONTROL = {chr(i) for i in range(32)} | {chr(127)}
_FINAL = ("paid", "expired")


def _bounded(value: Any, limit: int, field: str) -> str:
    value = text(value, limit, field=field)
    if any(char in _CONTROL for char in value):
        raise InvalidInput(f"{field} must not contain control characters")
    return value


def _url(value: Any, field: str) -> str:
    value = _bounded(value, 2048, field)
    parts = urlsplit(value)
    try:
        _ = parts.port
    except ValueError as exc:
        raise InvalidInput(f"{field} must be a valid HTTPS URL") from exc
    if parts.scheme.lower() != "https" or not parts.hostname or parts.fragment:
        raise InvalidInput(f"{field} must be an HTTPS URL without a fragment")
    if parts.username is not None or parts.password is not None:
        raise InvalidInput(f"{field} must not contain credentials")
    return value


def _origin(value: str) -> tuple[str, int]:
    parts = urlsplit(value)
    return parts.hostname.lower(), parts.port or 443


def _checkout_url(value: Any) -> str:
    value = _bounded(value, 2048, "checkout_url")
    parts = urlsplit(value)
    if (parts.scheme.lower() != "https" or parts.netloc.lower() != "checkout.stripe.com"
            or parts.username is not None or parts.password is not None):
        raise Conflict("provider checkout URL is not the Stripe Checkout origin")
    return value


def _provider_result(data: Any, intent_id: str, amount: int) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise InvalidInput("provider result must be an object")
    session_id = _bounded(data.get("id"), 255, "provider session id")
    if not session_id.startswith("cs_"):
        raise Conflict("provider result has an invalid session id")
    if data.get("client_reference_id") != intent_id:
        raise Conflict("provider result does not reference this intent")
    metadata = data.get("metadata")
    if metadata != {"obligation_id": data.get("_obligation_id"), "intent_id": intent_id}:
        raise Conflict("provider result metadata does not match the checkout intent")
    if data.get("currency") != "usd":
        raise Conflict("provider result currency is not usd")
    total = data.get("amount_total")
    if isinstance(total, bool) or not isinstance(total, int) or total != amount:
        raise Conflict("provider result amount does not match the checkout intent")
    status = _bounded(data.get("status"), 32, "provider status")
    payment_status = _bounded(data.get("payment_status"), 32, "provider payment status")
    pi = data.get("payment_intent")
    if pi is not None:
        pi = _bounded(pi, 255, "provider payment intent")
        if not pi.startswith("pi_"):
            raise Conflict("provider result has an invalid payment intent")
    url = data.get("checkout_url", data.get("url"))
    return {"id": session_id, "status": status, "payment_status": payment_status,
            "payment_intent": pi, "url": url}


class CheckoutMixin:
    """Durable prepared/open hosted checkout sessions."""

    def checkout_prepare(self, obligation_id, actor, command_key, success_url, cancel_url,
                         *, _conn=None) -> dict[str, Any]:
        obligation_id = identifier(obligation_id)
        actor = _bounded(actor, 128, "actor")
        key = _bounded(command_key, 255, "command_key")
        success_url, cancel_url = _url(success_url, "success_url"), _url(cancel_url, "cancel_url")
        if _origin(success_url) != _origin(cancel_url):
            raise InvalidInput("success_url and cancel_url must have the same origin")
        with self._transaction(_conn) as conn:
            self._serialize_idempotency_key(conn, key)
            existing = conn.execute(select(payment_intents).where(
                payment_intents.c.command_key == key)).mappings().first()
            if existing is not None:
                request = existing["request"]
                metadata = request.get("metadata") if isinstance(request, dict) else None
                if (existing["obligation_id"] != obligation_id or not isinstance(request, dict)
                        or request.get("currency") != "usd"
                        or request.get("success_url") != success_url
                        or request.get("cancel_url") != cancel_url
                        or metadata != {"obligation_id": obligation_id,
                                        "intent_id": existing["id"]}):
                    raise Conflict("command_key is already used by a different checkout request")
                return dict(existing)
            obligation = self._row(conn, obligations, obligation_id, lock=True)
            active = conn.execute(select(payment_intents.c.id).where(
                payment_intents.c.obligation_id == obligation_id,
                payment_intents.c.state.in_(("prepared", "open")))).first()
            if active is not None:
                raise Conflict("obligation already has an active checkout intent")
            if (obligation["currency"] or "").lower() != "usd":
                raise Conflict("online checkout supports USD obligations only")
            amount = self._totals(conn, obligation_id)["balance_cents"]
            if amount <= 0:
                raise Conflict("obligation has no positive balance to pay")
            self._checkout_owner(conn, obligation, actor)
            intent_id, now = new_id(), utcnow()
            request = {"amount_cents": amount, "currency": "usd",
                       "success_url": success_url, "cancel_url": cancel_url,
                       "metadata": {"obligation_id": obligation_id, "intent_id": intent_id}}
            row = {"id": intent_id, "obligation_id": obligation_id, "command_key": key,
                   "request": request, "amount_cents": amount, "currency": "usd",
                   "state": "prepared", "provider_session_id": None, "checkout_url": None,
                   "provider_payment_id": None, "last_error": None,
                   "created_at": now, "updated_at": now}
            conn.execute(payment_intents.insert().values(**row))
            return row

    def checkout_get(self, intent_id) -> dict[str, Any]:
        intent_id = identifier(intent_id)
        with self._transaction() as conn:
            return dict(self._row(conn, payment_intents, intent_id))

    def checkout_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise InvalidInput("limit must be an integer between 1 and 50")
        with self._transaction() as conn:
            rows = conn.execute(select(payment_intents).where(
                payment_intents.c.state.in_(("prepared", "open")))
                .order_by(payment_intents.c.created_at, payment_intents.c.id)
                .limit(limit)).mappings().all()
        return [dict(row) for row in rows]

    def checkout_apply(self, intent_id, actor, provider_result, *, _conn=None) -> dict[str, Any]:
        intent_id, actor = identifier(intent_id), _bounded(actor, 128, "actor")
        with self._transaction(_conn) as conn:
            self._serialize_idempotency_key(conn, f"checkout:{intent_id}")
            probe = conn.execute(select(payment_intents.c.obligation_id).where(
                payment_intents.c.id == intent_id)).mappings().first()
            if probe is None:
                raise NotFound("payment intent not found")
            self._row(conn, obligations, probe["obligation_id"], lock=True)
            intent = self._row(conn, payment_intents, intent_id, lock=True)
            data = dict(provider_result) if isinstance(provider_result, dict) else provider_result
            if isinstance(data, dict):
                data = {**data, "_obligation_id": intent["obligation_id"]}
            result = _provider_result(data, intent_id, intent["amount_cents"])
            if (intent["provider_session_id"] is not None
                    and result["id"] != intent["provider_session_id"]):
                raise Conflict("provider session id does not match the checkout intent")
            state = intent["state"]
            if state == "paid":
                if (result["status"] == "complete" and result["payment_status"] == "paid"
                        and result["id"] == intent["provider_session_id"]
                        and result["payment_intent"] == intent["provider_payment_id"]):
                    return dict(intent)
                raise Conflict("payment intent is already paid with different provider evidence")
            if state == "expired":
                if (result["status"] == "expired" and result["payment_status"] != "paid"
                        and (intent["provider_session_id"] is None
                             or result["id"] == intent["provider_session_id"])):
                    return dict(intent)
                raise Conflict("payment intent is expired with different provider evidence")
            if state not in ("prepared", "open"):
                raise Conflict("payment intent is not open for provider updates")
            if result["status"] == "expired":
                if result["payment_status"] == "paid":
                    raise Conflict("provider returned conflicting expired and paid states")
                return self._save_intent(conn, intent_id, state="expired",
                                         provider_session_id=result["id"])
            if result["status"] == "open":
                url = _checkout_url(result["url"])
                return self._save_intent(conn, intent_id, state="open",
                                         provider_session_id=result["id"], checkout_url=url,
                                         last_error=None)
            if result["status"] == "complete" and result["payment_status"] == "paid":
                if result["payment_intent"] is None:
                    raise Conflict("paid checkout has no payment intent")
                self._record_money(intent["obligation_id"], actor, intent["amount_cents"],
                                   f"stripe checkout {intent_id}", f"checkout:{intent_id}",
                                   operation="payment", method="stripe",
                                   provider_id=result["payment_intent"], _conn=conn)
                return self._save_intent(conn, intent_id, state="paid",
                                         provider_session_id=result["id"],
                                         provider_payment_id=result["payment_intent"],
                                         last_error=None)
            raise Conflict("provider checkout is not in a payable state")

    def checkout_submit(self, intent_id, actor, transport) -> dict[str, Any]:
        row = self.checkout_get(intent_id)
        intent_id = row["id"]
        if row["state"] in _FINAL:
            return row
        try:
            if row["state"] == "open":
                if not row["provider_session_id"]:
                    raise Conflict("open checkout has no provider session")
                result = transport.retrieve_checkout(row["provider_session_id"])
            elif row["state"] == "prepared":
                result = transport.create_checkout(intent_id, row["request"])
            else:
                raise Conflict("payment intent cannot be submitted")
            return self.checkout_apply(intent_id, actor, result)
        except Exception:
            self._checkout_error(intent_id)
            raise

    def checkout_poll(self, intent_id, actor, transport) -> dict[str, Any]:
        row = self.checkout_get(intent_id)
        if row["state"] in _FINAL:
            return row
        if not row["provider_session_id"]:
            raise Conflict("payment intent has no provider session")
        result = transport.retrieve_checkout(row["provider_session_id"])
        return self.checkout_apply(row["id"], actor, result)

    @staticmethod
    def _save_intent(conn, intent_id: str, **values) -> dict[str, Any]:
        values["updated_at"] = utcnow()
        conn.execute(update(payment_intents).where(
            payment_intents.c.id == intent_id).values(**values))
        return dict(conn.execute(select(payment_intents).where(
            payment_intents.c.id == intent_id)).mappings().one())

    def _checkout_error(self, intent_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(update(payment_intents).where(
                payment_intents.c.id == intent_id,
                payment_intents.c.state.in_(("prepared", "open"))).values(
                    last_error="provider request failed", updated_at=utcnow()))

    @staticmethod
    def _checkout_owner(conn, obligation, actor: str) -> None:
        kind, entity_id = obligation["entity_kind"], obligation["entity_id"]
        if kind == "booking":
            row = conn.execute(select(bookings.c.household_id, bookings.c.state).where(
                bookings.c.id == entity_id)).mappings().first()
            state, owner = (row["state"], row["household_id"]) if row else (None, None)
        elif kind == "registration":
            row = conn.execute(select(registrations.c.state, participants.c.household_id)
                               .select_from(registrations.join(
                                   participants,
                                   registrations.c.participant_id == participants.c.id))
                               .where(registrations.c.id == entity_id)).mappings().first()
            state, owner = (row["state"], row["household_id"]) if row else (None, None)
        else:
            raise Conflict("obligation is not linked to a bookable entity")
        if row is None:
            raise NotFound("obligation entity not found")
        if actor != f"resident:{owner}":
            raise Unauthorized("actor does not own the obligation entity")
        if state != "confirmed":
            if state == "pending":
                raise Conflict("pending bookings require approval before payment")
            raise Conflict("obligation entity is not confirmed")
