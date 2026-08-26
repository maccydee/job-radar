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
import threading
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
PER_HOST_RPS = {"apply.workable.com": 0.7}

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

    def __init__(self, rps: float = DEFAULT_PER_HOST_RPS,
                 overrides: dict[str, float] | None = None) -> None:
        self.rps = rps
        self.overrides = dict(PER_HOST_RPS if overrides is None else overrides)
        self._next_ok: dict[str, float] = defaultdict(float)
        self._blocked_until: dict[str, float] = {}
        # Consecutive sources on a host that spent all their retries and still
        # got 429. See `note_refusal`.
        self._refusals: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def gap_for(self, host: str) -> float:
        """Seconds between requests to `host`. The stricter of the two rules.

        `min` rather than the override alone: turning the global rate down for
        a slow connection must not silently turn Workable's rate back UP to
        0.7 when the caller asked for 0.2.
        """
        if self.rps <= 0:
            return 0.0
        rps = min(self.rps, self.overrides.get(host, self.rps))
        return 1.0 / rps if rps > 0 else 0.0

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

    def note_ok(self, url: str) -> None:
        """This host answered. Forget any run of refusals against it.

        A host that is serving is not rate-limiting, so the breaker's count
        has to be a run of CONSECUTIVE refusals rather than a total. Without
        this, a long scan would eventually accumulate enough scattered 429s
        from an otherwise healthy host to arm the breaker against it.
        """
        host = urlparse(url).netloc
        with self._lock:
            if self._refusals.get(host):
                self._refusals[host] = 0

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

def fetch_workday(
    src: Source,
    terms: list[str],
    *,
    timeout: int = 20,
    retries: int = 2,
    user_agent: str = "job-radar/0.1",
    max_pages: int = 3,
) -> Result:
    """Workday needs its own path, for two reasons.

    Its page size is hard-capped at 20 (asking for 100 returns a 400), and its
    boards are enormous: Barclays reports 1,055 open roles. Paging through that
    for every enterprise tenant would be dozens of requests each, per scan, for
    results almost all of which get discarded by the title filter anyway.

    Workday is also the only platform here with server-side search, so the
    filtering happens at their end instead: one query per wanted title,
    shallowly paged. That turns a thousand postings into the handful that
    matter, in two or three requests rather than fifty.
    """
    session = _thread_session()
    merged: dict[str, dict] = {}
    total = 0
    first_error: Result | None = None

    for term in (terms or [""])[:3]:
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
            total = max(total, int(res.payload.get("total") or 0))
            for p in posts:
                key = p.get("externalPath") or p.get("title")
                if key:
                    merged.setdefault(key, p)
            if len(posts) < 20:
                break

    if not merged and first_error is not None:
        return first_error
    return Result(src, payload={"jobPostings": list(merged.values()), "total": total})


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

    Deliberately a different host from apply.workable.com, so the per-host
    pacing that exists for the 2,094 boards does not also throttle this, and
    a block on one does not silently take out the other.
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
    return Result(src, payload={"jobs": list(merged.values()),
                                "totalSize": total, "truncated": truncated})


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
    sep = "&" if "?" in src.url else "?"

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
        if "search-result" not in res.payload:
            break
        parts.append(res.payload)
        # A short page means the results ran out.
        if res.payload.count('data-test="search-result"') < 10:
            break

    if not parts:
        return first_error or Result(src, error="no pages returned")
    return Result(src, payload="".join(parts))


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

    if not merged and first_error is not None:
        return first_error
    return Result(src, payload={"results": list(merged.values()), "count": total})


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

    if not merged:
        # Fall back to the ten embedded in the page rather than returning none.
        return fetch_one(src, timeout=timeout, retries=retries, user_agent=user_agent)
    return Result(src, payload={"refineSearch": {"data": {"jobs": list(merged.values())},
                                                 "totalHits": total}})


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

    # An empty term means the unfiltered board, which is right for the small
    # tenants: Metro Bank publish six roles and a keyword search would only
    # hide four of them.
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
            fresh = {u for u in _AV_JOB.findall(res.payload)} - seen
            if not fresh:
                break
            seen |= fresh
            pages.append(res.payload)
            nxt = _AV_NEXT.search(res.payload)
            if not nxt:
                break
            url = nxt.group(1).replace("&amp;", "&")

    if not pages:
        return first_error or Result(src, error="no pages returned")
    # `parse_avature` already drops a repeated /JobDetail/ link, so joining the
    # pages is safe and keeps the parser a pure function of one HTML string.
    return Result(src, payload="".join(pages))


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

    for term in (terms or [""])[:3]:
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

    if not pages:
        return first_error or Result(src, error="no pages returned")
    return Result(src, payload="".join(pages))


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
PAGE_SIZES = {
    "avature": 10, "rmk": RMK_PAGE, "phenom": 50, "workday": 20,
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
) -> list[Result]:
    out: list[Result] = []
    # Queued so that consecutive tasks land on different hosts. Without this,
    # per-host pacing and a contiguous 4,078-entry Greenhouse block combine
    # into a pool where every worker is asleep waiting for the same host.
    queue = interleave_by_host(list(sources))
    limiter = HostLimiter(per_host_rps)
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
            res = f.result()
            out.append(res)
            if on_result:
                on_result(res)
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
    if src.platform == "phenom":
        return fetch_phenom(src, terms, timeout=timeout, retries=retries,
                            user_agent=ua)
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
