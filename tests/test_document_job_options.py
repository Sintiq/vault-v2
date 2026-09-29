"""Document jobs use stable local sampling; conversational chat stays unchanged."""
import json
import io
from http.client import IncompleteRead, RemoteDisconnected
import threading
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from vault_v2.agent import Backend, DOCUMENT_JOB_SEED, LocalModelUnavailable, OllamaBackend, document_chat
from vault_v2.ask import AskDoc, ask
from vault_v2.health import HealthDoc, agent_entries
from vault_v2.sorting import SortInput, agent_cards
from vault_v2.tasks import TaskDoc, agent_tasks


class Response:
    def __init__(self, reply):
        self.reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter([json.dumps({"message": {"content": self.reply}, "done": True}).encode()])

    def read(self):
        return b"{}"


def run_job(name, backend):
    quote = "Visit and refill by 2026-10-15."
    if name == "tasks":
        return agent_tasks(backend, [TaskDoc("doc-001", "synthetic.txt", "synthetic.txt", "a"*64, quote)])
    if name == "health":
        return agent_entries(backend, [HealthDoc("doc-001", "synthetic.txt", "a"*64, quote)])
    if name == "sort":
        return agent_cards(backend, [SortInput("doc-001", Path("synthetic.txt"), "synthetic.txt", "TEXT", len(quote), "a"*64, quote)])
    return ask("synthetic", [AskDoc("doc-001", Path("synthetic.txt"), None)], backend)


def expected_document_format(name):
    if name == "health":
        return {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                    "kind": {"type": "string"},
                    "label": {"type": "string", "maxLength": 140},
                    "quote": {"type": "string", "maxLength": 200},
                },
                "required": ["doc", "date", "kind", "label", "quote"],
                "additionalProperties": False,
            },
        }
    return {"type": "object"} if name == "ask" else {"type": "array", "items": {"type": "object"}}


def test_health_generation_limits_are_explicit_for_every_document(monkeypatch):
    captured = []

    def capture(_opener, request, timeout):
        captured.append(json.loads(request.data))
        return Response("[]")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    agent_entries(OllamaBackend("llama3.1:8b"), [
        HealthDoc("doc-001", "a.txt", "a" * 64, "Visit documented."),
        HealthDoc("doc-002", "b.txt", "b" * 64, "Lab test documented."),
    ])

    expected = expected_document_format("health")
    expected["maxItems"] = 24
    assert captured[0]["format"] == expected
    system = captured[0]["messages"][0]["content"]
    assert "at most 12 entries per document" in system
    assert "at most 200 Unicode characters" in system


@pytest.mark.parametrize("document_count", [None, 0, -1, True, 1.0, "2"])
def test_health_document_count_is_validated_before_transport(monkeypatch, document_count):
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid document count must not make a model request")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", unexpected)
    with pytest.raises(ValueError, match="positive integer"):
        document_chat(OllamaBackend("llama3.1:8b"), "synthetic", [], lambda _: None,
                      json_shape="health", document_count=document_count)


@pytest.mark.parametrize("name", ["tasks", "health", "sort", "ask"])
def test_production_document_jobs_send_options_without_changing_chat(monkeypatch, name):
    requests = []

    def capture(_opener, request, timeout):
        requests.append((request.full_url, json.loads(request.data)))
        return Response('{"documents":[],"recipient":"PERSONAL","reason":"synthetic"}' if name == "ask" else "[]")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    backend = OllamaBackend("llama3.1:8b")
    backend.chat("synthetic ordinary chat", [{"role":"user", "content":"hello"}], lambda _: None)
    run_job(name, backend)
    chunks = []
    backend.chat("synthetic ordinary chat", [{"role":"user", "content":"hello again"}], chunks.append)
    assert len(requests) == 3
    assert all(url == "http://127.0.0.1:11434/api/chat" for url, _ in requests)
    assert "options" not in requests[0][1] and "options" not in requests[2][1]
    assert "format" not in requests[0][1] and "format" not in requests[2][1]
    expected_format = expected_document_format(name)
    assert requests[1][1]["format"] == expected_format
    assert requests[1][1]["options"] == {"temperature": 0, "seed": DOCUMENT_JOB_SEED,
                                       "num_ctx": 8192, "num_predict": 2048}
    assert isinstance(DOCUMENT_JOB_SEED, int)
    assert all(payload["model"] == "llama3.1:8b" and payload["stream"] is True for _, payload in requests)
    assert chunks, "ordinary chat streaming remains intact after a document job"


def test_document_sampling_settings_repeat_without_backend_mutation(monkeypatch):
    requests = []

    def capture(_opener, request, timeout):
        requests.append(json.loads(request.data))
        return Response("[]")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    backend = OllamaBackend("llama3.1:8b")
    messages = [{"role":"user", "content":"synthetic only"}]
    before = json.dumps(messages)
    for _ in range(3):
        assert document_chat(backend, "synthetic", messages, lambda _: None) == "[]"
    assert [row["options"] for row in requests] == [
        {"temperature":0, "seed":DOCUMENT_JOB_SEED, "num_ctx":8192, "num_predict":2048}]*3
    assert json.dumps(messages) == before


@pytest.mark.parametrize("inherits_backend", [True, False])
def test_document_helper_preserves_existing_backend_test_contract(inherits_backend):
    class Reply(Backend if inherits_backend else object):
        def chat(self, system, messages, on_chunk):
            on_chunk("synthetic")
            return "reply"

    chunks = []
    assert document_chat(Reply(), "sys", [], chunks.append) == "reply"
    assert chunks == ["synthetic"]


def test_document_transport_failure_is_not_retried_as_chat(monkeypatch):
    requests = []

    def fail(_opener, request, timeout):
        requests.append(json.loads(request.data))
        raise urllib.error.URLError("synthetic connection failure")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail)
    with pytest.raises(LocalModelUnavailable):
        run_job("tasks", OllamaBackend("llama3.1:8b"))
    assert len(requests) == 1 and requests[0]["options"]["temperature"] == 0


@pytest.mark.parametrize("name", ["tasks", "health", "sort", "ask"])
@pytest.mark.parametrize("terminal", ["length", "missing", "reset", "bad_frame", "incomplete_read", "remote_disconnect"])
def test_document_incomplete_output_cannot_publish_even_valid_json(monkeypatch, name, terminal):
    from vault_v2.document_budget import DocumentOutputIncomplete

    class IncompleteResponse(Response):
        def __iter__(self):
            row = {"message": {"content": self.reply}, "done": terminal == "length"}
            if terminal == "length":
                row["done_reason"] = "length"
            yield json.dumps(row).encode()
            if terminal == "reset":
                raise ConnectionResetError("synthetic interrupted response")
            if terminal == "bad_frame":
                yield b'{invalid transport frame'
            if terminal == "incomplete_read":
                raise IncompleteRead(b"partial", 99)
            if terminal == "remote_disconnect":
                raise RemoteDisconnected("synthetic disconnected response")

    requests = []
    def capture(_opener, request, timeout):
        requests.append(json.loads(request.data))
        return IncompleteResponse(json.dumps(job_reply(name)))

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    with pytest.raises(DocumentOutputIncomplete, match="incomplete") as refused:
        run_job(name, OllamaBackend("llama3.1:8b"))
    assert refused.value.documents == ("doc-001 — synthetic.txt",)
    assert len(requests) == 1, "no retry, baseline publication or extra model call"
    if terminal == "incomplete_read":
        with pytest.raises(IncompleteRead):
            OllamaBackend("llama3.1:8b").chat("ordinary chat", [], lambda _: None)
    if terminal == "remote_disconnect":
        with pytest.raises(LocalModelUnavailable):
            OllamaBackend("llama3.1:8b").chat("ordinary chat", [], lambda _: None)


def job_reply(name):
    quote = "Visit and refill by 2026-10-15."
    if name == "tasks":
        return [{"doc":"doc-001", "title":"Refill", "due":"2026-10-15", "quote":quote}]
    if name == "health":
        return [{"doc":"doc-001", "kind":"VISIT", "label":"Visit", "date":"2026-10-15", "quote":quote}]
    if name == "sort":
        return [{"id":"doc-001", "shelf":"HEALTH", "topics":["synthetic"], "issuer":"UNCONFIRMED", "recipients":["DOCTOR"]}]
    return {"documents":["doc-001"], "recipient":"PERSONAL", "reason":"synthetic"}


@pytest.mark.parametrize("name", ["tasks", "health", "sort", "ask"])
def test_schema_prevents_missing_closing_delimiter_at_production_jobs(monkeypatch, name):
    encoded = json.dumps(job_reply(name))
    expected_format = expected_document_format(name)
    captured = []

    def constrained_server(_opener, request, timeout):
        payload = json.loads(request.data)
        captured.append(payload)
        # Reproduce the observed natural stop before the final ] unless the
        # request selects the server's supported constrained generation shape.
        return Response(encoded if payload.get("format") == expected_format else encoded[:-1])

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", constrained_server)
    result = run_job(name, OllamaBackend("llama3.1:8b"))
    if name == "ask":
        assert result.agent_ids == ("doc-001",) and not result.error
    else:
        accepted, notes = result
        assert len(accepted) == 1 and not notes
    assert len(captured) == 1 and captured[0]["format"] == expected_format


@pytest.mark.parametrize("name", ["tasks", "health", "sort", "ask"])
def test_document_parser_still_rejects_truncation_instead_of_repairing(monkeypatch, name):
    malformed = json.dumps(job_reply(name))[:-1]
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *args, **kwargs: Response(malformed))
    result = run_job(name, OllamaBackend("llama3.1:8b"))
    if name == "ask":
        assert result.agent_ids is None and "unusable" in result.error
    else:
        accepted, notes = result
        assert not accepted and notes


@pytest.mark.parametrize("shape", ["string", "json", "ARRAY", "", None, {}, []])
def test_document_shape_is_a_controlled_choice_before_http(monkeypatch, shape):
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid shape must not make a model request")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", unexpected)
    with pytest.raises(ValueError):
        document_chat(OllamaBackend("llama3.1:8b"), "synthetic", [], lambda _: None, json_shape=shape)


@pytest.mark.parametrize("name", ["tasks", "health", "sort", "ask"])
def test_open_cloud_chat_door_preserves_local_document_generation(monkeypatch, tmp_path, name):
    from PySide6.QtWidgets import QApplication
    from vault_v2.chat import ChatPane
    from vault_v2.agent_door import AgentDoor
    from vault_v2.receipts import ReceiptLog

    application = QApplication.instance() or QApplication([])
    requests = []
    warmed = threading.Event()
    class WarmResponse(io.BytesIO):
        def read(self, *args):
            value = super().read(*args)
            warmed.set()
            return value
    def respond(_opener, request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        if url.endswith("/tags"):
            return io.BytesIO(b'{"models":[{"name":"llama3.1:8b"}]}')
        payload = json.loads(request.data)
        if not any(message.get("role") == "system" for message in payload.get("messages", [])):
            return WarmResponse(b"{}")
        requests.append(payload)
        return Response(json.dumps(job_reply(name)))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", respond)
    staging = tmp_path / "staging"
    staging.mkdir()
    log = ReceiptLog(tmp_path / ".receipts")
    pane = ChatPane(staging, {}, door=AgentDoor(log, {}))
    try:
        assert warmed.wait(3), "synthetic warm-up must finish before transport teardown"
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        run_job(name, pane.get_local_backend())
        payload = requests[-1]
        expected_format = expected_document_format(name)
        assert payload.get("options") == {"temperature": 0, "seed": DOCUMENT_JOB_SEED,
                                          "num_ctx": 8192, "num_predict": 2048}
        assert payload.get("format") == expected_format
        assert not any(row["op"] == "agent_door_request" for row in log.tail())
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()
