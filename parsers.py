"""Detect and parse uploaded documents.

Plain text and Markdown are decoded in-process. PDF and HTML are parsed in a
separate, short-lived Python process with CPU, memory and time limits, so a
malicious or pathological file can at worst kill that process, not the API.

The type is sniffed from the bytes. A declared content type or file extension
that disagrees with the bytes is rejected rather than trusted.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

PDF, HTML, MARKDOWN, TEXT = "application/pdf", "text/html", "text/markdown", "text/plain"
EXTENSIONS = {
    ".pdf": PDF,
    ".html": HTML,
    ".htm": HTML,
    ".md": MARKDOWN,
    ".markdown": MARKDOWN,
    ".txt": TEXT,
}
# Signatures of formats we refuse outright (archives, executables, images, Office).
REJECTED_SIGNATURES = (b"PK\x03\x04", b"MZ", b"\x7fELF", b"\x89PNG", b"GIF8", b"\xff\xd8\xff")
MAX_PAGES = 500
MAX_EXTRACTED_CHARS = 5_000_000
SANDBOX_TIMEOUT_SECONDS = 30
SANDBOX_MEMORY_BYTES = 512 * 1024 * 1024

Segment = tuple[int | None, str]


class UnsupportedDocumentError(ValueError):
    """The upload is not an accepted, well-formed document."""


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    content_type: str
    segments: tuple[Segment, ...]


def sniff(data: bytes, filename: str, declared: str | None = None) -> str:
    """Return the content type established from the bytes themselves."""
    head = data[:1024]
    if data.startswith(b"%PDF-"):
        kind = PDF
    elif any(data.startswith(signature) for signature in REJECTED_SIGNATURES):
        raise UnsupportedDocumentError("unsupported file type")
    elif re.match(rb"\s*(<!doctype\s+html|<html[\s>])", head, re.I):
        kind = HTML
    else:
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise UnsupportedDocumentError("text documents must be valid UTF-8") from exc
        kind = MARKDOWN if Path(filename).suffix.lower() in {".md", ".markdown"} else TEXT

    textual = {TEXT, MARKDOWN}
    extension_type = EXTENSIONS.get(Path(filename).suffix.lower())
    if extension_type and extension_type != kind and not {extension_type, kind} <= textual:
        raise UnsupportedDocumentError(f"file extension does not match its {kind} content")
    if declared:
        declared_type = declared.split(";")[0].strip().lower()
        if declared_type not in {PDF, HTML, MARKDOWN, TEXT}:
            raise UnsupportedDocumentError(f"unsupported content type {declared_type}")
        if declared_type != kind and not {declared_type, kind} <= textual:
            raise UnsupportedDocumentError("declared content type does not match the content")
    return kind


def parse(data: bytes, filename: str, declared: str | None = None) -> ParsedDocument:
    kind = sniff(data, filename, declared)
    if kind in {TEXT, MARKDOWN}:
        return ParsedDocument(kind, ((None, data.decode("utf-8")),))
    return ParsedDocument(kind, tuple(_run_sandboxed(kind, data)))


def _limit_resources() -> None:  # pragma: no cover - runs in the child process
    import resource

    for limit, value in (
        (resource.RLIMIT_CPU, SANDBOX_TIMEOUT_SECONDS),
        (resource.RLIMIT_AS, SANDBOX_MEMORY_BYTES),
    ):
        # Not every platform (e.g. macOS) enforces RLIMIT_AS.
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(limit, (value, value))


def _run_sandboxed(kind: str, data: bytes) -> list[Segment]:
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), kind],
            input=data,
            capture_output=True,
            timeout=SANDBOX_TIMEOUT_SECONDS,
            env={},
            preexec_fn=_limit_resources if sys.platform != "win32" else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnsupportedDocumentError("document parsing timed out") from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = message[-1] if message else "parser crashed"
        raise UnsupportedDocumentError(f"could not parse document: {reason[:200]}")
    return [(page, text) for page, text in json.loads(completed.stdout)]


class _TextExtractor(HTMLParser):
    SKIPPED = frozenset({"script", "style", "noscript", "template", "svg", "head"})
    BLOCKS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "title"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        if tag in self.SKIPPED:
            self._skip_depth += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def parse_html(data: bytes) -> list[Segment]:
    extractor = _TextExtractor()
    extractor.feed(data.decode("utf-8", errors="replace"))
    extractor.close()
    text = re.sub(r"\n\s*\n+", "\n\n", "".join(extractor.parts)).strip()
    return [(None, text[:MAX_EXTRACTED_CHARS])]


def parse_pdf(data: bytes) -> list[Segment]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise UnsupportedDocumentError("encrypted PDFs are not supported")
    segments: list[Segment] = []
    total = 0
    for number, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        text = (page.extract_text() or "").strip()
        total += len(text)
        if total > MAX_EXTRACTED_CHARS:
            raise UnsupportedDocumentError("document has too much text")
        if text:
            segments.append((number, text))
    return segments


def _main() -> None:  # pragma: no cover - exercised through the sandbox
    kind = sys.argv[1]
    data = sys.stdin.buffer.read()
    try:
        segments = parse_pdf(data) if kind == PDF else parse_html(data)
    except UnsupportedDocumentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f"{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(3) from None
    json.dump(segments, sys.stdout)


if __name__ == "__main__":
    _main()
