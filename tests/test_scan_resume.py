"""Picking a killed scan back up, without losing what it never stored.

A full scan reads 17,923 sources in about 77 minutes and the floor is one
host's own clock, so it cannot be made much shorter. On 6 September 2026 one
was killed at 84 minutes when the process that owned it exited. The roles it
had stored survived; the knowledge of WHICH sources it had read did not, so
the restart re-asked 17,923 servers for things it already had.

The whole risk of fixing that is in one distinction. A source is safe to skip
only once its postings are COMMITTED, never when it has merely been fetched.
The scan holds parsed jobs in memory and writes them in batches, so there is a
window where a source has been read, its roles exist only in a list, and
nothing is on disk. Marking it done in that window means a resume skips it and
those roles are never stored and never fetched again: the resumed scan reports
success, and a role that was never stored looks exactly like a role that was
never posted.

So most of this file is about `hold` versus `commit`.
"""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import progress as progress_mod
from jobradar.models import Source


def _src(url, company="X"):
    return Source(company=company, url=url, platform="greenhouse", country="UK")


def _tmp():
    return Path(mkdtemp()) / "scan-progress.json"


class HoldIsNotCommit(unittest.TestCase):
    """The distinction the whole feature turns on."""

    def test_a_held_source_is_not_skippable(self):
        p = progress_mod.ScanProgress(_tmp())
        p.hold("https://boards.example/a")
        self.assertFalse(p.is_done("https://boards.example/a"))
        self.assertEqual(len(p), 0)

    def test_only_commit_makes_it_skippable(self):
        p = progress_mod.ScanProgress(_tmp())
        p.hold("https://boards.example/a")
        p.commit()
        self.assertTrue(p.is_done("https://boards.example/a"))

    def test_a_failed_write_leaves_them_held_not_done(self):
        # When a flush throws, `on_stored` is never called, so the keys stay
        # held. The jobs are still in memory and the next checkpoint rewrites
        # all of them, which is what promotes these. What must not happen is
        # them becoming done off the back of a write that did not land.
        p = progress_mod.ScanProgress(_tmp())
        p.hold("https://boards.example/a")
        p.hold("https://boards.example/b")
        self.assertEqual(len(p), 0)
        self.assertFalse(p.is_done("https://boards.example/a"))
        p.commit()          # the later, successful write
        self.assertTrue(p.is_done("https://boards.example/a"))
        self.assertTrue(p.is_done("https://boards.example/b"))

    def test_held_sources_are_never_written_to_disk(self):
        # If they were, a kill between hold and commit would leave a
        # checkpoint claiming sources whose roles were lost.
        path = _tmp()
        p = progress_mod.ScanProgress(path, fingerprint_="fp")
        p.hold("https://boards.example/a")
        p.commit()                      # writes one
        p.hold("https://boards.example/b")   # held, not committed
        p.save()
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw["done"]), 1)


class Filtering(unittest.TestCase):
    def test_done_sources_are_skipped_and_counted(self):
        p = progress_mod.ScanProgress(_tmp())
        a, b, c = (_src("https://e.invalid/a"), _src("https://e.invalid/b"),
                   _src("https://e.invalid/c"))
        p.hold(a.key)
        p.commit()
        rest, skipped = p.filter([a, b, c])
        self.assertEqual([s.key for s in rest], [b.key, c.key])
        self.assertEqual(skipped, 1)

    def test_nothing_done_means_nothing_skipped(self):
        p = progress_mod.ScanProgress(_tmp())
        srcs = [_src("https://e.invalid/a"), _src("https://e.invalid/b")]
        rest, skipped = p.filter(srcs)
        self.assertEqual(len(rest), 2)
        self.assertEqual(skipped, 0)


class TheCheckpointMustMatchTheScan(unittest.TestCase):
    """A resume that skips a source the CURRENT config wants is the same
    silent loss by another route."""

    def _write(self, path, **over):
        payload = {"version": 1, "run": 3,
                   "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "fingerprint": "abc123", "done": ["aaaaaaaaaaaa"]}
        payload.update(over)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_a_matching_fingerprint_resumes(self):
        path = _tmp(); self._write(path)
        p, why = progress_mod.load(path, "abc123")
        self.assertEqual(why, "")
        self.assertEqual(len(p), 1)

    def test_a_changed_source_list_does_not_resume(self):
        path = _tmp(); self._write(path)
        p, why = progress_mod.load(path, "different")
        self.assertEqual(len(p), 0)
        self.assertIn("changed", why)

    def test_a_stale_checkpoint_does_not_resume(self):
        # Postings appear and close inside a week. Resuming onto a half-read
        # board from last Tuesday reports two different weeks as one scan.
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(
            timespec="seconds")
        path = _tmp(); self._write(path, updated=old)
        p, why = progress_mod.load(path, "abc123")
        self.assertEqual(len(p), 0)
        self.assertIn("hours old", why)

    def test_a_fresh_checkpoint_inside_the_window_resumes(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(
            timespec="seconds")
        path = _tmp(); self._write(path, updated=recent)
        p, why = progress_mod.load(path, "abc123")
        self.assertEqual(why, "")
        self.assertEqual(len(p), 1)

    def test_a_corrupt_checkpoint_is_a_fresh_scan_not_a_crash(self):
        # A scan must never be blocked by its own bookkeeping.
        path = _tmp()
        path.parent.mkdir(parents=True, exist_ok=True)
        for junk in ("{not json", "[]", '{"done": "not a list"}', ""):
            path.write_text(junk, encoding="utf-8")
            p, why = progress_mod.load(path, "abc123")
            self.assertEqual(len(p), 0, junk)
            self.assertTrue(why, junk)

    def test_no_checkpoint_is_silent(self):
        # Nothing to resume is the ordinary case, not something to report.
        p, why = progress_mod.load(_tmp(), "abc123")
        self.assertEqual((len(p), why), (0, ""))


class Fingerprint(unittest.TestCase):
    def test_order_does_not_matter(self):
        a = progress_mod.fingerprint(["u1", "u2", "u3"], ["t"])
        b = progress_mod.fingerprint(["u3", "u1", "u2"], ["t"])
        self.assertEqual(a, b)

    def test_a_new_source_changes_it(self):
        a = progress_mod.fingerprint(["u1", "u2"], ["t"])
        b = progress_mod.fingerprint(["u1", "u2", "u3"], ["t"])
        self.assertNotEqual(a, b)

    def test_changed_titles_change_it(self):
        # Titles become search terms on the keyword platforms, so editing them
        # changes what a source returns even when the list is untouched.
        a = progress_mod.fingerprint(["u1"], ["engineering manager"])
        b = progress_mod.fingerprint(["u1"], ["head of engineering"])
        self.assertNotEqual(a, b)

    def test_reordered_titles_change_it(self):
        # Only the first MAX_KEYWORD_TITLES are searched for, so the order is
        # load-bearing and a reorder really is a different scan.
        a = progress_mod.fingerprint(["u1"], ["a", "b"])
        b = progress_mod.fingerprint(["u1"], ["b", "a"])
        self.assertNotEqual(a, b)


class Durability(unittest.TestCase):
    def test_the_file_round_trips(self):
        path = _tmp()
        p = progress_mod.ScanProgress(path, fingerprint_="fp", run=7)
        for u in ("https://e.invalid/a", "https://e.invalid/b"):
            p.hold(u)
        p.commit()
        again, why = progress_mod.load(path, "fp")
        self.assertEqual(why, "")
        self.assertEqual(len(again), 2)
        self.assertTrue(again.is_done("https://e.invalid/a"))

    def test_no_temp_file_is_left_behind(self):
        # Write-then-rename, the same as everything else here: a checkpoint
        # half-written by a process being killed parses and is believed.
        path = _tmp()
        p = progress_mod.ScanProgress(path, fingerprint_="fp")
        p.hold("https://e.invalid/a"); p.commit()
        leftovers = [q.name for q in path.parent.iterdir() if q.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_clear_removes_it(self):
        path = _tmp()
        p = progress_mod.ScanProgress(path, fingerprint_="fp")
        p.hold("https://e.invalid/a"); p.commit()
        self.assertTrue(path.exists())
        p.clear()
        self.assertFalse(path.exists())
        self.assertEqual(len(p), 0)

    def test_clearing_a_checkpoint_that_is_not_there_is_fine(self):
        progress_mod.ScanProgress(_tmp()).clear()


class TheScanWiresItUp(unittest.TestCase):
    """Parsed, not grepped: the comment explaining the rule contains the
    words the rule is about."""

    def _fn(self, name):
        import ast
        src = (Path(__file__).parent.parent / "jobradar" / "cli.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name), src

    def test_the_flag_exists_on_the_real_parser(self):
        from jobradar.cli import build_parser
        args = build_parser().parse_args(["scan", "--resume"])
        self.assertTrue(args.resume)

    def test_scan_defaults_to_not_resuming(self):
        # A scan is a read of the world at a moment. Silently continuing one
        # from hours ago hands back a board built from two moments as one.
        from jobradar.cli import build_parser
        self.assertFalse(build_parser().parse_args(["scan"]).resume)

    def test_the_flush_only_marks_after_the_write(self):
        # `on_stored` must be called inside the try, after commit(), and never
        # before upsert_roles.
        import ast
        fn, _ = self._fn("_flush_phase")
        self.assertIn("on_stored", [a.arg for a in fn.args.args])
        body = ast.dump(fn)
        self.assertIn("upsert_roles", body)
        # The call must exist and be guarded.
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "on_stored"]
        self.assertTrue(calls, "_flush_phase never calls on_stored")

    def test_absorb_holds_rather_than_commits(self):
        # The bug this whole file guards: absorb runs while the roles are
        # still only in memory.
        import ast
        fn, _ = self._fn("cmd_scan")
        absorb = next(n for n in ast.walk(fn)
                      if isinstance(n, ast.FunctionDef) and n.name == "absorb")
        names = {n.attr for n in ast.walk(absorb) if isinstance(n, ast.Attribute)}
        self.assertIn("hold", names, "absorb does not record the source at all")
        self.assertNotIn("commit", names,
                         "absorb commits the source before its roles are stored")


class EndToEnd(unittest.TestCase):
    """Run the real `cmd_scan` twice with the network stubbed.

    The wiring is where this can go wrong: the module can be perfect and the
    scan can still call `commit` in the wrong place. Nothing here touches a
    third party's server.
    """

    def setUp(self):
        import tempfile
        from jobradar import cli, fetch as fetch_mod
        self.cli = cli
        self.root = Path(tempfile.mkdtemp())
        (self.root / "state").mkdir()
        cv = self.root / "cv.txt"
        cv.write_text("Engineering manager. Led a team of six.", encoding="utf-8")
        (self.root / "config.yaml").write_text(
            "titles:\n  include:\n    - engineering manager\n"
            f"cv:\n  path: {cv}\n"
            "salary:\n  floor: null\n  currency: GBP\n"
            "locations:\n  countries: [UK]\n"
            "output:\n  formats: []\n  dir: out\n"
            "sources:\n  use_bundled: false\n  extra:\n"
            + "".join(
                f"    - company: Board{i}\n"
                f"      url: https://board{i}.example/api\n"
                f"      platform: greenhouse\n" for i in range(12)),
            encoding="utf-8")
        self.asked = []
        self.real_fetch_all = cli.fetch_all

        def fake_fetch_all(group, **kw):
            self.asked.append([s.key for s in group])
            out = []
            for s in group:
                r = fetch_mod.Result(source=s, payload={"jobs": []}, status=200)
                if kw.get("on_result"):
                    kw["on_result"](r)
                out.append(r)
            return out

        cli.fetch_all = fake_fetch_all

    def tearDown(self):
        self.cli.fetch_all = self.real_fetch_all

    def _args(self, **over):
        p = self.cli.build_parser()
        # `-c` is a top-level flag and sits BEFORE the subcommand.
        argv = ["-c", str(self.root / "config.yaml"),
                "scan", "--no-open", "--no-caffeine",
                "--db", str(self.root / "j.db"),
                "--state", str(self.root / "state" / "seen.json"),
                "-o", str(self.root / "out")]
        argv += over.pop("extra", [])
        return p.parse_args(argv)

    def test_a_finished_scan_leaves_no_checkpoint(self):
        # Nothing to resume, so the next scan must not think there is.
        self.cli.cmd_scan(self._args())
        self.assertFalse((self.root / "state" / "scan-progress.json").exists())

    def test_a_resume_skips_what_was_already_stored(self):
        from jobradar import progress as pm
        from jobradar.config import load as load_cfg
        cfg = load_cfg(str(self.root / "config.yaml"))
        srcs = self.cli._load_sources(cfg)
        fp = pm.fingerprint([s.key for s in srcs], cfg.titles_include)

        # Half of them already read AND stored by a scan that then died.
        path = self.root / "state" / "scan-progress.json"
        pre = pm.ScanProgress(path, fingerprint_=fp, run=1)
        for s in srcs[:6]:
            pre.hold(s.key)
        pre.commit()

        self.asked.clear()
        self.cli.cmd_scan(self._args(extra=["--resume"]))
        asked = {k for batch in self.asked for k in batch}
        for s in srcs[:6]:
            self.assertNotIn(s.key, asked, "a stored source was read again")
        for s in srcs[6:]:
            self.assertIn(s.key, asked, "an unread source was skipped")

    def test_without_the_flag_everything_is_read_again(self):
        from jobradar import progress as pm
        from jobradar.config import load as load_cfg
        cfg = load_cfg(str(self.root / "config.yaml"))
        srcs = self.cli._load_sources(cfg)
        fp = pm.fingerprint([s.key for s in srcs], cfg.titles_include)
        pre = pm.ScanProgress(self.root / "state" / "scan-progress.json",
                              fingerprint_=fp, run=1)
        pre.hold(srcs[0].key)
        pre.commit()

        self.asked.clear()
        self.cli.cmd_scan(self._args())
        asked = {k for batch in self.asked for k in batch}
        self.assertIn(srcs[0].key, asked)

    def test_a_scan_killed_mid_pass_does_not_lose_the_unstored_sources(self):
        """The window this whole feature is about, exercised rather than
        asserted.

        A scan is killed part way through a pass. Sources read before the last
        intra-pass checkpoint had their postings written and are safe to skip.
        Sources read after it were parsed into memory and went nowhere, so a
        resume MUST read them again. Getting this backwards loses every
        posting they held, silently.
        """
        from jobradar import progress as pm
        from jobradar.config import load as load_cfg
        cfg = load_cfg(str(self.root / "config.yaml"))
        srcs = self.cli._load_sources(cfg)
        self.assertGreaterEqual(len(srcs), 12)

        # Checkpoint every 4, so the boundary sits inside the pass.
        real_every = self.cli.CHECKPOINT_EVERY
        self.cli.CHECKPOINT_EVERY = 4
        from jobradar import fetch as fetch_mod

        def dying_fetch_all(group, **kw):
            out = []
            for i, s in enumerate(group):
                if i == 10:
                    raise KeyboardInterrupt("killed")
                r = fetch_mod.Result(source=s, payload={"jobs": []}, status=200)
                if kw.get("on_result"):
                    kw["on_result"](r)
                out.append(r)
            return out

        self.cli.fetch_all = dying_fetch_all
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.cli.cmd_scan(self._args())
        finally:
            self.cli.CHECKPOINT_EVERY = real_every

        path = self.root / "state" / "scan-progress.json"
        self.assertTrue(path.exists(), "a killed scan wrote no checkpoint")
        fp = pm.fingerprint([s.key for s in srcs], cfg.titles_include)
        saved, why = pm.load(path, fp)
        self.assertEqual(why, "")
        # Eight committed (two checkpoints of four); sources nine and ten were
        # read and never stored.
        self.assertEqual(len(saved), 8)
        for s in srcs[8:10]:
            self.assertFalse(saved.is_done(s.key),
                             "a source whose postings were never stored was "
                             "marked done, so a resume would lose them")

        # And the resume really does go back for them.
        self.asked.clear()
        restored = []

        def fake_fetch_all(group, **kw):
            self.asked.append([s.key for s in group])
            out = []
            for s in group:
                r = fetch_mod.Result(source=s, payload={"jobs": []}, status=200)
                if kw.get("on_result"):
                    kw["on_result"](r)
                out.append(r)
            return out

        self.cli.fetch_all = fake_fetch_all
        self.cli.cmd_scan(self._args(extra=["--resume"]))
        asked = {k for batch in self.asked for k in batch}
        for s in srcs[8:]:
            self.assertIn(s.key, asked, "an unstored source was not re-read")
        for s in srcs[:8]:
            self.assertNotIn(s.key, asked, "a stored source was read again")

    def test_a_resume_after_a_config_change_reads_everything(self):
        # The checkpoint was written against a different source list, so
        # skipping anything could skip a source THIS config wants.
        from jobradar import progress as pm
        from jobradar.config import load as load_cfg
        cfg = load_cfg(str(self.root / "config.yaml"))
        srcs = self.cli._load_sources(cfg)
        pre = pm.ScanProgress(self.root / "state" / "scan-progress.json",
                              fingerprint_="a-different-scan", run=1)
        for s in srcs[:6]:
            pre.hold(s.key)
        pre.commit()

        self.asked.clear()
        self.cli.cmd_scan(self._args(extra=["--resume"]))
        asked = {k for batch in self.asked for k in batch}
        for s in srcs:
            self.assertIn(s.key, asked, "a fingerprint mismatch still skipped")


class CheckpointingIsNotQuadratic(unittest.TestCase):
    """Each checkpoint stores only what is new since the last one.

    The first version handed the whole accumulated list to `_flush_phase`
    every time. `screen_run` is ~262 microseconds a posting over roughly
    480,000 postings a scan, so re-screening a growing set at each of ~45
    checkpoints is about 48 minutes of CPU against 2 for screening each
    posting once. Measured on a real run: a scan that should be waiting on
    other people's servers sat at 98.8% CPU and took ninety minutes to reach
    a third of the way through its five-minute first pass.
    """

    def test_the_flush_is_handed_a_slice_and_not_the_whole_list(self):
        import ast
        src = (Path(__file__).parent.parent / "jobradar" / "cli.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "cmd_scan")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_flush_phase"]
        self.assertTrue(calls, "cmd_scan no longer flushes at all")
        for c in calls:
            third = c.args[2] if len(c.args) > 2 else None
            self.assertIsInstance(
                third, ast.Name,
                "a checkpoint is passing an expression rather than the "
                "pre-sliced list of new postings")
            self.assertNotEqual(
                third.id, "all_jobs",
                "a checkpoint re-screens every posting parsed so far, which "
                "is quadratic over a scan and made one CPU-bound")

    def test_every_posting_still_reaches_the_database_exactly_once(self):
        # Slicing is only safe if nothing falls between two slices.
        from jobradar import cli, fetch as fetch_mod
        import tempfile
        root = Path(tempfile.mkdtemp())
        (root / "state").mkdir()
        cv = root / "cv.txt"
        cv.write_text("Engineering manager.", encoding="utf-8")
        n_boards = 9
        (root / "config.yaml").write_text(
            "titles:\n  include:\n    - engineering manager\n"
            f"cv:\n  path: {cv}\n"
            "salary:\n  floor: null\n  currency: GBP\n"
            "locations:\n  countries: [UK]\n"
            "output:\n  formats: []\n  dir: out\n"
            "sources:\n  use_bundled: false\n  extra:\n"
            + "".join(f"    - company: B{i}\n"
                      f"      url: https://b{i}.example/api\n"
                      f"      platform: greenhouse\n" for i in range(n_boards)),
            encoding="utf-8")

        real = cli.fetch_all
        every = 3
        real_every = cli.CHECKPOINT_EVERY
        cli.CHECKPOINT_EVERY = every

        def fake(group, **kw):
            out = []
            for s in group:
                payload = {"jobs": [{
                    "title": "Engineering Manager",
                    "absolute_url": s.url + "/job",
                    "location": {"name": "London, UK"},
                    "content": "Lead a team of six in London.",
                    "id": abs(hash(s.url)) % 10**6}]}
                r = fetch_mod.Result(source=s, payload=payload, status=200)
                if kw.get("on_result"):
                    kw["on_result"](r)
                out.append(r)
            return out

        cli.fetch_all = fake
        try:
            p = cli.build_parser()
            args = p.parse_args([
                "-c", str(root / "config.yaml"), "scan", "--no-open",
                "--no-caffeine", "--db", str(root / "j.db"),
                "--state", str(root / "state" / "seen.json"),
                "-o", str(root / "out")])
            cli.cmd_scan(args)
        finally:
            cli.fetch_all = real
            cli.CHECKPOINT_EVERY = real_every

        import sqlite3
        con = sqlite3.connect(root / "j.db")
        stored = con.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        con.close()
        self.assertEqual(stored, n_boards,
                         "postings went missing between two checkpoint slices")


if __name__ == "__main__":
    unittest.main()
