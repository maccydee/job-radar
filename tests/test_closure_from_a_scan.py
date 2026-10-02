"""The scan has to write down what each board actually said.

Closure rests entirely on this. Without a per-run record of which sources were
read and whether the payload parsed, the only absence signal in the tool is a
role missing from a scan, and a role is missing from a scan for a dozen
reasons that say nothing about the employer: the host 403'd, it was inside a
block it set during the last run, `--limit` read a stride and never asked, the
pager stopped at its cap, the API changed shape, the run was killed.

Two faults this file exists to keep fixed:

- a failed read recorded as anything other than a failure. One mis-recorded
  403 closes every role on that board, and the report reads "47 roles closed"
  in a clean sentence with nothing true in it;
- `last_seen` meaning "got through screening" rather than "a board listed
  this". A scan stores only what the config wants, so a role whose title stops
  matching an edited config is read off its board every run and never written.
  Its `last_seen` ages exactly like a withdrawn vacancy, and closing on that
  would mark live roles closed because the READER changed their mind.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import store                              # noqa: E402
from jobradar.cli import main                           # noqa: E402

A = "https://a.invalid/x"
B = "https://b.invalid/x"


def _cfg(tmp: Path, titles="engineering manager") -> Path:
    cfg = tmp / "config.yaml"
    cfg.write_text(
        f"titles:\n  include: ['{titles}']\n"
        "locations:\n  countries: ['UK']\n"
        "sources:\n  use_bundled: false\n  extra:\n"
        f"    - {{company: Aco, platform: greenhouse, url: '{A}'}}\n"
        f"    - {{company: Bco, platform: greenhouse, url: '{B}'}}\n",
        encoding="utf-8")
    return cfg


def _greenhouse(src, titles=("Engineering Manager",)):
    # The URL carries the title, because `uid` is derived from the URL and a
    # fixture that reuses `/job/0` for a different posting hands the new one
    # the old one's identity. The first draft of this file did exactly that
    # and the closure test passed for the wrong reason: the board's
    # replacement vacancy was touching the row of the vacancy it replaced.
    return {"jobs": [
        {"title": t,
         "absolute_url": f"{src.url}/job/{t.lower().replace(' ', '-')}",
         "location": {"name": "London, United Kingdom"},
         "content": "We are hiring an engineering manager. " + "detail " * 60}
        for t in titles]}


def _scan(cfg, db, out, *extra, fetch):
    buf = io.StringIO()
    state = Path(db).parent / "state" / "seen.json"
    with contextlib.redirect_stdout(buf), \
            mock.patch("jobradar.cli.fetch_all", side_effect=fetch):
        rc = main(["-c", str(cfg), "scan", "--no-enrich", "--no-caffeine",
                   "--no-open", "--db", str(db), "--out", str(out),
                   "--state", str(state), *extra])
    return rc, buf.getvalue()


@contextlib.contextmanager
def _tmp():
    root = Path(tempfile.mkdtemp())
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _reads(db):
    con = store.connect(db)
    try:
        return {r["source_key"]: dict(r)
                for r in con.execute("SELECT * FROM source_reads")}
    finally:
        con.close()


def _both(srcs, **_):
    from jobradar.fetch import Result
    return [Result(source=s, payload=_greenhouse(s), status=200) for s in srcs]


# ----------------------------------------------------------- what was said

def test_a_scan_records_which_sources_were_read_and_which_failed():
    def fetch(srcs, **_):
        from jobradar.fetch import Result
        out = []
        for s in srcs:
            if s.url == B:
                out.append(Result(source=s, status=403, error="HTTP 403"))
            else:
                out.append(Result(source=s, payload=_greenhouse(s), status=200))
        return out

    with _tmp() as root:
        db = root / "db.db"
        # A clean scan first, so both boards have a stored role. The log only
        # keeps sources this database holds a role from: a read of a board
        # nothing came from could never be evidence about any role, and
        # keeping all of them is 17,810 rows a run on the bundled list.
        rc, said = _scan(_cfg(root), db, root / "out", fetch=_both)
        assert rc == 0, said
        rc, said = _scan(_cfg(root), db, root / "out", fetch=fetch)
        assert rc == 0, said
        reads = _reads(db)
        assert set(reads) == {A, B}, reads
        assert reads[A]["ok"] == 1 and reads[A]["roles"] == 1, reads[A]
        assert reads[B]["ok"] == 0, reads[B]
        assert "403" in reads[B]["why"], reads[B]


def test_a_200_with_an_unparseable_body_is_recorded_as_a_failure():
    """The board answered. Nothing was read. Those are different facts, and
    for months they arrived at the scan summary as the same one."""
    def fetch(srcs, **_):
        from jobradar.fetch import Result
        out = []
        for s in srcs:
            payload = ("<html>Scheduled maintenance</html>" if s.url == B
                       else _greenhouse(s))
            out.append(Result(source=s, payload=payload, status=200))
        return out

    with _tmp() as root:
        db = root / "db.db"
        _scan(_cfg(root), db, root / "out", fetch=_both)   # see above
        rc, said = _scan(_cfg(root), db, root / "out", fetch=fetch)
        # `adapters._unreadable` is a module-level accumulator and this scan
        # deliberately put a row in it. Left there it fails
        # `test_empty_boards`, two files away.
        from jobradar import adapters
        adapters.clear_unreadable()
        assert rc == 0, said
        reads = _reads(db)
        assert reads[B]["ok"] == 0, reads[B]
        assert reads[B]["why"], "a parse that raised must say what it was"
        assert reads[A]["ok"] == 1, reads[A]


def test_a_read_of_a_board_no_stored_role_came_from_is_not_logged():
    """Said out loud so it is a decision rather than a surprise.

    `_reads` exists to answer one question: was this role on the board the
    last time the board was read. For a source nothing in the database came
    from there is no such question, and on the bundled list keeping those
    rows anyway is 17,810 a run against about 1,000. It is a narrowing of the
    log, not of the rule: the moment a role from that board is stored, its
    reads start being kept.
    """
    def only_a(srcs, **_):
        from jobradar.fetch import Result
        return [Result(source=s, payload=_greenhouse(s), status=200)
                if s.url == A else Result(source=s, status=403,
                                          error="HTTP 403")
                for s in srcs]

    with _tmp() as root:
        db = root / "db.db"
        rc, said = _scan(_cfg(root), db, root / "out", fetch=only_a)
        assert rc == 0, said
        assert set(_reads(db)) == {A}, _reads(db)


def test_a_dry_run_records_nothing():
    with _tmp() as root:
        db = root / "db.db"
        rc, said = _scan(_cfg(root), db, root / "out", "--dry-run",
                         fetch=_both)
        assert rc == 0, said
        assert not db.exists(), "a dry run created the database"


# ------------------------------------------------- last_seen means listed

def test_a_posting_the_config_stopped_wanting_is_still_seen():
    """The role is on the board. The reader no longer wants it. Only one of
    those is a fact about the vacancy, and `last_seen` records that one."""
    with _tmp() as root:
        db, out = root / "db.db", root / "out"
        rc, said = _scan(_cfg(root), db, out, fetch=_both)
        assert rc == 0, said
        con = store.connect(db)
        uid = con.execute("SELECT uid FROM roles WHERE company='Aco'"
                          ).fetchone()["uid"]
        old = (date.today() - timedelta(days=9)).isoformat()
        con.execute("UPDATE roles SET last_seen=? WHERE uid=?", (old, uid))
        con.close()

        # A config that wants something else entirely. The same payload comes
        # back from the same board, and the role is screened out of it.
        rc, said = _scan(_cfg(root, titles="chief dog officer"), db, out,
                         fetch=_both)
        assert rc == 0, said
        con = store.connect(db)
        try:
            seen = con.execute("SELECT last_seen FROM roles WHERE uid=?",
                               (uid,)).fetchone()["last_seen"]
        finally:
            con.close()
        assert seen == date.today().isoformat(), seen


# -------------------------------------------------------- end to end close

def test_one_scan_closes_nothing_and_two_close_the_role_that_went():
    with _tmp() as root:
        db, out = root / "db.db", root / "out"
        rc, said = _scan(_cfg(root), db, out, fetch=_both)
        assert rc == 0, said

        con = store.connect(db)
        gone = con.execute("SELECT uid FROM roles WHERE company='Bco'"
                           ).fetchone()["uid"]
        stays = con.execute("SELECT uid FROM roles WHERE company='Aco'"
                            ).fetchone()["uid"]
        con.execute("UPDATE roles SET last_seen=? WHERE uid=?",
                    ((date.today() - timedelta(days=6)).isoformat(), gone))
        con.close()

        # Bco's board is read successfully from here on and does not list it.
        def without_b(srcs, **_):
            from jobradar.fetch import Result
            res = []
            for s in srcs:
                titles = (("Engineering Manager",) if s.url == A
                          else ("Chief Dog Officer",))
                res.append(Result(source=s, payload=_greenhouse(s, titles),
                                  status=200))
            return res

        rc, said = _scan(_cfg(root), db, out, fetch=without_b)
        assert rc == 0, said
        con = store.connect(db)
        assert store.status_of(con, gone) == "new", "one read is not evidence"
        # A day passes. Written rather than waited for: two readings on one
        # day are one day of evidence, which is the guard being exercised.
        con.execute("UPDATE source_reads SET read_on=? WHERE read_on=?",
                    ((date.today() - timedelta(days=1)).isoformat(),
                     date.today().isoformat()))
        con.close()

        rc, said = _scan(_cfg(root), db, out, fetch=without_b)
        assert rc == 0, said
        con = store.connect(db)
        try:
            assert store.status_of(con, gone) == "closed", said
            assert store.status_of(con, stays) == "new"
            note = con.execute("SELECT note FROM role_state WHERE uid=?",
                               (gone,)).fetchone()["note"]
            assert B in note and "closed automatically" in note, note
        finally:
            con.close()
        assert "marked closed" in said, said


def test_a_board_that_keeps_failing_closes_nothing_however_often():
    """The naive implementation's exact failure mode, end to end."""
    def a_only(srcs, **_):
        from jobradar.fetch import Result
        return [Result(source=s, payload=_greenhouse(s), status=200)
                if s.url == A
                else Result(source=s, status=429,
                            error="HTTP 429 retry after 57841s", throttled=True)
                for s in srcs]

    with _tmp() as root:
        db, out = root / "db.db", root / "out"
        _scan(_cfg(root), db, out, fetch=_both)
        con = store.connect(db)
        gone = con.execute("SELECT uid FROM roles WHERE company='Bco'"
                           ).fetchone()["uid"]
        con.execute("UPDATE roles SET last_seen=? WHERE uid=?",
                    ((date.today() - timedelta(days=6)).isoformat(), gone))
        con.close()

        for day in (3, 2, 1, 0):
            rc, said = _scan(_cfg(root), db, out, fetch=a_only)
            assert rc == 0, said
            con = store.connect(db)
            con.execute("UPDATE source_reads SET read_on=? WHERE read_on=?",
                        ((date.today() - timedelta(days=day)).isoformat(),
                         date.today().isoformat()))
            assert store.status_of(con, gone) == "new", day
            con.close()
        con = store.connect(db)
        try:
            assert store.status_of(con, gone) == "new"
            # Four refusals, and the one successful read from the seeding
            # scan at the top. Absence from a refusal is not absence.
            rows = con.execute("SELECT ok FROM source_reads WHERE source_key=?",
                               (B,)).fetchall()
            failed = sum(1 for r in rows if r["ok"] == 0)
            assert failed >= 4, [dict(r) for r in rows]
        finally:
            con.close()
