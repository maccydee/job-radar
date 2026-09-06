"""How old everything is, and whether that is too old.

Written after an afternoon in which three separate scheduled jobs turned out
to have been failing for weeks, all in the same shape and none of them
noticed:

  * The weekly source validation died on the first templated source, every
    Sunday since 30 August, so 17,923 boards went unchecked. It filed an issue
    each week blaming a throttled runner.
  * The weekly seed rebuild spent an hour harvesting 287,219 roles and then
    dropped every one of them at the upload, because launchd's PATH has no
    `gh`. The published shard set sat at 28 August while the job "ran".
  * Two full scans died part way, so `meta.last_run` still said 31 August
    while the board showed roles seen today.

The common failure is not the crash. It is that a stale artefact looks exactly
like a fresh one. Last week's seed is a perfectly good seed. An unvalidated
source list is a source list. A board built from a scan that died at 88% has
thousands of roles on it and no gap a reader could see. Nothing in the tool
ever said "this is old", so nothing was old until somebody happened to look.

So this module answers one question per moving part, out loud, with a number:
when was this last done, and is that longer ago than it should be. It reads
only things already recorded, and it never fetches, so it is cheap enough to
run on every dashboard render and in a terminal whenever.

A missing answer is its own verdict and is never read as fresh. "No scan has
ever finished" and "a scan finished this morning" must not render the same,
which is the mistake this file exists to stop repeating.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

# How many days each thing may go before it is worth saying something.
#
# These are the intervals the jobs themselves run at, plus enough slack for
# one missed cycle. A weekly job that has not run in eight days has missed
# one; at fifteen it has missed two and is not coming back on its own.
STALE_DAYS = {
    # A scan is the whole point of the tool and roles close inside a week.
    "scan": 2,
    # Rebuilt weekly from a real machine, and published for everyone.
    "seed": 8,
    # The bundled list is revalidated weekly upstream.
    "sources": 8,
}

# Past this it is not late, it is broken, and the wording changes to say so.
BROKEN_MULTIPLE = 2


def _days_since(stamp) -> int | None:
    """Whole days between a date or timestamp and today, or None."""
    if not stamp:
        return None
    text = str(stamp).strip()
    if not text:
        return None
    for parse in (datetime.fromisoformat, lambda s: datetime.combine(
            date.fromisoformat(s), datetime.min.time())):
        try:
            when = parse(text)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is not None:
            when = when.replace(tzinfo=None)
        return max(0, (datetime.now() - when).days)
    return None


class Item:
    """One moving part, its age, and what to do about it."""

    def __init__(self, key: str, label: str, days: int | None,
                 detail: str = "", fix: str = ""):
        self.key = key
        self.label = label
        self.days = days
        self.detail = detail
        self.fix = fix
        self.limit = STALE_DAYS.get(key, 8)

    @property
    def state(self) -> str:
        """ok | stale | broken | unknown.

        `unknown` is deliberately not `ok`. Never having done a thing and
        having done it this morning are opposite facts, and the whole reason
        this module exists is that they were rendering the same.
        """
        if self.days is None:
            return "unknown"
        if self.days > self.limit * BROKEN_MULTIPLE:
            return "broken"
        if self.days > self.limit:
            return "stale"
        return "ok"

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def says(self) -> str:
        if self.days is None:
            return f"{self.label}: never, as far as this can tell"
        if self.days == 0:
            return f"{self.label}: today"
        if self.days == 1:
            return f"{self.label}: yesterday"
        return f"{self.label}: {self.days} days ago"


def _scan_item(con) -> Item:
    """When a scan last FINISHED, which is not when a role was last seen.

    `meta.last_run` is written at the end of a scan, so a scan that dies part
    way leaves it alone. That is the right behaviour and it is also the number
    that matters: a killed scan stores real roles and refreshes real dates, so
    `max(last_seen)` can say "today" while nothing has read the whole list in
    a week. Both are reported, because the gap between them is the symptom.
    """
    from . import store
    last = store.get_meta(con, "last_run", "") or ""
    days = _days_since(last)
    seen = ""
    try:
        row = con.execute("SELECT MAX(last_seen) m FROM roles").fetchone()
        seen = (row["m"] if row and row["m"] else "") or ""
    except Exception:
        seen = ""
    seen_days = _days_since(seen)
    detail = ""
    if seen_days is not None and days is not None and days - seen_days >= 2:
        detail = (f"roles were refreshed {seen_days} day(s) ago, so scans have "
                  f"been running and dying before the end")
    return Item("scan", "Last completed scan", days, detail,
                "job-radar scan --resume")


def _sources_item() -> Item:
    from . import sources as src_mod
    return Item("sources", "Source list checked", src_mod.age_days(), "",
                "git pull --ff-only")


def _seed_item(path=None) -> Item:
    """Age of the local shard set, from the build it last completed.

    Read off the newest shard rather than a recorded date, because the date
    the builder writes is the date it MEANT to publish and the shards are what
    it actually produced. A build that fell over at the upload leaves both,
    and only one of them is a fact.
    """
    out = Path(path) if path else Path("seed-build")
    if not out.is_dir():
        return Item("seed", "Seed shards built", None, "no local shard set",
                    "python3 tools/refresh_seed.py")
    shards = list(out.glob("*.jsonl.gz"))
    if not shards:
        return Item("seed", "Seed shards built", None, "no shards in " + str(out),
                    "python3 tools/refresh_seed.py")
    newest = max(p.stat().st_mtime for p in shards)
    days = max(0, (datetime.now() - datetime.fromtimestamp(newest)).days)
    detail = ""
    idx = out / "index.json"
    if idx.exists():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            roles = sum(v.get("roles", 0)
                        for v in (data.get("shards") or {}).values())
            if roles:
                detail = f"{roles:,} roles in {len(shards)} shards"
        except (OSError, ValueError):
            detail = "the index beside the shards could not be read"
    return Item("seed", "Seed shards built", days, detail,
                "python3 tools/refresh_seed.py")


def report(con=None, *, seed_path=None) -> list[Item]:
    """Every moving part, oldest problem first."""
    items = [_sources_item(), _seed_item(seed_path)]
    if con is not None:
        items.insert(0, _scan_item(con))
    order = {"broken": 0, "unknown": 1, "stale": 2, "ok": 3}
    return sorted(items, key=lambda i: (order[i.state], -(i.days or 0)))


def worst(items) -> str:
    """The single worst state across everything, for a one-line summary."""
    for state in ("broken", "unknown", "stale"):
        if any(i.state == state for i in items):
            return state
    return "ok"
