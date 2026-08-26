"""Three faults that reported success, or nothing at all, while losing work.

Each one is reproduced here as the input that triggers it, before it is
guarded:

  * the setup wizard builds a stand-in for the `scan` argument namespace by
    hand. `--no-enrich` was added to the real parser and not to the stand-in,
    so the first scan a new user ever runs died on `AttributeError` after the
    fetch and before the output, and the wizard's catch-all printed a
    truncated apology with no clue in it;
  * every network exception `fetch_one` cannot classify is reduced to its
    class name, so DNS failure, connection refused, connection reset and a
    mid-stream abort all reach `validate` as the single word
    "ConnectionError". A run reporting seven thousand of them says nothing
    about whether the boards are gone or the network blinked;
  * `rank` reads a CV that is not a `.docx` as UTF-8 text with errors
    ignored. A PDF -- the format a CV is most likely to be in -- survives
    that as thousands of characters of file structure, walks past the guard
    that exists to catch an unreadable CV, and is sent to the model as the
    document every score is judged against.

Nothing here touches the network or the `claude` binary: the fetch case
raises a real exception from a stubbed session, and the CV cases read files
written into a temporary directory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, rank as rank_mod, setup_wizard   # noqa: E402


class _Tmp:
    """A throwaway directory that cleans itself up."""

    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return Path(self._d.name)

    def __exit__(self, *exc):
        self._d.cleanup()
        return False


# --------------------------------------------------------------------------
# 1. The wizard's stand-in namespace must satisfy every attribute cmd_scan
#    reads, or the first scan dies after doing all of the work.
# --------------------------------------------------------------------------

def test_the_wizard_passes_every_argument_the_scan_actually_reads():
    """The stand-in is built by hand, so it goes stale silently.

    Asserting on the union of attributes `cmd_scan` reads, rather than on
    `no_enrich` alone, is the point: the next flag added to the parser and
    forgotten here fails this test instead of failing a stranger's first run.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cli.cmd_scan))
    fn = tree.body[0]
    arg_name = fn.args.args[0].arg
    reads = {
        node.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == arg_name
    }
    assert reads, "could not find any argument reads in cmd_scan"

    src = inspect.getsource(setup_wizard)
    start = src.index("class _Args:")
    end = src.index("cli.cmd_scan", start)
    stand_in = src[start:end]
    provided = {
        line.split("=")[0].strip()
        for line in stand_in.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }

    missing = sorted(reads - provided)
    assert not missing, (
        f"cmd_scan reads {missing} which the wizard's _Args does not set. "
        f"The first scan a new user runs will die on AttributeError.")


# --------------------------------------------------------------------------
# 2. A transport failure must carry the reason it failed.
# --------------------------------------------------------------------------

def test_a_connection_failure_says_what_actually_went_wrong():
    """"ConnectionError" alone cannot be acted on.

    A name resolution failure and a refused connection are the difference
    between "this board is gone" and "your network blinked", and `validate`
    prints this string thousands of times in a run.
    """
    import requests

    from jobradar import fetch
    from jobradar.models import Source

    cause = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='boards.example.test', port=443): "
        "Max retries exceeded (Caused by NewConnectionError("
        "'<urllib3.connection.HTTPSConnection object at 0x1>: "
        "Failed to establish a new connection: "
        "[Errno 61] Connection refused'))")

    class _Boom:
        auth = None

        def get(self, *a, **k):
            raise cause

        def post(self, *a, **k):
            raise cause

    src = Source(company="Example", url="https://boards.example.test/jobs",
                 platform="greenhouse")
    res = fetch.fetch_one(src, timeout=1, retries=0, user_agent="test",
                          session=_Boom())

    assert not res.ok, "the stubbed session raised, so this cannot be ok"
    assert res.error, "a failed fetch must carry an error"
    assert "Connection refused" in res.error or "Errno 61" in res.error, (
        f"the reason the connection failed was discarded; error was "
        f"{res.error!r}. A caller cannot tell this from a DNS failure.")


# --------------------------------------------------------------------------
# 3. A CV the tool cannot actually read must be refused, not sent.
# --------------------------------------------------------------------------

# The opening bytes of a real single-page PDF, which is what `cv.path`
# points at for anyone who exported their CV from Word or Google Docs.
_PDF_HEAD = (
    b"%PDF-1.4\n%\xc7\xec\x8f\xa2\n"
    b"1 0 obj\n<</Title (Resume) /Producer (Skia/PDF m153 "
    b"Google Docs Renderer)>>\nendobj\n"
    b"3 0 obj\n<</ca 1 /BM /Normal>>\nendobj\n"
    b"9 0 obj\n<</Filter /FlateDecode /Length 19422>>\nstream\n"
)


def _pdf_bytes() -> bytes:
    """Enough of a PDF to clear any length guard, with the NUL bytes and
    binary a Flate stream really carries."""
    filler = bytes(range(256)) * 40
    return _PDF_HEAD + filler + b"\nendstream\nendobj\n%%EOF\n"


class _Cfg:
    def __init__(self, path):
        self.cv_path = str(path)


def test_a_pdf_cv_is_not_passed_off_as_its_own_text():
    """Read as UTF-8 with errors ignored, a PDF yields thousands of
    characters of file structure that clear the length guard.

    Either the text is really extracted, or this is refused. What must not
    happen is scoring a job search against `%PDF-1.4 ... /FlateDecode`.
    """
    with _Tmp() as tmp:
        cv = tmp / "resume.pdf"
        cv.write_bytes(_pdf_bytes())

        try:
            text = rank_mod._cv_text(_Cfg(cv))
        except SystemExit as e:
            msg = str(e)
            assert "pdf" in msg.lower(), (
                f"refusing is a fine answer, but the message must say the "
                f"format is the problem; got {msg!r}")
            return

        assert "%PDF" not in text, (
            "the PDF header was sent to the model as the candidate's CV")
        assert "FlateDecode" not in text and "endobj" not in text, (
            "PDF file structure was sent to the model as the candidate's CV")
        assert "\x00" not in text, (
            "a NUL byte reached the prompt; subprocess refuses to exec this, "
            "so every ranking call dies on ValueError: embedded null byte")


def test_a_cv_that_is_only_binary_is_refused_rather_than_scored():
    """The length guard measures characters, not whether any of them are the
    candidate's career."""
    with _Tmp() as tmp:
        cv = tmp / "resume.pdf"
        cv.write_bytes(_pdf_bytes())
        try:
            text = rank_mod._cv_text(_Cfg(cv))
        except SystemExit:
            return
        letters = sum(ch.isalpha() or ch.isspace() for ch in text)
        assert letters / max(1, len(text)) > 0.8, (
            f"only {letters}/{len(text)} characters are text; this is a "
            f"binary file being scored as a CV")
