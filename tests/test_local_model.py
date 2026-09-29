"""The local model: chosen on evidence, and kept warm."""

from __future__ import annotations

import json

import pytest

from vault_v2 import agent
from vault_v2.agent import OLLAMA_KEEP_ALIVE, OllamaBackend, warm_ollama


class _FakeResponse:
    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> bytes:
        return b"".join(self._lines)


@pytest.fixture()
def captured(monkeypatch):
    sent: list[dict] = []

    def fake_urlopen(opener, request, timeout=0):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse([json.dumps({"message": {"content": "hi"}, "done": True}).encode()])

    monkeypatch.setattr(agent.urllib.request.OpenerDirector, "open", fake_urlopen)
    return sent


def test_a_chat_asks_the_model_to_stay_loaded(captured) -> None:
    """Without this the model unloads while he thinks, and every question waits."""
    reply = OllamaBackend("llama3.1:8b").chat("sys", [{"role": "user", "content": "q"}], lambda _s: None)
    assert reply == "hi"
    assert captured[0]["keep_alive"] == OLLAMA_KEEP_ALIVE
    assert captured[0]["model"] == "llama3.1:8b"
    assert captured[0]["messages"][0] == {"role": "system", "content": "sys"}


def test_warming_loads_the_model_and_keeps_it(captured) -> None:
    assert warm_ollama("llama3.1:8b") is True
    assert captured[0]["keep_alive"] == OLLAMA_KEEP_ALIVE
    assert captured[0]["stream"] is False


def test_warming_a_dead_ollama_is_not_an_error(monkeypatch) -> None:
    """The vault must open whether or not a local model is running."""
    def boom(opener, request, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(agent.urllib.request.OpenerDirector, "open", boom)
    assert warm_ollama("llama3.1:8b") is False
