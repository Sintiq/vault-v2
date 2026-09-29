"""One synthetic, offscreen PDF viewer check; never reads owner documents.

Writes a PNG and a small JSON record to a new output directory chosen by caller.
The fixture has an AcroForm value, no saved appearance and AES owner restrictions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import QThread, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QGraphicsView, QLabel

from tools.synthetic_viewer_files import form_pdf
from vault_v2.paths import VaultPaths
from vault_v2.receipts import ReceiptLog
from vault_v2.viewer import FileViewer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    args.output_directory.mkdir(parents=True, exist_ok=False)
    repository = Path(__file__).resolve().parents[1]
    measured_sources = ("vault_v2/pdf_preview.py", "vault_v2/pdf_process.py",
                        "vault_v2/pdf_worker.py", "vault_v2/text_extract.py",
                        "vault_v2/viewer.py", "tools/qualify_pdf_process_preview.py")
    fingerprints = lambda: {name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
                            for name in measured_sources}
    source_fingerprints = fingerprints()
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # The Windows offscreen platform may not discover the installed UI font.
    # Load the same Segoe UI family named by the application's stylesheet.
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
        app.setFont(QFont("Segoe UI", 10))
    with tempfile.TemporaryDirectory(prefix="vault-pdf-process-qa-") as directory:
        paths = VaultPaths(Path(directory) / "synthetic-vault").ensure()
        log = ReceiptLog(paths.receipts)
        source = form_pdf(paths.documents / "SYNTHETIC-no-appearance-AES.pdf",
                          with_appearance=False, encrypted=True,
                          encryption_algorithm="AES-256")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        started = time.monotonic()
        viewer = FileViewer(paths, source)
        construction_seconds = time.monotonic() - started
        labels = lambda: [label.text() for label in viewer.findChildren(QLabel)]
        initial_rendering = "rendering…" in labels()
        beats = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: beats.append(time.monotonic())
                              if "rendering…" in labels() else None)
        timer.start()
        viewer.show()
        try:
            while "rendering…" in labels() and time.monotonic() - started < 20:
                app.processEvents()
                time.sleep(0.005)
            elapsed = time.monotonic() - started
            assert initial_rendering, "constructor did not expose its pending render"
            assert "Page 1 of 1" in labels(), labels()
            assert beats, "no GUI heartbeat observed during the actual PDF child"
            picture = viewer.findChild(QGraphicsView)
            image = picture.scene().items()[0].pixmap().toImage()
            assert not image.isNull()
            app.processEvents()
            assert viewer.grab().save(str(args.output_directory / "synthetic-preview.png"))
            assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
            assert log.tail() == []
            assert fingerprints() == source_fingerprints, "source changed during qualification"
            record = {
                "fixture": "synthetic AcroForm /V without /AP, AES-256 owner restrictions",
                "initial_rendering": initial_rendering,
                "construction_seconds": construction_seconds,
                "completion_seconds": elapsed,
                "gui_heartbeats_during_render": len(beats),
                "image_size": [image.width(), image.height()],
                "source_unchanged": True, "receipts": 0,
                "code_sha256": source_fingerprints,
            }
            (args.output_directory / "result.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(record, ensure_ascii=False), flush=True)
        finally:
            timer.stop()
            viewer.close()
            cleanup_deadline = time.monotonic() + 8
            while (any(worker.isRunning() for worker in viewer.findChildren(QThread))
                   and time.monotonic() < cleanup_deadline):
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
