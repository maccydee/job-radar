"""A role with its full advert stored must not say it has no advert.

Found on 14 September 2026: 215 of 217 LinkedIn roles carried a description
of several thousand characters and were still labelled "listing-only: no
description available from this source". Three faults, one shape:

  * `_rescreen` removed only flags containing "not screened", so the LinkedIn
    adapter's "listing-only" wording and a short read's "barely screened"
    both survived screening against the full text.
  * `_rescreen` ran only when enrichment fetched something new that scan. The
    scan's upsert rewrites flags from a fresh parse, and a LinkedIn card parses
    with an empty description, so a role enriched on an earlier run got the
    stale label back every time and nothing put it right.
  * The dashboard's caveat filter looked for "listing only" with a space,
    which the adapter never writes, so the one role that genuinely had no
    advert showed no note either.

A label that says "not screened" on a role that was screened sends the reader
to open an advert the tool has already read, and hides whether its
dealbreakers were ever checked.
"""

import json
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, store  # noqa: E402
from jobradar.config import load  # noqa: E402
from jobradar.output import interactive  # noqa: E402

LISTING = "listing-only: no description available from this source"
BARELY = "barely screened: this source gave 120 characters of advert, too little to check properly"
DESC = "Lead a team of engineers building our platform. " * 10


def _cfg():
    d = Path(tempfile.mkdtemp())
    (d / "cv.txt").write_text("Callum. EM.", encoding="utf-8")
    p = d / "c.yaml"
    p.write_text(textwrap.dedent(f"""
        titles:
          include: [engineering manager]
        locations:
          countries: [UK]
        cv:
          path: {d / 'cv.txt'}
        salary:
          floor: 90000
          currency: GBP
        dealbreakers:
          - name: coding round
            pattern: 'take.?home'
            hard: true
    """), encoding="utf-8")
    return load(p)


def _role(con, uid, flags, description=DESC):
    con.execute(
        "INSERT INTO roles (uid,company,title,url,location,platform,description,"
        "score,reasons,flags,first_seen,last_seen) VALUES (?,'Acme',"
        "'Engineering Manager',?,'London','linkedin',?,70,'[]',?,"
        "'2026-09-14','2026-09-14')",
        (uid, f"https://example.invalid/{uid}", description, json.dumps(flags)))
    con.execute("INSERT INTO role_state (uid,status,updated_at) "
                "VALUES (?,'new','2026-09-14')", (uid,))


def _flags(con, uid):
    return json.loads(con.execute("SELECT flags FROM roles WHERE uid=?",
                                  (uid,)).fetchone()["flags"])


def test_rescreen_drops_every_missing_text_wording_and_keeps_the_rest():
    con = store.connect(":memory:")
    _role(con, "li", [LISTING, "contract or interim (12 month FTC)",
                      "unconfirmed salary"])
    _role(con, "short", [BARELY, "unconfirmed salary"])
    assert cli._rescreen(con, _cfg()) == 0
    li, short = _flags(con, "li"), _flags(con, "short")
    assert not any("listing-only" in f for f in li), li
    assert not any("barely screened" in f for f in short), short
    assert "contract or interim (12 month FTC)" in li
    assert "unconfirmed salary" in li and "unconfirmed salary" in short


def test_a_scan_with_nothing_to_fetch_still_corrects_the_labels():
    """The upsert has just rewritten the flag; nothing new was enriched."""
    con = store.connect(":memory:")
    _role(con, "li", [LISTING, "unconfirmed salary"])
    with mock.patch("jobradar.enrich.candidates", lambda *a, **k: []):
        cli._enrich_step(con, _cfg())
    assert not any("listing-only" in f for f in _flags(con, "li"))


def test_a_role_with_no_advert_still_shows_the_note_on_the_dashboard():
    con = store.connect(":memory:")
    store.migrate(con, state_path=str(Path(tempfile.mkdtemp()) / "none.json"))
    _role(con, "bare", [LISTING], description="")
    page = interactive.render(con)
    assert f'class="note">{LISTING}' in page, \
        "a genuinely listing-only role has to say so where the buttons are"
