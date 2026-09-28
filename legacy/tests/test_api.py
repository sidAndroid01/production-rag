from fastapi.testclient import TestClient

from rag_api.main import app

HEADERS = {"x-api-key": "local-development-key"}


def test_health_is_public() -> None:
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}


def test_query_requires_authentication() -> None:
    with TestClient(app) as client:
        assert client.post("/v1/query", json={"question": "hello"}).status_code == 401


def test_ingest_then_grounded_query() -> None:
    with TestClient(app) as client:
        ingest = client.post(
            "/v1/documents",
            headers=HEADERS,
            files={
                "file": (
                    "handbook.txt",
                    b"Employees receive twenty days of annual leave.",
                    "text/plain",
                )
            },
        )
        assert ingest.status_code == 201

        result = client.post(
            "/v1/query", headers=HEADERS, json={"question": "How many annual leave days?"}
        )
        assert result.status_code == 200
        assert result.json()["grounded"] is True
        assert result.json()["citations"][0]["source"] == "handbook.txt"


def test_refuses_unsupported_file_type() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            headers=HEADERS,
            files={"file": ("image.png", b"not an image", "image/png")},
        )
        assert response.status_code == 415
