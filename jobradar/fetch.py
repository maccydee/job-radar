"""Polite HTTP with throttle detection.

The reason this file exists rather than a bare `requests.get` loop: several of
these APIs fail in ways that look exactly like success.

  * Ashby and SmartRecruiters return HTTP 200 with an empty array for a board
    token that does not exist, and for one that is being rate-limited. Status
    code tells you nothing; job count does.
  * Greenhouse returns 403 if you attach a body to a GET, which is easy to do
    accidentally when one code path handles both GET and POST platforms.
  * Workday returns 406 rather than 404 for a tenant that does not exist,
    because of wildcard DNS. A non-404 is not evidence a tenant is real.

So a source that used to return jobs and now returns none is reported as a
suspected throttle rather than "no jobs", because the difference matters and
the API will not tell you.
"""

from __future__ import annotations

import random
import re
import json
import threading
from pathlib import Path
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import (parse_qsl, quote_plus, urlencode, urlparse,
                          urlsplit, urlunsplit)

import requests

from .models import Source


@dataclass
class Result:
    source: Source
    payload: Any = None
    error: str | None = None
    status: int | None = None
    elapsed: float = 0.0
    throttled: bool = False
    # Set when the failure happened below HTTP: the TLS handshake never
    # completed, so there is no status code and never was one. See
    # `handshake_failure`. Kept separate from `error` because the string is for
    # a human and this is for code: `validate --prune` has to be able to ask
    # "was this the board's answer or this machine's?" without parsing prose.
    transport: str | None = None
    # Set when a paged fetcher stopped because it hit its own page cap while
    # the board still had more to give. Every pager here has a cap, because a
    # broken stop condition with no cap behind it is an infinite loop. The cap
    # is the right guard and the wrong thing to be silent about: a result that
    # is the first N of an unknown number reads exactly like a complete one,
    # which is the failure-that-looks-like-success this file keeps producing.
    # `ok` stays True (the rows that came back are real, there are just more
    # of them), so this is a fact ABOUT a good result, like `throttled`, not a
    # kind of failure. See `capped_sources`.
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.payload is not None


_local = threading.local()


# How many requests a second one host will take without answering 429. The
# number is per host and not per scan, because the work is bimodal: 10,011 of
# the 17,809 bundled sources sit on seven API hosts and roughly 7,748 hosts
# carry one board each. A single global concurrency number cannot serve both.
# Set low enough for seven hosts it wastes an hour on the long tail; set high
# enough for the long tail it turns into a burst against Greenhouse.
DEFAULT_PER_HOST_RPS = 3.0

# Hosts that need less than the default. Workable is the strict one and this
# is not a politeness preference, it is data loss: it answers 429 readily, and
# a 429 that reaches the adapter is parsed as a board with no jobs, which is
# indistinguishable from a board that really has none. That is how 250 live
# employers, Contentful and Ecosia among them, were once thrown away in one
# run. 0.7 is the rate the maintainer's enumerator has sustained overnight
# against this host without a 429.
PER_HOST_RPS = {
    # The only rate here that is a ceiling rather than a measurement, and the
    # only one that must never be raised. Not a rate limit but a long-window
    # quota: a burst of 90 requests passes at 3.0/s and a sustained 1.5/s is
    # refused at request 301. See docs/PLATFORMS.md.
    "apply.workable.com": 0.7,
    # Workable's other host, and a ceiling rather than a measurement: nobody
    # has measured this host's limit, and nobody should go and find it. It ran
    # at the 3.0 default, which no evidence ever justified, and every scan from
    # 7 to 17 September 2026 ended with it refusing for 11 to 24 hours.
    # 0.7 because it is the same company's infrastructure as the line above,
    # and 0.7 is the fastest rate at which that host has been seen to take a
    # run of 250 requests without a refusal (1.5/s was refused at the 301st).
    # Pacing alone does not fix a quota, though, which is why
    # `HOST_REQUEST_BUDGET` exists as well.
    "jobs.workable.com": 0.7,
    # These three were 3.0 by default, and 3.0 was never measured. It entered
    # in the commit that introduced per-host pacing, whose own sentence cites
    # evidence for Workable's 0.7 and none for this one. Only six hosts are
    # governed by the default at all: 7,748 of 7,781 hold a single board, and
    # iCIMS puts each of its 1,744 on its own hostname, so none of them is
    # ever paced.
    #
    # Measured after a full scan had finished, with the scan's own user agent,
    # real board tokens, pooled so the target rate was actually reached, and a
    # hard stop on the first refusal. Greenhouse took 6.39/s for 360
    # consecutive requests and Ashby 6.36/s for 300, both clean. Set at 5.0,
    # about 20% under the highest rate seen, because neither publishes a limit
    # and the evidence is measurement alone.
    "boards-api.greenhouse.io": 5.0,
    "api.ashbyhq.com": 5.0,
    # The one host here that documents a number that applies: SmartRecruiters
    # publishes 10 requests a second for most endpoints, and tolerated 7.75.
    "api.smartrecruiters.com": 8.0,
}

# How many requests one run may send a host whose limit is a quota rather than
# a rate. Counted per `HostLimiter`, which is one per `fetch_all`, and a scan's
# jobs.workable.com sources all sit in its first pass, so in practice this is
# per scan. Retries count: a retry is a request as far as the host is concerned.
#
# Why a count and not only a slower pace. Workable's limit behaves like a
# budget over a long window, and a budget is spent by requests however slowly
# they arrive: apply.workable.com refused from the 176th request of a run paced
# at 0.7/s, and at the 301st of one paced at 1.5/s. A rate table cannot see
# where a run is in that window. A count can.
#
# What jobs.workable.com was being sent. One scan with twelve titles and four
# countries expands the keyword search into 48 walks of up to 15 pages, and the
# recently-posted sweep was walked to exhaustion: 21,062 postings in a week is
# 1,054 pages. So roughly 1,100 to 1,800 requests, at 3.0/s, all inside the
# first pass. On 17 September the scan log was created at 09:43:12 and the
# 24-hour block was written at 09:45:57, which at 3.0/s is at most about 500
# requests in, and plausibly the same few hundred that apply.workable.com
# allows. Every scan since 7 September ended the same way, and the refusal
# takes out every search there, not only the one that tripped it.
#
# 150, and a ceiling rather than a measurement: under the lowest refusal ever
# seen on Workable's infrastructure (the 176th request), deliberately not
# measured here, because finding this host's real number costs a day of it.
# When it is spent the remaining sources there come back UNKNOWN and the scan
# says so; a walk it stops part way through keeps its rows and is marked cut
# off. Lower it freely. Raise it only on evidence, and never by probing.
HOST_REQUEST_BUDGET = {
    "jobs.workable.com": 150,
}

# Pages of the recently-posted sweep a scan reads, out of the host budget above.
#
# It used to be 2,000, so that a week of Workable (1,054 pages) was read in
# full. That walk alone is seven times the budget, and it starts within the
# first two hundred tasks of the first pass, so uncapped it would spend the
# whole budget before most of the keyword searches had started and leave them
# UNKNOWN on every scan. The searches are the reader's own titles in the
# reader's own countries; the sweep is 21,062 postings of which the title
# filter keeps under one in a hundred. So the sweep is held to a slice and the
# searches keep the rest: 30 pages, 600 postings, leaving 120 requests for
# the searches, which need 48 first pages on a twelve-title, four-country
# config. When the cap bites the result is marked cut off and the scan says so,
# which is the thing the old uncapped walk was protecting against.
WORKABLE_RECENT_MAX_PAGES = 30

# Workers, not requests per second. Politeness is the limiter's job now, so
# this number only decides how many DIFFERENT hosts are in flight at once, and
# on a list where 7,748 hosts hold one board each, four at a time is most of an
# hour spent waiting on other people's latency. Sixteen is chosen so the pool
# still has workers free while some are parked waiting for a Workable slot: at
# 12% of the list on a 0.7/s host, roughly two workers are parked at any moment.
DEFAULT_CONCURRENCY = 16

# The ceiling a config can ask for. Not a politeness limit any more, a
# resources one: the sockets, file descriptors and DNS lookups belong to
# whoever is running this, and a four-figure worker count only exhausts them.
MAX_CONCURRENCY = 64

# What a host is slowed down by when it answers 429, and how far that can go.
#
# `PER_HOST_RPS` above is a table somebody measured once. Measured again on
# 2026-08-26 against the 0.7 in it: a run over 3,568 bundled sources, paced at
# exactly that rate, got 41 of its 419 apply.workable.com boards (9.8%) back
# as HTTP 429 after their retries. Every one of those employers was UNKNOWN
# for that run. The refusals began at the 176th request to that host and
# continued at roughly one in four to the end of it.
#
# What it is NOT, because all three were checked against the same host the
# same day and every one of them drew zero refusals: 250 boards single
# threaded at 0.7/s; 250 at 0.35/s; 150 across eight workers at 0.7/s with
# eight requests in flight at the peak. So neither the instantaneous rate nor
# the concurrency explains it on its own, and no number in a static table is
# going to. It behaves like a budget that a long run exhausts and that comes
# back with time, and a table cannot know where a run is in one.
#
# What matters more than the mechanism is that nothing here responded to it.
# The circuit breaker arms on CONSECUTIVE_429_LIMIT refusals IN A ROW, these
# were interleaved with successes, and `note_ok` reset the run every time. So
# the host said no 41 times and the pacing it was saying no to never changed.
#
# Slowing down is the right answer where blocking is the wrong one: a host
# serving between 429s is busy, not shut, and
# `test_the_breaker_needs_a_run_of_refusals_not_a_scattered_few` pins that it
# must not be shut out. Halving the rate spends the budget slower, keeps every
# board reachable, and costs only time.
#
# The cap is what stops a bad afternoon turning into an eight-hour scan: at
# 8x, Workable's 0.7/s becomes one request every 11 seconds, and past that
# point the honest answer is that the host does not want this traffic today.
HOST_SLOWDOWN_STEP = 2.0
MAX_HOST_SLOWDOWN = 8.0

# Successes on one host before its gap is narrowed again by one step. Recovery
# has to be slower than the slowdown, or a host that refuses one request in
# four ends up oscillating around the rate that refuses. Twenty is about three
# times the run of successes Workable gave between refusals.
OK_RUN_TO_SPEED_UP = 20


class HostLimiter:
    """A minimum gap between requests to the same host, across all workers.

    Global concurrency is the wrong dial for this list. Raising it speeds up
    the 7,748 hosts that hold one board each, and at the same time aims the
    whole pool at `boards-api.greenhouse.io` for the 4,078 consecutive entries
    that live there, because the bundled source list is sorted into contiguous
    per-platform blocks. This decouples the two: the pool can be wide because
    each host is still paced on its own clock.

    An rps of 0 or less disables pacing entirely. That exists so a benchmark
    can measure what the pacing costs; it is not a mode to scan in.
    """

    def __init__(self, rps: float | None = None,
                 overrides: dict[str, float] | None = None,
                 budgets: dict[str, int] | None = None) -> None:
        # Requests this limiter may still send each budgeted host, and how
        # many sources it turned away once a budget ran out. See
        # `HOST_REQUEST_BUDGET` and `spend`.
        self.budgets = dict(HOST_REQUEST_BUDGET if budgets is None else budgets)
        self._spent: dict[str, int] = defaultdict(int)
        self._turned_away: dict[str, int] = defaultdict(int)
        # Whether the caller ASKED for this rate or simply got it. `gap_for`
        # needs to tell those apart, and a bare float cannot: a caller passing
        # 3.0 deliberately and a caller passing nothing arrived identical.
        self.rps_was_chosen = rps is not None
        self.rps = DEFAULT_PER_HOST_RPS if rps is None else rps
        self.overrides = dict(PER_HOST_RPS if overrides is None else overrides)
        self._next_ok: dict[str, float] = defaultdict(float)
        self._blocked_until: dict[str, float] = {}
        # Where a long block is remembered between runs, if anybody set it.
        # See `remember_blocks`.
        self._blocks_path: Path | None = None
        # Consecutive sources on a host that spent all their retries and still
        # got 429. See `note_refusal`.
        self._refusals: dict[str, int] = defaultdict(int)
        # What this host's configured gap is currently multiplied by, and the
        # run of clean answers since it was last widened. See `note_throttle`.
        self._slowdown: dict[str, float] = defaultdict(lambda: 1.0)
        self._ok_run: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def gap_for(self, host: str) -> float:
        """Seconds between requests to `host`. The stricter of the two rules.

        An override BELOW the global rate always wins, because it is a
        ceiling: turning the global rate down for a slow connection must not
        silently turn Workable's 0.7 back up when the caller asked for 0.2.

        An override ABOVE it wins only when the caller did not choose the
        global rate. `min` on its own made every measured override above the
        default a no-op: Greenhouse and Ashby were set to 5.0 and paced at
        3.0, SmartRecruiters to 8.0 and paced at 3.0, and the commit that set
        them claimed a floor of 13.6 minutes that was still 22.7. It shipped
        because the tests asserted the table's contents rather than the
        limiter's behaviour, so they were true while nothing had changed.
        """
        if self.rps <= 0:
            return 0.0
        over = self.overrides.get(host)
        if over is None:
            rps = self.rps
        elif over < self.rps or self.rps_was_chosen:
            rps = min(self.rps, over)
        else:
            rps = over
        if rps <= 0:
            return 0.0
        # Multiplied, not replaced, so a host that has been slowed by its own
        # 429s stays slower than the table asked for rather than being reset
        # to it on the next request.
        return (1.0 / rps) * self._slowdown.get(host, 1.0)

    def block(self, url: str, seconds: float) -> None:
        """Record that this host has shut the door, and for how long.

        Measured on this machine: apply.workable.com answered every request
        with 429 and `Retry-After: 57841`, a sixteen hour block, after a scan
        had aimed all four of its workers at that one host for an hour. The
        old code capped the wait at 30 seconds and retried twice, so each of
        the 2,094 Workable sources cost 60 seconds of sleeping and returned
        nothing: 8.7 hours of a four worker pool spent asking a host that had
        already said no for the rest of the day.
        """
        host = urlparse(url).netloc
        with self._lock:
            until = time.monotonic() + seconds
            if until > self._blocked_until.get(host, 0.0):
                self._blocked_until[host] = until
                self._persist(host, seconds)

    def note_ok(self, url: str) -> None:
        """This host answered. Forget any run of refusals against it.

        A host that is serving is not rate-limiting, so the breaker's count
        has to be a run of CONSECUTIVE refusals rather than a total. Without
        this, a long scan would eventually accumulate enough scattered 429s
        from an otherwise healthy host to arm the breaker against it.

        A run of clean answers also earns back some of the pacing a 429 cost,
        one step at a time. Without that, one refusal early in a scan would
        slow a host for the remaining seventeen thousand sources, and a
        permanent penalty for a momentary refusal is its own kind of wrong.
        """
        host = urlparse(url).netloc
        with self._lock:
            if self._refusals.get(host):
                self._refusals[host] = 0
            if self._slowdown.get(host, 1.0) > 1.0:
                self._ok_run[host] += 1
                if self._ok_run[host] >= OK_RUN_TO_SPEED_UP:
                    self._ok_run[host] = 0
                    self._slowdown[host] = max(
                        1.0, self._slowdown[host] / HOST_SLOWDOWN_STEP)

    def note_throttle(self, url: str) -> float:
        """This host answered 429. Widen its gap, and say what it now is.

        Separate from `note_refusal`, and it fires earlier: `note_refusal`
        only runs once a source has spent every retry and is being given up
        on, which on a host refusing one request in four never happens three
        times in a row. This fires on the refusal itself, including the ones
        that then succeed on retry, because those are the same signal and
        they are the only signal such a host gives.

        Returns the multiplier now in force, so a caller can say out loud
        that it has slowed down rather than doing it silently.
        """
        host = urlparse(url).netloc
        with self._lock:
            self._ok_run[host] = 0
            widened = min(self._slowdown.get(host, 1.0) * HOST_SLOWDOWN_STEP,
                          MAX_HOST_SLOWDOWN)
            self._slowdown[host] = widened
        return widened

    def spend(self, url: str) -> bool:
        """Take one request from this host's budget. False if none is left.

        Called for every request actually about to be sent, retries included,
        and before the pacing wait so a source turned away does not first
        claim a slot and sleep in it. A host with no budget always answers
        True: the budget is for quotas, and pacing is still the rule
        everywhere else.
        """
        host = urlparse(url).netloc
        cap = self.budgets.get(host)
        if cap is None:
            return True
        with self._lock:
            if self._spent[host] >= cap:
                self._turned_away[host] += 1
                return False
            self._spent[host] += 1
            return True

    def budgets_spent(self) -> dict[str, tuple[int, int]]:
        """`{host: (budget, requests turned away)}` for each budget that ran out.

        Only hosts where something was actually refused for want of budget. A
        run that used exactly its budget and then had nothing left to ask
        lost nothing, and saying otherwise would be noise.
        """
        with self._lock:
            return {h: (self.budgets[h], n)
                    for h, n in self._turned_away.items() if n}

    def slowdown_for(self, url: str) -> float:
        """How much this host's configured gap is currently multiplied by."""
        return self._slowdown.get(urlparse(url).netloc, 1.0)

    def note_refusal(self, url: str) -> float:
        """One source has now spent all its retries and still got 429.

        Returns the seconds it just blocked the host for, or 0.0.

        `block` was only ever reachable from the huge-`Retry-After` branch, so
        a 429 carrying no `Retry-After` at all, or one under MAX_RETRY_AFTER,
        left the host unrecorded and every remaining source on it repeated the
        whole retry-and-sleep cycle into the same closed door. On Workable's
        2,094 sources that is the same arithmetic as the 8.7 hour bug the long
        block was added to kill: the header is the only thing that differs, and
        a host is not obliged to send it.

        Deliberately conservative in both directions. It takes
        CONSECUTIVE_429_LIMIT different sources to arm, so one flaky board
        cannot, and the block it sets is BREAKER_BLOCK_SECONDS rather than
        anything open-ended, because a block that outlives the rate limiting
        skips boards that would have answered, and that failure is silent.
        """
        host = urlparse(url).netloc
        with self._lock:
            self._refusals[host] += 1
            if self._refusals[host] < CONSECUTIVE_429_LIMIT:
                return 0.0
            self._refusals[host] = 0
            until = time.monotonic() + BREAKER_BLOCK_SECONDS
            if until <= self._blocked_until.get(host, 0.0):
                return 0.0       # already blocked for longer, by a real header
            self._blocked_until[host] = until
        return BREAKER_BLOCK_SECONDS

    # ------------------------------------------------------------------
    # Remembering a long refusal between runs.
    #
    # `_blocked_until` is monotonic and per process, so a host that answered
    # "Retry-After: 82613" -- not for another 23 hours -- was asked again by
    # the very next `scan` or `validate`, and refused again, every time. The
    # careful handling inside a run was undone by the run ending. Observed on
    # apply.workable.com, which is the one host here whose limit is a
    # long-window quota rather than a rate.
    #
    # Only long blocks are worth writing down. The circuit breaker's own short
    # block is a within-run measure and persisting it would hold a host shut
    # over a transient wobble, which is the opposite failure.
    REMEMBER_ABOVE_SECONDS = 900

    def remember_blocks(self, path) -> None:
        """Read any still-live blocks from `path`, and write new ones there.

        Stored as wall clock, because monotonic means nothing to the next
        process. Wall clock can move -- a laptop that sleeps, a clock that
        syncs -- so an entry claiming more than a day is treated as a clock
        problem and dropped rather than honoured.

        Never raises. A host block that could not be read is not a reason to
        refuse to scan; it is a reason to be careful, and the host will say no
        again if it is still saying no.
        """
        self._blocks_path = Path(path) if path else None
        if not self._blocks_path or not self._blocks_path.exists():
            return
        try:
            saved = json.loads(self._blocks_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            # Valid JSON of the wrong shape. A list here raised
            # AttributeError out of a function whose whole contract is that
            # it never does, which would have taken a scan down over a file
            # nothing important depends on.
            return
        now = time.time()
        with self._lock:
            for host, expires in saved.items():
                try:
                    left = float(expires) - now
                except (TypeError, ValueError):
                    continue
                if 0 < left <= 86400 + self.REMEMBER_ABOVE_SECONDS:
                    self._blocked_until[host] = max(
                        self._blocked_until.get(host, 0.0),
                        time.monotonic() + left)

    def _persist(self, host: str, seconds: float) -> None:
        """Called with the lock held. Never raises."""
        if not self._blocks_path or seconds < self.REMEMBER_ABOVE_SECONDS:
            return
        try:
            try:
                saved = json.loads(
                    self._blocks_path.read_text(encoding="utf-8"))
                saved = saved if isinstance(saved, dict) else {}
            except (OSError, ValueError):
                saved = {}
            now = time.time()
            saved = {h: e for h, e in saved.items()
                     if isinstance(e, (int, float)) and e > now}
            saved[host] = now + seconds
            self._blocks_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._blocks_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(saved, indent=1), encoding="utf-8")
            tmp.replace(self._blocks_path)
        except OSError:
            pass

    def blocked_for(self, url: str) -> float:
        """Seconds left on this host's block, or 0 if it is not blocked."""
        host = urlparse(url).netloc
        with self._lock:
            until = self._blocked_until.get(host)
        return max(0.0, until - time.monotonic()) if until else 0.0

    def wait(self, url: str) -> None:
        """Block until this host's next slot is due, then claim it.

        The slot is claimed under the lock and the sleep happens outside it, so
        a worker waiting four seconds for Workable does not also hold up the
        worker that wants Greenhouse.
        """
        host = urlparse(url).netloc
        while True:
            gap = self.gap_for(host)
            if gap <= 0:
                return
            with self._lock:
                now = time.monotonic()
                if now >= self._next_ok[host]:
                    self._next_ok[host] = now + gap
                    return
                delay = self._next_ok[host] - now
            time.sleep(delay)


def pace_this_thread(limiter: "HostLimiter | None") -> None:
    """Put a limiter where `fetch_one` will find it, for work on this thread.

    Anything that fetches from a pool of its own needs this, not just `scan`.
    `validate` is the one that matters most: it reads every source, and with
    `--prune` it DELETES the ones it read as dead. Its own docstring records a
    429 from a busy platform being reported as a dead board, so an unpaced
    validate is a route to deleting live employers from the list.
    """
    _local.limiter = limiter


def _limiter() -> "HostLimiter | None":
    """The pacing in force on this worker thread, if any.

    Carried on the thread rather than threaded through the signature of all
    eight platform fetchers. Pacing is a property of the run, not of any one
    call, and every one of those fetchers already funnels its requests through
    `fetch_one`, so there is exactly one place that has to consult it.
    """
    return getattr(_local, "limiter", None)



def _thread_session() -> requests.Session:
    """One connection pool per worker thread, reused for every source it handles.

    The pool used to be thrown away after every single request. `fetch_one`
    fell back to a fresh `requests.Session()` whenever a caller passed none,
    and the ordinary dispatch path passed none, so a full scan paid
    a TCP connect and a TLS handshake 17,809 times over. Measured on this
    machine against boards-api.greenhouse.io: six requests took 13.72s with a
    new Session each and 1.08s with one reused Session, which is 2.29s per
    request against 0.18s. It lands hardest exactly where the volume is,
    because 56% of the sources sit on seven API hosts.

    Thread-local rather than one Session shared by every worker: a Session
    mutates its cookie jar and its header dict per request, and sharing that
    across threads is a data race. Per thread it is private, and a pool of
    N workers opens N connections per host at worst rather than one per
    request.

    pool_connections is far above urllib3's default of 10 because the host mix
    is bimodal: seven hosts carry 10,011 of the sources and roughly 7,748
    hosts carry one each. At the default, one stretch of long-tail hosts
    evicts the keep-alive connection to Greenhouse, and the next Greenhouse
    board pays for a fresh handshake anyway, which is the entire cost this
    exists to avoid. max_retries stays 0 so urllib3 never retries behind the
    back of the loop in `fetch_one`, which is the one that honours Retry-After.
    """
    # Keyed on the class, not just on "is there one cached". A cached session
    # built from a previous class would outlive a test that substitutes its own
    # Session and the substitute would never be called, so the test would pass
    # while asserting nothing about the code under test.
    cls = requests.Session
    cached = getattr(_local, "session", None)
    if cached is not None and type(cached) is cls:
        return cached
    s = cls()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=64, pool_maxsize=8, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    _local.session = s
    return s


# The longest this is willing to sit and wait for one source. Past it, the
# host has not asked for a pause, it has said no for the rest of the day, and
# the right answer is to stop asking rather than to sleep in 30 second slices.
MAX_RETRY_AFTER = 60.0

# The circuit breaker for a host that is rate-limiting us without saying for
# how long. See `HostLimiter.note_refusal`.
#
# Three, because it has to be a number no single bad source can reach: three
# DIFFERENT sources on one host, each having exhausted its retries on a 429,
# is a fact about the host. One or two is a board with a problem of its own.
# It costs about twenty seconds to reach on a busy host, against the hours it
# saves there.
CONSECUTIVE_429_LIMIT = 3

# Five minutes, and short on purpose. A block that is too long is the
# dangerous direction: it silently skips boards that would have answered, and
# nothing in the output distinguishes that from a quiet day. Five minutes means
# a host that recovers mid-scan is re-probed roughly twelve times an hour, and
# each re-probe costs at most the three sources it takes to re-arm. A header
# that asks for longer still wins: `block` keeps the later of the two.
BREAKER_BLOCK_SECONDS = 300.0


def retry_after_seconds(value: str | None) -> float | None:
    """`Retry-After` as a number of seconds, whichever of the two forms it is in.

    RFC 9110 allows a count of seconds or an HTTP date, and both are used in
    the wild. Reading only the number meant a date-shaped header fell through
    to plain exponential backoff, so a host that had said "not until 3am" got
    asked again two seconds later.
    """
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (when - now).total_seconds())


# --------------------------------------------------------------------------
# Transport failures that are facts about THIS machine, not about the board
# --------------------------------------------------------------------------
# The alert names OpenSSL and LibreSSL put in `SSLError.reason` when the two
# ends cannot agree on a protocol version or a cipher suite. Every one of them
# happens before a single byte of HTTP is exchanged, so there is no status
# code, no body, and no evidence whatsoever about whether the board still
# exists. `www.roke.co.uk` is the worked example on the maintainer's Mac:
# /usr/bin/python3 is linked against LibreSSL 2.8.3, which cannot complete the
# handshake that host requires and raises TLSV1_ALERT_PROTOCOL_VERSION, while
# `curl` on the same machine links a modern OpenSSL, gets HTTP 200, and the
# payload parses to 34 correct roles.
#
# These are listed by name rather than treating every SSLError alike on
# purpose. CERTIFICATE_VERIFY_FAILED is deliberately NOT here: an expired or
# mis-issued certificate is a fact about the host, is what a browser would
# refuse too, and should keep reading as a broken source.
_HANDSHAKE_REASONS = frozenset({
    "TLSV1_ALERT_PROTOCOL_VERSION",
    "UNSUPPORTED_PROTOCOL",
    "VERSION_TOO_LOW",
    "UNSUPPORTED_PROTOCOL_OR_VERSION",
    "WRONG_VERSION_NUMBER",
    "WRONG_SSL_VERSION",
    "SSLV3_ALERT_HANDSHAKE_FAILURE",
    "TLSV1_ALERT_INTERNAL_ERROR",
    "NO_PROTOCOLS_AVAILABLE",
    "NO_CIPHERS_AVAILABLE",
    "NO_SHARED_CIPHER",
    "SSLV3_ALERT_ILLEGAL_PARAMETER",
    "TLSV1_ALERT_INSUFFICIENT_SECURITY",
    "LEGACY_SIGALG_DISALLOWED_OR_NOT_SUPPORTED",
    "EE_KEY_TOO_SMALL",
    "DH_KEY_TOO_SMALL",
    "UNEXPECTED_EOF_WHILE_READING",
})
# Same alerts as they appear in the exception's text, for the builds that
# leave `reason` as None. LibreSSL 2.8.3 fills `reason` in for the case that
# matters here, but this is cheap and the failure mode of missing one is a
# live board being deleted.
_HANDSHAKE_TEXT = re.compile(
    "|".join(sorted(_HANDSHAKE_REASONS)) + r"|sslv3 alert handshake failure"
    r"|unsupported protocol|wrong version number|no shared cipher",
    re.I)


def ssl_backend() -> str:
    """What this interpreter's `ssl` module is actually linked against.

    Named in the error text because the whole point is to tell the reader the
    problem is on their side of the wire and which library to blame.
    """
    try:
        import ssl as _ssl
        return _ssl.OPENSSL_VERSION
    except Exception:      # pragma: no cover - ssl is always importable
        return "unknown TLS library"


def handshake_failure(exc: BaseException) -> str | None:
    """The name of the TLS alert, if this exception is one; otherwise None.

    A true return value means: nothing was ever heard from the board. It is
    not a 404, it is not an empty board, and `validate --prune` must never
    read it as one.
    """
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        reason = getattr(cur, "reason", None)
        if isinstance(reason, str) and reason.upper() in _HANDSHAKE_REASONS:
            return reason.upper()
        name = type(cur).__name__
        if name in ("SSLError", "SSLEOFError", "SSLZeroReturnError",
                    "SSLSyscallError") or "SSLError" in name:
            m = _HANDSHAKE_TEXT.search(str(cur))
            if m:
                return m.group(0).upper().replace(" ", "_")
        cur = cur.__cause__ or cur.__context__
    return None


def _sleep_backoff(attempt: int, retry_after: str | None) -> None:
    secs = retry_after_seconds(retry_after)
    if secs is not None:
        time.sleep(min(secs, 30.0))
        return
    time.sleep(min(2 ** attempt, 20) + random.uniform(0, 0.75))


def fetch_one(
    src: Source,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    session: requests.Session | None = None,
    # Explicit for a single direct call or a test; left None inside a scan,
    # where `fetch_all` puts the run's shared limiter on the worker thread.
    limiter: "HostLimiter | None" = None,
    # One platform needs a header nothing else does, and it is not optional:
    # Taleo's search endpoint answers 500 "An Error Occurred in TEE" without a
    # `tz` header and 200 with one, whatever the value. Passing it per call
    # rather than adding it to the defaults, so no other platform is sent a
    # header it never asked for.
    extra_headers: dict[str, str] | None = None,
) -> Result:
    s = session or _thread_session()
    headers = {"User-Agent": user_agent, "Accept": "application/json",
               **(extra_headers or {})}
    t0 = time.time()
    last = "unknown error"
    status = None

    lim = limiter or _limiter()
    for attempt in range(retries + 1):
        try:
            # A host that has already said no for the rest of the day is not
            # asked again. Reported as throttled rather than as an error, so
            # `detect_throttling` names it and the reader is told the board is
            # UNKNOWN today. The alternative is what it replaces: 2,094 sources
            # each sleeping 60 seconds into the same closed door and every one
            # of them coming back looking like a board with nothing on it.
            if lim is not None:
                left = lim.blocked_for(src.url)
                if left > 0:
                    return Result(
                        src, error=f"HTTP 429, host blocked for another "
                                   f"{int(left)}s", status=429,
                        elapsed=time.time() - t0, throttled=True)
            # A host whose limit is a quota gets a fixed number of requests a
            # run, checked here so a retry spends from it like anything else.
            # Throttled, not an error of the board's: this is us declining to
            # ask, which is exactly as unknown as the host declining to
            # answer, and `validate --prune` and `detect_throttling` both
            # already read `throttled` as "cannot say". No figure followed by
            # an "s" in the text, because `cmd_scan` reads one out of a
            # throttled error as the host's block length and would print a
            # wait that nobody asked for.
            if lim is not None and not lim.spend(src.url):
                return Result(
                    src, error="not asked: this run's request budget for "
                               "this host is spent, so this source is "
                               "unknown today rather than empty",
                    status=None, elapsed=time.time() - t0, throttled=True)
            # Inside the retry loop, so a retry is paced like a first attempt.
            # A source that just answered 429 is the last one that should be
            # allowed to skip the queue on its way back in.
            if lim is not None:
                lim.wait(src.url)
            if src.method.upper() == "POST":
                r = s.post(src.url, json=src.body or {}, headers=headers, timeout=timeout)
            else:
                # Never attach a body to a GET. Greenhouse 403s on it.
                r = s.get(src.url, headers=headers, timeout=timeout)
            status = r.status_code

            if r.status_code == 429 or 500 <= r.status_code < 600:
                last = f"HTTP {r.status_code}"
                # Every 429, not only the ones that end in giving up.
                # `note_refusal` below runs once a source has spent all its
                # retries, and needs CONSECUTIVE_429_LIMIT of those in a row;
                # a host refusing one request in four never produces that, so
                # nothing here would otherwise ever change the rate. Measured
                # on apply.workable.com: 41 of 419 boards came back unknown
                # and the pacing stayed exactly as it was.
                #
                # Once per SOURCE, not once per attempt. A board that is
                # refused, retried and refused again is one fact about the
                # host, and counting it three times compounds: the step is 2x
                # and the ceiling 8x, so a single unlucky board on its third
                # attempt takes apply.workable.com from a 1.43s gap to 11.43s
                # by itself, and then needs sixty clean answers to climb back.
                #
                # Measured over 2,094 boards by driving the real limiter with
                # no network and summing the gaps: at one refusal in two
                # hundred that is 109 minutes against 49.9, and widening once
                # per source instead brings it to 56.1. Twenty-two extra
                # requests were costing fifty-three minutes, and the cost was
                # the compounding rather than the count.
                if r.status_code == 429 and lim is not None and attempt == 0:
                    lim.note_throttle(src.url)
                wait = retry_after_seconds(r.headers.get("Retry-After"))
                # A Retry-After measured in hours is not a pause, it is a
                # refusal, and retrying it can only fail more slowly. Record it
                # against the host so the other sources on it are spared the
                # same wait, and give up on this one now.
                # Not gated on 429. A 503 with `Retry-After: 3600` is the
                # other standard way a host sheds load, and gating this on 429
                # left it falling through to `_sleep_backoff`, which does
                # `min(secs, 30)`: the clamp-and-retry that cost 8.7 hours of
                # sleeping for zero results, still live on the 5xx branch.
                # `discover` already counts 503 as backpressure everywhere.
                if wait is not None and wait > MAX_RETRY_AFTER:
                    if lim is not None:
                        lim.block(src.url, wait)
                    return Result(src, error=f"HTTP {r.status_code}, retry "
                                             f"after {int(wait)}s", status=status,
                                  elapsed=time.time() - t0, throttled=True)
                if attempt < retries:
                    _sleep_backoff(attempt, r.headers.get("Retry-After"))
                    continue
                # Out of retries and still refused. An ordinary 429 arrives
                # with no usable `Retry-After` and so never reached `block`
                # above; count it, and let the breaker shut the host if this
                # is the third source in a row to end up here.
                if r.status_code == 429 and lim is not None:
                    secs = lim.note_refusal(src.url)
                    if secs:
                        # Worded like the long block, and with the seconds in
                        # it, because `cmd_scan` reads the number back out of
                        # this string to tell the reader which HOST is
                        # rate-limiting them. Without that they get a list of
                        # employers to squint at instead of the one fact that
                        # explains all of them.
                        return Result(
                            src, error=f"HTTP 429, host blocked for another "
                                       f"{int(secs)}s after "
                                       f"{CONSECUTIVE_429_LIMIT} refusals",
                            status=status, elapsed=time.time() - t0,
                            throttled=True)
                return Result(src, error=last, status=status,
                              elapsed=time.time() - t0, throttled=r.status_code == 429)

            if r.status_code >= 400:
                return Result(src, error=f"HTTP {r.status_code}", status=status,
                              elapsed=time.time() - t0)

            # 304 Not Modified carries no body, and the body is the board.
            #
            # Nothing here sends `If-None-Match` or `If-Modified-Since`, so
            # this is unreachable on the current code. It is here because the
            # obvious saving to reach for on a list this size is conditional
            # requests, and bolting them on without this line writes the
            # failure this module keeps having: an empty payload parses to
            # zero postings, `ok` stays True, and the board is recorded as an
            # employer with no vacancies. That is indistinguishable from a
            # real empty board, it is what `validate --prune` deletes, and it
            # is how 250 live employers were once thrown away.
            #
            # Worth saying plainly while the reader is here: a 304 still costs
            # a request. It saves bytes and parsing, and it cannot buy back
            # one second of a host whose limit is counted in REQUESTS, which
            # is the limit that sets this scan's floor. See
            # tests/test_request_budget.py.
            if r.status_code == 304:
                if lim is not None:
                    lim.note_ok(src.url)
                return Result(
                    src, error="HTTP 304, not modified: the response carries "
                               "no postings, so this board is unknown rather "
                               "than empty",
                    status=status, elapsed=time.time() - t0)

            ctype = (r.headers.get("Content-Type") or "").lower()
            # A tuple, not the string "[{": `"" in "[{"` is True, so an
            # empty 200 body took the JSON branch, raised JSONDecodeError and
            # was retried twice before being reported as a transport error
            # rather than as the empty page it was.
            if lim is not None:
                # This host is serving, so whatever run of refusals it had is
                # over. Only a run of them, unbroken, means it is shut.
                lim.note_ok(src.url)
            # requests falls back to ISO-8859-1 for any text/* body whose
            # Content-Type states no charset, which is what RFC 2616 said to
            # do and is wrong for nearly every board here. Personio serves its
            # `/xml` feed as bare `text/xml`, and the feed's own XML
            # declaration says UTF-8: every German board on it came back with
            # "Düsseldorf" spelled "DÃ¼sseldorf" and "München" as "MÃ¼nchen",
            # which no location filter or `--country` flag matches.
            # Only overridden when the bytes really do decode as UTF-8, so a
            # board that is genuinely Latin-1 keeps the old behaviour.
            raw = getattr(r, "content", None)
            if (getattr(r, "encoding", None) and "charset=" not in ctype
                    and isinstance(raw, (bytes, bytearray))):
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    r.encoding = "utf-8"
            if "json" in ctype or r.text.lstrip()[:1] in ("[", "{"):
                return Result(src, payload=r.json(), status=status, elapsed=time.time() - t0)
            return Result(src, payload=r.text, status=status, elapsed=time.time() - t0)

        except requests.RequestException as e:
            alert = handshake_failure(e)
            if alert:
                # Deterministic, and nothing to do with the board. Retrying it
                # cannot change the answer: it costs three connections and two
                # backoff sleeps per source, every scan, to arrive at the same
                # alert. Report it at once, and say whose fault it is, because
                # the bare "SSLError" this used to return is the string that
                # made a live employer indistinguishable from a dead one.
                return Result(
                    src,
                    error=f"TLS handshake failed ({alert}): this machine's "
                          f"Python is linked against {ssl_backend()}, which "
                          f"cannot complete the handshake {urlparse(src.url).hostname} "
                          f"requires. The board was never reached, so this is "
                          f"not evidence it is gone.",
                    status=None, transport=alert, elapsed=time.time() - t0)
            # The class name on its own cannot be acted on. DNS failure,
            # connection refused, connection reset and a handshake aborted
            # mid-stream are all `ConnectionError`, and `validate` prints
            # this string once per source: a run reporting seven thousand of
            # them said nothing about whether the boards were gone or the
            # network had blinked. urllib3 puts the reason in the innermost
            # cause, so carry that up with it.
            last = type(e).__name__
            why = _root_cause(e)
            if why:
                last = f"{last}: {why}"
            if attempt < retries:
                _sleep_backoff(attempt, None)
                continue

    return Result(src, error=last, status=status, elapsed=time.time() - t0)



def _root_cause(e: BaseException, limit: int = 120) -> str:
    """The innermost explanation in a requests or urllib3 exception chain.

    `str(ConnectionError)` is a nest of repr'd pool and socket objects with
    the one useful phrase buried in the middle of it, so walk to the
    innermost cause and keep that. Trimmed, because this ends up on a report
    line next to a company name.
    """
    cur, seen = e, {id(e)}
    while True:
        nxt = getattr(cur, "reason", None) or cur.__cause__ or cur.__context__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        cur = nxt
    text = " ".join(str(cur).split())
    # Whether the chain was walked or the whole thing arrived as one string,
    # the useful phrase sits at the END of it, after the last "Caused by".
    # Trimming from the left would keep "HTTPSConnectionPool(host=..., port=
    # 443): Max retries exceeded" -- which is true of every failure and tells
    # nobody anything -- and cut off the reason.
    if "Caused by" in text:
        text = text.split("Caused by", 1)[1].strip()
    # urllib3 prefixes NewConnectionError with the repr of the connection
    # object, which names a memory address and nothing a reader can use.
    text = re.sub(r"^\w+\(\s*", "", text)
    text = re.sub(r"^['\"]?<[^>]+>:\s*", "", text)
    text = text.strip("'\")( ")
    if not text or text == type(e).__name__:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _no_rows(src: Source, first_error: "Result | None",
             answered: bool) -> Result:
    """What a paged fetcher returns when it walked away with no rows.

    Three of the fetchers here spelled this `Result(src, error="no pages
    returned")`, which says two different things in one string and gets one of
    them wrong. A board that never answered and a board that answered with an
    empty result set are not the same event, and the difference is the one
    this whole file exists to keep: `ok` False means "we could not read it",
    and a search that legitimately matched nothing was being reported that
    way.

    Measured live on 2026-08-26: Orano's Avature board, searched for a title
    it has no vacancy for, answers HTTP 200 with a perfectly good results page
    holding zero rows, and `fetch_avature` returned `ok=False, error="no pages
    returned"`. `cmd_scan` counts that source under "did not respond" instead
    of "responded with no postings at all", so the run's own summary is wrong
    about which of the two happened, and the reader is told nothing about a
    board that answered them.

    So: a real failure wins, because half an answer is not an answer. Failing
    that, a board that answered gets an empty payload, which every parser here
    reads as no jobs. Only a fetcher that never got as far as a response
    reports one.
    """
    if first_error is not None:
        return first_error
    if answered:
        # `ok` is True: error is None and the payload is a string, empty.
        # The board was read and had nothing in it, which is a fact about the
        # board rather than about us.
        return Result(src, payload="")
    return Result(src, error="no page was ever requested, so nothing is "
                             "known about this board")


def fetch_workday(
    src: Source,
    terms: list[str],
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 10,
) -> Result:
    """Workday needs its own path, for two reasons.

    Its page size is hard-capped at 20 (asking for 100 returns a 400), and its
    boards are enormous: Barclays reports 1,055 open roles. Paging through that
    for every enterprise tenant would be dozens of requests each, per scan, for
    results almost all of which get discarded by the title filter anyway.

    Workday is also the only platform here with server-side search, so the
    filtering happens at their end instead: one query per wanted title,
    shallowly paged. That turns a thousand postings into the handful that
    matter, in a few requests rather than fifty.

    `max_pages` was 3, and three pages was too few by a wide margin. Workday's
    `searchText` is a FULL-TEXT match, not a title match, so a wanted title
    scores against every posting whose description happens to mention it, and
    the genuine matches are NOT all at the top. Measured against NVIDIA for
    "engineering manager": page 1 held 20 real title matches, pages 4 and 5
    held none, and pages 7, 8 and 9 held another 19 between them. Relevance
    does not decay monotonically, so stopping early on a quiet page would be
    just as wrong as the old fixed cap; the only honest answer is to page
    further and say when the cap bit.

    Across 30 tenants that took matching titles from 83 to 134. It cost 139
    requests instead of 243, and almost all of that went to the five biggest
    boards: 24 of the 30 spent exactly the same three requests as before,
    because a board smaller than one page still stops on its first short page.
    """
    session = _thread_session()
    merged: dict[str, dict] = {}
    total = 0
    first_error: Result | None = None
    truncated = False

    for term in (terms or [""])[:3]:
        # Per term, and captured from the FIRST page of that term's search.
        # Workday reports the real hit count on page one and then returns
        # `total: 0` on every later page of the same search, so a running
        # `max()` across the whole walk is the only reading of it that is
        # stable, and a per-term copy is the only one that can bound a term.
        term_total = 0
        for page in range(max_pages):
            probe = Source(
                company=src.company, url=src.url, platform="workday",
                sector=src.sector, country=src.country, domain=src.domain,
                method="POST",
                body={"appliedFacets": {}, "limit": 20,
                      "offset": page * 20, "searchText": term},
            )
            res = fetch_one(probe, timeout=timeout, retries=retries,
                            user_agent=user_agent, session=session)
            if not res.ok or not isinstance(res.payload, dict):
                # Carry status and throttled, not just the string. Dropping
                # them made a 429 from a Workday tenant indistinguishable from
                # a broken board: `detect_throttling` did not count it and
                # `validate --prune` saw a candidate for deletion.
                if first_error is None:
                    first_error = Result(src, error=res.error or "bad payload",
                                         status=res.status,
                                         throttled=res.throttled,
                                         transport=res.transport)
                break
            posts = res.payload.get("jobPostings") or []
            page_total = int(res.payload.get("total") or 0)
            total = max(total, page_total)
            if page == 0:
                term_total = page_total
            for p in posts:
                key = p.get("externalPath") or p.get("title")
                if key:
                    merged.setdefault(key, p)
            if len(posts) < 20:
                break
            # The cap bit while this term still had hits to give. Said out
            # loud rather than returned as though it were everything: the
            # first 200 of 1,055 reads exactly like a complete answer, and a
            # reader has no way to tell them apart. `ok` stays True because
            # the rows are real; there are simply more of them.
            if page == max_pages - 1 and term_total > max_pages * 20:
                truncated = True

    if not merged and first_error is not None:
        return first_error
    return Result(src, payload={"jobPostings": list(merged.values()),
                                "total": total}, truncated=truncated)


def fetch_workable_search(
    src: Source,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 15,
) -> Result:
    """Walk jobs.workable.com's search, which pages twenty at a time.

    Same hard cap as Workday: `limit=100` is a 400, and pageSize, size,
    per_page and page_size are all accepted and all ignored. Unlike Workday
    the cursor is opaque, a `nextPageToken` that has to come back from the
    previous page, so the pages cannot be requested in parallel and the walk
    is strictly sequential.

    max_pages of 15 is 300 postings per title, which covered every query
    tried here with the token exhausted before the cap. It is a guard against
    a very broad title, not a sample: when it does bite, the scan says so
    rather than quietly returning the first 300.

    Deliberately a different host from apply.workable.com, so a block on one
    does not silently take out the other and the two queues do not wait on
    each other. Not a faster one, though: it is the same company's
    infrastructure, it refused for a day at the end of every scan run at 3.0/s,
    and it is now paced like the boards and capped per run by
    `HOST_REQUEST_BUDGET`. See `PER_HOST_RPS`.
    """
    session = _thread_session()
    merged: dict[str, dict] = {}
    first_error: Result | None = None
    total = 0
    token = None
    truncated = False

    for page in range(max_pages):
        url = src.url
        if token:
            url += ("&" if "?" in url else "?") + "pageToken=" + quote_plus(token)
        probe = Source(company=src.company, url=url, platform="workable",
                       sector=src.sector, country=src.country)
        res = fetch_one(probe, timeout=timeout, retries=retries,
                        user_agent=user_agent, session=session)
        if not res.ok or not isinstance(res.payload, dict):
            # Carried, not flattened to a string, for the same reason as every
            # other paged fetcher here: a 429 has to stay distinguishable from
            # a broken search or `detect_throttling` will not count it.
            if first_error is None:
                first_error = Result(src, error=res.error or "bad payload",
                                     status=res.status, throttled=res.throttled,
                                     transport=res.transport)
            # A walk that already has pages and then stops on a refusal, a
            # block or a spent budget has NOT reached the end of the search:
            # the token said there was more. It used to return those pages
            # with `truncated` False, so a search refused on page three read
            # as a complete two-page answer.
            if merged:
                truncated = True
            break
        jobs = res.payload.get("jobs") or []
        total = max(total, int(res.payload.get("totalSize") or 0))
        for j in jobs:
            key = j.get("id") or j.get("url")
            if key:
                merged.setdefault(key, j)
        token = res.payload.get("nextPageToken")
        if not token or not jobs:
            break
    else:
        truncated = bool(token)

    if not merged and first_error is not None:
        return first_error
    # On the Result, not inside the payload. A parser should not have to know
    # about a fetcher's paging, and `detect_throttling` and the scan summary
    # read Results, not payloads.
    return Result(src, payload={"jobs": list(merged.values()),
                                "totalSize": total}, truncated=truncated)


def fetch_nhs(
    src: Source,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 5,
) -> Result:
    """NHS Jobs returns ten results a page and has no page-size parameter.

    The search is already narrowed by keyword in the source URL, so a handful
    of pages is plenty; walking all 10,000 results would be both slow and
    pointless when the title filter discards almost all of them.
    """
    session = _thread_session()
    parts: list[str] = []
    first_error: Result | None = None
    answered = False
    sep = "&" if "?" in src.url else "?"

    truncated = False
    for page in range(1, max_pages + 1):
        probe = Source(company=src.company, url=f"{src.url}{sep}page={page}",
                       platform="nhs", sector=src.sector, country=src.country)
        res = fetch_one(probe, timeout=timeout, retries=retries,
                        user_agent=user_agent, session=session)
        if not res.ok or not isinstance(res.payload, str):
            # Same rule as every other paged fetcher here. NHS Jobs was the
            # one that dropped status and throttled, so a rate-limited NHS
            # came back as "no pages returned" and `discover` was once
            # scheduled to prune it as dead.
            first_error = Result(src, error=res.error or "bad payload",
                                 status=res.status, throttled=res.throttled,
                                 transport=res.transport)
            break
        answered = True
        if "search-result" not in res.payload:
            break
        parts.append(res.payload)
        # A short page means the results ran out.
        if res.payload.count('data-test="search-result"') < 10:
            break

    # for/else fires only when the range is exhausted, which means
    # the cap stopped the walk rather than the board running out.
    # Said out loud: the first N of an unknown number reads exactly
    # like a complete answer. See Result.truncated.
    else:
        truncated = True
    if not parts:
        return _no_rows(src, first_error, answered)
    return Result(src, payload="".join(parts), truncated=truncated)


# Reed hard-limits a page to 100 and documents it. Three pages per keyword is
# 300 postings for one job title, which is far past the point the title filter
# has stopped discarding things, and `expand_templates` already makes one of
# these per title in `titles.include`.
REED_PAGE = 100


def fetch_reed(
    src: Source,
    api_key: str,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 3,
) -> Result:
    """Reed's jobseeker API. Keyed, and paged with resultsToSkip.

    The key goes in as the HTTP Basic username with an empty password, which
    is Reed's own documented scheme. It is set on the session rather than
    built into the URL, so it never lands in a log line, a saved source list
    or an error message.

    With no key this returns a stated error rather than fetching. Reed answers
    401 to an unkeyed request, and a 401 arriving through the ordinary path
    would be reported next to genuinely broken boards as "could not be read",
    which tells the reader nothing about the one thing they need to do.
    """
    if not api_key:
        return Result(src, error="no Reed API key: set sources.reed_api_key in "
                                 "your config, or the REED_API_KEY environment "
                                 "variable. Free key: "
                                 "https://www.reed.co.uk/developers/jobseeker")

    session = _thread_session()
    # (key, "") is Basic auth with an empty password, which is what Reed asks
    # for. requests base64-encodes it into the Authorization header.
    #
    # Restored in the `finally` because this session is the WORKER THREAD'S,
    # shared by every source that thread goes on to handle. `session.auth` is
    # a session-wide default that requests merges into every later request, so
    # setting it and walking away sent `Authorization: Basic <reed key>` to
    # every Greenhouse, Ashby and one-off employer domain that worker touched
    # for the rest of the scan. The key is the user's private credential and
    # those are thousands of third parties' access logs.
    previous_auth = session.auth
    session.auth = (api_key, "")
    try:
        sep = "&" if "?" in src.url else "?"

        merged: dict[Any, dict] = {}
        total = 0
        first_error: Result | None = None

        for page in range(max_pages):
            probe = Source(
                company=src.company,
                url=f"{src.url}{sep}resultsToTake={REED_PAGE}"
                    f"&resultsToSkip={page * REED_PAGE}",
                platform="reed", sector=src.sector, country=src.country,
            )
            res = fetch_one(probe, timeout=timeout, retries=retries,
                            user_agent=user_agent, session=session)
            if not res.ok or not isinstance(res.payload, dict):
                # Carry the failure on the ORIGINAL source, not the paged
                # probe: `detect_throttling` and the state file key on
                # `source.key`, and a URL with resultsToSkip in it is a
                # different key every page.
                first_error = Result(src, error=res.error or "bad payload",
                                     status=res.status, throttled=res.throttled,
                                     transport=res.transport)
                break
            rows = res.payload.get("results") or []
            total = max(total, int(res.payload.get("totalResults") or 0))
            for r in rows:
                if isinstance(r, dict) and r.get("jobId") is not None:
                    merged.setdefault(r["jobId"], r)
            if len(rows) < REED_PAGE:
                break
    finally:
        session.auth = previous_auth

    if not merged and first_error is not None:
        return first_error
    return Result(src, payload={"results": list(merged.values()),
                                "totalResults": total})


# Adzuna's documented ceiling is 50 a page. Three pages of one job title is
# 150 postings, which is well past the point the title filter has stopped
# discarding anything, and `expand_templates` already makes one of these per
# entry in `titles.include`. It also has to be counted against the free tier:
# six titles at three pages is eighteen calls a scan, and the free limits are
# 250 a day and 2,500 a month.
ADZUNA_PAGE = 50

# The page number lives in the PATH (/search/1), not in a query parameter, so
# paging means rewriting the URL rather than appending to it.
_ADZUNA_PAGE_PATH = re.compile(r"(/v1/api/jobs/[a-z]{2}/search/)\d+", re.I)


def fetch_adzuna(
    src: Source,
    app_id: str,
    app_key: str,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 3,
) -> Result:
    """Adzuna's search API. Two credentials, both in the query string.

    Adzuna offers no header authentication, so unlike Reed the credential
    cannot be kept out of the URL. What it can be kept out of is everything
    that outlives the request: the paged, credentialled URL is built onto a
    throwaway probe, and every Result returned from here carries the ORIGINAL
    source. The state file and `detect_throttling` key on `source.key`, so
    returning the probe would write an app_key into state.json and into the
    source list this repo publishes.

    With no credentials this returns a stated error rather than fetching.
    Adzuna answers an unkeyed request with 400 and an HTML error page, which
    through the ordinary path is reported as "could not be read" next to
    genuinely broken boards, and tells the reader nothing about the one thing
    they need to do.
    """
    if not (app_id and app_key):
        return Result(src, error="no Adzuna credentials: set sources.adzuna_app_id "
                                 "and sources.adzuna_app_key in your config, or the "
                                 "ADZUNA_APP_ID and ADZUNA_APP_KEY environment "
                                 "variables. Free: "
                                 "https://developer.adzuna.com/signup")

    session = _thread_session()
    # The shipped URL already asks for a page size, so drop any that is there
    # before adding ours. Sending the same parameter twice leaves Adzuna to
    # choose between two values and makes the paging arithmetic below a guess.
    # Rebuilt through urlencode rather than cut out with a regex: stripping
    # "?results_per_page=50" out of a URL where it happens to come first takes
    # the "?" with it and turns every remaining parameter into part of the path.
    _u = urlsplit(src.url)
    _q = [(k, v) for k, v in parse_qsl(_u.query, keep_blank_values=True)
          if k != "results_per_page"]
    base = urlunsplit((_u.scheme, _u.netloc, _u.path, urlencode(_q), ""))
    sep = "&" if "?" in base else "?"
    merged: dict[Any, dict] = {}
    total = 0
    first_error: Result | None = None

    truncated = False
    for page in range(1, max_pages + 1):
        paged = _ADZUNA_PAGE_PATH.sub(rf"\g<1>{page}", base)
        probe = Source(
            company=src.company,
            url=f"{paged}{sep}app_id={app_id}&app_key={app_key}"
                f"&results_per_page={ADZUNA_PAGE}",
            platform="adzuna", sector=src.sector, country=src.country,
        )
        res = fetch_one(probe, timeout=timeout, retries=retries,
                        user_agent=user_agent, session=session)
        if not res.ok or not isinstance(res.payload, dict):
            first_error = Result(src, error=res.error or "bad payload",
                                 status=res.status, throttled=res.throttled,
                                 transport=res.transport)
            break
        rows = res.payload.get("results") or []
        total = max(total, int(res.payload.get("count") or 0))
        for r in rows:
            if isinstance(r, dict) and r.get("id") is not None:
                merged.setdefault(r["id"], r)
        # Stop on an empty page or once we hold everything Adzuna says exists,
        # never on a short one. `results_per_page` is a request, not a promise:
        # if Adzuna quietly caps a page below what we asked for, "shorter than
        # we asked" is true on every page and stopping there would throw away
        # everything past the first.
        if not rows or len(merged) >= total > 0:
            break

    # for/else fires only when the range is exhausted, which means
    # the cap stopped the walk rather than the board running out.
    # Said out loud: the first N of an unknown number reads exactly
    # like a complete answer. See Result.truncated.
    else:
        truncated = True
    if not merged and first_error is not None:
        return first_error
    return Result(src, payload={"results": list(merged.values()), "count": total},
                  truncated=truncated)


def fetch_phenom(
    src: Source,
    terms: list[str] | None = None,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 4,
) -> Result:
    """Phenom's search page embeds only the first ten results, but the site is
    driven by a `/widgets` POST endpoint that returns fifty at a time and
    reports the true total. Serco publish 359 roles; ten of them is not a
    useful view of an employer.
    """
    from urllib.parse import urlparse

    session = _thread_session()
    host = urlparse(src.url).netloc
    # Prefer the country in the URL, then the one the source is tagged with.
    # "gb" is only the last resort: a UK default baked in below the config
    # layer is invisible to anyone who is not in the UK.
    country = (src.country or "gb").lower()
    if country == "uk":
        country = "gb"
    m = re.search(r"//[^/]+/([a-z]{2})/", src.url)
    if m:
        country = m.group(1)

    merged: dict[str, dict] = {}
    total = 0
    # One narrow search per wanted title, the same shape as Workday, Avature
    # and RMK. Serco publish 359 roles and four unfiltered pages of fifty stop
    # at 200 of them, so an unfiltered walk was quietly deciding which 200 of
    # an employer's roles this tool would ever see. `keywords` is server-side,
    # so narrowing first is both more complete and fewer requests.
    for term in (terms or [""])[:3]:
        # Counted per term, not against `merged`: two titles overlap, and a
        # shared counter would call the second search complete on the first
        # one's rows.
        got = 0
        truncated = False
        for page in range(max_pages):
            probe = Source(
                company=src.company, url=f"https://{host}/widgets", platform="phenom",
                sector=src.sector, country=src.country, method="POST",
                body={"lang": f"en_{country}", "deviceType": "desktop", "country": country,
                      "pageName": "search-results", "ddoKey": "refineSearch",
                      "from": page * 50, "size": 50, "jobs": True, "counts": True,
                      "all_fields": [], "keywords": term, "global": True,
                      "siteType": "external", "clearAll": False},
            )
            res = fetch_one(probe, timeout=timeout, retries=retries,
                            user_agent=user_agent, session=session)
            if not res.ok or not isinstance(res.payload, dict):
                break
            er = res.payload.get("refineSearch") or {}
            jobs = (er.get("data") or {}).get("jobs") or []
            total = max(total, int(er.get("totalHits") or 0))
            for j in jobs:
                key = j.get("jobSeqNo") or j.get("jobId") or j.get("applyUrl")
                if key:
                    merged.setdefault(key, j)
            got += len(jobs)
            # Stop on an empty page, or once this term's whole result set is
            # held. Never on a short one: `size` is a request, not a promise,
            # and a site that caps a page below fifty makes every page short,
            # so "shorter than we asked" would throw away everything past the
            # first page of every board.
            if not jobs or got >= int(er.get("totalHits") or 0) > 0:
                break

        # for/else fires only when the range is exhausted, which means
        # the cap stopped the walk rather than the board running out.
        # Said out loud: the first N of an unknown number reads exactly
        # like a complete answer. See Result.truncated.
        else:
            truncated = True
    if not merged:
        # Fall back to the ten embedded in the page rather than returning none.
        return fetch_one(src, timeout=timeout, retries=retries, user_agent=user_agent)
    return Result(src, payload={"refineSearch": {"data": {"jobs": list(merged.values())},
                                                 "totalHits": total}}, truncated=truncated)


def fetch_amazon(
    src: Source,
    terms: list[str] | None = None,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 100,
) -> Result:
    """Walk `amazon.jobs/en/search.json` on `offset`, a hundred rows a time.

    A hundred is the ceiling and it is enforced by silence: ask for 500 and
    the response is `{"hits": 0, "jobs": null}`, a clean 200 saying the board
    is empty. Measured, not read in a document. So the page size is fixed here
    rather than raised hopefully, and `PAGE_SIZES` carries it so a board whose
    whole result is exactly a hundred is visibly a paging bug.

    `hits` caps at 10000 and `offset` past it returns nothing, whatever the
    board really holds. That is a truncation the API performs on us and it
    cannot be paged around, so a run that reaches it is reported truncated
    rather than passed off as the whole board: the alternative is a source
    that silently forgets everything after ten thousand and looks complete
    doing it. Country-scoped sources stay well under it, which is why the
    bundled entry is scoped.
    """
    session = _thread_session()
    seen: dict[Any, dict] = {}
    total = 0
    truncated = False
    for page in range(max_pages):
        probe = Source(
            company=src.company,
            url=_with_query(src.url, offset=str(page * AMAZON_PAGE)),
            platform="amazon", sector=src.sector, country=src.country,
        )
        res = fetch_one(probe, timeout=timeout, retries=retries,
                        user_agent=user_agent, session=session)
        if not res.ok or not isinstance(res.payload, dict):
            truncated = bool(seen)
            if not seen:
                return res
            break
        rows = res.payload.get("jobs") or []
        total = max(total, int(res.payload.get("hits") or 0))
        for j in rows:
            if isinstance(j, dict) and j.get("id_icims") is not None:
                seen.setdefault(j["id_icims"], j)
        # Stop on an empty page or once the board's own count is held. Never
        # on a short one: a short page here is the ordinary last page.
        if not rows or (total and len(seen) >= total):
            break
    else:
        truncated = True
    if total >= AMAZON_HIT_CAP:
        truncated = True
    return Result(src, payload={"jobs": list(seen.values()), "hits": total},
                  truncated=truncated)


def fetch_pcsx(
    src: Source,
    terms: list[str] | None = None,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 400,
) -> Result:
    """Walk `/api/pcsx/search` on `start`, ten rows at a time.

    Ten is not a choice. `num` and `pgSz` are both accepted and both ignored:
    asked for a hundred, the endpoint answers with ten and a 200. So
    Microsoft's 2,120 postings are 212 requests, against one for a Greenhouse
    board, and `max_pages` is set above that rather than near it.

    The stop is `data.count`, which is the board's own total, and an empty
    page. Never a short page: every page here is short by definition, and
    stopping on "fewer than we asked for" would return the first ten postings
    of every PCSX employer and look like a complete read.

    Deliberately unfiltered. `query=` works server-side and would cut this to
    a handful of requests, but the terms come from the reader's config, and a
    source that reads only the titles one person asked for is that person's
    search saved as an employer list. See CLAUDE.md.
    """
    session = _thread_session()
    seen: dict[Any, dict] = {}
    total = 0
    truncated = False
    for page in range(max_pages):
        probe = Source(
            company=src.company, url=_with_query(src.url, start=str(page * 10)),
            platform="pcsx", sector=src.sector, country=src.country,
        )
        res = fetch_one(probe, timeout=timeout, retries=retries,
                        user_agent=user_agent, session=session)
        if not res.ok or not isinstance(res.payload, dict):
            # A failure mid-walk is not an employer with fewer jobs. Carry
            # what we have and say it is partial, so nothing downstream reads
            # a truncated board as a shrinking one and prunes it.
            truncated = bool(seen)
            if not seen:
                return res
            break
        data = res.payload.get("data") or {}
        rows = [j for j in (data.get("positions") or []) if isinstance(j, dict)]
        total = max(total, int(data.get("count") or 0))
        for j in rows:
            key = j.get("id") or j.get("positionUrl")
            if key is not None:
                seen.setdefault(key, j)
        if not rows or (total and len(seen) >= total):
            break
    else:
        truncated = True
    return Result(src, payload={"data": {"positions": list(seen.values()),
                                         "count": total}},
                  truncated=truncated)


# Google Careers boots its own JavaScript from an `AF_initDataCallback` block.
# `ds:1` is the search results; `ds:0` is chrome. Anchored on the key so a
# page that gains another block does not silently start returning the wrong one.
_G_BOOT = re.compile(
    r"AF_initDataCallback\(\{[^}]*?key:\s*'ds:1'.*?data:\s*(\[.*?\])\s*,\s*sideChannel",
    re.S)
GOOGLE_PAGE = 20


def _google_payload(html: str) -> tuple[Any, int]:
    """The `ds:1` rows and the board's own total, or `(None, 0)`.

    The total matters more than it looks. Google answer a page past the end
    with a 200 and an empty list, which is indistinguishable from an employer
    with no vacancies unless you know what the board said its size was.
    """
    m = _G_BOOT.search(html or "")
    if not m:
        return None, 0
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None, 0
    if not isinstance(data, list) or not data:
        return None, 0
    # Positional, so read the position. The trailing cells are
    # `[rows, null, total, pageSize]`, and taking `max()` of them returns the
    # page size for any board smaller than one page: a 7-role employer would
    # have reported a total of 20. Index 2, or nothing.
    total = data[2] if len(data) > 2 and isinstance(data[2], int) else 0
    return data, max(total, 0)


def fetch_google_careers(
    src: Source,
    terms: list[str] | None = None,
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 200,
) -> Result:
    """Walk the careers search on `page`, twenty rows at a time.

    Twenty is the board's, not ours: the payload states its own page size and
    ignores anything asked for. 3,528 postings globally is 177 requests, so
    `max_pages` sits above that rather than near it.

    The stop is the board's stated total, or a page that returns no rows.
    Never a short page: the last page is short by definition.

    Deliberately unfiltered. `q=` works server-side and would cut this to a
    handful of requests, but the terms come from the reader's config, and a
    source that reads only the titles one person asked for is that person's
    search saved as an employer list. Any `location=` already on the source
    URL is the source's own scope and is left alone. See CLAUDE.md.
    """
    session = _thread_session()
    seen: dict[Any, list] = {}
    total = 0
    truncated = False
    for page in range(max_pages):
        probe = Source(
            company=src.company, url=_with_query(src.url, page=str(page + 1)),
            platform="google_careers", sector=src.sector, country=src.country,
        )
        res = fetch_one(probe, timeout=timeout, retries=retries,
                        user_agent=user_agent, session=session)
        html_text = res.payload if isinstance(res.payload, str) else None
        if not res.ok or html_text is None:
            truncated = bool(seen)
            if not seen:
                return res
            break
        data, stated = _google_payload(html_text)
        if data is None:
            # The page came back but carried no boot payload. That is this
            # adapter failing to read Google, not Google having no jobs, and
            # the two must not look the same: an empty board is what
            # `validate --prune` deletes sources for.
            if seen:
                truncated = True
                break
            return Result(src, status=res.status,
                          error="no ds:1 payload in the careers page: the "
                                "boot format changed, or the response was a "
                                "consent or challenge page")
        total = max(total, stated)
        rows = data[0] if isinstance(data[0], list) else []
        for row in rows:
            if isinstance(row, list) and row and isinstance(row[0], str):
                seen.setdefault(row[0], row)
        if not rows or (total and len(seen) >= total):
            break
    else:
        truncated = True
    return Result(src, payload=[list(seen.values()), None, total, GOOGLE_PAGE],
                  truncated=truncated)


def _with_query(url: str, **params: str) -> str:
    """Replace query parameters, rather than appending a second copy.

    Appending is what a naive `url + "&startrow=25"` does, and the shipped
    Avature and RMK URLs already carry `q=` and `jobRecordsPerPage=`. Two
    values for one parameter leaves the server to pick, and the paging
    arithmetic here becomes a guess. Same reasoning as `fetch_adzuna`, which
    learned it the expensive way.
    """
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
         if k not in params]
    q += [(k, v) for k, v in params.items()]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


# The link Avature renders for "Next >>". Following it beats computing the
# next offset ourselves, because the page size is the tenant's choice and not
# ours: Tesco answers ten rows however many `jobRecordsPerPage` asks for, and
# advertises `jobRecordsPerPage=10&jobOffset=10` in this very link. Stepping
# by the size we requested would have skipped forty rows out of every fifty.
_AV_NEXT = re.compile(
    r'class="[^"]*paginationNextLink[^"]*"\s+href="([^"]+)"', re.I)
# `[^"?]` and not `[^"]`: every Avature card also carries Twitter and
# Facebook share links whose query string contains the job URL. Those are
# not rows, and counting them as "fresh" would keep the pager walking a
# board that had already run out.
_AV_JOB = re.compile(r'href="(https?://[^"?]*?/JobDetail/[^"?]+)"', re.I)


def fetch_avature(
    src: Source,
    terms: list[str],
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 4,
) -> Result:
    """Avature's search page, paged, and narrowed at the server where possible.

    One request returns whatever the tenant configured, which for Tesco is ten
    rows out of "999+". A source that quietly returns ten of three thousand is
    worse than one that fails: `validate` calls it live, the scan reports it
    healthy, and nobody finds out. So this pages.

    Paging alone is not enough either. Ten at a time through a 999+ board is a
    hundred requests per scan for an employer whose roles are almost all
    discarded by the title filter. `semanticSearch=` is a real server-side
    keyword filter (47 results for "engineering manager" against 999+ with no
    filter), so this does what `fetch_workday` does: one narrow search per
    wanted title, paged shallowly, instead of one deep unfiltered walk.

    The page cap is hard and applies per term. There is no total anywhere in
    the markup to stop on, so the stop condition is "no next link, or no rows
    we have not already seen", and a cap is the only thing standing between a
    broken stop condition and a loop that never ends.
    """
    session = _thread_session()
    pages: list[str] = []
    seen: set[str] = set()
    first_error: Result | None = None
    # Whether the board ever answered, whatever was in the answer. See
    # `_no_rows` for why that is not the same question as whether any page
    # was kept.
    answered = False

    # An empty term means the unfiltered board, which is right for the small
    # tenants: Metro Bank publish six roles and a keyword search would only
    # hide four of them.
    truncated = False
    for term in (terms or [""])[:3]:
        url = _with_query(src.url, semanticSearch=term) if term else src.url
        for _ in range(max_pages):
            probe = Source(company=src.company, url=url, platform="avature",
                           sector=src.sector, country=src.country)
            res = fetch_one(probe, timeout=timeout, retries=retries,
                            user_agent=user_agent, session=session)
            if not res.ok or not isinstance(res.payload, str):
                # Carry the failure on the ORIGINAL source: `detect_throttling`
                # and the state file key on `source.key`, and a URL with an
                # offset in it is a different key on every page.
                first_error = first_error or Result(
                    src, error=res.error or "bad payload", status=res.status,
                    throttled=res.throttled,
                    transport=res.transport)
                break
            answered = True
            fresh = {u for u in _AV_JOB.findall(res.payload)} - seen
            if not fresh:
                break
            seen |= fresh
            pages.append(res.payload)
            nxt = _AV_NEXT.search(res.payload)
            if not nxt:
                break
            url = nxt.group(1).replace("&amp;", "&")

        # for/else fires only when the range is exhausted, so the cap
        # stopped the walk rather than the board running out. The first
        # N of an unknown number reads exactly like a complete answer.
        # See Result.truncated.
        else:
            truncated = True
    if not pages:
        return _no_rows(src, first_error, answered)
    # `parse_avature` already drops a repeated /JobDetail/ link, so joining the
    # pages is safe and keeps the parser a pure function of one HTML string.
    return Result(src, payload="".join(pages), truncated=truncated)


# SuccessFactors RMK pages on `startrow` and serves twenty-five rows, and no
# part of the markup states a total: "Showing {0} to {1}" is a client-side
# template with the numbers filled in by JavaScript we never run. Verified
# against Transport for London, where startrow=0 returns 24 and startrow=10
# returns 14, so the offset is honoured and TfL really does have 24 rather
# than being truncated.
RMK_PAGE = 25


def fetch_rmk(
    src: Source,
    terms: list[str],
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 4,
) -> Result:
    """SuccessFactors RMK, paged on startrow and narrowed with `q`.

    Without this the adapter reads exactly the first twenty-five rows of every
    tenant and reports that as the board. SAP's own careers site sits on this
    platform and publishes thousands.

    `q` is server-side (TfL: 24 rows unfiltered, 10 for "engineer"), so the
    same shape as Workday and Avature applies: search per wanted title rather
    than walk the whole board.
    """
    session = _thread_session()
    pages: list[str] = []
    seen: set[str] = set()
    first_error: Result | None = None
    answered = False

    for term in (terms or [""])[:3]:
        truncated = False
        for page in range(max_pages):
            url = _with_query(src.url, q=term, startrow=str(page * RMK_PAGE))
            probe = Source(company=src.company, url=url, platform="rmk",
                           sector=src.sector, country=src.country)
            res = fetch_one(probe, timeout=timeout, retries=retries,
                            user_agent=user_agent, session=session)
            if not res.ok or not isinstance(res.payload, str):
                first_error = first_error or Result(
                    src, error=res.error or "bad payload", status=res.status,
                    throttled=res.throttled,
                    transport=res.transport)
                break
            answered = True
            fresh = set(re.findall(r'href="([^"]*?/job/[^"?]+)"',
                                   res.payload)) - seen
            # Stop on nothing new, never on a short page. A tenant that serves
            # fewer than RMK_PAGE rows per page would otherwise be truncated
            # to its first page on every scan, which is the exact fault this
            # function exists to fix.
            if not fresh:
                break
            seen |= fresh
            pages.append(res.payload)

        # for/else fires only when the range is exhausted, which means
        # the cap stopped the walk rather than the board running out.
        # Said out loud: the first N of an unknown number reads exactly
        # like a complete answer. See Result.truncated.
        else:
            truncated = True
    if not pages:
        return _no_rows(src, first_error, answered)
    return Result(src, payload="".join(pages), truncated=truncated)


# Taleo's career section page is a JavaScript shell. It carries the search
# form, the facet panel and nothing else: zero job rows, on every one of the
# seven live boards checked. The rows come from a JSON endpoint the page
# calls, and three things about that endpoint are worth writing down.
#
# It needs a `tz` request header. Without one it answers **HTTP 500** with the
# body "An Error Occurred in TEE"; with one it answers 200. The value is not
# validated at all, `tz: x` works, so this is a required-field check rather
# than anything meaningful. Nothing else is required: no cookie, no session,
# no CSRF token, no referer, no browser user agent. That last point is what
# separates Taleo from Cornerstone OnDemand, whose equivalent endpoint answers
# 401 "no Authorization header found" and can only be reached by lifting a
# token out of the page. See the README.
#
# It is addressed by a `portal` number that appears nowhere but inside the
# page, which is why this is a two-step: read the page, then call the API.
# The number is per-tenant and not unique across them. BAE Systems and
# D.R. Horton both sit on portal 101430233 and return 159 and 578 different
# postings respectively, so the number identifies nothing on its own.
#
# And it pages badly, in two separate ways that both read as success:
#
#   * `pageSize` in the request is ignored, but echoed back in the response.
#     Asking for 100 returns 25 rows under a `pagingData.pageSize` of 100. A
#     stop condition that believed the echo would think it had the lot.
#   * Asking for a page past the end does not return an empty list. Requesting
#     page 100 of D.R. Horton's 24 pages returns the last page again, and TfL
#     returns its single row for every page number. A loop that stopped on an
#     empty page would never stop.
#
# `totalCount` is not a safe bound either: TfL reports 3 and serves 1. So the
# stop condition is the one Avature and RMK already use, no new contest
# numbers, with a hard cap behind it.
TALEO_PAGE = 25

# Six pages is 150 postings per search term, 450 across the three terms, which
# covers every board checked except D.R. Horton's 578. Walking that one whole
# would be 24 requests per scan for one employer whose roles are then almost
# all discarded by the title filter, so the same shape as Workday, Avature and
# RMK applies: KEYWORD is a real server-side filter (D.R. Horton 578 unfiltered,
# 118 for "manager"), so search per wanted title rather than crawl the board.
TALEO_MAX_PAGES = 6

_TL_PORTAL = re.compile(r"portalNo\s*:\s*'(\d+)'")
# Taleo appends this to the career section's name in the feed. Left on, every
# employer would be stored as "Acme - Custom Job List".
_TL_FEED_SUFFIX = re.compile(r"\s*-\s*Custom Job List\s*$", re.I)
_TL_FEED_TITLE = re.compile(r"<channel>\s*<title>(.*?)</title>", re.S | re.I)


def _taleo_body(term: str, page: int) -> dict:
    """The search request the career section's own JavaScript sends.

    `sortBySelectionParam` "1" is POSTING_DATE and `ascendingSortingOrder`
    "false" is newest first. That pairing matters precisely because the page
    cap above exists: if only the first 150 of a board are ever read, they
    should be the 150 newest rather than whatever Taleo's relevancy score
    puts first for an empty keyword.
    """
    return {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "1",
                             "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": term, "LOCATION": ""}, "valid": True},
        "filterSelectionParam": {"searchFilterSelections": []},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
        "pageNo": page,
    }


def _taleo_employer(host: str, portal: str, session: requests.Session,
                    timeout: int, user_agent: str) -> str:
    """The employer's own name, from the RSS channel title.

    This is one extra request per board per scan and it is worth it, because
    it is the ONLY place on the platform where Taleo says who the employer is.
    Both `<title>` tags on an unbranded career section read "Job Search": The
    College of New Jersey's board says "Job Search" twice and does not contain
    the words "College of New Jersey" anywhere in its markup. Filling the
    company field from the label we were handed instead would make every
    identity check circular, and taking the page title would give 255 boards
    the same name, which is exactly how 252 Jobvite employers merged into one
    row and Ookla, Enphase Energy and Barracuda Networks vanished.
    Checked live, the channel title is the employer and it is distinct on
    every board: "TTEC", "Baesystems", "D.R. Horton, Inc.", "TFL", "Hilton",
    "Texas Comptroller of Public Accounts", "THE COLLEGE OF NEW JERSEY".
    The feed's ITEMS are useless, which is the trap: it serves at most 11 of
    them whatever the board holds (11 of TTEC's 116, 11 of D.R. Horton's 578)
    and answers a board with nothing open with one placeholder item titled
    "Unable to Create an RSS Feed". Only the channel title is read here.
    """
    url = (f"https://{host}/careersection/feed/joblist.rss"
           f"?lang=en&portal={portal}&searchtype=3")
    lim = _limiter()
    if lim is not None:
        # The only request in this module that does not go through `fetch_one`,
        # so it is the only one that has to ask the limiter for itself.
        lim.wait(url)
    try:
        r = session.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    except requests.RequestException:
        return ""
    if r.status_code != 200:
        return ""
    m = _TL_FEED_TITLE.search(r.text or "")
    if not m:
        return ""
    return _TL_FEED_SUFFIX.sub("", m.group(1)).strip()


def fetch_taleo(
    src: Source,
    terms: list[str],
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = TALEO_MAX_PAGES,
) -> Result:
    """Oracle Taleo: resolve the portal number, then page the JSON endpoint.

    Returns the merged rows under `requisitionList` plus the employer's own
    name under `employerName`, which is the shape `parse_taleo` reads.
    """
    session = _thread_session()
    host = urlparse(src.url).netloc

    page_res = fetch_one(
        Source(company=src.company, url=src.url, platform="taleo",
               sector=src.sector, country=src.country),
        timeout=timeout, retries=retries, user_agent=user_agent, session=session)
    if not page_res.ok or not isinstance(page_res.payload, str):
        # Carry the failure on the ORIGINAL source: the state file and
        # `detect_throttling` key on `source.key`.
        return Result(src, error=page_res.error or "bad payload",
                      status=page_res.status, throttled=page_res.throttled)

    m = _TL_PORTAL.search(page_res.payload)
    if not m:
        # Two different real cases land here and neither is a transport
        # failure, so neither may be reported as one. A career section that
        # does not exist answers 200 with "Career Section Unavailable", and an
        # older, pre-faceted career section (Cook County, EFSA) has no portal
        # number at all because it renders its own rows server side. Both are
        # "we could not read this", which is what `validate` needs to hear so
        # that `--prune` leaves the board alone.
        return Result(src, error="no portal number on the career section page",
                      status=page_res.status)
    portal = m.group(1)

    employer = _taleo_employer(host, portal, session, timeout, user_agent)

    api = f"https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}"
    rows: list[dict] = []
    seen: set[str] = set()
    first_error: Result | None = None

    # An empty term means the unfiltered board, which is right for the small
    # tenants: Transport for London publish three roles and a keyword search
    # would hide two of them.
    for term in (terms or [""])[:3]:
        for page in range(1, max_pages + 1):
            probe = Source(company=src.company, url=api, platform="taleo",
                           sector=src.sector, country=src.country,
                           method="POST", body=_taleo_body(term, page))
            res = fetch_one(probe, timeout=timeout, retries=retries,
                            user_agent=user_agent, session=session,
                            extra_headers={"tz": "GMT+00:00"})
            if not res.ok or not isinstance(res.payload, dict):
                first_error = first_error or Result(
                    src, error=res.error or "bad payload", status=res.status,
                    throttled=res.throttled,
                    transport=res.transport)
                break
            got = res.payload.get("requisitionList") or []
            fresh = [r for r in got
                     if isinstance(r, dict)
                     and str(r.get("contestNo") or r.get("jobId") or "") not in seen]
            # Stop on nothing new, never on a short page and never on the
            # stated total. Past the end Taleo repeats the last page rather
            # than returning nothing, so this is the only condition that
            # actually fires.
            if not fresh:
                break
            for r in fresh:
                seen.add(str(r.get("contestNo") or r.get("jobId") or ""))
            rows.extend(fresh)

    if not rows and first_error:
        return first_error
    # A board with nothing open is a real answer and is not an error: Hilton's
    # `us_hotel_ext` returns totalCount 0 for an empty keyword and for
    # "manager" alike. It reaches `parse_taleo` as zero jobs, which is what
    # liveness is measured on everywhere in this tool.
    return Result(src, payload={"employerName": employer,
                                "requisitionList": rows})


# What one page looks like on the platforms that cap one. A source whose whole
# result is exactly one of these numbers is the signature of a paging bug: the
# board answered, the parser worked, and everything past row N was silently
# dropped. Tesco returning exactly 10 of "999+" looked healthy for as long as
# nobody counted.
# amazon.jobs refuses a larger page by answering `{"hits": 0, "jobs": null}`,
# a clean 200 that reads as an empty board rather than as "too many".
AMAZON_PAGE = 100
# `hits` never exceeds this and `offset` past it returns nothing, however many
# roles the board really holds.
AMAZON_HIT_CAP = 10000

PAGE_SIZES = {
    "avature": 10, "rmk": RMK_PAGE, "phenom": 50, "workday": 20,
    # PCSX answers ten however many are asked for, so a whole-board read
    # landing on exactly ten means the walk stopped after page one.
    "pcsx": 10,
    "amazon": AMAZON_PAGE,
    # Google state their page size in the payload and ignore anything asked
    # for, so a whole-board read landing on exactly twenty means the walk
    # stopped after page one.
    "google_careers": GOOGLE_PAGE,
    "nhs": 10, "reed": REED_PAGE, "adzuna": ADZUNA_PAGE,
    "taleo": TALEO_PAGE,
    # The Teamtailor builder asks for per_page=200; the feed's own default is
    # the first 100, so a board sitting on either number is worth a look.
    "teamtailor": 200,
}


def pinned_to_one_page(counts: dict[str, int], sources: Iterable[Source]) -> list[str]:
    """Sources whose entire result is exactly one page of their platform.

    Not proof of a fault: a board can genuinely have twenty roles on a
    platform that pages in twenty-fives. It is the only cheap signal there is,
    though, and the alternative is what happened here, where a source returned
    ten of three thousand for months and read as healthy the whole time.
    """
    out = []
    for src in sources:
        size = PAGE_SIZES.get(src.platform)
        if size and counts.get(src.key, 0) == size:
            out.append(src.company)
    return sorted(set(out))


def interleave_by_host(sources: list[Source]) -> list[Source]:
    """Spread each host's sources evenly across the queue.

    The bundled list is sorted into contiguous per-platform blocks: all 2,094
    Workable boards are one unbroken run, all 4,078 Greenhouse boards another.
    Submitted in that order, every worker in the pool is on the same host at
    the same time, and once each host is paced separately the run costs the SUM
    of the per-host times instead of the longest one. Observed on the unpaced
    code: a scan spent over an hour with all four workers pointed at
    apply.workable.com and nothing else progressing at all.

    Each source is keyed by its fractional position within its own host, so a
    host with 2,094 entries lands one every ~8 slots. Each host also gets its
    own starting offset, which is the part that is easy to leave out and wrong
    to: without it every host holding a single board keys to exactly 0.5, and
    7,748 of them do, so they would all pile into the middle of the queue and
    leave both ends as solid blocks of the busy hosts. The offset is drawn from
    a fixed seed over the sorted host names, so the order is the same on every
    run and a scan is reproducible.
    """
    by_host: dict[str, list[Source]] = defaultdict(list)
    for src in sources:
        by_host[urlparse(src.url).netloc].append(src)
    rng = random.Random(0)
    phase = {host: rng.random() for host in sorted(by_host)}
    keyed: list[tuple[float, str, int, Source]] = []
    for host, group in by_host.items():
        n = len(group)
        for i, src in enumerate(group):
            keyed.append(((i + phase[host]) / n, host, i, src))
    keyed.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in keyed]


def fetch_all(
    sources: Iterable[Source],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    per_host_rps: float = DEFAULT_PER_HOST_RPS,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    search_terms: list[str] | None = None,
    # Keyed by credential name, not by platform: Adzuna needs two. Passed down rather
    # than read from the environment inside the fetcher, so a caller can run
    # two configs in one process without them sharing a key.
    api_keys: dict[str, str] | None = None,
    on_result: Callable[[Result], None] | None = None,
    # Where to remember a host that has shut the door for hours. Optional
    # because a test, a benchmark or a one-off probe has no business writing
    # to anybody's state directory; the scan passes it, nothing else does.
    blocks_path: "str | Path | None" = None,
) -> list[Result]:
    out: list[Result] = []
    # Queued so that consecutive tasks land on different hosts. Without this,
    # per-host pacing and a contiguous 4,078-entry Greenhouse block combine
    # into a pool where every worker is asleep waiting for the same host.
    queue = interleave_by_host(list(sources))
    limiter = HostLimiter(per_host_rps)
    if blocks_path:
        limiter.remember_blocks(blocks_path)
    # The old opening stagger, `(i % concurrency) * 0.05`, is gone. It only
    # ever delayed the first `concurrency` tasks, so at four workers it was
    # 0.15 seconds once and nothing at all for the other 17,805 sources. The
    # burst it was meant to prevent is now prevented per host, for the whole
    # run, rather than for the first 200 milliseconds of it.
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(_fetch_dispatch, src, limiter, timeout, retries,
                          user_agent, search_terms or [],
                          api_keys or {}): src for src in queue}
        for f in as_completed(futs):
            # One board must never be able to end the run.
            #
            # `fetch_one` catches `requests.RequestException`, which is the
            # failure a board is expected to produce. It is not the only one a
            # board can cause: a response body of 60,000 open brackets makes
            # the JSON decoder recurse until Python gives up, and the
            # `RecursionError` comes back out of `f.result()` where nothing
            # was catching anything. Demonstrated against a loopback server;
            # every other board's results in that run were lost, because this
            # loop never reaches `return out`. On the Actions path that is a
            # red run and no roles after up to 300 minutes of fetching, and
            # any one of the 17,807 third parties here can trigger it with a
            # small response body.
            #
            # A raise here is therefore recorded as this source's failure, in
            # the same shape as every other failure, and the run continues.
            # `BaseException` is deliberately not caught: KeyboardInterrupt
            # and SystemExit are the user asking to stop.
            try:
                res = f.result()
            except Exception as e:
                res = Result(futs[f], error=f"{type(e).__name__}: {e}"[:300])
            out.append(res)
            if on_result:
                on_result(res)

    # Say which hosts slowed us down.
    #
    # `note_throttle` widens a host's gap on every 429, including the ones a
    # retry then recovers from, and nothing reported that it had fired. A
    # retried 429 comes back as an ordinary 200 with `throttled` False, and
    # `detect_throttling` only sees boards that returned nothing at all, so
    # "no throttling was reported" was not the same statement as "no
    # throttling happened". Its own docstring says the multiplier is returned
    # so a caller can say out loud that it has slowed down rather than doing
    # it silently, and no caller was reading it.
    #
    # This is the Workable fault one level up: a host refusing quietly while
    # the run looks healthy.
    if limiter is not None:
        slowed = sorted(
            {urlparse(r.source.url).netloc for r in out}
            - {h for h in {urlparse(r.source.url).netloc for r in out}
               if limiter.slowdown_for(f"https://{h}/") <= 1.0})
        for host in slowed:
            x = limiter.slowdown_for(f"https://{host}/")
            print(f"  ! {host} refused during this run, so it was read "
                  f"{x:.0f}x slower than usual by the end", flush=True)

        # Say which hosts ran out of budget, and what that cost.
        #
        # A source turned away for want of budget comes back throttled, so it
        # is never stored as an empty board, but `cmd_scan` only names a host
        # when the error carries a block length, and there is none here. Left
        # unsaid, the reader would see a few employers in the "look throttled"
        # list and no reason for it, and a search cut off part way would sit in
        # the "cut off at the page limit" list as though the page cap had bitten.
        for host, (cap, _) in sorted(limiter.budgets_spent().items()):
            mine = [r for r in out if urlparse(r.source.url).netloc == host]
            unread = sum(1 for r in mine if not r.ok)
            partial = sum(1 for r in mine if r.ok and r.truncated)
            print(f"  ! {host} limits requests over a long window, so a run may "
                  f"send it {cap} and this one used them all: {unread} "
                  f"source(s) there are UNKNOWN today, not empty, and "
                  f"{partial} came back incomplete.", flush=True)
    return out


def _fetch_dispatch(src, limiter, timeout, retries, ua, terms, keys=None) -> Result:
    # Runs on the pool worker, which is the only place that can put the run's
    # limiter where `fetch_one` will find it without every platform fetcher
    # having to carry it through its signature.
    pace_this_thread(limiter)
    if src.platform == "reed":
        return fetch_reed(src, (keys or {}).get("reed", ""), timeout=timeout,
                          retries=retries, user_agent=ua)
    if src.platform == "adzuna":
        return fetch_adzuna(src, (keys or {}).get("adzuna_app_id", ""),
                            (keys or {}).get("adzuna_app_key", ""),
                            timeout=timeout, retries=retries, user_agent=ua)
    if src.platform == "workday":
        return fetch_workday(src, terms, timeout=timeout, retries=retries,
                             user_agent=ua)
    if src.platform == "nhs":
        return fetch_nhs(src, timeout=timeout, retries=retries, user_agent=ua)
    if src.platform == "workable_search":
        return fetch_workable_search(src, timeout=timeout, retries=retries,
                                     user_agent=ua)
    if src.platform == "workable_recent":
        # Capped, and not silently. It used to be walked to exhaustion, 1,054
        # pages for a week, on the grounds that a cap would quietly drop the
        # tail of a sweep whose job is completeness. Two things changed that.
        # The walk is most of what a scan sends jobs.workable.com, and every
        # scan from 7 to 17 September ended with that host refusing for up to
        # a day, which left the sweep AND most keyword searches unknown, so
        # "complete" was never what it delivered. And a cap is no longer silent: the pager
        # marks the result cut off and the scan names it. See
        # `WORKABLE_RECENT_MAX_PAGES` for why the searches keep most of the budget.
        return fetch_workable_search(src, timeout=timeout, retries=retries,
                                     user_agent=ua,
                                     max_pages=WORKABLE_RECENT_MAX_PAGES)
    if src.platform == "phenom":
        return fetch_phenom(src, terms, timeout=timeout, retries=retries,
                            user_agent=ua)
    if src.platform == "amazon":
        return fetch_amazon(src, terms, timeout=timeout, retries=retries,
                            user_agent=ua)
    if src.platform == "pcsx":
        return fetch_pcsx(src, terms, timeout=timeout, retries=retries,
                          user_agent=ua)
    if src.platform == "google_careers":
        return fetch_google_careers(src, terms, timeout=timeout,
                                    retries=retries, user_agent=ua)
    if src.platform == "avature":
        return fetch_avature(src, terms, timeout=timeout, retries=retries,
                             user_agent=ua)
    if src.platform == "rmk":
        return fetch_rmk(src, terms, timeout=timeout, retries=retries,
                         user_agent=ua)
    if src.platform == "taleo":
        return fetch_taleo(src, terms, timeout=timeout, retries=retries,
                           user_agent=ua)
    return fetch_one(src, timeout=timeout, retries=retries, user_agent=ua)


def detect_throttling(
    results: list[Result],
    counts: dict[str, int],
    history: dict[str, int],
) -> list[str]:
    """Sources that previously returned jobs and now return none.

    Silent throttling is the failure mode that makes this whole tool lie: an
    empty array reads as "nothing matched" when it actually means "you were
    blocked". Anything here should be treated as unknown, not as zero.
    """
    suspects = []
    for res in results:
        key = res.source.key
        was = history.get(key, 0)
        now = counts.get(key, 0)
        if was >= 3 and now == 0:
            suspects.append(res.source.company)
        elif res.throttled:
            suspects.append(res.source.company)
    return sorted(set(suspects))
