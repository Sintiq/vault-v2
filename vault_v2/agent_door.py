"""Explicit, chat-only one-shot agent access; never an automatic fallback."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from threading import Event, RLock
from uuid import uuid4

from .door_process import OneShotRunner, ProcessFailure
from .receipts import ReceiptLog

CODEX_UNAVAILABLE = "not available yet — no tools-free mode verified"
MODELS = ("claude-opus-5-5", "claude-fable-5-1")
LABEL = "Claude Code (Anthropic cloud model) · messages and Staging listing are sent to this agent"


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("invalid JSON constant")


class DoorError(RuntimeError):
    def __init__(self, message: str):
        self.public_message = message
        super().__init__(message)


class DoorCleanupError(DoorError):
    def __init__(self):
        super().__init__("agent door temporary directory cleanup failed; local data may remain in temporary storage")


@dataclass(frozen=True)
class AgentCommand:
    argv: list[str]
    env: dict[str, str] | None = None


class AgentDoor:
    def __init__(self, log: ReceiptLog, settings: dict, *, command=None, runner=None, timeout_s=180):
        self.log, self.settings = log, dict(settings)
        self._command, self._runner, self.timeout_s = command, runner, timeout_s
        self._lock = RLock()
        self._agent = None
        self._generation = 0
        self._active = None

    @property
    def agent(self):
        with self._lock:
            return self._agent

    @property
    def generation(self):
        with self._lock:
            return self._generation

    @property
    def busy(self):
        with self._lock:
            return self._active is not None

    @property
    def label(self):
        return LABEL if self.agent == "claude" else "door is closed"

    def _record(self, op, agent, *, sha256="", size=0, **extra):
        try:
            self.log.append(op, "", "", sha256=sha256, size=size, extra={"agent": agent, **extra})
        except Exception:
            raise DoorError("agent door receipt unavailable") from None

    def select(self, agent):
        if agent not in (None, "claude", "codex"):
            raise DoorError("invalid agent door selection")
        if agent == "codex":
            raise DoorError(CODEX_UNAVAILABLE)
        if agent == "claude" and self.settings.get("agent_door_claude_model", MODELS[0]) not in MODELS:
            raise DoorError("invalid agent_door_claude_model configuration")
        if agent is not None and agent == self.agent:
            return
        old_agent, generation = self._revoke()
        if old_agent is not None:
            self._record("agent_door_close", old_agent)
        if agent is not None:
            try:
                with self.log.write("agent door open"), self._lock:
                    if generation != self._generation:
                        raise DoorError("agent door opening cancelled")
                    self._record("agent_door_open", agent)
                    self._agent = agent
            except DoorError:
                raise
            except Exception:
                raise DoorError("agent door receipt unavailable") from None

    def cancel(self):
        with self._lock:
            self._generation += 1
            if self._active is not None:
                self._active.set()

    def _revoke(self):
        with self._lock:
            agent = self._agent
            self._agent = None
            self.cancel()
            return agent, self._generation

    def close(self):
        agent, _ = self._revoke()
        if agent is not None:
            self._record("agent_door_close", agent)

    def request(self, system: str, messages: list[dict], *, files_count: int = 0,
                expected_generation: int | None = None) -> str:
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise DoorError("agent door request cancelled")
            if self._agent is None:
                raise DoorError("door is closed")
            if self._active is not None:
                raise DoorError("door is busy")
            agent, generation = self._agent, self._generation
            cancel = self._active = Event()
        request_id = uuid4().hex
        recorded = False
        status, reply = "unusable", ""
        try:
            packet = json.dumps({"system": system, "messages": messages}, ensure_ascii=False,
                                sort_keys=True, separators=(",", ":")).encode("utf-8")
            if self._command is None:
                from .door_agents import claude_command
                command = claude_command(agent, self.settings)
            else:
                command = self._command(agent, self.settings)
            self._record("agent_door_request", agent, sha256=sha256(packet).hexdigest(),
                         size=len(packet), files_count=files_count, request_id=request_id)
            recorded = True
            output = (self._runner or OneShotRunner()).run(
                command.argv, packet, cancel=cancel, timeout_s=self.timeout_s, env=command.env)
            result = json.loads(output.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_invalid_constant)
            if (not isinstance(result, dict) or result.get("type") != "result"
                    or result.get("subtype") != "success" or result.get("is_error") is not False
                    or not isinstance(result.get("result"), str) or not result["result"].strip()
                    or len(result["result"].encode("utf-8")) > 65536):
                raise DoorError("unusable reply")
            reply = result["result"]
            status = "accepted"
        except ProcessFailure as exc:
            status = exc.status
        except DoorError:
            if not recorded:
                raise
        except Exception:
            status = "unusable"
        finally:
            try:
                if recorded:
                    # Lock order stays root -> door. No process or I/O wait lives here.
                    # HTTP admission is fail-fast; outcome audit after process
                    # teardown must wait out a simultaneous close receipt.
                    with self.log.completion_write("agent door result"), self._lock:
                        if status != "cleanup_failed" and (cancel.is_set()
                                or generation != self._generation or agent != self._agent):
                            status = "cancelled"
                        self._record("agent_door_result", agent, status=status, request_id=request_id,
                                     sha256=sha256(reply.encode("utf-8")).hexdigest() if status == "accepted" else "")
            except Exception:
                raise DoorError("agent door receipt unavailable") from None
            finally:
                with self._lock:
                    if self._active is cancel:
                        self._active = None
        if status == "accepted":
            return reply
        if status == "timeout":
            raise DoorError(f"no answer from Claude Code in {self.timeout_s:g} s")
        if status == "cancelled":
            raise DoorError("agent door request cancelled")
        if status == "cleanup_failed":
            raise DoorCleanupError()
        raise DoorError("unusable reply")
