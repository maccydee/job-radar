import contextlib
import io
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, deadlines, store    # noqa: E402
from jobradar.models import Job               # noqa: E402
from jobradar.output import interactive       # noqa: E402

TODAY = date(2026, 10, 5)


def _db(desc):
    con = store.connect(":memory:")
    j = Job(company="Bluetree", title="Software Engineering Manager",
            url="https://bt.example/1", platform="custom", location="Milton Keynes",
            description=desc)
    store.upsert_roles(con, [j], run=1)
    return con, j.uid


def test_backfill_reads_a_closing_date_and_keeps_the_words():
    con, uid = _db("Closing date: 17th April 2026")
    out = store.backfill_closing_dates(con, TODAY)
    row = con.execute("SELECT closes_on, closes_evidence FROM roles WHERE uid=?", (uid,)).fetchone()
    assert out["set"] == 1 and row["closes_on"] == "2026-04-17"
    assert "17th April 2026" in row["closes_evidence"]


def test_backfill_never_overwrites_a_date_set_by_hand():
    con, uid = _db("Closing date: 17th April 2026")
    store.set_closing(con, uid, "2026-10-09", "by hand")
    store.backfill_closing_dates(con, TODAY)
    assert con.execute("SELECT closes_on FROM roles WHERE uid=?", (uid,)).fetchone()[0] == "2026-10-09"


def test_backfill_counts_ambiguous_and_missing_text_separately():
    con, uid = _db("Closing date 04/05/2026")
    assert store.backfill_closing_dates(con, TODAY) == {"set": 0, "ambiguous": 1, "no_text": 0, "nothing": 0}
    con2, _ = _db("")
    assert store.backfill_closing_dates(con2, TODAY)["no_text"] == 1


def test_text_with_no_closing_cue_is_nothing_not_ambiguous():
    con, uid = _db("A good role. We were founded on 3 March 2014.")
    assert store.backfill_closing_dates(con, TODAY) == {"set": 0, "ambiguous": 0, "no_text": 0, "nothing": 1}


def test_backfill_leaves_the_column_blank_when_it_cannot_read():
    con, uid = _db("Closing date 04/05/2026")
    store.backfill_closing_dates(con, TODAY)
    assert con.execute("SELECT closes_on FROM roles WHERE uid=?", (uid,)).fetchone()[0] == ""


def test_set_closing_rejects_a_non_date():
    con, uid = _db("")
    try:
        store.set_closing(con, uid, "next friday", "typed")
    except ValueError:
        return
    raise AssertionError("an unparseable date was stored")


def test_short_date():
    assert interactive._short_date("2026-10-23") == "23 Oct"
    assert interactive._short_date("2026-01-05") == "5 Jan"
    assert interactive._short_date("not a date") == "not a date"


def test_caption_for_a_future_a_past_and_a_today_deadline():
    t = date(2026, 10, 8)
    assert deadlines.caption("2026-10-13", "new", t) == "closes 2026-10-13 (5 days)"
    assert deadlines.caption("2026-10-09", "new", t) == "closes 2026-10-09 (1 day)"
    assert deadlines.caption("2026-10-08", "new", t) == "closes 2026-10-08 (today)"
    assert (deadlines.caption("2026-10-05", "new", t)
            == "closed 2026-10-05 (3 days ago, and not applied)")
    assert deadlines.caption("2026-10-05", "applied", t) == "closed 2026-10-05 (3 days ago)"
    assert deadlines.caption("", "new", t) == ""
    assert deadlines.caption("2026-10-13", "rejected", t) == ""


def _cli_db(closes):
    """A database file with one role per entry of `closes` (days from today,
    or None for no date), and a config the CLI will accept."""
    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: ['engineering manager']\n"
                   "locations:\n  countries: ['UK']\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "t.db"
    con = store.connect(db)
    uids = []
    for i, off in enumerate(closes):
        j = Job(company=f"Co{i}", title="Engineering Manager",
                url=f"https://x.example/{i}", platform="custom", location="London",
                description="A role. " * 20)
        store.upsert_roles(con, [j], run=1)
        if off is not None:
            store.set_closing(con, j.uid, (date.today() + timedelta(days=off)).isoformat(),
                              "by hand")
        uids.append(j.uid)
    con.close()
    return cfg, db, uids


def _run(cfg, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["-c", str(cfg), *argv])
    return code, buf.getvalue()


def test_list_shows_when_a_role_closes_and_when_it_already_has():
    cfg, db, uids = _cli_db([5, -3, None])
    code, out = _run(cfg, "list", "--db", str(db))
    assert code == 0, out
    soon = (date.today() + timedelta(days=5)).isoformat()
    gone = (date.today() - timedelta(days=3)).isoformat()
    assert f"closes {soon} (5 days)" in out, out
    assert f"closed {gone} (3 days ago, and not applied)" in out, out
    assert out.count("closes ") + out.count("closed ") == 2     # the undated one says nothing


def test_closing_within_keeps_only_roles_closing_in_the_window():
    cfg, db, uids = _cli_db([5, -3, 30, None])
    code, out = _run(cfg, "list", "--db", str(db), "--closing-within", "7")
    assert code == 0, out
    assert "Co0" in out
    assert not any(c in out for c in ("Co1", "Co2", "Co3")), out
    assert "1 role(s)" in out


def test_closing_within_excludes_settled_roles():
    cfg, db, uids = _cli_db([5])
    con = store.connect(db)
    store.set_status(con, uids[0], "rejected")
    con.close()
    code, out = _run(cfg, "list", "--db", str(db), "--closing-within", "7")
    assert "0 role(s)" in out, out


def test_a_dashboard_row_with_a_closing_date_says_so():
    con, uid = _db("")
    store.set_closing(con, uid, "2026-10-23", "by hand")
    page = interactive.render(con)
    assert "Closes 23 Oct" in page


def test_rescreen_reads_closing_dates_and_says_what_it_could_not_read():
    cfg, db, uids = _cli_db([])
    con = store.connect(db)
    for i, text in enumerate(("Closing date: 17th April 2099", "Closing date 04/05/2026", "")):
        j = Job(company=f"R{i}", title="Engineering Manager",
                url=f"https://r.example/{i}", platform="custom", location="London",
                description=text)
        store.upsert_roles(con, [j], run=1)
    con.close()
    code, out = _run(cfg, "rescreen", "--db", str(db))
    assert code == 0, out
    assert "closing dates: 1 read, 1 ambiguous (left blank), 1 postings had no text" in out, out


def test_a_year_less_date_is_read_against_the_day_the_posting_was_first_seen():
    """Found on the real database: "Closing date: 30th September" in a posting
    first read on 10 September came out as 30 September of the NEXT year when
    the backfill ran in October, a deadline eleven months wrong that renders
    exactly like a right one. A date with no year is relative to when it was
    written, not to when it is read."""
    con, uid = _db("Closing date: 30th September")
    con.execute("UPDATE roles SET first_seen='2026-09-10' WHERE uid=?", (uid,))
    store.backfill_closing_dates(con, date(2026, 10, 8))
    assert con.execute("SELECT closes_on FROM roles WHERE uid=?", (uid,)).fetchone()[0] == "2026-09-30"


def test_a_parser_written_date_can_be_re_read_and_a_hand_set_one_cannot():
    """A date a buggy parser wrote is never corrected by the plain backfill,
    which skips anything already set. `rederive` re-reads only dates the parser
    wrote, and leaves one typed by hand alone."""
    con, uid = _db("Closing date: 23 October 2026. Interviews: 6 November 2026.")
    store.set_closing(con, uid, "2026-11-06", "6 November 2026")          # the old parser's answer
    con2, uid2 = _db("Closing date: 23 October 2026. Interviews: 6 November 2026.")
    store.set_closing(con2, uid2, "2026-11-06", "by hand")
    con3, uid3 = _db("Join a close-knit team. Posted 1 September 2026.")
    store.set_closing(con3, uid3, "2026-09-01", "1 September 2026")
    assert store.backfill_closing_dates(con, TODAY)["set"] == 0              # plain: untouched
    out = store.backfill_closing_dates(con, TODAY, rederive=True)
    assert out["changed"] == 1, out
    assert con.execute("SELECT closes_on FROM roles").fetchone()[0] == "2026-10-23"
    store.backfill_closing_dates(con2, TODAY, rederive=True)
    assert con2.execute("SELECT closes_on FROM roles").fetchone()[0] == "2026-11-06"
    out = store.backfill_closing_dates(con3, TODAY, rederive=True)
    assert out["cleared"] == 1, out
    assert con3.execute("SELECT closes_on, closes_evidence FROM roles").fetchone()[:] == ("", "")


def test_rescreen_can_re_read_parser_written_closing_dates():
    cfg, db, uids = _cli_db([])
    con = store.connect(db)
    j = Job(company="R", title="Engineering Manager", url="https://r.example/x", platform="custom",
            location="London", description="Apply by 20 October 2099; start date 4 January 2100.")
    store.upsert_roles(con, [j], run=1)
    store.set_closing(con, j.uid, "2100-01-04", "4 January 2100")
    con.close()
    code, out = _run(cfg, "rescreen", "--reread-closing-dates", "--db", str(db))
    assert code == 0 and "1 changed" in out, out
    con = store.connect(db)
    assert con.execute("SELECT closes_on FROM roles WHERE uid=?", (j.uid,)).fetchone()[0] == "2099-10-20"


# --------------------------------- review finding 16: stale year-less dates

def test_a_year_less_date_already_past_when_first_seen_is_left_blank_not_rolled_a_year():
    """First seen 8 October, "Closing date: 30th September": the posting had
    already closed, and reading it as 30 September of the next year stored a
    deadline eleven months out."""
    con, uid = _db("Closing date: 30th September. Apply now.")
    con.execute("UPDATE roles SET first_seen='2026-10-08' WHERE uid=?", (uid,))
    out = store.backfill_closing_dates(con, date(2026, 10, 8))
    assert out["set"] == 0 and out["ambiguous"] == 1, out
    assert con.execute("SELECT closes_on FROM roles WHERE uid=?", (uid,)).fetchone()[0] == ""


def test_a_year_less_date_a_few_days_ahead_is_still_read():
    con, uid = _db("Closing date: 30th October. Apply now.")
    con.execute("UPDATE roles SET first_seen='2026-10-08' WHERE uid=?", (uid,))
    store.backfill_closing_dates(con, date(2026, 10, 8))
    assert con.execute("SELECT closes_on FROM roles WHERE uid=?", (uid,)).fetchone()[0] == "2026-10-30"


def test_the_dashboard_says_closed_for_a_past_date_and_gives_a_year_that_is_not_this_one():
    t = date(2026, 10, 8)
    assert interactive._closing_note("2026-10-23", t) == "Closes 23 Oct"
    assert interactive._closing_note("2026-09-30", t) == "Closed 30 Sep"
    assert interactive._closing_note("2027-01-05", t) == "Closes 5 Jan 2027"
    assert interactive._closing_note("2025-12-01", t) == "Closed 1 Dec 2025"
    assert interactive._closing_note("not a date", t) == "Closes not a date"
