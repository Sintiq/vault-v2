"""Synthetic preview fixtures only; never reads a real vault/document."""
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,
                          DictionaryObject, FloatObject, NameObject,
                          NumberObject, TextStringObject)


def form_pdf(path: Path, value="SYNTHETIC-VALUE", *, encrypted=False, xfa_only=False,
             with_appearance=True, need_appearances=False, encryption_algorithm=None):
    """A real field tree and widget appearance; value is NOT in page content."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=240)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    form = DictionaryObject({NameObject("/Fields"): ArrayObject(),
                             NameObject("/NeedAppearances"): BooleanObject(need_appearances),
                             NameObject("/DR"): resources})
    if xfa_only:
        xfa = DecodedStreamObject()
        xfa.set_data(b'<xdp:xdp xmlns:xdp="http://ns.adobe.com/xdp/"><template xmlns="http://www.xfa.org/schema/xfa-template/3.3/"><subform name="synthetic"/></template></xdp:xdp>')
        form[NameObject("/XFA")] = writer._add_object(xfa)
    else:
        appearance = DecodedStreamObject()
        appearance.update({NameObject("/Type"): NameObject("/XObject"),
                           NameObject("/Subtype"): NameObject("/Form"),
                           NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(320), NumberObject(45)]),
                           NameObject("/Resources"): resources})
        appearance.set_data(("q 1 1 1 rg 0 0 320 45 re f 0.5 G 1 w 0.5 0.5 319 44 re S "
                             + (f"BT /F1 18 Tf 0 g 10 15 Td ({value}) Tj ET " if value else "")
                             + "Q").encode("ascii"))
        field = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject("synthetic_field"),
            NameObject("/V"): TextStringObject(value), NameObject("/F"): NumberObject(4),
            NameObject("/Rect"): ArrayObject([FloatObject(n) for n in (40, 110, 360, 155)]),
            NameObject("/DA"): TextStringObject("/F1 18 Tf 0 g"), NameObject("/P"): page.indirect_reference,
        })
        if with_appearance:
            field[NameObject("/AP")] = DictionaryObject({NameObject("/N"): writer._add_object(appearance)})
        ref = writer._add_object(field)
        page[NameObject("/Annots")] = ArrayObject([ref])
        form[NameObject("/Fields")] = ArrayObject([ref])
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(form)
    if encrypted:
        writer.encrypt(user_password="", owner_password="synthetic-owner-only", permissions_flag=0,
                       algorithm=encryption_algorithm)
    with path.open("wb") as output:
        writer.write(output)
    return path


def create_fixture_files(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "filled": form_pdf(directory / "synthetic-filled-form.pdf"),
        "blank": form_pdf(directory / "synthetic-blank-form.pdf", ""),
        "encrypted": form_pdf(directory / "synthetic-owner-rights.pdf", encrypted=True),
        "without_appearance": form_pdf(directory / "synthetic-no-appearance.pdf", with_appearance=False),
        "xfa": form_pdf(directory / "synthetic-xfa-only.pdf", xfa_only=True),
    }
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for number in (1, 2):
        page = writer.add_blank_page(width=400, height=240)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        text = DecodedStreamObject()
        text.set_data(f"BT /F1 24 Tf 40 170 Td (Synthetic page {number}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(text)
    files["pages"] = directory / "synthetic-two-pages.pdf"
    with files["pages"].open("wb") as output:
        writer.write(output)
    files["text"] = directory / "synthetic-full-text.md"
    files["text"].write_text("# Synthetic owner report\n\n" + "A complete local-only line.\n" * 500
                             + "\nEND OF SYNTHETIC REPORT\n", encoding="utf-8")
    from PySide6.QtGui import QColor, QImage, QPainter
    image = QImage(1600, 900, QImage.Format.Format_RGB32)
    image.fill(QColor("#17445b"))
    painter = QPainter(image)
    painter.fillRect(200, 180, 1200, 500, QColor("#6db79d"))
    painter.end()
    files["photo"] = directory / "synthetic-photo.png"
    if not image.save(str(files["photo"])):
        raise RuntimeError("Could not create synthetic image")
    return files
