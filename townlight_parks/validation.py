"""Pydantic validation schemas for public Parks snapshots."""
from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parseaddr
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .store import InvalidInput

__all__ = [
    "HouseholdSpec",
    "ParticipantSpec",
    "ProgramSpec",
    "ResourceSpec",
    "snapshot",
]

_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_LOCAL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_DOMAIN = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("invalid IANA timezone") from None
    return value


def _minutes(value: str, *, end: bool) -> int:
    if end and value == "24:00":
        return 1440
    if not _TIME.fullmatch(value):
        raise ValueError("time must use HH:MM")
    return int(value[:2]) * 60 + int(value[3:])


def _email(value: str) -> str:
    if parseaddr(value)[1] != value:
        raise ValueError("invalid email address")
    local, separator, domain = value.rpartition("@")
    if (
        not separator
        or not local
        or not domain
        or len(value) > 254
        or len(local) > 64
        or len(domain) > 253
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _LOCAL.fullmatch(local)
        or not _DOMAIN.fullmatch(domain)
    ):
        raise ValueError("invalid email address")
    return value


class ResourceSpec(_Spec):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=1000)
    timezone: str
    description: str = Field(default="", max_length=10000)
    amenities: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(max_length=100)
    accessibility: str = Field(max_length=5000)
    hours: dict[str, list[list[str]]]
    public: bool = Field(default=False, strict=True)
    approval_required: bool = Field(default=True, strict=True)
    buffer_before_minutes: int = Field(strict=True, ge=0, le=1440)
    buffer_after_minutes: int = Field(strict=True, ge=0, le=1440)
    fee_cents: int = Field(strict=True, ge=0, le=100_000_000)
    deposit_cents: int = Field(strict=True, ge=0, le=100_000_000)
    currency: str = "USD"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _timezone(value)

    @field_validator("hours")
    @classmethod
    def _valid_hours(cls, value: dict[str, list[list[str]]]):
        for day, windows in value.items():
            if day not in _DAYS or len(windows) > 4:
                raise ValueError("invalid day or too many windows")
            previous_end = None
            for window in windows:
                if len(window) != 2:
                    raise ValueError("each window must have a start and end")
                start = _minutes(window[0], end=False)
                stop = _minutes(window[1], end=True)
                if start >= stop or (previous_end is not None and start < previous_end):
                    raise ValueError("windows must be ordered and non-overlapping")
                previous_end = stop
        return value

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be three uppercase letters")
        return value


class ProgramSpec(_Spec):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=10000)
    timezone: str
    public: bool = Field(strict=True)
    min_age: int | None = Field(default=None, strict=True, ge=0, le=120)
    max_age: int | None = Field(default=None, strict=True, ge=0, le=120)
    eligibility_text: str = Field(default="", max_length=5000)
    accessibility: str = Field(default="", max_length=5000)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _timezone(value)

    @model_validator(mode="after")
    def _coherent_ages(self):
        if (
            self.min_age is not None
            and self.max_age is not None
            and self.min_age > self.max_age
        ):
            raise ValueError("minimum age cannot exceed maximum age")
        return self


class HouseholdSpec(_Spec):
    contact_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=1000)
    language: str = Field(default="en", min_length=1, max_length=16)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return _email(value)

    @field_validator("language")
    @classmethod
    def _valid_language(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})?", value):
            raise ValueError("invalid language code")
        return value


class ParticipantSpec(_Spec):
    name: str = Field(min_length=1, max_length=200)
    birth_date: date
    accommodations: str = Field(default="", max_length=5000)

    @field_validator("birth_date", mode="before")
    @classmethod
    def _strict_birth_date(cls, value: object) -> date:
        if isinstance(value, (bool, int, datetime)):
            raise ValueError(  # noqa: TRY004 - Pydantic validators need ValueError
                "birth date must be a date or YYYY-MM-DD string"
            )
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("birth date must use YYYY-MM-DD")
            try:
                return date.fromisoformat(value)
            except ValueError:
                raise ValueError("invalid birth date") from None
        raise ValueError("birth date must be a date or YYYY-MM-DD string")

    @field_validator("birth_date")
    @classmethod
    def _not_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("birth date cannot be in the future")
        return value


def snapshot(model: type[BaseModel], value: object) -> dict:
    """Validate an exact public dict and return its JSON-mode dump."""
    if type(value) is not dict:
        raise InvalidInput("snapshot must be an object")
    try:
        return model.model_validate(value).model_dump(mode="json")
    except ValidationError:
        raise InvalidInput("invalid snapshot") from None
