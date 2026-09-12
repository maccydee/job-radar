"""How long a board has been empty, so one quiet Sunday cannot delete it.

`validate` asks each board for its postings and calls a board that answers
with none "dead". `validate --prune` then deletes it from a source list that
ships to everybody. The gap in that, found on 12 September 2026 by re-checking
a run from five days earlier:

    357 of 363 "dead" rows said `no postings returned`. Not a timeout, not a
    403, not a TLS failure. HTTP 200, board answered, nothing open that day.

    Re-checked five days later, 2 of 25 sampled were serving jobs again, plus
    Contentful with six and Parallelz with one from a targeted check. Around
    8%, so roughly 28 of 355 would have been deleted while perfectly alive.

They had not died and recovered. They were companies with nothing open on a
Sunday. A thirty-person employer between hires and an abandoned board return
byte-identical answers, and no single reading can tell them apart.

This repo already separates "could not read the board" from "read it and it
was empty", and is careful about it. What it did not separate is "empty today"
from "gone", which is the same mistake one layer further in.

So emptiness is recorded over time rather than judged in the moment. A board
becomes deletable only once it has answered empty on several separate
validations AND has been empty for weeks. Both, not either: a maintainer
running `validate` three times in an hour satisfies a run count while learning
nothing, and the day count is what actually carries the evidence.

Anything that comes back alive is forgotten immediately, so a board that
posts, goes quiet for a month and posts again starts from nothing.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

DEFAULT_NAME = "empty-since.json"

# Beside the source list it describes, and TRACKED, which `state/` is not.
#
# This has to survive between runs or it does nothing at all. The weekly job
# runs on a fresh GitHub runner with an empty checkout, so a log under
# `state/` would start empty every Sunday, no board would ever reach the
# thresholds, and the prune would be blocked for ever: a guard that never lets
# anything through is the same as a broken prune, just quieter.
#
# Committed alongside the prune it justifies, so the evidence is in the pull
# request. A reviewer can see "this board has answered empty since 4 August"
# in the diff rather than taking the job's word for it.
DEFAULT_DIR = "sources"

# How many separate validations must have seen it empty.
#
# Three, because the weekly job is the normal caller and three weeks of
# nothing is a real signal where one Sunday is not.
MIN_RUNS = 3

# And how long the first of those was ago.
#
# The run count alone is gameable by frequency: three runs in an hour is three
# runs. This is the threshold that holds the actual evidence, and 21 days
# comfortably clears the recovery window measured above.
MIN_DAYS = 21


def _today() -> str:
    return date.today().isoformat()


def _days_between(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return 0


class EmptyLog:
    """Per source URL: when it was first seen empty, and how often since."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else Path(DEFAULT_DIR) / DEFAULT_NAME
        self._rows: dict[str, dict] = {}

    # -- reading ----------------------------------------------------------

    def load(self) -> "EmptyLog":
        """Never raises. A log that cannot be read is a log with nothing in
        it, and that errs towards keeping boards rather than deleting them."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self
        if isinstance(raw, dict) and isinstance(raw.get("sources"), dict):
            self._rows = {k: v for k, v in raw["sources"].items()
                          if isinstance(v, dict)}
        return self

    def __len__(self) -> int:
        return len(self._rows)

    def runs(self, url: str) -> int:
        return int(self._rows.get(url, {}).get("runs", 0))

    def first_empty(self, url: str) -> str:
        return str(self._rows.get(url, {}).get("first_empty", ""))

    def days_empty(self, url: str) -> int:
        first = self.first_empty(url)
        return _days_between(first, _today()) if first else 0

    def settled(self, url: str) -> bool:
        """Has this been empty long enough, and often enough, to delete?"""
        return (self.runs(url) >= MIN_RUNS
                and self.days_empty(url) >= MIN_DAYS)

    def why_kept(self, url: str) -> str:
        """One line for a report, when something empty is being kept."""
        r, d = self.runs(url), self.days_empty(url)
        if r < MIN_RUNS:
            return f"empty on {r} of the {MIN_RUNS} checks needed"
        return f"empty for {d} days, {MIN_DAYS} needed"

    # -- writing ----------------------------------------------------------

    def record(self, url: str, empty: bool, when: str | None = None) -> None:
        """One board's answer from one validation.

        A board with postings is forgotten entirely rather than reset to zero,
        so a board that posts, goes quiet for a month and posts again starts
        from nothing the next time it empties.
        """
        if not empty:
            self._rows.pop(url, None)
            return
        day = when or _today()
        row = self._rows.get(url)
        if not row:
            self._rows[url] = {"first_empty": day, "last_empty": day, "runs": 1}
            return
        # Two checks on the same day are one day's evidence. Without this a
        # maintainer re-running `validate` to debug something would march a
        # board towards deletion on an afternoon's worth of readings.
        if row.get("last_empty") == day:
            return
        row["last_empty"] = day
        row["runs"] = int(row.get("runs", 0)) + 1
        row.setdefault("first_empty", day)

    def forget_missing(self, urls) -> int:
        """Drop anything no longer in the source list, so the file does not
        grow for ever with rows nobody will ask about again."""
        keep = set(urls)
        gone = [u for u in self._rows if u not in keep]
        for u in gone:
            del self._rows[u]
        return len(gone)

    def save(self) -> None:
        """Write-then-rename, like everything else here."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "version": 1,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "min_runs": MIN_RUNS,
            "min_days": MIN_DAYS,
            "sources": self._rows,
        }
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.path)
