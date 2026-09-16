"""A skipped test is not a failed one, and not a passed one either.

Every CI run from 12 September 2026 was red while the same suite passed
locally. Three tests in test_screening_filters.py call `self.skipTest(...)`
when the checkout has no config, which is true on every runner because
`config.yaml` is gitignored. This runner executes tests through
`TestCase.debug()`, which does not handle skips: `unittest.SkipTest` came out
as an ordinary exception and was counted as a failure.

The shape is this repo's usual one. The suite said "1493/1496 passed" and
exited 1, so the summary line looked almost healthy and the cause was three
tracebacks thousands of lines up. A skip now says so, in the per-test line and
again in the summary, because a test that could not run is a gap rather than a
result.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_all  # noqa: E402


class SkipIsItsOwnVerdict(unittest.TestCase):
    def test_a_skip_is_not_a_failure(self):
        def skips():
            raise unittest.SkipTest("no config in this checkout")
        verdict, why = run_all.run_one(skips)
        self.assertEqual(verdict, "skip")
        self.assertIn("no config", why)

    def test_a_skip_through_the_real_testcase_api(self):
        """Via self.skipTest and debug(), which is how it actually arrives."""
        class T(unittest.TestCase):
            def runTest(self):
                self.skipTest("nothing to test here")
        verdict, why = run_all.run_one(lambda: T().debug())
        self.assertEqual(verdict, "skip", "skipTest must not read as a failure")

    def test_a_pass_is_still_a_pass(self):
        self.assertEqual(run_all.run_one(lambda: None)[0], "pass")

    def test_a_failure_is_still_a_failure(self):
        def boom():
            raise AssertionError("nope")
        self.assertEqual(run_all.run_one(boom)[0], "fail")

    def test_systemexit_is_still_a_failure(self):
        """The reason this handler catches BaseException at all."""
        def exits():
            raise SystemExit(2)
        self.assertEqual(run_all.run_one(exits)[0], "fail")


if __name__ == "__main__":
    unittest.main()
