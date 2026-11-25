# Townlight Parks implementation design

Approved scope: APPROVED-PLAN.md in the integration work folder. Implement after 311 and Communications source workflows. Windows only, existing staff roles, no software tagging/publication without Scott. New distribution townlight-parks 0.2.0, import townlight_parks, Core 1.3.0.dev2. PyPI JSON returned 404 on November 24, 2026; this is availability, not a reservation.

## Screens and complete operator flows

Staff Parks workspace contains facilities/closures, reservation calendar and requests, programs/sessions, registrations/waitlist, private rosters/check-in, fees/deposits/refunds/reconciliation, maintenance links, Records archive and approved aggregate Data export. Label all fields; expose saved identity, revision, real state and actionable errors. Existing local-admin/city-staff/clerk permissions control operations; separate Records permission applies to archives. Shared-host fixed native commands use the same proof/session and allocation receipts as 311/Communications.

Resident portal contains published facility/program catalogue, availability query, booking/registration forms, a private tracking capability, confirmation and cancellation/change requests, and hosted payment links. Never expose household/participant/minor/accommodation/contact details publicly. CSRF and session ownership protect commands; source city service remains private behind the encrypted resident inbox. Durable command identity prevents replay duplication. Queued is not confirmed until the host applies the command.

## Durable contracts

Use explicit SQLAlchemy migrations and production bootstrap=False in schema townlight_parks. Reuse the append-only document revision/event approach, with dedicated relational occupancy, registration, payment and integration tables where concurrency requires them. Integer cents and explicit currency; no floating point money. Actual attachment bytes with digest and bounded limits, never only metadata.

Facility records: name, address/location, amenities, accessibility, operating hours/timezone, reservable resources, approval policy, fees/deposits, buffers, public eligibility and closures. Versioned staff changes require expected revision. Dates carry timezone; store occupancy as UTC. Validate operating hours, closure/blackout intervals and setup/cleanup buffers on every booking and reschedule. Lock the owning resource row FOR UPDATE inside the same transaction as the overlap query and occupancy write; every recurrence locks in stable order and the entire series succeeds or fails. Pending approval consumes the resource; cancelled/rejected releases it. A changed closure conflicts with active bookings and requires explicit staff resolution, never silent cancellation.

Programs/sessions: public descriptions, dates/timezone, eligibility/age at session start, capacity, fees, accommodations guidance. Private household contact and participants have guarded actor ownership. Lock the session row for registration/cancellation/waitlist promotion. Store queued order and promote the first eligible waiting participant without exceeding capacity. Staff attendance reads actual roster and writes idempotent check-ins. Enrollment, cancellation, refund and notification state remain separately visible. Capacity cannot fall below current enrollment.

Payment obligations link the real reservation/registration, charge, refundable deposit and currency. Append-only ledger stores offline payments/refunds with staff actor/reference and provider captures/refunds with unique provider identities; sums cannot exceed amount due/paid/refundable amount. Receipts expose actual amount/status, not a paid flag invented by UI. Hosted processor adapter must use provider idempotency keys and hosted checkout, never card handling. Provider return URLs do not prove payment; host verifies provider transaction and amount/currency/obligation identity before applying it. Ambiguous network outcomes are reconciled before retry. Processor selection is an optional pending product choice; independent catalogue/reservation/program work proceeds meanwhile.

## Integration and recovery

Parks maintenance creates a real 311 request using durable idempotency, actual location/photo bytes and returned request identity. Both modules retrieve the same live assignment/resolution; no handoff-ready substitute. Public closures/program descriptions become eligible immutable Communications source snapshots after explicit staff public approval. Transactional booking/registration messages use shared SMTP settings and a durable outbox with bounded safe retries and uncertain state. Confirmation email never substitutes for the actual canonical reservation.

Records archives contain actual current record/history/documents/financial receipts under appropriate permissions; verify stored bytes and returned identifiers. Data exports aggregate utilization, enrollment, fees and maintenance counts, excluding participant/contact/accommodation data; explicit staff approval precedes public queueing. Budget/Grants connections are links only, no new ledger/accounting scope.

Back up database, managed files, portal inbox, profile encryption keys and publisher queue together. Restart reopens durable requests and resumes known-safe jobs without duplicates. No camera, media engine, VM or new installer is needed during source development.

## Installed completion

In the one signed Windows candidate: resident facility booking and receipt; competing two-PC booking rejection; recurring/buffer/closure/reschedule handling; program capacity, waitlist and cancellation promotion; private roster and attendance; offline and configured hosted payment/refund reconciliation; Parks-to-311 photo/assignment/resolution round trip; approved Communications announcement; actual Records archive/readback; privacy-safe approved Data output; restart and backup/restore. Report controlled provider doubles separately from any live provider acceptance. Use existing acceptance evidence for unaffected modules. Update maintained source docs, user/admin/architecture/API sections and all existing download/landing surfaces.
