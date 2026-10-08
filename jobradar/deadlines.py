"""When a posting closes, read from its own words, and left blank when unclear.

Most boards do not send a closing date as a field. Some put it in the text:
"Closing Date for Applications: Friday 23rd October 2026". Reading it matters
because the cost of missing one is an application that cannot be made, and the
cost of misreading one is worse: a deadline moved a month by a swapped
day and month looks exactly like a correct one. So the rule is the repo's rule.
A date is returned only when the text says it closes AND the date can be read
one way. Anything else is `None`, which means "nothing here can say".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_MON = "|".join(MONTHS)

# The words that make a nearby date a closing date. A bare date is a posting
# date, a founding date or a start date, and must never be read as a deadline.
#
# Two strengths. "until" and "by end of" also introduce contract end dates and
# notice periods, so a date near one is only used when no clearer cue found
# one: otherwise "Closing date: 17th April 2026 ... contract running until 31
# December 2026" is read as closing in December, which looks like a deadline
# and is eight months wrong.
#
# "close" and "closes" on their own are not cues. "Join a close-knit team.
# Posted 1 September 2026" stored 1 September as a deadline, and "close
# partnership with the CFO ... Start date: 5 January 2027" stored the start
# date. "closes"/"closing" count only as "closes on", "closes at", "closes:"
# or directly before a date; and no cue counts when a hyphen joins it to the
# next word ("close-knit", "deadline-driven").
_THEN_DATE = r"(?=\s+(?:on\s+)?(?:\d|(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b))"
_STRONG = (r"(?:closing\s+date|closes\s+(?:on|at)\b|closes\s*:|closing\s*:|close\s+on\b|"
           rf"closes{_THEN_DATE}|closing{_THEN_DATE}|deadline|apply\s+by|"
           r"applications?\s+(?:close|must\s+be)|posting\s+period)(?!-\w)")
_WEAK = r"(?:until|by\s+(?:cob|end\s+of))(?!-\w)"
_CUE = re.compile(rf"\b(?:{_STRONG}|{_WEAK})", re.I)
_STRONG_CUE = re.compile(rf"\b{_STRONG}", re.I)
# Between the two dates of an explicit range: "23/09/2026 - 09/10/2026",
# "1 October to 30 October".
_RANGE_JOIN = re.compile(r"^\s*(?:-|\u2013|to|until|till|through)\s*$", re.I)

_LONG = re.compile(
    rf"(?:(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?,?\s+)?"
    rf"(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m>{_MON})\s*,?\s*(?P<y>\d{{4}})?",
    re.I)
_US = re.compile(
    rf"(?P<m>{_MON})\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(?P<y>\d{{4}})", re.I)
_NUM = re.compile(r"\b(?P<a>\d{1,2})[/.\-](?P<b>\d{1,2})[/.\-](?P<y>\d{4})\b")
# YYYY-MM-DD reads one way only. It was neither read nor counted ambiguous:
# "Closing Date: 2026-10-23" came back as no deadline at all.
_ISO = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")

# How far after a cue a date still belongs to it.
_WINDOW = 60

# A date with no year is read as the next occurrence on or after a week before
# the reference day (the day the posting was first seen). One that lands
# further ahead than this is not read at all: "Closing date: 30th September"
# on a posting first seen on 8 October is a posting that had already closed,
# and rolling it to September of the next year stored a deadline eleven
# months out that rendered exactly like a real one. Half a year is longer
# than any posting window seen in this data.
YEARLESS_MAX_AHEAD = 183


@dataclass(frozen=True)
class ClosingDate:
    iso: str
    evidence: str


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _day_first_document(text: str) -> bool | None:
    """True if some numeric date can only be day-first, False if only
    month-first, None if every numeric date is ambiguous."""
    for m in _NUM.finditer(text):
        a, b = int(m["a"]), int(m["b"])
        if a > 12 and b <= 12:
            return True
        if b > 12 and a <= 12:
            return False
    return None


def _dates_after(window: str, offset: int, convention: bool | None,
                 today: date) -> list[tuple[int, int, date, str]]:
    """Every readable date in one cue's window as (start, end, date, words),
    in the order they appear."""
    found: list[tuple[int, int, date, str]] = []
    # Long forms first: "23rd October 2026" is unambiguous.
    for rx in (_LONG, _US):
        for m in rx.finditer(window):
            mon = MONTHS[m["m"].lower()]
            day = int(m["d"])
            year = int(m["y"]) if m["y"] else None
            if year is None:
                cand = _mk(today.year, mon, day)
                if cand and cand < today - timedelta(days=7):
                    cand = _mk(today.year + 1, mon, day)
                if cand and (cand - today).days > YEARLESS_MAX_AHEAD:
                    cand = None               # which year is a guess: left blank
            else:
                cand = _mk(year, mon, day)
            if cand:
                found.append((offset + m.start(), offset + m.end(), cand, m.group(0).strip()))
    for m in _ISO.finditer(window):
        cand = _mk(int(m["y"]), int(m["m"]), int(m["d"]))
        if cand:
            found.append((offset + m.start(), offset + m.end(), cand, m.group(0)))
    for m in _NUM.finditer(window):
        if convention is None:
            continue                      # ambiguous: unknown, not a guess
        a, b, y = int(m["a"]), int(m["b"]), int(m["y"])
        cand = _mk(y, b, a) if convention else _mk(y, a, b)
        if cand:
            found.append((offset + m.start(), offset + m.end(), cand, m.group(0)))
    found.sort(key=lambda t: t[0])
    return found


def _dates_near(text: str, cues: re.Pattern, convention: bool | None,
                today: date) -> list[tuple[int, date, str]]:
    """One date per cue: the first one after it.

    It used to be the latest date anywhere in the window, which read "Closing
    date: 23 October 2026. Interviews: 6 November 2026." as closing on the
    interview date. The latest date is right only for an explicit range, two
    dates joined by a dash or "to", which is the one case kept.
    """
    found: list[tuple[int, date, str]] = []
    for cue in cues.finditer(text):
        dates = _dates_after(text[cue.start(): cue.end() + _WINDOW], cue.start(),
                             convention, today)
        if not dates:
            continue
        start, end, when, ev = dates[0]
        for nxt in dates[1:]:
            if nxt[0] >= end and _RANGE_JOIN.match(text[end:nxt[0]]):
                start, end, when, ev = nxt
            else:
                break
        found.append((start, when, ev))
    return found


def closing_date(text: str | None, today: date) -> ClosingDate | None:
    if not text:
        return None
    convention = _day_first_document(text)
    for cues in (_STRONG_CUE, _CUE):
        found = _dates_near(text, cues, convention, today)
        if found:
            break
    if not found:
        return None
    # Two cues naming two different dates ("Closing date: 23 October ...
    # Deadline for references: 6 November") cannot be told apart by position
    # alone, so neither is chosen. `has_closing_cue_with_unreadable_date`
    # counts it as ambiguous, which is what it is.
    if len({when for _, when, _ in found}) > 1:
        return None
    _, when, ev = found[0]
    return ClosingDate(when.isoformat(), ev)


def has_closing_cue_with_unreadable_date(text: str | None,
                                         today: date | None = None) -> bool:
    """True when the text says something closes and puts a date next to it, and
    `closing_date` still could not read one.

    This is the difference between "this posting has no deadline in it" and "it
    has one and it was left blank": the first is the common case and needs no
    attention, the second is a deadline a person may be missing.
    """
    if not text or closing_date(text, today or date.today()) is not None:
        return False
    for cue in _CUE.finditer(text):
        window = text[cue.start(): cue.end() + _WINDOW]
        if (_LONG.search(window) or _US.search(window) or _NUM.search(window)
                or _ISO.search(window)):
            return True
    return False


def caption(closes_on: str | None, status: str, today: date) -> str:
    """The line `list` prints for a role's closing date, or "" for none.

    A settled role says nothing: its deadline no longer matters. A deadline
    that has passed on a role nothing was sent for says so, because that is the
    one a person might still be planning to apply to.
    """
    from .store import IN_FLIGHT, SETTLED
    if not closes_on or status in SETTLED:
        return ""
    try:
        when = date.fromisoformat(closes_on)
    except ValueError:
        return f"closing date unreadable: {closes_on!r}"
    days = (when - today).days
    if days > 0:
        return f"closes {closes_on} ({days} day{'s' if days != 1 else ''})"
    if days == 0:
        return f"closes {closes_on} (today)"
    ago = f"{-days} day{'s' if days != -1 else ''} ago"
    tail = "" if status in IN_FLIGHT else ", and not applied"
    return f"closed {closes_on} ({ago}{tail})"
