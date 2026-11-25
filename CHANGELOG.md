# Changelog

## [0.2.0] - Unreleased

### Added

- Source integration candidate for `townlight-parks` 0.2.0 (`import townlight_parks`) against Townlight Core `1.3.0.dev2`.
- Completed source capabilities: public catalogue and availability, booking and registration, recurring reservations with buffers and closures, private households and participants, sessions with eligibility/capacity/waitlists/attendance, integer-cent ledger and refunds, real 311 maintenance links, approved Communications snapshots, Records private archive, approved aggregate Data export, and notifications outbox.
- Optional hosted Stripe Checkout adapter; no Stripe account is configured and no live charge has been verified.

### Changed

- New source package name `townlight-parks`; target Core `townlight-core==1.3.0.dev2`.
- Documentation links and architecture notes updated for the 0.2.0 source candidate.

No installed build, signed installer, download, or production acceptance is claimed.

## [0.1.1] - 2026-04-28

### Added

- Optional SQLAlchemy-backed registration assistance and maintenance triage workpaper records via `CIVICPARKS_WORKPAPER_DB_URL`.
- Registration assistance and maintenance triage retrieval endpoints for persisted records.

### Changed

- Dependency-alignment release: moved CivicParks to `civiccore==0.3.0` while preserving the existing v0.1.0 runtime foundation behavior.
- Updated CI, verification gates, package metadata, docs, runtime tests, landing page, and public UI labels for the v0.1.1 release.

## [0.1.0] - 2026-04-27

### Added

- CivicParks runtime foundation with FastAPI health, root, public UI, and deterministic API endpoints.
- Parks policy Q&A, program/facility Q&A, registration-link assistance, and maintenance request triage helpers.
- Professional docs, browser QA artifacts, GitHub community templates, placeholder-import gate, release gate, and package build.
