"""Getting the current source list, without anyone having to remember.

The list is data and it rots: boards migrate between applicant tracking
systems, tokens get renamed, companies are acquired. A weekly job upstream
revalidates the whole thing and commits the result, and until this module
existed none of that reached anybody's copy. `scan` printed a note saying the
list was old and that `git pull` would fix it, which is the tool telling its
user to go and do the tool's job. On 22 September 2026 the maintainer's own
list was nine days behind and the scan was quietly missing roles, which is
exactly the failure that note was supposed to prevent and did not.

So `scan` fetches it. Three decisions are worth the space, because each one
was the alternative that lost.

**It is an HTTPS fetch of the published list, not `git pull --ff-only`.**
A pull updates the CODE as well as the data, and nobody typed `upgrade`: it
would swap adapters and pacing out from under a run that is about to spend an
hour on other people's servers. It also cannot work at all for the people who
most need it, which is anyone who installed with pip, unpacked a tarball, sits
on a branch, has local commits, or is on a detached HEAD. They can never make
their list current and they are the ones silently losing roles. And a pull
needs git AND a remote AND a matching upstream branch AND a clean tree, which
is four ways to fail against one. The dashboard keeps its Pull button, because
there a person has clicked a thing labelled "pull" and is asking for exactly
that.

**What is downloaded is written beside your state, never over
`sources/sources.json`.** That file is tracked in a git checkout, so writing
it would leave every user with a permanently dirty tree and a `git pull` that
refuses to merge. Writing to `state/` means the tracked file is never touched,
so a local modification to it cannot be overwritten: there is no code path
here that opens it for writing. `sources.active_file` then loads whichever of
the two carries the newer `meta.checked`, so a later `git pull` that brings a
newer list wins on its own, and a local `validate --prune`, which stamps
`checked` with today, keeps winning until upstream publishes something newer.

**A download that looks wrong is refused, not installed.** Same shape as the
gate in `tools/refresh_seed.py`: a short list is not visibly broken, it is a
list with fewer employers on it, and the employers that fell off look exactly
like employers that do not exist. This is the repository's signature failure
and a refresh is a perfect place to reintroduce it.

Nothing here is silent. Every outcome prints, including the ones where nothing
happened, and no outcome claims the list is current unless something actually
said so.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

from . import sources as src_mod
# The existing eight-day threshold the note, the daily nudge, the dashboard
# and `freshness.STALE_DAYS` already share. Reused rather than restated: a
# second number here would be a second definition of "old" that nobody would
# keep in step with the first.
from .freshness import STALE_DAYS
from .state import DEFAULT_DIR, atomic_write_text

STALE_AFTER_DAYS = STALE_DAYS["sources"]

# The published list, on the branch the weekly validation commits to.
#
# Deliberately the raw file rather than a release asset. The validation
# workflow commits `sources/sources.json` straight to main, so main is where
# the current list actually is; a release would be a second publishing step
# that nothing maintains and that would go stale without saying so.
URL = ("https://raw.githubusercontent.com/maccydee/job-radar/main/"
       "sources/sources.json")

# Plain HTTPS, no authentication, no identifier beyond a user agent. Asking
# for the source list says nothing about who is asking or what they are
# looking for, and it should stay that way.
USER_AGENT = ("job-radar source list update "
              "(+https://github.com/maccydee/job-radar)")

# Where the record of what was tried lives, beside the seen-set rather than
# beside the list it updates. `state/` is gitignored and the scan workflow
# force-adds only `state/seen.json`, so neither of these can reach a commit.
RECORD_NAME = "source-update.json"

# How much smaller than the list you already have a download may be before it
# is treated as a bad read rather than a pruned list.
#
# The same fraction as `tools/refresh_seed.py`, for the same reason and
# against the same failure. The weekly prune deletes tens of boards, not
# thousands: `validate --prune` refuses outright above a quarter of the list,
# so anything under 80% did not come from upstream pruning and is a truncated
# or wrong file.
MIN_FRACTION = 0.80

# Nothing promises this file a size, and 3.2MB is what it is today. The cap is
# ten times that: a body still arriving past it is not the file that was asked
# for, and there is no reason to keep reading it.
_MAX_BODY = 32 << 20

# One request, no retries. A refusal is an answer and this asks again
# tomorrow; hammering somebody's CDN because the first answer was not the one
# we wanted is exactly what the politeness rules in CLAUDE.md forbid.
_TIMEOUT = 30


class Result:
    """What the update did, in a form both a human and a caller can read.

    `state` is the fact. `message` is the sentence, and it is empty only when
    there is genuinely nothing to say, which is when the list was already
    fresh enough to leave alone.

    `current` is the question every caller actually has: does anything here
    justify saying the list is up to date? It is true only when the network
    said so, either by sending a newer list or by answering 304 to the copy we
    hold. Every failure leaves it false, because "we could not find out" must
    never render as "it is fine". That is the mistake `jobradar/freshness.py`
    was written to stop and it would be trivial to reintroduce here.
    """

    def __init__(self, state: str, message: str = "", *,
                 current: bool = False, days: int | None = None):
        self.state = state
        self.message = message
        self.current = current
        self.days = days

    def __repr__(self) -> str:      # for a failing assert to be readable
        return f"Result({self.state!r}, current={self.current})"


def record_path(state_dir=None) -> Path:
    return Path(state_dir or DEFAULT_DIR) / RECORD_NAME


def _read_record(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        # A record that cannot be read costs one extra request and nothing
        # else, so it is not worth refusing a run over.
        return {}


def _locally_modified(repo_root: Path) -> bool | None:
    """Has the tracked source list been edited here? None means cannot tell.

    Git is used read-only and as a detector, never as the mechanism. Nothing
    below writes `sources/sources.json`, so a hand-edited list is safe on disk
    either way; what this stops is the subtler version, where the edit
    survives and is quietly ignored because a downloaded copy loads instead.

    Three answers, not two. `None` is "there is no git here, or git could not
    answer", which is the ordinary case for a pip install and is not a fault.
    The caller proceeds on `None` exactly as it does on `False`, and the
    distinction is kept anyway so that the next reader has to decide what to
    do with it rather than inheriting "we could not tell" already collapsed
    into "no". That collapse is this repository's most repeated mistake and it
    has been made three times in one afternoon before now.
    """
    if not (repo_root / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--",
             "sources/sources.json"],
            capture_output=True, text=True, encoding="utf-8", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    return bool(r.stdout.strip())


def _http_get(url: str, etag: str = "", timeout: int = _TIMEOUT,
              cap: int = _MAX_BODY):
    """`(status, body, etag)`. The one seam the tests substitute.

    `body` is None for any status that carries no list, 304 included. A 304 is
    an answer and not a failure: it means the copy we already hold is byte for
    byte the published one, which is the single most useful thing this can
    learn and the reason the ETag is kept at all. raw.githubusercontent.com
    sends no Last-Modified, so the ETag is the only conditional available.

    Read in chunks against a ceiling rather than `r.read()`, for the reason
    `seed._http_get` gives: the only size anybody has promised is the one we
    decided to expect, and a body running past it is not the file we asked
    for.
    """
    import urllib.error
    import urllib.request

    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = bytearray()
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                body += chunk
                if len(body) > cap:
                    raise ValueError(
                        f"the source list is still arriving after "
                        f"{len(body):,} bytes, past the {cap:,} this was told "
                        f"to expect. Refusing to read an unbounded file.")
            return r.status, bytes(body), (r.headers.get("ETag") or "")
    except urllib.error.HTTPError as exc:
        # Every HTTP status the server chose to send, handed back as a fact
        # for the caller to report. Not raised, because a 304 arrives this way
        # too and it is the good case.
        tag = exc.headers.get("ETag") if exc.headers else ""
        return exc.code, None, (tag or "")


def _count(body: dict) -> int:
    """Entries with a URL, which is what `sources.load_file` will keep."""
    items = body.get("sources") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return 0
    n = 0
    for d in items:
        if isinstance(d, dict) and "json" in d and isinstance(d["json"], dict):
            d = d["json"]
        if isinstance(d, dict) and d.get("url"):
            n += 1
    return n


def _checked(body: dict) -> str:
    meta = body.get("meta") if isinstance(body, dict) else None
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("checked") or meta.get("validated") or "")[:10]


def _refuse(new: dict, local_path: Path) -> str:
    """Why this download must not replace what is here, or "".

    The gate from `tools/refresh_seed.py`, pointed the other way. There it
    stops a short build being published; here it stops a short download being
    installed. Both exist because a list with fewer rows in it is not visibly
    broken.
    """
    fresh = _count(new)
    if fresh == 0:
        return ("it carries no usable sources at all, so it is not the "
                "published list")
    try:
        local = json.loads(local_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local = {}
    have = _count(local)
    if have and fresh < have * MIN_FRACTION:
        return (f"it holds {fresh:,} sources against the {have:,} you already "
                f"have, which is {100 * fresh / have:.0f}% of them. Anything "
                f"under {100 * MIN_FRACTION:.0f}% is a bad download, not a "
                f"pruned list")
    stamp = _checked(new)
    if not stamp:
        return ("it carries no checked date, so there is no way to tell "
                "whether it is newer than yours")
    try:
        date.fromisoformat(stamp)
    except ValueError:
        return f"its checked date reads {stamp!r}, which is not a date"
    mine = _checked(local)
    if mine and stamp < mine:
        return (f"it was checked {stamp} and yours was checked {mine}, so it "
                f"is the older of the two")
    return ""


def _stamp(day: date | None = None) -> str:
    return (day or date.today()).isoformat()


def update(cfg, *, off_because: str = "", state_dir=None, http=None,
           repo_root=None, today: date | None = None) -> Result:
    """Make the bundled source list current, and say what happened.

    Returns a `Result` and prints nothing. The caller decides where the
    sentence goes, which is what lets `scan` put it above the run and the
    tests read it without capturing stdout.
    """
    http = http or _http_get
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    now = today or date.today()

    if not getattr(cfg, "use_bundled_sources", True):
        return Result("not-bundled")

    days = src_mod.age_days(state_dir=state_dir)
    # A list with no date is not a fresh list. `freshness.Item.state` makes
    # the same call for the same reason: never having checked and having
    # checked this morning are opposite facts and must not render alike.
    if days is not None and days < STALE_AFTER_DAYS:
        return Result("fresh", days=days)

    age = f"{days} days ago" if days is not None else "at no date it records"
    if off_because:
        # Named, not just refused. "Updates are off" with no reason sends the
        # reader to the config to look for a setting that may not be the one
        # that is off, and one of the three reasons is a flag they typed on
        # this very command line.
        return Result(
            "off",
            f"  Source list update is off ({off_because}), and this list was "
            f"last checked {age}. Boards that have moved since are being read "
            f"at their old addresses, and employers added since are not being "
            f"read at all.",
            days=days)

    rec_path = record_path(state_dir)
    rec = _read_record(rec_path)

    # At most one request a day, on top of the staleness gate above.
    #
    # Not a second cadence for staleness: the staleness gate is still what
    # decides whether to ask at all. This is a cap on how often a run may put
    # a request on somebody else's CDN, and it exists because `scan --limit`
    # is a thing people run repeatedly while tuning a config, and an offline
    # laptop would otherwise make one failed request per run all day.
    if rec.get("attempted") == _stamp(now):
        if rec.get("state") == "confirmed":
            return Result(
                "confirmed",
                f"  Source list was checked {age} and confirmed today as the "
                f"current published one.",
                current=True, days=days)
        return Result(
            "tried-today",
            f"  Source list update was already tried today and did not "
            f"succeed ({rec.get('why') or 'no reason recorded'}). This scan "
            f"is using the list it has, last checked {age}.",
            days=days)

    modified = _locally_modified(root)
    if modified:
        return Result(
            "local-changes",
            f"  Source list has local changes in this checkout, so it was "
            f"left alone. This scan is using your copy, last checked {age}. "
            f"Commit or revert `sources/sources.json` to let updates in.",
            days=days)

    def _remember(state: str, why: str = "", etag: str = "") -> None:
        """Write the record, and never let that failure become the story.

        A read-only or missing state directory is a reason the request cannot
        be remembered, not a reason the update failed, and raising here would
        turn a successful refresh into a traceback.
        """
        try:
            atomic_write_text(rec_path, json.dumps({
                "attempted": _stamp(now),
                "state": state,
                "why": why,
                "etag": etag or rec.get("etag", ""),
            }, indent=1))
        except OSError:
            pass

    try:
        status, body, etag = http(URL, rec.get("etag", ""))
    except Exception as exc:                    # noqa: BLE001
        # Deliberately everything. urllib raises URLError, socket.timeout,
        # ssl.SSLError, OSError and ValueError from the size cap, and a scan
        # must not die because a laptop is on a train. What it must not do is
        # carry on quietly, which is why this both records and reports.
        _remember("failed", str(exc)[:200])
        return Result(
            "failed",
            f"  Source list update failed: {exc}. This scan is using the "
            f"list it has, last checked {age}, so it may be missing boards. "
            f"It will try again on the next scan.",
            days=days)

    if status == 304:
        _remember("confirmed", etag=etag or rec.get("etag", ""))
        return Result(
            "confirmed",
            f"  Source list was last checked {age} and is the current "
            f"published one. Upstream has not changed it since.",
            current=True, days=days)

    if status != 200 or body is None:
        # A refusal is an answer. Report the code, record it, and ask again
        # tomorrow rather than retrying now.
        _remember("refused", f"HTTP {status}")
        return Result(
            "refused",
            f"  Source list update was refused: HTTP {status}. Not retrying. "
            f"This scan is using the list it has, last checked {age}.",
            days=days)

    try:
        new = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _remember("refused", f"unreadable: {exc}"[:200])
        return Result(
            "refused",
            f"  Source list update refused: what arrived is not readable "
            f"JSON ({exc}). This scan is using the list it has, last checked "
            f"{age}.",
            days=days)

    why = _refuse(new, src_mod.active_file(state_dir=state_dir))
    if why:
        _remember("refused", why[:200])
        return Result(
            "refused",
            f"  Source list update refused: {why}. Keeping the list you have, "
            f"last checked {age}.",
            days=days)

    before = src_mod.age_days(state_dir=state_dir)
    dest = src_mod.updated_file(state_dir)
    try:
        # Write-then-rename, like every other write here of something that is
        # not cheaply regenerable. A body truncated into place would parse as
        # a shorter list, which is the failure the gate above exists for
        # arriving through the back door.
        atomic_write_text(dest, body.decode("utf-8"))
    except OSError as exc:
        _remember("failed", f"could not be saved: {exc}"[:200])
        return Result(
            "failed",
            f"  Source list downloaded but could not be saved to {dest} "
            f"({exc}). This scan is using the list it has, last checked "
            f"{age}.",
            days=days)
    _remember("updated", etag=etag)

    after = src_mod.age_days(state_dir=state_dir)
    was = f"{before} days old" if before is not None else "undated"
    return Result(
        "updated",
        f"  Source list updated: {_count(new):,} sources checked "
        f"{_checked(new)}, replacing one that was {was}."
        + ("" if after == 0 else f" It is now {after} day(s) old."),
        current=True, days=after)
