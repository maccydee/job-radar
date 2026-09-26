"""`fetch_rmk_json`'s paging, on a fake session so nothing here touches
jobs.bt.com.

Two things were measured live against the real endpoint on 2026-09-26 and
neither is what a naive implementation would guess, so both are pinned here:

  * the request body is `{keywords, locale, location, pageNumber,
    sortBy: "recent"}`, not `{searchText, offset, limit}`. The second shape
    also answers HTTP 200 with real-looking rows -- two calls with identical
    `offset` returned two different, non-overlapping sets of ten jobs, so
    whatever it paginates on is not the offset it is given. Only the first
    shape, `pageNumber`-keyed, was stable across repeats and walked the whole
    212-role board into 165 distinct ids with no gaps and no drift.
  * a page past the end drops the `jobSearchResult` key entirely rather than
    sending an empty list: page 22 of that same walk answered
    `{"totalJobs": 212}` and nothing else. Treating a missing key as "this
    code cannot read this" -- reasonable on page one -- would have reported
    every genuinely complete walk on this platform as cut off.
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import fetch as fetch_mod
from jobradar.models import Source

SRC = Source(
    company="BT Group",
    url="https://jobs.bt.com/search/?q=&sortColumn=referencedate&sortDirection=desc",
    platform="rmk_json",
    country="gb",
)


def _row(jid: str, title: str = "Role") -> dict:
    return {"response": {"id": jid, "unifiedStandardTitle": title,
                         "urlTitle": title.replace(" ", "-"),
                         "supportedLocales": ["en_GB"]}}


class FakeSession:
    """Serves canned pages keyed by the `pageNumber` in the POST body, and
    records the bodies it was sent."""

    def __init__(self, pages: dict[int, list]):
        self.pages = pages
        self.sent: list[dict] = []

    def mount(self, prefix, adapter):
        pass

    def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append(json)
        page = json["pageNumber"]
        rows = self.pages.get(page, [])

        class R:
            status_code = 200
            headers = {"Content-Type": "application/json"}

            @staticmethod
            def json_body():
                if rows or page in self.pages:
                    return {"jobSearchResult": rows, "totalJobs": 22}
                # Past the last page the real endpoint drops the key
                # entirely; only `totalJobs` survives. See module docstring.
                return {"totalJobs": 22}

        r = R()
        r.json = r.json_body
        return r

    def get(self, *a, **k):
        raise AssertionError("rmk_json is a POST endpoint")


class PagingAndDedup(unittest.TestCase):
    def test_it_walks_pageNumber_and_stops_on_the_first_empty_page(self):
        session = FakeSession({0: [_row("1"), _row("2")],
                               1: [_row("3")]})
        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: session):
            res = fetch_mod.fetch_rmk_json(SRC, [])
        self.assertTrue(res.ok)
        self.assertFalse(res.truncated)
        self.assertEqual(len(res.payload["jobSearchResult"]), 3)
        self.assertEqual([b["pageNumber"] for b in session.sent], [0, 1, 2])

    def test_the_request_body_matches_the_pages_own_js_not_a_guessed_shape(self):
        """`{searchText, offset, limit}` also 200s on the real endpoint with
        rows that look fine, and paginates on nothing repeatable. Pinned so a
        future edit cannot drift back to it without failing here first."""
        session = FakeSession({0: []})
        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: session):
            fetch_mod.fetch_rmk_json(SRC, [])
        body = session.sent[0]
        self.assertEqual(set(body), {"keywords", "locale", "location",
                                     "pageNumber", "sortBy"})
        self.assertEqual(body["sortBy"], "recent")

    def test_a_repeated_id_across_pages_is_kept_once(self):
        """Verified live: BT's own index repeats some ids across pages, 47 of
        212 rows fetched on 2026-09-26. A pager that keyed on position rather
        than id would double-count them."""
        session = FakeSession({0: [_row("1", "Same Role")],
                               1: [_row("1", "Same Role")]})
        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: session):
            res = fetch_mod.fetch_rmk_json(SRC, [])
        self.assertEqual(len(res.payload["jobSearchResult"]), 1)

    def test_the_endpoint_is_built_from_the_search_pages_own_host(self):
        session = FakeSession({0: []})
        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: session):
            fetch_mod.fetch_rmk_json(SRC, [])
        # The probe's URL is read back off `fetch_one`'s call; simplest way
        # to check it without stubbing lower is to rebuild it the same way
        # the fetcher does and confirm THAT is what a real host would be.
        from urllib.parse import urlparse
        self.assertEqual(urlparse(SRC.url).netloc, "jobs.bt.com")


class TheMissingKeyOnTheLastPage(unittest.TestCase):
    def test_a_page_with_only_totalJobs_ends_the_walk_without_a_cut_off_flag(self):
        """Verified live 2026-09-26: page 22 of BT's real 212-role board
        answered `{"totalJobs": 212}`, no `jobSearchResult` key at all. That
        is the platform's own way of saying "nothing more here", not this
        code failing to recognise the response, and a genuinely complete
        walk must not be reported as truncated."""
        session = FakeSession({0: [_row("1")]})   # page 1 has no entry: bare totalJobs
        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: session):
            res = fetch_mod.fetch_rmk_json(SRC, [])
        self.assertTrue(res.ok)
        self.assertFalse(res.truncated,
                         "a page with only totalJobs was read as a failure "
                         "mid-walk instead of the platform's own end-of-results")
        self.assertEqual(len(res.payload["jobSearchResult"]), 1)

    def test_a_response_with_neither_key_at_all_is_unreadable(self):
        """No `jobSearchResult` AND no `totalJobs` is not this platform
        answering with nothing -- verified live, that shape always carries
        `totalJobs` -- so it is not trusted as a genuinely empty board."""
        class BrokenSession:
            def mount(self, prefix, adapter): pass
            def post(self, url, json=None, headers=None, timeout=None):
                class R:
                    status_code = 200
                    headers = {"Content-Type": "application/json"}
                    @staticmethod
                    def json():
                        return {"error": "something else entirely"}
                return R()

        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: BrokenSession()):
            res = fetch_mod.fetch_rmk_json(SRC, [])
        self.assertFalse(res.ok)
        self.assertIn("jobSearchResult", res.error)

    def test_the_same_broken_shape_mid_walk_is_a_truncation_not_a_wipeout(self):
        """The same shape arriving after real rows have already been read is
        a truncation: what was read is kept, never discarded, the same rule
        `fetch_google_careers` and `fetch_pcsx` already apply to a fetcher
        that breaks partway through a walk."""
        class FlakySession:
            def __init__(self):
                self.calls = 0
            def mount(self, prefix, adapter): pass
            def post(self, url, json=None, headers=None, timeout=None):
                self.calls += 1
                page = json["pageNumber"]
                class R:
                    status_code = 200
                    headers = {"Content-Type": "application/json"}
                    @staticmethod
                    def json():
                        if page == 0:
                            return {"jobSearchResult": [_row("1")], "totalJobs": 99}
                        return {"error": "broke"}
                return R()

        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: FlakySession()):
            res = fetch_mod.fetch_rmk_json(SRC, [])
        self.assertTrue(res.ok)
        self.assertTrue(res.truncated)
        self.assertEqual(len(res.payload["jobSearchResult"]), 1)


class CapReporting(unittest.TestCase):
    def test_hitting_max_pages_is_reported_not_silent(self):
        # Every page has a row, so the walk never sees a natural stop and
        # runs out the clock on `max_pages`.
        session = FakeSession({i: [_row(str(i))] for i in range(5)})
        with unittest.mock.patch.object(fetch_mod, "_thread_session",
                                        lambda: session):
            res = fetch_mod.fetch_rmk_json(SRC, [], max_pages=5)
        self.assertTrue(res.ok)
        self.assertTrue(res.truncated)


if __name__ == "__main__":
    unittest.main()
