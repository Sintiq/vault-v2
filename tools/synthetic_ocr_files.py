"""Known, generated OCR inputs; never reads owner documents."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

ENGLISH = "Payment is due by October 15, 2026"
RUSSIAN = "Оплатить до 15 октября 2026 года"


def text_pdf(path: Path, text: str = ENGLISH):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 18 Tf 45 700 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)
    return path


def image_bytes(text=ENGLISH, *, format="PNG"):
    picture = Image.new("RGB", (1600, 500), "white")
    try:
        draw = ImageDraw.Draw(picture)
        font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 42)
        draw.text((45, 100), text, font=font, fill="black")
        output = BytesIO()
        picture.save(output, format=format)
        return output.getvalue()
    finally:
        picture.close()


def scan_file(path: Path, text=ENGLISH):
    path.write_bytes(image_bytes(text, format="PDF" if path.suffix.lower() == ".pdf" else "PNG"))
    return path
