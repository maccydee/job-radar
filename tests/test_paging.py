"""Every paged fetcher has a cap, and none of them used to say when it bit.

A cap is the right guard: a stop condition that is wrong with no cap behind it
is an infinite loop. It is the wrong thing to be silent about, because the
first N of an unknown number reads exactly like a complete answer, and the
reader has no way to tell the two apart. That is the shape this codebase keeps
producing, arriving this time through paging.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import fetch as fetch_mod

PAGERS = ["fetch_workday", "fetch_workable_search", "fetch_nhs", "fetch_adzuna",
          "fetch_phenom", "fetch_avature", "fetch_rmk", "fetch_rmk_json"]

SRC = (Path(__file__).resolve().parent.parent / "jobradar" / "fetch.py"
       ).read_text(encoding="utf-8")


def _body(name: str) -> str:
    a = SRC.index(f"\ndef {name}(")
    b = SRC.index("\ndef ", a + 10)
    return SRC[a:b]


def test_a_result_can_say_it_was_cut_off():
    """On the Result, not inside a payload, because a parser should not have to
    know about a fetcher's paging and the scan summary reads Results."""
    r = fetch_mod.Result(source=None, payload={"x": 1}, truncated=True)
    # `assert r.truncated is True` used to lead here and asserted only that a
    # dataclass stores the argument it was handed. It is a fact about a GOOD result: the rows are real, there are just
    # more of them. Not a failure, so `ok` is unaffected.
    assert r.ok is True
    assert fetch_mod.Result(source=None, payload={}).truncated is False


def test_every_paged_fetcher_reports_hitting_its_cap():
    """Written as a sweep rather than seven tests, so a pager added later is
    covered the day it is added."""
    silent = []
    for name in PAGERS:
        b = _body(name)
        declares = "truncated = False" in b or "truncated = bool" in b
        sets = "truncated = True" in b or "truncated = bool" in b
        returns = "truncated=truncated" in b
        if not (declares and sets and returns):
            silent.append(name)
    assert not silent, f"these cap silently: {silent}"


def test_the_scan_tells_you_which_sources_were_cut_off():
    cli = (Path(__file__).resolve().parent.parent / "jobradar" / "cli.py"
           ).read_text(encoding="utf-8")
    assert "r.truncated" in cli
    assert "cut off at" in cli


def test_workday_pages_far_enough_to_find_a_late_match():
    """Workday's `searchText` is a full-text match, not a title match, so a
    wanted title scores against every posting whose description mentions it
    and the real matches are not all at the top. Measured against one large
    tenant for "engineering manager": page 1 held 20 real title matches, pages
    4 and 5 held none, and pages 7 to 9 held another 19 between them.

    So a cap of three was not a shallow-paging trade-off, it was a wrong
    answer, and stopping early on a quiet page would be wrong for the same
    reason. The only honest options are to page further and to say when the
    cap bit.
    """
    m = re.search(r"max_pages: int = (\d+)", _body("fetch_workday"))
    assert m and int(m.group(1)) >= 10, "back to shallow paging"


def test_a_pager_stops_on_a_short_page_without_claiming_it_was_cut_off():
    """The common case, and the one that must not start crying wolf: a board
    smaller than one page still finishes in a single request and is complete."""
    calls = {"n": 0}

    class OnePage:
        def mount(self, prefix, adapter): pass

        def post(self, url, json=None, headers=None, timeout=None):
            calls["n"] += 1

            class R:
                status_code = 200
                headers = {"Content-Type": "application/json"}
                text = '{"jobPostings": [], "total": 0}'

                @staticmethod
                def json():
                    return {"jobPostings": [{"title": "EM", "externalPath": "/a"}],
                            "total": 1}
            return R()

        def get(self, *a, **k):
            return self.post(*a, **k)

    from unittest import mock
    from jobradar.models import Source
    src = Source(company="Small", platform="workday",
                 url="https://x.wd1.myworkdayjobs.com/wday/cxs/x/y/jobs")
    with mock.patch.object(fetch_mod, "_thread_session", lambda: OnePage()):
        res = fetch_mod.fetch_workday(src, ["engineering manager"])
    assert res.ok
    assert res.truncated is False, "a complete small board claimed it was cut off"
    assert calls["n"] == 1, "a board that fits in one page should cost one request"
