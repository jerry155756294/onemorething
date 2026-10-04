from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import os
import socket
import threading
import time
from typing import BinaryIO
from urllib.parse import urlparse


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        super().__init__("rate limit exceeded")
        self.retry_after = max(1, int(retry_after))


class OperationBusy(Exception):
    pass


class ResponseTooLarge(Exception):
    pass


class UnsafeOutboundTarget(Exception):
    pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class SecurityPolicy:
    global_requests_per_minute: int = 240
    auth_requests_per_minute: int = 20
    interpret_requests_per_minute: int = 8
    sync_requests_per_minute: int = 6
    default_write_body_bytes: int = 2 * 1024 * 1024
    avatar_request_body_bytes: int = 4 * 1024 * 1024
    oauth_response_bytes: int = 2 * 1024 * 1024
    calendar_response_bytes: int = 12 * 1024 * 1024
    calendar_max_pages: int = 40
    calendar_max_items: int = 10_000
    calendar_max_calendars: int = 50
    max_sessions_per_user: int = 10
    enforce_origin_check: bool = False
    allow_private_calendar_hosts: bool = True
    allow_insecure_calendar_http: bool = True

    @classmethod
    def from_environment(cls, environment: str) -> "SecurityPolicy":
        production = environment in {"production", "prod"}
        return cls(
            global_requests_per_minute=_env_int("OMT_RATE_LIMIT_GLOBAL_PER_MINUTE", cls.global_requests_per_minute, 10, 100_000),
            auth_requests_per_minute=_env_int("OMT_RATE_LIMIT_AUTH_PER_MINUTE", cls.auth_requests_per_minute, 1, 10_000),
            interpret_requests_per_minute=_env_int("OMT_RATE_LIMIT_INTERPRET_PER_MINUTE", cls.interpret_requests_per_minute, 1, 1_000),
            sync_requests_per_minute=_env_int("OMT_RATE_LIMIT_SYNC_PER_MINUTE", cls.sync_requests_per_minute, 1, 1_000),
            default_write_body_bytes=_env_int("OMT_MAX_WRITE_BODY_BYTES", cls.default_write_body_bytes, 16 * 1024, 64 * 1024 * 1024),
            avatar_request_body_bytes=_env_int("OMT_MAX_AVATAR_REQUEST_BODY_BYTES", cls.avatar_request_body_bytes, 512 * 1024, 16 * 1024 * 1024),
            oauth_response_bytes=_env_int("OMT_MAX_OAUTH_RESPONSE_BYTES", cls.oauth_response_bytes, 64 * 1024, 16 * 1024 * 1024),
            calendar_response_bytes=_env_int("OMT_MAX_CALENDAR_RESPONSE_BYTES", cls.calendar_response_bytes, 256 * 1024, 64 * 1024 * 1024),
            calendar_max_pages=_env_int("OMT_CALENDAR_MAX_PAGES", cls.calendar_max_pages, 1, 500),
            calendar_max_items=_env_int("OMT_CALENDAR_MAX_ITEMS", cls.calendar_max_items, 100, 100_000),
            calendar_max_calendars=_env_int("OMT_CALENDAR_MAX_CALENDARS", cls.calendar_max_calendars, 1, 500),
            max_sessions_per_user=_env_int("OMT_MAX_SESSIONS_PER_USER", cls.max_sessions_per_user, 1, 100),
            enforce_origin_check=_env_bool("OMT_ENFORCE_ORIGIN_CHECK", production),
            allow_private_calendar_hosts=_env_bool("OMT_ALLOW_PRIVATE_CALENDAR_HOSTS", not production),
            allow_insecure_calendar_http=_env_bool("OMT_ALLOW_INSECURE_CALENDAR_HTTP", not production),
        )


class InMemoryRateLimiter:
    """Small single-process limiter. Put a shared/edge limiter in front of multi-worker deployments."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, bucket: str, key: str, limit: int, window_seconds: int = 60) -> None:
        if limit <= 0:
            return
        now = time.monotonic()
        cutoff = now - window_seconds
        bucket_key = (bucket, key)
        with self._lock:
            samples = self._buckets[bucket_key]
            while samples and samples[0] <= cutoff:
                samples.popleft()
            if len(samples) >= limit:
                retry_after = int(window_seconds - (now - samples[0])) + 1
                raise RateLimitExceeded(retry_after)
            samples.append(now)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


class OperationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()

    @contextmanager
    def exclusive(self, key: str):
        with self._lock:
            if key in self._active:
                raise OperationBusy(key)
            self._active.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._active.discard(key)


RATE_LIMITER = InMemoryRateLimiter()
OPERATIONS = OperationRegistry()


def read_limited(stream: BinaryIO, max_bytes: int) -> bytes:
    content_length = None
    headers = getattr(stream, "headers", None)
    if headers is not None:
        value = headers.get("Content-Length")
        if value:
            try:
                content_length = int(value)
            except (TypeError, ValueError):
                content_length = None
    if content_length is not None and content_length > max_bytes:
        raise ResponseTooLarge()
    try:
        payload = stream.read(max_bytes + 1)
    except TypeError:
        payload = stream.read()
    if len(payload) > max_bytes:
        raise ResponseTooLarge()
    return payload


def resolved_addresses(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost") or normalized.endswith(".local"):
        return {ipaddress.ip_address("127.0.0.1")}
    try:
        return {ipaddress.ip_address(normalized)}
    except ValueError:
        try:
            return {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as error:
            raise UnsafeOutboundTarget("host could not be resolved") from error


def validate_outbound_http_target(url: str, *, allow_private: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeOutboundTarget("only http(s) targets are supported")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise UnsafeOutboundTarget("invalid port") from error
    if allow_private:
        return url
    addresses = resolved_addresses(parsed.hostname, port)
    if not addresses:
        raise UnsafeOutboundTarget("host did not resolve")
    if not allow_private and any(not address.is_global for address in addresses):
        raise UnsafeOutboundTarget("private or non-public network targets are disabled")
    return url
