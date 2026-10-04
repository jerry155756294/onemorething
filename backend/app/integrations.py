"""Provider-neutral contracts for account and external-service integrations.

The persistence implementation lives in ``main.py`` for now because the MVP
uses one SQLite unit of work.  This module keeps provider names, lifecycle
states, and adapter contracts independent from the User model and HTTP layer.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class CalendarProvider(str, Enum):
    NEXTCLOUD = "nextcloud_calendar"
    GOOGLE = "google_calendar"


class CalendarConnectionStatus(str, Enum):
    CONNECTED = "connected"
    SYNCING = "syncing"
    READY = "ready"
    ERROR = "error"
    EXPIRED = "expired"
    DISCONNECTED = "disconnected"


class CalendarSyncMode(str, Enum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class CalendarConnection(BaseModel):
    """The provider-neutral representation exposed to account settings."""

    id: str
    user_id: str
    provider: CalendarProvider
    status: CalendarConnectionStatus
    credential_ref: str | None = None
    last_sync_at: str | None = None
    sync_mode: CalendarSyncMode = CalendarSyncMode.MANUAL
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class CalendarProviderAdapter(Protocol):
    """Provider operations behind the connection lifecycle."""

    provider: CalendarProvider

    def sync(self, connection: CalendarConnection) -> dict[str, Any]: ...


CALENDAR_PROVIDER_LABELS = {
    CalendarProvider.NEXTCLOUD.value: "Nextcloud Calendar",
    CalendarProvider.GOOGLE.value: "Google Calendar",
}


def normalize_calendar_provider(value: str) -> CalendarProvider:
    aliases = {
        "nextcloud": CalendarProvider.NEXTCLOUD,
        "nextcloud-caldav": CalendarProvider.NEXTCLOUD,
        "nextcloud_calendar": CalendarProvider.NEXTCLOUD,
        "google": CalendarProvider.GOOGLE,
        "google-calendar": CalendarProvider.GOOGLE,
        "google_calendar": CalendarProvider.GOOGLE,
    }
    try:
        return aliases[value.strip().lower()]
    except (AttributeError, KeyError) as error:
        raise ValueError(f"Unsupported calendar provider: {value}") from error

