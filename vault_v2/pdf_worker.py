"""Private one-page PDF parser/renderer; stdin snapshot, bounded binary reply."""
from io import BytesIO
import json
import math
import socket
import sys

MAX_INPUT = 512 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024
PAGE_TEXT_LIMIT = 100000
XFA_WARNING = "this is an XFA form; its filled data may not show here"


class Refused(Exception):
    pass


def _deny_socket(*args, **kwargs):
    raise RuntimeError("Python network is not used by the PDF worker")


def _form_warning(data):
    from pypdf import PdfReader
    from pypdf.generic import DictionaryObject

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return "This PDF needs an opening password; no preview is available."
        catalog = reader.trailer["/Root"]
        if not isinstance(catalog, DictionaryObject):
            raise ValueError("Invalid PDF catalog")
        form = catalog.get("/AcroForm")
        if form is not None:
            form = form.get_object()
            if not isinstance(form, DictionaryObject):
                raise ValueError("Invalid PDF form dictionary")
            if "/XFA" in form:
                return XFA_WARNING
    except Exception:
        return "PDF form structure could not be checked; filled data may not show."
    return ""


def _render(document, page, width):
    current = bitmap = None
    try:
        current = document.get_page(page)
        point_width, point_height = current.get_size()
        if (not math.isfinite(point_width) or not math.isfinite(point_height)
                or point_width <= 0 or point_height <= 0):
            raise Refused("geometry")
        scaled_height = width * point_height / point_width
        if not math.isfinite(scaled_height) or scaled_height > 10000:
            raise Refused("tall")
        if width * math.ceil(scaled_height) > 16000000:
            raise Refused("tall")
        bitmap = current.render(scale=width / point_width, draw_annots=True, may_draw_forms=True)
        picture = bitmap.to_pil()
        output = BytesIO()
        try:
            picture.save(output, format="PNG")
        finally:
            picture.close()
        result = output.getvalue()
        if len(result) > MAX_OUTPUT - 4096:
            raise Refused("output")
        return result
    finally:
        if bitmap is not None:
            bitmap.close()
        if current is not None:
            current.close()


def _form_text(data: bytes, selected_page: int):
    """Read stored values (not appearances), keeping each widget on its page."""
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Refused("password")
        form = reader.trailer["/Root"].get("/AcroForm")
        warnings = []
        if form is not None and "/XFA" in form.get_object():
            warnings.append("XFA form data was not extracted; visible page text may be incomplete.")
        result = []
        for index in (selected_page,):
            lines = []
            seen_lines, page_chars = set(), 0
            annotations = reader.pages[index].get("/Annots", ArrayObject()).get_object()
            if not isinstance(annotations, ArrayObject):
                raise ValueError("Invalid form annotation array")
            if len(annotations) > 10000:
                raise ValueError("Too many form annotations")
            for reference in annotations:
                field = reference.get_object()
                if not isinstance(field, DictionaryObject) or field.get("/Subtype") != "/Widget":
                    continue
                names, value, seen = [], None, set()
                for depth in range(32):
                    if not isinstance(field, DictionaryObject) or id(field) in seen:
                        raise ValueError("Invalid form ancestry")
                    seen.add(id(field))
                    name = field.get("/T")
                    if isinstance(name, TextStringObject) and name:
                        if len(name) > PAGE_TEXT_LIMIT or sum(map(len, names)) + len(name) > PAGE_TEXT_LIMIT:
                            raise ValueError("Form name limit")
                        names.insert(0, str(name))
                    if value is None and "/V" in field:
                        value = field["/V"]
                    parent = field.get("/Parent")
                    if parent is None:
                        break
                    field = parent.get_object()
                else:
                    raise ValueError("Form ancestry too deep")
                if isinstance(value, (TextStringObject, NameObject)):
                    if len(value) > PAGE_TEXT_LIMIT:
                        raise ValueError("Form value limit")
                    value_text = str(value)
                elif isinstance(value, ArrayObject) and all(isinstance(v, (TextStringObject, NameObject)) for v in value):
                    if sum(len(v) + 2 for v in value) > PAGE_TEXT_LIMIT:
                        raise ValueError("Form value limit")
                    value_text = ", ".join(str(v) for v in value)
                else:
                    value_text = ""
                if value_text:
                    label = '.'.join(names) or 'unnamed field'
                    if len(label) + len(value_text) + 2 > PAGE_TEXT_LIMIT:
                        raise ValueError("Form text limit")
                    key = (label, value_text)
                    if key not in seen_lines:
                        page_chars += len(label) + len(value_text) + 3
                        if page_chars > PAGE_TEXT_LIMIT:
                            raise ValueError("Form text limit")
                        seen_lines.add(key)
                        lines.append(f"{label}: {value_text}")
            text = "\n".join(lines)
            result.append(text)
        return result, warnings
    except Refused:
        raise
    except Exception:
        raise Refused("forms") from None


def _native_text(document, data, index):
    page = textpage = None
    try:
        page = document.get_page(index)
        textpage = page.get_textpage()
        if textpage.count_chars() > PAGE_TEXT_LIMIT:
            raise Refused("text_limit")
        native = textpage.get_text_range()
    finally:
        if textpage is not None:
            textpage.close()
        if page is not None:
            page.close()
    fields, warnings = _form_text(data, index)
    if len(native) + len(fields[0]) + bool(native and fields[0]) > PAGE_TEXT_LIMIT:
        raise Refused("text_limit")
    return json.dumps({"native": native, "fields": fields[0], "warnings": warnings},
                      ensure_ascii=False).encode("utf-8")


def main():
    # Python only: not an OS/native-DLL network or filesystem sandbox.
    socket.socket = _deny_socket
    header = sys.stdin.buffer.readline(4097)
    if len(header) > 4096 or not header.endswith(b"\n"):
        raise Refused("decode")
    request = json.loads(header)
    if not isinstance(request, dict) or set(request) != {"operation", "page", "width"}:
        raise Refused("decode")
    operation, page, width = request["operation"], request["page"], request["width"]
    if (operation not in ("info", "render", "text") or type(page) is not int or page < 0
            or type(width) is not int or not 1 <= width <= 1600):
        raise Refused("decode")
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if not data or len(data) > MAX_INPUT:
        raise Refused("decode")
    import pypdfium2 as pdfium

    document = None
    try:
        document = pdfium.PdfDocument(data, password="")
        count = len(document)
        metadata = {"page_count": count, "warning": _form_warning(data) if operation != "text" else ""}
        if operation == "info":
            return metadata, b""
        if page >= count:
            raise Refused("page")
        if operation == "render":
            document.init_forms()
            metadata["page_index"] = page
            return metadata, _render(document, page, width)
        metadata["page_index"] = page
        return metadata, _native_text(document, data, page)
    except pdfium.PdfiumError:
        raise Refused("decode") from None
    finally:
        if document is not None:
            document.close()


if __name__ == "__main__":
    try:
        metadata, body = main()
    except Refused as exc:
        metadata, body = {"error": str(exc)}, b""
    except Exception:
        metadata, body = {"error": "worker_failed"}, b""
    output = json.dumps(metadata, ensure_ascii=True).encode("ascii") + b"\n"
    if len(output) + len(body) > MAX_OUTPUT:
        output, body = b'{"error":"output"}\n', b""
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.write(body)
