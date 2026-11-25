# Parks domain implementation contracts

Repository composition: ParksRepository(engine, *, bootstrap=False). New SQLAlchemy Core metadata schema townlight_parks, explicit migrations.upgrade(engine) and assert_ready(engine), SQLite schema translation only. store.py supplies Conflict, InvalidInput, NotFound, Unauthorized, utcnow(), new_id(), canonical_bytes(value), strict bounded text/UUID/revision helpers and row-locking fetches. No constructor creates production tables. All public methods require explicit actor for staff mutations; staff API applies actual native authorization.

Tables and column contracts (UUIDs use String(36), UTC-aware timestamps):
- resources(id PK, revision positive, snapshot JSON, created_at, updated_at). Snapshot carries facility name/address/timezone/amenities/accessibility/hours, public flag, approval_required, buffer_before_minutes/buffer_after_minutes, fee_cents/deposit_cents/currency.
- closures(id PK, resource_id FK, starts_at, ends_at, reason, actor, created_at), end greater than start.
- households(id PK, tracking_hash unique, encrypted_tracking_token, snapshot JSON, created_at, updated_at). Snapshot contains private contact details. Token retained encrypted only for private transactional links.
- participants(id PK, household_id FK, revision, snapshot JSON, created_at, updated_at). Name, birth date and accommodations are private; never catalogue fields.
- bookings(id PK, resource_id FK, household_id FK, revision, series_id, starts_at, ends_at, occupied_from, occupied_until, state, purpose, created_at, updated_at). State pending/confirmed/cancelled/rejected. Immutable events retain changes; recurring creation is atomic under resource lock. Cancellation releases occupancy by state.
- programs(id PK, revision, snapshot JSON, created_at, updated_at). Public program descriptions and eligibility rules.
- sessions(id PK, program_id FK, revision, starts_at, ends_at, capacity positive, fee_cents nonnegative, currency, state open/closed/cancelled, created_at, updated_at).
- registrations(id PK, session_id FK, participant_id FK, revision, state confirmed/waitlisted/cancelled, queue_order integer, checked_in_at nullable, created_at, updated_at). Unique(session_id, participant_id); cancelled enrollment can be explicitly reactivated under session lock without duplicate rows.
- obligations(id PK, entity_kind booking/registration, entity_id, amount_cents, deposit_cents, currency, created_at). Unique(entity_kind, entity_id); never silently change an existing financial obligation when a schedule changes.
- ledger(id PK, obligation_id FK, operation payment/refund, amount_cents positive, method offline/stripe, reference, idempotency_key unique, provider_id nullable unique, actor, created_at). Append-only; totals validated with obligation FOR UPDATE. No card details.
- events(id PK, entity_kind, entity_id, revision, kind, actor, payload JSON, created_at).
- command_receipts(command_id PK, operation, payload_sha256, result JSON, created_at). Same transaction as canonical resident mutation; PostgreSQL advisory lock protects duplicate command identities.
- integration_links(id PK, resource_id FK, request_id unique, command_id unique, created_at). Actual 311 identity, never only a boolean.
- attachments(id PK, entity_kind, entity_id, name, media_type, content binary, sha256, size, actor, created_at). Bounded actual bytes.
- outbox(id PK, entity_kind, entity_id, recipient, encrypted_body, message_id unique, state pending/claimed/sent/failed/uncertain, claim_token nullable, lease_until nullable, attempts, next_attempt_at, last_error nullable, created_at). Shared SMTP transport; known-safe retry only.

Keep migrations composed from imported domain metadata. Tables for hosted checkout intents and publication/archive receipts can be added by their owning bounded assignment before the first installer; preserve this common metadata object. Do not make reflection-driven or fallback constructors. Tests import concrete public methods and real tables, with PostgreSQL concurrency tests for resource/session serialization.
