"""Put a real PDF and a real photograph into the demo vault.

Hand-made bytes, so the check does not depend on any file of the owner's.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path


def tiny_pdf(text: str) -> bytes:
    """A valid one-page PDF, written by hand so there is no dependency."""
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{start}\n%%EOF\n").encode()
    return bytes(out)


def tiny_png(width: int = 320, height: int = 200) -> bytes:
    """A PNG with a couple of stripes, so a viewer showing it is obvious."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            band = (y * 4 // height) % 2
            raw += bytes((217, 119, 87) if band else (250, 249, 245))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big")
                    + bytes((8, 2, 0, 0, 0)))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def main(root: Path) -> None:
    documents = root / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    (documents / "Insurance policy scan.pdf").write_bytes(tiny_pdf("Blue Shield PPO-4471 2026"))
    (documents / "Pharmacy receipt.png").write_bytes(tiny_png())
    (documents / "Spreadsheet.xlsx").write_bytes(b"PK\x03\x04 not viewable on purpose")
    print("added a PDF, a picture and one deliberately unviewable file")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
