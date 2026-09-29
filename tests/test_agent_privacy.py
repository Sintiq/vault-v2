"""Local-only agent policy at public model-selection and HTTP boundaries."""

from __future__ import annotations

import io
import json
from email.message import Message
import urllib.error
import urllib.request
import urllib.response

import pytest

from vault_v2 import agent


@pytest.fixture()
def transport(monkeypatch):
    """Fake urllib's HTTP boundary: no request can reach a real server."""
    state = {"requests": [], "status": 200, "headers": {},
             "body": {"models": [{"name": "llama3.1:8b"}]}}

    def open_http(handler, request):
        state["requests"].append(request)
        headers = Message()
        for key, value in state["headers"].items():
            headers[key] = value
        body = state.get("raw_body", json.dumps(state["body"]).encode("utf-8") + b"\n")
        response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, state["status"])
        response.msg = "synthetic HTTP response"
        return response

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", open_http)
    monkeypatch.setattr(urllib.request, "_opener", None)
    return state


def test_unsupported_backend_is_a_configuration_error(transport):
    with pytest.raises(agent.LocalModelUnavailable, match="agent_backend") as error:
        agent.pick_backend({"agent_backend": "bridge"})
    assert error.value.retryable is False
    assert error.value.public_message == agent.LOCAL_UNAVAILABLE
    assert transport["requests"] == []


def test_arbitrary_local_error_text_is_not_public():
    error = agent.LocalModelUnavailable("synthetic private server details")
    assert error.retryable is True
    assert error.public_message == agent.LOCAL_UNAVAILABLE
    assert "private" not in error.public_message


@pytest.mark.parametrize("models", [[], [{"name": "llama3.1:8b"}]])
def test_absent_requested_model_does_not_substitute_an_installed_model(transport, models):
    transport["body"] = {"models": models}
    with pytest.raises(agent.LocalModelUnavailable) as error:
        agent.pick_backend({"ollama_model": "wanted:local"})
    expected = "model wanted:local is not installed — run: ollama pull wanted:local"
    assert str(error.value) == error.value.public_message == expected
    assert error.value.retryable is True
    assert len(transport["requests"]) == 1


@pytest.mark.parametrize("backend", [None, "auto", "ollama"])
def test_credentials_do_not_change_local_selection(transport, monkeypatch, backend):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-not-a-secret")
    selected = agent.pick_backend({"agent_backend": backend})
    assert selected.info.kind == "ollama"
    assert selected.info.model == "llama3.1:8b"


@pytest.mark.parametrize("backend", ["anthropic", "bridge", "unknown", "", {}])
def test_unsupported_backend_never_probes_or_reads_credentials(transport, monkeypatch, backend):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-secret")
    with pytest.raises(agent.LocalModelUnavailable, match="agent_backend"):
        agent.pick_backend({"agent_backend": backend})
    assert transport["requests"] == []


@pytest.mark.parametrize("mode", ["hybrid", "cloud", "unknown", "", None, {}])
def test_unsupported_mode_is_a_configuration_error_before_http(transport, mode):
    with pytest.raises(agent.LocalModelUnavailable, match="Unsupported agent_mode.*local_only"):
        agent.pick_backend({"agent_mode": mode})
    assert transport["requests"] == []


@pytest.mark.parametrize("status", [404, 500, 503])
def test_http_failure_is_not_misreported_as_missing_model(transport, status):
    transport["status"] = status
    with pytest.raises(agent.LocalModelUnavailable) as error:
        agent.pick_backend({})
    assert type(error.value) is agent.LocalModelUnavailable
    assert str(error.value) == error.value.public_message == agent.LOCAL_UNAVAILABLE
    assert error.value.retryable is True
    assert len(transport["requests"]) == 1


def test_offline_ollama_has_precise_retryable_message(transport, monkeypatch):
    def offline(handler, request):
        transport["requests"].append(request)
        raise urllib.error.URLError(ConnectionRefusedError("synthetic refusal"))

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", offline)
    with pytest.raises(agent.LocalModelUnavailable) as error:
        agent.pick_backend({})
    assert str(error.value) == error.value.public_message == "Ollama is not running on this PC"
    assert error.value.retryable is True
    assert len(transport["requests"]) == 1


@pytest.mark.parametrize("body", [None, [], {}, {"models": None}, {"models": [None]}, {"models": [{"name": 5}]}])
def test_malformed_discovery_cannot_authorize_missing_model_hint(transport, body):
    transport["body"] = body
    with pytest.raises(agent.LocalModelUnavailable) as error:
        agent.pick_backend({})
    assert type(error.value) is agent.LocalModelUnavailable
    assert str(error.value) == error.value.public_message == agent.LOCAL_UNAVAILABLE
    assert len(transport["requests"]) == 1


@pytest.mark.parametrize("raw", [b"{broken JSON", b"\xff"])
def test_invalid_discovery_bytes_have_no_missing_model_hint(transport, raw):
    transport["raw_body"] = raw
    with pytest.raises(agent.LocalModelUnavailable) as error:
        agent.pick_backend({})
    assert type(error.value) is agent.LocalModelUnavailable
    assert str(error.value) == error.value.public_message == agent.LOCAL_UNAVAILABLE
    assert len(transport["requests"]) == 1


@pytest.mark.parametrize("model", [
    None, {}, "", "-model", "model name", "model\n", "model;command", "model&command",
    "$(command)", "model`command", 'model"', "model:cloud", "https://remote.invalid/model",
])
def test_unsafe_selectors_are_configuration_errors_before_http(transport, model):
    with pytest.raises(agent.LocalModelConfigurationError, match="Invalid ollama_model") as error:
        agent.pick_backend({"ollama_model": model})
    assert error.value.retryable is False
    assert error.value.public_message == agent.LOCAL_UNAVAILABLE
    assert "ollama pull" not in str(error.value)
    assert transport["requests"] == []


def test_missing_model_error_cannot_embed_an_unsafe_command():
    with pytest.raises(agent.LocalModelConfigurationError):
        agent.LocalModelNotInstalled("model;command")


def test_legacy_discovery_still_returns_empty_when_offline(transport, monkeypatch):
    def offline(handler, request):
        raise urllib.error.URLError(ConnectionRefusedError("synthetic refusal"))

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", offline)
    assert agent.ollama_models() == []


@pytest.mark.parametrize("remote_field", ["remote_host", "remote_model"])
def test_installed_but_remote_selected_model_has_no_pull_hint(transport, remote_field):
    transport["body"] = {"models": [{"name": "wanted:local", remote_field: "synthetic-remote"}]}
    with pytest.raises(agent.LocalModelUnavailable) as error:
        agent.pick_backend({"ollama_model": "wanted:local"})
    assert type(error.value) is agent.LocalModelUnavailable
    assert str(error.value) == error.value.public_message == agent.LOCAL_UNAVAILABLE
    assert "ollama pull" not in str(error.value)
    assert len(transport["requests"]) == 1


def _call_model(operation: str):
    if operation == "models":
        return agent.ollama_models()
    if operation == "warm":
        return agent.warm_ollama("llama3.1:8b")
    return agent.OllamaBackend("llama3.1:8b").chat("synthetic system", [], lambda _part: None)


@pytest.mark.parametrize("operation", ["models", "warm", "chat"])
def test_model_requests_ignore_globally_installed_opener(transport, monkeypatch, operation):
    class GlobalOpener:
        def open(self, *args, **kwargs):
            raise AssertionError("global opener must not receive a model request")

    monkeypatch.setattr(urllib.request, "_opener", GlobalOpener())
    _call_model(operation)
    assert len(transport["requests"]) == 1


@pytest.mark.parametrize("operation", ["models", "warm", "chat"])
def test_model_requests_ignore_proxy_settings(transport, monkeypatch, operation):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    _call_model(operation)
    assert [request.host for request in transport["requests"]] == ["127.0.0.1:11434"]


@pytest.mark.parametrize("operation", ["models", "warm", "chat"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_refused_without_second_request(transport, operation, status):
    transport["status"] = status
    transport["headers"] = {"Location": "http://outside.invalid/leak"}
    if operation == "chat":
        with pytest.raises(RuntimeError):
            _call_model(operation)
    else:
        assert _call_model(operation) == ([] if operation == "models" else False)
    assert len(transport["requests"]) == 1


@pytest.mark.parametrize("base", [
    "http://outside.invalid:11434", "http://localhost:11434", "http://127.0.0.1:11435",
    "http://127.0.0.1", "http://user:password@127.0.0.1:11434", "http://127.0.0.1:11434/other",
    "http://127.0.0.1:11434?query=1", "http://127.0.0.1:11434#fragment",
])
@pytest.mark.parametrize("operation", ["models", "warm", "chat"])
def test_only_fixed_loopback_endpoint_is_allowed(transport, monkeypatch, base, operation):
    monkeypatch.setattr(agent, "OLLAMA_URL", base)
    if operation == "chat":
        with pytest.raises(RuntimeError):
            _call_model(operation)
    else:
        assert _call_model(operation) == ([] if operation == "models" else False)
    assert transport["requests"] == []


def test_chat_transport_errors_have_sanitized_local_error_type(transport, monkeypatch):
    def fail(handler, request):
        raise OSError("synthetic sensitive server detail")

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", fail)
    with pytest.raises(agent.LocalModelUnavailable) as error:
        _call_model("chat")
    assert str(error.value) == agent.LOCAL_UNAVAILABLE
    assert "sensitive" not in str(error.value)


def test_discovery_excludes_known_remote_models(transport):
    transport["body"] = {"models": [
        {"name": "llama3.1:8b"}, {"name": "model:cloud"},
        {"name": "other:local", "remote_host": "remote.invalid"},
        {"name": "remote-alias", "remote_model": "remote-id"},
    ]}
    assert agent.ollama_models() == ["llama3.1:8b"]


@pytest.mark.parametrize("body", [None, [], {}, {"models": None}, {"models": {}}, {"models": [None, {}, {"name": 5}]}])
def test_invalid_discovery_shapes_are_unavailable(transport, body):
    transport["body"] = body
    assert agent.ollama_models() == []


@pytest.mark.parametrize("model", ["model:CLOUD", "http://remote.invalid/model", "", None])
def test_direct_remote_model_requests_refuse_without_http(transport, model):
    assert agent.warm_ollama(model) is False
    with pytest.raises(agent.LocalModelUnavailable):
        agent.OllamaBackend(model)
    assert transport["requests"] == []
