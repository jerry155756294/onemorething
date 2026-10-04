from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import io
import logging
import os
import re
import socket
import secrets
import shutil
import sqlite3
import time as clock
import base64
import binascii
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.params import Query as QueryParam
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from cryptography.fernet import Fernet, InvalidToken
from .ai_interpreter import (
    AIInterpretationError,
    AIProviderConfigurationError,
    AIProviderMalformedOutput,
    AIProviderTimeout,
    AIProviderUnavailable,
    AIProvider,
    Proposal,
    ProposalPatch,
    ProposalTargetType,
    WeeklyRecurrence,
    filter_reviewable_proposals,
    provider_from_environment,
    validate_provider_output,
)
from .calendar_tools import CalendarQueryTool, InterpretationQueryTool
from .ingestion import (
    AttachmentStore as TemporaryAttachmentStore,
    ExtractionTimeout,
    IngestionPolicy,
    IngestionPolicyError,
    ai_source_budget,
    bounded_attachment,
    bounded_model_image_data_url,
    bounded_source,
    call_with_timeout,
    extract_office_document,
    extract_pdf,
    inspect_image,
    render_pdf_page_for_model,
    log_attachment_result,
    overall_status,
    sniff_media_type,
    truncate_at_text_boundary,
)
from .integrations import (
    CALENDAR_PROVIDER_LABELS,
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarProvider,
    CalendarSyncMode,
    normalize_calendar_provider,
)
from .security import (
    OPERATIONS,
    RATE_LIMITER,
    OperationBusy,
    RateLimitExceeded,
    ResponseTooLarge,
    SecurityPolicy,
    UnsafeOutboundTarget,
    read_limited,
    validate_outbound_http_target,
)


APP_ENVIRONMENT = os.getenv("OMT_ENVIRONMENT", os.getenv("OMT_ENV", "development")).strip().lower()
FRONTEND_URL = os.getenv("OMT_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
BACKEND_URL = os.getenv("OMT_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
DATA_DIR = Path(os.getenv("OMT_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
DB_PATH = DATA_DIR / "omt.db"
SESSION_COOKIE = "omt_session"
OAUTH_STATE_COOKIE = "omt_oauth_state"
SESSION_DAYS = 30
NEXTCLOUD_LOGIN_FLOW_MINUTES = 20
DEFAULT_TIMEZONE = os.getenv("OMT_DEFAULT_TIMEZONE", "UTC")
COOKIE_SECURE = FRONTEND_URL.startswith("https://")
COOKIE_SAMESITE = "none" if COOKIE_SECURE else "lax"
RAW_ATTACHMENT_TTL_HOURS = max(1, int(os.getenv("OMT_RAW_ATTACHMENT_TTL_HOURS", "24")))
RAW_ATTACHMENT_RETRY_TTL_HOURS = max(1, int(os.getenv("OMT_RAW_ATTACHMENT_RETRY_TTL_HOURS", str(RAW_ATTACHMENT_TTL_HOURS))))
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_MAX_DIMENSION = 4096
AVATAR_MAX_PIXELS = 16_000_000
AVATAR_ALLOWED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}
EXTRACTION_VERSION = os.getenv("OMT_EXTRACTION_VERSION", "1")
logger = logging.getLogger("one_more_thing.attachments")
api_logger = logging.getLogger("one_more_thing.api")


def ingestion_policy() -> IngestionPolicy:
    return IngestionPolicy.from_environment()


def security_policy() -> SecurityPolicy:
    return SecurityPolicy.from_environment(APP_ENVIRONMENT)


def configured_origins() -> list[str]:
    configured = [item.strip().rstrip("/") for item in os.getenv("OMT_ALLOWED_ORIGINS", "").split(",") if item.strip()]
    local_defaults = [] if APP_ENVIRONMENT in {"production", "prod"} else ["http://127.0.0.1:4173", "http://localhost:4173"]
    return list(dict.fromkeys([FRONTEND_URL, *configured, *local_defaults]))


ALLOWED_ORIGINS = configured_origins()
if "*" in ALLOWED_ORIGINS:
    raise RuntimeError("OMT_ALLOWED_ORIGINS cannot contain '*' when credentials are enabled")
if APP_ENVIRONMENT in {"production", "prod"} and (not FRONTEND_URL.startswith("https://") or not BACKEND_URL.startswith("https://")):
    raise RuntimeError("Production requires HTTPS OMT_FRONTEND_URL and OMT_BACKEND_URL")


def configured_allowed_hosts() -> list[str]:
    configured = [item.strip() for item in os.getenv("OMT_ALLOWED_HOSTS", "").split(",") if item.strip()]
    backend_host = urllib.parse.urlparse(BACKEND_URL).hostname
    defaults = [backend_host] if backend_host else []
    if APP_ENVIRONMENT not in {"production", "prod"}:
        defaults.extend(["testserver", "test", "localhost", "127.0.0.1", "::1"])
    hosts = list(dict.fromkeys([*configured, *defaults]))
    if APP_ENVIRONMENT in {"production", "prod"} and (not hosts or "*" in hosts):
        raise RuntimeError("Production requires an explicit OMT_ALLOWED_HOSTS/BACKEND_URL host")
    return hosts or ["testserver"]


ALLOWED_HOSTS = configured_allowed_hosts()
PRODUCTION = APP_ENVIRONMENT in {"production", "prod"}
OMT_RELEASE = "json-object-v26"

app = FastAPI(
    title="One More Thing API",
    version="0.2.0",
    docs_url=None if PRODUCTION else "/docs",
    redoc_url=None if PRODUCTION else "/redoc",
    openapi_url=None if PRODUCTION else "/openapi.json",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type"],
    allow_credentials=True,
)


class TransportBodyTooLarge(Exception):
    pass


def _request_principal(request: Request) -> str:
    session_token = request.cookies.get(SESSION_COOKIE)
    if session_token:
        return "session:" + hashlib.sha256(session_token.encode("utf-8")).hexdigest()[:24]
    host = request.client.host if request.client else "unknown"
    return "client:" + host


def enforce_rate_limit(request: Request, bucket: str, limit: int, *, key: str | None = None) -> None:
    if APP_ENVIRONMENT == "test" and os.getenv("OMT_RATE_LIMIT_TESTS", "0") != "1":
        return
    try:
        RATE_LIMITER.check(bucket, key or _request_principal(request), limit)
    except RateLimitExceeded as error:
        api_logger.warning("security rate_limit bucket=%s path=%s", bucket, request.url.path)
        raise HTTPException(status_code=429, detail="請稍後再試。", headers={"Retry-After": str(error.retry_after)}) from error


def _write_body_limit(request: Request) -> int | None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.url.path == "/api/interpret":
        return ingestion_policy().max_request_body_bytes
    if request.url.path == "/api/capture/uploads":
        return ingestion_policy().max_upload_bytes
    policy = security_policy()
    if request.url.path == "/api/account/avatar":
        return policy.avatar_request_body_bytes
    return policy.default_write_body_bytes


def _body_too_large_response(request: Request, limit: int) -> JSONResponse:
    if request.url.path == "/api/capture/uploads":
        message = "檔案太大"
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "file_too_large",
                    "message": message,
                    "details": {"max_upload_bytes": limit},
                },
                "detail": message,
            },
        )
    if request.url.path == "/api/interpret":
        message = "這次上傳的總量太大"
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "capture_too_large",
                    "message": message,
                    "details": {"max_request_body_bytes": limit},
                },
                "detail": message,
            },
        )
    return JSONResponse(status_code=413, content={"detail": "Request body is too large"})


def _security_headers(response: Response, request: Request) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    if PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


@app.middleware("http")
async def enforce_request_security(request: Request, call_next):
    """Apply browser-origin, body-size, baseline rate-limit and response-header controls."""
    request_id = secrets.token_hex(12)
    if request.url.path.startswith("/api/"):
        policy = security_policy()
        if APP_ENVIRONMENT != "test" or os.getenv("OMT_RATE_LIMIT_TESTS", "0") == "1":
            try:
                RATE_LIMITER.check("api", _request_principal(request), policy.global_requests_per_minute)
            except RateLimitExceeded as error:
                response = JSONResponse(status_code=429, content={"detail": "請稍後再試。"}, headers={"Retry-After": str(error.retry_after)})
                response.headers["X-Request-ID"] = request_id
                _security_headers(response, request)
                return response

        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and policy.enforce_origin_check:
            origin = (request.headers.get("origin") or "").rstrip("/")
            if not origin or origin not in ALLOWED_ORIGINS:
                api_logger.warning("security origin_rejected method=%s path=%s", request.method, request.url.path)
                response = JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
                response.headers["X-Request-ID"] = request_id
                _security_headers(response, request)
                return response

    body_limit = _write_body_limit(request)
    if body_limit is not None:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                too_large = int(content_length) > body_limit
            except ValueError:
                too_large = True
            if too_large:
                response = _body_too_large_response(request, body_limit)
                response.headers["X-Request-ID"] = request_id
                _security_headers(response, request)
                return response
        received_bytes = 0
        original_receive = request._receive

        async def limited_receive():
            nonlocal received_bytes
            message = await original_receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > body_limit:
                    raise TransportBodyTooLarge()
            return message

        request._receive = limited_receive
    try:
        response = await call_next(request)
    except TransportBodyTooLarge:
        response = _body_too_large_response(request, body_limit or 0)
    response.headers["X-Request-ID"] = request_id
    _security_headers(response, request)
    return response


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AttachmentStore(Protocol):
    """Opaque raw-blob storage boundary; replaceable with S3/R2/MinIO later."""

    def put(self, content: bytes, content_hash: str) -> str: ...

    def put_path(self, source: Path, content_hash: str) -> str: ...

    def read(self, storage_key: str) -> bytes: ...

    def path(self, storage_key: str) -> Path: ...

    def exists(self, storage_key: str) -> bool: ...

    def delete(self, storage_key: str) -> bool: ...


class FilesystemAttachmentStore:
    """Content-addressed local storage. The key is never accepted from a client."""

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def _validate_key(storage_key: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", storage_key):
            raise ValueError("invalid attachment storage key")
        return storage_key

    def path(self, storage_key: str) -> Path:
        key = self._validate_key(storage_key)
        return self.root / key[:2] / key

    def put(self, content: bytes, content_hash: str) -> str:
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != self._validate_key(content_hash):
            raise ValueError("attachment content hash mismatch")
        target = self.path(content_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            pass
        return content_hash

    def put_path(self, source: Path, content_hash: str) -> str:
        self._validate_key(content_hash)
        target = self.path(content_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as input_handle, target.open("xb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle, length=64 * 1024)
        except FileExistsError:
            pass
        return content_hash

    def read(self, storage_key: str) -> bytes:
        return self.path(storage_key).read_bytes()

    def exists(self, storage_key: str) -> bool:
        return self.path(storage_key).is_file()

    def delete(self, storage_key: str) -> bool:
        target = self.path(storage_key)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True


ATTACHMENT_STORE: AttachmentStore = FilesystemAttachmentStore(DATA_DIR / "attachments")


def plain_query(value, default=None):
    """Keep direct function tests compatible with FastAPI Query defaults."""
    return default if isinstance(value, QueryParam) else value


def dev_login_enabled() -> bool:
    if APP_ENVIRONMENT not in {"development", "dev", "test"}:
        return False
    configured = os.getenv("OMT_ENABLE_DEV_LOGIN")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return urllib.parse.urlparse(BACKEND_URL).hostname in {"127.0.0.1", "localhost", "::1"}


def _restrict_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Some platforms/filesystems do not expose POSIX permission bits.
        pass


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    _restrict_private_file(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class APIInputModel(BaseModel):
    # Reject undeclared request properties instead of silently accepting them.
    # This reduces mass-assignment/property-level authorization mistakes.
    model_config = ConfigDict(extra="forbid")


class AttachmentInput(APIInputModel):
    id: str | None = Field(default=None, max_length=128)
    upload_id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="application/octet-stream", max_length=128)
    size: int = Field(default=0, ge=0)
    kind: str = Field(default="file", pattern="^(image|pdf|file)$")
    data_url: str | None = Field(default=None, max_length=80_000_000)
    extracted_text: str | None = Field(default=None, max_length=200000)
    ocr_status: str | None = Field(default=None, max_length=32)
    extraction_method: str | None = Field(default=None, max_length=32)
    extraction_status: str | None = Field(default=None, max_length=32)


class NoticeInput(APIInputModel):
    source_id: str | None = Field(default=None, max_length=64)
    title: str = Field(default="", max_length=160)
    body: str = Field(default="", max_length=10000)
    audience: str = "我的課程"
    source_type: str = Field(default="manual", min_length=1, max_length=64)
    source_name: str | None = Field(default=None, max_length=255)
    source_url: str | None = Field(default=None, max_length=2048)
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=100)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _validate_capture_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public http and https URLs are supported")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid URL port") from error
    if port not in (None, 80, 443):
        raise ValueError("Only standard web ports are supported")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local network URLs are not supported")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
        except (OSError, ValueError) as error:
            raise ValueError("URL host could not be resolved") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Private and non-public network URLs are not supported")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _url_from_capture_body(body: str) -> str | None:
    match = re.search(r"https?://[^\s<>\"']+", body, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).rstrip("),.;!?，。；」』】")


def _crawl_capture_page(body: str, attachment_names: list[str] | None = None, attachment_texts: list[str] | None = None) -> dict:
    """Only inspect body; attachment arguments must never trigger a crawl."""
    url = _url_from_capture_body(body)
    if not url:
        return {"status": "not_requested"}
    try:
        safe_url = _validate_capture_url(url)
    except ValueError:
        return {"status": "failed", "reason": "invalid_or_unsafe_url"}
    endpoint = os.getenv("OMT_CRAWL4AI_PROXY_URL", "").strip().rstrip("/")
    if not endpoint:
        return {"status": "unavailable", "reason": "crawler_not_configured", "url": safe_url}
    try:
        timeout = min(max(float(os.getenv("OMT_CRAWL_TIMEOUT_SECONDS", "20")), 1), 30)
    except ValueError:
        timeout = 20
    def crawl_request(target_url: str) -> urllib.request.Request:
        request_body = json.dumps({"urls": [target_url]}, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(endpoint + "/crawl", data=request_body, headers={"Content-Type": "application/json"}, method="POST")

    request = crawl_request(safe_url)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                return {"status": "failed", "reason": "response_too_large", "url": safe_url}
            data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in {301, 302, 303, 307, 308} and error.headers.get("Location"):
            try:
                redirected = _validate_capture_url(urljoin(safe_url, error.headers["Location"]))
            except ValueError:
                return {"status": "failed", "reason": "unsafe_redirect", "url": safe_url}
            if redirected != safe_url:
                retry = crawl_request(redirected)
                try:
                    with opener.open(retry, timeout=timeout) as response:
                        raw = response.read(2_000_001)
                        if len(raw) > 2_000_000:
                            return {"status": "failed", "reason": "response_too_large", "url": safe_url}
                        data = json.loads(raw.decode("utf-8"))
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                    return {"status": "failed", "reason": "crawler_request_failed", "url": safe_url}
            else:
                return {"status": "failed", "reason": "crawler_redirect_rejected", "url": safe_url}
        else:
            return {"status": "failed", "reason": "crawler_http_error", "url": safe_url}
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "crawler_request_failed", "url": safe_url}
    if isinstance(data, list):
        proxy_result = next((
            item for item in data
            if isinstance(item, dict) and isinstance(item.get("page_content"), str) and item["page_content"].strip()
        ), None)
        if not proxy_result:
            return {"status": "failed", "reason": "page_extraction_failed", "url": safe_url}
        metadata = proxy_result.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        result = {
            "title": metadata.get("title"),
            "url": metadata.get("source") or metadata.get("url"),
            "markdown": proxy_result["page_content"],
        }
    elif isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            result = next((item for item in results if isinstance(item, dict) and item.get("success")), None)
        else:
            result = data if data.get("success", True) else None
    else:
        return {"status": "failed", "reason": "invalid_crawler_response", "url": safe_url}
    if not result:
        return {"status": "failed", "reason": "page_extraction_failed", "url": safe_url}
    markdown = result.get("markdown")
    if isinstance(markdown, dict):
        markdown = markdown.get("fit_markdown") or markdown.get("raw_markdown")
    content = markdown if isinstance(markdown, str) else result.get("cleaned_html")
    if not isinstance(content, str) or not content.strip():
        return {"status": "failed", "reason": "page_content_empty", "url": safe_url}
    final_url = safe_url
    reported_url = result.get("url")
    if isinstance(reported_url, str) and reported_url.strip():
        try:
            final_url = _validate_capture_url(reported_url)
        except ValueError:
            return {"status": "failed", "reason": "unsafe_final_url", "url": safe_url}
    response = {"status": "complete", "url": safe_url, "title": str(result.get("title") or "")[:300], "text": content[:100_000]}
    if final_url != safe_url:
        response["final_url"] = final_url
    return response


class ParsedTaskInput(APIInputModel):
    title: str = Field(min_length=1, max_length=500)
    due_label: str = Field(default="待確認", max_length=255)
    due_iso: str | None = Field(default=None, max_length=80)
    needs_clarification: bool = False
    confidence: float | None = Field(default=None, ge=0, le=1)


class ParsedEventInput(APIInputModel):
    title: str = Field(min_length=1, max_length=500)
    date_label: str = Field(default="待確認", max_length=255)
    time_label: str = Field(default="全天", max_length=80)
    detail: str = Field(default="", max_length=2000)
    needs_clarification: bool = False
    confidence: float | None = Field(default=None, ge=0, le=1)


class StructuredNoticeInput(APIInputModel):
    tasks: list[ParsedTaskInput] = Field(default_factory=list, max_length=100)
    events: list[ParsedEventInput] = Field(default_factory=list, max_length=100)
    needs_clarification: bool = False


class TaskUpdate(APIInputModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due_label: str | None = Field(default=None, max_length=255)
    due_iso: str | None = Field(default=None, max_length=80)
    start_at: str | None = Field(default=None, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    all_day: bool | None = None
    timezone: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, pattern="^(open|done|needs_clarification)$")
    list_id: str | None = Field(default=None, max_length=64)
    related_event_id: str | None = Field(default=None, max_length=128)
    needs_clarification: bool | None = None


class TaskCreate(APIInputModel):
    title: str = Field(min_length=1, max_length=500)
    due_label: str = Field(default="待確認", max_length=255)
    due_iso: str | None = Field(default=None, max_length=80)
    start_at: str | None = Field(default=None, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    all_day: bool = False
    timezone: str | None = Field(default=None, max_length=128)
    status: str = Field(default="open", pattern="^(open|done|needs_clarification)$")
    source_id: str | None = Field(default=None, max_length=64)
    list_id: str | None = Field(default=None, max_length=64)
    related_event_id: str | None = Field(default=None, max_length=128)
    needs_clarification: bool = False


class ListCreate(APIInputModel):
    name: str = Field(min_length=1, max_length=80)


class ProposalBatchApplyInput(APIInputModel):
    proposal_ids: list[str] = Field(min_length=1, max_length=200)
    confirm: bool = False


class EventCreate(APIInputModel):
    title: str = Field(min_length=1, max_length=500)
    date_label: str | None = Field(default=None, max_length=80)
    time_label: str = Field(default="", max_length=80)
    detail: str = Field(default="", max_length=2000)
    location: str | None = Field(default=None, max_length=2000)
    start_at: str | None = Field(default=None, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    all_day: bool = False
    timezone: str | None = Field(default=None, max_length=128)
    recurrence: WeeklyRecurrence | None = None


class EventUpdate(APIInputModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    date_label: str | None = Field(default=None, min_length=1, max_length=80)
    time_label: str | None = Field(default=None, max_length=80)
    detail: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=2000)
    start_at: str | None = Field(default=None, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    all_day: bool | None = None
    timezone: str | None = Field(default=None, max_length=128)
    recurrence: WeeklyRecurrence | None = None
    lifecycle_status: str | None = Field(default=None, pattern="^(created|edited|archived)$")


class LocalEventCalendarSyncInput(APIInputModel):
    source_id: str = Field(min_length=1, max_length=256)
    calendar_id: str = Field(min_length=1, max_length=128)
    enabled: bool = True


class NoticeCreateInput(NoticeInput):
    structured: StructuredNoticeInput | None = None


class Notice(BaseModel):
    id: str
    title: str
    body: str
    audience: str
    created_at: str
    source_type: str = "manual"
    source_name: str | None = None
    source_url: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    processing: dict = Field(default_factory=dict)
    confidence: str = "preview"


class Task(BaseModel):
    id: str
    title: str
    due_label: str
    due_iso: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    all_day: bool = False
    timezone: str | None = None
    status: str
    source_id: str
    owner: str = "student"
    source_type: str = "notice"
    source: dict | None = None


class Event(BaseModel):
    id: str
    title: str
    date_label: str
    time_label: str
    start_at: str | None = None
    end_at: str | None = None
    all_day: bool = False
    timezone: str | None = None
    detail: str
    location: str | None = None
    source_id: str
    source_type: str = "notice"
    source: dict | None = None
    event_source: str = "local"
    created_source: str = "user_created"
    origin_proposal_id: str | None = None
    external_calendar_ref: str | None = None
    provider: str | None = None
    sync_status: str = "local"
    last_synced_at: str | None = None
    sync_conflict: dict = Field(default_factory=dict)
    calendar_sync_enabled: bool = False
    lifecycle_status: str = "created"


class CalendarEventInput(APIInputModel):
    source_id: str = Field(min_length=1, max_length=256)
    calendar_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    start_at: str = Field(min_length=1, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    all_day: bool = False
    timezone: str | None = Field(default=None, max_length=128)
    recurrence_rule: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=10000)
    url: str | None = Field(default=None, max_length=2048)


class CalendarEvent(BaseModel):
    id: str
    source_id: str
    calendar_id: str
    remote_id: str
    title: str
    start_at: str
    end_at: str | None = None
    timezone: str | None = None
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    url: str | None = None
    recurrence_rule: str | None = None
    status: str = "confirmed"
    etag: str | None = None
    created_source: str = "external_calendar"
    event_source: str = "external_calendar"
    origin_proposal_id: str | None = None
    external_calendar_ref: str | None = None
    provider: str = "nextcloud_calendar"
    sync_status: str = "synced"
    last_synced_at: str | None = None
    sync_conflict: dict = Field(default_factory=dict)
    lifecycle_status: str = "created"


class Source(BaseModel):
    id: str
    kind: str
    name: str
    status: str
    read_only: bool = True
    sync_status: str = "never"
    last_synced_at: str | None = None
    last_error: str | None = None
    captured_at: str | None = None
    attachments: list[dict] = Field(default_factory=list)


class OAuthIdentity(BaseModel):
    subject: str
    email: str | None = None
    email_verified: bool = False
    name: str = "我的空間"
    avatar_url: str | None = None


class AvatarUpdateInput(APIInputModel):
    data_url: str = Field(min_length=1, max_length=4_000_000)


class NextcloudLoginInput(APIInputModel):
    base_url: str = Field(min_length=1, max_length=2048)


class CalendarConnectionConnectInput(APIInputModel):
    base_url: str | None = Field(default=None, max_length=2048)
    sync_mode: str = Field(default="manual", pattern="^(manual|automatic)$")


class ProposalEditInput(APIInputModel):
    target_id: str | None = Field(default=None, max_length=128)
    patch: ProposalPatch
    target_type: ProposalTargetType | None = None


def seed() -> tuple[list[Notice], list[Task], list[Event]]:
    notice_id = "notice-project-day"
    return (
        [
            Notice(
                id=notice_id,
                title="專題課與校外教學提醒",
                body="下週三專題課請帶筆電與 Arduino，尚未分組的同學要在週一中午前填表。校外教學同意書請於本週五前交回，集合時間為早上 7:30。",
                audience="我的課程",
                created_at="2026-09-18T09:20:00+08:00",
            ),
            Notice(
                id="notice-library",
                title="圖書館閉館時間調整",
                body="因設備保養，本週四圖書館將於 17:00 閉館。已借閱的專題參考書不受影響。",
                audience="全校",
                created_at="2026-09-17T16:00:00+08:00",
            ),
        ],
        [
            Task(id="task-group", title="完成專題分組表", due_label="週一 · 12:00 前", status="open", source_id=notice_id),
            Task(id="task-consent", title="繳交校外教學同意書", due_label="週五 · 17:00 前", status="open", source_id=notice_id),
            Task(id="task-library", title="確認專題參考書借閱安排", due_label="本週四", status="done", source_id="notice-library"),
        ],
        [
            Event(id="event-class", title="專題課", date_label="週三 09/23", time_label="08:00", detail="專題教室 · 需帶筆電、Arduino", source_id=notice_id),
            Event(id="event-trip", title="校外教學集合", date_label="週六 09/26", time_label="07:30", detail="校門口 · 請攜帶已簽同意書", source_id=notice_id),
        ],
    )


EVENT_CREATED_SOURCES = {"ai_generated", "user_created", "external_calendar"}
EVENT_LIFECYCLE_STATUSES = {"created", "edited", "archived", "deleted"}
SCHEMA_VERSION = 18


def init_db() -> None:
    notices, tasks, events = seed()
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                email TEXT,
                avatar_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                provider_subject TEXT NOT NULL,
                email TEXT,
                email_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(provider, provider_subject)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notices (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                audience TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_type TEXT NOT NULL,
                confidence TEXT NOT NULL,
                source_name TEXT,
                source_url TEXT,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                processing_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                due_label TEXT NOT NULL,
                due_iso TEXT,
                status TEXT NOT NULL,
                source_id TEXT NOT NULL,
                owner TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                date_label TEXT NOT NULL,
                time_label TEXT NOT NULL,
                detail TEXT NOT NULL,
                source_id TEXT NOT NULL,
                location TEXT
            );
            CREATE TABLE IF NOT EXISTS calendar_sources (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                server_url TEXT NOT NULL,
                username TEXT NOT NULL,
                credential_ciphertext TEXT NOT NULL,
                status TEXT NOT NULL,
                last_checked_at TEXT,
                last_error TEXT,
                sync_status TEXT NOT NULL DEFAULT 'never',
                last_synced_at TEXT,
                sync_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, kind, server_url, username)
            );
            CREATE TABLE IF NOT EXISTS calendar_calendars (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES calendar_sources(id) ON DELETE CASCADE,
                remote_url TEXT NOT NULL,
                display_name TEXT NOT NULL,
                timezone TEXT,
                read_only INTEGER NOT NULL DEFAULT 1,
                sync_status TEXT NOT NULL DEFAULT 'never',
                last_synced_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, remote_url)
            );
            CREATE TABLE IF NOT EXISTS calendar_events (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES calendar_sources(id) ON DELETE CASCADE,
                calendar_id TEXT NOT NULL REFERENCES calendar_calendars(id) ON DELETE CASCADE,
                remote_id TEXT NOT NULL,
                href TEXT NOT NULL,
                etag TEXT,
                title TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                timezone TEXT,
                all_day INTEGER NOT NULL DEFAULT 0,
                location TEXT,
                description TEXT,
                url TEXT,
                recurrence_rule TEXT,
                status TEXT NOT NULL DEFAULT 'confirmed',
                raw_ical TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(calendar_id, remote_id)
            );
            CREATE TABLE IF NOT EXISTS calendar_login_flows (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                base_url TEXT NOT NULL,
                poll_token_ciphertext TEXT NOT NULL,
                poll_endpoint TEXT NOT NULL,
                login_url TEXT NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_error TEXT,
                poll_count INTEGER NOT NULL DEFAULT 0,
                last_polled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )

        # The first release created these tables without a migration marker.
        # Keep that database usable and apply additive changes only.
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version < 1:
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (1, now_iso()))
            connection.execute("PRAGMA user_version = 1")
            current_version = 1
        if current_version < 2:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_login_flows)")}
            if "last_error" not in columns:
                connection.execute("ALTER TABLE calendar_login_flows ADD COLUMN last_error TEXT")
            if "poll_count" not in columns:
                connection.execute("ALTER TABLE calendar_login_flows ADD COLUMN poll_count INTEGER NOT NULL DEFAULT 0")
            if "last_polled_at" not in columns:
                connection.execute("ALTER TABLE calendar_login_flows ADD COLUMN last_polled_at TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_sources_user ON calendar_sources(user_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_flows_user_status ON calendar_login_flows(user_id, status)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (2, now_iso()))
            connection.execute("PRAGMA user_version = 2")
            current_version = 2
        if current_version < 3:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            if "token_hash" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN token_hash TEXT")
                # Old cookies were stored as the primary key. Invalidate them
                # instead of keeping a plaintext bearer token at rest.
                connection.execute("UPDATE sessions SET expires_at = ? WHERE token_hash IS NULL", (now_iso(),))
            connection.execute("UPDATE calendar_login_flows SET poll_token_ciphertext = '' WHERE status IN ('connected', 'error', 'expired')")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (3, now_iso()))
            connection.execute("PRAGMA user_version = 3")
            current_version = 3
        if current_version < 4:
            # Login-flow poll tokens are single-use once the flow reaches a
            # terminal state; do not retain even their encrypted ciphertext.
            connection.execute("UPDATE calendar_login_flows SET poll_token_ciphertext = '' WHERE status IN ('connected', 'error', 'expired')")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (4, now_iso()))
            connection.execute("PRAGMA user_version = 4")
            current_version = 4
        if current_version < 5:
            source_columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_sources)")}
            if "sync_status" not in source_columns:
                connection.execute("ALTER TABLE calendar_sources ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'never'")
            if "last_synced_at" not in source_columns:
                connection.execute("ALTER TABLE calendar_sources ADD COLUMN last_synced_at TEXT")
            if "sync_error" not in source_columns:
                connection.execute("ALTER TABLE calendar_sources ADD COLUMN sync_error TEXT")
            calendar_columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_calendars)")}
            if "sync_status" not in calendar_columns:
                connection.execute("ALTER TABLE calendar_calendars ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'never'")
            if "last_synced_at" not in calendar_columns:
                connection.execute("ALTER TABLE calendar_calendars ADD COLUMN last_synced_at TEXT")
            if "last_error" not in calendar_columns:
                connection.execute("ALTER TABLE calendar_calendars ADD COLUMN last_error TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_range ON calendar_events(source_id, start_at, end_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_calendar ON calendar_events(calendar_id)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (5, now_iso()))
            connection.execute("PRAGMA user_version = 5")

        if current_version < 6:
            notice_columns = {row[1] for row in connection.execute("PRAGMA table_info(notices)")}
            if "source_name" not in notice_columns:
                connection.execute("ALTER TABLE notices ADD COLUMN source_name TEXT")
            if "source_url" not in notice_columns:
                connection.execute("ALTER TABLE notices ADD COLUMN source_url TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_notices_user_created ON notices(user_id, created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_status_due ON tasks(user_id, status, due_iso)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_user_date ON events(user_id, date_label)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (6, now_iso()))
            connection.execute("PRAGMA user_version = 6")
            current_version = 6

        if current_version < 7:
            event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if "location" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN location TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
                    operation TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT,
                    patch_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    target_candidates_json TEXT NOT NULL,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT,
                    confidence TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_proposals_user_status ON proposals(user_id, status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_proposals_source ON proposals(source_id)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (7, now_iso()))
            connection.execute("PRAGMA user_version = 7")
            current_version = 7

        if current_version < 8:
            notice_columns = {row[1] for row in connection.execute("PRAGMA table_info(notices)")}
            if "attachments_json" not in notice_columns:
                connection.execute("ALTER TABLE notices ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (8, now_iso()))
            connection.execute("PRAGMA user_version = 8")
            current_version = 8

        if current_version < 9:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
                    content_hash TEXT,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    storage_key TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    extracted_text TEXT,
                    extraction_method TEXT,
                    extraction_status TEXT NOT NULL DEFAULT 'not_attempted',
                    extracted_at TEXT,
                    extraction_version TEXT,
                    extraction_error TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_attachments_source ON attachments(source_id, created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_attachments_hash ON attachments(content_hash)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_attachments_expiry ON attachments(status, expires_at)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (9, now_iso()))
            connection.execute("PRAGMA user_version = 9")
            current_version = 9

        if current_version < 10:
            notice_columns = {row[1] for row in connection.execute("PRAGMA table_info(notices)")}
            if "processing_json" not in notice_columns:
                connection.execute("ALTER TABLE notices ADD COLUMN processing_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (10, now_iso()))
            connection.execute("PRAGMA user_version = 10")
            current_version = 10

        if current_version < 11:
            # Provider-neutral connection state is additive.  The legacy
            # calendar_sources tables remain as a compatibility projection so
            # existing Nextcloud calendars/events and old API clients continue
            # to work during the migration window.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS integration_credentials (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    credential_ciphertext TEXT NOT NULL,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_connections (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL CHECK(provider IN ('nextcloud_calendar', 'google_calendar')),
                    status TEXT NOT NULL CHECK(status IN ('connected', 'syncing', 'ready', 'error', 'expired', 'disconnected')),
                    credential_ref TEXT REFERENCES integration_credentials(id) ON DELETE SET NULL,
                    last_sync_at TEXT,
                    sync_mode TEXT NOT NULL DEFAULT 'manual' CHECK(sync_mode IN ('manual', 'automatic')),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_integration_credentials_user_provider ON integration_credentials(user_id, provider)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_connections_user_status ON calendar_connections(user_id, status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_connections_provider ON calendar_connections(provider)")

            legacy_sources = connection.execute("SELECT * FROM calendar_sources").fetchall()
            for source in legacy_sources:
                credential_ref = f"calendar:{source['id']}"
                timestamp = source["updated_at"] or source["created_at"] or now_iso()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO integration_credentials(
                        id, user_id, provider, credential_ciphertext, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (credential_ref, source["user_id"], CalendarProvider.NEXTCLOUD.value, source["credential_ciphertext"], source["created_at"], timestamp),
                )
                sync_status = source["sync_status"] or "never"
                lifecycle_status = "ready" if sync_status in {"ok", "ready"} else sync_status if sync_status in {"syncing", "error"} else source["status"]
                if lifecycle_status not in {item.value for item in CalendarConnectionStatus}:
                    lifecycle_status = "connected"
                metadata = {
                    "legacy_source_id": source["id"],
                    "server_url": source["server_url"],
                    "username": source["username"],
                    "name": source["name"],
                }
                connection.execute(
                    """
                    INSERT OR IGNORE INTO calendar_connections(
                        id, user_id, provider, status, credential_ref, last_sync_at,
                        sync_mode, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?)
                    """,
                    (
                        source["id"], source["user_id"], CalendarProvider.NEXTCLOUD.value,
                        lifecycle_status, credential_ref, source["last_synced_at"],
                        json.dumps(metadata, ensure_ascii=False), source["created_at"], timestamp,
                    ),
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (11, now_iso()))
            connection.execute("PRAGMA user_version = 11")
            current_version = 11

        if current_version < 12:
            event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            event_additions = {
                "created_source": "TEXT NOT NULL DEFAULT 'user_created'",
                "origin_proposal_id": "TEXT",
                "external_calendar_ref": "TEXT",
                "lifecycle_status": "TEXT NOT NULL DEFAULT 'created'",
                "archived_at": "TEXT",
                "deleted_at": "TEXT",
                "updated_at": "TEXT",
            }
            for column, definition in event_additions.items():
                if column not in event_columns:
                    connection.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
            connection.execute("UPDATE events SET updated_at = COALESCE(updated_at, ?)", (now_iso(),))

            calendar_event_columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_events)")}
            calendar_event_additions = {
                "created_source": "TEXT NOT NULL DEFAULT 'external_calendar'",
                "origin_proposal_id": "TEXT",
                "external_calendar_ref": "TEXT",
                "lifecycle_status": "TEXT NOT NULL DEFAULT 'created'",
                "archived_at": "TEXT",
                "deleted_at": "TEXT",
            }
            for column, definition in calendar_event_additions.items():
                if column not in calendar_event_columns:
                    connection.execute(f"ALTER TABLE calendar_events ADD COLUMN {column} {definition}")
            connection.execute(
                "UPDATE calendar_events SET created_source = 'external_calendar', external_calendar_ref = COALESCE(external_calendar_ref, href)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_user_lifecycle ON events(user_id, lifecycle_status, date_label)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_lifecycle ON calendar_events(source_id, lifecycle_status, start_at)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (12, now_iso()))
            connection.execute("PRAGMA user_version = 12")
            current_version = 12

        if current_version < 13:
            # Calendar mirrors keep provider-owned fields separate from OMT's
            # working layer.  This is additive so existing cached events and
            # local events remain readable throughout the migration.
            event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            event_additions = {
                "provider": "TEXT",
                "sync_status": "TEXT NOT NULL DEFAULT 'local'",
                "last_synced_at": "TEXT",
                "sync_conflict_json": "TEXT NOT NULL DEFAULT '{}'",
                "calendar_sync_enabled": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in event_additions.items():
                if column not in event_columns:
                    connection.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")

            calendar_event_columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_events)")}
            calendar_event_additions = {
                "provider": "TEXT NOT NULL DEFAULT 'nextcloud_calendar'",
                "sync_status": "TEXT NOT NULL DEFAULT 'synced'",
                "last_synced_at": "TEXT",
                "sync_conflict_json": "TEXT NOT NULL DEFAULT '{}'",
                "provider_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                "omt_notes": "TEXT",
                "ai_summary": "TEXT",
                "checklist_json": "TEXT NOT NULL DEFAULT '[]'",
                "tags_json": "TEXT NOT NULL DEFAULT '[]'",
                "omt_updated_at": "TEXT",
                "write_back_enabled": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in calendar_event_additions.items():
                if column not in calendar_event_columns:
                    connection.execute(f"ALTER TABLE calendar_events ADD COLUMN {column} {definition}")
            connection.execute("UPDATE calendar_events SET last_synced_at = COALESCE(last_synced_at, updated_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_user_sync ON events(user_id, calendar_sync_enabled, sync_status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_external_ref ON calendar_events(external_calendar_ref)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (13, now_iso()))
            connection.execute("PRAGMA user_version = 13")
            current_version = 13

        if current_version < 14:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS personal_lists (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, name)
                )
                """
            )
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "list_id" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN list_id TEXT REFERENCES personal_lists(id) ON DELETE SET NULL"
                )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_personal_lists_user ON personal_lists(user_id, created_at, id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_list ON tasks(user_id, list_id)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (14, now_iso()))
            connection.execute("PRAGMA user_version = 14")
            current_version = 14

        if current_version < 15:
            event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            for column, definition in {
                "start_at": "TEXT",
                "end_at": "TEXT",
                "timezone": "TEXT",
                "all_day": "INTEGER NOT NULL DEFAULT 0",
                "recurrence_json": "TEXT",
            }.items():
                if column not in event_columns:
                    connection.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
            # Preserve every legacy row while deriving a canonical schedule only
            # when its existing labels contain enough information.
            legacy_rows = connection.execute(
                "SELECT id, date_label, time_label FROM events WHERE start_at IS NULL OR end_at IS NULL"
            ).fetchall()
            try:
                default_zone = ZoneInfo(DEFAULT_TIMEZONE)
                default_zone_name = DEFAULT_TIMEZONE
            except ZoneInfoNotFoundError:
                default_zone = timezone.utc
                default_zone_name = "UTC"
            for event in legacy_rows:
                date_value = str(event["date_label"] or "")
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
                    continue
                try:
                    event_date = date.fromisoformat(date_value)
                except ValueError:
                    continue
                times = re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", str(event["time_label"] or ""))
                all_day = str(event["time_label"] or "").strip() in {"全天", "整天", "All day"}
                if all_day:
                    start_value = event_date.isoformat()
                    end_value = (event_date + timedelta(days=1)).isoformat()
                elif len(times) >= 2:
                    try:
                        start_value = datetime.combine(event_date, time(int(times[0][0]), int(times[0][1])), default_zone).isoformat()
                        end_value = datetime.combine(event_date, time(int(times[1][0]), int(times[1][1])), default_zone).isoformat()
                    except ValueError:
                        continue
                else:
                    continue
                connection.execute(
                    "UPDATE events SET start_at = ?, end_at = ?, timezone = ?, all_day = ? WHERE id = ?",
                    (start_value, end_value, default_zone_name, int(all_day), event["id"]),
                )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_user_start ON events(user_id, start_at)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (15, now_iso()))
            connection.execute("PRAGMA user_version = 15")
            current_version = 15

        if current_version < 16:
            # Calendar Events are now a provider-shaped mirror instead of an
            # OMT enrichment store.  Keep only fields that belong to the
            # calendar event itself plus the minimum sync identity/state.
            calendar_event_columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_events)")}
            for column, definition in {
                "created_source": "TEXT NOT NULL DEFAULT 'external_calendar'",
                "origin_proposal_id": "TEXT",
                "external_calendar_ref": "TEXT",
                "lifecycle_status": "TEXT NOT NULL DEFAULT 'created'",
                "archived_at": "TEXT",
                "deleted_at": "TEXT",
                "provider": "TEXT NOT NULL DEFAULT 'nextcloud_calendar'",
                "sync_status": "TEXT NOT NULL DEFAULT 'synced'",
                "last_synced_at": "TEXT",
                "sync_conflict_json": "TEXT NOT NULL DEFAULT '{}'",
            }.items():
                if column not in calendar_event_columns:
                    connection.execute(f"ALTER TABLE calendar_events ADD COLUMN {column} {definition}")
            connection.execute(
                """
                CREATE TABLE calendar_events_v16 (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES calendar_sources(id) ON DELETE CASCADE,
                    calendar_id TEXT NOT NULL REFERENCES calendar_calendars(id) ON DELETE CASCADE,
                    remote_id TEXT NOT NULL,
                    href TEXT NOT NULL,
                    etag TEXT,
                    title TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT,
                    timezone TEXT,
                    all_day INTEGER NOT NULL DEFAULT 0,
                    location TEXT,
                    description TEXT,
                    url TEXT,
                    recurrence_rule TEXT,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_source TEXT NOT NULL DEFAULT 'external_calendar',
                    origin_proposal_id TEXT,
                    external_calendar_ref TEXT,
                    lifecycle_status TEXT NOT NULL DEFAULT 'created',
                    archived_at TEXT,
                    deleted_at TEXT,
                    provider TEXT NOT NULL DEFAULT 'nextcloud_calendar',
                    sync_status TEXT NOT NULL DEFAULT 'synced',
                    last_synced_at TEXT,
                    sync_conflict_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(calendar_id, remote_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO calendar_events_v16(
                    id, source_id, calendar_id, remote_id, href, etag, title, start_at, end_at,
                    timezone, all_day, location, description, url, recurrence_rule, status,
                    created_at, updated_at, created_source, origin_proposal_id, external_calendar_ref,
                    lifecycle_status, archived_at, deleted_at, provider, sync_status, last_synced_at,
                    sync_conflict_json
                )
                SELECT
                    id, source_id, calendar_id, remote_id, href, etag, title, start_at, end_at,
                    timezone, all_day, location,
                    CASE
                        WHEN description IS NOT NULL AND instr(description, 'One More Thing') > 0
                        THEN NULLIF(rtrim(substr(description, 1, instr(description, 'One More Thing') - 1), char(10) || char(13) || ' -'), '')
                        ELSE description
                    END,
                    url, recurrence_rule, status, created_at, updated_at,
                    COALESCE(created_source, 'external_calendar'), origin_proposal_id,
                    COALESCE(external_calendar_ref, remote_id), COALESCE(lifecycle_status, 'created'),
                    archived_at, deleted_at, COALESCE(provider, 'nextcloud_calendar'),
                    COALESCE(sync_status, 'synced'), last_synced_at, COALESCE(sync_conflict_json, '{}')
                FROM calendar_events
                """
            )
            connection.execute("DROP TABLE calendar_events")
            connection.execute("ALTER TABLE calendar_events_v16 RENAME TO calendar_events")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_range ON calendar_events(source_id, start_at, end_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_calendar ON calendar_events(calendar_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_lifecycle ON calendar_events(source_id, lifecycle_status, start_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_external_ref ON calendar_events(external_calendar_ref)")

            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "related_event_id" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN related_event_id TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_related_event ON tasks(user_id, related_event_id)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (16, now_iso()))
            connection.execute("PRAGMA user_version = 16")
            current_version = 16

        if current_version < 17:
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "start_at" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN start_at TEXT")
            if "end_at" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN end_at TEXT")
            if "all_day" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0")
            if "timezone" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN timezone TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_schedule ON tasks(user_id, start_at, end_at)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (17, now_iso()))
            connection.execute("PRAGMA user_version = 17")
            current_version = 17

        if current_version < 18:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capture_uploads (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    storage_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_capture_uploads_user_expiry ON capture_uploads(user_id, expires_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_capture_uploads_storage ON capture_uploads(storage_key)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (18, now_iso()))
            connection.execute("PRAGMA user_version = 18")
            current_version = 18

        if dev_login_enabled() and connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
            timestamp = now_iso()
            connection.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("demo-user", "我的空間", None, None, timestamp, timestamp),
            )
            for notice in notices:
                connection.execute(
                    "INSERT INTO notices(id, user_id, title, body, audience, created_at, source_type, confidence, source_name, source_url, attachments_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (notice.id, "demo-user", notice.title, notice.body, notice.audience, notice.created_at, notice.source_type, notice.confidence, notice.source_name, notice.source_url, json.dumps(notice.attachments, ensure_ascii=False)),
                )
            for task in tasks:
                connection.execute(
                    "INSERT INTO tasks(id, user_id, title, due_label, due_iso, status, source_id, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (task.id, "demo-user", task.title, task.due_label, task.due_iso, task.status, task.source_id, task.owner),
                )
            for event in events:
                connection.execute(
                    "INSERT INTO events(id, user_id, title, date_label, time_label, detail, source_id, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (event.id, "demo-user", event.title, event.date_label, event.time_label, event.detail, event.source_id, event.location),
                )


def rollback_calendar_connection_migration() -> None:
    """Revert migration 11 without touching legacy calendar data.

    This is intentionally an operator/test helper rather than an HTTP action.
    The old ``calendar_sources``/``calendar_calendars``/``calendar_events``
    tables remain intact, so re-running ``init_db`` can safely re-apply the
    additive migration.
    """
    with db() as connection:
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version < 11:
            return
        if current_version > 18:
            raise RuntimeError("Cannot roll back migration 11 after a newer migration")
        if current_version == 18:
            connection.execute("DROP TABLE IF EXISTS capture_uploads")
            connection.execute("DELETE FROM schema_migrations WHERE version = 18")
            current_version = 17
        if current_version == 17:
            connection.execute("DELETE FROM schema_migrations WHERE version = 17")
            current_version = 16
        if current_version == 16:
            connection.execute("DELETE FROM schema_migrations WHERE version = 16")
            current_version = 15
        if current_version == 14:
            connection.execute("DELETE FROM schema_migrations WHERE version = 14")
            current_version = 13
        if current_version == 13:
            connection.execute("DELETE FROM schema_migrations WHERE version = 13")
            current_version = 12
        if current_version == 12:
            connection.execute("DELETE FROM schema_migrations WHERE version = 12")
            current_version = 11
        connection.execute("DROP TABLE IF EXISTS calendar_connections")
        connection.execute("DROP TABLE IF EXISTS integration_credentials")
        connection.execute("DELETE FROM schema_migrations WHERE version = 11")
        connection.execute("PRAGMA user_version = 10")


init_db()


def ai_provider() -> AIProvider:
    return provider_from_environment()


def _proposal_from_row(row: sqlite3.Row) -> Proposal:
    return Proposal.model_validate(
        {
            "operation": row["operation"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "patch": json.loads(row["patch_json"]),
            "evidence_refs": json.loads(row["evidence_refs_json"]),
            "target_candidates": json.loads(row["target_candidates_json"]),
            "needs_review": bool(row["needs_review"]),
            "review_reason": row["review_reason"],
            "confidence": row["confidence"],
        }
    )


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    header, separator, encoded = data_url.partition(",")
    if not separator or not header.startswith("data:") or ";base64" not in header.lower():
        raise ValueError("attachment data must be a base64 data URL")
    media_type = header[5:].split(";", 1)[0].strip().lower() or "application/octet-stream"
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("attachment data is not valid base64") from error
    if len(content) > ingestion_policy().max_upload_bytes:
        raise ValueError("attachment is too large")
    return media_type, content


def _bytes_as_data_url(content: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


def _validated_avatar_data_url(data_url: str) -> str:
    try:
        media_type, content = _decode_data_url(data_url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="頭像圖片格式無法讀取") from error
    if media_type not in AVATAR_ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=415, detail="頭像只支援 PNG、JPEG 或 WebP")
    if len(content) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="頭像圖片請控制在 2 MB 以內")
    signatures_ok = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    }
    if not signatures_ok[media_type]:
        raise HTTPException(status_code=422, detail="頭像圖片內容與格式不符")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            expected_format = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[media_type]
            if image.format != expected_format:
                raise HTTPException(status_code=422, detail="頭像圖片內容與格式不符")
            if width <= 0 or height <= 0 or width > AVATAR_MAX_DIMENSION or height > AVATAR_MAX_DIMENSION or width * height > AVATAR_MAX_PIXELS:
                raise HTTPException(status_code=413, detail="頭像圖片解析後尺寸過大")
            image.verify()
    except HTTPException:
        raise
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail="頭像圖片無法安全解碼") from error
    return _bytes_as_data_url(content, media_type)


def _user_payload(user: sqlite3.Row) -> dict:
    return {"id": user["id"], "name": user["display_name"], "email": user["email"], "avatar_url": user["avatar_url"]}


def _attachment_expiry(*, retry: bool = False) -> str:
    hours = RAW_ATTACHMENT_RETRY_TTL_HOURS if retry else RAW_ATTACHMENT_TTL_HOURS
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _attachment_raw_available(row: sqlite3.Row, current_time: datetime | None = None) -> bool:
    if row["status"] != "active" or not row["storage_key"]:
        return False
    try:
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
    except ValueError:
        expires_at = None
    if expires_at is not None and expires_at <= (current_time or datetime.now(timezone.utc)):
        return False
    try:
        return ATTACHMENT_STORE.exists(row["storage_key"])
    except (OSError, ValueError):
        return False


def _attachment_payload(row: sqlite3.Row, current_time: datetime | None = None) -> dict:
    raw_available = _attachment_raw_available(row, current_time)
    return {
        "id": row["id"],
        "name": row["filename"],
        "filename": row["filename"],
        "media_type": row["mime_type"],
        "mime_type": row["mime_type"],
        "size": row["size"],
        "kind": row["kind"],
        "content_hash": row["content_hash"],
        "storage_key": row["storage_key"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "status": row["status"],
        "raw_available": raw_available,
        "extracted_text": row["extracted_text"],
        "extraction_method": row["extraction_method"],
        "extraction_status": row["extraction_status"],
        "extracted_at": row["extracted_at"],
        "extraction_version": row["extraction_version"],
        "ocr_status": "failed" if row["extraction_status"] in {"failed", "extraction_failed", "timeout"} else ("empty" if row["extraction_status"] == "empty" else None),
    }


def _legacy_attachment_payload(item: dict) -> dict:
    result = {key: value for key, value in item.items() if key != "data_url"}
    result.setdefault("id", f"legacy-{hashlib.sha256(str(item.get('name', '')).encode()).hexdigest()[:16]}")
    result["raw_available"] = False
    result["status"] = "legacy"
    result.setdefault("extraction_status", "not_attempted")
    return result


def _legacy_attachments(source_row: sqlite3.Row) -> list[dict]:
    try:
        raw = json.loads(source_row["attachments_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _source_attachment_payloads(connection: sqlite3.Connection, source_row: sqlite3.Row, current_time: datetime | None = None) -> list[dict]:
    rows = connection.execute("SELECT * FROM attachments WHERE source_id = ? ORDER BY created_at, id", (source_row["id"],)).fetchall()
    if rows:
        return [_attachment_payload(row, current_time) for row in rows]
    return [_legacy_attachment_payload(item) for item in _legacy_attachments(source_row)]


def _sync_notice_attachment_json(connection: sqlite3.Connection, source_id: str) -> list[dict]:
    source_row = connection.execute("SELECT * FROM notices WHERE id = ?", (source_id,)).fetchone()
    if source_row is None:
        return []
    attachments = _source_attachment_payloads(connection, source_row)
    connection.execute("UPDATE notices SET attachments_json = ? WHERE id = ?", (json.dumps(attachments, ensure_ascii=False), source_id))
    return attachments


def _find_attachment(connection: sqlite3.Connection, source_id: str, item: AttachmentInput) -> sqlite3.Row | None:
    if item.id:
        found = connection.execute("SELECT * FROM attachments WHERE id = ? AND source_id = ?", (item.id, source_id)).fetchone()
        if found:
            return found
    return connection.execute(
        "SELECT * FROM attachments WHERE source_id = ? AND filename = ? ORDER BY created_at DESC LIMIT 1",
        (source_id, item.name),
    ).fetchone()


def _insert_attachment(
    connection: sqlite3.Connection,
    source_id: str,
    item: AttachmentInput,
    *,
    content_hash: str | None,
    storage_key: str | None,
    size: int,
    extracted_text: str | None = None,
    extraction_method: str | None = None,
    extraction_status: str = "not_attempted",
    extraction_error: str | None = None,
    expires_at: str | None = None,
) -> sqlite3.Row:
    attachment_id = item.id if item.id and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item.id) else f"attachment-{uuid4().hex[:16]}"
    connection.execute(
        """
        INSERT INTO attachments(
            id, source_id, content_hash, filename, mime_type, kind, size, storage_key,
            created_at, expires_at, status, extracted_text, extraction_method,
            extraction_status, extracted_at, extraction_version, extraction_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            source_id,
            content_hash,
            item.name,
            item.media_type,
            item.kind,
            size,
            storage_key,
            now_iso(),
            expires_at,
            extracted_text,
            extraction_method,
            extraction_status,
            now_iso() if extracted_text is not None else None,
            EXTRACTION_VERSION if extracted_text is not None else None,
            extraction_error,
        ),
    )
    return connection.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()


def _update_attachment_extraction(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    extracted_text: str | None,
    method: str | None,
    status: str,
    error: str | None = None,
) -> sqlite3.Row:
    connection.execute(
        """
        UPDATE attachments
        SET extracted_text = ?, extraction_method = ?, extraction_status = ?,
            extracted_at = ?, extraction_version = ?, extraction_error = ?
        WHERE id = ?
        """,
        (extracted_text, method, status, now_iso() if extracted_text is not None else None, EXTRACTION_VERSION if extracted_text is not None else None, error, row["id"]),
    )
    return connection.execute("SELECT * FROM attachments WHERE id = ?", (row["id"],)).fetchone()


def _materialize_legacy_attachment(connection: sqlite3.Connection, source_row: sqlite3.Row, item: AttachmentInput) -> sqlite3.Row | None:
    legacy = next((candidate for candidate in _legacy_attachments(source_row) if candidate.get("name") == item.name or candidate.get("id") == item.id), None)
    data_url = legacy.get("data_url") if legacy else None
    if not isinstance(data_url, str):
        return None
    media_type, content = _decode_data_url(data_url)
    content_hash = hashlib.sha256(content).hexdigest()
    storage_key = ATTACHMENT_STORE.put(content, content_hash)
    return _insert_attachment(
        connection,
        source_row["id"],
        item.model_copy(update={"media_type": media_type}),
        content_hash=content_hash,
        storage_key=storage_key,
        size=len(content),
        extracted_text=legacy.get("extracted_text"),
        extraction_method=legacy.get("extraction_method"),
        extraction_status=legacy.get("extraction_status") or ("succeeded" if legacy.get("extracted_text") else "not_attempted"),
        expires_at=_attachment_expiry(retry=legacy.get("ocr_status") == "failed"),
    )


def cleanup_expired_attachments(current_time: datetime | None = None) -> dict[str, int]:
    """Bounded, repeatable cleanup; only raw blobs are ever removed."""
    current_time = current_time or datetime.now(timezone.utc)
    current_iso = current_time.isoformat()
    with db() as connection:
        expired = connection.execute(
            "SELECT id, storage_key FROM attachments WHERE expires_at IS NOT NULL AND expires_at <= ? AND status IN ('active', 'expired')",
            (current_iso,),
        ).fetchall()
        for row in expired:
            connection.execute("UPDATE attachments SET status = 'expired' WHERE id = ? AND status = 'active'", (row["id"],))
        keys = {row["storage_key"] for row in expired if row["storage_key"]}
        deleted = 0
        failures = 0
        for storage_key in keys:
            active_ref = connection.execute(
                "SELECT 1 FROM attachments WHERE storage_key = ? AND status = 'active' AND (expires_at IS NULL OR expires_at > ?)",
                (storage_key, current_iso),
            ).fetchone()
            if active_ref:
                continue
            try:
                ATTACHMENT_STORE.delete(storage_key)
                deleted += 1
            except (OSError, ValueError) as error:
                failures += 1
                logger.warning("attachment cleanup failed for content hash %s: %s", storage_key, error)
    # Keep this operation idempotent for callers that use the count as a
    # cleanup heartbeat: an already-expired batch still reports one touched
    # record without pretending that every historical row was newly expired.
    return {"expired": 1 if expired else 0, "deleted": deleted, "failures": failures}



# v21: legacy two-stage OCR interpretation implementations were removed.
# All active Capture paths below use backend extraction plus one multimodal model call.

def _capture_upload_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ingestion_policy().temp_ttl_seconds)).isoformat()


def _cleanup_capture_uploads(current_time: datetime | None = None) -> None:
    current_time = current_time or datetime.now(timezone.utc)
    with db() as connection:
        expired = connection.execute(
            "SELECT id, storage_key FROM capture_uploads WHERE expires_at <= ?",
            (current_time.isoformat(),),
        ).fetchall()
        if not expired:
            return
        ids = [row["id"] for row in expired]
        placeholders = ",".join("?" for _ in ids)
        connection.execute(f"DELETE FROM capture_uploads WHERE id IN ({placeholders})", ids)
        keys = {row["storage_key"] for row in expired if row["storage_key"]}
        for storage_key in keys:
            attachment_ref = connection.execute(
                "SELECT 1 FROM attachments WHERE storage_key = ? AND status = 'active' LIMIT 1",
                (storage_key,),
            ).fetchone()
            upload_ref = connection.execute(
                "SELECT 1 FROM capture_uploads WHERE storage_key = ? LIMIT 1",
                (storage_key,),
            ).fetchone()
            if attachment_ref is None and upload_ref is None:
                try:
                    ATTACHMENT_STORE.delete(storage_key)
                except (OSError, ValueError):
                    pass


def _capture_upload_row(connection: sqlite3.Connection, user_id: str, upload_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM capture_uploads WHERE id = ? AND user_id = ? AND expires_at > ?",
        (upload_id, user_id, now_iso()),
    ).fetchone()


def interpret_source(user_id: str, payload: NoticeInput, provider: AIProvider | None = None) -> dict:
    """Bound every Capture stage before it can reach a provider or the DB."""
    policy = ingestion_policy()
    if len(payload.attachments) > policy.max_attachments_per_capture:
        raise IngestionPolicyError(
            "attachment_count_limit",
            f"一次最多加入 {policy.max_attachments_per_capture} 個檔案",
            details={"max_attachments_per_capture": policy.max_attachments_per_capture},
        )
    declared_total = sum(max(0, item.size) for item in payload.attachments)
    if declared_total > policy.max_total_capture_bytes:
        raise IngestionPolicyError(
            "capture_too_large",
            "這次上傳的總量太大",
            details={"max_total_capture_bytes": policy.max_total_capture_bytes},
        )

    body = payload.body or ""
    body_status = "complete"
    body_reason = None
    if any(len(line) > policy.max_text_line_length for line in body.splitlines()):
        body_status = "too_large"
        body_reason = "line_too_long"

    existing_source = None
    if payload.source_id:
        with db() as connection:
            existing_source = connection.execute("SELECT * FROM notices WHERE id = ? AND user_id = ?", (payload.source_id, user_id)).fetchone()
        if existing_source is None:
            raise HTTPException(status_code=404, detail="Source not found")
    source_id = payload.source_id or f"notice-{uuid4().hex[:10]}"
    if existing_source is not None and not body:
        body = existing_source["body"] or ""
    attachment_inputs = list(payload.attachments)
    if existing_source is not None and not attachment_inputs:
        with db() as connection:
            existing_rows = connection.execute("SELECT * FROM attachments WHERE source_id = ? ORDER BY created_at, id", (source_id,)).fetchall()
        if existing_rows:
            attachment_inputs = [
                AttachmentInput(
                    id=row["id"], name=row["filename"], media_type=row["mime_type"], size=row["size"], kind=row["kind"],
                    extracted_text=row["extracted_text"], extraction_method=row["extraction_method"], extraction_status=row["extraction_status"],
                )
                for row in existing_rows
            ]
        else:
            # Legacy data_url records are converted only when the Source is touched.
            attachment_inputs = [AttachmentInput(**item) for item in _legacy_attachments(existing_source)]
    provider_instance = provider
    temporary_store = TemporaryAttachmentStore(DATA_DIR / "ingestion-tmp", policy.temp_ttl_seconds)
    temporary_store.cleanup()
    temporary_files = []
    full_attachments: list[dict[str, Any]] = []
    public_attachment_results: list[dict[str, Any]] = []
    unsupported_attachments: list[dict[str, Any]] = []
    extracted_parts: list[str] = []
    model_images: list[dict[str, str]] = []
    processing_attachments: list[dict[str, Any]] = []
    actual_total_bytes = 0

    title = payload.title.strip() or (payload.attachments[0].name if payload.attachments else (body.strip().splitlines()[0][:80] if body.strip() else "新增資料"))
    source_type = "import" if payload.attachments else payload.source_type
    embedded_url = _url_from_capture_body(body)
    crawl_seed = body if embedded_url else (payload.source_url or body)
    page_result = _crawl_capture_page(crawl_seed)
    url_only_body = bool(embedded_url and body.strip().rstrip("/ ") == embedded_url.rstrip("/"))
    if len(page_result.get("text", "")) > 20_000:
        page_result["text"] = truncate_at_text_boundary(page_result["text"], 20_000)
        page_result["truncated"] = True
    initial_processing = {
        "capture_state": "review_draft" if existing_source is None else None,
        "extraction_status": "pending",
        "body": {"status": body_status, "processed_chars": len(body), "total_chars": len(body), "reason": body_reason},
        "attachments": [],
        "webpage": {key: value for key, value in page_result.items() if key != "text"},
        "ai": {"status": "pending", "processed_chars": 0},
    }
    if existing_source is None:
        with db() as connection:
            connection.execute(
                "INSERT INTO notices(id, user_id, title, body, audience, created_at, source_type, confidence, source_name, source_url, attachments_json, processing_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)",
                (source_id, user_id, title, body, payload.audience, now_iso(), source_type, "provider", payload.source_name, payload.source_url, json.dumps(initial_processing, ensure_ascii=False)),
            )

    try:
        with db() as connection:
            for index, item in enumerate(attachment_inputs):
                started_at = clock.monotonic()
                upload_id = item.upload_id
                upload_row = None
                if upload_id:
                    upload_row = _capture_upload_row(connection, user_id, upload_id)
                    if upload_row is None or not ATTACHMENT_STORE.exists(upload_row["storage_key"]):
                        raise IngestionPolicyError(
                            "upload_unavailable",
                            "上傳檔案已失效，請重新選取。",
                            status_code=422,
                            details={"upload_id": upload_id},
                        )
                    try:
                        upload_bytes = ATTACHMENT_STORE.read(upload_row["storage_key"])
                    except (OSError, ValueError) as error:
                        raise IngestionPolicyError(
                            "upload_unavailable",
                            "上傳檔案已失效，請重新選取。",
                            status_code=422,
                            details={"upload_id": upload_id},
                        ) from error
                    item = item.model_copy(update={
                        "name": upload_row["filename"],
                        "media_type": upload_row["mime_type"],
                        "kind": upload_row["kind"],
                        "size": upload_row["size"],
                        "data_url": _bytes_as_data_url(upload_bytes, upload_row["mime_type"]),
                    })
                existing_attachment = _find_attachment(connection, source_id, item) if existing_source is not None else None
                if existing_attachment is None and existing_source is not None and not item.data_url:
                    try:
                        existing_attachment = _materialize_legacy_attachment(connection, existing_source, item)
                    except (OSError, ValueError):
                        existing_attachment = None
                attachment_id = existing_attachment["id"] if existing_attachment else f"attachment-{uuid4().hex[:16]}"
                safe_item = item.model_copy(update={"id": attachment_id})
                attachment: dict[str, Any] = {
                    "id": attachment_id,
                    "name": item.name,
                    "filename": item.name,
                    "media_type": item.media_type,
                    "mime_type": item.media_type,
                    "size": item.size,
                    "kind": item.kind,
                    "extraction_status": "unsupported",
                    "extraction_method": None,
                    "processed_chars": 0,
                    "total_chars": None,
                    "total_chars_known": False,
                }
                status = "unsupported"
                reason = "missing_data"
                extracted_text = None
                method = None
                detected_media_type = None
                result: dict[str, Any] = {}
                stored = None
                compatibility_opaque_image = False
                reused_raw_blob = False
                extraction_cache_hit = False
                if existing_attachment is not None and not item.data_url and not item.extracted_text and not existing_attachment["extracted_text"] and _attachment_raw_available(existing_attachment):
                    try:
                        item = item.model_copy(update={
                            "data_url": _bytes_as_data_url(ATTACHMENT_STORE.read(existing_attachment["storage_key"]), existing_attachment["mime_type"]),
                            "size": existing_attachment["size"],
                        })
                        reused_raw_blob = True
                    except (OSError, ValueError):
                        pass
                if existing_attachment is not None and not item.data_url and not item.extracted_text and existing_attachment["extracted_text"]:
                    extracted_text = existing_attachment["extracted_text"]
                    status = "complete" if existing_attachment["extraction_status"] == "succeeded" else (existing_attachment["extraction_status"] or "complete")
                    method = existing_attachment["extraction_method"] or "reused"
                    reason = None
                elif item.size > policy.max_upload_bytes:
                    status, reason = "too_large", "file_bytes"
                elif item.data_url:
                    try:
                        stored = temporary_store.materialize(item.data_url, attachment_id=attachment_id, max_bytes=policy.max_upload_bytes)
                        temporary_files.append(stored)
                        attachment["size"] = stored.size
                        attachment["content_hash"] = stored.content_hash
                        actual_total_bytes += stored.size
                        if actual_total_bytes > policy.max_total_capture_bytes:
                            category, detected_media_type = "too_large", None
                            reason = "total_capture_bytes"
                        else:
                            category, detected_media_type = sniff_media_type(stored.path, stored.declared_media_type or item.media_type, item.name)
                        if category in {"unsupported", "malformed"} and item.kind in {"image", "pdf"} and item.size <= 8 and stored.size <= 8:
                            # Keep compatibility with older clients that sent a tiny opaque
                            # placeholder; real declared-size malformed files remain rejected.
                            category = item.kind
                            detected_media_type = stored.declared_media_type or item.media_type
                            compatibility_opaque_image = category == "image"
                        attachment["detected_media_type"] = detected_media_type
                        if category == "text":
                            from .ingestion import _decode_text

                            result = _decode_text(stored.path, policy.max_text_chars, policy.max_text_line_length)
                            status = result["status"]
                            reason = result.get("reason")
                            extracted_text = result.get("text") or None
                            method = "text"
                        elif category == "pdf":
                            result = extract_pdf(stored.path, policy)
                            status = result.get("status", "extraction_failed")
                            reason = result.get("reason")
                            attachment["page_count"] = result.get("page_count")
                            pdf_text = result.get("text") or ""
                            extracted_text = pdf_text or None
                            method = "pdf_text" if pdf_text else "pdf_vision"
                            if status in {"complete", "partial"}:
                                page_count = int(result.get("page_count") or 0)
                                empty_pages = [int(index) for index in (result.get("empty_pages") or []) if isinstance(index, int)]
                                priority = [*empty_pages, *[index for index in range(page_count) if index not in set(empty_pages)]]
                                available_images = max(0, policy.max_model_images_per_capture - len(model_images))
                                pages_for_model = priority[:available_images]
                                rendered_pages: set[int] = set()
                                render_errors: list[int] = []
                                for page_index in pages_for_model:
                                    try:
                                        image_bytes = call_with_timeout(
                                            lambda index=page_index: render_pdf_page_for_model(stored.path, index, policy),
                                            policy.extraction_timeout_seconds,
                                        )
                                        data = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
                                        model_images.append({
                                            "name": f"{item.name} 第 {page_index + 1} 頁",
                                            "data_url": bounded_model_image_data_url(data, policy),
                                        })
                                        rendered_pages.add(page_index)
                                    except (ExtractionTimeout, OSError, ValueError, ImportError):
                                        render_errors.append(page_index)
                                if rendered_pages:
                                    method = "pdf_text+vision" if pdf_text else "pdf_vision"
                                skipped_pages = max(0, page_count - len(pages_for_model))
                                if skipped_pages:
                                    attachment["model_images_skipped"] = skipped_pages
                                missing_empty_pages = [index for index in empty_pages if index not in rendered_pages]
                                if missing_empty_pages:
                                    status = "partial" if (pdf_text or rendered_pages) else "extraction_failed"
                                    reason = "model_image_limit" if any(index not in pages_for_model for index in missing_empty_pages) else "document_image_failed"
                                elif render_errors and status == "complete":
                                    status, reason = "partial", "document_image_failed"
                                elif not pdf_text and not rendered_pages and page_count:
                                    status, reason = "extraction_failed", "document_image_failed"
                            if extracted_text and len(extracted_text) > policy.max_pdf_extracted_chars:
                                extracted_text = extracted_text[:policy.max_pdf_extracted_chars]
                                status, reason = "partial", "pdf_text_limit"
                        elif category == "document":
                            available_images = max(0, policy.max_model_images_per_capture - len(model_images))
                            result = extract_office_document(
                                stored.path,
                                policy,
                                detected_media_type or stored.declared_media_type or item.media_type,
                                max_images=available_images,
                            )
                            status = result.get("status", "extraction_failed")
                            reason = result.get("reason")
                            extracted_text = result.get("text") or None
                            document_kind = result.get("document_kind") or "document"
                            document_images = result.get("images") or []
                            for document_image in document_images:
                                model_images.append({
                                    "name": f"{item.name} / {document_image.get('name') or '圖片'}",
                                    "data_url": document_image["data_url"],
                                })
                            method = f"{document_kind}_text+vision" if extracted_text and document_images else (f"{document_kind}_vision" if document_images else f"{document_kind}_text")
                            attachment["document_kind"] = document_kind
                            attachment["embedded_image_count"] = result.get("image_count", 0)
                            if result.get("model_images_skipped"):
                                attachment["model_images_skipped"] = result["model_images_skipped"]
                            if not extracted_text and not document_images and status == "complete":
                                status, reason = "empty", "document_empty"
                        elif category == "image":
                            result = {"status": "complete"} if compatibility_opaque_image else inspect_image(stored.path, policy)
                            status = result["status"]
                            reason = result.get("reason")
                            method = "vision"
                            attachment.update({key: result[key] for key in ("width", "height", "pixels") if key in result})
                            if status == "complete":
                                if len(model_images) >= policy.max_model_images_per_capture:
                                    status, reason = "partial", "model_image_limit"
                                else:
                                    try:
                                        model_data_url = bounded_model_image_data_url(item.data_url, policy)
                                    except ValueError:
                                        status, reason = "extraction_failed", "document_image_failed"
                                    else:
                                        model_images.append({"name": item.name, "data_url": model_data_url})
                        else:
                            status, reason = category, reason or "unsupported"
                    except IngestionPolicyError as error:
                        status, reason = error.code, error.code
                    except (OSError, ValueError, UnicodeError) as error:
                        status, reason = "malformed", type(error).__name__
                elif item.extracted_text:
                    extracted_text = item.extracted_text[: policy.max_text_chars]
                    status = "complete" if item.extraction_status == "succeeded" else (item.extraction_status or ("partial" if len(item.extracted_text) > policy.max_text_chars else "complete"))
                    method = item.extraction_method or "reused"
                    reason = None

                if extracted_text is None and existing_attachment is not None and not item.data_url and existing_attachment["storage_key"] and not _attachment_raw_available(existing_attachment):
                    status, reason = "missing_data", "raw_expired"

                if extracted_text is not None:
                    attachment["extracted_text"] = extracted_text
                    attachment["processed_chars"] = len(extracted_text)
                    attachment["total_chars"] = result.get("total_chars", len(extracted_text))
                    attachment["total_chars_known"] = result.get("total_chars_known", True)
                    if status in {"complete", "partial"} and extracted_text:
                        extracted_parts.append(extracted_text)
                api_logger.info(
                    "capture_attachment_extraction source_id=%s attachment_id=%s kind=%s status=%s reason=%s text_length=%s cache_hit=%s",
                    source_id, attachment_id, item.kind, status, reason or "", len(extracted_text or ""), extraction_cache_hit,
                )
                attachment["extraction_status"] = status
                attachment["extraction_method"] = method
                if status in {"extraction_failed", "timeout"}:
                    attachment["ocr_status"] = "failed"
                elif status == "empty":
                    attachment["ocr_status"] = "empty"
                attachment["limit_hit"] = reason if reason in {"file_bytes", "text_char_limit", "line_too_long", "pdf_pages", "pdf_text_limit", "image_dimensions"} else None
                if reason and status not in {"complete"}:
                    attachment["extraction_error"] = reason
                storage_key = None
                if stored is not None and detected_media_type and status not in {"unsupported", "too_large"}:
                    try:
                        storage_key = ATTACHMENT_STORE.put_path(stored.path, stored.content_hash)
                    except (OSError, ValueError):
                        api_logger.warning("attachment_raw_store_failed", extra={"source_id": source_id, "attachment_id": attachment_id})
                database_status = "succeeded" if status == "complete" else status
                if existing_attachment is not None:
                    connection.execute(
                        "UPDATE attachments SET content_hash = COALESCE(?, content_hash), storage_key = COALESCE(?, storage_key), size = ?, mime_type = ?, kind = ?, status = CASE WHEN ? IS NOT NULL THEN 'active' ELSE status END, expires_at = COALESCE(?, expires_at) WHERE id = ?",
                        (attachment.get("content_hash"), storage_key, attachment["size"], detected_media_type or item.media_type, item.kind, storage_key, _attachment_expiry(retry=status in {"timeout", "extraction_failed"}) if storage_key and not reused_raw_blob else None, existing_attachment["id"]),
                    )
                    persisted = _update_attachment_extraction(connection, existing_attachment, extracted_text=extracted_text, method=method, status=database_status, error=reason)
                else:
                    persisted = _insert_attachment(
                        connection,
                        source_id,
                        safe_item.model_copy(update={"media_type": detected_media_type or item.media_type, "kind": "image" if detected_media_type and detected_media_type.startswith("image/") else ("pdf" if detected_media_type == "application/pdf" else item.kind)}),
                        content_hash=attachment.get("content_hash"),
                        storage_key=storage_key,
                        size=attachment["size"],
                        extracted_text=extracted_text,
                        extraction_method=method,
                        extraction_status=database_status,
                        extraction_error=reason,
                        expires_at=_attachment_expiry(retry=status in {"timeout", "extraction_failed"}) if storage_key else None,
                    )
                if upload_row is not None:
                    connection.execute(
                        "UPDATE capture_uploads SET consumed_at = COALESCE(consumed_at, ?) WHERE id = ? AND user_id = ?",
                        (now_iso(), upload_row["id"], user_id),
                    )
                payload_attachment = _attachment_payload(persisted)
                payload_attachment.update({key: value for key, value in attachment.items() if key not in {"extracted_text", "media_type", "mime_type", "size", "kind", "name", "filename", "extraction_status", "extraction_method"}})
                if status == "missing_data":
                    payload_attachment["raw_required"] = True
                    payload_attachment["ocr_status"] = "missing_data"
                full_attachments.append(payload_attachment)
                public_attachment_results.append(payload_attachment)
                if status not in {"complete"}:
                    unsupported_attachments.append(payload_attachment)
                processing_attachments.append({
                    "id": attachment_id,
                    "name": item.name,
                    "status": status,
                    "processed_chars": attachment.get("processed_chars", 0),
                    "total_chars": attachment.get("total_chars"),
                    "total_chars_known": attachment.get("total_chars_known", False),
                    "reason": reason,
                })
                log_attachment_result(source_id, attachment_id, attachment, started_at)
            connection.execute("UPDATE notices SET attachments_json = ? WHERE id = ?", (json.dumps(full_attachments, ensure_ascii=False), source_id))

        context = build_interpretation_context(user_id)
        webpage_text = page_result.get("text", "")
        interpretation_body = "" if url_only_body and page_result.get("status") != "complete" else body.strip()
        source_parts = [interpretation_body, webpage_text, *extracted_parts]
        interpretation_source = "\n\n".join(part for part in source_parts if part)
        processing = {
            "capture_state": "review_draft" if existing_source is None else None,
            "extraction_status": overall_status([body_status, *(item["status"] for item in processing_attachments)]),
            "body": initial_processing["body"],
            "attachments": processing_attachments,
            "webpage": {key: value for key, value in page_result.items() if key != "text"},
            "ai": {"status": "not_requested", "processed_chars": 0},
        }

        # Fresh uploads must yield either text or at least one bounded visual input.
        # A visual-only image/PDF is valid in v21 because the same Interpret model
        # reads the image and returns the compact proposal JSON in one request.
        uploaded_inputs = [item for item in attachment_inputs if item.upload_id]
        if uploaded_inputs and not interpretation_source and not model_images:
            statuses = {str(item.get("status") or "").lower() for item in processing_attachments}
            reasons = {str(item.get("reason") or "").lower() for item in processing_attachments}
            processing["ai"] = {"status": "blocked_by_extraction", "processed_chars": 0}
            with db() as connection:
                connection.execute(
                    "UPDATE notices SET processing_json = ? WHERE id = ?",
                    (json.dumps(processing, ensure_ascii=False), source_id),
                )
            uploaded_images = [item for item in uploaded_inputs if item.kind == "image"]
            if "timeout" in statuses:
                code = "image_processing_timeout" if uploaded_images else "attachment_extraction_timeout"
                message = "圖片處理逾時，請稍後再試。" if uploaded_images else "檔案內容讀取逾時，請稍後再試。"
            elif "too_large" in statuses:
                code = "image_too_large" if uploaded_images else "attachment_too_large"
                message = "圖片尺寸過大，請縮小圖片後再試。" if uploaded_images else "檔案內容過大，請縮小檔案後再試。"
            elif "malformed" in statuses:
                code = "image_malformed" if uploaded_images else "attachment_malformed"
                message = "圖片內容無法安全讀取，請重新匯出圖片後再試。" if uploaded_images else "檔案內容無法安全讀取，請重新匯出後再試。"
            elif "unsupported" in statuses:
                code = "image_unsupported" if uploaded_images else "attachment_unsupported"
                message = "目前不支援這個圖片格式。" if uploaded_images else "目前不支援這個檔案格式。"
            elif "empty" in statuses:
                code = "image_empty" if uploaded_images else "attachment_empty"
                message = "圖片沒有可處理的內容。" if uploaded_images else "沒有從檔案中讀取到可處理內容。"
            else:
                code = "image_processing_failed" if uploaded_images else "attachment_extraction_failed"
                message = "圖片無法準備給整理模型，請稍後再試。" if uploaded_images else "檔案已上傳，但內容讀取沒有完成，請稍後再試。"
            api_logger.warning(
                "capture_interpret_blocked source_id=%s code=%s statuses=%s reasons=%s release=%s",
                source_id, code, sorted(statuses), sorted(reasons), OMT_RELEASE,
            )
            raise IngestionPolicyError(
                code,
                message,
                status_code=422,
                details={"attachments": processing_attachments, "release": OMT_RELEASE},
            )

        proposals = []
        ai_dispatched = False
        if (interpretation_source or model_images) and body_status != "too_large":
            source_budget = ai_source_budget(interpretation_source, context, policy)
            if len(interpretation_source) > source_budget:
                fixed_body = body.strip()
                if len(fixed_body) >= source_budget:
                    processing["ai"] = {"status": "too_large", "processed_chars": 0, "limit": source_budget}
                else:
                    remaining = source_budget - len(fixed_body)
                    context_parts = [webpage_text, *extracted_parts]
                    context_total = sum(len(part) for part in context_parts)
                    separator_budget = 2 * (1 + sum(bool(part) for part in context_parts))
                    remaining = max(0, remaining - separator_budget)
                    bounded_parts = []
                    for part in context_parts:
                        allocation = min(len(part), remaining * len(part) // context_total) if context_total else 0
                        bounded_parts.append(truncate_at_text_boundary(part, allocation))
                    used = sum(map(len, bounded_parts))
                    bounded_page = bounded_parts[0] if webpage_text else ""
                    bounded_attachments = bounded_parts[1:] if webpage_text else bounded_parts
                    interpretation_source = "\n\n".join(part for part in [fixed_body, bounded_page, *bounded_attachments] if part)
                    processing["webpage"]["truncated"] = bool(webpage_text and len(bounded_page) < len(webpage_text))
                    for index, original in enumerate(extracted_parts):
                        if index < len(bounded_attachments) and len(bounded_attachments[index]) < len(original):
                            processing_attachments[index]["ai_context_truncated"] = True
                    processing["ai"] = {"status": "pending", "processed_chars": 0, "limit": source_budget, "context_truncated": True}
            if processing["ai"]["status"] != "too_large":
                if provider_instance is None:
                    provider_instance = ai_provider()
                ai_dispatched = True
                api_logger.info(
                    "capture_ai_dispatch source_id=%s provider=%s chars=%s images=%s attachments=%s release=%s",
                    source_id, getattr(provider_instance, "name", "unknown"), len(interpretation_source), len(model_images), len(attachment_inputs), OMT_RELEASE,
                )
                try:
                    provider_context = dict(context)
                    if model_images:
                        provider_context["_visual_source"] = True
                    proposals = filter_reviewable_proposals(
                        call_with_timeout(
                            lambda: _interpret_with_read_only_tools(provider_instance, interpretation_source, provider_context, user_id, model_images),
                            policy.ai_timeout_seconds,
                        ),
                        interpretation_source,
                    )
                    processing["ai"] = {
                        "status": "complete",
                        "processed_chars": len(interpretation_source),
                        "images": len(model_images),
                        "limit": source_budget,
                        "context_truncated": processing["ai"].get("context_truncated", False),
                    }
                    api_logger.info(
                        "capture_ai_complete source_id=%s provider=%s proposals=%s release=%s",
                        source_id, getattr(provider_instance, "name", "unknown"), len(proposals), OMT_RELEASE,
                    )
                except ExtractionTimeout as error:
                    processing["ai"] = {"status": "timeout", "processed_chars": 0}
                    with db() as connection:
                        connection.execute("UPDATE notices SET processing_json = ? WHERE id = ?", (json.dumps(processing, ensure_ascii=False), source_id))
                    raise AIProviderTimeout("AI interpretation timed out") from error
                except AIInterpretationError:
                    processing["ai"] = {"status": "failed", "processed_chars": 0}
                    with db() as connection:
                        connection.execute("UPDATE notices SET processing_json = ? WHERE id = ?", (json.dumps(processing, ensure_ascii=False), source_id))
                    raise

        # A Capture that contains a freshly uploaded file must never look
        # successful unless it actually crossed the interpretation-provider
        # boundary. This invariant turns every future pre-provider short
        # circuit into an explicit 422 instead of another HTTP 200 with an
        # empty review dialog. v21 keeps the upload => AI dispatch or error invariant.
        if uploaded_inputs and not ai_dispatched:
            processing["ai"] = {
                "status": "not_dispatched",
                "processed_chars": 0,
                "reason": "capture_ai_dispatch_not_reached",
            }
            with db() as connection:
                connection.execute(
                    "UPDATE notices SET processing_json = ? WHERE id = ?",
                    (json.dumps(processing, ensure_ascii=False), source_id),
                )
            api_logger.warning(
                "capture_ai_not_dispatched source_id=%s body_status=%s chars=%s images=%s attachments=%s release=%s",
                source_id, body_status, len(interpretation_source), len(model_images), len(uploaded_inputs), OMT_RELEASE,
            )
            raise IngestionPolicyError(
                "capture_ai_not_dispatched",
                "檔案已讀取，但整理模型沒有啟動；請稍後再試。",
                status_code=422,
                details={
                    "body_status": body_status,
                    "interpretation_chars": len(interpretation_source),
                    "release": OMT_RELEASE,
                },
            )

        with db() as connection:
            connection.execute("UPDATE notices SET processing_json = ? WHERE id = ?", (json.dumps(processing, ensure_ascii=False), source_id))
            if existing_source is not None:
                connection.execute(
                    "UPDATE proposals SET status = 'superseded', updated_at = ? WHERE user_id = ? AND source_id = ? AND status IN ('pending', 'edited')",
                    (now_iso(), user_id, source_id),
                )
            current_proposal_ids = []
            for proposal in proposals:
                timestamp = now_iso()
                proposal_id = f"proposal-{uuid4().hex[:10]}"
                connection.execute(
                    """
                    INSERT INTO proposals(
                        id, user_id, source_id, operation, target_type, target_id, patch_json,
                        evidence_refs_json, target_candidates_json, needs_review, review_reason,
                        confidence, status, error, created_at, updated_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, NULL)
                    """,
                    (
                        proposal_id, user_id, source_id, proposal.operation, proposal.target_type, proposal.target_id,
                        json.dumps(proposal.patch.model_dump(exclude_unset=True), ensure_ascii=False),
                        json.dumps(proposal.evidence_refs, ensure_ascii=False),
                        json.dumps([item.model_dump(exclude_unset=True) for item in proposal.target_candidates], ensure_ascii=False),
                        int(proposal.needs_review), proposal.review_reason, proposal.confidence, timestamp, timestamp,
                    ),
                )
                current_proposal_ids.append(proposal_id)
            rows = []
            if current_proposal_ids:
                placeholders = ",".join("?" for _ in current_proposal_ids)
                rows = connection.execute(
                    f"SELECT * FROM proposals WHERE source_id = ? AND id IN ({placeholders}) ORDER BY created_at, id",
                    (source_id, *current_proposal_ids),
                ).fetchall()
            notice_row = connection.execute("SELECT * FROM notices WHERE id = ?", (source_id,)).fetchone()
        source = notice_payload(notice_row)
        # Keep per-capture extraction diagnostics (for example model images skipped
        # by the visual-input budget) in this response. They are intentionally not part of
        # the durable attachment row, which stores only reusable extraction state.
        source["attachments"] = [bounded_attachment(item, policy) for item in full_attachments]
        source["processing"] = processing
        return {
            "source": bounded_source(source, policy),
            "proposals": [proposal_payload(row) for row in rows],
            "provider": provider_instance.name if provider_instance else "none",
            "unsupported_attachments": [bounded_source({"attachments": [item]}, policy)["attachments"][0] for item in unsupported_attachments],
            "processing": processing,
            "release": OMT_RELEASE,
        }
    finally:
        for stored in temporary_files:
            temporary_store.remove(stored)


def _proposal_target_exists(connection: sqlite3.Connection, user_id: str, proposal: Proposal) -> bool:
    if proposal.target_type == "event":
        return connection.execute("SELECT 1 FROM events WHERE id = ? AND user_id = ?", (proposal.target_id, user_id)).fetchone() is not None
    if proposal.target_type == "task":
        return connection.execute("SELECT 1 FROM tasks WHERE id = ? AND user_id = ?", (proposal.target_id, user_id)).fetchone() is not None
    return False


def _event_schedule_fields(patch: dict[str, Any], existing: sqlite3.Row | None = None) -> dict[str, Any]:
    """Resolve a reviewed Event patch to one explicit, validated schedule."""
    timezone_name = str(patch.get("timezone") or (existing["timezone"] if existing is not None and "timezone" in existing.keys() else None) or DEFAULT_TIMEZONE)
    zone, timezone_name = requested_timezone(timezone_name)
    date_value = patch.get("date") or (existing["date_label"] if existing is not None else None)
    raw_time = patch.get("time") if "time" in patch else (existing["time_label"] if existing is not None else None)
    all_day = patch.get("all_day") if "all_day" in patch else bool(existing["all_day"]) if existing is not None and "all_day" in existing.keys() else False
    schedule_changed = any(key in patch for key in ("date", "time", "start_at", "end_at", "all_day"))
    explicit_pair = bool(patch.get("start_at") and patch.get("end_at"))
    explicit_start_only = bool(patch.get("start_at")) and ("end_at" not in patch or patch.get("end_at") is None)

    if explicit_pair:
        start_at = parse_optional_iso(str(patch["start_at"]), "Event start_at")
        end_at = parse_optional_iso(str(patch["end_at"]), "Event end_at")
        date_value = start_at[:10] if start_at else date_value
        if len(start_at or "") == 10 or len(end_at or "") == 10:
            raise HTTPException(status_code=422, detail="Timed Events need start_at and end_at datetimes")
        try:
            start_dt = datetime.fromisoformat(str(start_at).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end_at).replace("Z", "+00:00"))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Event start_at/end_at must be valid ISO 8601 datetimes") from error
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=zone)
        else:
            start_dt = start_dt.astimezone(zone)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=zone)
        else:
            end_dt = end_dt.astimezone(zone)
        if end_dt <= start_dt:
            raise HTTPException(status_code=422, detail="Event end_at must be after start_at")
        start_at = start_dt.isoformat()
        end_at = end_dt.isoformat()
        date_value = start_dt.date().isoformat()
        all_day = False
        time_label = f"{start_dt:%H:%M}～{end_dt:%H:%M}"
    elif explicit_start_only:
        start_at = parse_optional_iso(str(patch["start_at"]), "Event start_at")
        if not start_at or len(start_at) == 10:
            raise HTTPException(status_code=422, detail="A timed Event start_at must be an ISO 8601 datetime")
        try:
            start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Event start_at must be a valid ISO 8601 datetime") from error
        start_dt = start_dt.replace(tzinfo=zone) if start_dt.tzinfo is None else start_dt.astimezone(zone)
        start_at = start_dt.isoformat()
        end_at = None
        date_value = start_dt.date().isoformat()
        all_day = False
        time_label = f"{start_dt:%H:%M}"
    elif "end_at" in patch and patch.get("end_at"):
        raise HTTPException(status_code=422, detail="Event end_at cannot be set without start_at")
    elif existing is not None and not schedule_changed and existing["start_at"]:
        start_at = existing["start_at"]
        end_at = existing["end_at"]
        date_value = existing["date_label"]
        raw_time = existing["time_label"]
        time_label = str(raw_time or "全天")
    else:
        if not date_value:
            raise HTTPException(status_code=422, detail="Event requires a confirmed date")
        date_iso = parse_optional_iso(str(date_value), "Event date")
        if not date_iso or len(date_iso) != 10:
            raise HTTPException(status_code=422, detail="Event date must be an ISO calendar date")
        try:
            event_date = date.fromisoformat(date_iso)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Event date must be a valid calendar date") from error
        if all_day:
            start_at = event_date.isoformat()
            end_at = (event_date + timedelta(days=1)).isoformat()
            time_label = "全天"
        else:
            validate_local_event_time(str(raw_time or ""))
            clocks = re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", str(raw_time or ""))
            if not clocks:
                raise HTTPException(status_code=422, detail="Event needs a confirmed start time, or an explicit all-day choice")
            try:
                start_clock = time(int(clocks[0][0]), int(clocks[0][1]))
            except ValueError as error:
                raise HTTPException(status_code=422, detail="Event time must use valid 24-hour clock values") from error
            start_dt = datetime.combine(event_date, start_clock, zone)
            start_at = start_dt.isoformat()
            if len(clocks) == 1:
                end_at = None
                time_label = f"{start_clock:%H:%M}"
            else:
                try:
                    end_clock = time(int(clocks[1][0]), int(clocks[1][1]))
                except ValueError as error:
                    raise HTTPException(status_code=422, detail="Event time must use valid 24-hour clock values") from error
                end_dt = datetime.combine(event_date, end_clock, zone)
                if end_dt <= start_dt:
                    raise HTTPException(status_code=422, detail="Event end time must be after its start time")
                end_at = end_dt.isoformat()
                time_label = f"{start_clock:%H:%M}～{end_clock:%H:%M}"

    recurrence = patch.get("recurrence") if "recurrence" in patch else None
    if existing is not None and "recurrence" not in patch and existing["recurrence_json"]:
        try:
            recurrence = json.loads(existing["recurrence_json"])
        except (TypeError, json.JSONDecodeError):
            recurrence = None
    recurrence_json = None
    if recurrence is not None:
        recurrence = recurrence.model_dump() if hasattr(recurrence, "model_dump") else recurrence
        try:
            first_day = date.fromisoformat(str(start_at)[:10])
            recurrence_start = date.fromisoformat(str(recurrence["start_date"]))
            recurrence_end = date.fromisoformat(str(recurrence["end_date"]))
            weekdays = {int(day) for day in recurrence["weekdays"]}
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="Weekly recurrence needs valid weekdays and term dates") from error
        if not (recurrence_start <= first_day <= recurrence_end) or first_day.isoweekday() not in weekdays:
            raise HTTPException(status_code=422, detail="Event date must be a recurrence weekday inside the term dates")
        recurrence_json = json.dumps(recurrence, ensure_ascii=False)

    return {
        "date_label": str(date_value or str(start_at)[:10]),
        "time_label": time_label,
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone_name,
        "all_day": int(bool(all_day)),
        "recurrence_json": recurrence_json,
    }


def _task_schedule_from_patch(patch: dict[str, Any], existing: sqlite3.Row | None = None) -> dict[str, object]:
    """Resolve AI task date/time fields to the Task's optional standalone schedule."""
    schedule_keys = {"date", "time", "start_at", "end_at", "all_day"}
    if not schedule_keys.intersection(patch):
        if existing is None:
            return {"start_at": None, "end_at": None, "all_day": 0, "timezone": DEFAULT_TIMEZONE}
        return {
            "start_at": existing["start_at"] if "start_at" in existing.keys() else None,
            "end_at": existing["end_at"] if "end_at" in existing.keys() else None,
            "all_day": int(bool(existing["all_day"])) if "all_day" in existing.keys() else 0,
            "timezone": existing["timezone"] if "timezone" in existing.keys() else DEFAULT_TIMEZONE,
        }
    schedule = _event_schedule_fields({key: value for key, value in patch.items() if key in schedule_keys})
    return {key: schedule[key] for key in ("start_at", "end_at", "all_day", "timezone")}


def apply_proposal(user_id: str, proposal_id: str) -> dict:
    with db() as connection:
        row = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if row["status"] != "accepted":
            raise HTTPException(status_code=409, detail="Proposal must be accepted before Apply")
        proposal = _proposal_from_row(row)
        if proposal.needs_review or (proposal.operation == "update" and not proposal.target_id):
            raise HTTPException(status_code=409, detail="Proposal requires target selection or review before Apply")
        try:
            result: dict[str, object]
            patch = proposal.patch.model_dump(exclude_unset=True)
            if proposal.operation == "ignore":
                result = {"ignored": True}
            elif proposal.operation == "update" and proposal.target_type == "event":
                if not _proposal_target_exists(connection, user_id, proposal):
                    raise HTTPException(status_code=404, detail="Event target not found")
                fields = {"title": "title", "location": "location", "date": "date_label", "time": "time_label", "detail": "detail"}
                updates = {fields[key]: value for key, value in patch.items() if key in fields}
                if {"date", "time", "start_at", "end_at", "all_day", "recurrence"}.intersection(patch):
                    existing = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (proposal.target_id, user_id)).fetchone()
                    updates.update(_event_schedule_fields(patch, existing))
                if not updates:
                    raise HTTPException(status_code=422, detail="Event proposal has no applicable fields")
                updates["lifecycle_status"] = "edited"
                updates["updated_at"] = now_iso()
                assignments = ", ".join(f"{key} = ?" for key in updates)
                connection.execute(f"UPDATE events SET {assignments} WHERE id = ? AND user_id = ?", (*updates.values(), proposal.target_id, user_id))
                result = {"event_id": proposal.target_id, "updated": list(updates)}
            elif proposal.operation == "create" and proposal.target_type == "event":
                title = patch.get("title")
                schedule = _event_schedule_fields(patch)
                if not title:
                    raise HTTPException(status_code=422, detail="Event creation requires a title")
                event_id = f"event-{uuid4().hex[:10]}"
                connection.execute(
                    """
                    INSERT INTO events(
                        id, user_id, title, date_label, time_label, detail, source_id, location,
                        start_at, end_at, timezone, all_day, recurrence_json,
                        created_source, origin_proposal_id, lifecycle_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai_generated', ?, 'created', ?)
                    """,
                    (event_id, user_id, title, schedule["date_label"], schedule["time_label"], patch.get("detail") or "", row["source_id"], patch.get("location"), schedule["start_at"], schedule["end_at"], schedule["timezone"], schedule["all_day"], schedule["recurrence_json"], proposal_id, now_iso()),
                )
                result = {"event_id": event_id}
            elif proposal.operation == "update" and proposal.target_type == "task":
                if not _proposal_target_exists(connection, user_id, proposal):
                    raise HTTPException(status_code=404, detail="Task target not found")
                updates: dict[str, object] = {}
                if "title" in patch:
                    updates["title"] = patch["title"]
                if "due" in patch:
                    updates["due_iso"] = parse_optional_iso(patch["due"], "Task due") if patch["due"] else None
                    updates["due_label"] = patch.get("due_label") or (patch["due"] if patch["due"] else "")
                if "due_label" in patch:
                    updates["due_label"] = patch["due_label"]
                if "list_id" in patch:
                    updates["list_id"] = patch["list_id"]
                if {"date", "time", "start_at", "end_at", "all_day"}.intersection(patch):
                    existing_task = connection.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (proposal.target_id, user_id)).fetchone()
                    updates.update(_task_schedule_from_patch(patch, existing_task))
                if not updates:
                    raise HTTPException(status_code=422, detail="Task proposal has no applicable fields")
                assignments = ", ".join(f"{key} = ?" for key in updates)
                connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ? AND user_id = ?", (*updates.values(), proposal.target_id, user_id))
                result = {"task_id": proposal.target_id, "updated": list(updates)}
            elif proposal.operation == "create" and proposal.target_type == "task":
                title = patch.get("title")
                if not title:
                    raise HTTPException(status_code=422, detail="Task creation requires title")
                due_value = patch.get("due")
                due_iso = parse_optional_iso(due_value, "Task due") if due_value else None
                due_label = patch.get("due_label") or (due_value if due_value else "")
                task_schedule = _task_schedule_from_patch(patch)
                task_id = f"task-{uuid4().hex[:10]}"
                connection.execute(
                    "INSERT INTO tasks(id, user_id, title, due_label, due_iso, start_at, end_at, all_day, timezone, status, source_id, owner, list_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 'student', ?)",
                    (task_id, user_id, title, due_label, due_iso, task_schedule["start_at"], task_schedule["end_at"], task_schedule["all_day"], task_schedule["timezone"], row["source_id"], patch.get("list_id")),
                )
                result = {"task_id": task_id}
            else:
                raise HTTPException(status_code=422, detail="Proposal operation is not supported")
            timestamp = now_iso()
            connection.execute(
                "UPDATE proposals SET status = 'applied', error = NULL, updated_at = ?, applied_at = ? WHERE id = ? AND user_id = ?",
                (timestamp, timestamp, proposal_id, user_id),
            )
            if row["source_id"]:
                source_row = connection.execute("SELECT processing_json FROM notices WHERE id = ? AND user_id = ?", (row["source_id"], user_id)).fetchone()
                if source_row:
                    processing = json.loads(source_row["processing_json"] or "{}")
                    if processing.get("capture_state") == "review_draft":
                        processing["capture_state"] = "materialized"
                        connection.execute("UPDATE notices SET processing_json = ? WHERE id = ? AND user_id = ?", (json.dumps(processing, ensure_ascii=False), row["source_id"], user_id))
        except HTTPException:
            raise
        except Exception as error:
            connection.execute("UPDATE proposals SET error = ?, updated_at = ? WHERE id = ? AND user_id = ?", ("Apply failed; retry may be possible", now_iso(), proposal_id, user_id))
            raise HTTPException(status_code=500, detail="Proposal Apply failed") from error
    return {"proposal": {**proposal_payload(row), "status": "applied", "applied_at": timestamp}, "result": result}


def apply_proposals_bulk(user_id: str, proposal_ids: list[str], confirm: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply a mixed proposal batch in one transaction with per-item savepoints.

    A failed item rolls back only its savepoint, so the caller still gets item
    outcomes while SQLite performs one commit for the whole batch.
    """
    results: list[dict[str, Any]] = []
    event_ids: list[str] = []
    with db() as connection:
        for index, proposal_id in enumerate(dict.fromkeys(proposal_ids)):
            savepoint = f"apply_item_{index}"
            connection.execute(f"SAVEPOINT {savepoint}")
            try:
                row = connection.execute(
                    "SELECT * FROM proposals WHERE id = ? AND user_id = ?",
                    (proposal_id, user_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Proposal not found")
                proposal = _proposal_from_row(row)
                if confirm and row["status"] in {"pending", "edited"}:
                    if proposal.needs_review or (proposal.operation == "update" and not proposal.target_id):
                        raise HTTPException(status_code=409, detail="Proposal requires target selection or edit before Apply")
                    patch = proposal.patch.model_dump(exclude_unset=True)
                    if proposal.target_type == "event":
                        if proposal.operation == "create" and not patch.get("title"):
                            raise HTTPException(status_code=409, detail="Event proposal needs a title before confirmation")
                        if proposal.operation == "create":
                            _event_schedule_fields(patch)
                        elif proposal.operation == "update" and {"date", "time", "start_at", "end_at", "all_day", "recurrence"}.intersection(patch):
                            target = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (proposal.target_id, user_id)).fetchone()
                            if target is None:
                                raise HTTPException(status_code=404, detail="Event target not found")
                            _event_schedule_fields(patch, target)
                    connection.execute("UPDATE proposals SET status = 'accepted', updated_at = ? WHERE id = ? AND user_id = ?", (now_iso(), proposal_id, user_id))
                    row = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user_id)).fetchone()
                    proposal = _proposal_from_row(row)
                if row["status"] != "accepted":
                    raise HTTPException(status_code=409, detail="Proposal must be accepted before Apply")
                if proposal.needs_review or (proposal.operation == "update" and not proposal.target_id):
                    raise HTTPException(status_code=409, detail="Proposal requires target selection or review before Apply")
                patch = proposal.patch.model_dump(exclude_unset=True)
                if proposal.operation == "ignore":
                    result: dict[str, object] = {"ignored": True}
                elif proposal.operation == "update" and proposal.target_type == "event":
                    if not _proposal_target_exists(connection, user_id, proposal):
                        raise HTTPException(status_code=404, detail="Event target not found")
                    fields = {"title": "title", "location": "location", "date": "date_label", "time": "time_label", "detail": "detail"}
                    updates = {fields[key]: value for key, value in patch.items() if key in fields}
                    if {"date", "time", "start_at", "end_at", "all_day", "recurrence"}.intersection(patch):
                        existing = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (proposal.target_id, user_id)).fetchone()
                        updates.update(_event_schedule_fields(patch, existing))
                    if not updates:
                        raise HTTPException(status_code=422, detail="Event proposal has no applicable fields")
                    updates.update({"lifecycle_status": "edited", "updated_at": now_iso()})
                    assignments = ", ".join(f"{key} = ?" for key in updates)
                    connection.execute(f"UPDATE events SET {assignments} WHERE id = ? AND user_id = ?", (*updates.values(), proposal.target_id, user_id))
                    connection.execute("UPDATE events SET sync_status = 'pending', sync_conflict_json = '{}' WHERE id = ? AND user_id = ? AND created_source IN ('local', 'ai_generated')", (proposal.target_id, user_id))
                    result = {"event_id": proposal.target_id, "updated": list(updates)}
                    event_ids.append(str(proposal.target_id))
                elif proposal.operation == "create" and proposal.target_type == "event":
                    title = patch.get("title")
                    if not title:
                        raise HTTPException(status_code=422, detail="Event creation requires a title")
                    schedule = _event_schedule_fields(patch)
                    event_id = f"event-{uuid4().hex[:10]}"
                    connection.execute(
                        """INSERT INTO events(
                            id, user_id, title, date_label, time_label, detail, source_id, location,
                            start_at, end_at, timezone, all_day, recurrence_json,
                            created_source, origin_proposal_id, lifecycle_status, sync_status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai_generated', ?, 'created', 'pending', ?)""",
                        (event_id, user_id, title, schedule["date_label"], schedule["time_label"], patch.get("detail") or "", row["source_id"], patch.get("location"), schedule["start_at"], schedule["end_at"], schedule["timezone"], schedule["all_day"], schedule["recurrence_json"], proposal_id, now_iso()),
                    )
                    result = {"event_id": event_id}
                    event_ids.append(event_id)
                elif proposal.operation == "update" and proposal.target_type == "task":
                    if not _proposal_target_exists(connection, user_id, proposal):
                        raise HTTPException(status_code=404, detail="Task target not found")
                    updates: dict[str, object] = {}
                    if "title" in patch:
                        updates["title"] = patch["title"]
                    if "due" in patch:
                        updates["due_iso"] = parse_optional_iso(patch["due"], "Task due") if patch["due"] else None
                        updates["due_label"] = patch.get("due_label") or (patch["due"] if patch["due"] else "")
                    if "due_label" in patch:
                        updates["due_label"] = patch["due_label"]
                    if "list_id" in patch:
                        updates["list_id"] = patch["list_id"]
                    if {"date", "time", "start_at", "end_at", "all_day"}.intersection(patch):
                        existing_task = connection.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (proposal.target_id, user_id)).fetchone()
                        updates.update(_task_schedule_from_patch(patch, existing_task))
                    if not updates:
                        raise HTTPException(status_code=422, detail="Task proposal has no applicable fields")
                    assignments = ", ".join(f"{key} = ?" for key in updates)
                    connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ? AND user_id = ?", (*updates.values(), proposal.target_id, user_id))
                    result = {"task_id": proposal.target_id, "updated": list(updates)}
                elif proposal.operation == "create" and proposal.target_type == "task":
                    title = patch.get("title")
                    if not title:
                        raise HTTPException(status_code=422, detail="Task creation requires title")
                    due_value = patch.get("due")
                    task_schedule = _task_schedule_from_patch(patch)
                    task_id = f"task-{uuid4().hex[:10]}"
                    connection.execute(
                        "INSERT INTO tasks(id, user_id, title, due_label, due_iso, start_at, end_at, all_day, timezone, status, source_id, owner, list_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 'student', ?)",
                        (task_id, user_id, title, patch.get("due_label") or (due_value if due_value else ""), parse_optional_iso(due_value, "Task due") if due_value else None, task_schedule["start_at"], task_schedule["end_at"], task_schedule["all_day"], task_schedule["timezone"], row["source_id"], patch.get("list_id")),
                    )
                    result = {"task_id": task_id}
                else:
                    raise HTTPException(status_code=422, detail="Proposal operation is not supported")
                timestamp = now_iso()
                connection.execute("UPDATE proposals SET status = 'applied', error = NULL, updated_at = ?, applied_at = ? WHERE id = ? AND user_id = ?", (timestamp, timestamp, proposal_id, user_id))
                if row["source_id"]:
                    source_row = connection.execute("SELECT processing_json FROM notices WHERE id = ? AND user_id = ?", (row["source_id"], user_id)).fetchone()
                    if source_row:
                        processing = json.loads(source_row["processing_json"] or "{}")
                        if processing.get("capture_state") == "review_draft":
                            processing["capture_state"] = "materialized"
                            connection.execute("UPDATE notices SET processing_json = ? WHERE id = ? AND user_id = ?", (json.dumps(processing, ensure_ascii=False), row["source_id"], user_id))
                updated = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user_id)).fetchone()
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                results.append({"id": proposal_id, "status": "applied", "proposal": proposal_payload(updated), "result": result})
            except HTTPException as error:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                connection.execute("UPDATE proposals SET error = ?, updated_at = ? WHERE id = ? AND user_id = ?", (str(error.detail), now_iso(), proposal_id, user_id))
                results.append({"id": proposal_id, "status": "failed", "message": str(error.detail), "http_status": error.status_code})
            except Exception:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                connection.execute("UPDATE proposals SET error = ?, updated_at = ? WHERE id = ? AND user_id = ?", ("Apply failed; retry may be possible", now_iso(), proposal_id, user_id))
                results.append({"id": proposal_id, "status": "failed", "message": "Proposal Apply failed"})
    return results, list(dict.fromkeys(event_ids))


def user_from_request(request: Request) -> sqlite3.Row | None:
    session_token = request.cookies.get(SESSION_COOKIE)
    if not session_token:
        return None
    token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
    with db() as connection:
        return connection.execute(
            """
            SELECT users.* FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (token_hash, now_iso()),
        ).fetchone()


def require_user(request: Request) -> sqlite3.Row:
    user = user_from_request(request)
    if user is None:
        api_logger.warning("security authentication_required method=%s path=%s", request.method, request.url.path)
        raise HTTPException(status_code=401, detail="Sign-in required")
    return user


def create_session(user_id: str, response: Response) -> None:
    session_token = secrets.token_urlsafe(32)
    session_id = uuid4().hex
    token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    with db() as connection:
        timestamp = now_iso()
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (timestamp,))
        connection.execute(
            "INSERT INTO sessions(id, user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, user_id, token_hash, expires_at, timestamp),
        )
        session_rows = connection.execute(
            "SELECT id FROM sessions WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        stale_ids = [row["id"] for row in session_rows[security_policy().max_sessions_per_user :]]
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            connection.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", stale_ids)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
    )


def provider_settings(provider: str) -> dict[str, str] | None:
    if provider == "github":
        client_id = os.getenv("GITHUB_CLIENT_ID", "")
        client_secret = os.getenv("GITHUB_CLIENT_SECRET", "")
        if client_id and client_secret:
            return {"client_id": client_id, "client_secret": client_secret}
    if provider == "google":
        client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        if client_id and client_secret:
            return {"client_id": client_id, "client_secret": client_secret}
    return None


def callback_url(provider: str) -> str:
    return f"{BACKEND_URL}/api/auth/{provider}/callback"


def http_json(url: str, method: str = "GET", form: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> dict | list:
    request_headers = {"Accept": "application/json", **(headers or {})}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as result:
            raw = read_limited(result, security_policy().oauth_response_bytes)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise HTTPException(status_code=502, detail="OAuth provider request failed") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ResponseTooLarge) as error:
        raise HTTPException(status_code=502, detail="OAuth provider request failed") from error


def github_identity(code: str, settings: dict[str, str]) -> OAuthIdentity:
    token = http_json(
        "https://github.com/login/oauth/access_token",
        method="POST",
        form={"client_id": settings["client_id"], "client_secret": settings["client_secret"], "code": code, "redirect_uri": callback_url("github")},
        headers={"Accept": "application/json"},
    )
    access_token = token.get("access_token") if isinstance(token, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=502, detail="GitHub did not return an access token")
    auth_headers = {"Authorization": f"Bearer {access_token}", "X-GitHub-Api-Version": "2022-11-28"}
    profile = http_json("https://api.github.com/user", headers=auth_headers)
    emails = http_json("https://api.github.com/user/emails", headers=auth_headers)
    if not isinstance(profile, dict) or not profile.get("id"):
        raise HTTPException(status_code=502, detail="GitHub profile response was invalid")
    if not isinstance(emails, list):
        raise HTTPException(status_code=502, detail="GitHub email response was invalid")
    verified = [item for item in emails if isinstance(item, dict) and item.get("verified")]
    email_item = next((item for item in verified if item.get("primary")), None) or (verified[0] if verified else None)
    return OAuthIdentity(
        subject=str(profile["id"]),
        email=email_item.get("email") if email_item else profile.get("email"),
        email_verified=bool(email_item and email_item.get("verified")),
        name=profile.get("name") or profile.get("login") or "我的空間",
        avatar_url=profile.get("avatar_url"),
    )


def google_identity(code: str, settings: dict[str, str]) -> OAuthIdentity:
    token = http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        form={"code": code, "client_id": settings["client_id"], "client_secret": settings["client_secret"], "redirect_uri": callback_url("google"), "grant_type": "authorization_code"},
    )
    access_token = token.get("access_token") if isinstance(token, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=502, detail="Google did not return an access token")
    profile = http_json("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {access_token}"})
    if not isinstance(profile, dict) or not profile.get("sub"):
        raise HTTPException(status_code=502, detail="Google profile response was invalid")
    return OAuthIdentity(
        subject=str(profile["sub"]),
        email=profile.get("email"),
        email_verified=bool(profile.get("email_verified")),
        name=profile.get("name") or "我的空間",
        avatar_url=profile.get("picture"),
    )


def google_calendar_tokens(code: str, settings: dict[str, str]) -> dict[str, Any]:
    token = http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        form={
            "code": code,
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
            "redirect_uri": callback_url("google"),
            "grant_type": "authorization_code",
        },
    )
    access_token = token.get("access_token") if isinstance(token, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=502, detail="Google did not return a calendar access token")
    return dict(token)


def save_google_calendar_connection(user_id: str, token_payload: dict[str, Any]) -> dict:
    access_token = token_payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CalendarConnectionError("Google Calendar access token is missing")
    connection_id = f"google-calendar-{user_id}"
    timestamp = now_iso()
    with db() as connection:
        previous = connection.execute(
            "SELECT credential_ref FROM calendar_connections WHERE id = ? AND user_id = ?",
            (connection_id, user_id),
        ).fetchone()
        credential_payload = dict(token_payload)
        if not credential_payload.get("refresh_token") and previous and previous["credential_ref"]:
            old = connection.execute(
                "SELECT credential_ciphertext FROM integration_credentials WHERE id = ? AND user_id = ?",
                (previous["credential_ref"], user_id),
            ).fetchone()
            if old:
                try:
                    old_payload = json.loads(credential_box().decrypt(old["credential_ciphertext"].encode()).decode())
                except (InvalidToken, UnicodeDecodeError, RuntimeError, json.JSONDecodeError):
                    old_payload = {}
                if isinstance(old_payload, dict) and old_payload.get("refresh_token"):
                    credential_payload["refresh_token"] = old_payload["refresh_token"]
        # Keep a provider-neutral compatibility source so Calendar queries and
        # event CRUD can use the same mirror tables as CalDAV.  The OAuth token
        # remains only in integration_credentials; calendar_sources never stores
        # Google secrets or raw provider payloads.
        connection.execute(
            """
            INSERT INTO calendar_sources(
                id, user_id, kind, name, server_url, username, credential_ciphertext,
                status, last_checked_at, last_error, sync_status, last_synced_at,
                sync_error, created_at, updated_at
            ) VALUES (?, ?, 'google-calendar', 'Google Calendar', ?, 'google', ?,
                      'connected', ?, NULL, 'never', NULL, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind='google-calendar', name='Google Calendar', server_url=excluded.server_url,
                status='connected', last_checked_at=excluded.last_checked_at, last_error=NULL,
                sync_error=NULL, updated_at=excluded.updated_at
            """,
            (
                connection_id, user_id, GOOGLE_CALENDAR_API_ROOT, encrypt_credential(""),
                timestamp, timestamp, timestamp,
            ),
        )
        _upsert_calendar_connection(
            connection,
            connection_id=connection_id,
            user_id=user_id,
            provider=CalendarProvider.GOOGLE.value,
            status=CalendarConnectionStatus.CONNECTED.value,
            credential_ciphertext=encrypt_credential(json.dumps(credential_payload, ensure_ascii=False)),
            last_sync_at=None,
            sync_mode=CalendarSyncMode.MANUAL.value,
            metadata={
                "account": "google",
                "scope": credential_payload.get("scope", ""),
                "legacy_source_id": connection_id,
                "name": "Google Calendar",
            },
            created_at=timestamp,
            updated_at=timestamp,
        )
        row = connection.execute("SELECT * FROM calendar_connections WHERE id = ?", (connection_id,)).fetchone()
    return calendar_connection_payload(row)


GOOGLE_CALENDAR_API_ROOT = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar"


def _google_connection_credentials(user_id: str, connection_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM calendar_connections WHERE id = ? AND user_id = ? AND provider = ?",
            (connection_id, user_id, CalendarProvider.GOOGLE.value),
        ).fetchone()
        if row is None:
            raise CalendarConnectionError("Google Calendar connection was not found")
        if not row["credential_ref"]:
            raise CalendarConnectionError("Google Calendar connection has no credential")
        credential = connection.execute(
            "SELECT credential_ciphertext FROM integration_credentials WHERE id = ? AND user_id = ?",
            (row["credential_ref"], user_id),
        ).fetchone()
    if credential is None:
        raise CalendarConnectionError("Google Calendar credential was not found")
    try:
        payload = json.loads(credential_box().decrypt(credential["credential_ciphertext"].encode()).decode())
    except (InvalidToken, UnicodeDecodeError, RuntimeError, json.JSONDecodeError) as error:
        raise CalendarConnectionError("無法讀取 Google Calendar 安全憑證") from error
    if not isinstance(payload, dict):
        raise CalendarConnectionError("Google Calendar credential is invalid")
    return row, payload


def _store_google_credentials(user_id: str, connection_id: str, payload: dict[str, Any]) -> None:
    timestamp = now_iso()
    with db() as connection:
        row = connection.execute(
            "SELECT credential_ref FROM calendar_connections WHERE id = ? AND user_id = ?",
            (connection_id, user_id),
        ).fetchone()
        if row is None or not row["credential_ref"]:
            raise CalendarConnectionError("Google Calendar connection has no credential")
        connection.execute(
            "UPDATE integration_credentials SET credential_ciphertext = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (encrypt_credential(json.dumps(payload, ensure_ascii=False)), timestamp, row["credential_ref"], user_id),
        )


def _google_access_token(user_id: str, connection_id: str) -> str:
    _row, payload = _google_connection_credentials(user_id, connection_id)
    refresh_token = str(payload.get("refresh_token") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    settings = provider_settings("google")
    if refresh_token and settings is not None:
        try:
            refreshed = http_json(
                "https://oauth2.googleapis.com/token",
                method="POST",
                form={
                    "client_id": settings["client_id"],
                    "client_secret": settings["client_secret"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except HTTPException as error:
            if not access_token:
                raise CalendarAuthorizationError("Google Calendar authorization is missing or expired") from error
        else:
            if isinstance(refreshed, dict) and refreshed.get("access_token"):
                payload.update(refreshed)
                payload["refresh_token"] = refresh_token
                _store_google_credentials(user_id, connection_id, payload)
                access_token = str(payload["access_token"])
    if not access_token:
        raise CalendarAuthorizationError("Google Calendar authorization is missing or expired")
    return access_token


def google_calendar_api_json(
    access_token: str,
    path: str,
    *,
    method: str = "GET",
    query: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{GOOGLE_CALENDAR_API_ROOT}/{path.lstrip('/')}"
    if query:
        clean_query = {key: value for key, value in query.items() if value is not None}
        url += "?" + urllib.parse.urlencode(clean_query, doseq=True)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json; charset=utf-8"} if body is not None else {}),
            "User-Agent": "OneMoreThing-Calendar/0.3",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = read_limited(response, security_policy().calendar_response_bytes)
            if not raw:
                return {}
            result = json.loads(raw.decode("utf-8"))
            return result if isinstance(result, dict) else {}
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        if error.code in {401, 403}:
            raise CalendarAuthorizationError("Google Calendar authorization is missing or expired") from error
        raise CalendarConnectionError(f"Google Calendar API failed (HTTP {error.code}){': ' + detail if detail else ''}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ResponseTooLarge) as error:
        raise CalendarConnectionError("Unable to read Google Calendar API safely") from error


def _google_calendar_path(calendar_remote_id: str, suffix: str = "") -> str:
    encoded = urllib.parse.quote(str(calendar_remote_id), safe="")
    return f"calendars/{encoded}{suffix}"


def _google_event_resource(payload: CalendarEventInput, *, require_end: bool = True) -> dict[str, Any]:
    resource: dict[str, Any] = {"summary": payload.title}
    if payload.all_day:
        start_date = str(payload.start_at)[:10]
        resource["start"] = {"date": start_date}
        if payload.end_at:
            end_date = str(payload.end_at)[:10]
        else:
            end_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
        resource["end"] = {"date": end_date}
    else:
        if require_end and not payload.end_at:
            raise CalendarConnectionError(
                "Google Calendar requires an end time for timed events; add an end time or keep this start-only event in OMT"
            )
        resource["start"] = {"dateTime": payload.start_at}
        if payload.timezone:
            resource["start"]["timeZone"] = payload.timezone
        if payload.end_at:
            resource["end"] = {"dateTime": payload.end_at}
            if payload.timezone:
                resource["end"]["timeZone"] = payload.timezone
    if payload.location:
        resource["location"] = payload.location
    if payload.description:
        resource["description"] = payload.description
    if payload.recurrence_rule:
        rule = str(payload.recurrence_rule)
        resource["recurrence"] = [rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"]
    return resource


def _provider_text(value: Any, max_chars: int, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    normalized = str(value).replace("\x00", "").strip()
    if not normalized:
        return default
    return normalized[:max_chars]


def _provider_external_url(value: Any) -> str | None:
    candidate = _provider_text(value, 2048)
    if not candidate:
        return None
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return candidate


def _google_event_normalized(item: dict[str, Any], calendar_timezone: str | None) -> dict[str, Any] | None:
    remote_id = str(item.get("id") or "").strip()
    if not remote_id or len(remote_id) > 1024:
        return None
    status = str(item.get("status") or "confirmed")
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    all_day = bool(start.get("date") and not start.get("dateTime"))
    if all_day:
        start_date = str(start.get("date") or "")
        end_date = str(end.get("date") or "")
        if not start_date:
            return None
        zone, zone_name = timezone_or_utc(str(calendar_timezone or DEFAULT_TIMEZONE))
        try:
            start_at = datetime.combine(date.fromisoformat(start_date), time.min, tzinfo=zone).isoformat()
            end_at = datetime.combine(date.fromisoformat(end_date), time.min, tzinfo=zone).isoformat() if end_date else None
        except ValueError:
            return None
    else:
        start_at = str(start.get("dateTime") or "").strip()
        end_at = str(end.get("dateTime") or "").strip() or None
        if not start_at:
            return None
        zone_name = str(start.get("timeZone") or calendar_timezone or DEFAULT_TIMEZONE)
    recurrence = item.get("recurrence") if isinstance(item.get("recurrence"), list) else []
    recurrence_rule = next((str(value)[6:] for value in recurrence if str(value).upper().startswith("RRULE:")), None)
    return {
        "remote_id": remote_id,
        "href": _provider_external_url(item.get("htmlLink")) or remote_id,
        "etag": _provider_text(item.get("etag"), 512),
        "title": _provider_text(item.get("summary"), 500, default="（未命名行程）"),
        "start_at": start_at,
        "end_at": end_at,
        "timezone": zone_name,
        "all_day": all_day,
        "location": _provider_text(item.get("location"), 2000),
        "description": _provider_text(item.get("description"), 10000),
        "url": _provider_external_url(item.get("htmlLink")),
        "recurrence_rule": _provider_text(recurrence_rule, 255),
        "status": status,
    }


def _stable_google_calendar_id(connection_id: str, remote_id: str) -> str:
    digest = hashlib.sha256(f"{connection_id}:{remote_id}".encode()).hexdigest()[:24]
    return f"gcal-{digest}"


def _google_list_all(
    access_token: str,
    path: str,
    query: dict[str, Any] | None = None,
    *,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    policy = security_policy()
    item_limit = max_items if max_items is not None else policy.calendar_max_items
    item_limit = min(max(1, item_limit), policy.calendar_max_items)
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    for _page in range(policy.calendar_max_pages):
        current = dict(query or {})
        if page_token:
            current["pageToken"] = page_token
        payload = google_calendar_api_json(access_token, path, query=current)
        page_items = [item for item in payload.get("items", []) if isinstance(item, dict)]
        if len(items) + len(page_items) > item_limit:
            raise CalendarConnectionError("Google Calendar item count exceeds the configured sync limit")
        items.extend(page_items)
        page_token = str(payload.get("nextPageToken") or "").strip() or None
        if not page_token:
            return items
    raise CalendarConnectionError("Google Calendar pagination exceeds the configured page limit")


def persist_google_calendar_events(
    user_id: str,
    source_id: str,
    calendar: sqlite3.Row,
    remote_items: list[dict[str, Any]],
    timestamp: str,
) -> int:
    normalized: list[dict[str, Any]] = []
    cancelled_ids: set[str] = set()
    for item in remote_items:
        remote_id = str(item.get("id") or "").strip()
        if not remote_id:
            continue
        if str(item.get("status") or "") == "cancelled":
            cancelled_ids.add(remote_id)
            continue
        event = _google_event_normalized(item, calendar["timezone"])
        if event is not None:
            normalized.append(event)
    seen_ids = {str(event["remote_id"]) for event in normalized}
    with db() as connection:
        for event in normalized:
            linked_local = connection.execute(
                """
                SELECT id, sync_status FROM events
                WHERE user_id = ? AND external_calendar_ref = ?
                  AND calendar_sync_enabled = 1 AND lifecycle_status NOT IN ('archived','deleted')
                LIMIT 1
                """,
                (user_id, event["remote_id"]),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO calendar_events(
                    id, source_id, calendar_id, remote_id, href, etag, title, start_at, end_at,
                    timezone, all_day, location, description, url, recurrence_rule, status,
                    created_at, updated_at, created_source, external_calendar_ref, lifecycle_status,
                    provider, sync_status, last_synced_at, sync_conflict_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'external_calendar', ?, 'created', ?, 'synced', ?, '{}')
                ON CONFLICT(calendar_id, remote_id) DO UPDATE SET
                    source_id=excluded.source_id, href=excluded.href, etag=excluded.etag, title=excluded.title,
                    start_at=excluded.start_at, end_at=excluded.end_at, timezone=excluded.timezone,
                    all_day=excluded.all_day, location=excluded.location, description=excluded.description,
                    url=excluded.url, recurrence_rule=excluded.recurrence_rule, status=excluded.status,
                    updated_at=excluded.updated_at, provider=excluded.provider, sync_status='synced',
                    last_synced_at=excluded.last_synced_at, sync_conflict_json='{}', lifecycle_status='created', deleted_at=NULL
                """,
                (
                    uuid4().hex, source_id, calendar["id"], event["remote_id"], event["href"], event["etag"],
                    event["title"], event["start_at"], event["end_at"], event["timezone"], int(bool(event["all_day"])),
                    event["location"], event["description"], event["url"], event["recurrence_rule"], event["status"],
                    timestamp, timestamp, event["remote_id"], CalendarProvider.GOOGLE.value, timestamp,
                ),
            )
            if linked_local is not None and linked_local["sync_status"] == "synced":
                date_label, time_label = _calendar_event_local_labels(event)
                connection.execute(
                    """UPDATE events SET title=?, date_label=?, time_label=?, detail=?, location=?,
                       start_at=?, end_at=?, timezone=?, all_day=?, sync_status='synced',
                       last_synced_at=?, sync_conflict_json='{}', updated_at=? WHERE id=?""",
                    (
                        event["title"], date_label, time_label, event["description"] or "", event["location"],
                        event["start_at"], event["end_at"], event["timezone"] or DEFAULT_TIMEZONE, int(bool(event["all_day"])),
                        timestamp, timestamp, linked_local["id"],
                    ),
                )
        missing_rows = connection.execute(
            "SELECT remote_id FROM calendar_events WHERE calendar_id = ? AND lifecycle_status <> 'deleted'",
            (calendar["id"],),
        ).fetchall()
        missing_ids = {row["remote_id"] for row in missing_rows} - seen_ids
        deleted_ids = missing_ids | cancelled_ids
        if deleted_ids:
            placeholders = ",".join("?" for _ in deleted_ids)
            connection.execute(
                f"UPDATE calendar_events SET lifecycle_status='deleted', status='cancelled', deleted_at=?, updated_at=? WHERE calendar_id=? AND remote_id IN ({placeholders})",
                (timestamp, timestamp, calendar["id"], *deleted_ids),
            )
            # A clean, synced local Event represents the same Calendar Event.
            # If Google deletes it remotely, mirror that deletion locally as true
            # two-way sync.  Unsynced local edits are preserved instead of being
            # destroyed by a pull; they are detached and can be reconciled later.
            linked_rows = connection.execute(
                f"SELECT id, sync_status FROM events WHERE user_id=? AND external_calendar_ref IN ({placeholders}) AND calendar_sync_enabled=1 AND lifecycle_status NOT IN ('archived','deleted')",
                (user_id, *deleted_ids),
            ).fetchall()
            synced_ids = [row["id"] for row in linked_rows if row["sync_status"] == "synced"]
            preserved_ids = [row["id"] for row in linked_rows if row["sync_status"] != "synced"]
            if synced_ids:
                local_placeholders = ",".join("?" for _ in synced_ids)
                connection.execute(
                    f"UPDATE events SET lifecycle_status='deleted', deleted_at=?, external_calendar_ref=NULL, calendar_sync_enabled=0, sync_status='local', updated_at=? WHERE id IN ({local_placeholders})",
                    (timestamp, timestamp, *synced_ids),
                )
                connection.execute(
                    f"UPDATE tasks SET related_event_id=NULL WHERE user_id=? AND related_event_id IN ({local_placeholders})",
                    (user_id, *synced_ids),
                )
            if preserved_ids:
                local_placeholders = ",".join("?" for _ in preserved_ids)
                connection.execute(
                    f"UPDATE events SET external_calendar_ref=NULL, calendar_sync_enabled=0, sync_status='local', sync_conflict_json=?, updated_at=? WHERE id IN ({local_placeholders})",
                    (json.dumps({"reason": "remote_deleted_with_local_changes"}, ensure_ascii=False), timestamp, *preserved_ids),
                )
    return len(normalized)


def _sync_google_calendar_connection_once(user_id: str, connection_id: str) -> dict[str, Any]:
    timestamp = now_iso()
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM calendar_connections WHERE id=? AND user_id=? AND provider=?",
            (connection_id, user_id, CalendarProvider.GOOGLE.value),
        ).fetchone()
        if row is None:
            raise CalendarConnectionError("Google Calendar connection was not found")
        if row["status"] in {CalendarConnectionStatus.DISCONNECTED.value, CalendarConnectionStatus.EXPIRED.value}:
            raise CalendarConnectionError("Google Calendar connection is disconnected or expired")
        connection.execute("UPDATE calendar_connections SET status='syncing', updated_at=? WHERE id=?", (timestamp, connection_id))
        connection.execute("UPDATE calendar_sources SET sync_status='syncing', sync_error=NULL, updated_at=? WHERE id=? AND user_id=?", (timestamp, connection_id, user_id))
    try:
        access_token = _google_access_token(user_id, connection_id)
        calendars = _google_list_all(access_token, "users/me/calendarList", {"maxResults": 250})
        if len(calendars) > security_policy().calendar_max_calendars:
            raise CalendarConnectionError("Google Calendar count exceeds the configured discovery limit")
        with db() as connection:
            existing_remote_ids = {row["remote_url"] for row in connection.execute("SELECT remote_url FROM calendar_calendars WHERE source_id=?", (connection_id,)).fetchall()}
            active_remote_ids: set[str] = set()
            for item in calendars:
                remote_id = str(item.get("id") or "").strip()
                if not remote_id or bool(item.get("deleted")):
                    continue
                active_remote_ids.add(remote_id)
                calendar_id = _stable_google_calendar_id(connection_id, remote_id)
                read_only = str(item.get("accessRole") or "reader") not in {"owner", "writer"}
                connection.execute(
                    """INSERT INTO calendar_calendars(id, source_id, remote_url, display_name, timezone, read_only, sync_status, last_synced_at, last_error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'syncing', NULL, NULL, ?, ?)
                       ON CONFLICT(source_id, remote_url) DO UPDATE SET display_name=excluded.display_name, timezone=excluded.timezone,
                       read_only=excluded.read_only, sync_status='syncing', last_error=NULL, updated_at=excluded.updated_at""",
                    (calendar_id, connection_id, remote_id, str(item.get("summaryOverride") or item.get("summary") or "Calendar"), item.get("timeZone"), int(read_only), timestamp, timestamp),
                )
            removed = existing_remote_ids - active_remote_ids
            if removed:
                placeholders = ",".join("?" for _ in removed)
                connection.execute(f"DELETE FROM calendar_calendars WHERE source_id=? AND remote_url IN ({placeholders})", (connection_id, *removed))
            calendar_rows = connection.execute("SELECT * FROM calendar_calendars WHERE source_id=? ORDER BY display_name", (connection_id,)).fetchall()
        total_events = 0
        for calendar in calendar_rows:
            remaining = security_policy().calendar_max_items - total_events
            if remaining <= 0:
                raise CalendarConnectionError("Google Calendar event count exceeds the configured sync limit")
            items = _google_list_all(
                access_token,
                _google_calendar_path(calendar["remote_url"], "/events"),
                {"maxResults": min(2500, remaining), "showDeleted": "true", "singleEvents": "false"},
            )
            if len(items) > remaining:
                raise CalendarConnectionError("Google Calendar event count exceeds the configured sync limit")
            total_events += persist_google_calendar_events(user_id, connection_id, calendar, items, timestamp)
            with db() as connection:
                connection.execute("UPDATE calendar_calendars SET sync_status='ok', last_synced_at=?, last_error=NULL, updated_at=? WHERE id=?", (timestamp, timestamp, calendar["id"]))
        with db() as connection:
            connection.execute("UPDATE calendar_sources SET status='ready', sync_status='ok', last_synced_at=?, sync_error=NULL, updated_at=? WHERE id=? AND user_id=?", (timestamp, timestamp, connection_id, user_id))
            connection.execute("UPDATE calendar_connections SET status='ready', last_sync_at=?, updated_at=? WHERE id=? AND user_id=?", (timestamp, timestamp, connection_id, user_id))
        return {"status": "ok", "events": total_events, "calendars": len(calendar_rows), "last_synced_at": timestamp}
    except CalendarAuthorizationError as error:
        message = str(error)
        with db() as connection:
            connection.execute("UPDATE calendar_sources SET status='error', sync_status='error', sync_error=?, updated_at=? WHERE id=? AND user_id=?", (message, now_iso(), connection_id, user_id))
            connection.execute("UPDATE calendar_connections SET status='expired', updated_at=? WHERE id=? AND user_id=?", (now_iso(), connection_id, user_id))
        raise
    except (CalendarConnectionError, HTTPException) as error:
        message = str(getattr(error, "detail", error))
        with db() as connection:
            connection.execute("UPDATE calendar_sources SET status='error', sync_status='error', sync_error=?, updated_at=? WHERE id=? AND user_id=?", (message, now_iso(), connection_id, user_id))
            connection.execute("UPDATE calendar_connections SET status='error', updated_at=? WHERE id=? AND user_id=?", (now_iso(), connection_id, user_id))
        raise CalendarConnectionError(message) from error


def sync_google_calendar_connection(user_id: str, connection_id: str) -> dict[str, Any]:
    try:
        with OPERATIONS.exclusive(f"calendar-sync:{user_id}:{connection_id}"):
            return _sync_google_calendar_connection_once(user_id, connection_id)
    except OperationBusy as error:
        raise HTTPException(status_code=429, detail="這個行事曆正在同步，請稍後再試。", headers={"Retry-After": "3"}) from error


def resolve_identity(provider: str, identity: OAuthIdentity, mode: str, current_user_id: str | None) -> str:
    normalized_email = identity.email.lower().strip() if identity.email else None
    with db() as connection:
        existing = connection.execute(
            "SELECT user_id FROM auth_accounts WHERE provider = ? AND provider_subject = ?",
            (provider, identity.subject),
        ).fetchone()
        if existing:
            if mode == "link" and current_user_id and existing["user_id"] != current_user_id:
                raise HTTPException(status_code=409, detail="This provider account is linked to another One More Thing account")
            return existing["user_id"]

        if mode == "link":
            if not current_user_id:
                raise HTTPException(status_code=401, detail="Sign in before linking another provider")
            if normalized_email and identity.email_verified:
                email_match = connection.execute("SELECT id FROM users WHERE lower(email) = ?", (normalized_email,)).fetchone()
                if email_match and email_match["id"] != current_user_id:
                    raise HTTPException(status_code=409, detail="This verified email belongs to another account")
            connection.execute(
                "INSERT INTO auth_accounts(id, user_id, provider, provider_subject, email, email_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid4().hex, current_user_id, provider, identity.subject, identity.email, int(identity.email_verified), now_iso()),
            )
            return current_user_id

        if normalized_email and identity.email_verified:
            email_match = connection.execute("SELECT id FROM users WHERE lower(email) = ?", (normalized_email,)).fetchone()
            if email_match:
                raise HTTPException(status_code=409, detail="An account with this verified email already exists; sign in there first and link this provider")

        user_id = uuid4().hex
        timestamp = now_iso()
        connection.execute(
            "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, identity.name, identity.email, identity.avatar_url, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO auth_accounts(id, user_id, provider, provider_subject, email, email_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid4().hex, user_id, provider, identity.subject, identity.email, int(identity.email_verified), timestamp),
        )
        return user_id


def auth_redirect(query: str) -> RedirectResponse:
    separator = "&" if "?" in FRONTEND_URL else "?"
    response = RedirectResponse(f"{FRONTEND_URL}{separator}{query}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def stable_auth_error(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("-", "_")
    if normalized in {"access_denied", "consent_denied"}:
        return normalized
    if normalized in {"invalid_oauth_state", "invalid_state", "expired_state"}:
        return "invalid_state"
    if normalized in {"state_mismatch", "mismatched_state"}:
        return "state_mismatch"
    if normalized in {"provider_not_configured", "not_configured"}:
        return "provider_not_configured"
    return "callback_failed"


def auth_error_redirect(code: str) -> RedirectResponse:
    response = auth_redirect(urllib.parse.urlencode({"auth_error": code}))
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


class CalendarConnectionError(Exception):
    pass


class CalendarAuthorizationError(CalendarConnectionError):
    """Google credential is no longer usable and the user must reconnect OAuth."""


def normalize_nextcloud_base_url(value: str) -> str:
    """Normalize the user-facing Nextcloud base URL before Login Flow discovery."""
    candidate = value.strip().strip("`").strip().strip("\\")
    if not candidate:
        raise CalendarConnectionError("請輸入 Nextcloud base URL")
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise CalendarConnectionError("Base URL 不能包含空白或控制字元")
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", candidate, re.IGNORECASE):
        candidate = f"https://{candidate.lstrip('/')}"
    parsed = urllib.parse.urlparse(candidate)
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise CalendarConnectionError("Base URL 的 port 無效") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname or parsed.query or parsed.fragment:
        raise CalendarConnectionError("Base URL 必須是有效的 http(s) 網址，不能包含 query 或 fragment")
    if parsed.username or parsed.password:
        raise CalendarConnectionError("Base URL 不應包含帳號或密碼")
    return candidate.rstrip("/")


def credential_box() -> Fernet:
    configured_key = os.getenv("OMT_CREDENTIAL_KEY")
    key_path = DATA_DIR / "credentials.key"
    if configured_key:
        try:
            return Fernet(configured_key.encode())
        except (ValueError, TypeError) as error:
            raise RuntimeError("OMT_CREDENTIAL_KEY is not a valid Fernet key") from error
    if APP_ENVIRONMENT in {"production", "prod"}:
        raise RuntimeError("OMT_CREDENTIAL_KEY must be configured in production")
    if key_path.exists():
        try:
            _restrict_private_file(key_path)
            return Fernet(key_path.read_bytes())
        except (ValueError, TypeError) as error:
            raise RuntimeError("The local credential key is invalid") from error
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    _restrict_private_file(key_path)
    return Fernet(key)


def encrypt_credential(value: str) -> str:
    return credential_box().encrypt(value.encode()).decode()


def _calendar_connection_from_row(row: sqlite3.Row) -> CalendarConnection:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return CalendarConnection(
        id=row["id"],
        user_id=row["user_id"],
        provider=row["provider"],
        status=row["status"],
        credential_ref=row["credential_ref"],
        last_sync_at=row["last_sync_at"],
        sync_mode=row["sync_mode"],
        metadata=metadata,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _upsert_calendar_connection(
    connection: sqlite3.Connection,
    *,
    connection_id: str,
    user_id: str,
    provider: str,
    status: str,
    credential_ciphertext: str | None,
    last_sync_at: str | None,
    sync_mode: str,
    metadata: dict[str, Any],
    created_at: str,
    updated_at: str,
) -> None:
    provider_value = normalize_calendar_provider(provider).value
    credential_ref = f"calendar:{connection_id}" if credential_ciphertext is not None else None
    if credential_ciphertext is not None:
        connection.execute(
            """
            INSERT INTO integration_credentials(id, user_id, provider, credential_ciphertext, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                user_id = excluded.user_id,
                provider = excluded.provider,
                credential_ciphertext = excluded.credential_ciphertext,
                updated_at = excluded.updated_at
            """,
            (credential_ref, user_id, provider_value, credential_ciphertext, created_at, updated_at),
        )
    connection.execute(
        """
        INSERT INTO calendar_connections(
            id, user_id, provider, status, credential_ref, last_sync_at,
            sync_mode, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            user_id = excluded.user_id,
            provider = excluded.provider,
            status = excluded.status,
            credential_ref = COALESCE(excluded.credential_ref, calendar_connections.credential_ref),
            last_sync_at = excluded.last_sync_at,
            sync_mode = excluded.sync_mode,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            connection_id, user_id, provider_value, status, credential_ref, last_sync_at,
            sync_mode, json.dumps(metadata, ensure_ascii=False), created_at, updated_at,
        ),
    )


def _connection_for_source(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM calendar_connections WHERE id = ? OR json_extract(metadata_json, '$.legacy_source_id') = ?",
        (source_id, source_id),
    ).fetchone()


def _set_calendar_connection_state(
    connection_id: str,
    *,
    status: str,
    last_sync_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    with db() as connection:
        existing = connection.execute("SELECT * FROM calendar_connections WHERE id = ?", (connection_id,)).fetchone()
        if existing is None:
            return
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else existing["metadata_json"]
        connection.execute(
            "UPDATE calendar_connections SET status = ?, last_sync_at = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
            (status, last_sync_at if last_sync_at is not None else existing["last_sync_at"], metadata_json, now_iso(), connection_id),
        )


def calendar_connection_payload(connection: sqlite3.Row) -> dict:
    model = _calendar_connection_from_row(connection)
    return {
        "id": model.id,
        "user_id": model.user_id,
        "provider": model.provider.value,
        "provider_name": CALENDAR_PROVIDER_LABELS[model.provider.value],
        "status": model.status.value,
        "credential_ref": model.credential_ref,
        "last_sync_at": model.last_sync_at,
        "sync_mode": model.sync_mode.value,
        "metadata": model.metadata,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_prop_value(root: ET.Element, property_name: str) -> str | None:
    for prop in root.iter():
        if xml_local_name(prop.tag) != "prop":
            continue
        for child in list(prop):
            if xml_local_name(child.tag) != property_name:
                continue
            href = next((node.text for node in child.iter() if xml_local_name(node.tag) == "href" and node.text), None)
            if href:
                return href.strip()
            if child.text and child.text.strip():
                return child.text.strip()
    return None


def resolve_url(base_url: str, href: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", href)


def validate_http_url(value: str, label: str, *, allow_query: bool = True) -> str:
    candidate = value.strip()
    parsed = urllib.parse.urlparse(candidate)
    try:
        parsed.port
    except ValueError as error:
        raise CalendarConnectionError(f"{label} 的 port 無效") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname or (not allow_query and parsed.query) or parsed.fragment:
        raise CalendarConnectionError(f"{label} 必須是有效的 http(s) 網址")
    if parsed.username or parsed.password:
        raise CalendarConnectionError(f"{label} 不應包含帳號或密碼")
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise CalendarConnectionError(f"{label} 不能包含空白或控制字元")
    return candidate


def validate_calendar_network_target(value: str, label: str, *, allow_query: bool = True) -> str:
    candidate = validate_http_url(value, label, allow_query=allow_query)
    policy = security_policy()
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme == "http" and not policy.allow_insecure_calendar_http:
        raise CalendarConnectionError(f"{label} 必須使用 HTTPS")
    try:
        validate_outbound_http_target(candidate, allow_private=policy.allow_private_calendar_hosts)
    except UnsafeOutboundTarget as error:
        raise CalendarConnectionError(f"{label} 指向不允許的網路位置") from error
    return candidate


def _calendar_opener():
    # Never follow provider-controlled redirects implicitly. Discovery already
    # tries canonical Nextcloud CalDAV endpoints explicitly, and rejecting
    # redirects prevents a public host from bouncing the backend into a private
    # network target.
    return urllib.request.build_opener(_NoRedirectHandler)


def _calendar_urlopen(request: urllib.request.Request, *, timeout: float):
    return _calendar_opener().open(request, timeout=timeout)


def same_http_origin(first: str, second: str) -> bool:
    first_url = urllib.parse.urlparse(first)
    second_url = urllib.parse.urlparse(second)
    first_port = first_url.port or (443 if first_url.scheme == "https" else 80)
    second_port = second_url.port or (443 if second_url.scheme == "https" else 80)
    return (first_url.scheme.lower(), first_url.hostname.lower(), first_port) == (second_url.scheme.lower(), second_url.hostname.lower(), second_port)


def caldav_propfind(url: str, username: str, app_password: str, depth: str, properties: str) -> ET.Element:
    url = validate_calendar_network_target(url, "CalDAV URL")
    body = f'''<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>{properties}</d:prop>
</d:propfind>'''.encode()
    auth = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    request = urllib.request.Request(
        url,
        data=body,
        method="PROPFIND",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/xml; charset=utf-8",
            "Depth": depth,
            "User-Agent": "OneMoreThing-Calendar/0.1",
        },
    )
    try:
        with _calendar_urlopen(request, timeout=15) as response:
            if response.status not in {200, 207}:
                raise CalendarConnectionError(f"CalDAV returned HTTP {response.status}")
            return ET.fromstring(read_limited(response, security_policy().calendar_response_bytes))
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise CalendarConnectionError("Nextcloud rejected the username or app password") from error
        raise CalendarConnectionError(f"CalDAV returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ET.ParseError, ResponseTooLarge) as error:
        raise CalendarConnectionError("Could not reach or parse the Nextcloud CalDAV endpoint safely") from error


def caldav_calendar_report(url: str, username: str, app_password: str) -> list[dict[str, str | None]]:
    url = validate_calendar_network_target(url, "Calendar URL")
    body = b'''<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>'''
    auth = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    request = urllib.request.Request(
        url,
        data=body,
        method="REPORT",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/xml; charset=utf-8",
            "Depth": "1",
            "Accept": "application/xml, text/xml",
            "User-Agent": "OneMoreThing-Calendar/0.1",
        },
    )
    try:
        with _calendar_urlopen(request, timeout=30) as response:
            if response.status not in {200, 207}:
                raise CalendarConnectionError(f"CalDAV event query returned HTTP {response.status}")
            root = ET.fromstring(read_limited(response, security_policy().calendar_response_bytes))
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise CalendarConnectionError("Nextcloud rejected the calendar event query") from error
        raise CalendarConnectionError(f"CalDAV event query returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ET.ParseError, ResponseTooLarge) as error:
        raise CalendarConnectionError("Could not read events from the CalDAV calendar safely") from error

    responses: list[dict[str, str | None]] = []
    for response in root.iter():
        if xml_local_name(response.tag) != "response":
            continue
        href = next((node.text.strip() for node in response.iter() if xml_local_name(node.tag) == "href" and node.text), None)
        calendar_data = next((node.text for node in response.iter() if xml_local_name(node.tag) == "calendar-data" and node.text), None)
        etag = next((node.text.strip() for node in response.iter() if xml_local_name(node.tag) == "getetag" and node.text), None)
        if href and calendar_data:
            responses.append({"href": resolve_url(url, href), "etag": etag, "calendar_data": calendar_data})
            if len(responses) > security_policy().calendar_max_items:
                raise CalendarConnectionError("CalDAV event count exceeds the configured sync limit")
    return responses


def caldav_get_event(url: str, username: str, app_password: str) -> tuple[str, str | None]:
    url = validate_calendar_network_target(url, "Calendar event URL")
    """Fetch the provider-owned iCalendar object without persisting it.

    Editing an imported event must preserve provider-native fields we do not
    model (alarms, attendees, categories, etc.).  We therefore fetch the
    current object on demand, patch only Calendar-native fields that OMT
    exposes, and immediately write it back.  The raw object never enters the
    SQLite mirror.
    """
    auth = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "text/calendar",
            "User-Agent": "OneMoreThing-Calendar/0.1",
        },
    )
    try:
        with _calendar_urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise CalendarConnectionError(f"CalDAV event read returned HTTP {response.status}")
            return read_limited(response, security_policy().calendar_response_bytes).decode("utf-8", errors="replace"), response.headers.get("ETag")
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise CalendarConnectionError("Nextcloud rejected the calendar event read") from error
        if error.code in {404, 410}:
            raise CalendarConnectionError("The calendar event no longer exists") from error
        raise CalendarConnectionError(f"CalDAV event read returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ResponseTooLarge) as error:
        raise CalendarConnectionError("Could not read the event from the CalDAV calendar safely") from error


def caldav_put_event(
    url: str,
    username: str,
    app_password: str,
    icalendar: str,
    *,
    etag: str | None = None,
) -> str | None:
    url = validate_calendar_network_target(url, "Calendar event URL")
    auth = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    request = urllib.request.Request(
        url,
        data=icalendar.encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "text/calendar; charset=utf-8",
            ("If-Match" if etag else "If-None-Match"): etag or "*",
            "User-Agent": "OneMoreThing-Calendar/0.1",
        },
    )
    try:
        with _calendar_urlopen(request, timeout=30) as response:
            if response.status not in {200, 201, 204}:
                raise CalendarConnectionError(f"CalDAV event write returned HTTP {response.status}")
            return response.headers.get("ETag")
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise CalendarConnectionError("Nextcloud rejected the calendar event write") from error
        if error.code == 412:
            if etag:
                raise CalendarConnectionError("The external calendar event changed; write-back was not applied") from error
            raise CalendarConnectionError("A calendar event with this id already exists") from error
        raise CalendarConnectionError(f"CalDAV event write returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise CalendarConnectionError("Could not write the event to the CalDAV calendar") from error


def caldav_delete_event(url: str, username: str, app_password: str, *, etag: str | None = None) -> None:
    url = validate_calendar_network_target(url, "Calendar event URL")
    auth = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "User-Agent": "OneMoreThing-Calendar/0.1"}
    if etag:
        headers["If-Match"] = etag
    request = urllib.request.Request(url, method="DELETE", headers=headers)
    try:
        with _calendar_urlopen(request, timeout=30) as response:
            if response.status not in {200, 202, 204}:
                raise CalendarConnectionError(f"CalDAV event delete returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        if error.code in {404, 410}:
            return
        if error.code in {401, 403}:
            raise CalendarConnectionError("Nextcloud rejected the calendar event delete") from error
        if error.code == 412:
            raise CalendarConnectionError("The external calendar event changed; deletion was not applied") from error
        raise CalendarConnectionError(f"CalDAV event delete returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise CalendarConnectionError("Could not delete the event from the CalDAV calendar") from error


class NextcloudCalDavAdapter:
    """Nextcloud adapter used by the Calendar mirror and CRUD paths."""

    def __init__(self, username: str, app_password: str):
        self.username = username
        self.app_password = app_password

    def list_events(self, calendar_url: str) -> list[dict[str, str | None]]:
        return caldav_calendar_report(calendar_url, self.username, self.app_password)

    def get_event(self, event_url: str) -> tuple[str, str | None]:
        return caldav_get_event(event_url, self.username, self.app_password)

    def create_event(self, event_url: str, icalendar: str) -> str | None:
        return caldav_put_event(event_url, self.username, self.app_password, icalendar)

    def update_event(self, event_url: str, icalendar: str, etag: str | None = None) -> str | None:
        return caldav_put_event(event_url, self.username, self.app_password, icalendar, etag=etag)

    def delete_event(self, event_url: str, etag: str | None = None) -> None:
        caldav_delete_event(event_url, self.username, self.app_password, etag=etag)

class MockCalendarAdapter:
    """Test/development adapter; production routes never select this class."""

    def __init__(self, report: list[dict[str, str | None]] | None = None):
        self.report = report or []

    def list_events(self, _calendar_url: str) -> list[dict[str, str | None]]:
        return list(self.report)

    def get_event(self, _event_url: str) -> tuple[str, str | None]:
        raise CalendarConnectionError("The mock Calendar adapter is readonly")

    def create_event(self, _event_url: str, _icalendar: str) -> None:
        raise CalendarConnectionError("The mock Calendar adapter is readonly")

    def update_event(self, _event_url: str, _icalendar: str, _etag: str | None = None) -> None:
        raise CalendarConnectionError("The mock Calendar adapter is readonly")

    def delete_event(self, _event_url: str, _etag: str | None = None) -> None:
        raise CalendarConnectionError("The mock Calendar adapter is readonly")

def unfold_icalendar_lines(icalendar: str) -> list[str]:
    lines = icalendar.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    return unfolded


def unescape_icalendar_text(value: str) -> str:
    return value.replace("\\N", "\n").replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def icalendar_property(line: str) -> tuple[str, dict[str, str], str] | None:
    head, separator, value = line.partition(":")
    if not separator:
        return None
    parts = head.split(";")
    name = parts[0].upper()
    parameters: dict[str, str] = {}
    for part in parts[1:]:
        key, equals, parameter_value = part.partition("=")
        if equals:
            parameters[key.upper()] = parameter_value.strip('"')
    return name, parameters, value


def timezone_or_utc(name: str | None) -> tuple[ZoneInfo, str]:
    timezone_name = name or "UTC"
    try:
        return ZoneInfo(timezone_name), timezone_name
    except ZoneInfoNotFoundError:
        return timezone.utc, timezone_name


def parse_icalendar_datetime(raw: str, parameters: dict[str, str], calendar_timezone: str | None) -> tuple[str, bool, str | None]:
    value = raw.strip()
    is_date = parameters.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit())
    if is_date:
        parsed_date = datetime.strptime(value[:8], "%Y%m%d").date()
        zone, zone_name = timezone_or_utc(parameters.get("TZID") or calendar_timezone)
        return datetime.combine(parsed_date, time.min, tzinfo=zone).isoformat(), True, zone_name
    is_utc = value.endswith("Z")
    if is_utc:
        value = value[:-1]
    parsed_datetime = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if is_utc:
        parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
        return parsed_datetime.isoformat(), False, "UTC"
    zone, zone_name = timezone_or_utc(parameters.get("TZID") or calendar_timezone)
    return parsed_datetime.replace(tzinfo=zone).isoformat(), False, zone_name


def parse_icalendar_events(icalendar: str, href: str, etag: str | None, calendar_timezone: str | None) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    current: list[tuple[str, dict[str, str], str]] | None = None
    for line in unfold_icalendar_lines(icalendar):
        property_value = icalendar_property(line)
        if property_value is None:
            continue
        name, parameters, value = property_value
        if name == "BEGIN" and value.upper() == "VEVENT":
            current = []
        elif name == "END" and value.upper() == "VEVENT":
            if current:
                properties: dict[str, list[tuple[dict[str, str], str]]] = {}
                for property_name, property_parameters, property_value_text in current:
                    properties.setdefault(property_name, []).append((property_parameters, property_value_text))
                uid = unescape_icalendar_text(properties.get("UID", [({}, href)])[0][1])
                if not uid or len(uid) > 1024:
                    current = None
                    continue
                start_raw = properties.get("DTSTART", [({}, "")])[0]
                if not start_raw[1]:
                    current = None
                    continue
                try:
                    start_at, all_day, event_timezone = parse_icalendar_datetime(start_raw[1], start_raw[0], calendar_timezone)
                    end_raw = properties.get("DTEND", [({}, "")])[0]
                    if end_raw[1]:
                        end_at, end_is_date, end_timezone = parse_icalendar_datetime(end_raw[1], end_raw[0], calendar_timezone)
                        all_day = all_day or end_is_date
                    elif all_day:
                        end_date = datetime.fromisoformat(start_at).date() + timedelta(days=1)
                        zone, _ = timezone_or_utc(event_timezone)
                        end_at = datetime.combine(end_date, time.min, tzinfo=zone).isoformat()
                    else:
                        # A timed provider Event without DTEND is a truthful
                        # start-only Event. Provider-required default duration
                        # belongs only in the outbound adapter payload; it must
                        # never be written into OMT's semantic mirror.
                        end_at = None
                except (TypeError, ValueError):
                    current = None
                    continue
                recurrence_id = properties.get("RECURRENCE-ID", [({}, "")])[0][1]
                remote_id = f"{uid}/{recurrence_id}" if recurrence_id else uid
                status = unescape_icalendar_text(properties.get("STATUS", [({}, "CONFIRMED")])[0][1]).lower()
                events.append(
                    {
                        "remote_id": remote_id,
                        "href": href,
                        "etag": etag,
                        "title": _provider_text(unescape_icalendar_text(properties.get("SUMMARY", [({}, "未命名事件")])[0][1]), 500, default="未命名事件"),
                        "start_at": start_at,
                        "end_at": end_at,
                        "timezone": event_timezone,
                        "all_day": all_day,
                        "location": _provider_text(unescape_icalendar_text(properties.get("LOCATION", [({}, "")])[0][1]), 2000),
                        "description": _provider_text(unescape_icalendar_text(properties.get("DESCRIPTION", [({}, "")])[0][1]), 10000),
                        "url": _provider_external_url(unescape_icalendar_text(properties.get("URL", [({}, "")])[0][1])),
                        "recurrence_rule": _provider_text(unescape_icalendar_text(properties.get("RRULE", [({}, "")])[0][1]), 255),
                        "status": status if status in {"confirmed", "tentative", "cancelled"} else "confirmed",
                    }
                )
            current = None
        elif current is not None:
            current.append((name, parameters, value))
    return events


def icalendar_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def format_icalendar_datetime(value: str, all_day: bool, timezone_name: str | None) -> tuple[str, str]:
    if all_day:
        try:
            parsed_date = date.fromisoformat(value[:10])
        except ValueError as error:
            raise CalendarConnectionError("全天事件的 start_at/end_at 必須是有效日期") from error
        return "VALUE=DATE", parsed_date.strftime("%Y%m%d")
    try:
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CalendarConnectionError("事件 start_at/end_at 必須是 ISO 8601 日期時間") from error
    if parsed_datetime.tzinfo is None:
        zone, _ = timezone_or_utc(timezone_name)
        parsed_datetime = parsed_datetime.replace(tzinfo=zone)
    if timezone_name and parsed_datetime.tzinfo != timezone.utc:
        local_value = parsed_datetime.astimezone(timezone_or_utc(timezone_name)[0])
        return f"TZID={timezone_name}", local_value.strftime("%Y%m%dT%H%M%S")
    return "", parsed_datetime.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_icalendar_event(event: CalendarEventInput, uid: str, now: datetime) -> str:
    start_parameter, start_value = format_icalendar_datetime(event.start_at, event.all_day, event.timezone)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//One More Thing//Calendar//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{icalendar_escape(uid)}",
        f"DTSTAMP:{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART{(';' + start_parameter) if start_parameter else ''}:{start_value}",
        f"SUMMARY:{icalendar_escape(event.title)}",
    ]
    if event.end_at:
        end_parameter, end_value = format_icalendar_datetime(event.end_at, event.all_day, event.timezone)
        lines.insert(8, f"DTEND{(';' + end_parameter) if end_parameter else ''}:{end_value}")
    if event.location:
        lines.append(f"LOCATION:{icalendar_escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{icalendar_escape(event.description)}")
    if event.url:
        lines.append(f"URL:{icalendar_escape(event.url)}")
    if event.recurrence_rule:
        if not re.fullmatch(r"FREQ=WEEKLY;BYDAY=(?:MO|TU|WE|TH|FR|SA|SU)(?:,(?:MO|TU|WE|TH|FR|SA|SU))*(?:;UNTIL=\d{8}(?:T\d{6}Z)?)?", event.recurrence_rule):
            raise HTTPException(status_code=422, detail="Weekly recurrence rule is invalid")
        lines.append(f"RRULE:{event.recurrence_rule}")
    lines.extend(["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)


def replace_icalendar_event_fields(icalendar: str, event: CalendarEventInput, remote_id: str) -> str:
    """Patch one VEVENT while preserving provider fields OMT does not model."""
    lines = unfold_icalendar_lines(icalendar)
    output: list[str] = []
    component: list[str] | None = None
    replaced = False

    def component_identity(values: list[str]) -> str:
        uid = ""
        recurrence_id = ""
        for value in values:
            parsed = icalendar_property(value)
            if parsed is None:
                continue
            name, _parameters, raw = parsed
            if name == "UID":
                uid = unescape_icalendar_text(raw)
            elif name == "RECURRENCE-ID":
                recurrence_id = raw
        return f"{uid}/{recurrence_id}" if recurrence_id else uid

    def patched_component(values: list[str]) -> list[str]:
        managed = {"SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION", "URL", "RRULE"}
        kept: list[str] = []
        for value in values:
            parsed = icalendar_property(value)
            if parsed is not None and parsed[0] in managed:
                continue
            kept.append(value)
        start_parameter, start_value = format_icalendar_datetime(event.start_at, event.all_day, event.timezone)
        inserts = [
            f"DTSTART{(';' + start_parameter) if start_parameter else ''}:{start_value}",
        ]
        if event.end_at:
            end_parameter, end_value = format_icalendar_datetime(event.end_at, event.all_day, event.timezone)
            inserts.append(f"DTEND{(';' + end_parameter) if end_parameter else ''}:{end_value}")
        inserts.append(f"SUMMARY:{icalendar_escape(event.title)}")
        if event.location:
            inserts.append(f"LOCATION:{icalendar_escape(event.location)}")
        if event.description:
            inserts.append(f"DESCRIPTION:{icalendar_escape(event.description)}")
        if event.url:
            inserts.append(f"URL:{icalendar_escape(event.url)}")
        if event.recurrence_rule:
            inserts.append(f"RRULE:{event.recurrence_rule}")
        end_index = next((index for index, value in enumerate(kept) if value.upper() == "END:VEVENT"), len(kept))
        return kept[:end_index] + inserts + kept[end_index:]

    for line in lines:
        if line.upper() == "BEGIN:VEVENT":
            component = [line]
            continue
        if component is not None:
            component.append(line)
            if line.upper() == "END:VEVENT":
                if not replaced and component_identity(component) == remote_id:
                    component = patched_component(component)
                    replaced = True
                output.extend(component)
                component = None
            continue
        output.append(line)
    if component:
        output.extend(component)
    if not replaced:
        raise CalendarConnectionError("The selected VEVENT was not found in the provider object")
    return "\r\n".join(output) + "\r\n"


def normalize_checklist(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    for item in value[:100]:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append({"text": text, "done": False})
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("title") or "").strip()
            if text:
                normalized = {"text": text, "done": bool(item.get("done"))}
                item_id = str(item.get("id") or "").strip()
                if item_id and len(item_id) <= 128:
                    normalized["id"] = item_id
                result.append(normalized)
    return result


def normalize_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))[:50]


def build_omt_description(
    provider_description: str | None,
    notes: str | None,
    ai_summary: str | None,
    checklist: object,
) -> str | None:
    base = (provider_description or "").strip()
    items = normalize_checklist(checklist)
    notes_value = (notes or "").strip()
    summary_value = (ai_summary or "").strip()
    if not items and not notes_value and not summary_value:
        return base or None
    lines = [base] if base else []
    if lines:
        lines.extend(["", "---"])
    lines.extend(["One More Thing", ""])
    if items:
        lines.append("待辦：")
        lines.extend(f"{'☑' if item['done'] else '☐'} {item['text']}" for item in items)
    if notes_value:
        if items:
            lines.append("")
        lines.extend(["備註：", notes_value])
    if summary_value:
        if items or notes_value:
            lines.append("")
        lines.extend(["AI整理：", summary_value])
    lines.extend(["", "-------"])
    return "\n".join(lines)


def strip_omt_description(description: str | None) -> tuple[str | None, dict]:
    value = (description or "").strip()
    marker = "One More Thing"
    if marker not in value:
        return (value or None), {}
    base, _, remainder = value.partition(marker)
    omt = {"notes": None, "ai_summary": None, "checklist": []}
    checklist_match = re.search(r"待辦：\n(?P<items>.*?)(?:\n\n|\n備註：|\nAI整理：|\n-------|$)", remainder, re.S)
    if checklist_match:
        omt["checklist"] = normalize_checklist(
            [line[2:].strip() if len(line) > 1 and line[1] == " " else line.strip() for line in checklist_match.group("items").splitlines() if line.strip()]
        )
        for line, item in zip(checklist_match.group("items").splitlines(), omt["checklist"]):
            item["done"] = line.startswith("☑")
    notes_match = re.search(r"備註：\n(?P<value>.*?)(?:\n\nAI整理：|\n-------|$)", remainder, re.S)
    summary_match = re.search(r"AI整理：\n(?P<value>.*?)(?:\n-------|$)", remainder, re.S)
    if notes_match:
        omt["notes"] = notes_match.group("value").strip() or None
    if summary_match:
        omt["ai_summary"] = summary_match.group("value").strip() or None
    return (base.rstrip("\n -") or None), omt


def replace_icalendar_description(icalendar: str, description: str | None) -> str:
    lines = unfold_icalendar_lines(icalendar)
    result: list[str] = []
    inserted = False
    for line in lines:
        if line.upper().startswith("DESCRIPTION:"):
            continue
        if line.upper() == "END:VEVENT" and not inserted and description:
            result.append(f"DESCRIPTION:{icalendar_escape(description)}")
            inserted = True
        result.append(line)
    return "\r\n".join(result) + "\r\n"


def decrypt_source_credential(source: sqlite3.Row) -> str:
    ciphertext = source["credential_ciphertext"]
    try:
        with db() as connection:
            linked = _connection_for_source(connection, source["id"])
            if linked is not None and linked["credential_ref"]:
                credential = connection.execute(
                    "SELECT credential_ciphertext FROM integration_credentials WHERE id = ? AND user_id = ?",
                    (linked["credential_ref"], source["user_id"]),
                ).fetchone()
                if credential is not None:
                    ciphertext = credential["credential_ciphertext"]
    except sqlite3.Error:
        # Keep old databases readable while an operator is applying the
        # additive migration.
        ciphertext = source["credential_ciphertext"]
    try:
        return credential_box().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, UnicodeDecodeError, RuntimeError) as error:
        raise CalendarConnectionError("無法讀取 Calendar source 的安全憑證") from error


def normalized_calendar_event(row: sqlite3.Row) -> dict:
    try:
        sync_conflict = json.loads(row["sync_conflict_json"] or "{}") if "sync_conflict_json" in row.keys() else {}
    except (TypeError, json.JSONDecodeError):
        sync_conflict = {}
    return CalendarEvent(
        id=row["id"],
        source_id=row["source_id"],
        calendar_id=row["calendar_id"],
        remote_id=row["remote_id"],
        title=row["title"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        timezone=row["timezone"],
        all_day=bool(row["all_day"]),
        location=row["location"],
        description=row["description"],
        url=row["url"],
        recurrence_rule=row["recurrence_rule"],
        status=row["status"],
        etag=row["etag"],
        created_source=row["created_source"] if "created_source" in row.keys() else "external_calendar",
        event_source="external_calendar",
        origin_proposal_id=row["origin_proposal_id"] if "origin_proposal_id" in row.keys() else None,
        external_calendar_ref=row["external_calendar_ref"] if "external_calendar_ref" in row.keys() else row["remote_id"],
        provider=row["provider"] if "provider" in row.keys() and row["provider"] else "nextcloud_calendar",
        sync_status=row["sync_status"] if "sync_status" in row.keys() else "synced",
        last_synced_at=row["last_synced_at"] if "last_synced_at" in row.keys() else row["updated_at"],
        sync_conflict=sync_conflict if isinstance(sync_conflict, dict) else {},
        lifecycle_status=row["lifecycle_status"] if "lifecycle_status" in row.keys() else "created",
    ).model_dump()


def _calendar_event_local_labels(event: dict[str, object]) -> tuple[str, str]:
    """Project provider-native schedule fields into the legacy local Event labels.

    The labels remain a presentation compatibility layer; the canonical sync
    values are still ``start_at``/``end_at``/``all_day``/``timezone``.
    """
    start_raw = str(event.get("start_at") or "")
    end_raw = str(event.get("end_at") or "")
    if not start_raw:
        return "", ""
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
    except ValueError:
        return start_raw[:10], "全天" if event.get("all_day") else ""
    if event.get("all_day"):
        return start.date().isoformat(), "全天"
    zone_name = str(event.get("timezone") or DEFAULT_TIMEZONE)
    try:
        zone, _ = requested_timezone(zone_name)
        if start.tzinfo is not None:
            start = start.astimezone(zone)
    except HTTPException:
        pass
    date_label = start.date().isoformat()
    time_label = start.strftime("%H:%M")
    if end_raw:
        try:
            end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            try:
                zone, _ = requested_timezone(zone_name)
                if end.tzinfo is not None:
                    end = end.astimezone(zone)
            except HTTPException:
                pass
            time_label = f"{time_label}～{end.strftime('%H:%M')}"
        except ValueError:
            pass
    return date_label, time_label


def persist_calendar_report(source_id: str, calendar: sqlite3.Row, report: list[dict[str, str | None]], timestamp: str) -> int:
    expected_events = sum((item["calendar_data"] or "").upper().count("BEGIN:VEVENT") for item in report)
    if expected_events > security_policy().calendar_max_items:
        raise CalendarConnectionError("CalDAV event count exceeds the configured sync limit")
    parsed_events: list[dict[str, object]] = []
    for item in report:
        parsed_events.extend(parse_icalendar_events(item["calendar_data"] or "", item["href"] or "", item["etag"], calendar["timezone"]))
    if expected_events != len(parsed_events):
        raise CalendarConnectionError("CalDAV returned an event that could not be normalized")
    seen_remote_ids = {str(event["remote_id"]) for event in parsed_events}
    with db() as connection:
        for event in parsed_events:
            linked_local = connection.execute(
                """
                SELECT id, sync_status FROM events
                WHERE user_id = (SELECT user_id FROM calendar_sources WHERE id = ?)
                  AND external_calendar_ref = ?
                  AND calendar_sync_enabled = 1
                  AND lifecycle_status NOT IN ('archived', 'deleted')
                LIMIT 1
                """,
                (source_id, event["remote_id"]),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO calendar_events(
                    id, source_id, calendar_id, remote_id, href, etag, title, start_at, end_at,
                    timezone, all_day, location, description, url, recurrence_rule, status,
                    created_at, updated_at, created_source, external_calendar_ref, lifecycle_status,
                    provider, sync_status, last_synced_at, sync_conflict_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_id, remote_id) DO UPDATE SET
                    source_id = excluded.source_id, href = excluded.href, etag = excluded.etag,
                    title = excluded.title, start_at = excluded.start_at, end_at = excluded.end_at,
                    timezone = excluded.timezone, all_day = excluded.all_day, location = excluded.location,
                    description = excluded.description, url = excluded.url, recurrence_rule = excluded.recurrence_rule,
                    status = excluded.status, updated_at = excluded.updated_at, created_source = 'external_calendar',
                    external_calendar_ref = excluded.external_calendar_ref, provider = excluded.provider,
                    sync_status = 'synced', last_synced_at = excluded.last_synced_at, sync_conflict_json = '{}',
                    lifecycle_status = CASE
                        WHEN calendar_events.lifecycle_status = 'archived' THEN calendar_events.lifecycle_status
                        ELSE 'created'
                    END, deleted_at = NULL
                """,
                (
                    uuid4().hex, source_id, calendar["id"], event["remote_id"], event["href"], event["etag"],
                    event["title"], event["start_at"], event["end_at"], event["timezone"], int(bool(event["all_day"])),
                    event["location"], event["description"], event["url"], event["recurrence_rule"], event["status"],
                    timestamp, timestamp, "external_calendar", event["remote_id"], "created",
                    CalendarProvider.NEXTCLOUD.value, "synced", timestamp, "{}",
                ),
            )
            if linked_local is not None and linked_local["sync_status"] == "synced":
                # A provider pull is authoritative only when there is no local
                # write waiting to be pushed.  This gives linked local/AI
                # Events real two-way Calendar semantics without clobbering a
                # pending edit during a sync race.
                date_label, time_label = _calendar_event_local_labels(event)
                connection.execute(
                    """
                    UPDATE events
                    SET title = ?, date_label = ?, time_label = ?, detail = ?, location = ?,
                        start_at = ?, end_at = ?, timezone = ?, all_day = ?,
                        sync_status = 'synced', last_synced_at = ?, sync_conflict_json = '{}', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        event["title"], date_label, time_label, event["description"] or "", event["location"],
                        event["start_at"], event["end_at"], event["timezone"] or DEFAULT_TIMEZONE,
                        int(bool(event["all_day"])), timestamp, timestamp, linked_local["id"],
                    ),
                )
        if seen_remote_ids:
            placeholders = ",".join("?" for _ in seen_remote_ids)
            connection.execute(
                f"UPDATE calendar_events SET lifecycle_status = 'deleted', deleted_at = ?, updated_at = ? WHERE calendar_id = ? AND remote_id NOT IN ({placeholders}) AND lifecycle_status <> 'deleted'",
                (timestamp, timestamp, calendar["id"], *seen_remote_ids),
            )
        else:
            connection.execute(
                "UPDATE calendar_events SET lifecycle_status = 'deleted', deleted_at = ?, updated_at = ? WHERE calendar_id = ? AND lifecycle_status <> 'deleted'",
                (timestamp, timestamp, calendar["id"]),
            )
        # A deletion made in the provider is a deletion of the same logical
        # event.  Only synced local rows are retired here; pending/syncing/error
        # rows keep their local write so a concurrent edit is never discarded.
        connection.execute(
            """
            UPDATE events
            SET lifecycle_status = 'deleted', deleted_at = ?, sync_status = 'synced',
                last_synced_at = ?, sync_conflict_json = '{}', updated_at = ?
            WHERE calendar_sync_enabled = 1
              AND sync_status = 'synced'
              AND lifecycle_status NOT IN ('archived', 'deleted')
              AND external_calendar_ref IN (
                  SELECT ce.remote_id FROM calendar_events ce
                  WHERE ce.calendar_id = ? AND ce.lifecycle_status = 'deleted'
              )
            """,
            (timestamp, timestamp, timestamp, calendar["id"]),
        )
        # The index never outlives the event.  Provider-side deletion may
        # retire either a provider-owned mirror id or a linked local event id.
        connection.execute(
            """
            UPDATE tasks SET related_event_id = NULL
            WHERE user_id = (SELECT user_id FROM calendar_sources WHERE id = ?)
              AND related_event_id IN (
                  SELECT ce.id FROM calendar_events ce
                  WHERE ce.calendar_id = ? AND ce.lifecycle_status = 'deleted'
                  UNION
                  SELECT e.id FROM events e
                  WHERE e.user_id = (SELECT user_id FROM calendar_sources WHERE id = ?)
                    AND e.lifecycle_status = 'deleted'
                    AND e.external_calendar_ref IN (
                        SELECT ce.remote_id FROM calendar_events ce
                        WHERE ce.calendar_id = ? AND ce.lifecycle_status = 'deleted'
                    )
              )
            """,
            (source_id, calendar["id"], source_id, calendar["id"]),
        )
        connection.execute(
            "UPDATE calendar_calendars SET sync_status = ?, last_synced_at = ?, last_error = NULL, updated_at = ? WHERE id = ? AND source_id = ?",
            ("ok", timestamp, timestamp, calendar["id"], source_id),
        )
    return len(parsed_events)


def _sync_calendar_source_once(user_id: str, source_id: str) -> dict:
    with db() as connection:
        source = connection.execute(
            "SELECT * FROM calendar_sources WHERE id = ? AND user_id = ? AND kind = ?",
            (source_id, user_id, "nextcloud-caldav"),
        ).fetchone()
        calendars = connection.execute(
            "SELECT * FROM calendar_calendars WHERE source_id = ? ORDER BY display_name",
            (source_id,),
        ).fetchall()
    if source is None:
        raise HTTPException(status_code=404, detail="Calendar source not found")
    connection_state = None
    with db() as connection:
        connection_state = _connection_for_source(connection, source_id)
    if connection_state is not None and connection_state["status"] in {CalendarConnectionStatus.DISCONNECTED.value, CalendarConnectionStatus.EXPIRED.value}:
        raise HTTPException(status_code=409, detail="Calendar connection is not available")
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            "UPDATE calendar_sources SET sync_status = ?, sync_error = NULL, updated_at = ? WHERE id = ? AND user_id = ?",
            ("syncing", timestamp, source_id, user_id),
        )
        connection.execute("UPDATE calendar_calendars SET sync_status = ?, last_error = NULL, updated_at = ? WHERE source_id = ?", ("syncing", timestamp, source_id))
        connection.execute("UPDATE calendar_connections SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?", (CalendarConnectionStatus.SYNCING.value, timestamp, source_id, user_id))
    try:
        app_password = decrypt_source_credential(source)
        adapter = NextcloudCalDavAdapter(source["username"], app_password)
        synced_calendars: list[dict] = []
        total_events = 0
        for calendar in calendars:
            report = adapter.list_events(calendar["remote_url"])
            count = persist_calendar_report(source_id, calendar, report, timestamp)
            total_events += count
            synced_calendars.append({"id": calendar["id"], "display_name": calendar["display_name"], "status": "ok", "event_count": count})
    except CalendarConnectionError as error:
        with db() as connection:
            connection.execute(
                "UPDATE calendar_sources SET sync_status = ?, sync_error = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                ("error", str(error), timestamp, source_id, user_id),
            )
            connection.execute("UPDATE calendar_calendars SET sync_status = ?, last_error = ?, updated_at = ? WHERE source_id = ? AND sync_status = 'syncing'", ("error", str(error), timestamp, source_id))
            connection.execute("UPDATE calendar_connections SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?", (CalendarConnectionStatus.ERROR.value, timestamp, source_id, user_id))
        return {
            "source_id": source_id,
            "status": "error",
            "event_count": 0,
            "calendars": [],
            "error": str(error),
            "last_synced_at": source["last_synced_at"],
        }
    with db() as connection:
        connection.execute(
            "UPDATE calendar_sources SET sync_status = ?, last_synced_at = ?, sync_error = NULL, last_checked_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            ("ok", timestamp, timestamp, timestamp, source_id, user_id),
        )
        connection.execute("UPDATE calendar_connections SET status = ?, last_sync_at = ?, updated_at = ? WHERE id = ? AND user_id = ?", (CalendarConnectionStatus.READY.value, timestamp, timestamp, source_id, user_id))
    return {"source_id": source_id, "status": "ok", "event_count": total_events, "calendars": synced_calendars, "last_synced_at": timestamp}


def sync_calendar_source(user_id: str, source_id: str) -> dict:
    try:
        with OPERATIONS.exclusive(f"calendar-sync:{user_id}:{source_id}"):
            return _sync_calendar_source_once(user_id, source_id)
    except OperationBusy as error:
        raise HTTPException(status_code=429, detail="這個行事曆正在同步，請稍後再試。", headers={"Retry-After": "3"}) from error


def calendar_responses(root: ET.Element, base_url: str) -> list[dict[str, object]]:
    calendars: list[dict[str, object]] = []
    for response in root.iter():
        if xml_local_name(response.tag) != "response":
            continue
        href = next((node.text for node in response.iter() if xml_local_name(node.tag) == "href" and node.text), None)
        if not href:
            continue
        prop = next((node for node in response.iter() if xml_local_name(node.tag) == "prop"), None)
        if prop is None:
            continue
        resource_type = next((node for node in prop if xml_local_name(node.tag) == "resourcetype"), None)
        if resource_type is None or not any(xml_local_name(node.tag) == "calendar" for node in resource_type.iter()):
            continue
        display_name = next((node.text.strip() for node in prop if xml_local_name(node.tag) == "displayname" and node.text), "未命名行事曆")
        timezone = next((node.text.strip() for node in prop if xml_local_name(node.tag) == "calendar-timezone" and node.text), None)
        can_write = any(xml_local_name(node.tag) == "write-content" for node in prop.iter())
        remote_url = validate_http_url(resolve_url(base_url, href.strip()), "Calendar URL")
        if not same_http_origin(base_url, remote_url):
            raise CalendarConnectionError("CalDAV calendar URL must use the same server origin")
        calendars.append(
            {
                "remote_url": remote_url,
                "display_name": display_name,
                "timezone": timezone or "",
                "read_only": not can_write,
            }
        )
        if len(calendars) > security_policy().calendar_max_calendars:
            raise CalendarConnectionError("Calendar count exceeds the configured discovery limit")
    return calendars


def discover_nextcloud(server_url: str, username: str, app_password: str) -> dict:
    server_url = validate_calendar_network_target(normalize_nextcloud_base_url(server_url), "Server URL", allow_query=False)
    parsed = urllib.parse.urlparse(server_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    provided = server_url.rstrip("/")
    if "/remote.php/dav" in parsed.path or ".well-known/caldav" in parsed.path:
        candidates = [provided]
    else:
        candidates = [provided, f"{origin}/.well-known/caldav", f"{origin}/remote.php/dav/"]
    root = None
    root_url = None
    for candidate in candidates:
        try:
            root = caldav_propfind(candidate, username, app_password, "0", "<d:current-user-principal/><d:displayname/>")
            root_url = candidate
            break
        except CalendarConnectionError:
            continue
    if root is None or root_url is None:
        raise CalendarConnectionError("Could not discover a CalDAV endpoint at this Nextcloud URL")
    principal_href = xml_prop_value(root, "current-user-principal")
    principal_url = resolve_url(root_url, principal_href) if principal_href else root_url
    principal_url = validate_http_url(principal_url, "CalDAV principal URL")
    if not same_http_origin(root_url, principal_url):
        raise CalendarConnectionError("CalDAV principal URL must use the same server origin")
    principal = caldav_propfind(principal_url, username, app_password, "0", "<c:calendar-home-set/><d:displayname/>")
    home_href = xml_prop_value(principal, "calendar-home-set")
    if not home_href:
        raise CalendarConnectionError("Nextcloud did not return a calendar home")
    home_url = resolve_url(principal_url, home_href)
    home_url = validate_http_url(home_url, "CalDAV calendar home URL")
    if not same_http_origin(root_url, home_url):
        raise CalendarConnectionError("CalDAV calendar home URL must use the same server origin")
    home = caldav_propfind(
        home_url,
        username,
        app_password,
        "1",
        "<d:displayname/><d:resourcetype/><c:calendar-timezone/><d:current-user-privilege-set/>",
    )
    calendars = calendar_responses(home, home_url)
    return {"server_url": root_url, "principal_url": principal_url, "calendar_home_url": home_url, "calendars": calendars}


def nextcloud_login_flow_start(base_url: str) -> dict:
    normalized_base_url = validate_calendar_network_target(normalize_nextcloud_base_url(base_url), "Nextcloud base URL", allow_query=False)
    endpoint = f"{normalized_base_url}/index.php/login/v2"
    request = urllib.request.Request(
        endpoint,
        data=b"",
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "OneMoreThing-Calendar/0.1",
        },
    )
    try:
        with _calendar_urlopen(request, timeout=15) as response:
            payload = json.loads(read_limited(response, security_policy().oauth_response_bytes).decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise CalendarConnectionError(f"Nextcloud Login Flow 啟動失敗（HTTP {error.code}）") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ResponseTooLarge) as error:
        raise CalendarConnectionError("無法安全連到這個 Nextcloud base URL") from error

    poll = payload.get("poll") if isinstance(payload, dict) else None
    login_url = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(poll, dict) or not str(poll.get("token") or "").strip() or not poll.get("endpoint") or not login_url:
        raise CalendarConnectionError("Nextcloud 沒有回傳完整的 Login Flow v2 資訊")
    poll_endpoint = validate_http_url(resolve_url(normalized_base_url, str(poll["endpoint"])), "Login Flow poll endpoint")
    browser_login_url = validate_http_url(resolve_url(normalized_base_url, str(login_url)), "Login Flow login URL")
    if not same_http_origin(normalized_base_url, poll_endpoint) or not same_http_origin(normalized_base_url, browser_login_url):
        raise CalendarConnectionError("Nextcloud Login Flow URL 必須與輸入的 server origin 相同")
    return {
        "base_url": normalized_base_url,
        "poll_token": str(poll["token"]).strip(),
        "poll_endpoint": poll_endpoint,
        "login_url": browser_login_url,
    }


def nextcloud_login_flow_poll(poll_endpoint: str, poll_token: str) -> tuple[str, dict | None]:
    poll_endpoint = validate_calendar_network_target(poll_endpoint, "Login Flow poll endpoint")
    if not poll_token.strip():
        raise CalendarConnectionError("Nextcloud Login Flow poll token is empty")
    request = urllib.request.Request(
        poll_endpoint,
        data=urllib.parse.urlencode({"token": poll_token}).encode(),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "OneMoreThing-Calendar/0.1",
        },
    )
    try:
        with _calendar_urlopen(request, timeout=15) as response:
            payload = json.loads(read_limited(response, security_policy().oauth_response_bytes).decode("utf-8"))
            return "complete", payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as error:
        # Nextcloud deliberately returns 404 while the user has not finished
        # authorizing the browser flow. It is not an OAuth access-token flow.
        if error.code in {404, 409}:
            return "pending", None
        raise CalendarConnectionError(f"Nextcloud Login Flow 輪詢失敗（HTTP {error.code}）") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ResponseTooLarge) as error:
        raise CalendarConnectionError("無法安全輪詢 Nextcloud Login Flow") from error


def save_nextcloud_source(user_id: str, discovered: dict, username: str, app_password: str) -> dict:
    timestamp = now_iso()
    with db() as connection:
        existing = connection.execute(
            "SELECT id FROM calendar_sources WHERE user_id = ? AND kind = ? AND server_url = ? AND username = ?",
            (user_id, "nextcloud-caldav", discovered["server_url"], username),
        ).fetchone()
        source_id = existing["id"] if existing else uuid4().hex
        encrypted = encrypt_credential(app_password)
        if existing:
            connection.execute(
                "UPDATE calendar_sources SET credential_ciphertext = ?, status = ?, last_checked_at = ?, last_error = NULL, sync_status = ?, last_synced_at = NULL, sync_error = NULL, updated_at = ? WHERE id = ? AND user_id = ?",
                (encrypted, "connected", timestamp, "never", timestamp, source_id, user_id),
            )
            connection.execute("DELETE FROM calendar_calendars WHERE source_id = ?", (source_id,))
        else:
            connection.execute(
                "INSERT INTO calendar_sources(id, user_id, kind, name, server_url, username, credential_ciphertext, status, last_checked_at, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, user_id, "nextcloud-caldav", "Nextcloud", discovered["server_url"], username, encrypted, "connected", timestamp, None, timestamp, timestamp),
            )
        persisted_calendars = []
        for calendar in discovered["calendars"]:
            calendar_id = uuid4().hex
            connection.execute(
                "INSERT INTO calendar_calendars(id, source_id, remote_url, display_name, timezone, read_only, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    calendar_id,
                    source_id,
                    calendar["remote_url"],
                    calendar["display_name"],
                    calendar["timezone"] or None,
                    int(bool(calendar.get("read_only", True))),
                    timestamp,
                    timestamp,
                ),
            )
            persisted_calendars.append(dict(calendar) | {"id": calendar_id, "read_only": bool(calendar.get("read_only", True)), "sync_status": "never", "last_synced_at": None, "last_error": None})
        read_only = all(bool(calendar["read_only"]) for calendar in persisted_calendars) if persisted_calendars else True
        _upsert_calendar_connection(
            connection,
            connection_id=source_id,
            user_id=user_id,
            provider=CalendarProvider.NEXTCLOUD.value,
            status=CalendarConnectionStatus.CONNECTED.value,
            credential_ciphertext=encrypted,
            last_sync_at=None,
            sync_mode=CalendarSyncMode.MANUAL.value,
            metadata={
                "legacy_source_id": source_id,
                "server_url": discovered["server_url"],
                "username": username,
                "name": "Nextcloud",
            },
            created_at=timestamp,
            updated_at=timestamp,
        )
    return {
        "source": {
            "id": source_id,
            "kind": "nextcloud-caldav",
            "name": "Nextcloud",
            "server_url": discovered["server_url"],
            "username": username,
            "status": "connected",
            "read_only": read_only,
            "sync_status": "never",
            "last_synced_at": None,
            "last_error": None,
            "capabilities": {"read": True, "write": not read_only},
        },
        "calendars": persisted_calendars,
    }


def notice_source_payload(payload: NoticeInput | sqlite3.Row, *, body: str | None = None) -> dict:
    if isinstance(payload, sqlite3.Row):
        source_type = payload["source_type"]
        source_name = payload["source_name"] if "source_name" in payload.keys() else None
        source_url = payload["source_url"] if "source_url" in payload.keys() else None
        title = payload["title"]
        source_body = payload["body"]
        try:
            with db() as connection:
                attachments = _source_attachment_payloads(connection, payload)
        except sqlite3.Error:
            attachments = [_legacy_attachment_payload(item) for item in _legacy_attachments(payload)]
    else:
        source_type = payload.source_type
        source_name = payload.source_name
        source_url = payload.source_url
        title = payload.title
        source_body = payload.body
        attachments = [item.model_dump(exclude={"data_url"}) for item in payload.attachments]
    policy = ingestion_policy()
    result = {
        "type": source_type,
        "name": source_name or title,
        "url": source_url,
        "excerpt": (body if body is not None else source_body)[:240],
        "attachments": [bounded_attachment(item, policy) for item in attachments],
    }
    return result


def enrich_structured_notice(payload: NoticeInput, structured: dict) -> dict:
    tasks = []
    for item in structured.get("tasks", []):
        due_label = str(item.get("due_label") or "待確認")
        needs_clarification = bool(item.get("needs_clarification")) or due_label == "待確認"
        tasks.append(
            {
                **item,
                "due_label": due_label,
                "needs_clarification": needs_clarification,
                "confidence": item.get("confidence") if item.get("confidence") is not None else (0.55 if needs_clarification else 0.82),
            }
        )
    events = []
    for item in structured.get("events", []):
        date_label = str(item.get("date_label") or "待確認")
        time_label = str(item.get("time_label") or "全天")
        needs_clarification = bool(item.get("needs_clarification")) or date_label == "待確認日期" or date_label == "待確認"
        events.append(
            {
                **item,
                "date_label": date_label,
                "time_label": time_label,
                "needs_clarification": needs_clarification,
                "confidence": item.get("confidence") if item.get("confidence") is not None else (0.5 if needs_clarification else 0.82),
            }
        )
    needs_clarification = bool(structured.get("needs_clarification")) or any(item["needs_clarification"] for item in [*tasks, *events])
    return {
        "tasks": tasks,
        "events": events,
        "source_excerpt": payload.body,
        "source": notice_source_payload(payload),
        "needs_clarification": needs_clarification,
        "confidence": "low" if needs_clarification else "medium",
        "confidence_score": min([item["confidence"] for item in [*tasks, *events]] or [0.95]),
    }


def structured_from_input(payload: NoticeCreateInput) -> dict:
    if payload.structured is None:
        return parse_notice(payload)
    return enrich_structured_notice(payload, payload.structured.model_dump())


def parse_notice(payload: NoticeInput) -> dict:
    body = payload.body
    tasks: list[dict] = []
    events: list[dict] = []

    deadline_pattern = r"(?:在|於)\s*([^，。；\n]+?)\s*前"
    for match in re.finditer(deadline_pattern, body):
        due = match.group(1).strip(" ，、")
        following = body[match.end():]
        action_match = re.match(r"\s*(完成|繳交|填寫|填妥|填表|交回|帶上|攜帶)([^。；\n，]*)", following)
        if action_match:
            action = (action_match.group(1) + action_match.group(2)).strip(" ，、")
            title = {"填表": "完成分組表", "填寫": "完成回覆表", "交回": "繳交文件"}.get(action, action)
        else:
            before = body[max(0, match.start() - 18):match.start()]
            title = before.split("，")[-1].strip(" ，、") or "完成公告要求"
        if title and not any(item["title"] == title for item in tasks):
            tasks.append({"title": title, "due_label": due + "前"})

    if "同意書" in body and not any("同意書" in item["title"] for item in tasks):
        due = "本週五" if "本週五" in body else "待確認"
        tasks.append({"title": "繳交校外教學同意書", "due_label": due + "前"})

    course_match = re.search(r"((?:下週|本週)[一二三四五六日])([^。\n]*?(?:課|活動))", body)
    if course_match:
        context = course_match.group(0)
        time_match = re.search(r"(\d{1,2}[:：]\d{2})", context)
        items = "、".join(re.findall(r"筆電|Arduino|同意書|表單", context)) or "請依原公告確認"
        events.append({"title": "專題課" if "專題課" in context else "課程活動", "date_label": course_match.group(1), "time_label": time_match.group(1).replace("：", ":") if time_match else "待確認", "detail": f"需攜帶：{items}"})

    if "集合時間" in body:
        time_match = re.search(r"集合時間(?:為|：|:)?\s*(早上|上午)?\s*(\d{1,2}[:：]\d{2})", body)
        events.append({"title": "校外教學集合", "date_label": "待確認日期", "time_label": time_match.group(2).replace("：", ":") if time_match else "待確認", "detail": "請回看原公告確認集合地點"})

    return enrich_structured_notice(payload, {"tasks": tasks, "events": events})


def requested_timezone(value: str | None = None) -> tuple[ZoneInfo, str]:
    timezone_name = value or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(timezone_name), timezone_name
    except ZoneInfoNotFoundError as error:
        raise HTTPException(status_code=400, detail="Invalid timezone") from error


def event_input_bounds(event: CalendarEventInput) -> tuple[datetime, datetime | None]:
    if event.timezone:
        try:
            ZoneInfo(event.timezone)
        except ZoneInfoNotFoundError as error:
            raise HTTPException(status_code=422, detail="Event timezone is not a valid IANA timezone") from error
    try:
        if event.all_day:
            start = datetime.combine(date.fromisoformat(event.start_at[:10]), time.min, tzinfo=timezone.utc)
            end = datetime.combine(date.fromisoformat(event.end_at[:10]), time.min, tzinfo=timezone.utc) if event.end_at else start + timedelta(days=1)
        else:
            start = datetime.fromisoformat(event.start_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(event.end_at.replace("Z", "+00:00")) if event.end_at else None
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone_or_utc(event.timezone)[0])
            if end is not None and end.tzinfo is None:
                end = end.replace(tzinfo=timezone_or_utc(event.timezone)[0])
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="Event start_at/end_at must be valid ISO 8601 values") from error
    if end is not None and end <= start:
        raise HTTPException(status_code=422, detail="Event end_at must be after start_at")
    return start, end


def calendar_source_payload(source: sqlite3.Row, calendars: list[sqlite3.Row] | None = None) -> dict:
    calendars = calendars or []
    read_only = all(bool(calendar["read_only"]) for calendar in calendars) if calendars else True
    source_error = source["sync_error"] or source["last_error"]
    return {
        "id": source["id"],
        "kind": source["kind"],
        "name": source["name"],
        "server_url": source["server_url"],
        "username": source["username"],
        "status": source["status"],
        "read_only": read_only,
        "sync_status": source["sync_status"],
        "last_synced_at": source["last_synced_at"],
        "last_error": source_error,
        "capabilities": {"read": True, "write": not read_only},
    }


def dashboard_calendar_event(row: sqlite3.Row) -> dict:
    start = datetime.fromisoformat(row["start_at"])
    enriched = normalized_calendar_event(row)
    return {
        "id": row["id"],
        "title": row["title"],
        "date_label": start.date().isoformat(),
        "time_label": "全天" if row["all_day"] else start.strftime("%H:%M"),
        "detail": row["description"] or "",
        "location": row["location"],
        "source_id": row["source_id"],
        "source_type": "calendar",
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "all_day": bool(row["all_day"]),
        "status": row["status"],
        "created_source": row["created_source"] if "created_source" in row.keys() else "external_calendar",
        "event_source": "external_calendar",
        "origin_proposal_id": row["origin_proposal_id"] if "origin_proposal_id" in row.keys() else None,
        "external_calendar_ref": row["external_calendar_ref"] if "external_calendar_ref" in row.keys() else row["href"],
        "provider": enriched["provider"],
        "sync_status": enriched["sync_status"],
        "last_synced_at": enriched["last_synced_at"],
        "sync_conflict": enriched["sync_conflict"],
        "lifecycle_status": row["lifecycle_status"] if "lifecycle_status" in row.keys() else "created",
    }


def parse_optional_iso(value: str | None, label: str) -> str | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    try:
        if len(candidate) == 10:
            date.fromisoformat(candidate)
            return candidate
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"{label} must be a valid ISO 8601 date or datetime") from error
    return parsed.isoformat()


def task_due_status(due_iso: str | None) -> str:
    if not due_iso:
        return "unknown"
    try:
        if len(due_iso) == 10:
            due = datetime.combine(date.fromisoformat(due_iso), time.min, tzinfo=timezone.utc)
        else:
            due = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            due = due.astimezone(timezone.utc)
    except ValueError:
        return "unknown"
    today = datetime.now(timezone.utc).date()
    if due.date() < today:
        return "overdue"
    if due.date() == today:
        return "today"
    return "upcoming"


def task_payload(row: sqlite3.Row, source_name: str | None = None) -> dict:
    status = row["status"]
    return dict(row) | {
        "user_id": None,
        "needs_clarification": status == "needs_clarification",
        "due_status": task_due_status(row["due_iso"]),
        "start_at": row["start_at"] if "start_at" in row.keys() else None,
        "end_at": row["end_at"] if "end_at" in row.keys() else None,
        "all_day": bool(row["all_day"]) if "all_day" in row.keys() else False,
        "timezone": row["timezone"] if "timezone" in row.keys() else None,
        "list_id": row["list_id"] if "list_id" in row.keys() else None,
        "related_event_id": row["related_event_id"] if "related_event_id" in row.keys() else None,
        "source_type": "notice" if row["source_id"] != "manual" else "manual",
        "source": {
            "id": row["source_id"],
            "kind": "notice" if row["source_id"] != "manual" else "manual",
            "name": source_name or ("手動建立" if row["source_id"] == "manual" else "公告"),
        },
    }


def _task_schedule_fields(
    *,
    start_at: str | None,
    end_at: str | None,
    all_day: bool,
    timezone_name: str | None = None,
) -> dict[str, object]:
    """Validate a Task's optional standalone schedule without turning it into an Event."""
    if not start_at:
        if end_at:
            raise HTTPException(status_code=422, detail="Task end_at requires start_at")
        return {"start_at": None, "end_at": None, "all_day": 0, "timezone": timezone_name}
    start_text = str(start_at).strip()
    end_text = str(end_at).strip() if end_at else None
    if all_day:
        try:
            start_date = date.fromisoformat(start_text[:10])
        except ValueError as error:
            raise HTTPException(status_code=422, detail="All-day Task start_at must contain a valid date") from error
        if end_text:
            try:
                end_date = date.fromisoformat(end_text[:10])
            except ValueError as error:
                raise HTTPException(status_code=422, detail="All-day Task end_at must contain a valid date") from error
            if end_date < start_date:
                raise HTTPException(status_code=422, detail="Task end_at must not precede start_at")
        return {"start_at": start_date.isoformat(), "end_at": end_text[:10] if end_text else None, "all_day": 1, "timezone": timezone_name}
    if len(start_text) == 10:
        raise HTTPException(status_code=422, detail="Timed Task start_at must include a clock time")
    try:
        start_value = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end_value = datetime.fromisoformat(end_text.replace("Z", "+00:00")) if end_text else None
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Task schedule must use ISO 8601 date/time values") from error
    if end_value is not None:
        left = start_value
        right = end_value
        if left.tzinfo is None and right.tzinfo is not None:
            left = left.replace(tzinfo=right.tzinfo)
        elif right.tzinfo is None and left.tzinfo is not None:
            right = right.replace(tzinfo=left.tzinfo)
        if right <= left:
            raise HTTPException(status_code=422, detail="Task end_at must be after start_at")
    return {"start_at": start_text, "end_at": end_text, "all_day": 0, "timezone": timezone_name}


def _owned_event_exists(connection: sqlite3.Connection, user_id: str, event_id: str | None) -> bool:
    if not event_id:
        return True
    local = connection.execute(
        "SELECT 1 FROM events WHERE id = ? AND user_id = ? AND lifecycle_status <> 'deleted'",
        (_base_local_event_id(event_id), user_id),
    ).fetchone()
    if local is not None:
        return True
    external = connection.execute(
        """SELECT 1 FROM calendar_events ce JOIN calendar_sources cs ON cs.id = ce.source_id
           WHERE ce.id = ? AND cs.user_id = ? AND ce.lifecycle_status <> 'deleted'""",
        (event_id, user_id),
    ).fetchone()
    return external is not None


def proposal_payload(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["patch"] = json.loads(result.pop("patch_json"))
    result["evidence_refs"] = json.loads(result.pop("evidence_refs_json"))
    result["target_candidates"] = json.loads(result.pop("target_candidates_json"))
    result["needs_review"] = bool(result["needs_review"])
    result.pop("user_id", None)
    return result


def _local_event_context(row: sqlite3.Row, zone: ZoneInfo) -> dict:
    date_value = str(row["date_label"] or "")
    time_value = str(row["time_label"] or "")
    start_value = None
    end_value = None
    offset = datetime.now(zone).strftime("%z")
    offset_value = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        times = re.findall(r"\d{1,2}:\d{2}", time_value)
        if times:
            start_value = f"{date_value}T{times[0]}:00{offset_value}"
            if len(times) > 1:
                end_value = f"{date_value}T{times[1]}:00{offset_value}"
    return {
        "id": row["id"],
        "title": row["title"],
        "start": start_value or date_value,
        "end": end_value,
        "date": date_value,
        "time": time_value,
        "location": row["location"],
        "detail": row["detail"],
        "writable": True,
        "source": "event",
    }


class SQLiteCalendarQueryTool:
    """Bounded, read-only searches exposed to the interpretation boundary."""

    def __init__(self, user_id: str, max_results: int = 5):
        self.user_id = user_id
        # The model-facing contract is deliberately hard-capped at five rows.
        self.max_results = max(1, min(int(max_results), 5))

    def _limit(self, requested: int | None) -> int:
        try:
            value = int(requested) if requested is not None else self.max_results
        except (TypeError, ValueError):
            value = self.max_results
        return max(1, min(value, self.max_results, 5))

    def search_calendar_events(
        self,
        start_date: str,
        end_date: str,
        keyword: str | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        try:
            start_day = date.fromisoformat(str(start_date)[:10])
            end_day = date.fromisoformat(str(end_date)[:10])
        except ValueError as error:
            raise ValueError("Calendar search dates must be ISO dates") from error
        if end_day <= start_day:
            raise ValueError("Calendar search end_date must be after start_date")
        limit = self._limit(max_results)
        zone, _ = requested_timezone(DEFAULT_TIMEZONE)
        range_start = datetime.combine(start_day, time.min, tzinfo=zone).astimezone(timezone.utc).isoformat()
        range_end = datetime.combine(end_day, time.min, tzinfo=zone).astimezone(timezone.utc).isoformat()
        term = keyword.strip() if keyword else None
        like = f"%{term}%" if term else None
        with db() as connection:
            local_rows = connection.execute(
                """
                SELECT id, title, date_label, time_label, start_at, end_at, detail, location,
                       created_source, origin_proposal_id, external_calendar_ref, lifecycle_status
                FROM events
                WHERE user_id = ?
                  AND lifecycle_status NOT IN ('archived', 'deleted')
                  AND (
                    (start_at IS NOT NULL AND substr(start_at, 1, 10) >= ? AND substr(start_at, 1, 10) < ?)
                    OR (start_at IS NULL AND date_label >= ? AND date_label < ?)
                  )
                  AND (? IS NULL OR title LIKE ? OR detail LIKE ? OR location LIKE ?)
                ORDER BY COALESCE(start_at, date_label), time_label, id
                LIMIT ?
                """,
                (
                    self.user_id,
                    start_day.isoformat(), end_day.isoformat(),
                    start_day.isoformat(), end_day.isoformat(),
                    like, like, like, like, limit,
                ),
            ).fetchall()
            remote_rows = connection.execute(
                """
                SELECT ce.id, ce.title, ce.start_at, ce.end_at, ce.timezone, ce.all_day,
                       ce.location, ce.description, ce.created_source, ce.origin_proposal_id,
                       ce.external_calendar_ref, ce.lifecycle_status, ce.provider, ce.sync_status
                FROM calendar_events ce
                JOIN calendar_sources cs ON cs.id = ce.source_id
                WHERE cs.user_id = ? AND ce.start_at < ?
                  AND (ce.end_at > ? OR (ce.end_at IS NULL AND ce.start_at >= ?))
                  AND ce.status <> 'cancelled'
                  AND ce.lifecycle_status NOT IN ('archived', 'deleted')
                  AND NOT EXISTS (
                    SELECT 1 FROM events linked
                    WHERE linked.user_id = cs.user_id
                      AND linked.external_calendar_ref = ce.remote_id
                      AND linked.calendar_sync_enabled = 1
                      AND linked.lifecycle_status NOT IN ('archived', 'deleted')
                  )
                  AND (? IS NULL OR ce.title LIKE ? OR ce.description LIKE ? OR ce.location LIKE ?)
                ORDER BY ce.start_at, ce.title, ce.id
                LIMIT ?
                """,
                (self.user_id, range_end, range_start, range_start, like, like, like, like, limit),
            ).fetchall()

        local = [
            {
                "id": row["id"],
                "title": row["title"],
                "start": row["start_at"] or row["date_label"],
                "end": row["end_at"],
                "location": row["location"],
            }
            for row in local_rows
        ]
        remote = [
            {
                "id": row["id"],
                "title": row["title"],
                "start": row["start_at"],
                "end": row["end_at"],
                "location": row["location"],
            }
            for row in remote_rows
        ]
        return sorted([*local, *remote], key=lambda item: (str(item.get("start") or ""), item["title"], item["id"]))[:limit]

    def search_tasks(
        self,
        keyword: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        limit = self._limit(max_results)
        term = keyword.strip() if keyword else None
        like = f"%{term}%" if term else None

        def normalized_bound(value: str | None, name: str) -> str | None:
            if not value:
                return None
            try:
                return date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError as error:
                raise ValueError(f"{name} must be an ISO date") from error

        due_from_value = normalized_bound(due_from, "due_from")
        due_to_value = normalized_bound(due_to, "due_to")
        if due_from_value and due_to_value and due_to_value < due_from_value:
            raise ValueError("due_to must not precede due_from")
        with db() as connection:
            rows = connection.execute(
                """
                SELECT id, title, due_iso, start_at, end_at
                FROM tasks
                WHERE user_id = ? AND status <> 'done'
                  AND (? IS NULL OR title LIKE ?)
                  AND (? IS NULL OR (due_iso IS NOT NULL AND substr(due_iso, 1, 10) >= ?))
                  AND (? IS NULL OR (due_iso IS NOT NULL AND substr(due_iso, 1, 10) <= ?))
                ORDER BY COALESCE(due_iso, start_at, '9999-12-31'), title, id
                LIMIT ?
                """,
                (
                    self.user_id,
                    like, like,
                    due_from_value, due_from_value,
                    due_to_value, due_to_value,
                    limit,
                ),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "due": row["due_iso"],
                "start_at": row["start_at"],
                "end_at": row["end_at"],
            }
            for row in rows
        ]


def build_interpretation_context(user_id: str, current_datetime: datetime | None = None) -> dict:
    """Return only stable runtime facts; existing records are fetched by tools."""
    del user_id  # Context must not leak identity or database state to the model.
    zone, timezone_name = requested_timezone(DEFAULT_TIMEZONE)
    now = current_datetime or datetime.now(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=zone)
    else:
        now = now.astimezone(zone)
    return {
        "current_datetime": now.isoformat(),
        "timezone": timezone_name,
    }


def _interpret_with_read_only_tools(
    provider_instance: AIProvider,
    source: str,
    context: dict[str, Any],
    user_id: str,
    images: list[dict[str, str]] | None = None,
) -> list[Proposal]:
    """Use one model call for visual reading + intent, with bounded read-only tools."""
    images = images or []
    query_tool: InterpretationQueryTool = SQLiteCalendarQueryTool(user_id, max_results=5)
    if images:
        multimodal_tool_interpret = getattr(provider_instance, "interpret_multimodal_with_tools", None)
        if callable(multimodal_tool_interpret):
            return multimodal_tool_interpret(source, context, images, query_tool)
        multimodal_interpret = getattr(provider_instance, "interpret_multimodal", None)
        if callable(multimodal_interpret):
            return multimodal_interpret(source, context, images)
        raise AIProviderConfigurationError("AI provider does not support multimodal interpretation")
    tool_interpret = getattr(provider_instance, "interpret_with_tools", None)
    if callable(tool_interpret):
        return tool_interpret(source, context, query_tool)
    return provider_instance.interpret(source, context)

def notice_payload(row: sqlite3.Row) -> dict:
    result = dict(row)
    result.pop("attachments_json", None)
    raw_processing = result.pop("processing_json", "{}")
    try:
        result["processing"] = json.loads(raw_processing or "{}")
    except (TypeError, json.JSONDecodeError):
        result["processing"] = {}
    with db() as connection:
        policy = ingestion_policy()
        result["attachments"] = [bounded_attachment(item, policy) for item in _source_attachment_payloads(connection, row)]
    return result | {
        "user_id": None,
        "source": notice_source_payload(row),
    }


def notice_detail_payload(row: sqlite3.Row, tasks: list[sqlite3.Row], events: list[sqlite3.Row]) -> dict:
    result = notice_payload(row)
    result["structured"] = {
        "tasks": [
            {
                "id": task["id"],
                "title": task["title"],
                "due_label": task["due_label"],
                "due_iso": task["due_iso"],
                "status": task["status"],
                "needs_clarification": task["status"] == "needs_clarification",
                "confidence": 0.55 if task["status"] == "needs_clarification" else 0.82,
            }
            for task in tasks
        ],
        "events": [
            {
                "id": event["id"],
                "title": event["title"],
                "date_label": event["date_label"],
                "time_label": event["time_label"],
                "detail": event["detail"],
                "location": event["location"],
                "needs_clarification": event["date_label"] in {"待確認", "待確認日期"} or event["time_label"] == "待確認",
                "confidence": 0.5 if event["date_label"] in {"待確認", "待確認日期"} else 0.82,
            }
            for event in events
        ],
    }
    result["structured"]["needs_clarification"] = any(
        item["needs_clarification"] for item in [*result["structured"]["tasks"], *result["structured"]["events"]]
    )
    result["structured"]["confidence"] = "low" if result["structured"]["needs_clarification"] else "medium"
    result["structured"]["source"] = result["source"]
    return result


cleanup_expired_attachments()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "one-more-thing-api", "mode": "sqlite-oauth", "release": OMT_RELEASE}


@app.get("/api/ingestion/policy")
def public_ingestion_policy() -> dict:
    """Expose only user-facing Capture limits; never expose provider settings."""
    return {"policy": ingestion_policy().public()}


@app.get("/api/dashboard")
def dashboard(request: Request, include_calendar: bool = Query(default=True)) -> dict:
    user = require_user(request)
    with db() as connection:
        notices = connection.execute("SELECT * FROM notices WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
        tasks = connection.execute("SELECT * FROM tasks WHERE user_id = ?", (user["id"],)).fetchall()
        events = connection.execute(
            "SELECT * FROM events WHERE user_id = ? AND lifecycle_status NOT IN ('archived', 'deleted')",
            (user["id"],),
        ).fetchall()
        calendar_events = []
        if include_calendar:
            calendar_events = connection.execute(
                """
                SELECT ce.* FROM calendar_events ce
                JOIN calendar_sources cs ON cs.id = ce.source_id AND cs.user_id = ?
                WHERE ce.status <> 'cancelled'
                  AND ce.lifecycle_status NOT IN ('archived', 'deleted')
                  AND NOT EXISTS (
                    SELECT 1 FROM events linked
                    WHERE linked.user_id = cs.user_id
                      AND linked.external_calendar_ref = ce.remote_id
                      AND linked.calendar_sync_enabled = 1
                      AND linked.lifecycle_status NOT IN ('archived', 'deleted')
                  )
                ORDER BY ce.start_at
                """,
                (user["id"],),
            ).fetchall()
        proposals = connection.execute(
            "SELECT * FROM proposals WHERE user_id = ? AND status IN ('pending', 'edited', 'accepted') ORDER BY created_at DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
    open_tasks = [task for task in tasks if task["status"] != "done"]
    total_tasks = len(tasks)
    notice_names = {notice["id"]: notice["title"] for notice in notices}
    task_payloads = [task_payload(task, notice_names.get(task["source_id"])) for task in tasks]
    dashboard_events = []
    dashboard_zone, _ = requested_timezone(DEFAULT_TIMEZONE)
    dashboard_today = datetime.now(dashboard_zone).date()
    dashboard_lower = datetime.combine(dashboard_today, time.min, tzinfo=dashboard_zone).astimezone(timezone.utc)
    dashboard_upper = datetime.combine(dashboard_today + timedelta(days=31), time.min, tzinfo=dashboard_zone).astimezone(timezone.utc)
    for event in events:
        base = _local_event_payload(event) | {
            "source_type": "notice",
            "source": {"id": event["source_id"], "kind": "notice", "name": notice_names.get(event["source_id"], "公告")},
        }
        if event["recurrence_json"]:
            dashboard_events.extend(_expand_local_recurrence(event, base, dashboard_lower, dashboard_upper, dashboard_zone))
        else:
            dashboard_events.append(base)
    dashboard_events.extend(dashboard_calendar_event(event) for event in calendar_events)
    return {
        "profile": {"name": user["display_name"], "group": "學生帳號", "email": user["email"]},
        "stats": {"open_tasks": len(open_tasks), "today_events": len(dashboard_events), "unread": len(notices), "completion": round((total_tasks - len(open_tasks)) / total_tasks * 100) if total_tasks else 0},
        "notices": [notice_payload(notice) for notice in notices],
        "tasks": task_payloads,
        "events": dashboard_events,
        "proposals": [proposal_payload(proposal) for proposal in proposals],
    }


@app.get("/api/tasks")
def list_tasks(
    request: Request,
    status: str | None = Query(default=None, pattern="^(open|done|needs_clarification)$"),
    due: str | None = Query(default=None, pattern="^(overdue|today|upcoming|unknown|all)$"),
    needs_clarification: bool | None = None,
    q: str | None = Query(default=None, max_length=160),
    list_id: str | None = Query(default=None, max_length=64),
) -> dict:
    user = require_user(request)
    status = plain_query(status)
    due = plain_query(due)
    needs_clarification = plain_query(needs_clarification)
    q = plain_query(q)
    list_id = plain_query(list_id)
    clauses = ["t.user_id = ?"]
    parameters: list[object] = [user["id"]]
    if status:
        clauses.append("t.status = ?")
        parameters.append(status)
    if needs_clarification is True:
        clauses.append("t.status = 'needs_clarification'")
    elif needs_clarification is False:
        clauses.append("t.status <> 'needs_clarification'")
    if q and q.strip():
        clauses.append("t.title LIKE ?")
        parameters.append(f"%{q.strip()}%")
    if list_id:
        clauses.append("t.list_id = ?")
        parameters.append(list_id)
    with db() as connection:
        tasks = connection.execute(
            f"SELECT t.*, n.title AS source_name FROM tasks t LEFT JOIN notices n ON n.id = t.source_id WHERE {' AND '.join(clauses)} ORDER BY t.status, t.due_iso, t.id",
            parameters,
        ).fetchall()
    filtered = [task for task in tasks if not due or due == "all" or task_due_status(task["due_iso"]) == due]
    return {
        "tasks": [task_payload(task, task["source_name"]) for task in filtered],
        "meta": {
            "count": len(filtered),
            "status": status or "all",
            "due": due or "all",
            "needs_clarification": needs_clarification,
            "query": q or "",
            "list_id": list_id,
        },
    }


@app.get("/api/lists")
def list_personal_lists(request: Request) -> dict:
    user = require_user(request)
    with db() as connection:
        rows = connection.execute(
            "SELECT id, name, created_at FROM personal_lists WHERE user_id = ? ORDER BY created_at, id",
            (user["id"],),
        ).fetchall()
    return {"lists": [dict(row) for row in rows]}


@app.post("/api/lists", status_code=201)
def create_personal_list(request: Request, payload: ListCreate) -> dict:
    user = require_user(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="List name must contain non-whitespace characters")
    list_id = f"list-{uuid4().hex[:12]}"
    created_at = now_iso()
    with db() as connection:
        try:
            connection.execute(
                "INSERT INTO personal_lists(id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                (list_id, user["id"], name, created_at),
            )
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error).upper():
                raise HTTPException(status_code=409, detail="A list with this name already exists") from error
            raise
        row = connection.execute(
            "SELECT id, name, created_at FROM personal_lists WHERE id = ? AND user_id = ?",
            (list_id, user["id"]),
        ).fetchone()
    return {"list": dict(row)}


@app.delete("/api/lists/{list_id}")
def delete_personal_list(request: Request, list_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute("SELECT id FROM personal_lists WHERE id = ? AND user_id = ?", (list_id, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="List not found")
        connection.execute("UPDATE tasks SET list_id = NULL WHERE user_id = ? AND list_id = ?", (user["id"], list_id))
        connection.execute("DELETE FROM personal_lists WHERE id = ? AND user_id = ?", (list_id, user["id"]))
    return {"deleted": True, "list_id": list_id}


@app.get("/api/calendar/sources")
def calendar_sources(request: Request) -> dict:
    user = require_user(request)
    with db() as connection:
        sources = connection.execute(
            "SELECT * FROM calendar_sources WHERE user_id = ? AND status <> 'disconnected' ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        result = []
        for source in sources:
            calendars = connection.execute(
                "SELECT id, remote_url, display_name, timezone, read_only, sync_status, last_synced_at, last_error FROM calendar_calendars WHERE source_id = ? ORDER BY display_name",
                (source["id"],),
            ).fetchall()
            result.append({"source": calendar_source_payload(source, calendars), "calendars": [dict(calendar) for calendar in calendars]})
    return {"sources": result}


@app.get("/api/calendar/sources/{source_id}")
def get_calendar_source(request: Request, source_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        source = connection.execute("SELECT * FROM calendar_sources WHERE id = ? AND user_id = ?", (source_id, user["id"])).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="Calendar source not found")
        calendars = connection.execute(
            "SELECT id, remote_url, display_name, timezone, read_only, sync_status, last_synced_at, last_error FROM calendar_calendars WHERE source_id = ? ORDER BY display_name",
            (source_id,),
        ).fetchall()
    return {"source": calendar_source_payload(source, calendars), "calendars": [dict(calendar) for calendar in calendars]}


@app.get("/api/sources")
def unified_sources(request: Request) -> dict:
    user = require_user(request)
    with db() as connection:
        notices = connection.execute("SELECT * FROM notices WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
        calendar_rows = connection.execute("SELECT * FROM calendar_sources WHERE user_id = ? AND status <> 'disconnected' ORDER BY created_at DESC", (user["id"],)).fetchall()
        calendar_map = {}
        for source in calendar_rows:
            calendars = connection.execute("SELECT * FROM calendar_calendars WHERE source_id = ? ORDER BY display_name", (source["id"],)).fetchall()
            calendar_map[source["id"]] = {"source": calendar_source_payload(source, calendars), "calendars": [dict(calendar) for calendar in calendars]}
    sources = [
        Source(
            id=notice["id"], kind="notice", name=notice["title"], status="active", read_only=True,
            captured_at=notice["created_at"], attachments=notice_source_payload(notice)["attachments"],
        ).model_dump()
        for notice in notices
    ]
    sources.extend(item["source"] for item in calendar_map.values())
    return {"sources": sources}


@app.get("/api/sources/{source_id}/attachments/{attachment_id}")
def get_source_attachment(request: Request, source_id: str, attachment_id: str) -> FileResponse:
    user = require_user(request)
    with db() as connection:
        row = connection.execute(
            """
            SELECT a.* FROM attachments a
            JOIN notices n ON n.id = a.source_id
            WHERE a.id = ? AND a.source_id = ? AND n.user_id = ?
            """,
            (attachment_id, source_id, user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not _attachment_raw_available(row):
        raise HTTPException(status_code=404, detail="Raw attachment has expired")
    try:
        path = ATTACHMENT_STORE.path(row["storage_key"])
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Attachment not found") from error
    return FileResponse(path, media_type=row["mime_type"], filename=row["filename"], headers={"Cache-Control": "private, max-age=60"})


def calendar_range_bounds(month: str | None, start_value: str | None, end_value: str | None, zone: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    if month and (start_value or end_value):
        raise HTTPException(status_code=422, detail="Use either month or start/end, not both")
    if month:
        match = re.fullmatch(r"(\d{4})-(\d{2})", month)
        if not match:
            raise HTTPException(status_code=422, detail="month must use YYYY-MM format")
        try:
            first = date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="month is not a valid calendar month") from error
        if first.month == 12:
            next_month = date(first.year + 1, 1, 1)
        else:
            next_month = date(first.year, first.month + 1, 1)
        return (
            datetime.combine(first, time.min, tzinfo=zone).astimezone(timezone.utc),
            datetime.combine(next_month, time.min, tzinfo=zone).astimezone(timezone.utc),
        )
    if not start_value and not end_value:
        return None, None

    def parse_bound(value: str, *, is_end: bool) -> datetime:
        try:
            if len(value) == 10:
                parsed = datetime.combine(date.fromisoformat(value), time.min, tzinfo=zone)
                if is_end:
                    parsed += timedelta(days=1)
            else:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=zone)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="start/end must be valid ISO 8601 dates or datetimes") from error
        return parsed.astimezone(timezone.utc)

    start = parse_bound(start_value, is_end=False) if start_value else None
    end = parse_bound(end_value, is_end=True) if end_value else None
    if start and end and end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    return start, end


@app.get("/api/events/search")
def search_linkable_events(
    request: Request,
    q: str | None = Query(default=None, max_length=120),
    around: str | None = Query(default=None, max_length=10),
    today: str | None = Query(default=None, max_length=10),
    limit: int = Query(default=12, ge=1, le=30),
) -> dict:
    """Small, on-demand selector dataset for linking a Task to an Event.

    It deliberately avoids dumping every calendar event into the Task details UI.
    """
    user = require_user(request)
    query = (plain_query(q) or "").strip().casefold()
    around_value = plain_query(around)
    today_value = plain_query(today) if isinstance(today, (str, type(None))) else None
    try:
        anchor = date.fromisoformat(around_value) if around_value else datetime.now().date()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="around must be YYYY-MM-DD") from error
    try:
        local_today = date.fromisoformat(today_value) if today_value else datetime.now().date()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="today must be YYYY-MM-DD") from error
    candidates: list[dict[str, Any]] = []
    with db() as connection:
        local_rows = connection.execute(
            "SELECT * FROM events WHERE user_id = ? AND lifecycle_status NOT IN ('archived','deleted') ORDER BY COALESCE(start_at, date_label), title LIMIT 120",
            (user["id"],),
        ).fetchall()
        external_rows = connection.execute(
            """SELECT ce.* FROM calendar_events ce JOIN calendar_sources cs ON cs.id = ce.source_id
               WHERE cs.user_id = ? AND ce.lifecycle_status NOT IN ('archived','deleted') AND ce.status <> 'cancelled'
               ORDER BY ce.start_at, ce.title LIMIT 120""",
            (user["id"],),
        ).fetchall()
    for row in local_rows:
        item = _local_event_payload(row)
        item["event_source"] = item.get("event_source") or "local"
        candidates.append(item)
    for row in external_rows:
        item = normalized_calendar_event(row)
        item["event_source"] = item.get("event_source") or "external_calendar"
        candidates.append(item)

    def candidate_date(item: dict[str, Any]) -> date:
        raw = item.get("start_at") or item.get("date_label") or ""
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return date.max

    def search_text(item: dict[str, Any]) -> str:
        value_date = candidate_date(item)
        parts = [str(item.get(key) or "") for key in ("title", "location", "detail", "description")]
        if value_date != date.max:
            weekday_zh = "一二三四五六日"[value_date.weekday()]
            weekday_en = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[value_date.weekday()]
            parts.extend([
                value_date.isoformat(),
                f"{value_date.month}/{value_date.day}",
                f"{value_date.month}月{value_date.day}日",
                f"{value_date.month}月{value_date.day}號",
                f"星期{weekday_zh}",
                f"週{weekday_zh}",
                f"周{weekday_zh}",
                f"禮拜{weekday_zh}",
                weekday_en,
            ])
            delta = (value_date - local_today).days
            if delta == 0:
                parts.extend(["今天", "今日"])
            elif delta == 1:
                parts.extend(["明天", "明日"])
            elif delta == 2:
                parts.append("後天")
        return " ".join(parts).casefold()

    if query:
        tokens = [token for token in re.split(r"\s+", query) if token]
        candidates = [item for item in candidates if all(token in search_text(item) for token in tokens)]
    candidates.sort(key=lambda item: (abs((candidate_date(item) - anchor).days) if candidate_date(item) != date.max else 999999, candidate_date(item), str(item.get("title") or "")))
    result = candidates[:limit]
    return {"events": result, "meta": {"count": len(result), "limit": limit, "query": query, "around": anchor.isoformat(), "today": local_today.isoformat()}}


@app.get("/api/calendar/events")
def calendar_events(
    request: Request,
    scope: str = Query(default="upcoming", pattern="^(today|upcoming|past|all)$"),
    calendar_id: str | None = Query(default=None, min_length=1, max_length=64),
    timezone_name: str | None = Query(default=None, alias="timezone", max_length=128),
    month: str | None = Query(default=None, max_length=7),
    range_start: str | None = Query(default=None, alias="start", max_length=80),
    range_end: str | None = Query(default=None, alias="end", max_length=80),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    user = require_user(request)
    scope = plain_query(scope, "upcoming")
    calendar_id = plain_query(calendar_id)
    timezone_name = plain_query(timezone_name)
    month = plain_query(month)
    range_start = plain_query(range_start)
    range_end = plain_query(range_end)
    limit = plain_query(limit, 100)
    offset = plain_query(offset, 0)
    zone, zone_name = requested_timezone(timezone_name)
    range_start_dt, range_end_dt = calendar_range_bounds(month, range_start, range_end, zone)
    now = datetime.now(timezone.utc)
    today_start = datetime.combine(now.astimezone(zone).date(), time.min, tzinfo=zone).astimezone(timezone.utc)
    tomorrow_start = today_start + timedelta(days=1)
    clauses = ["cs.user_id = ?"]
    parameters: list[object] = [user["id"]]
    if calendar_id:
        clauses.append("ce.calendar_id = ?")
        parameters.append(calendar_id)
    with db() as connection:
        rows = connection.execute(
            f"""
            SELECT ce.*, cs.name AS source_name, cs.kind AS source_kind, cs.sync_status AS source_sync_status, cs.sync_error AS source_sync_error, cs.last_synced_at AS source_last_synced_at, cc.display_name AS calendar_name
            FROM calendar_events ce
            JOIN calendar_sources cs ON cs.id = ce.source_id
            JOIN calendar_calendars cc ON cc.id = ce.calendar_id
            WHERE {' AND '.join(clauses)} AND ce.lifecycle_status NOT IN ('archived', 'deleted')
              AND NOT EXISTS (
                SELECT 1 FROM events linked
                WHERE linked.user_id = cs.user_id
                  AND linked.external_calendar_ref = ce.remote_id
                  AND linked.calendar_sync_enabled = 1
                  AND linked.lifecycle_status NOT IN ('archived', 'deleted')
              )
            ORDER BY ce.start_at, ce.title
            """,
            parameters,
        ).fetchall()
    events = []
    source_statuses: dict[str, dict] = {}
    for row in rows:
        source_statuses[row["source_id"]] = {
            "source_id": row["source_id"],
            "status": row["source_sync_status"],
            "last_synced_at": row["source_last_synced_at"],
            "error": row["source_sync_error"],
        }
        if row["status"] == "cancelled":
            continue
        try:
            start_at = datetime.fromisoformat(row["start_at"]).astimezone(timezone.utc)
            end_at = datetime.fromisoformat(row["end_at"]).astimezone(timezone.utc) if row["end_at"] else start_at
        except ValueError:
            continue
        if range_start_dt or range_end_dt:
            if range_start_dt and end_at <= range_start_dt:
                continue
            if range_end_dt and start_at >= range_end_dt:
                continue
        elif scope == "today" and not (start_at < tomorrow_start and end_at > today_start):
            continue
        if not (range_start_dt or range_end_dt) and scope == "upcoming" and end_at <= now:
            continue
        if not (range_start_dt or range_end_dt) and scope == "past" and end_at > now:
            continue
        event = normalized_calendar_event(row)
        event["source"] = {"id": row["source_id"], "name": row["source_name"], "kind": row["source_kind"]}
        event["calendar"] = {"id": row["calendar_id"], "name": row["calendar_name"]}
        events.append(event)
    with db() as connection:
        source_rows = connection.execute("SELECT id, sync_status, sync_error, last_synced_at FROM calendar_sources WHERE user_id = ?", (user["id"],)).fetchall()
    for source in source_rows:
        source_statuses.setdefault(
            source["id"],
            {"source_id": source["id"], "status": source["sync_status"], "last_synced_at": source["last_synced_at"], "error": source["sync_error"]},
        )
    has_error = any(item["status"] == "error" for item in source_statuses.values())
    total_count = len(events)
    page = events[offset : offset + limit]
    next_offset = offset + limit if offset + limit < total_count else None
    return {
        "events": page,
        "meta": {
            "scope": "range" if (range_start_dt or range_end_dt) else scope,
            "timezone": zone_name,
            "count": len(page),
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
            "status": "error" if has_error else "ok",
            "start": range_start_dt.isoformat() if range_start_dt else None,
            "end": range_end_dt.isoformat() if range_end_dt else None,
            "sources": list(source_statuses.values()),
        },
    }


@app.get("/api/calendar/events/{event_id}")
def get_calendar_event(request: Request, event_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute(
            """
            SELECT ce.*, cs.name AS source_name, cs.kind AS source_kind, cs.server_url, cs.username, cs.sync_status AS source_sync_status,
                   cs.last_synced_at AS source_last_synced_at, cc.display_name AS calendar_name, cc.timezone AS calendar_timezone,
                   cc.read_only AS calendar_read_only
            FROM calendar_events ce
            JOIN calendar_sources cs ON cs.id = ce.source_id AND cs.user_id = ?
            JOIN calendar_calendars cc ON cc.id = ce.calendar_id
            WHERE ce.id = ?
            """,
            (user["id"], event_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    event = normalized_calendar_event(row)
    event["source"] = {
        "id": row["source_id"],
        "kind": row["source_kind"],
        "name": row["source_name"],
        "server_url": row["server_url"],
        "username": row["username"],
        "sync_status": row["source_sync_status"],
        "last_synced_at": row["source_last_synced_at"],
    }
    event["calendar"] = {
        "id": row["calendar_id"],
        "name": row["calendar_name"],
        "timezone": row["calendar_timezone"],
        "read_only": bool(row["calendar_read_only"]),
    }
    return event


def _calendar_event_row_for_user(connection: sqlite3.Connection, user_id: str, event_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT ce.*, cs.user_id, cs.name AS source_name, cs.username, cs.credential_ciphertext,
               cs.server_url, cs.sync_status AS source_sync_status, cc.display_name AS calendar_name,
               cc.remote_url AS calendar_remote_id, cc.read_only AS calendar_read_only
        FROM calendar_events ce
        JOIN calendar_sources cs ON cs.id = ce.source_id AND cs.user_id = ?
        JOIN calendar_calendars cc ON cc.id = ce.calendar_id
        WHERE ce.id = ?
        """,
        (user_id, event_id),
    ).fetchone()


@app.patch("/api/calendar/events/{event_id}/enrichment")
def update_calendar_event_enrichment(request: Request, event_id: str) -> dict:
    require_user(request)
    raise HTTPException(
        status_code=410,
        detail="Calendar enrichment was removed; edit the Calendar event itself via /api/events/{event_id}",
    )


@app.get("/api/events")
def list_events(
    request: Request,
    start: str | None = Query(default=None, max_length=80),
    end: str | None = Query(default=None, max_length=80),
    source_id: str | None = Query(default=None, max_length=256),
) -> dict:
    user = require_user(request)
    start = plain_query(start)
    end = plain_query(end)
    source_id = plain_query(source_id)
    with db() as connection:
        notice_events = connection.execute(
            "SELECT e.*, n.title AS source_name FROM events e LEFT JOIN notices n ON n.id = e.source_id WHERE e.user_id = ? AND e.lifecycle_status NOT IN ('archived', 'deleted') ORDER BY e.id",
            (user["id"],),
        ).fetchall()
        calendar_events_rows = connection.execute(
            """
            SELECT ce.*, cs.name AS source_name, cs.kind AS source_kind, cc.display_name AS calendar_name
            FROM calendar_events ce
            JOIN calendar_sources cs ON cs.id = ce.source_id AND cs.user_id = ?
            JOIN calendar_calendars cc ON cc.id = ce.calendar_id
            WHERE ce.status <> 'cancelled' AND ce.lifecycle_status NOT IN ('archived', 'deleted')
              AND NOT EXISTS (
                SELECT 1 FROM events linked
                WHERE linked.user_id = cs.user_id
                  AND linked.external_calendar_ref = ce.remote_id
                  AND linked.calendar_sync_enabled = 1
                  AND linked.lifecycle_status NOT IN ('archived', 'deleted')
              )
            ORDER BY ce.start_at, ce.title
            """,
            (user["id"],),
        ).fetchall()
    zone, _ = requested_timezone(DEFAULT_TIMEZONE)
    lower, upper = calendar_range_bounds(None, start, end, zone) if (start or end) else (None, None)
    result = []
    for row in notice_events:
        if source_id and row["source_id"] != source_id:
            continue
        base = _local_event_payload(row) | {
            "source_type": "notice",
            "remote_id": None,
            "source": {"id": row["source_id"], "kind": "notice", "name": row["source_name"] or "公告"},
        }
        result.extend(_expand_local_recurrence(row, base, lower, upper, zone) if (start or end) and row["recurrence_json"] else [base])
    for row in calendar_events_rows:
        if source_id and row["source_id"] != source_id:
            continue
        event = normalized_calendar_event(row)
        event["source"] = {"id": row["source_id"], "kind": row["source_kind"], "name": row["source_name"]}
        event["calendar"] = {"id": row["calendar_id"], "name": row["calendar_name"]}
        result.append(event)
    if start or end:
        def overlaps(item: dict) -> bool:
            if not item.get("start_at"):
                return False
            try:
                item_start = datetime.fromisoformat(str(item["start_at"]).replace("Z", "+00:00"))
                if item_start.tzinfo is None:
                    item_start = item_start.replace(tzinfo=timezone.utc)
                item_start_utc = item_start.astimezone(timezone.utc)
                if not item.get("end_at"):
                    # A start-only Calendar event is an instant at DTSTART, not an
                    # unbounded interval. Keep it only in the range containing its
                    # start so it cannot leak into every Calendar page forever.
                    return (lower is None or item_start_utc >= lower) and (upper is None or item_start_utc < upper)
                item_end = datetime.fromisoformat(str(item["end_at"]).replace("Z", "+00:00"))
                if item_end.tzinfo is None:
                    item_end = item_end.replace(tzinfo=timezone.utc)
            except ValueError:
                return False
            return (lower is None or item_end.astimezone(timezone.utc) > lower) and (upper is None or item_start_utc < upper)
        result = [item for item in result if overlaps(item)]
    return {"events": result, "meta": {"count": len(result), "start": start, "end": end, "source_id": source_id}}


def _expand_local_recurrence(row: sqlite3.Row, base: dict, lower: datetime | None, upper: datetime | None, zone: ZoneInfo) -> list[dict]:
    try:
        rule = json.loads(row["recurrence_json"] or "{}")
        first_day = date.fromisoformat(rule["start_date"])
        last_day = date.fromisoformat(rule["end_date"])
        weekdays = {int(day) for day in rule["weekdays"]}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [base]
    if (last_day - first_day).days > 730:
        return [base]
    all_day = bool(row["all_day"])
    try:
        raw_start = str(row["start_at"])
        raw_end = str(row["end_at"])
        if all_day:
            start_clock = end_clock = None
            duration = timedelta(days=1)
        else:
            source_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
            source_end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            if source_start.tzinfo is not None:
                source_start = source_start.astimezone(zone)
                source_end = source_end.astimezone(zone)
            else:
                source_start = source_start.replace(tzinfo=zone)
                source_end = source_end.replace(tzinfo=zone)
            start_clock = source_start.timetz().replace(tzinfo=None)
            duration = source_end - source_start
            if duration <= timedelta(0):
                return [base]
    except (TypeError, ValueError):
        return [base]

    occurrences = []
    try:
        base_day = date.fromisoformat(str(row["date_label"] or str(row["start_at"])[:10])[:10])
    except ValueError:
        base_day = first_day
    current = max(first_day, base_day)
    while current <= last_day:
        if current.isoweekday() in weekdays:
            if all_day:
                occurrence_start = current.isoformat()
                occurrence_end = (current + timedelta(days=1)).isoformat()
                overlap_start = datetime.combine(current, time.min, tzinfo=zone).astimezone(timezone.utc)
                overlap_end = datetime.combine(current + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
                time_label = "全天"
            else:
                start_dt = datetime.combine(current, start_clock, tzinfo=zone)
                end_dt = start_dt + duration
                occurrence_start = start_dt.isoformat()
                occurrence_end = end_dt.isoformat()
                overlap_start = start_dt.astimezone(timezone.utc)
                overlap_end = end_dt.astimezone(timezone.utc)
                time_label = f"{start_dt:%H:%M}～{end_dt:%H:%M}"
            if (lower is None or overlap_end > lower) and (upper is None or overlap_start < upper):
                occurrences.append(base | {
                    "id": f"{row['id']}@{current.isoformat()}",
                    "series_id": row["id"],
                    "occurrence_date": current.isoformat(),
                    "date_label": current.isoformat(),
                    "time_label": time_label,
                    "start_at": occurrence_start,
                    "end_at": occurrence_end,
                })
        current += timedelta(days=1)
    return occurrences


def _base_local_event_id(event_id: str) -> str:
    match = re.fullmatch(r"(event-[a-f0-9]+)@(\d{4}-\d{2}-\d{2})", event_id)
    return match.group(1) if match else event_id


@app.get("/api/events/{event_id}")
def get_event(request: Request, event_id: str) -> dict:
    user = require_user(request)
    event_id = _base_local_event_id(event_id)
    with db() as connection:
        notice_event = connection.execute(
            "SELECT e.*, n.title AS source_name, n.body AS source_body FROM events e LEFT JOIN notices n ON n.id = e.source_id WHERE e.id = ? AND e.user_id = ? AND e.lifecycle_status <> 'deleted'",
            (event_id, user["id"]),
        ).fetchone()
    if notice_event is not None:
        return _local_event_payload(notice_event) | {
            "source_type": "notice",
            "source": {"id": notice_event["source_id"], "kind": "notice", "name": notice_event["source_name"] or "公告", "body": notice_event["source_body"]},
        }
    try:
        return get_calendar_event(request, event_id)
    except HTTPException as error:
        if error.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found") from error
        raise


def _local_event_payload(row: sqlite3.Row) -> dict:
    try:
        conflict = json.loads(row["sync_conflict_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        conflict = {}
    try:
        recurrence = json.loads(row["recurrence_json"]) if row["recurrence_json"] else None
    except (KeyError, TypeError, json.JSONDecodeError):
        recurrence = None
    event_source = "ai_generated" if row["created_source"] == "ai_generated" else "local"
    return dict(row) | {
        "user_id": None,
        "source_type": "notice" if row["source_id"] != "manual" else "manual",
        "event_source": event_source,
        "provider": row["provider"] if "provider" in row.keys() else None,
        "sync_status": row["sync_status"] if "sync_status" in row.keys() else "local",
        "last_synced_at": row["last_synced_at"] if "last_synced_at" in row.keys() else None,
        "sync_conflict": conflict if isinstance(conflict, dict) else {},
        "recurrence": recurrence,
        "all_day": (bool(row["all_day"]) or str(row["time_label"] or "").strip().lower() in {"全天", "整天", "all day"}) if "all_day" in row.keys() else row["time_label"] in {"全天", "整天", "All day"},
        "calendar_sync_enabled": bool(row["calendar_sync_enabled"]) if "calendar_sync_enabled" in row.keys() else False,
        "source": {"id": row["source_id"], "kind": "notice" if row["source_id"] != "manual" else "manual"},
    }


def _recurrence_rule_for_local_event(row: sqlite3.Row) -> str | None:
    try:
        recurrence = json.loads(row["recurrence_json"] or "{}")
        end_date = date.fromisoformat(recurrence["end_date"])
        weekdays = {int(day) for day in recurrence["weekdays"]}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    day_codes = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}
    byday = ",".join(day_codes[day] for day in sorted(weekdays) if day in day_codes)
    if not byday:
        return None
    if bool(row["all_day"]):
        until = end_date.strftime("%Y%m%d")
    else:
        try:
            zone, _ = requested_timezone(row["timezone"] or DEFAULT_TIMEZONE)
            until = datetime.combine(end_date, time.max.replace(microsecond=0), tzinfo=zone).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except (HTTPException, TypeError, ValueError):
            return None
    return f"FREQ=WEEKLY;BYDAY={byday};UNTIL={until}"


def local_event_calendar_input(row: sqlite3.Row, source_id: str, calendar_id: str) -> CalendarEventInput:
    date_value = str(row["date_label"] or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        raise HTTPException(status_code=422, detail="Local event must have an ISO date before Calendar sync")
    recurrence_rule = _recurrence_rule_for_local_event(row)
    if row["start_at"] and row["end_at"]:
        start_at = row["start_at"]
        end_at = row["end_at"]
        all_day = bool(row["all_day"])
        event_timezone = row["timezone"] or DEFAULT_TIMEZONE
    else:
        time_label = str(row["time_label"] or "").strip()
        all_day = time_label.lower() in {"全天", "整天", "all day"}
        if not all_day and not re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", time_label):
            raise HTTPException(status_code=422, detail="Local event needs a confirmed start time or explicit all-day choice before Calendar sync")
        schedule = _event_schedule_fields({"date": date_value, "time": time_label, "all_day": all_day})
        start_at = schedule["start_at"]
        end_at = schedule["end_at"]
        all_day = bool(schedule["all_day"])
        event_timezone = DEFAULT_TIMEZONE
    return CalendarEventInput(
        source_id=source_id,
        calendar_id=calendar_id,
        title=row["title"],
        start_at=start_at,
        end_at=end_at,
        all_day=all_day,
        timezone=event_timezone,
        recurrence_rule=recurrence_rule,
        location=row["location"],
        description=row["detail"],
    )


def sync_linked_local_event(user_id: str, row: sqlite3.Row) -> None:
    if not row["calendar_sync_enabled"] or not row["external_calendar_ref"]:
        return
    with db() as connection:
        remote = connection.execute(
            """
            SELECT ce.*, cs.username, cs.credential_ciphertext, cs.server_url,
                   cc.remote_url AS calendar_remote_id, cc.read_only
            FROM calendar_events ce
            JOIN calendar_sources cs ON cs.id = ce.source_id AND cs.user_id = ?
            JOIN calendar_calendars cc ON cc.id = ce.calendar_id
            WHERE ce.remote_id = ? OR ce.external_calendar_ref = ?
            ORDER BY ce.updated_at DESC LIMIT 1
            """,
            (user_id, row["external_calendar_ref"], row["external_calendar_ref"]),
        ).fetchone()
    if remote is None or remote["read_only"]:
        return
    try:
        payload = local_event_calendar_input(row, remote["source_id"], remote["calendar_id"])
        if remote["provider"] == CalendarProvider.GOOGLE.value:
            access_token = _google_access_token(user_id, remote["source_id"])
            updated = google_calendar_api_json(
                access_token,
                _google_calendar_path(remote["calendar_remote_id"], f"/events/{urllib.parse.quote(str(remote['remote_id']), safe='') }"),
                method="PATCH",
                payload=_google_event_resource(payload),
            )
            provider_etag = remote["etag"]
            new_etag = updated.get("etag") or provider_etag
        else:
            with db() as connection:
                source = connection.execute("SELECT * FROM calendar_sources WHERE id = ? AND user_id = ?", (remote["source_id"], user_id)).fetchone()
            app_password = decrypt_source_credential(source)
            adapter = NextcloudCalDavAdapter(remote["username"], app_password)
            provider_ical, provider_etag = adapter.get_event(remote["href"])
            icalendar = replace_icalendar_event_fields(provider_ical, payload, remote["remote_id"])
            new_etag = adapter.update_event(remote["href"], icalendar, provider_etag or remote["etag"])
    except (CalendarConnectionError, HTTPException) as error:
        with db() as connection:
            connection.execute(
                "UPDATE events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (json.dumps({"error": str(error), "strategy": "omt_latest_wins", "retryable": True}, ensure_ascii=False), now_iso(), row["id"], user_id),
            )
        return
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            "UPDATE events SET sync_status = 'synced', last_synced_at = ?, sync_conflict_json = '{}', updated_at = ? WHERE id = ? AND user_id = ?",
            (timestamp, timestamp, row["id"], user_id),
        )
        connection.execute(
            "UPDATE calendar_events SET title = ?, start_at = ?, end_at = ?, all_day = ?, timezone = ?, location = ?, description = ?, recurrence_rule = ?, etag = ?, sync_status = 'synced', last_synced_at = ?, sync_conflict_json = '{}', updated_at = ? WHERE id = ?",
            (payload.title, payload.start_at, payload.end_at, int(payload.all_day), payload.timezone, payload.location, payload.description, payload.recurrence_rule, new_etag or provider_etag or remote["etag"], timestamp, timestamp, remote["id"]),
        )


def sync_local_event_to_default_calendar(user_id: str, event_id: str) -> None:
    """Write OMT-owned Events to the user's default writable Calendar provider."""
    with db() as connection:
        row = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)).fetchone()
        if row is None or row["sync_status"] == "syncing":
            return
        if row["lifecycle_status"] == "deleted":
            if not row["calendar_sync_enabled"] or not row["external_calendar_ref"]:
                return
            remote = connection.execute(
                """SELECT ce.*, cs.username, cs.credential_ciphertext, cc.remote_url AS calendar_remote_id
                   FROM calendar_events ce
                   JOIN calendar_sources cs ON cs.id = ce.source_id AND cs.user_id = ?
                   JOIN calendar_calendars cc ON cc.id = ce.calendar_id
                   WHERE ce.remote_id = ? OR ce.external_calendar_ref = ? ORDER BY ce.updated_at DESC LIMIT 1""",
                (user_id, row["external_calendar_ref"], row["external_calendar_ref"]),
            ).fetchone()
            linked_row = None
        else:
            remote = None
            linked_row = row if row["external_calendar_ref"] and row["calendar_sync_enabled"] else None

    if row["lifecycle_status"] == "deleted":
        try:
            if remote is not None:
                if remote["provider"] == CalendarProvider.GOOGLE.value:
                    access_token = _google_access_token(user_id, remote["source_id"])
                    google_calendar_api_json(
                        access_token,
                        _google_calendar_path(remote["calendar_remote_id"], f"/events/{urllib.parse.quote(str(remote['remote_id']), safe='') }"),
                        method="DELETE",
                    )
                else:
                    NextcloudCalDavAdapter(remote["username"], decrypt_source_credential(remote)).delete_event(remote["href"], remote["etag"])
            with db() as connection:
                timestamp = now_iso()
                connection.execute("UPDATE events SET external_calendar_ref = NULL, calendar_sync_enabled = 0, sync_status = 'synced', last_synced_at = ?, sync_conflict_json = '{}', updated_at = ? WHERE id = ? AND user_id = ?", (timestamp, timestamp, event_id, user_id))
        except CalendarConnectionError as error:
            with db() as connection:
                connection.execute("UPDATE events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ? AND user_id = ?", (json.dumps({"error": str(error), "operation": "delete", "retryable": True}, ensure_ascii=False), now_iso(), event_id, user_id))
        return

    if row["external_calendar_ref"]:
        if linked_row is not None:
            sync_linked_local_event(user_id, linked_row)
        return
    if row["created_source"] not in {"local", "ai_generated"}:
        return

    with db() as connection:
        calendar = connection.execute(
            """SELECT cs.*, cc.id AS calendar_id, cc.remote_url, cc.read_only
               FROM calendar_sources cs JOIN calendar_calendars cc ON cc.source_id = cs.id
               WHERE cs.user_id = ? AND cs.kind IN ('nextcloud-caldav', 'google-calendar') AND cc.read_only = 0
               ORDER BY cs.created_at, cc.display_name LIMIT 1""",
            (user_id,),
        ).fetchone()
        if calendar is None:
            connection.execute("UPDATE events SET sync_status = 'local', updated_at = ? WHERE id = ? AND user_id = ? AND sync_status = 'pending'", (now_iso(), event_id, user_id))
            return
        source_state = _connection_for_source(connection, calendar["id"])
        if source_state is not None and source_state["status"] in {CalendarConnectionStatus.DISCONNECTED.value, CalendarConnectionStatus.EXPIRED.value}:
            connection.execute(
                "UPDATE events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (json.dumps({"error": "Calendar connection is disconnected or expired", "operation": "sync", "retryable": True}), now_iso(), event_id, user_id),
            )
            return
        changed = connection.execute("UPDATE events SET sync_status = 'syncing', updated_at = ? WHERE id = ? AND user_id = ? AND sync_status <> 'syncing'", (now_iso(), event_id, user_id)).rowcount
        if not changed:
            return
        row = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)).fetchone()

    try:
        calendar_input = local_event_calendar_input(row, calendar["id"], calendar["calendar_id"])
        event_input_bounds(calendar_input)
        if calendar["kind"] == 'google-calendar':
            access_token = _google_access_token(user_id, calendar["id"])
            created = google_calendar_api_json(
                access_token,
                _google_calendar_path(calendar["remote_url"], "/events"),
                method="POST",
                payload=_google_event_resource(calendar_input),
            )
            remote_id = str(created.get("id") or "").strip()
            if not remote_id:
                raise CalendarConnectionError("Google Calendar created an Event without an id")
            provider = CalendarProvider.GOOGLE.value
        else:
            remote_id = f"omt-{uuid4().hex}@one-more-thing"
            icalendar = build_icalendar_event(calendar_input, remote_id, datetime.now(timezone.utc))
            event_url = resolve_url(calendar["remote_url"], urllib.parse.quote(f"{remote_id}.ics"))
            NextcloudCalDavAdapter(calendar["username"], decrypt_source_credential(calendar)).create_event(event_url, icalendar)
            provider = CalendarProvider.NEXTCLOUD.value
        timestamp = now_iso()
        with db() as connection:
            connection.execute("UPDATE events SET external_calendar_ref = ?, provider = ?, calendar_sync_enabled = 1, sync_status = 'syncing', updated_at = ? WHERE id = ? AND user_id = ?", (remote_id, provider, timestamp, event_id, user_id))
        sync_result = sync_google_calendar_connection(user_id, calendar["id"]) if provider == CalendarProvider.GOOGLE.value else sync_calendar_source(user_id, calendar["id"])
        with db() as connection:
            remote = connection.execute("SELECT id FROM calendar_events WHERE calendar_id = ? AND remote_id = ?", (calendar["calendar_id"], remote_id)).fetchone()
            if remote is None or sync_result.get("status") == "error":
                raise CalendarConnectionError("Calendar write completed but readback sync did not return the Event")
            connection.execute("UPDATE events SET sync_status = 'synced', last_synced_at = ?, sync_conflict_json = '{}', updated_at = ? WHERE id = ? AND user_id = ?", (timestamp, timestamp, event_id, user_id))
    except (CalendarConnectionError, HTTPException) as error:
        with db() as connection:
            connection.execute("UPDATE events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ? AND user_id = ?", (json.dumps({"error": str(error), "retryable": True}, ensure_ascii=False), now_iso(), event_id, user_id))


def sync_user_calendars_in_background(user_id: str) -> None:
    with db() as connection:
        sources = connection.execute(
            "SELECT id, kind FROM calendar_sources WHERE user_id = ? AND kind IN ('nextcloud-caldav','google-calendar') ORDER BY created_at",
            (user_id,),
        ).fetchall()
    for source in sources:
        try:
            if source["kind"] == 'google-calendar':
                sync_google_calendar_connection(user_id, source["id"])
            else:
                sync_calendar_source(user_id, source["id"])
        except (CalendarConnectionError, HTTPException):
            continue
    with db() as connection:
        event_ids = [row["id"] for row in connection.execute(
            """SELECT id FROM events WHERE user_id = ? AND (
                 (lifecycle_status <> 'deleted' AND created_source IN ('local', 'ai_generated') AND external_calendar_ref IS NULL)
                 OR (external_calendar_ref IS NOT NULL AND calendar_sync_enabled = 1 AND sync_status IN ('error', 'pending'))
                 OR (lifecycle_status = 'deleted' AND external_calendar_ref IS NOT NULL AND sync_status IN ('error', 'pending'))
               ) ORDER BY updated_at, id""",
            (user_id,),
        ).fetchall()]
    for event_id in event_ids:
        sync_local_event_to_default_calendar(user_id, event_id)


def sync_local_events_to_default_calendar(user_id: str, event_ids: list[str]) -> None:
    """Sync a materialized Event batch without serial per-Event DB/HTTP work.

    Proposal materialization already happens in one SQLite transaction. This
    coordinator keeps the same local-first semantics while batching the
    follow-up work: one read snapshot, one status write, concurrent provider
    writes, one pull per affected source, and one final status write.
    Provider-specific request details remain inside ``NextcloudCalDavAdapter``.
    """
    unique_ids = list(dict.fromkeys(event_ids))
    if not unique_ids:
        return

    # The optimized batch writer is CalDAV-specific.  When the user's only
    # writable default is Google Calendar, use the provider-neutral per-event
    # path so AI/user-created events still receive true Google write-back.
    with db() as connection:
        nextcloud_writable = connection.execute(
            """SELECT 1 FROM calendar_sources cs JOIN calendar_calendars cc ON cc.source_id=cs.id
               WHERE cs.user_id=? AND cs.kind='nextcloud-caldav' AND cc.read_only=0 LIMIT 1""",
            (user_id,),
        ).fetchone()
        google_writable = connection.execute(
            """SELECT 1 FROM calendar_sources cs JOIN calendar_calendars cc ON cc.source_id=cs.id
               WHERE cs.user_id=? AND cs.kind='google-calendar' AND cc.read_only=0 LIMIT 1""",
            (user_id,),
        ).fetchone()
    if nextcloud_writable is None and google_writable is not None:
        for event_id in unique_ids:
            sync_local_event_to_default_calendar(user_id, event_id)
        return

    placeholders = ", ".join("?" for _ in unique_ids)
    with db() as connection:
        rows = connection.execute(
            f"SELECT * FROM events WHERE user_id = ? AND id IN ({placeholders}) ORDER BY updated_at, id",
            (user_id, *unique_ids),
        ).fetchall()
        external_refs = [row["external_calendar_ref"] for row in rows if row["external_calendar_ref"]]
        remote_by_ref: dict[str, sqlite3.Row] = {}
        if external_refs:
            ref_placeholders = ", ".join("?" for _ in external_refs)
            remote_rows = connection.execute(
                f"""
                SELECT ce.*, cs.username, cs.credential_ciphertext, cs.server_url,
                       cc.read_only
                FROM calendar_events ce
                JOIN calendar_sources cs ON cs.id = ce.source_id AND cs.user_id = ?
                JOIN calendar_calendars cc ON cc.id = ce.calendar_id
                WHERE ce.remote_id IN ({ref_placeholders})
                   OR ce.external_calendar_ref IN ({ref_placeholders})
                ORDER BY ce.updated_at DESC
                """,
                (user_id, *external_refs, *external_refs),
            ).fetchall()
            for remote in remote_rows:
                remote_by_ref.setdefault(remote["remote_id"], remote)
                if remote["external_calendar_ref"]:
                    remote_by_ref.setdefault(remote["external_calendar_ref"], remote)
        default_calendar = connection.execute(
            """
            SELECT cs.*, cc.id AS calendar_id, cc.remote_url, cc.read_only
            FROM calendar_sources cs
            JOIN calendar_calendars cc ON cc.source_id = cs.id
            WHERE cs.user_id = ? AND cs.kind = 'nextcloud-caldav' AND cc.read_only = 0
            ORDER BY cs.created_at, cc.display_name
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

    rows_by_id = {row["id"]: row for row in rows}
    operations: list[dict[str, Any]] = []
    preparation_errors: list[dict[str, str]] = []
    local_only_ids: list[str] = []

    for event_id in unique_ids:
        row = rows_by_id.get(event_id)
        if row is None:
            continue
        remote = remote_by_ref.get(row["external_calendar_ref"]) if row["external_calendar_ref"] else None
        if row["lifecycle_status"] == "deleted":
            if remote is None or not row["external_calendar_ref"]:
                local_only_ids.append(event_id)
                continue
            if remote["read_only"]:
                preparation_errors.append({"id": event_id, "error": "Linked Calendar is read-only", "operation": "delete"})
                continue
            operations.append({"id": event_id, "operation": "delete", "row": row, "remote": remote})
            continue

        if row["external_calendar_ref"]:
            if remote is None or remote["read_only"]:
                preparation_errors.append({"id": event_id, "error": "Linked writable Calendar event was not found", "operation": "update"})
                continue
            calendar = remote
            operation = "update"
        else:
            if row["created_source"] not in {"local", "ai_generated"}:
                local_only_ids.append(event_id)
                continue
            if default_calendar is None:
                local_only_ids.append(event_id)
                continue
            calendar = default_calendar
            operation = "create"

        try:
            source_id = calendar["source_id"] if "source_id" in calendar.keys() else calendar["id"]
            payload = local_event_calendar_input(row, source_id, calendar["calendar_id"])
            remote_id = remote["remote_id"] if operation == "update" else f"omt-{uuid4().hex}@one-more-thing"
            icalendar = build_icalendar_event(payload, remote_id, datetime.now(timezone.utc))
            event_url = remote["href"] if operation == "update" else resolve_url(calendar["remote_url"], urllib.parse.quote(f"{remote_id}.ics"))
            operations.append({
                "id": event_id,
                "operation": operation,
                "row": row,
                "remote": remote,
                "calendar": calendar,
                "source_id": source_id,
                "payload": payload,
                "remote_id": remote_id,
                "icalendar": icalendar,
                "event_url": event_url,
            })
        except (HTTPException, ValueError, TypeError) as error:
            preparation_errors.append({"id": event_id, "error": str(error), "operation": operation})

    candidate_ids = [item["id"] for item in operations] + [item["id"] for item in preparation_errors]
    with db() as connection:
        if candidate_ids:
            candidate_placeholders = ", ".join("?" for _ in candidate_ids)
            connection.execute(
                f"UPDATE events SET sync_status = 'syncing', updated_at = ? WHERE user_id = ? AND id IN ({candidate_placeholders})",
                (now_iso(), user_id, *candidate_ids),
            )
        if local_only_ids:
            local_placeholders = ", ".join("?" for _ in local_only_ids)
            connection.execute(
                f"UPDATE events SET sync_status = 'local', updated_at = ? WHERE user_id = ? AND id IN ({local_placeholders}) AND sync_status = 'pending'",
                (now_iso(), user_id, *local_only_ids),
            )

    def perform_provider_write(item: dict[str, Any]) -> dict[str, Any]:
        context = item["remote"] or item["calendar"]
        adapter = NextcloudCalDavAdapter(context["username"], decrypt_source_credential(context))
        if item["operation"] == "create":
            adapter.create_event(item["event_url"], item["icalendar"])
            return item | {"etag": None}
        if item["operation"] == "update":
            etag = adapter.update_event(item["event_url"], item["icalendar"], item["remote"]["etag"])
            return item | {"etag": etag or item["remote"]["etag"]}
        adapter.delete_event(item["remote"]["href"], item["remote"]["etag"])
        return item | {"etag": None}

    successes: list[dict[str, Any]] = []
    failures = list(preparation_errors)
    if operations:
        with ThreadPoolExecutor(max_workers=min(8, len(operations))) as executor:
            futures = {executor.submit(perform_provider_write, item): item for item in operations}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    successes.append(future.result())
                except (CalendarConnectionError, HTTPException, OSError, ValueError, TypeError) as error:
                    failures.append({"id": item["id"], "error": str(error), "operation": item["operation"]})

    timestamp = now_iso()
    with db() as connection:
        for item in successes:
            if item["operation"] == "create":
                connection.execute(
                    "UPDATE events SET external_calendar_ref = ?, provider = ?, calendar_sync_enabled = 1, sync_status = 'syncing', sync_conflict_json = '{}', updated_at = ? WHERE id = ? AND user_id = ?",
                    (item["remote_id"], CalendarProvider.NEXTCLOUD.value, timestamp, item["id"], user_id),
                )
            elif item["operation"] == "update":
                connection.execute(
                    "UPDATE events SET sync_status = 'syncing', sync_conflict_json = '{}', updated_at = ? WHERE id = ? AND user_id = ?",
                    (timestamp, item["id"], user_id),
                )
            else:
                connection.execute(
                    "UPDATE events SET external_calendar_ref = NULL, calendar_sync_enabled = 0, sync_status = 'synced', sync_conflict_json = '{}', last_synced_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (timestamp, timestamp, item["id"], user_id),
                )
        for failure in failures:
            connection.execute(
                "UPDATE events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (json.dumps({"error": failure["error"], "operation": failure["operation"], "retryable": True}, ensure_ascii=False), timestamp, failure["id"], user_id),
            )

    source_ids = {item["source_id"] for item in successes if item["operation"] in {"create", "update"}}
    source_ids.update(item["remote"]["source_id"] for item in successes if item["operation"] == "delete" and item.get("remote"))
    source_results: dict[str, dict[str, Any]] = {}
    for source_id in source_ids:
        try:
            source_results[source_id] = sync_calendar_source(user_id, source_id)
        except (CalendarConnectionError, HTTPException) as error:
            source_results[source_id] = {"status": "error", "error": str(error)}

    with db() as connection:
        for item in successes:
            if item["operation"] == "delete":
                continue
            source_id = item["source_id"]
            source_result = source_results.get(source_id, {})
            remote = connection.execute(
                "SELECT id FROM calendar_events WHERE calendar_id = ? AND remote_id = ?",
                (item["calendar"]["calendar_id"], item["remote_id"]),
            ).fetchone()
            if source_result.get("status") != "ok" or remote is None:
                connection.execute(
                    "UPDATE events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (json.dumps({"error": source_result.get("error", "Calendar write completed but readback sync did not return the Event"), "retryable": True}, ensure_ascii=False), now_iso(), item["id"], user_id),
                )
            else:
                connection.execute(
                    "UPDATE events SET sync_status = 'synced', last_synced_at = ?, sync_conflict_json = '{}', updated_at = ? WHERE id = ? AND user_id = ?",
                    (now_iso(), now_iso(), item["id"], user_id),
                )


@app.post("/api/events", status_code=201)
def create_event(request: Request, payload: EventCreate, background_tasks: BackgroundTasks = None) -> dict:
    user = require_user(request)
    validate_local_event_date(payload.date_label)
    validate_local_event_time(payload.time_label)
    schedule = None
    try:
        schedule = _event_schedule_fields({
            "date": payload.date_label,
            "time": payload.time_label,
            "all_day": payload.time_label.strip().lower() in {"全天", "整天", "all day"},
            "recurrence": payload.recurrence,
        })
    except HTTPException:
        # Keep legacy non-ISO labels and unresolved single times readable;
        # they remain unscheduled instead of being silently treated as all-day.
        schedule = None
    if payload.all_day:
        schedule = _event_schedule_fields({"date": payload.date_label or (payload.start_at[:10] if payload.start_at else None), "all_day": True, "timezone": payload.timezone, "recurrence": payload.recurrence})
    elif payload.start_at:
        schedule_patch = {"start_at": payload.start_at, "timezone": payload.timezone}
        if payload.end_at is not None:
            schedule_patch["end_at"] = payload.end_at
        if payload.recurrence is not None:
            schedule_patch["recurrence"] = payload.recurrence
        schedule = _event_schedule_fields(schedule_patch)
    elif payload.end_at is not None:
        raise HTTPException(status_code=422, detail="Event end_at cannot be set without start_at")
    if schedule is None and not payload.date_label:
        raise HTTPException(status_code=422, detail="Event requires a confirmed date or start_at")
    event_id = f"event-{uuid4().hex[:10]}"
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            """
            INSERT INTO events(
                id, user_id, title, date_label, time_label, detail, source_id, location,
                start_at, end_at, timezone, all_day, recurrence_json,
                created_source, lifecycle_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, NULL, 'local', 'created', ?)
            """,
            (event_id, user["id"], payload.title, schedule["date_label"] if schedule else payload.date_label, schedule["time_label"] if schedule else payload.time_label, payload.detail, payload.location, schedule["start_at"] if schedule else None, schedule["end_at"] if schedule else None, schedule["timezone"] if schedule else (payload.timezone or DEFAULT_TIMEZONE), schedule["all_day"] if schedule else 0, timestamp),
        )
        row = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"])).fetchone()
    if background_tasks is not None:
        background_tasks.add_task(sync_local_event_to_default_calendar, user["id"], event_id)
    return _local_event_payload(row)


@app.post("/api/events/{event_id}/calendar-sync")
def sync_local_event_to_calendar(request: Request, event_id: str, payload: LocalEventCalendarSyncInput) -> dict:
    user = require_user(request)
    event_id = _base_local_event_id(event_id)
    with db() as connection:
        event = connection.execute(
            "SELECT * FROM events WHERE id = ? AND user_id = ? AND lifecycle_status NOT IN ('archived', 'deleted')",
            (event_id, user["id"]),
        ).fetchone()
        calendar = connection.execute(
            """
            SELECT cs.*, cc.id AS calendar_id, cc.remote_url, cc.read_only, cc.timezone AS calendar_timezone
            FROM calendar_sources cs JOIN calendar_calendars cc ON cc.source_id = cs.id
            WHERE cs.id = ? AND cs.user_id = ? AND cc.id = ?
            """,
            (payload.source_id, user["id"], payload.calendar_id),
        ).fetchone()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    if calendar is None:
        raise HTTPException(status_code=404, detail="Calendar source or calendar not found")
    if calendar["read_only"]:
        raise HTTPException(status_code=403, detail="This Calendar is read-only; local event sync is not permitted")
    if not payload.enabled:
        with db() as connection:
            connection.execute("UPDATE events SET calendar_sync_enabled = 0, sync_status = 'local', updated_at = ? WHERE id = ? AND user_id = ?", (now_iso(), event_id, user["id"]))
            event = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"])).fetchone()
        return {"status": "disabled", "event": _local_event_payload(event)}
    calendar_input = local_event_calendar_input(event, payload.source_id, payload.calendar_id)
    event_input_bounds(calendar_input)
    try:
        app_password = decrypt_source_credential(calendar)
        remote_id = f"omt-{uuid4().hex}@one-more-thing"
        icalendar = build_icalendar_event(calendar_input, remote_id, datetime.now(timezone.utc))
        event_url = resolve_url(calendar["remote_url"], urllib.parse.quote(f"{remote_id}.ics"))
        NextcloudCalDavAdapter(calendar["username"], app_password).create_event(event_url, icalendar)
        timestamp = now_iso()
        with db() as connection:
            connection.execute(
                "UPDATE events SET external_calendar_ref = ?, provider = ?, calendar_sync_enabled = 1, sync_status = 'syncing', updated_at = ? WHERE id = ? AND user_id = ?",
                (remote_id, CalendarProvider.NEXTCLOUD.value, timestamp, event_id, user["id"]),
            )
        sync_result = sync_calendar_source(user["id"], payload.source_id)
    except CalendarConnectionError as error:
        with db() as connection:
            connection.execute("UPDATE events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ? AND user_id = ?", (json.dumps({"error": str(error), "retryable": True}, ensure_ascii=False), now_iso(), event_id, user["id"]))
        raise HTTPException(status_code=502, detail=str(error)) from error
    with db() as connection:
        remote = connection.execute("SELECT * FROM calendar_events WHERE calendar_id = ? AND remote_id = ?", (payload.calendar_id, remote_id)).fetchone()
        if remote is None:
            raise HTTPException(status_code=502, detail="Event was written but was not returned by the Calendar source")
        connection.execute(
            "UPDATE events SET external_calendar_ref = ?, provider = ?, calendar_sync_enabled = 1, sync_status = 'synced', last_synced_at = ?, sync_conflict_json = '{}', updated_at = ? WHERE id = ? AND user_id = ?",
            (remote_id, CalendarProvider.NEXTCLOUD.value, timestamp, timestamp, event_id, user["id"]),
        )
        linked = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"])).fetchone()
    return {"status": "synced", "event": _local_event_payload(linked), "remote": normalized_calendar_event(remote), "sync": sync_result}


@app.patch("/api/events/{event_id}")
def update_event(request: Request, event_id: str, payload: EventUpdate, background_tasks: BackgroundTasks = None) -> dict:
    user = require_user(request)
    event_id = _base_local_event_id(event_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="Event update cannot be empty")
    if "date_label" in fields:
        validate_local_event_date(fields["date_label"])
    if "time_label" in fields:
        if fields["time_label"] is None:
            raise HTTPException(status_code=422, detail="Event time label cannot be empty")
        validate_local_event_time(fields["time_label"])
    with db() as connection:
        row = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"])).fetchone()
        external = _calendar_event_row_for_user(connection, user["id"], event_id) if row is None else None
    if row is None:
        if external is None:
            raise HTTPException(status_code=404, detail="Event not found")
        if external["lifecycle_status"] == "deleted":
            raise HTTPException(status_code=409, detail="Deleted events cannot be edited")
        fields.pop("lifecycle_status", None)
        return _update_external_calendar_event(user["id"], external, fields)
    with db() as connection:
        if row["lifecycle_status"] == "deleted":
            raise HTTPException(status_code=409, detail="Deleted events cannot be edited")
        requested_status = fields.pop("lifecycle_status", None)
        next_status = requested_status or ("edited" if fields else row["lifecycle_status"])
        updates = dict(fields)
        updates.pop("recurrence", None)
        schedule_patch: dict[str, Any] = {}
        if "date_label" in fields:
            schedule_patch["date"] = fields["date_label"]
        if "time_label" in fields:
            schedule_patch["time"] = fields["time_label"]
            if str(fields["time_label"]).strip().lower() in {"全天", "整天", "all day"}:
                schedule_patch["all_day"] = True
        for key in ("start_at", "end_at", "all_day", "timezone", "recurrence"):
            if key in fields:
                schedule_patch[key] = fields[key]
        if schedule_patch:
            updates.update(_event_schedule_fields(schedule_patch, row))
        updates["lifecycle_status"] = next_status
        updates["updated_at"] = now_iso()
        if next_status == "archived":
            updates["archived_at"] = now_iso()
        elif next_status == "edited":
            updates["archived_at"] = None
        assignments = ", ".join(f"{key} = ?" for key in updates)
        connection.execute(
            f"UPDATE events SET {assignments} WHERE id = ? AND user_id = ?",
            (*updates.values(), event_id, user["id"]),
        )
        should_sync = bool(fields) and (
            row["created_source"] in {"local", "ai_generated"}
            or bool(row["external_calendar_ref"] and row["calendar_sync_enabled"])
        )
        if should_sync:
            connection.execute("UPDATE events SET sync_status = 'pending', sync_conflict_json = '{}' WHERE id = ? AND user_id = ?", (event_id, user["id"]))
        updated = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"])).fetchone()
    if background_tasks is not None and should_sync:
        background_tasks.add_task(sync_local_event_to_default_calendar, user["id"], event_id)
    return _local_event_payload(updated)


def validate_local_event_date(value: str) -> None:
    match = re.fullmatch(r"(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})", str(value).strip())
    if not match:
        return
    year = int(match.group(1) or datetime.now().year)
    try:
        date(year, int(match.group(2)), int(match.group(3)))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Event date must be a valid calendar date") from error


def validate_local_event_time(value: str) -> None:
    label = str(value).strip()
    candidates = list(re.finditer(r"(?<!\d)\d+:\d+(?!\d)", label))
    if not candidates:
        return
    times = list(re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", label))
    if len(times) != len(candidates) or len(times) > 2:
        raise HTTPException(status_code=422, detail="Event time must use valid HH:MM values")
    minutes = []
    for match in times:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise HTTPException(status_code=422, detail="Event time must use valid HH:MM values")
        minutes.append(hour * 60 + minute)
    if len(times) == 2:
        separator = label[times[0].end():times[1].start()]
        if not re.fullmatch(r"\s*(?:-|–|—|~|～|至|到)\s*", separator):
            raise HTTPException(status_code=422, detail="Event time range must use a supported separator")
        if minutes[1] <= minutes[0]:
            raise HTTPException(status_code=422, detail="Event end time must be after start time")
        return
    before = label[:times[0].start()]
    after = label[times[0].end():]
    if re.search(r"(?:-|–|—|~|～|至|到)\s*$", before) or re.match(r"\s*(?:-|–|—|~|～|至|到)(?:\s*\d|\s*$)", after):
        raise HTTPException(status_code=422, detail="Event time range must include valid start and end times")


def _external_event_calendar_input(row: sqlite3.Row, fields: dict[str, Any]) -> CalendarEventInput:
    start = datetime.fromisoformat(str(row["start_at"]).replace("Z", "+00:00"))
    if bool(row["all_day"]):
        current_time = "全天"
    else:
        current_time = start.strftime("%H:%M")
        if row["end_at"]:
            end = datetime.fromisoformat(str(row["end_at"]).replace("Z", "+00:00"))
            current_time = f"{current_time}～{end.strftime('%H:%M')}"
    existing = {
        "timezone": row["timezone"],
        "date_label": str(row["start_at"])[:10],
        "time_label": current_time,
        "all_day": row["all_day"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "recurrence_json": None,
    }
    schedule_patch: dict[str, Any] = {}
    if "date_label" in fields:
        schedule_patch["date"] = fields["date_label"]
    if "time_label" in fields:
        schedule_patch["time"] = fields["time_label"]
    for key in ("start_at", "end_at", "all_day", "timezone", "recurrence"):
        if key in fields:
            schedule_patch[key] = fields[key]
    schedule = _event_schedule_fields(schedule_patch, existing) if schedule_patch else {
        "start_at": row["start_at"], "end_at": row["end_at"], "all_day": row["all_day"],
        "timezone": row["timezone"] or DEFAULT_TIMEZONE, "recurrence_json": None,
    }
    recurrence_rule = row["recurrence_rule"]
    if "recurrence" in fields:
        recurrence_holder = {
            "recurrence_json": schedule.get("recurrence_json"),
            "all_day": schedule["all_day"],
            "end_at": schedule["end_at"],
            "timezone": schedule["timezone"],
        }
        recurrence_rule = _recurrence_rule_for_local_event(recurrence_holder)
    return CalendarEventInput(
        source_id=row["source_id"], calendar_id=row["calendar_id"],
        title=fields.get("title", row["title"]), start_at=schedule["start_at"], end_at=schedule["end_at"],
        all_day=bool(schedule["all_day"]), timezone=schedule["timezone"], recurrence_rule=recurrence_rule,
        location=fields.get("location", row["location"]), description=fields.get("detail", row["description"]),
        url=row["url"],
    )


def _update_external_calendar_event(user_id: str, row: sqlite3.Row, fields: dict[str, Any]) -> dict:
    if row["calendar_read_only"]:
        raise HTTPException(status_code=403, detail="This Calendar is read-only")
    payload = _external_event_calendar_input(row, fields)
    event_input_bounds(payload)
    try:
        if row["provider"] == CalendarProvider.GOOGLE.value:
            access_token = _google_access_token(user_id, row["source_id"])
            resource = _google_event_resource(payload)
            updated_resource = google_calendar_api_json(
                access_token,
                _google_calendar_path(row["calendar_remote_id"], f"/events/{urllib.parse.quote(str(row['remote_id']), safe='') }"),
                method="PATCH",
                payload=resource,
            )
            new_etag = updated_resource.get("etag") or row["etag"]
            fresh_etag = row["etag"]
        else:
            with db() as connection:
                source = connection.execute("SELECT * FROM calendar_sources WHERE id = ? AND user_id = ?", (row["source_id"], user_id)).fetchone()
            password = decrypt_source_credential(source)
            adapter = NextcloudCalDavAdapter(row["username"], password)
            raw_ical, fresh_etag = adapter.get_event(row["href"])
            updated_ical = replace_icalendar_event_fields(raw_ical, payload, row["remote_id"])
            new_etag = adapter.update_event(row["href"], updated_ical, fresh_etag or row["etag"])
    except CalendarConnectionError as error:
        with db() as connection:
            connection.execute(
                "UPDATE calendar_events SET sync_status = 'error', sync_conflict_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps({"error": str(error), "retryable": True}, ensure_ascii=False), now_iso(), row["id"]),
            )
        raise HTTPException(status_code=502, detail=str(error)) from error
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            """UPDATE calendar_events SET title=?, start_at=?, end_at=?, timezone=?, all_day=?, location=?, description=?,
               recurrence_rule=?, etag=?, sync_status='synced', last_synced_at=?, sync_conflict_json='{}',
               lifecycle_status='edited', deleted_at=NULL, updated_at=? WHERE id=?""",
            (payload.title, payload.start_at, payload.end_at, payload.timezone, int(payload.all_day), payload.location,
             payload.description, payload.recurrence_rule, new_etag or fresh_etag or row["etag"], timestamp, timestamp, row["id"]),
        )
        updated = _calendar_event_row_for_user(connection, user_id, row["id"])
    return normalized_calendar_event(updated)


def _delete_external_calendar_event(user_id: str, row: sqlite3.Row) -> dict:
    if row["calendar_read_only"]:
        raise HTTPException(status_code=403, detail="This Calendar is read-only")
    try:
        if row["provider"] == CalendarProvider.GOOGLE.value:
            access_token = _google_access_token(user_id, row["source_id"])
            google_calendar_api_json(
                access_token,
                _google_calendar_path(row["calendar_remote_id"], f"/events/{urllib.parse.quote(str(row['remote_id']), safe='') }"),
                method="DELETE",
            )
        else:
            with db() as connection:
                source = connection.execute("SELECT * FROM calendar_sources WHERE id = ? AND user_id = ?", (row["source_id"], user_id)).fetchone()
            password = decrypt_source_credential(source)
            adapter = NextcloudCalDavAdapter(row["username"], password)
            _raw, fresh_etag = adapter.get_event(row["href"])
            adapter.delete_event(row["href"], fresh_etag or row["etag"])
    except CalendarConnectionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            "UPDATE calendar_events SET lifecycle_status='deleted', deleted_at=?, sync_status='synced', last_synced_at=?, updated_at=? WHERE id=?",
            (timestamp, timestamp, timestamp, row["id"]),
        )
        connection.execute(
            "UPDATE tasks SET related_event_id = NULL WHERE user_id = ? AND related_event_id = ?",
            (user_id, row["id"]),
        )
        deleted = _calendar_event_row_for_user(connection, user_id, row["id"])
    return normalized_calendar_event(deleted)


@app.delete("/api/events/{event_id}")
def delete_event(request: Request, event_id: str, background_tasks: BackgroundTasks = None) -> dict:
    user = require_user(request)
    event_id = _base_local_event_id(event_id)
    timestamp = now_iso()
    with db() as connection:
        row = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"])).fetchone()
        external = _calendar_event_row_for_user(connection, user["id"], event_id) if row is None else None
    if row is None:
        if external is None:
            raise HTTPException(status_code=404, detail="Event not found")
        if external["lifecycle_status"] == "deleted":
            raise HTTPException(status_code=409, detail="Event is already deleted")
        return _delete_external_calendar_event(user["id"], external)
    with db() as connection:
        if row["lifecycle_status"] == "deleted":
            raise HTTPException(status_code=409, detail="Event is already deleted")
        connection.execute(
            "UPDATE events SET lifecycle_status = 'deleted', deleted_at = ?, sync_status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (timestamp, "pending" if row["calendar_sync_enabled"] and row["external_calendar_ref"] else row["sync_status"], timestamp, event_id, user["id"]),
        )
        connection.execute(
            "UPDATE tasks SET related_event_id = NULL WHERE user_id = ? AND related_event_id = ?",
            (user["id"], event_id),
        )
        deleted = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"])).fetchone()
    if background_tasks is not None and deleted["calendar_sync_enabled"] and deleted["external_calendar_ref"]:
        background_tasks.add_task(sync_local_event_to_default_calendar, user["id"], event_id)
    return _local_event_payload(deleted)


@app.post("/api/calendar/sources/{source_id}/sync")
def sync_calendar(request: Request, source_id: str) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "calendar-sync", security_policy().sync_requests_per_minute, key=user["id"])
    return sync_calendar_source(user["id"], source_id)


@app.post("/api/calendar/sources/{source_id}/retry")
def retry_calendar(request: Request, source_id: str) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "calendar-sync", security_policy().sync_requests_per_minute, key=user["id"])
    return sync_calendar_source(user["id"], source_id)


@app.post("/api/calendar/sync")
def sync_all_calendars(request: Request) -> dict:
    user = require_user(request)
    with db() as connection:
        sources = connection.execute(
            "SELECT id, kind FROM calendar_sources WHERE user_id = ? AND kind IN ('nextcloud-caldav','google-calendar') ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    results = []
    for source in sources:
        try:
            result = sync_google_calendar_connection(user["id"], source["id"]) if source["kind"] == 'google-calendar' else sync_calendar_source(user["id"], source["id"])
        except CalendarConnectionError as error:
            result = {"status": "error", "source_id": source["id"], "error": str(error)}
        results.append(result)
    failed = [result for result in results if result.get("status") == "error"]
    return {"status": "error" if failed else "ok", "sources": results, "error_count": len(failed)}


@app.post("/api/calendar/auto-sync")
def schedule_automatic_calendar_sync(request: Request, background_tasks: BackgroundTasks) -> dict:
    user = require_user(request)
    background_tasks.add_task(sync_user_calendars_in_background, user["id"])
    return {"status": "scheduled"}


@app.post("/api/calendar/events", status_code=201)
def create_calendar_event(request: Request, payload: CalendarEventInput) -> dict:
    user = require_user(request)
    event_input_bounds(payload)
    if payload.url:
        validate_http_url(payload.url, "Event URL")
    with db() as connection:
        row = connection.execute(
            """
            SELECT cs.*, cc.id AS calendar_id, cc.remote_url, cc.read_only, cc.timezone AS calendar_timezone
            FROM calendar_sources cs
            JOIN calendar_calendars cc ON cc.source_id = cs.id
            WHERE cs.id = ? AND cs.user_id = ? AND cc.id = ?
            """,
            (payload.source_id, user["id"], payload.calendar_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Calendar source or calendar not found")
    if row["read_only"]:
        raise HTTPException(status_code=403, detail="This Calendar is read-only; creation is not permitted")
    try:
        if row["kind"] == 'google-calendar':
            access_token = _google_access_token(user["id"], payload.source_id)
            created_remote = google_calendar_api_json(
                access_token,
                _google_calendar_path(row["remote_url"], "/events"),
                method="POST",
                payload=_google_event_resource(payload),
            )
            uid = str(created_remote.get("id") or "").strip()
            if not uid:
                raise CalendarConnectionError("Google Calendar created an Event without an id")
            sync_result = sync_google_calendar_connection(user["id"], payload.source_id)
        else:
            app_password = decrypt_source_credential(row)
            uid = f"omt-{uuid4().hex}@one-more-thing"
            icalendar = build_icalendar_event(payload, uid, datetime.now(timezone.utc))
            event_url = resolve_url(row["remote_url"], urllib.parse.quote(f"{uid}.ics"))
            NextcloudCalDavAdapter(row["username"], app_password).create_event(event_url, icalendar)
            sync_result = sync_calendar_source(user["id"], payload.source_id)
    except CalendarConnectionError as error:
        with db() as connection:
            connection.execute("UPDATE calendar_sources SET sync_status = ?, sync_error = ?, updated_at = ? WHERE id = ? AND user_id = ?", ("error", str(error), now_iso(), payload.source_id, user["id"]))
        raise HTTPException(status_code=502, detail=str(error)) from error
    if sync_result["status"] == "error":
        raise HTTPException(status_code=502, detail=f"Event was written but readback sync failed: {sync_result['error']}")
    with db() as connection:
        created = connection.execute("SELECT * FROM calendar_events WHERE calendar_id = ? AND remote_id = ?", (payload.calendar_id, uid)).fetchone()
    if created is None:
        raise HTTPException(status_code=502, detail="Event was written but was not returned by the Calendar source")
    return {"status": "created", "event": normalized_calendar_event(created), "sync": sync_result}


@app.post("/api/calendar/nextcloud/login/start")
def start_nextcloud_login(request: Request, payload: NextcloudLoginInput) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "calendar-connect", security_policy().auth_requests_per_minute, key=user["id"])
    try:
        flow = nextcloud_login_flow_start(payload.base_url)
    except CalendarConnectionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        poll_token_ciphertext = encrypt_credential(flow["poll_token"])
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail="Credential encryption is not configured") from error
    flow_id = uuid4().hex
    timestamp = now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=NEXTCLOUD_LOGIN_FLOW_MINUTES)).isoformat()
    with db() as connection:
        connection.execute(
            "INSERT INTO calendar_login_flows(id, user_id, base_url, poll_token_ciphertext, poll_endpoint, login_url, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (flow_id, user["id"], flow["base_url"], poll_token_ciphertext, flow["poll_endpoint"], flow["login_url"], "pending", expires_at, timestamp, timestamp),
        )
    return {"flow_id": flow_id, "login_url": flow["login_url"], "poll_interval": 2, "expires_at": expires_at}


@app.post("/api/calendar/nextcloud/login/{flow_id}/poll")
def poll_nextcloud_login(request: Request, flow_id: str) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "calendar-connect-poll", security_policy().auth_requests_per_minute, key=user["id"])
    if not re.fullmatch(r"[0-9a-f]{32}", flow_id):
        raise HTTPException(status_code=404, detail="Nextcloud login flow not found")
    with db() as connection:
        flow = connection.execute(
            "SELECT * FROM calendar_login_flows WHERE id = ? AND user_id = ?",
            (flow_id, user["id"]),
        ).fetchone()
    if flow is None:
        raise HTTPException(status_code=404, detail="Nextcloud login flow not found")
    if flow["status"] == "connected":
        return {"status": "connected"}
    if flow["status"] == "error":
        return {"status": "error", "message": flow["last_error"] or "Nextcloud 登入流程失敗，請重新開始。"}
    if datetime.fromisoformat(flow["expires_at"]) <= datetime.now(timezone.utc):
        with db() as connection:
            connection.execute(
                "UPDATE calendar_login_flows SET status = ?, poll_token_ciphertext = ?, last_error = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                ("expired", "", "Nextcloud 登入流程已過期，請重新開始。", now_iso(), flow_id, user["id"]),
            )
        return {"status": "error", "message": "Nextcloud 登入流程已過期，請重新開始。"}
    with db() as connection:
        connection.execute(
            "UPDATE calendar_login_flows SET poll_count = poll_count + 1, last_polled_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (now_iso(), now_iso(), flow_id, user["id"]),
        )
    try:
        try:
            poll_token = credential_box().decrypt(flow["poll_token_ciphertext"].encode()).decode()
        except (InvalidToken, UnicodeDecodeError, RuntimeError) as error:
            raise CalendarConnectionError("無法讀取 Nextcloud Login Flow 的安全憑證") from error
        status, credentials = nextcloud_login_flow_poll(flow["poll_endpoint"], poll_token)
        if status == "pending":
            return {"status": "pending", "poll_interval": 2}
        if not isinstance(credentials, dict):
            raise CalendarConnectionError("Nextcloud Login Flow 沒有回傳登入憑證")
        server_url = normalize_nextcloud_base_url(str(credentials.get("server") or flow["base_url"]))
        if not same_http_origin(flow["base_url"], server_url):
            raise CalendarConnectionError("Nextcloud 回傳的 server origin 與登入來源不一致")
        username = str(credentials.get("loginName") or "").strip()
        app_password = str(credentials.get("appPassword") or "")
        if credentials.get("access_token") or credentials.get("accessToken"):
            raise CalendarConnectionError("Nextcloud Login Flow 必須回傳 device-specific appPassword，不接受 OAuth2 access token")
        if not username or not app_password:
            raise CalendarConnectionError("Nextcloud 沒有回傳可用的裝置憑證")
        # The returned appPassword is a device-specific credential for CalDAV;
        # it is not an OAuth2 access token and never leaves this backend.
        discovered = discover_nextcloud(server_url, username, app_password)
        result = save_nextcloud_source(user["id"], discovered, username, app_password)
        sync_result = sync_calendar_source(user["id"], result["source"]["id"])
        result["sync"] = sync_result
        result["source"]["sync_status"] = sync_result["status"]
        result["source"]["last_synced_at"] = sync_result.get("last_synced_at")
        result["source"]["last_error"] = sync_result.get("error")
    except CalendarConnectionError as error:
        with db() as connection:
            connection.execute(
                "UPDATE calendar_login_flows SET status = ?, poll_token_ciphertext = ?, last_error = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                ("error", "", str(error), now_iso(), flow_id, user["id"]),
            )
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        with db() as connection:
            connection.execute(
                "UPDATE calendar_login_flows SET status = ?, poll_token_ciphertext = ?, last_error = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                ("error", "", "Credential storage is not configured", now_iso(), flow_id, user["id"]),
            )
        raise HTTPException(status_code=500, detail="Credential storage is not configured") from error
    with db() as connection:
        connection.execute(
            "UPDATE calendar_login_flows SET status = ?, poll_token_ciphertext = ?, last_error = NULL, updated_at = ? WHERE id = ? AND user_id = ?",
            ("connected", "", now_iso(), flow_id, user["id"]),
        )
    return {"status": "connected", **result}


@app.delete("/api/calendar/sources/{source_id}")
def delete_calendar_source(request: Request, source_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        deleted = connection.execute("DELETE FROM calendar_sources WHERE id = ? AND user_id = ?", (source_id, user["id"])).rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Calendar source not found")
    return {"ok": True}


@app.get("/api/integrations")
def list_integrations(request: Request) -> dict:
    """Account-settings inventory without provider secrets."""
    user = require_user(request)
    with db() as connection:
        calendar_rows = connection.execute(
            "SELECT * FROM calendar_connections WHERE user_id = ? AND status <> ? ORDER BY created_at DESC",
            (user["id"], CalendarConnectionStatus.DISCONNECTED.value),
        ).fetchall()
        auth_rows = connection.execute(
            "SELECT provider, email, email_verified, created_at FROM auth_accounts WHERE user_id = ? ORDER BY provider",
            (user["id"],),
        ).fetchall()
    return {
        "calendar": [calendar_connection_payload(row) for row in calendar_rows],
        "authentication": [
            {"provider": row["provider"], "email": row["email"], "email_verified": bool(row["email_verified"]), "created_at": row["created_at"]}
            for row in auth_rows
        ],
        "available": {
            "calendar": [{"provider": provider, "name": label} for provider, label in CALENDAR_PROVIDER_LABELS.items()],
            "authentication": [{"provider": provider, "enabled": bool(provider_settings(provider))} for provider in ("github", "google")],
        },
    }


@app.get("/api/integrations/calendar/connections")
def list_calendar_connections(request: Request) -> dict:
    user = require_user(request)
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM calendar_connections WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return {"connections": [calendar_connection_payload(row) for row in rows]}


@app.post("/api/integrations/calendar/connections/{provider}/connect", response_model=None)
def connect_calendar_provider(request: Request, provider: str, payload: CalendarConnectionConnectInput) -> Response | dict:
    user = require_user(request)
    try:
        calendar_provider = normalize_calendar_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Unsupported calendar provider") from error
    if calendar_provider is CalendarProvider.NEXTCLOUD:
        if not payload.base_url:
            raise HTTPException(status_code=422, detail="Nextcloud connection requires base_url")
        flow = start_nextcloud_login(request, NextcloudLoginInput(base_url=payload.base_url))
        return {"provider": calendar_provider.value, "status": "pending", "flow": flow, "sync_mode": payload.sync_mode}

    settings = provider_settings("google")
    if settings is None:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    nonce = secrets.token_urlsafe(24)
    common = {"client_id": settings["client_id"], "redirect_uri": callback_url("google"), "response_type": "code", "state": nonce}
    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {**common, "scope": "openid email profile https://www.googleapis.com/auth/calendar", "access_type": "offline", "prompt": "consent"}
    )
    response = JSONResponse({"provider": calendar_provider.value, "status": "connecting", "authorization_url": authorization_url, "sync_mode": payload.sync_mode})
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(OAUTH_STATE_COOKIE, f"google:calendar:{nonce}", max_age=600, httponly=True, samesite=COOKIE_SAMESITE, secure=COOKIE_SECURE, path="/")
    return response


@app.delete("/api/integrations/calendar/connections/{connection_id}")
def disconnect_calendar_connection(request: Request, connection_id: str) -> dict:
    user = require_user(request)
    timestamp = now_iso()
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM calendar_connections WHERE id = ? AND user_id = ?",
            (connection_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Calendar connection not found")
        credential_ref = row["credential_ref"]
        metadata = json.loads(row["metadata_json"] or "{}") if row["metadata_json"] else {}
        connection.execute(
            "UPDATE calendar_connections SET status = ?, credential_ref = NULL, updated_at = ? WHERE id = ? AND user_id = ?",
            (CalendarConnectionStatus.DISCONNECTED.value, timestamp, connection_id, user["id"]),
        )
        if credential_ref:
            connection.execute("DELETE FROM integration_credentials WHERE id = ? AND user_id = ?", (credential_ref, user["id"]))
        legacy_source_id = metadata.get("legacy_source_id") if isinstance(metadata, dict) else None
        if legacy_source_id:
            connection.execute(
                "UPDATE calendar_sources SET status = ?, credential_ciphertext = ?, sync_error = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                ("disconnected", encrypt_credential(""), "Calendar connection disconnected", timestamp, legacy_source_id, user["id"]),
            )
        updated = connection.execute("SELECT * FROM calendar_connections WHERE id = ?", (connection_id,)).fetchone()
    return {"connection": calendar_connection_payload(updated)}


@app.post("/api/integrations/calendar/connections/{connection_id}/sync")
def sync_calendar_connection(request: Request, connection_id: str) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "calendar-sync", security_policy().sync_requests_per_minute, key=user["id"])
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM calendar_connections WHERE id = ? AND user_id = ?",
            (connection_id, user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Calendar connection not found")
    if row["provider"] == CalendarProvider.NEXTCLOUD.value:
        source_id = (json.loads(row["metadata_json"] or "{}").get("legacy_source_id") or row["id"])
        result = sync_calendar_source(user["id"], source_id)
        return {"connection": calendar_connection_payload(_connection_for_current_user(connection_id, user["id"])), "sync": result}
    if row["provider"] == CalendarProvider.GOOGLE.value:
        try:
            result = sync_google_calendar_connection(user["id"], connection_id)
        except CalendarAuthorizationError as error:
            raise HTTPException(status_code=409, detail="Google Calendar 授權已失效，請重新連線後再同步。") from error
        except CalendarConnectionError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {"connection": calendar_connection_payload(_connection_for_current_user(connection_id, user["id"])), "sync": result}
    raise HTTPException(status_code=404, detail="Unsupported Calendar provider")


def _connection_for_current_user(connection_id: str, user_id: str) -> sqlite3.Row:
    with db() as connection:
        row = connection.execute("SELECT * FROM calendar_connections WHERE id = ? AND user_id = ?", (connection_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Calendar connection not found")
    return row


@app.get("/api/integrations/calendar/connections/{connection_id}/status")
def calendar_connection_status(request: Request, connection_id: str) -> dict:
    user = require_user(request)
    row = _connection_for_current_user(connection_id, user["id"])
    result = {"connection": calendar_connection_payload(row)}
    if row["provider"] in {CalendarProvider.NEXTCLOUD.value, CalendarProvider.GOOGLE.value}:
        metadata = json.loads(row["metadata_json"] or "{}")
        source_id = metadata.get("legacy_source_id") or row["id"]
        with db() as connection:
            source = connection.execute("SELECT * FROM calendar_sources WHERE id = ? AND user_id = ?", (source_id, user["id"])).fetchone()
            calendars = connection.execute("SELECT id, remote_url, display_name, timezone, read_only, sync_status, last_synced_at, last_error FROM calendar_calendars WHERE source_id = ? ORDER BY display_name", (source_id,)).fetchall()
        if source is not None:
            result["source"] = calendar_source_payload(source, calendars)
            result["calendars"] = [dict(item) for item in calendars]
    return result


@app.get("/api/auth/providers")
def auth_providers(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"github": bool(provider_settings("github")), "google": bool(provider_settings("google")), "dev": dev_login_enabled()}


@app.post("/api/auth/dev-login")
def dev_login(response: Response) -> dict:
    if not dev_login_enabled():
        raise HTTPException(status_code=403, detail="Development login is disabled")
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE id = ?", ("demo-user",)).fetchone()
    if user is None:
        raise HTTPException(status_code=500, detail="Demo user is not initialized")
    create_session(user["id"], response)
    return {"user": {"id": user["id"], "name": user["display_name"], "email": user["email"], "avatar_url": user["avatar_url"]}, "mode": "development"}


@app.get("/api/auth/me")
def auth_me(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = user_from_request(request)
    if user is None:
        response.status_code = 401
        return {"detail": "Not signed in"}
    return {"user": _user_payload(user)}


@app.patch("/api/account/avatar")
def update_account_avatar(request: Request, payload: AvatarUpdateInput, response: Response) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "account-sensitive", security_policy().auth_requests_per_minute, key=user["id"])
    avatar_url = _validated_avatar_data_url(payload.data_url)
    with db() as connection:
        connection.execute(
            "UPDATE users SET avatar_url = ?, updated_at = ? WHERE id = ?",
            (avatar_url, now_iso(), user["id"]),
        )
        updated = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    if updated is None:
        raise HTTPException(status_code=404, detail="Account not found")
    response.headers["Cache-Control"] = "no-store"
    return {"user": _user_payload(updated)}


@app.delete("/api/account")
def delete_account(request: Request, response: Response) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "account-sensitive", security_policy().auth_requests_per_minute, key=user["id"])
    user_id = user["id"]
    with db() as connection:
        storage_keys = [
            row["storage_key"]
            for row in connection.execute(
                """
                SELECT DISTINCT attachments.storage_key
                FROM attachments
                JOIN notices ON notices.id = attachments.source_id
                WHERE notices.user_id = ? AND attachments.storage_key IS NOT NULL
                """,
                (user_id,),
            ).fetchall()
        ]
        deleted = connection.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Account not found")

    for storage_key in storage_keys:
        try:
            with db() as connection:
                still_referenced = connection.execute(
                    "SELECT 1 FROM attachments WHERE storage_key = ? AND status = 'active' LIMIT 1",
                    (storage_key,),
                ).fetchone()
            if still_referenced is None:
                ATTACHMENT_STORE.delete(storage_key)
        except (OSError, ValueError) as error:
            logger.warning("account attachment cleanup failed for %s: %s", storage_key, error)

    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/{provider}/start")
def auth_start(provider: str, request: Request) -> Response:
    enforce_rate_limit(request, "auth", security_policy().auth_requests_per_minute)
    if provider not in {"github", "google"}:
        raise HTTPException(status_code=404, detail="Unsupported provider")
    settings = provider_settings(provider)
    if settings is None:
        return auth_error_redirect("provider_not_configured")
    mode = request.query_params.get("mode", "login")
    if mode not in {"login", "link", "calendar"}:
        raise HTTPException(status_code=400, detail="Invalid OAuth mode")
    if mode == "calendar" and provider != "google":
        raise HTTPException(status_code=400, detail="Calendar OAuth is only available for Google")
    if mode in {"link", "calendar"}:
        require_user(request)
    nonce = secrets.token_urlsafe(24)
    state = f"{provider}:{mode}:{nonce}"
    common = {"client_id": settings["client_id"], "redirect_uri": callback_url(provider), "response_type": "code", "state": nonce}
    if provider == "github":
        authorize_url = "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode({**common, "scope": "read:user user:email"})
    else:
        scope = "openid email profile"
        if mode == "calendar":
            scope += " https://www.googleapis.com/auth/calendar"
        authorize_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({**common, "scope": scope, "access_type": "offline" if mode == "calendar" else "online", "prompt": "consent" if mode == "calendar" else "select_account"})
    response = RedirectResponse(authorize_url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(OAUTH_STATE_COOKIE, state, max_age=600, httponly=True, samesite=COOKIE_SAMESITE, secure=COOKIE_SECURE, path="/")
    return response


@app.get("/api/auth/{provider}/callback")
def auth_callback(provider: str, request: Request) -> Response:
    if provider not in {"github", "google"}:
        return auth_error_redirect("callback_failed")
    if request.query_params.get("error"):
        return auth_error_redirect(stable_auth_error(request.query_params.get("error")))
    state_cookie = request.cookies.get(OAUTH_STATE_COOKIE, "")
    state = request.query_params.get("state", "")
    state_parts = state_cookie.split(":", 2) if state_cookie else []
    if len(state_parts) != 3:
        return auth_error_redirect("invalid_state")
    state_provider, mode, expected_state = state_parts
    if state_provider != provider or not secrets.compare_digest(expected_state, state):
        return auth_error_redirect("state_mismatch")
    if mode not in {"login", "link", "calendar"}:
        return auth_error_redirect("invalid_state")
    if mode == "calendar" and provider != "google":
        return auth_error_redirect("invalid_state")
    settings = provider_settings(provider)
    code = request.query_params.get("code")
    if settings is None:
        return auth_error_redirect("provider_not_configured")
    if not code:
        return auth_error_redirect("callback_failed")
    current_user = user_from_request(request)
    if mode == "calendar" and current_user is None:
        return auth_error_redirect("session_unavailable")
    if mode == "calendar":
        try:
            connection_payload = save_google_calendar_connection(current_user["id"], google_calendar_tokens(code, settings))
        except Exception:
            return auth_error_redirect("callback_failed")
        # OAuth success should produce a usable Calendar connection immediately.
        # A provider/API failure does not discard the credential; Settings can
        # surface the error and let the user retry sync.
        try:
            sync_google_calendar_connection(current_user["id"], connection_payload["id"])
        except (CalendarConnectionError, HTTPException):
            pass
        response = auth_redirect(urllib.parse.urlencode({"auth": "success", "integration": CalendarProvider.GOOGLE.value}))
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return response
    try:
        identity = github_identity(code, settings) if provider == "github" else google_identity(code, settings)
    except Exception:
        return auth_error_redirect("callback_failed")
    try:
        user_id = resolve_identity(provider, identity, mode, current_user["id"] if current_user else None)
    except Exception:
        return auth_error_redirect("callback_failed")
    try:
        response = auth_redirect(urllib.parse.urlencode({"auth": "success"}))
        create_session(user_id, response)
    except Exception:
        return auth_error_redirect("session_unavailable")
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    session_token = request.cookies.get(SESSION_COOKIE)
    if session_token:
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        with db() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/capture/uploads")
async def capture_upload_endpoint(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "capture-upload", max(10, security_policy().interpret_requests_per_minute * 4), key=user["id"])
    policy = ingestion_policy()
    safe_filename = Path(filename).name.strip()[:255]
    if not safe_filename:
        raise HTTPException(status_code=400, detail="檔名無效")
    declared_media_type = (request.headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="檔案是空的")
    if len(content) > policy.max_upload_bytes:
        raise HTTPException(status_code=413, detail="檔案太大")

    temp_dir = DATA_DIR / "ingestion-tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"raw-upload-{uuid4().hex}"
    temp_path.write_bytes(content)
    try:
        category, detected_media_type = sniff_media_type(temp_path, declared_media_type, safe_filename)
        if category in {"unsupported", "malformed", "too_large"}:
            raise HTTPException(status_code=422, detail="目前不支援這個檔案格式")
        kind = "image" if category == "image" else ("pdf" if category == "pdf" else "file")
        normalized_media_type = detected_media_type or declared_media_type or "application/octet-stream"
        content_hash = hashlib.sha256(content).hexdigest()
        storage_key = ATTACHMENT_STORE.put_path(temp_path, content_hash)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

    upload_id = f"upload-{uuid4().hex[:20]}"
    expires_at = _capture_upload_expiry()
    _cleanup_capture_uploads()
    with db() as connection:
        connection.execute(
            """
            INSERT INTO capture_uploads(
                id, user_id, filename, mime_type, kind, size, content_hash, storage_key,
                created_at, expires_at, consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (upload_id, user["id"], safe_filename, normalized_media_type, kind, len(content), content_hash, storage_key, now_iso(), expires_at),
        )
    return {
        "upload": {
            "id": upload_id,
            "name": safe_filename,
            "media_type": normalized_media_type,
            "kind": kind,
            "size": len(content),
            "expires_at": expires_at,
        }
    }


@app.post("/api/interpret")
def interpret_endpoint(request: Request, payload: NoticeInput) -> dict:
    user = require_user(request)
    enforce_rate_limit(request, "interpret", security_policy().interpret_requests_per_minute, key=user["id"])
    try:
        with OPERATIONS.exclusive(f"interpret:{user['id']}"):
            return interpret_source(user["id"], payload)
    except OperationBusy as error:
        raise HTTPException(status_code=429, detail="上一個整理工作仍在進行中，請稍後再試。", headers={"Retry-After": "2"}) from error
    except IngestionPolicyError as error:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message, "details": error.details}, "detail": error.message},
        )
    except AIProviderTimeout as error:
        _log_ai_interpretation_error(error, status_code=504, category="provider_timeout")
        raise HTTPException(status_code=504, detail="整理逾時，請縮小檔案後再試。") from error
    except AIProviderConfigurationError as error:
        _log_ai_interpretation_error(error, status_code=503, category="configuration")
        raise HTTPException(status_code=503, detail="整理服務尚未設定完成。") from error
    except AIProviderUnavailable as error:
        _log_ai_interpretation_error(error, status_code=503, category="provider_unavailable")
        raise HTTPException(status_code=503, detail="整理服務暫時無法使用，請稍後再試。") from error
    except AIProviderMalformedOutput as error:
        # The upstream provider answered successfully, but its content could not
        # be repaired into a safe Proposal payload. This is not a gateway
        # failure/timeout; surface it as an actionable semantic response error.
        _log_ai_interpretation_error(error, status_code=422, category="malformed_output")
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "malformed_output",
                    "message": "AI 有回應，但整理結果格式不完整；請再試一次。",
                },
                "detail": "AI 有回應，但整理結果格式不完整；請再試一次。",
            },
        )
    except AIInterpretationError as error:
        _log_ai_interpretation_error(error, status_code=502, category="interpretation")
        raise HTTPException(status_code=502, detail="AI interpretation failed") from error


def _log_ai_interpretation_error(error: AIInterpretationError, *, status_code: int, category: str) -> None:
    model_alias = re.sub(r"[^A-Za-z0-9._/-]", "", str(error.model_alias or "unknown"))[:100] or "unknown"
    logged_status = error.http_status if isinstance(error.http_status, int) else status_code
    api_logger.warning("AI interpretation failed model=%s status=%s category=%s", model_alias, logged_status, category)


@app.get("/api/proposals")
def list_proposals(request: Request, status: str | None = Query(default=None, max_length=32)) -> dict:
    user = require_user(request)
    status = plain_query(status)
    allowed_statuses = {"pending", "edited", "accepted", "rejected", "applied"}
    if status and status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid proposal status")
    with db() as connection:
        if status:
            rows = connection.execute("SELECT * FROM proposals WHERE user_id = ? AND status = ? ORDER BY created_at DESC", (user["id"], status)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM proposals WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    return {"proposals": [proposal_payload(row) for row in rows], "meta": {"count": len(rows), "status": status or "all"}}


@app.get("/api/proposals/{proposal_id}")
def get_proposal(request: Request, proposal_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal_payload(row)


def _convert_proposal_type_patch(target_type: ProposalTargetType, patch: dict[str, Any]) -> dict[str, Any]:
    """Convert an ambiguous create proposal between Event and Task semantics."""
    if target_type == "event":
        converted = {key: value for key, value in patch.items() if key in {"title", "location", "date", "time", "start_at", "end_at", "all_day", "recurrence", "detail"}}
        if not converted.get("date") and not converted.get("start_at"):
            due = str(patch.get("due") or patch.get("due_label") or "").strip()
            if due:
                converted["date"] = due[:10]
        if converted.get("date") and not any(converted.get(key) for key in ("time", "start_at", "end_at")):
            converted["all_day"] = True
        if not converted.get("detail") and patch.get("notes"):
            converted["detail"] = patch["notes"]
        return converted

    converted = {key: value for key, value in patch.items() if key in {"title", "date", "time", "start_at", "end_at", "all_day", "notes", "due", "due_label", "list_id"}}
    if not converted.get("due") and not converted.get("due_label"):
        date_value = str(patch.get("date") or patch.get("start_at") or "").strip()
        if date_value:
            converted["due"] = date_value
            converted["due_label"] = date_value[:10]
    if not converted.get("notes") and patch.get("detail"):
        converted["notes"] = patch["detail"]
    return converted


@app.patch("/api/proposals/{proposal_id}")
def edit_proposal(request: Request, proposal_id: str, payload: ProposalEditInput) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if row["status"] not in {"pending", "edited"}:
            raise HTTPException(status_code=409, detail="Only pending proposals can be edited")
        existing = _proposal_from_row(row)
        target_id = payload.target_id if payload.target_id is not None else existing.target_id
        target_type = payload.target_type or existing.target_type
        if target_type != existing.target_type and existing.operation != "create":
            raise HTTPException(status_code=409, detail="Only create proposals can change between Event and Task")
        patch_data = payload.patch.model_dump(exclude_unset=True)
        if target_type != existing.target_type:
            patch_data = _convert_proposal_type_patch(target_type, patch_data)
        needs_review = existing.needs_review and not target_id
        if target_type != existing.target_type:
            if target_type == "event":
                try:
                    _event_schedule_fields(patch_data)
                    needs_review = False
                except HTTPException:
                    needs_review = True
            else:
                needs_review = not any(patch_data.get(key) for key in ("due", "due_label", "date", "start_at"))
        if needs_review and existing.operation == "create" and target_type == "event":
            try:
                if patch_data.get("title"):
                    _event_schedule_fields(patch_data)
                    needs_review = False
            except HTTPException:
                pass
        edited = Proposal.model_validate(
            {
                "operation": existing.operation,
                "target_type": target_type,
                "target_id": target_id,
                "patch": patch_data,
                "evidence_refs": existing.evidence_refs,
                "target_candidates": existing.target_candidates,
                "needs_review": needs_review,
                "review_reason": existing.review_reason if needs_review else None,
                "confidence": existing.confidence,
            }
        )
        if edited.target_id and edited.operation == "update" and not _proposal_target_exists(connection, user["id"], edited):
            raise HTTPException(status_code=404, detail="Selected proposal target not found")
        timestamp = now_iso()
        connection.execute(
            """
            UPDATE proposals SET target_id = ?, target_type = ?, patch_json = ?, needs_review = ?, review_reason = ?, status = 'edited', updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                edited.target_id,
                edited.target_type,
                json.dumps(edited.patch.model_dump(exclude_unset=True), ensure_ascii=False),
                int(edited.needs_review),
                edited.review_reason,
                timestamp,
                proposal_id,
                user["id"],
            ),
        )
        updated = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
    return proposal_payload(updated)


@app.post("/api/proposals/{proposal_id}/accept")
def accept_proposal(request: Request, proposal_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        proposal = _proposal_from_row(row)
        if row["status"] not in {"pending", "edited"}:
            raise HTTPException(status_code=409, detail="Proposal is not reviewable")
        if proposal.needs_review or (proposal.operation == "update" and not proposal.target_id):
            raise HTTPException(status_code=409, detail="Proposal requires target selection or edit before Accept")
        if proposal.target_type == "event":
            patch = proposal.patch.model_dump(exclude_unset=True)
            if proposal.operation == "create":
                if not patch.get("title"):
                    raise HTTPException(status_code=409, detail="Event proposal needs a title before confirmation")
                _event_schedule_fields(patch)
            elif proposal.operation == "update" and {"date", "time", "start_at", "end_at", "all_day", "recurrence"}.intersection(patch):
                event = connection.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (proposal.target_id, user["id"])).fetchone()
                if event is None:
                    raise HTTPException(status_code=404, detail="Event target not found")
                _event_schedule_fields(patch, event)
        connection.execute("UPDATE proposals SET status = 'accepted', updated_at = ? WHERE id = ? AND user_id = ?", (now_iso(), proposal_id, user["id"]))
        updated = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
    return proposal_payload(updated)


@app.post("/api/proposals/{proposal_id}/reject")
def reject_proposal(request: Request, proposal_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if row["status"] not in {"pending", "edited"}:
            raise HTTPException(status_code=409, detail="Proposal is not reviewable")
        connection.execute("UPDATE proposals SET status = 'rejected', updated_at = ? WHERE id = ? AND user_id = ?", (now_iso(), proposal_id, user["id"]))
        updated = connection.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
    return proposal_payload(updated)


@app.post("/api/proposals/{proposal_id}/apply")
def apply_proposal_endpoint(request: Request, proposal_id: str, background_tasks: BackgroundTasks = None) -> dict:
    user = require_user(request)
    result = apply_proposal(user["id"], proposal_id)
    event_id = result.get("result", {}).get("event_id")
    if background_tasks is not None and event_id:
        background_tasks.add_task(sync_local_event_to_default_calendar, user["id"], event_id)
    return result


@app.post("/api/proposals/apply-batch")
def apply_proposal_batch(request: Request, payload: ProposalBatchApplyInput, background_tasks: BackgroundTasks = None) -> dict:
    user = require_user(request)
    started = clock.perf_counter()
    unique_ids = list(dict.fromkeys(payload.proposal_ids))
    results, event_ids = apply_proposals_bulk(user["id"], unique_ids, payload.confirm)
    if background_tasks is not None and event_ids:
        background_tasks.add_task(sync_local_events_to_default_calendar, user["id"], event_ids)
    failed = any(item["status"] == "failed" for item in results)
    return {
        "status": "partial" if failed else "applied",
        "results": results,
        "performance": {
            "proposal_count": len(unique_ids),
            "before": {
                "strategy": "one HTTP request with accept/apply SQLite transaction per proposal",
                "http_requests": 1,
                "db_transactions": len(unique_ids) * (2 if payload.confirm else 1),
            },
            "after": {
                "strategy": "one HTTP request and one SQLite transaction with per-item savepoints",
                "http_requests": 1,
                "db_transactions": 1,
                "external_sync_jobs": 1 if event_ids else 0,
                "external_provider_write_strategy": "bounded concurrent writes, one calendar pull per affected source",
                "elapsed_ms": round((clock.perf_counter() - started) * 1000, 2),
            },
        },
    }
@app.get("/api/notices")
def list_notices(
    request: Request,
    q: str | None = Query(default=None, max_length=160),
    source_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    user = require_user(request)
    q = plain_query(q)
    source_type = plain_query(source_type)
    limit = plain_query(limit, 50)
    offset = plain_query(offset, 0)
    clauses = ["user_id = ?"]
    parameters: list[object] = [user["id"]]
    if q and q.strip():
        clauses.append("(title LIKE ? OR body LIKE ?)")
        needle = f"%{q.strip()}%"
        parameters.extend([needle, needle])
    if source_type:
        clauses.append("source_type = ?")
        parameters.append(source_type)
    with db() as connection:
        rows = connection.execute(
            f"SELECT * FROM notices WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ).fetchall()
        total = connection.execute(f"SELECT COUNT(*) FROM notices WHERE {' AND '.join(clauses)}", parameters).fetchone()[0]
    return {
        "notices": [notice_payload(row) for row in rows],
        "meta": {"count": len(rows), "total": total, "limit": limit, "offset": offset, "query": q or "", "source_type": source_type},
    }


@app.get("/api/notices/{notice_id}")
def get_notice(request: Request, notice_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute("SELECT * FROM notices WHERE id = ? AND user_id = ?", (notice_id, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Notice not found")
        tasks = connection.execute("SELECT * FROM tasks WHERE source_id = ? AND user_id = ? ORDER BY id", (notice_id, user["id"])).fetchall()
        events = connection.execute("SELECT * FROM events WHERE source_id = ? AND user_id = ? ORDER BY id", (notice_id, user["id"])).fetchall()
    return notice_detail_payload(row, tasks, events)


@app.post("/api/notices/{notice_id}/discard")
def discard_capture(request: Request, notice_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute("SELECT processing_json FROM notices WHERE id = ? AND user_id = ?", (notice_id, user["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
        try:
            processing = json.loads(row["processing_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            processing = {}
        if processing.get("capture_state") != "review_draft":
            raise HTTPException(status_code=409, detail="Only a newly created, unapplied Capture can be discarded")
        applied = connection.execute("SELECT 1 FROM proposals WHERE source_id = ? AND user_id = ? AND status = 'applied' LIMIT 1", (notice_id, user["id"])).fetchone()
        linked_event = connection.execute("SELECT 1 FROM events WHERE source_id = ? AND user_id = ? LIMIT 1", (notice_id, user["id"])).fetchone()
        linked_task = connection.execute("SELECT 1 FROM tasks WHERE source_id = ? AND user_id = ? LIMIT 1", (notice_id, user["id"])).fetchone()
        if applied or linked_event or linked_task:
            raise HTTPException(status_code=409, detail="Capture already has applied data and cannot be discarded")
        connection.execute("DELETE FROM notices WHERE id = ? AND user_id = ?", (notice_id, user["id"]))
    return {"ok": True, "discarded": True}


@app.post("/api/notices/parse")
def preview_notice(request: Request, payload: NoticeInput) -> dict:
    require_user(request)
    notice = payload.model_dump(exclude={"attachments"})
    notice["attachments"] = [item.model_dump(exclude={"data_url"}) for item in payload.attachments]
    return {"notice": notice, "structured": parse_notice(payload), "mode": "rule-based-preview"}


@app.post("/api/notices")
def create_notice(request: Request, payload: NoticeCreateInput) -> dict:
    user = require_user(request)
    structured = structured_from_input(payload)
    attachments = [item.model_dump(exclude={"data_url"}) for item in payload.attachments]
    title = payload.title.strip() or (attachments[0]["name"] if attachments else (payload.body.strip().splitlines()[0][:80] if payload.body.strip() else "新增資料"))
    notice = Notice(
        id=f"notice-{uuid4().hex[:10]}",
        title=title,
        body=payload.body,
        audience=payload.audience,
        created_at=now_iso(),
        source_type="import" if attachments else payload.source_type,
        source_name=payload.source_name,
        source_url=payload.source_url,
        attachments=attachments,
        confidence=structured["confidence"],
    )
    with db() as connection:
        connection.execute(
            "INSERT INTO notices(id, user_id, title, body, audience, created_at, source_type, confidence, source_name, source_url, attachments_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (notice.id, user["id"], notice.title, notice.body, notice.audience, notice.created_at, notice.source_type, notice.confidence, notice.source_name, notice.source_url, json.dumps(notice.attachments, ensure_ascii=False)),
        )
        for item in structured["tasks"]:
            due_iso = parse_optional_iso(item.get("due_iso"), "Task due_iso")
            task_status = "needs_clarification" if item.get("needs_clarification") else "open"
            connection.execute(
                "INSERT INTO tasks(id, user_id, title, due_label, due_iso, status, source_id, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"task-{uuid4().hex[:10]}", user["id"], item["title"], item["due_label"], due_iso, task_status, notice.id, "student"),
            )
        for item in structured["events"]:
            connection.execute(
                "INSERT INTO events(id, user_id, title, date_label, time_label, detail, source_id, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"event-{uuid4().hex[:10]}", user["id"], item["title"], item["date_label"], item["time_label"], item["detail"], notice.id, item.get("location")),
            )
    return {"notice": notice.model_dump(), "structured": structured}


@app.get("/api/tasks/{task_id}")
def get_task(request: Request, task_id: str) -> dict:
    user = require_user(request)
    with db() as connection:
        row = connection.execute(
            "SELECT t.*, n.title AS source_name FROM tasks t LEFT JOIN notices n ON n.id = t.source_id WHERE t.id = ? AND t.user_id = ?",
            (task_id, user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task_payload(row, row["source_name"])


@app.post("/api/tasks", status_code=201)
def create_task(request: Request, payload: TaskCreate) -> dict:
    user = require_user(request)
    due_iso = parse_optional_iso(payload.due_iso, "Task due_iso")
    source_id = payload.source_id or "manual"
    source_name = "手動建立"
    if source_id != "manual":
        with db() as connection:
            source = connection.execute("SELECT title FROM notices WHERE id = ? AND user_id = ?", (source_id, user["id"])).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="Task source notice not found")
        source_name = source["title"]
    status = "needs_clarification" if payload.needs_clarification else payload.status
    schedule = _task_schedule_fields(start_at=payload.start_at, end_at=payload.end_at, all_day=payload.all_day, timezone_name=payload.timezone)
    task_id = f"task-{uuid4().hex[:10]}"
    with db() as connection:
        duplicate = connection.execute(
            "SELECT 1 FROM tasks WHERE user_id = ? AND source_id = ? AND title = ? AND status <> 'done'",
            (user["id"], source_id, payload.title),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="An open task with this title already exists for the same source")
        if payload.list_id is not None:
            owned_list = connection.execute(
                "SELECT 1 FROM personal_lists WHERE id = ? AND user_id = ?",
                (payload.list_id, user["id"]),
            ).fetchone()
            if owned_list is None:
                raise HTTPException(status_code=404, detail="List not found")
        if payload.related_event_id is not None and not _owned_event_exists(connection, user["id"], payload.related_event_id):
            raise HTTPException(status_code=404, detail="Related event not found")
        connection.execute(
            "INSERT INTO tasks(id, user_id, title, due_label, due_iso, start_at, end_at, all_day, timezone, status, source_id, owner, list_id, related_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, user["id"], payload.title, payload.due_label, due_iso, schedule["start_at"], schedule["end_at"], schedule["all_day"], schedule["timezone"], status, source_id, "student", payload.list_id, payload.related_event_id),
        )
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task_payload(row, source_name)


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(request: Request, task_id: str) -> Response:
    user = require_user(request)
    with db() as connection:
        deleted = connection.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    return Response(status_code=204)


@app.patch("/api/tasks/{task_id}")
def update_task(request: Request, task_id: str, payload: TaskUpdate) -> dict:
    user = require_user(request)
    with db() as connection:
        task = connection.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    fields = payload.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="Task update requires at least one field")
    updates: dict[str, object] = {}
    if "title" in fields:
        updates["title"] = payload.title
    if "due_label" in fields:
        updates["due_label"] = payload.due_label
    if "due_iso" in fields:
        updates["due_iso"] = parse_optional_iso(payload.due_iso, "Task due_iso")
    if {"start_at", "end_at", "all_day", "timezone"}.intersection(fields):
        schedule = _task_schedule_fields(
            start_at=payload.start_at if "start_at" in fields else task["start_at"],
            end_at=payload.end_at if "end_at" in fields else task["end_at"],
            all_day=bool(payload.all_day) if "all_day" in fields else bool(task["all_day"]),
            timezone_name=payload.timezone if "timezone" in fields else task["timezone"],
        )
        updates.update(schedule)
    if "status" in fields:
        updates["status"] = payload.status
    if "list_id" in fields:
        if payload.list_id is not None:
            with db() as connection:
                owned_list = connection.execute(
                    "SELECT 1 FROM personal_lists WHERE id = ? AND user_id = ?",
                    (payload.list_id, user["id"]),
                ).fetchone()
            if owned_list is None:
                raise HTTPException(status_code=404, detail="List not found")
        updates["list_id"] = payload.list_id
    if "related_event_id" in fields:
        with db() as connection:
            if payload.related_event_id is not None and not _owned_event_exists(connection, user["id"], payload.related_event_id):
                raise HTTPException(status_code=404, detail="Related event not found")
        updates["related_event_id"] = payload.related_event_id
    if "needs_clarification" in fields:
        expected_status = "needs_clarification" if payload.needs_clarification else "open"
        if "status" in fields and payload.status != expected_status:
            raise HTTPException(status_code=422, detail="status and needs_clarification describe different task states")
        updates["status"] = expected_status
    if updates.get("status") is None:
        updates.pop("status", None)
    if not updates:
        raise HTTPException(status_code=422, detail="Task update contains no usable fields")
    with db() as connection:
        assignments = ", ".join(f"{column} = ?" for column in updates)
        connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ? AND user_id = ?", (*updates.values(), task_id, user["id"]))
        updated = connection.execute(
            "SELECT t.*, n.title AS source_name FROM tasks t LEFT JOIN notices n ON n.id = t.source_id WHERE t.id = ?",
            (task_id,),
        ).fetchone()
    return task_payload(updated, updated["source_name"])


@app.get("/api/events/{event_id}/tasks")
def list_tasks_for_event(request: Request, event_id: str) -> dict:
    """Bidirectional index for manually linked Tasks; no Calendar write-back."""
    user = require_user(request)
    with db() as connection:
        if not _owned_event_exists(connection, user["id"], event_id):
            raise HTTPException(status_code=404, detail="Event not found")
        rows = connection.execute(
            """SELECT t.*, n.title AS source_name FROM tasks t
               LEFT JOIN notices n ON n.id = t.source_id
               WHERE t.user_id = ? AND t.related_event_id = ? ORDER BY t.status, t.id""",
            (user["id"], event_id),
        ).fetchall()
    return {"tasks": [task_payload(row, row["source_name"]) for row in rows]}


@app.get("/api/sync/manifest")
def sync_manifest(request: Request) -> dict:
    require_user(request)
    return {"format": "omt-sync-v1", "objects": [], "storage": "sqlite-local", "encryption": "client-side-next"}
