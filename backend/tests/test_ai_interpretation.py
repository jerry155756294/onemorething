import os
import base64
import io
import json
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


DATA_DIR = tempfile.TemporaryDirectory()
os.environ["OMT_DATA_DIR"] = DATA_DIR.name
os.environ["OMT_ENVIRONMENT"] = "test"
os.environ["OMT_ENABLE_DEV_LOGIN"] = "1"
os.environ["OMT_DEFAULT_TIMEZONE"] = "Asia/Taipei"

from app import main  # noqa: E402
from app.ai_interpreter import (  # noqa: E402
    AIProviderMalformedOutput,
    AIProviderUnavailable,
    DeterministicFakeProvider,
    DATE_DISCOVERY_RESPONSE_FORMAT,
    LiteLLMProvider,
    MINIMAL_ADD_RESPONSE_FORMAT,
    MINIMAL_INTERPRET_OUTPUT_TOKENS,
    MINIMAL_UPDATE_RESPONSE_FORMAT,
    READ_ONLY_INTERPRET_TOOLS,
    _prompt,
    estimate_prompt_token_upper_bound,
    expand_minimal_output,
    validate_provider_output,
)


class AIInterpretationTests(unittest.TestCase):
    current_datetime = datetime.fromisoformat("2026-09-21T09:00:00+08:00")

    def setUp(self):
        self.user_id = f"ai-user-{self._testMethodName}"
        with main.db() as connection:
            connection.execute(
                "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.user_id, "AI Test User", None, None, main.now_iso(), main.now_iso()),
            )

    def _source(self, body):
        return main.NoticeInput(title="測試來源", body=body, audience="我的課程")

    def _request(self):
        from starlette.requests import Request

        return Request({"type": "http", "method": "POST", "path": "/api/proposals"})

    def _insert_event(self, event_id, title="體育", date_label=None):
        event_id = f"{self.user_id}-{event_id}"
        date_label = date_label or (datetime.now().date() + timedelta(days=1)).isoformat()
        with main.db() as connection:
            connection.execute(
                "INSERT INTO events(id, user_id, title, date_label, time_label, detail, source_id, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, self.user_id, title, date_label, "10:15–11:05", "", "manual", None),
            )
        return event_id

    def _interpret(self, body, output):
        with patch.object(main, "build_interpretation_context", wraps=main.build_interpretation_context) as context_builder:
            result = main.interpret_source(
                self.user_id,
                self._source(body),
                DeterministicFakeProvider(output),
            )
        self.assertTrue(context_builder.called)
        return result

    def test_existing_event_location_update(self):
        event_id = self._insert_event("event-sport")
        result = self._interpret(
            "各位明天體育課在攀岩場喔",
            {
                "proposals": [
                    {
                        "operation": "update",
                        "target_type": "event",
                        "target_id": event_id,
                        "patch": {"location": "攀岩場"},
                        "evidence_refs": ["各位明天體育課在攀岩場喔"],
                        "confidence": "high",
                    }
                ]
            },
        )
        proposal_id = result["proposals"][0]["id"]
        main.accept_proposal(self._request_with_user(), proposal_id)
        main.apply_proposal(self.user_id, proposal_id)
        with main.db() as connection:
            event = connection.execute("SELECT title, location FROM events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(dict(event), {"title": "體育", "location": "攀岩場"})

    def test_ai_cannot_create_task_event_relation(self):
        event_id = self._insert_event("manual-link-target", title="人工才能連結")
        proposals = validate_provider_output(
            {"proposals": [{
                "operation": "create",
                "target_type": "task",
                "patch": {"title": "準備資料", "due": None, "related_event_id": event_id},
                "evidence_refs": ["準備資料"],
                "confidence": "high",
            }]},
            {"current_datetime": self.current_datetime.isoformat(), "target_ids": {"event": [event_id], "task": []}},
            "準備資料",
        )
        self.assertEqual(len(proposals), 1)
        self.assertNotIn("related_event_id", proposals[0].patch.model_fields_set)
        self.assertIsNone(proposals[0].patch.related_event_id)

    def test_task_creation_without_due_date(self):
        result = self._interpret(
            "記得完成 U2 第1回小卷",
            {
                "proposals": [
                    {
                        "operation": "create",
                        "target_type": "task",
                        "patch": {"title": "完成 U2 第1回小卷", "due": None},
                        "evidence_refs": ["記得完成 U2 第1回小卷"],
                        "confidence": "high",
                    }
                ]
            },
        )
        proposal = result["proposals"][0]
        self.assertIsNone(proposal["patch"]["due"])
        main.accept_proposal(self._request_with_user(), proposal["id"])
        applied = main.apply_proposal(self.user_id, proposal["id"])
        self.assertEqual(applied["result"]["task_id"].startswith("task-"), True)
        with main.db() as connection:
            task = connection.execute("SELECT title, due_iso FROM tasks WHERE id = ?", (applied["result"]["task_id"],)).fetchone()
        self.assertEqual(dict(task), {"title": "完成 U2 第1回小卷", "due_iso": None})

    def test_task_defaults_are_optional_and_deduplicated(self):
        source = "\\u660e\\u5929\\u8cb7\\u897f\\u74dc\\u54c8\\u5bc6\\u74dc\\uff0c\\u4e0d\\u540clist"
        buy_watermelon = "".join(chr(code) for code in (36023, 35199, 29916))
        buy_melon = "".join(chr(code) for code in (36023, 21704, 23494, 29916))
        source = "".join(chr(code) for code in (26126, 22825, 36023, 35199, 29916, 21704, 23494, 29916, 65292, 19981, 21516)) + "list"
        raw = {
            "proposals": [
                {"operation": "create", "target_type": "task", "patch": {"title": "\\u8cb7\\u897f\\u74dc"}, "needs_review": True, "review_reason": "missing due"},
                {"operation": "create", "target_type": "task", "patch": {"title": "\\u8cb7\\u897f\\u74dc"}, "needs_review": True, "review_reason": "missing list"},
                {"operation": "create", "target_type": "task", "patch": {"title": "\\u8cb7\\u54c8\\u5bc6\\u74dc"}, "needs_review": True, "review_reason": "missing due"},
                {"operation": "create", "target_type": "list", "patch": {"title": "\\u8cfc\\u7269\\u6e05\\u55ae"}, "needs_review": True, "review_reason": "missing list"},
            ]
        }
        raw["proposals"][0]["patch"]["title"] = buy_watermelon
        raw["proposals"][1]["patch"]["title"] = buy_watermelon
        raw["proposals"][2]["patch"]["title"] = buy_melon
        proposals = validate_provider_output(raw, {"current_datetime": self.current_datetime.isoformat()}, source)
        self.assertEqual([item.target_type for item in proposals], ["task", "task"])
        self.assertEqual({item.patch.title for item in proposals}, {buy_watermelon, buy_melon})
        self.assertEqual({item.patch.due for item in proposals}, {"2026-09-22"})
        self.assertTrue(all(item.patch.list_id is None and not item.needs_review for item in proposals), [item.model_dump() for item in proposals])

    def test_create_for_similar_existing_event_adds_without_mutation(self):
        event_id = self._insert_event(
            "library-return",
            title="圖書館還書",
            date_label="2026-09-28",
        )
        with main.db() as connection:
            connection.execute(
                "UPDATE events SET time_label = ?, start_at = ?, end_at = ? WHERE id = ?",
                ("10:30～11:00", "2026-09-28T10:30:00+08:00", "2026-09-28T11:00:00+08:00", event_id),
            )

        result = self._interpret(
            "明天十點半去圖書館還書",
            {
                "proposals": [{
                    "operation": "create",
                    "target_type": "event",
                    "patch": {
                        "title": "圖書館還書",
                        "date": "2026-09-28",
                        "time": "10:30～11:00",
                    },
                    "evidence_refs": ["明天十點半去圖書館還書"],
                    "confidence": "high",
                }]
            },
        )
        proposal = result["proposals"][0]
        self.assertEqual(proposal["operation"], "create")
        main.accept_proposal(self._request_with_user(), proposal["id"])
        applied = main.apply_proposal(self.user_id, proposal["id"])

        self.assertNotEqual(applied["result"]["event_id"], event_id)
        with main.db() as connection:
            existing = connection.execute(
                "SELECT date_label, time_label, start_at, end_at FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT id FROM events WHERE user_id = ? AND title = ? AND date_label = ?",
                (self.user_id, "圖書館還書", "2026-09-28"),
            ).fetchall()
        self.assertEqual(
            dict(existing),
            {
                "date_label": "2026-09-28",
                "time_label": "10:30～11:00",
                "start_at": "2026-09-28T10:30:00+08:00",
                "end_at": "2026-09-28T11:00:00+08:00",
            },
        )
        self.assertEqual({row["id"] for row in events}, {event_id, applied["result"]["event_id"]})
        reloaded = main.list_events(
            self._request_with_user(),
            start="2026-09-28",
            end="2026-09-28",
        )
        self.assertEqual(
            {item["id"] for item in reloaded["events"]},
            {event_id, applied["result"]["event_id"]},
        )

    def _create_ai_event(self, suffix):
        result = self._interpret(
            "AI event",
            {
                "proposals": [
                    {
                        "operation": "create",
                        "target_type": "event",
                        "patch": {"title": f"AI event {suffix}", "date": "2026-09-24", "time": "09:00～10:00"},
                        "evidence_refs": ["AI event"],
                        "confidence": "high",
                    }
                ]
            },
        )
        proposal_id = result["proposals"][0]["id"]
        main.accept_proposal(self._request_with_user(), proposal_id)
        applied = main.apply_proposal(self.user_id, proposal_id)
        return applied["result"]["event_id"], proposal_id

    def test_event_apply_keeps_full_range_and_weekly_term(self):
        result = self._interpret("每週一 09:00 到 10:00，學期 9/28 至 10/12", {
            "proposals": [{
                "operation": "create",
                "target_type": "event",
                "patch": {
                    "title": "課程",
                    "date": "2026-09-30",
                    "start_at": "2026-09-30T09:00:00+08:00",
                    "end_at": "2026-09-30T10:00:00+08:00",
                    "recurrence": {"frequency": "weekly", "weekdays": [3], "start_date": "2026-09-30", "end_date": "2026-10-28"},
                },
                "evidence_refs": ["每週一 09:00 到 10:00"],
                "confidence": "high",
            }]
        })
        proposal_id = result["proposals"][0]["id"]
        main.accept_proposal(self._request_with_user(), proposal_id)
        applied = main.apply_proposal(self.user_id, proposal_id)
        event_id = applied["result"]["event_id"]
        with main.db() as connection:
            row = connection.execute("SELECT start_at, end_at, timezone, recurrence_json FROM events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["start_at"], "2026-09-30T09:00:00+08:00")
        self.assertEqual(row["end_at"], "2026-09-30T10:00:00+08:00")
        self.assertEqual(row["timezone"], "Asia/Taipei")
        self.assertEqual(json.loads(row["recurrence_json"])["end_date"], "2026-10-28")
        listed = main.list_events(self._request_with_user(), start="2026-09-30", end="2026-10-28")
        self.assertEqual([item["occurrence_date"] for item in listed["events"]], ["2026-09-30", "2026-10-07", "2026-10-14", "2026-10-21", "2026-10-28"])

    def test_prompt_sends_only_minimal_runtime_context(self):
        messages = _prompt(
            "明道中學線上課表查詢系統\n首頁\n課表查詢\n登入",
            {
                "current_datetime": "2026-10-03T11:00:00+08:00",
                "timezone": "Asia/Taipei",
                "source_type": "url",
                "source_url": "https://example.test/timetable",
                "nearby_calendar_events": [{"id": "must-not-leak"}],
                "relevant_open_tasks": [{"id": "must-not-leak"}],
            },
        )
        sent = json.loads(messages[1]["content"])
        self.assertEqual(set(sent), {"current_datetime", "timezone", "text"})
        self.assertEqual(sent["text"], "明道中學線上課表查詢系統\n首頁\n課表查詢\n登入")
        self.assertNotIn("must-not-leak", messages[1]["content"])
        self.assertIn('{"events":[{"title":"...","date":"YYYY-MM-DD"', messages[0]["content"])
        self.assertIn("search_calendar_events", messages[0]["content"])
        self.assertIn("search_tasks", messages[0]["content"])

    def test_minimal_prompt_does_not_expose_legacy_proposal_fields(self):
        guidance = _prompt("one event next week", {"current_datetime": self.current_datetime.isoformat(), "timezone": "Asia/Taipei"})[0]["content"]
        for legacy_field in ("evidence_refs", "confidence", "target_candidates", "needs_review"):
            self.assertNotIn(legacy_field, guidance)
        self.assertIn("不要輸出 evidence、null、false", guidance)

    def test_vague_one_time_event_drops_provider_inferred_recurrence(self):
        result = self._interpret("下週找一天跟老師聊一下報告，可能下午吧。", {
            "proposals": [{
                "operation": "create",
                "target_type": "event",
                "patch": {
                    "title": "跟老師聊報告",
                    "date": "2026-09-28",
                    "start_at": "2026-09-28T14:00:00+08:00",
                    "end_at": "2026-09-28T15:00:00+08:00",
                    "recurrence": {"frequency": "weekly", "weekdays": [4, 5, 6], "start_date": "2026-09-28", "end_date": "2026-10-12"},
                },
                "evidence_refs": ["每週四、週五、週六"],
                "confidence": "high",
            }]
        })
        proposal = result["proposals"][0]
        self.assertNotIn("recurrence", proposal["patch"])

        proposal_id = proposal["id"]
        main.accept_proposal(self._request_with_user(), proposal_id)
        applied = main.apply_proposal(self.user_id, proposal_id)
        with main.db() as connection:
            row = connection.execute("SELECT recurrence_json FROM events WHERE id = ?", (applied["result"]["event_id"],)).fetchone()
        self.assertIsNone(row["recurrence_json"])
        events = main.list_events(self._request_with_user(), start="2026-09-28", end="2026-10-12")["events"]
        self.assertEqual(len(events), 1)

    def test_single_model_routing_ignores_legacy_high_load_parameters(self):
        source = "short capture"
        context = {}
        estimate = estimate_prompt_token_upper_bound(_prompt(source, context))
        low_load = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_load_model="ministral-14b-latest", high_token_threshold=estimate + 1,
        )
        high_load = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_load_model="ministral-14b-latest", high_token_threshold=estimate - 1,
        )
        with patch.object(low_load, "_complete", return_value='{"events":[]}') as low_call:
            low_load.interpret(source, context)
        with patch.object(high_load, "_complete", return_value='{"events":[]}') as high_call:
            high_load.interpret(source, context)
        self.assertEqual(low_call.call_args.args[0], "mistral-small-latest")
        self.assertEqual(high_call.call_args.args[0], "mistral-small-latest")
        self.assertEqual(low_call.call_args.kwargs["max_tokens"], MINIMAL_INTERPRET_OUTPUT_TOKENS)
        self.assertEqual(high_call.call_args.kwargs["max_tokens"], MINIMAL_INTERPRET_OUTPUT_TOKENS)

    def test_environment_defaults_to_gpt_35_turbo_and_ignores_global_litellm_model(self):
        with patch.dict(os.environ, {"OMT_AI_API_KEY": "test-key", "LITELLM_MODEL": "legacy-global-model"}, clear=True):
            provider = LiteLLMProvider.from_environment()
        self.assertEqual(provider.model, "gpt-3.5-turbo")
        self.assertEqual(provider.high_load_model, "gpt-3.5-turbo")

    def test_chat_completion_preserves_provider_finish_reason(self):
        provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest")

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps({
                    "choices": [{
                        "message": {"role": "assistant", "content": '{"events":[]}'},
                        "finish_reason": "length",
                    }]
                }).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=Response()):
            content = provider._complete(
                provider.model,
                _prompt("課表", {"current_datetime": self.current_datetime.isoformat(), "timezone": "Asia/Taipei"}),
                response_format=MINIMAL_ADD_RESPONSE_FORMAT,
                max_tokens=4096,
            )
        self.assertEqual(str(content), '{"events":[]}')
        self.assertEqual(content.finish_reason, "length")

    def test_single_model_unavailable_fails_once_without_fallback(self):
        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_load_model="ministral-14b-latest", high_token_threshold=1,
        )
        with patch.object(provider, "_complete", side_effect=AIProviderUnavailable("Mistral unavailable")) as complete:
            with self.assertRaises(AIProviderUnavailable):
                provider.interpret("small", {})
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(complete.call_args.args[0], "mistral-small-latest")

    def test_compact_multilingual_capture_uses_mistral_small_with_4096_output_budget(self):
        source = "課" * 1354
        context = {"current_datetime": self.current_datetime.isoformat(), "timezone": "Asia/Taipei"}
        messages = _prompt(source, context)
        self.assertLess(estimate_prompt_token_upper_bound(messages), 6000)
        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_load_model="ministral-14b-latest", high_token_threshold=6000,
        )
        with patch.object(provider, "_complete", return_value='{"events":[]}') as complete:
            self.assertEqual(provider.interpret(source, context), [])
        complete.assert_called_once()
        self.assertEqual(complete.call_args.args[0], "mistral-small-latest")
        self.assertEqual(complete.call_args.kwargs["response_format"], MINIMAL_ADD_RESPONSE_FORMAT)
        self.assertEqual(complete.call_args.kwargs["max_tokens"], MINIMAL_INTERPRET_OUTPUT_TOKENS)

    def test_mistral_small_http_error_keeps_only_status_and_model_alias(self):
        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_load_model="ministral-14b-latest", high_token_threshold=1,
        )
        upstream_error = urllib.error.HTTPError(
            "http://127.0.0.1:4000/v1/chat/completions", 502, "Bad Gateway", None, io.BytesIO(b"private upstream body"),
        )
        with patch("urllib.request.urlopen", side_effect=upstream_error):
            with self.assertRaises(AIProviderUnavailable) as raised:
                provider.interpret("high load", {})
        self.assertEqual(raised.exception.model_alias, "mistral-small-latest")
        self.assertEqual(raised.exception.http_status, 502)
        self.assertNotIn("private upstream body", str(raised.exception))

    def test_mistral_small_invalid_json_is_classified_and_logged_without_capture_content(self):
        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_load_model="ministral-14b-latest", high_token_threshold=1,
        )
        with patch.object(provider, "_complete", return_value="not JSON"):
            with self.assertRaises(AIProviderMalformedOutput) as raised:
                provider.interpret("private capture text", {})
        self.assertEqual(raised.exception.model_alias, "mistral-small-latest")
        with patch.object(main, "interpret_source", side_effect=raised.exception):
            with patch.object(main.api_logger, "warning") as warning:
                response = main.interpret_endpoint(self._request_with_user(), main.NoticeInput(body="private capture text"))
        self.assertEqual(response.status_code, 422)
        payload = json.loads(response.body)
        self.assertEqual(payload["error"]["code"], "malformed_output")
        self.assertNotIn("private capture text", response.body.decode("utf-8"))
        warning.assert_called_once_with(
            "AI interpretation failed model=%s status=%s category=%s",
            "mistral-small-latest", 422, "malformed_output",
        )

    def test_invalid_strict_output_fails_fast_without_repair_round_trip(self):
        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_load_model="ministral-14b-latest", high_token_threshold=1,
        )
        with patch.object(provider, "_complete", return_value="not JSON") as complete:
            with self.assertRaises(AIProviderMalformedOutput):
                provider.interpret("private capture text", {})
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(complete.call_args.args[0], "mistral-small-latest")
        self.assertEqual(complete.call_args.kwargs["response_format"], MINIMAL_ADD_RESPONSE_FORMAT)

    def test_blank_event_creates_are_removed_but_substantive_proposals_remain(self):
        class DirectProvider:
            name = "test-direct"

            def interpret(self, _source, _context):
                from app.ai_interpreter import Proposal, ProposalPatch

                return [
                    Proposal(operation="create", target_type="event", patch=ProposalPatch()),
                    Proposal(operation="create", target_type="event", patch=ProposalPatch(title="期中考")),
                    Proposal(operation="create", target_type="task", patch=ProposalPatch(title="繳交作業")),
                ]

        result = main.interpret_source(self.user_id, self._source("本週有課"), DirectProvider())
        self.assertEqual([proposal["patch"].get("title") for proposal in result["proposals"]], ["期中考", "繳交作業"])
        with main.db() as connection:
            persisted = connection.execute("SELECT COUNT(*) FROM proposals WHERE source_id = ?", (result["source"]["id"],)).fetchone()[0]
        self.assertEqual(persisted, 2)

    def test_high_load_error_log_contains_only_safe_diagnostic_fields(self):
        error = AIProviderUnavailable("do not log this", model_alias="ministral-14b-latest\nprivate", http_status=502)
        with patch.object(main.api_logger, "warning") as warning:
            main._log_ai_interpretation_error(error, status_code=503, category="provider_unavailable")
        warning.assert_called_once_with(
            "AI interpretation failed model=%s status=%s category=%s",
            "ministral-14b-latestprivate", 502, "provider_unavailable",
        )

    def test_timetable_capture_review_apply_across_all_supported_sources(self):
        import fitz
        from PIL import Image, ImageDraw

        fixture = (
            "Fall Term Timetable\n"
            "Mathematics 101 | 2026-09-28 | 09:00-09:50 | Room A101\n"
            "English 202 | 2026-09-30 | 10:00-10:50 | Room B202\n"
            "Biology 303 | 2026-10-02 | 11:00-11:50 | Room C303\n"
            "Homework: Submit the lab worksheet | due 2026-10-05"
        )
        output = {"proposals": [
            {"operation": "create", "target_type": "event", "patch": {"title": "Mathematics 101", "date": "2026-09-28", "start_at": "2026-09-28T09:00:00+08:00", "end_at": "2026-09-28T09:50:00+08:00", "location": "Room A101"}},
            {"operation": "create", "target_type": "event", "patch": {"title": "English 202", "date": "2026-09-30", "start_at": "2026-09-30T10:00:00+08:00", "end_at": "2026-09-30T10:50:00+08:00", "location": "Room B202"}},
            {"operation": "create", "target_type": "event", "patch": {"title": "Biology 303", "date": "2026-10-02", "start_at": "2026-10-02T11:00:00+08:00", "end_at": "2026-10-02T11:50:00+08:00", "location": "Room C303"}},
            {"operation": "create", "target_type": "task", "patch": {"title": "Submit the lab worksheet", "due": "2026-10-05"}},
        ]}

        image_buffer = io.BytesIO(); image = Image.new("RGB", (1400, 500), "white")
        ImageDraw.Draw(image).text((24, 24), fixture, fill="black", spacing=14); image.save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue(); image_data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        searchable_pdf = fitz.open(); page = searchable_pdf.new_page(width=612, height=792); page.insert_textbox(fitz.Rect(40, 40, 570, 400), fixture, fontsize=11)
        searchable_pdf_bytes = searchable_pdf.tobytes(); searchable_pdf.close()
        scanned_pdf = fitz.open(); page = scanned_pdf.new_page(width=612, height=792); page.insert_image(page.rect, stream=image_bytes)
        scanned_pdf_bytes = scanned_pdf.tobytes(); scanned_pdf.close()

        class Provider(DeterministicFakeProvider):
            def __init__(self):
                super().__init__(output); self.text_sources = []; self.visual_sources = []
            def interpret(self, source, context):
                self.text_sources.append(source); return DeterministicFakeProvider.interpret(self, source, context)
            def interpret_multimodal_with_tools(self, source, context, images, query_tool):
                del query_tool; self.visual_sources.append((source, list(images))); return DeterministicFakeProvider.interpret(self, source, context)

        sources = [
            ("paste", main.NoticeInput(title="Timetable", body=fixture)),
            ("image", main.NoticeInput(attachments=[{"name": "timetable.png", "media_type": "image/png", "kind": "image", "data_url": image_data_url}])),
            ("searchable_pdf", main.NoticeInput(attachments=[{"name": "timetable.pdf", "media_type": "application/pdf", "kind": "pdf", "data_url": "data:application/pdf;base64," + base64.b64encode(searchable_pdf_bytes).decode("ascii")}])),
            ("scanned_pdf", main.NoticeInput(attachments=[{"name": "scanned-timetable.pdf", "media_type": "application/pdf", "kind": "pdf", "data_url": "data:application/pdf;base64," + base64.b64encode(scanned_pdf_bytes).decode("ascii")}])),
            ("url", main.NoticeInput(source_url="https://example.edu/timetable")),
        ]
        for source_kind, payload in sources:
            with self.subTest(source=source_kind):
                provider = Provider()
                if source_kind == "url":
                    with patch.object(main, "_crawl_capture_page", return_value={"status": "complete", "url": "https://example.edu/timetable", "title": "Timetable", "text": fixture}):
                        result = main.interpret_source(self.user_id, payload, provider)
                else:
                    result = main.interpret_source(self.user_id, payload, provider)
                self.assertEqual(result["processing"]["ai"]["status"], "complete")
                self.assertEqual(len(result["proposals"]), 4)
                if source_kind in {"image", "searchable_pdf", "scanned_pdf"}:
                    self.assertEqual(len(provider.visual_sources), 1)
                    self.assertTrue(provider.visual_sources[0][1])
                else:
                    self.assertIn("Mathematics 101", provider.text_sources[0])
                proposal_ids = [item["id"] for item in result["proposals"]]
                for proposal_id in proposal_ids: main.accept_proposal(self._request_with_user(), proposal_id)
                for proposal_id in proposal_ids: main.apply_proposal(self.user_id, proposal_id)
                with main.db() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE user_id=? AND source_id=?", (self.user_id, result["source"]["id"])).fetchone()[0], 3)
                    task = connection.execute("SELECT title,due_iso FROM tasks WHERE user_id=? AND source_id=?", (self.user_id, result["source"]["id"])).fetchone()
                self.assertEqual((task["title"], task["due_iso"]), ("Submit the lab worksheet", "2026-10-05"))

    def test_event_without_complete_time_cannot_be_accepted_as_all_day(self):
        result = self._interpret("明天有課", {"proposals": [{
            "operation": "create", "target_type": "event",
            "patch": {"title": "時間待確認的課程", "date": "2026-09-25"},
            "evidence_refs": ["明天有課"], "confidence": "low",
        }]})
        proposal_id = result["proposals"][0]["id"]
        with self.assertRaises(main.HTTPException) as error:
            main.accept_proposal(self._request_with_user(), proposal_id)
        self.assertEqual(error.exception.status_code, 422)
        with main.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM proposals WHERE id = ?", (proposal_id,)).fetchone()[0], "pending")

    def test_prompt_distinguishes_task_deadlines_from_scheduled_events(self):
        system = _prompt("10/5 前繳交數學作業", {"current_datetime": self.current_datetime.isoformat(), "timezone": "Asia/Taipei"})[0]["content"]
        self.assertIn("要繳交/完成/買/處理/截止是待辦語意", system)
        self.assertIn("上課/開會/考試/活動/比賽/發表是行程語意", system)
        self.assertIn("Task 有日期不代表 Event", system)

    def test_october_fifth_task_does_not_keep_false_ambiguous_review(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}
        proposals = validate_provider_output(
            {"proposals": [{
                "operation": "create",
                "target_type": "task",
                "patch": {"title": "交報告", "due": "2026-10-05"},
                "needs_review": True,
                "review_reason": "截止日期不完整，需明確日期",
            }]},
            context,
            "10/5要交報告",
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].target_type, "task")
        self.assertEqual(proposals[0].patch.due, "2026-10-05")
        self.assertFalse(proposals[0].needs_review)
        self.assertIsNone(proposals[0].review_reason)

    def test_flat_event_add_regressions_classify_tasks_and_events_without_duplicates(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}
        cases = [
            ("10/5要繳交報告", {"events": [{"title": "繳交報告", "date": "2026-10-05"}]}, "task", "2026-10-05"),
            ("10/5 14:00要繳交報告", {"events": [{"title": "繳交報告", "date": "2026-10-05", "start": "14:00"}]}, "task", "2026-10-05T14:00:00+08:00"),
            ("10/5 14:00開班會", {"events": [{"title": "開班會", "date": "2026-10-05", "start": "14:00"}]}, "event", "2026-10-05T14:00:00+08:00"),
            ("10/5前完成簡報", {"events": [{"title": "完成簡報", "date": "2026-10-05"}]}, "task", "2026-10-05"),
        ]
        for source, raw, target_type, expected_time in cases:
            with self.subTest(source=source):
                proposals = expand_minimal_output(raw, source, context)
                self.assertEqual(len(proposals), 1)
                proposal = proposals[0]
                self.assertEqual(proposal.target_type, target_type)
                actual = proposal.patch.due if target_type == "task" else proposal.patch.start_at
                self.assertEqual(actual, expected_time)
                self.assertFalse(proposal.needs_review)

        mixed = expand_minimal_output(
            {"events": [
                {"title": "開班會", "date": "2026-10-05", "start": "14:00"},
                {"title": "完成簡報", "date": "2026-10-05"},
            ]},
            "10/5 14:00開班會，10/5前完成簡報",
            context,
        )
        self.assertEqual([item.target_type for item in mixed], ["event", "task"])
        self.assertEqual(mixed[0].patch.start_at, "2026-10-05T14:00:00+08:00")
        self.assertEqual(mixed[1].patch.due, "2026-10-05")

    def test_provider_response_formats_use_json_object_and_backend_keeps_shape_validation(self):
        self.assertEqual(MINIMAL_ADD_RESPONSE_FORMAT, {"type": "json_object"})
        self.assertEqual(MINIMAL_UPDATE_RESPONSE_FORMAT, {"type": "json_object"})
        self.assertEqual(DATE_DISCOVERY_RESPONSE_FORMAT, {"type": "json_object"})
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}
        with self.assertRaises(AIProviderMalformedOutput):
            expand_minimal_output({"events": [{"title": "數學", "date": "2026-10-13", "location": "301"}]}, "10/13 數學", context)
        with self.assertRaises(AIProviderMalformedOutput):
            expand_minimal_output({"events": [{"title": "數學"}]}, "10/13 數學", context)

    def test_finish_reason_length_recovers_40_events_in_date_batches(self):
        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
        )
        context = {"current_datetime": "2026-10-03T14:12:00+08:00", "timezone": "Asia/Taipei"}
        first_day = datetime.fromisoformat("2026-10-13T00:00:00+08:00").date()
        dates = [(first_day + timedelta(days=index)).isoformat() for index in range(40)]
        calls = []

        def chat(_model, messages, **kwargs):
            calls.append((messages, kwargs))
            system = messages[0]["content"]
            if system.startswith("找出來源中所有需要建立項目的日曆日期"):
                return {"content": json.dumps({"dates": dates}), "_finish_reason": "stop"}
            user_content = messages[1]["content"]
            if isinstance(user_content, list):
                user_content = user_content[0]["text"]
            payload = json.loads(user_content)
            target_dates = payload.get("target_dates")
            if target_dates:
                return {
                    "content": json.dumps({
                        "events": [
                            {
                                "title": f"數學課 {dates.index(target_date) + 1:02d}",
                                "date": target_date,
                                "start": "08:15",
                                "end": "09:05",
                                "detail": "王大明",
                            }
                            for target_date in target_dates
                        ]
                    }, ensure_ascii=False),
                    "_finish_reason": "stop",
                }
            return {
                "content": '{"events":[{"title":"數學課 01","date":"2026-10-13"',
                "_finish_reason": "length",
            }

        with patch.object(provider, "_chat_message", side_effect=chat):
            proposals = provider.interpret("課表", context)

        self.assertEqual(len(proposals), 40)
        self.assertTrue(all(item.target_type == "event" for item in proposals))
        self.assertEqual(proposals[0].patch.start_at, "2026-10-13T08:15:00+08:00")
        self.assertEqual(proposals[-1].patch.end_at, "2026-11-21T09:05:00+08:00")
        # 1 initial truncated request + 1 date discovery + ceil(40 / 8) date-batched retries.
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(call[1]["max_tokens"] == 4096 for call in calls))
        retry_payloads = []
        for messages, _kwargs in calls[2:]:
            content = messages[1]["content"]
            if isinstance(content, list):
                content = content[0]["text"]
            retry_payloads.append(json.loads(content)["target_dates"])
        self.assertEqual([len(batch) for batch in retry_payloads], [8, 8, 8, 8, 8])

    def test_non_length_malformed_output_does_not_trigger_date_retry(self):
        provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest")
        with patch.object(provider, "_chat_message", return_value={"content": "not JSON", "_finish_reason": "stop"}) as chat:
            with self.assertRaises(AIProviderMalformedOutput):
                provider.interpret("10/13 數學課", {"current_datetime": self.current_datetime.isoformat(), "timezone": "Asia/Taipei"})
        self.assertEqual(chat.call_count, 1)

    def test_grouped_add_expands_shared_date_and_location(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei", "_visual_source": True}
        proposals = expand_minimal_output(
            {"days": [{
                "date": "2026-10-13",
                "events": [
                    {"title": "數學", "start": "08:15", "end": "09:05", "detail": "王大明", "location": "301"},
                    {"title": "英文", "start": "09:15", "end": "10:05"},
                ],
            }]},
            "課表",
            context,
        )
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0].target_type, "event")
        self.assertEqual(proposals[0].patch.start_at, "2026-10-13T08:15:00+08:00")
        self.assertEqual(proposals[0].patch.end_at, "2026-10-13T09:05:00+08:00")
        self.assertEqual(proposals[0].patch.detail, "王大明")
        self.assertEqual(proposals[0].patch.location, "301")
        self.assertEqual(proposals[1].patch.start_at, "2026-10-13T09:15:00+08:00")

    def test_flat_time_range_stays_event_even_when_capture_also_contains_task_language(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei", "_visual_source": True}
        proposals = expand_minimal_output(
            {"events": [{"title": "數學", "date": "2026-10-13", "start": "08:15", "end": "09:05"}]},
            "課表；10/15前完成作業",
            context,
        )
        self.assertEqual(proposals[0].target_type, "event")

    def test_flat_add_omits_empty_optional_fields(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}
        with self.assertRaises(AIProviderMalformedOutput):
            expand_minimal_output(
                {"events": [{"title": "數學", "date": "2026-10-13", "detail": ""}]},
                "10/13 數學",
                context,
            )

    def test_flat_add_supports_more_than_40_items(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei", "_visual_source": True}
        events = []
        for day_offset in range(5):
            day = f"2026-10-{13 + day_offset:02d}"
            events.extend([
                {"title": f"課程 {day_offset}-{index}", "date": day, "start": f"{8 + index // 4:02d}:{(index % 4) * 15:02d}", "end": f"{8 + index // 4:02d}:{(index % 4) * 15 + 10:02d}"}
                for index in range(9)
            ])
        proposals = expand_minimal_output({"events": events}, "課表", context)
        self.assertEqual(len(proposals), 45)

    def test_finish_reason_length_retries_by_date_only(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}

        class QueryTool:
            def search_calendar_events(self, *args, **kwargs):
                return []
            def search_tasks(self, *args, **kwargs):
                return []

        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", high_token_threshold=1,
        )
        truncated = {"role": "assistant", "content": '{"events":[{"title":"數學","date":"2026-10-13"', "_finish_reason": "length"}
        batched = {
            "role": "assistant",
            "content": json.dumps({"events": [
                {"title": "數學", "date": "2026-10-13", "start": "08:15", "end": "09:05"},
                {"title": "英文", "date": "2026-10-14", "start": "09:15", "end": "10:05"},
            ]}, ensure_ascii=False),
            "_finish_reason": "stop",
        }
        with patch.object(provider, "_chat_message", side_effect=[truncated, batched]) as chat:
            proposals = provider.interpret_with_tools("10/13 數學 08:15-09:05；10/14 英文 09:15-10:05", context, QueryTool())
        self.assertEqual(len(proposals), 2)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(chat.call_args_list[0].kwargs["max_tokens"], 4096)
        retry_payload = json.loads(chat.call_args_list[1].args[1][1]["content"])
        self.assertEqual(retry_payload["target_dates"], ["2026-10-13", "2026-10-14"])

    def test_truncated_date_batch_splits_until_each_date_fits(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}

        class QueryTool:
            def search_calendar_events(self, *args, **kwargs):
                return []
            def search_tasks(self, *args, **kwargs):
                return []

        provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest")
        initial = {"role": "assistant", "content": '{"events":[', "_finish_reason": "length"}
        batch_length = {"role": "assistant", "content": '{"events":[', "_finish_reason": "length"}
        day_13 = {"role": "assistant", "content": '{"events":[{"title":"數學","date":"2026-10-13","start":"08:15"}]}', "_finish_reason": "stop"}
        day_14 = {"role": "assistant", "content": '{"events":[{"title":"英文","date":"2026-10-14","start":"09:15"}]}', "_finish_reason": "stop"}
        with patch.object(provider, "_chat_message", side_effect=[initial, batch_length, day_13, day_14]) as chat:
            proposals = provider.interpret_with_tools("10/13 數學 08:15；10/14 英文 09:15", context, QueryTool())
        self.assertEqual(len(proposals), 2)
        self.assertEqual(chat.call_count, 4)
        left = json.loads(chat.call_args_list[2].args[1][1]["content"])["target_dates"]
        right = json.loads(chat.call_args_list[3].args[1][1]["content"])["target_dates"]
        self.assertEqual(left, ["2026-10-13"] )
        self.assertEqual(right, ["2026-10-14"] )

    def test_visual_length_recovery_discovers_dates_only_after_truncation(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei", "_visual_source": True}

        class QueryTool:
            def search_calendar_events(self, *args, **kwargs):
                return []
            def search_tasks(self, *args, **kwargs):
                return []

        provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest")
        initial = {"role": "assistant", "content": '{"events":[', "_finish_reason": "length"}
        dates = {"role": "assistant", "content": '{"dates":["2026-10-13"]}', "_finish_reason": "stop"}
        retry = {"role": "assistant", "content": '{"events":[{"title":"數學","date":"2026-10-13","start":"08:15","end":"09:05"}]}', "_finish_reason": "stop"}
        images = [{"name": "schedule.png", "data_url": "data:image/png;base64,AA=="}]
        with patch.object(provider, "_chat_message", side_effect=[initial, dates, retry]) as chat:
            proposals = provider.interpret_multimodal_with_tools("", context, images, QueryTool())
        self.assertEqual(len(proposals), 1)
        self.assertEqual(chat.call_count, 3)
        self.assertIsNone(chat.call_args_list[0].kwargs["tools"])
        self.assertIsNone(chat.call_args_list[0].kwargs["tool_choice"])
        self.assertEqual(chat.call_args_list[1].kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(json.loads(chat.call_args_list[2].args[1][1]["content"][0]["text"])["target_dates"], ["2026-10-13"])

    def test_finish_reason_stop_does_not_retry(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}

        class QueryTool:
            def search_calendar_events(self, *args, **kwargs):
                return []
            def search_tasks(self, *args, **kwargs):
                return []

        provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest")
        final = {"role": "assistant", "content": '{"events":[]}', "_finish_reason": "stop"}
        with patch.object(provider, "_chat_message", return_value=final) as chat:
            self.assertEqual(provider.interpret_with_tools("沒有要新增的內容", context, QueryTool()), [])
        self.assertEqual(chat.call_count, 1)

    def test_minimal_update_calls_read_only_search_then_returns_only_delta(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}

        class QueryTool:
            def __init__(self):
                self.calls = []

            def search_calendar_events(self, start_date, end_date, keyword=None, max_results=5):
                self.calls.append(("event", start_date, end_date, keyword, max_results))
                return [{
                    "id": "event-sport", "title": "體育課",
                    "start": "2026-10-05T10:00:00+08:00", "end": "2026-10-05T11:00:00+08:00",
                    "location": "操場",
                }]

            def search_tasks(self, keyword=None, due_from=None, due_to=None, max_results=5):
                self.calls.append(("task", keyword, due_from, due_to, max_results))
                return []

        provider = LiteLLMProvider(
            "http://127.0.0.1:4000/v1", "test-key", "mistral-small-latest", "legacy-ocr-model",
            high_token_threshold=999999,
        )
        query = QueryTool()
        tool_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "search_calendar_events",
                    "arguments": json.dumps({
                        "start_date": "2026-10-05", "end_date": "2026-10-06",
                        "keyword": "體育", "max_results": 99,
                    }),
                },
            }],
        }
        final = {"role": "assistant", "content": '{"u":[["event-sport",{"location":"攀岩場"}]]}'}
        with patch.object(provider, "_chat_message", side_effect=[tool_call, final]) as chat:
            proposals = provider.interpret_with_tools("把10/5體育課地點改成攀岩場", context, query)

        self.assertEqual(query.calls, [("event", "2026-10-05", "2026-10-06", "體育", 5)])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].operation, "update")
        self.assertEqual(proposals[0].target_id, "event-sport")
        self.assertEqual(proposals[0].patch.model_dump(exclude_unset=True), {"location": "攀岩場"})
        self.assertEqual(chat.call_args_list[0].kwargs["response_format"], MINIMAL_ADD_RESPONSE_FORMAT)
        self.assertEqual(chat.call_args_list[1].kwargs["response_format"], MINIMAL_UPDATE_RESPONSE_FORMAT)
        self.assertEqual(chat.call_args_list[0].kwargs["tools"], READ_ONLY_INTERPRET_TOOLS)

    def test_ambiguous_minimal_update_needs_review_and_never_guesses_target_id(self):
        context = {"current_datetime": "2026-10-03T11:00:00+08:00", "timezone": "Asia/Taipei"}
        trace = [{
            "name": "search_calendar_events",
            "args": {"start_date": "2026-10-05", "end_date": "2026-10-06", "keyword": "體育", "max_results": 5},
            "results": [
                {"id": "event-a", "title": "體育課", "start": "2026-10-05T09:00:00+08:00", "end": "2026-10-05T10:00:00+08:00"},
                {"id": "event-b", "title": "體育課", "start": "2026-10-05T14:00:00+08:00", "end": "2026-10-05T15:00:00+08:00"},
            ],
        }]
        proposals = expand_minimal_output(
            {"u": [["", {"location": "攀岩場"}]]},
            "10/5體育課改到攀岩場",
            context,
            trace,
        )
        self.assertEqual(len(proposals), 1)
        self.assertTrue(proposals[0].needs_review)
        self.assertIsNone(proposals[0].target_id)
        self.assertEqual({item.id for item in proposals[0].target_candidates}, {"event-a", "event-b"})

        with self.assertRaises(AIProviderMalformedOutput):
            expand_minimal_output(
                {"u": [["event-not-searched", {"location": "攀岩場"}]]},
                "10/5體育課改到攀岩場",
                context,
                trace,
            )

    def test_explicit_all_day_source_is_canonicalized_and_applies_as_all_day(self):
        result = self._interpret(
            "10/5 校慶全天",
            {"proposals": [{
                "operation": "create", "target_type": "event",
                "patch": {"title": "校慶", "date": "2026-10-05"},
                "evidence_refs": ["10/5 校慶全天"], "confidence": "high",
            }]},
        )
        proposal = result["proposals"][0]
        self.assertTrue(proposal["patch"]["all_day"])
        main.accept_proposal(self._request_with_user(), proposal["id"])
        applied = main.apply_proposal(self.user_id, proposal["id"])
        with main.db() as connection:
            row = connection.execute(
                "SELECT date_label, start_at, end_at, all_day FROM events WHERE id = ?",
                (applied["result"]["event_id"],),
            ).fetchone()
        self.assertEqual(dict(row), {
            "date_label": "2026-10-05",
            "start_at": "2026-10-05",
            "end_at": "2026-10-06",
            "all_day": 1,
        })

    def test_start_only_ai_event_can_be_accepted_and_stays_open_ended_locally(self):
        result = self._interpret(
            "明天 09:00 集合",
            {"proposals": [{
                "operation": "create", "target_type": "event",
                "patch": {"title": "集合", "date": "2026-09-25", "time": "09:00"},
                "evidence_refs": ["明天 09:00 集合"], "confidence": "high",
            }]},
        )
        proposal = result["proposals"][0]
        main.accept_proposal(self._request_with_user(), proposal["id"])
        applied = main.apply_proposal(self.user_id, proposal["id"])
        with main.db() as connection:
            row = connection.execute(
                "SELECT date_label, time_label, start_at, end_at, all_day FROM events WHERE id = ?",
                (applied["result"]["event_id"],),
            ).fetchone()
        self.assertEqual(row["date_label"], "2026-09-25")
        self.assertEqual(row["time_label"], "09:00")
        self.assertTrue(str(row["start_at"]).startswith("2026-09-25T09:00"))
        self.assertIsNone(row["end_at"])
        self.assertEqual(row["all_day"], 0)

    def test_provider_cannot_invent_event_end_time_for_start_only_source(self):
        result = self._interpret(
            "明天 09:00 集合",
            {"proposals": [{
                "operation": "create", "target_type": "event",
                "patch": {
                    "title": "集合",
                    "start_at": "2026-09-25T09:00:00+08:00",
                    "end_at": "2026-09-25T09:30:00+08:00",
                },
                "evidence_refs": ["明天 09:00 集合"], "confidence": "high",
            }]},
        )
        self.assertIsNone(result["proposals"][0]["patch"].get("end_at"))

    def test_provider_keeps_explicit_event_end_time_from_source(self):
        result = self._interpret(
            "明天 09:00–09:30 集合",
            {"proposals": [{
                "operation": "create", "target_type": "event",
                "patch": {
                    "title": "集合",
                    "start_at": "2026-09-25T09:00:00+08:00",
                    "end_at": "2026-09-25T09:30:00+08:00",
                },
                "evidence_refs": ["明天 09:00–09:30 集合"], "confidence": "high",
            }]},
        )
        self.assertEqual(result["proposals"][0]["patch"]["end_at"], "2026-09-25T09:30:00+08:00")

    def test_ai_created_event_can_be_edited(self):
        event_id, proposal_id = self._create_ai_event("edit")
        edited = main.update_event(self._request_with_user(), event_id, main.EventUpdate(title="Edited AI event"))
        self.assertEqual(edited["title"], "Edited AI event")
        self.assertEqual(edited["created_source"], "ai_generated")
        self.assertEqual(edited["origin_proposal_id"], proposal_id)
        self.assertEqual(edited["lifecycle_status"], "edited")

    def test_ai_created_event_can_be_deleted(self):
        event_id, proposal_id = self._create_ai_event("delete")
        deleted = main.delete_event(self._request_with_user(), event_id)
        self.assertEqual(deleted["lifecycle_status"], "deleted")
        with main.db() as connection:
            row = connection.execute(
                "SELECT created_source, origin_proposal_id, lifecycle_status FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
        self.assertEqual(dict(row), {"created_source": "ai_generated", "origin_proposal_id": proposal_id, "lifecycle_status": "deleted"})

    def test_read_only_external_events_reject_mutation_without_raw_storage(self):
        saved = main.save_nextcloud_source(
            self.user_id,
            {"server_url": "https://calendar.example/dav/", "calendars": [{"remote_url": "https://calendar.example/cal/", "display_name": "School", "timezone": "Asia/Taipei", "read_only": True}]},
            "student", "secret",
        )
        calendar_id = saved["calendars"][0]["id"]
        raw = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:remote-1\r\nDTSTART;TZID=Asia/Taipei:20260924T090000\r\nDTEND;TZID=Asia/Taipei:20260924T100000\r\nSUMMARY:External\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        with main.db() as connection:
            calendar = connection.execute("SELECT * FROM calendar_calendars WHERE id=?", (calendar_id,)).fetchone()
        main.persist_calendar_report(saved["source"]["id"], calendar, [{"href":"https://calendar.example/cal/remote-1.ics", "etag":'"v1"', "calendar_data":raw}], main.now_iso())
        with main.db() as connection:
            event_id = connection.execute("SELECT id FROM calendar_events WHERE remote_id='remote-1'").fetchone()["id"]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(calendar_events)")}
        self.assertNotIn("raw_ical", columns)
        request = self._request_with_user()
        with self.assertRaises(main.HTTPException) as error:
            main.update_event(request, event_id, main.EventUpdate(title="Nope"))
        self.assertEqual(error.exception.status_code, 403)

    def test_calendar_query_returns_only_bounded_results(self):
        self._insert_event("inside", title="Keep this", date_label="2026-09-22")
        with main.db() as connection:
            connection.execute(
                "INSERT INTO events(id, user_id, title, date_label, time_label, detail, source_id, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{self.user_id}-outside", self.user_id, "Outside", "2026-10-01", "09:00", "", "manual", None),
            )
        tool = main.SQLiteCalendarQueryTool(self.user_id, max_results=10)
        results = tool.search_calendar_events("2026-09-22", "2026-09-23")
        self.assertEqual([item["title"] for item in results], ["Keep this"])
        self.assertFalse({"source", "event_source", "created_source", "origin_proposal_id", "provider", "sync_status"} & set(results[0]))
        keyword_results = tool.search_calendar_events("2026-09-22", "2026-09-23", keyword="Keep")
        self.assertEqual(len(keyword_results), 1)

    def test_task_creation_canonicalizes_omitted_due_to_null(self):
        result = self._interpret(
            "記得完成 U2 第1回小卷",
            {
                "proposals": [
                    {
                        "operation": "create",
                        "target_type": "task",
                        "patch": {"title": "完成 U2 第1回小卷"},
                    }
                ]
            },
        )
        self.assertIn("due", result["proposals"][0]["patch"])
        self.assertIsNone(result["proposals"][0]["patch"]["due"])

    def test_interpretation_context_contains_only_clock_and_timezone(self):
        self._insert_event("context-event")
        task_id = f"{self.user_id}-context-task"
        with main.db() as connection:
            connection.execute(
                "INSERT INTO tasks(id, user_id, title, due_label, due_iso, status, source_id, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, self.user_id, "Context task", "tomorrow", "2026-09-22T12:00:00+08:00", "open", "manual", "student"),
            )

        class ContextProvider(DeterministicFakeProvider):
            def interpret(self, source, context):
                self.source = source
                self.context = context
                return super().interpret(source, context)

        provider = ContextProvider({"proposals": []})
        main.interpret_source(self.user_id, self._source("context input"), provider)
        self.assertEqual(provider.source, "context input")
        self.assertEqual(set(provider.context), {"current_datetime", "timezone"})
        self.assertEqual(provider.context["timezone"], "Asia/Taipei")
        self.assertNotIn("nearby_calendar_events", provider.context)
        self.assertNotIn("relevant_open_tasks", provider.context)
        self.assertNotIn("target_ids", provider.context)

    def test_context_build_does_not_query_calendar_or_tasks_eagerly(self):
        class ContextProvider(DeterministicFakeProvider):
            def interpret(self, source, context):
                self.called = True
                self.context = context
                return super().interpret(source, context)

        provider = ContextProvider({"proposals": []})
        with patch.object(main.SQLiteCalendarQueryTool, "search_calendar_events", side_effect=AssertionError("eager calendar query")), \
             patch.object(main.SQLiteCalendarQueryTool, "search_tasks", side_effect=AssertionError("eager task query")):
            result = main.interpret_source(self.user_id, self._source("明天有什麼？"), provider)

        self.assertTrue(provider.called)
        self.assertEqual(result["proposals"], [])
        self.assertEqual(set(provider.context), {"current_datetime", "timezone"})

    def test_source_list_contract_excludes_raw_attachment_bytes(self):
        result = main.interpret_source(
            self.user_id,
            main.NoticeInput(
                body="source contract",
                attachments=[{"name": "contract.pdf", "media_type": "application/pdf", "kind": "pdf", "data_url": "data:application/pdf;base64,AA=="}],
            ),
            DeterministicFakeProvider({"proposals": []}),
        )
        listed = main.unified_sources(self._request_with_user())
        source = next(item for item in listed["sources"] if item["id"] == result["source"]["id"])
        self.assertNotIn("data_url", json.dumps(source))
        self.assertIn("content_hash", json.dumps(source))

    def test_ambiguous_event_requires_review(self):
        event_a = self._insert_event("event-sport-a")
        event_b = self._insert_event("event-sport-b")
        result = self._interpret(
            "明天體育課在攀岩場",
            {
                "proposals": [
                    {
                        "operation": "update",
                        "target_type": "event",
                        "target_id": None,
                        "patch": {"location": "攀岩場"},
                        "target_candidates": [
                            {"id": event_a, "title": "體育"},
                            {"id": event_b, "title": "體育"},
                        ],
                        "needs_review": True,
                        "review_reason": "有兩個相符的體育行程",
                    }
                ]
            },
        )
        proposal = result["proposals"][0]
        with self.assertRaises(main.HTTPException) as error:
            main.accept_proposal(self._request_with_user(), proposal["id"])
        self.assertEqual(error.exception.status_code, 409)

    def test_no_matching_event_requires_review(self):
        result = self._interpret(
            "明天體育課在攀岩場",
            {
                "proposals": [
                    {
                        "operation": "update",
                        "target_type": "event",
                        "patch": {"location": "攀岩場"},
                        "needs_review": True,
                        "review_reason": "找不到可安全更新的體育行程",
                    }
                ]
            },
        )
        self.assertIsNone(result["proposals"][0]["target_id"])
        self.assertEqual(result["proposals"][0]["status"], "pending")

    def test_malformed_provider_output(self):
        with self.assertRaises(AIProviderMalformedOutput):
            self._interpret("來源", {"proposals": [{"operation": "not-valid"}]})

    def test_provider_unavailable_is_not_fake_success(self):
        class UnavailableProvider:
            name = "litellm"

            def interpret(self, source, context):
                raise AIProviderUnavailable("unavailable")

        with self.assertRaises(AIProviderUnavailable):
            main.interpret_source(self.user_id, self._source("來源"), UnavailableProvider())

    def test_attachments_are_saved_on_source_and_reported_as_unsupported(self):
        payload = main.NoticeInput(
            body="",
            attachments=[
                {"name": "LINE_20260921.jpg", "media_type": "image/jpeg", "size": 1204, "kind": "image"},
                {"name": "英文通知.pdf", "media_type": "application/pdf", "size": 2408, "kind": "pdf"},
            ],
        )
        result = main.interpret_source(self.user_id, payload)
        self.assertEqual(result["proposals"], [])
        self.assertEqual([item["name"] for item in result["unsupported_attachments"]], ["LINE_20260921.jpg", "英文通知.pdf"])
        with main.db() as connection:
            source = connection.execute("SELECT title, attachments_json FROM notices WHERE id = ?", (result["source"]["id"],)).fetchone()
        self.assertEqual(source["title"], "LINE_20260921.jpg")
        self.assertEqual([item["kind"] for item in json.loads(source["attachments_json"])], ["image", "pdf"])

    def test_image_is_sent_once_to_multimodal_interpretation_without_persisting_ocr_transcript(self):
        from PIL import Image
        image = io.BytesIO(); Image.new("RGB", (32, 32), "white").save(image, format="PNG")
        image_data_url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        class VisionFake(DeterministicFakeProvider):
            def __init__(self):
                super().__init__({"proposals": [{"operation": "create", "target_type": "task", "patch": {"title": "完成 U2 第1回小卷"}}]})
                self.calls = []
            def interpret_multimodal_with_tools(self, source, context, images, query_tool):
                del query_tool; self.calls.append((source, list(images))); return DeterministicFakeProvider.interpret(self, source, context)
        provider = VisionFake()
        result = main.interpret_source(self.user_id, main.NoticeInput(attachments=[{
            "name": "通知截圖.png", "media_type": "image/png", "size": len(image.getvalue()), "kind": "image", "data_url": image_data_url,
        }]), provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][0], "")
        self.assertEqual(len(provider.calls[0][1]), 1)
        attachment = result["source"]["attachments"][0]
        self.assertEqual(attachment["extraction_method"], "vision")
        self.assertEqual(attachment["extraction_status"], "succeeded")
        self.assertNotIn("extracted_text", attachment)
        with main.db() as connection:
            row = connection.execute("SELECT extracted_text, extraction_method, extraction_status FROM attachments WHERE id=?", (attachment["id"],)).fetchone()
        self.assertIsNone(row["extracted_text"])
        self.assertEqual((row["extraction_method"], row["extraction_status"]), ("vision", "succeeded"))

    def test_image_model_failure_keeps_source_and_surfaces_provider_error(self):
        from PIL import Image
        image = io.BytesIO(); Image.new("RGB", (16, 16), "white").save(image, format="PNG")
        data = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        class FailureProvider(DeterministicFakeProvider):
            def __init__(self): super().__init__({"proposals": []})
            def interpret_multimodal_with_tools(self, source, context, images, query_tool):
                raise AIProviderUnavailable("temporary visual model failure")
        with self.assertRaises(AIProviderUnavailable):
            main.interpret_source(self.user_id, main.NoticeInput(attachments=[{"name":"通知.png","media_type":"image/png","kind":"image","data_url":data}]), FailureProvider())
        with main.db() as connection:
            row = connection.execute("SELECT extraction_method, extraction_status FROM attachments WHERE source_id IN (SELECT id FROM notices WHERE user_id=?) ORDER BY created_at DESC LIMIT 1", (self.user_id,)).fetchone()
        self.assertEqual((row["extraction_method"], row["extraction_status"]), ("vision", "succeeded"))

    def test_saved_ocr_text_is_reused_when_source_is_reopened(self):
        class ReopenedSourceProvider(DeterministicFakeProvider):
            def extract_text(self, attachment):
                raise AssertionError("saved OCR text should avoid a second image request")

            def interpret(self, source, context):
                self.interpreted_source = source
                return super().interpret(source, context)

        provider = ReopenedSourceProvider({"proposals": []})
        result = main.interpret_source(
            self.user_id,
            main.NoticeInput(
                body="",
                attachments=[
                    {
                        "name": "通知截圖.png",
                        "media_type": "image/png",
                        "size": 8,
                        "kind": "image",
                        "extracted_text": "記得完成 U2 第1回小卷",
                    }
                ],
            ),
            provider,
        )
        self.assertEqual(provider.interpreted_source, "記得完成 U2 第1回小卷")
        self.assertEqual(result["unsupported_attachments"], [])
        self.assertEqual(result["source"]["attachments"][0]["extracted_text"], "記得完成 U2 第1回小卷")

    def test_attachment_upload_creates_temporary_blob_reference(self):
        result = main.interpret_source(
            self.user_id,
            main.NoticeInput(body="", attachments=[{"name": "temporary.png", "media_type": "image/png", "kind": "image", "data_url": "data:image/png;base64,AA=="}]),
            DeterministicFakeProvider({"proposals": []}),
        )
        attachment = result["source"]["attachments"][0]
        self.assertTrue(attachment["content_hash"])
        self.assertTrue(attachment["raw_available"])
        self.assertIsNotNone(attachment["expires_at"])
        with main.db() as connection:
            stored = connection.execute("SELECT attachments_json FROM notices WHERE id = ?", (result["source"]["id"],)).fetchone()
            row = connection.execute("SELECT * FROM attachments WHERE id = ?", (attachment["id"],)).fetchone()
        self.assertNotIn("data_url", stored["attachments_json"])
        self.assertTrue(main.ATTACHMENT_STORE.exists(row["storage_key"]))

    def test_visual_attachment_persists_metadata_not_an_ocr_transcript(self):
        from PIL import Image
        image = io.BytesIO(); Image.new("RGB", (16,16), "white").save(image, format="PNG")
        data = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        result = main.interpret_source(self.user_id, main.NoticeInput(attachments=[{"name":"vision.png","media_type":"image/png","kind":"image","data_url":data}]), DeterministicFakeProvider({"proposals": []}))
        with main.db() as connection:
            row = connection.execute("SELECT extracted_text, extraction_method, extraction_status FROM attachments WHERE source_id=?", (result["source"]["id"],)).fetchone()
        self.assertEqual(dict(row), {"extracted_text": None, "extraction_method": "vision", "extraction_status": "succeeded"})

    def test_reprocessing_visual_attachment_uses_raw_blob_again_while_available(self):
        from PIL import Image
        image = io.BytesIO(); Image.new("RGB", (16,16), "white").save(image, format="PNG")
        data = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        class Provider(DeterministicFakeProvider):
            def __init__(self): super().__init__({"proposals": []}); self.calls = 0
            def interpret_multimodal_with_tools(self, source, context, images, query_tool): self.calls += 1; return []
        first_provider = Provider(); first = main.interpret_source(self.user_id, main.NoticeInput(attachments=[{"name":"reuse.png","media_type":"image/png","kind":"image","data_url":data}]), first_provider)
        attachment = first["source"]["attachments"][0]; second_provider = Provider()
        second = main.interpret_source(self.user_id, main.NoticeInput(source_id=first["source"]["id"], attachments=[{"id":attachment["id"],"name":attachment["name"],"media_type":attachment["media_type"],"kind":"image"}]), second_provider)
        self.assertEqual(first_provider.calls, 1); self.assertEqual(second_provider.calls, 1)
        self.assertEqual(second["source"]["id"], first["source"]["id"])
        self.assertNotIn("extracted_text", second["source"]["attachments"][0])

    def test_reprocessing_does_not_duplicate_source(self):
        first = main.interpret_source(self.user_id, main.NoticeInput(body="保留同一來源"), DeterministicFakeProvider({"proposals": []}))
        main.interpret_source(self.user_id, main.NoticeInput(source_id=first["source"]["id"], body=""), DeterministicFakeProvider({"proposals": []}))
        with main.db() as connection:
            count = connection.execute("SELECT COUNT(*) FROM notices WHERE id = ?", (first["source"]["id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_reprocessing_supersedes_old_unreviewed_proposals_without_returning_history(self):
        first = main.interpret_source(
            self.user_id,
            main.NoticeInput(body="繳交作業"),
            DeterministicFakeProvider({"proposals": [{
                "operation": "create", "target_type": "task",
                "patch": {"title": "繳交作業", "due": "2026-09-25"},
                "evidence_refs": ["繳交作業"], "confidence": "high",
            }]}),
        )
        old_id = first["proposals"][0]["id"]
        second = main.interpret_source(
            self.user_id,
            main.NoticeInput(source_id=first["source"]["id"], body=""),
            DeterministicFakeProvider({"proposals": [{
                "operation": "create", "target_type": "task",
                "patch": {"title": "繳交作業", "due": "2026-09-28"},
                "evidence_refs": ["繳交作業"], "confidence": "high",
            }]}),
        )
        self.assertEqual(len(second["proposals"]), 1)
        self.assertEqual(second["proposals"][0]["patch"]["due"], "2026-09-28")
        with main.db() as connection:
            history = connection.execute(
                "SELECT id, status FROM proposals WHERE source_id = ? ORDER BY created_at, id",
                (first["source"]["id"],),
            ).fetchall()
        self.assertEqual(len(history), 2)
        self.assertEqual(next(row["status"] for row in history if row["id"] == old_id), "superseded")
        self.assertEqual(next(row["status"] for row in history if row["id"] == second["proposals"][0]["id"]), "pending")

    def test_expired_blob_is_deleted_but_backend_extracted_text_survives(self):
        raw = b"durable extraction"
        result = main.interpret_source(self.user_id, main.NoticeInput(attachments=[{"name":"expire.txt","media_type":"text/plain","kind":"file","size":len(raw),"data_url":"data:text/plain;base64,"+base64.b64encode(raw).decode("ascii")}]), DeterministicFakeProvider({"proposals": []}))
        attachment = result["source"]["attachments"][0]
        with main.db() as connection:
            row = connection.execute("SELECT status,extracted_text,storage_key FROM attachments WHERE id=?", (attachment["id"],)).fetchone()
        self.assertEqual(row["status"], "active"); self.assertEqual(row["extracted_text"], "durable extraction")
        self.assertIsNone(row["storage_key"])

    def test_extraction_failure_retains_blob_until_ttl(self):
        class FailureProvider:
            name = "fixture"

            def extract_text(self, attachment):
                raise AIProviderUnavailable("temporary")

            def interpret(self, source, context):
                return []

        result = main.interpret_source(
            self.user_id,
            main.NoticeInput(body="", attachments=[{"name": "retry.png", "media_type": "image/png", "kind": "image", "data_url": "data:image/png;base64,AA=="}]),
            FailureProvider(),
        )
        attachment = result["source"]["attachments"][0]
        expiry = datetime.fromisoformat(attachment["expires_at"])
        self.assertTrue(main.ATTACHMENT_STORE.exists(attachment["storage_key"]))
        main.cleanup_expired_attachments(expiry - main.timedelta(seconds=1))
        self.assertTrue(main.ATTACHMENT_STORE.exists(attachment["storage_key"]))

    def test_identical_bytes_dedupe_blob_but_not_sources(self):
        payload = {"name": "same.pdf", "media_type": "application/pdf", "kind": "pdf", "data_url": "data:application/pdf;base64,AA=="}
        first = main.interpret_source(self.user_id, main.NoticeInput(attachments=[payload]), DeterministicFakeProvider({"proposals": []}))
        second = main.interpret_source(self.user_id, main.NoticeInput(attachments=[payload]), DeterministicFakeProvider({"proposals": []}))
        with main.db() as connection:
            rows = connection.execute("SELECT source_id, content_hash, storage_key FROM attachments WHERE source_id IN (?, ?)", (first["source"]["id"], second["source"]["id"])).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["content_hash"], rows[1]["content_hash"])
        self.assertEqual(rows[0]["storage_key"], rows[1]["storage_key"])
        self.assertNotEqual(first["source"]["id"], second["source"]["id"])

    def test_expired_raw_required_source_reports_file_again(self):
        result = main.interpret_source(
            self.user_id,
            main.NoticeInput(attachments=[{"name": "vision.png", "media_type": "image/png", "kind": "image", "data_url": "data:image/png;base64,AA=="}]),
            DeterministicFakeProvider({"proposals": []}),
        )
        attachment = result["source"]["attachments"][0]
        expiry = datetime.fromisoformat(attachment["expires_at"])
        main.cleanup_expired_attachments(expiry + main.timedelta(seconds=1))
        second = main.interpret_source(
            self.user_id,
            main.NoticeInput(source_id=result["source"]["id"], attachments=[{"id": attachment["id"], "name": attachment["name"], "media_type": attachment["media_type"], "kind": "image"}]),
            DeterministicFakeProvider({"proposals": []}),
        )
        self.assertTrue(second["unsupported_attachments"][0]["raw_required"])

    def test_backend_extracted_text_source_reprocesses_after_raw_expiry(self):
        raw = b"still usable extracted text"
        first = main.interpret_source(self.user_id, main.NoticeInput(attachments=[{"name":"text.txt","media_type":"text/plain","kind":"file","size":len(raw),"data_url":"data:text/plain;base64,"+base64.b64encode(raw).decode("ascii")}]), DeterministicFakeProvider({"proposals": []}))
        attachment = first["source"]["attachments"][0]
        self.assertFalse(attachment["raw_available"])
        second = main.interpret_source(self.user_id, main.NoticeInput(source_id=first["source"]["id"]), DeterministicFakeProvider({"proposals": []}))
        self.assertEqual(second["unsupported_attachments"], [])
        self.assertEqual(second["source"]["attachments"][0]["extracted_text"], "still usable extracted text")

    def test_unauthorized_attachment_access_fails(self):
        result = main.interpret_source(
            self.user_id,
            main.NoticeInput(attachments=[{"name": "private.pdf", "media_type": "application/pdf", "kind": "pdf", "data_url": "data:application/pdf;base64,AA=="}]),
            DeterministicFakeProvider({"proposals": []}),
        )
        other_user = f"other-{self._testMethodName}"
        with main.db() as connection:
            connection.execute("INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (other_user, "Other", None, None, main.now_iso(), main.now_iso()))
        from starlette.responses import Response
        response = Response()
        main.create_session(other_user, response)
        cookie = response.headers["set-cookie"].split(";", 1)[0]
        from starlette.requests import Request
        other_request = Request({"type": "http", "method": "GET", "path": "/api/sources", "headers": [(b"cookie", cookie.encode())]})
        with self.assertRaises(main.HTTPException) as error:
            main.get_source_attachment(other_request, result["source"]["id"], result["source"]["attachments"][0]["id"])
        self.assertEqual(error.exception.status_code, 404)

    def test_cleanup_is_idempotent(self):
        result = main.interpret_source(self.user_id, main.NoticeInput(attachments=[{"name": "idempotent.pdf", "media_type": "application/pdf", "kind": "pdf", "data_url": "data:application/pdf;base64,AA=="}]), DeterministicFakeProvider({"proposals": []}))
        expiry = datetime.fromisoformat(result["source"]["attachments"][0]["expires_at"]) + main.timedelta(seconds=1)
        first = main.cleanup_expired_attachments(expiry)
        second = main.cleanup_expired_attachments(expiry)
        self.assertGreaterEqual(first["expired"], 1)
        self.assertGreaterEqual(second["expired"], 1)
        self.assertEqual(second["failures"], 0)

    def test_legacy_data_url_materializes_when_reprocessed(self):
        source_id = f"legacy-{self._testMethodName}"
        legacy = {"id": "legacy-file", "name": "legacy.png", "media_type": "image/png", "kind": "image", "size": 1, "data_url": "data:image/png;base64,AA==", "extracted_text": "legacy OCR"}
        with main.db() as connection:
            connection.execute("INSERT INTO notices(id, user_id, title, body, audience, created_at, source_type, confidence, source_name, source_url, attachments_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (source_id, self.user_id, "Legacy", "", "我的課程", main.now_iso(), "import", "provider", None, None, json.dumps([legacy])))
        result = main.interpret_source(self.user_id, main.NoticeInput(source_id=source_id, attachments=[{"id": "legacy-file", "name": "legacy.png", "media_type": "image/png", "kind": "image"}]), DeterministicFakeProvider({"proposals": []}))
        with main.db() as connection:
            count = connection.execute("SELECT COUNT(*) FROM attachments WHERE source_id = ?", (source_id,)).fetchone()[0]
            raw_json = connection.execute("SELECT attachments_json FROM notices WHERE id = ?", (source_id,)).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertNotIn("data_url", raw_json)
        self.assertEqual(result["source"]["attachments"][0]["extracted_text"], "legacy OCR")

    def test_source_list_exposes_metadata_without_raw_data(self):
        result = main.interpret_source(self.user_id, main.NoticeInput(attachments=[{"name": "listed.pdf", "media_type": "application/pdf", "kind": "pdf", "data_url": "data:application/pdf;base64,AA=="}]), DeterministicFakeProvider({"proposals": []}))
        with main.db() as connection:
            source = connection.execute("SELECT * FROM notices WHERE id = ?", (result["source"]["id"],)).fetchone()
        listed = main.notice_source_payload(source)
        self.assertIn("attachments", listed)
        self.assertNotIn("data_url", json.dumps(listed))

    def test_multimodal_interpretation_uses_primary_model_and_compact_output(self):
        from PIL import Image
        image = io.BytesIO(); Image.new("RGB", (1,1), "white").save(image, format="PNG")
        image_data_url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test-key", "vision-model", "legacy-ocr-model")
        with patch.object(provider, "_complete", return_value='{"events":[]}') as complete:
            result = provider.interpret_multimodal("", {"current_datetime": self.current_datetime.isoformat(), "timezone":"Asia/Taipei"}, [{"name":"課表.png","data_url":image_data_url}])
        self.assertEqual(result, [])
        self.assertEqual(complete.call_args.args[0], "vision-model")
        messages = complete.call_args.args[1]
        self.assertEqual(messages[1]["content"][1]["image_url"]["url"], image_data_url)
        self.assertEqual(complete.call_count, 1)

    def test_legacy_ocr_model_arguments_do_not_create_a_second_model_fallback(self):
        from PIL import Image
        image = io.BytesIO(); Image.new("RGB", (1,1), "white").save(image, format="PNG")
        image_data_url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test-key", "vision-model", "legacy-ocr-model", ocr_fallback_model="legacy-fallback")
        attempted=[]
        def complete(model, messages, **kwargs): attempted.append(model); raise AIProviderUnavailable("primary unavailable")
        with patch.object(provider, "_complete", side_effect=complete):
            with self.assertRaises(AIProviderUnavailable):
                provider.interpret_multimodal("", {"current_datetime": self.current_datetime.isoformat(), "timezone":"Asia/Taipei"}, [{"name":"x.png","data_url":image_data_url}])
        self.assertEqual(attempted, ["vision-model"])

    def test_accept_does_not_mutate(self):
        event_id = self._insert_event("event-sport")
        result = self._interpret("明天體育課在攀岩場", {"proposals": [{"operation": "update", "target_type": "event", "target_id": event_id, "patch": {"location": "攀岩場"}}]})
        proposal_id = result["proposals"][0]["id"]
        accepted = main.accept_proposal(self._request_with_user(), proposal_id)
        self.assertEqual(accepted["status"], "accepted")
        with main.db() as connection:
            self.assertIsNone(connection.execute("SELECT location FROM events WHERE id = ?", (event_id,)).fetchone()[0])

    def test_apply_mutates(self):
        event_id = self._insert_event("event-sport")
        result = self._interpret("明天體育課在攀岩場", {"proposals": [{"operation": "update", "target_type": "event", "target_id": event_id, "patch": {"location": "攀岩場"}}]})
        proposal_id = result["proposals"][0]["id"]
        main.accept_proposal(self._request_with_user(), proposal_id)
        main.apply_proposal(self.user_id, proposal_id)
        with main.db() as connection:
            self.assertEqual(connection.execute("SELECT location FROM events WHERE id = ?", (event_id,)).fetchone()[0], "攀岩場")

    def test_rejected_proposal_changes_nothing(self):
        event_id = self._insert_event("event-sport")
        result = self._interpret("明天體育課在攀岩場", {"proposals": [{"operation": "update", "target_type": "event", "target_id": event_id, "patch": {"location": "攀岩場"}}]})
        proposal_id = result["proposals"][0]["id"]
        rejected = main.reject_proposal(self._request_with_user(), proposal_id)
        self.assertEqual(rejected["status"], "rejected")
        with main.db() as connection:
            self.assertIsNone(connection.execute("SELECT location FROM events WHERE id = ?", (event_id,)).fetchone()[0])

    def test_http_text_flow_accept_then_apply(self):
        event_id = "demo-http-event"
        with main.db() as connection:
            connection.execute(
                "INSERT INTO events(id, user_id, title, date_label, time_label, detail, source_id, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, "demo-user", "體育", (datetime.now().date() + timedelta(days=1)).isoformat(), "10:15–11:05", "", "manual", None),
            )
        output = {
            "proposals": [
                {
                    "operation": "update",
                    "target_type": "event",
                    "target_id": event_id,
                    "patch": {"location": "攀岩場"},
                    "evidence_refs": ["各位明天體育課在攀岩場喔"],
                    "confidence": "high",
                }
            ]
        }
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        import uvicorn

        config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="critical")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/auth/providers", timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("HTTP test server did not start")

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

            def call(method, path, payload=None):
                data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=data,
                    method=method,
                    headers={"Content-Type": "application/json"} if data else {},
                )
                try:
                    with opener.open(request, timeout=5) as response:
                        return response.status, json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as error:
                    self.fail(f"HTTP {error.code} from {path}: {error.read().decode('utf-8')}")

            with patch.object(main, "ai_provider", return_value=DeterministicFakeProvider(output)):
                self.assertEqual(call("POST", "/api/auth/dev-login")[0], 200)
                status, interpreted = call(
                    "POST",
                    "/api/interpret",
                    {"title": "課堂資訊", "body": "各位明天體育課在攀岩場喔", "audience": "個人"},
                )
                self.assertEqual(status, 200)
                proposal = interpreted["proposals"][0]
                proposal_id = proposal["id"]
                self.assertEqual(proposal["operation"], "update")

                accepted_status, accepted = call("POST", f"/api/proposals/{proposal_id}/accept")
                self.assertEqual(accepted_status, 200)
                self.assertEqual(accepted["status"], "accepted")
                applied_status, applied = call("POST", f"/api/proposals/{proposal_id}/apply")
                self.assertEqual(applied_status, 200)
                self.assertEqual(applied["proposal"]["status"], "applied")

            with main.db() as connection:
                location = connection.execute("SELECT location FROM events WHERE id = ?", (event_id,)).fetchone()[0]
            self.assertEqual(location, "攀岩場")
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def _request_with_user(self):
        from starlette.responses import Response

        response = Response()
        main.create_session(self.user_id, response)
        cookie = response.headers["set-cookie"].split(";", 1)[0]
        from starlette.requests import Request

        return Request({"type": "http", "method": "POST", "path": "/api/proposals", "headers": [(b"cookie", cookie.encode())]})


if __name__ == "__main__":
    unittest.main()
