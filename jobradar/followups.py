"""Applications that have gone quiet, and a draft to chase each one.

Twenty-five applications with no reply is a list nobody keeps in their head.
This makes it one, and writes a short note for each that can fairly be chased.

It cannot send. The module imports no mail, network or process code (a test
reads its imports to prove it), so the worst a mistake here can do is write a
text file somebody then has to open and decide about. The draft says only what
the application record says: the role, the company, the day and the route. It
makes no claim about the person, because a chasing note is the easiest place
to say something nobody checked.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import store

# A reply of any kind means the application has not gone quiet. A note about
# which CV was sent and a draft written by this module are not replies.
_NOT_A_REPLY = ("applied", "followup_drafted", "note")
MAX_DRAFTS = 2
PLACEHOLDER_NAME = "[your name]"


def _day(value: str, uid: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"the application for role {uid} has applied_on {value!r}, "
                         f"which is not a date") from None


def quiet(con, today: date, days: int = 10) -> list[dict]:
    """Applications with nothing after the application itself."""
    out = []
    marks = ",".join("?" * len(_NOT_A_REPLY))
    for a in con.execute(
            "SELECT a.uid, MAX(a.applied_on) AS applied_on, a.route, a.contact, a.source, "
            "r.company, r.title, r.closes_on, s.status FROM applications a "
            "JOIN roles r ON r.uid=a.uid JOIN role_state s ON s.uid=a.uid "
            "WHERE s.status IN ('applied','submitted') GROUP BY a.uid "
            "ORDER BY applied_on, r.company").fetchall():
        later = con.execute(
            f"SELECT 1 FROM app_events WHERE uid=? AND at>=? AND kind NOT IN ({marks}) "
            f"LIMIT 1", (a["uid"], a["applied_on"], *_NOT_A_REPLY)).fetchone()
        silent = (today - _day(a["applied_on"], a["uid"])).days
        if later or silent < days:
            continue
        out.append({**dict(a), "silent_days": silent,
                    "drafted": con.execute(
                        "SELECT COUNT(*) FROM app_events WHERE uid=? AND kind='followup_drafted'",
                        (a["uid"],)).fetchone()[0]})
    return out


def reason_no_draft(item: dict, today: date) -> str | None:
    """Why this application is listed and not drafted, or None if it can be."""
    if item["drafted"] >= MAX_DRAFTS:
        return "two follow-ups already drafted; leave it"
    closes = item.get("closes_on") or ""
    if closes and closes >= today.isoformat():
        # Chasing before the board closes helps nobody: the reader is still
        # collecting applications, not reading them.
        return f"board still open until {closes}"
    if not (item.get("contact") or "").strip():
        return "no contact on file, nothing to draft"
    return None


def _first_name(contact: str) -> str:
    plain = re.sub(r"<.*?>", "", contact or "").strip()
    first = plain.split(" ")[0] if plain else ""
    # A bare address is not a name; "Hi wren@x.example," would read as a mistake.
    return first if first and "@" not in first else "there"


def draft(item: dict, name: str | None) -> str:
    # No route on file is no route in the draft. It used to say "through your
    # careers page", a claim the record does not hold, sent to an employer.
    via = f" through {item['route']}" if (item["route"] or "").strip() else ""
    # An estimated date is when a status last changed, and a draft that tells
    # an employer that day as the day it went in is a claim nobody checked.
    on = ("" if store.is_estimated(item.get("source"))
          else f" on {item['applied_on']}")
    return (f"Hi {_first_name(item['contact'])},\n\n"
            f"I applied for the {item['title']} role at {item['company']}{on}{via} and "
            f"wanted to check it had reached the right person. "
            f"I am still keen on it, and happy to send anything that would help.\n\n"
            f"Thanks,\n{name or PLACEHOLDER_NAME}\n")


def _slug(*parts: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", " ".join(parts).lower()).strip("-")[:60]


def write_drafts(con, items: list[dict], out_dir: Path, today: date,
                 name: str | None) -> list[Path]:
    """Write a text file per draftable application and record that it was
    drafted. A file that already exists is left alone and not counted again."""
    written = []
    out_dir = Path(out_dir)
    for item in items:
        if reason_no_draft(item, today):
            continue
        path = out_dir / f"followup-{_slug(item['company'], item['title'])}-{item['uid'][:6]}.md"
        if path.exists():
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(draft(item, name), encoding="utf-8")
        store.add_event(con, item["uid"], "followup_drafted", at=today.isoformat(),
                        detail=path.name, source="followups")
        written.append(path)
    return written
