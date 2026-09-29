"""Read-only PDF interfaces; native PDF code runs only in a bounded child."""
from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import struct

from PIL import Image

from .errors import VaultError
from .paths import VaultPaths
from .pdf_process import PdfPreviewBusy, pdf_request, _unique_object

XFA_WARNING = "this is an XFA form; its filled data may not show here"


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    warning: str = ""


@dataclass(frozen=True)
class PdfPagePreview:
    png: bytes
    page_count: int
    page_index: int
    warning: str = ""


def _info(metadata, expected):
    if (set(metadata) != expected or type(metadata.get("page_count")) is not int
            or not 0 <= metadata["page_count"] <= 1000000
            or not isinstance(metadata.get("warning"), str)
            or len(metadata["warning"]) > 1000):
        raise VaultError("PDF page not shown: invalid child result.")


def read_pdf_info(paths: VaultPaths, path: Path, *, wait=True, cancel_event=None, timeout_s=10.0) -> PdfInfo:
    """Read count and form warning. Busy refuses before reading source bytes."""
    metadata, body = pdf_request((paths, path), "info", wait=wait, cancel_event=cancel_event, timeout_s=timeout_s)
    _info(metadata, {"page_count", "warning"})
    if body:
        raise VaultError("PDF page not shown: invalid child result.")
    return PdfInfo(**metadata)


def _render(source, page, width, wait, cancel_event, timeout_s):
    if type(page) is not int or page < 0:
        raise VaultError("PDF page must be a non-negative integer.")
    if type(width) is not int or not 1 <= width <= 1600:
        raise VaultError("PDF preview width must be between 1 and 1600 pixels.")
    metadata, png = pdf_request(source, "render", page=page, width=width, wait=wait,
                                cancel_event=cancel_event, timeout_s=timeout_s)
    _info(metadata, {"page_count", "page_index", "warning"})
    if (type(metadata["page_index"]) is not int or metadata["page_index"] != page
            or not page < metadata["page_count"] or len(png) < 33
            or not png.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR")):
        raise VaultError("PDF page not shown: invalid child result.")
    actual_width, height = struct.unpack(">II", png[16:24])
    # PDFium rounds its scaled dimensions upward; permit one rounding pixel,
    # still respecting the independent hard raster bounds.
    if (not width <= actual_width <= width + 1 or actual_width > 1600
            or not 1 <= height <= 10000 or actual_width * height > 16000000):
        raise VaultError("PDF page not shown: invalid child image size.")
    try:
        # Header dimensions are already bounded. verify checks PNG framing/CRC
        # without rasterizing; no PDF or native PDF library enters this process.
        with Image.open(BytesIO(png)) as picture:
            picture.verify()
    except Exception:
        raise VaultError("PDF page not shown: invalid child image.") from None
    return PdfPagePreview(png=png, **metadata)


def render_pdf_page(paths: VaultPaths, path: Path, page=0, width=1200, *,
                    wait=True, cancel_event=None, timeout_s=10.0) -> PdfPagePreview:
    """Render one visible page including AcroForms, with a 10-second ceiling."""
    return _render((paths, path), page, width, wait, cancel_event, timeout_s)


def render_pdf_bytes_page(data: bytes, page=0, width=1600, *,
                          wait=True, cancel_event=None, timeout_s=10.0) -> PdfPagePreview:
    """Same renderer for an immutable OCR snapshot; never reopens its source."""
    return _render(data, page, width, wait, cancel_event, timeout_s)


def read_pdf_bytes_text_page(data: bytes, page=0, *, cancel_event=None, timeout_s=10.0):
    """One native text/form page, not unbounded whole-document extraction."""
    if type(page) is not int or page < 0:
        raise VaultError("PDF page must be a non-negative integer.")
    metadata, body = pdf_request(data, "text", page=page, cancel_event=cancel_event, timeout_s=timeout_s)
    _info(metadata, {"page_count", "page_index", "warning"})
    try:
        result = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(result, dict) or set(result) != {"native", "fields", "warnings"}
                or any(not isinstance(result[key], str) or len(result[key]) > 100000
                       for key in ("native", "fields"))
                or not isinstance(result["warnings"], list) or len(result["warnings"]) > 2
                or any(not isinstance(value, str) or len(value) > 1000 for value in result["warnings"])
                or type(metadata["page_index"]) is not int or metadata["page_index"] != page
                or not page < metadata["page_count"]):
            raise ValueError("invalid text fields")
    except Exception:
        raise VaultError("PDF text not read: invalid child result.") from None
    return metadata["page_count"], result["native"], result["fields"], result["warnings"]
