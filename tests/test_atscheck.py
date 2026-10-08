import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from jobradar import atscheck, cvcheck, pdftext    # noqa: E402
from pdfmaker import make_pdf                      # noqa: E402

GLYPH = chr(0xF0E0)           # an icon-font envelope: private use, renders as a picture
LINE = "Alex Morgan alex@example.com 07700 900123 Engineering Manager who leads platform teams"


def _by(checks):
    return {c.name: c for c in checks}


def test_a_literal_email_in_the_text_layer_is_found():
    ok, detail = atscheck.contact_check(LINE * 3, "alex@example.com")
    assert ok is True and "literal text" in detail


def test_an_email_replaced_by_an_icon_glyph_is_not_found():
    ok, detail = atscheck.contact_check(f"{GLYPH} alex  {GLYPH} 07700 900123", "alex@example.com")
    assert ok is False and detail == "email not found as literal text"


def test_the_match_ignores_case_but_not_the_address():
    assert atscheck.contact_check("Alex@Example.com", "alex@example.com")[0] is True
    assert atscheck.contact_check("alex@example.co", "alex@example.com")[0] is False


def test_no_email_given_is_not_a_pass():
    ok, detail = atscheck.contact_check(LINE, None)
    assert ok is False and "no --contact-email" in detail


def test_garbled_text_is_caught_and_clean_text_is_not():
    bad, detail = atscheck.garbled((GLYPH * 20 + "ab") * 10)
    assert bad is True and "private-use" in detail
    assert atscheck.garbled(LINE * 5)[0] is False
    assert atscheck.garbled("")[0] is True            # no text is not clean text


def test_garbled_threshold_is_eight_percent():
    clean = "a" * 92
    assert atscheck.garbled(clean + GLYPH * 7)[0] is False
    assert atscheck.garbled(clean + GLYPH * 9)[0] is True


def test_a_replacement_character_counts_as_garbled():
    assert atscheck.garbled("a" * 50 + chr(0xFFFD) * 50)[0] is True


def test_keyword_coverage_reports_the_gap_and_does_not_stuff_it():
    covered, missing = atscheck.keyword_coverage(
        "I run Kubernetes clusters and lead hiring for the team.",
        "Requirements: Kubernetes, Terraform, hiring.")
    assert sorted(covered) == ["Kubernetes", "hiring"] and missing == ["Terraform"]


def test_a_term_must_match_as_a_whole_word():
    covered, missing = atscheck.keyword_coverage("I did some cooking", "We use Go and Rust.")
    assert "Go" not in covered and "Rust" in missing


def test_boilerplate_is_not_a_keyword():
    terms = atscheck.jd_terms("You will have strong experience working with our team. Kubernetes.")
    assert "Kubernetes" in terms and "experience" not in [t.lower() for t in terms]


def test_distinctive_terms_come_first_in_a_long_posting():
    jd = ("Platform role. " + "We value collaboration and delivery. " * 6
          + "You know Terraform and AWS. " + "Planning matters. " * 6)
    terms = atscheck.jd_terms(jd, limit=3)
    assert "Terraform" in terms and "AWS" in terms, terms


def test_symbols_in_a_tech_name_survive():
    terms = atscheck.jd_terms("Experience with C++, C#, Node.js and CI/CD.")
    assert {"C++", "C#", "Node.js"} <= set(terms), terms


# --------------------------------------------------------- inside cvcheck

def _pdf_checks(lines, **kw):
    d = Path(tempfile.mkdtemp())
    pdf = make_pdf(d / "CV.pdf", lines)
    return _by(cvcheck.check_file(pdf, claims=None, master_text=None, author_name=None,
                                  max_pages=None, **kw))


def _needs_extractor():
    if not (pdftext._have_pypdf() or pdftext._have_pdftotext()):
        raise unittest.SkipTest("no PDF extractor installed")


def test_a_pdf_gets_the_contact_and_glyph_and_keyword_checks():
    _needs_extractor()
    c = _pdf_checks([LINE] * 4, contact_email="alex@example.com",
                    jd_text="Requirements: Kubernetes, Terraform, hiring.")
    assert c["contact"].state == cvcheck.PASS
    assert c["garbled_glyphs"].state == cvcheck.PASS
    kc = c["keyword_coverage"]
    assert kc.state == cvcheck.PASS and "0 of 3 covered" in kc.detail and "Kubernetes" in kc.detail, kc


def test_a_gap_in_keywords_is_information_not_a_failure():
    _needs_extractor()
    c = _pdf_checks([LINE + " Kubernetes hiring"] * 4,
                    jd_text="Requirements: Kubernetes, Terraform, hiring.")
    assert c["keyword_coverage"].state == cvcheck.PASS
    assert c["keyword_coverage"].detail == "2 of 3 covered; not found: Terraform"


def test_no_job_or_no_email_is_unmeasured_not_a_pass():
    _needs_extractor()
    c = _pdf_checks([LINE] * 4)
    assert c["contact"].state == cvcheck.UNMEASURED
    assert c["keyword_coverage"].state == cvcheck.UNMEASURED
    c = _pdf_checks([LINE] * 4, jd_text="   ")
    assert c["keyword_coverage"].state == cvcheck.UNMEASURED and "no description" in c["keyword_coverage"].detail


def test_a_pdf_whose_text_layer_has_no_email_fails_contact():
    _needs_extractor()
    # Helvetica cannot draw a private-use character, so the PDF carries the
    # visible text "alex" and the address never appears as text.
    c = _pdf_checks([f"Alex Morgan  alex  07700 900123 Engineering Manager who leads teams"] * 4,
                    contact_email="alex@example.com")
    assert c["contact"].state == cvcheck.FAIL and "not found as literal text" in c["contact"].detail


def test_when_the_text_cannot_be_read_all_three_are_unmeasured_with_the_reason():
    d = Path(tempfile.mkdtemp())
    pdf = make_pdf(d / "CV.pdf", [LINE] * 4)
    real = pdftext._have_pypdf, pdftext._have_pdftotext
    pdftext._have_pypdf = pdftext._have_pdftotext = lambda: False
    try:
        c = _by(cvcheck.check_file(pdf, claims=None, master_text=None, author_name=None,
                                   max_pages=None, contact_email="alex@example.com",
                                   jd_text="Kubernetes"))
    finally:
        pdftext._have_pypdf, pdftext._have_pdftotext = real
    for name in ("garbled_glyphs", "contact", "keyword_coverage"):
        assert c[name].state == cvcheck.UNMEASURED, (name, c[name])
        assert "neither pypdf nor pdftotext" in c[name].detail
