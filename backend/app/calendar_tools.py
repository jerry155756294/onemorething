"""Read-only query contracts used by the AI interpretation boundary."""

from __future__ import annotations

from typing import Any, Protocol


class CalendarQueryTool(Protocol):
    """Read only the calendar range an interpreter explicitly needs."""

    def search_calendar_events(
        self,
        start_date: str,
        end_date: str,
        keyword: str | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Return a bounded set of calendar events for the requested range."""


class InterpretationQueryTool(CalendarQueryTool, Protocol):
    """The complete read-only tool surface exposed to interpretation models."""

    def search_tasks(
        self,
        keyword: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Return a bounded set of open tasks matching the supplied filters."""
