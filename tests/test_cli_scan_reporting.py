"""Two ways a scan reported something other than what happened.

The first is a cost: an `output.dir` this user cannot write is knowable before
the first request, and was discovered at the last one. A 77-minute run over
17,811 boards ended in a `PermissionError` traceback out of
`atomic_write_text`, which reads like the scan itself was lost. It was not:
everything found had been committed to the database an hour earlier, and
nothing on the screen said so.

The second is worse, because it looks fine. Two scans over the same database
is not an exotic case, it is a cron scan and somebody running one by hand.
Both read the same 250 roles on a fresh database. The first said "this is the
first scan, so all of them are new". The second said `250 match your config,
0 new`. No lock error, no corruption, no warning: just a run number read at
one moment, stamped at another, and queried at a third.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli, store
from jobradar.cli import main
from jobradar.models import Job


# --------------------------------------------------------------- the harness


def _cfg(tmp: Path, n_sources: int = 2) -> Path:
    cfg = tmp / "config.yaml"
    extra = "".join(
        f"    - {{company: C{i}, platform: greenhouse, "
        f"url: 'https://c{i}.invalid/x'}}\n" for i in range(n_sources))
    cfg.write_text(
        "titles:\n  include: ['engineering manager']\n"
        "locations:\n  countries: ['UK']\n"
        "sources:\n  use_bundled: false\n  extra:\n" + extra,
        encoding="utf-8")
    return cfg


def _payload(src, n=1):
    """A real Greenhouse list response, small."""
    return {"jobs": [
        {"title": "Engineering Manager",
         "absolute_url": f"{src.url}/job/{i}",
         "location": {"name": "London, United Kingdom"},
         "content": "We are hiring an engineering manager. " + "detail " * 60}
        for i in range(n)]}


def _results(srcs, **_):
    from jobradar.fetch import Result
    return [Result(source=s, payload=_payload(s), status=200) for s in srcs]


def _scan(cfg, db, out, *extra, fetch=_results):
    """Always with its own `--state`.

    Without it `args.state` is None, `State(None)` falls back to
    `state/seen.json` RELATIVE TO THE WORKING DIRECTORY, and the suite runs
    from the repo root. So these tests were writing the developer's real
    seen-set on every run, and once scans learned to checkpoint they deleted
    the real `state/scan-progress.json` too, which is how it was noticed: a
    resumable scan lost its resume point to the test suite.
    """
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
        for p in root.rglob("*"):
            with contextlib.suppress(OSError):
                if p.is_dir():
                    p.chmod(0o755)
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------ the output directory, first

def test_an_unwritable_output_directory_stops_the_scan_before_it_starts():
    """The whole point. This used to be an hour of somebody else's servers
    followed by a traceback, for a fact that was true when they pressed
    enter."""
    if _cannot_make_a_directory_unwritable():
        return
    with _tmp() as root:
        ro = root / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            rc, out = _scan(_cfg(root), root / "db.db", ro / "dash")
        finally:
            ro.chmod(0o755)
        assert rc == 1, out
        assert "Traceback" not in out
        assert "output directory" in out, out

        # The other side of it, here so it cannot be dropped separately:
        # `--dry-run` prints "out/ was left alone" and means it, so refusing
        # one over a directory it was never going to touch would be inventing
        # a failure rather than reporting one.
        ro.chmod(0o500)
        try:
            rc, out = _scan(_cfg(root), root / "db.db", ro / "dash",
                            "--dry-run")
        finally:
            ro.chmod(0o755)
        assert rc == 0, out
        assert "left alone" in out, out


def test_the_output_check_runs_before_a_single_request():
    """A check that fires after the reading has saved nobody anything. Proved
    by a fetch that raises: if it is reached at all, this test fails."""
    if _cannot_make_a_directory_unwritable():
        return

    def explode(*a, **k):
        raise AssertionError("the scan fetched before checking where it would "
                             "write")

    with _tmp() as root:
        ro = root / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            rc, out = _scan(_cfg(root), root / "db.db", ro / "dash",
                            fetch=explode)
        finally:
            ro.chmod(0o755)
        assert rc == 1, out


def test_a_file_where_the_output_directory_should_be_is_named_as_one():
    """`output.dir: out` with a FILE called `out` beside it. mkdir raises
    FileExistsError, which says nothing about which of the two things it
    found."""
    with _tmp() as root:
        blocker = root / "out"
        blocker.write_text("not a directory", encoding="utf-8")
        why = cli.out_dir_problem(blocker)
        assert "is a file, not a directory" in why, why


def test_a_write_that_fails_at_the_end_says_where_the_roles_are():
    """The pre-flight check cannot cover an hour: a disk fills, a share drops,
    somebody changes a mode. What must not happen again is a bare traceback as
    the last act of a long run, with no word that everything it found is
    already stored and readable."""
    with _tmp() as root:
        db = root / "roles.db"
        with mock.patch("jobradar.cli.output.html_out.write",
                        side_effect=OSError("No space left on device")):
            rc, out = _scan(_cfg(root), db, root / "dash")
        assert "Traceback" not in out
        assert rc == 1, out
        assert "nothing was lost" in out, out
        assert "job-radar serve" in out, out
        # And it has to be true. The claim is only worth making because the
        # roles really are in the database at that point.
        con = store.connect(db)
        try:
            n = con.execute("SELECT COUNT(*) c FROM roles").fetchone()["c"]
        finally:
            con.close()
        assert n > 0, "the message promised roles that are not there"


# --------------------------------------------------- two scans at once

def _jobs(n=250):
    return [Job(company=f"C{i}", title="Engineering Manager",
                url=f"https://c{i}.invalid/j/1", platform="greenhouse",
                location="London") for i in range(n)]


def test_two_scans_over_the_same_roles_do_not_report_zero_new():
    """The exact interleave, at the layer it happened in. Scan A writes the
    roles and bumps the counter; scan B, which started at the same moment,
    then asks what was new on its run and is told nothing was, because it
    stamped `first_run=1` and went looking for `first_run=2`."""
    with _tmp() as root:
        db = root / "race.db"
        jobs = _jobs(12)
        uids = [j.uid for j in jobs]
        a = store.connect(db)
        b = store.connect(db)
        try:
            # Both scans pin their run number when they open the database,
            # which on a fresh one is the same number for both.
            run_a = store.current_run(a) + 1
            run_b = store.current_run(b) + 1
            store.upsert_roles(a, jobs, run=run_a)
            store.upsert_roles(b, jobs, run=run_b)     # all seen already
            new_a = store.new_since_last_run(a, uids, run=run_a)
            first_a = run_a == 1
            store.bump_runs(a)
            new_b = store.new_since_last_run(b, uids, run=run_b)
            first_b = run_b == 1
            store.bump_runs(b)
        finally:
            a.close()
            b.close()
        # Neither is allowed to be the pair "not the first scan, and nothing
        # new", which is the sentence that hid 250 roles.
        assert not (new_a == set() and not first_a), "scan A reported 0 new"
        assert not (new_b == set() and not first_b), "scan B reported 0 new"


def test_a_scan_finishing_underneath_another_one_does_not_silence_it():
    """The same thing through the command, because the bug was in when the
    counter was read rather than in any one function. `fetch_all` here runs a
    whole second scan to completion, which is what a cron job overlapping a
    manual run looks like from inside the manual one."""
    with _tmp() as root:
        db = root / "shared.db"
        cfg = _cfg(root)
        other = {"ran": False}

        def fetch_then_let_the_other_scan_finish(srcs, **k):
            if not other["ran"]:
                other["ran"] = True
                _scan(cfg, db, root / "dash-a")
            return _results(srcs, **k)

        rc, out = _scan(cfg, db, root / "dash-b",
                        fetch=fetch_then_let_the_other_scan_finish)
        assert other["ran"], "the overlapping scan never ran"
        assert rc == 0, out
        assert ", 0 new" not in out, out


def test_the_run_counter_does_not_lose_an_increment():
    """`bump_runs` was `read() + 1` then `write()`, so two scans finishing at
    once left the counter one higher rather than two. Simulated exactly: the
    other scan bumps in the window between this one's read and its write."""
    with _tmp() as root:
        db = root / "counter.db"
        a = store.connect(db)
        b = store.connect(db)
        real_get_meta = store.get_meta
        fired = []

        def racing_get_meta(con, k, default=None):
            v = real_get_meta(con, k, default)
            if k == "runs" and con is a and not fired:
                fired.append(True)
                store.bump_runs(b)      # the other scan finishes right here
            return v

        store.get_meta = racing_get_meta
        try:
            store.bump_runs(a)
        finally:
            store.get_meta = real_get_meta
            a.close()
            b.close()
        con = store.connect(db)
        try:
            assert store.current_run(con) == 2, (
                f"two scans bumped and the counter says "
                f"{store.current_run(con)}")
        finally:
            con.close()


def _cannot_make_a_directory_unwritable() -> bool:
    """Whether this platform can even set up the case under test.

    Two ways it cannot. On Windows a directory mode is not a write
    permission: `chmod(0o500)` sets the read-only attribute, which NTFS
    ignores for creating files inside, so the "unwritable" directory is
    perfectly writable and the test asserts against a thing that did not
    happen. And root writes anywhere regardless of the mode.

    Checked by trying it rather than by naming platforms, so a filesystem
    that behaves differently is handled by what it does, not by what its
    operating system usually does. `os.geteuid` is not used: it does not
    exist on Windows, and every one of these tests died on the
    AttributeError before reaching its own subject.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        ro = pathlib.Path(d) / "ro"
        ro.mkdir()
        try:
            ro.chmod(0o500)
            (ro / "probe").write_text("x", encoding="utf-8")
        except OSError:
            return False        # genuinely refused: the case is testable
        finally:
            try:
                ro.chmod(0o700)
            except OSError:
                pass
        return True             # the write went through, so there is no case
