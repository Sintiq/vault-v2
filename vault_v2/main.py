"""Vault V2 main window: three Commander-style panes + agent chat."""

from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path

from PySide6.QtCore import QSize, QLocale, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .agent_api import AgentAPI
from .agent import LocalModelUnavailable
from .agent_scope import AgentTextScope
from .agent_door import AgentDoor, DoorError
from .api import ApiServer, VaultAPI, api_port, bind_host, existing_legacy_key, front_door, tailscale_ip
from .ask import collect_archive_docs
from .ask_dialog import AskDialog
from .backup_dialog import BackupDialog
from .cards import CardStore, load_shelves
from .chat import ChatPane
from .dialogs import confirm
from .export_dialog import ExportDialog
from .gatekeeper import Gatekeeper
from .health import HealthStore
from .health_pane import HealthPane
from .ops import VaultError, VaultOps
from .shelves_dialog import ShelvesDialog
from .viewer import FileViewer
from .sort_dialog import SortDialog
from .sorting import collect_inputs
from .panes import Pane
from .devices_dialog import DevicesDialog, PairDialog
from .devices import DeviceRegistry
from .setup import Setup
from .setup_dialog import SetupDialog
from .reader import StagingReader
from .receipts import ReceiptLog
from .runtime import RuntimeLease, updater_python
from .errors import VaultBusy
from .reminders import remind
from .tasks import TaskStore
from .tasks_pane import TasksPane
from .paths import PANE_NAMES, VaultPaths, default_root
from .icons import glyph_icon
from .theme import DARK, LIGHT, stylesheet
from .trash_dialog import TrashDialog


# Reminders: a first look shortly after the window opens (Tailscale and the
# phone may still be waking up), then once a day.
REMIND_FIRST_MS = 90_000
REMIND_EVERY_MS = 24 * 60 * 60 * 1000


class _ScopeWarmThread(QThread):
    completed = Signal(str)

    def __init__(self, scope: AgentTextScope, folder: Path, parent):
        super().__init__(parent)
        self.scope, self.folder = scope, folder

    def run(self) -> None:
        try:
            notes = self.scope.warm(self.folder)
            message = f"Agent text preparation finished: {self.folder.name}"
            if notes:
                message += " · " + " · ".join(notes)
        except Exception as exc:  # noqa: BLE001 - report a failed background job without losing its lease
            message = f"Agent text preparation stopped: {exc}"
        self.completed.emit(message)


class PendingOperationsDialog(QDialog):
    """Display evidence only; closing is not an acknowledgement."""

    def __init__(self, log, rows: list[dict], parent=None):
        super().__init__(parent)
        self.log, self.rows = log, rows
        self.setWindowTitle("Operations may have applied — review pending evidence")
        self.resize(680, 380)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlainText("\n\n".join(
            f"{row.get('op', 'unknown')} · {row.get('ts', 'unknown')}\n"
            f"{json.dumps(row.get('paths', {}), ensure_ascii=False, indent=2)}\n"
            f"{row.get('error', '')}" for row in rows
        ))
        self.status = QLabel("No operation has been repeated, reversed, or marked successful.")
        self.status.setWordWrap(True)
        self.ack_btn = QPushButton("Acknowledge reviewed evidence")
        self.ack_btn.setAutoDefault(False)
        self.ack_btn.setEnabled(not log.read_only)
        self.ack_btn.clicked.connect(self.acknowledge)
        close = QPushButton("Close without acknowledgement")
        close.setDefault(True)
        close.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.details)
        layout.addWidget(self.ack_btn)
        layout.addWidget(close)

    def acknowledge(self) -> None:
        if self.log.read_only:
            return
        try:
            with self.log.nonblocking():
                self.log.acknowledge_pending([row["id"] for row in self.rows])
        except (VaultError, OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self.accept()


class MainWindow(QMainWindow):
    background_status = Signal(str)

    def __init__(self, root: Path, *, start_services: bool = True, door_factory=AgentDoor):
        super().__init__()
        self.runtime = RuntimeLease(root)
        self.read_only = self.runtime.read_only
        self.read_only_reason = self.runtime.reason
        self.paths = VaultPaths(Path(root))
        try:
            if self.runtime.reclaimed is not None:
                ReceiptLog(self.paths.receipts).append(
                    "runtime_lock_reclaimed", self.paths.root / ".vault.lock", self.paths.root / ".vault.lock",
                    note="Previous runtime process is demonstrably absent.", extra=self.runtime.reclaimed,
                )
            self.ops = VaultOps(self.paths, read_only=self.read_only)
        except (VaultError, ValueError, OSError) as exc:
            self.read_only = True
            self.read_only_reason = f"read-only: {exc}"
            self.ops = VaultOps(self.paths, read_only=True)
        self._ui_nonblocking = self.ops.log.prefer_nonblocking()
        self._ui_nonblocking.__enter__()
        load_shelves(self.paths.settings())
        self.cards = CardStore(self.paths.root / ".cards", self.ops.log)
        self.gate = Gatekeeper(self.ops)
        self.reader = StagingReader(self.ops, self.cards, purpose="agent")
        self.tasks = TaskStore(self.paths.root / ".tasks", self.ops.log)
        self.health = HealthStore(self.paths.root / ".health", self.ops.log)
        self.titles = self.paths.titles()
        self.dark = bool(self.paths.settings().get("dark", False))
        self.active: Pane | None = None
        self.api_server: ApiServer | None = None
        self._retired_api_servers = []
        self._reminder_workers = []
        self._background_dialogs: set[QDialog] = set()
        self._scope_workers: set[QThread] = set()
        self.text_scope = None if self.read_only else AgentTextScope(self.paths, self.ops.log)
        self.text_queue = self.text_scope.queue if self.text_scope is not None else None
        self._closing = False

        self.setWindowTitle(f"Vault V2 · {root}" + (f" · {self.read_only_reason}" if self.read_only else ""))
        self.resize(1500, 860)

        self.panes: dict[str, Pane] = {}
        splitter = QSplitter(Qt.Orientation.Horizontal)
        for name in PANE_NAMES:
            pane = Pane(name, self.titles[name], self.paths.pane(name), self.ops, self.cards)
            pane.status.connect(self._status)
            pane.focused.connect(self._set_active)
            pane.manage_shelves.connect(self.show_shelves)
            pane.scope_granted.connect(self._warm_text_scope)
            self.panes[name] = pane
            splitter.addWidget(pane)
        self.side = QTabWidget()
        self.side.setObjectName("side")
        self.side.setDocumentMode(True)
        if self.read_only:
            self.chat = QLabel("Read-only window: browse files here. Agent, tasks, health edits and phone services remain in the writable window.")
            self.chat.setWordWrap(True)
            self.chat.backend = None
            self.tasks_pane = self.health_pane = None
            self.side.addTab(self.chat, "Read-only")
        else:
            self.chat = ChatPane(self.paths.staging, self.paths.settings(),
                                 door=door_factory(self.ops.log, self.paths.settings()))
            self.tasks_pane = TasksPane(self.tasks, self.reader, self.chat.get_local_backend)
            self.tasks_pane.status.connect(self._status)
            self.health_pane = HealthPane(self.health, self.reader, self.chat.get_local_backend)
            self.health_pane.status.connect(self._status)
            self.side.addTab(self.chat, "Agent")
            self.side.addTab(self.tasks_pane, "Tasks")
            self.side.addTab(self.health_pane, "Health")
        self.side.setMinimumWidth(280)
        splitter.addWidget(self.side)
        splitter.setSizes([430, 430, 430, 360])
        for i, f in enumerate((3, 3, 3, 2)):
            splitter.setStretchFactor(i, f)

        host = QWidget()
        lay = QHBoxLayout(host)
        lay.setContentsMargins(10, 6, 10, 10)
        lay.addWidget(splitter)
        self.setCentralWidget(host)

        # One registry for the whole window: a server restart keeps pairings in flight
        # and the record of which devices have been seen.
        self.devices = DeviceRegistry(self.paths.root, self.ops.log)
        self._setup: Setup | None = None
        self._toolbar()
        self.setStatusBar(QStatusBar())
        self.background_status.connect(self._status)
        self.guard_status = QLabel("")
        self.statusBar().addPermanentWidget(self.guard_status)
        self._guard_timer = QTimer(self)
        self._guard_timer.setInterval(100)
        self._guard_timer.timeout.connect(self._update_write_state)
        self._guard_timer.start()
        self._update_write_state()
        self._apply_theme()
        self._verify_receipts()
        # The phone expects the vault to answer whenever this window is open;
        # a port clash must not stop the desk from working.
        if not self.read_only and start_services:
            try:
                self._start_api()
            except (VaultError, OSError) as exc:
                self.statusBar().showMessage(f"phone server not started: {exc}", 8000)
        self.panes["staging"].view.setFocus()
        if not self.read_only and start_services:
            self._start_reminders()
            # A new vault with no device at all yet: open the wizard once the window is up.
            if self.api_server is not None and not self.api_server.api.devices.all():
                QTimer.singleShot(800, self.show_setup)
        self.pending_dialog = None
        self._pending_startup_timer = QTimer(self)
        self._pending_startup_timer.setSingleShot(True)
        self._pending_startup_timer.timeout.connect(self._startup_pending)
        self._pending_startup_timer.start(0)
        self._text_startup_timer = QTimer(self)
        self._text_startup_timer.setSingleShot(True)
        self._text_startup_timer.timeout.connect(self._warm_marked_folders)
        if not self.read_only:
            self._text_startup_timer.start(0)

    # -- reminders ------------------------------------------------------------

    def _warm_marked_folders(self) -> None:
        if self.read_only or self._closing:
            return
        try:
            folders = self.text_scope.grants()
        except (VaultError, OSError) as exc:
            self._status(f"Agent text preparation stopped: {exc}")
            return
        for folder in folders:
            self._warm_text_scope(folder)

    def _warm_text_scope(self, folder: Path) -> None:
        if self.read_only or self._closing:
            return
        worker = _ScopeWarmThread(AgentTextScope(self.paths, self.ops.log), folder, self)
        self._scope_workers.add(worker)
        worker.completed.connect(self._status)
        worker.finished.connect(lambda: self._scope_workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _start_reminders(self) -> None:
        """Once now, then once a day: tasks that are due become a knock on the phone."""
        if self.read_only:
            return
        self._remind_timer = QTimer(self)
        self._remind_timer.setInterval(REMIND_EVERY_MS)
        self._remind_timer.timeout.connect(self._remind_now)
        self._remind_timer.start()
        QTimer.singleShot(REMIND_FIRST_MS, self._remind_now)

    def _remind_now(self) -> None:
        # Network is done on a thread; the status bar gets the one-line result.
        if self.read_only or self._closing:
            return
        import threading

        def run() -> None:
            try:
                status = remind(self.paths.root, self.tasks, self.ops.log)
            except (VaultError, OSError) as exc:
                status = f"reminders not completed: {exc}"
            self.background_status.emit(status)

        worker = threading.Thread(target=run, daemon=True)
        self._reminder_workers.append(worker)
        worker.start()

    # -- toolbar / shortcuts --------------------------------------------------

    def _toolbar(self) -> None:
        self.write_actions = []
        self.icon_actions: list[QAction] = []
        tb = QToolBar("main")
        tb.setMovable(False)
        # Windows' own glyphs beside the words, like the Explorer next door.
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        def act(text: str, key: str | None, slot, *, writes: bool = True, toolbar: bool = True,
                icon: str | None = None, tip: str | None = None) -> QAction:
            a = QAction(text, self)
            if key:
                a.setShortcut(QKeySequence(key))
            if tip:
                a.setToolTip(tip + (f"  ({key})" if key else ""))
            def run():
                try:
                    slot()
                except (VaultError, OSError) as exc:
                    self._status(str(exc))
            a.triggered.connect(run)
            if toolbar:
                tb.addAction(a)
            self.addAction(a)
            if writes:
                self.write_actions.append(a)
            if icon:
                a.setProperty("icon_name", icon)
                self.icon_actions.append(a)
            return a

        # In and out of the vault first, then what you do with a selection,
        # then the agent, then housekeeping, then the phone and the records.
        act("Upload…", "Ctrl+O", self.import_files, icon="upload", tip="Bring files into Staging")
        act("Export…", "Ctrl+E", self.export_staging, icon="export",
            tip="Take files out of the vault, through the gate")
        tb.addSeparator()
        act("F5 Copy", "F5", lambda: self._transfer(move=False), icon="copy",
            tip="Copy the selected files to the other pane")
        act("F6 Move", "F6", lambda: self._transfer(move=True), icon="move",
            tip="Move the selected files to the other pane")
        act("F7 Folder", "F7", lambda: self._active().new_folder(), icon="folder",
            tip="New folder in the active pane")
        act("F8 Trash", "F8", lambda: self._active().trash_selected(), icon="delete",
            tip="Send the selected files to Trash (they can be restored)")
        tb.addSeparator()
        act("Sort…", None, self.sort_staging, icon="sort", tip="Let the agent suggest shelves for what is in Staging")
        act("Ask…", None, self.ask_staging, icon="ask", tip="Find documents by what you need them for")
        tb.addSeparator()
        act("Clear Staging", None, self.clear_staging, icon="broom", tip="Move everything in Staging to Trash")
        act("Trash…", None, self.show_trash, icon="restore", tip="View Trash and restore files")
        act("Backup…", "Ctrl+B", self.show_backup, icon="save", tip="Back up or restore the whole vault")
        act("Manage shelves…", "Ctrl+Shift+S", self.show_shelves, toolbar=False)
        tb.addSeparator()
        # The phone, the records and the theme are glyph-only, with the word as a tooltip:
        # they are looked at, not worked with, and the bar has to fit beside the filter.
        compact = [
            act("Setup…", None, self.show_setup, icon="setup"),
            act("Phone…", "Ctrl+P", self.show_phone, icon="phone"),
            act("Receipts", None, self.show_receipts, writes=False, icon="receipts"),
            act("Pending…", None, self.show_pending, writes=False, icon="pending"),
        ]
        self.theme_action = act("Theme", "Ctrl+D", self.toggle_theme, writes=False, icon="moon")
        compact.append(self.theme_action)
        for a in compact:
            a.setToolTip(a.text().rstrip("…") + (f"  ({a.shortcut().toString()})" if not a.shortcut().isEmpty() else ""))
            tb.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter: name, shelf, topic, year…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMinimumWidth(150)
        self.filter.setMaximumWidth(240)
        self.filter.textChanged.connect(lambda t: [p.set_filter(t) for p in self.panes.values()])
        tb.addSeparator()
        tb.addWidget(self.filter)
        a = QAction("Next pane", self)
        a.setShortcut(QKeySequence("Tab"))
        a.triggered.connect(self._next_pane)
        self.addAction(a)

        # Keep the owner's toolbar order intact. Updates are an explicit menu
        # command, available also in read-only windows, never a startup job.
        self.updates_action = act("Check for updates…", None, self.show_updates,
                                  writes=False, toolbar=False,
                                  tip="Close this window and open Updates; nothing installs automatically")
        self.menuBar().addMenu("Vault").addAction(self.updates_action)

    # -- helpers --------------------------------------------------------------

    def pane_title(self, name: str) -> str:
        return self.titles.get(name, name)

    def _update_write_state(self) -> None:
        guard = self.ops.log.guard
        blocked = self.read_only or self._closing or guard.busy or guard.blocked
        for action in self.write_actions:
            action.setEnabled(not blocked)
        for pane in self.panes.values():
            pane.set_write_enabled(not blocked)
        self.side.setEnabled(not self.read_only)
        if not self.read_only:
            self.chat.set_write_enabled(not blocked)
            self.tasks_pane.setEnabled(not blocked)
            self.health_pane.setEnabled(not blocked)
        self.guard_status.setText(
            self.read_only_reason if self.read_only else
            "closing — waiting for running requests to finish" if self._closing else
            "writes blocked — an operation may have applied; restart and review" if guard.blocked else
            f"busy: {guard.status or 'writing vault'}" if guard.busy else ""
        )

    def _can_write(self) -> bool:
        self._update_write_state()
        return not (self.read_only or self._closing or self.ops.log.guard.busy or self.ops.log.guard.blocked)

    def show_pending(self) -> None:
        try:
            rows = self.ops.log.pending()
        except VaultBusy as exc:
            self.statusBar().showMessage(str(exc))
            return
        if not rows:
            return
        self.pending_dialog = PendingOperationsDialog(self.ops.log, rows, self)
        self.pending_dialog.show()

    def _startup_pending(self) -> None:
        if self._closing:
            return
        try:
            rows = self.ops.log.pending()
        except VaultBusy:
            self._pending_startup_timer.start(250)
            return
        if rows:
            self.pending_dialog = PendingOperationsDialog(self.ops.log, rows, self)
            self.pending_dialog.show()

    def _active(self) -> Pane:
        return self.active or self.panes["staging"]

    def _set_active(self, pane: Pane) -> None:
        self.active = pane

    def _next_pane(self) -> None:
        names = list(PANE_NAMES)
        nxt = names[(names.index(self._active().name) + 1) % len(names)]
        self.panes[nxt].view.setFocus()

    def _other_pane(self) -> Pane:
        """Target for F5/F6: the pane to the right of the active one (wraps)."""
        names = list(PANE_NAMES)
        return self.panes[names[(names.index(self._active().name) + 1) % len(names)]]

    def _status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 6000)
        for p in self.panes.values():
            p.refresh()

    def _verify_receipts(self) -> None:
        if self.read_only:
            return
        try:
            n = self.ops.log.verify()
            self.statusBar().showMessage(f"receipts: {n}, chain intact", 4000)
        except VaultBusy as exc:
            self.statusBar().showMessage(str(exc))
        except ValueError as exc:
            QMessageBox.critical(self, "Vault", f"Receipt chain is broken:\n{exc}")

    # -- actions --------------------------------------------------------------

    def import_files(self) -> None:
        if not self._can_write():
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Upload into Staging")
        n = 0
        for f in files:
            try:
                self.ops.import_file(Path(f), "staging")
                n += 1
            except (VaultError, OSError) as exc:
                QMessageBox.warning(self, "Vault", f"{f}: {exc}")
        if n:
            self._status(f"uploaded into Staging: {n}")

    def _transfer(self, move: bool) -> None:
        if not self._can_write():
            return
        src_pane = self._active()
        dst_pane = self._other_pane()
        paths = src_pane.selected_paths()
        if not paths:
            self._status("nothing selected")
            return
        dest = dst_pane.current_dir()
        n = 0
        for p in paths:
            try:
                (self.ops.move if move else self.ops.copy)(p, dest)
                n += 1
            except (VaultError, OSError) as exc:
                QMessageBox.warning(self, "Vault", f"{p.name}: {exc}")
        self._status(f"{'moved' if move else 'copied'} to {dst_pane.title.text()}: {n}")

    def sort_staging(self) -> None:
        if not self._can_write():
            return
        try:
            backend = self.chat.get_local_backend()
        except LocalModelUnavailable as exc:
            self._status(exc.public_message)
            return
        dlg = SortDialog(self.cards, lambda: collect_inputs(self.paths.staging, self.cards, reader=self.reader), backend, self)
        dlg.exec()
        self._retain_running_dialog(dlg)
        if dlg.confirmed:
            self._status(f"cards confirmed: {dlg.confirmed}")

    def ask_staging(self) -> None:
        if not self._can_write():
            return
        try:
            backend = self.chat.get_local_backend()
        except LocalModelUnavailable as exc:
            self._status(exc.public_message)
            return
        dlg = AskDialog(self.gate, lambda: collect_archive_docs(self.paths, self.cards), backend, self)
        dlg.exec()
        self._retain_running_dialog(dlg)
        if dlg.exported is not None:
            self._status(f"pack exported → {dlg.exported.destination}")

    def _retain_running_dialog(self, dialog: QDialog) -> None:
        workers = [worker for worker in dialog.findChildren(QThread) if worker.isRunning()]
        if not workers:
            return
        # Qt parentage alone does not retain every Python dialog wrapper after
        # exec() returns. Keep its Python-owned worker until completion drains.
        self._background_dialogs.add(dialog)
        for worker in workers:
            worker.finished.connect(self._release_finished_dialogs)

    def _release_finished_dialogs(self) -> None:
        self._background_dialogs = {
            dialog for dialog in self._background_dialogs
            if any(worker.isRunning() for worker in dialog.findChildren(QThread))
        }

    def export_staging(self) -> None:
        """Export the selected Staging items, or all of Staging if nothing is selected."""
        if not self._can_write():
            return
        staging = self.panes["staging"]
        sources = staging.selected_paths() if self._active() is staging else []
        if not sources:
            sources = [p for p in staging.current_dir().iterdir() if not p.name.startswith(".")]
        if not sources:
            self._status("Staging is empty — nothing to export")
            return
        dlg = ExportDialog(self.gate, sources, self)
        dlg.exec()
        if dlg.result is not None:
            self._status(f"exported {len(dlg.result.written)} file(s) → {dlg.result.destination}")

    def clear_staging(self) -> None:
        if not self._can_write():
            return
        if not confirm(self, "Clear Staging", "Everything in Staging goes to Trash. Originals in the other panes are untouched."):
            return
        try:
            n = self.ops.clear_staging()
        except (VaultError, OSError) as exc:
            self._status(str(exc))
            return
        self._status(f"Staging cleared, moved to Trash: {n}")

    def show_shelves(self) -> None:
        if not self._can_write():
            return
        dlg = ShelvesDialog(self.paths, self.cards, self)
        dlg.exec()
        if dlg.changed:
            self._status("shelves updated")

    def show_backup(self) -> None:
        if not self._can_write():
            return
        dlg = BackupDialog(self.paths.root, self.ops.log, self)
        dlg.exec()
        if dlg.result is not None:
            self._status(f"backup verified → {dlg.result.path}")

    def show_trash(self) -> None:
        if not self._can_write():
            return
        dlg = TrashDialog(self.ops, self)
        dlg.exec()
        if dlg.restored:
            self._status(f"restored: {dlg.restored}")

    # -- the phone ------------------------------------------------------------

    def _start_api(self) -> None:
        """Bring the phone server up. Never 0.0.0.0: either our Tailscale
        address, or localhost when Tailscale itself fronts us with HTTPS."""
        if self.read_only or self._closing:
            return
        if self.api_server is not None and self.api_server.running:
            return
        if self.api_server is not None:
            self._retired_api_servers.append(self.api_server)
        settings = self.paths.settings()
        agent = AgentAPI(self.paths.staging, self.reader, self.cards, self.tasks, self.health,
                         self.chat.get_local_backend)
        api = VaultAPI(self.ops, self.cards, self.tasks, self.health, agent, self.devices)
        self.api_server = ApiServer(
            api,
            existing_legacy_key(self.paths.root),
            Path(__file__).resolve().parent / "web" / "index.html",
            self.chat.remote_send,
            self.chat.remote_state,
            bind_host(settings),
            api_port(settings),
        )
        self.api_server.start()
        self._watch_binding()
        where = self.phone_url()
        self._status(f"phone server on {where}" +
                     ("" if tailscale_ip() else " (Tailscale is down — PC only)"))

    def phone_url(self) -> str:
        """What to show the owner: the Tailscale name when Tailscale fronts us."""
        if front_door(self.paths.settings()) == "proxied":
            name = (self.paths.settings().get("tailscale_name") or "").strip().rstrip(".")
            if name:
                return f"https://{name}/"
        return self.api_server.url

    def _watch_binding(self) -> None:
        """Move onto the Tailscale address if it only turns up later.

        Bound to localhost the vault is invisible to the phone, and after a
        reboot that is exactly where it can land — Tailscale is often not
        ready when the session starts. So it keeps looking, and moves itself
        rather than waiting to be restarted by hand.
        """
        if self.read_only or front_door(self.paths.settings()) != "direct":
            return
        if self.api_server is None or self.api_server.host != "127.0.0.1":
            return
        timer = QTimer(self)
        timer.setInterval(30_000)

        def look() -> None:
            if self._closing:
                timer.stop()
                return
            if self.api_server is None or self.api_server.host != "127.0.0.1":
                timer.stop()
                return
            ip = tailscale_ip(attempts=1)
            if not ip:
                return
            timer.stop()
            key, page = self.api_server.key, self.api_server.page
            port = self.api_server.port
            self.api_server.stop()
            self._retired_api_servers.append(self.api_server)
            agent = AgentAPI(self.paths.staging, self.reader, self.cards, self.tasks, self.health,
                             self.chat.get_local_backend)
            api = VaultAPI(self.ops, self.cards, self.tasks, self.health, agent, self.devices)
            self.api_server = ApiServer(api, key, page, self.chat.remote_send,
                                        self.chat.remote_state, ip, port)
            self.api_server.start()
            self._status(f"Tailscale came up; the phone can reach the vault at {self.api_server.url}")

        timer.timeout.connect(look)
        timer.start()

    def _stop_api(self) -> None:
        if self.api_server is not None:
            drained = self.api_server.stop()
            self._status("phone server stopped" if drained else "phone server stopped; existing requests are finishing")

    def show_updates(self) -> None:
        """Close normally before a separate installed updater; never force close."""
        if getattr(self, "_opening_updates", False):
            return
        self._opening_updates = True
        closed = False
        try:
            try:
                executable = updater_python()
            except (OSError, RuntimeError):
                QMessageBox.warning(self, "Updates", "The installed updater is unavailable. Repair the installation before trying again.")
                return
            if executable is None:
                QMessageBox.information(self, "Updates", "Updates are available in the installed Windows app. This source checkout will stay open.")
                return
            answer = QMessageBox.question(
                self, "Open Updates?",
                "Close this Vault window and open the separate updater?\n"
                "Nothing downloads or installs automatically. Other Vault windows must also be closed before installing.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            if not self.close():
                # closeEvent keeps ownership while workers/phone requests drain.
                return
            closed = True
            try:
                subprocess.Popen(
                    [executable, "-I", "-B", "-m", "vault_v2.update_dialog"],
                    cwd=Path(executable).parent.parent,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                # The root lease is already released: do not resurrect this
                # closed window as a writer. No installation success is claimed.
                QMessageBox.warning(None, "Updates", "The updater could not start. Open Vault V2 Updates from the Start menu, or repair the installation.")
        finally:
            if not closed:
                self._opening_updates = False

    def show_setup(self) -> None:
        """The wizard: Tailscale, HTTPS, the Vault route, a paired phone — each checked, not assumed."""
        if not self._can_write():
            return
        try:
            self._start_api()
        except OSError as exc:
            QMessageBox.warning(self, "Setup", f"Could not start the phone server:\n{exc}")
            return
        if self._setup is None:
            web = Path(__file__).resolve().parent / "web"
            self._setup = Setup(self.paths.root, self.ops.log, lambda: self.devices, self.api_server.port,
                                front=lambda: front_door(self.paths.settings()),
                                apk_present=lambda: (web / "vault.apk").is_file())
        setup = self._setup

        def add_phone():
            dlg = PairDialog(self.devices, self.phone_url(), "app", self)
            dlg.exec()
            if dlg.paired is not None:
                setup.note_paired(dlg.paired.id)

        def served(name: str):
            self._set_front("proxied", name)

        def unserved():
            # This PC only: phones lose the door until it is served again. No plain HTTP fallback.
            self._set_front("local", None)

        SetupDialog(setup, on_add_phone=add_phone, on_served=served, on_unserved=unserved, parent=self).exec()

    def _set_front(self, front: str, name: str | None) -> None:
        """Record how phones reach the vault, then move the server to match."""
        settings = self.paths.settings()
        if settings.get("front") == front and (name is None or settings.get("tailscale_name") == name):
            return
        settings["front"] = front
        if name:
            settings["tailscale_name"] = name
        with self.ops.log.write("phone front door"):
            self.ops.log.effect()
            tmp = self.paths.settings_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.paths.settings_file)
            self.ops.log.append("setup_front_changed", front, self.paths.settings_file)
        if self.api_server is not None:
            self.api_server.stop()
        self._start_api()

    def show_phone(self) -> None:
        if not self._can_write():
            return
        try:
            self._start_api()
        except OSError as exc:
            QMessageBox.warning(self, "Phone", f"Could not start the phone server:\n{exc}")
            return
        url = self.phone_url()
        dlg = DevicesDialog(url, self.devices, url.startswith("https") or
                            self.api_server.host not in ("127.0.0.1", "localhost"), self)
        dlg.stop_requested.connect(self._stop_api)
        dlg.exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self.read_only:
            try:
                self.chat.shutdown()
            except DoorError as exc:
                self.statusBar().showMessage(exc.public_message)
        if self.ops.log.guard.busy or self._running_workers():
            self.statusBar().showMessage("Vault is busy — finish the running work before closing.")
            event.ignore()
            return
        self._closing = True
        servers = self._retired_api_servers + ([self.api_server] if self.api_server is not None else [])
        drained = [server.stop() for server in servers]
        if not all(drained):
            self.statusBar().showMessage("Finishing a phone request — keep this window open, then close again.")
            event.ignore()
            return
        if self.text_queue is not None and not self.text_queue.close_if_idle():
            self.statusBar().showMessage("Finishing archive text preparation — keep this window open, then close again.")
            event.ignore()
            return
        previous_read_only = self.ops.log.read_only
        self.ops.log.read_only = True
        try:
            self.runtime.close()
        except OSError as exc:
            self.ops.log.read_only = previous_read_only
            self.statusBar().showMessage(f"Runtime lease could not be released safely: {exc}")
            event.ignore()
            return
        self._guard_timer.stop()
        self._pending_startup_timer.stop()
        self._text_startup_timer.stop()
        if hasattr(self, "_remind_timer"):
            self._remind_timer.stop()
        if hasattr(self.chat, "_watch"):
            self.chat._watch.stop()
        if self.pending_dialog is not None:
            self.pending_dialog.reject()
        for preview in self.findChildren(FileViewer):
            preview.close()
        self._ui_nonblocking.__exit__(None, None, None)
        super().closeEvent(event)

    def _running_workers(self) -> bool:
        if self.text_queue is not None and self.text_queue.busy:
            return True
        if not self.read_only and self.chat.has_pending_work():
            return True
        if any(worker.is_alive() for worker in self._reminder_workers):
            return True
        # Hidden/rejected Sort and Ask dialogs still own asynchronous workers.
        # Their completion slots can mutate stores, so retain the lease too.
        return any(worker.isRunning() for worker in self.findChildren(QThread))

    def show_receipts(self) -> None:
        try:
            n = self.ops.log.verify()
            rows = self.ops.log.tail(20)
        except VaultBusy as exc:
            self.statusBar().showMessage(str(exc))
            return
        except ValueError as exc:
            QMessageBox.critical(self, "Receipts", str(exc))
            return
        text = "\n".join(
            f"{r['ts'][11:19]}  {r['op']:8}  {Path(r['src']).name} → {Path(r['dst']).name}  {r['sha256'][:12]}" for r in rows
        )
        QMessageBox.information(self, "Receipts", f"Total: {n}, chain intact.\n\n{text or '(empty)'}")

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self._apply_theme()

    def _apply_theme(self) -> None:
        theme = DARK if self.dark else LIGHT
        QApplication.instance().setStyleSheet(stylesheet(theme))
        self.theme_action.setToolTip("Light theme  (Ctrl+D)" if self.dark else "Dark theme  (Ctrl+D)")
        self.theme_action.setProperty("icon_name", "sun" if self.dark else "moon")
        self._apply_icons(theme["text"])

    def _apply_icons(self, color: str) -> None:
        """Redraw the toolbar glyphs in the theme's text colour."""
        for action in self.icon_actions:
            action.setIcon(glyph_icon(action.property("icon_name"), color))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    root = Path(argv[1]) if len(argv) > 1 else default_root()
    # English UI regardless of the Windows display language (sizes, dates).
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
    app = QApplication(argv[:1])
    app.setApplicationName("Vault V2")
    app.setApplicationVersion(__version__)
    win = MainWindow(root)
    win.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
