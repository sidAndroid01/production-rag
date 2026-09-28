from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from typing import Any
from urllib import error, request

import pytest

from api import ApiError, RagApiApplication, RequestHandler

HEADERS = {"x-api-key": "test-key"}
DOCUMENT = {"filename": "policy.txt", "tenant_id": "tenant-a", "content": "Refunds take 30 days."}


def call(
    app: RagApiApplication, method: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode()
    try:
        return app.handle(method, path, HEADERS, body)
    except ApiError as exc:
        return int(exc.status), {"detail": exc.message}


def wait_for_job(app: RagApiApplication, job_id: str) -> dict[str, Any]:
    for _ in range(200):
        status, job = call(app, "GET", f"/v1/ingestion/jobs/{job_id}")
        assert status == 200
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.fixture
def app() -> RagApiApplication:
    return RagApiApplication(api_key="test-key", tenant_id="tenant-a")


@pytest.fixture
def server(app: RagApiApplication) -> Iterator[str]:
    RequestHandler.application = app
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        RequestHandler.application = None


def http(base: str, method: str, path: str, payload: dict[str, Any] | None = None) -> int:
    data = None if payload is None else json.dumps(payload).encode()
    req = request.Request(base + path, data=data, method=method, headers=HEADERS)
    try:
        with request.urlopen(req) as response:
            assert response.headers["x-request-id"]
            return int(response.status)
    except error.HTTPError as exc:
        return exc.code


def test_delete_works_over_real_http(server: str) -> None:
    assert http(server, "POST", "/v1/documents", DOCUMENT) == 201
    document_id = json.loads(
        request.urlopen(request.Request(server + "/v1/documents", headers=HEADERS)).read()
    )["documents"][0]["document_id"]
    assert http(server, "DELETE", f"/v1/documents/{document_id}") == 200
    assert http(server, "GET", f"/v1/documents/{document_id}") == 404
    assert http(server, "PUT", "/v1/documents") == 405


def test_list_get_and_duplicate_upload(app: RagApiApplication) -> None:
    status, created = call(app, "POST", "/v1/documents", DOCUMENT)
    assert status == 201 and created["already_existed"] is False
    status, again = call(app, "POST", "/v1/documents", DOCUMENT)
    assert status == 200 and again["already_existed"] is True and again["chunks_created"] == 0
    status, listing = call(app, "GET", "/v1/documents")
    assert status == 200 and len(listing["documents"]) == 1
    status, one = call(app, "GET", f"/v1/documents/{created['document_id']}")
    assert status == 200 and one["source"] == "policy.txt" and one["chunk_count"] == 1
    # Re-uploading must not duplicate evidence in answers.
    _, answer = call(app, "POST", "/v1/query", {"question": "How long do refunds take?"})
    assert len(answer["citations"]) == 1


def test_empty_document_is_rejected(app: RagApiApplication) -> None:
    status, body = call(app, "POST", "/v1/documents", {**DOCUMENT, "content": "   "})
    assert status == 422 and body["detail"] == "document has no text to index"


def test_job_status_reports_result_and_failure(app: RagApiApplication) -> None:
    status, job = call(app, "POST", "/v1/ingestion/jobs", DOCUMENT)
    assert status == 202 and job["status"] == "queued"
    finished = wait_for_job(app, job["job_id"])
    assert finished["status"] == "completed"
    assert finished["result"]["chunks_created"] == 1

    _, bad = call(app, "POST", "/v1/ingestion/jobs", {**DOCUMENT, "content": " "})
    failed = wait_for_job(app, bad["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "document has no text to index"


def test_job_submission_validates_input_up_front(app: RagApiApplication) -> None:
    status, _ = call(app, "POST", "/v1/ingestion/jobs", {"tenant_id": "tenant-a"})
    assert status == 422


def test_unknown_job_and_route(app: RagApiApplication) -> None:
    assert call(app, "GET", "/v1/ingestion/jobs/nope")[0] == 404
    assert call(app, "GET", "/v1/unknown")[0] == 404


def test_protected_routes_require_key(app: RagApiApplication) -> None:
    with pytest.raises(ApiError) as caught:
        app.handle("GET", "/v1/documents", {}, b"")
    assert caught.value.status == 401
    assert app.handle("GET", "/health/live", {}, b"")[0] == 200
