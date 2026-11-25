"""Read-only aggregate reporting.

The report intentionally exposes only aggregate counters and sums.  It never
selects household, participant, contact, location, or free-text fields.
"""
from __future__ import annotations

from sqlalchemy import case, func, select

from .models import bookings, integration_links, ledger, obligations, registrations

_MAX_ROWS = 100_000


class ReportingMixin:
    """Mixin for services that expose aggregate operational reporting."""

    @staticmethod
    def _duration_expr(dialect: str):
        if dialect == "sqlite":
            return (
                func.julianday(bookings.c.ends_at)
                - func.julianday(bookings.c.starts_at)
            ) * 1440.0
        if dialect == "postgresql":
            return func.extract(
                "epoch", bookings.c.ends_at - bookings.c.starts_at
            ) / 60.0
        raise ValueError(f"unsupported reporting dialect: {dialect}")

    @staticmethod
    def _bounded(conn, stmt):
        rows = conn.execute(stmt.limit(_MAX_ROWS + 1)).all()
        if len(rows) > _MAX_ROWS:
            raise OverflowError("report aggregate read exceeded row cap")
        return rows

    def aggregate_report(self):
        engine = self.engine
        dialect = engine.dialect.name
        duration = self._duration_expr(dialect)
        out = []
        with engine.connect() as conn:
            total_bookings = conn.execute(
                select(func.count()).select_from(bookings)
            ).scalar_one()
            if total_bookings > _MAX_ROWS:
                raise OverflowError("booking report read exceeded row cap")
            booking_stmt = (
                select(
                    bookings.c.state,
                    func.count(),
                    func.coalesce(func.sum(duration), 0),
                )
                .group_by(bookings.c.state)
            )
            for state, count, minutes in self._bounded(conn, booking_stmt):
                out.append({
                    "metric": "bookings", "state": state,
                    "count": count, "amount_cents": 0,
                    "minutes": round(minutes or 0),
                })

            attendance = case(
                (
                    (registrations.c.state == "confirmed")
                    & registrations.c.checked_in_at.is_not(None),
                    1,
                ),
                else_=0,
            )
            registration_stmt = (
                select(
                    registrations.c.state,
                    func.count(),
                    func.coalesce(func.sum(attendance), 0),
                )
                .group_by(registrations.c.state)
            )
            for state, count, checked_in in self._bounded(conn, registration_stmt):
                out.append({
                    "metric": "registrations", "state": state,
                    "count": int(count), "amount_cents": 0, "minutes": 0,
                })
                out.append({
                    "metric": "attendance_confirmed", "state": state,
                    "count": int(checked_in or 0), "amount_cents": 0,
                    "minutes": 0,
                })

            ledger_stmt = (
                select(
                    ledger.c.operation,
                    ledger.c.method,
                    func.count(),
                    func.coalesce(func.sum(ledger.c.amount_cents), 0),
                )
                .group_by(ledger.c.operation, ledger.c.method)
            )
            for operation, method, count, amount in self._bounded(conn, ledger_stmt):
                out.append({
                    "metric": "payments" if operation == "payment" else "refunds",
                    "state": method, "count": int(count),
                    "amount_cents": int(amount or 0), "minutes": 0,
                })

            net = (
                select(func.coalesce(func.sum(case(
                    (ledger.c.operation == "payment", ledger.c.amount_cents),
                    else_=-ledger.c.amount_cents,
                )), 0))
                .where(ledger.c.obligation_id == obligations.c.id)
                .correlate(obligations)
                .scalar_subquery()
            )
            balance = obligations.c.amount_cents - net
            outstanding = case((balance > 0, balance), else_=0)
            outstanding_count = case((balance > 0, 1), else_=0)
            obligation_stmt = select(
                func.coalesce(func.sum(outstanding_count), 0),
                func.coalesce(func.sum(outstanding), 0),
            ).select_from(obligations)
            count, amount = conn.execute(obligation_stmt).one()
            out.append({
                "metric": "outstanding", "state": "all",
                "count": int(count or 0), "amount_cents": max(0, int(amount or 0)),
                "minutes": 0,
            })

            linked = conn.execute(
                select(func.count()).select_from(integration_links)
            ).scalar_one()
            out.append({
                "metric": "maintenance_linked", "state": "linked",
                "count": linked, "amount_cents": 0, "minutes": 0,
            })

        out.sort(key=lambda row: (row["metric"], row["state"]))
        return out
