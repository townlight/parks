# Townlight Parks 0.2.0

**Status:** Townlight Parks 0.2.0 ships in the signed [Townlight for Windows 1.10.0.1](https://github.com/townlight/townlight/releases/tag/townlight-windows-v1.10.0.1) public beta. This describes the `townlight-parks` 0.2.0 source tree (`import townlight_parks`) against Townlight Core `1.3.0.dev2`.

In the [release acceptance record](https://github.com/townlight/townlight/releases/download/townlight-windows-v1.10.0.1/ACCEPTANCE-Windows-1.10.0.1.md): Hosted Windows checks covered resident Parks, reservations, enrollment, payments, refunds, maintenance, Records archiving and recovery. The staff workflow completed eight observed milestones across 311, Communications and Parks. The earlier Windows release `1.9.3.2` does **not** include Parks.

## What Parks 0.2.0 implements

Parks adds a real parks and recreation workflow to the Townlight Suite source:

- Public facility and program catalogue, availability, booking and registration forms.
- Facility reservations with recurrence, setup/cleanup buffers, closures/blackouts, rescheduling and cancellation. Resource and session rows are locked in the same transaction as overlap checks and occupancy writes; recurrences are all-or-nothing and lock in stable order.
- Households, participants and minor/accessibility/contact data kept private. Public pages expose catalogue and availability only, never household, participant, minor, accommodation, contact or private tracking details.
- Programs and sessions with eligibility, capacity, enrollment, waitlists, promotion, private rosters and idempotent attendance check-in.
- Fees, deposits, refunds and reconciliation in integer cents with explicit currency. Offline payments and refunds are recorded in an append-only ledger with staff actor and reference.
- Optional hosted Stripe Checkout adapter for online payment. No Stripe account is configured by default, and **no live charge has been verified**. Card data does not enter Townlight. Provider return URLs are not payment proof; the host retrieves and verifies provider transaction, amount, currency and obligation identity before recording a ledger payment.
- Maintenance links create real 311 requests with durable idempotency, actual location/photo bytes and returned 311 request identity.
- Explicitly approved public closures and program descriptions can become Communications source snapshots.
- Records private ZIP archive/readback and approved aggregate Data export. Records archive access remains permission-controlled; Data exports exclude participant/contact/accommodation details.

## Suite context and roles

This is part of the Townlight Suite: TypeScript staff surfaces plus Python runtime, resident portal and encrypted command handling. Existing roles remain `local-admin`, `city-staff` and `clerk`; Records archive permission is separate. Parks reuses the shared command/session proof and allocation-receipt approach used by 311 and Communications.

A standalone `main.py` is retained as a foundation/demo entry point. It is **not** the full Suite authentication path, not a normal installer, and not the ordinary city-staff deployment route. Ordinary city staff should use the signed Townlight for Windows 1.10.0.1 installer.

## Source installation and migrations

- Target: Windows, Python `>=3.11`.
- Package: `townlight-parks==0.2.0`; import: `townlight_parks`; Core target: `townlight-core==1.3.0.dev2`.
- For development, install Core from the pinned `townlight/core` git commit `2f6ba9be4a95e3772dc47a3682ac780c5283b626`. The `townlight-core==1.3.0.dev2` package requirement is not a promise of PyPI availability.
- Database schema: `townlight_parks`. Production bootstrap is `False`; tables are created only through explicit, programmatic migrations (`migrations.upgrade(engine)`, then `migrations.assert_ready(engine)` via the package migrations module). Parks 0.2.0 does not add a migration CLI.
- Configure database credentials and hosted payment secrets outside source control. Do not place secrets or card details in configuration files, logs or the repository.

## Privacy, concurrency and recovery

Private tracking links, CSRF checks and session ownership protect resident commands. Durable command identities prevent replay duplication; queued work is not confirmed until the host applies it.

Back up the database, managed files, portal inbox, profile encryption keys and publisher queue together. Restart must reopen durable requests and resume known-safe jobs without duplicates. Payment intents and refund intents persist across restart; ambiguous provider outcomes are reconciled before retry.

## Documentation and licensing

- [User manual](USER-MANUAL.md)
- [Architecture notes](docs/architecture.md)
- [Landing page](docs/index.html)

Licensing remains as existing: code under AGPL-3.0-only with the existing public-institution exception; documentation under CC BY. See `LICENSE-CODE` and `LICENSE-DOCS`.
