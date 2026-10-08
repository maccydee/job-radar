import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from jobradar import cli, cvcheck, pdftext    # noqa: E402
from pdfmaker import make_pdf                 # noqa: E402

NAME = "Alex Morgan"
LINE = f"{NAME} alex@example.com 07700 900123 Engineering Manager who leads platform teams"
DAY = 86400


def _dir():
    return Path(tempfile.mkdtemp())


def _claims(d, text=None):
    p = d / "claims.local.yaml"
    p.write_text(text if text is not None else (
        "author: Alex Morgan\nmax_pages: 2\nbanned:\n"
        "  - pattern: 'two (engineers )?(now )?run'\n"
        "    why: 'the backlog fell to 1-3, and two engineers do not run workstreams'\n"),
        encoding="utf-8")
    return p


def _load(d, text=None):
    claims, problem = cvcheck.load_claims(_claims(d, text))
    assert claims is not None, problem
    return claims


def _by(checks):
    return {c.name: c for c in checks}


def _md(d, body, name="CV.md"):
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def _check(path, claims=None, master=None, author=None, max_pages=None):
    return _by(cvcheck.check_file(Path(path), claims=claims, master_text=master,
                                  author_name=author, max_pages=max_pages))


# --------------------------------------------------------------- the claims

def test_a_banned_phrase_fails_and_quotes_the_line_and_the_reason():
    d = _dir()
    cv = _md(d, f"# {NAME}\n\nLed the team.\nTwo now run workstreams.\n")
    c = _check(cv, claims=_load(d))["banned_phrase"]
    assert c.state == cvcheck.FAIL
    assert "line 4" in c.detail and "Two now run workstreams" in c.detail
    assert "do not run workstreams" in c.detail


def test_a_clean_document_passes_the_banned_check():
    d = _dir()
    cv = _md(d, f"# {NAME}\n\nLed the team.\n")
    assert _check(cv, claims=_load(d))["banned_phrase"].state == cvcheck.PASS


def test_no_claims_file_is_unmeasured_never_a_pass():
    d = _dir()
    cv = _md(d, "Two now run workstreams.\n")
    c = _check(cv, claims=None)["banned_phrase"]
    assert c.state == cvcheck.UNMEASURED
    assert "no claims file: cvcheck cannot say a phrase is banned" in c.detail


def test_an_empty_claims_file_is_unmeasured_and_an_explicit_empty_list_is_a_pass():
    d = _dir()
    claims, problem = cvcheck.load_claims(_claims(d, ""))
    assert claims is None and "empty" in problem
    claims, problem = cvcheck.load_claims(_claims(d, "author: X\n"))
    assert _check(_md(d, "anything\n"), claims=claims)["banned_phrase"].state == cvcheck.UNMEASURED
    claims, problem = cvcheck.load_claims(_claims(d, "banned: []\n"))
    assert _check(_md(d, "anything\n"), claims=claims)["banned_phrase"].state == cvcheck.PASS


def test_a_pattern_that_does_not_compile_is_reported_and_is_not_a_crash():
    d = _dir()
    claims, problem = cvcheck.load_claims(_claims(d, "banned:\n  - pattern: '('\n    why: x\n"))
    assert claims is not None and claims["bad_patterns"], claims
    c = _check(_md(d, "clean text\n"), claims=claims)["banned_phrase"]
    assert c.state == cvcheck.UNMEASURED and "do not compile" in c.detail, c


def test_a_claims_file_that_is_not_yaml_is_a_problem_not_a_pass():
    d = _dir()
    claims, problem = cvcheck.load_claims(_claims(d, "banned: [unclosed\n"))
    assert claims is None and "not valid YAML" in problem, problem


# ------------------------------------------------------------ invented / dash

def test_a_number_not_in_the_master_cv_is_an_invented_specific():
    d = _dir()
    cv = _md(d, "Managed 1,500 engineers across the group.\n")
    c = _check(cv, master="Managed a team of 12 engineers.")["invented_specifics"]
    assert c.state == cvcheck.FAIL and "1,500" in c.detail, c


def test_a_number_that_is_in_the_master_passes():
    d = _dir()
    cv = _md(d, "Managed 12 engineers.\n")
    assert _check(cv, master="Managed a team of 12 engineers.")["invented_specifics"].state == cvcheck.PASS


def test_without_a_master_cv_the_invented_check_is_unmeasured():
    d = _dir()
    assert _check(_md(d, "Managed 1,500 engineers.\n"))["invented_specifics"].state == cvcheck.UNMEASURED


def test_an_em_dash_fails_with_its_line_number():
    d = _dir()
    cv = _md(d, "fine line\nbroken " + chr(0x2014) + " line\n")
    c = _check(cv)["no_em_dash"]
    assert c.state == cvcheck.FAIL and "line 2" in c.detail
    assert _check(_md(d, "fine\n", "other.md"))["no_em_dash"].state == cvcheck.PASS


def test_an_empty_document_is_unmeasured_not_a_clean_pass():
    d = _dir()
    c = _check(_md(d, ""), claims=_load(d))
    assert c["banned_phrase"].state == cvcheck.UNMEASURED and "empty" in c["banned_phrase"].detail


# -------------------------------------------------------------- stale pdfs

def _age(path, days):
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def test_a_pdf_older_than_its_source_is_stale():
    d = _dir()
    pdf = make_pdf(d / "CV.pdf", [LINE] * 4)
    md = _md(d, "# x\n")
    _age(pdf, 5)
    _age(md, 2)
    c = _check(pdf)["stale_pdf"]
    assert c.state == cvcheck.FAIL
    assert "CV.pdf is older than CV.md by 3 days" in c.detail, c.detail


def test_a_pdf_newer_than_its_source_passes():
    d = _dir()
    md = _md(d, "# x\n")
    pdf = make_pdf(d / "CV.pdf", [LINE] * 4)
    _age(md, 2)
    assert _check(pdf)["stale_pdf"].state == cvcheck.PASS


def test_a_pdf_with_no_source_beside_it_is_unmeasured():
    d = _dir()
    pdf = make_pdf(d / "Alex-Morgan.pdf", [LINE] * 4)
    c = _check(pdf)["stale_pdf"]
    assert c.state == cvcheck.UNMEASURED
    assert "no CV.md, CV.docx or cover-letter.md beside it to compare with" in c.detail


def test_a_pdf_named_for_the_person_is_compared_with_cv_md_not_the_cover_letter():
    d = _dir()
    cl = _md(d, "letter\n", "cover-letter.md")
    pdf = make_pdf(d / "Alex-Morgan-CV-Engineering-Management.pdf", [LINE] * 4)
    cv = _md(d, "# x\n")
    _age(pdf, 5)
    _age(cv, 4)
    _age(cl, 0)                          # a newer letter says nothing about the CV
    c = _check(pdf)["stale_pdf"]
    assert c.state == cvcheck.FAIL and "CV.md" in c.detail and "cover-letter" not in c.detail, c


# ------------------------------------------------------------ docx and pdf

def _docx(d, creator, name="CV.docx"):
    p = d / name
    core = ('<?xml version="1.0"?><cp:coreProperties xmlns:cp="x" xmlns:dc="y">'
            + (f"<dc:creator>{creator}</dc:creator>" if creator is not None else "")
            + "</cp:coreProperties>")
    doc = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello CV text</w:t></w:r></w:p>'
           "</w:body></w:document>")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("docProps/core.xml", core)
        z.writestr("word/document.xml", doc)
    return p


def test_a_docx_with_another_author_fails_naming_both():
    d = _dir()
    c = _check(_docx(d, "Somebody Else"), author=NAME)["author"]
    assert c.state == cvcheck.FAIL
    assert "Somebody Else" in c.detail and NAME in c.detail, c


def test_a_docx_with_the_right_author_passes_and_a_blank_one_fails():
    d = _dir()
    assert _check(_docx(d, NAME), author=NAME)["author"].state == cvcheck.PASS
    blank = _check(_docx(d, None, "b.docx"), author=NAME)["author"]
    assert blank.state == cvcheck.FAIL and "no author" in blank.detail, blank


def test_no_configured_author_is_unmeasured():
    d = _dir()
    assert _check(_docx(d, NAME))["author"].state == cvcheck.UNMEASURED


def test_a_docx_that_is_not_a_zip_is_unmeasured_not_a_pass():
    d = _dir()
    bad = d / "CV.docx"
    bad.write_bytes(b"not a zip " * 30)
    c = _check(bad, author=NAME)
    assert c["author"].state == cvcheck.UNMEASURED


def _needs_pypdf():
    if not pdftext._have_pypdf():
        raise unittest.SkipTest("pypdf not installed")


def test_a_pdf_over_the_page_limit_fails():
    _needs_pypdf()
    d = _dir()
    pdf = make_pdf(d / "CV.pdf", [LINE] * 4, pages=3)
    c = _check(pdf, max_pages=2)["pages"]
    assert c.state == cvcheck.FAIL and "3 pages, limit 2" in c.detail, c
    assert _check(make_pdf(d / "ok.pdf", [LINE] * 4, pages=2), max_pages=2)["pages"].state == cvcheck.PASS


def test_a_pdf_author_is_checked_too():
    _needs_pypdf()
    d = _dir()
    pdf = make_pdf(d / "CV.pdf", [LINE] * 4, author="Somebody Else")
    assert _check(pdf, author=NAME)["author"].state == cvcheck.FAIL
    assert _check(make_pdf(d / "b.pdf", [LINE] * 4, author=NAME), author=NAME)["author"].state == cvcheck.PASS


def test_a_pdf_with_a_text_layer_passes_and_reads_the_claims_against_it():
    if not (pdftext._have_pypdf() or pdftext._have_pdftotext()):
        raise unittest.SkipTest("no PDF extractor installed")
    d = _dir()
    bad = make_pdf(d / "CV.pdf", [LINE] * 4 + ["Two now run workstreams across the group."])
    c = _check(bad, claims=_load(d))
    assert c["text_layer"].state == cvcheck.PASS
    assert c["banned_phrase"].state == cvcheck.FAIL, c["banned_phrase"]


def test_with_no_extractor_the_pdf_checks_are_unmeasured_and_name_both_tools():
    d = _dir()
    pdf = make_pdf(d / "CV.pdf", [LINE] * 4)
    real = pdftext._have_pypdf, pdftext._have_pdftotext
    pdftext._have_pypdf = pdftext._have_pdftotext = lambda: False
    try:
        c = _check(pdf, claims=_load(d), author=NAME, max_pages=2)
    finally:
        pdftext._have_pypdf, pdftext._have_pdftotext = real
    for name in ("text_layer", "pages", "author", "banned_phrase"):
        assert c[name].state == cvcheck.UNMEASURED, (name, c[name])
    assert "pypdf" in c["text_layer"].detail and "pdftotext" in c["text_layer"].detail


def test_text_under_200_characters_or_a_raw_pdf_header_is_not_a_text_layer():
    short = cvcheck.text_layer_check("Alex Morgan")
    assert short.state == cvcheck.FAIL
    raw = cvcheck.text_layer_check("%PDF-1.4\n" + "1 0 obj << /Type /Catalog >> endobj\n" * 20)
    assert raw.state == cvcheck.FAIL and "%PDF" in raw.detail
    assert cvcheck.text_layer_check(LINE * 4).state == cvcheck.PASS


def test_a_pdf_with_no_usable_text_does_not_pass_the_text_checks():
    """An image-only PDF has no words to be banned; saying so is a pass that
    means nothing, so the text-based checks are unmeasured instead."""
    if not (pdftext._have_pypdf() or pdftext._have_pdftotext()):
        raise unittest.SkipTest("no PDF extractor installed")
    d = _dir()
    pdf = make_pdf(d / "CV.pdf", ["x"])
    c = _check(pdf, claims=_load(d))
    assert c["text_layer"].state == cvcheck.FAIL
    assert c["banned_phrase"].state == cvcheck.UNMEASURED


# ---------------------------------------------------------------------- CLI

def _run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["cvcheck", *argv])
    return code, buf.getvalue()


def test_a_path_that_does_not_exist_is_exit_2_and_named():
    d = _dir()
    good = _md(d, "fine\n")
    code, out = _run(str(good), str(d / "missing.pdf"), "--claims", str(_claims(d)))
    assert code == 2 and "missing.pdf" in out, out


def test_a_failing_file_is_exit_1_and_the_summary_counts_it():
    d = _dir()
    cv = _md(d, "Two now run workstreams.\n")
    code, out = _run(str(cv), "--claims", str(_claims(d)), "--master", str(_md(d, "m\n", "m.md")),
                     "--author", NAME)
    assert code == 1, out
    assert "FAIL  banned_phrase" in out and "1 file(s): 1 failed" in out, out


def test_unmeasured_checks_alone_make_the_exit_code_1():
    d = _dir()
    cv = _md(d, "A perfectly fine line.\n")
    code, out = _run(str(cv), "--claims", str(_claims(d)))        # no master, no author
    assert code == 1, out
    assert "----  invented_specifics" in out and "unmeasured" in out, out


def test_everything_passing_is_exit_0():
    d = _dir()
    cv = _md(d, "A perfectly fine line with 12 engineers.\n")
    master = _md(d, "Led 12 engineers.\n", "master.md")
    docx = _docx(d, NAME)
    code, out = _run(str(cv), "--claims", str(_claims(d)), "--master", str(master),
                     "--author", NAME)
    assert code == 0, out
    assert "0 failed, 0 unmeasured" in out, out


def test_json_output_lists_every_check():
    d = _dir()
    cv = _md(d, "Two now run workstreams.\n")
    code, out = _run(str(cv), "--claims", str(_claims(d)), "--json")
    data = json.loads(out)
    names = {c["name"]: c["state"] for c in data[0]["checks"]}
    assert names["banned_phrase"] == "fail" and names["invented_specifics"] == "unmeasured"


def test_a_directory_is_refused_rather_than_skipped():
    d = _dir()
    code, out = _run(str(d), "--claims", str(_claims(d)))
    assert code == 2 and "directory" in out, out


def test_role_and_contact_email_reach_the_pdf_checks():
    if not (pdftext._have_pypdf() or pdftext._have_pdftotext()):
        raise unittest.SkipTest("no PDF extractor installed")
    from jobradar import store
    from jobradar.models import Job
    d = _dir()
    db = d / "t.db"
    con = store.connect(db)
    job = Job(company="Acme", title="Engineering Manager", url="https://a.example/1",
              platform="custom", location="London",
              description="Requirements: Kubernetes, Terraform, leadership.")
    store.upsert_roles(con, [job], run=1)
    con.close()
    pdf = make_pdf(d / "CV.pdf", [LINE + " Kubernetes leadership"] * 4)
    code, out = _run(str(pdf), "--claims", str(_claims(d)), "--role", job.uid, "--db", str(db),
                     "--contact-email", "alex@example.com", "--json")
    names = {c["name"]: c for c in json.loads(out)[0]["checks"]}
    assert names["contact"]["state"] == "pass"
    assert names["keyword_coverage"]["detail"] == "2 of 3 covered; not found: Terraform", names


def test_an_unknown_role_stops_the_command():
    d = _dir()
    db = d / "t.db"
    from jobradar import store
    store.connect(db).close()
    code, out = _run(str(_md(d, "x\n")), "--claims", str(_claims(d)), "--role", "nothing-like-it",
                     "--db", str(db))
    assert code == 2 and "--role" in out, out


def test_the_claims_file_can_carry_the_contact_email():
    d = _dir()
    claims, _ = cvcheck.load_claims(_claims(d, "email: alex@example.com\nbanned: []\n"))
    assert claims["email"] == "alex@example.com"


# --------------------------------------------- review finding 5: wrapped lines

def _banned(pattern):
    import re
    return {"path": "claims.local.yaml", "bad_patterns": [],
            "banned": [(re.compile(pattern, re.I), pattern, "FALSE claim")]}


def test_a_banned_claim_wrapped_onto_a_second_line_is_still_found():
    """PDF text wraps at the visual line, so a claim split across two lines is
    the normal case, and it reported PASS."""
    wrapped = "Grew the team so that two engineers now\nrun workstreams on their own, cutting the backlog."
    for pattern in ("two engineers now run workstreams", r"two\s+engineers\s+now\s+run\s+workstreams"):
        c = cvcheck.banned_check(wrapped, _banned(pattern))
        assert c.state == cvcheck.FAIL, (pattern, c)
        assert "line 1" in c.detail and "FALSE claim" in c.detail, c.detail


def test_a_wrapped_hit_reports_the_line_it_starts_on():
    text = "Header\n\nIntro line.\nWe grew so that two engineers\n   now run workstreams.\n"
    c = cvcheck.banned_check(text, _banned("two engineers now run workstreams"))
    assert c.state == cvcheck.FAIL and "line 4" in c.detail, c.detail


def test_a_claim_on_one_line_still_fails_once():
    c = cvcheck.banned_check("Grew the team; two engineers now run workstreams on their own.",
                             _banned("two engineers now run workstreams"))
    assert c.state == cvcheck.FAIL and c.detail.count("line 1") == 1, c.detail
