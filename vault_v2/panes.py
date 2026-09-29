"""Explorer pane — one of the three Commander-style windows.

A pane shows one folder tree rooted at its vault pane (staging / documents /
personal). It can drag files out (as URLs) and accept drops from the other
panes or from Windows Explorer. Every drop becomes a VaultOps call, never a
direct file write, so the receipt log stays complete.

Drop rules:
- from another vault pane: ask Move / Copy / Cancel;
- from outside the vault (Explorer): copy in, original untouched;
- Shift held while dropping: move without asking.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QSortFilterProxyModel, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QKeyEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileIconProvider,
    QFileSystemModel,
    QFrame,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import cards as card_types
from .agent_scope import AgentTextScope
from .cards import CardError
from .dialogs import confirm
from .file_access import visible_directory, visible_file
from .ops import VaultError, VaultOps
from .viewer import FileViewer

HEADERS = {0: "Name", 1: "Size", 2: "Shelf", 3: "Modified"}
HASH_DISPLAY_LIMIT = 50 * 1024 * 1024  # do not hash huge files just to paint a cell


class _PaneProxy(QSortFilterProxyModel):
    """Hide dot-folders (.trash, .receipts) and trash manifests; short headers."""

    def __init__(self, parent=None, cards=None, paths=None, scope=None):
        super().__init__(parent)
        self.cards = cards
        self.paths = paths
        self.scope = scope
        self.pane_root: Path | None = None
        self.needle: tuple[str, ...] = ()

    def set_filter(self, text: str) -> None:
        self.needle = tuple(t for t in text.lower().split() if t)
        self.invalidateFilter()

    def _card(self, src_idx: QModelIndex):
        fs = self.sourceModel()
        if self.cards is None or fs.isDir(src_idx) or fs.size(src_idx) > HASH_DISPLAY_LIMIT:
            return None
        try:
            return self.cards.for_path(Path(fs.filePath(src_idx)))
        except (OSError, VaultError):
            return None

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        fs = self.sourceModel()
        idx = fs.index(row, 0, parent)
        name = fs.fileName(idx)
        if name == "..":
            if self.paths is None:
                return False
            # Commander-style "up" row: shown inside sub-folders, never at the pane root
            current = Path(fs.filePath(parent))
            try:
                visible_directory(self.paths, current)
                return self.pane_root is not None and current != self.pane_root and current.is_relative_to(self.pane_root)
            except (OSError, VaultError):
                return False
        if self.paths is not None and self.pane_root is not None:
            path = Path(fs.filePath(idx))
            # QFileSystemModel needs the root's structural ancestors to map its
            # root index. They are not displayed document rows or read targets.
            if path != self.pane_root and self.pane_root.is_relative_to(path):
                return True
            if not path.is_relative_to(self.pane_root):
                return False
            try:
                if fs.isDir(idx):
                    visible_directory(self.paths, path)
                else:
                    visible_file(self.paths, path)
            except VaultError as exc:
                # Keep ordinary inaccessible rows selected so an owner action
                # reports failure instead of silently dropping the selection.
                # Card lookup and navigation still refuse to read this path.
                return isinstance(exc.__cause__, OSError)
            except OSError:
                return False
        if name.startswith(".") or name.lower().endswith(".trash.json"):
            return False
        if not self.needle or fs.isDir(idx):
            return True
        hay = name.lower()
        card = self._card(idx)
        if card is not None:
            hay += " " + " ".join((card.shelf, *card.topics, card.issuer, str(card.year or ""), *card.recipients)).lower()
        return all(t in hay for t in self.needle)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        fs = self.sourceModel()
        asc = self.sortOrder() == Qt.SortOrder.AscendingOrder
        if fs.fileName(left.siblingAtColumn(0)) == "..":
            return asc  # ".." stays on top whatever the sort order
        if fs.fileName(right.siblingAtColumn(0)) == "..":
            return not asc
        if left.column() == 2:
            a, b = self._card(left.siblingAtColumn(0)), self._card(right.siblingAtColumn(0))
            return (a.shelf if a else "~") < (b.shelf if b else "~")
        return super().lessThan(left, right)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS.get(section, super().headerData(section, orientation, role))
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if self.scope is not None and index.column() == 0 and role in (
                Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole,
                Qt.ItemDataRole.AccessibleTextRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            source = self.mapToSource(index)
            fs = self.sourceModel()
            if fs.isDir(source) and fs.fileName(source) != "..":
                path = Path(fs.filePath(source))
                try:
                    covering = self.scope.covering_folder(path)
                except (VaultError, OSError):
                    covering = None
                if covering is not None:
                    name = fs.fileName(source)
                    if role == Qt.ItemDataRole.DisplayRole:
                        return name + " [A]"
                    explanation = ("Agent text is allowed here" if covering == path else
                                   f"Agent text is allowed by {covering.relative_to(self.paths.root)}")
                    if role == Qt.ItemDataRole.AccessibleTextRole:
                        return f"{name}. {explanation}"
                    return explanation
        # Size and date come from Qt's own translations/system locale; render
        # them ourselves so the UI stays English on any Windows language.
        if role == Qt.ItemDataRole.DisplayRole and index.column() == 2:
            card = self._card(self.mapToSource(index).siblingAtColumn(0))
            if card is None:
                return ""
            return card.shelf if card.confirmed else card.shelf + " ?"
        if role == Qt.ItemDataRole.ToolTipRole and index.column() in (0, 2):
            card = self._card(self.mapToSource(index).siblingAtColumn(0))
            if card is not None:
                return f"{card.shelf} · {', '.join(card.topics) or 'UNTAGGED'} · {card.issuer} · {card.year or 'year unknown'} · {', '.join(card.recipients)}"
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 3:
            return self.sourceModel().lastModified(self.mapToSource(index)).toString("yyyy-MM-dd HH:mm:ss")
        if role == Qt.ItemDataRole.DisplayRole and index.column() in (1, 3):
            src = self.mapToSource(index)
            fs = self.sourceModel()
            if index.column() == 1:
                return "" if fs.isDir(src) else _human_size(fs.size(src))
            return fs.lastModified(src).toString("yy-MM-dd")
        return super().data(index, role)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class PaneView(QTreeView):
    dropped = Signal(list, Path, object)  # sources, destination dir, forced action or None

    def __init__(self, pane: "Pane"):
        super().__init__(pane)
        self.pane = pane
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setSortingEnabled(True)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(20, 20))
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    # Drops are handled here, not by the model, so ops go through VaultOps.
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:  # noqa: N802
        if self.pane.write_enabled and e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e: QDragMoveEvent) -> None:  # noqa: N802
        if self.pane.write_enabled and e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e: QDropEvent) -> None:  # noqa: N802
        if not self.pane.write_enabled or not e.mimeData().hasUrls():
            e.ignore()
            return
        dest = self.pane.current_dir()
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid():
            p = self.pane.path_for(idx)
            if p.is_dir():
                dest = p
        sources = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        forced = "move" if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else None
        # Always report Copy to the source view: we do our own move via VaultOps,
        # so Qt must never try to remove rows from the source model.
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()
        self.dropped.emit(sources, dest, forced)


class Pane(QFrame):
    """Title + folder view for one vault pane."""

    status = Signal(str)
    focused = Signal(object)
    manage_shelves = Signal()
    scope_granted = Signal(Path)

    def __init__(self, name: str, title: str, root: Path, ops: VaultOps, cards=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("pane")
        self.name = name
        self.root = Path(root).absolute()
        self.ops = ops
        self.scope = AgentTextScope(ops.paths, ops.log)
        self.write_enabled = not ops.log.read_only
        self._dir = self.root

        self.title = QLabel(title)
        self.title.setObjectName("paneTitle")
        self.sub = QLabel("")
        self.sub.setObjectName("paneSub")

        self.model = QFileSystemModel(self)
        self.model.setReadOnly(True)
        self.model.setIconProvider(QFileIconProvider())
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDot | QDir.Filter.AllDirs)
        self.proxy = _PaneProxy(self, cards, ops.paths, self.scope)
        self.proxy.pane_root = self.root
        self.proxy.setSourceModel(self.model)
        self.empty_model = QStandardItemModel(0, len(HEADERS), self)
        self.empty_model.setHorizontalHeaderLabels(list(HEADERS.values()))

        self.view = PaneView(self)
        self.view.setModel(self.proxy)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.dropped.connect(self._on_drop)
        self.view.doubleClicked.connect(self._on_double_click)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.installEventFilter(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addWidget(self.view, 1)

        self.set_dir(root)
        hdr = self.view.header()
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(40)
        for col in range(4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        self.model.directoryLoaded.connect(lambda _p: self._fit_columns())
        self._fit_columns()

    def _fit_columns(self) -> None:
        """Size, Shelf and Modified take what they need; Name gets the rest."""
        used = 0
        for col, width in ((1, 62), (2, 106), (3, 88)):
            self.view.setColumnWidth(col, width)
            used += width
        self.view.setColumnWidth(0, max(120, self.view.viewport().width() - used - 12))  # 12: tree padding + frame

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._fit_columns)  # after the view got its final size

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        QTimer.singleShot(50, self._fit_columns)

    # -- navigation -----------------------------------------------------------

    def current_dir(self) -> Path:
        return self._dir

    def set_write_enabled(self, enabled: bool) -> None:
        self.write_enabled = bool(enabled) and not self.ops.log.read_only
        self.view.setDragEnabled(self.write_enabled)
        self.view.setAcceptDrops(self.write_enabled)
        self.view.setDropIndicatorShown(self.write_enabled)

    def _can_write(self) -> bool:
        guard = self.ops.log.guard
        return self.write_enabled and not self.ops.log.read_only and not guard.busy and not guard.blocked

    def set_dir(self, d: Path) -> None:
        d = Path(d).absolute()
        try:
            d = visible_directory(self.ops.paths, d)
        except VaultError as exc:
            if (self.ops.log.read_only and d == self.root
                    and isinstance(exc.__cause__, FileNotFoundError)):
                # A second window must not create a missing pane or fall back
                # to QFileSystemModel's computer-wide root index.
                self._dir = d
                self.view.setModel(self.empty_model)
                self.sub.setText("Folder unavailable — read-only")
                return
            raise
        try:
            rel = d.relative_to(self.root)
        except ValueError as exc:
            raise VaultError("Directory must be inside this pane.") from exc
        self._dir = d
        if self.view.model() is not self.proxy:
            self.view.setModel(self.proxy)
        src_idx = self.model.setRootPath(str(d))
        self.view.setRootIndex(self.proxy.mapFromSource(src_idx))
        self.sub.setText("/" if not rel.parts else "/" + "/".join(rel.parts))

    def go_up(self) -> None:
        if self._dir != self.root:
            self.set_dir(self._dir.parent)

    def path_for(self, idx: QModelIndex) -> Path:
        return Path(self.model.filePath(self.proxy.mapToSource(idx)))

    def selected_paths(self) -> list[Path]:
        rows = {i.row(): i for i in self.view.selectedIndexes()}
        return [p for p in (self.path_for(i) for i in rows.values()) if p.name != ".."]

    def refresh(self) -> None:
        self.set_dir(self._dir)
        self.proxy.invalidate()
        self._fit_columns()

    def set_filter(self, text: str) -> None:
        self.proxy.set_filter(text)

    # -- events ---------------------------------------------------------------

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self.view and ev.type() == ev.Type.FocusIn:
            self.setProperty("active", True)
            self.style().unpolish(self)
            self.style().polish(self)
            self.focused.emit(self)
        elif obj is self.view and ev.type() == ev.Type.FocusOut:
            self.setProperty("active", False)
            self.style().unpolish(self)
            self.style().polish(self)
        elif obj is self.view and ev.type() == ev.Type.KeyPress:
            if self._key(ev):
                return True
        return super().eventFilter(obj, ev)

    def _key(self, ev: QKeyEvent) -> bool:
        k = ev.key()
        if k == Qt.Key.Key_Backspace:
            self.go_up()
            return True
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            idx = self.view.currentIndex()
            if idx.isValid():
                self._on_double_click(idx)
            return True
        return False

    def _on_double_click(self, idx: QModelIndex) -> None:
        if idx.isValid():
            self._open_path(self.path_for(idx))

    def _open_path(self, p: Path) -> None:
        try:
            if p.name == "..":
                self.go_up()
            elif p.is_dir():
                self.set_dir(p)
            else:
                viewer = FileViewer(self.ops.paths, p, cards=self.proxy.cards, parent=self)
                viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                viewer.show()
        except (VaultError, OSError) as exc:
            QMessageBox.warning(self, "Preview", str(exc))

    # -- drop -----------------------------------------------------------------

    def _ask_move_or_copy(self, n: int, src_title: str) -> str | None:
        box = QMessageBox(self)
        box.setWindowTitle("Transfer")
        box.setText(f"{n} item(s) from {src_title} → {self.title.text()}.\n\nCut from the source folder or leave a copy?")
        move = box.addButton("Move", QMessageBox.ButtonRole.AcceptRole)
        copy = box.addButton("Copy", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(copy)
        box.exec()
        if box.clickedButton() is move:
            return "move"
        if box.clickedButton() is copy:
            return "copy"
        return None

    def _on_drop(self, sources: list[Path], dest: Path, forced: str | None) -> None:
        if not self._can_write():
            return
        sources = [s for s in sources if s.exists() and s.resolve() != dest.resolve() and s.resolve().parent != dest.resolve()]
        if not sources:
            return
        inside = [s for s in sources if self.ops.paths.pane_of(s) is not None]
        outside = [s for s in sources if self.ops.paths.pane_of(s) is None]

        action = forced
        if inside and action is None:
            src_pane = self.ops.paths.pane_of(inside[0]) or ""
            src_title = self.window().pane_title(src_pane) if hasattr(self.window(), "pane_title") else src_pane
            action = self._ask_move_or_copy(len(inside), src_title)
            if action is None:
                return

        done, failed = 0, []
        for src in inside:
            try:
                (self.ops.move if action == "move" else self.ops.copy)(src, dest)
                done += 1
            except (VaultError, OSError) as exc:
                failed.append(f"{src.name}: {exc}")
        for src in outside:
            try:
                self.ops.copy(src, dest) if src.is_dir() else self.ops.import_file(src, self.name) if dest == self.root else self.ops.copy(src, dest)
                done += 1
            except (VaultError, OSError) as exc:
                failed.append(f"{src.name}: {exc}")

        verb = "moved" if action == "move" else "copied"
        msg = f"{verb}: {done}"
        if failed:
            msg += f"; errors: {len(failed)}"
            QMessageBox.warning(self, "Vault", "\n".join(failed))
        self.status.emit(msg)

    # -- context menu ---------------------------------------------------------

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        a_open = menu.addAction("Open")
        selected = self.selected_paths()
        a_open.setEnabled(len(selected) == 1)
        if len(selected) == 1:
            try:
                folder = visible_directory(self.ops.paths, selected[0])
                covering = self.scope.covering_folder(folder)
            except (VaultError, OSError):
                folder = None
            if folder is not None:
                if covering == self.ops.paths.staging.absolute():
                    menu.addAction("Staging text access is always enabled").setEnabled(False)
                elif covering is not None:
                    label = ("Stop reading text here" if covering == folder else
                             f"Stop reading text at {covering.relative_to(self.ops.paths.root)}")
                    if covering != folder:
                        menu.addAction(f"Agent text allowed by {covering.relative_to(self.ops.paths.root)}").setEnabled(False)
                    action = menu.addAction(label)
                    action.setEnabled(self._can_write())
                    action.triggered.connect(lambda _checked=False, path=covering: self._set_text_scope(path, False))
                else:
                    action = menu.addAction("Allow agent to read text here")
                    action.setEnabled(self._can_write())
                    action.triggered.connect(lambda _checked=False, path=folder: self._set_text_scope(path, True))
        shelf_menu = menu.addMenu("Shelf")
        can_assign = (self._can_write() and self.proxy.cards is not None
                      and bool(selected) and all(path.is_file() for path in selected))
        for shelf in card_types.SHELVES:
            action = shelf_menu.addAction(shelf)
            action.setCheckable(True)
            action.setEnabled(can_assign)
            action.triggered.connect(lambda _checked=False, name=shelf: self.set_shelf_selected(name))

        def mark_current_shelf():
            # Hash only when the owner opens Shelf, never just to offer Open.
            current = set()
            for path in selected:
                try:
                    self.proxy.cards._shelf_file(path)
                    card = self.proxy.cards.for_path(path)
                    current.add(card.shelf if card else None)
                except (AttributeError, CardError, VaultError, OSError):
                    current.add(None)
            for action in shelf_menu.actions():
                if action.isCheckable():
                    action.setChecked(current == {action.text()})

        shelf_menu.aboutToShow.connect(mark_current_shelf)
        shelf_menu.addSeparator()
        manage = shelf_menu.addAction("Manage shelves…")
        manage.setEnabled(self._can_write())
        manage.triggered.connect(self.manage_shelves.emit)
        menu.addSeparator()
        a_new = menu.addAction("New folder\tF7")
        a_trash = menu.addAction("Move to Trash\tF8")
        menu.addSeparator()
        a_up = menu.addAction("Up\tBackspace")
        a_new.setEnabled(self._can_write())
        a_trash.setEnabled(self._can_write())
        if len(selected) == 1:
            a_open.triggered.connect(lambda: self._open_path(selected[0]))
        a_new.triggered.connect(self.new_folder)
        a_trash.triggered.connect(self.trash_selected)
        a_up.triggered.connect(self.go_up)
        try:
            menu.exec(self.view.viewport().mapToGlobal(pos))
        finally:
            menu.deleteLater()

    # -- actions used by the main window ------------------------------------

    def _set_text_scope(self, folder: Path, enabled: bool) -> None:
        if not self._can_write():
            return
        try:
            self.scope.set_folder(folder, enabled)
        except (VaultError, OSError) as exc:
            self.status.emit(str(exc))
            return
        self.refresh()
        self.status.emit(f"Agent text {'allowed' if enabled else 'stopped'}: {folder.relative_to(self.ops.paths.root)}")
        if enabled:
            self.scope_granted.emit(folder)

    def set_shelf_selected(self, shelf: str) -> None:
        if not self._can_write() or self.proxy.cards is None:
            return
        paths = self.selected_paths()
        if not paths:
            return
        done, failed = 0, []
        for source in paths:
            try:
                self.proxy.cards.set_shelf(source, shelf)
                done += 1
            except (CardError, VaultError, OSError) as exc:
                failed.append(f"{source.name}: {exc}")
        self.refresh()
        summary = f"{done} set, {len(failed)} failed"
        self.status.emit(summary)
        if failed:
            QMessageBox.warning(self, "Set shelf", summary + "\n\n" + "\n".join(failed))

    def new_folder(self) -> None:
        if not self._can_write():
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            try:
                self.ops.mkdir(self._dir, name.strip())
                self.status.emit(f"folder created: {name.strip()}")
            except (VaultError, OSError) as exc:
                QMessageBox.warning(self, "Vault", str(exc))

    def trash_selected(self) -> None:
        if not self._can_write():
            return
        paths = self.selected_paths()
        if not paths:
            return
        names = "\n".join(p.name for p in paths[:10]) + ("\n…" if len(paths) > 10 else "")
        if not confirm(self, "Move to Trash", f"Move to Trash ({len(paths)}):\n{names}"):
            return
        n = 0
        for p in paths:
            try:
                self.ops.trash(p)
                n += 1
            except (VaultError, OSError) as exc:
                QMessageBox.warning(self, "Vault", f"{p.name}: {exc}")
        self.status.emit(f"moved to Trash: {n}")
