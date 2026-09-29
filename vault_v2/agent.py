"""Local-only model routing; no external provider or mailbox transport.

This is a client routing policy, not an OS sandbox or an attestation of the
loopback model server. The callers determine the messages sent to the model.
"""

from __future__ import annotations

import json
import re
from http.client import HTTPException
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .document_budget import (DOCUMENT_CONTEXT_TOKENS, DOCUMENT_OUTPUT_TOKENS,
                              DocumentOutputIncomplete, DocumentRequestRefused,
                              admit_document_request)

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DOCUMENT_JOB_SEED = 42

OnChunk = Callable[[str], None]
LOCAL_UNAVAILABLE = "Local-only: selected model unavailable or blocked; cloud fallback is disabled. Check Ollama and local settings."


class LocalModelUnavailable(RuntimeError):
    """Local operation cannot run; callers must not retry externally."""
    retryable = True
    public_message = LOCAL_UNAVAILABLE

    def __init__(self, message: str = LOCAL_UNAVAILABLE):
        super().__init__(message)


class LocalModelConfigurationError(LocalModelUnavailable):
    """Retry requires a settings change, not another discovery request."""
    retryable = False


class OllamaNotRunning(LocalModelUnavailable):
    """The local discovery request could not connect to the service."""
    public_message = "Ollama is not running on this PC"

    def __init__(self):
        super().__init__(self.public_message)


class LocalModelNotInstalled(LocalModelUnavailable):
    """A valid discovery response did not include the exact selected model."""
    def __init__(self, model: str):
        if not _safe_model_selector(model):
            raise LocalModelConfigurationError("Invalid ollama_model configuration: choose a local model name.")
        self.public_message = f"model {model} is not installed — run: ollama pull {model}"
        super().__init__(self.public_message)


@dataclass(frozen=True)
class BackendInfo:
    kind: str  # "ollama"
    model: str
    label: str


class Backend:
    info: BackendInfo

    def chat(self, system: str, messages: list[dict], on_chunk: OnChunk) -> str:  # pragma: no cover
        raise NotImplementedError

    def document_chat(self, system: str, messages: list[dict], on_chunk: OnChunk,
                      *, json_shape: str = "array", document_count: int | None = None) -> str:
        """Document-job entrypoint; non-Ollama backends keep their existing contract."""
        return self.chat(system, messages, on_chunk)


def document_chat(backend: Backend, system: str, messages: list[dict], on_chunk: OnChunk,
                  *, json_shape: str = "array", documents: tuple[str, ...] = (),
                  reading_notes: tuple[str, ...] = (), document_count: int | None = None) -> str:
    """Run one document job without altering subsequent conversational chat.

    Keep simple duck-typed test backends usable without new keyword arguments.
    Routing and the permitted local backend are still chosen by the caller.
    """
    _document_format(json_shape, document_count=document_count)
    admit_document_request(system, messages, documents=documents, reading_notes=reading_notes)
    try:
        method = getattr(backend, "document_chat", None)
        if method is None:
            return backend.chat(system, messages, on_chunk)
        options = {"json_shape": json_shape}
        if json_shape == "health":
            options["document_count"] = document_count
        return method(system, messages, on_chunk, **options)
    except DocumentRequestRefused as exc:
        raise type(exc)(exc.public_message, documents=exc.documents or documents,
                        reading_notes=exc.reading_notes or reading_notes) from None


def _document_format(json_shape: str, *, document_count: int | None = None) -> dict:
    """Constrain syntax at generation time; callers still validate every field."""
    if json_shape == "health":
        if type(document_count) is not int or document_count < 1:
            raise ValueError("Health document count must be a positive integer")
        return {
            "type": "array", "maxItems": 12 * document_count,
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
    if document_count is not None:
        raise ValueError("document count is only accepted for the Health format")
    if json_shape == "array":
        return {"type": "array", "items": {"type": "object"}}
    if json_shape == "object":
        return {"type": "object"}
    raise ValueError("document JSON shape must be array, object, or health")


# -- Ollama -------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_local(request, *, timeout: float):
    url = request if isinstance(request, str) else request.full_url
    if url not in ("http://127.0.0.1:11434/api/tags", "http://127.0.0.1:11434/api/chat"):
        raise urllib.error.URLError("model endpoint refused by local-only policy")
    # A private opener ignores globally installed openers and OS/env proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


def _local_model_name(model: object) -> bool:
    # Conservative rejection of explicit remote hints, not server attestation.
    return (isinstance(model, str) and bool(model.strip())
            and "cloud" not in model.lower() and "://" not in model)


def _safe_model_selector(model: object) -> bool:
    # This value appears in a copyable CLI hint: no options or shell syntax.
    return (_local_model_name(model)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model) is not None)


def _probe_ollama_models(wanted: str | None = None) -> list[str]:
    try:
        with _open_local(f"{OLLAMA_URL}/api/tags", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        # An HTTP error is a response, not evidence of absent installation.
        raise LocalModelUnavailable() from None
    except (urllib.error.URLError, OSError):
        raise OllamaNotRunning() from None
    except ValueError:
        raise LocalModelUnavailable() from None
    rows = data.get("models") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip()
        for row in rows
    ):
        raise LocalModelUnavailable()
    if wanted is not None and any(
        row["name"] == wanted and (row.get("remote_host") or row.get("remote_model"))
        for row in rows
    ):
        # Present but blocked is not evidence that another pull is needed.
        raise LocalModelUnavailable()
    return [m["name"] for m in rows if _local_model_name(m["name"])
            and not m.get("remote_host") and not m.get("remote_model")]


def ollama_models() -> list[str]:
    """Compatibility discovery list; selection uses the precise single probe."""
    try:
        return _probe_ollama_models()
    except LocalModelUnavailable:
        return []


# Ollama unloads a model after five idle minutes, and loading an 8B model
# from cold takes the better part of a minute on this machine. Asking it to
# stay resident turns "ask a question, then wait a minute" into "ask".
OLLAMA_KEEP_ALIVE = "30m"


def warm_ollama(model: str) -> bool:
    """Load the model now, so the owner's first question is not the one that waits."""
    if not _local_model_name(model):
        return False
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ready?"}],
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _open_local(req, timeout=300) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


class OllamaBackend(Backend):
    def __init__(self, model: str):
        if not _local_model_name(model):
            raise LocalModelUnavailable(LOCAL_UNAVAILABLE)
        self.info = BackendInfo("ollama", model, f"local model · {model}")

    def chat(self, system: str, messages: list[dict], on_chunk: OnChunk) -> str:
        return self._chat(system, messages, on_chunk)

    def document_chat(self, system: str, messages: list[dict], on_chunk: OnChunk,
                      *, json_shape: str = "array", document_count: int | None = None) -> str:
        # Request-local settings: no global Ollama/model mutation or chat spillover.
        return self._chat(system, messages, on_chunk,
                          options={"temperature": 0, "seed": DOCUMENT_JOB_SEED,
                                   "num_ctx": DOCUMENT_CONTEXT_TOKENS,
                                   "num_predict": DOCUMENT_OUTPUT_TOKENS},
                          json_format=_document_format(json_shape, document_count=document_count))

    def _chat(self, system: str, messages: list[dict], on_chunk: OnChunk,
              *, options: dict | None = None, json_format: dict | None = None) -> str:
        payload = {
            "model": self.info.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        }
        if options is not None:
            payload["options"] = options
        if json_format is not None:
            payload["format"] = json_format
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        parts: list[str] = []
        completed = False
        done_reason = None
        response_started = False
        interrupted = False
        try:
            with _open_local(req, timeout=600) as resp:
                response_started = True
                for line in resp:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    piece = obj.get("message", {}).get("content", "")
                    if piece:
                        parts.append(piece)
                        on_chunk(piece)
                    if obj.get("done"):
                        completed = True
                        done_reason = obj.get("done_reason")
                        break
        except (urllib.error.URLError, OSError, ValueError):
            if json_format is None or not response_started:
                raise LocalModelUnavailable(LOCAL_UNAVAILABLE) from None
            interrupted = True
        except HTTPException:
            # Chunked HTTP can raise IncompleteRead rather than OSError. Keep
            # ordinary chat's historical exception unchanged; documents refuse
            # a started response instead of falling back to a partial result.
            if json_format is None:
                raise
            if not response_started:
                raise LocalModelUnavailable(LOCAL_UNAVAILABLE) from None
            interrupted = True
        if json_format is not None and (interrupted or not completed or done_reason == "length"):
            reason = ("response interrupted by output limit (ответ прерван лимитом)"
                      if done_reason == "length" else "stream interrupted or ended without completion")
            raise DocumentOutputIncomplete(
                f"Document response incomplete: {reason}; no proposals published. "
                "Try fewer documents. No automatic baseline or partial answer was used.")
        return "".join(parts)


# -- selection ----------------------------------------------------------------


def pick_backend(settings: dict) -> Backend:
    """Select Ollama only; unsupported configuration is an explicit error."""
    if settings.get("agent_backend") not in (None, "auto", "ollama"):
        raise LocalModelConfigurationError(
            "Unsupported agent_backend configuration: only Ollama is available. Choose agent_backend=ollama."
        )
    if settings.get("agent_mode", "local_only") != "local_only":
        raise LocalModelConfigurationError("Unsupported agent_mode configuration: only local_only is available.")
    wanted = settings.get("ollama_model", DEFAULT_OLLAMA_MODEL)
    if not _safe_model_selector(wanted):
        raise LocalModelConfigurationError("Invalid ollama_model configuration: choose a local model name.")
    if wanted not in _probe_ollama_models(wanted):
        raise LocalModelNotInstalled(wanted)
    return OllamaBackend(wanted)
