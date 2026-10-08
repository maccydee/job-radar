"""A minimal real PDF, built as bytes, for tests that need a text layer.

Not named `test_*`, so `run_all` does not collect it. The content streams are
left uncompressed so the text is plain in the file, and the xref table is
computed rather than guessed, because pypdf reads a PDF with a wrong xref by
silently rebuilding it and that would hide a bad fixture.
"""

from __future__ import annotations

from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path, lines: list[str], *, pages: int = 1, author: str | None = None) -> Path:
    """Write a PDF with `pages` pages, each carrying every line of `lines`."""
    objs: list[bytes] = []                       # object n is objs[n - 1]

    def add(body: str | bytes) -> int:
        objs.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objs)

    catalog = add("")                            # filled in below
    pages_obj = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for _ in range(pages):
        text = "".join(f"BT /F1 10 Tf 40 {780 - 14 * i} Td ({_esc(ln)}) Tj ET\n"
                       for i, ln in enumerate(lines))
        stream = text.encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        kids.append(add(f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 595 842] "
                        f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"))
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("latin-1")
    objs[pages_obj - 1] = (f"<< /Type /Pages /Count {pages} /Kids ["
                           + " ".join(f"{k} 0 R" for k in kids) + "] >>").encode("latin-1")
    info = add(f"<< /Author ({_esc(author)}) >>") if author is not None else None

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = f"<< /Size {len(objs) + 1} /Root {catalog} 0 R"
    if info:
        trailer += f" /Info {info} 0 R"
    out += ("trailer\n" + trailer + " >>\n").encode("latin-1") + b"startxref\n%d\n%%%%EOF\n" % xref
    p = Path(path)
    p.write_bytes(bytes(out))
    return p
