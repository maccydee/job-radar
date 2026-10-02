"""A role nobody can vouch for must not render as a live vacancy.

`roles.last_seen` was written on every scan and read by nothing. The dashboard
showed every role inside a fourteen day window identically, so a vacancy
confirmed on this morning's read and one whose board has been refusing for
three weeks were the same row with the same pay chip and the same Apply
button. That is the "a value meaning we cannot say must never be read as no"
fault in CLAUDE.md, pointed the other way: here the unknown case was being
read as YES.

So three states reach the page and the text output: listed, absent, unknown.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import closure, store                    # noqa: E402
from jobradar.models import Job                        # noqa: E402
from jobradar.output import interactive                # noqa: E402

BOARD = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
FRESH = "https://api.ashbyhq.com/posting-api/job-board/fresh"


def _job(n, source, company="Acme"):
    return Job(company=company, title=f"Engineering Manager {n}",
               url=f"https://boards.greenhouse.io/acme/jobs/{n}",
               platform="greenhouse", location="London", source_id=source,
               description="Lead a platform team. " * 20)


def _day(ago):
    return (date.today() - timedelta(days=ago)).isoformat()


# Inside `store.LIVE_WINDOW_DAYS`, deliberately. A role older than that window
# already drops off the page, and the row this is about is the one still ON
# the page: five days stale, still rendered, still carrying an Apply button,
# and until now indistinguishable from a role confirmed this morning.
STALE_DAYS = 5


def _db():
    """One stale role on a board, and one seen on the latest scan."""
    con = store.connect(":memory:")
    stale = _job(1, BOARD)
    store.upsert_roles(con, [stale], run=1)
    con.execute("UPDATE roles SET last_seen=?", (_day(STALE_DAYS),))
    fresh = _job(2, FRESH, company="Fresh")
    store.upsert_roles(con, [fresh], run=1)
    return con, stale, fresh


def test_the_dashboard_row_query_carries_the_three_states():
    con, stale, fresh = _db()
    rows = {r["uid"]: r for r in interactive._rows(con)}
    assert rows[stale.uid]["source_state"] == "unknown"
    assert rows[fresh.uid]["source_state"] == "listed"

    store.record_source_read(con, BOARD, run=2, ok=True, roles=5,
                             when=_day(2))
    rows = {r["uid"]: r for r in interactive._rows(con)}
    assert rows[stale.uid]["source_state"] == "absent"


def test_an_unknown_role_says_so_on_the_page():
    con, stale, fresh = _db()
    page = interactive.render(con)
    assert "has not been read successfully since" in page, page[:400]
    # And the role confirmed this morning carries no such caption.
    assert page.count("has not been read successfully since") == 1


def test_a_role_read_off_its_board_without_it_says_that_instead():
    con, stale, fresh = _db()
    store.record_source_read(con, BOARD, run=2, ok=True, roles=5,
                             when=_day(2))
    page = interactive.render(con)
    assert "has not been read successfully since" not in page
    assert "no longer listed" in page, page[:400]


def test_a_failed_read_leaves_the_role_unknown_rather_than_absent():
    """The invariant, as the page states it. A board that 403'd has said
    nothing, and the row must not start claiming the job has gone."""
    con, stale, fresh = _db()
    store.record_source_read(con, BOARD, run=2, ok=False, roles=0,
                             when=_day(2), why="HTTP 403")
    rows = {r["uid"]: r for r in interactive._rows(con)}
    assert rows[stale.uid]["source_state"] == "unknown"
    page = interactive.render(con)
    assert "has not been read successfully since" in page
    assert "no longer listed" not in page


def test_the_text_output_distinguishes_them_too():
    """`list` is the only view with no fourteen day window, so it is the one
    that still shows a role whose board went quiet a month ago."""
    con, stale, fresh = _db()
    from jobradar import cli, store as store_mod
    said = []
    say, connect = cli._say, store_mod.connect
    cli._say = lambda m="": said.append(m)
    # Stubbed, because `cmd_list` opens the database itself and `:memory:` is
    # a different empty database every time it is asked for.
    store_mod.connect = lambda *a, **k: con
    try:
        cli.cmd_list(_Args())
    finally:
        cli._say, store_mod.connect = say, connect
    text = "\n".join(said)
    assert "still open is unknown" in text, text
    assert stale.title in text and fresh.title in text, text
    assert text.count("still open is unknown") == 1, text


class _Args:
    """What `cmd_list` reads off its namespace."""

    db = None
    status = None
    all = False
    new = False
    limit = 0
    json = False


def test_json_is_not_how_this_is_recorded():
    """A guard against the obvious shortcut: the state is derived from the
    read log every time it is asked for, never stored on the role. A stored
    copy is a second truth that goes stale the moment a board answers."""
    con, stale, _ = _db()
    cols = {r["name"] for r in con.execute("PRAGMA table_info(roles)")}
    assert "source_state" not in cols
    assert "closed_because" not in cols
    # And the flags column is not quietly rewritten by asking.
    before = con.execute("SELECT flags FROM roles WHERE uid=?",
                         (stale.uid,)).fetchone()["flags"]
    closure.source_states(con)
    after = con.execute("SELECT flags FROM roles WHERE uid=?",
                        (stale.uid,)).fetchone()["flags"]
    assert json.loads(before or "[]") == json.loads(after or "[]")
