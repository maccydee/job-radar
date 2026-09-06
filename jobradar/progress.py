"""Where a scan got to, so a killed one does not start again from nothing.

A full scan reads 17,923 sources in about 77 minutes, and the floor is one
host's own clock rather than this machine, so it cannot be made much shorter.
On 6 September 2026 one was killed at 84 minutes when the process that owned
it exited. The roles it had already stored survived, because passes flush as
they go. What did not survive was the knowledge of WHICH sources it had read,
so the restart began again at source one and re-asked 17,923 servers for
things it already had.

That is the waste this file removes, and it is also the politeness problem:
those are other people's servers and re-reading all of them is the rudest
possible way to recover.

THE TRAP, and the whole reason this is a separate module with its own tests.

A source may be recorded as done only once its postings are COMMITTED, never
when it has merely been fetched. The scan holds parsed jobs in memory and
writes them in batches, so there is a window in which a source has been read,
its roles exist only in `all_jobs`, and nothing is on disk. Marking it done in
that window means a resume skips it, and the roles it held are never stored
and never fetched again. Nothing anywhere would say so: the resumed scan
reports success, the board simply does not have those roles, and a role that
was never stored looks exactly like a role that was never posted.

So the only way to mark anything here is `commit`, which takes the keys that
were just written and is called after the write returned, never before.

A resume also has to be sure it is resuming the same scan. A checkpoint is
tied to a fingerprint of the source list and the titles, because a config
edited between the kill and the resume changes what should be read, and
skipping a source the NEW config wants is the same silent loss by a different
route. A fingerprint mismatch is not an error, it just means there is nothing
to resume from.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# How old a checkpoint may be and still be worth resuming.
#
# A day. Postings appear and close inside a week, so resuming onto a
# half-finished read from last Tuesday would report a board built from two
# different weeks as one scan. Past this the checkpoint is ignored and the
# scan starts clean, which is slower and correct.
MAX_AGE_HOURS = 24

DEFAULT_NAME = "scan-progress.json"


def _key_hash(key: str) -> str:
    """A source key shortened for storage.

    The keys are URLs and there are ~18,000 of them, which is about 2MB of
    JSON written every few hundred sources. Twelve hex characters of SHA-1 is
    a collision risk of roughly one in ten million at this size, and a
    collision costs one skipped source rather than anything worse.
    """
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def fingerprint(source_keys, titles) -> str:
    """What this scan is reading, in a form that changes when that changes.

    Sorted, so the same set of sources fingerprints the same however it was
    ordered. Titles are in because they become search terms on the keyword
    platforms, so editing them changes what a source returns even when the
    source list is untouched.
    """
    h = hashlib.sha1()
    for k in sorted(source_keys):
        h.update(k.encode("utf-8"))
        h.update(b"\0")
    h.update(b"||titles||")
    for t in list(titles or []):
        h.update(str(t).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


class ScanProgress:
    """The set of sources whose postings are already safely stored."""

    def __init__(self, path: Path | str | None = None, *,
                 fingerprint_: str = "", run: int = 0):
        self.path = Path(path) if path else Path("state") / DEFAULT_NAME
        self.fingerprint = fingerprint_
        self.run = run
        self.started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._done: set[str] = set()
        # Sources read and parsed but NOT yet written. Deliberately not part
        # of `_done` and deliberately never saved: this is the window the
        # module docstring is about.
        self._pending: list[str] = []

    # -- reading ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._done)

    def is_done(self, key: str) -> bool:
        return _key_hash(key) in self._done

    def filter(self, sources) -> tuple[list, int]:
        """(the sources still to read, how many were skipped)."""
        out = [s for s in sources if not self.is_done(s.key)]
        return out, len(sources) - len(out)

    # -- writing ----------------------------------------------------------

    def hold(self, key: str) -> None:
        """Note that a source has been read. NOT that it is safe to skip."""
        self._pending.append(_key_hash(key))

    # There is deliberately no `drop_pending`. When a write fails the scan
    # keeps the parsed jobs in memory and the next checkpoint re-screens and
    # re-writes all of them, so the held keys are still owed and are committed
    # by that later write. Forgetting them would only make a resume re-ask
    # servers whose postings had in fact been stored.

    def commit(self, save: bool = True) -> int:
        """Promote everything read since the last commit to done.

        Call this only after the postings are in the database and the write
        returned. Returns how many were promoted.
        """
        n = len(self._pending)
        self._done.update(self._pending)
        self._pending.clear()
        if save and n:
            self.save()
        return n

    def save(self) -> None:
        """Write-then-rename, the same as everything else here.

        A checkpoint half-written by a process being killed is worse than no
        checkpoint, because it parses and is believed.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "version": 1,
            "run": self.run,
            "started": self.started,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fingerprint": self.fingerprint,
            "done": sorted(self._done),
        }
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        """Finished cleanly, so there is nothing to resume."""
        self._done.clear()
        self._pending.clear()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def load(path, fingerprint_: str, *, run: int = 0,
         max_age_hours: int = MAX_AGE_HOURS) -> tuple[ScanProgress, str]:
    """Return (progress, why_it_is_empty).

    `why` is a sentence for the user when a checkpoint existed but was not
    used, and "" when it was used or when there was none. Never raises: a
    checkpoint that cannot be read is a checkpoint that does not exist, and a
    scan must not be blocked by it.
    """
    p = ScanProgress(path, fingerprint_=fingerprint_, run=run)
    try:
        raw = json.loads(Path(p.path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return p, ""
    except (OSError, ValueError):
        return p, "the saved scan progress could not be read, so this is a fresh scan"

    if not isinstance(raw, dict) or not isinstance(raw.get("done"), list):
        return p, "the saved scan progress was not the right shape, so this is a fresh scan"

    if raw.get("fingerprint") != fingerprint_:
        return p, ("your sources or titles changed since that scan, so there "
                   "is nothing safe to resume and this is a fresh scan")

    stamp = raw.get("updated") or raw.get("started") or ""
    try:
        when = datetime.fromisoformat(stamp)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - when).total_seconds() / 3600
    except ValueError:
        age = None
    if age is None:
        return p, "the saved scan progress had no usable date, so this is a fresh scan"
    if age > max_age_hours:
        return p, (f"the saved scan progress is {age:.0f} hours old, past the "
                   f"{max_age_hours}-hour limit, so this is a fresh scan")

    p._done = {str(x) for x in raw["done"]}
    p.started = raw.get("started") or p.started
    p.run = raw.get("run") or run
    return p, ""
