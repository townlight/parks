from __future__ import annotations

import base64
import uuid
from datetime import UTC
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select
from test_booking_workflow import _at, _household, _resource
from test_booking_workflow import repo as repo  # noqa: PLC0414 - pytest fixture re-export

from townlight_parks.models import ledger
from townlight_parks.store import Conflict, InvalidInput, Unauthorized
from townlight_parks.stripe_transport import PaymentProviderError, StripeTransport

SUCCESS = "https://parks.example/checkout/success?ref=hosted"
CANCEL = "https://parks.example/checkout/cancel?ref=hosted"


def _confirmed_obligation(repo, name):
    resource = _resource(repo, name, public=True, approval_required=False)
    household = _household(repo)
    start, end = _at()
    created = repo.booking_create(
        resource["id"], household["id"], "staff", resource["revision"],
        [{"starts_at": start, "ends_at": end}], name,
    )
    booking = repo.booking_get(created["bookings"][0]["id"])
    assert booking["state"] == "confirmed"
    return household, booking["obligation_id"]


def _ledger_rows(repo, obligation_id):
    with repo.engine.connect() as conn:
        return conn.execute(
            select(ledger).where(ledger.c.obligation_id == obligation_id)
        ).mappings().all()


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _assert_same_with_utc_timestamps(first, second):
    assert first.keys() == second.keys()
    for key in first:
        if key in ("created_at", "updated_at"):
            assert _utc(first[key]) == _utc(second[key])
        else:
            assert first[key] == second[key]


class ControlledProvider:
    """Controlled in-memory provider; no network and no charges."""

    def __init__(self, obligation_id, amount, *, create_status="open"):
        self.obligation_id = obligation_id
        self.amount = amount
        self.create_status = create_status
        self.intent_id = None
        self.polls = 0
        self.paid = None
        self.session_id = "cs_test" + uuid.uuid4().hex
        self.payment_intent_id = "pi_test" + uuid.uuid4().hex

    def _payload(self, intent_id, status, payment_status, payment_intent=None,
                 session_id=None):
        session_id = self.session_id if session_id is None else session_id
        return {
            "id": session_id,
            "client_reference_id": intent_id,
            "metadata": {"obligation_id": self.obligation_id, "intent_id": intent_id},
            "currency": "usd",
            "amount_total": self.amount,
            "status": status,
            "payment_status": payment_status,
            "payment_intent": payment_intent,
            "url": f"https://checkout.stripe.com/c/pay/{session_id}#fragment",
        }

    def create_checkout(self, intent_id, request):
        assert request["amount_cents"] == self.amount
        self.intent_id = intent_id
        return self._payload(intent_id, self.create_status, "unpaid")

    def retrieve_checkout(self, session_id):
        assert session_id == self.session_id
        self.polls += 1
        if self.polls == 1:
            return self._payload(self.intent_id, "open", "unpaid")
        self.paid = self._payload(
            self.intent_id, "complete", "paid",
            payment_intent=self.payment_intent_id,
        )
        return self.paid


def test_prepare_replay_active_conflict_owner_and_https_origin(repo):
    household, obligation_id = _confirmed_obligation(repo, "Hosted Prepare Hall")
    actor = f"resident:{household['id']}"
    first = repo.checkout_prepare(
        obligation_id, actor, "checkout-key-1", SUCCESS, CANCEL
    )
    assert first["state"] == "prepared"
    assert first["amount_cents"] == 120
    replay = repo.checkout_prepare(
        obligation_id, actor, "checkout-key-1", SUCCESS, CANCEL
    )
    _assert_same_with_utc_timestamps(replay, first)
    with pytest.raises(Conflict):
        repo.checkout_prepare(
            obligation_id, actor, "checkout-key-2", SUCCESS, CANCEL
        )
    with pytest.raises(Conflict):
        repo.record_offline_payment(
            obligation_id, "staff", 10, "manual while hosted", "offline-active"
        )
    _other_household, other_obligation = _confirmed_obligation(
        repo, "Hosted Owner Hall"
    )
    with pytest.raises(Unauthorized):
        repo.checkout_prepare(
            other_obligation, actor, "checkout-wrong-owner", SUCCESS, CANCEL
        )
    with pytest.raises(InvalidInput):
        repo.checkout_prepare(
            obligation_id, actor, "checkout-http", "http://parks.example/s",
            "http://parks.example/c",
        )
    with pytest.raises(InvalidInput):
        repo.checkout_prepare(
            obligation_id, actor, "checkout-origin", SUCCESS,
            "https://other.example/cancel",
        )


def test_controlled_provider_open_poll_paid_replay_mismatch_and_expiry(repo):
    household, obligation_id = _confirmed_obligation(repo, "Hosted Provider Hall")
    actor = f"resident:{household['id']}"
    prepared = repo.checkout_prepare(
        obligation_id, actor, "provider-key", SUCCESS, CANCEL
    )
    provider = ControlledProvider(obligation_id, prepared["amount_cents"])
    submitted = repo.checkout_submit(prepared["id"], actor, provider)
    assert (submitted["state"], submitted["provider_session_id"]) == (
        "open", provider.session_id
    )
    assert submitted["checkout_url"].startswith(
        "https://checkout.stripe.com/c/pay/"
    )
    first_poll = repo.checkout_poll(prepared["id"], actor, provider)
    assert (first_poll["state"], first_poll["provider_payment_id"]) == ("open", None)
    paid = repo.checkout_poll(prepared["id"], actor, provider)
    assert (paid["state"], paid["provider_payment_id"]) == (
        "paid", provider.payment_intent_id
    )
    replay = repo.checkout_apply(prepared["id"], actor, provider.paid)
    assert replay["state"] == "paid"
    assert repo.checkout_poll(prepared["id"], actor, provider)["state"] == "paid"
    rows = _ledger_rows(repo, obligation_id)
    assert len(rows) == 1
    assert (rows[0]["operation"], rows[0]["amount_cents"]) == ("payment", 120)

    mismatch_household, mismatch_obligation = _confirmed_obligation(
        repo, "Hosted Mismatch Hall"
    )
    mismatch_actor = f"resident:{mismatch_household['id']}"
    mismatch_intent = repo.checkout_prepare(
        mismatch_obligation, mismatch_actor, "mismatch-key", SUCCESS, CANCEL
    )
    mismatch_provider = ControlledProvider(
        mismatch_obligation, mismatch_intent["amount_cents"]
    )
    assert repo.checkout_submit(
        mismatch_intent["id"], mismatch_actor, mismatch_provider
    )["state"] == "open"
    open_payload = mismatch_provider._payload(
        mismatch_intent["id"], "open", "unpaid"
    )
    bad_payloads = [
        {**open_payload, "amount_total": open_payload["amount_total"] + 1},
        {**open_payload, "id": mismatch_provider.session_id + "_other"},
        {**open_payload, "currency": "eur"},
        {**open_payload, "metadata": {
            "obligation_id": "ob_other", "intent_id": mismatch_intent["id"],
        }},
    ]
    for bad in bad_payloads:
        with pytest.raises(Conflict):
            repo.checkout_apply(mismatch_intent["id"], mismatch_actor, bad)
        assert _ledger_rows(repo, mismatch_obligation) == []

    expire_household, expire_obligation = _confirmed_obligation(
        repo, "Hosted Expiry Hall"
    )
    expire_actor = f"resident:{expire_household['id']}"
    expire_intent = repo.checkout_prepare(
        expire_obligation, expire_actor, "expiry-key", SUCCESS, CANCEL
    )
    expired = repo.checkout_submit(
        expire_intent["id"], expire_actor,
        ControlledProvider(
            expire_obligation, expire_intent["amount_cents"],
            create_status="expired",
        ),
    )
    assert expired["state"] == "expired"
    repo.record_offline_payment(
        expire_obligation, "staff", expire_intent["amount_cents"],
        "provider expired", "offline-after-expiry",
    )
    assert repo.obligation_get(expire_obligation)["balance_cents"] == 0


def test_stripe_transport_fixed_origin_form_auth_version_and_redaction(repo):
    secret = "sk_test_hosted_secret"
    intent_id = "intent_transport_1"
    provider = {
        "id": "cs_test123",
        "client_reference_id": intent_id,
        "metadata": {"obligation_id": "ob_controlled", "intent_id": intent_id},
        "currency": "usd",
        "amount_total": 120,
        "status": "open",
        "payment_status": "unpaid",
        "url": "https://checkout.stripe.com/c/pay/cs_test123#fragment",
    }
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            if request.url.path.endswith("/cs_test_redirect"):
                return httpx.Response(
                    302, headers={"Location": "https://elsewhere.invalid/leak"}
                )
            if request.url.path.endswith("/cs_test_fail"):
                return httpx.Response(500, json={"error": {"message": secret}})
            assert request.url.path == "/v1/checkout/sessions/cs_test123"
            return httpx.Response(200, json=provider)
        assert str(request.url) == "https://api.stripe.com/v1/checkout/sessions"
        assert request.headers["Stripe-Version"] == "2026-11-18.endive"
        assert request.headers["Idempotency-Key"] == intent_id
        expected = base64.b64encode(f"{secret}:".encode()).decode()
        assert request.headers["Authorization"] == f"Basic {expected}"
        form = parse_qs(request.content.decode())
        assert form["mode"] == ["payment"]
        assert form["client_reference_id"] == [intent_id]
        assert form["line_items[0][price_data][unit_amount]"] == ["120"]
        assert form["metadata[obligation_id]"] == ["ob_controlled"]
        assert form["metadata[intent_id]"] == [intent_id]
        assert form["success_url"] == ["https://parks.example/success"]
        return httpx.Response(200, json=provider)

    request = {
        "amount_cents": 120,
        "currency": "usd",
        "success_url": "https://parks.example/success",
        "cancel_url": "https://parks.example/cancel",
        "metadata": {"obligation_id": "ob_controlled", "intent_id": intent_id},
    }
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        StripeTransport(secret, client=client) as transport,
    ):
        assert secret not in repr(transport)
        created = transport.create_checkout(intent_id, request)
        assert created["id"] == "cs_test123"
        opened = transport.retrieve_checkout("cs_test123")
        assert opened["url"] == provider["url"]
        with pytest.raises(PaymentProviderError) as failure:
            transport.retrieve_checkout("cs_test_fail")
        assert secret not in str(failure.value)
        with pytest.raises(PaymentProviderError):
            transport.retrieve_checkout("cs_test_redirect")
    assert all(str(item.url).startswith("https://api.stripe.com/") for item in seen)
    assert not any("elsewhere.invalid" in str(item.url) for item in seen)
