from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from api import ApiError, RagApiApplication
from auth import ApiKeyStore, hash_key
from parsers import HTML, MARKDOWN, PDF, TEXT, UnsupportedDocumentError, parse, sniff
from permissions import Principal


def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


KEYS = ApiKeyStore(
    {
        hash_key("alice"): Principal("acme", "alice", frozenset({"hr"})),
        hash_key("bob"): Principal("acme", "bob"),
    }
)


def call(
    app: RagApiApplication,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    key: str = "alice",
) -> tuple[int, dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode()
    try:
        return app.handle(method, path, {"x-api-key": key}, body)
    except ApiError as exc:
        return int(exc.status), {"detail": exc.message}


def upload(app: RagApiApplication, filename: str, data: bytes, **extra: Any) -> dict[str, Any]:
    payload = {"filename": filename, "content_base64": base64.b64encode(data).decode(), **extra}
    status, body = call(app, "POST", "/v1/documents", payload, key=extra.pop("key", "alice"))
    assert status in {200, 201}, body
    return body


@pytest.fixture
def app() -> RagApiApplication:
    return RagApiApplication(keys=KEYS)


def test_sniffing_trusts_bytes_not_labels() -> None:
    assert sniff(b"%PDF-1.4 ...", "a.pdf") == PDF
    assert sniff(b"<!DOCTYPE html><p>x", "a.html") == HTML
    assert sniff(b"# Title", "notes.md") == MARKDOWN
    assert sniff(b"plain", "notes") == TEXT
    with pytest.raises(UnsupportedDocumentError, match="unsupported file type"):
        sniff(b"PK\x03\x04zip", "a.txt")
    with pytest.raises(UnsupportedDocumentError, match="extension"):
        sniff(b"just text", "report.pdf")
    with pytest.raises(UnsupportedDocumentError, match="declared"):
        sniff(b"%PDF-1.4", "a", declared="text/plain")
    with pytest.raises(UnsupportedDocumentError, match="UTF-8"):
        sniff(b"\xff\xfe\xfa", "a.txt")


def test_html_is_parsed_without_scripts() -> None:
    html = b"<html><head><script>steal()</script></head><body><h1>Hours</h1><p>Open 9-5.</p>"
    parsed = parse(html, "hours.html")
    text = parsed.segments[0][1]
    assert "Open 9-5." in text and "steal" not in text


def test_pdf_pages_are_extracted_in_a_sandbox() -> None:
    parsed = parse(make_pdf(["Refunds take thirty days.", "Shipping is free."]), "p.pdf")
    assert parsed.content_type == PDF
    assert [(page, text.strip()) for page, text in parsed.segments] == [
        (1, "Refunds take thirty days."),
        (2, "Shipping is free."),
    ]


def test_malformed_pdf_is_rejected_not_crashing_the_api() -> None:
    with pytest.raises(UnsupportedDocumentError, match="could not parse"):
        parse(b"%PDF-1.4 this is not really a pdf", "broken.pdf")


def test_pdf_upload_cites_pages(app: RagApiApplication) -> None:
    created = upload(app, "policy.pdf", make_pdf(["Returns within thirty days.", "Free shipping."]))
    assert created["content_type"] == PDF
    _, answer = call(app, "POST", "/v1/query", {"question": "Is shipping free?"})
    assert answer["grounded"] and answer["citations"][0]["page"] == 2


def test_bad_upload_shapes_are_rejected(app: RagApiApplication) -> None:
    both = {"filename": "a.txt", "content": "x", "content_base64": "eA=="}
    assert call(app, "POST", "/v1/documents", both)[0] == 422
    bad = {"filename": "a.txt", "content_base64": "not base64!"}
    assert call(app, "POST", "/v1/documents", bad)[0] == 422
    zipped = {"filename": "a.zip", "content_base64": base64.b64encode(b"PK\x03\x04").decode()}
    assert call(app, "POST", "/v1/documents", zipped)[1]["detail"] == "unsupported file type"


def test_reupload_creates_a_new_version(app: RagApiApplication) -> None:
    first = upload(app, "hours.txt", b"The store opens at nine.")
    second = upload(app, "hours.txt", b"The store opens at ten.")
    assert (first["version"], second["version"]) == (1, 2)
    assert second["superseded"] == [first["document_id"]]
    _, listing = call(app, "GET", "/v1/documents")
    assert [row["version"] for row in listing["documents"]] == [2]
    _, everything = call(app, "GET", "/v1/documents?include_superseded=true")
    assert len(everything["documents"]) == 2
    _, answer = call(app, "POST", "/v1/query", {"question": "When does the store open?"})
    assert "ten" in answer["answer"] and "nine" not in answer["answer"]
    # Restoring the old bytes reinstates that document as the newest version.
    restored = upload(app, "hours.txt", b"The store opens at nine.")
    assert restored["already_existed"] and restored["version"] == 3
    assert restored["superseded"] == [second["document_id"]]


def test_users_cannot_supersede_documents_they_cannot_see(app: RagApiApplication) -> None:
    upload(app, "plan.txt", b"The confidential plan.", allowed_groups=["hr"])
    public = upload(app, "plan.txt", b"The public plan.", key="bob")
    assert public["version"] == 1 and public["superseded"] == []
    _, alice_view = call(app, "GET", "/v1/documents")
    assert len(alice_view["documents"]) == 2
