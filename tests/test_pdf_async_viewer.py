"""Synthetic desktop event-loop/lifetime checks at the FileViewer interface."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from vault_v2.paths import VaultPaths
from vault_v2.receipts import ReceiptLog


@pytest.mark.parametrize("action", ["close", "reject", "escape"])
def test_closing_a_rendering_preview_never_destroys_a_running_thread(tmp_path, action):
    from tools.synthetic_viewer_files import create_fixture_files

    paths = VaultPaths(tmp_path / "vault").ensure()
    log = ReceiptLog(paths.receipts)
    path = create_fixture_files(paths.documents)["pages"]
    source = path.read_bytes()
    code = """
import subprocess, sys, time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from vault_v2.paths import VaultPaths
from vault_v2.viewer import FileViewer
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
real_popen = subprocess.Popen
children = []
def hanging_child(argv, *args, **kwargs):
    assert Path(argv[-1]).name == 'pdf_worker.py'
    child = real_popen([*argv[:-1], '-c', 'import time; time.sleep(60)'], *args, **kwargs)
    children.append(child)
    return child
subprocess.Popen = hanging_child
viewer = FileViewer(VaultPaths(Path(sys.argv[1])), Path(sys.argv[2]))
viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
viewer.show()
assert 'rendering…' in [label.text() for label in viewer.findChildren(QLabel)]
action = sys.argv[3]
destroyed = []
viewer.destroyed.connect(lambda: destroyed.append(True))
started = []
ticks = []
closed = []
def dismiss():
    if action == 'escape':
        QTest.keyClick(viewer, Qt.Key.Key_Escape)
    else:
        getattr(viewer, action)()
    closed.append(time.monotonic())
def tick():
    ticks.append(time.monotonic())
    if children and not started:
        started.append(time.monotonic())
    if started and not closed and time.monotonic() - started[0] > 0.2:
        dismiss()
    if destroyed and children[0].poll() is not None:
        app.quit()
timer = QTimer()
timer.setInterval(10)
timer.timeout.connect(tick)
timer.start()
QTimer.singleShot(3000, app.quit)
app.exec()
assert len(ticks) >= 10, 'GUI event loop stopped while rendering'
assert destroyed and children and children[0].poll() is not None, 'Close did not reap its child'
assert time.monotonic() - closed[0] < 2, 'Close waited for the render deadline'
"""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code,
                                str(paths.root), str(path), action],
                               cwd=Path(__file__).resolve().parents[1], env=env,
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert path.read_bytes() == source and log.tail() == []


def test_gui_keeps_ticking_while_real_pdf_child_renders(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QGraphicsView, QLabel
    from tools.synthetic_viewer_files import create_fixture_files
    from vault_v2.viewer import FileViewer

    application = QApplication.instance() or QApplication([])
    paths = VaultPaths(tmp_path / "vault").ensure()
    log = ReceiptLog(paths.receipts)
    path = create_fixture_files(paths.documents)["pages"]
    source = path.read_bytes()
    real_popen = subprocess.Popen

    def slow_real_child(argv, *args, **kwargs):
        assert Path(argv[-1]).name == "pdf_worker.py"
        bootstrap = "import runpy,time;time.sleep(0.35);runpy.run_path(" + repr(argv[-1]) + ",run_name='__main__')"
        return real_popen([*argv[:-1], "-c", bootstrap], *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", slow_real_child)
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    viewer = FileViewer(paths, path)
    try:
        assert "rendering…" in [label.text() for label in viewer.findChildren(QLabel)]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            application.processEvents()
            if "Page 1 of 2" in [label.text() for label in viewer.findChildren(QLabel)]:
                break
            time.sleep(0.005)
        assert "Page 1 of 2" in [label.text() for label in viewer.findChildren(QLabel)]
        assert len(ticks) >= 10
        assert not viewer.findChild(QGraphicsView).scene().items()[0].pixmap().isNull()
        assert path.read_bytes() == source and log.tail() == []
    finally:
        timer.stop()
        viewer.close()
