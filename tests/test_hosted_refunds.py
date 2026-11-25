from __future__ import annotations

import uuid

import pytest
from test_catalog_households import repo as repo  # noqa PLC0414
from test_hosted_checkout import _confirmed_obligation, _ledger_rows

from townlight_parks.store import Conflict


def _stripe_payment(repo, obligation_id):
    payment = repo._record_money(
        obligation_id,
        "staff",
        120,
        "verified stripe payment",
        f"stripe-pay-{uuid.uuid4().hex}",
        operation="payment",
        method="stripe",
        provider_id="pi_test" + uuid.uuid4().hex,
    )
    assert (payment["method"], payment["amount_cents"]) == ("stripe", 120)
    return payment


def _refund_rows(repo, obligation_id):
    return [
        row
        for row in _ledger_rows(repo, obligation_id)
        if row["operation"] == "refund"
    ]


class ControlledRefundProvider:
    """In-memory verified-provider double; no network and no real charge."""

    def __init__(self, payment_intent, obligation_id, *, timeout_once=False):
        self.payment_intent = payment_intent
        self.obligation_id = obligation_id
        self.timeout_once = timeout_once
        self.accepted = False
        self.refund_id = "re_test" + uuid.uuid4().hex
        self.next_status = "pending"
        self.create_calls = []
        self.retrieve_calls = []
        self.last_result = None

    def create_refund(self, refund_id, payment_intent, amount_cents, metadata):
        self.create_calls.append(
            (refund_id, payment_intent, amount_cents, dict(metadata))
        )
        assert payment_intent == self.payment_intent
        assert amount_cents == 20
        assert dict(metadata) == {
            "refund_intent_id": refund_id,
            "obligation_id": self.obligation_id,
        }
        self.last_result = {
            "id": self.refund_id,
            "status": "pending",
            "currency": "usd",
            "amount": 20,
            "payment_intent": payment_intent,
            "metadata": dict(metadata),
        }
        if self.timeout_once and not self.accepted:
            self.accepted = True
            raise TimeoutError("provider accepted but response timed out")
        return self.last_result

    def retrieve_refund(self, provider_id):
        self.retrieve_calls.append(provider_id)
        assert provider_id == self.refund_id
        return {**self.last_result, "status": self.next_status}


def test_hosted_refund_prepare_pending_succeeded_replay_and_offline_isolation(repo):
    _household, obligation_id = _confirmed_obligation(repo, "Hosted Refund Hall")
    payment = _stripe_payment(repo, obligation_id)
    first = repo.refund_prepare(
        payment["id"], "staff", 20, "partial refund", "refund-key-1"
    )
    assert first["state"] == "prepared"
    assert first["amount_cents"] == 20
    replay = repo.refund_prepare(
        payment["id"], "staff", 20, "partial refund", "refund-key-1"
    )
    assert replay["id"] == first["id"]
    assert replay["state"] == "prepared"
    with pytest.raises(Conflict):
        repo.refund_prepare(
            payment["id"], "staff", 21, "partial refund", "refund-key-1"
        )
    with pytest.raises(Conflict):
        repo.refund_prepare(
            payment["id"], "staff", 20, "changed payload", "refund-key-1"
        )
    with pytest.raises(Conflict):
        repo.refund_prepare(
            payment["id"], "staff", 120, "over reserve", "refund-key-2"
        )

    provider = ControlledRefundProvider(
        payment["provider_id"], obligation_id
    )
    pending = repo.refund_submit(first["id"], "staff", provider)
    assert pending["state"] == "pending"
    assert pending["provider_refund_id"] == provider.refund_id
    assert pending["last_error"] is None
    assert provider.refund_id.startswith("re_test")
    assert provider.last_result == {
        "id": provider.refund_id,
        "status": "pending",
        "currency": "usd",
        "amount": 20,
        "payment_intent": payment["provider_id"],
        "metadata": {
            "refund_intent_id": first["id"],
            "obligation_id": obligation_id,
        },
    }
    assert _refund_rows(repo, obligation_id) == []

    provider.next_status = "succeeded"
    completed = repo.refund_submit(first["id"], "staff", provider)
    assert completed["state"] == "succeeded"
    assert provider.retrieve_calls == [provider.refund_id]
    refunds = _refund_rows(repo, obligation_id)
    assert len(refunds) == 1
    assert refunds[0]["amount_cents"] == 20
    assert refunds[0]["provider_id"] == provider.refund_id

    replay = repo.refund_submit(first["id"], "staff", provider)
    assert replay["state"] == "succeeded"
    assert len(provider.create_calls) == 1
    assert len(provider.retrieve_calls) == 1
    assert len(_refund_rows(repo, obligation_id)) == 1
    receipt = repo.payment_receipt(obligation_id)
    assert receipt["totals"]["paid_cents"] == 120
    assert receipt["totals"]["refunded_cents"] == 20
    assert receipt["totals"]["net_paid_cents"] == 100

    with pytest.raises(Conflict):
        repo.record_offline_refund(
            obligation_id, "staff", 10, "offline stripe attempt", "offline-refund-1"
        )
    assert len(_refund_rows(repo, obligation_id)) == 1


def test_hosted_refund_provider_mismatches_rejected_without_refund_ledger(repo):
    _household, obligation_id = _confirmed_obligation(
        repo, "Hosted Refund Mismatch Hall"
    )
    payment = _stripe_payment(repo, obligation_id)
    intent = repo.refund_prepare(
        payment["id"], "staff", 20, "mismatch checks", "refund-mismatch-key"
    )
    provider = ControlledRefundProvider(payment["provider_id"], obligation_id)
    assert repo.refund_submit(intent["id"], "staff", provider)["state"] == "pending"
    base = provider.last_result
    bad_payloads = [
        {**base, "amount": 21},
        {**base, "currency": "eur"},
        {**base, "payment_intent": "pi_test" + uuid.uuid4().hex},
        {**base, "id": "re_test" + uuid.uuid4().hex},
    ]
    for bad in bad_payloads:
        with pytest.raises(Conflict):
            repo.refund_apply(intent["id"], "staff", bad)
        assert _refund_rows(repo, obligation_id) == []


def test_hosted_refund_timeout_after_provider_accepts_resubmits_same_uuid(repo):
    _household, obligation_id = _confirmed_obligation(
        repo, "Hosted Refund Timeout Hall"
    )
    payment = _stripe_payment(repo, obligation_id)
    intent = repo.refund_prepare(
        payment["id"], "staff", 20, "timeout retry", "refund-timeout-key"
    )
    provider = ControlledRefundProvider(
        payment["provider_id"], obligation_id, timeout_once=True
    )
    with pytest.raises(TimeoutError):
        repo.refund_submit(intent["id"], "staff", provider)
    prepared = repo.refund_get(intent["id"])
    assert prepared["state"] == "prepared"
    assert prepared["provider_refund_id"] is None
    assert prepared["last_error"] == "refund provider request failed"
    assert _refund_rows(repo, obligation_id) == []

    retried = repo.refund_submit(intent["id"], "staff", provider)
    assert retried["state"] == "pending"
    assert [call[0] for call in provider.create_calls] == [
        intent["id"], intent["id"]
    ]
    assert len(provider.create_calls) == 2
    assert _refund_rows(repo, obligation_id) == []

    provider.next_status = "succeeded"
    completed = repo.refund_submit(intent["id"], "staff", provider)
    assert completed["state"] == "succeeded"
    assert provider.retrieve_calls == [provider.refund_id]
    assert len(_refund_rows(repo, obligation_id)) == 1
    replay = repo.refund_submit(intent["id"], "staff", provider)
    assert replay["state"] == "succeeded"
    assert len(provider.create_calls) == 2
    assert len(provider.retrieve_calls) == 1
    assert len(_refund_rows(repo, obligation_id)) == 1
