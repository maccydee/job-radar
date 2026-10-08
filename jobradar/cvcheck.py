"""A lint for a CV or cover letter, wherever it was made.

`generate` runs its gates on the drafts it writes. A CV written by hand, edited
afterwards or exported again is not covered by any of them, and three PDFs went
out carrying a claim their author had ruled out, because nothing ever compared
a PDF with the document it was made from.

Every check returns one of three states, and the third is the point:

    pass         it ran and found nothing
    fail         it ran and found something
    unmeasured   it could not run (no claims file, no PDF library, no source
                 to compare with), and nothing was learned

`unmeasured` is reported and counted against the file. Reading "could not
check" as "checked, fine" is how a quality gate that never ran rendered as one
that passed, so the exit code is 0 only when every check passed.
"""

from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import atscheck, pdftext

PASS, FAIL, UNMEASURED = "pass", "fail", "unmeasured"
CLAIMS_PATHS = [Path("claims.local.yaml"), Path("claims.yaml")]
TEXT_SUFFIXES = (".md", ".txt", ".docx", ".pdf")
EM_DASH = chr(0x2014)       # written as a code point so no em-dash sits in the source
DAY = 86400
MIN_TEXT = 200


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""


# ------------------------------------------------------------------ claims

def _search_paths() -> list[Path]:
    repo = Path(__file__).resolve().parent.parent
    return CLAIMS_PATHS + [repo / p for p in CLAIMS_PATHS]


def load_claims(path: Path | None = None):
    """(claims, problem). `claims` is None when no file could be read, which is
    different from a file that says `banned: []`."""
    if path is not None:
        p = Path(path)
        if not p.is_file():
            return None, f"claims file {p} does not exist"
    else:
        p = next((c for c in _search_paths() if c.is_file()), None)
        if p is None:
            return None, ("no claims file found (looked for "
                          + ", ".join(str(c) for c in CLAIMS_PATHS) + ")")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        return None, f"claims file {p} is not valid YAML: {e}"
    if not isinstance(raw, dict) or not raw:
        return None, f"claims file {p} is empty"
    banned_raw = raw.get("banned")
    banned, bad = None, []
    if isinstance(banned_raw, list):
        banned = []
        for entry in banned_raw:
            pattern = entry.get("pattern") if isinstance(entry, dict) else None
            if not isinstance(pattern, str) or not pattern:
                bad.append((str(entry)[:60], "entry has no `pattern`"))
                continue
            try:
                banned.append((re.compile(pattern, re.I), pattern,
                               str(entry.get("why") or "")))
            except re.error as e:
                bad.append((pattern, str(e)))
    elif banned_raw is not None:
        return None, f"claims file {p}: `banned` must be a list"
    author = raw.get("author")
    max_pages = raw.get("max_pages")
    email = raw.get("email")
    return {"path": str(p), "banned": banned, "bad_patterns": bad,
            "author": str(author) if author else None,
            "email": str(email) if email else None,
            "max_pages": int(max_pages) if isinstance(max_pages, int) else None}, ""


# --------------------------------------------------------------------- text

def read_document(path: Path) -> tuple[str | None, str]:
    """The document's text, or None and the reason it could not be got. Never
    an empty string standing in for a failure."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".md", ".txt"):
            text = path.read_bytes().decode("utf-8")
        elif suffix == ".docx":
            from .runner import docx_to_text
            text = docx_to_text(path)
            if not text.strip():
                return None, f"no text could be read from {path.name} (is it a valid .docx?)"
        elif suffix == ".pdf":
            text, why = pdftext.extract(path)
            if text is None:
                return None, why
        else:
            return None, f"{path.name}: not a file type cvcheck reads"
    except UnicodeDecodeError:
        return None, f"{path.name} is not UTF-8 text"
    except OSError as e:
        return None, f"{path.name} could not be read: {e}"
    if not text.strip():
        return None, f"{path.name} is empty"
    return text, ""


def _collapsed(text: str) -> tuple[str, list[int]]:
    """The text with every run of whitespace (newlines included) as one space,
    and for each character of it the line number it came from."""
    out, lines, n, gap = [], [], 1, False
    for ch in text:
        if ch.isspace():
            if not gap and out:
                out.append(" ")
                lines.append(n)
            gap = True
            if ch == "\n":
                n += 1
            continue
        gap = False
        out.append(ch)
        lines.append(n)
    return "".join(out), lines


def _lines_matching(text: str, rx: re.Pattern):
    """Every hit as (first line, quoted text), matched against the whole text.

    Line by line missed a claim the PDF wrapped: "two engineers now" / "run
    workstreams" passed with a pattern of single spaces and with `\\s+`, and a
    PDF's text wraps at the visual line, so that is the normal case.
    """
    flat, line_of = _collapsed(text)
    seen = set()
    raw_lines = text.splitlines()
    for m in rx.finditer(flat):
        if m.end() == m.start():
            continue
        first, last = line_of[m.start()], line_of[m.end() - 1]
        if first in seen:
            continue
        seen.add(first)
        quoted = " ".join(raw_lines[i - 1].strip() for i in range(first, last + 1)
                          if i - 1 < len(raw_lines))
        yield first, quoted


def banned_check(text: str, claims, problem: str = "") -> Check:
    if claims is None:
        return Check("banned_phrase", UNMEASURED,
                     "no claims file: cvcheck cannot say a phrase is banned"
                     + (f" ({problem})" if problem else ""))
    if claims["banned"] is None:
        return Check("banned_phrase", UNMEASURED,
                     f"{claims['path']} has no `banned` list; write `banned: []` "
                     f"to say there is nothing to ban")
    hits = []
    for rx, pattern, why in claims["banned"]:
        for n, line in _lines_matching(text, rx):
            hits.append(f'line {n}: "{line[:100]}"' + (f" ({why})" if why else ""))
    if hits:
        return Check("banned_phrase", FAIL, "; ".join(hits[:5]))
    if claims["bad_patterns"]:
        return Check("banned_phrase", UNMEASURED,
                     f"{len(claims['bad_patterns'])} pattern(s) in {claims['path']} "
                     f"do not compile, so those phrases were not checked: "
                     + "; ".join(f"{p!r} ({e})" for p, e in claims["bad_patterns"]))
    return Check("banned_phrase", PASS,
                 f"none of {len(claims['banned'])} banned phrase(s) found")


def invented_check(text: str, master_text: str | None) -> Check:
    if master_text is None:
        return Check("invented_specifics", UNMEASURED,
                     "no --master CV given, so nothing to compare the figures with")
    from .runner import _invented
    found = _invented(text, master_text)
    if found:
        return Check("invented_specifics", FAIL,
                     "not in the master CV: " + ", ".join(found))
    return Check("invented_specifics", PASS, "every figure appears in the master CV")


def em_dash_check(text: str) -> Check:
    lines = [n for n, line in enumerate(text.splitlines(), 1) if EM_DASH in line]
    if lines:
        return Check("no_em_dash", FAIL,
                     "em-dash on line " + ", ".join(str(n) for n in lines[:10]))
    return Check("no_em_dash", PASS, "")


def text_layer_check(text: str) -> Check:
    """A PDF whose text layer is missing or is the file's own bytes. CLAUDE.md
    records this exact failure: a PDF read as UTF-8 became thousands of
    characters of `%PDF-1.4` and object tables."""
    stripped = text.lstrip()
    if stripped.startswith("%PDF"):
        return Check("text_layer", FAIL,
                     "the text begins %PDF: the file was read as text, not parsed")
    if len(text.strip()) < MIN_TEXT:
        return Check("text_layer", FAIL,
                     f"{len(text.strip())} characters of text; under {MIN_TEXT} means "
                     f"no usable text layer (a scanned image, or outlined text)")
    return Check("text_layer", PASS, f"{len(text.strip())} characters of text")


# -------------------------------------------------------------- the files

def _sources_beside(pdf: Path) -> list[Path]:
    stem = pdf.stem
    names = [stem + ".md", stem + ".docx"]
    low = stem.lower()
    if "cover" in low:
        names += ["cover-letter.md"]
    elif "cv" in low or "resume" in low:
        names += ["CV.md", "CV.docx"]
    else:
        names += ["CV.md", "CV.docx", "cover-letter.md"]
    seen, out = set(), []
    for n in names:
        p = pdf.parent / n
        if n not in seen and p.is_file() and p.resolve() != pdf.resolve():
            seen.add(n)
            out.append(p)
    return out


def _span(seconds: float) -> str:
    days = int(seconds // DAY)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = max(1, int(seconds // 3600))
    return f"{hours} hour{'s' if hours != 1 else ''}"


def stale_pdf_check(pdf: Path) -> Check:
    sources = _sources_beside(pdf)
    if not sources:
        return Check("stale_pdf", UNMEASURED,
                     "no CV.md, CV.docx or cover-letter.md beside it to compare with")
    newest = max(sources, key=lambda p: p.stat().st_mtime)
    gap = newest.stat().st_mtime - pdf.stat().st_mtime
    if gap > 0:
        return Check("stale_pdf", FAIL,
                     f"{pdf.name} is older than {newest.name} by {_span(gap)}: "
                     f"re-export it before it is sent")
    return Check("stale_pdf", PASS, f"newer than {newest.name}")


def _docx_author(path: Path) -> tuple[str | None, str]:
    try:
        with zipfile.ZipFile(path) as z:
            try:
                xml = z.read("docProps/core.xml").decode("utf-8", "replace")
            except KeyError:
                return "", "docx"                       # no properties at all
    except (zipfile.BadZipFile, OSError) as e:
        return None, f"{path.name} could not be opened as a .docx: {e}"
    m = re.search(r"<dc:creator>(.*?)</dc:creator>", xml, re.S)
    return (html.unescape(m.group(1)).strip() if m else ""), "docx"


def author_check(path: Path, expected: str | None) -> Check:
    if path.suffix.lower() == ".docx":
        found, why = _docx_author(path)
    else:
        found, why = pdftext.author(path)
    if not expected:
        return Check("author", UNMEASURED,
                     "no author configured: pass --author or set `author` in the claims file")
    if found is None:
        return Check("author", UNMEASURED, why)
    if not found.strip():
        return Check("author", FAIL,
                     f"no author set in the file properties (expected {expected!r}); "
                     f"a blank author is what a recruiter notices")
    if found.strip().lower() != expected.strip().lower():
        return Check("author", FAIL, f"author is {found!r}, expected {expected!r}")
    return Check("author", PASS, found)


def pages_check(path: Path, limit: int | None) -> Check:
    n, why = pdftext.pages(path)
    if limit is None:
        return Check("pages", UNMEASURED,
                     "no page limit configured: pass --max-pages or set `max_pages` "
                     "in the claims file")
    if n is None:
        return Check("pages", UNMEASURED, why)
    if n > limit:
        return Check("pages", FAIL, f"{n} pages, limit {limit}")
    return Check("pages", PASS, f"{n} page{'s' if n != 1 else ''}, limit {limit}")


def ats_checks(text: str | None, reason: str, contact_email: str | None,
               jd_text: str | None) -> list[Check]:
    """The checks that depend on what a parser can read out of the PDF. `text`
    is None when it could not be got, and then all three are unmeasured with
    the reason: a PDF nobody could read has not passed anything."""
    if text is None:
        return [Check(n, UNMEASURED, reason)
                for n in ("garbled_glyphs", "contact", "keyword_coverage")]
    bad, detail = atscheck.garbled(text)
    out = [Check("garbled_glyphs", FAIL if bad else PASS, detail)]
    if not contact_email:
        out.append(Check("contact", UNMEASURED,
                         "no contact email configured: pass --contact-email or set "
                         "`email` in the claims file"))
    else:
        ok, detail = atscheck.contact_check(text, contact_email)
        out.append(Check("contact", PASS if ok else FAIL, detail))
    if jd_text is None:
        out.append(Check("keyword_coverage", UNMEASURED,
                         "no --role given, so no posting to compare with"))
    elif not jd_text.strip():
        out.append(Check("keyword_coverage", UNMEASURED,
                         "the role has no description stored, so nothing to compare with"))
    else:
        terms = atscheck.jd_terms(jd_text)
        covered, missing = atscheck.keyword_coverage(text, jd_text)
        if not terms:
            out.append(Check("keyword_coverage", UNMEASURED,
                             "no distinctive terms could be read from the posting"))
        else:
            # A gap is information, so the state is pass: the check measured
            # something and nothing is wrong with the file. It is never a
            # reason to add a word the CV's author cannot stand behind.
            out.append(Check(
                "keyword_coverage", PASS,
                f"{len(covered)} of {len(terms)} covered"
                + (f"; not found: {', '.join(missing)}" if missing else "")))
    return out


def check_file(path: Path, *, claims, master_text: str | None, author_name: str | None,
               max_pages: int | None, claims_problem: str = "",
               contact_email: str | None = None, jd_text: str | None = None) -> list[Check]:
    path = Path(path)
    suffix = path.suffix.lower()
    text, why = read_document(path)
    checks: list[Check] = []
    usable, reason = text is not None, why
    if suffix == ".pdf":
        if text is None:
            tl = Check("text_layer", UNMEASURED, why)
        else:
            tl = text_layer_check(text)
        checks.append(tl)
        usable = tl.state == PASS
        reason = tl.detail if text is not None else why
        checks += ats_checks(text if usable else None, reason, contact_email, jd_text)
    if usable:
        checks += [banned_check(text, claims, claims_problem),
                   invented_check(text, master_text),
                   em_dash_check(text)]
    else:
        # Not a pass: a document with no readable words has no banned phrases
        # only in the sense that nothing was looked at.
        why_not = f"the text could not be checked: {reason}"
        checks += [Check("banned_phrase", UNMEASURED, why_not),
                   Check("invented_specifics", UNMEASURED, why_not),
                   Check("no_em_dash", UNMEASURED, why_not)]
    if suffix == ".pdf":
        checks.append(stale_pdf_check(path))
    if suffix in (".docx", ".pdf"):
        checks.append(author_check(path, author_name))
    if suffix == ".pdf":
        checks.append(pages_check(path, max_pages))
    return checks
