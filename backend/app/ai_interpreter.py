"""Provider boundary for Source -> Proposal interpretation.

The domain only receives validated Proposal objects.  Provider responses are
treated as untrusted text and never reach the persistence layer directly.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .calendar_tools import InterpretationQueryTool


LOGGER = logging.getLogger("omt.ai")

ProposalOperation = Literal["create", "update", "link", "ignore"]
ProposalTargetType = Literal["event", "task", "list"]
ProposalConfidence = Literal["high", "medium", "low"]


class AIInterpretationError(Exception):
    """Base error that can be presented as a stable API error."""

    def __init__(self, message: str, *, model_alias: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.model_alias = model_alias
        self.http_status = http_status


class AIProviderConfigurationError(AIInterpretationError):
    pass


class AIProviderUnavailable(AIInterpretationError):
    pass


class AIProviderTimeout(AIInterpretationError):
    pass


class AIProviderMalformedOutput(AIInterpretationError):
    pass


class WeeklyRecurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequency: Literal["weekly"]
    weekdays: list[int] = Field(min_length=1, max_length=7)
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_term(self) -> "WeeklyRecurrence":
        try:
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
        except ValueError as error:
            raise ValueError("recurrence dates must be ISO calendar dates") from error
        if end < start:
            raise ValueError("recurrence end_date must not precede start_date")
        if (end - start).days > 730:
            raise ValueError("recurrence term cannot exceed two years")
        if len(set(self.weekdays)) != len(self.weekdays) or any(day < 1 or day > 7 for day in self.weekdays):
            raise ValueError("recurrence weekdays must be unique ISO weekdays")
        return self


class ProposalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    location: str | None = Field(default=None, max_length=2000)
    date: str | None = Field(default=None, max_length=80)
    time: str | None = Field(default=None, max_length=80)
    start_at: str | None = Field(default=None, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    all_day: bool | None = None
    recurrence: WeeklyRecurrence | None = None
    detail: str | None = Field(default=None, max_length=2000)
    due: str | None = Field(default=None, max_length=80)
    due_label: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)
    list_id: str | None = Field(default=None, max_length=64)
    related_event_id: str | None = Field(default=None, max_length=64)


class TargetCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    start: str | None = Field(default=None, max_length=80)
    end: str | None = Field(default=None, max_length=80)
    location: str | None = Field(default=None, max_length=2000)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: ProposalOperation
    target_type: ProposalTargetType
    target_id: str | None = Field(default=None, max_length=128)
    patch: ProposalPatch = Field(default_factory=ProposalPatch)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    target_candidates: list[TargetCandidate] = Field(default_factory=list, max_length=10)
    needs_review: bool = False
    review_reason: str | None = Field(default=None, max_length=500)
    confidence: ProposalConfidence | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "Proposal":
        patch_fields = self.patch.model_fields_set
        allowed = {
            "event": {"title", "location", "date", "time", "start_at", "end_at", "all_day", "recurrence", "detail"},
            "task": {"title", "due", "due_label", "notes", "list_id", "date", "time", "start_at", "end_at", "all_day"},
            "list": {"title"},
        }[self.target_type]
        unexpected = patch_fields - allowed
        if unexpected:
            raise ValueError(f"patch fields are not valid for {self.target_type}: {sorted(unexpected)}")
        if self.operation == "create" and self.target_id is not None:
            raise ValueError("create proposals cannot have target_id")
        if self.operation == "update" and not self.target_id and not self.needs_review:
            raise ValueError("update proposals without target_id require review")
        if self.operation == "update" and not patch_fields:
            raise ValueError("update proposals require a non-empty patch")
        if self.needs_review and not self.review_reason:
            raise ValueError("review proposals require review_reason")
        if self.operation == "create" and self.target_type == "task" and "title" not in patch_fields:
            raise ValueError("task creation requires a title")
        return self


class InterpretationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[Proposal] = Field(default_factory=list, max_length=200)


class AIProvider(Protocol):
    name: str

    def interpret(self, source: str, context: dict[str, Any]) -> list[Proposal]:
        """Interpret a source using only the supplied structured context."""


EVENT_PATCH_FIELDS = {"title", "location", "date", "time", "start_at", "end_at", "all_day", "recurrence", "detail"}
TASK_PATCH_FIELDS = {"title", "due", "due_label", "notes", "list_id", "date", "time", "start_at", "end_at", "all_day"}


def _extract_json(content: Any) -> Any:
    """Recover common provider formatting slips before strict backend validation.

    The recovery is deliberately syntax-only: it can unwrap code fences, prose,
    a JSON-encoded JSON string, Python-style dict/list literals, and trailing
    commas. It never invents Proposal fields or values; Pydantic remains the
    persistence boundary.
    """
    if isinstance(content, (dict, list)):
        return content
    if not isinstance(content, str):
        raise AIProviderMalformedOutput("provider response did not contain JSON text")

    text = content.strip().lstrip("\ufeff")
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    # Some reasoning-capable gateways include a think block before the final JSON.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    text = re.sub(r"^```(?:json|javascript|js|python)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()

    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    candidates = [text]
    if starts:
        candidates.append(text[min(starts):])

    last_error: Exception | None = None
    for candidate in candidates:
        variants = [candidate]
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        if repaired != candidate:
            variants.append(repaired)
        for variant in variants:
            parsed: Any = None
            try:
                parsed = json.loads(variant)
            except json.JSONDecodeError as error:
                last_error = error
                try:
                    parsed = json.JSONDecoder().raw_decode(variant)[0]
                except json.JSONDecodeError as raw_error:
                    last_error = raw_error
                    # A few models emit a valid Python literal (`'` quotes,
                    # True/False/None). literal_eval is non-executing and the
                    # result is still passed through the strict Proposal schema.
                    try:
                        parsed = ast.literal_eval(variant)
                    except (SyntaxError, ValueError) as literal_error:
                        last_error = literal_error
                        continue
            # Occasionally the API encodes the entire JSON object as a JSON
            # string. Unwrap at most once to avoid recursive/resource abuse.
            if isinstance(parsed, str) and parsed.strip() != variant.strip():
                try:
                    return _extract_json(parsed)
                except AIProviderMalformedOutput as nested_error:
                    last_error = nested_error
                    continue
            if isinstance(parsed, (dict, list)):
                return parsed
    raise AIProviderMalformedOutput("provider response was not valid JSON") from last_error


CALENDAR_DAY_EVENT_TERMS = (
    "校慶", "國慶", "節日", "紀念日", "放假", "補假", "休假", "假日", "生日",
)


def _source_explicitly_all_day(source: str | None) -> bool:
    """Return whether the Source explicitly describes an all-day event."""
    if not source:
        return False
    normalized = " ".join(source.split())
    return bool(
        any(term in normalized for term in CALENDAR_DAY_EVENT_TERMS)
        or re.search(r"(?:all[-\s]?day)", normalized, re.IGNORECASE)
    )


def _normalize_explicit_all_day(proposal: Proposal, source: str | None) -> Proposal:
    """Preserve an explicit all-day statement even if the provider omits the flag.

    We only canonicalize when the patch already contains a date and contains no
    timed fields. This never turns an ambiguous date-only capture into all-day;
    the Source itself must say it is all-day.
    """
    if proposal.target_type != "event" or not _source_explicitly_all_day(source):
        return proposal
    patch_data = proposal.patch.model_dump(exclude_unset=True)
    if not patch_data.get("date"):
        return proposal
    if any(patch_data.get(field) for field in ("time", "start_at", "end_at")):
        return proposal
    if patch_data.get("all_day") is True:
        return proposal
    patch_data["all_day"] = True
    return proposal.model_copy(update={"patch": ProposalPatch.model_validate(patch_data)})


def _source_explicitly_contains_end_time(source: str | None, proposal: Proposal) -> bool:
    """Return whether the captured text supports a second Event time.

    Provider output is untrusted. A start-only statement must stay start-only
    even when a model supplies a plausible duration or end timestamp.
    """
    if not source or not proposal.patch.end_at:
        return False
    end_match = re.search(r"(?:T|\s)(\d{1,2}:\d{2})", str(proposal.patch.end_at))
    if end_match and end_match.group(1) in re.findall(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", source):
        times = re.findall(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", source)
        start_match = re.search(r"(?:T|\s)(\d{1,2}:\d{2})", str(proposal.patch.start_at or ""))
        return len(times) >= 2 and (not start_match or times.index(end_match.group(1)) > times.index(start_match.group(1)))
    return str(proposal.patch.end_at)[:10] in source and bool(re.search(r"(?:-|至|到|until|to)", source, re.IGNORECASE))


def _task_deadline_from_source(source: str | None, context: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """Resolve a single source-backed date for a Task create."""
    normalized = " ".join((source or "").split())
    explicit_marker = re.search(
        r"(?:截止|期限|deadline|due|之前|以前|前|繳交|提交|交(?:付|作業|報告)?|完成|記得|要|需|買|處理|本週|下週|週[一二三四五六日天])",
        normalized,
        re.IGNORECASE,
    )
    resolved, label = resolve_source_date(normalized, context)
    if resolved:
        clocks = _source_clocks(normalized)
        due = _iso_at(resolved, clocks[0], context) if clocks else resolved
        return due, label or resolved, bool(explicit_marker)
    return None, None, bool(explicit_marker)


def _source_supports_due(source: str | None, due: str | None, context: dict[str, Any]) -> bool:
    if not source or not due:
        return False
    due_date = str(due)[:10]
    try:
        parsed = date.fromisoformat(due_date)
    except ValueError:
        return False
    text = " ".join(source.split())
    direct_patterns = (
        rf"(?<!\d){parsed.year}-{parsed.month:02d}-{parsed.day:02d}(?!\d)",
        rf"(?<!\d){parsed.month}[/-]0?{parsed.day}(?!\d)",
        rf"(?<!\d)0?{parsed.month}月\s*0?{parsed.day}日?",
    )
    if any(re.search(pattern, text) for pattern in direct_patterns):
        return True
    resolved, _ = resolve_source_date(text, context)
    return resolved == due_date


def _normalize_task_create(proposal: Proposal, source: str | None, context: dict[str, Any]) -> Proposal:
    if proposal.operation != "create" or proposal.target_type != "task":
        return proposal
    patch_data = proposal.patch.model_dump(exclude_unset=True)

    existing_due = patch_data.get("due")
    if existing_due:
        # Keep the provider's selected deadline in multi-date sources; only
        # clear a contradictory review flag when that exact date is visibly
        # supported by the source (e.g. 10/5要交報告).
        if _source_supports_due(source, str(existing_due), context):
            return proposal.model_copy(
                update={
                    "needs_review": False,
                    "review_reason": None,
                    "patch": ProposalPatch.model_validate(patch_data),
                }
            )
        return proposal.model_copy(update={"patch": ProposalPatch.model_validate(patch_data)})

    due, due_label, deadline_is_explicit = _task_deadline_from_source(source, context)
    if due:
        patch_data["due"] = due
        if due_label:
            patch_data["due_label"] = due_label
        return proposal.model_copy(
            update={
                "needs_review": False,
                "review_reason": None,
                "patch": ProposalPatch.model_validate(patch_data),
            }
        )

    if "due" not in patch_data:
        patch_data["due"] = None
    if not deadline_is_explicit:
        patch_data.pop("due_label", None)
        return proposal.model_copy(
            update={
                "needs_review": False,
                "review_reason": None,
                "patch": ProposalPatch.model_validate(patch_data),
            }
        )
    return proposal.model_copy(update={"patch": ProposalPatch.model_validate(patch_data)})

def validate_provider_output(raw: Any, context: dict[str, Any], source: str | None = None) -> list[Proposal]:
    """Validate and constrain provider output before any Proposal is persisted."""
    try:
        if isinstance(raw, list):
            payload = raw
        elif isinstance(raw, dict):
            payload = raw.get("proposals")
            if payload is None and isinstance(raw.get("proposal"), dict):
                payload = [raw["proposal"]]
            if isinstance(payload, dict):
                payload = list(payload.values())
        else:
            payload = None
        if not isinstance(payload, list):
            raise ValueError("provider output must contain a proposals array")
        # Task↔Event links are a deliberate user action.  Silently discard a
        # model's attempted relation instead of persisting an inferred link or
        # rejecting the otherwise-useful proposal.
        payload = [dict(item) if isinstance(item, dict) else item for item in payload]
        for item in payload:
            if not isinstance(item, dict) or item.get("target_type") != "task":
                continue
            patch = item.get("patch")
            if isinstance(patch, dict):
                item["patch"] = {key: value for key, value in patch.items() if key != "related_event_id"}
        result = [Proposal.model_validate(item) for item in payload]
    except (AttributeError, TypeError, ValidationError, ValueError) as error:
        raise AIProviderMalformedOutput("provider output did not match the Proposal schema") from error

    targets = context.get("target_ids", {})
    has_target_scope = "target_ids" in context
    canonicalized: list[Proposal] = []
    seen_task_creates: set[str] = set()
    for proposal in result:
        valid_ids = set(targets.get(proposal.target_type, []))
        if has_target_scope:
            if proposal.target_id and proposal.target_id not in valid_ids:
                raise AIProviderMalformedOutput("provider selected a target outside the supplied context")
            for candidate in proposal.target_candidates:
                if candidate.id not in valid_ids:
                    raise AIProviderMalformedOutput("provider returned a candidate outside the supplied context")
        if proposal.target_type == "event" and set(proposal.patch.model_fields_set) - EVENT_PATCH_FIELDS:
            raise AIProviderMalformedOutput("provider returned an invalid event patch")
        if proposal.target_type == "task" and set(proposal.patch.model_fields_set) - TASK_PATCH_FIELDS:
            raise AIProviderMalformedOutput("provider returned an invalid task patch")
        proposal = _normalize_explicit_all_day(proposal, source)
        if proposal.target_type == "event" and proposal.patch.end_at and not context.get("_visual_source") and not _source_explicitly_contains_end_time(source, proposal):
            patch_data = proposal.patch.model_dump(exclude_unset=True, exclude={"end_at"})
            if proposal.operation == "update" and not patch_data:
                continue
            proposal = proposal.model_copy(update={"patch": ProposalPatch.model_validate(patch_data)})
        proposal = _normalize_task_create(proposal, source, context)
        if proposal.operation == "create" and proposal.target_type == "task":
            title_key = " ".join((proposal.patch.title or "").casefold().split())
            if title_key in seen_task_creates:
                continue
            seen_task_creates.add(title_key)
        if proposal.operation == "create" and proposal.target_type == "list":
            list_title = (proposal.patch.title or "").strip().casefold()
            if not list_title or list_title not in (source or "").casefold():
                continue
        if is_reviewable_proposal(proposal):
            canonicalized.append(proposal)
    return canonicalized


def is_reviewable_proposal(proposal: Proposal) -> bool:
    """Discard event creates that contain no title or date/time to review."""
    if proposal.operation != "create" or proposal.target_type != "event":
        return True
    patch = proposal.patch
    return any(
        isinstance(value, str) and value.strip()
        for value in (patch.title, patch.date, patch.time, patch.start_at, patch.end_at)
    )


def _source_explicitly_repeats_weekly(source: str) -> bool:
    normalized = " ".join((source or "").split()).casefold()
    patterns = (
        re.compile(r"每\s*(?:週|周|星期|禮拜)|逢\s*(?:週|周|星期|禮拜)|每\s*(?:個|一個)?\s*(?:工作日|上班日)"),
        re.compile(r"\b(?:weekly|every\s+(?:week|weekday|working\s+day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|each\s+(?:week|weekday|working\s+day|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.IGNORECASE),
    )
    negative_before = re.compile(r"(?:不|非|沒|無|未|勿|不要|不用|不必|並非|不是|not|never|no)\s*$", re.IGNORECASE)
    negative_after = re.compile(r"^\s*(?:不|非|沒|無|未|勿|取消|停辦|不再|not|never)", re.IGNORECASE)
    for pattern in patterns:
        for match in pattern.finditer(normalized):
            before = normalized[max(0, match.start() - 12):match.start()]
            after = normalized[match.end():match.end() + 12]
            if negative_before.search(before) or negative_after.search(after):
                continue
            return True
    has_timetable = re.search(r"\b(?:timetable|class\s+schedule|weekly\s+schedule)\b|課表|課程時間表", normalized)
    has_term = re.search(r"\b(?:term|semester|school\s+year)\b|學期|學年", normalized)
    has_weekday = re.search(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|(?:週|周|星期|禮拜)[一二三四五六日天]",
        normalized,
    )
    return bool(has_timetable and has_term and has_weekday)


def _without_inferred_recurrence(proposal: Proposal, source: str) -> Proposal | None:
    if proposal.target_type != "event" or proposal.patch.recurrence is None:
        return proposal
    if _source_explicitly_repeats_weekly(source):
        return proposal

    patch_data = proposal.patch.model_dump(exclude_unset=True, exclude={"recurrence"})
    if proposal.operation != "create" and not patch_data:
        return None
    return proposal.model_copy(update={"patch": ProposalPatch.model_validate(patch_data)})


def filter_reviewable_proposals(proposals: list[Proposal], source: str | None = None) -> list[Proposal]:
    """Keep substantive proposals and only retain recurrence stated by the Source."""
    result = []
    for proposal in proposals:
        if source is not None:
            proposal = _without_inferred_recurrence(proposal, source)
            if proposal is None:
                continue
        if is_reviewable_proposal(proposal):
            result.append(proposal)
    return result


MINIMAL_INTERPRET_OUTPUT_TOKENS = 4096
MAX_INTERPRET_TOOL_CALLS = 3
MAX_INTERPRET_TOOL_RESULTS = 5
MAX_CAPTURE_DATES = 120
MAX_CAPTURE_PROPOSALS = 200
DATE_RETRY_BATCH_SIZE = 8
MAX_LENGTH_RECOVERY_CALLS = 32
DEFAULT_INTERPRET_MODEL = "gpt-3.5-turbo"

MINIMAL_UPDATE_FIELDS = ("title", "start_at", "end_at", "detail", "location", "due", "notes")

JSON_OBJECT_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}
# Keep the existing exported names so callers/tests do not need a wider API change.
# Shape enforcement now lives at the backend boundary (expand_minimal_output /
# date-discovery validation) instead of being duplicated in provider JSON Schema.
MINIMAL_ADD_RESPONSE_FORMAT = JSON_OBJECT_RESPONSE_FORMAT.copy()
MINIMAL_UPDATE_RESPONSE_FORMAT = JSON_OBJECT_RESPONSE_FORMAT.copy()
DATE_DISCOVERY_RESPONSE_FORMAT = JSON_OBJECT_RESPONSE_FORMAT.copy()
MINIMAL_OUTPUT_RESPONSE_FORMAT = MINIMAL_ADD_RESPONSE_FORMAT


READ_ONLY_INTERPRET_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_calendar_events",
            "description": "修改既有行程前先搜尋。結果每列為 [id,title,start_at,end_at,location]，最多 5 筆。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD，搜尋結束日（不含）"},
                    "keyword": {"type": "string", "maxLength": 160},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["start_date", "end_date", "keyword", "max_results"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tasks",
            "description": "修改既有待辦前先搜尋。結果每列為 [id,title,due,start_at,end_at]，最多 5 筆。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "maxLength": 160},
                    "due_from": {"type": "string", "description": "YYYY-MM-DD；不限定時傳空字串"},
                    "due_to": {"type": "string", "description": "YYYY-MM-DD；不限定時傳空字串"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["keyword", "due_from", "due_to", "max_results"],
                "additionalProperties": False,
            },
        },
    },
]


def _context_now(context: dict[str, Any]) -> datetime | None:
    try:
        current = datetime.fromisoformat(str(context.get("current_datetime") or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if current.tzinfo is None:
        timezone_name = str(context.get("timezone") or "").strip()
        if timezone_name:
            try:
                current = current.replace(tzinfo=ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError:
                pass
    return current


def _month_day_year(month: int, day: int, current: date) -> date | None:
    try:
        candidate = date(current.year, month, day)
    except ValueError:
        return None
    # Bare month/day near New Year should stay useful without turning a date a
    # few days ago into next year's event. Only large backward jumps roll over.
    if candidate < current and (current - candidate).days > 183:
        try:
            candidate = date(current.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def resolve_source_date(source: str | None, context: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve a small, deterministic set of user-facing date expressions."""
    normalized = " ".join((source or "").split())
    current = _context_now(context)
    if not normalized or current is None:
        return None, None
    today = current.date()

    iso_match = re.search(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", normalized)
    if iso_match:
        try:
            result = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
            return result.isoformat(), iso_match.group(0)
        except ValueError:
            return None, None

    zh_match = re.search(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?", normalized)
    if zh_match:
        year = int(zh_match.group(1)) if zh_match.group(1) else None
        try:
            result = date(year, int(zh_match.group(2)), int(zh_match.group(3))) if year else _month_day_year(int(zh_match.group(2)), int(zh_match.group(3)), today)
        except ValueError:
            result = None
        return (result.isoformat(), zh_match.group(0)) if result else (None, None)

    short_match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", normalized)
    if short_match:
        result = _month_day_year(int(short_match.group(1)), int(short_match.group(2)), today)
        return (result.isoformat(), short_match.group(0)) if result else (None, None)

    for marker, days in (("今天", 0), ("今日", 0), ("明天", 1), ("明日", 1), ("後天", 2)):
        if marker in normalized:
            return (today + timedelta(days=days)).isoformat(), marker

    weekday_match = re.search(r"(?:(下下週|下下周|下週|下周|本週|本周|這週|这周)\s*)?(?:週|周|星期|禮拜)([一二三四五六日天])", normalized)
    if weekday_match:
        weekday_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
        monday = today - timedelta(days=today.isoweekday() - 1)
        prefix = weekday_match.group(1) or ""
        weeks = 2 if prefix in {"下下週", "下下周"} else 1 if prefix in {"下週", "下周"} else 0
        candidate = monday + timedelta(days=weekday_map[weekday_match.group(2)] - 1 + weeks * 7)
        if not prefix and candidate < today:
            candidate += timedelta(days=7)
        return candidate.isoformat(), weekday_match.group(0)

    if re.search(r"(?:這|本)週末|(?:這|本)周末", normalized):
        saturday = today + timedelta(days=(6 - today.isoweekday()) % 7)
        return saturday.isoformat(), "這週末"
    if re.search(r"下週末|下周末", normalized):
        saturday = today + timedelta(days=(6 - today.isoweekday()) % 7 + 7)
        return saturday.isoformat(), "下週末"
    if "月底" in normalized:
        next_month = date(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1)
        return (next_month - timedelta(days=1)).isoformat(), "月底"
    return None, None


def _source_clocks(source: str | None) -> list[str]:
    normalized = " ".join((source or "").split())
    clocks = [f"{int(hour):02d}:{minute}" for hour, minute in re.findall(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", normalized)]
    for meridiem, hour_text, minute_text, half in re.findall(r"(凌晨|早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[點点時时]\s*(?:(\d{1,2})\s*分|([半]))?", normalized):
        hour = int(hour_text)
        minute = 30 if half else int(minute_text or 0)
        if meridiem in {"下午", "晚上"} and hour < 12:
            hour += 12
        elif meridiem == "中午" and hour < 11:
            hour += 12
        elif meridiem in {"凌晨", "早上", "上午"} and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            clock = f"{hour:02d}:{minute:02d}"
            if clock not in clocks:
                clocks.append(clock)
    return clocks


def _iso_at(date_iso: str, clock: str, context: dict[str, Any]) -> str:
    current = _context_now(context)
    parsed = datetime.fromisoformat(f"{date_iso}T{clock}:00")
    if current is not None and current.tzinfo is not None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    return parsed.isoformat()


def _normalize_minimal_temporal(value: str, source: str, context: dict[str, Any], *, index: int = 0) -> str:
    """Validate one compact temporal value without borrowing another row's time.

    The model may use the source clock in its own row, but the backend never
    injects the first clock it sees into a date-only value.  This matters for
    mixed captures such as ``10/5 14:00開班會，10/5前完成簡報``: the meeting
    is at 14:00 while the task is only due that day.
    """
    del index  # kept for the call-site contract; end-time support is checked separately.
    value = (value or "").strip()
    if not value:
        return ""
    resolved_date, _ = resolve_source_date(source, context)
    try:
        if len(value) == 10:
            parsed_date = date.fromisoformat(value)
            return resolved_date or parsed_date.isoformat()
        parsed_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""

    if resolved_date:
        clock = parsed_dt.strftime("%H:%M")
        return _iso_at(resolved_date, clock, context)
    return parsed_dt.isoformat()

def _source_supports_end(source: str) -> bool:
    clocks = _source_clocks(source)
    if len(clocks) >= 2:
        return True
    return bool(re.search(r"(?:到|至|～|~|—|–|-|until|\bto\b)", source, re.IGNORECASE) and len(clocks) >= 2)


def _strong_event_text(text: str) -> bool:
    normalized = " ".join((text or "").split())
    return bool(
        any(term in normalized for term in CALENDAR_DAY_EVENT_TERMS)
        or re.search(r"(?:meeting|class|course|exam|test|activity|ceremony|holiday|event)", normalized, re.IGNORECASE)
    )


TASK_DEADLINE_TERMS = (
    "交作業", "繳交", "截止", "期限", "到期", "待辦", "任務",
    "提交", "上傳", "完成", "準備", "閱讀", "複習", "報告", "作業",
)


def _strong_task_text(text: str) -> bool:
    normalized = " ".join((text or "").split())
    return bool(
        any(term in normalized for term in TASK_DEADLINE_TERMS)
        or re.search(r"(?:deadline|\bdue\b)", normalized, re.IGNORECASE)
    )


def _classify_add(source: str, title: str, start_at: str, end_at: str) -> tuple[Literal["event", "task"], bool]:
    # Strong event nouns win over a generic "要" (e.g. "要開會").
    if _strong_event_text(title):
        return "event", False
    if _strong_task_text(title):
        return "task", False
    # An explicit per-item time range is stronger evidence than unrelated task
    # language elsewhere in a multi-item capture (for example a timetable plus homework).
    if end_at:
        return "event", False
    if _strong_event_text(source) and title and title in source:
        return "event", False
    if _strong_task_text(source) or re.search(r"(?:要|需|需要|得)\s*[^，。；;]{1,30}", source):
        return "task", False
    if "T" in start_at:
        return "event", True
    return "task", True


def _source_has_deadline_semantics(source: str, title: str = "") -> bool:
    text = f"{source} {title}"
    normalized = " ".join(text.split())
    return bool(any(term in normalized for term in TASK_DEADLINE_TERMS) or re.search(r"(?:deadline|\bdue\b)", normalized, re.IGNORECASE))


def _source_explicit_all_day_any(source: str) -> bool:
    return bool(re.search(r"(?:全天|整天|全日|all[-\s]?day)", source, re.IGNORECASE))


def _minimal_add_proposal(row: list[str], source: str, context: dict[str, Any]) -> Proposal | None:
    if len(row) != 4 or not all(isinstance(value, str) for value in row):
        raise AIProviderMalformedOutput("minimal add rows must contain four strings")
    title, raw_start, raw_end, detail = (value.strip() for value in row)
    if not title:
        return None
    start_at = _normalize_minimal_temporal(raw_start, source, context, index=0)
    end_supported = _source_supports_end(source) or bool(context.get("_visual_source"))
    end_at = _normalize_minimal_temporal(raw_end, source, context, index=1) if raw_end and end_supported else ""
    target_type, uncertain_type = _classify_add(source, title, start_at, end_at)
    patch: dict[str, Any] = {"title": title}
    needs_review = False
    review_reason = None

    if target_type == "task":
        if detail:
            patch["notes"] = detail
        if start_at:
            if _source_has_deadline_semantics(source, title) or "T" not in start_at:
                patch["due"] = start_at
                patch["due_label"] = start_at
            else:
                patch["start_at"] = start_at
                if end_at:
                    patch["end_at"] = end_at
        elif _source_has_deadline_semantics(source, title):
            needs_review = True
            review_reason = "截止日期尚未確認。"
        elif uncertain_type:
            needs_review = True
            review_reason = "項目類型或日期仍有歧義，請確認。"
    else:
        if detail:
            patch["detail"] = detail
        if start_at:
            if "T" in start_at:
                patch["start_at"] = start_at
                if end_at:
                    patch["end_at"] = end_at
            else:
                patch["date"] = start_at
                if _source_explicit_all_day_any(source) or re.search(r"(?:校慶|生日|放假|假日|紀念日)", title):
                    patch["all_day"] = True
                else:
                    needs_review = True
                    review_reason = "開始時間或全天選項尚未確認。"
        else:
            needs_review = True
            review_reason = "行程日期與開始時間尚未確認。"
        if uncertain_type and not needs_review:
            needs_review = True
            review_reason = "項目類型仍有歧義，請確認。"

    return Proposal(
        operation="create",
        target_type=target_type,
        patch=ProposalPatch.model_validate(patch),
        needs_review=needs_review,
        review_reason=review_reason,
    )


def _grouped_clock(date_iso: str, value: str, context: dict[str, Any]) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        date.fromisoformat(date_iso)
    except ValueError:
        return ""
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        return _iso_at(date_iso, value, context)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return _iso_at(date_iso, parsed.strftime("%H:%M"), context)


def _flat_add_proposal(event: dict[str, Any], source: str, context: dict[str, Any]) -> Proposal | None:
    if not isinstance(event, dict):
        raise AIProviderMalformedOutput("event add item must be an object")
    allowed = {"title", "date", "start", "end", "location", "detail"}
    if set(event) - allowed:
        raise AIProviderMalformedOutput("event add item used an unsupported field")
    if any(not isinstance(value, str) for value in event.values()):
        raise AIProviderMalformedOutput("event add fields must be strings")
    if any(not value.strip() for value in event.values()):
        raise AIProviderMalformedOutput("event add must omit empty fields")

    title = str(event.get("title") or "").strip()
    day_date = str(event.get("date") or "").strip()
    if not title:
        return None
    try:
        canonical_date = date.fromisoformat(day_date).isoformat()
    except ValueError as error:
        raise AIProviderMalformedOutput("event add date must be YYYY-MM-DD") from error

    raw_start = str(event.get("start") or "").strip()
    raw_end = str(event.get("end") or "").strip()
    location = str(event.get("location") or "").strip()
    detail = str(event.get("detail") or "").strip()

    start_at = _grouped_clock(canonical_date, raw_start, context) if raw_start else canonical_date
    end_at = _grouped_clock(canonical_date, raw_end, context) if raw_end else ""
    target_type, uncertain_type = _classify_add(source, title, start_at, end_at)
    patch: dict[str, Any] = {"title": title}
    needs_review = False
    review_reason = None

    if target_type == "task":
        if detail:
            patch["notes"] = detail
        if raw_start and not _source_has_deadline_semantics(source, title):
            patch["start_at"] = start_at
            if end_at:
                patch["end_at"] = end_at
        else:
            patch["due"] = canonical_date if not raw_start else start_at
            patch["due_label"] = patch["due"]
        if uncertain_type and not _source_has_deadline_semantics(source, title):
            needs_review = True
            review_reason = "項目類型仍有歧義，請確認。"
    else:
        if location:
            patch["location"] = location
        if detail:
            patch["detail"] = detail
        if raw_start:
            patch["start_at"] = start_at
            if end_at:
                patch["end_at"] = end_at
        else:
            patch["date"] = canonical_date
            if _source_explicit_all_day_any(source) or re.search(r"(?:校慶|生日|放假|假日|紀念日)", title):
                patch["all_day"] = True
            else:
                needs_review = True
                review_reason = "開始時間或全天選項尚未確認。"
        if uncertain_type and not needs_review:
            needs_review = True
            review_reason = "項目類型仍有歧義，請確認。"

    return Proposal(
        operation="create",
        target_type=target_type,
        patch=ProposalPatch.model_validate(patch),
        needs_review=needs_review,
        review_reason=review_reason,
    )


def _grouped_add_proposal(day_date: str, event: dict[str, Any], source: str, context: dict[str, Any]) -> Proposal | None:
    """Legacy v22 grouped-days compatibility; runtime no longer asks the model for this shape."""
    if not isinstance(event, dict):
        raise AIProviderMalformedOutput("grouped add event must be an object")
    allowed = {"title", "start", "end", "detail", "location"}
    if set(event) - allowed:
        raise AIProviderMalformedOutput("grouped add event used an unsupported field")
    if any(not isinstance(value, str) for value in event.values()):
        raise AIProviderMalformedOutput("grouped add fields must be strings")
    if any(not value.strip() for value in event.values()):
        raise AIProviderMalformedOutput("grouped add must omit empty fields")
    location = str(event.get("location") or "").strip()
    converted = {key: value for key, value in event.items() if key != "location"}
    converted["date"] = day_date
    proposal = _flat_add_proposal(converted, source, context)
    if proposal is not None and location and proposal.target_type == "event":
        proposal = proposal.model_copy(update={"patch": proposal.patch.model_copy(update={"location": location})})
    return proposal

def _candidate_payload(result: dict[str, Any], target_type: str) -> TargetCandidate:
    return TargetCandidate(
        id=str(result.get("id") or ""),
        title=str(result.get("title") or "未命名"),
        start=str(result.get("start") or result.get("due") or "") or None,
        end=str(result.get("end") or "") or None,
        location=str(result.get("location") or "") or None,
    )


def _candidate_date(result: dict[str, Any]) -> str:
    for key in ("start", "due", "start_at"):
        value = str(result.get(key) or "")
        if re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return value[:10]
    return ""


def _target_is_unambiguous(source: str, selected: dict[str, Any], candidates: list[dict[str, Any]], context: dict[str, Any]) -> bool:
    if len(candidates) <= 1:
        return True
    source_folded = " ".join(source.casefold().split())
    title_matches = [item for item in candidates if str(item.get("title") or "").casefold() in source_folded and str(item.get("title") or "").strip()]
    if len(title_matches) == 1:
        return title_matches[0].get("id") == selected.get("id")
    resolved_date, _ = resolve_source_date(source, context)
    if resolved_date:
        date_matches = [item for item in candidates if _candidate_date(item) == resolved_date]
        if len(date_matches) == 1:
            return date_matches[0].get("id") == selected.get("id")
    return False


def _normalize_update_patch(target_type: Literal["event", "task"], raw_patch: dict[str, str], source: str, context: dict[str, Any]) -> dict[str, Any]:
    if not raw_patch:
        raise AIProviderMalformedOutput("minimal update requires changed fields")
    if any(field not in MINIMAL_UPDATE_FIELDS for field in raw_patch):
        raise AIProviderMalformedOutput("minimal update used an unsupported field")
    if any(not isinstance(value, str) for value in raw_patch.values()):
        raise AIProviderMalformedOutput("minimal update values must be strings")
    patch: dict[str, Any] = {}
    for field, value in raw_patch.items():
        value = value.strip()
        if not value:
            continue
        if field in {"start_at", "end_at", "due"}:
            temporal = _normalize_minimal_temporal(value, source, context, index=1 if field == "end_at" else 0)
            if temporal:
                if field == "end_at" and not (_source_supports_end(source) or context.get("_visual_source")):
                    continue
                patch[field] = temporal
            continue
        if target_type == "event":
            if field == "notes":
                patch["detail"] = value
            elif field == "due":
                continue
            else:
                patch[field] = value
        else:
            if field == "detail":
                patch["notes"] = value
            elif field == "location":
                continue
            else:
                patch[field] = value
    if target_type == "task" and "start_at" in patch and _source_has_deadline_semantics(source):
        patch["due"] = patch.pop("start_at")
        patch.pop("end_at", None)
    return patch


def expand_minimal_output(raw: Any, source: str, context: dict[str, Any], tool_trace: list[dict[str, Any]] | None = None) -> list[Proposal]:
    """Expand flat event create output or token-minimal update deltas into Proposal objects."""
    if not isinstance(raw, dict) or set(raw) not in ({"events"}, {"days"}, {"a"}, {"u"}):
        raise AIProviderMalformedOutput("provider output must contain exactly one of events, u, or a legacy create shape")
    trace = tool_trace or []

    if "events" in raw:
        events = raw["events"]
        if not isinstance(events, list) or len(events) > MAX_CAPTURE_PROPOSALS:
            raise AIProviderMalformedOutput("event add payload is invalid")
        result: list[Proposal] = []
        for event in events:
            proposal = _flat_add_proposal(event, source, context)
            if proposal is not None:
                result.append(proposal)
        return filter_reviewable_proposals(result, source)

    # Legacy v22 grouped-days compatibility; runtime schema no longer asks the model for `days`.
    if "days" in raw:
        days = raw["days"]
        if not isinstance(days, list) or len(days) > MAX_CAPTURE_DATES:
            raise AIProviderMalformedOutput("grouped add payload is invalid")
        result: list[Proposal] = []
        for day in days:
            if not isinstance(day, dict) or set(day) != {"date", "events"}:
                raise AIProviderMalformedOutput("grouped add day must contain date and events")
            day_date = day.get("date")
            events = day.get("events")
            if not isinstance(day_date, str) or not isinstance(events, list):
                raise AIProviderMalformedOutput("grouped add day is invalid")
            for event in events:
                proposal = _grouped_add_proposal(day_date, event, source, context)
                if proposal is not None:
                    result.append(proposal)
                    if len(result) > MAX_CAPTURE_PROPOSALS:
                        raise AIProviderMalformedOutput("capture contains too many proposals")
        return filter_reviewable_proposals(result, source)

    # Legacy compatibility for old handoff fixtures; runtime schema no longer asks the model for `a`.
    if "a" in raw:
        rows = raw["a"]
        if not isinstance(rows, list) or len(rows) > MAX_CAPTURE_PROPOSALS:
            raise AIProviderMalformedOutput("minimal add payload is invalid")
        result = []
        for row in rows:
            if not isinstance(row, list):
                raise AIProviderMalformedOutput("minimal add row is invalid")
            proposal = _minimal_add_proposal(row, source, context)
            if proposal is not None:
                result.append(proposal)
        return filter_reviewable_proposals(result, source)

    rows = raw["u"]
    if not isinstance(rows, list) or len(rows) > MAX_CAPTURE_PROPOSALS:
        raise AIProviderMalformedOutput("minimal update payload is invalid")
    if rows and not trace:
        raise AIProviderMalformedOutput("updates require a read-only search tool call")

    searched: dict[str, tuple[Literal["event", "task"], dict[str, Any]]] = {}
    candidates_by_type: dict[str, list[dict[str, Any]]] = {"event": [], "task": []}
    for call in trace:
        target_type: Literal["event", "task"] = "event" if call.get("name") == "search_calendar_events" else "task"
        for item in call.get("results") or []:
            item_id = str(item.get("id") or "")
            if item_id:
                searched[item_id] = (target_type, item)
                if all(existing.get("id") != item_id for existing in candidates_by_type[target_type]):
                    candidates_by_type[target_type].append(item)

    proposals: list[Proposal] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], dict):
            raise AIProviderMalformedOutput("minimal update rows must be [target_id, patch]")
        target_id = row[0].strip()
        raw_patch = row[1]
        if target_id:
            found = searched.get(target_id)
            if found is None:
                raise AIProviderMalformedOutput("provider selected a target that was not returned by a search tool")
            target_type, selected = found
            patch_data = _normalize_update_patch(target_type, raw_patch, source, context)
            if not patch_data:
                continue
            candidates = candidates_by_type[target_type][:MAX_INTERPRET_TOOL_RESULTS]
            unambiguous = _target_is_unambiguous(source, selected, candidates, context)
            proposals.append(
                Proposal(
                    operation="update",
                    target_type=target_type,
                    target_id=target_id if unambiguous else None,
                    patch=ProposalPatch.model_validate(patch_data),
                    target_candidates=[] if unambiguous else [_candidate_payload(item, target_type) for item in candidates],
                    needs_review=not unambiguous,
                    review_reason=None if unambiguous else "找到多筆可能項目，請選擇要修改哪一筆。",
                )
            )
            continue

        searched_types: list[Literal["event", "task"]] = []
        for call in trace:
            kind: Literal["event", "task"] = "event" if call.get("name") == "search_calendar_events" else "task"
            if kind not in searched_types:
                searched_types.append(kind)
        candidate_types = [kind for kind in searched_types if candidates_by_type[kind]] or searched_types
        for target_type in candidate_types:
            patch_data = _normalize_update_patch(target_type, raw_patch, source, context)
            if not patch_data:
                continue
            candidates = candidates_by_type[target_type][:MAX_INTERPRET_TOOL_RESULTS]
            proposals.append(
                Proposal(
                    operation="update",
                    target_type=target_type,
                    target_id=None,
                    patch=ProposalPatch.model_validate(patch_data),
                    target_candidates=[_candidate_payload(item, target_type) for item in candidates],
                    needs_review=True,
                    review_reason=(
                        "找不到可唯一確認的目標，請選擇要修改的項目。"
                        if len(candidates) <= 1
                        else "找到多筆可能項目，請選擇要修改哪一筆。"
                    ),
                )
            )
    return proposals


def _prompt(
    source: str,
    context: dict[str, Any],
    images: list[dict[str, str]] | None = None,
    target_dates: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compact multimodal prompt: source + local time + bounded attachment images."""
    system = (
        "Interpret the source for OMT and return JSON only. "
        'For new events use {"events":[{"title":"...","date":"YYYY-MM-DD","start":"HH:MM","end":"HH:MM","location":"...","detail":"..."}]}. '
        "Use title for the subject or event name only. Put teacher or speaker in detail; put room, venue, or campus in location. "
        "Include only values explicitly present in the source; omit empty or unknown fields and never guess. Merge duplicate rows for the same event. "
        "Normalize date to YYYY-MM-DD and times to HH:MM. Treat classes, meetings, exams, activities, deadlines, all-day items, and recurring items as calendar events when the source supports them. "
        "Before updating an existing event or task, call the relevant search tool and use its returned id. "
        'For updates return {"u":[["target_id",{"changed_field":"new_value"}]]}. '
        "Do not return evidence, null, false, empty strings, or prose."
    )
    payload: dict[str, Any] = {
        "current_datetime": context.get("current_datetime"),
        "timezone": context.get("timezone"),
        "text": source,
    }
    if target_dates:
        system += "When target_dates is present, return only events whose date is in target_dates."
        system += "target_dates 存在時，只輸出 date 屬於這些日期的 events；其他日期完全不要輸出。"
    images = [item for item in (images or []) if isinstance(item, dict) and isinstance(item.get("data_url"), str)]
    if images:
        payload["images"] = [str(item.get("name") or f"image-{index + 1}")[:160] for index, item in enumerate(images)]
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
        ]
        content.extend({"type": "image_url", "image_url": {"url": item["data_url"]}} for item in images)
        user_content: Any = content
    else:
        user_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def _date_discovery_prompt(source: str, context: dict[str, Any], images: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    system = (
        "找出來源中所有需要建立項目的日曆日期。只回 {\"dates\":[\"YYYY-MM-DD\"]}。"
        "不要 OCR 逐字稿、不要事件內容、不要解釋、不要 null。去重並依來源出現順序排列。"
    )
    payload = {
        "current_datetime": context.get("current_datetime"),
        "timezone": context.get("timezone"),
        "text": source,
    }
    images = [item for item in (images or []) if isinstance(item, dict) and isinstance(item.get("data_url"), str)]
    if images:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
        ]
        content.extend({"type": "image_url", "image_url": {"url": item["data_url"]}} for item in images)
        user_content: Any = content
    else:
        user_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]


def _retry_dates_from_text(source: str, partial_content: Any, context: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    def add(value: str) -> None:
        try:
            canonical = date.fromisoformat(value).isoformat()
        except ValueError:
            return
        if canonical not in candidates:
            candidates.append(canonical)

    partial_text = partial_content if isinstance(partial_content, str) else ""
    for match in re.finditer(r'\"date\"\s*:\s*\"(\d{4}-\d{2}-\d{2})\"', partial_text):
        add(match.group(1))

    patterns = (
        r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)",
        r"(?:(?:\d{4})\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日?",
        r"(?<!\d)\d{1,2}/\d{1,2}(?!\d)",
    )
    seen_fragments: set[str] = set()
    matches: list[tuple[int, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, source):
            fragment = match.group(0)
            key = f"{match.start()}:{fragment}"
            if key not in seen_fragments:
                seen_fragments.add(key)
                matches.append((match.start(), fragment))
    for _, fragment in sorted(matches, key=lambda item: item[0]):
        resolved, _ = resolve_source_date(fragment, context)
        if resolved:
            add(resolved)
    return candidates[:MAX_CAPTURE_DATES]


def _merge_flat_events(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            raise AIProviderMalformedOutput("event retry did not return events")
        for event in events:
            if not isinstance(event, dict):
                raise AIProviderMalformedOutput("event retry item is invalid")
            day_date = str(event.get("date") or "")
            try:
                date.fromisoformat(day_date)
            except ValueError as error:
                raise AIProviderMalformedOutput("event retry date is invalid") from error
            signature = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if signature not in seen:
                seen.add(signature)
                merged.append(event)
            if len(merged) > MAX_CAPTURE_PROPOSALS:
                raise AIProviderMalformedOutput("capture contains too many proposals")
    return {"events": merged}

def _estimate_sanitized(value: Any) -> Any:
    if isinstance(value, list):
        return [_estimate_sanitized(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image_url" and isinstance(value.get("image_url"), dict):
            return {"type": "image_url", "image_url": {"url": "<image>"}}
        return {key: _estimate_sanitized(item) for key, item in value.items()}
    return value

def estimate_prompt_token_upper_bound(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    """UTF-8 bytes conservatively bound input tokens for multilingual prompts."""
    payload: dict[str, Any] = {"messages": _estimate_sanitized(messages)}
    if tools:
        payload["tools"] = tools
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class _CompletionText(str):
    def __new__(cls, value: str, finish_reason: str | None = None):
        obj = super().__new__(cls, value)
        obj.finish_reason = finish_reason
        return obj


class LiteLLMProvider:
    """OpenAI-compatible LiteLLM gateway adapter with bounded read-only tools."""

    name = "litellm"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        ocr_model: str | None = None,
        timeout: float = 60.0,
        ocr_fallback_model: str | None = None,
        ocr_max_image_pixels: int = 3_000_000,
        ocr_max_image_dimension: int = 2_800,
        high_load_model: str = "gpt-3.5-turbo",
        high_token_threshold: int = 6_000,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = (model or DEFAULT_INTERPRET_MODEL).strip() or DEFAULT_INTERPRET_MODEL
        # ocr_* arguments are accepted only for v20 constructor compatibility.
        # v22 uses ``model`` for both visual reading and intent extraction in one request.
        self.timeout = timeout
        # v22 always uses one interpretation model. Legacy high-load parameters
        # remain accepted for constructor compatibility but no longer affect routing.
        self.high_load_model = self.model
        self.high_token_threshold = max(1, int(high_token_threshold))

    @classmethod
    def from_environment(cls) -> "LiteLLMProvider":
        api_key = os.getenv("OMT_AI_API_KEY") or os.getenv("LITELLM_API_KEY")
        model = os.getenv("OMT_AI_MODEL") or DEFAULT_INTERPRET_MODEL
        base_url = os.getenv("OMT_AI_BASE_URL") or os.getenv("LITELLM_BASE_URL") or "https://api.openai.com/v1"
        if not api_key:
            raise AIProviderConfigurationError("AI provider is not configured: set OMT_AI_API_KEY in the backend environment")
        return cls(
            base_url,
            api_key,
            model,
            None,
            float(os.getenv("OMT_AI_TIMEOUT_SECONDS", "60")),
            None,
            3_000_000,
            2_800,
            model,
            6000,
        )

    def _chat_message(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        endpoint = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        request_body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
        # Groq's Qwen route rejects JSON mode when function tools are present.
        # Tool turns are still parsed and validated at this boundary, so omit
        # the transport-level JSON mode only for tool-enabled requests.
        if response_format and not tools:
            request_body["response_format"] = response_format
        if max_tokens:
            request_body["max_tokens"] = max_tokens
        if tools:
            request_body["tools"] = tools
            request_body["parallel_tool_calls"] = False
        if tool_choice is not None:
            request_body["tool_choice"] = tool_choice
        body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise AIProviderUnavailable(
                "AI provider is unavailable; check LiteLLM connectivity and backend configuration",
                http_status=error.code,
            ) from error
        except TimeoutError as error:
            raise AIProviderTimeout("AI provider timed out") from error
        except (urllib.error.URLError, json.JSONDecodeError) as error:
            raise AIProviderUnavailable("AI provider is unavailable; check LiteLLM connectivity and backend configuration") from error
        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise AIProviderMalformedOutput("AI provider response did not contain a chat completion") from error
        if not isinstance(message, dict):
            raise AIProviderMalformedOutput("AI provider response did not contain a valid message")
        result = dict(message)
        result["_finish_reason"] = choice.get("finish_reason") if isinstance(choice, dict) else None
        return result

    def _complete(self, model: str, messages: list[dict[str, Any]], *, response_format: dict[str, Any] | None = None, max_tokens: int | None = None) -> Any:
        message = self._chat_message(model, messages, response_format=response_format, max_tokens=max_tokens)
        content = message.get("content")
        if isinstance(content, str):
            return _CompletionText(content, message.get("_finish_reason"))
        return content

    @staticmethod
    def _tool_args(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        call_id = str(tool_call.get("id") or "") if isinstance(tool_call, dict) else ""
        raw_args = function.get("arguments") if isinstance(function, dict) else None
        if not name or not call_id or not isinstance(raw_args, str):
            raise AIProviderMalformedOutput("provider returned an invalid tool call")
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as error:
            raise AIProviderMalformedOutput("provider tool arguments were not valid JSON") from error
        if not isinstance(args, dict):
            raise AIProviderMalformedOutput("provider tool arguments must be an object")
        return name, args, call_id

    @staticmethod
    def _compact_tool_result(name: str, results: list[dict[str, Any]]) -> str:
        if name == "search_calendar_events":
            rows = [[item.get("id") or "", item.get("title") or "", item.get("start") or "", item.get("end") or "", item.get("location") or ""] for item in results]
        else:
            rows = [[item.get("id") or "", item.get("title") or "", item.get("due") or "", item.get("start_at") or "", item.get("end_at") or ""] for item in results]
        return json.dumps({"r": rows}, ensure_ascii=False, separators=(",", ":"))

    def _run_read_only_tool(self, query_tool: InterpretationQueryTool, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        requested = args.get("max_results", MAX_INTERPRET_TOOL_RESULTS)
        try:
            max_results = max(1, min(int(requested), MAX_INTERPRET_TOOL_RESULTS))
        except (TypeError, ValueError):
            max_results = MAX_INTERPRET_TOOL_RESULTS
        if name == "search_calendar_events":
            start_date = str(args.get("start_date") or "").strip()
            end_date = str(args.get("end_date") or "").strip()
            keyword = str(args.get("keyword") or "").strip() or None
            return query_tool.search_calendar_events(start_date, end_date, keyword, max_results=max_results)[:MAX_INTERPRET_TOOL_RESULTS]
        if name == "search_tasks":
            keyword = str(args.get("keyword") or "").strip() or None
            due_from = str(args.get("due_from") or "").strip() or None
            due_to = str(args.get("due_to") or "").strip() or None
            return query_tool.search_tasks(keyword, due_from, due_to, max_results=max_results)[:MAX_INTERPRET_TOOL_RESULTS]
        raise AIProviderMalformedOutput("provider attempted an unavailable tool")

    def _discover_retry_dates(
        self,
        model: str,
        source: str,
        context: dict[str, Any],
        images: list[dict[str, str]],
        partial_content: Any,
        retry_budget: list[int],
    ) -> list[str]:
        source_dates = _retry_dates_from_text(source, "", context)
        if source_dates and not images and not context.get("_visual_source"):
            return source_dates
        dates = _retry_dates_from_text("", partial_content, context)
        if retry_budget[0] <= 0:
            raise AIProviderMalformedOutput("output-length recovery exceeded the retry-call budget")
        retry_budget[0] -= 1
        message = self._chat_message(
            model,
            _date_discovery_prompt(source, context, images),
            response_format=DATE_DISCOVERY_RESPONSE_FORMAT,
            max_tokens=MINIMAL_INTERPRET_OUTPUT_TOKENS,
        )
        if message.get("_finish_reason") == "length":
            raise AIProviderMalformedOutput("date discovery was truncated after an output-length stop")
        raw = _extract_json(message.get("content"))
        if not isinstance(raw, dict) or set(raw) != {"dates"} or not isinstance(raw.get("dates"), list):
            raise AIProviderMalformedOutput("date discovery returned an invalid payload")
        for item in raw["dates"]:
            if not isinstance(item, str):
                raise AIProviderMalformedOutput("date discovery returned a non-string date")
            try:
                canonical = date.fromisoformat(item).isoformat()
            except ValueError as error:
                raise AIProviderMalformedOutput("date discovery returned an invalid date") from error
            if canonical not in dates:
                dates.append(canonical)
            if len(dates) >= MAX_CAPTURE_DATES:
                break
        return dates

    def _retry_event_date_batch(
        self,
        model: str,
        source: str,
        context: dict[str, Any],
        images: list[dict[str, str]],
        target_dates: list[str],
        retry_budget: list[int],
    ) -> list[dict[str, Any]]:
        if retry_budget[0] <= 0:
            raise AIProviderMalformedOutput("output-length recovery exceeded the retry-call budget")
        retry_budget[0] -= 1
        message = self._chat_message(
            model,
            _prompt(source, context, images, target_dates),
            response_format=MINIMAL_ADD_RESPONSE_FORMAT,
            max_tokens=MINIMAL_INTERPRET_OUTPUT_TOKENS,
        )
        if message.get("_finish_reason") == "length":
            if len(target_dates) <= 1:
                raise AIProviderMalformedOutput(f"provider output for {target_dates[0]} was still truncated")
            midpoint = max(1, len(target_dates) // 2)
            return self._retry_event_date_batch(model, source, context, images, target_dates[:midpoint], retry_budget) + self._retry_event_date_batch(
                model, source, context, images, target_dates[midpoint:], retry_budget
            )

        raw = _extract_json(message.get("content"))
        if not isinstance(raw, dict) or set(raw) != {"events"} or not isinstance(raw.get("events"), list):
            raise AIProviderMalformedOutput("date-batched retry must return events")
        returned_dates = {str(event.get("date") or "") for event in raw["events"] if isinstance(event, dict)}
        expected_dates = set(target_dates)
        if returned_dates - expected_dates:
            raise AIProviderMalformedOutput("date-batched retry returned an out-of-scope date")
        if expected_dates - returned_dates:
            raise AIProviderMalformedOutput("date-batched retry omitted a requested date")
        return [raw]

    def _recover_event_add_after_length(
        self,
        model: str,
        source: str,
        context: dict[str, Any],
        images: list[dict[str, str]],
        partial_content: Any,
    ) -> list[Proposal]:
        retry_budget = [MAX_LENGTH_RECOVERY_CALLS]
        dates = self._discover_retry_dates(model, source, context, images, partial_content, retry_budget)
        if not dates:
            raise AIProviderMalformedOutput("provider output was truncated and no retry dates could be identified")
        chunks: list[dict[str, Any]] = []
        for start in range(0, len(dates), DATE_RETRY_BATCH_SIZE):
            chunks.extend(self._retry_event_date_batch(model, source, context, images, dates[start:start + DATE_RETRY_BATCH_SIZE], retry_budget))
        return expand_minimal_output(_merge_flat_events(chunks), source, context, [])

    def _interpret_with_tools_core(
        self,
        source: str,
        context: dict[str, Any],
        query_tool: InterpretationQueryTool,
        images: list[dict[str, str]] | None = None,
    ) -> list[Proposal]:
        messages: list[dict[str, Any]] = list(_prompt(source, context, images))
        model = self.model
        tool_trace: list[dict[str, Any]] = []
        # Image captures are create-oriented: the model must read the image and
        # return the event batch in one structured response. Sending calendar
        # search tools alongside a timetable image made Mistral repeatedly call
        # the same read-only tool instead of producing its final JSON payload.
        allow_tools = not images
        try:
            for _ in range(MAX_INTERPRET_TOOL_CALLS + 1):
                message = self._chat_message(
                    model,
                    messages,
                    response_format=MINIMAL_UPDATE_RESPONSE_FORMAT if tool_trace else MINIMAL_ADD_RESPONSE_FORMAT,
                    max_tokens=MINIMAL_INTERPRET_OUTPUT_TOKENS,
                    tools=READ_ONLY_INTERPRET_TOOLS if allow_tools else None,
                    tool_choice="auto" if allow_tools else None,
                )
                tool_calls = message.get("tool_calls") or []
                if tool_calls:
                    if len(tool_trace) + len(tool_calls) > MAX_INTERPRET_TOOL_CALLS:
                        raise AIProviderMalformedOutput("provider exceeded the read-only tool-call budget")
                    messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})
                    for tool_call in tool_calls:
                        name, args, call_id = self._tool_args(tool_call)
                        try:
                            results = self._run_read_only_tool(query_tool, name, args)
                            tool_content = self._compact_tool_result(name, results)
                        except (TypeError, ValueError):
                            results = []
                            tool_content = '{"r":[]}'
                        tool_trace.append({"name": name, "args": args, "results": results})
                        messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": tool_content})
                    continue
                if message.get("_finish_reason") == "length":
                    if tool_trace:
                        raise AIProviderMalformedOutput("update output was truncated after the read-only search")
                    return self._recover_event_add_after_length(model, source, context, images or [], message.get("content"))
                return expand_minimal_output(_extract_json(message.get("content")), source, context, tool_trace)
            raise AIProviderMalformedOutput("provider did not finish after the read-only tool-call budget")
        except AIInterpretationError as error:
            error.model_alias = model
            raise

    def interpret_with_tools(self, source: str, context: dict[str, Any], query_tool: InterpretationQueryTool) -> list[Proposal]:
        return self._interpret_with_tools_core(source, context, query_tool, [])

    def interpret_multimodal_with_tools(
        self, source: str, context: dict[str, Any], images: list[dict[str, str]], query_tool: InterpretationQueryTool
    ) -> list[Proposal]:
        return self._interpret_with_tools_core(source, context, query_tool, images)

    def _interpret_core(self, source: str, context: dict[str, Any], images: list[dict[str, str]] | None = None) -> list[Proposal]:
        messages = _prompt(source, context, images)
        model = self.model
        try:
            content = self._complete(model, messages, response_format=MINIMAL_ADD_RESPONSE_FORMAT, max_tokens=MINIMAL_INTERPRET_OUTPUT_TOKENS)
            if getattr(content, "finish_reason", None) == "length":
                return self._recover_event_add_after_length(model, source, context, images or [], content)
            return expand_minimal_output(_extract_json(content), source, context, [])
        except AIInterpretationError as error:
            error.model_alias = model
            raise

    def interpret(self, source: str, context: dict[str, Any]) -> list[Proposal]:
        return self._interpret_core(source, context, [])

    def interpret_multimodal(self, source: str, context: dict[str, Any], images: list[dict[str, str]]) -> list[Proposal]:
        return self._interpret_core(source, context, images)


class DeterministicFakeProvider:
    """Test-only provider. It is injected by tests and never selected by runtime config."""

    name = "fixture"

    def __init__(self, output: Any):
        self.output = output

    def interpret(self, source: str, context: dict[str, Any]) -> list[Proposal]:
        return validate_provider_output(self.output, context, source)

    def interpret_multimodal(self, source: str, context: dict[str, Any], images: list[dict[str, str]]) -> list[Proposal]:
        return self.interpret(source, context)

    def interpret_multimodal_with_tools(
        self, source: str, context: dict[str, Any], images: list[dict[str, str]], query_tool: InterpretationQueryTool
    ) -> list[Proposal]:
        del query_tool
        return self.interpret_multimodal(source, context, images)


def provider_from_environment() -> LiteLLMProvider:
    provider = (os.getenv("OMT_AI_PROVIDER") or "litellm").strip().lower()
    if provider != "litellm":
        raise AIProviderConfigurationError(f"Unsupported AI provider: {provider}")
    return LiteLLMProvider.from_environment()
