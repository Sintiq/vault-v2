"""Chat pane — a real conversation with the agent, Claude-like look.

Rules:
- this chat receives the Staging listing (names, sizes) and conversation;
  the separate agent jobs may also receive Staging cards and text excerpts;
- local Ollama remains the backend for every document job; an explicit,
  non-persistent owner switch can route chat alone to a one-shot agent;
- door output is untrusted plain text, never a command or file operation.
"""

from __future__ import annotations

import threading
import os
import stat
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .agent import Backend, LOCAL_UNAVAILABLE, LocalModelUnavailable, document_chat, pick_backend, warm_ollama
from .agent_door import DoorCleanupError, DoorError
from .errors import VaultError
from .file_access import visible_directory, visible_file
from .paths import VaultPaths

# How often to check whether a local model has turned up. Long enough to be
# invisible, short enough that a late-starting local model becomes usable.
SETTLE_MS = 20_000
CHAT_ONLY = "local model unavailable — the agent door covers chat only"


class _LocalJobBackend(Backend):
    """A local transport with the door's scope notice, never a cloud route."""
    def __init__(self, backend):
        self.backend, self.info = backend, backend.info

    def chat(self, system, messages, on_chunk):
        try:
            return self.backend.chat(system, messages, on_chunk)
        except LocalModelUnavailable:
            error = LocalModelUnavailable(CHAT_ONLY)
            error.public_message = CHAT_ONLY
            raise error from None

    def document_chat(self, system, messages, on_chunk, *, json_shape="array", document_count=None):
        try:
            return document_chat(self.backend, system, messages, on_chunk,
                                 json_shape=json_shape, document_count=document_count)
        except LocalModelUnavailable:
            error = LocalModelUnavailable(CHAT_ONLY)
            error.public_message = CHAT_ONLY
            raise error from None

SYSTEM_PROMPT = (
    "You are the Vault assistant inside a local desktop app. You can see ONLY "
    "the list of files in the Staging pane that the owner placed there (names "
    "and sizes). You have no access to other folders, to file contents, or to "
    "the internet. Answer briefly, in the owner's language. Never claim to "
    "have done something you cannot do; any export or write needs the owner's "
    "explicit approval in the app."
)


class _Worker(QObject):
    chunk = Signal(object, str)
    done = Signal(object, str)
    failed = Signal(object, str)
    cleanup_failed = Signal()

    def __init__(self, ticket, run):
        super().__init__()
        self.ticket, self.execute = ticket, run

    def run(self) -> None:
        try:
            text = self.execute(self.ticket, lambda text: self.chunk.emit(self.ticket, text))
            self.done.emit(self.ticket, text)
        except DoorCleanupError:
            self.cleanup_failed.emit()
        except RuntimeError as exc:
            self.failed.emit(self.ticket, str(exc))
        except Exception:
            self.failed.emit(self.ticket, "unusable reply")


class Bubble(QLabel):
    def __init__(self, text: str, kind: str):
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setObjectName({"user": "userBubble", "agent": "agentBubble"}.get(kind, "sysBubble"))
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        self.setMaximumWidth(520)


class ChatPane(QFrame):
    # a turn that arrived over the API; rendered here so the phone and the desk
    # show the same conversation (queued: the HTTP thread must not touch widgets)
    remote_turn = Signal(str, str)
    _remote_event = Signal(object, str, str)
    _state_changed = Signal()
    _cleanup_warning = Signal()

    def __init__(self, staging_dir: Path, settings: dict, parent: QWidget | None = None, *, door=None):
        super().__init__(parent)
        self.setObjectName("chat")
        self.staging_dir = Path(staging_dir).absolute()
        self.settings = settings
        self.door = door
        self.history: list[dict] = []
        self._state_lock = threading.RLock()
        self._epoch = 0
        self._active = None
        self._switching = False
        self._closing = False
        self._write_enabled = True
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._current: Bubble | None = None
        self.backend: Backend | None = None
        self._backend_error: LocalModelUnavailable | None = None

        title = QLabel("Agent")
        title.setObjectName("chatTitle")
        self.mode = QComboBox()
        self.mode.addItem("Local model", None)
        self.mode.addItem("Door: Claude", "claude")
        self.mode.addItem("Door: Codex — not available yet — no tools-free mode verified", "codex")
        self.mode.model().item(2).setEnabled(False)
        self.mode.setEnabled(door is not None)
        self.mode.currentIndexChanged.connect(self._select_mode)
        self.sub = QLabel("")
        self.sub.setObjectName("chatSub")
        self.sub.setWordWrap(True)  # a long status line must not widen the pane
        self.setMinimumWidth(280)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("chatScroll")
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.body.setObjectName("chatBody")
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(12, 8, 12, 8)
        self.body_lay.setSpacing(10)
        self.body_lay.addStretch(1)
        self.scroll.setWidget(self.body)

        self.input = QTextEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("Ask the agent about the files in Staging…  (Ctrl+Enter to send)")
        self.input.setFixedHeight(72)
        self.input.installEventFilter(self)

        self.send = QPushButton("Send")
        self.send.setObjectName("sendBtn")
        self.send.clicked.connect(self.on_send)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("ghostBtn")
        self.clear_btn.clicked.connect(self.clear)

        row = QHBoxLayout()
        row.addWidget(self.clear_btn)
        row.addStretch(1)
        row.addWidget(self.send)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 10)
        lay.setSpacing(6)
        lay.addWidget(title)
        lay.addWidget(self.mode)
        lay.addWidget(self.sub)
        lay.addWidget(self.scroll, 1)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(12, 0, 12, 0)
        wl.addWidget(self.input)
        wl.addLayout(row)
        lay.addWidget(wrap)

        self.remote_turn.connect(self._add)
        self._remote_event.connect(self._show_remote)
        self._state_changed.connect(self._display_status)
        self._cleanup_warning.connect(self._show_cleanup_warning)
        self.refresh_status()
        # At logon Ollama may not be ready yet. Retry only the local selection;
        # unsupported settings stay refused until explicitly changed.
        self._watch = QTimer(self)
        self._watch.setInterval(SETTLE_MS)
        self._watch.timeout.connect(self._look_again)
        self._watch.start()

    def _look_again(self) -> None:
        """Retry a missing local model; never switch to another provider."""
        if self.backend is not None or (self._backend_error is not None and not self._backend_error.retryable):
            self._watch.stop()
            return
        self.refresh_status()
        if self.backend is not None:
            self._watch.stop()
            self.system(f"local model ready: {self.backend.info.model}")

    # -- from the phone -------------------------------------------------------

    def _unavailable_message(self) -> str:
        if self._backend_error is None:
            return LOCAL_UNAVAILABLE
        return (self._backend_error.public_message if self._backend_error.retryable
                else str(self._backend_error))

    def get_local_backend(self):
        """Non-chat jobs keep the local route and closed-door baselines."""
        if self.door is not None and self.door.agent:
            if self.backend is None:
                error = LocalModelUnavailable(CHAT_ONLY)
                error.public_message = CHAT_ONLY
                raise error
            return _LocalJobBackend(self.backend)
        return self.backend

    def remote_state(self) -> dict:
        with self._state_lock:
            return {
                "backend": self.door.label if self.door is not None and self.door.agent else
                           self.backend.info.label if self.backend else self._unavailable_message(),
                "ready": not self._closing and (bool(self.door is not None and self.door.agent)
                                                or self.backend is not None),
                "history": [dict(row) for row in self.history],
            }

    def _reserve(self, text):
        with self._state_lock:
            if self._closing:
                raise DoorError("door is closed")
            if self._switching or self._active is not None or self._thread is not None:
                raise DoorError("door is busy")
            if not self._write_enabled:
                raise DoorError("Vault is busy — try again in a moment")
            door_open = self.door is not None and self.door.agent is not None
            if self.backend is None and not door_open:
                error = LocalModelUnavailable(self._unavailable_message())
                if self._backend_error is not None:
                    error.public_message = self._backend_error.public_message
                    error.retryable = self._backend_error.retryable
                raise error
            ticket = {"epoch": self._epoch, "door": door_open,
                      "generation": self.door.generation if door_open else None,
                      "backend": self.backend}
            self.history.append({"role": "user", "content": text})
            ticket["messages"] = [dict(row) for row in self.history]
            self._active = ticket
            return ticket

    def _valid(self, ticket):
        return ticket["epoch"] == self._epoch and not self._closing

    def _execute(self, ticket, on_chunk):
        try:
            with self._state_lock:
                if not self._valid(ticket):
                    raise DoorError("cancelled")
            listing, files_count = self._staging_snapshot()
            system = SYSTEM_PROMPT + "\n\nFiles currently in Staging:\n" + listing
            if ticket["door"]:
                reply = self.door.request(system, ticket["messages"],
                                          files_count=files_count,
                                          expected_generation=ticket["generation"])
            else:
                reply = ticket["backend"].chat(system, ticket["messages"], on_chunk)
            with self._state_lock:
                if not self._valid(ticket):
                    raise DoorError("cancelled")
                self.history.append({"role": "assistant", "content": reply})
            return reply
        except Exception as exc:
            with self._state_lock:
                if self._valid(ticket) and self.history and self.history[-1]["role"] == "user":
                    self.history.pop()
            if ticket["door"] and not isinstance(exc, DoorError):
                raise DoorError("unusable reply") from None
            raise
        finally:
            with self._state_lock:
                if self._active is ticket:
                    self._active = None
            self._state_changed.emit()

    def _show_remote(self, ticket, text, kind):
        with self._state_lock:
            if not self._valid(ticket):
                return
        self.remote_turn.emit(text, kind)

    def remote_send(self, text: str) -> str:
        """Called on an HTTP thread. One conversation, shared with this pane."""
        ticket = self._reserve(text)
        self._remote_event.emit(ticket, text, "user")
        try:
            reply = self._execute(ticket, lambda _s: None)
        except DoorCleanupError:
            self._cleanup_warning.emit()
            raise
        self._remote_event.emit(ticket, reply, "agent")
        return reply

    # -- status ---------------------------------------------------------------

    def _select_mode(self, _index: int) -> None:
        if self.door is None:
            return
        with self._state_lock:
            self._switching = True
            self._epoch += 1
            self.history.clear()
            self._current = None
        try:
            self.door.select(self.mode.currentData())
        except RuntimeError as exc:
            self.system(str(exc))
        finally:
            with self._state_lock:
                self._switching = False
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.door.agent)))
        self.mode.blockSignals(False)
        self.system(f"— door opened: Claude Code —" if self.door.agent else "— door closed: local model —")
        self._display_status()

    def _display_status(self) -> None:
        may_send = (self._write_enabled and not self._closing and self._active is None
                    and self._thread is None)
        self.sub.setStyleSheet("font-size: 12pt; font-weight: 600;"
                              if self.door is not None and self.door.agent else "")
        if self.door is not None and self.door.agent:
            self.sub.setText(self.door.label)
            self.send.setEnabled(may_send)
        elif self.backend is None:
            message = self._unavailable_message()
            if self._backend_error is None or self._backend_error.retryable:
                message += " · sees only Staging"
            self.sub.setText(message)
            self.send.setEnabled(False)
        else:
            self.sub.setText(f"{self.backend.info.label} · sees only Staging")
            self.send.setEnabled(may_send)

    def set_write_enabled(self, enabled: bool) -> None:
        self._write_enabled = enabled
        self.input.setEnabled(enabled and not self._closing)
        self.mode.setEnabled(self.door is not None and not self._closing)
        self._display_status()

    def has_pending_work(self) -> bool:
        with self._state_lock:
            return self._active is not None or self._thread is not None

    def shutdown(self) -> None:
        """Revoke before any caller waits for workers or releases its lease."""
        with self._state_lock:
            self._closing = True
            self._epoch += 1
            self._current = None
        try:
            if self.door is not None:
                with self.door.log.nonblocking():
                    self.door.close()
        finally:
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(0)
            self.mode.blockSignals(False)
            self.mode.setEnabled(False)
            self._display_status()

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.shutdown()
        except DoorError as exc:
            self.system(exc.public_message)
        if self.has_pending_work():
            event.ignore()
            return
        if hasattr(self, "_watch"):
            self._watch.stop()
        super().closeEvent(event)

    def refresh_status(self) -> None:
        self._backend_error = None
        try:
            self.backend = pick_backend(self.settings)
        except LocalModelUnavailable as exc:
            self.backend = None
            self._backend_error = exc.with_traceback(None)
        self._display_status()
        if self.backend is not None:
            self._warm()

    def _warm(self) -> None:
        """Load a local model in the background, so the first question does not wait."""
        if self.backend is None or self.backend.info.kind != "ollama":
            return
        model = self.backend.info.model
        threading.Thread(target=warm_ollama, args=(model,), daemon=True).start()

    def staging_listing(self) -> str:
        return self._staging_snapshot()[0]

    def _staging_snapshot(self) -> tuple[str, int]:
        paths = VaultPaths(self.staging_dir.parent)

        def linked(info):
            return (stat.S_ISLNK(info.st_mode) or
                    getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

        def directory_identity(path):
            try:
                visible_directory(paths, path)
            except VaultError as exc:
                raise DoorError("unusable reply") from exc
            info = path.lstat()
            if linked(info) or not stat.S_ISDIR(info.st_mode):
                raise DoorError("unusable reply")
            return info.st_dev, info.st_ino

        # Inspect containers before descending; filtering rglob's output is too
        # late because it already enumerated hidden folders and Windows junctions.
        # Rechecks detect ordinary replacement during the snapshot, not a hostile
        # same-user ABA swap; this path-based walk is not an OS sandbox.
        pending = [(self.staging_dir, directory_identity(self.staging_dir))]
        visited = []
        items = []
        files_count = 0
        while pending:
            directory, identity = pending.pop()
            if directory_identity(directory) != identity:
                raise DoorError("unusable reply")
            visited.append((directory, identity))
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or entry.name.lower().endswith(".trash.json"):
                        continue
                    path = directory / entry.name
                    info = path.lstat()
                    attributes = getattr(info, "st_file_attributes", 0)
                    hidden = (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0)
                              | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0))
                    if linked(info) or attributes & hidden:
                        continue
                    rel = str(path.relative_to(self.staging_dir))
                    if stat.S_ISREG(info.st_mode):
                        try:
                            visible_file(paths, path)
                        except VaultError as exc:
                            raise DoorError("unusable reply") from exc
                        items.append((rel, f"- {rel} ({info.st_size} bytes)"))
                        files_count += 1
                    elif stat.S_ISDIR(info.st_mode):
                        try:
                            visible_directory(paths, path)
                        except VaultError as exc:
                            raise DoorError("unusable reply") from exc
                        items.append((rel, f"- {rel}/"))
                        pending.append((path, (info.st_dev, info.st_ino)))
        for directory, identity in visited:
            if directory_identity(directory) != identity:
                raise DoorError("unusable reply")
        return ("\n".join(row for _rel, row in sorted(items)) if items else "(Staging is empty)", files_count)

    # -- messages -------------------------------------------------------------

    def _add(self, text: str, kind: str) -> Bubble:
        b = Bubble(text, kind)
        row = QHBoxLayout()
        if kind == "user":
            row.addStretch(1)
            row.addWidget(b)
        else:
            row.addWidget(b)
            row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        self.body_lay.insertWidget(self.body_lay.count() - 1, holder)
        self._scroll_down()
        return b

    def _scroll_down(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def system(self, text: str) -> None:
        self._add(text, "sys")

    def clear(self) -> None:
        with self._state_lock:
            self._epoch += 1
            self.history.clear()
            self._current = None
        if self.door is not None:
            self.door.cancel()
        while self.body_lay.count() > 1:
            item = self.body_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self.input and ev.type() == ev.Type.KeyPress:
            if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.on_send()
                return True
        return super().eventFilter(obj, ev)

    def on_send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        try:
            ticket = self._reserve(text)
        except RuntimeError as exc:
            self.system(str(exc))
            return
        self.input.clear()
        self._add(text, "user")
        self._current = self._add("…", "agent")
        self._buffer = ""
        self.send.setEnabled(False)

        self._thread = QThread(self)
        self._worker = _Worker(ticket, self._execute)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.cleanup_failed.connect(self._on_cleanup_failed)
        self._thread.start()

    def _on_chunk(self, ticket, piece: str) -> None:
        if not piece or not self._valid(ticket):
            return
        self._buffer += piece
        if self._current is not None:
            self._current.setText(self._buffer)
            self._scroll_down()

    def _finish(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._worker = None
        self._current = None
        self._display_status()

    def _on_done(self, ticket, text: str) -> None:
        if self._valid(ticket) and self._current is not None:
            self._current.setText(text)
        self._finish()

    def _on_failed(self, ticket, err: str) -> None:
        if self._valid(ticket) and self._current is not None:
            self._current.setText(f"Failed: {err}")
        self._finish()

    def _show_cleanup_warning(self) -> None:
        # This is a fixed storage warning, not content from a revoked answer.
        self.system(DoorCleanupError().public_message)

    def _on_cleanup_failed(self) -> None:
        if self._current is not None:
            self._current.setText(DoorCleanupError().public_message)
        else:
            self._show_cleanup_warning()
        self._finish()
