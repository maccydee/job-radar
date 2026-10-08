"""Text out of a PDF, or an honest statement that it could not be got.

Returns (text, why). `text` is None when no extractor could run, and the
caller must treat that as "not measured", never as "empty" and never as
"fine". Both extractors are optional: pypdf is the `[pdf]` extra and
pdftotext is poppler's.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _have_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401  (WHY: lazy, the package works without it)
        return True
    except ImportError:
        return False


def _have_pdftotext() -> bool:
    return shutil.which("pdftotext") is not None


def _clean(text: str) -> str:
    # pdftotext ends every page with a form feed, which is a control character
    # and would count against the text as though the PDF were garbled.
    return text.replace("\x0c", "\n")


def extract(path) -> tuple[str | None, str]:
    p = Path(path)
    failures = []
    if _have_pypdf():
        from pypdf import PdfReader
        try:
            r = PdfReader(str(p))
            return _clean("\n".join((pg.extract_text() or "") for pg in r.pages)), "pypdf"
        except Exception as e:                        # a broken PDF is a finding
            failures.append(f"pypdf could not read {p.name}: {type(e).__name__}: {e}")
    if _have_pdftotext():
        try:
            r = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               stdin=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired:
            failures.append(f"pdftotext timed out on {p.name}")
        else:
            if r.returncode == 0:
                return _clean(r.stdout), "pdftotext"
            failures.append(f"pdftotext failed on {p.name}: {r.stderr.strip()[:200]}")
    if failures:
        return None, "; ".join(failures)
    return None, ("neither pypdf nor pdftotext is installed: "
                  "`pip install pypdf` or install poppler")


def _needs_pypdf(what: str) -> str:
    """Why `what` could not be read. Names both tools when neither is here, so
    the person knows the whole of what is missing and not half of it."""
    if not _have_pdftotext():
        return ("neither pypdf nor pdftotext is installed: "
                "`pip install pypdf` or install poppler")
    return f"the {what} needs pypdf: `pip install pypdf` (pdftotext cannot report it)"


def pages(path) -> tuple[int | None, str]:
    if _have_pypdf():
        from pypdf import PdfReader
        try:
            return len(PdfReader(str(path)).pages), "pypdf"
        except Exception as e:
            return None, f"pypdf could not read {Path(path).name}: {e}"
    return None, _needs_pypdf("page count")


def author(path) -> tuple[str | None, str]:
    if _have_pypdf():
        from pypdf import PdfReader
        try:
            meta = PdfReader(str(path)).metadata
            return ((meta.author if meta else "") or ""), "pypdf"
        except Exception as e:
            return None, f"pypdf could not read {Path(path).name}: {e}"
    return None, _needs_pypdf("author")
