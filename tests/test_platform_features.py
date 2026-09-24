import json
import time

from api import RagApiApplication
from providers import ChatTurn, ModelProvider


class FakeProvider(ModelProvider):
    def __init__(self) -> None:
        self.histories: list[int] = []

    def generate(self, question: str, context: list[str], history: tuple[ChatTurn, ...]) -> str:
        del question, context
        self.histories.append(len(history))
        return "model answer"


def call(app: RagApiApplication, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    try:
        return app.handle("POST", path, {"x-api-key": "test-key"}, json.dumps(payload).encode())
    except Exception as exc:  # API errors are intentionally represented as status payloads here.
        return int(exc.status), {"detail": exc.message}


def test_chat_history_is_bounded_and_reused() -> None:
    provider = FakeProvider()
    app = RagApiApplication(api_key="test-key", provider=provider, tenant_id="tenant-a")
    document = {"filename": "policy.txt", "tenant_id": "tenant-a", "content": "Refunds are available within thirty days."}
    assert call(app, "/v1/documents", document)[0] == 201
    payload = {"tenant_id": "tenant-a", "user_id": "u1", "session_id": "s1", "question": "How long are refunds available?"}
    assert call(app, "/v1/chat", payload)[0] == 200
    assert call(app, "/v1/chat", payload)[0] == 200
    assert provider.histories == [0, 2]


def test_background_ingestion_job_completes() -> None:
    app = RagApiApplication(api_key="test-key", tenant_id="tenant-a")
    status, response = call(
        app,
        "/v1/ingestion/jobs",
        {"filename": "async.txt", "tenant_id": "tenant-a", "content": "background evidence"},
    )
    assert status == 202
    job_id = response["job_id"]
    for _ in range(50):
        if app.worker.status(job_id) == "completed":
            break
        time.sleep(0.01)
    assert app.worker.status(job_id) == "completed"
