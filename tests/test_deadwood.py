"""An empty board is a company with nothing open, not a dead one.

Measured on 12 September 2026, against a validate report from five days
earlier. 357 of its 363 "dead" rows said `no postings returned`: HTTP 200, the
board answered, nothing open that day. Not a timeout, not a 403, no transport
failure.

Re-checked five days later, 2 of 25 sampled were serving jobs again, and a
targeted check found Contentful with six and Parallelz with one. Around 8%,
so roughly 28 of 355 would have been deleted from a list that ships to
everybody, while perfectly alive.

They had not died and recovered. They were companies with nothing open on a
Sunday. A thirty-person employer between hires and an abandoned board return
byte-identical answers and no single reading can tell them apart.

This repo already separates "could not read the board" from "read it and it
was empty". What it did not separate is "empty today" from "gone".
"""

import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import deadwood

URL = "https://boards.example/api/jobs"


def _day(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _log():
    return deadwood.EmptyLog(Path(mkdtemp()) / "empty-since.json")


class OneQuietSundayDeletesNothing(unittest.TestCase):
    def test_a_board_empty_once_is_not_settled(self):
        log = _log()
        log.record(URL, True)
        self.assertFalse(log.settled(URL))

    def test_a_board_never_seen_is_not_settled(self):
        self.assertFalse(_log().settled(URL))

    def test_the_reason_is_reportable(self):
        log = _log()
        log.record(URL, True)
        self.assertIn("1 of the 3", log.why_kept(URL))


class BothThresholdsMustBeMet(unittest.TestCase):
    """Runs and days, not either.

    A maintainer re-running `validate` three times in an hour satisfies a run
    count while learning nothing. The day count carries the evidence.
    """

    def test_enough_runs_but_not_enough_days(self):
        log = _log()
        for n in (2, 1, 0):
            log.record(URL, True, when=_day(n))
        self.assertEqual(log.runs(URL), 3)
        self.assertFalse(log.settled(URL))
        self.assertIn("days", log.why_kept(URL))

    def test_enough_days_but_not_enough_runs(self):
        log = _log()
        log.record(URL, True, when=_day(60))
        log.record(URL, True, when=_day(0))
        self.assertGreaterEqual(log.days_empty(URL), deadwood.MIN_DAYS)
        self.assertEqual(log.runs(URL), 2)
        self.assertFalse(log.settled(URL))

    def test_both_met_is_settled(self):
        log = _log()
        for n in (40, 25, 10, 0):
            log.record(URL, True, when=_day(n))
        self.assertTrue(log.settled(URL))

    def test_the_thresholds_are_weeks_not_days(self):
        # The weekly job is the normal caller, and the measured recovery
        # window was five days.
        self.assertGreaterEqual(deadwood.MIN_DAYS, 14)
        self.assertGreaterEqual(deadwood.MIN_RUNS, 3)


class ComingBackToLifeResetsEverything(unittest.TestCase):
    def test_a_board_with_jobs_is_forgotten(self):
        log = _log()
        for n in (40, 25, 10):
            log.record(URL, True, when=_day(n))
        self.assertTrue(log.settled(URL))
        log.record(URL, False)
        self.assertFalse(log.settled(URL))
        self.assertEqual(log.runs(URL), 0)

    def test_it_starts_from_nothing_next_time(self):
        # A board that posts, goes quiet for a month and posts again must not
        # inherit its old emptiness.
        log = _log()
        log.record(URL, True, when=_day(60))
        log.record(URL, False, when=_day(30))
        log.record(URL, True, when=_day(0))
        self.assertEqual(log.runs(URL), 1)
        self.assertEqual(log.days_empty(URL), 0)


class TwoChecksInOneDayAreOneDaysEvidence(unittest.TestCase):
    def test_same_day_records_do_not_advance_the_count(self):
        # Otherwise an afternoon of debugging marches a board to deletion.
        log = _log()
        for _ in range(9):
            log.record(URL, True, when=_day(0))
        self.assertEqual(log.runs(URL), 1)


class Durability(unittest.TestCase):
    def test_it_round_trips(self):
        path = Path(mkdtemp()) / "empty-since.json"
        a = deadwood.EmptyLog(path)
        for n in (40, 20, 0):
            a.record(URL, True, when=_day(n))
        a.save()
        b = deadwood.EmptyLog(path).load()
        self.assertTrue(b.settled(URL))
        self.assertEqual(b.runs(URL), 3)

    def test_an_unreadable_log_keeps_boards_rather_than_deleting_them(self):
        # Erring the other way would delete on a corrupt file.
        path = Path(mkdtemp()) / "empty-since.json"
        path.write_text("{not json", encoding="utf-8")
        log = deadwood.EmptyLog(path).load()
        self.assertEqual(len(log), 0)
        self.assertFalse(log.settled(URL))

    def test_a_missing_log_is_not_an_error(self):
        self.assertEqual(len(deadwood.EmptyLog(
            Path(mkdtemp()) / "nope.json").load()), 0)

    def test_sources_no_longer_in_the_list_are_forgotten(self):
        log = _log()
        log.record(URL, True)
        log.record("https://other.example/x", True)
        self.assertEqual(log.forget_missing([URL]), 1)
        self.assertEqual(len(log), 1)

    def test_no_temp_file_is_left_behind(self):
        path = Path(mkdtemp()) / "empty-since.json"
        log = deadwood.EmptyLog(path)
        log.record(URL, True)
        log.save()
        self.assertEqual(
            [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")],
            [])


class ItHasToSurviveBetweenRuns(unittest.TestCase):
    """A log the weekly job cannot keep is a prune that never happens.

    The runner starts from a fresh checkout every Sunday. A log under
    `state/`, which is gitignored, would be empty every time: no board would
    ever reach the thresholds and nothing would ever be pruned. A guard that
    never lets anything through is a broken prune, just quieter.
    """

    def test_the_default_lives_somewhere_tracked(self):
        self.assertEqual(deadwood.DEFAULT_DIR, "sources")
        self.assertFalse(
            "sources/" in (Path(__file__).parent.parent / ".gitignore")
            .read_text(encoding="utf-8"))

    def test_the_workflow_commits_it_with_the_prune(self):
        wf = (Path(__file__).parent.parent / ".github" / "workflows"
              / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("sources/empty-since.json", wf,
                      "the prune pull request does not carry the evidence it "
                      "argues from, so the next run starts from nothing")


class WiredIntoValidate(unittest.TestCase):
    def test_the_flag_exists_on_the_real_parser(self):
        from jobradar.cli import build_parser
        args = build_parser().parse_args(["validate", "--prune",
                                          "--state-dir", "/tmp/x"])
        self.assertEqual(args.state_dir, "/tmp/x")

    def test_prune_consults_the_log(self):
        import ast
        src = (Path(__file__).parent.parent / "jobradar" / "cli.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "cmd_validate")
        body = ast.dump(fn)
        self.assertIn("settled", body,
                      "cmd_validate deletes without asking how long the board "
                      "has been empty")
        self.assertIn("EmptyLog", body)


if __name__ == "__main__":
    unittest.main()
