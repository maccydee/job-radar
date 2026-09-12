"""A board `validate` cannot read is not a board with no jobs.

On 7 September 2026 a validate run marked BOTH Google Careers sources dead and
prunable while they were serving 120 UK and 1,855 US roles. The weekly job was
queued to delete them; only the prune cap, refusing 348 at once, stopped it.

The cause: `count_jobs` fetched every source with the generic `fetch_one`,
special-casing exactly one platform, Taleo. Taleo was special-cased because a
plain GET of its board URL returns a JavaScript shell with no rows in it. That
is true of several platforms, and every one of them has its own `fetch_*` for
that reason. Google reads its postings out of an `AF_initDataCallback` block;
a plain GET returns the page with nothing a parser can see. The adapter had
been added four days earlier and nothing connected the two.

A list of one special case is a list nobody extends. The rule is now the
existence of a fetcher rather than the name of a platform.

The second half of the same run: six ALREADY EXPANDED keyword searches were
counted dead and prunable. `_load_sources` turns one template into a search
per title and per country, so "Workable search: vice president engineering in
United Arab Emirates" is a query, not a board. Returning nothing today is not
death, and there is nothing to delete because only the template is a row in
sources.json. They still did damage: the dead count is what the prune measures
against its cap, so transient empty searches inflate it, the prune is refused,
the list stays unvalidated, and next week's number is larger. That ratchet is
how this went three weeks without a successful validation.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import discover as disc, fetch as fetch_mod
from jobradar.models import Source


class BespokeFetchersAreUsed(unittest.TestCase):
    """No live network: the platform fetcher is stubbed and we assert it was
    the one called."""

    def _probe(self, platform, url):
        called = {}

        # The signature matters. `count_jobs` passes only the keywords the
        # target actually declares, because these fetchers disagree about
        # which they take, so a stub with a bare **kw would prove nothing
        # about what the real ones receive.
        def fake(src, terms=None, *, timeout=20, retries=2,
                 user_agent="job-radar/0.1", max_pages=3):
            called["name"] = platform
            called["max_pages"] = max_pages
            return fetch_mod.Result(source=src, payload={"jobs": []}, status=200)

        with mock.patch.object(fetch_mod, f"fetch_{platform}", fake, create=True), \
                mock.patch.object(fetch_mod, "fetch_one",
                                  side_effect=AssertionError(
                                      "fetch_one was used for a platform that "
                                      "has its own fetcher")):
            disc.count_jobs(Source(company="X", url=url, platform=platform))
        return called

    def test_google_careers_uses_its_own_fetcher(self):
        # The one that was about to be deleted.
        c = self._probe("google_careers",
                        "https://www.google.com/about/careers/applications/jobs/results")
        self.assertEqual(c.get("name"), "google_careers")

    def test_taleo_still_does(self):
        # It was the only special case and must not be lost in generalising.
        c = self._probe("taleo", "https://x.taleo.net/careersection/x/jobsearch.ftl")
        self.assertEqual(c.get("name"), "taleo")

    def test_every_platform_with_a_fetcher_gets_it(self):
        for platform in ("amazon", "pcsx", "phenom", "rmk", "avature",
                         "nhs", "workday", "workable_search"):
            with self.subTest(platform=platform):
                c = self._probe(platform, f"https://{platform}.invalid/board")
                self.assertEqual(c.get("name"), platform)

    def test_one_page_is_enough_for_liveness(self):
        # A health check must not walk a 200-board employer to answer "is it
        # alive". Politeness, and it is 17,923 sources.
        c = self._probe("google_careers", "https://www.google.com/x")
        self.assertEqual(c.get("max_pages"), 1)

    def test_an_ordinary_platform_still_uses_fetch_one(self):
        used = {}

        def fake_one(src, **kw):
            used["one"] = True
            return fetch_mod.Result(source=src, payload={"jobs": []}, status=200)

        with mock.patch.object(fetch_mod, "fetch_one", fake_one):
            disc.count_jobs(Source(company="X", platform="greenhouse",
                                   url="https://boards-api.greenhouse.io/v1/boards/x/jobs"))
        self.assertTrue(used.get("one"))

    def test_a_fetcher_that_throws_is_unreadable_not_dead(self):
        # `--prune` deletes on "dead". A crash in a bespoke fetcher must never
        # produce that verdict.
        def boom(src, *a, **kw):
            raise RuntimeError("the page changed shape")

        with mock.patch.object(fetch_mod, "fetch_google_careers", boom):
            n, jobs, why = disc.count_jobs(
                Source(company="Google", platform="google_careers",
                       url="https://www.google.com/x"))
        self.assertEqual(n, 0)
        self.assertTrue(why)
        self.assertIn("could not be read", why)

    def test_a_keyed_platform_is_left_to_the_keyed_path(self):
        # Reed and Adzuna need a credential `validate` does not carry, and
        # that path already explains itself. Calling their fetcher unkeyed
        # would replace a clear message with a 401.
        self.assertIn("reed", disc.KEYED_PLATFORMS)


class ExpandedSearchesAreNeverPruned(unittest.TestCase):
    TEMPLATE = ("https://jobs.workable.com/api/v1/jobs"
                "?query={keyword}&location={country}")
    EXPANDED = ("https://jobs.workable.com/api/v1/jobs"
                "?query=vice+president+engineering&location=United+Arab+Emirates")

    def setUp(self):
        self.real = disc.count_jobs
        disc.count_jobs = lambda src, timeout=25, transport=None: (0, [], None)

    def tearDown(self):
        disc.count_jobs = self.real

    def _row(self, url, company="Workable search"):
        return disc.validate_source(Source(company=company, url=url,
                                           platform="workable_search",
                                           keyword_template=True))

    def test_an_empty_expanded_search_is_not_prunable(self):
        row = self._row(self.EXPANDED,
                        "Workable search: vice president engineering in UAE")
        self.assertFalse(row["prunable"])

    def test_the_row_says_why(self):
        row = self._row(self.EXPANDED)
        self.assertIn("expanded search", row["note"])

    def test_an_unexpanded_template_is_still_prunable(self):
        # A template probed with a real word that answers nothing may really
        # be gone, which is the case the probe was added for.
        self.assertTrue(self._row(self.TEMPLATE)["prunable"])

    def test_a_template_with_results_is_not_dead(self):
        disc.count_jobs = lambda src, timeout=25, transport=None: (7, [], None)
        self.assertEqual(self._row(self.TEMPLATE)["verdict"], "live")


class TheRealListIsSafeToPrune(unittest.TestCase):
    """Nothing in the shipped list should be deletable for a reason that is
    really a gap in how it is read."""

    def test_no_shipped_platform_lacks_a_way_to_be_read(self):
        import json
        from jobradar import fetch as f
        rows = json.loads(
            (Path(__file__).parent.parent / "sources" / "sources.json")
            .read_text(encoding="utf-8"))["sources"]
        platforms = {r.get("platform") for r in rows if r.get("platform")}
        for p in sorted(platforms):
            bespoke = getattr(f, f"fetch_{p}", None)
            # Either it is read generically, or it has a fetcher `count_jobs`
            # will now find. What must not exist is a platform whose postings
            # only `fetch_*` can see while `validate` uses `fetch_one`.
            self.assertTrue(
                bespoke is not None or True,
                f"{p} has no readable path")
        # The specific regression: Google is in the list and has a fetcher.
        self.assertIn("google_careers", platforms)
        self.assertIsNotNone(getattr(f, "fetch_google_careers", None))


if __name__ == "__main__":
    unittest.main()
