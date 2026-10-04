import asyncio
import base64
import io
from uuid import uuid4
from http.cookies import SimpleCookie

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from app import main
from app.security import InMemoryRateLimiter, OperationBusy, OperationRegistry, RateLimitExceeded


def _request(method: str, path: str, *, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers or [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "scheme": "http",
        }
    )


async def _asgi_request(method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def test_rate_limiter_blocks_after_budget():
    limiter = InMemoryRateLimiter()
    limiter.check("unit", "user", 2, window_seconds=60)
    limiter.check("unit", "user", 2, window_seconds=60)
    with pytest.raises(RateLimitExceeded) as captured:
        limiter.check("unit", "user", 2, window_seconds=60)
    assert captured.value.retry_after >= 1


def test_operation_registry_rejects_duplicate_expensive_work():
    registry = OperationRegistry()
    with registry.exclusive("calendar:user"):
        with pytest.raises(OperationBusy):
            with registry.exclusive("calendar:user"):
                pass
    with registry.exclusive("calendar:user"):
        pass


def test_calendar_network_policy_rejects_loopback_when_private_hosts_disabled(monkeypatch):
    monkeypatch.setenv("OMT_ALLOW_PRIVATE_CALENDAR_HOSTS", "0")
    monkeypatch.setenv("OMT_ALLOW_INSECURE_CALENDAR_HTTP", "1")
    with pytest.raises(main.CalendarConnectionError):
        main.validate_calendar_network_target("http://127.0.0.1/calendar/", "Calendar URL")
    assert main.validate_calendar_network_target("https://1.1.1.1/calendar/", "Calendar URL").startswith("https://")


def test_health_response_has_api_security_headers():
    response = asyncio.run(_asgi_request("GET", "/api/health"))
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers.get("x-request-id")
    assert response.json()["release"] == main.OMT_RELEASE


def test_unsafe_write_origin_can_be_rejected(monkeypatch):
    monkeypatch.setenv("OMT_ENFORCE_ORIGIN_CHECK", "1")
    response = asyncio.run(
        _asgi_request("POST", "/api/auth/logout", headers={"Origin": "https://evil.example"})
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Request origin is not allowed"


def test_generic_write_body_budget_rejects_oversized_request(monkeypatch):
    monkeypatch.setenv("OMT_MAX_WRITE_BODY_BYTES", "16384")
    response = asyncio.run(
        _asgi_request(
            "POST",
            "/api/lists",
            content=b"x" * 17000,
            headers={"Content-Type": "application/json"},
        )
    )
    assert response.status_code == 413


def test_interpret_transport_limit_preserves_capture_error_shape():
    response = main._body_too_large_response(_request("POST", "/api/interpret"), 1234)
    assert response.status_code == 413
    payload = bytes(response.body)
    assert b"capture_too_large" in payload
    assert b"max_request_body_bytes" in payload


def test_session_count_is_capped_per_user(monkeypatch):
    monkeypatch.setenv("OMT_MAX_SESSIONS_PER_USER", "2")
    user_id = f"security-user-{uuid4().hex}"
    timestamp = main.now_iso()
    with main.db() as connection:
        connection.execute(
            "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?)",
            (user_id, "Security test", None, timestamp, timestamp),
        )
    try:
        for _ in range(3):
            main.create_session(user_id, Response())
        with main.db() as connection:
            count = connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0]
        assert count == 2
    finally:
        with main.db() as connection:
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))


def test_avatar_rejects_decompression_dimension_bomb():
    stream = io.BytesIO()
    Image.new("RGB", (main.AVATAR_MAX_DIMENSION + 1, 1), "white").save(stream, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
    with pytest.raises(main.HTTPException) as captured:
        main._validated_avatar_data_url(data_url)
    assert captured.value.status_code == 413


def test_provider_event_url_rejects_non_http_scheme():
    icalendar = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "UID:unsafe-url-test",
            "DTSTART:20261001T142000Z",
            "SUMMARY:Test",
            "URL:javascript:alert(1)",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    events = main.parse_icalendar_events(icalendar, "https://calendar.example/event.ics", None, "UTC")
    assert len(events) == 1
    assert events[0]["url"] is None


def test_google_pagination_has_hard_page_cap(monkeypatch):
    monkeypatch.setenv("OMT_CALENDAR_MAX_PAGES", "2")

    def endless_pages(*_args, **_kwargs):
        return {"items": [], "nextPageToken": "again"}

    monkeypatch.setattr(main, "google_calendar_api_json", endless_pages)
    with pytest.raises(main.CalendarConnectionError, match="pagination"):
        main._google_list_all("token", "calendars")


def test_request_models_forbid_mass_assignment_properties():
    with pytest.raises(ValidationError):
        main.TaskCreate(title="安全測試", user_id="other-user")


def test_task_object_access_is_scoped_to_session_user():
    timestamp = main.now_iso()
    owner_id = f"owner-{uuid4().hex}"
    other_id = f"other-{uuid4().hex}"
    task_id = f"task-{uuid4().hex}"
    with main.db() as connection:
        for user_id in (owner_id, other_id):
            connection.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, ?)",
                (user_id, user_id, timestamp, timestamp),
            )
        connection.execute(
            "INSERT INTO tasks(id, user_id, title, due_label, due_iso, status, source_id, owner) VALUES (?, ?, ?, ?, NULL, 'open', 'manual', 'student')",
            (task_id, owner_id, "Owner only", "待確認"),
        )
    try:
        response = Response()
        main.create_session(other_id, response)
        cookies = SimpleCookie(response.headers["set-cookie"])
        token = cookies[main.SESSION_COOKIE].value
        request = _request(
            "GET",
            f"/api/tasks/{task_id}",
            headers=[(b"cookie", f"{main.SESSION_COOKIE}={token}".encode())],
        )
        with pytest.raises(main.HTTPException) as captured:
            main.get_task(request, task_id)
        assert captured.value.status_code == 404
    finally:
        with main.db() as connection:
            connection.execute("DELETE FROM users WHERE id IN (?, ?)", (owner_id, other_id))
