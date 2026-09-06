"""One templated URL must not kill the whole weekly validation.

`validate_source` filled only `{keyword}`, and `str.format` raises KeyError on
any placeholder it was not given. The error came back out of
`ThreadPoolExecutor.map` in `cmd_validate`, so the run died at the first
source carrying another placeholder and every source after it went unchecked:
the health check then reported nothing about them either way, which is the
shape this repo keeps finding.

It had been failing every week on `Workable search`, whose URL carries
`location={country}`, with `KeyError: 'country'`. The workflow filed an issue
each time offering two explanations, a throttled runner or the step timeout,
and the real cause was neither.

`sources.fill_template` was written against exactly this class of bug, with a
docstring saying one source carrying `&loc={location}` killed a whole run.
This call site was simply never moved over.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import discover as discover_mod
from jobradar.models import Source

WORKABLE_SEARCH = ("https://jobs.workable.com/api/v1/jobs"
                   "?query={keyword}&location={country}")


class _Counted(unittest.TestCase):
    """No live network: the probe's counter is stubbed."""

    def setUp(self):
        self.real = discover_mod.count_jobs
        self.asked = []

        def fake(src, transport=None):
            self.asked.append(src.url)
            return 7, [], None

        discover_mod.count_jobs = fake

    def tearDown(self):
        discover_mod.count_jobs = self.real


class ASecondPlaceholderDoesNotRaise(_Counted):
    def test_the_country_placeholder_is_filled_not_fatal(self):
        s = Source(company="Workable search", platform="workable_search",
                   url=WORKABLE_SEARCH, keyword_template=True)
        row = validate = discover_mod.validate_source(s)
        self.assertEqual(row["verdict"], "live")

    def test_the_probe_url_has_no_placeholders_left_in_it(self):
        # A URL still carrying `{country}` is sent literally, and a board
        # asked for postings in the place called "{country}" answers with
        # none, which reads as dead.
        s = Source(company="Workable search", platform="workable_search",
                   url=WORKABLE_SEARCH, keyword_template=True)
        discover_mod.validate_source(s)
        self.assertTrue(self.asked)
        self.assertNotIn("{", self.asked[0])
        self.assertNotIn("}", self.asked[0])

    def test_the_keyword_is_a_real_word(self):
        s = Source(company="NHS Jobs", platform="nhs",
                   url="https://www.jobs.nhs.uk/candidate/search/results"
                       "?keyword={keyword}", keyword_template=True)
        discover_mod.validate_source(s)
        self.assertIn(discover_mod.PROBE_KEYWORD.split()[0], self.asked[0])

    def test_the_row_still_says_it_was_a_keyword_probe(self):
        s = Source(company="Workable search", platform="workable_search",
                   url=WORKABLE_SEARCH, keyword_template=True)
        self.assertIn("keyword search",
                      discover_mod.validate_source(s)["note"])


class AnUnfillableUrlIsOneSourcesProblem(_Counted):
    """Reported, never raised, and never prunable."""

    BAD = "https://boards.example/api?q={keyword}&loc={location}"

    def _row(self):
        s = Source(company="Handwritten", platform="custom", url=self.BAD,
                   keyword_template=True)
        return discover_mod.validate_source(s)

    def test_it_does_not_raise(self):
        self._row()

    def test_it_is_unreachable_rather_than_dead(self):
        # The tool could not ask the question. That is not the board having no
        # answer, and `--prune` deletes on "dead".
        self.assertEqual(self._row()["verdict"], "unreachable")

    def test_it_is_never_prunable(self):
        self.assertFalse(self._row()["prunable"])

    def test_the_note_names_the_placeholder(self):
        note = self._row()["note"]
        self.assertIn("location", note)

    def test_nothing_was_fetched_for_it(self):
        self._row()
        self.assertEqual(self.asked, [])


class TheRestOfTheListIsStillChecked(_Counted):
    """The actual damage was never one bad row: it was the 17,922 after it."""

    def test_a_bad_source_does_not_stop_the_ones_behind_it(self):
        srcs = [
            Source(company="Good 1", platform="greenhouse",
                   url="https://boards.example/a?q={keyword}",
                   keyword_template=True),
            Source(company="Bad", platform="custom",
                   url="https://boards.example/b?q={keyword}&loc={location}",
                   keyword_template=True),
            Source(company="Good 2", platform="greenhouse",
                   url="https://boards.example/c?q={keyword}",
                   keyword_template=True),
        ]
        rows = [discover_mod.validate_source(s) for s in srcs]
        self.assertEqual([r["verdict"] for r in rows],
                         ["live", "unreachable", "live"])


class TheBundledListIsProbeable(unittest.TestCase):
    """Every templated source that ships can actually be filled in.

    Parsed from the file rather than fetched, so this runs offline and
    catches a hand-added template before the weekly job does.
    """

    def test_every_shipped_template_fills(self):
        import json
        from jobradar.sources import UnusableSourceURL, fill_template
        rows = json.loads(
            (Path(__file__).parent.parent / "sources" / "sources.json")
            .read_text(encoding="utf-8"))["sources"]
        for r in rows:
            if "{" not in r["url"]:
                continue
            try:
                filled = fill_template(r["url"], keyword="test", country="UK")
            except UnusableSourceURL as e:
                self.fail(f"{r['company']}: {e}")
            self.assertNotIn("{", filled, r["company"])


if __name__ == "__main__":
    unittest.main()
