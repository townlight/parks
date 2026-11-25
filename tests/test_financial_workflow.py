from __future__ import annotations

import pytest
from sqlalchemy import func, select
from test_booking_workflow import _at, _household, _resource
from test_booking_workflow import repo as repo  # noqa: PLC0414 - pytest fixture re-export

from townlight_parks.models import ledger
from townlight_parks.store import Conflict, InvalidInput


def _booking(repo, name="Finance Hall", *, household=None):
    resource = _resource(repo, name)
    if household is None:
        household = _household(repo)
    start, end = _at()
    created = repo.booking_create(
        resource["id"],
        household["id"],
        "staff",
        resource["revision"],
        [{"starts_at": start, "ends_at": end}],
        name,
    )
    return created["bookings"][0]["obligation_id"]


def test_offline_payment_receipt_replay_idempotency_and_append_only(repo):
    obligation_id = _booking(repo, "Payment Hall")
    first = repo.record_offline_payment(
        obligation_id, "staff", 100, "deposit", "pay-a"
    )
    second = repo.record_offline_payment(
        obligation_id, "staff", 20, "balance", "pay-b"
    )
    assert first["id"] != second["id"]
    state = repo.obligation_get(obligation_id)
    assert state["amount_cents"] == 120
    assert state["deposit_cents"] == 20
    assert (
        state["paid_cents"],
        state["refunded_cents"],
        state["net_paid_cents"],
        state["balance_cents"],
    ) == (120, 0, 120, 0)
    receipt = repo.payment_receipt(obligation_id)
    assert receipt["totals"] == {
        "paid_cents": 120,
        "refunded_cents": 0,
        "net_paid_cents": 120,
        "balance_cents": 0,
        "deposit_cents": 20,
    }
    assert repo.record_offline_payment(
        obligation_id, "staff", 100, "deposit", "pay-a"
    )["id"] == first["id"]
    with pytest.raises(Conflict):
        repo.record_offline_payment(obligation_id, "staff", 99, "deposit", "pay-a")
    with pytest.raises(Conflict):
        repo.record_offline_payment(obligation_id, "staff", 100, "changed", "pay-a")
    other_obligation = _booking(repo, "Other Payment Hall")
    with pytest.raises(Conflict):
        repo.record_offline_payment(
            other_obligation, "staff", 100, "deposit", "pay-a"
        )
    with pytest.raises(Conflict):
        repo.record_offline_payment(obligation_id, "staff", 1, "overpay", "pay-c")
    with repo.engine.connect() as conn:
        rows = conn.execute(
            select(ledger).where(ledger.c.obligation_id == obligation_id)
        ).mappings().all()
        total_rows = conn.execute(select(func.count()).select_from(ledger)).scalar_one()
    assert len(rows) == 2
    assert sum(r["amount_cents"] for r in rows if r["operation"] == "payment") == 120
    assert total_rows == 2


def test_offline_refund_replay_limits_and_stripe_method_isolation(repo):
    obligation_id = _booking(repo, "Refund Hall")
    repo.record_offline_payment(
        obligation_id, "staff", 100, "deposit", "refund-pay-a"
    )
    repo.record_offline_payment(
        obligation_id, "staff", 20, "balance", "refund-pay-b"
    )
    refund = repo.record_offline_refund(
        obligation_id, "staff", 20, "partial refund", "refund-a"
    )
    state = repo.obligation_get(obligation_id)
    assert (
        state["paid_cents"],
        state["refunded_cents"],
        state["net_paid_cents"],
        state["balance_cents"],
    ) == (120, 20, 100, 20)
    assert repo.record_offline_refund(
        obligation_id, "staff", 20, "partial refund", "refund-a"
    )["id"] == refund["id"]
    with pytest.raises(Conflict):
        repo.record_offline_refund(
            obligation_id, "staff", 101, "too much", "refund-b"
        )
    with pytest.raises(InvalidInput):
        repo.record_offline_refund(
            obligation_id, "staff", -1, "negative", "refund-c"
        )
    with pytest.raises(InvalidInput):
        repo.record_offline_refund(
            obligation_id, "staff", True, "boolean", "refund-d"
        )
    with repo.engine.connect() as conn:
        refunds = conn.execute(
            select(ledger).where(
                ledger.c.obligation_id == obligation_id,
                ledger.c.operation == "refund",
            )
        ).mappings().all()
    assert len(refunds) == 1
    stripe_obligation = _booking(repo, "Stripe Hall")
    # Controlled verified-adapter-style insert: internal path, no SDK/network.
    stripe_entry = repo._record_money(
        stripe_obligation,
        "staff",
        20,
        "verified stripe",
        "stripe-pay-1",
        operation="payment",
        method="stripe",
        provider_id="pi_test_verified_20",
    )
    assert stripe_entry["method"] == "stripe"
    assert repo.obligation_get(stripe_obligation)["paid_cents"] == 20
    with pytest.raises(Conflict):
        repo.record_offline_refund(
            stripe_obligation, "staff", 20, "offline refund stripe", "refund-e"
        )


def _receipt_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _receipt_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _receipt_keys(child)


def test_reconciliation_actual_totals_bounds_and_private_receipt(repo):
    household = _household(repo)
    first = _booking(repo, "Reconcile A", household=household)
    second = _booking(repo, "Reconcile B", household=household)
    repo.record_offline_payment(first, "staff", 120, "paid in full", "recon-pay-a")
    repo.record_offline_payment(second, "staff", 120, "paid in full", "recon-pay-b")
    repo.record_offline_refund(second, "staff", 20, "partial", "recon-refund-b")
    rows = repo.reconciliation(limit=100, offset=0)
    by_id = {row["obligation_id"]: row for row in rows}
    assert {first, second} <= set(by_id)
    assert by_id[first]["amount_cents"] == 120
    assert by_id[first]["paid_cents"] == 120
    assert by_id[first]["refunded_cents"] == 0
    assert by_id[first]["balance_cents"] == 0
    assert by_id[second]["refunded_cents"] == 20
    assert by_id[second]["net_paid_cents"] == 100
    assert by_id[second]["balance_cents"] == 20
    assert all(row["currency"] == "USD" for row in rows)
    assert all(isinstance(row["obligation_id"], str) for row in rows)
    for kwargs in (
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
        {"offset": -1},
        {"offset": True},
    ):
        with pytest.raises(InvalidInput):
            repo.reconciliation(**kwargs)
    receipt = repo.payment_receipt(first)
    assert receipt["schema_version"] == 1
    assert receipt["currency"] == "USD"
    assert receipt["entity"]["id"] == first
    assert receipt["entity"]["entity_kind"] == "booking"
    assert receipt["totals"]["paid_cents"] == 120
    keys = {key.lower() for key in _receipt_keys(receipt)}
    forbidden = ("household", "participant", "secret", "password", "token", "email", "phone")
    assert not any(word in key for key in keys for word in forbidden)
