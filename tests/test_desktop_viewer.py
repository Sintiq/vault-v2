"""Owner-facing preview behavior on synthetic files, with no live vault."""
from pathlib import Path
import os
import threading
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QGraphicsView, QLabel, QPlainTextEdit, QPushButton

from vault_v2.paths import VaultPaths
from vault_v2.receipts import ReceiptLog
from vault_v2.errors import VaultError
from vault_v2.viewer import FileViewer


def wait_for(application, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Viewer did not complete its asynchronous operation")


def wait_for_pdf(application, viewer):
    wait_for(application, lambda: "rendering…" not in
             [label.text() for label in viewer.findChildren(QLabel)])


@pytest.fixture(scope="module")
def application():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def vault(tmp_path):
    paths = VaultPaths(tmp_path / "vault").ensure()
    return paths, ReceiptLog(paths.receipts)


def test_text_preview_shows_complete_plaintext_without_receipts(application, vault):
    paths, log = vault
    path = paths.documents / "report.md"
    content = "<img src='https://example.invalid/private'>\n" + "synthetic line\n" * 2500
    path.write_text(content, encoding="utf-8")
    viewer = FileViewer(paths, path)
    try:
        text = viewer.findChild(QPlainTextEdit)
        assert text is not None and text.isReadOnly()
        assert text.toPlainText() == content
        assert log.tail() == []
    finally:
        viewer.close()


def test_pdf_opens_with_rendering_notice_before_its_result(application, vault):
    from tools.synthetic_viewer_files import create_fixture_files

    paths, log = vault
    path = create_fixture_files(paths.documents)["pages"]
    source = path.read_bytes()
    viewer = FileViewer(paths, path)
    try:
        assert "rendering…" in [label.text() for label in viewer.findChildren(QLabel)]
        assert not any(button.isEnabled() for button in viewer.findChildren(QPushButton)
                       if button.text() in {"Previous page", "Next page"})
        wait_for_pdf(application, viewer)
        assert "Page 1 of 2" in [label.text() for label in viewer.findChildren(QLabel)]
        assert path.read_bytes() == source and log.tail() == []
    finally:
        viewer.close()


def test_pdf_owner_can_page_forward_and_back(application, vault):
    from tools.synthetic_viewer_files import create_fixture_files

    paths, log = vault
    path = create_fixture_files(paths.documents)["pages"]
    viewer = FileViewer(paths, path)
    try:
        wait_for_pdf(application, viewer)
        pdf = viewer.findChild(QGraphicsView)
        assert pdf is not None
        first_page = pdf.scene().items()[0].pixmap().toImage()
        previous = next(b for b in viewer.findChildren(QPushButton) if b.text() == "Previous page")
        following = next(b for b in viewer.findChildren(QPushButton) if b.text() == "Next page")
        assert not previous.isEnabled() and following.isEnabled()
        following.click()
        wait_for_pdf(application, viewer)
        assert "Page 2 of 2" in [label.text() for label in viewer.findChildren(QLabel)]
        assert pdf.scene().items()[0].pixmap().toImage() != first_page
        assert previous.isEnabled() and not following.isEnabled()
        previous.click()
        wait_for_pdf(application, viewer)
        assert "Page 1 of 2" in [label.text() for label in viewer.findChildren(QLabel)]
        assert log.tail() == []
    finally:
        viewer.close()


def test_failed_next_pdf_page_keeps_matching_previous_image_and_page_number(application, vault):
    from tools.synthetic_viewer_files import create_fixture_files

    paths, log = vault
    path = create_fixture_files(paths.documents)["pages"]
    viewer = FileViewer(paths, path)
    try:
        wait_for_pdf(application, viewer)
        picture = viewer.findChild(QGraphicsView)
        first = picture.scene().items()[0].pixmap().toImage()
        following = next(button for button in viewer.findChildren(QPushButton)
                         if button.text() == "Next page")
        path.write_bytes(b"synthetic file replaced before the next page")
        following.click()
        assert not following.isEnabled()
        wait_for_pdf(application, viewer)
        labels = [label.text() for label in viewer.findChildren(QLabel)]
        assert "Page 1 of 2" in labels and "Page 2 of 2" not in labels
        assert any("cannot be decoded" in label for label in labels)
        assert picture.scene().items()[0].pixmap().toImage() == first
        assert following.isEnabled()
        assert path.read_bytes() == b"synthetic file replaced before the next page"
        assert log.tail() == []
    finally:
        viewer.close()


@pytest.mark.parametrize("location", ["outside", "hidden", "service", "dotdot", "directory"])
def test_preview_rejects_files_not_visible_in_panes(application, vault, location):
    paths, log = vault
    if location == "outside":
        path = paths.root.parent / "outside.txt"
    elif location == "hidden":
        path = paths.documents / ".hidden" / "secret.txt"
    elif location == "service":
        path = paths.staging / "gone.trash.json"
    elif location == "dotdot":
        path = paths.documents / ".." / "staging" / "note.txt"
    else:
        path = paths.documents
    if location != "directory":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic", encoding="utf-8")
    with pytest.raises(VaultError):
        FileViewer(paths, path)
    assert log.tail() == []


def test_photo_fits_view_and_owner_can_zoom(application, vault):
    paths, log = vault
    path = paths.personal / "photo.png"
    image = QImage(1600, 900, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(path))
    viewer = FileViewer(paths, path)
    try:
        viewer.show()
        application.processEvents()
        picture = viewer.findChild(QGraphicsView)
        assert picture is not None
        bounds = picture.scene().itemsBoundingRect()
        assert (bounds.width(), bounds.height()) == (1600, 900)
        fit_scale = picture.transform().m11()
        assert 0 < fit_scale < 1
        next(b for b in viewer.findChildren(QPushButton) if b.text() == "Zoom in").click()
        assert picture.transform().m11() > fit_scale
        next(b for b in viewer.findChildren(QPushButton) if b.text() == "Fit to window").click()
        assert picture.transform().m11() == pytest.approx(fit_scale)
        assert log.tail() == []
    finally:
        viewer.close()


def test_unsupported_preview_is_information_only_with_shelf_and_digest(application, vault):
    from vault_v2.cards import CardStore

    paths, log = vault
    path = paths.staging / "sample.docx"
    path.write_bytes(b"abc")
    cards = CardStore(paths.root / ".cards", log)
    cards.set_shelf(path, "TAXES")
    before = log.tail()
    viewer = FileViewer(paths, path, cards=cards)
    try:
        labels = "\n".join(label.text() for label in viewer.findChildren(QLabel))
        assert "no preview for this file type" in labels
        assert "sample.docx" in labels and "3 bytes" in labels
        assert "TAXES" in labels and "ba7816bf8f01" in labels
        assert "Modified:" in labels
        assert not any("Windows" in button.text() for button in viewer.findChildren(QPushButton))
        assert log.tail() == before
    finally:
        viewer.close()


@pytest.mark.parametrize("suffix", [".txt", ".md", ".csv", ".json", ".log"])
def test_all_declared_text_types_are_plaintext(application, vault, suffix):
    paths, log = vault
    path = paths.personal / ("text" + suffix)
    path.write_bytes(b"<b>not rich text</b>\n\xff")
    viewer = FileViewer(paths, path)
    try:
        assert viewer.findChild(QPlainTextEdit).toPlainText() == "<b>not rich text</b>\n\ufffd"
        assert log.tail() == []
    finally:
        viewer.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("location", ["inside-pane", "vault-root", "ancestor-of-root"])
def test_preview_refuses_real_junctions_at_any_depth(application, vault, location):
    import _winapi

    paths, log = vault
    target = paths.root.parent / "junction-target"
    target.mkdir()
    (target / "note.txt").write_text("synthetic", encoding="utf-8")
    if location == "inside-pane":
        link = paths.documents / "linked"
        candidate = link / "note.txt"
        routed = paths
    elif location == "vault-root":
        link = paths.root.parent / "linked-vault"
        _target = target / "documents"
        _target.mkdir()
        (_target / "note.txt").write_text("synthetic", encoding="utf-8")
        candidate = link / "documents" / "note.txt"
        routed = VaultPaths(link)
    else:
        link = paths.root.parent / "linked-parent"
        _target = target / "nested" / "documents"
        _target.mkdir(parents=True)
        (_target / "note.txt").write_text("synthetic", encoding="utf-8")
        candidate = link / "nested" / "documents" / "note.txt"
        routed = VaultPaths(link / "nested")
    _winapi.CreateJunction(str(target), str(link))
    try:
        assert link.is_junction()
        with pytest.raises(VaultError, match="Linked"):
            FileViewer(routed, candidate)
        assert log.tail() == []
    finally:
        link.rmdir()  # remove the junction itself, never its target


def test_readonly_preview_does_not_wait_for_writer_or_add_receipt(application, vault):
    paths, log = vault
    path = paths.documents / "note.txt"
    path.write_text("Owner can read while another operation writes.", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()

    def writer():
        with log.write("synthetic concurrent writer"):
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=writer)
    worker.start()
    assert entered.wait(2)
    readonly = ReceiptLog(paths.receipts, read_only=True)
    try:
        from vault_v2.cards import CardStore
        cards = CardStore(paths.root / ".cards", readonly)
        viewer = FileViewer(paths, path, cards=cards)
        assert viewer.findChild(QPlainTextEdit).toPlainText().startswith("Owner can read")
        viewer.close()
    finally:
        release.set()
        worker.join(2)
    assert readonly.tail() == []


def test_preview_rejects_file_symlink_even_when_target_is_inside(application, vault):
    paths, log = vault
    target = paths.staging / "actual.txt"
    target.write_text("synthetic", encoding="utf-8")
    link = paths.documents / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"File symlink privilege is unavailable: {exc}")
    with pytest.raises(VaultError, match="Linked"):
        FileViewer(paths, link)
    assert log.tail() == []


def test_xfa_only_preview_warns_instead_of_claiming_form_values(application, vault):
    from tools.synthetic_viewer_files import form_pdf

    paths, log = vault
    path = form_pdf(paths.documents / "xfa-only.pdf", xfa_only=True)
    viewer = FileViewer(paths, path)
    try:
        wait_for_pdf(application, viewer)
        labels = "\n".join(label.text() for label in viewer.findChildren(QLabel))
        assert "this is an XFA form; its filled data may not show here" in labels
        assert not any("Windows" in button.text() for button in viewer.findChildren(QPushButton))
        assert log.tail() == []
    finally:
        viewer.close()


@pytest.mark.parametrize("suffix", [".pdf", ".png"])
def test_corrupt_known_type_reports_no_decodable_preview(application, vault, suffix):
    paths, log = vault
    path = paths.documents / ("corrupt" + suffix)
    path.write_bytes(b"synthetic invalid content")
    viewer = FileViewer(paths, path)
    try:
        if suffix == ".pdf":
            wait_for_pdf(application, viewer)
        assert "cannot be decoded" in "\n".join(label.text() for label in viewer.findChildren(QLabel))
        assert log.tail() == []
    finally:
        viewer.close()


@pytest.mark.parametrize("encrypted,algorithm", [(False, None), (True, None),
                                                (True, "AES-128"), (True, "AES-256")])
def test_pdf_form_render_shows_real_widget_values_and_preserves_source(vault, encrypted, algorithm):
    from io import BytesIO
    from PIL import Image, ImageChops
    from pypdf import PdfReader
    from tools.synthetic_viewer_files import form_pdf
    from vault_v2.pdf_preview import render_pdf_page

    paths, log = vault
    filled = form_pdf(paths.documents / "filled.pdf", encrypted=encrypted, encryption_algorithm=algorithm)
    blank = form_pdf(paths.documents / "blank.pdf", "", encrypted=encrypted, encryption_algorithm=algorithm)
    source = filled.read_bytes()
    reader = PdfReader(BytesIO(source))
    if encrypted:
        assert reader.is_encrypted and reader.decrypt("")
    assert reader.get_fields()["synthetic_field"]["/V"] == "SYNTHETIC-VALUE"
    widget = reader.pages[0]["/Annots"][0].get_object()
    assert widget["/V"] == "SYNTHETIC-VALUE"
    assert b"SYNTHETIC-VALUE" in widget["/AP"]["/N"].get_data()
    assert not reader.pages[0].get("/Contents"), "Value must live only in the widget, not the page"
    output = render_pdf_page(paths, filled, width=1200)
    empty = render_pdf_page(paths, blank, width=1200)
    assert output.page_count == 1 and output.page_index == 0
    image = Image.open(BytesIO(output.png)).convert("RGB")
    baseline = Image.open(BytesIO(empty.png)).convert("RGB")
    difference = ImageChops.difference(image, baseline).crop((140, 270, 1050, 365))
    assert sum(pixel != (0, 0, 0) for pixel in difference.get_flattened_data()) > 100
    assert filled.read_bytes() == source
    assert log.tail() == []


def test_pdf_form_values_reach_the_actual_preview_widget(application, vault):
    from tools.synthetic_viewer_files import form_pdf

    paths, log = vault
    filled = FileViewer(paths, form_pdf(paths.documents / "filled.pdf"))
    blank = FileViewer(paths, form_pdf(paths.documents / "blank.pdf", ""))
    try:
        wait_for_pdf(application, filled)
        wait_for_pdf(application, blank)
        left = filled.findChild(QGraphicsView).scene().items()[0].pixmap().toImage()
        right = blank.findChild(QGraphicsView).scene().items()[0].pixmap().toImage()
        assert sum(left.pixel(x, y) != right.pixel(x, y)
                   for x in range(140, 1050) for y in range(270, 365)) > 100
        assert log.tail() == []
    finally:
        filled.close()
        blank.close()


@pytest.mark.parametrize("page,width", [(-1, 1200), (1, 1200), (0, 0), (0, 1601), (0, True)])
def test_pdf_page_render_refuses_invalid_selection(vault, page, width):
    from tools.synthetic_viewer_files import form_pdf
    from vault_v2.pdf_preview import render_pdf_page

    paths, log = vault
    path = form_pdf(paths.documents / "filled.pdf")
    with pytest.raises(VaultError):
        render_pdf_page(paths, path, page=page, width=width)
    assert log.tail() == []


def test_extreme_pdf_geometry_is_refused_without_allocating_huge_bitmap(vault):
    from pypdf import PdfWriter
    from vault_v2.pdf_preview import render_pdf_page

    paths, log = vault
    path = paths.documents / "too-tall.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=1, height=1000000)
    with path.open("wb") as output:
        writer.write(output)
    with pytest.raises(VaultError, match="too tall"):
        render_pdf_page(paths, path)
    assert log.tail() == []


def test_renderer_can_be_imported_and_called_without_qt(vault):
    import subprocess
    import sys
    from tools.synthetic_viewer_files import form_pdf

    paths, _log = vault
    path = form_pdf(paths.documents / "headless.pdf")
    code = (
        "import sys; from pathlib import Path; "
        "from vault_v2.pdf_preview import render_pdf_page; "
        "from vault_v2.paths import VaultPaths; "
        "result=render_pdf_page(VaultPaths(Path(sys.argv[1])),Path(sys.argv[2]),width=400); "
        "assert result.png.startswith(bytes.fromhex('89504e470d0a1a0a')); "
        "assert not any(name.startswith('PySide6') for name in sys.modules)"
    )
    completed = subprocess.run([sys.executable, "-B", "-c", code, str(paths.root), str(path)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True,
                               text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


def test_renderer_handles_concurrent_read_requests_without_writes(vault):
    from concurrent.futures import ThreadPoolExecutor
    from tools.synthetic_viewer_files import form_pdf
    from vault_v2.pdf_preview import render_pdf_page

    paths, log = vault
    path = form_pdf(paths.documents / "parallel.pdf", encrypted=True)
    source = path.read_bytes()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: render_pdf_page(paths, path, width=400), range(8)))
    assert all(result.png == results[0].png for result in results)
    assert path.read_bytes() == source
    assert log.tail() == []


def test_renderer_refuses_a_pdf_outside_visible_panes(vault):
    from tools.synthetic_viewer_files import form_pdf
    from vault_v2.pdf_preview import render_pdf_page

    paths, log = vault
    path = form_pdf(paths.root.parent / "outside.pdf")
    with pytest.raises(VaultError, match="visible vault pane"):
        render_pdf_page(paths, path)
    assert log.tail() == []


def test_malformed_catalog_reports_a_useful_preview_message(application, vault):
    import re
    from pypdf import PdfWriter

    paths, log = vault
    path = paths.documents / "null-catalog.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=240)
    with path.open("wb") as output:
        writer.write(output)
    path.write_bytes(re.sub(rb"/Root \d+ \d+ R", b"/Root null", path.read_bytes()))
    source = path.read_bytes()
    viewer = FileViewer(paths, path)
    try:
        wait_for_pdf(application, viewer)
        labels = "\n".join(label.text() for label in viewer.findChildren(QLabel))
        assert "cannot be decoded" in labels or "could not be checked" in labels
        assert path.read_bytes() == source
        assert log.tail() == []
    finally:
        viewer.close()


def test_aes_preview_survives_missing_structure_crypto_with_explicit_warning(vault, monkeypatch):
    import subprocess
    from tools.synthetic_viewer_files import form_pdf
    from vault_v2.pdf_preview import render_pdf_page

    paths, log = vault
    path = form_pdf(paths.documents / "aes.pdf", encrypted=True, encryption_algorithm="AES-256")
    source = path.read_bytes()
    missing_crypto = """
import importlib.abc
import sys
class MissingCrypto(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cryptography', 'Crypto', 'Cryptodome'}:
            raise ImportError('synthetic clean install without crypto provider')
sys.meta_path.insert(0, MissingCrypto())
"""
    actual_popen = subprocess.Popen

    def child_without_crypto(argv, *args, **kwargs):
        # Absence must affect the real parser child, not only its parent.
        assert Path(argv[-1]).name == "pdf_worker.py"
        bootstrap = missing_crypto + "\nimport runpy\nrunpy.run_path(" + repr(argv[-1]) + ", run_name='__main__')"
        return actual_popen([*argv[:-1], "-c", bootstrap], *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", child_without_crypto)
    result = render_pdf_page(paths, path)
    assert result.png.startswith(bytes.fromhex("89504e470d0a1a0a"))
    assert "could not be checked" in result.warning
    assert path.read_bytes() == source and log.tail() == []


@pytest.mark.parametrize("need_appearances", [False, True])
@pytest.mark.parametrize("encrypted,algorithm", [(False, None), (True, None), (True, "AES-256")])
def test_native_form_render_shows_values_without_saved_appearance(vault, need_appearances, encrypted, algorithm):
    from io import BytesIO
    from PIL import Image, ImageChops
    from pypdf import PdfReader
    from tools.synthetic_viewer_files import form_pdf
    from vault_v2.pdf_preview import render_pdf_page

    paths, log = vault
    options = {"with_appearance": False, "need_appearances": need_appearances,
               "encrypted": encrypted, "encryption_algorithm": algorithm}
    filled = form_pdf(paths.documents / "missing-appearance.pdf", **options)
    blank = form_pdf(paths.documents / "empty-missing-appearance.pdf", "", **options)
    source = filled.read_bytes()
    reader = PdfReader(BytesIO(source))
    if encrypted:
        assert reader.decrypt("")
    widget = reader.pages[0]["/Annots"][0].get_object()
    assert "/AP" not in widget
    assert widget["/V"] == reader.get_fields()["synthetic_field"]["/V"] == "SYNTHETIC-VALUE"
    assert not reader.pages[0].get("/Contents")
    shown = render_pdf_page(paths, filled, width=1200)
    empty = render_pdf_page(paths, blank, width=1200)
    actual = Image.open(BytesIO(shown.png)).convert("RGB")
    baseline = Image.open(BytesIO(empty.png)).convert("RGB")
    difference = ImageChops.difference(actual, baseline).crop((140, 270, 1050, 365))
    assert sum(pixel != (0, 0, 0) for pixel in difference.get_flattened_data()) > 100
    assert filled.read_bytes() == source
    assert log.tail() == []
