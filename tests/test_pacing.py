"""Where the scan's time goes, and which dial actually moves it.

The scan has two floors and cannot beat the larger of them:

  per-host   requests to one host x that host's gap
  machine    total request-seconds / workers

Only the first is knowable before a run starts, and it is the one nobody
guesses right, because it is not proportional to anything visible.
`apply.workable.com` is 2,094 of 17,810 sources, 12% of the list, and has been
100% of the fifty minutes, purely because it is the one busy host paced below
the default rate.

The finding these tests were written for: `DEFAULT_PER_HOST_RPS` sounds like a
number about 7,781 hosts and is a number about six. 7,749 of those hosts hold
exactly one board, and a gap only ever delays a SECOND request to the same
host, so on nearly every host in the file the rate is never consulted at all.
Those boards are limited by `fetch.concurrency` and nothing else. Tuning
either dial by looking at the total gets both of them wrong.

No timings are asserted anywhere here. A timing test is flaky by construction
and this suite has already lost a morning to one, so what is pinned is the
arithmetic and the behaviour, and the live measurements live in
`docs/PLATFORMS.md` next to the dates they were taken.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import cli
from jobradar.fetch import (DEFAULT_PER_HOST_RPS, PER_HOST_RPS, HostLimiter,
                            Result)
from jobradar.models import Source

BUNDLED = Path(__file__).resolve().parent.parent / "sources" / "sources.json"


def _srcs(spec: dict[str, int]) -> list[Source]:
    """`{host: boards}` as sources, one board per entry."""
    out = []
    for host, n in spec.items():
        for i in range(n):
            out.append(Source(company=f"{host}-{i}",
                              url=f"https://{host}/v1/boards/{host}-{i}/jobs",
                              platform="greenhouse"))
    return out


# ---------------------------------------------------------------------------
# What the floor is, and what it is not
# ---------------------------------------------------------------------------

def test_the_floor_is_the_slowest_host_and_not_the_biggest_one():
    """The whole reason this cannot be read off a board count.

    Greenhouse has nearly twice Workable's boards and is not the floor, because
    it is paced at the 3.0 default and Workable at 0.7. Anyone sizing this scan
    by looking at which platform has the most employers optimises the wrong
    host, which is exactly what happened before the per-host limiter existed.
    """
    floors = cli.pacing_floors(_srcs({"boards-api.greenhouse.io": 4078,
                                      "apply.workable.com": 2094}))
    assert floors[0][1] == "apply.workable.com", floors
    assert floors[1][1] == "boards-api.greenhouse.io", floors
    # 2,094 at 0.7/s is 49.9 minutes and 4,078 at 3.0/s is 22.7: half the
    # boards, more than twice the wait.
    assert floors[0][0] > 2 * floors[1][0], floors


def test_a_host_holding_one_board_is_not_paced_at_all():
    """The fact that makes `DEFAULT_PER_HOST_RPS` a number about six hosts.

    A gap delays the SECOND request to a host. A host asked once is never
    delayed by any rate, so five hundred single-board hosts have no floor
    between them, however low the rate is set. Set the rate to a crawl and the
    answer must not change.
    """
    single = _srcs({f"careers-{i}.icims.com": 1 for i in range(500)})
    assert cli.pacing_floors(single) == []
    assert cli.pacing_floors(single, HostLimiter(0.05)) == []


def test_the_floor_follows_the_rate_rather_than_being_written_down():
    """A recommendation has to be applyable by changing one number.

    `PER_HOST_RPS` and `DEFAULT_PER_HOST_RPS` live in `fetch.py`; this reads
    them through `HostLimiter.gap_for` rather than restating any figure, so
    raising Greenhouse to 5/s moves the reported floor without a second edit
    here or in the scan's pre-flight.
    """
    # A host with no override, so the default is what is being varied.
    # Greenhouse was used here until its measured rate went into
    # PER_HOST_RPS, at which point the default stopped governing it and this
    # test was measuring the override instead.
    srcs = _srcs({"jobs.jobvite.com": 3000})
    slow = cli.pacing_floors(srcs, HostLimiter(3.0))[0][0]
    fast = cli.pacing_floors(srcs, HostLimiter(6.0))[0][0]
    assert abs(slow - 2 * fast) < 1e-6, (slow, fast)
    # And an override still beats the default, in the direction that costs
    # time rather than the one that saves it.
    over = HostLimiter(3.0, {"jobs.jobvite.com": 1.0})
    assert cli.pacing_floors(srcs, over)[0][0] > slow


def test_pacing_is_measured_in_requests_not_in_wall_clock():
    """No stopwatch in here, deliberately.

    `pacing_floors` must be pure arithmetic over the source list, or it cannot
    run in the scan's pre-flight and cannot be tested without a network. Ten
    thousand sources are counted here in well under the time a single real
    request would take; the assertion is that it returns at all, from data
    alone, with no host contacted and no clock read.
    """
    floors = cli.pacing_floors(_srcs({"boards-api.greenhouse.io": 10000}))
    assert floors[0][2] == 10000
    assert floors[0][0] == 10000 * floors[0][3]


# ---------------------------------------------------------------------------
# The same arithmetic against the real list
# ---------------------------------------------------------------------------

def _bundled_sources() -> list[Source]:
    raw = json.loads(BUNDLED.read_text(encoding="utf-8"))
    return [Source(company=d["company"], url=d["url"],
                   platform=d.get("platform", "")) for d in raw["sources"]]


def test_the_default_rate_governs_a_handful_of_hosts_and_not_the_list():
    """Derived from the bundled file rather than asserted as a count.

    Counts rot -- this repo has shipped 17,625, 17,826 and 17,828 in prose
    while none was true -- so nothing here states how many boards there are.
    What is pinned is the SHAPE: the overwhelming majority of hosts carry one
    board and are therefore never paced, and the handful that are paced is
    small enough to reason about host by host, which is what makes a per-host
    table the right way to set these rates.
    """
    srcs = _bundled_sources()
    hosts = Counter(urlparse(s.url).netloc for s in srcs)
    paced = cli.pacing_floors(srcs)
    assert len(hosts) > 1000, len(hosts)
    # Nearly every host is asked exactly once.
    singles = sum(1 for n in hosts.values() if n == 1)
    assert singles / len(hosts) > 0.95, (singles, len(hosts))
    # And the paced ones are few enough to name.
    assert len(paced) < 40, [f[1] for f in paced]
    named = {f[1] for f in paced}
    assert "boards-api.greenhouse.io" in named, sorted(named)
    assert "api.ashbyhq.com" in named, sorted(named)


def test_removing_workable_hands_the_floor_to_greenhouse():
    """The question this file was opened to answer, as arithmetic.

    Workable's 2,094 boards are the floor today. Take them out -- the
    cross-employer search at `jobs.workable.com` is a different host and is
    not affected -- and the floor becomes `boards-api.greenhouse.io` at
    whatever `DEFAULT_PER_HOST_RPS` happens to be, which is a number nobody
    measured. That is the point: the next floor is set by a default, not by a
    finding, and this test is here so that stays visible if the rate changes.
    """
    srcs = [s for s in _bundled_sources()
            if urlparse(s.url).netloc != "apply.workable.com"]
    floors = cli.pacing_floors(srcs)
    assert floors[0][1] == "boards-api.greenhouse.io", floors[:3]
    assert floors[1][1] == "api.ashbyhq.com", floors[:3]
    # Greenhouse is now a measured override rather than the default, and the
    # floor still follows whatever that number is. It was 3.0 by default when
    # this test was written, and 3.0 had never been measured.
    assert PER_HOST_RPS["boards-api.greenhouse.io"] > DEFAULT_PER_HOST_RPS
    assert PER_HOST_RPS["api.ashbyhq.com"] > DEFAULT_PER_HOST_RPS
    # The gap the floor host is ACTUALLY paced at, read from the limiter,
    # not the number in the table and not the default. Asserting the table
    # was how three measured overrides shipped as no-ops: `gap_for` took
    # `min(global, override)`, so 5.0 against a 3.0 default paced at 3.0
    # while the table said 5.0 and the test agreed with it.
    assert abs(floors[0][3] - 1.0 / PER_HOST_RPS["boards-api.greenhouse.io"]) < 1e-9, floors[0]
    assert abs(HostLimiter().gap_for("boards-api.greenhouse.io")
               - 1.0 / PER_HOST_RPS["boards-api.greenhouse.io"]) < 1e-9


# ---------------------------------------------------------------------------
# The pre-flight
# ---------------------------------------------------------------------------

def _cfg(d: Path, boards: int) -> Path:
    rows = "".join(
        f"    - company: c{i}\n"
        f"      url: https://boards-api.greenhouse.io/v1/boards/c{i}/jobs\n"
        for i in range(boards))
    (d / "config.yaml").write_text(
        "titles:\n  include: [engineering manager]\n"
        "output:\n  formats: []\n  dir: " + str(d / "out") + "\n"
        "sources:\n  use_bundled: false\n  extra:\n" + rows,
        encoding="utf-8")
    return d / "config.yaml"


def _args(d: Path, cfg: Path):
    class A:
        config = str(cfg)
        db = str(d / "x.db")
        state = str(d / "seen.json")
        out = str(d / "out")
        docs = None
        dry_run = True
        no_enrich = True
        no_caffeine = True
        resume = False
        no_open = True
        limit = 0
    return A


def _scan(d: Path, boards: int) -> str:
    """Run `cmd_scan` with the network stubbed out, and return what it said."""
    real_fetch, real_parse = cli.fetch_all, cli.adapters.parse
    cli.fetch_all = lambda srcs, **kw: [Result(source=s, payload=b"[]")
                                        for s in srcs]
    cli.adapters.parse = lambda payload, src: []
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cli.cmd_scan(_args(d, _cfg(d, boards)))
    finally:
        cli.fetch_all, cli.adapters.parse = real_fetch, real_parse
    return buf.getvalue()


def test_the_scan_says_where_its_floor_is_before_it_spends_it():
    """Fifty minutes of deliberate sleeping used to be invisible until it had
    already happened, and the run said only how many sources it was fetching.

    A board count is not the fact a reader needs. The fact is that one host
    sets a floor no amount of concurrency can move, and it is worth saying
    before the wait rather than in a report afterwards.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        out = _scan(d, 800)
    assert "boards-api.greenhouse.io" in out, out
    assert "floor" in out, out
    # And it says which dial is the wrong one to reach for.
    assert "concurrency" in out, out


def test_a_small_config_is_not_told_about_a_floor_it_does_not_have():
    """The other half of saying something: not saying it when it is noise.

    Twenty boards on one host is under seven seconds of pacing. Announcing a
    floor there trains the reader to skip the line on the day it matters.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        out = _scan(d, 20)
    assert "floor" not in out, out


# ---------------------------------------------------------------------------
# What must survive raising a rate
# ---------------------------------------------------------------------------

def test_a_raised_rate_is_still_a_ceiling_a_refusing_host_can_lower():
    """Any recommendation to speed a host up rests on this.

    A rate in the table is a starting point, not a promise: the host gets the
    last word. `note_throttle` fires on every 429, including the ones that
    then succeed on retry, which on a host refusing one request in four is the
    only signal it gives. It has to widen the gap from whatever the table
    said, so raising Greenhouse to 5/s cannot make a bad afternoon
    unrecoverable.
    """
    lim = HostLimiter(5.0)
    url = "https://boards-api.greenhouse.io/v1/boards/x/jobs"
    base = lim.gap_for("boards-api.greenhouse.io")
    assert abs(base - 0.2) < 1e-9, base
    lim.note_throttle(url)
    assert lim.slowdown_for(url) > 1.0
    assert lim.gap_for("boards-api.greenhouse.io") > base


def test_the_slowdown_a_host_forces_is_readable_from_outside():
    """So the run can say it out loud rather than slowing down in silence.

    This is the Workable shape one level up. The circuit breaker needed three
    refusals in a row, a host refusing one in four never gives three in a row,
    and 41 boards came back unknown while the pacing never changed. The
    per-429 slowdown fixed the pacing; a slowdown nobody can read is the same
    silence again, so `slowdown_for` is part of the interface and not a
    private detail.
    """
    lim = HostLimiter(3.0)
    url = "https://api.ashbyhq.com/posting-api/job-board/x"
    assert lim.slowdown_for(url) == 1.0
    first = lim.note_throttle(url)
    assert lim.slowdown_for(url) == first > 1.0
