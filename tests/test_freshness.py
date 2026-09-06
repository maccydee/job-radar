"""Nothing may go stale quietly.

Written after an afternoon in which three scheduled jobs turned out to have
been failing for weeks, none of them noticed by looking at the tool:

  * the weekly source validation died on its first templated source, every
    Sunday since 30 August, so 17,923 boards went unchecked;
  * the weekly seed rebuild spent an hour harvesting 287,219 roles and dropped
    all of them at the upload, because launchd's PATH has no `gh`;
  * two full scans were killed part way, so `meta.last_run` still said 31
    August while the board showed roles seen that morning.

The crash is not the failure. The failure is that a stale artefact renders
exactly like a fresh one, so nothing was old until somebody happened to look.
"""

import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import freshness, store


def _ago(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


class UnknownIsNotOk(unittest.TestCase):
    """The mistake this module exists to stop repeating."""

    def test_never_done_is_its_own_state(self):
        self.assertEqual(freshness.Item("scan", "x", None).state, "unknown")

    def test_never_done_is_not_ok(self):
        # "No scan has ever finished" and "a scan finished this morning" must
        # not render the same.
        self.assertFalse(freshness.Item("scan", "x", None).ok)

    def test_never_done_reads_as_never(self):
        self.assertIn("never", freshness.Item("seed", "Seed", None).says())


class Ageing(unittest.TestCase):
    def test_fresh_is_ok(self):
        self.assertEqual(freshness.Item("scan", "x", 0).state, "ok")
        self.assertEqual(freshness.Item("scan", "x", 2).state, "ok")

    def test_past_the_limit_is_stale(self):
        self.assertEqual(freshness.Item("scan", "x", 3).state, "stale")

    def test_far_past_it_is_broken_not_merely_late(self):
        # A weekly job eight days late has missed one cycle. At fifteen it is
        # not coming back on its own, and the wording should not be the same.
        self.assertEqual(freshness.Item("seed", "x", 9).state, "stale")
        self.assertEqual(freshness.Item("seed", "x", 20).state, "broken")

    def test_each_thing_has_its_own_limit(self):
        # A scan is daily-ish; the seed and the source list are weekly.
        self.assertLess(freshness.STALE_DAYS["scan"],
                        freshness.STALE_DAYS["seed"])

    def test_the_wording_is_human_at_the_short_end(self):
        self.assertIn("today", freshness.Item("scan", "S", 0).says())
        self.assertIn("yesterday", freshness.Item("scan", "S", 1).says())
        self.assertIn("4 days ago", freshness.Item("scan", "S", 4).says())


class ParsingDates(unittest.TestCase):
    def test_a_full_timestamp(self):
        self.assertEqual(freshness._days_since(_ago(3)), 3)

    def test_a_bare_date(self):
        d = (datetime.now() - timedelta(days=5)).date().isoformat()
        self.assertEqual(freshness._days_since(d), 5)

    def test_nothing_is_none_not_zero(self):
        # Zero would read as "today", which is the whole bug.
        for junk in ("", None, "   ", "not a date", 0, []):
            self.assertIsNone(freshness._days_since(junk), repr(junk))


class AKilledScanIsVisible(unittest.TestCase):
    """The exact shape of today's failure.

    A scan that dies part way stores real roles and refreshes real dates, so
    `max(last_seen)` says today while nothing has read the whole list in a
    week. Both numbers are reported, because the gap between them IS the
    symptom.
    """

    def setUp(self):
        self.con = store.connect(":memory:")

    def tearDown(self):
        self.con.close()

    def _role(self, last_seen):
        self.con.execute(
            "INSERT INTO roles (uid,company,title,url,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (last_seen + "-x", "Acme", "Engineering Manager", "u",
             last_seen, last_seen))

    def test_a_stale_last_run_with_fresh_roles_is_called_out(self):
        store.set_meta(self.con, "last_run", _ago(6))
        self._role(datetime.now().date().isoformat())
        item = freshness._scan_item(self.con)
        self.assertEqual(item.state, "broken")
        self.assertIn("dying before the end", item.detail)

    def test_a_healthy_scan_says_nothing_extra(self):
        store.set_meta(self.con, "last_run", _ago(0))
        self._role(datetime.now().date().isoformat())
        item = freshness._scan_item(self.con)
        self.assertEqual(item.state, "ok")
        self.assertEqual(item.detail, "")

    def test_no_scan_ever_is_unknown_not_ok(self):
        item = freshness._scan_item(self.con)
        self.assertEqual(item.state, "unknown")


class SeedAgeComesFromTheShards(unittest.TestCase):
    """The date a builder writes is what it MEANT to publish. The shards are
    what it actually produced, and a build that fell over at the upload leaves
    both."""

    def test_no_directory_is_unknown(self):
        item = freshness._seed_item(Path(mkdtemp()) / "nope")
        self.assertEqual(item.state, "unknown")

    def test_an_empty_directory_is_unknown_not_fresh(self):
        d = Path(mkdtemp())
        item = freshness._seed_item(d)
        self.assertEqual(item.state, "unknown")

    def test_shards_give_the_age(self):
        d = Path(mkdtemp())
        p = d / "UK.jsonl.gz"
        p.write_bytes(b"x")
        old = time.time() - 20 * 86400
        import os
        os.utime(p, (old, old))
        item = freshness._seed_item(d)
        self.assertEqual(item.days, 20)
        self.assertEqual(item.state, "broken")

    def test_a_broken_index_does_not_break_the_check(self):
        d = Path(mkdtemp())
        (d / "UK.jsonl.gz").write_bytes(b"x")
        (d / "index.json").write_text("{not json", encoding="utf-8")
        item = freshness._seed_item(d)
        self.assertIsNotNone(item.days)


class TheReport(unittest.TestCase):
    def test_the_worst_thing_comes_first(self):
        items = [freshness.Item("a", "A", 0), freshness.Item("b", "B", 999),
                 freshness.Item("c", "C", None)]
        items.sort(key=lambda i: ({"broken": 0, "unknown": 1, "stale": 2,
                                   "ok": 3}[i.state], -(i.days or 0)))
        self.assertEqual(items[0].label, "B")

    def test_worst_of_nothing_wrong_is_ok(self):
        self.assertEqual(freshness.worst([freshness.Item("a", "A", 0)]), "ok")

    def test_worst_prefers_broken_over_stale(self):
        self.assertEqual(
            freshness.worst([freshness.Item("seed", "A", 9),
                             freshness.Item("seed", "B", 99)]), "broken")

    def test_unknown_outranks_stale(self):
        # Something nobody has ever done is a bigger question than something
        # done a bit too long ago.
        self.assertEqual(
            freshness.worst([freshness.Item("seed", "A", 9),
                             freshness.Item("seed", "B", None)]), "unknown")

    def test_a_report_without_a_database_still_answers(self):
        # A fresh checkout has no database, and that is not a failure of this.
        items = freshness.report(None, seed_path=Path(mkdtemp()))
        self.assertTrue(items)
        self.assertNotIn("scan", {i.key for i in items})


class TheCommand(unittest.TestCase):
    def test_doctor_is_on_the_real_parser(self):
        from jobradar.cli import build_parser
        args = build_parser().parse_args(["doctor"])
        self.assertTrue(callable(args.func))

    def test_it_exits_non_zero_when_something_is_stale(self):
        # So a wrapper or a launchd job can act on it without parsing prose.
        import ast
        src = (Path(__file__).parent.parent / "jobradar" / "cli.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "cmd_doctor")
        returns = {n.value.value for n in ast.walk(fn)
                   if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)}
        self.assertIn(0, returns)
        self.assertIn(1, returns)


class TheDashboardSaysIt(unittest.TestCase):
    def _board(self, last_run, rows=1):
        con = store.connect(":memory:")
        store.set_meta(con, "last_run", last_run)
        today = datetime.now().date().isoformat()
        for i in range(rows):
            con.execute(
                "INSERT INTO roles (uid,company,title,url,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?)",
                (f"u{i}", "Acme", "Engineering Manager",
                 f"https://e.invalid/{i}", today, today))
        return con

    def test_a_stale_board_shows_a_badge(self):
        from jobradar.output import interactive
        con = self._board(_ago(30))
        html = interactive.render(con, "GBP")
        con.close()
        self.assertIn("last completed scan", html)

    def test_a_fresh_board_does_not_nag(self):
        # A permanent badge saying everything is fine becomes furniture and
        # then it is not read at all.
        from jobradar.output import interactive
        con = self._board(_ago(0))
        html = interactive.render(con, "GBP")
        con.close()
        self.assertNotIn("last completed scan", html)

    def test_a_checkout_that_has_never_scanned_is_not_nagged_twice(self):
        # The empty state already explains, at length, that no scan has run.
        # A second message about the same absence is one too many.
        from jobradar.output import interactive
        con = store.connect(":memory:")
        html = interactive.render(con, "GBP")
        con.close()
        self.assertNotIn("last completed scan", html)

    def test_the_badge_does_not_wear_the_source_lists_class(self):
        # `.sync.warn` belongs to the source-list badge and a test asserts on
        # it exactly. Two elements wearing one class made that test unable to
        # tell them apart.
        from jobradar.output import interactive
        con = self._board(_ago(30))
        html = interactive.render(con, "GBP")
        con.close()
        self.assertIn("sync agecheck", html)


if __name__ == "__main__":
    unittest.main()
