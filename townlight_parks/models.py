"""SQLAlchemy Core definitions for the Parks domain.

This module only declares shared metadata. It does not create an engine, run
DDL, or open a database connection; migrations perform schema changes.
"""
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

SCHEMA = "townlight_parks"
UUIDString36 = String(36)
metadata = MetaData(schema=SCHEMA)


def _c(*args, **kwargs):
    kwargs.setdefault("nullable", False)
    return Column(*args, **kwargs)


def _ts(name):
    return _c(name, DateTime(timezone=True))


resources = Table(
    "resources", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("revision", Integer),
    _c("snapshot", JSON), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("revision > 0", name="ck_resources_revision_positive"),
)

closures = Table(
    "closures", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("resource_id", UUIDString36, ForeignKey(f"{SCHEMA}.resources.id", name="fk_closures_resource_id_resources")),
    _ts("starts_at"), _ts("ends_at"), _c("reason", Text), _c("actor", Text), _ts("created_at"),
    CheckConstraint("ends_at > starts_at", name="ck_closures_window_order"),
    Index("ix_closures_resource_window", "resource_id", "starts_at", "ends_at"),
)

households = Table(
    "households", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("tracking_hash", String(64)), _c("encrypted_tracking_token", LargeBinary),
    _c("snapshot", JSON), _ts("created_at"), _ts("updated_at"),
    UniqueConstraint("tracking_hash", name="uq_households_tracking_hash"),
)

participants = Table(
    "participants", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("household_id", UUIDString36, ForeignKey(f"{SCHEMA}.households.id", name="fk_participants_household_id_households")),
    _c("revision", Integer), _c("snapshot", JSON), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("revision > 0", name="ck_participants_revision_positive"),
    Index("ix_participants_household_id", "household_id"),
)

bookings = Table(
    "bookings", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("resource_id", UUIDString36, ForeignKey(f"{SCHEMA}.resources.id", name="fk_bookings_resource_id_resources")),
    _c("household_id", UUIDString36, ForeignKey(f"{SCHEMA}.households.id", name="fk_bookings_household_id_households")),
    _c("revision", Integer), _c("series_id", UUIDString36),
    _ts("starts_at"), _ts("ends_at"), _ts("occupied_from"), _ts("occupied_until"),
    _c("state", String(16)), _c("purpose", Text), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("revision > 0", name="ck_bookings_revision_positive"),
    CheckConstraint("ends_at > starts_at", name="ck_bookings_window_order"),
    CheckConstraint("occupied_until > occupied_from", name="ck_bookings_occupied_window_order"),
    CheckConstraint("occupied_from <= starts_at", name="ck_bookings_occupied_from_bound"),
    CheckConstraint("ends_at <= occupied_until", name="ck_bookings_occupied_until_bound"),
    CheckConstraint("state IN ('pending','confirmed','cancelled','rejected')", name="ck_bookings_state"),
    Index("ix_bookings_resource_occupied_window", "resource_id", "occupied_from", "occupied_until"),
    Index("ix_bookings_household_id", "household_id"),
    Index("ix_bookings_series_id", "series_id"),
    Index("ix_bookings_resource_state", "resource_id", "state"),
)

programs = Table(
    "programs", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("revision", Integer), _c("snapshot", JSON), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("revision > 0", name="ck_programs_revision_positive"),
)

sessions = Table(
    "sessions", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("program_id", UUIDString36, ForeignKey(f"{SCHEMA}.programs.id", name="fk_sessions_program_id_programs")),
    _c("revision", Integer), _ts("starts_at"), _ts("ends_at"),
    _c("capacity", Integer), _c("fee_cents", Integer), _c("currency", String(3)),
    _c("state", String(16)), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("revision > 0", name="ck_sessions_revision_positive"),
    CheckConstraint("ends_at > starts_at", name="ck_sessions_window_order"),
    CheckConstraint("capacity > 0", name="ck_sessions_capacity_positive"),
    CheckConstraint("fee_cents >= 0", name="ck_sessions_fee_nonnegative"),
    CheckConstraint("state IN ('open','closed','cancelled')", name="ck_sessions_state"),
    Index("ix_sessions_program_window", "program_id", "starts_at", "ends_at"),
)

registrations = Table(
    "registrations", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("session_id", UUIDString36, ForeignKey(f"{SCHEMA}.sessions.id", name="fk_registrations_session_id_sessions")),
    _c("participant_id", UUIDString36, ForeignKey(f"{SCHEMA}.participants.id", name="fk_registrations_participant_id_participants")),
    _c("revision", Integer), _c("state", String(16)), _c("queue_order", Integer),
    _c("checked_in_at", DateTime(timezone=True), nullable=True), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("revision > 0", name="ck_registrations_revision_positive"),
    CheckConstraint("state IN ('confirmed','waitlisted','cancelled')", name="ck_registrations_state"),
    CheckConstraint("queue_order >= 0", name="ck_registrations_queue_order_nonnegative"),
    UniqueConstraint("session_id", "participant_id", name="uq_registrations_session_participant"),
    Index("ix_registrations_session_state_queue", "session_id", "state", "queue_order"),
    Index("ix_registrations_participant_id", "participant_id"),
)

obligations = Table(
    "obligations", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("entity_kind", String(16)), _c("entity_id", UUIDString36),
    _c("amount_cents", Integer), _c("deposit_cents", Integer),
    _c("currency", String(3)), _ts("created_at"),
    CheckConstraint("entity_kind IN ('booking','registration')", name="ck_obligations_entity_kind"),
    CheckConstraint("amount_cents >= 0", name="ck_obligations_amount_nonnegative"),
    CheckConstraint("deposit_cents >= 0", name="ck_obligations_deposit_nonnegative"),
    CheckConstraint("deposit_cents <= amount_cents", name="ck_obligations_deposit_le_amount"),
    UniqueConstraint("entity_kind", "entity_id", name="uq_obligations_entity"),
    Index("ix_obligations_entity", "entity_kind", "entity_id"),
)

ledger = Table(
    "ledger", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("obligation_id", UUIDString36, ForeignKey(f"{SCHEMA}.obligations.id", name="fk_ledger_obligation_id_obligations")),
    _c("operation", String(16)), _c("amount_cents", Integer), _c("method", String(16)),
    _c("reference", Text), _c("idempotency_key", String(255)),
    _c("provider_id", String(255), nullable=True), _c("actor", Text), _ts("created_at"),
    CheckConstraint("operation IN ('payment','refund')", name="ck_ledger_operation"),
    CheckConstraint("method IN ('offline','stripe')", name="ck_ledger_method"),
    CheckConstraint("amount_cents > 0", name="ck_ledger_amount_positive"),
    UniqueConstraint("idempotency_key", name="uq_ledger_idempotency_key"),
    UniqueConstraint("provider_id", name="uq_ledger_provider_id"),
    Index("ix_ledger_obligation_id", "obligation_id"),
)

payment_intents = Table(
    "payment_intents", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("obligation_id", UUIDString36, ForeignKey(f"{SCHEMA}.obligations.id", name="fk_payment_intents_obligation_id_obligations")),
    _c("command_key", String(255)), _c("request", JSON),
    _c("amount_cents", Integer), _c("currency", String(3)), _c("state", String(16)),
    _c("provider_session_id", String(255), nullable=True),
    _c("checkout_url", Text, nullable=True),
    _c("provider_payment_id", String(255), nullable=True),
    _c("last_error", Text, nullable=True), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("amount_cents > 0", name="ck_payment_intents_amount_positive"),
    CheckConstraint("state IN ('prepared','open','paid','expired','failed')", name="ck_payment_intents_state"),
    UniqueConstraint("command_key", name="uq_payment_intents_command_key"),
    UniqueConstraint("provider_session_id", name="uq_payment_intents_provider_session_id"),
    Index("ix_payment_intents_obligation_state", "obligation_id", "state"),
)

refund_intents = Table(
    "refund_intents", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("payment_ledger_id", UUIDString36, ForeignKey(f"{SCHEMA}.ledger.id", name="fk_refund_intents_payment_ledger_id_ledger")),
    _c("obligation_id", UUIDString36, ForeignKey(f"{SCHEMA}.obligations.id", name="fk_refund_intents_obligation_id_obligations")),
    _c("command_key", String(255)), _c("amount_cents", Integer), _c("reason", String(255)),
    _c("state", String(16)),
    _c("provider_refund_id", String(255), nullable=True),
    _c("last_error", Text, nullable=True), _ts("created_at"), _ts("updated_at"),
    CheckConstraint("amount_cents > 0", name="ck_refund_intents_amount_positive"),
    CheckConstraint("state IN ('prepared','pending','succeeded','failed')", name="ck_refund_intents_state"),
    UniqueConstraint("command_key", name="uq_refund_intents_command_key"),
    UniqueConstraint("provider_refund_id", name="uq_refund_intents_provider_refund_id"),
    Index("ix_refund_intents_payment_ledger_state", "payment_ledger_id", "state"),
)

events = Table(
    "events", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("entity_kind", String(32)), _c("entity_id", UUIDString36), _c("revision", Integer),
    _c("kind", String(64)), _c("actor", Text), _c("payload", JSON), _ts("created_at"),
    CheckConstraint("revision > 0", name="ck_events_revision_positive"),
    Index("ix_events_entity", "entity_kind", "entity_id", "created_at"),
)

command_receipts = Table(
    "command_receipts", metadata,
    _c("command_id", UUIDString36, primary_key=True),
    _c("operation", String(128)), _c("payload_sha256", String(64)),
    _c("result", JSON), _ts("created_at"),
)

integration_links = Table(
    "integration_links", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("resource_id", UUIDString36, ForeignKey(f"{SCHEMA}.resources.id", name="fk_integration_links_resource_id_resources")),
    _c("request_id", String(255)), _c("command_id", UUIDString36), _ts("created_at"),
    UniqueConstraint("request_id", name="uq_integration_links_request_id"),
    UniqueConstraint("command_id", name="uq_integration_links_command_id"),
    Index("ix_integration_links_resource_id", "resource_id"),
)

attachments = Table(
    "attachments", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("entity_kind", String(32)), _c("entity_id", UUIDString36), _c("name", Text),
    _c("media_type", String(255)), _c("content", LargeBinary), _c("sha256", String(64)),
    _c("size", Integer), _c("actor", Text), _ts("created_at"),
    CheckConstraint("size >= 0", name="ck_attachments_size_nonnegative"),
    Index("ix_attachments_entity", "entity_kind", "entity_id"),
)

outbox = Table(
    "outbox", metadata,
    _c("id", UUIDString36, primary_key=True),
    _c("entity_kind", String(32)), _c("entity_id", UUIDString36), _c("recipient", Text),
    _c("encrypted_body", LargeBinary), _c("message_id", String(255)),
    _c("state", String(16)), _c("claim_token", String(255), nullable=True),
    _c("lease_until", DateTime(timezone=True), nullable=True), _c("attempts", Integer),
    _ts("next_attempt_at"), _c("last_error", Text, nullable=True), _ts("created_at"),
    CheckConstraint("state IN ('pending','claimed','sent','failed','uncertain')", name="ck_outbox_state"),
    CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonnegative"),
    UniqueConstraint("message_id", name="uq_outbox_message_id"),
    Index("ix_outbox_state_next_attempt_at", "state", "next_attempt_at"),
)

schema_versions = Table(
    "schema_versions", metadata,
    _c("version", Integer, primary_key=True),
    _ts("applied_at"),
)
