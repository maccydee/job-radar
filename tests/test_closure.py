"""Nothing ever marked a role closed from absence, and the obvious fix is wrong.

213 rows on the maintainer's own database sat at `closed` because somebody had
set them by hand, while 1,372 roles nobody has acted on had not been seen for
over a month and still rendered as open. `roles.last_seen` was the only
staleness signal in the tool and no code anywhere read it to decide anything.

The naive fix -- "anything missing from the latest scan is closed" -- is this
repository's signature bug with a new hat on. A board that 403s, times out,
answers 429, or changes the shape of its JSON drops every one of its postings
out of the scan, and a sweep over absence would report "47 roles closed" in a
clean confident sentence with nothing true in it. `deadwood.py` already makes
exactly this distinction one layer out, for source deletion, and the reasoning
is identical here.

So the invariant these tests exist to hold: **a role may only be closed on the
evidence of a successful read of the source it came from.** Absence from a run
where that source failed, was skipped, was throttled, was cut off at a page
cap, or was never attempted is not evidence of anything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import closure, store              # noqa: E402
from jobradar.models import Job                  # noqa: E402

BOARD = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
OTHER = "https://api.ashbyhq.com/posting-api/job-board/other"


def _job(n, source=BOARD, company="Acme"):
    return Job(company=company, title=f"Engineering Manager {n}",
               url=f"https://boards.greenhouse.io/acme/jobs/{n}",
               platform="greenhouse", location="London", source_id=source)


FRESH = "https://api.ashbyhq.com/posting-api/job-board/fresh"


def _sentinel():
    """A role from this morning, on a source of its own.

    Every test needs one, because "stale" is relative to the newest reading in
    the table -- the same way `store.LIVE_SQL` and `store.NEW_SQL` define
    recency -- and a database holding one role from three weeks ago has that
    role AS its newest reading.
    """
    return _job(99, FRESH, company="Fresh")


def _db(roles=(BOARD,), last_seen="2026-09-01"):
    """A store holding one role per source key, last seen on `last_seen`."""
    con = store.connect(":memory:")
    jobs = [_job(i, src) for i, src in enumerate(roles, start=1)]
    store.upsert_roles(con, jobs, run=1)
    con.execute("UPDATE roles SET last_seen=?", (last_seen,))
    store.upsert_roles(con, [_sentinel()], run=1)   # last_seen is today
    return con, jobs


def _read(con, source=BOARD, *, when, ok=True, roles=4, run=2, **kw):
    store.record_source_read(con, source, run=run, ok=ok, roles=roles,
                             when=when, **kw)


# ------------------------------------------------------------ the invariant

def test_a_source_that_failed_cannot_close_its_roles():
    """The whole point. Two runs where the board refused is not absence."""
    con, (job,) = _db()
    _read(con, when="2026-09-20", ok=False, roles=0, why="HTTP 403")
    _read(con, when="2026-09-21", ok=False, roles=0, why="timeout")
    assert closure.candidates(con) == []
    assert closure.close_absent(con)["closed"] == []
    assert store.status_of(con, job.uid) == "new"


def test_a_200_that_could_not_be_parsed_is_a_failure_not_a_success():
    """A board answering 200 with a holding page is unread, not empty.

    `adapters.parse` returns `[]` for a payload its platform's parser raises
    on, which is right -- one malformed board must not end a run over 17,810
    sources -- and that empty list used to be indistinguishable at the call
    site from an employer with no vacancies. Closure cannot be built on a
    signal with that hole in it.
    """
    con, (job,) = _db()
    from jobradar import adapters
    from jobradar.models import Source
    src = Source(company="Acme", url=BOARD, platform="greenhouse")
    jobs, why = adapters.parse_or_why("<html>maintenance</html>", src)
    # Cleared, because `_unreadable` is a module-level accumulator and a test
    # that deliberately feeds the parser a bad payload leaves a row in it.
    # `test_empty_boards` asserts that list is empty, and left dirty this test
    # failed that one from two files away -- which is itself the shape this
    # suite keeps catching: a failure appearing somewhere other than its
    # cause.
    adapters.clear_unreadable()
    assert jobs == []
    assert why, "a payload the parser raised on must say so"

    _read(con, when="2026-09-20", ok=False, roles=0, why=why)
    _read(con, when="2026-09-21", ok=False, roles=0, why=why)
    assert closure.candidates(con) == []
    assert store.status_of(con, job.uid) == "new"


def test_one_successful_read_is_not_enough_and_n_of_them_is():
    con, (job,) = _db()
    _read(con, when="2026-09-20")
    assert closure.candidates(con) == [], "one read is one reading"
    assert store.status_of(con, job.uid) == "new"

    _read(con, when="2026-09-21", run=3)
    got = closure.candidates(con)
    assert [c["uid"] for c in got] == [job.uid], got
    assert got[0]["confirmations"] == closure.MIN_CONFIRMATIONS
    assert closure.close_absent(con)["closed"] == [job.uid]
    assert store.status_of(con, job.uid) == "closed"


def test_two_reads_on_the_same_day_are_one_day_of_evidence():
    """`deadwood.MIN_RUNS` has the same guard and for the same reason: a
    maintainer re-running a command to debug something must not march rows
    towards a terminal state on one afternoon's readings."""
    con, (job,) = _db()
    _read(con, when="2026-09-20", run=2)
    _read(con, when="2026-09-20", run=3)
    assert closure.candidates(con) == []
    assert store.status_of(con, job.uid) == "new"


def test_a_role_with_no_source_key_is_never_closed():
    """Rows imported by `migrate` and by `seed load` cannot be attributed to
    a source, so nothing can be concluded from their absence from one.

    The read rows here are inserted by hand, with an empty `source_key`, and
    that is the whole point of the test. Reverting the guard it covers changed
    nothing while the rows were written through `record_source_read`: an
    unattributed role joins no read, so it was being protected by a join that
    happened to miss rather than by a rule. A test that passes against the
    broken code is a claim of coverage that is not there, so it is written
    against the one shape that can actually reach the guard.
    """
    con, _ = _db()
    con.execute("UPDATE roles SET source_key=''")
    for day in ("2026-09-20", "2026-09-21"):
        con.execute("INSERT INTO source_reads (source_key,run,read_on,ok,"
                    "roles,complete,keyword) VALUES ('',2,?,1,4,1,0)", (day,))
    assert closure.candidates(con) == []
    rows = con.execute("SELECT uid FROM roles").fetchall()
    assert {store.status_of(con, r["uid"]) for r in rows} == {"new"}


def test_an_unattributed_read_is_not_recorded_at_all():
    """The first line of the same defence. A read nothing can attribute to a
    source is not a reading of anything."""
    con, _ = _db()
    store.record_source_read(con, "", run=2, ok=True, roles=4,
                             when="2026-09-20")
    assert con.execute("SELECT COUNT(*) c FROM source_reads"
                       ).fetchone()["c"] == 0


def test_a_read_of_a_different_source_closes_nothing():
    con, (job,) = _db()
    _read(con, OTHER, when="2026-09-20")
    _read(con, OTHER, when="2026-09-21", run=3)
    assert closure.candidates(con) == []
    assert store.status_of(con, job.uid) == "new"


def test_a_human_set_status_is_never_overwritten():
    """The database is the record of what you did about a role. A scan does
    not get to overrule it."""
    for status in ("applied", "submitted", "interviewing", "offer",
                   "rejected", "withdrawn", "skipped"):
        con, (job,) = _db()
        store.set_status(con, job.uid, status, note="mine")
        _read(con, when="2026-09-20")
        _read(con, when="2026-09-21", run=3)
        assert closure.candidates(con) == [], status
        assert closure.close_absent(con)["closed"] == [], status
        assert store.status_of(con, job.uid) == status
        row = con.execute("SELECT note FROM role_state WHERE uid=?",
                          (job.uid,)).fetchone()
        assert row["note"] == "mine", status


def test_interested_is_closed_and_the_note_says_what_closed_it():
    """`interested` is the reader saying they want the role, not a record of
    anything that passed between them and the employer. A vacancy they were
    interested in having been taken down is precisely the thing they need
    told, so it closes -- and it is counted separately in the report, because
    it is the one auto-closed status somebody chose by hand."""
    con, (job,) = _db()
    store.set_status(con, job.uid, "interested")
    _read(con, when="2026-09-20")
    _read(con, when="2026-09-21", run=3)
    out = closure.close_absent(con)
    assert out["closed"] == [job.uid]
    assert out["was_interested"] == [job.uid]
    assert store.status_of(con, job.uid) == "closed"
    note = con.execute("SELECT note FROM role_state WHERE uid=?",
                       (job.uid,)).fetchone()["note"]
    assert "2" in note and "2026-09-21" in note, note


def test_a_read_from_before_the_role_was_last_seen_is_not_evidence():
    con, (job,) = _db(last_seen="2026-09-25")
    _read(con, when="2026-09-20")
    _read(con, when="2026-09-21", run=3)
    assert closure.candidates(con) == []
    # The read on the day it was last seen is, or may be, the read that saw
    # it, so it is one short rather than enough. Written as the boundary
    # deliberately: with the comparison at `>=` instead of `>` these two
    # readings are two confirmations and the role closes on the strength of
    # the scan that found it.
    _read(con, when="2026-09-25", run=4)
    _read(con, when="2026-09-26", run=5)
    got = closure.candidates(con)
    assert got == [], got
    # One more day and it is evidence.
    _read(con, when="2026-09-27", run=6)
    assert [c["uid"] for c in closure.candidates(con)] == [job.uid]


def test_a_read_cut_off_at_the_page_cap_is_not_evidence():
    """A pager that stopped because it ran out of allowance has not seen the
    whole board, so it cannot say a posting is not on it. `Result.truncated`
    is the fetcher saying exactly that."""
    con, (job,) = _db()
    _read(con, when="2026-09-20", complete=False)
    _read(con, when="2026-09-21", run=3, complete=False)
    assert closure.candidates(con) == []
    assert store.status_of(con, job.uid) == "new"


def test_a_keyword_search_is_never_evidence_of_absence():
    """Reed, LinkedIn and the Workable search return what the KEYWORD asked
    for. A posting missing from one of those searches may simply not match the
    terms this config happens to be using today, and `MAX_KEYWORD_TITLES`
    caps those terms at twelve, so reordering the titles in a config silently
    changes which postings are reachable at all. Closing roles on that would
    read as a board going quiet when it was the config that moved."""
    con, (job,) = _db(roles=("https://www.linkedin.com/jobs/search?keywords=x",))
    key = "https://www.linkedin.com/jobs/search?keywords=x"
    _read(con, key, when="2026-09-20", keyword=True)
    _read(con, key, when="2026-09-21", run=3, keyword=True)
    assert closure.candidates(con) == []
    assert store.status_of(con, job.uid) == "new"


def test_a_board_that_answered_with_nothing_is_not_evidence_either():
    """The `deadwood.py` case, inside the role table. A thirty-person
    employer between hires and a board whose API quietly changed shape return
    byte-identical answers: HTTP 200, parses, no rows. A read that returned
    postings and did not return THIS one is positive evidence; a read that
    returned nothing at all is the ambiguous case, and it is withheld and
    counted rather than acted on."""
    con, (job,) = _db()
    _read(con, when="2026-09-20", roles=0)
    _read(con, when="2026-09-21", run=3, roles=0)
    assert closure.candidates(con) == []
    out = closure.close_absent(con)
    assert out["closed"] == []
    assert out["withheld_empty"] == 1, out


def test_closing_is_idempotent_and_does_not_reclose():
    con, (job,) = _db()
    _read(con, when="2026-09-20")
    _read(con, when="2026-09-21", run=3)
    assert closure.close_absent(con)["closed"] == [job.uid]
    assert closure.close_absent(con)["closed"] == []


def test_the_evidence_a_role_carries_is_readable_without_closing_anything():
    """`candidates` is a report, not an action. The dry path has to be able to
    say what it would do and why, because a closure sweep that can only be
    inspected by running it is a sweep nobody can review."""
    con, (job,) = _db()
    _read(con, when="2026-09-20")
    _read(con, when="2026-09-21", run=3)
    got = closure.candidates(con)[0]
    assert got["source_key"] == BOARD
    assert got["last_read"] == "2026-09-21"
    assert got["last_seen"] == "2026-09-01"
    assert got["status"] == "new"
    assert store.status_of(con, job.uid) == "new", "reporting closed a role"


def test_a_stale_role_whose_source_was_never_read_reports_as_unknown():
    """Unknown is a third state, not a synonym for open. A role nobody has
    heard about because its board has not been read is not the same as a role
    confirmed still listed, and the two used to render identically."""
    con, (job,) = _db()
    fresh = _sentinel()
    state = closure.source_states(con)
    assert state[job.uid] == "unknown", state
    assert state[fresh.uid] == "listed", state

    _read(con, when="2026-09-20")
    assert closure.source_states(con)[job.uid] == "absent"

    con.execute("UPDATE roles SET last_seen=? WHERE uid=?",
                ("2026-09-20", job.uid))
    assert closure.source_states(con)[job.uid] == "listed"


def test_the_number_of_confirmations_is_a_named_constant():
    assert closure.MIN_CONFIRMATIONS == 2


def test_a_role_with_no_link_is_never_closed():
    """`migrate` imported the old `state/seen.json`, whose rows hold a uid, a
    company and a title and no link. Their uid is hashed from
    company|title|location rather than from a URL, so no posting read off a
    board can ever produce that id, so `touch_seen` can never say they are
    still listed however alive they are. 103 of them sit on this database, and
    a sweep arguing from absence would close every one on the first two reads
    of whatever source they were attributed to."""
    con, (job,) = _db()
    con.execute("UPDATE roles SET url='' WHERE uid=?", (job.uid,))
    _read(con, when="2026-09-20")
    _read(con, when="2026-09-21", run=3)
    assert closure.candidates(con) == []
    assert store.status_of(con, job.uid) == "new"


def test_touch_seen_says_a_board_still_lists_a_role_and_never_rewinds():
    con, (job,) = _db(last_seen="2026-09-01")
    assert store.touch_seen(con, [job.uid], when="2026-09-20") == 1
    seen = con.execute("SELECT last_seen FROM roles WHERE uid=?",
                       (job.uid,)).fetchone()["last_seen"]
    assert seen == "2026-09-20"
    # A resumed scan and a seed load both write dates that may be older than
    # the newest reading, and a date going backwards hands a role back its
    # staleness.
    assert store.touch_seen(con, [job.uid], when="2026-09-10") == 0
    seen = con.execute("SELECT last_seen FROM roles WHERE uid=?",
                       (job.uid,)).fetchone()["last_seen"]
    assert seen == "2026-09-20"
    # And it never invents a row for a posting the config does not want.
    before = con.execute("SELECT COUNT(*) c FROM roles").fetchone()["c"]
    store.touch_seen(con, ["nothing-like-this"], when="2026-09-21")
    after = con.execute("SELECT COUNT(*) c FROM roles").fetchone()["c"]
    assert before == after


# -------------------------------------------------------------- backfill

def test_the_backfill_only_fills_in_what_is_unambiguous():
    from jobradar.models import Source
    con, (job,) = _db()
    con.execute("UPDATE roles SET source_key=''")
    srcs = [Source(company="Acme", url=BOARD, platform="greenhouse"),
            Source(company="Fresh", url=FRESH, platform="ashby")]
    got = closure.backfill_source_keys(con, srcs)
    assert got["filled"] == 2, got
    assert con.execute("SELECT source_key FROM roles WHERE uid=?",
                       (job.uid,)).fetchone()["source_key"] == BOARD


def test_a_company_on_two_boards_is_left_empty_rather_than_guessed():
    """A wrong attribution is not cosmetic: it closes a live role on another
    board's evidence, silently."""
    from jobradar.models import Source
    con, (job,) = _db()
    con.execute("UPDATE roles SET source_key=''")
    srcs = [Source(company="Acme", url=BOARD, platform="greenhouse"),
            Source(company="Acme", url=BOARD + "?dept=eng",
                   platform="greenhouse")]
    got = closure.backfill_source_keys(con, srcs)
    assert got["ambiguous"] == 1, got
    assert con.execute("SELECT source_key FROM roles WHERE uid=?",
                       (job.uid,)).fetchone()["source_key"] == ""


def test_the_backfill_never_touches_a_role_that_already_has_a_source():
    from jobradar.models import Source
    con, (job,) = _db()
    srcs = [Source(company="Acme", url=OTHER, platform="greenhouse")]
    got = closure.backfill_source_keys(con, srcs)
    assert got["looked_at"] == 0, got
    assert con.execute("SELECT source_key FROM roles WHERE uid=?",
                       (job.uid,)).fetchone()["source_key"] == BOARD


def test_a_keyword_template_is_never_a_backfill_target():
    """Its URL holds a `{keyword}` placeholder, so the key in the file is not
    a key anything was ever read under."""
    from jobradar.models import Source
    con, (job,) = _db()
    con.execute("UPDATE roles SET source_key=''")
    srcs = [Source(company="Acme", url="https://x.invalid/s?q={keyword}",
                   platform="linkedin", keyword_template=True)]
    got = closure.backfill_source_keys(con, srcs)
    assert got["filled"] == 0, got


def test_a_database_made_before_source_key_existed_still_opens(tmp=None):
    """The migration bug this shipped with for about ten minutes.

    `SCHEMA` runs on every connect. Against a database made by an older
    version, `CREATE TABLE IF NOT EXISTS roles` is a no-op, so an index on the
    new column placed in that script refuses with "no such column:
    source_key" -- and that is not one broken feature, it is every command on
    that database refusing to open it. Found by running it against the
    maintainer's own 6,839 role database, which is the only place it could be
    found, because every test here builds its database from the current
    schema.
    """
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as td:
        path = _P(td) / "old.db"
        raw = sqlite3.connect(path)
        # The 2026-09 shape: roles without source_key, and no source_reads.
        raw.execute("CREATE TABLE roles (uid TEXT PRIMARY KEY, company TEXT, "
                    "title TEXT, url TEXT, platform TEXT, flags TEXT, "
                    "reasons TEXT, score REAL, first_seen TEXT NOT NULL, "
                    "last_seen TEXT NOT NULL)")
        raw.execute("CREATE TABLE role_state (uid TEXT PRIMARY KEY, "
                    "status TEXT NOT NULL DEFAULT 'new', note TEXT, "
                    "updated_at TEXT NOT NULL)")
        raw.execute("INSERT INTO roles (uid,company,title,url,platform,"
                    "first_seen,last_seen) VALUES "
                    "('abc','Acme','EM','https://x/1','greenhouse',"
                    "'2026-09-01','2026-09-01')")
        raw.commit()
        raw.close()

        con = store.connect(path)
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(roles)")}
            assert "source_key" in cols
            assert con.execute("SELECT COUNT(*) c FROM source_reads"
                               ).fetchone()["c"] == 0
            # And the column it added is empty, not guessed at.
            assert con.execute("SELECT source_key FROM roles WHERE uid='abc'"
                               ).fetchone()["source_key"] in ("", None)
            assert closure.candidates(con) == []
        finally:
            con.close()


def test_the_backfill_leaves_a_row_with_no_link_unattributed():
    """Measured on the maintainer's database: the first version of this
    attributed 3,818 of the 4,260 rows `migrate` imported from the old
    seen-set to a board on a company-name match. Those rows have no link, so
    their uid is hashed from company|title|location and no posting read off
    that board can ever produce it. The attribution could never be tested by
    anything, which makes it a claim rather than a record."""
    from jobradar.models import Source
    con, (job,) = _db()
    con.execute("UPDATE roles SET source_key='', url='' WHERE uid=?",
                (job.uid,))
    srcs = [Source(company="Acme", url=BOARD, platform="greenhouse")]
    got = closure.backfill_source_keys(con, srcs)
    assert got["looked_at"] == 0, got
    assert con.execute("SELECT source_key FROM roles WHERE uid=?",
                       (job.uid,)).fetchone()["source_key"] == ""


def test_the_backfill_can_place_a_row_that_has_a_link_but_no_platform():
    """`seed load` writes rows with no platform. One with a real link and a
    company that matches exactly one board is placeable, and the second pass
    is what places it."""
    from jobradar.models import Source
    con, (job,) = _db()
    con.execute("UPDATE roles SET source_key='', platform='' WHERE uid=?",
                (job.uid,))
    srcs = [Source(company="Acme", url=BOARD, platform="greenhouse")]
    got = closure.backfill_source_keys(con, srcs)
    assert got["filled"] >= 1, got
    assert con.execute("SELECT source_key FROM roles WHERE uid=?",
                       (job.uid,)).fetchone()["source_key"] == BOARD
