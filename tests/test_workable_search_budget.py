"""jobs.workable.com refused for a day at the end of every scan.

Every scan from 7 to 17 September 2026 ended with "jobs.workable.com is
rate-limiting this connection, so the next request there waits 23h 59m" (11h
27m on the 7th), and 39 to 61 sources there UNKNOWN. The host ran at the 3.0/s
default, which nothing had justified, and a scan sent it roughly 1,100 to 1,800
requests: the recently-posted sweep walked to exhaustion (1,054 pages for a
week) plus twelve titles times four countries of keyword search at up to fifteen
pages each. apply.workable.com, the same company's infrastructure, is known to
be a long-window quota that refuses from a few hundred requests in however they
are paced, so the fix is a ceiling on the pace AND a count.

No network anywhere in here. The session is a stand-in that always has another
page to give, which is the worst case the budget has to survive.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import threading
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import fetch as fetch_mod
from jobradar.models import Source

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workable_search.json"
HOST = "jobs.workable.com"


class EndlessWorkable:
    """Answers every request with a real search page that has a next page.

    Ids are made unique per request so a walk's merged rows really grow, and
    `refuse_at` turns one numbered request into a day-long 429, the answer the
    scan logs recorded.
    """

    def __init__(self, refuse_at: int | None = None) -> None:
        self.page = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.lock = threading.Lock()
        self.sent: list[str] = []
        self.refuse_at = refuse_at

    def mount(self, prefix, adapter):
        pass

    def get(self, url, headers=None, timeout=None):
        with self.lock:
            self.sent.append(url)
            n = len(self.sent)
        if self.refuse_at is not None and n >= self.refuse_at:
            class Refused:
                status_code = 429
                headers = {"Retry-After": "86400"}
                text = ""
            return Refused()
        body = copy.deepcopy(self.page)
        for i, j in enumerate(body["jobs"]):
            j["id"] = f"{n}-{i}"
        body["nextPageToken"] = f"token-{n}"

        class Page:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            text = json.dumps(body)
            content = text.encode("utf-8")
            encoding = "utf-8"

            @staticmethod
            def json():
                return body
        return Page()

    def post(self, *a, **k):
        return self.get(*a, **k)

    def to_host(self) -> int:
        return sum(1 for u in self.sent if urlparse(u).netloc == HOST)


def _scan_sources() -> list[Source]:
    """What `sources.load` builds for the config that kept being blocked:
    the recent sweep, and twelve titles in four countries."""
    out = [Source(company="Workable, posted recently", platform="workable_recent",
                  url=f"https://{HOST}/api/v1/jobs?day_range=7", country="multi")]
    for t in range(12):
        for place in ("United+Kingdom", "Canada", "United+Arab+Emirates",
                      "Australia"):
            out.append(Source(
                company=f"Workable search: title{t} in {place}",
                platform="workable_search", keyword_template=True,
                country="multi",
                url=f"https://{HOST}/api/v1/jobs?query=title{t}&location={place}"))
    return out


def _run_scan(session: EndlessWorkable, workers: int = 8) -> tuple[list, str]:
    out = io.StringIO()
    # per_host_rps=0 turns pacing off so the test does not sleep. The budget is
    # a count, not a rate, and has to hold with pacing off too.
    with mock.patch.object(fetch_mod, "_thread_session", lambda: session), \
            contextlib.redirect_stdout(out):
        results = fetch_mod.fetch_all(_scan_sources(), concurrency=workers,
                                      per_host_rps=0, retries=0)
    return results, out.getvalue()


def test_jobs_workable_com_is_paced_no_faster_than_workables_boards():
    """It inherited 3.0/s from the default, and no measurement ever backed
    that. Same company, same infrastructure as apply.workable.com, which is
    refused at a sustained 1.5/s."""
    lim = fetch_mod.HostLimiter()
    assert lim.gap_for(HOST) >= lim.gap_for("apply.workable.com"), (
        f"{HOST} paced at {1 / lim.gap_for(HOST):.2f} req/s, faster than "
        f"Workable's boards")


def test_a_scan_sends_jobs_workable_com_no_more_than_its_budget():
    """The count the logs point at. Uncapped, this config sends the host
    2,000 pages of sweep plus 48 walks of 15, and a quota does not care how
    slowly they arrive."""
    session = EndlessWorkable()
    results, _ = _run_scan(session)
    budget = fetch_mod.HOST_REQUEST_BUDGET[HOST]
    assert session.to_host() <= budget, (
        f"{session.to_host()} requests to {HOST} in one scan, budget {budget}")
    # And the budget itself stays under the evidence, so raising it is a
    # decision somebody has to make here rather than a number that drifts:
    # the lowest refusal ever seen on Workable's infrastructure was the 176th
    # request of a run.
    assert session.to_host() < 176, (
        f"{session.to_host()} requests in one scan; apply.workable.com has "
        f"refused from the 176th")
    assert len(results) == len(_scan_sources()), "a source went missing"


def test_a_source_the_budget_turned_away_is_unknown_and_the_scan_says_so():
    """Turned away is not empty. It must not be `ok` (which is what gets
    stored as a board with nothing on it), it must read as throttled (which is
    what `validate` and `detect_throttling` treat as cannot-say), and the run
    has to name the host, because `cmd_scan` only names one when a block length
    is in the error and there is none."""
    session = EndlessWorkable()
    results, said = _run_scan(session)
    unread = [r for r in results if not r.ok]
    assert unread, "the worst case should exhaust the budget"
    assert all(r.throttled for r in unread), [r.error for r in unread]
    # `cmd_scan` pulls `(\d+)s` out of a throttled error as the host's block
    # length. A budget is not a block and must not print a wait.
    import re
    assert not any(re.search(r"(\d+)s", r.error or "") for r in unread), (
        [r.error for r in unread])
    assert HOST in said and "UNKNOWN today, not empty" in said, said


def test_the_recent_sweep_leaves_most_of_the_budget_to_the_searches():
    """The sweep starts within the first two hundred tasks of the pass. Walked
    to exhaustion it spends the whole budget before most searches start, and
    every one of the reader's own titles comes back unknown on every scan."""
    session = EndlessWorkable()
    src = _scan_sources()[0]
    lim = fetch_mod.HostLimiter(rps=0)
    with mock.patch.object(fetch_mod, "_thread_session", lambda: session):
        res = fetch_mod._fetch_dispatch(src, lim, 20, 0, "job-radar-test", [])
    budget = fetch_mod.HOST_REQUEST_BUDGET[HOST]
    assert session.to_host() <= budget // 2, (
        f"the sweep alone sent {session.to_host()} of a budget of {budget}")
    assert res.ok and res.truncated, (
        "a capped sweep has to say it was cut off, or it reads as complete")


def test_a_walk_refused_part_way_keeps_its_rows_and_says_it_was_cut_off():
    """Page one answered, page two was refused for a day. The rows are real,
    but the search is not finished, and it used to come back looking like a
    complete one-page answer."""
    session = EndlessWorkable(refuse_at=2)
    src = _scan_sources()[1]
    lim = fetch_mod.HostLimiter(rps=0)
    fetch_mod.pace_this_thread(lim)
    try:
        with mock.patch.object(fetch_mod, "_thread_session", lambda: session):
            res = fetch_mod.fetch_workable_search(src, retries=0)
    finally:
        fetch_mod.pace_this_thread(None)
    assert res.ok and res.payload["jobs"], "page one's rows were thrown away"
    assert res.truncated is True, "a refused walk read as a complete answer"


def test_one_day_long_refusal_stops_every_search_on_the_host():
    """What already happens on a 429 carrying a long `Retry-After`, pinned
    because the budget leans on it: the host is blocked for the run and no
    other search there sends anything. One refusal, not one per search."""
    session = EndlessWorkable(refuse_at=1)
    # One worker, so the count is about what the code does after hearing the
    # refusal and not about requests already in flight when it arrived. A real
    # scan paces this host to one request at a time anyway.
    results, said = _run_scan(session, workers=1)
    assert session.to_host() == 1, (
        f"{session.to_host()} requests after the host said not for a day")
    assert not any(r.ok for r in results)
    assert all(r.throttled for r in results), [r.error for r in results]
