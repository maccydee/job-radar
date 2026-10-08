import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from jobradar import pdftext                  # noqa: E402
from pdfmaker import make_pdf                 # noqa: E402

LINE = "Alex Morgan alex@example.com 07700 900123 Engineering Manager"


def _pdf(**kw):
    return make_pdf(Path(tempfile.mkdtemp()) / "cv.pdf", [LINE] * 4, **kw)


def _skip_without_an_extractor():
    if not (pdftext._have_pypdf() or pdftext._have_pdftotext()):
        raise unittest.SkipTest("no PDF extractor installed")


def test_the_text_of_a_real_pdf_comes_back_readable():
    _skip_without_an_extractor()
    text, how = pdftext.extract(_pdf())
    assert text is not None and "alex@example.com" in text, (text, how)
    assert "\x0c" not in text


def test_each_extractor_reads_the_same_pdf_on_its_own():
    p = _pdf()
    real = pdftext._have_pypdf, pdftext._have_pdftotext
    # Called, not just read: these are functions, so `if real[0]` was true on
    # a machine with neither tool and the test then failed on a None result.
    have = real[0](), real[1]()
    try:
        if have[0]:
            pdftext._have_pdftotext = lambda: False
            text, how = pdftext.extract(p)
            assert how == "pypdf" and "alex@example.com" in text, (text, how)
        if have[1]:
            pdftext._have_pdftotext = real[1]
            pdftext._have_pypdf = lambda: False
            text, how = pdftext.extract(p)
            assert how == "pdftotext" and "alex@example.com" in text, (text, how)
    finally:
        pdftext._have_pypdf, pdftext._have_pdftotext = real
    if not any(have):
        raise unittest.SkipTest("no PDF extractor installed")


def test_with_no_extractor_the_answer_is_none_and_names_both_tools():
    real = pdftext._have_pypdf, pdftext._have_pdftotext
    pdftext._have_pypdf = pdftext._have_pdftotext = lambda: False
    try:
        text, why = pdftext.extract(_pdf())
        assert text is None
        assert "neither pypdf nor pdftotext is installed" in why, why
        assert pdftext.pages(_pdf())[0] is None
        assert pdftext.author(_pdf())[0] is None
    finally:
        pdftext._have_pypdf, pdftext._have_pdftotext = real


def test_a_file_that_is_not_a_pdf_is_a_finding_not_an_empty_text():
    _skip_without_an_extractor()
    bad = Path(tempfile.mkdtemp()) / "cv.pdf"
    bad.write_bytes(b"this is not a pdf at all " * 20)
    text, why = pdftext.extract(bad)
    assert text is None and "cv.pdf" in why, (text, why)


def test_page_count_and_author_come_from_the_pdf():
    if not pdftext._have_pypdf():
        raise unittest.SkipTest("pypdf not installed")
    p = _pdf(pages=3, author="Alex Morgan")
    assert pdftext.pages(p) == (3, "pypdf")
    assert pdftext.author(p) == ("Alex Morgan", "pypdf")
    assert pdftext.author(_pdf()) == ("", "pypdf")      # no author is "", not None
