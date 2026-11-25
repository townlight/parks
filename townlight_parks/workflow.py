"""Composed public repository for Townlight Parks."""

from __future__ import annotations

from .archive import ArchiveMixin
from .attachments import AttachmentMixin
from .booking_changes import BookingChangesMixin
from .booking_create import BookingCreateMixin
from .catalog import CatalogMixin
from .checkout import CheckoutMixin
from .commands import ResidentCommandMixin
from .enrollment import EnrollmentMixin
from .enrollment_changes import EnrollmentChangesMixin
from .finance import FinanceMixin
from .households import HouseholdMixin
from .maintenance import MaintenanceMixin
from .notification_queue import NotificationQueueMixin
from .notification_state import NotificationStateMixin
from .public_catalog import PublicCatalogMixin
from .refunds import RefundMixin
from .reporting import ReportingMixin
from .sessions import SessionMixin
from .store import RepositoryBase

__all__ = ["ParksRepository"]


class ParksRepository(
    SessionMixin, EnrollmentMixin, EnrollmentChangesMixin, FinanceMixin, RefundMixin,
    ReportingMixin, PublicCatalogMixin,
    CheckoutMixin, BookingCreateMixin, BookingChangesMixin, CatalogMixin,
    AttachmentMixin, ArchiveMixin, HouseholdMixin, MaintenanceMixin,
    NotificationQueueMixin, NotificationStateMixin, ResidentCommandMixin,
    RepositoryBase,
):
    """Single repository exposing catalog and household contracts."""
