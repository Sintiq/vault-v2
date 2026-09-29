"""Public chat-pane behavior with synthetic files and a fake HTTP boundary."""

import io
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest

from vault_v2.agent import LocalModelUnavailable
from vault_v2.chat import ChatPane
from vault_v2 import chat


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def local_http(monkeypatch):
    state = {"models": [], "requests": [], "payloads": [], "warm": threading.Event(), "offline": False}

    def open_http(_opener, request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        state["requests"].append(url)
        assert url.startswith("http://127.0.0.1:11434/api/")
        if url.endswith("/api/tags"):
            if state["offline"]:
                raise ConnectionRefusedError("synthetic private connection detail")
            return io.BytesIO(json.dumps({"models": [
                {"name": name} for name in state["models"]
            ]}).encode())
        if url.endswith("/api/chat"):
            payload = json.loads(request.data)
            state["payloads"].append(payload)
            if payload["stream"] is False:
                state["warm"].set()
            return io.BytesIO(b'{"message":{"content":"Synthetic reply"},"done":true}\n')
        raise AssertionError(f"unexpected model URL: {url}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_http)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key-must-not-be-used")
    return state


@pytest.mark.parametrize("backend", ["bridge", "anthropic", "unknown"])
def test_unsupported_backend_is_visible_and_cannot_send(application, local_http, tmp_path: Path, backend):
    staging = tmp_path / "staging"
    staging.mkdir()
    pane = ChatPane(staging, {"agent_backend": backend})
    try:
        assert pane.sub.text() == "Unsupported agent_backend configuration: only Ollama is available. Choose agent_backend=ollama."
        assert pane.send.isEnabled() is False
        assert pane.remote_state()["ready"] is False
        assert pane.remote_state()["history"] == []
        assert not (tmp_path / ".chat").exists()
        assert local_http["requests"] == []
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


def test_missing_model_refuses_phone_turn_without_recording_it(application, local_http, tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    pane = ChatPane(staging, {})
    delivered = []
    pane.remote_turn.connect(lambda *turn: delivered.append(turn))
    try:
        assert pane.sub.text() == "model llama3.1:8b is not installed — run: ollama pull llama3.1:8b · sees only Staging"
        assert pane.send.isEnabled() is False
        with pytest.raises(LocalModelUnavailable) as first:
            pane.remote_send("synthetic private question")
        with pytest.raises(LocalModelUnavailable) as second:
            pane.remote_send("another synthetic private question")
        assert first.value is not second.value, "unavailable turns must not accumulate on a retained exception traceback"
        assert first.value.public_message == second.value.public_message == (
            "model llama3.1:8b is not installed — run: ollama pull llama3.1:8b"
        )
        assert pane.remote_state() == {
            "backend": "model llama3.1:8b is not installed — run: ollama pull llama3.1:8b", "ready": False, "history": []
        }
        assert delivered == []
        assert local_http["payloads"] == []
        assert not (tmp_path / ".chat").exists()
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


def test_api_key_does_not_change_local_model_or_payload(application, local_http, tmp_path: Path):
    local_http["models"] = ["llama3.1:8b"]
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "synthetic.txt").write_text("file body is not part of basic chat", encoding="utf-8")
    pane = ChatPane(staging, {})
    try:
        assert local_http["warm"].wait(2), "warmup must finish at the fake HTTP boundary"
        assert pane.sub.text() == "local model · llama3.1:8b · sees only Staging"
        assert pane.send.isEnabled() is True
        assert pane.remote_send("synthetic request") == "Synthetic reply"
        assert pane.remote_state() == {
            "backend": "local model · llama3.1:8b", "ready": True,
            "history": [{"role": "user", "content": "synthetic request"},
                        {"role": "assistant", "content": "Synthetic reply"}],
        }
        payload = local_http["payloads"][-1]
        assert payload["model"] == "llama3.1:8b"
        assert "synthetic.txt" in payload["messages"][0]["content"]
        assert "file body is not part of basic chat" not in json.dumps(payload)
        assert not (tmp_path / ".chat").exists()
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


def test_local_retry_preserves_old_mailbox_files(application, local_http, tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    mailbox = tmp_path / ".chat"
    mailbox.mkdir()
    stored = {"inbox.jsonl": b'{"text":"old synthetic inbox"}\n',
              "outbox.jsonl": b'{"text":"old synthetic outbox"}\n'}
    for name, contents in stored.items():
        (mailbox / name).write_bytes(contents)
    pane = ChatPane(staging, {})
    try:
        assert pane.remote_state()["ready"] is False
        local_http["models"] = ["llama3.1:8b"]
        pane.refresh_status()
        assert local_http["warm"].wait(2)
        assert pane.remote_state()["ready"] is True
        pane.remote_send("new synthetic request")
        assert "old synthetic" not in json.dumps(local_http["payloads"])
        assert {name: (mailbox / name).read_bytes() for name in stored} == stored
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("offline", [False, True])
def test_timer_recovers_after_service_or_selected_model_appears(application, local_http, tmp_path, monkeypatch, offline):
    # Accelerate only time; the real Qt timer, selection and HTTP boundary run.
    monkeypatch.setattr(chat, "SETTLE_MS", 10)
    local_http["offline"] = offline
    staging = tmp_path / "staging"
    staging.mkdir()
    pane = ChatPane(staging, {"ollama_model": "wanted:local"})
    try:
        expected = ("Ollama is not running on this PC" if offline else
                    "model wanted:local is not installed — run: ollama pull wanted:local")
        assert pane.sub.text() == expected + " · sees only Staging"
        assert not pane.remote_state()["ready"]
        deadline = time.monotonic() + 1
        while len(local_http["requests"]) < 3 and time.monotonic() < deadline:
            QTest.qWait(10)
        assert len(local_http["requests"]) >= 3, "retry must continue through two unavailable ticks"
        local_http["offline"] = False
        local_http["models"] = ["wanted:local"]
        deadline = time.monotonic() + 1
        while not pane.remote_state()["ready"] and time.monotonic() < deadline:
            QTest.qWait(10)
        assert pane.remote_state()["ready"]
        assert pane.sub.text() == "local model · wanted:local · sees only Staging"
        assert pane.send.isEnabled()
        assert local_http["warm"].wait(2)
        settled_requests = len(local_http["requests"])
        QTest.qWait(50)
        assert len(local_http["requests"]) == settled_requests, "ready state must stop discovery retries"
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


def test_configuration_error_stops_periodic_discovery(application, local_http, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SETTLE_MS", 10)
    staging = tmp_path / "staging"
    staging.mkdir()
    pane = ChatPane(staging, {"agent_backend": "anthropic"})
    try:
        QTest.qWait(50)
        assert not pane.send.isEnabled()
        assert local_http["requests"] == []
        assert not any(timer.isActive() for timer in pane.findChildren(QTimer))
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()
