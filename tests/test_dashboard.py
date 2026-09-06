"""The surface a person actually reads, and the ways it lied to them.

Every test here comes from something the dashboard showed that was not true:
a count that promised roles the tab was hiding, an unranked role sorted as
though it had been read and judged, a salary comparison across currencies the
rest of the tool refuses to make, a note that could not be deleted, and a row
that ran off the edge of a container that clips rather than scrolls.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import store
from jobradar.output import html as html_mod
from jobradar.output import interactive

SRC = (Path(__file__).resolve().parent.parent / "jobradar" / "output"
       / "interactive.py").read_text(encoding="utf-8")


def _con():
    con = store.connect(":memory:")
    store.migrate(con, state_path=str(Path(tempfile.mkdtemp()) / "none.json"))
    con.execute("INSERT INTO roles (uid,company,title,url,platform,first_seen,"
                "last_seen) VALUES ('u1','Acme','Engineering Manager',"
                "'https://x.invalid/1','lever','2026-08-25','2026-08-25')")
    return con


def _note(con, uid="u1"):
    r = con.execute("SELECT note FROM role_state WHERE uid=?", (uid,)).fetchone()
    return r["note"] if r else None


def test_emptying_the_note_box_deletes_the_note():
    """Nothing anywhere could remove one. Clearing the box sent "", the API
    answered ok, the page said "Note saved" and reloaded, and the old note was
    still there, because the write kept the previous note whenever the new one
    was empty."""
    con = _con()
    store.set_status(con, "u1", "interested", "second round 3 Sept")
    assert _note(con) == "second round 3 Sept"
    store.set_status(con, "u1", "interested", "")
    assert _note(con) == "", "an emptied box has to actually empty it"


def test_a_status_button_does_not_wipe_the_note_on_its_way_past():
    """The other half, and the reason the old behaviour existed. Skip and
    Apply send a status and no note at all, and must leave the note alone."""
    con = _con()
    store.set_status(con, "u1", "interested", "second round 3 Sept")
    store.set_status(con, "u1", "applied")          # note not supplied
    assert _note(con) == "second round 3 Sept"
    assert con.execute("SELECT status FROM role_state WHERE uid='u1'"
                       ).fetchone()["status"] == "applied"


def test_an_unranked_role_sorts_above_a_role_judged_worthless():
    """fit -1 means "not read yet", not "read and bad". The first fix for this
    mapped -1 to -0.5, which is still below zero, so it did nothing: an
    unranked Head of Engineering still sorted underneath a role scored 0 with
    the reason "no people leadership at all". Fit scores are whole numbers, so
    the sentinel has to sit strictly between 0 and 1."""
    assert "v<0 ? 0.5 : v" in SRC, "unranked is below a genuine zero again"
    assert "v<0 ? -0.5 : v" not in SRC


def test_the_salary_sort_does_not_invent_a_floor_or_cross_currencies():
    """data-payfloor fell back to the top of the range, so "up to 175,000"
    claimed a floor of 175,000 and beat a role guaranteeing 150,000 to
    180,000: the vaguer advert winning on a figure nobody had promised. The
    comparison also ran across currencies, which is the exact guess
    `salary.clears_floor` refuses to make, on rows already flagged "not
    compared"."""
    assert 'data-payfloor="{int(r["salary_min"] or 0)}"' in SRC
    assert 'data-payfloor="{int(r["salary_min"] or r["salary_max"] or 0)}"' not in SRC
    assert "payGroup" in SRC and "HOME_CUR" in SRC
    assert 'data-paycur=' in SRC, "the currency has to travel with the figure"


def test_the_facet_counts_are_counted_against_the_tab_you_are_in():
    """They were counted once, in Python, over every row in the database,
    while the page opens on Open and Open hides everything settled. A board
    with one skipped public-sector role rendered "Public sector 1", and
    clicking it emptied the list and said "Nothing matches those filters"."""
    assert "function paintCounts(" in SRC
    # Every facet the page offers, counted in the browser against the rows the
    # current tab admits. Named individually rather than matched as one call
    # string: a new facet used to break this test purely by being added, which
    # is a failure that says nothing about whether the counts are right.
    assert "paintCounts(cSec,cMode,cEmp,cCountry,cCity)" in SRC
    for dim in ("cSec", "cMode", "cEmp", "cCountry", "cCity"):
        assert f"{dim}={{}}" in SRC.replace(" ", "") or f"{dim}={{}}," in SRC.replace(" ", ""), dim


def test_a_word_with_no_spaces_in_it_cannot_run_off_the_edge():
    """`.list` clips rather than scrolls and the page does not scroll
    sideways, so an unbroken string was not merely ugly, it was unreachable.
    Measured in a browser before the fix: a 180 character location rendered
    1,384px wide inside a 620px row and everything past the edge was cut off
    with nothing to say it was there. Afterwards, 435px and no overflow.

    A long job title was never the problem, because titles have spaces in
    them. A company name, a German compound word or a bare URL is."""
    css = html_mod._CSS if hasattr(html_mod, "_CSS") else ""
    blob = css or Path(html_mod.__file__).read_text(encoding="utf-8")
    assert "overflow-wrap:anywhere" in blob
    for cls in (".role", ".meta"):
        assert cls in blob.split("overflow-wrap:anywhere")[0][-120:] or \
            cls in [c.strip() for c in blob.split("overflow-wrap")[0]
                    .rsplit("\n", 1)[-1].split(",")], f"{cls} still cannot break"


def test_a_very_long_title_is_rendered_in_full_rather_than_cut():
    """A 395 character title is legitimate. The user's example: "Senior
    Principal Staff Engineering Manager of Distributed Platform Reliability
    and Developer Experience for the Global Payments and Settlement
    Organisation". Truncating it would hide the seniority, which is the part
    that decides whether the role is worth reading."""
    from jobradar.models import Job
    from jobradar.output import html_out

    title = ("Senior Principal Staff Engineering Manager of Distributed "
             "Platform Reliability and Developer Experience for the Global "
             "Payments and Settlement Organisation")
    j = Job(company="Acme", title=title, url="https://x.invalid/1",
            platform="lever", location="London, UK", description="d " * 50)
    j.fit = 60
    out = Path(tempfile.mkdtemp())
    p = html_out.write(out / "index.html", new=[j], seen=[], dropped={},
                       sources_ok=1, sources_total=1, throttled={}, postings=1)
    assert title in Path(p).read_text(encoding="utf-8")


def test_a_scan_pointed_at_another_database_does_not_import_this_one():
    """`--db` reads as isolation and was not one.

    `store.migrate` resolves state/seen.json and applications.local.yaml
    against the working directory, and `cmd_scan` called it with no arguments
    at all. So a scan started inside the repo with `--db /tmp/scratch.db`
    still read this directory's history and copied 1,526 roles and a real
    application history into the scratch file. Nothing was written back, but
    the copy is somebody's job search sitting in a temp directory they will
    never think to clear. Found when an audit's throwaway database turned out
    to be holding the owner's applications.
    """
    import json
    import os

    d = Path(tempfile.mkdtemp())
    (d / "state").mkdir()
    (d / "state" / "seen.json").write_text(json.dumps({"seen": {
        "old1": {"company": "Previous Employer", "title": "Engineering Manager"}}}),
        encoding="utf-8")
    (d / "applications.local.yaml").write_text(
        "applications:\n  - company: Previous Employer\n"
        "    title: Engineering Manager\n    status: interviewing\n",
        encoding="utf-8")

    cwd = os.getcwd()
    try:
        os.chdir(d)
        # Somewhere else entirely: the legacy stores here are not its history.
        elsewhere = store.connect(str(d / "scratch.db"))
        got = store.migrate(elsewhere, state_path="", apps_path="")
        assert got["roles"] == 0, "a scratch database inherited someone's history"
        assert elsewhere.execute("SELECT COUNT(*) n FROM roles"
                                 ).fetchone()["n"] == 0

        # The configured database still gets the upgrade path this exists for.
        own = store.connect(str(d / "data" / "job-radar.db"))
        got = store.migrate(own, state_path="state/seen.json")
        assert got["roles"] == 1, "the real upgrade path stopped working"
        assert own.execute("SELECT company FROM roles").fetchone()["company"] \
            == "Previous Employer"
    finally:
        os.chdir(cwd)


def test_a_job_that_ran_too_long_is_told_so_and_not_blamed_on_a_restart():
    """The timeout sweep was dead code.

    The blanket sweep ran first and matched every running and pending row
    unconditionally, so the one below it could never match: `runner.TIMEOUT`,
    which the server passes in, did nothing at all. A job that had been
    running forty minutes and one that started ten seconds ago were both told
    "the server restarted while this was running", which is the wrong
    explanation for the first and a misleading one for anybody chasing a
    generation that had stuck.
    """
    con = _con()
    con.execute("INSERT INTO jobs (uid,kind,state,requested_at,started_at) "
                "VALUES ('u1','cv','running','2020-01-01T00:00:00',"
                "'2020-01-01T00:00:00')")
    con.execute("INSERT INTO jobs (uid,kind,state,requested_at,started_at) "
                "VALUES ('u1','screen','running',datetime('now','localtime'),"
                "datetime('now','localtime'))")

    # Mid-flight: only the genuinely old one dies, because the young one is
    # a job that is actually running.
    assert store.reap_orphans(con, timeout_s=900, restarted=False) == 1
    rows = {r["kind"]: r for r in con.execute("SELECT kind,state,error FROM jobs")}
    assert rows["cv"]["state"] == "failed"
    assert "gave up after 15 minutes" in rows["cv"]["error"]
    assert rows["screen"]["state"] == "running", "killed a job that was running"

    # At startup the survivor is orphaned too, with the accurate reason.
    assert store.reap_orphans(con, timeout_s=900) == 1
    rows = {r["kind"]: r for r in con.execute("SELECT kind,state,error FROM jobs")}
    assert rows["screen"]["state"] == "failed"
    assert "server restarted" in rows["screen"]["error"]
    assert "gave up after 15 minutes" in rows["cv"]["error"], (
        "the old job's real reason was overwritten by the blanket sweep")


def test_rescreen_reports_before_it_removes_and_never_touches_what_you_acted_on():
    """A scan filters what it fetched that day and never looks back.

    So every config change applies only to roles found afterwards: tighten an
    exclude and the roles it was written for stay on the dashboard for ever.
    Measured on a real database after a day of config changes, 196 of 1,670
    roles no longer matched the config supposedly producing them.

    Reporting is the default because this is the one command whose whole job
    is deleting rows somebody may be relying on, and a role with a status is
    never removed: that status is a decision, and it outranks a filter.
    """
    import io
    from jobradar.cli import main

    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: [engineering manager]\n"
                   "  exclude: [mechanical]\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    db = d / "x.db"
    con = store.connect(str(db))
    for uid, title in (("keep", "Engineering Manager"),
                       ("drop", "Mechanical Engineering Manager"),
                       ("mine", "Mechanical Engineering Manager")):
        con.execute("INSERT INTO roles (uid,company,title,url,platform,first_seen,"
                    "last_seen) VALUES (?,'Acme',?,?,'lever','2026-08-25',"
                    "'2026-08-25')", (uid, title, f"https://x.invalid/{uid}"))
    store.set_status(con, "mine", "applied")
    con.commit()
    con.close()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["-c", str(cfg), "rescreen", "--db", str(db)]) == 0
    assert "Nothing was removed" in out.getvalue()
    con = store.connect(str(db))
    assert con.execute("SELECT COUNT(*) n FROM roles").fetchone()["n"] == 3
    con.close()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["-c", str(cfg), "rescreen", "--db", str(db), "--remove"]) == 0
    con = store.connect(str(db))
    left = {r["uid"] for r in con.execute("SELECT uid FROM roles")}
    assert left == {"keep", "mine"}, f"removed the wrong rows: {left}"
    con.close()


def test_rescreen_re_applies_the_dealbreakers_and_the_floor_not_just_the_title():
    """`rescreen` ran the title and location gate and called that the config.

    Its own help says it re-applies "titles, locations, dealbreakers or the
    salary floor". Two of those four never ran. On a real database a hard
    dealbreaker matching every stored role, and a floor above every stated
    figure, both produced "All N roles still match your config" -- the
    sentence that means everything is fine, printed because the check that
    would have disagreed was never made. That is the worst shape a bug can
    take in this tool: a failure that renders identically to a success, on
    the one command whose entire job is to be the second opinion.
    """
    import io
    from jobradar.cli import main

    d = Path(tempfile.mkdtemp())
    db = d / "x.db"
    con = store.connect(str(db))
    # All three pass the title gate. They differ only in the things `rescreen`
    # was not looking at.
    con.execute(
        "INSERT INTO roles (uid,company,title,url,platform,description,"
        "salary_min,salary_max,salary_currency,salary_period,salary_confirmed,"
        "first_seen,last_seen) VALUES ('clean','Acme','Engineering Manager',"
        "'https://x.invalid/clean','lever','A perfectly ordinary advert. ' || "
        "hex(randomblob(200)),150000,160000,'GBP','year',1,"
        "'2026-08-25','2026-08-25')")
    con.execute(
        "INSERT INTO roles (uid,company,title,url,platform,description,"
        "salary_min,salary_max,salary_currency,salary_period,salary_confirmed,"
        "first_seen,last_seen) VALUES ('lowpay','Acme','Engineering Manager',"
        "'https://x.invalid/lowpay','lever','A perfectly ordinary advert. ' || "
        "hex(randomblob(200)),20000,21000,'GBP','year',1,"
        "'2026-08-25','2026-08-25')")
    con.execute(
        "INSERT INTO roles (uid,company,title,url,platform,description,"
        "first_seen,last_seen) VALUES ('breaker','Acme','Engineering Manager',"
        "'https://x.invalid/breaker','lever',"
        "'You will carry the pager on a 24/7 on-call rota. ' || "
        "hex(randomblob(200)),'2026-08-25','2026-08-25')")
    con.commit()
    con.close()

    # Titles alone: nothing has changed, and the command must say so.
    plain = d / "plain.yaml"
    plain.write_text("titles:\n  include: [engineering manager]\n"
                     "sources:\n  use_bundled: false\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["-c", str(plain), "rescreen", "--db", str(db)]) == 0
    assert "All 3 roles still match" in out.getvalue(), out.getvalue()

    # A floor above the low-paying role. Only that one goes.
    floor = d / "floor.yaml"
    floor.write_text("titles:\n  include: [engineering manager]\n"
                     "salary:\n  floor: 100000\n  currency: GBP\n"
                     "sources:\n  use_bundled: false\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["-c", str(floor), "rescreen", "--db", str(db)]) == 0
    text = out.getvalue()
    assert "1 of 3 roles no longer match" in text, text
    assert "Nothing was removed" in text, "reporting is still the default"

    # A hard dealbreaker the description matches. Only that one goes.
    db_cfg = d / "db.yaml"
    db_cfg.write_text("titles:\n  include: [engineering manager]\n"
                      "dealbreakers:\n  - name: on-call\n"
                      "    pattern: 'on.?call rota|carry the pager'\n"
                      "    hard: true\n"
                      "sources:\n  use_bundled: false\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["-c", str(db_cfg), "rescreen", "--db", str(db)]) == 0
    text = out.getvalue()
    assert "1 of 3 roles no longer match" in text, text

    # And --remove takes the right row, not merely the right number of rows.
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["-c", str(db_cfg), "rescreen", "--db", str(db),
                     "--remove"]) == 0
    con = store.connect(str(db))
    left = {r["uid"] for r in con.execute("SELECT uid FROM roles")}
    con.close()
    assert left == {"clean", "lowpay"}, f"removed the wrong rows: {left}"


def test_rescreen_never_removes_a_role_whose_advert_was_never_fetched():
    """A dealbreaker cannot match text that was never downloaded.

    Headline-only sources store an empty description, and roughly a quarter
    of a real board arrives that way. If an unmatched pattern counted as a
    reason to delete, `rescreen --remove` would quietly bin every role whose
    enrichment had not run yet -- deleting roles for failing a test they were
    never given.
    """
    import io
    from jobradar.cli import main

    d = Path(tempfile.mkdtemp())
    db = d / "x.db"
    con = store.connect(str(db))
    con.execute("INSERT INTO roles (uid,company,title,url,platform,description,"
                "first_seen,last_seen) VALUES ('bare','Acme',"
                "'Engineering Manager','https://x.invalid/bare','workday',''"
                ",'2026-08-25','2026-08-25')")
    con.commit()
    con.close()

    cfg = d / "config.yaml"
    cfg.write_text("titles:\n  include: [engineering manager]\n"
                   "salary:\n  floor: 500000\n  currency: GBP\n"
                   "dealbreakers:\n  - name: on-call\n"
                   "    pattern: 'carry the pager'\n    hard: true\n"
                   "sources:\n  use_bundled: false\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["-c", str(cfg), "rescreen", "--db", str(db),
                     "--remove"]) == 0
    assert "All 1 roles still match" in out.getvalue(), out.getvalue()
    con = store.connect(str(db))
    n = con.execute("SELECT COUNT(*) n FROM roles").fetchone()["n"]
    con.close()
    assert n == 1, "deleted a role for failing a test it was never given"


def test_the_salary_sort_groups_on_your_currency_not_the_boards():
    """`bySalary` compares figures only inside one currency, because
    `clears_floor` refuses to compare across them.

    Which currency counted as "yours" was decided by a vote of the board's own
    stated salaries. On a GBP floor over a board holding more USD figures than
    GBP ones -- ordinary for anyone in the UK reading a list of mostly US
    employers -- USD won, so every row already stamped "salary in USD, floor
    in GBP, not compared" was ranked ABOVE the sterling rows that had been
    compared. The sort contradicted the caveat printed on the same row.
    """
    con = _con()
    con.execute("DELETE FROM roles")
    rows = [("gbp", "GBP", 150000), ("usd1", "USD", 200000),
            ("usd2", "USD", 210000), ("usd3", "USD", 220000)]
    for uid, cur, lo in rows:
        con.execute(
            "INSERT INTO roles (uid,company,title,url,platform,salary_min,"
            "salary_max,salary_currency,salary_period,salary_confirmed,"
            "first_seen,last_seen) VALUES (?,'Acme','Engineering Manager',?,"
            "'lever',?,?,?,'year',1,'2026-08-25','2026-08-25')",
            (uid, f"https://x.invalid/{uid}", lo, lo + 1000, cur))
    con.commit()

    # No currency given: the board's own majority is the fallback, unchanged.
    assert 'const HOME_CUR="USD"' in interactive.render(con)
    # Your currency given: it wins, even though it is in the minority.
    assert 'const HOME_CUR="GBP"' in interactive.render(con, "GBP")
    # Case is not the reader's problem.
    assert 'const HOME_CUR="GBP"' in interactive.render(con, "gbp")
    con.close()


def test_serve_hands_the_dashboard_the_currency_the_floor_is_written_in():
    """The renderer can only group by your currency if something passes it.

    `render` grew the argument and nothing filled it in, which would have left
    the fix inert while every test of the renderer passed.
    """
    import inspect
    from jobradar import serve as serve_mod

    src = inspect.getsource(serve_mod)
    assert "home_currency" in src, "serve never reads salary.currency"
    assert "interactive.render(con, self.home_currency)" in src, (
        "the dashboard is still rendered without the configured currency")
    assert "salary_currency" in src, (
        "home_currency is set from something other than the config")


def test_the_first_scan_announces_the_number_of_sources_it_will_read():
    """The wizard's last line said "It reads 17,807 job boards".

    That literal is the size of the bundled list with no sectors and no
    source countries chosen -- and the wizard has just walked its reader
    through choosing both. With sectors set it announced 17,807 and then
    `cmd_scan` printed "Fetching 13,440 sources" on the very next line: two
    numbers four thousand apart, in consecutive sentences, in the first thing
    anybody runs. It also went stale on its own every time the list was
    regrown upstream.
    """
    from jobradar import setup_wizard
    from jobradar import sources as src_mod
    from jobradar.config import load as load_cfg

    src = (Path(__file__).resolve().parent.parent / "jobradar"
           / "setup_wizard.py").read_text(encoding="utf-8")
    assert "17,807 job boards" not in src, "the count is hard-coded again"

    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text(
        "titles:\n  include: [engineering manager]\n"
        "sources:\n  use_bundled: false\n"
        "  extra:\n"
        "    - company: One\n      url: https://boards.greenhouse.io/one\n"
        "    - company: Two\n      url: https://boards.greenhouse.io/two\n",
        encoding="utf-8")
    n = setup_wizard._sources_it_will_read(cfg)
    assert n == 2, f"announced {n} sources for a two-source config"
    # And it is the same number the scan itself will print, by construction.
    assert n == len(src_mod.load(load_cfg(str(cfg))))

    # A config it cannot read is one sentence of a progress message, not a
    # crash before the scan that would have reported the problem properly.
    assert setup_wizard._sources_it_will_read(d / "nope.yaml") == 0


def test_a_limited_scan_says_so_on_the_first_run_too():
    """`scan --limit 200` is the quick look the wizard recommends by name.

    The warning that most of the list went unread lived in the `else` branch
    of the new-roles message, and a first run takes the branch above it. So
    the one person who would run a limited scan -- somebody trying the tool
    for the first time -- was the one person never told that the roles on the
    other 13,240 sources would be stamped "new" days later.

    The number also has to be what was really read. `--limit 20000` against a
    13,440-source config read all of them and still said "only 20000 sources
    were read".
    """
    import io
    from jobradar import cli
    from jobradar.fetch import Result
    from jobradar.models import Job

    d = Path(tempfile.mkdtemp())
    cfg = d / "config.yaml"
    cfg.write_text(
        "titles:\n  include: [engineering manager]\n"
        "output:\n  formats: []\n  dir: " + str(d / "out") + "\n"
        "sources:\n  use_bundled: false\n  extra:\n"
        "    - company: One\n      url: https://boards.greenhouse.io/one\n"
        "    - company: Two\n      url: https://boards.greenhouse.io/two\n"
        "    - company: Three\n      url: https://boards.greenhouse.io/three\n",
        encoding="utf-8")

    class _Args:
        config = str(cfg)
        db = str(d / "x.db")
        state = str(d / "seen.json")
        out = str(d / "out")
        docs = None
        dry_run = False
        no_enrich = True
        no_caffeine = True
        resume = False
        no_open = True
        limit = 0

    real_fetch, real_parse = cli.fetch_all, cli.adapters.parse
    cli.fetch_all = lambda srcs, **kw: [Result(source=s, payload=b"[]")
                                        for s in srcs]
    cli.adapters.parse = lambda payload, src: [
        Job(company=src.company, title="Engineering Manager",
            url=f"https://x.invalid/{src.company}", platform="greenhouse",
            location="London")]
    try:
        # Limited: the note appears even though this is run one.
        _Args.limit = 2
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.cmd_scan(_Args) == 0
        text = out.getvalue()
        assert "This is the first scan" in text, text
        assert "only 2 of your 3 sources were read" in text, text

        # A limit larger than the list cut nothing, so it must say nothing.
        _Args.limit = 20000
        _Args.db = str(d / "y.db")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.cmd_scan(_Args) == 0
        assert "were read" not in out.getvalue(), out.getvalue()

        # A dry run records nothing, so nothing can be stamped new later and
        # the note would be describing a consequence that cannot happen.
        _Args.limit, _Args.dry_run = 2, True
        _Args.db = str(d / "z.db")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.cmd_scan(_Args) == 0
        assert "were read" not in out.getvalue(), out.getvalue()
    finally:
        cli.fetch_all, cli.adapters.parse = real_fetch, real_parse


def test_a_role_with_no_link_is_history_and_not_a_listing():
    """`migrate` imports the old state/seen.json, which holds a uid, a company
    and a title and no link, because its whole job was answering "have I seen
    this before". Those rows went into the same table as real listings and the
    dashboard rendered them with an empty href: 103 of them on a real
    database, indistinguishable from a live vacancy until you clicked one and
    the page reloaded on itself.

    They still have to exist, or every role in the old seen-set comes back as
    new on the next scan. They just must not be offered as something to apply
    to.
    """
    from jobradar.output import interactive

    con = store.connect(":memory:")
    con.execute("INSERT INTO roles (uid,company,title,url,platform,first_seen,"
                "last_seen) VALUES ('real','Acme','Engineering Manager',"
                "'https://x.invalid/1','lever','2026-08-25','2026-08-25')")
    con.execute("INSERT INTO roles (uid,company,title,first_seen,last_seen) "
                "VALUES ('legacy','Anthropic','Engineering Manager',"
                "'2026-08-25','2026-08-25')")
    con.commit()

    rows = interactive._rows(con)
    assert [r["uid"] for r in rows] == ["real"], "a row with no link was offered"
    # And it is still in the table, so the seen-set still knows about it.
    assert con.execute("SELECT COUNT(*) n FROM roles").fetchone()["n"] == 2


def test_the_first_scan_and_the_dashboard_agree_about_what_new_means():
    """`scan` said "none are marked new yet" and the New tab then showed all
    of them.

    The two count different things: the scan line is per-run, so the first run
    has nothing to compare against, while the tab is per-date, so everything
    first seen today qualifies. Each is defensible alone. Together they are a
    contradiction on the one day a person has no way to know which to believe.
    """
    from jobradar.output import interactive

    con = store.connect(":memory:")
    con.execute("INSERT INTO roles (uid,company,title,url,platform,first_seen,"
                "last_seen) VALUES ('a','Acme','Engineering Manager',"
                "'https://x.invalid/1','lever','2026-08-25','2026-08-25')")
    con.commit()
    # The tab really does show it, which is what made the old wording wrong
    # rather than merely terse.
    assert interactive.render(con).count('data-new="1"') == 1

    # Asserted against what the command prints, not against the source, so the
    # comment explaining the old wording cannot satisfy the test.
    import inspect
    from jobradar import cli
    printed = [ln for ln in inspect.getsource(cli.cmd_scan).splitlines()
               if "_say(" in ln or ln.strip().startswith('f"')]
    blob = " ".join(printed)
    assert "none are marked new yet" not in blob
    assert "This is the first scan" in blob
