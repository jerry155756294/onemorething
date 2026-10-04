import base64
import io
import json
import os
import struct
import tempfile
import time
import zipfile
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import pytest

from app import main
from app.ai_interpreter import DeterministicFakeProvider, LiteLLMProvider
from app.ingestion import (
    AI_OUTPUT_TOKEN_LIMIT,
    AttachmentStore,
    ExtractionTimeout,
    IngestionPolicy,
    ai_source_budget,
    bounded_attachment,
    bounded_model_image_data_url,
    call_with_timeout,
    image_dimensions,
    inspect_image,
    sniff_media_type,
    truncate_at_text_boundary,
)


def data_url(media_type: str, content: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


def png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


def png_fixture(width: int, height: int) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def user_id() -> str:
    value = f"ingestion-test-{uuid4().hex}"
    with main.db() as connection:
        connection.execute(
            "INSERT INTO users(id, display_name, email, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (value, "Ingestion Test", None, None, main.now_iso(), main.now_iso()),
        )
    return value


class VisionProvider(DeterministicFakeProvider):
    """Records the v21 one-pass visual + intent boundary."""

    def __init__(self, output=None):
        super().__init__(output or {"proposals": []})
        self.interpret_calls = 0
        self.multimodal_calls = 0
        self.model_images = []
        self.interpreted_sources = []

    def interpret(self, source, context):
        self.interpret_calls += 1
        self.interpreted_sources.append(source)
        return DeterministicFakeProvider.interpret(self, source, context)

    def interpret_multimodal(self, source, context, images):
        self.multimodal_calls += 1
        self.interpreted_sources.append(source)
        self.model_images.append(list(images))
        return DeterministicFakeProvider.interpret(self, source, context)

    def interpret_multimodal_with_tools(self, source, context, images, query_tool):
        del query_tool
        return self.interpret_multimodal(source, context, images)


def test_policy_defaults_are_centralized_and_public():
    policy = IngestionPolicy()
    assert policy.max_upload_bytes == 10 * 1024 * 1024
    assert policy.max_attachments_per_capture == 5
    assert policy.max_total_capture_bytes == 25 * 1024 * 1024
    assert policy.model_max_image_pixels == 3_000_000
    assert policy.model_max_image_dimension == 2_800
    assert policy.max_model_images_per_capture == 5
    # Old env/diagnostic names remain read-only aliases during migration.
    assert policy.ocr_max_image_pixels == policy.model_max_image_pixels
    assert policy.public()["max_model_images_per_capture"] == 5
    assert policy.public()["preview_char_limit"] == 2000

def test_model_image_budget_downscales_only_the_transient_copy():
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(output, format="PNG")
    original = data_url("image/png", output.getvalue())
    policy = IngestionPolicy(model_max_image_pixels=1_000_000, model_max_image_dimension=1000)

    prepared = bounded_model_image_data_url(original, policy)
    _, encoded = prepared.split(",", 1)
    resized = Image.open(io.BytesIO(base64.b64decode(encoded)))

    assert prepared.startswith("data:image/png;base64,")
    assert (resized.width, resized.height) == (1000, 666)
    assert resized.width * resized.height <= policy.model_max_image_pixels
    assert prepared != original

def test_normal_text_and_oversized_text_are_bounded(monkeypatch):
    monkeypatch.setenv("OMT_INGEST_MAX_UPLOAD_BYTES", "8")
    uid = user_id()
    provider = DeterministicFakeProvider({"proposals": []})
    small = main.interpret_source(uid, main.NoticeInput(attachments=[{"name": "note.txt", "media_type": "text/plain", "kind": "file", "size": 5, "data_url": data_url("text/plain", b"hello")}]), provider)
    assert small["source"]["attachments"][0]["extraction_status"] == "succeeded"
    large = main.interpret_source(uid, main.NoticeInput(attachments=[{"name": "large.txt", "media_type": "text/plain", "kind": "file", "size": 9, "data_url": data_url("text/plain", b"123456789")}]), provider)
    assert large["processing"]["attachments"][0]["status"] == "too_large"


@pytest.mark.parametrize(("filename", "media_type"), [
    ("note.txt", "text/plain"),
    ("note.md", "text/markdown"),
    ("note.markdown", "text/markdown"),
    ("notes.csv", "text/csv"),
    ("notes.tsv", "text/tab-separated-values"),
])
def test_plain_text_attachment_extensions_enter_the_same_interpretation_source(filename, media_type):
    uid = user_id()
    provider = VisionProvider()
    contents = "期中考在 10 月 15 日上午 9 點，記得帶學生證。"
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "name": filename,
        "media_type": media_type,
        "kind": "file",
        "size": len(contents.encode("utf-8")),
        "data_url": data_url(media_type, contents.encode("utf-8")),
    }]), provider)

    assert result["processing"]["attachments"][0]["status"] == "complete"
    assert result["unsupported_attachments"] == []
    assert provider.interpreted_sources == [contents]
    assert result["proposals"] == []


def test_unreadable_text_encoding_returns_a_user_actionable_reason():
    uid = user_id()
    provider = VisionProvider()
    contents = b"\x81\x30\x81\x30"
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "name": "unreadable.txt",
        "media_type": "text/plain",
        "kind": "file",
        "size": len(contents),
        "data_url": data_url("text/plain", contents),
    }]), provider)

    assert result["processing"]["attachments"][0]["status"] == "unsupported"
    assert result["unsupported_attachments"][0]["extraction_error"] == "unsupported_encoding"
    assert provider.interpreted_sources == []


def test_heic_fixture_is_bounded_and_transiently_converted_for_multimodal_model():
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    pillow_heif.register_heif_opener()
    source = Image.new("RGB", (3200, 2400), "white")
    output = io.BytesIO()
    source.save(output, format="HEIF", quality=85)
    raw_heic = output.getvalue()
    uid = user_id()
    provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "name": "iphone-photo.heic", "media_type": "image/heic", "kind": "image",
        "size": len(raw_heic), "data_url": data_url("image/heic", raw_heic),
    }]), provider)

    assert result["processing"]["attachments"][0]["status"] == "complete"
    assert provider.multimodal_calls == 1
    prepared = provider.model_images[0][0]["data_url"]
    assert prepared.startswith("data:image/jpeg;base64,")
    converted = base64.b64decode(prepared.split(",", 1)[1])
    with Image.open(io.BytesIO(converted)) as model_image:
        assert max(model_image.size) <= 2_800
        assert model_image.width * model_image.height <= 3_000_000

def test_long_line_and_binary_renamed_text_are_not_interpreted(monkeypatch):
    monkeypatch.setenv("OMT_INGEST_MAX_TEXT_LINE_LENGTH", "4")
    uid = user_id()
    provider = VisionProvider()
    long_line = main.interpret_source(uid, main.NoticeInput(attachments=[{"name": "line.txt", "media_type": "text/plain", "kind": "file", "size": 6, "data_url": data_url("text/plain", b"abcdef")}]), provider)
    assert long_line["processing"]["attachments"][0]["status"] == "too_large"
    binary = main.interpret_source(uid, main.NoticeInput(attachments=[{"name": "wrong.txt", "media_type": "text/plain", "kind": "file", "size": 2, "data_url": data_url("text/plain", b"\x00\xff")}]), provider)
    assert binary["processing"]["attachments"][0]["status"] == "unsupported"
    assert provider.interpret_calls == 0


def test_pdf_page_limit_and_malformed_pdf_are_bounded(monkeypatch):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    output = tempfile.SpooledTemporaryFile()
    writer.write(output)
    output.seek(0)
    pdf = output.read()
    monkeypatch.setenv("OMT_INGEST_MAX_PDF_PAGES", "1")
    uid = user_id()
    page_limited = main.interpret_source(uid, main.NoticeInput(attachments=[{"name": "many.pdf", "media_type": "application/pdf", "kind": "pdf", "size": len(pdf), "data_url": data_url("application/pdf", pdf)}]), DeterministicFakeProvider({"proposals": []}))
    assert page_limited["processing"]["attachments"][0]["status"] == "too_large"
    malformed = main.interpret_source(uid, main.NoticeInput(attachments=[{"name": "bad.pdf", "media_type": "application/pdf", "kind": "pdf", "size": 20, "data_url": data_url("application/pdf", b"not a pdf")}]), DeterministicFakeProvider({"proposals": []}))
    assert malformed["processing"]["attachments"][0]["status"] in {"malformed", "extraction_failed"}


def test_scanned_pdf_pages_render_into_single_multimodal_interpretation(monkeypatch):
    from pypdf import PdfWriter

    writer = PdfWriter(); writer.add_blank_page(width=200, height=120)
    output = tempfile.SpooledTemporaryFile(); writer.write(output); output.seek(0); pdf = output.read()
    uid = user_id(); provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "name": "scan.pdf", "media_type": "application/pdf", "kind": "pdf",
        "size": len(pdf), "data_url": data_url("application/pdf", pdf),
    }]), provider)

    assert provider.multimodal_calls == 1
    assert len(provider.model_images[0]) == 1
    attachment = result["source"]["attachments"][0]
    assert attachment["extraction_method"] == "pdf_vision"
    assert attachment["extraction_status"] == "succeeded"
    assert "extracted_text" not in attachment

def test_scanned_pdf_model_images_are_bounded_per_capture(monkeypatch):
    from pypdf import PdfWriter

    monkeypatch.setenv("OMT_INGEST_MAX_MODEL_IMAGES", "2")
    writer = PdfWriter()
    for _ in range(6): writer.add_blank_page(width=200, height=120)
    output = tempfile.SpooledTemporaryFile(); writer.write(output); output.seek(0); pdf = output.read()
    uid = user_id(); provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "name": "six-pages.pdf", "media_type": "application/pdf", "kind": "pdf",
        "size": len(pdf), "data_url": data_url("application/pdf", pdf),
    }]), provider)

    assert provider.multimodal_calls == 1
    assert len(provider.model_images[0]) == 2
    processing = result["processing"]["attachments"][0]
    assert processing["status"] == "partial"
    assert processing["reason"] == "model_image_limit"
    assert result["source"]["attachments"][0]["model_images_skipped"] == 4

def test_image_dimensions_and_corrupt_image_are_checked():
    policy = IngestionPolicy(max_image_pixels=100, max_image_dimension=100)
    path = Path(tempfile.mktemp(suffix=".png"))
    try:
        path.write_bytes(png_header(101, 1))
        assert image_dimensions(path) == (101, 1)
        assert inspect_image(path, policy)["status"] == "too_large"
        path.write_bytes(b"corrupt")
        assert inspect_image(path, policy)["status"] == "malformed"
    finally:
        path.unlink(missing_ok=True)


def test_mime_mismatch_and_unsupported_image_type_are_rejected(tmp_path):
    pdf = tmp_path / "homework.txt"
    pdf.write_bytes(b"%PDF-1.7")
    assert sniff_media_type(pdf, "text/plain", pdf.name) == ("unsupported", "mime_mismatch")
    svg = tmp_path / "drawing.svg"
    svg.write_text("<svg></svg>", encoding="utf-8")
    assert sniff_media_type(svg, "image/svg+xml", svg.name)[0] == "malformed"


def test_capture_count_total_and_mixed_attachment_errors(monkeypatch):
    monkeypatch.setenv("OMT_INGEST_MAX_ATTACHMENTS", "2")
    uid = user_id()
    with pytest.raises(main.IngestionPolicyError):
        main.interpret_source(uid, main.NoticeInput(attachments=[{"name": f"{index}.txt", "size": 0} for index in range(3)]))
    monkeypatch.setenv("OMT_INGEST_MAX_ATTACHMENTS", "5")
    monkeypatch.setenv("OMT_INGEST_MAX_TOTAL_CAPTURE_BYTES", "6")
    result = main.interpret_source(uid, main.NoticeInput(attachments=[
        {"name": "ok.txt", "media_type": "text/plain", "kind": "file", "size": 2, "data_url": data_url("text/plain", b"ok!")},
        {"name": "too.txt", "media_type": "text/plain", "kind": "file", "size": 3, "data_url": data_url("text/plain", b"more")},
    ]), DeterministicFakeProvider({"proposals": []}))
    assert [item["status"] for item in result["processing"]["attachments"]] == ["complete", "too_large"]


def test_duplicate_text_upload_reuses_successful_extraction():
    uid = user_id(); contents = b"10/5 before submit report"
    first = VisionProvider(); second = VisionProvider()
    payload = main.NoticeInput(attachments=[{
        "name": "repeat.txt", "media_type": "text/plain", "kind": "file",
        "size": len(contents), "data_url": data_url("text/plain", contents),
    }])
    one = main.interpret_source(uid, payload, first)
    attachment = one["source"]["attachments"][0]
    two = main.interpret_source(uid, main.NoticeInput(source_id=one["source"]["id"], attachments=[{
        "id": attachment["id"], "name": attachment["name"], "media_type": attachment["media_type"], "kind": "file",
    }]), second)
    assert two["source"]["attachments"][0]["extracted_text"] == contents.decode()
    assert second.interpreted_sources == [contents.decode()]

def test_ai_budget_partial_state_and_timeout(monkeypatch):
    monkeypatch.setenv("OMT_INGEST_AI_CONTEXT_CHARS", "20")
    uid = user_id()
    provider = VisionProvider("x" * 100)
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{"name": "note.txt", "media_type": "text/plain", "kind": "file", "size": 100, "data_url": data_url("text/plain", b"x" * 100)}]), provider)
    assert result["processing"]["ai"]["status"] == "too_large"
    assert provider.interpret_calls == 0
    with pytest.raises(ExtractionTimeout):
        call_with_timeout(lambda: time.sleep(0.2), 0.01)


def test_capture_url_is_detected_only_from_text_body(monkeypatch):
    uid = user_id(); provider = VisionProvider()
    image = png_fixture(20, 20)
    called = []
    monkeypatch.setattr(main, "_crawl_capture_page", lambda value: called.append(value) or {"status": "none", "text": ""})
    main.interpret_source(uid, main.NoticeInput(attachments=[{
        "name": "https-example.png", "media_type": "image/png", "kind": "image",
        "size": len(image), "data_url": data_url("image/png", image),
    }]), provider)
    assert provider.multimodal_calls == 1
    assert called == [""]

def test_capture_url_uses_proxy_urls_contract_and_reads_page_content(monkeypatch):
    target_url = "https://example.edu/classes"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return json.dumps([{
                "page_content": "課程資訊與重要日期",
                "metadata": {"title": "課程頁面", "source": target_url},
            }]).encode("utf-8")[:limit]

    class FakeOpener:
        def __init__(self):
            self.requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            assert timeout == 20
            return FakeResponse()

    opener = FakeOpener()
    monkeypatch.setenv("OMT_CRAWL4AI_PROXY_URL", "http://crawl4ai-proxy:8000")
    monkeypatch.setattr(main, "_validate_capture_url", lambda url: url)
    monkeypatch.setattr(main.urllib.request, "build_opener", lambda *_args: opener)

    result = main._crawl_capture_page(f"請整理 {target_url}")

    assert result == {
        "status": "complete",
        "url": target_url,
        "title": "課程頁面",
        "text": "課程資訊與重要日期",
    }
    assert json.loads(opener.requests[0].data) == {"urls": [target_url]}


def test_capture_url_succeeds_after_one_safe_proxy_redirect(monkeypatch):
    target_url = "https://example.edu/classes"
    redirected_url = "https://courses.example.edu/current"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return json.dumps([{
                "page_content": "Course schedule",
                "metadata": {"title": "Current courses", "source": redirected_url},
            }]).encode("utf-8")[:limit]

    class FakeOpener:
        def __init__(self):
            self.requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise main.urllib.error.HTTPError(
                    "http://crawl4ai-proxy:8000/crawl", 307, "Temporary Redirect",
                    {"Location": redirected_url}, None,
                )
            return FakeResponse()

    opener = FakeOpener()
    monkeypatch.setenv("OMT_CRAWL4AI_PROXY_URL", "http://crawl4ai-proxy:8000")
    monkeypatch.setattr(main, "_validate_capture_url", lambda url: url)
    monkeypatch.setattr(main.urllib.request, "build_opener", lambda *_args: opener)

    result = main._crawl_capture_page(f"Please read {target_url}")

    assert result == {
        "status": "complete",
        "url": target_url,
        "final_url": redirected_url,
        "title": "Current courses",
        "text": "Course schedule",
    }
    assert [json.loads(request.data) for request in opener.requests] == [
        {"urls": [target_url]},
        {"urls": [redirected_url]},
    ]


def test_capture_url_rejects_an_unsafe_crawler_final_url(monkeypatch):
    target_url = "https://example.edu/classes"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return json.dumps([{
                "page_content": "Must not reach interpretation",
                "metadata": {"source": "http://127.0.0.1/admin"},
            }]).encode("utf-8")[:limit]

    class FakeOpener:
        def open(self, _request, timeout):
            return FakeResponse()

    def validate(url):
        if url != target_url:
            raise ValueError("unsafe final URL")
        return url

    monkeypatch.setenv("OMT_CRAWL4AI_PROXY_URL", "http://crawl4ai-proxy:8000")
    monkeypatch.setattr(main, "_validate_capture_url", validate)
    monkeypatch.setattr(main.urllib.request, "build_opener", lambda *_args: FakeOpener())

    assert main._crawl_capture_page(target_url) == {
        "status": "failed",
        "reason": "unsafe_final_url",
        "url": target_url,
    }


def test_url_only_capture_uses_source_url_for_fetch_and_interpretation(monkeypatch):
    target_url = "https://example.edu/timetable"
    crawl_inputs = []
    monkeypatch.setattr(main, "_crawl_capture_page", lambda value: crawl_inputs.append(value) or {
        "status": "complete", "url": target_url, "title": "課表", "text": "週一 09:00～10:00 數學",
    })
    uid = user_id()
    provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(source_url=target_url), provider)
    assert crawl_inputs == [target_url]
    assert provider.interpret_calls == 1
    assert result["processing"]["webpage"]["status"] == "complete"


def test_url_only_capture_does_not_interpret_a_bare_url_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(main, "_crawl_capture_page", lambda _value: {"status": "unavailable", "reason": "crawler_not_configured"})
    uid = user_id()
    provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(source_url="https://example.edu/timetable"), provider)
    assert provider.interpret_calls == 0
    assert result["processing"]["webpage"]["status"] == "unavailable"
    assert result["processing"]["ai"]["status"] == "not_requested"


def test_url_content_and_attachment_text_share_ai_budget(monkeypatch):
    monkeypatch.setenv("OMT_INGEST_AI_CONTEXT_CHARS", "5200")
    monkeypatch.setattr(main, "_validate_capture_url", lambda url: url)
    monkeypatch.setattr(main, "_crawl_capture_page", lambda body: {
        "status": "complete", "url": main._url_from_capture_body(body), "text": "P" * 20_000,
    })
    uid = user_id()
    provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(
        body="https://example.edu/classes",
        attachments=[{"name": "notes.txt", "media_type": "text/plain", "kind": "file", "size": 8, "data_url": data_url("text/plain", b"filetext")}],
    ), provider)
    assert provider.interpret_calls == 1
    assert result["processing"]["ai"]["processed_chars"] <= result["processing"]["ai"]["limit"]
    assert result["processing"]["ai"]["context_truncated"] is True


def _docx_fixture(text: str, image: bytes | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>")
        archive.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
        if image: archive.writestr("word/media/image1.png", image)
    return output.getvalue()


def _odf_fixture(text: str, image: bytes | None, media_type: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", media_type)
        archive.writestr("content.xml", f'<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text><text:p>{text}</text:p></office:text></office:body></office:document-content>')
        if image: archive.writestr("Pictures/image1.png", image)
    return output.getvalue()


def _odt_fixture(text: str, image: bytes | None = None) -> bytes:
    return _odf_fixture(text, image, "application/vnd.oasis.opendocument.text")


def _ods_fixture(text: str, image: bytes | None = None) -> bytes:
    return _odf_fixture(text, image, "application/vnd.oasis.opendocument.spreadsheet")


def _odp_fixture(text: str, image: bytes | None = None) -> bytes:
    return _odf_fixture(text, image, "application/vnd.oasis.opendocument.presentation")


@pytest.mark.parametrize(("name", "media_type", "factory", "expected_method"), [
    ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", _docx_fixture, "docx_text+vision"),
    ("notes.odt", "application/vnd.oasis.opendocument.text", _odt_fixture, "odf_text+vision"),
    ("budget.ods", "application/vnd.oasis.opendocument.spreadsheet", _ods_fixture, "odf_text+vision"),
    ("slides.odp", "application/vnd.oasis.opendocument.presentation", _odp_fixture, "odf_text+vision"),
])
def test_office_documents_feed_extracted_text_and_embedded_images_to_one_model_call(name, media_type, factory, expected_method):
    uid = user_id(); image = png_fixture(24, 24); raw = factory("10/5要交報告", image); provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "name": name, "media_type": media_type, "kind": "file", "size": len(raw), "data_url": data_url(media_type, raw),
    }]), provider)
    assert provider.multimodal_calls == 1
    assert provider.interpreted_sources == ["10/5要交報告"]
    assert len(provider.model_images[0]) == 1
    attachment = result["source"]["attachments"][0]
    assert attachment["extracted_text"] == "10/5要交報告"
    assert attachment["extraction_method"] == expected_method
    assert attachment["extraction_status"] == "succeeded"


def test_preview_is_bounded_and_temp_cleanup_is_repeatable(tmp_path):
    policy = IngestionPolicy(preview_char_limit=4)
    bounded = bounded_attachment({"name": "x.txt", "extracted_text": "123456789"}, policy)
    assert bounded["preview_text"] == "1234"
    assert bounded["preview_truncated"] is True
    store = AttachmentStore(tmp_path, ttl_seconds=1)
    stale = tmp_path / "omt-stale"
    stale.write_bytes(b"x")
    old = time.time() - 10
    os.utime(stale, (old, old))
    assert store.cleanup() == 1
    assert store.cleanup() == 0


def test_ai_budget_accounts_for_context():
    policy = IngestionPolicy(ai_context_chars=100, ai_context_token_budget=100)
    assert ai_source_budget("x" * 1000, {"events": []}, policy) == 0


def test_ai_budget_uses_utf8_bytes_for_multilingual_source():
    source = "課" * 10_000
    context = {"events": []}
    policy = IngestionPolicy(ai_context_chars=60_000, ai_context_token_budget=12_000)

    char_budget = ai_source_budget(source, context, policy)
    remaining_bytes = 12_000 - 4_096 - AI_OUTPUT_TOKEN_LIMIT - len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    assert 0 < char_budget < len(source)
    assert len(source[:char_budget].encode("utf-8")) <= remaining_bytes


def test_long_text_truncates_at_paragraph_boundary_with_hard_limit_fallback():
    assert truncate_at_text_boundary("Header\n\nFirst paragraph\n\nSecond paragraph", 24) == "Header\n\nFirst paragraph"
    assert truncate_at_text_boundary("x" * 100, 12) == "x" * 12
    assert truncate_at_text_boundary("anything", 0) == ""


def test_interpretation_uses_fixed_output_token_limit():
    provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test", "vision-model")
    with patch.object(provider, "_complete", return_value='{"events":[]}') as complete:
        assert provider.interpret("10/5要交報告", {"current_datetime": "2026-10-03T12:00:00+08:00", "timezone": "Asia/Taipei"}) == []
    assert complete.call_args.kwargs["max_tokens"] == 4096

def test_capture_upload_id_is_consumed_by_single_multimodal_interpretation_without_client_data_url():
    uid = user_id(); content = png_fixture(48, 48)
    content_hash = __import__("hashlib").sha256(content).hexdigest(); storage_key = main.ATTACHMENT_STORE.put(content, content_hash)
    upload_id = f"upload-{uuid4().hex[:12]}"; expires_at = (main.datetime.now(main.timezone.utc) + main.timedelta(hours=1)).isoformat()
    with main.db() as connection:
        connection.execute("""INSERT INTO capture_uploads(id,user_id,filename,mime_type,kind,size,content_hash,storage_key,created_at,expires_at,consumed_at)
            VALUES (?,?,?,'image/png','image',?,?,?,?,?,NULL)""", (upload_id, uid, "capture.png", len(content), content_hash, storage_key, main.now_iso(), expires_at))
    provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "upload_id": upload_id, "name": "capture.png", "media_type": "image/png", "kind": "image", "size": len(content),
    }]), provider)
    assert provider.multimodal_calls == 1
    assert len(provider.model_images[0]) == 1
    assert result["processing"]["ai"]["status"] == "complete"
    with main.db() as connection:
        assert connection.execute("SELECT consumed_at FROM capture_uploads WHERE id=?", (upload_id,)).fetchone()[0]

def test_uploaded_large_png_reaches_one_litellm_multimodal_call_without_preprovider_short_circuit():
    uid = user_id(); content = png_fixture(3000, 2000)
    content_hash = __import__("hashlib").sha256(content).hexdigest(); storage_key = main.ATTACHMENT_STORE.put(content, content_hash)
    upload_id = f"upload-large-{uuid4().hex[:12]}"; expires_at = (main.datetime.now(main.timezone.utc) + main.timedelta(hours=1)).isoformat()
    with main.db() as connection:
        connection.execute("""INSERT INTO capture_uploads(id,user_id,filename,mime_type,kind,size,content_hash,storage_key,created_at,expires_at,consumed_at)
            VALUES (?,?,?,'image/png','image',?,?,?,?,?,NULL)""", (upload_id, uid, "large.png", len(content), content_hash, storage_key, main.now_iso(), expires_at))
    provider = LiteLLMProvider("http://127.0.0.1:4000/v1", "test", "vision-model", "legacy-ocr-model")
    calls=[]
    def chat(model, messages, **kwargs):
        calls.append((model, messages, kwargs)); return {"content": '{"events":[]}'}
    with patch.object(provider, "_chat_message", side_effect=chat):
        result = main.interpret_source(uid, main.NoticeInput(attachments=[{
            "upload_id": upload_id, "name": "large.png", "media_type": "image/png", "kind": "image", "size": len(content),
        }]), provider)
    assert result["processing"]["ai"]["status"] == "complete"
    assert len(calls) == 1
    assert calls[0][0] == "vision-model"
    image_url = calls[0][1][1]["content"][1]["image_url"]["url"]
    _, encoded = image_url.split(",", 1)
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as resized:
        assert resized.width * resized.height <= IngestionPolicy().model_max_image_pixels

def test_uploaded_visual_only_image_is_valid_and_dispatches_model():
    uid = user_id(); content = png_fixture(32, 32)
    content_hash = __import__("hashlib").sha256(content).hexdigest(); storage_key = main.ATTACHMENT_STORE.put(content, content_hash)
    upload_id = f"upload-visual-{uuid4().hex[:12]}"; expires_at = (main.datetime.now(main.timezone.utc) + main.timedelta(hours=1)).isoformat()
    with main.db() as connection:
        connection.execute("""INSERT INTO capture_uploads(id,user_id,filename,mime_type,kind,size,content_hash,storage_key,created_at,expires_at,consumed_at)
            VALUES (?,?,?,'image/png','image',?,?,?,?,?,NULL)""", (upload_id, uid, "visual.png", len(content), content_hash, storage_key, main.now_iso(), expires_at))
    provider = VisionProvider()
    result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        "upload_id": upload_id, "name": "visual.png", "media_type": "image/png", "kind": "image", "size": len(content),
    }]), provider)
    assert provider.multimodal_calls == 1
    assert result["processing"]["ai"]["status"] == "complete"
    assert result["source"]["attachments"][0]["extraction_method"] == "vision"

def test_uploaded_image_model_failure_is_not_returned_as_empty_http_200_semantics():
    from app.ai_interpreter import AIProviderUnavailable
    uid = user_id(); content = png_fixture(32, 32)
    content_hash = __import__("hashlib").sha256(content).hexdigest(); storage_key = main.ATTACHMENT_STORE.put(content, content_hash)
    upload_id = f"upload-fail-{uuid4().hex[:12]}"; expires_at = (main.datetime.now(main.timezone.utc) + main.timedelta(hours=1)).isoformat()
    with main.db() as connection:
        connection.execute("""INSERT INTO capture_uploads(id,user_id,filename,mime_type,kind,size,content_hash,storage_key,created_at,expires_at,consumed_at)
            VALUES (?,?,?,'image/png','image',?,?,?,?,?,NULL)""", (upload_id, uid, "fail.png", len(content), content_hash, storage_key, main.now_iso(), expires_at))
    class FailingVision(VisionProvider):
        def interpret_multimodal_with_tools(self, source, context, images, query_tool):
            raise AIProviderUnavailable("vision unavailable")
    with pytest.raises(AIProviderUnavailable):
        main.interpret_source(uid, main.NoticeInput(attachments=[{
            "upload_id": upload_id, "name": "fail.png", "media_type": "image/png", "kind": "image", "size": len(content),
        }]), FailingVision())

def test_uploaded_capture_never_returns_http_success_when_ai_dispatch_is_skipped(monkeypatch):
    monkeypatch.setenv("OMT_INGEST_MAX_TEXT_LINE_LENGTH", "100")
    uid = user_id(); content = png_fixture(32, 32)
    content_hash = __import__("hashlib").sha256(content).hexdigest(); storage_key = main.ATTACHMENT_STORE.put(content, content_hash)
    upload_id = f"upload-no-dispatch-{uuid4().hex[:12]}"; expires_at = (main.datetime.now(main.timezone.utc) + main.timedelta(hours=1)).isoformat()
    with main.db() as connection:
        connection.execute("""INSERT INTO capture_uploads(id,user_id,filename,mime_type,kind,size,content_hash,storage_key,created_at,expires_at,consumed_at)
            VALUES (?,?,?,'image/png','image',?,?,?,?,?,NULL)""", (upload_id, uid, "no-dispatch.png", len(content), content_hash, storage_key, main.now_iso(), expires_at))
    provider = VisionProvider()
    with pytest.raises(main.IngestionPolicyError) as exc:
        main.interpret_source(uid, main.NoticeInput(body='x' * 101, attachments=[{
            'upload_id': upload_id, 'name': 'no-dispatch.png', 'media_type': 'image/png', 'kind': 'image', 'size': len(content),
        }]), provider)
    assert exc.value.code == 'capture_ai_not_dispatched'
    assert provider.multimodal_calls == 0

def test_reopened_visual_source_reuses_raw_blob_and_dispatches_again_while_available():
    uid = user_id(); content = png_fixture(48, 48); first = VisionProvider()
    first_result = main.interpret_source(uid, main.NoticeInput(attachments=[{
        'name': 'repeat.png', 'media_type': 'image/png', 'kind': 'image', 'size': len(content), 'data_url': data_url('image/png', content),
    }]), first)
    assert first.multimodal_calls == 1
    attachment = first_result['source']['attachments'][0]
    second = VisionProvider()
    second_result = main.interpret_source(uid, main.NoticeInput(source_id=first_result['source']['id'], attachments=[{
        'id': attachment['id'], 'name': attachment['name'], 'media_type': attachment['media_type'], 'kind': 'image',
    }]), second)
    assert second.multimodal_calls == 1
    assert second_result['processing']['ai']['status'] == 'complete'

