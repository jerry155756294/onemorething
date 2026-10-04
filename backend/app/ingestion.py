"""Bounded, content-aware ingestion for Capture attachments.

The JSON Capture API is intentionally kept small for this MVP. Uploaded bytes
are materialized into a short-lived private temp file, inspected, extracted
within policy, and removed in a finally block. Raw bytes are never persisted in
the Source record.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import multiprocessing
import os
import re
import struct
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

AI_OUTPUT_TOKEN_LIMIT = 2048
AI_PROMPT_OVERHEAD_TOKEN_BUDGET = 4096


LOGGER = logging.getLogger("one_more_thing.ingestion")
_HEIF_OPENER_REGISTERED = False
_HEIF_OPENER_LOCK = threading.Lock()


def _register_heif_opener() -> None:
    global _HEIF_OPENER_REGISTERED
    if _HEIF_OPENER_REGISTERED:
        return
    with _HEIF_OPENER_LOCK:
        if _HEIF_OPENER_REGISTERED:
            return
        try:
            from pillow_heif import register_heif_opener
        except ModuleNotFoundError:
            # HEIF support is optional at runtime. Standard PNG/JPEG vision must
            # keep working even if the HEIF plugin is not installed; actual
            # HEIC/HEIF input will still fail cleanly when Pillow opens it.
            return

        register_heif_opener()
        _HEIF_OPENER_REGISTERED = True


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class IngestionPolicy:
    """All Capture resource limits in one configurable policy."""

    max_upload_bytes: int = 10 * 1024 * 1024
    max_attachments_per_capture: int = 5
    max_total_capture_bytes: int = 25 * 1024 * 1024
    max_text_chars: int = 200_000
    max_text_line_length: int = 20_000
    max_pdf_pages: int = 25
    max_pdf_extracted_chars: int = 200_000
    max_image_pixels: int = 20_000_000
    max_image_dimension: int = 8_000
    model_max_image_pixels: int = 3_000_000
    model_max_image_dimension: int = 2_800
    max_model_images_per_capture: int = 5
    max_document_entries: int = 2_000
    max_document_uncompressed_bytes: int = 40 * 1024 * 1024
    extraction_timeout_seconds: float = 8.0
    ai_timeout_seconds: float = 60.0
    ai_context_chars: int = 60_000
    ai_context_token_budget: int = 16_000
    preview_char_limit: int = 2_000
    temp_ttl_seconds: int = 3_600

    @classmethod
    def from_environment(cls) -> "IngestionPolicy":
        return cls(
            max_upload_bytes=_env_int("OMT_INGEST_MAX_UPLOAD_BYTES", cls.max_upload_bytes),
            max_attachments_per_capture=_env_int("OMT_INGEST_MAX_ATTACHMENTS", cls.max_attachments_per_capture),
            max_total_capture_bytes=_env_int("OMT_INGEST_MAX_TOTAL_CAPTURE_BYTES", cls.max_total_capture_bytes),
            max_text_chars=_env_int("OMT_INGEST_MAX_TEXT_CHARS", cls.max_text_chars),
            max_text_line_length=_env_int("OMT_INGEST_MAX_TEXT_LINE_LENGTH", cls.max_text_line_length),
            max_pdf_pages=_env_int("OMT_INGEST_MAX_PDF_PAGES", cls.max_pdf_pages),
            max_pdf_extracted_chars=_env_int("OMT_INGEST_MAX_PDF_EXTRACTED_CHARS", cls.max_pdf_extracted_chars),
            max_image_pixels=_env_int("OMT_INGEST_MAX_IMAGE_PIXELS", cls.max_image_pixels),
            max_image_dimension=_env_int("OMT_INGEST_MAX_IMAGE_DIMENSION", cls.max_image_dimension),
            model_max_image_pixels=_env_int("OMT_INGEST_MODEL_IMAGE_PIXELS", _env_int("OMT_INGEST_OCR_MAX_IMAGE_PIXELS", cls.model_max_image_pixels)),
            model_max_image_dimension=_env_int("OMT_INGEST_MODEL_IMAGE_DIMENSION", _env_int("OMT_INGEST_OCR_MAX_IMAGE_DIMENSION", cls.model_max_image_dimension)),
            max_model_images_per_capture=_env_int("OMT_INGEST_MAX_MODEL_IMAGES", _env_int("OMT_INGEST_MAX_OCR_REQUESTS", cls.max_model_images_per_capture)),
            max_document_entries=_env_int("OMT_INGEST_MAX_DOCUMENT_ENTRIES", cls.max_document_entries),
            max_document_uncompressed_bytes=_env_int("OMT_INGEST_MAX_DOCUMENT_UNCOMPRESSED_BYTES", cls.max_document_uncompressed_bytes),
            extraction_timeout_seconds=_env_float("OMT_INGEST_EXTRACTION_TIMEOUT_SECONDS", cls.extraction_timeout_seconds),
            ai_timeout_seconds=_env_float("OMT_INGEST_AI_TIMEOUT_SECONDS", cls.ai_timeout_seconds),
            ai_context_chars=_env_int("OMT_INGEST_AI_CONTEXT_CHARS", cls.ai_context_chars),
            ai_context_token_budget=_env_int("OMT_INGEST_AI_CONTEXT_TOKENS", cls.ai_context_token_budget),
            preview_char_limit=_env_int("OMT_INGEST_PREVIEW_CHARS", cls.preview_char_limit),
            temp_ttl_seconds=_env_int("OMT_INGEST_TEMP_TTL_SECONDS", cls.temp_ttl_seconds),
        )

    # Read-only aliases keep older deployment diagnostics working while the
    # runtime no longer has a separate OCR stage/model.
    @property
    def ocr_max_image_pixels(self) -> int:
        return self.model_max_image_pixels

    @property
    def ocr_max_image_dimension(self) -> int:
        return self.model_max_image_dimension

    @property
    def max_ocr_requests_per_capture(self) -> int:
        return self.max_model_images_per_capture

    @property
    def max_request_body_bytes(self) -> int:
        # Base64 expands raw bytes by about 4/3. Keep headroom for JSON fields.
        return (self.max_total_capture_bytes * 4 // 3) + 512 * 1024

    def public(self) -> dict[str, int | float]:
        return {
            "max_upload_bytes": self.max_upload_bytes,
            "max_attachments_per_capture": self.max_attachments_per_capture,
            "max_total_capture_bytes": self.max_total_capture_bytes,
            "max_text_chars": self.max_text_chars,
            "max_text_line_length": self.max_text_line_length,
            "max_pdf_pages": self.max_pdf_pages,
            "max_pdf_extracted_chars": self.max_pdf_extracted_chars,
            "max_image_pixels": self.max_image_pixels,
            "max_image_dimension": self.max_image_dimension,
            "model_max_image_pixels": self.model_max_image_pixels,
            "model_max_image_dimension": self.model_max_image_dimension,
            "max_model_images_per_capture": self.max_model_images_per_capture,
            "ocr_max_image_pixels": self.model_max_image_pixels,
            "ocr_max_image_dimension": self.model_max_image_dimension,
            "max_ocr_requests_per_capture": self.max_model_images_per_capture,
            "max_document_entries": self.max_document_entries,
            "max_document_uncompressed_bytes": self.max_document_uncompressed_bytes,
            "extraction_timeout_seconds": self.extraction_timeout_seconds,
            "ai_timeout_seconds": self.ai_timeout_seconds,
            "ai_context_chars": self.ai_context_chars,
            "ai_context_token_budget": self.ai_context_token_budget,
            "preview_char_limit": self.preview_char_limit,
            "temp_ttl_seconds": self.temp_ttl_seconds,
        }


class IngestionPolicyError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 413, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class ExtractionTimeout(Exception):
    pass


@dataclass(frozen=True)
class StoredAttachment:
    path: Path
    size: int
    content_hash: str
    declared_media_type: str


class AttachmentStore:
    """Short-lived raw storage; no raw upload survives a processing call."""

    def __init__(self, root: Path, ttl_seconds: int):
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> int:
        cutoff = time.time() - self.ttl_seconds
        removed = 0
        for path in self.root.glob("omt-*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                LOGGER.warning("attachment_temp_cleanup_failed", extra={"path": str(path)})
        return removed

    def materialize(self, data_url: str, *, attachment_id: str, max_bytes: int) -> StoredAttachment:
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            raise IngestionPolicyError("missing_data", "無法讀取檔案", status_code=422)
        header, separator, payload = data_url.partition(",")
        if not separator or ";base64" not in header.lower():
            raise IngestionPolicyError("unsupported_encoding", "不支援這種檔案", status_code=422)
        declared_media_type = header[5:].split(";", 1)[0].strip().lower() or "application/octet-stream"
        if len(payload) > ((max_bytes + 2) * 4 // 3) + 32:
            raise IngestionPolicyError("file_too_large", "檔案太大", details={"max_upload_bytes": max_bytes})

        descriptor, raw_path = tempfile.mkstemp(prefix=f"omt-{attachment_id}-", suffix=".upload", dir=self.root)
        os.close(descriptor)
        path = Path(raw_path)
        digest = hashlib.sha256()
        written = 0
        carry = ""
        try:
            with path.open("wb") as handle:
                for offset in range(0, len(payload), 16_384):
                    chunk = re.sub(r"\s+", "", payload[offset : offset + 16_384])
                    if not chunk:
                        continue
                    data = carry + chunk
                    usable = len(data) - (len(data) % 4)
                    carry = data[usable:]
                    if usable:
                        decoded = base64.b64decode(data[:usable], validate=True)
                        written += len(decoded)
                        if written > max_bytes:
                            raise IngestionPolicyError("file_too_large", "檔案太大", details={"max_upload_bytes": max_bytes})
                        handle.write(decoded)
                        digest.update(decoded)
                if carry:
                    decoded = base64.b64decode(carry, validate=True)
                    written += len(decoded)
                    if written > max_bytes:
                        raise IngestionPolicyError("file_too_large", "檔案太大", details={"max_upload_bytes": max_bytes})
                    handle.write(decoded)
                    digest.update(decoded)
        except (binascii.Error, ValueError) as error:
            raise IngestionPolicyError("malformed", "無法讀取檔案", status_code=422) from error
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return StoredAttachment(path, written, digest.hexdigest(), declared_media_type)

    @staticmethod
    def remove(stored: StoredAttachment | None) -> None:
        if stored is None:
            return
        try:
            stored.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.warning("attachment_temp_remove_failed", extra={"path": str(stored.path)})


def _extension_category(name: str) -> str | None:
    suffix = Path(name or "").suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml"}:
        return "text"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "word"
    if suffix in {".odt", ".ods", ".odp"}:
        return "odf"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"}:
        return "image"
    return None


def _image_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 16 and data[4:8] == b"ftyp":
        box_size = int.from_bytes(data[:4], "big")
        brand_data = data[8:min(len(data), box_size)]
        brands = {brand_data[:4]}
        brands.update(brand_data[index:index + 4] for index in range(8, len(brand_data) - 3, 4))
        heic_brands = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
        if brands & heic_brands:
            return "image/heic"
        if brands & {b"mif1", b"msf1"}:
            return "image/heif"
    return None


def _is_heif_media_type(media_type: str) -> bool:
    return media_type in {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}


_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_ODF_MEDIA_TYPES = {
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
}


def _zip_document_media_type(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names and "[Content_Types].xml" in names:
                return _DOCX_MEDIA_TYPE
            if "content.xml" in names and "mimetype" in names:
                media_type = archive.read("mimetype")[:160].decode("ascii", errors="ignore").strip()
                if media_type in _ODF_MEDIA_TYPES:
                    return media_type
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return None
    return None


def sniff_media_type(path: Path, declared_media_type: str, name: str) -> tuple[str, str | None]:
    with path.open("rb") as handle:
        sample = handle.read(65_536)
    declared = (declared_media_type or "application/octet-stream").lower().split(";", 1)[0]
    extension = _extension_category(name)
    if sample.startswith(b"%PDF-"):
        detected = "application/pdf"
    else:
        detected = _image_kind(sample)
    if detected:
        detected_category = "image" if detected.startswith("image/") else "pdf"
        if declared.startswith("text/") or extension == "text":
            return "unsupported", "mime_mismatch"
        if declared.startswith("image/") and declared not in {detected, "image/*"} and not (_is_heif_media_type(declared) and _is_heif_media_type(detected)):
            return "unsupported", "mime_mismatch"
        if declared == "application/pdf" and detected_category != "pdf":
            return "unsupported", "mime_mismatch"
        return detected_category, detected

    if sample.startswith(b"PK\x03\x04"):
        document_media_type = _zip_document_media_type(path)
        if document_media_type:
            detected_extension = "word" if document_media_type == _DOCX_MEDIA_TYPE else "odf"
            if declared.startswith("text/") or declared.startswith("image/") or declared == "application/pdf" or extension in {"text", "image", "pdf"}:
                return "unsupported", "mime_mismatch"
            if extension in {"word", "odf"} and extension != detected_extension:
                return "unsupported", "mime_mismatch"
            if declared not in {"", "application/octet-stream", "application/zip", document_media_type} and (declared == _DOCX_MEDIA_TYPE or declared in _ODF_MEDIA_TYPES):
                return "unsupported", "mime_mismatch"
            return "document", document_media_type
        if extension in {"word", "odf"} or declared == _DOCX_MEDIA_TYPE or declared in _ODF_MEDIA_TYPES:
            return "malformed", "content_signature"
        return "unsupported", "binary_content"

    if b"\x00" in sample or sum(byte < 9 or (13 < byte < 32) for byte in sample) > max(32, len(sample) // 10):
        return "unsupported", "binary_content"
    if declared.startswith("image/") or declared == "application/pdf" or declared == _DOCX_MEDIA_TYPE or declared in _ODF_MEDIA_TYPES or extension in {"image", "pdf", "word", "odf"}:
        return "malformed", "content_signature"
    return "text", None


def _decode_text(path: Path, max_chars: int, max_line_length: int) -> dict[str, Any]:
    with path.open("rb") as handle:
        prefix = handle.read(4)
    if prefix.startswith(b"\xff\xfe") or prefix.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    elif prefix.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"

    def consume(codec: str) -> dict[str, Any]:
        decoder = __import__("codecs").getincrementaldecoder(codec)(errors="strict")
        parts: list[str] = []
        total = 0
        line_length = 0
        status = "complete"
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(16_384)
                if not chunk:
                    break
                decoded = decoder.decode(chunk, final=False)
                for char in decoded:
                    if char == "\n":
                        line_length = 0
                    else:
                        line_length += 1
                    if line_length > max_line_length:
                        status = "too_large"
                        break
                    if total < max_chars:
                        parts.append(char)
                        total += 1
                    else:
                        status = "partial"
                if status == "too_large":
                    break
            if status != "too_large":
                tail = decoder.decode(b"", final=True)
                for char in tail:
                    if char == "\n":
                        line_length = 0
                    else:
                        line_length += 1
                    if line_length > max_line_length:
                        status = "too_large"
                        break
                    if total < max_chars:
                        parts.append(char)
                        total += 1
                    else:
                        status = "partial"
        text = "".join(parts)
        return {
            "status": status,
            "encoding": codec,
            "text": text,
            "processed_chars": len(text),
            "total_chars": len(text) if status == "complete" else None,
            "total_chars_known": status == "complete",
            "reason": "line_too_long" if status == "too_large" else ("text_char_limit" if status == "partial" else None),
        }

    try:
        return consume(encoding)
    except UnicodeDecodeError:
        if encoding != "utf-8":
            return {"status": "unsupported", "reason": "unsupported_encoding", "text": "", "processed_chars": 0, "total_chars": None, "total_chars_known": False}
        try:
            return consume("cp950")
        except (UnicodeDecodeError, LookupError):
            return {"status": "unsupported", "reason": "unsupported_encoding", "text": "", "processed_chars": 0, "total_chars": None, "total_chars_known": False}


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return struct.unpack("<HH", data[6:10])
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        marker = data[index] if index < len(data) else 0
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[index : index + 2])[0]
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
            if segment_length >= 7:
                height, width = struct.unpack(">HH", data[index + 3 : index + 7])
                return width, height
        index += segment_length
    return None


def image_dimensions(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as handle:
        data = handle.read(1_048_576)
    dimensions = _png_dimensions(data) or _gif_dimensions(data) or _jpeg_dimensions(data) or (
        struct.unpack("<II", data[18:26]) if data.startswith(b"BM") and len(data) >= 26 else None
    )
    if dimensions:
        return dimensions
    if not _image_kind(data):
        return None
    try:
        _register_heif_opener()
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError, RuntimeError):
        return None


def inspect_image(path: Path, policy: IngestionPolicy) -> dict[str, Any]:
    dimensions = image_dimensions(path)
    if not dimensions or dimensions[0] <= 0 or dimensions[1] <= 0:
        return {"status": "malformed", "reason": "image_dimensions"}
    width, height = dimensions
    if width > policy.max_image_dimension or height > policy.max_image_dimension or width * height > policy.max_image_pixels:
        return {
            "status": "too_large",
            "reason": "image_dimensions",
            "width": width,
            "height": height,
            "pixels": width * height,
        }
    return {"status": "complete", "width": width, "height": height, "pixels": width * height}


def bounded_model_image_data_url(data_url: str, policy: IngestionPolicy) -> str:
    """Downsample only the transient multimodal copy; keep the stored original untouched."""
    if not isinstance(data_url, str) or "," not in data_url:
        raise ValueError("model image data URL is invalid")
    header, encoded = data_url.split(",", 1)
    normalized_header = header.lower()
    if not normalized_header.startswith(("data:image/", "data:application/octet-stream")) or ";base64" not in normalized_header:
        raise ValueError("model image data URL is invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
        _register_heif_opener()
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("model image dimensions are invalid")
            if width > policy.max_image_dimension or height > policy.max_image_dimension or width * height > policy.max_image_pixels:
                raise ValueError("model image exceeds the decoded image limits")
            image.seek(0)
            image.load()
            scale = min(
                1.0,
                policy.model_max_image_dimension / width,
                policy.model_max_image_dimension / height,
                (policy.model_max_image_pixels / (width * height)) ** 0.5,
            )
            is_heif = image.format in {"HEIF", "HEIC"} or normalized_header.split(";", 1)[0] in {"data:image/heic", "data:image/heif", "data:image/heic-sequence", "data:image/heif-sequence"}
            requires_jpeg = is_heif or image.format not in {"JPEG", "PNG"}
            if scale >= 1.0 and not requires_jpeg:
                return data_url
            size = (max(1, int(width * scale)), max(1, int(height * scale)))
            if requires_jpeg:
                resized = image.convert("RGB")
                if size != (width, height):
                    resized = resized.resize(size, Image.Resampling.LANCZOS)
                max_encoded_bytes = min(policy.max_upload_bytes, 8 * 1024 * 1024)
                while True:
                    for quality in (88, 78, 68):
                        buffer = io.BytesIO()
                        resized.save(buffer, format="JPEG", quality=quality, optimize=True)
                        if buffer.tell() <= max_encoded_bytes:
                            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                    if max(resized.size) <= 320:
                        break
                    next_size = (max(1, int(resized.width * 0.75)), max(1, int(resized.height * 0.75)))
                    resized = resized.resize(next_size, Image.Resampling.LANCZOS)
                raise ValueError("model image conversion exceeds the encoded image limit")
            mode = "RGBA" if "A" in image.getbands() else "RGB"
            resized = image.convert(mode).resize(size, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG", optimize=True)
    except (binascii.Error, OSError, RuntimeError, ValueError) as error:
        raise ValueError("model image could not be decoded safely") from error
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


# Backward-compatible aliases for older callers/configuration names. The v21
# pipeline no longer performs a separate OCR model request.
bounded_ocr_image_data_url = bounded_model_image_data_url


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _document_text_from_xml(xml_bytes: bytes, *, kind: str) -> str:
    root = ET.fromstring(xml_bytes)
    lines: list[str] = []
    if kind == "docx":
        for paragraph in (node for node in root.iter() if _xml_local_name(node.tag) == "p"):
            pieces: list[str] = []
            for node in paragraph.iter():
                local = _xml_local_name(node.tag)
                if local == "t" and node.text:
                    pieces.append(node.text)
                elif local == "tab":
                    pieces.append("\t")
                elif local in {"br", "cr"}:
                    pieces.append("\n")
            line = "".join(pieces).strip()
            if line:
                lines.append(line)
    else:
        for node in root.iter():
            if _xml_local_name(node.tag) not in {"p", "h"}:
                continue
            line = "".join(node.itertext()).strip()
            if line:
                lines.append(line)
    return "\n".join(lines)


def extract_office_document(path: Path, policy: IngestionPolicy, media_type: str, *, max_images: int | None = None) -> dict[str, Any]:
    """Extract DOCX/ODF text plus bounded embedded images for one multimodal call."""
    limit = policy.max_model_images_per_capture if max_images is None else max(0, min(max_images, policy.max_model_images_per_capture))
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > policy.max_document_entries:
                return {"status": "too_large", "reason": "document_entries", "processed_chars": 0, "images": []}
            total_uncompressed = sum(max(0, info.file_size) for info in infos)
            if total_uncompressed > policy.max_document_uncompressed_bytes:
                return {"status": "too_large", "reason": "document_uncompressed_bytes", "processed_chars": 0, "images": []}
            names = {info.filename for info in infos}
            is_docx = media_type == _DOCX_MEDIA_TYPE
            if is_docx:
                if "word/document.xml" not in names:
                    return {"status": "malformed", "reason": "document_xml", "processed_chars": 0, "images": []}
                xml_parts = ["word/document.xml"]
                xml_parts.extend(sorted(name for name in names if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)))
                xml_parts.extend(name for name in ("word/footnotes.xml", "word/endnotes.xml") if name in names)
                image_names = sorted(name for name in names if name.startswith("word/media/") and not name.endswith("/"))
                kind = "docx"
            elif media_type in _ODF_MEDIA_TYPES:
                if "content.xml" not in names:
                    return {"status": "malformed", "reason": "document_xml", "processed_chars": 0, "images": []}
                xml_parts = ["content.xml"]
                image_names = sorted(name for name in names if name.startswith("Pictures/") and not name.endswith("/"))
                kind = "odf"
            else:
                return {"status": "unsupported", "reason": "document_type", "processed_chars": 0, "images": []}

            text_parts: list[str] = []
            for xml_name in xml_parts:
                try:
                    part = _document_text_from_xml(archive.read(xml_name), kind=kind)
                except (KeyError, ET.ParseError, UnicodeError):
                    if xml_name == xml_parts[0]:
                        return {"status": "malformed", "reason": "document_xml", "processed_chars": 0, "images": []}
                    continue
                if part:
                    text_parts.append(part)
            text = "\n".join(text_parts)
            truncated = len(text) > policy.max_pdf_extracted_chars
            if truncated:
                text = text[: policy.max_pdf_extracted_chars]

            images: list[dict[str, str]] = []
            invalid_images = 0
            for image_name in image_names[:limit]:
                try:
                    raw = archive.read(image_name)
                    detected = _image_kind(raw)
                    if not detected:
                        invalid_images += 1
                        continue
                    raw_url = "data:" + detected + ";base64," + base64.b64encode(raw).decode("ascii")
                    images.append({"name": image_name, "data_url": bounded_model_image_data_url(raw_url, policy)})
                except (KeyError, OSError, RuntimeError, ValueError):
                    invalid_images += 1
            skipped_images = max(0, len(image_names) - limit) + invalid_images
            status = "partial" if truncated or skipped_images else "complete"
            reason = "document_text_limit" if truncated else ("model_image_limit" if len(image_names) > limit else ("document_image_failed" if invalid_images else None))
            return {
                "status": status,
                "reason": reason,
                "text": text,
                "processed_chars": len(text),
                "total_chars": len(text) if not truncated else None,
                "total_chars_known": not truncated,
                "images": images,
                "image_count": len(image_names),
                "model_images_skipped": skipped_images,
                "document_kind": kind,
            }
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return {"status": "malformed", "reason": "document_parse", "processed_chars": 0, "images": []}


def _pdf_worker(path: str, max_pages: int, max_chars: int, result_queue: Any) -> None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(path, strict=False)
        page_count = len(reader.pages)
        if page_count > max_pages:
            result_queue.put({"status": "too_large", "reason": "pdf_pages", "page_count": page_count, "processed_chars": 0})
            return
        parts: list[str] = []
        empty_pages: list[int] = []
        processed = 0
        status = "complete"
        for page_index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if not text.strip():
                empty_pages.append(page_index)
            remaining = max_chars - processed
            if len(text) > remaining:
                parts.append(text[:remaining])
                processed += max(0, remaining)
                status = "partial"
                break
            parts.append(text)
            processed += len(text)
        result_queue.put({
            "status": status,
            "page_count": page_count,
            "empty_pages": empty_pages,
            "text": "\n\n".join(parts),
            "processed_chars": processed,
            "total_chars": processed if status == "complete" else None,
            "total_chars_known": status == "complete",
            "reason": "pdf_text_limit" if status == "partial" else None,
        })
    except Exception as error:  # parser details stay in the worker/logs, not the user response
        result_queue.put({"status": "malformed", "reason": "pdf_parse", "error_type": type(error).__name__})


def extract_pdf(path: Path, policy: IngestionPolicy) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(1)
    process = context.Process(target=_pdf_worker, args=(str(path), policy.max_pdf_pages, policy.max_pdf_extracted_chars, result_queue))
    process.start()
    process.join(policy.extraction_timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(1)
        result_queue.close()
        return {"status": "timeout", "reason": "pdf_extraction_timeout", "processed_chars": 0}
    try:
        return result_queue.get_nowait()
    except Exception:
        return {"status": "extraction_failed", "reason": "pdf_no_result", "processed_chars": 0}
    finally:
        result_queue.close()


def render_pdf_page_for_model(path: Path, page_index: int, policy: IngestionPolicy) -> bytes:
    """Render one PDF page within the same multimodal image budget as uploads."""
    import fitz

    with fitz.open(path) as document:
        if page_index < 0 or page_index >= len(document):
            raise ValueError("PDF page index is outside the document")
        page = document[page_index]
        width = max(float(page.rect.width), 1.0)
        height = max(float(page.rect.height), 1.0)
        scale = min(
            2.0,
            policy.max_image_dimension / width,
            policy.max_image_dimension / height,
            (policy.max_image_pixels / (width * height)) ** 0.5,
        )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return pixmap.tobytes("png")


render_pdf_page_for_ocr = render_pdf_page_for_model


def call_with_timeout(function: Callable[[], Any], timeout_seconds: float) -> Any:
    """Run provider code in a daemon thread so a hung call cannot hold the request."""
    result: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(function())
        except BaseException as exception:  # propagate provider errors to caller
            error.append(exception)

    thread = threading.Thread(target=run, name="omt-ingestion-provider", daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise ExtractionTimeout
    if error:
        raise error[0]
    return result[0] if result else None


def preview_text(text: str, policy: IngestionPolicy) -> tuple[str, bool]:
    value = (text or "")[: policy.preview_char_limit]
    return value, len(text or "") > len(value)


def bounded_attachment(attachment: dict[str, Any], policy: IngestionPolicy, *, include_small_text: bool = True) -> dict[str, Any]:
    result = {key: value for key, value in attachment.items() if key not in {"data_url"}}
    extracted = result.pop("extracted_text", None)
    if extracted:
        preview, truncated = preview_text(extracted, policy)
        result["preview_text"] = preview
        result["preview_truncated"] = truncated
        if include_small_text and not truncated:
            result["extracted_text"] = extracted
    return result


def bounded_source(source: dict[str, Any], policy: IngestionPolicy) -> dict[str, Any]:
    result = dict(source)
    result["body"] = (source.get("body") or "")[: policy.preview_char_limit]
    result["body_preview_truncated"] = len(source.get("body") or "") > len(result["body"])
    result["attachments"] = [bounded_attachment(item, policy) for item in source.get("attachments", [])]
    return result


def ai_source_budget(source: str, context: dict[str, Any], policy: IngestionPolicy) -> int:
    context_bytes = len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    # UTF-8 bytes are a conservative upper bound for the byte-fallback tokenizers
    # used by the configured multilingual models; do not assume four chars/token.
    available_source_bytes = min(
        policy.ai_context_chars,
        policy.ai_context_token_budget - AI_PROMPT_OVERHEAD_TOKEN_BUDGET - AI_OUTPUT_TOKEN_LIMIT,
    ) - context_bytes
    if available_source_bytes <= 0:
        return 0
    source_char_budget = 0
    for character in source:
        character_bytes = len(character.encode("utf-8"))
        if character_bytes > available_source_bytes:
            break
        available_source_bytes -= character_bytes
        source_char_budget += 1
    return source_char_budget


def truncate_at_text_boundary(text: str, limit: int) -> str:
    """Keep a complete paragraph/line when possible, with a hard bounded fallback."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    boundary = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit))
    best = boundary
    for marker in (". ", "。", "！", "？", "; ", "；"):
        candidate = text.rfind(marker, 0, limit)
        if candidate >= limit // 2:
            best = max(best, candidate + len(marker))
    return text[:best].rstrip() if best >= limit // 2 else text[:limit]


def overall_status(statuses: list[str]) -> str:
    if not statuses:
        return "complete"
    if any(status in {"timeout", "malformed", "extraction_failed", "unsupported", "too_large"} for status in statuses):
        return "partial" if any(status in {"complete", "partial"} for status in statuses) else statuses[0]
    if any(status == "partial" for status in statuses):
        return "partial"
    return "complete"


def log_attachment_result(source_id: str, attachment_id: str, attachment: dict[str, Any], started_at: float) -> None:
    LOGGER.info(
        "attachment_processed",
        extra={
            "source_id": source_id,
            "attachment_id": attachment_id,
            "byte_size": attachment.get("size", 0),
            "detected_media_type": attachment.get("detected_media_type"),
            "extraction_method": attachment.get("extraction_method"),
            "processing_duration_ms": round((time.monotonic() - started_at) * 1000),
            "extraction_status": attachment.get("extraction_status"),
            "processed_chars": attachment.get("processed_chars", 0),
            "limit_hit": attachment.get("limit_hit"),
        },
    )
