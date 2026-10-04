import io
import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import threading
import urllib.parse
import unittest
import httpx
from pathlib import Path
from http.cookies import SimpleCookie
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from uuid import uuid4
from xml.sax.saxutils import escape


_DATA_DIR = tempfile.TemporaryDirectory()
os.environ["OMT_DATA_DIR"] = _DATA_DIR.name
os.environ["OMT_ENVIRONMENT"] = "test"
os.environ["OMT_ENABLE_DEV_LOGIN"] = "1"
os.environ["OMT_DEFAULT_TIMEZONE"] = "Asia/Taipei"

from app import main  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


class WritableCalDavFixture:
    """Local writable CalDAV-shaped HTTP fixture for exercising the real adapter."""

    def __init__(self):
        self.resources = {}
        self.history = []
        self.fail_put = False
        self._etag_counter = 0
        self._lock = threading.Lock()
        self.server = None
        self.thread = None

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with fixture._lock:
                    fixture.history.append(("PUT", self.path, body.decode("utf-8")))
                    if fixture.fail_put:
                        self._respond(503, b"fixture write failure")
                        return
                    existing = fixture.resources.get(self.path)
                    if self.headers.get("If-None-Match") == "*" and existing is not None:
                        self._respond(412)
                        return
                    if self.headers.get("If-Match") and (existing is None or self.headers["If-Match"] != existing[0]):
                        self._respond(412)
                        return
                    fixture._etag_counter += 1
                    etag = f'"fixture-{fixture._etag_counter}"'
                    fixture.resources[self.path] = (etag, body)
                self._respond(201 if existing is None else 204, headers={"ETag": etag})

            def do_GET(self):
                with fixture._lock:
                    fixture.history.append(("GET", self.path, ""))
                    existing = fixture.resources.get(self.path)
                    if existing is None:
                        self._respond(404)
                        return
                    etag, body = existing
                self._respond(200, body, "text/calendar; charset=utf-8", headers={"ETag": etag})

            def do_REPORT(self):
                with fixture._lock:
                    fixture.history.append(("REPORT", self.path, ""))
                    responses = []
                    for path, (etag, body) in fixture.resources.items():
                        responses.append(
                            "<d:response>"
                            f"<d:href>{escape(path)}</d:href>"
                            "<d:propstat><d:prop>"
                            f"<d:getetag>{escape(etag)}</d:getetag>"
                            f"<c:calendar-data>{escape(body.decode('utf-8'))}</c:calendar-data>"
                            "</d:prop></d:propstat></d:response>"
                        )
                xml = (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                    + "".join(responses)
                    + "</d:multistatus>"
                ).encode("utf-8")
                self._respond(207, xml, "application/xml; charset=utf-8")

            def do_DELETE(self):
                with fixture._lock:
                    fixture.history.append(("DELETE", self.path, ""))
                    existing = fixture.resources.get(self.path)
                    if existing is None:
                        self._respond(404)
                        return
                    if self.headers.get("If-Match") and self.headers["If-Match"] != existing[0]:
                        self._respond(412)
                        return
                    del fixture.resources[self.path]
                self._respond(204)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def calendar_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/calendar/"

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class NextcloudBackendTests(unittest.TestCase):
    @staticmethod
    def _request_with_session(response):
        cookies = SimpleCookie(response.headers["set-cookie"])
        session_token = cookies[main.SESSION_COOKIE].value
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/calendar/nextcloud/login/poll",
                "headers": [(b"cookie", f"{main.SESSION_COOKIE}={session_token}".encode())],
            }
        )

    @staticmethod
    def _request(response, method="GET", path="/api"):
        cookies = SimpleCookie(response.headers["set-cookie"])
        session_token = cookies[main.SESSION_COOKIE].value
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [(b"cookie", f"{main.SESSION_COOKIE}={session_token}".encode())],
            }
        )

    @staticmethod
    def _request_with_cookie(cookie, query=None, method="GET", path="/api"):
        query_string = urllib.parse.urlencode(query or {}).encode()
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": query_string,
                "headers": [(b"cookie", cookie.encode())],
            }
        )

    def test_base_url_adds_https_and_rejects_unsafe_parts(self):
        self.assertEqual(main.normalize_nextcloud_base_url("cloud.example/"), "https://cloud.example")
        with self.assertRaises(main.CalendarConnectionError):
            main.normalize_nextcloud_base_url("https://user:password@cloud.example")
        with self.assertRaises(main.CalendarConnectionError):
            main.normalize_nextcloud_base_url("https://cloud.example/?redirect=elsewhere")

    def test_accept_proposal_records_review_without_mutation_until_apply(self):
        timestamp = main.now_iso()
        proposals = [
            {
                "id": f"proposal-{uuid4().hex}",
                "operation": "create",
                "target_type": "event",
                "patch": {"title": f"Lifecycle event {uuid4().hex[:8]}", "date": "2026-09-28", "time": "08:00～09:00"},
            },
            {
                "id": f"proposal-{uuid4().hex}",
                "operation": "create",
                "target_type": "task",
                "patch": {"title": f"Lifecycle task {uuid4().hex[:8]}", "due": "2026-09-28"},
            },
        ]
        with main.db() as connection:
            for proposal in proposals:
                connection.execute(
                    """
                    INSERT INTO proposals(
                        id, user_id, source_id, operation, target_type, target_id,
                        patch_json, evidence_refs_json, target_candidates_json,
                        needs_review, review_reason, confidence, status, error,
                        created_at, updated_at, applied_at
                    ) VALUES (?, 'demo-user', 'notice-project-day', ?, ?, NULL, ?, '[]', '[]', 0, NULL, 'high', 'pending', NULL, ?, ?, NULL)
                    """,
                    (
                        proposal["id"], proposal["operation"], proposal["target_type"],
                        json.dumps(proposal["patch"]), timestamp, timestamp,
                    ),
                )

        response = Response()
        main.create_session("demo-user", response)
        for proposal in proposals:
            request = self._request(response, "POST", f"/api/proposals/{proposal['id']}/accept")
            accepted = main.accept_proposal(request, proposal["id"])
            self.assertEqual(accepted["status"], "accepted")

        with main.db() as connection:
            for proposal in proposals:
                if proposal["target_type"] == "event":
                    created = connection.execute("SELECT id FROM events WHERE title = ?", (proposal["patch"]["title"],)).fetchone()
                else:
                    created = connection.execute("SELECT id FROM tasks WHERE title = ?", (proposal["patch"]["title"],)).fetchone()
                self.assertIsNone(created, "Accept must not create an Event or Task")

        applied = []
        for proposal in proposals:
            request = self._request(response, "POST", f"/api/proposals/{proposal['id']}/apply")
            applied.append(main.apply_proposal_endpoint(request, proposal["id"]))

        self.assertEqual([result["proposal"]["status"] for result in applied], ["applied", "applied"])
        with main.db() as connection:
            event = connection.execute("SELECT origin_proposal_id FROM events WHERE title = ?", (proposals[0]["patch"]["title"],)).fetchone()
            task = connection.execute("SELECT source_id FROM tasks WHERE title = ?", (proposals[1]["patch"]["title"],)).fetchone()
        self.assertEqual(event["origin_proposal_id"], proposals[0]["id"])
        self.assertEqual(task["source_id"], "notice-project-day")

    def test_task_proposal_edit_accept_apply_persists_iso_due_date(self):
        timestamp = main.now_iso()
        task_id = f"task-{uuid4().hex}"
        proposal_id = f"proposal-{uuid4().hex}"
        with main.db() as connection:
            connection.execute(
                "INSERT INTO tasks(id, user_id, title, due_label, due_iso, status, source_id, owner) VALUES (?, 'demo-user', ?, '尚未確認', NULL, 'open', 'notice-project-day', 'student')",
                (task_id, "提交 OMT QA 測試報告"),
            )
            connection.execute(
                """
                INSERT INTO proposals(
                    id, user_id, source_id, operation, target_type, target_id, patch_json,
                    evidence_refs_json, target_candidates_json, needs_review,
                    confidence, status, created_at, updated_at
                ) VALUES (?, 'demo-user', 'notice-project-day', 'update', 'task', ?, '{"title":"提交 OMT QA 測試報告"}', '[]', '[]', 0, 'high', 'pending', ?, ?)
                """,
                (proposal_id, task_id, timestamp, timestamp),
            )

        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "PATCH", f"/api/proposals/{proposal_id}")
        edited = main.edit_proposal(
            request,
            proposal_id,
            main.ProposalEditInput(
                target_id=task_id,
                patch=main.ProposalPatch(title="提交 OMT QA 測試報告", due="2026-10-05"),
            ),
        )
        self.assertEqual(edited["patch"]["due"], "2026-10-05")
        accepted = main.accept_proposal(self._request(response, "POST", f"/api/proposals/{proposal_id}/accept"), proposal_id)
        self.assertEqual(accepted["status"], "accepted")
        main.apply_proposal_endpoint(self._request(response, "POST", f"/api/proposals/{proposal_id}/apply"), proposal_id)

        with main.db() as connection:
            task = connection.execute("SELECT due_iso, due_label FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(task["due_iso"], "2026-10-05")
        self.assertEqual(task["due_label"], "2026-10-05")

    def test_calendar_connection_migration_backfills_legacy_source_and_is_reversible(self):
        legacy_id = f"legacy-{uuid4().hex}"
        timestamp = main.now_iso()
        main.rollback_calendar_connection_migration()
        with main.db() as connection:
            connection.execute(
                """
                INSERT INTO calendar_sources(
                    id, user_id, kind, name, server_url, username, credential_ciphertext,
                    status, last_checked_at, last_error, sync_status, last_synced_at,
                    sync_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_id, "demo-user", "nextcloud-caldav", "Legacy Nextcloud",
                    "https://legacy.example/dav/", "legacy-user", main.encrypt_credential("legacy-secret"),
                    "connected", timestamp, None, "ok", timestamp, None, timestamp, timestamp,
                ),
            )
        main.init_db()
        with main.db() as connection:
            migrated = connection.execute("SELECT * FROM calendar_connections WHERE id = ?", (legacy_id,)).fetchone()
            credential = connection.execute("SELECT * FROM integration_credentials WHERE id = ?", (f"calendar:{legacy_id}",)).fetchone()
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        self.assertEqual(migrated["provider"], "nextcloud_calendar")
        self.assertEqual(migrated["status"], "ready")
        self.assertEqual(migrated["credential_ref"], f"calendar:{legacy_id}")
        self.assertEqual(main.credential_box().decrypt(credential["credential_ciphertext"].encode()).decode(), "legacy-secret")
        self.assertNotIn("provider", user_columns)
        self.assertNotIn("credential_ciphertext", user_columns)

        main.rollback_calendar_connection_migration()
        with main.db() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'calendar_connections'").fetchone())
            self.assertIsNotNone(connection.execute("SELECT id FROM calendar_sources WHERE id = ?", (legacy_id,)).fetchone())
        main.init_db()

    def test_calendar_connection_api_exposes_lifecycle_and_disconnects_without_secret(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://lifecycle.example/dav/", "calendars": [{"remote_url": "https://lifecycle.example/cal/", "display_name": "Lifecycle", "timezone": "UTC", "read_only": True}]},
            "lifecycle-user",
            "lifecycle-secret",
        )
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "GET", "/api/integrations")
        connection_id = saved["source"]["id"]
        listed = main.list_integrations(request)
        connection = next(item for item in listed["calendar"] if item["id"] == connection_id)
        self.assertEqual(connection["provider"], "nextcloud_calendar")
        self.assertEqual(connection["status"], "connected")
        self.assertNotIn("lifecycle-secret", json.dumps(connection))

        with patch.object(main, "caldav_calendar_report", return_value=[]):
            synced = main.sync_calendar_connection(request, connection_id)
        self.assertEqual(synced["connection"]["status"], "ready")
        self.assertEqual(synced["sync"]["status"], "ok")
        status = main.calendar_connection_status(request, connection_id)
        self.assertEqual(status["connection"]["status"], "ready")

        disconnected = main.disconnect_calendar_connection(request, connection_id)
        self.assertEqual(disconnected["connection"]["status"], "disconnected")
        self.assertIsNone(disconnected["connection"]["credential_ref"])
        with main.db() as connection:
            legacy = connection.execute("SELECT status FROM calendar_sources WHERE id = ?", (connection_id,)).fetchone()
            self.assertEqual(legacy["status"], "disconnected")
        # A disconnected integration may remain as an internal tombstone for
        # reconciliation, but it must disappear from the legacy source picker.
        visible_sources = main.calendar_sources(request)
        self.assertFalse(any(item["source"]["id"] == connection_id for item in visible_sources["sources"]))
        with self.assertRaises(main.HTTPException) as unavailable:
            main.sync_calendar_connection(request, connection_id)
        self.assertEqual(unavailable.exception.status_code, 409)

    def test_google_calendar_connection_uses_encrypted_generic_credential(self):
        connection = main.save_google_calendar_connection(
            "demo-user",
            {"access_token": "google-access-secret", "refresh_token": "google-refresh-secret", "scope": "calendar.readonly"},
        )
        self.assertEqual(connection["provider"], "google_calendar")
        self.assertEqual(connection["status"], "connected")
        self.assertNotIn("google-access-secret", json.dumps(connection))
        with main.db() as database:
            row = database.execute("SELECT * FROM integration_credentials WHERE id = ?", (connection["credential_ref"],)).fetchone()
        self.assertNotEqual(row["credential_ciphertext"], "google-access-secret")
        token_payload = json.loads(main.credential_box().decrypt(row["credential_ciphertext"].encode()).decode())
        self.assertEqual(token_payload["refresh_token"], "google-refresh-secret")

    def test_google_calendar_auth_expiry_returns_reconnect_status_instead_of_502(self):
        saved = main.save_google_calendar_connection(
            "demo-user",
            {"access_token": "expired-access", "refresh_token": "expired-refresh", "scope": "https://www.googleapis.com/auth/calendar"},
        )
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "POST", f"/api/integrations/calendar/connections/{saved['id']}/sync")
        with patch.object(main, "_google_access_token", side_effect=main.CalendarAuthorizationError("Google Calendar authorization is missing or expired")):
            with self.assertRaises(main.HTTPException) as error:
                main.sync_calendar_connection(request, saved["id"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("重新連線", error.exception.detail)
        with main.db() as database:
            connection = database.execute("SELECT status FROM calendar_connections WHERE id=?", (saved["id"],)).fetchone()
            source = database.execute("SELECT status,sync_status,sync_error,last_synced_at FROM calendar_sources WHERE id=?", (saved["id"],)).fetchone()
        self.assertEqual(connection["status"], "expired")
        self.assertEqual((source["status"], source["sync_status"]), ("error", "error"))
        self.assertEqual(source["sync_error"], "Google Calendar authorization is missing or expired")
        self.assertIsNone(source["last_synced_at"])

    def test_google_calendar_non_auth_sync_failure_remains_gateway_error(self):
        saved = main.save_google_calendar_connection(
            "demo-user",
            {"access_token": "access", "refresh_token": "refresh", "scope": "https://www.googleapis.com/auth/calendar"},
        )
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "POST", f"/api/integrations/calendar/connections/{saved['id']}/sync")
        with patch.object(main, "_google_access_token", side_effect=main.CalendarConnectionError("Google Calendar transport failed")):
            with self.assertRaises(main.HTTPException) as error:
                main.sync_calendar_connection(request, saved["id"])
        self.assertEqual(error.exception.status_code, 502)
        with main.db() as database:
            row = database.execute("SELECT status FROM calendar_connections WHERE id=?", (saved["id"],)).fetchone()
        self.assertEqual(row["status"], "error")

    def test_google_calendar_true_sync_pull_edit_and_delete(self):
        connection = main.save_google_calendar_connection(
            "demo-user",
            {
                "access_token": "google-access-secret",
                "refresh_token": "google-refresh-secret",
                "scope": "https://www.googleapis.com/auth/calendar",
            },
        )

        def list_all(_token, path, _query=None):
            if path == "users/me/calendarList":
                return [{
                    "id": "primary",
                    "summary": "我的 Google 行事曆",
                    "timeZone": "Asia/Taipei",
                    "accessRole": "owner",
                }]
            self.assertIn("calendars/primary/events", path)
            return [{
                "id": "google-event-1",
                "etag": '"g1"',
                "status": "confirmed",
                "summary": "Google 原始行程",
                "start": {"dateTime": "2026-10-02T09:00:00+08:00", "timeZone": "Asia/Taipei"},
                "end": {"dateTime": "2026-10-02T10:00:00+08:00", "timeZone": "Asia/Taipei"},
                "location": "A101",
                "description": "原始備註",
                "htmlLink": "https://calendar.google.com/event?eid=google-event-1",
            }]

        with patch.object(main, "_google_access_token", return_value="fresh-google-token"), patch.object(main, "_google_list_all", side_effect=list_all):
            result = main.sync_google_calendar_connection("demo-user", connection["id"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"], 1)

        with main.db() as database:
            event_row = database.execute("SELECT * FROM calendar_events WHERE remote_id = 'google-event-1'").fetchone()
            calendar_row = database.execute("SELECT * FROM calendar_calendars WHERE id = ?", (event_row["calendar_id"],)).fetchone()
        self.assertEqual(event_row["provider"], "google_calendar")
        self.assertEqual(calendar_row["remote_url"], "primary")
        self.assertEqual(event_row["description"], "原始備註")

        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "PATCH", f"/api/events/{event_row['id']}")
        writes = []
        def api_write(_token, path, *, method="GET", query=None, payload=None):
            writes.append((method, path, payload))
            return {"id": "google-event-1", "etag": '"g2"'} if method == "PATCH" else {}

        with patch.object(main, "_google_access_token", return_value="fresh-google-token"), patch.object(main, "google_calendar_api_json", side_effect=api_write):
            updated = main.update_event(request, event_row["id"], main.EventUpdate(detail="更新後備註", location="B202"))
        self.assertEqual(updated["description"], "更新後備註")
        self.assertEqual(updated["location"], "B202")
        patch_write = next(item for item in writes if item[0] == "PATCH")
        self.assertEqual(patch_write[2]["description"], "更新後備註")
        self.assertEqual(patch_write[2]["location"], "B202")

        with patch.object(main, "_google_access_token", return_value="fresh-google-token"), patch.object(main, "google_calendar_api_json", side_effect=api_write):
            deleted = main.delete_event(request, event_row["id"] )
        self.assertEqual(deleted["lifecycle_status"], "deleted")
        self.assertTrue(any(method == "DELETE" for method, _path, _payload in writes))

    def test_google_all_day_normalization_keeps_calendar_timezone(self):
        event = main._google_event_normalized(
            {
                "id": "all-day-1",
                "status": "confirmed",
                "summary": "全天活動",
                "start": {"date": "2026-10-03"},
                "end": {"date": "2026-10-04"},
            },
            "Asia/Taipei",
        )
        self.assertEqual(event["timezone"], "Asia/Taipei")
        self.assertEqual(event["start_at"], "2026-10-03T00:00:00+08:00")
        self.assertEqual(event["end_at"], "2026-10-04T00:00:00+08:00")
        self.assertTrue(event["all_day"])

    def test_google_local_event_create_round_trip_and_remote_delete(self):
        user_id = f"google-round-trip-{uuid4().hex}"
        timestamp = main.now_iso()
        with main.db() as database:
            database.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, ?)",
                (user_id, "Google round trip", timestamp, timestamp),
            )
        connection = main.save_google_calendar_connection(
            user_id,
            {
                "access_token": "google-access-secret",
                "refresh_token": "google-refresh-secret",
                "scope": "https://www.googleapis.com/auth/calendar",
            },
        )
        remote_events = []
        writes = []

        def list_all(_token, path, _query=None):
            if path == "users/me/calendarList":
                return [{
                    "id": "primary",
                    "summary": "我的 Google 行事曆",
                    "timeZone": "Asia/Taipei",
                    "accessRole": "owner",
                }]
            self.assertIn("calendars/primary/events", path)
            return list(remote_events)

        def api_json(_token, path, *, method="GET", query=None, payload=None):
            writes.append((method, path, payload))
            if method == "POST":
                created = {
                    "id": "omt-google-created",
                    "etag": '"created-v1"',
                    "status": "confirmed",
                    "summary": payload["summary"],
                    "start": payload["start"],
                    "end": payload["end"],
                    "location": payload.get("location"),
                    "description": payload.get("description"),
                    "htmlLink": "https://calendar.google.com/event?eid=omt-google-created",
                }
                remote_events[:] = [created]
                return created
            if method == "PATCH":
                remote_events[0]["summary"] = payload["summary"]
                remote_events[0]["description"] = payload.get("description")
                remote_events[0]["location"] = payload.get("location")
                remote_events[0]["etag"] = '"created-v2"'
                return remote_events[0]
            if method == "DELETE":
                remote_events.clear()
                return {}
            return {}

        with patch.object(main, "_google_access_token", return_value="fresh-google-token"), patch.object(main, "_google_list_all", side_effect=list_all), patch.object(main, "google_calendar_api_json", side_effect=api_json):
            main.sync_google_calendar_connection(user_id, connection["id"])
            response = Response(); main.create_session(user_id, response)
            request = self._request(response, "POST", "/api/events")
            event = main.create_event(request, main.EventCreate(title="OMT → Google", date_label="2026-10-05", time_label="09:00-10:00", detail="初始備註", location="A101"))
            main.sync_local_event_to_default_calendar(user_id, event["id"])

            with main.db() as database:
                linked = database.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()
            self.assertEqual(linked["external_calendar_ref"], "omt-google-created")
            self.assertEqual(linked["provider"], "google_calendar")
            self.assertEqual(linked["sync_status"], "synced")

            main.update_event(request, event["id"], main.EventUpdate(title="Google round-trip edited", detail="更新備註"))
            main.sync_local_event_to_default_calendar(user_id, event["id"])
            self.assertTrue(any(method == "PATCH" for method, _path, _payload in writes))
            self.assertEqual(remote_events[0]["summary"], "Google round-trip edited")

            # Simulate deletion on Google itself, then pull it back into OMT.
            remote_events.clear()
            main.sync_google_calendar_connection(user_id, connection["id"])
            with main.db() as database:
                after_delete = database.execute("SELECT lifecycle_status, external_calendar_ref, calendar_sync_enabled FROM events WHERE id=?", (event["id"],)).fetchone()
            self.assertEqual(after_delete["lifecycle_status"], "deleted")
            self.assertIsNone(after_delete["external_calendar_ref"])
            self.assertFalse(bool(after_delete["calendar_sync_enabled"]))

    def test_login_flow_start_resolves_relative_urls(self):
        response = FakeResponse(
            b'{"poll":{"token":"poll-secret","endpoint":"/index.php/login/v2/poll"},"login":"/login/flow"}'
        )
        with patch.object(main, "_calendar_urlopen", return_value=response) as urlopen:
            flow = main.nextcloud_login_flow_start("cloud.example")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://cloud.example/index.php/login/v2")
        self.assertEqual(flow["poll_endpoint"], "https://cloud.example/index.php/login/v2/poll")
        self.assertEqual(flow["login_url"], "https://cloud.example/login/flow")
        self.assertEqual(flow["poll_token"], "poll-secret")

    def test_login_flow_does_not_send_poll_token_to_another_origin(self):
        response = FakeResponse(
            b'{"poll":{"token":"poll-secret","endpoint":"https://collector.example/poll"},"login":"/login/flow"}'
        )
        with patch.object(main, "_calendar_urlopen", return_value=response):
            with self.assertRaises(main.CalendarConnectionError):
                main.nextcloud_login_flow_start("cloud.example")

    def test_login_flow_404_is_pending(self):
        pending = main.urllib.error.HTTPError(
            "https://cloud.example/index.php/login/v2/poll", 404, "pending", {}, io.BytesIO()
        )
        with patch.object(main, "_calendar_urlopen", side_effect=pending):
            self.assertEqual(main.nextcloud_login_flow_poll("https://cloud.example/poll", "poll-secret"), ("pending", None))

    def test_icalendar_parser_normalizes_timed_all_day_and_cancelled_events(self):
        icalendar = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:timed-1\r
DTSTART;TZID=Asia/Taipei:20260919T090000\r
DTEND;TZID=Asia/Taipei:20260919T100000\r
SUMMARY:課堂\r
LOCATION:教室 A\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:day-1\r
DTSTART;VALUE=DATE:20260920\r
DTEND;VALUE=DATE:20260921\r
SUMMARY:全天活動\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:start-only-1\r
DTSTART;TZID=Asia/Taipei:20260921T110000\r
SUMMARY:Start only\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancelled-1\r
DTSTART:20200101T000000Z\r
DTEND:20200101T010000Z\r
SUMMARY:已取消\r
STATUS:CANCELLED\r
END:VEVENT\r
END:VCALENDAR\r
"""
        events = main.parse_icalendar_events(icalendar, "https://cloud.example/demo.ics", '"etag-1"', "Asia/Taipei")
        self.assertEqual([event["remote_id"] for event in events], ["timed-1", "day-1", "start-only-1", "cancelled-1"])
        self.assertEqual(events[0]["start_at"], "2026-09-19T09:00:00+08:00")
        self.assertTrue(events[1]["all_day"])
        self.assertEqual(events[2]["start_at"], "2026-09-21T11:00:00+08:00")
        self.assertIsNone(events[2]["end_at"])
        self.assertEqual(events[3]["status"], "cancelled")

    def test_caldav_report_and_sync_persist_normalized_events(self):
        report_xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
          <d:response><d:href>/cal/demo/event.ics</d:href><d:propstat><d:prop><d:getetag>"v1"</d:getetag><c:calendar-data>BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:event-1\r\nDTSTART:20260919T010000Z\r\nDTEND:20260919T020000Z\r\nSUMMARY:Remote event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n</c:calendar-data></d:prop></d:propstat></d:response>
        </d:multistatus>'''
        with patch.object(main, "_calendar_urlopen", return_value=FakeResponse(report_xml, 207)):
            report = main.caldav_calendar_report("https://cloud.example/cal/", "demo", "secret")
        self.assertEqual(report[0]["etag"], '"v1"')
        self.assertIn("BEGIN:VEVENT", report[0]["calendar_data"] or "")

        discovered = {
            "server_url": "https://cloud.example/remote.php/dav/",
            "calendars": [{"remote_url": "https://cloud.example/cal/", "display_name": "Demo", "timezone": "UTC", "read_only": True}],
        }
        saved = main.save_nextcloud_source("demo-user", discovered, "demo", "secret")
        with patch.object(main, "caldav_calendar_report", return_value=[{"href": "https://cloud.example/cal/event.ics", "etag": '"v1"', "calendar_data": "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:event-1\r\nDTSTART:20260919T010000Z\r\nDTEND:20260919T020000Z\r\nSUMMARY:Remote event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"}]):
            result = main.sync_calendar_source("demo-user", saved["source"]["id"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["event_count"], 1)
        with main.db() as connection:
            stored = connection.execute("SELECT title, all_day, status FROM calendar_events WHERE source_id = ?", (saved["source"]["id"],)).fetchone()
        self.assertEqual(dict(stored), {"title": "Remote event", "all_day": 0, "status": "confirmed"})

    def test_completed_login_flow_persists_app_password_and_runs_initial_sync(self):
        response = Response()
        main.create_session("demo-user", response)
        flow_id = uuid4().hex
        with main.db() as connection:
            connection.execute(
                "INSERT INTO calendar_login_flows(id, user_id, base_url, poll_token_ciphertext, poll_endpoint, login_url, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    flow_id,
                    "demo-user",
                    "https://cloud.example",
                    main.encrypt_credential("poll-secret"),
                    "https://cloud.example/poll",
                    "https://cloud.example/login",
                    "pending",
                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    main.now_iso(),
                    main.now_iso(),
                ),
            )
        discovered = {
            "server_url": "https://cloud.example/remote.php/dav/",
            "calendars": [{"remote_url": "https://cloud.example/cal/", "display_name": "Demo", "timezone": "UTC", "read_only": True}],
        }
        with patch.object(main, "nextcloud_login_flow_poll", return_value=("complete", {"server": "https://cloud.example", "loginName": "demo", "appPassword": "device-secret"})), patch.object(main, "discover_nextcloud", return_value=discovered), patch.object(main, "sync_calendar_source", return_value={"source_id": "source", "status": "ok", "event_count": 0, "last_synced_at": main.now_iso()}):
            result = main.poll_nextcloud_login(self._request_with_session(response), flow_id)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["sync"]["status"], "ok")
        with main.db() as connection:
            flow = connection.execute("SELECT status, poll_token_ciphertext FROM calendar_login_flows WHERE id = ?", (flow_id,)).fetchone()
            source = connection.execute("SELECT credential_ciphertext FROM calendar_sources WHERE id = ?", (result["source"]["id"],)).fetchone()
        self.assertEqual(flow["status"], "connected")
        self.assertEqual(flow["poll_token_ciphertext"], "")
        self.assertEqual(main.credential_box().decrypt(source["credential_ciphertext"].encode()).decode(), "device-secret")

    def test_calendars_are_persisted_read_only_with_encrypted_credential(self):
        result = main.save_nextcloud_source(
            "demo-user",
            {
                "server_url": "https://cloud.example/remote.php/dav/",
                "calendars": [{"remote_url": "https://cloud.example/calendars/demo/", "display_name": "Demo", "timezone": "UTC"}],
            },
            "demo",
            "device-app-password",
        )
        self.assertEqual(result["calendars"][0]["display_name"], "Demo")
        with main.db() as connection:
            source = connection.execute("SELECT credential_ciphertext FROM calendar_sources WHERE id = ?", (result["source"]["id"],)).fetchone()
            calendar = connection.execute("SELECT read_only FROM calendar_calendars WHERE source_id = ?", (result["source"]["id"],)).fetchone()
        self.assertNotEqual(source["credential_ciphertext"], "device-app-password")
        self.assertEqual(main.credential_box().decrypt(source["credential_ciphertext"].encode()).decode(), "device-app-password")
        self.assertEqual(calendar["read_only"], 1)

    def test_dev_login_is_disabled_outside_development(self):
        with patch.object(main, "APP_ENVIRONMENT", "production"), patch.dict(os.environ, {"OMT_ENABLE_DEV_LOGIN": "1"}):
            self.assertFalse(main.dev_login_enabled())

    def test_session_cookie_is_hashed_at_rest_and_can_be_read_back(self):
        response = Response()
        main.create_session("demo-user", response)
        cookies = SimpleCookie(response.headers["set-cookie"])
        session_token = cookies[main.SESSION_COOKIE].value
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/auth/me",
                "headers": [(b"cookie", f"{main.SESSION_COOKIE}={session_token}".encode())],
            }
        )
        self.assertEqual(main.user_from_request(request)["id"], "demo-user")
        with main.db() as connection:
            stored = connection.execute("SELECT id, token_hash FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertNotEqual(stored["id"], session_token)
        self.assertNotEqual(stored["token_hash"], session_token)
        self.assertEqual(len(stored["token_hash"]), 64)

    def test_oauth_callback_uses_stable_errors_and_clears_state_cookie(self):
        request = self._request_with_cookie(
            f"{main.OAUTH_STATE_COOKIE}=github:login:nonce",
            {"error": "access_denied"},
            path="/api/auth/github/callback",
        )
        response = main.auth_callback("github", request)
        self.assertIn("auth_error=access_denied", response.headers["location"])
        self.assertIn(f"{main.OAUTH_STATE_COOKIE}=\"\"", response.headers["set-cookie"])
        self.assertIn("Max-Age=0", response.headers["set-cookie"])

        mismatch = self._request_with_cookie(
            f"{main.OAUTH_STATE_COOKIE}=github:login:expected",
            {"state": "actual", "code": "opaque-code"},
            path="/api/auth/github/callback",
        )
        response = main.auth_callback("github", mismatch)
        self.assertIn("auth_error=state_mismatch", response.headers["location"])

    def test_oauth_callback_creates_session_without_exposing_provider_token(self):
        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "client-id", "GITHUB_CLIENT_SECRET": "client-secret"}):
            identity = main.OAuthIdentity(subject="github-123", email="oauth@example.com", email_verified=True, name="OAuth User")
            request = self._request_with_cookie(
                f"{main.OAUTH_STATE_COOKIE}=github:login:nonce",
                {"state": "nonce", "code": "provider-code"},
                path="/api/auth/github/callback",
            )
            with patch.object(main, "github_identity", return_value=identity), patch.object(main, "resolve_identity", return_value="demo-user"):
                response = main.auth_callback("github", request)
        self.assertIn("auth=success", response.headers["location"])
        self.assertIn(main.SESSION_COOKIE, response.headers["set-cookie"])
        self.assertNotIn("provider-code", response.headers["location"])
        self.assertNotIn("client-secret", response.headers["location"])

    def test_oauth_callback_converts_provider_failures_to_stable_error(self):
        request = self._request_with_cookie(
            f"{main.OAUTH_STATE_COOKIE}=github:login:nonce",
            {"state": "nonce", "code": "provider-code"},
            path="/api/auth/github/callback",
        )
        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "client-id", "GITHUB_CLIENT_SECRET": "client-secret"}), patch.object(
            main, "github_identity", side_effect=ValueError("malformed provider payload")
        ):
            response = main.auth_callback("github", request)
        self.assertIn("auth_error=callback_failed", response.headers["location"])
        self.assertNotIn("malformed provider payload", response.headers["location"])

    def test_oauth_callback_rejects_tampered_state_mode(self):
        request = self._request_with_cookie(
            f"{main.OAUTH_STATE_COOKIE}=unexpected:nonce",
            {"state": "nonce", "code": "provider-code"},
            path="/api/auth/github/callback",
        )
        response = main.auth_callback("github", request)
        self.assertIn("auth_error=invalid_state", response.headers["location"])

    def test_dev_login_is_forbidden_when_disabled_and_auth_me_is_no_store(self):
        with patch.object(main, "APP_ENVIRONMENT", "production"):
            with self.assertRaises(main.HTTPException) as disabled:
                main.dev_login(Response())
        self.assertEqual(disabled.exception.status_code, 403)
        response = Response()
        request = Request({"type": "http", "method": "GET", "path": "/api/auth/me", "headers": []})
        missing = main.auth_me(request, response)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(missing["detail"], "Not signed in")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_account_avatar_replacement_overwrites_previous_avatar(self):
        user_id = f"avatar-user-{uuid4().hex[:8]}"
        timestamp = main.now_iso()
        with main.db() as connection:
            connection.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, "Avatar User", "avatar@example.com", "https://old.example/avatar.png", timestamp, timestamp),
            )
        session_response = Response()
        main.create_session(user_id, session_response)
        request = self._request(session_response, "PATCH", "/api/account/avatar")
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC")
        data_url = main._bytes_as_data_url(png, "image/png")

        response = Response()
        result = main.update_account_avatar(request, main.AvatarUpdateInput(data_url=data_url), response)

        self.assertEqual(result["user"]["avatar_url"], data_url)
        self.assertNotIn("old.example", result["user"]["avatar_url"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        with main.db() as connection:
            stored = connection.execute("SELECT avatar_url FROM users WHERE id = ?", (user_id,)).fetchone()
        self.assertEqual(stored["avatar_url"], data_url)

    def test_delete_account_removes_user_sessions_and_linked_records(self):
        user_id = f"delete-user-{uuid4().hex[:8]}"
        timestamp = main.now_iso()
        with main.db() as connection:
            connection.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?)",
                (user_id, "Delete User", "delete@example.com", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO auth_accounts(id, user_id, provider, provider_subject, email, email_verified, created_at) VALUES (?, ?, 'github', ?, ?, 1, ?)",
                (uuid4().hex, user_id, f"subject-{user_id}", "delete@example.com", timestamp),
            )
            connection.execute(
                "INSERT INTO personal_lists(id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                (uuid4().hex, user_id, "Temporary list", timestamp),
            )
        session_response = Response()
        main.create_session(user_id, session_response)
        request = self._request(session_response, "DELETE", "/api/account")
        response = Response()

        result = main.delete_account(request, response)

        self.assertTrue(result["ok"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(any(main.SESSION_COOKIE in value for value in response.headers.getlist("set-cookie")))
        with main.db() as connection:
            self.assertIsNone(connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_accounts WHERE user_id = ?", (user_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM personal_lists WHERE user_id = ?", (user_id,)).fetchone()[0], 0)

    def test_notice_preview_and_detail_include_clarification_confidence_and_source(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/notices/parse")
        payload = main.NoticeInput(
            title="上傳的校外教學公告",
            body="集合時間為早上 07:30，地點待確認。",
            audience="我的課程",
            source_type="upload",
            source_name="field-trip.txt",
        )
        preview = main.preview_notice(request, payload)
        self.assertTrue(preview["structured"]["needs_clarification"])
        self.assertEqual(preview["structured"]["source"]["name"], "field-trip.txt")
        self.assertIn("confidence_score", preview["structured"])

        created = main.create_notice(
            request,
            main.NoticeCreateInput(
                title="可編輯公告",
                body="請週五前交回表單。",
                source_type="manual",
                structured=main.StructuredNoticeInput(
                    tasks=[main.ParsedTaskInput(title="交回表單", due_label="週五前", needs_clarification=True)],
                    events=[],
                ),
            ),
        )
        detail = main.get_notice(request, created["notice"]["id"])
        self.assertEqual(detail["id"], created["notice"]["id"])
        self.assertEqual(detail["body"], created["notice"]["body"])
        self.assertEqual(detail["structured"]["tasks"][0]["status"], "needs_clarification")
        listed = main.list_notices(request, q="可編輯")
        self.assertEqual(listed["meta"]["count"], 1)

    def test_task_crud_filters_and_error_semantics(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/tasks")
        created = main.create_task(
            request,
            main.TaskCreate(title=f"手動任務-{uuid4().hex[:6]}", due_label="今天", needs_clarification=True),
        )
        task_id = created["id"]
        self.assertEqual(created["status"], "needs_clarification")
        filtered = main.list_tasks(request, status="needs_clarification", needs_clarification=True)
        self.assertTrue(any(task["id"] == task_id for task in filtered["tasks"]))
        updated = main.update_task(request, task_id, main.TaskUpdate(title="已更新任務", status="done"))
        self.assertEqual(updated["title"], "已更新任務")
        self.assertEqual(updated["status"], "done")
        with self.assertRaises(main.HTTPException) as empty_update:
            main.update_task(request, task_id, main.TaskUpdate())
        self.assertEqual(empty_update.exception.status_code, 422)
        main.delete_task(request, task_id)
        with self.assertRaises(main.HTTPException) as missing:
            main.get_task(request, task_id)
        self.assertEqual(missing.exception.status_code, 404)

    def test_task_delete_http_returns_204_and_reload_excludes_task(self):
        response = Response()
        main.create_session("demo-user", response)
        task = main.create_task(
            self._request(response, "POST", "/api/tasks"),
            main.TaskCreate(title=f"Task delete lifecycle {uuid4().hex[:8]}", due_label="Tomorrow"),
        )
        session_token = SimpleCookie(response.headers["set-cookie"])[main.SESSION_COOKIE].value

        async def exercise_http_lifecycle():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(main.SESSION_COOKIE, session_token)
                before = await client.get("/api/tasks")
                self.assertEqual(before.status_code, 200)
                self.assertIn(task["id"], {item["id"] for item in before.json()["tasks"]})

                deleted = await client.delete(f"/api/tasks/{task['id']}")
                self.assertEqual(deleted.status_code, 204)
                self.assertEqual(deleted.content, b"")

                after = await client.get("/api/tasks")
                self.assertEqual(after.status_code, 200)
                self.assertNotIn(task["id"], {item["id"] for item in after.json()["tasks"]})
                self.assertEqual((await client.get(f"/api/tasks/{task['id']}")).status_code, 404)

        asyncio.run(exercise_http_lifecycle())

    def test_capture_raw_upload_accepts_image_bytes_without_base64_json(self):
        response = Response()
        main.create_session("demo-user", response)
        session_token = SimpleCookie(response.headers["set-cookie"])[main.SESSION_COOKIE].value
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZK0sAAAAASUVORK5CYII="
        )

        async def exercise_upload():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(main.SESSION_COOKIE, session_token)
                uploaded = await client.post(
                    "/api/capture/uploads",
                    params={"filename": "sample.png"},
                    content=png,
                    headers={"Content-Type": "image/png"},
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                payload = uploaded.json()["upload"]
                self.assertTrue(payload["id"].startswith("upload-"))
                self.assertEqual(payload["name"], "sample.png")
                self.assertEqual(payload["kind"], "image")
                self.assertEqual(payload["size"], len(png))
                return payload["id"]

        upload_id = asyncio.run(exercise_upload())
        with main.db() as connection:
            stored = connection.execute(
                "SELECT * FROM capture_uploads WHERE id = ? AND user_id = 'demo-user'",
                (upload_id,),
            ).fetchone()
        self.assertIsNotNone(stored)
        self.assertTrue(main.ATTACHMENT_STORE.exists(stored["storage_key"]))

    def test_personal_lists_are_persisted_user_scoped_and_assignable(self):
        first_response = Response()
        main.create_session("demo-user", first_response)
        first_request = self._request(first_response, "POST", "/api/lists")
        created_list = main.create_personal_list(first_request, main.ListCreate(name="  考試準備  "))["list"]
        self.assertEqual(created_list["name"], "考試準備")
        self.assertEqual(main.list_personal_lists(first_request)["lists"], [created_list])

        with self.assertRaises(main.HTTPException) as duplicate:
            main.create_personal_list(first_request, main.ListCreate(name="考試準備"))
        self.assertEqual(duplicate.exception.status_code, 409)

        assigned = main.create_task(
            first_request,
            main.TaskCreate(title=f"清單任務-{uuid4().hex[:6]}", list_id=created_list["id"]),
        )
        self.assertEqual(assigned["list_id"], created_list["id"])
        self.assertEqual(main.list_tasks(first_request, list_id=created_list["id"])["tasks"][0]["id"], assigned["id"])
        moved = main.update_task(first_request, assigned["id"], main.TaskUpdate(list_id=None))
        self.assertIsNone(moved["list_id"])

        other_id = f"other-{uuid4().hex}"
        timestamp = main.now_iso()
        with main.db() as connection:
            connection.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, ?)",
                (other_id, "Other", timestamp, timestamp),
            )
        other_response = Response()
        main.create_session(other_id, other_response)
        other_request = self._request(other_response, "GET", "/api/lists")
        self.assertEqual(main.list_personal_lists(other_request)["lists"], [])
        with self.assertRaises(main.HTTPException) as foreign_list:
            main.create_task(other_request, main.TaskCreate(title="跨帳號分組", list_id=created_list["id"]))
        self.assertEqual(foreign_list.exception.status_code, 404)

        persisted_task = main.create_task(
            first_request,
            main.TaskCreate(title=f"保留清單關聯-{uuid4().hex[:6]}", list_id=created_list["id"]),
        )

        with main.db() as connection:
            before = [tuple(row) for row in connection.execute("SELECT id, user_id, title, list_id FROM tasks ORDER BY id").fetchall()]
        main.init_db()
        with main.db() as connection:
            after = [tuple(row) for row in connection.execute("SELECT id, user_id, title, list_id FROM tasks ORDER BY id").fetchall()]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(version, main.SCHEMA_VERSION)
        self.assertIn((persisted_task["id"], "demo-user", persisted_task["title"], created_list["id"]), after)

    def test_deleting_personal_list_preserves_tasks_and_moves_them_to_ungrouped(self):
        delete_routes = [route for route in main.app.routes if getattr(route, "path", None) == "/api/lists/{list_id}" and "DELETE" in (getattr(route, "methods", None) or set())]
        self.assertEqual(len(delete_routes), 1)
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "DELETE", "/api/lists/list-id")
        personal_list = main.create_personal_list(request, main.ListCreate(name=f"刪除測試-{uuid4().hex[:6]}"))["list"]
        tasks = [
            main.create_task(request, main.TaskCreate(title=f"保留待辦-{index}-{uuid4().hex[:5]}", list_id=personal_list["id"]))
            for index in range(2)
        ]

        result = main.delete_personal_list(request, personal_list["id"])

        self.assertEqual(result["deleted"], True)
        self.assertNotIn(personal_list["id"], {item["id"] for item in main.list_personal_lists(request)["lists"]})
        remaining = {item["id"]: item for item in main.list_tasks(request)["tasks"]}
        for task in tasks:
            self.assertIn(task["id"], remaining)
            self.assertIsNone(remaining[task["id"]]["list_id"])

    def test_start_only_event_stays_open_ended_through_calendar_serialization(self):
        schedule = main._event_schedule_fields({"date": "2026-10-02", "time": "09:00"})
        self.assertEqual(schedule["time_label"], "09:00")
        self.assertEqual(datetime.fromisoformat(schedule["start_at"]).strftime("%Y-%m-%d %H:%M"), "2026-10-02 09:00")
        self.assertIsNone(schedule["end_at"])

        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="只有開始時間", date_label="2026-10-02", time_label="09:00"))
        with main.db() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event["id"],)).fetchone()
        calendar_input = main.local_event_calendar_input(row, "source", "calendar")
        self.assertEqual(calendar_input.start_at, schedule["start_at"])
        self.assertIsNone(calendar_input.end_at)
        serialized = main.build_icalendar_event(calendar_input, "start-only", datetime.now(timezone.utc))
        self.assertIn("DTSTART", serialized)
        self.assertNotIn("DTEND", serialized)

        # Start-only events behave like an instant for range queries. They must
        # appear around DTSTART, but must not leak into unrelated days merely
        # because end_at is intentionally NULL.
        in_range = main.list_events(
            request,
            start="2026-10-02T08:00:00+08:00",
            end="2026-10-02T10:00:00+08:00",
        )["events"]
        outside_range = main.list_events(
            request,
            start="2026-10-03T08:00:00+08:00",
            end="2026-10-03T10:00:00+08:00",
        )["events"]
        self.assertIn(event["id"], {item["id"] for item in in_range})
        self.assertNotIn(event["id"], {item["id"] for item in outside_range})

        all_day = main._event_schedule_fields({"date": "2026-10-03", "all_day": True})
        self.assertEqual(all_day["start_at"], "2026-10-03")
        self.assertEqual(all_day["end_at"], "2026-10-04")

        start_and_end = main.create_event(
            request,
            main.EventCreate(title="完整時段", start_at="2026-10-04T09:00:00+08:00", end_at="2026-10-04T10:30:00+08:00", timezone="Asia/Taipei"),
        )
        self.assertEqual(start_and_end["start_at"], "2026-10-04T09:00:00+08:00")
        self.assertEqual(start_and_end["end_at"], "2026-10-04T10:30:00+08:00")
        all_day_event = main.create_event(request, main.EventCreate(title="全天活動", date_label="2026-10-05", all_day=True))
        self.assertEqual((all_day_event["start_at"], all_day_event["end_at"], all_day_event["all_day"]), ("2026-10-05", "2026-10-06", True))

    def test_batch_apply_route_is_registered(self):
        batch_routes = [route for route in main.app.routes if getattr(route, "path", None) == "/api/proposals/apply-batch" and "POST" in (getattr(route, "methods", None) or set())]
        self.assertEqual(len(batch_routes), 1)

    def test_batch_apply_input_accepts_bulk_capture_over_40_items(self):
        proposal_ids = [f"proposal-{index:03d}" for index in range(45)]
        payload = main.ProposalBatchApplyInput(proposal_ids=proposal_ids, confirm=True)
        self.assertEqual(payload.proposal_ids, proposal_ids)

    def test_batch_apply_confirms_and_applies_each_proposal_independently(self):
        timestamp = main.now_iso()
        valid_id = f"proposal-{uuid4().hex}"
        invalid_id = f"proposal-{uuid4().hex}"
        with main.db() as connection:
            connection.execute(
                "INSERT INTO proposals(id, user_id, source_id, operation, target_type, target_id, patch_json, evidence_refs_json, target_candidates_json, needs_review, review_reason, confidence, status, created_at, updated_at) VALUES (?, 'demo-user', 'notice-project-day', 'create', 'task', NULL, ?, '[]', '[]', 0, NULL, 'high', 'pending', ?, ?)",
                (valid_id, json.dumps({"title": f"批次待辦-{uuid4().hex[:6]}"}), timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO proposals(id, user_id, source_id, operation, target_type, target_id, patch_json, evidence_refs_json, target_candidates_json, needs_review, review_reason, confidence, status, created_at, updated_at) VALUES (?, 'demo-user', 'notice-project-day', 'create', 'task', NULL, ?, '[]', '[]', 1, 'needs review', 'low', 'pending', ?, ?)",
                (invalid_id, json.dumps({"title": "需要補充"}), timestamp, timestamp),
            )
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/proposals/apply-batch")

        result = main.apply_proposal_batch(request, main.ProposalBatchApplyInput(proposal_ids=[valid_id, invalid_id], confirm=True))

        self.assertEqual([item["status"] for item in result["results"]], ["applied", "failed"])
        self.assertEqual(result["status"], "partial")
        with main.db() as connection:
            valid = connection.execute("SELECT status FROM proposals WHERE id = ?", (valid_id,)).fetchone()
            invalid = connection.execute("SELECT status FROM proposals WHERE id = ?", (invalid_id,)).fetchone()
            created = connection.execute("SELECT id FROM tasks WHERE source_id = 'notice-project-day' AND title LIKE '批次待辦-%'").fetchone()
        self.assertEqual(valid["status"], "applied")
        self.assertEqual(invalid["status"], "pending")
        self.assertIsNotNone(created)

    def test_batch_apply_uses_one_transaction_for_mixed_event_and_task(self):
        timestamp = main.now_iso()
        event_id = f"proposal-{uuid4().hex}"
        task_id = f"proposal-{uuid4().hex}"
        with main.db() as connection:
            for proposal_id, target_type, patch in (
                (event_id, "event", {"title": "批次活動", "date": "2026-10-06", "time": "09:00–10:00"}),
                (task_id, "task", {"title": "批次待辦"}),
            ):
                connection.execute(
                    "INSERT INTO proposals(id, user_id, source_id, operation, target_type, target_id, patch_json, evidence_refs_json, target_candidates_json, needs_review, review_reason, confidence, status, created_at, updated_at) VALUES (?, 'demo-user', 'notice-project-day', 'create', ?, NULL, ?, '[]', '[]', 0, NULL, 'high', 'pending', ?, ?)",
                    (proposal_id, target_type, json.dumps(patch), timestamp, timestamp),
                )
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/proposals/apply-batch")

        result = main.apply_proposal_batch(request, main.ProposalBatchApplyInput(proposal_ids=[event_id, task_id], confirm=True))

        self.assertEqual([item["status"] for item in result["results"]], ["applied", "applied"])
        self.assertEqual(result["performance"]["after"]["db_transactions"], 1)
        self.assertEqual(result["performance"]["before"]["db_transactions"], 4)
        self.assertEqual((result["performance"]["before"]["http_requests"], result["performance"]["after"]["http_requests"]), (1, 1))
        with main.db() as connection:
            event = connection.execute("SELECT title, start_at, end_at FROM events WHERE title = '批次活動'").fetchone()
            task = connection.execute("SELECT title FROM tasks WHERE title = '批次待辦'").fetchone()
        self.assertEqual((event["title"], event["start_at"], event["end_at"]), ("批次活動", "2026-10-06T09:00:00+08:00", "2026-10-06T10:00:00+08:00"))
        self.assertEqual(task["title"], "批次待辦")

    def test_automatic_calendar_sync_endpoint_only_queues_background_work(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/calendar/auto-sync")
        background = main.BackgroundTasks()

        result = main.schedule_automatic_calendar_sync(request, background)

        self.assertEqual(result, {"status": "scheduled"})
        self.assertEqual(len(background.tasks), 1)
        self.assertIs(background.tasks[0].func, main.sync_user_calendars_in_background)

    def test_editing_local_event_queues_default_calendar_sync(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="Draft event", date_label="2026-10-04", time_label="09:00"))
        background = main.BackgroundTasks()

        updated = main.update_event(
            request,
            event["id"],
            main.EventUpdate(title="Updated event"),
            background_tasks=background,
        )

        self.assertEqual(updated["title"], "Updated event")
        with main.db() as connection:
            saved = connection.execute("SELECT sync_status FROM events WHERE id = ?", (event["id"],)).fetchone()
        self.assertEqual(saved["sync_status"], "pending")
        self.assertEqual(len(background.tasks), 1)
        self.assertIs(background.tasks[0].func, main.sync_local_event_to_default_calendar)
        self.assertEqual(background.tasks[0].args, ("demo-user", event["id"]))

    def test_local_event_create_and_linked_delete_queue_default_calendar_sync(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        create_background = main.BackgroundTasks()
        event = main.create_event(request, main.EventCreate(title="Queued create", date_label="2026-10-06", time_label="09:00"), background_tasks=create_background)
        self.assertEqual(len(create_background.tasks), 1)
        self.assertIs(create_background.tasks[0].func, main.sync_local_event_to_default_calendar)

        with main.db() as connection:
            connection.execute("UPDATE events SET external_calendar_ref = ?, calendar_sync_enabled = 1 WHERE id = ?", ("remote-queued", event["id"]))
        delete_background = main.BackgroundTasks()
        deleted = main.delete_event(request, event["id"], background_tasks=delete_background)
        self.assertEqual(deleted["lifecycle_status"], "deleted")
        self.assertEqual(len(delete_background.tasks), 1)
        self.assertIs(delete_background.tasks[0].func, main.sync_local_event_to_default_calendar)

    def test_local_and_ai_events_round_trip_writable_calendar_create_update_delete(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://round-trip.example/dav/", "calendars": [{"remote_url": "https://round-trip.example/cal/", "display_name": "Writable", "timezone": "Asia/Taipei", "read_only": False}]},
            "student",
            "secret",
        )
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        local_event = main.create_event(request, main.EventCreate(title="OMT writable", date_label="2026-10-07", time_label="09:00-10:00"))
        ai_event = main.create_event(request, main.EventCreate(title="AI writable", date_label="2026-10-07", time_label="11:00-12:00"))
        with main.db() as connection:
            connection.execute("UPDATE events SET created_source = 'ai_generated', sync_status = 'pending' WHERE id = ?", (ai_event["id"],))

        writes = []
        deletes = []

        def fake_put(url, username, password, icalendar, *, etag=None):
            writes.append({"url": url, "icalendar": icalendar, "etag": etag})

        def fake_report(url, username, password):
            return [{"href": item["url"], "etag": f'"etag-{index}"', "calendar_data": item["icalendar"]} for index, item in enumerate(writes, start=1)]

        def fake_get(url, username, password):
            for index, item in enumerate(reversed(writes), start=1):
                if item["url"] == url:
                    return item["icalendar"], f'"get-{index}"'
            raise main.CalendarConnectionError("fixture event missing")

        def fake_delete(url, username, password, *, etag=None):
            deletes.append({"url": url, "etag": etag})

        with patch.object(main, "caldav_put_event", side_effect=fake_put), patch.object(main, "caldav_get_event", side_effect=fake_get), patch.object(main, "caldav_calendar_report", side_effect=fake_report), patch.object(main, "caldav_delete_event", side_effect=fake_delete):
            main.sync_local_event_to_default_calendar("demo-user", local_event["id"])
            main.sync_local_event_to_default_calendar("demo-user", ai_event["id"])

            updated = main.update_event(request, local_event["id"], main.EventUpdate(title="OMT writable updated"))
            main.sync_local_event_to_default_calendar("demo-user", local_event["id"])
            deleted = main.delete_event(request, local_event["id"])
            main.sync_local_event_to_default_calendar("demo-user", local_event["id"])

        self.assertEqual(updated["title"], "OMT writable updated")
        self.assertEqual(deleted["lifecycle_status"], "deleted")
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0]["etag"], None)
        self.assertIsNotNone(writes[2]["etag"])
        self.assertEqual(len(deletes), 1)
        with main.db() as connection:
            rows = connection.execute("SELECT id, sync_status, external_calendar_ref, calendar_sync_enabled FROM events WHERE id IN (?, ?)", (local_event["id"], ai_event["id"])).fetchall()
        by_id = {row["id"]: row for row in rows}
        self.assertEqual((by_id[ai_event["id"]]["sync_status"], bool(by_id[ai_event["id"]]["external_calendar_ref"]), bool(by_id[ai_event["id"]]["calendar_sync_enabled"])), ("synced", True, True))
        self.assertEqual((by_id[local_event["id"]]["sync_status"], by_id[local_event["id"]]["external_calendar_ref"], bool(by_id[local_event["id"]]["calendar_sync_enabled"])), ("synced", None, False))

    def test_writable_calendar_failure_keeps_local_edit_and_retryable_error(self):
        main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://failure.example/dav/", "calendars": [{"remote_url": "https://failure.example/cal/", "display_name": "Writable", "timezone": "Asia/Taipei", "read_only": False}]},
            "student",
            "secret",
        )
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="Before failure", date_label="2026-10-08", time_label="09:00-10:00"))
        main.update_event(request, event["id"], main.EventUpdate(title="Local edit kept"))

        with patch.object(main, "caldav_put_event", side_effect=main.CalendarConnectionError("provider unavailable")):
            main.sync_local_event_to_default_calendar("demo-user", event["id"])

        with main.db() as connection:
            saved = connection.execute("SELECT title, sync_status, sync_conflict_json FROM events WHERE id = ?", (event["id"],)).fetchone()
        self.assertEqual(saved["title"], "Local edit kept")
        self.assertEqual(saved["sync_status"], "error")
        self.assertTrue(json.loads(saved["sync_conflict_json"])["retryable"])

    def test_real_writable_caldav_http_adapter_round_trip_for_local_and_ai_events(self):
        with WritableCalDavFixture() as fixture:
            user_id = f"http-fixture-{uuid4().hex}"
            timestamp = main.now_iso()
            with main.db() as connection:
                connection.execute("INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, ?)", (user_id, "HTTP fixture", timestamp, timestamp))
            main.save_nextcloud_source(
                user_id,
                {"server_url": fixture.calendar_url, "calendars": [{"remote_url": fixture.calendar_url, "display_name": "Local fixture", "timezone": "Asia/Taipei", "read_only": False}]},
                "student",
                "secret",
            )
            response = Response()
            main.create_session(user_id, response)
            request = self._request(response, "POST", "/api/events")
            local_event = main.create_event(request, main.EventCreate(title="HTTP OMT", date_label="2026-10-09", time_label="09:00-10:00"))
            ai_event = main.create_event(request, main.EventCreate(title="HTTP AI", date_label="2026-10-09", time_label="11:00-12:00"))
            with main.db() as connection:
                connection.execute("UPDATE events SET created_source = 'ai_generated', sync_status = 'pending' WHERE id = ?", (ai_event["id"],))

            main.sync_local_event_to_default_calendar(user_id, local_event["id"])
            main.sync_local_event_to_default_calendar(user_id, ai_event["id"])

            unified = main.list_events(request, start="2026-10-09", end="2026-10-10")["events"]
            for title, local_id in (("HTTP OMT", local_event["id"]), ("HTTP AI", ai_event["id"])):
                matching = [item for item in unified if item["title"] == title]
                self.assertEqual(len(matching), 1, unified)
                self.assertEqual(matching[0]["id"], local_id)

            main.update_event(request, local_event["id"], main.EventUpdate(title="HTTP OMT edited"))
            main.sync_local_event_to_default_calendar(user_id, local_event["id"])
            main.delete_event(request, local_event["id"])
            main.sync_local_event_to_default_calendar(user_id, local_event["id"])

            methods = [item[0] for item in fixture.history]
            self.assertEqual(methods, ["PUT", "REPORT", "PUT", "REPORT", "GET", "PUT", "DELETE"])
            written_bodies = [item[2] for item in fixture.history if item[0] == "PUT"]
            self.assertIn("SUMMARY:HTTP OMT edited", written_bodies[-1])
            self.assertEqual(len(fixture.resources), 1, fixture.history)
            with main.db() as connection:
                rows = connection.execute("SELECT id, sync_status, external_calendar_ref, calendar_sync_enabled FROM events WHERE id IN (?, ?)", (local_event["id"], ai_event["id"])).fetchall()
            by_id = {row["id"]: row for row in rows}
            self.assertEqual((by_id[ai_event["id"]]["sync_status"], bool(by_id[ai_event["id"]]["external_calendar_ref"]), bool(by_id[ai_event["id"]]["calendar_sync_enabled"])), ("synced", True, True))
            self.assertEqual((by_id[local_event["id"]]["sync_status"], by_id[local_event["id"]]["external_calendar_ref"], bool(by_id[local_event["id"]]["calendar_sync_enabled"])), ("synced", None, False))

    def test_batch_calendar_sync_writes_provider_events_concurrently_and_pulls_once(self):
        user_id = f"batch-calendar-{uuid4().hex}"
        timestamp = main.now_iso()
        with main.db() as connection:
            connection.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, ?)",
                (user_id, "Batch calendar", timestamp, timestamp),
            )
        main.save_nextcloud_source(
            user_id,
            {"server_url": "https://batch.example/dav/", "calendars": [{"remote_url": "https://batch.example/cal/", "display_name": "Writable", "timezone": "Asia/Taipei", "read_only": False}]},
            "student",
            "secret",
        )
        response = Response()
        main.create_session(user_id, response)
        request = self._request(response, "POST", "/api/events")
        first = main.create_event(request, main.EventCreate(title="Batch one", date_label="2026-10-10", time_label="09:00-10:00"))
        second = main.create_event(request, main.EventCreate(title="Batch two", date_label="2026-10-10", time_label="11:00-12:00"))
        writes: list[dict[str, str]] = []
        write_barrier = threading.Barrier(2, timeout=3)
        report_calls = 0

        def fake_put(url, username, password, icalendar, *, etag=None):
            writes.append({"url": url, "icalendar": icalendar})
            write_barrier.wait()
            return None

        def fake_report(url, username, password):
            nonlocal report_calls
            report_calls += 1
            return [{"href": item["url"], "etag": f'"batch-{index}"', "calendar_data": item["icalendar"]} for index, item in enumerate(writes, start=1)]

        with patch.object(main, "caldav_put_event", side_effect=fake_put), patch.object(main, "caldav_calendar_report", side_effect=fake_report):
            main.sync_local_events_to_default_calendar(user_id, [first["id"], second["id"]])

        self.assertEqual(len(writes), 2)
        self.assertEqual(report_calls, 1)
        with main.db() as connection:
            rows = connection.execute("SELECT sync_status, external_calendar_ref FROM events WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        self.assertEqual([row["sync_status"] for row in rows], ["synced", "synced"])
        self.assertTrue(all(row["external_calendar_ref"] for row in rows))

    def test_real_writable_caldav_http_failure_keeps_local_edit_and_retryable_error(self):
        with WritableCalDavFixture() as fixture:
            user_id = f"http-failure-{uuid4().hex}"
            timestamp = main.now_iso()
            with main.db() as connection:
                connection.execute("INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, ?)", (user_id, "HTTP failure fixture", timestamp, timestamp))
            main.save_nextcloud_source(
                user_id,
                {"server_url": fixture.calendar_url, "calendars": [{"remote_url": fixture.calendar_url, "display_name": "Failing fixture", "timezone": "Asia/Taipei", "read_only": False}]},
                "student",
                "secret",
            )
            response = Response()
            main.create_session(user_id, response)
            request = self._request(response, "POST", "/api/events")
            event = main.create_event(request, main.EventCreate(title="HTTP before failure", date_label="2026-10-10", time_label="09:00-10:00"))
            main.update_event(request, event["id"], main.EventUpdate(title="HTTP local edit kept"))
            fixture.fail_put = True

            main.sync_local_event_to_default_calendar(user_id, event["id"])

            with main.db() as connection:
                row = connection.execute("SELECT title, sync_status, sync_conflict_json FROM events WHERE id = ?", (event["id"],)).fetchone()
            self.assertEqual(row["title"], "HTTP local edit kept")
            self.assertEqual(row["sync_status"], "error")
            self.assertTrue(json.loads(row["sync_conflict_json"])["retryable"])

    def test_local_event_sync_marks_disconnected_calendar_as_error(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://sync.example/dav/", "calendars": [{"remote_url": "https://sync.example/cal/", "display_name": "Writable", "timezone": "Asia/Taipei", "read_only": False}]},
            "student",
            "secret",
        )
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="Pending event", date_label="2026-10-05", time_label="10:00"))
        main._set_calendar_connection_state(saved["source"]["id"], status=main.CalendarConnectionStatus.EXPIRED.value)
        with main.db() as connection:
            state = main._connection_for_source(connection, saved["source"]["id"])
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], main.CalendarConnectionStatus.EXPIRED.value)

        with patch.object(main, "_connection_for_source", return_value=state):
            main.sync_local_event_to_default_calendar("demo-user", event["id"])

        with main.db() as connection:
            row = connection.execute("SELECT sync_status, sync_conflict_json FROM events WHERE id = ?", (event["id"],)).fetchone()
        self.assertEqual(row["sync_status"], "error")
        self.assertIn("expired", row["sync_conflict_json"])
        self.assertIn('"retryable": true', row["sync_conflict_json"])

    def test_discard_removes_only_new_unapplied_capture_drafts(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/notices/discard")
        timestamp = main.now_iso()
        draft_id = f"notice-draft-{uuid4().hex}"
        applied_id = f"notice-applied-{uuid4().hex}"
        existing_id = f"notice-existing-{uuid4().hex}"
        with main.db() as connection:
            for notice_id, processing in (
                (draft_id, {"capture_state": "review_draft"}),
                (applied_id, {"capture_state": "review_draft"}),
                (existing_id, {}),
            ):
                connection.execute(
                    "INSERT INTO notices(id, user_id, title, body, audience, created_at, source_type, confidence, attachments_json, processing_json) VALUES (?, 'demo-user', ?, '', 'me', ?, 'manual', 'provider', '[]', ?)",
                    (notice_id, notice_id, timestamp, json.dumps(processing)),
                )
            connection.execute(
                """INSERT INTO proposals(id, user_id, source_id, operation, target_type, target_id, patch_json, evidence_refs_json, target_candidates_json, needs_review, review_reason, confidence, status, error, created_at, updated_at, applied_at)
                   VALUES (?, 'demo-user', ?, 'create', 'task', NULL, '{}', '[]', '[]', 0, NULL, 'high', 'pending', NULL, ?, ?, NULL)""",
                (f"proposal-{uuid4().hex}", draft_id, timestamp, timestamp),
            )
            connection.execute(
                """INSERT INTO proposals(id, user_id, source_id, operation, target_type, target_id, patch_json, evidence_refs_json, target_candidates_json, needs_review, review_reason, confidence, status, error, created_at, updated_at, applied_at)
                   VALUES (?, 'demo-user', ?, 'create', 'task', NULL, '{}', '[]', '[]', 0, NULL, 'high', 'applied', NULL, ?, ?, ?)""",
                (f"proposal-{uuid4().hex}", applied_id, timestamp, timestamp, timestamp),
            )

        self.assertEqual(main.discard_capture(request, draft_id), {"ok": True, "discarded": True})
        with self.assertRaises(main.HTTPException) as applied:
            main.discard_capture(request, applied_id)
        self.assertEqual(applied.exception.status_code, 409)
        with self.assertRaises(main.HTTPException) as existing:
            main.discard_capture(request, existing_id)
        self.assertEqual(existing.exception.status_code, 409)
        with main.db() as connection:
            self.assertIsNone(connection.execute("SELECT id FROM notices WHERE id = ?", (draft_id,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT id FROM notices WHERE id = ?", (applied_id,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT id FROM notices WHERE id = ?", (existing_id,)).fetchone())
    def test_personal_list_migration_adds_schema_without_rewriting_legacy_tasks(self):
        with tempfile.TemporaryDirectory() as migration_dir:
            legacy_path = os.path.join(migration_dir, "legacy.db")
            legacy_task = ("legacy-task", "legacy-user", "Keep this task", "Tomorrow", None, "open", "manual", "student")
            connection = sqlite3.connect(legacy_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE users (
                        id TEXT PRIMARY KEY, display_name TEXT NOT NULL, email TEXT, avatar_url TEXT,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                        due_label TEXT NOT NULL, due_iso TEXT, status TEXT NOT NULL,
                        source_id TEXT NOT NULL, owner TEXT NOT NULL
                    );
                    CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                    PRAGMA user_version = 13;
                    """
                )
                connection.execute(
                    "INSERT INTO users VALUES (?, ?, NULL, NULL, ?, ?)",
                    ("legacy-user", "Legacy", main.now_iso(), main.now_iso()),
                )
                connection.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", legacy_task)
                connection.commit()
            finally:
                connection.close()
            with patch.object(main, "DB_PATH", Path(legacy_path)), patch.object(main, "DATA_DIR", Path(migration_dir)):
                main.init_db()
            connection = sqlite3.connect(legacy_path)
            try:
                migrated_task = connection.execute(
                    "SELECT id, user_id, title, due_label, due_iso, status, source_id, owner, list_id FROM tasks"
                ).fetchone()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                list_table = connection.execute("SELECT name FROM sqlite_master WHERE name = 'personal_lists'").fetchone()
            finally:
                connection.close()
            self.assertEqual(migrated_task, legacy_task + (None,))
            self.assertEqual(version, 18)
            self.assertIsNotNone(list_table)

    def test_calendar_month_range_and_event_detail_keep_remote_identity(self):
        discovered = {
            "server_url": "https://calendar-range.example/remote.php/dav/",
            "calendars": [{"remote_url": "https://calendar-range.example/cal/", "display_name": "Range", "timezone": "Asia/Taipei", "read_only": True}],
        }
        saved = main.save_nextcloud_source("demo-user", discovered, "range-user", "range-secret")
        calendar_id = saved["calendars"][0]["id"]
        report = [{
            "href": "https://calendar-range.example/cal/event.ics",
            "etag": '"range-v1"',
            "calendar_data": "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:range-event\r\nDTSTART;TZID=Asia/Taipei:20260919T090000\r\nDTEND;TZID=Asia/Taipei:20260919T100000\r\nSUMMARY:範圍事件\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        }]
        with patch.object(main, "caldav_calendar_report", return_value=report):
            main.sync_calendar_source("demo-user", saved["source"]["id"])
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "GET", "/api/calendar/events")
        month = main.calendar_events(request, scope="all", calendar_id=calendar_id, timezone_name="Asia/Taipei", month="2026-09")
        self.assertEqual(month["meta"]["scope"], "range")
        self.assertEqual(month["events"][0]["remote_id"], "range-event")
        first_page = main.calendar_events(
            request,
            scope="all",
            calendar_id=calendar_id,
            timezone_name="Asia/Taipei",
            month="2026-09",
            limit=1,
            offset=0,
        )
        self.assertEqual(first_page["meta"]["count"], 1)
        self.assertEqual(first_page["meta"]["total_count"], 1)
        self.assertIsNone(first_page["meta"]["next_offset"])
        detail = main.get_calendar_event(request, month["events"][0]["id"])
        self.assertEqual(detail["calendar"]["timezone"], "Asia/Taipei")
        self.assertEqual(detail["source"]["id"], saved["source"]["id"])
        with self.assertRaises(main.HTTPException) as readonly:
            main.create_calendar_event(
                request,
                main.CalendarEventInput(
                    source_id=saved["source"]["id"],
                    calendar_id=calendar_id,
                    title="不應寫入",
                    start_at="2026-09-19T11:00:00+08:00",
                    end_at="2026-09-19T12:00:00+08:00",
                    timezone="Asia/Taipei",
                ),
            )
        self.assertEqual(readonly.exception.status_code, 403)
        unified = main.list_events(request, start="2026-09-19", end="2026-09-20")
        self.assertTrue(any(item.get("remote_id") == "range-event" for item in unified["events"]))

    def test_calendar_event_pagination_reports_complete_month_metadata(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://pagination.example/dav/", "calendars": [{"remote_url": "https://pagination.example/cal/", "display_name": "Pagination", "timezone": "Asia/Taipei", "read_only": True}]},
            "pagination-user",
            "pagination-secret",
        )
        with main.db() as connection:
            calendar = connection.execute("SELECT * FROM calendar_calendars WHERE id = ?", (saved["calendars"][0]["id"],)).fetchone()
        reports = [{
            "href": f"https://pagination.example/cal/event-{index}.ics",
            "etag": f'"v{index}"',
            "calendar_data": f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:pagination-{index}\r\nDTSTART;TZID=Asia/Taipei:20260915T090000\r\nDTEND;TZID=Asia/Taipei:20260915T100000\r\nSUMMARY:Pagination {index}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        } for index in range(205)]
        main.persist_calendar_report(saved["source"]["id"], calendar, reports, main.now_iso())
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "GET", "/api/calendar/events")
        first = main.calendar_events(request, scope="all", calendar_id=calendar["id"], month="2026-09", limit=100, offset=0)
        second = main.calendar_events(request, scope="all", calendar_id=calendar["id"], month="2026-09", limit=100, offset=first["meta"]["next_offset"])
        last = main.calendar_events(request, scope="all", calendar_id=calendar["id"], month="2026-09", limit=100, offset=second["meta"]["next_offset"])
        self.assertEqual(first["meta"]["total_count"], 205)
        self.assertEqual(first["meta"]["next_offset"], 100)
        self.assertEqual(second["meta"]["next_offset"], 200)
        self.assertEqual(last["meta"]["next_offset"], None)
        self.assertEqual(len(first["events"] + second["events"] + last["events"]), 205)

    def test_local_delete_persists_and_read_only_provider_event_rejects_mutation(self):
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="Delete lifecycle fixture", date_label="2026-09-25", time_label="10:00-11:00"))
        deleted = main.delete_event(request, event["id"])
        self.assertEqual(deleted["lifecycle_status"], "deleted")

        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://delete-lifecycle.example/dav/", "calendars": [{"remote_url": "https://delete-lifecycle.example/cal/", "display_name": "Readonly", "timezone": "Asia/Taipei", "read_only": True}]},
            "delete-user", "delete-secret",
        )
        calendar = saved["calendars"][0]
        raw = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:delete-external\r\nDTSTART;TZID=Asia/Taipei:20260925T120000\r\nDTEND;TZID=Asia/Taipei:20260925T130000\r\nSUMMARY:Read only fixture\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        with main.db() as connection:
            calendar_row = connection.execute("SELECT * FROM calendar_calendars WHERE id=?", (calendar["id"],)).fetchone()
        main.persist_calendar_report(saved["source"]["id"], calendar_row, [{"href":"https://delete-lifecycle.example/cal/external.ics", "etag":'"v1"', "calendar_data":raw}], main.now_iso())
        external_id = main.calendar_events(request, scope="all", calendar_id=calendar["id"])["events"][0]["id"]
        with self.assertRaises(main.HTTPException) as edit_error:
            main.update_event(request, external_id, main.EventUpdate(title="Nope"))
        self.assertEqual(edit_error.exception.status_code, 403)
        with self.assertRaises(main.HTTPException) as delete_error:
            main.delete_event(request, external_id)
        self.assertEqual(delete_error.exception.status_code, 403)

    def test_local_event_create_and_update_reject_invalid_calendar_dates(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        with self.assertRaises(main.HTTPException) as invalid_create:
            main.create_event(request, main.EventCreate(title="Invalid date", date_label="2026-09-31"))
        self.assertEqual(invalid_create.exception.status_code, 422)
        event = main.create_event(request, main.EventCreate(title="Valid date", date_label="2026-09-30"))
        with self.assertRaises(main.HTTPException) as invalid_update:
            main.update_event(request, event["id"], main.EventUpdate(date_label="9/31"))
        self.assertEqual(invalid_update.exception.status_code, 422)
        with main.db() as connection:
            saved = connection.execute("SELECT date_label FROM events WHERE id = ?", (event["id"],)).fetchone()
        self.assertEqual(saved["date_label"], "2026-09-30")

    def test_local_event_create_and_update_validate_numeric_time_ranges(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        for label in ("All day", "全天", "整天", "待確認", "7:30"):
            with self.subTest(supported_label=label):
                main.validate_local_event_time(label)
        for label in ("24:00", "12:60", "100:00", "09:00-08:59", "08:00～08:00", "10:15-11"):
            with self.subTest(label=label), self.assertRaises(main.HTTPException) as invalid_create:
                main.create_event(request, main.EventCreate(title="Invalid time", date_label="2026-09-30", time_label=label))
            self.assertEqual(invalid_create.exception.status_code, 422)

        event = main.create_event(request, main.EventCreate(title="Unknown time", date_label="2026-09-30", time_label="待確認"))
        self.assertEqual(event["time_label"], "待確認")
        updated = main.update_event(request, event["id"], main.EventUpdate(time_label="07:30～08:30"))
        self.assertEqual(updated["time_label"], "07:30～08:30")
        updated = main.update_event(request, event["id"], main.EventUpdate(time_label="全天"))
        self.assertEqual(updated["time_label"], "全天")
        with self.assertRaises(main.HTTPException) as invalid_update:
            main.update_event(request, event["id"], main.EventUpdate(time_label="25:00-26:00"))
        self.assertEqual(invalid_update.exception.status_code, 422)
        with main.db() as connection:
            saved = connection.execute("SELECT time_label FROM events WHERE id = ?", (event["id"],)).fetchone()
        self.assertEqual(saved["time_label"], "全天")

    def test_calendar_sync_rejects_unresolved_legacy_time_labels(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="未確認時間", date_label="2026-09-30", time_label="待確認"))
        with main.db() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event["id"],)).fetchone()
        with self.assertRaises(main.HTTPException) as unresolved:
            main.local_event_calendar_input(row, "source", "calendar")
        self.assertEqual(unresolved.exception.status_code, 422)

        start_only = main.create_event(request, main.EventCreate(title="Start only", date_label="2026-09-30", time_label="07:30"))
        with main.db() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (start_only["id"],)).fetchone()
        projected = main.local_event_calendar_input(row, "source", "calendar")
        self.assertEqual(datetime.fromisoformat(projected.start_at).strftime("%Y-%m-%d %H:%M"), "2026-09-30 07:30")
        self.assertIsNone(projected.end_at)

    def test_external_event_uses_only_calendar_native_fields(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://mirror.example/dav/", "calendars": [{"remote_url": "https://mirror.example/cal/", "display_name": "School", "timezone": "Asia/Taipei", "read_only": False}]},
            "student", "secret",
        )
        calendar = saved["calendars"][0]
        raw = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:math-test\r\nDTSTART;TZID=Asia/Taipei:20260924T090000\r\nDTEND;TZID=Asia/Taipei:20260924T100000\r\nSUMMARY:數學小考\r\nLOCATION:203教室\r\nDESCRIPTION:原本備註\r\nATTENDEE:mailto:teacher@example.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        with main.db() as connection:
            calendar_row = connection.execute("SELECT * FROM calendar_calendars WHERE id = ?", (calendar["id"],)).fetchone()
        main.persist_calendar_report(saved["source"]["id"], calendar_row, [{"href": "https://mirror.example/cal/math.ics", "etag": '"v1"', "calendar_data": raw}], main.now_iso())
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "PATCH", "/api/events")
        event_id = main.calendar_events(request, scope="all", calendar_id=calendar["id"])["events"][0]["id"]
        with patch.object(main, "caldav_get_event", return_value=(raw, '"v1"')), patch.object(main, "caldav_put_event", return_value='"v2"') as put:
            updated = main.update_event(request, event_id, main.EventUpdate(detail="更新後備註", location="204教室"))
        self.assertEqual(updated["description"], "更新後備註")
        self.assertEqual(updated["location"], "204教室")
        written = put.call_args.args[3]
        self.assertIn("DESCRIPTION:更新後備註", written)
        self.assertIn("LOCATION:204教室", written)
        self.assertIn("ATTENDEE:mailto:teacher@example.com", written)
        with main.db() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_events)")}
        self.assertFalse({"raw_ical", "raw_hash", "provider_snapshot_json", "omt_notes", "ai_summary", "checklist_json", "tags_json"} & columns)

    def test_external_event_patch_preserves_unmanaged_provider_properties(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://writeback.example/dav/", "calendars": [{"remote_url": "https://writeback.example/cal/", "display_name": "Writable", "timezone": "Asia/Taipei", "read_only": False}]},
            "student", "secret",
        )
        calendar = saved["calendars"][0]
        raw = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:writeback-test\r\nDTSTART;TZID=Asia/Taipei:20260924T090000\r\nDTEND;TZID=Asia/Taipei:20260924T100000\r\nSUMMARY:原始標題\r\nLOCATION:203教室\r\nATTENDEE:mailto:teacher@example.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        with main.db() as connection:
            calendar_row = connection.execute("SELECT * FROM calendar_calendars WHERE id = ?", (calendar["id"],)).fetchone()
        main.persist_calendar_report(saved["source"]["id"], calendar_row, [{"href": "https://writeback.example/cal/writeback.ics", "etag": '"v1"', "calendar_data": raw}], main.now_iso())
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "PATCH", "/api/events")
        event_id = main.calendar_events(request, scope="all", calendar_id=calendar["id"])["events"][0]["id"]
        with patch.object(main, "caldav_get_event", return_value=(raw, '"v1"')), patch.object(main, "caldav_put_event", return_value='"v2"') as put:
            updated = main.update_event(request, event_id, main.EventUpdate(detail="帶計算紙"))
        written = put.call_args.args[3]
        self.assertIn("SUMMARY:原始標題", written)
        self.assertIn("DESCRIPTION:帶計算紙", written)
        self.assertIn("ATTENDEE:mailto:teacher@example.com", written)
        self.assertNotIn("One More Thing", written)
        self.assertEqual(updated["title"], "原始標題")

    def test_local_event_can_be_explicitly_synced_to_nextcloud(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://local-sync.example/dav/", "calendars": [{"remote_url": "https://local-sync.example/cal/", "display_name": "Writable", "timezone": "Asia/Taipei", "read_only": False}]},
            "student",
            "secret",
        )
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="我的複習", date_label="2026-09-25", time_label="19:00-20:00", detail="完成題庫"))
        captured = {}
        def capture_put(_url, _username, _password, icalendar, **_kwargs):
            captured["remote_id"] = next(line[4:] for line in icalendar.splitlines() if line.startswith("UID:"))
        def report_after_create(_url, _username, _password):
            remote_id = captured["remote_id"]
            return [{"href": "https://local-sync.example/cal/remote.ics", "etag": '"v1"', "calendar_data": f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:{remote_id}\r\nDTSTART;TZID=Asia/Taipei:20260925T190000\r\nDTEND;TZID=Asia/Taipei:20260925T200000\r\nSUMMARY:我的複習\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"}]
        with patch.object(main, "caldav_put_event", side_effect=capture_put), patch.object(main, "caldav_calendar_report", side_effect=report_after_create):
            result = main.sync_local_event_to_calendar(request, event["id"], main.LocalEventCalendarSyncInput(source_id=saved["source"]["id"], calendar_id=saved["calendars"][0]["id"]))
        self.assertEqual(result["status"], "synced")
        self.assertTrue(result["event"]["calendar_sync_enabled"])
        self.assertEqual(result["event"]["external_calendar_ref"], captured["remote_id"])

        # Pulling the provider copy back into OMT must reconcile with the local
        # Event instead of surfacing a second calendar row (sync echo).
        unified = main.list_events(request)["events"]
        matching = [item for item in unified if item["title"] == "我的複習"]
        self.assertEqual(len(matching), 1, matching)
        self.assertEqual(matching[0]["id"], event["id"])
        self.assertEqual(matching[0]["external_calendar_ref"], captured["remote_id"])

    def test_provider_pull_reconciles_linked_local_event_and_remote_delete_unlinks_task(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://two-way.example/dav/", "calendars": [{"remote_url": "https://two-way.example/cal/", "display_name": "Writable", "timezone": "Asia/Taipei", "read_only": False}]},
            "student", "secret",
        )
        calendar = saved["calendars"][0]
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="原本標題", date_label="2026-09-25", time_label="09:00～10:00", detail="舊備註", location="舊地點"))
        remote_id = "two-way-remote"
        with main.db() as connection:
            connection.execute(
                "UPDATE events SET external_calendar_ref=?, calendar_sync_enabled=1, provider=?, sync_status='synced' WHERE id=?",
                (remote_id, main.CalendarProvider.NEXTCLOUD.value, event["id"]),
            )
        task = main.create_task(
            request,
            main.TaskCreate(title="人工連結待辦", related_event_id=event["id"]),
        )
        with main.db() as connection:
            calendar_row = connection.execute("SELECT * FROM calendar_calendars WHERE id=?", (calendar["id"],)).fetchone()
        changed = [{
            "href": "https://two-way.example/cal/two-way.ics",
            "etag": '"v2"',
            "calendar_data": "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:two-way-remote\r\nDTSTART;TZID=Asia/Taipei:20260925T133000\r\nSUMMARY:遠端新標題\r\nLOCATION:新地點\r\nDESCRIPTION:遠端新備註\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        }]
        main.persist_calendar_report(saved["source"]["id"], calendar_row, changed, main.now_iso())
        with main.db() as connection:
            local = connection.execute("SELECT title, start_at, end_at, detail, location, lifecycle_status FROM events WHERE id=?", (event["id"],)).fetchone()
            linked = connection.execute("SELECT related_event_id FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(local["title"], "遠端新標題")
        self.assertEqual(local["start_at"], "2026-09-25T13:30:00+08:00")
        self.assertIsNone(local["end_at"])
        self.assertEqual(local["detail"], "遠端新備註")
        self.assertEqual(local["location"], "新地點")
        self.assertEqual(local["lifecycle_status"], "created")
        self.assertEqual(linked["related_event_id"], event["id"])

        main.persist_calendar_report(saved["source"]["id"], calendar_row, [], main.now_iso())
        with main.db() as connection:
            local = connection.execute("SELECT lifecycle_status FROM events WHERE id=?", (event["id"],)).fetchone()
            linked = connection.execute("SELECT related_event_id FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(local["lifecycle_status"], "deleted")
        self.assertIsNone(linked["related_event_id"])

    def test_task_event_manual_index_is_bidirectional_but_event_delete_unlinks_it(self):
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        event = main.create_event(request, main.EventCreate(title="社團會議", date_label="2026-10-02", time_label="17:00"))
        task = main.create_task(request, main.TaskCreate(title="帶社員名單", related_event_id=event["id"]))
        indexed = main.list_tasks_for_event(request, event["id"])["tasks"]
        self.assertEqual([item["id"] for item in indexed], [task["id"]])
        updated = main.update_task(request, task["id"], main.TaskUpdate(related_event_id=None))
        self.assertIsNone(updated["related_event_id"])
        self.assertEqual(main.list_tasks_for_event(request, event["id"])["tasks"], [])
        relinked = main.update_task(request, task["id"], main.TaskUpdate(related_event_id=event["id"]))
        self.assertEqual(relinked["related_event_id"], event["id"])
        main.delete_event(request, event["id"])
        with main.db() as connection:
            related = connection.execute("SELECT related_event_id FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
        self.assertIsNone(related)

    def test_task_standalone_schedule_round_trips_all_day_and_timed_ranges(self):
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/tasks")
        task = main.create_task(
            request,
            main.TaskCreate(
                title="買兩條飯圈",
                start_at="2026-10-01T21:00:00+08:00",
                end_at="2026-10-01T21:30:00+08:00",
                all_day=False,
                timezone="Asia/Taipei",
            ),
        )
        self.assertEqual(task["start_at"], "2026-10-01T21:00:00+08:00")
        self.assertEqual(task["end_at"], "2026-10-01T21:30:00+08:00")
        self.assertFalse(task["all_day"])
        all_day = main.update_task(
            request,
            task["id"],
            main.TaskUpdate(start_at="2026-10-02", end_at=None, all_day=True, timezone="Asia/Taipei"),
        )
        self.assertEqual(all_day["start_at"], "2026-10-02")
        self.assertIsNone(all_day["end_at"] )
        self.assertTrue(all_day["all_day"])
        with self.assertRaises(main.HTTPException) as invalid:
            main.update_task(
                request,
                task["id"],
                main.TaskUpdate(
                    start_at="2026-10-03T20:00:00+08:00",
                    end_at="2026-10-03T19:00:00+08:00",
                    all_day=False,
                ),
            )
        self.assertEqual(invalid.exception.status_code, 422)

    def test_task_event_link_selector_is_bounded_and_searchable(self):
        response = Response(); main.create_session("demo-user", response)
        request = self._request(response, "POST", "/api/events")
        for index in range(10):
            day = 1 + index
            main.create_event(
                request,
                main.EventCreate(
                    title=f"課程 {index}",
                    date_label=f"2026-10-{day:02d}",
                    time_label="14:20-15:10",
                    detail="",
                    location="實習工場" if index == 4 else "教室",
                ),
            )
        result = main.search_linkable_events(request, q=None, around="2026-10-05", limit=5)
        self.assertEqual(len(result["events"]), 5)
        self.assertEqual(result["meta"]["limit"], 5)
        searched = main.search_linkable_events(request, q="實習工場", around="2026-10-05", limit=12)
        self.assertEqual([item["title"] for item in searched["events"]], ["課程 4"] )
        by_date = main.search_linkable_events(request, q="10月5日 實習工場", around="2026-10-05", limit=12)
        self.assertIn("課程 4", [item["title"] for item in by_date["events"]])
        by_weekday = main.search_linkable_events(request, q="星期一 實習工場", around="2026-10-05", limit=12)
        self.assertIn("課程 4", [item["title"] for item in by_weekday["events"]])

    def test_provider_refresh_replaces_calendar_native_fields_without_omt_enrichment(self):
        saved = main.save_nextcloud_source(
            "demo-user",
            {"server_url": "https://conflict.example/dav/", "calendars": [{"remote_url": "https://conflict.example/cal/", "display_name": "School", "timezone": "UTC", "read_only": True}]},
            "student", "secret",
        )
        calendar = saved["calendars"][0]
        with main.db() as connection:
            calendar_row = connection.execute("SELECT * FROM calendar_calendars WHERE id = ?", (calendar["id"],)).fetchone()
        first = [{"href": "https://conflict.example/cal/item.ics", "etag": '"v1"', "calendar_data": "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:conflict-test\r\nDTSTART:20260925T090000Z\r\nDTEND:20260925T100000Z\r\nSUMMARY:第一次標題\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"}]
        second = [{"href": "https://conflict.example/cal/item.ics", "etag": '"v2"', "calendar_data": "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:conflict-test\r\nDTSTART:20260925T110000Z\r\nDTEND:20260925T120000Z\r\nSUMMARY:第二次標題\r\nDESCRIPTION:Provider note\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"}]
        main.persist_calendar_report(saved["source"]["id"], calendar_row, first, main.now_iso())
        main.persist_calendar_report(saved["source"]["id"], calendar_row, second, main.now_iso())
        with main.db() as connection:
            row = connection.execute("SELECT title, start_at, description, sync_conflict_json FROM calendar_events WHERE remote_id='conflict-test'").fetchone()
        self.assertEqual(row["title"], "第二次標題")
        self.assertEqual(row["start_at"], "2026-09-25T11:00:00+00:00")
        self.assertEqual(row["description"], "Provider note")
        self.assertEqual(json.loads(row["sync_conflict_json"]), {})

    def test_calendar_range_validation_returns_422(self):
        response = Response()
        main.create_session("demo-user", response)
        request = self._request(response, "GET", "/api/calendar/events")
        with self.assertRaises(main.HTTPException) as invalid:
            main.calendar_events(request, scope="all", range_start="not-a-date")
        self.assertEqual(invalid.exception.status_code, 422)
        with self.assertRaises(main.HTTPException) as invalid_rollover:
            main.calendar_events(request, scope="all", range_start="2026-09-31")
        self.assertEqual(invalid_rollover.exception.status_code, 422)

    def test_expired_login_flow_is_terminal_and_redacts_poll_token(self):
        response = Response()
        main.create_session("demo-user", response)
        flow_id = uuid4().hex
        with main.db() as connection:
            connection.execute(
                "INSERT INTO calendar_login_flows(id, user_id, base_url, poll_token_ciphertext, poll_endpoint, login_url, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    flow_id,
                    "demo-user",
                    "https://cloud.example",
                    main.encrypt_credential("poll-secret"),
                    "https://cloud.example/poll",
                    "https://cloud.example/login",
                    "pending",
                    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    main.now_iso(),
                    main.now_iso(),
                ),
            )
        result = main.poll_nextcloud_login(self._request_with_session(response), flow_id)
        self.assertEqual(result["status"], "error")
        with main.db() as connection:
            flow = connection.execute("SELECT status, poll_token_ciphertext FROM calendar_login_flows WHERE id = ?", (flow_id,)).fetchone()
        self.assertEqual(flow["status"], "expired")
        self.assertEqual(flow["poll_token_ciphertext"], "")


if __name__ == "__main__":
    unittest.main()
