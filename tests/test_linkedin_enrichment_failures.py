"""A failed LinkedIn fetch has to say why, and must not read as "no advert".

Found on 17 Sept 2026: a scan logged "fetching 88 postings that arrived as
headlines only... filled in 64 of 88" and said nothing about the other 24.
`enrich.fetch` (the LinkedIn per-posting reader) turned every failure -- a
network error, a non-200, a 200 that did not match the description regex --
into a bare `""`, which is exactly what a genuinely empty response looks
like. Ten of those 24 were still in the database with empty descriptions,
carrying the flags `parse_linkedin` and `screen.screen` write against an
EMPTY search result: "listing-only: no description available from this
source" and "not screened: no description from this source". Both are true
of the search card. Neither is true of the role: fetched by hand the same
day with the code's own User-Agent, two of the stuck ids (4465981744,
4467252999) answered HTTP 200 with 34KB and 75KB bodies. The source has a
description; the run just did not read it, and the stored label said the
opposite.

`tests/fixtures/linkedin_job_posting.html` is one of those two ids, trimmed:
a real 200 response with everything outside the description container
removed. No request in here reaches the network; the session is a stub, the
same shape `tests/test_reed_details_enrichment.py` uses.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from jobradar import cli, enrich, store
from jobradar.config import Config

FIXTURES = Path(__file__).parent / "fixtures"
POSTING_HTML = (FIXTURES / "linkedin_job_posting.html").read_text(encoding="utf-8")

# Real ids from the stuck roles, 17 Sept 2026 database.
STUCK_ID = "4465981744"          # AI Adoption Lead at Robert Half
STUCK_URL = ("https://uk.linkedin.com/jobs/view/"
             f"ai-adoption-lead-at-robert-half-{STUCK_ID}")

LISTING = "listing-only: no description available from this source"
NOT_SCREENED = "not screened: no description from this source"


class _Response:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _Session:
    """Stands in for requests.Session. Answers per job id, and records every
    call so a test can assert what was and was not asked for."""

    calls: list = []
    # job_id -> (status, text) | "raise"
    answers: dict = {}

    def get(self, url, headers=None, timeout=None):
        _Session.calls.append(url)
        jid = url.rstrip("/").rsplit("/", 1)[-1]
        ans = _Session.answers.get(jid, (200, ""))
        if ans == "raise":
            raise requests.ConnectionError("Connection reset by peer")
        status, text = ans
        return _Response(status, text)

    def close(self):
        pass


def _db():
    con = store.connect(Path(tempfile.mkdtemp()) / "t.db")
    return con


def _role(con, uid, url, flags, platform="linkedin", description=""):
    con.execute(
        "INSERT INTO roles (uid,company,title,url,location,platform,description,"
        "score,reasons,flags,first_seen,last_seen) VALUES (?,'Robert Half',"
        "'AI Adoption Lead',?,'London','" + platform + "',?,70,'[]',?,"
        "'2026-09-17','2026-09-17')",
        (uid, url, description, json.dumps(flags)))
    con.execute("INSERT INTO role_state (uid,status,updated_at) "
                "VALUES (?,'new','2026-09-17')", (uid,))
    con.commit()


def _row(con, uid):
    return con.execute("SELECT * FROM roles WHERE uid=?", (uid,)).fetchone()


def _flags(con, uid):
    return json.loads(_row(con, uid)["flags"])


def _enrich(con, cfg=None, answers=None):
    _Session.calls, _Session.answers = [], answers or {}
    real = requests.Session
    requests.Session = _Session
    try:
        notes: list = []
        got, tried = enrich.run(con, cfg or Config(), pause=0,
                                concurrency=1, notes=notes)
    finally:
        requests.Session = real
    return got, tried, notes


def test_the_fixture_is_a_real_page_the_regex_actually_matches():
    # If this fails the fixture stopped being what it claims to be, and every
    # test below would be proving nothing.
    assert "description__text" in POSTING_HTML
    text = enrich._text(POSTING_HTML)
    assert len(text) > 1000
    assert "AI Adoption Lead" in text
    # Written as an escape so this file does not itself contain the
    # character it is here to keep out, the same way test_loose_ends.py does.
    assert "\u2014" not in text


def test_a_stuck_linkedin_role_is_queued_for_a_fetch():
    con = _db()
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, "unconfirmed salary",
                                              NOT_SCREENED])
    queued = {r["uid"] for r in enrich.candidates(con)}
    assert "a9e2c8b0a3754079" in queued


def test_a_failed_fetch_is_counted_by_reason_and_reported():
    """This is the 24-of-88 bug. A 429 used to vanish into a bare `""` and
    the run's own notes said nothing; now it is counted and printed, the same
    way a refused Reed key already was."""
    con = _db()
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, "unconfirmed salary",
                                              NOT_SCREENED])
    got, tried, notes = _enrich(
        con, answers={STUCK_ID: (429, "")})
    assert (got, tried) == (0, 1)
    assert any("linkedin" in n and "HTTP 429" in n for n in notes), notes


def test_a_failed_fetch_relabels_could_not_fetch_instead_of_no_description():
    """The bug this fixes, precisely: the source has a description (checked
    live, 17 Sept 2026: HTTP 200, 56KB) and the stored flags said it did
    not."""
    con = _db()
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, "unconfirmed salary",
                                              NOT_SCREENED])
    _enrich(con, answers={STUCK_ID: (429, "")})

    flags = _flags(con, "a9e2c8b0a3754079")
    assert not any("listing-only" in f for f in flags), flags
    assert not any("not screened" in f for f in flags), flags
    assert any("could not fetch" in f and "HTTP 429" in f for f in flags), flags
    # Nothing else on the role gets thrown away.
    assert "unconfirmed salary" in flags

    # And the role is still queued: nothing here excludes it permanently.
    assert "a9e2c8b0a3754079" in {r["uid"] for r in enrich.candidates(con)}


def test_a_network_error_is_recorded_by_type_not_swallowed():
    con = _db()
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, NOT_SCREENED])
    got, tried, notes = _enrich(con, answers={STUCK_ID: "raise"})
    assert got == 0
    assert any("linkedin" in n and "ConnectionError" in n for n in notes), notes
    flags = _flags(con, "a9e2c8b0a3754079")
    assert any("could not fetch" in f and "ConnectionError" in f
              for f in flags), flags


def test_a_200_with_no_description_block_is_reported_not_silently_empty():
    """The other half of the old bug: a 200 that does not match `_BLOCK` --
    a removed listing, or LinkedIn having changed the page shape -- used to
    look exactly like a network failure and exactly like success. Now it
    says which of those it was."""
    con = _db()
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, NOT_SCREENED])
    got, tried, notes = _enrich(
        con, answers={STUCK_ID: (200,
                                 "<html><body><h1>This job is no longer "
                                 "accepting applications</h1></body></html>")})
    assert got == 0
    assert any("linkedin" in n and "no description block" in n
              for n in notes), notes


def test_a_successful_fetch_fills_the_description_and_clears_the_old_flags():
    con = _db()
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, "unconfirmed salary",
                                              NOT_SCREENED])
    got, tried, notes = _enrich(con, answers={STUCK_ID: (200, POSTING_HTML)})
    assert (got, tried, notes) == (1, 1, [])

    row = _row(con, "a9e2c8b0a3754079")
    assert len(row["description"]) > 1000
    assert "AI Adoption Lead" in row["description"]

    # `_rescreen` is what a real scan or `job-radar enrich` runs next; it has
    # to recognise the stale flags this module wrote (both the original
    # "listing-only" wording and, on a role retried after a prior failed
    # run, this module's own "could not fetch") and drop them now that the
    # advert is long enough to have been screened for real.
    cli._rescreen(con, Config())
    flags = _flags(con, "a9e2c8b0a3754079")
    assert not any("listing-only" in f for f in flags), flags
    assert not any("not screened" in f for f in flags), flags
    assert not any("could not fetch" in f for f in flags), flags


def test_a_retry_after_a_failure_recovers_cleanly():
    """The retry story end to end: a role that failed once, stayed queued,
    and succeeded on the very next `enrich.run` -- which is what an
    unpaced day and a calm one look like back to back, with nothing in
    between marking the role as permanently dead."""
    con = _db()
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, NOT_SCREENED])

    got1, _t, notes1 = _enrich(con, answers={STUCK_ID: (429, "")})
    assert got1 == 0
    assert any("could not fetch" in f for f in _flags(con, "a9e2c8b0a3754079"))

    got2, _t, notes2 = _enrich(con, answers={STUCK_ID: (200, POSTING_HTML)})
    assert got2 == 1
    assert notes2 == []
    row = _row(con, "a9e2c8b0a3754079")
    assert len(row["description"]) > 1000

    cli._rescreen(con, Config())
    flags = _flags(con, "a9e2c8b0a3754079")
    assert not any(cli._about_missing_text(f) for f in flags), flags


def test_a_role_rejected_by_rescreen_still_gets_its_stale_flag_corrected():
    """Found running the real fix against the 17 Sept database: 2 of the 10
    stuck roles filled in a description that then failed a hard dealbreaker,
    `_rescreen` correctly closed them -- and then `continue`d straight past
    the flag rewrite below, because that line only ran on the `keep` branch.
    Both stayed in the database reading "not screened: no description from
    this source" while `role_state.note` said "hidden after its full
    description was read", which is a role claiming in one column that it
    was never checked and in the next that it was checked and failed.
    """
    from jobradar.config import Dealbreaker

    con = _db()
    desc = ("Lead a team of engineers building our platform. " * 10
            + "Please note fully remote work is not offered for this role.")
    _role(con, "a9e2c8b0a3754079", STUCK_URL, [LISTING, NOT_SCREENED],
          description=desc)
    cfg = Config(dealbreakers=[Dealbreaker("no-remote",
                                           r"fully remote work is not offered")])

    dropped = cli._rescreen(con, cfg)
    assert dropped == 1
    status = con.execute("SELECT status FROM role_state WHERE uid=?",
                         ("a9e2c8b0a3754079",)).fetchone()["status"]
    assert status == "closed"

    flags = _flags(con, "a9e2c8b0a3754079")
    assert not any(cli._about_missing_text(f) for f in flags), flags


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception:
                failed += 1
                print("FAIL", name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
