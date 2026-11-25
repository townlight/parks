"""Minimal Stripe HTTPS transport for durable Townlight payment intents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx

__all__ = [
    "PaymentProviderError",
    "StripeTransport",
    "is_valid_checkout_url",
]

_API = "https://api.stripe.com/v1"
_VERSION = "2026-11-18.endive"
_LIMIT = 1024 * 1024
_GENERIC = "Stripe request failed"
_CS = re.compile(r"cs_[A-Za-z0-9_]+")
_RE = re.compile(r"re_[A-Za-z0-9_]+")
_PI = re.compile(r"pi_[A-Za-z0-9_]+")
_META_KEY = re.compile(r"[A-Za-z0-9_]{1,40}")


class PaymentProviderError(RuntimeError):
    """A provider call failed without exposing credentials or payloads."""


def _bad_constant(_value: str) -> None:
    raise ValueError("non-finite JSON")


def _text(value: Any, field: str, maximum: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{field} must be bounded text")
    return value


def _amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("amount_cents must be a positive integer")
    return value


def _field(request: Any, name: str) -> Any:
    if not isinstance(request, Mapping):
        raise TypeError("request must be a mapping")
    try:
        return request[name]
    except KeyError as exc:
        raise ValueError(f"{name} is required") from exc


def _metadata(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 40:
        raise ValueError("metadata must be a bounded mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _META_KEY.fullmatch(key) is None:
            raise ValueError("metadata keys must be safe identifiers")
        if not isinstance(item, str) or len(item) > 500:
            raise ValueError("metadata values must be bounded text")
        if any(ord(char) < 32 or ord(char) == 127 for char in item):
            raise ValueError("metadata values must not contain controls")
        result[key] = item
    return result


def _return_url(value: Any, field: str) -> str:
    value = _text(value, field, 2048)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"{field} must be an https URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{field} must be an https URL without credentials or fragment")
    return value


def is_valid_checkout_url(value: Any) -> bool:
    """Return true only for exact Stripe Checkout HTTPS URLs without auth."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "checkout.stripe.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


class StripeTransport:
    """Four fixed-origin Stripe calls; no retries, SDK, or implicit I/O."""

    def __init__(self, secret_key: str, *, client: httpx.Client | None = None):
        self._secret = _text(secret_key, "secret_key", 512)
        self._owned = client is None
        self._client = client or httpx.Client(
            timeout=20.0,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return "<StripeTransport>"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Stripe-Version": _VERSION}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with self._client.stream(
                method,
                _API + path,
                data=data,
                headers=headers,
                auth=httpx.BasicAuth(self._secret, ""),
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > _LIMIT:
                        raise PaymentProviderError(_GENERIC)
                    body.extend(chunk)
                payload = json.loads(bytes(body), parse_constant=_bad_constant)
                if not isinstance(payload, dict) or "error" in payload:
                    raise PaymentProviderError(_GENERIC)
                return payload
        except PaymentProviderError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise PaymentProviderError(_GENERIC) from None

    def create_checkout(self, intent_id: str, request: Any) -> dict[str, Any]:
        intent_id = _text(intent_id, "intent_id")
        amount = _amount(_field(request, "amount_cents"))
        currency = _text(_field(request, "currency"), "currency", 16).lower()
        if currency != "usd":
            raise ValueError("only USD online payments are supported")
        success_url = _return_url(_field(request, "success_url"), "success_url")
        cancel_url = _return_url(_field(request, "cancel_url"), "cancel_url")
        metadata = _metadata(_field(request, "metadata"))
        obligation_id = _text(
            metadata.get("obligation_id"), "metadata.obligation_id"
        )
        if metadata.get("intent_id") not in (None, intent_id):
            raise ValueError("metadata.intent_id does not match intent_id")
        metadata["obligation_id"] = obligation_id
        metadata["intent_id"] = intent_id
        data: dict[str, Any] = {
            "mode": "payment",
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": amount,
            "line_items[0][price_data][product_data][name]": "Townlight Parks",
            "line_items[0][quantity]": 1,
            "client_reference_id": intent_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        for key, value in metadata.items():
            data[f"metadata[{key}]"] = value
        payload = self._request(
            "POST", "/checkout/sessions", data=data, idempotency_key=intent_id
        )
        session_id = payload.get("id")
        if (
            not isinstance(session_id, str)
            or _CS.fullmatch(session_id) is None
            or not is_valid_checkout_url(payload.get("url"))
        ):
            raise PaymentProviderError(_GENERIC)
        return payload

    def retrieve_checkout(self, session_id: str) -> dict[str, Any]:
        session_id = _text(session_id, "session_id")
        if _CS.fullmatch(session_id) is None:
            raise ValueError("session_id must be a Stripe checkout session ID")
        payload = self._request(
            "GET", "/checkout/sessions/" + quote(session_id, safe="")
        )
        if payload.get("id") != session_id:
            raise PaymentProviderError(_GENERIC)
        return payload

    def create_refund(
        self,
        refund_id: str,
        payment_intent_id: str,
        amount_cents: int,
        metadata: Any,
    ) -> dict[str, Any]:
        refund_id = _text(refund_id, "refund_id")
        payment_intent_id = _text(payment_intent_id, "payment_intent_id")
        if _PI.fullmatch(payment_intent_id) is None:
            raise ValueError("payment_intent_id must be a Stripe payment intent ID")
        amount = _amount(amount_cents)
        meta = _metadata(metadata)
        data: dict[str, Any] = {
            "amount": amount,
            "payment_intent": payment_intent_id,
        }
        for key, value in meta.items():
            data[f"metadata[{key}]"] = value
        payload = self._request(
            "POST", "/refunds", data=data, idempotency_key=refund_id
        )
        provider_id = payload.get("id")
        if not isinstance(provider_id, str) or _RE.fullmatch(provider_id) is None:
            raise PaymentProviderError(_GENERIC)
        return payload

    def retrieve_refund(self, refund_id: str) -> dict[str, Any]:
        refund_id = _text(refund_id, "refund_id")
        if _RE.fullmatch(refund_id) is None:
            raise ValueError("refund_id must be a Stripe refund ID")
        payload = self._request("GET", "/refunds/" + quote(refund_id, safe=""))
        if payload.get("id") != refund_id:
            raise PaymentProviderError(_GENERIC)
        return payload
