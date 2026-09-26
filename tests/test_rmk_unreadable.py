"""The BT Group defect: `fetch_rmk` reporting a board it cannot read as a
board with nothing on it.

BT Group's jobs.bt.com was configured `platform: rmk`, the SuccessFactors
RMK adapter that reads `href="/job/..."` links out of server-rendered HTML.
It answered every request HTTP 200 with a 172KB page and 212 live roles, and
`fetch_rmk` returned `ok=True` with an empty payload anyway, because
SuccessFactors' newer "unified" front end builds the whole results area in
JavaScript this fetcher never runs: there is nothing server-rendered to find.
That is the exact shape CLAUDE.md keeps naming -- a failure that renders
identically to a success -- arriving through a platform that answers 200 and
tells the truth about nothing.

The fix teaches `fetch_rmk` the one thing that tells the two apart:
SuccessFactors' own "no results" marker, `id="noresults"`, which a genuinely
empty RMK search renders (with a message) whether the whole board is empty
or just this search matched nothing, verified live against two different
tenant templates. A page with neither a `/job/` link nor that marker has not
told this code anything a search can be judged on.

Run this file's first test against the code as it stood before that fix (a
plain `fetch_rmk` that ends `if not pages: return _no_rows(src, first_error,
answered)`, with no `_RMK_ANSWERED_SOMETHING` check) and it fails: BT's real
shell page satisfies `answered=True`, `first_error is None` and `pages == []`,
which `_no_rows` reads straight through to `Result(src, payload="")` --
`ok=True`, "no vacancies". This is `tests/CLAUDE.md`'s own test for a test:
reverted, it goes red; it does not pass by construction.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import adapters, fetch as fetch_mod
from jobradar.models import Source

FIXTURES = Path(__file__).parent / "fixtures"

# The genuine "nothing matched" markup, minimised from what Adidas's
# (table-layout) and Transport for London's (card-layout) RMK tenants both
# actually send back live for a keyword with no matches: the scaffold
# renders, and it says so.
_GENUINE_NO_MATCH = """
<html><body>
<h1 class="keyword-title">Search results for "zzzznonexistentqueryxyz123".</h1>
<div id="noresults" class="alert alert-block">
  <div id="attention">
    <label>There are currently no open positions matching
    "zzzznonexistentqueryxyz123".</label>
  </div>
</div>
</body></html>
"""


def _serving(html: str):
    class FakeSession:
        def mount(self, prefix, adapter):
            pass

        def get(self, url, headers=None, timeout=None):
            class R:
                status_code = 200
                headers = {"Content-Type": "text/html"}
                text = html
                content = html.encode("utf-8")
                encoding = "utf-8"
            return R()
    return FakeSession()


class TheBtShapedShellIsNotAnEmptyBoard(unittest.TestCase):
    """The regression this whole file exists for."""

    def test_bts_real_shell_page_is_reported_unreadable(self):
        html = (FIXTURES / "rmk_json_shell.html").read_text(encoding="utf-8")
        # Sanity on the fixture itself: if either signal were present the
        # rest of this test would prove nothing.
        self.assertNotIn("/job/", html)
        self.assertNotIn('id="noresults"', html)

        src = Source(
            company="BT Group", platform="rmk",
            url="https://jobs.bt.com/search/?q=&sortColumn=referencedate"
                "&sortDirection=desc")
        old, fetch_mod.requests.Session = fetch_mod.requests.Session, lambda: _serving(html)
        try:
            res = fetch_mod.fetch_rmk(src, [])
        finally:
            fetch_mod.requests.Session = old

        self.assertFalse(
            res.ok,
            "a page with no /job/ link and no results marker was read as a "
            "board with no vacancies, which is the exact BT Group defect")
        self.assertIn("results markup", res.error or "")

    def test_the_scan_path_does_not_count_it_among_boards_with_no_postings(self):
        """`ok=False` is what keeps `cmd_scan`'s "N sources responded with no
        postings at all" line from absorbing it -- that line only counts
        `res.ok and counts.get(...) == 0`. A `Result` this is not `ok` is
        already outside it; this pins the property the reporting line reads
        rather than re-implementing `cmd_scan` here."""
        html = (FIXTURES / "rmk_json_shell.html").read_text(encoding="utf-8")
        src = Source(company="BT Group", platform="rmk",
                    url="https://jobs.bt.com/search/?q=&sortColumn=x")
        old, fetch_mod.requests.Session = fetch_mod.requests.Session, lambda: _serving(html)
        try:
            res = fetch_mod.fetch_rmk(src, [])
        finally:
            fetch_mod.requests.Session = old
        counted_as_empty_board = res.ok and True  # `counts` would be 0 either way
        self.assertFalse(counted_as_empty_board)


class AGenuinelyEmptyBoardIsStillJustEmpty(unittest.TestCase):
    """The other half of the fix, and the one that matters just as much: a
    company that really has no open roles, or a search that really matched
    nothing, must not start reporting as unreadable. `CLAUDE.md` names this
    exact failure direction -- the iCIMS "no jobs were found" case had the
    same shape and the same fix."""

    def test_a_genuine_no_match_search_is_still_ok_with_zero_jobs(self):
        src = Source(company="Some Tenant", platform="rmk",
                    url="https://example.jobs2web.com/search/"
                        "?q=zzzznonexistentqueryxyz123")
        old, fetch_mod.requests.Session = (
            fetch_mod.requests.Session, lambda: _serving(_GENUINE_NO_MATCH))
        try:
            res = fetch_mod.fetch_rmk(src, [])
        finally:
            fetch_mod.requests.Session = old
        self.assertTrue(res.ok, res.error)
        self.assertEqual(list(adapters.parse(res.payload, src)), [])

    def test_a_live_rmk_board_that_genuinely_has_jobs_is_unaffected(self):
        """`rmk_search.html`, the fixture `test_core.py`'s own adapter tests
        already use, must keep parsing exactly as before: the new check only
        ever fires when a page has NEITHER a job link nor the marker, and a
        page full of jobs has the first."""
        html = (FIXTURES / "rmk_search.html").read_text(encoding="utf-8")
        src = Source(company="Adidas", platform="rmk",
                    url="https://jobs.adidas-group.com/search/?q=")
        old, fetch_mod.requests.Session = fetch_mod.requests.Session, lambda: _serving(html)
        try:
            res = fetch_mod.fetch_rmk(src, [])
        finally:
            fetch_mod.requests.Session = old
        self.assertTrue(res.ok, res.error)
        jobs = adapters.parse(res.payload, src)
        self.assertGreater(len(jobs), 0)


if __name__ == "__main__":
    unittest.main()
